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
