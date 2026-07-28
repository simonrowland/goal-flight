"""A fresh install must get a capacity profile with honest provenance.

Before this, nothing in the installer, init, or doctor created
~/.goal-flight/capacity.local.json, so every new machine silently ran on the
committed baseline while the tuned values lived in one hand-written file.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_agent_limits as limits  # noqa: E402
import goalflight_doctor as doctor  # noqa: E402


def test_hard_cap_follows_the_memory_algorithm(monkeypatch):
    monkeypatch.setattr(limits, "_system_memory_mb", lambda: 128 * 1024)
    # (131072 - 3072) / 400
    assert limits.recommended_hard_cap() == 320

    monkeypatch.setattr(limits, "_system_memory_mb", lambda: 16 * 1024)
    assert limits.recommended_hard_cap() == (16 * 1024 - 3072) // 400


def test_unknown_memory_falls_back_to_the_conservative_baseline(monkeypatch):
    """An unreadable machine gets the committed default, never a guess."""
    monkeypatch.setattr(limits, "_system_memory_mb", lambda: None)
    assert limits.recommended_hard_cap(40) == 40
    monkeypatch.setattr(limits, "_system_memory_mb", lambda: 1024)  # less than reserve
    assert limits.recommended_hard_cap(40) == 40


def test_seed_plants_committed_baseline_not_this_machines_tuning(
    monkeypatch,
    tmp_path,
):
    """LOCAL_OVERRIDES are merged into DEFAULT_AGENT_CAPS in place at import, so
    seeding from that dict would copy the seeding machine's hand-tuned values
    onto every fresh install."""
    override = tmp_path / "capacity.override.json"
    override.write_text(json.dumps({"agent_caps": {"codex": 999}}))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", str(override))
    spec = importlib.util.spec_from_file_location(
        "goalflight_agent_limits_with_override",
        REPO_ROOT / "scripts" / "goalflight_agent_limits.py",
    )
    assert spec is not None and spec.loader is not None
    overridden_limits = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(overridden_limits)
    assert (
        overridden_limits.DEFAULT_AGENT_CAPS
        != overridden_limits.COMMITTED_AGENT_CAPS
    )
    assert overridden_limits.DEFAULT_AGENT_CAPS["codex"] == 999

    target = tmp_path / "capacity.local.json"
    result = overridden_limits.seed_capacity_conf(target)
    assert result["status"] == "created"

    profile = json.loads(target.read_text())
    assert profile["agent_caps"] == overridden_limits.COMMITTED_AGENT_CAPS
    assert profile["hard_cap"] == profile["operating_total"]
    assert "reserve" in profile["_comment"] and "per agent" in profile["_comment"]


def test_doctor_seeds_the_configured_capacity_path(monkeypatch, tmp_path):
    """The no-argument doctor path must use the same configured path as loading."""
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "configured" / "capacity.local.json"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", str(target))
    monkeypatch.setattr(limits, "_system_memory_mb", lambda: 16 * 1024)

    result = doctor.check_capacity_profile()

    assert result["ok"] is True
    assert result["status"] == "created"
    assert result["path"] == str(target)
    assert target.is_file()
    assert not (home / ".goal-flight" / "capacity.local.json").exists()


def test_seed_never_clobbers_an_existing_profile(tmp_path):
    """The file is the operator's tuning record; overwriting it would discard
    measurements the committed defaults cannot reproduce."""
    target = tmp_path / "capacity.local.json"
    target.write_text(json.dumps({"hard_cap": 7, "agent_caps": {"grok": 1}}))
    result = limits.seed_capacity_conf(target)
    assert result["status"] == "exists"
    assert json.loads(target.read_text())["hard_cap"] == 7

    forced = limits.seed_capacity_conf(target, force=True)
    assert forced["status"] == "created"


def test_seed_reports_an_unwritable_target_instead_of_raising(tmp_path):
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("file, not a directory")
    result = limits.seed_capacity_conf(blocked / "sub" / "capacity.local.json")
    assert result["status"] == "error" and "error" in result
