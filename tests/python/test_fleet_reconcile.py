#!/usr/bin/env python3
"""Tests for fleet dispatch reconcile decision table (Track A goal 10b)."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("fleet reconcile fixtures use POSIX /tmp paths")

import datetime as dt
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
ACP_FINAL_FAILURE_STATES = (
    "tool_timeout",
    "stalled",
    "remote_turn_silence",
    "failed_worktree",
)


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


def _expire_lock(fleet_dir: Path, account_key: str = "openai/default") -> None:
    path = fleet.account_lock_path(fleet_dir, account_key)
    doc = fleet.load_account_lock(path)
    assert_true("lock loaded", doc is not None)
    doc["expires_at"] = fleet.iso(fleet.utc_now() - dt.timedelta(hours=2))
    fleet._atomic_write_json(path, doc)


def _write_aggregate(fleet_dir: Path, dispatch_ids: list[str]) -> None:
    fleet._atomic_write_json(
        fleet_dir / "register" / "aggregate.json",
        {
            "schema": "goalflight.fleet.register.aggregate.v1",
            "schema_version": 1,
            "min_reader_version": 1,
            "open_user_needs": [],
            "active_dispatches": dispatch_ids,
            "last_steering": None,
        },
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


def test_terminal_mirror_live_pid_holds_lock() -> None:
    dispatch_id = "acp-reconcile-terminal-live-pid"
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
                "node_id": "localhost",
                "lease_active": True,
                "pid_hint": "alive",
                "ssh_reachable": True,
            },
        )
        _write_aggregate(fleet_dir, [dispatch_id])

        row = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)

        assert_true("live pid does not release", row.action != "release_locks")
        assert_true("not released", row.released is False)
        assert_true("may not release", row.may_release is False)
        held = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock held", held is not None and held.get("state") == "active")


def test_terminal_failed_release() -> None:
    dispatch_id = "acp-reconcile-terminal-failed"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _acquire_lock(fleet_dir, dispatch_id)
        terminal = json.loads((FIXTURES / "valid_ok.json").read_text())
        terminal["state"] = "failed"
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
        _write_aggregate(fleet_dir, [dispatch_id])

        row = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)

        assert_true("release action", row.action == "release_locks")
        assert_true("released", row.released is True)
        released = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock released", released is None or released.get("state") == "released")


def test_acp_final_failure_states_release_locks() -> None:
    for state in ACP_FINAL_FAILURE_STATES:
        dispatch_id = f"acp-reconcile-{state.replace('_', '-')}"
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            _acquire_lock(fleet_dir, dispatch_id)
            terminal = json.loads((FIXTURES / "valid_ok.json").read_text())
            terminal["state"] = state
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
            _write_aggregate(fleet_dir, [dispatch_id])

            row = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)

            assert_true(f"{state} release action", row.action == "release_locks")
            assert_true(f"{state} terminal", row.classification_state == "terminal")
            assert_true(f"{state} may release", row.may_release is True)
            assert_true(f"{state} released", row.released is True)
            released = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
            assert_true(f"{state} lock released", released is None or released.get("state") == "released")


def test_terminal_error_release() -> None:
    dispatch_id = "acp-reconcile-terminal-error"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _acquire_lock(fleet_dir, dispatch_id)
        terminal = json.loads((FIXTURES / "valid_ok.json").read_text())
        terminal["state"] = "error"
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
        _write_aggregate(fleet_dir, [dispatch_id])

        row = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)

        assert_true("release action", row.action == "release_locks")
        assert_true("released", row.released is True)
        released = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock released", released is None or released.get("state") == "released")


def test_terminal_release_uses_status_json_account_key() -> None:
    dispatch_id = "acp-reconcile-status-account"
    account_key = "openai/status-json"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = _acquire_lock(fleet_dir, dispatch_id, account_key=account_key)
        terminal = json.loads((FIXTURES / "valid_ok.json").read_text())
        terminal["state"] = "complete"
        terminal["account_key"] = account_key
        terminal["account_lock_fencing_token"] = lock["fencing_token"]
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
        _write_aggregate(fleet_dir, [dispatch_id])

        row = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)

        assert_true("release action", row.action == "release_locks")
        assert_true("released", row.released is True)
        assert_true("account key", row.account_key == account_key)
        released = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, account_key))
        assert_true("lock released", released is None or released.get("state") == "released")


def test_terminal_release_fails_closed_when_active_lock_unresolved() -> None:
    dispatch_id = "acp-reconcile-corrupt-lock"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _acquire_lock(fleet_dir, dispatch_id)
        fleet.account_lock_path(fleet_dir, "openai/default").write_text("{not-json")
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
        _write_aggregate(fleet_dir, [dispatch_id])

        row = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)

        assert_true("release action", row.action == "release_locks")
        assert_true("not released", row.released is False)
        aggregate = fleet.read_json(fleet_dir / "register" / "aggregate.json")
        assert_true("dispatch still active", dispatch_id in aggregate.get("active_dispatches", []))
        meta = fleet.read_json(dispatch_dir / "meta.json")
        assert_true("not marked released", meta.get("row_state") != "released")


def test_pre_status_confirmed_failed_release() -> None:
    dispatch_id = "acp-reconcile-pre-status-failed"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = _acquire_lock(fleet_dir, dispatch_id)
        _write_dispatch(
            fleet_dir,
            dispatch_id=dispatch_id,
            mirror_name=None,
            meta={
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "billing_account": "openai/default",
                "account_key": "openai/default",
                "account_lock_fencing_token": lock["fencing_token"],
                "lease_active": True,
                "pid_hint": "dead",
                "ssh_reachable": True,
                "launch_unconfirmed": True,
                "launch_unconfirmed_status_misses": 2,
                "launch_issued_at": "2000-01-01T00:00:00+00:00",
                "row_state": "launch_unconfirmed",
            },
        )

        row = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)

        assert_true("release action", row.action == "release_locks")
        assert_true("released", row.released is True)
        assert_true("account key", row.account_key == "openai/default")
        released = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock released", released is None or released.get("state") == "released")


def test_pre_status_dead_before_grace_not_release() -> None:
    dispatch_id = "acp-reconcile-pre-status-before-grace"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = _acquire_lock(fleet_dir, dispatch_id)
        _write_dispatch(
            fleet_dir,
            dispatch_id=dispatch_id,
            mirror_name=None,
            meta={
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "account_key": "openai/default",
                "account_lock_fencing_token": lock["fencing_token"],
                "lease_active": True,
                "pid_hint": "dead",
                "ssh_reachable": True,
                "launch_unconfirmed": True,
                "launch_unconfirmed_status_misses": 1,
                "launch_issued_at": "2000-01-01T00:00:00+00:00",
                "row_state": "launch_unconfirmed",
            },
        )

        row = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)

        assert_true("not release action", row.action != "release_locks")
        assert_true("not released", row.released is False)
        held = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock held", held is not None and held.get("state") == "active")


def test_still_live_pre_status_not_release() -> None:
    dispatch_id = "acp-reconcile-pre-status-live"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = _acquire_lock(fleet_dir, dispatch_id)
        _write_dispatch(
            fleet_dir,
            dispatch_id=dispatch_id,
            mirror_name=None,
            meta={
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "account_key": "openai/default",
                "account_lock_fencing_token": lock["fencing_token"],
                "lease_active": True,
                "pid_hint": "alive",
                "ssh_reachable": True,
                "launch_unconfirmed": True,
                "launch_unconfirmed_status_misses": 99,
                "launch_issued_at": "2000-01-01T00:00:00+00:00",
                "row_state": "launch_unconfirmed",
            },
        )

        row = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)

        assert_true("not released", row.released is False)
        held = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock held", held is not None and held.get("state") == "active")


def test_reconcile_all_in_flight_releases_eligible_only() -> None:
    failed_id = "acp-reconcile-all-failed"
    complete_id = "acp-reconcile-all-complete"
    live_id = "acp-reconcile-all-live"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)

        for dispatch_id, account_key in (
            (failed_id, "openai/failed"),
            (complete_id, "openai/complete"),
            (live_id, "openai/live"),
        ):
            _acquire_lock(fleet_dir, dispatch_id, account_key=account_key)

        failed = json.loads((FIXTURES / "valid_ok.json").read_text())
        failed["state"] = "failed"
        _write_dispatch(
            fleet_dir,
            dispatch_id=failed_id,
            mirror_name=None,
            meta={
                "dispatch_id": failed_id,
                "node_id": "localhost",
                "billing_account": "openai/failed",
                "lease_active": True,
                "pid_hint": "dead",
                "ssh_reachable": True,
            },
        )
        failed_dir = fleet_dir / "register" / "dispatches" / failed_id
        (failed_dir / "status.json").write_text(json.dumps(failed) + "\n")

        complete = json.loads((FIXTURES / "valid_ok.json").read_text())
        complete["state"] = "complete"
        _write_dispatch(
            fleet_dir,
            dispatch_id=complete_id,
            mirror_name=None,
            meta={
                "dispatch_id": complete_id,
                "node_id": "localhost",
                "billing_account": "openai/complete",
                "lease_active": True,
                "pid_hint": "dead",
                "ssh_reachable": True,
            },
        )
        complete_dir = fleet_dir / "register" / "dispatches" / complete_id
        (complete_dir / "status.json").write_text(json.dumps(complete) + "\n")

        _write_dispatch(
            fleet_dir,
            dispatch_id=live_id,
            mirror_name="valid_ok.json",
            meta={
                "dispatch_id": live_id,
                "node_id": "localhost",
                "billing_account": "openai/live",
                "lease_active": True,
                "pid_hint": "alive",
                "ssh_reachable": True,
            },
        )
        _write_aggregate(fleet_dir, [failed_id, complete_id, live_id])

        result = fleet_reconcile.reconcile_all_in_flight(fleet_dir, mutate=True)

        assert_true("failed released", failed_id in result["released_dispatch_ids"])
        assert_true("complete released", complete_id in result["released_dispatch_ids"])
        assert_true("live held", live_id not in result["released_dispatch_ids"])
        live_lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/live"))
        assert_true("live lock active", live_lock is not None and live_lock.get("state") == "active")


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


def test_salvage_needed_dead_pid_holds_lock() -> None:
    dispatch_id = "acp-reconcile-salvage-needed"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _acquire_lock(fleet_dir, dispatch_id)
        salvage = json.loads((FIXTURES / "valid_ok.json").read_text())
        salvage["state"] = "salvage_needed"
        salvage["reason"] = "remote_launch_pid_dead_dirty_worktree"
        salvage["worktree_path"] = "/tmp/goal-flight-test/worktrees/acp-reconcile-salvage-needed"
        salvage["porcelain"] = " M scripts/example.py"
        dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        (dispatch_dir / "status.json").write_text(json.dumps(salvage) + "\n")
        fleet._atomic_write_json(
            dispatch_dir / "meta.json",
            {
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "lease_active": True,
                "pid_hint": "dead",
                "ssh_reachable": True,
                "row_state": "salvage_needed",
                "worktree_path": salvage["worktree_path"],
            },
        )
        _write_aggregate(fleet_dir, [dispatch_id])

        row = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)

        assert_true("noop action", row.action == "noop")
        assert_true("salvage reason", row.reason == "salvage_needed_hold_lock")
        assert_true("not released", row.released is False)
        assert_true("may not release", row.may_release is False)
        held = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock held", held is not None and held.get("state") == "active")
        meta = fleet.read_json(dispatch_dir / "meta.json")
        assert_true("not marked released", meta.get("row_state") != "released")


def test_salvage_needed_ttl_expired_lock_survives_stale_reconcile() -> None:
    dispatch_id = "acp-reconcile-salvage-ttl"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = _acquire_lock(fleet_dir, dispatch_id)
        salvage = json.loads((FIXTURES / "valid_ok.json").read_text())
        salvage["state"] = "salvage_needed"
        salvage["reason"] = "remote_launch_pid_dead_dirty_worktree"
        dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        (dispatch_dir / "status.json").write_text(json.dumps(salvage) + "\n")
        fleet._atomic_write_json(
            dispatch_dir / "meta.json",
            {
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "lease_active": True,
                "pid_hint": "dead",
                "ssh_reachable": True,
                "row_state": "salvage_needed",
            },
        )
        _write_aggregate(fleet_dir, [dispatch_id])
        _expire_lock(fleet_dir)

        result = fleet.reconcile_fleet(fleet_dir, release_stale=True)

        assert_true("not ttl released", "openai/default" not in (result.get("account_stale_released") or []))
        held = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock still active", held is not None and held.get("state") == "active")
        assert_true("same fencing", held.get("fencing_token") == lock.get("fencing_token"))


def test_non_salvage_terminal_ttl_expired_still_reaped() -> None:
    dispatch_id = "acp-reconcile-terminal-ttl"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = _acquire_lock(fleet_dir, dispatch_id)
        terminal = json.loads((FIXTURES / "valid_ok.json").read_text())
        terminal["state"] = "worker_dead"
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
                "row_state": "worker_dead",
            },
        )
        _write_aggregate(fleet_dir, [dispatch_id])
        _expire_lock(fleet_dir)

        result = fleet.reconcile_fleet(fleet_dir, release_stale=True)

        assert_true("ttl released", "openai/default" in (result.get("account_stale_released") or []))
        released = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock released", released is None or released.get("state") == "released")
        assert_true("fencing was", lock.get("fencing_token"))


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
    test_terminal_mirror_live_pid_holds_lock()
    test_terminal_failed_release()
    test_acp_final_failure_states_release_locks()
    test_terminal_error_release()
    test_terminal_release_uses_status_json_account_key()
    test_terminal_release_fails_closed_when_active_lock_unresolved()
    test_pre_status_confirmed_failed_release()
    test_pre_status_dead_before_grace_not_release()
    test_still_live_pre_status_not_release()
    test_reconcile_all_in_flight_releases_eligible_only()
    test_audit_log_appended_on_mutate()
    test_salvage_needed_dead_pid_holds_lock()
    test_salvage_needed_ttl_expired_lock_survives_stale_reconcile()
    test_non_salvage_terminal_ttl_expired_still_reaped()
    test_live_ssh_probe_quarantine_when_unreachable()
    print("OK: 17 fleet reconcile tests pass")


if __name__ == "__main__":
    main()
