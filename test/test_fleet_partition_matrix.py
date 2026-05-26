#!/usr/bin/env python3
"""C1 partition matrix with scaled timing (Track A goal 12)."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "test" / "fixtures"))

import fleet_ssh_stub
import goalflight_fleet as fleet
import goalflight_fleet_reconcile as fleet_reconcile
import goalflight_fleet_watch as fleet_watch

FIXTURES = ROOT / "test" / "fixtures" / "fleet_mirrors"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _seed(fleet_dir: Path, dispatch_id: str, *, state: str = "running", pid_hint: str = "alive") -> None:
    fleet.bootstrap(fleet_dir)
    fleet.acquire_account_lock(
        fleet_dir,
        account_key="openai/default",
        owner_dispatch_id=dispatch_id,
    )
    dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads((FIXTURES / "valid_ok.json").read_text())
    payload["state"] = state
    (dispatch_dir / "status.json").write_text(json.dumps(payload) + "\n")
    fleet._atomic_write_json(
        dispatch_dir / "meta.json",
        {
            "dispatch_id": dispatch_id,
            "node_id": "localhost",
            "lease_active": True,
            "pid_hint": pid_hint,
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


def scenario_partition_mid_run() -> None:
    dispatch_id = "c1-mid-run"
    running = fleet_ssh_stub.status_json(dispatch_id=dispatch_id, seq=3, state="running")
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _seed(fleet_dir, dispatch_id)
        transport = fleet_watch.SshFleetWatchTransport(
            runner=fleet_ssh_stub.FleetSshStubScenario(
                "mid",
                responses=[(0, running, "")],
                flap_after=0,
                flap_duration_calls=1,
            ).runner()
        )
        fleet_watch.sync_fleet_mirrors(fleet_dir, transport)
        row = fleet_reconcile.reconcile_dispatch(
            fleet_dir,
            dispatch_id,
            mutate=True,
            ssh_reachable=False,
        )
        assert_true("no release mid partition", row.released is False)


def scenario_complete_during_partition() -> None:
    dispatch_id = "c1-complete-partition"
    complete = fleet_ssh_stub.status_json(dispatch_id=dispatch_id, seq=8, state="complete")
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _seed(fleet_dir, dispatch_id, state="complete", pid_hint="dead")
        row = fleet_reconcile.reconcile_dispatch(
            fleet_dir,
            dispatch_id,
            mutate=True,
            ssh_reachable=False,
        )
        assert_true("partition blocks release without ssh", row.released is False)
        row2 = fleet_reconcile.reconcile_dispatch(
            fleet_dir,
            dispatch_id,
            mutate=True,
            ssh_reachable=True,
        )
        assert_true("release when ssh up + terminal", row2.released is True)


def scenario_partition_wedged_worker() -> None:
    dispatch_id = "c1-wedged"
    wedged = fleet_ssh_stub.status_json(dispatch_id=dispatch_id, seq=9, state="wedged")
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _seed(fleet_dir, dispatch_id, state="wedged", pid_hint="dead")
        first = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True, ssh_reachable=True)
        second = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True, ssh_reachable=True)
        assert_true("terminal release once", first.released is True)
        assert_true("no duplicate release", second.released is False)


def main() -> None:
    scenario_partition_mid_run()
    scenario_complete_during_partition()
    scenario_partition_wedged_worker()
    print("OK: fleet partition matrix tests pass")


if __name__ == "__main__":
    main()
