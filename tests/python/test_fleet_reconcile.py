#!/usr/bin/env python3
"""Tests for fleet dispatch reconcile decision table (Track A goal 10b)."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_reconcile as fleet_reconcile
import goalflight_fleet_status as status

FIXTURES = ROOT / "tests" / "fixtures" / "fleet_mirrors"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _fixture_fleet(fleet_dir: Path) -> None:
    fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    fleet_doc["nodes"] = {
        "localhost": {
            "node_id": "localhost",
            "status": "active",
            "ssh": {"alias": "localhost", "hostname": "localhost"},
            "repo_root": str(ROOT),
            "state_dir": "/tmp/goal-flight-test",
            "billing_accounts": [],
            "added_at": "2026-05-24T12:00:00+00:00",
        }
    }
    fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)


def _write_dispatch(
    fleet_dir: Path,
    *,
    dispatch_id: str,
    mirror_name: str | None,
    meta: dict,
) -> None:
    dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    if mirror_name:
        shutil.copy(FIXTURES / mirror_name, dispatch_dir / "status.json")
    fleet._atomic_write_json(dispatch_dir / "meta.json", meta)
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


def _acquire_lock(fleet_dir: Path, dispatch_id: str, account_key: str = "openai/default") -> dict:
    return fleet.acquire_account_lock(
        fleet_dir,
        account_key=account_key,
        owner_dispatch_id=dispatch_id,
    )


def test_ssh_down_quarantine_no_release() -> None:
    dispatch_id = "acp-reconcile-partition"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _acquire_lock(fleet_dir, dispatch_id)
        _write_dispatch(
            fleet_dir,
            dispatch_id=dispatch_id,
            mirror_name="valid_ok.json",
            meta={
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "lease_active": True,
                "pid_hint": "unknown",
                "ssh_reachable": False,
            },
        )
        row = fleet_reconcile.reconcile_dispatch(
            fleet_dir,
            dispatch_id,
            mutate=True,
            ssh_reachable=False,
        )
        assert_true("quarantine", row.action == "quarantine")
        assert_true("not released", row.released is False)
        lock = fleet_reconcile.find_account_lock_for_dispatch(fleet_dir, dispatch_id)
        assert_true("lock held", lock is not None and lock.get("state") == "active")


def test_terminal_dead_pid_release_once() -> None:
    dispatch_id = "acp-reconcile-terminal"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = _acquire_lock(fleet_dir, dispatch_id)
        terminal = json.loads((FIXTURES / "valid_ok.json").read_text())
        terminal["state"] = "complete"
        dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        (dispatch_dir / "status.json").write_text(json.dumps(terminal) + "\n")
        fleet._atomic_write_json(
            dispatch_dir / "meta.json",
            {
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "lease_active": True,
                "pid_hint": "dead",
                "ssh_reachable": True,
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
        first = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)
        assert_true("release action", first.action == "release_locks")
        assert_true("released", first.released is True)
        second = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)
        assert_true("idempotent second pass", second.released is False)
        released = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock released", released is None or released.get("state") == "released")
        assert_true("fencing was", lock.get("fencing_token"))


def test_audit_log_appended_on_mutate() -> None:
    dispatch_id = "acp-reconcile-audit"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _acquire_lock(fleet_dir, dispatch_id)
        terminal = json.loads((FIXTURES / "valid_ok.json").read_text())
        terminal["state"] = "complete"
        dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        (dispatch_dir / "status.json").write_text(json.dumps(terminal) + "\n")
        fleet._atomic_write_json(
            dispatch_dir / "meta.json",
            {
                "dispatch_id": dispatch_id,
                "lease_active": True,
                "pid_hint": "dead",
                "ssh_reachable": True,
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
        fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)
        audit_path = fleet_reconcile.reconcile_audit_path(fleet_dir)
        assert_true("audit exists", audit_path.exists())
        lines = audit_path.read_text().strip().splitlines()
        assert_true("one line", len(lines) == 1)
        entry = json.loads(lines[0])
        assert_true("dispatch id", entry.get("dispatch_id") == dispatch_id)


def test_live_ssh_probe_quarantine_when_unreachable() -> None:
    dispatch_id = "acp-reconcile-live-probe"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _acquire_lock(fleet_dir, dispatch_id)
        _write_dispatch(
            fleet_dir,
            dispatch_id=dispatch_id,
            mirror_name="valid_ok.json",
            meta={
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "lease_active": True,
                "pid_hint": "unknown",
            },
        )

        def fail_runner(_argv: list[str]) -> tuple[int, str, str]:
            return 255, "", "connection refused"

        row = fleet_reconcile.reconcile_dispatch(
            fleet_dir,
            dispatch_id,
            mutate=True,
            ssh_runner=fail_runner,
        )
        assert_true("quarantine", row.action == "quarantine")
        assert_true("partition reason", row.reason == "ssh_partition")
        lock = fleet_reconcile.find_account_lock_for_dispatch(fleet_dir, dispatch_id)
        assert_true("lock held", lock is not None and lock.get("state") == "active")


def main() -> None:
    test_ssh_down_quarantine_no_release()
    test_terminal_dead_pid_release_once()
    test_audit_log_appended_on_mutate()
    test_live_ssh_probe_quarantine_when_unreachable()
    print("OK: fleet reconcile tests pass")


if __name__ == "__main__":
    main()
