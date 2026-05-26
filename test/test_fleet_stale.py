#!/usr/bin/env python3
"""Tests for doctor fleet stale release (Track A goal 10c)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_stale as fleet_stale
import goalflight_fleet_status as status

FIXTURES = ROOT / "test" / "fixtures" / "fleet_mirrors"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _bootstrap(fleet_dir: Path) -> None:
    fleet.bootstrap(fleet_dir)


def test_doctor_will_not_release_ssh_partition() -> None:
    classification = status.DispatchClassification(
        "unknown",
        quarantine_reason=status.QUARANTINE_SSH_PARTITION,
    )
    assert_true("blocked", fleet_stale.doctor_may_release_dispatch_locks(classification) is False)


def test_running_ssh_down_report_quarantine_only() -> None:
    dispatch_id = "acp-doctor-partition"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _bootstrap(fleet_dir)
        fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id=dispatch_id,
        )
        dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        (dispatch_dir / "status.json").write_text((FIXTURES / "valid_ok.json").read_text())
        fleet._atomic_write_json(
            dispatch_dir / "meta.json",
            {
                "dispatch_id": dispatch_id,
                "lease_active": True,
                "pid_hint": "alive",
                "ssh_reachable": False,
            },
        )
        fleet._atomic_write_json(
            fleet_dir / "register" / "aggregate.json",
            {
                "schema": "goalflight.fleet.register.aggregate.v1",
                "schema_version": 1,
                "min_reader_version": 1,
                "open_user_needs": [],
                "active_dispatches": [dispatch_id],
                "last_steering": None,
            },
        )
        summary = fleet_stale.doctor_fleet_stale_release(fleet_dir, mutate=True)
        assert_true("quarantined listed", len(summary.get("dispatch_quarantined") or []) >= 1)
        lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock still active", lock and lock.get("state") == "active")


def main() -> None:
    test_doctor_will_not_release_ssh_partition()
    test_running_ssh_down_report_quarantine_only()
    print("OK: fleet stale doctor tests pass")


if __name__ == "__main__":
    main()
