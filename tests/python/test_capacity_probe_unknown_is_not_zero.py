#!/usr/bin/env python3
"""A failed capacity probe is UNKNOWN, never a measured zero leases.

``_active_leases_for`` used to return ``[]`` on any capacity-status failure,
so ``aggregate_status`` reported ``active_capacity_leases_in_project: 0`` and
could certify a project inactive while leases were live — the false floor
that over-commits the fleet exactly when the system is unhealthy. Unknown
now refuses to certify inactivity (resource admission fails closed), while a
measured zero stays zero.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_capacity as cap  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _seed_active_lease(project: Path) -> dict:
    now = cap.utc_now()
    lease = {
        "lease_id": "probe-lease-1",
        "dispatch_id": "probe-dispatch-1",
        "agent": "codex",
        "state": "active",
        "worker_pid": None,
        "controller_pid": os.getpid(),
        "mem_mb": 386,
        "project_root": str(project),
        "started_at": cap.iso(now),
        "expires_at": cap.iso(now + dt.timedelta(hours=1)),
    }
    cap.save_state(
        {
            "schema": cap.SCHEMA,
            "machine_id": cap.machine_id(),
            "leases": {lease["lease_id"]: lease},
            "cooldowns": {},
        }
    )
    return lease


def test_failed_capacity_probe_is_unknown_and_refuses_inactive_verdict(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    lease = _seed_active_lease(project)

    # Measured path: the live lease is counted, nothing about that changes.
    leases, error = sessions._active_leases_for(project)
    assert error is None
    assert [row.get("lease_id") for row in leases or []] == [lease["lease_id"]]
    status = sessions.aggregate_status(project)
    assert status["active"] is True
    assert status["active_capacity_leases_in_project"] == 1
    assert status["capacity_probe_measured"] is True
    assert status["capacity_probe_error"] is None

    # Real precondition (b-235): chmod 000 the capacity state dir so the real
    # status subprocess fails to take its lock — no stubbed answers.
    state_dir = Path(os.environ["GOALFLIGHT_STATE_DIR"])
    os.chmod(state_dir, 0o000)
    try:
        leases, error = sessions._active_leases_for(project)
        assert leases is None, "a failed probe is UNKNOWN, not a measured zero"
        assert error

        status = sessions.aggregate_status(project)
        assert status["capacity_probe_measured"] is False
        assert status["capacity_probe_error"]
        assert status["active_capacity_leases_in_project"] is None
        assert status["active_capacity_lease_dispatch_ids"] == []
        # Resource admission: unknown capacity must not certify "inactive".
        assert status["active"] is True
        assert "capacity_leases=unknown" in sessions.to_text(status)
    finally:
        os.chmod(state_dir, 0o700)

    leases, error = sessions._active_leases_for(project)
    assert error is None
    assert [row.get("lease_id") for row in leases or []] == [lease["lease_id"]]


def test_measured_zero_leases_unchanged(tmp_path: Path) -> None:
    project = _project(tmp_path)
    leases, error = sessions._active_leases_for(project)
    assert leases == [] and error is None

    status = sessions.aggregate_status(project)
    assert status["capacity_probe_measured"] is True
    assert status["capacity_probe_error"] is None
    assert status["active_capacity_leases_in_project"] == 0
    assert status["active_capacity_lease_dispatch_ids"] == []
    assert status["active"] is False
    assert "capacity_leases=unknown" not in sessions.to_text(status)
