"""Hermetic model-specific effort validation; no worker launches."""

import argparse
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import goalflight_dispatch as D


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "dispatch-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "wrong-home"))
    models = [
        {"slug": "limited", "priority": 2,
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
        {"slug": "capable", "priority": 1,
         "supported_reasoning_levels": [{"effort": "high"}, {"effort": "max"}]},
    ]
    (home / "models_cache.json").write_text(json.dumps({"models": models}))
    return home


def validate(home, model="capable", effort="max"):
    args = SimpleNamespace(agent="codex", model=model, reasoning_effort=effort)
    D._validate_codex_reasoning_effort(args, {"CODEX_HOME": str(home)})


def test_max_accepted_and_transmitted(codex_home):
    assert D._parse_reasoning_effort("MAX") == "max"
    validate(codex_home)
    args = SimpleNamespace(agent="codex", model="capable", reasoning_effort="max", cwd=None)
    argv, _ = D.build_worker(args, codex_home / "prompt.md", [])
    assert 'model_reasoning_effort="max"' in argv
    assert argv[argv.index("--model") + 1] == "capable"
    with pytest.raises(argparse.ArgumentTypeError):
        D._parse_reasoning_effort("typo")


def test_model_refusal_lists_actual_levels(codex_home):
    with pytest.raises(D.DispatchUsageError, match="'limited'; supported levels: high, low$"):
        validate(codex_home, "limited")
    # Even historically accepted levels must obey the model's catalog entry.
    with pytest.raises(D.DispatchUsageError, match="supported levels: high, low$"):
        validate(codex_home, "limited", "xhigh")


def test_default_model_and_explicit_override(codex_home):
    validate(codex_home, None)  # catalog priority, not array position
    config = codex_home / "config.toml"
    config.write_text('model = "limited"\n')
    with pytest.raises(D.DispatchUsageError, match="'limited'"):
        validate(codex_home, None)
    validate(codex_home, "capable")
    config.write_text('model = "capable"\nprofile = "review"\n[profiles.review]\nmodel = "limited"\n')
    with pytest.raises(D.DispatchUsageError, match="'limited'"):
        validate(codex_home, None)


def test_catalog_default_skips_hidden_model(codex_home):
    path = codex_home / "models_cache.json"
    cache = json.loads(path.read_text())
    cache["models"][0].update(priority=0, visibility="hide")
    cache["models"][1]["visibility"] = "list"
    path.write_text(json.dumps(cache))
    validate(codex_home, None)
    # Explicit selection still validates the requested model, even if hidden.
    with pytest.raises(D.DispatchUsageError, match="'limited'"):
        validate(codex_home, "limited")


@pytest.mark.parametrize("cache", [None, b"{broken", b"\xff", b"null", b"{}",
                                      b'{"models": []}', b'{"models": null}'])
def test_cache_fallback(codex_home, cache):
    path = codex_home / "models_cache.json"
    if cache is None:
        path.unlink()
    else:
        path.write_bytes(cache)
    validate(codex_home, effort="high")
    with pytest.raises(D.DispatchUsageError, match="supported levels: high, low, medium, xhigh; using fallback static set"):
        validate(codex_home)


def test_unknown_model_and_unreadable_cache(codex_home):
    with pytest.raises(D.DispatchUsageError, match="fallback static set"):
        validate(codex_home, "unknown")
    path = codex_home / "models_cache.json"
    path.unlink()
    path.mkdir()  # deterministic OSError, even under privileged test users
    with pytest.raises(D.DispatchUsageError, match="fallback static set"):
        validate(codex_home)


def test_autoreview_matches_parser_union():
    review = runpy.run_path(str(ROOT / "autoreview/scripts/autoreview"))
    assert review["THINKING_LEVELS_BY_ENGINE"]["codex"] == D.CODEX_REASONING_EFFORTS


@pytest.mark.parametrize("agent", ["codex", "codex-acp", "worker"])
@pytest.mark.parametrize("route", [["--shape", "acp"], ["--interactive"]])
@pytest.mark.parametrize("effort", sorted(D.CODEX_REASONING_EFFORTS))
def test_acp_refuses_effort_before_launch_setup(monkeypatch, capsys, agent, route, effort):
    def unexpected_setup(*_args, **_kwargs):
        pytest.fail("unsupported ACP effort reached launch setup")

    monkeypatch.setattr(D, "_ensure_assigned_engine_session", unexpected_setup)
    assert D.main([
        "--agent", agent, *route, "--model", "gpt-5.5",
        "--reasoning-effort", effort, "--prompt", "read only",
    ]) == 64
    error = capsys.readouterr().err
    assert "--reasoning-effort requires --agent codex --shape bash" in error
    assert "without --interactive or a raw command after --" in error


@pytest.mark.parametrize("route", [
    ["--agent", "codex", "--", "codex", "exec", "read only"],
    ["--agent", "claude"],
    ["--agent", "claude", "--shape", "bash"],
    ["--agent", "grok-code"],
    ["--agent", "grok-research"],
    ["--agent", "grok-acp"],
    ["--agent", "cursor"],
    ["--agent", "moonshot"],
    ["--agent", "codex-acp"],
    ["--shape", "bash"],
])
@pytest.mark.parametrize("replay", [False, True])
def test_unsupported_routes_refuse_effort(monkeypatch, capsys, route, replay):
    def unexpected_setup(*_args, **_kwargs):
        pytest.fail("unsupported effort reached launch setup")

    monkeypatch.setattr(D, "_ensure_assigned_engine_session", unexpected_setup)
    argv = ["--reasoning-effort", "max", "--prompt", "read only", *route]
    if replay:
        argv = D._reconstruct_launch_argv(argv)
        assert D._option_value_before_worker_remainder(argv, "--reasoning-effort") == "max"
    assert D.main(argv) == 64
    assert "--reasoning-effort requires --agent codex --shape bash" in capsys.readouterr().err


@pytest.mark.parametrize("route", [[], ["--shape", "bash"]])
def test_supported_route_preserves_effort_on_replay(monkeypatch, route):
    class LaunchSetupReached(Exception):
        pass

    def stop_before_setup(args):
        assert args.reasoning_effort == "max"
        assert args.shape == "bash"
        raise LaunchSetupReached

    monkeypatch.setattr(D, "_ensure_assigned_engine_session", stop_before_setup)
    argv = D._reconstruct_launch_argv([
        "--agent", "codex", *route, "--reasoning-effort", "max", "--prompt", "read only",
    ])
    with pytest.raises(LaunchSetupReached):
        D.main(argv)


@pytest.mark.parametrize("route", [["--shape", "acp"], ["--interactive"], ["--shape", "bash"]])
def test_codex_without_effort_reaches_launch_setup(monkeypatch, route):
    class LaunchSetupReached(Exception):
        pass

    def stop_before_setup(*_args, **_kwargs):
        raise LaunchSetupReached

    monkeypatch.setattr(D, "_ensure_assigned_engine_session", stop_before_setup)
    with pytest.raises(LaunchSetupReached):
        D.main(["--agent", "codex", *route, "--prompt", "read only"])
