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


@pytest.fixture
def grok_home(tmp_path):
    home = tmp_path / "grok-home"
    (home / ".grok").mkdir(parents=True)
    models = {
        "grok-4.5": {
            "info": {
                "supports_reasoning_effort": True,
                "reasoning_effort": "high",
                "reasoning_efforts": [{"id": level} for level in ("high", "medium", "low")],
            }
        },
        "grok-4.7": {
            "info": {
                "supports_reasoning_effort": True,
                "reasoning_effort": "high",
                "reasoning_efforts": [{"id": level} for level in ("xhigh", "high", "medium", "low")],
            }
        },
    }
    (home / ".grok" / "models_cache.json").write_text(
        json.dumps({"models": models})
    )
    return home


def validate_grok(home, model=None, effort="xhigh", agent="grok-code"):
    args = SimpleNamespace(agent=agent, model=model, reasoning_effort=effort)
    D._validate_grok_reasoning_effort(args, {"HOME": str(home)})


def test_max_accepted_and_transmitted(codex_home):
    assert D._parse_reasoning_effort("MAX") == "max"
    validate(codex_home)
    args = SimpleNamespace(agent="codex", model="capable", reasoning_effort="max", cwd=None)
    argv, _ = D.build_worker(args, codex_home / "prompt.md", [])
    assert 'model_reasoning_effort="max"' in argv
    assert argv[argv.index("--model") + 1] == "capable"
    with pytest.raises(argparse.ArgumentTypeError):
        D._parse_reasoning_effort("typo")


def test_grok_reasoning_effort_is_transmitted_to_cli():
    args = SimpleNamespace(
        agent="grok-code",
        model="grok-4.7",
        reasoning_effort="xhigh",
        cwd=None,
        read_only=False,
        os_sandbox=None,
        parent_dispatch_id=None,
        resume_reconstruction=False,
        engine_session_id=None,
    )
    argv, _ = D.build_worker(args, Path("prompt.md"), [])
    assert argv[argv.index("--reasoning-effort") + 1] == "xhigh"


def test_model_refusal_lists_actual_levels(codex_home):
    with pytest.raises(D.DispatchUsageError, match="'limited'; supported levels: high, low$"):
        validate(codex_home, "limited")
    # Even historically accepted levels must obey the model's catalog entry.
    with pytest.raises(D.DispatchUsageError, match="supported levels: high, low$"):
        validate(codex_home, "limited", "xhigh")


def test_grok_model_refusal_lists_actual_levels(grok_home):
    validate_grok(grok_home, effort="xhigh")
    with pytest.raises(
        D.DispatchUsageError,
        match="'grok-4.5'; supported levels: high, low, medium$",
    ):
        validate_grok(grok_home, "grok-4.5", "xhigh")


def test_grok_catalog_refusal_does_not_refresh_seat_state(monkeypatch, tmp_path):
    home = tmp_path / "grok-home"
    grok_dir = home / ".grok"
    grok_dir.mkdir(parents=True)
    (grok_dir / "auth.json").write_text("{}")
    (grok_dir / "config.toml").write_text('[ui]\npermission_mode = "always-approve"\n')
    (grok_dir / "models_cache.json").write_text(
        json.dumps(
            {
                "models": {
                    "grok-4.5": {
                        "info": {
                            "supports_reasoning_effort": True,
                            "reasoning_efforts": [{"id": "high"}],
                        }
                    }
                }
            }
        )
    )
    seat_state = tmp_path / "grok-seat-states.json"
    original = b"before\n"
    seat_state.write_bytes(original)

    def select_seat(**kwargs):
        if kwargs.get("allow_refresh", True):
            seat_state.write_bytes(b"refreshed\n")
        return "seat"

    monkeypatch.setattr(D, "_select_healthy_grok_account", select_seat)
    monkeypatch.setattr(D, "_account_home", lambda _account, _engine: home)
    args = D._build_launch_parser().parse_args(
        [
            "--agent",
            "grok-code",
            "--shape",
            "bash",
            "--model",
            "grok-4.5",
            "--reasoning-effort",
            "xhigh",
            "--prompt",
            "implement the requested change",
        ]
    )

    with pytest.raises(D.DispatchUsageError, match="not supported for Grok model"):
        D._validate_before_side_effects(args, [])
    assert seat_state.read_bytes() == original


def test_grok_catalog_selection_is_reused_by_launch(monkeypatch, grok_home):
    grok_dir = grok_home / ".grok"
    (grok_dir / "auth.json").write_text("{}")
    (grok_dir / "config.toml").write_text('[ui]\npermission_mode = "always-approve"\n')
    selected = []

    def select_seat(**_kwargs):
        selected.append(True)
        return "seat-a" if len(selected) == 1 else "seat-b"

    monkeypatch.setattr(D, "_select_healthy_grok_account", select_seat)
    monkeypatch.setattr(D, "_account_home", lambda _account, _engine: grok_home)
    args = D._build_launch_parser().parse_args(
        [
            "--agent",
            "grok-code",
            "--shape",
            "bash",
            "--reasoning-effort",
            "xhigh",
            "--prompt",
            "implement the requested change",
        ]
    )

    validated_env = D._validate_before_side_effects(args, [])
    launched_env = D._resolve_launch_account_env(args)
    assert len(selected) == 1
    assert args._grok_selected_account == "seat-a"
    assert validated_env["HOME"] == launched_env["HOME"] == str(grok_home)


