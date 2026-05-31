#!/usr/bin/env python3
"""B1 partition stub integration (Track A goal 10e)."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("fleet partition fixture uses POSIX /tmp paths")

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import fleet_ssh_stub
import goalflight_fleet as fleet
import goalflight_fleet_reconcile as fleet_reconcile
import goalflight_fleet_watch as fleet_watch

FIXTURES = ROOT / "tests" / "fixtures" / "fleet_mirrors"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _setup(fleet_dir: Path, dispatch_id: str) -> None:
    fleet.bootstrap(fleet_dir)
    fleet.acquire_account_lock(
        fleet_dir,
        account_key="openai/default",
        owner_dispatch_id=dispatch_id,
    )
    dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "valid_ok.json", dispatch_dir / "status.json")
    fleet._atomic_write_json(
        dispatch_dir / "meta.json",
        {
            "dispatch_id": dispatch_id,
            "node_id": "localhost",
            "lease_active": True,
            "pid_hint": "alive",
            "ssh_reachable": True,
            "remote_status_path": "/tmp/remote/status.json",
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


def test_b1_partition_no_release_on_flap() -> None:
    dispatch_id = "acp-b1-flap"
    running = fleet_ssh_stub.status_json(dispatch_id=dispatch_id, seq=4, state="running")
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _setup(fleet_dir, dispatch_id)
        scenario = fleet_ssh_stub.FleetSshStubScenario(
            name="flap",
            responses=[(0, running, ""), (0, running, "")],
            flap_after=1,
            flap_duration_calls=2,
        )
        transport = fleet_watch.SshFleetWatchTransport(runner=scenario.runner())
        fleet_watch.sync_fleet_mirrors(fleet_dir, transport)
        row = fleet_reconcile.reconcile_dispatch(
            fleet_dir,
            dispatch_id,
            mutate=True,
            ssh_reachable=False,
        )
        assert_true("quarantine on partition", row.action == "quarantine")
        assert_true("no release", row.released is False)
        lock = fleet_reconcile.find_account_lock_for_dispatch(fleet_dir, dispatch_id)
        assert_true("lock held", lock is not None and lock.get("state") == "active")


def test_b1_restore_consistent_running() -> None:
    dispatch_id = "acp-b1-restore"
    running = fleet_ssh_stub.status_json(dispatch_id=dispatch_id, seq=5, state="running")
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _setup(fleet_dir, dispatch_id)
        transport = fleet_watch.SshFleetWatchTransport(
            runner=lambda _argv: (0, running, ""),
        )
        fleet_watch.sync_fleet_mirrors(fleet_dir, transport)
        ctx = fleet_reconcile.build_dispatch_context(fleet_dir, dispatch_id, ssh_reachable=True)
        assert_true("running", ctx.classification.state == "running")


def main() -> None:
    test_b1_partition_no_release_on_flap()
    test_b1_restore_consistent_running()
    print("OK: fleet B1 partition stub tests pass")


if __name__ == "__main__":
    main()
