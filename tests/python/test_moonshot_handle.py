#!/usr/bin/env python3
"""Contract tests for the moonshot handle rename (the kimi handle is retired).

The dispatch handle for the direct Moonshot/kimi-CLI lane is ``moonshot``.
``kimi`` is retired as INPUT: dispatch preset validation rejects it with the
normal unknown-agent error (which lists the valid presets), and capacity
acquire refuses it outright. Records written before the rename still carry
``agent: "kimi"`` across every fleet ledger; record-reading paths map that
value onto the moonshot family via goalflight_agent_limits so history keeps
rendering, reconciling, and accounting exactly as before.
"""

from __future__ import annotations

import datetime as dt
import importlib
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_agent_limits as limits  # noqa: E402
import goalflight_capacity as cap  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_status as status  # noqa: E402
import goalflight_watch as watch  # noqa: E402


def _fresh_limits(monkeypatch) -> object:
    """Reload limits against an empty capacity conf so a machine-local
    ~/.goal-flight/capacity.local.json cannot skew the baseline assertions."""
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    return importlib.reload(limits)


# ----- the one-line legacy mapping and family helper -----


def test_canonical_agent_label_maps_only_the_retired_handle() -> None:
    assert limits.canonical_agent_label("kimi") == "moonshot"
    assert limits.canonical_agent_label("Kimi") == "moonshot"  # normalize, then map
    assert limits.canonical_agent_label("moonshot") == "moonshot"
    assert limits.canonical_agent_label("codex") == "codex"
    assert limits.canonical_agent_label("") == ""
    assert limits.canonical_agent_label(None) == ""


def test_moonshot_family_membership() -> None:
    assert limits.moonshot_family("moonshot")
    assert limits.moonshot_family("kimi")  # legacy record value
    assert not limits.moonshot_family("codex")
    assert not limits.moonshot_family("grok-code")
    assert not limits.moonshot_family("")
    assert not limits.moonshot_family(None)


def test_pool_renamed_and_unified(monkeypatch) -> None:
    fresh = _fresh_limits(monkeypatch)
    # The pool carries the new handle, with the same numbers the old handle had.
    assert fresh.DEFAULT_AGENT_CAPS["moonshot"] == 6
    assert "kimi" not in fresh.DEFAULT_AGENT_CAPS
    assert fresh.AGENT_RSS_MB["moonshot"] == 386
    assert "kimi" not in fresh.AGENT_RSS_MB
    # ...and a legacy "kimi" lease accounts against the SAME pool, so pre-rename
    # leases draw down post-rename capacity (continuous accounting, no dual pool).
    assert fresh.cap_pool("moonshot") == "moonshot"
    assert fresh.cap_pool("kimi") == "moonshot"


# ----- input boundary: the retired handle is refused -----


def _dispatch_args(**overrides) -> SimpleNamespace:
    base = {
        "agent": "codex",
        "read_only": False,
        "prompt": "COMPLETE: no-op",
        "prompt_file": None,
        "cwd": None,
        "ignore_git_warn": False,
        "model": None,
        "max_idle_secs": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_retired_handle_fails_preset_validation_with_preset_list() -> None:
    with pytest.raises(D.DispatchUsageError) as excinfo:
        D._validate_before_side_effects(_dispatch_args(agent="kimi"), [])
    message = str(excinfo.value)
    assert "no worker preset" in message
    # The unknown-agent error IS the migration mechanism: it must list the
    # valid presets, including moonshot, and must not offer the retired one.
    assert "moonshot" in message
    assert "grok-research|kimi" not in message


def test_moonshot_passes_preset_validation() -> None:
    assert "moonshot" in D.PRESET_AGENTS
    assert "kimi" not in D.PRESET_AGENTS
    # No raise: preset check, stdin-prompt check, and the sandbox posture
    # (default off, the only profile the kimi binary lane supports) all accept.
    D._validate_before_side_effects(_dispatch_args(agent="moonshot"), [])


def test_retired_handle_cannot_acquire_capacity(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cap.main(["acquire", "--agent", "kimi", "--lease-id", "retired-lease"])
    assert rc == 2
    payload = json.loads(out.getvalue())
    assert payload["decision"] == "error"
    assert "retired" in payload["reason"]
    assert "moonshot" in payload["reason"]
    # The refusal is side-effect free: no lease file was written.
    assert not cap.state_path().exists() or "retired-lease" not in json.loads(
        cap.state_path().read_text()
    ).get("leases", {})


def test_legacy_kimi_lease_occupies_the_moonshot_pool(tmp_path, monkeypatch) -> None:
    """Proof the two labels share ONE pool: an active pre-rename lease (agent
    "kimi") fills the moonshot agent cap for a new acquire."""
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_RATE_PRESSURE_THRESHOLD", "3")
    monkeypatch.setenv("GOALFLIGHT_RATE_PRESSURE_WINDOW_SECONDS", "600")
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        legacy_lease = {
            "lease_id": "legacy-kimi-lease",
            "agent": "kimi",
            "state": "active",
            "worker_pid": worker.pid,
            "controller_pid": os.getpid(),
            "claimant_pid": os.getpid(),
            "mem_mb": 386,
            "started_at": cap.iso(cap.utc_now()),
            "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
        }
        cap.save_state(
            {
                "schema": cap.SCHEMA,
                "machine_id": cap.machine_id(),
                "leases": {legacy_lease["lease_id"]: legacy_lease},
                "cooldowns": {},
            }
        )

        out = io.StringIO()
        with redirect_stdout(out):
            rc = cap.main(
                [
                    "acquire",
                    "--agent",
                    "moonshot",
                    "--agent-cap",
                    "1",
                    "--lease-id",
                    "new-moonshot-lease",
                    "--ram-mb",
                    "65536",
                    "--max-total",
                    "20",
                ]
            )
        assert rc == 2
        payload = json.loads(out.getvalue())
        assert payload["decision"] == "wait"
        assert payload["reason"] == "agent_worker_cap"
        # The one active lease counted against the moonshot pool is the legacy one.
        assert payload["active"] == 1

        # Control: a different pool is unaffected by the legacy lease.
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cap.main(
                [
                    "acquire",
                    "--agent",
                    "codex",
                    "--agent-cap",
                    "1",
                    "--lease-id",
                    "control-codex-lease",
                    "--ram-mb",
                    "65536",
                    "--max-total",
                    "20",
                ]
            )
        assert rc == 0, out.getvalue()
    finally:
        worker.kill()
        worker.wait()


# ----- record-reading boundary: legacy records reconcile exactly as before -----


def test_legacy_record_drives_kimi_marker_dialect(tmp_path) -> None:
    """The watcher classifies legacy and current Moonshot output identically."""
    tail = tmp_path / "worker.tail"
    tail.write_text("• COMPLETE: legacy record marker\n", encoding="utf-8")

    legacy = watch._last_line_is_terminal_marker(
        tail, ignore_prefix_lines=0, kimi_output=limits.moonshot_family("kimi")
    )
    assert legacy is not None and legacy["kind"] == "COMPLETE"

    current = watch._last_line_is_terminal_marker(
        tail, ignore_prefix_lines=0, kimi_output=limits.moonshot_family("moonshot")
    )
    assert current is not None and current["kind"] == "COMPLETE"

    other = watch._last_line_is_terminal_marker(
        tail, ignore_prefix_lines=0, kimi_output=limits.moonshot_family("codex")
    )
    assert other is None