def test_missing_grok_state_refuses_effort_without_refresh(monkeypatch, tmp_path):
    import grok_seats

    home = tmp_path / "grok-home"
    grok_dir = home / ".grok"
    grok_dir.mkdir(parents=True)
    (grok_dir / "auth.json").write_text("{}")
    (grok_dir / "config.toml").write_text('[ui]\npermission_mode = "always-approve"\n')
    state_path = tmp_path / "grok-seat-states.json"
    monkeypatch.setattr(grok_seats, "STATE_PATH", state_path)
    monkeypatch.setattr(D, "_account_home", lambda _account, _engine: home)
    monkeypatch.setattr(D, "_grok_account_admission_reason", lambda *_args, **_kwargs: None)

    def refresh_states(**_kwargs):
        state_path.write_text("refreshed\n")
        return {
            "version": 1,
            "updated_at": 1,
            "seats": {
                "seat": {
                    "probe_state": "usable",
                    "auth_state": "valid",
                    "used_percent": 1,
                }
            },
        }

    monkeypatch.setattr(grok_seats, "refresh_states", refresh_states)
    args = D._build_launch_parser().parse_args(
        [
            "--agent",
            "grok-code",
            "--shape",
            "bash",
            "--prompt",
            "implement the requested change",
            "--reasoning-effort",
            "xhigh",
        ]
    )

    with pytest.raises(D.DispatchUsageError, match="no usable grok seat"):
        D._validate_before_side_effects(args, [])
    assert not state_path.exists()


def test_no_eligible_grok_seat_refuses_effort_without_host_fallback(monkeypatch):
    monkeypatch.setattr(D, "_select_healthy_grok_account", lambda **_kwargs: None)
    args = D._build_launch_parser().parse_args(
        [
            "--agent",
            "grok-code",
            "--shape",
            "bash",
            "--prompt",
            "implement the requested change",
            "--reasoning-effort",
            "xhigh",
        ]
    )

    with pytest.raises(D.DispatchUsageError, match="no usable grok seat"):
        D._validate_before_side_effects(args, [])
    assert not hasattr(args, "_grok_selected_account")


def test_resume_acp_effort_refuses_before_resume_lock(monkeypatch, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("continue")
    state_root = tmp_path / "goalflight-state"
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(state_root))
    monkeypatch.setattr(D, "_resolve_launch_account_env", lambda _args: {})
    monkeypatch.setattr(D, "_validate_resume_worktree_source", lambda *_args: None)
    monkeypatch.setattr(
        D, "_refuse_launch_blocked_by_completion_authority", lambda *_args: None
    )
    monkeypatch.setattr(D, "_normalize_acp_agent", lambda _args: None)
    monkeypatch.setattr(D, "grok_selected_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(D, "_revalidate_resume_claim", lambda **_kwargs: None)
    source = {
        "record": {"worker_cwd": str(tmp_path), "project_root": str(tmp_path)},
        "engine": "grok",
        "session_id": "session-1",
    }
    candidate = [
        "--agent",
        "grok-code",
        "--shape",
        "acp",
        "--prompt-file",
        str(prompt),
        "--parent-dispatch-id",
        "parent-1",
        "--engine-session-id",
        "session-1",
        "--cwd",
        str(tmp_path),
        "--reasoning-effort",
        "xhigh",
    ]
    preflight = None
    try:
        with pytest.raises(D.DispatchUsageError, match="not supported for Grok ACP"):
            preflight = D._preflight_resume_dispatch(
                source,
                candidate_argv=candidate,
                dispatch_id="child-1",
            )
    finally:
        if preflight is not None:
            preflight["resume_lock"].__exit__(None, None, None)
    lock_dir = state_root / "dispatch-homes" / ".resume-locks"
    assert not lock_dir.exists() or not list(lock_dir.iterdir())


def test_grok_cache_fallback_uses_model_family(grok_home):
    cache = grok_home / ".grok" / "models_cache.json"
    cache.unlink()
    validate_grok(grok_home, "grok-4.5", "high")
    with pytest.raises(
        D.DispatchUsageError,
        match="'grok-4.5'; supported levels: high, low, medium; using fallback static set",
    ):
        validate_grok(grok_home, "grok-4.5", "xhigh")


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


@pytest.mark.parametrize("agent", sorted(D.GROK_ACP_REASONING_AGENTS))
def test_grok_acp_refuses_effort_without_session_options(
    monkeypatch, capsys, agent
):
    def unexpected_setup(*_args, **_kwargs):
        pytest.fail("unsupported Grok ACP effort reached launch setup")

    monkeypatch.setattr(D, "_ensure_assigned_engine_session", unexpected_setup)
    assert D.main([
        "--agent", agent, "--shape", "acp", "--model", "grok-4.7",
        "--reasoning-effort", "xhigh", "--prompt", "read only",
    ]) == 64
    error = capsys.readouterr().err
    assert "not supported for Grok ACP" in error
    assert "no Grok session-options hook" in error


@pytest.mark.parametrize("route", [
    ["--agent", "codex", "--", "codex", "exec", "read only"],
    ["--agent", "claude"],
    ["--agent", "claude", "--shape", "bash"],
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
    error = capsys.readouterr().err
    assert "--reasoning-effort requires --agent codex --shape bash" in error


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
