#!/usr/bin/env python3
"""Write handlers surface permanent faults without breaking transient recovery."""

from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_acp_run as acp_run  # noqa: E402
import goalflight_acp_client as acp_client  # noqa: E402
import goalflight_capacity as capacity  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_fleet as fleet  # noqa: E402
import goalflight_fleet_steering as fleet_steering  # noqa: E402
import goalflight_fleet_store as fleet_store  # noqa: E402
import goalflight_fleet_watch as fleet_watch  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as session_status  # noqa: E402
import goalflight_status as status  # noqa: E402
import goalflight_watch as watch  # noqa: E402


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    values = {
        "GOALFLIGHT_JOURNAL_DIR": tmp_path / "journal",
        "GOALFLIGHT_STATE_DIR": tmp_path / "state",
        "GOALFLIGHT_MESSAGES_DIR": tmp_path / "messages",
        "GOALFLIGHT_TASK_STORE": tmp_path / "task-store",
        "GOALFLIGHT_TASK_STORE_DIR": tmp_path / "task-store",
        "GOALFLIGHT_WAKE_LEDGER": tmp_path / "wake-ledger.json",
        "GOALFLIGHT_WAKE_LEDGER_DIR": tmp_path / "wake-ledger",
        "GOALFLIGHT_DISPATCH_DIR": tmp_path / "dispatch",
        "GOALFLIGHT_PIDFILE_DIR": tmp_path / "pidfiles",
        "GOAL_FLIGHT_PIDFILE_DIR": tmp_path / "pidfiles",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    project = tmp_path / "project"
    project.mkdir()
    return project


class _FailingCoverageAuthority:
    def __init__(self, error: Exception):
        self.error = error

    def exit_listener(self, _coverage_id: str, *, reason: str):
        raise self.error


class _ClosedDiagnosticStream:
    def write(self, _text: str) -> int:
        raise OSError("stderr closed")

    def flush(self) -> None:
        raise OSError("stderr closed")


@pytest.mark.parametrize(
    "error",
    (
        ValueError("listener exit reason is not registered"),
        journal.JournalUnavailable("journal busy"),
    ),
)
def test_coverage_exit_failure_is_visible_without_losing_final_event(
    error: Exception,
    capsys: pytest.CaptureFixture[str],
) -> None:
    messages._exit_listener_before_final_event(
        _FailingCoverageAuthority(error),  # type: ignore[arg-type]
        "coverage-id",
        reason="unregistered-test-reason",
    )

    stderr = capsys.readouterr().err
    assert "coverage exit write failed" in stderr
    assert type(error).__name__ in stderr
    assert "unregistered-test-reason" in stderr


@pytest.mark.parametrize(
    "error",
    (ValueError("bad monitor state"), OSError("temporary monitor I/O")),
)
def test_monitor_fault_write_is_visible_without_losing_final_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    monkeypatch.setattr(
        messages.goalflight_wake,
        "record_monitor_fault",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    messages._record_monitor_fault_before_final_event(
        tmp_path,
        controller_label="controller",
        lease_nonce="nonce",
        reason="listener-fault",
        detail="detail",
    )
    stderr = capsys.readouterr().err
    assert "monitor fault state write failed" in stderr
    assert type(error).__name__ in stderr


def test_secondary_write_diagnostic_failure_cannot_block_final_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(messages.sys, "stderr", _ClosedDiagnosticStream())
    messages._exit_listener_before_final_event(
        _FailingCoverageAuthority(ValueError("bad coverage contract")),  # type: ignore[arg-type]
        "coverage-id",
        reason="bad-reason",
    )
    monkeypatch.setattr(
        messages.goalflight_wake,
        "record_monitor_fault",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad monitor contract")),
    )
    messages._record_monitor_fault_before_final_event(
        tmp_path,
        controller_label="controller",
        lease_nonce="nonce",
        reason="listener-fault",
        detail="detail",
    )


class _FailingLeaseAuthority:
    def __init__(self, error: Exception):
        self.error = error

    def active_lease(self, _label: str):
        return None

    def claim_or_renew_lease(self, *_args, **_kwargs):
        raise self.error


def test_controller_claim_contract_fault_propagates(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_status.goalflight_journal,
        "open_or_create_journal",
        lambda _root: _FailingLeaseAuthority(ValueError("bad lease schema")),
    )

    with pytest.raises(ValueError, match="bad lease schema"):
        session_status.register_controller(
            isolated,
            "controller",
            session_id="nonce",
        )


@pytest.mark.parametrize(
    "error",
    (OSError("temporary I/O"), journal.JournalUnavailable("journal busy")),
)
def test_controller_claim_transient_failure_still_returns_claim_failed(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    monkeypatch.setattr(
        session_status.goalflight_journal,
        "open_or_create_journal",
        lambda _root: _FailingLeaseAuthority(error),
    )

    result = session_status.register_controller(
        isolated,
        "controller",
        session_id="nonce",
    )

    assert result["registered"] is False
    assert result["reason"] == "claim_failed"


@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("bad beacon claim"), True), (OSError("temporary beacon I/O"), False)),
)
def test_beacon_claim_distinguishes_contract_from_transient(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    identity = {"pid": 123, "start_token": "start"}
    monkeypatch.setattr(session_status, "_doomed_invocation_pid", lambda *_args: False)
    monkeypatch.setattr(session_status, "_controller_process_identity", lambda _pid: identity)
    monkeypatch.setattr(
        session_status,
        "claim_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    call = lambda: session_status.register_controller(
        isolated,
        "controller",
        pid=123,
        session_id="nonce",
        process_identity=identity,
        hold_lock=True,
    )
    if raises:
        with pytest.raises(ValueError, match="bad beacon claim"):
            call()
    else:
        result = call()
        assert result["registered"] is False
        assert result["reason"] == "claim_failed"


@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("bad startup claim"), True), (OSError("temporary startup I/O"), False)),
)
def test_startup_claim_distinguishes_contract_from_transient(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    identity = {"pid": 123, "start_token": "start"}
    monkeypatch.setattr(
        session_status,
        "_resolve_optional_incarnation",
        lambda *_args, **_kwargs: ({"pid": 123, "process_identity": identity}, None),
    )
    monkeypatch.setattr(session_status, "resolve_controller_label", lambda *_args, **_kwargs: "controller")
    monkeypatch.setattr(
        session_status,
        "claim_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    call = lambda: session_status.claim_controller_startup(
        isolated,
        pid=123,
        label="controller",
    )
    if raises:
        with pytest.raises(ValueError, match="bad startup claim"):
            call()
    else:
        result = call()
        assert result["claimed"] is False
        assert result["reason"] == "claim_failed"


@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("bad join claim"), True), (OSError("temporary join I/O"), False)),
)
def test_join_claim_distinguishes_contract_from_transient(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    identity = {"pid": 123, "start_token": "start"}
    monkeypatch.setattr(session_status, "_doomed_invocation_pid", lambda *_args: False)
    monkeypatch.setattr(session_status, "_controller_process_identity", lambda _pid: identity)
    monkeypatch.setattr(
        session_status.goalflight_journal,
        "open_or_create_journal",
        lambda _root: _FailingLeaseAuthority(RuntimeError("unused")),
    )
    monkeypatch.setattr(
        session_status,
        "claim_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    call = lambda: session_status.join_controller(
        isolated,
        "controller",
        pid=123,
        session_id="nonce",
        process_identity=identity,
        acknowledge_conflict=True,
        hold_lock=True,
    )
    if raises:
        with pytest.raises(ValueError, match="bad join claim"):
            call()
    else:
        result = call()
        assert result["joined"] is False
        assert result["reason"] == "claim_failed"


def _draft_reconciliation_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> tuple[dict, dict]:
    record_path = tmp_path / "dispatch.json"
    record_path.write_text(json.dumps({"dispatch_id": "draft-write", "state": "failed"}))
    monkeypatch.setattr(status.goalflight_ledger, "record_path", lambda _dispatch_id: record_path)
    monkeypatch.setattr(status.goalflight_ledger, "cmd_finish", lambda _args: 0)
    monkeypatch.setattr(status.goalflight_ledger, "StateLock", contextlib.nullcontext)
    monkeypatch.setattr(
        status.goalflight_ledger,
        "write_record",
        lambda _record: (_ for _ in ()).throw(error),
    )
    record = {"dispatch_id": "draft-write"}
    reconciled = {
        "draft_artifact_reconciliation": {"promoted": True},
        "terminal_marker": {"kind": "COMPLETE"},
        "reason": "draft promoted",
    }
    return record, reconciled


def test_status_reconciliation_contract_fault_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, reconciled = _draft_reconciliation_fixture(
        tmp_path,
        monkeypatch,
        ValueError("invalid ledger record"),
    )

    with pytest.raises(ValueError, match="invalid ledger record"):
        status._persist_draft_artifact_reconciliation(record, reconciled)


@pytest.mark.parametrize(
    "error",
    (OSError("temporary ledger I/O"), journal.JournalUnavailable("journal busy")),
)
def test_status_reconciliation_transient_io_is_deferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    record, reconciled = _draft_reconciliation_fixture(
        tmp_path,
        monkeypatch,
        error,
    )

    assert status._persist_draft_artifact_reconciliation(record, reconciled) is None


def test_status_reconciliation_corrupt_record_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_path = tmp_path / "dispatch.json"
    record_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(status.goalflight_ledger, "record_path", lambda _dispatch_id: record_path)
    with pytest.raises(json.JSONDecodeError):
        status._persist_draft_artifact_reconciliation(
            {"dispatch_id": "draft-write"},
            {"draft_artifact_reconciliation": {"promoted": True}},
        )


@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("invalid capacity payload"), True), (OSError("capacity busy"), False)),
)
def test_quota_stuck_lease_release_distinguishes_contract_from_transient(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    state = {
        "leases": {
            "lease-1": {"state": "active", "worker_pid": 123, "dispatch_id": "d-1"}
        }
    }
    monkeypatch.setattr(capacity, "StateLock", contextlib.nullcontext)
    monkeypatch.setattr(capacity, "load_state", lambda: state)
    monkeypatch.setattr(
        capacity,
        "save_state",
        lambda _data: (_ for _ in ()).throw(error),
    )
    call = lambda: acp_client._release_quota_stuck_lease(
        123,
        dispatch_id="d-1",
        reason={"reason": "quota"},
    )
    if raises:
        with pytest.raises(ValueError, match="invalid capacity payload"):
            call()
    else:
        result = call()
        assert result["released_lease_id"] == "lease-1"
        assert result["lease_release_error"] == "capacity busy"


@pytest.mark.parametrize(
    ("error", "raises"),
    (
        (ValueError("invalid terminal state"), True),
        (OSError("ledger busy"), False),
        (journal.JournalUnavailable("journal busy"), False),
    ),
)
def test_quota_stuck_terminal_write_distinguishes_contract_from_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    record_path = tmp_path / "dispatch.json"
    record_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        acp_client.goalflight_ledger,
        "record_path",
        lambda _dispatch_id: record_path,
    )
    monkeypatch.setattr(
        acp_client.goalflight_ledger,
        "cmd_finish",
        lambda _args: (_ for _ in ()).throw(error),
    )
    call = lambda: acp_client._finish_quota_stuck_ledger(
        {"dispatch_id": "d-1"},
        reason={"limit_state": "quota_exhausted"},
    )
    if raises:
        with pytest.raises(ValueError, match="invalid terminal state"):
            call()
    else:
        assert call() is False


def _proposal_with_envelope_failure(
    fleet_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> dict:
    fleet.bootstrap(fleet_dir)
    proposal = fleet_steering.propose_steering(
        fleet_dir,
        patch=[{"op": "add", "path": "/node_policy/priority/0", "value": "build-1"}],
        reason="write failure test",
        created_by={"controller_id": "test", "host_adapter": "pytest"},
    )
    monkeypatch.setattr(
        messages,
        "write_steering_envelope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    with fleet.RegistryLock(fleet_dir):
        return fleet_steering.apply_proposal(fleet_dir, proposal["proposal_id"])


def test_steering_envelope_contract_fault_propagates_after_durable_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet_dir = tmp_path / "fleet-contract"
    with pytest.raises(ValueError, match="bad envelope"):
        _proposal_with_envelope_failure(
            fleet_dir,
            monkeypatch,
            ValueError("bad envelope"),
        )
    assert fleet_steering.load_steering_doc(fleet_dir)["node_policy"]["priority"] == [
        "build-1"
    ]


def test_steering_envelope_transient_io_keeps_applied_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _proposal_with_envelope_failure(
        tmp_path / "fleet-transient",
        monkeypatch,
        OSError("temporary envelope I/O"),
    )
    assert result["ok"] is True


def _fleet_lock_fixture(tmp_path: Path) -> Path:
    fleet_dir = tmp_path / "fleet"
    fleet.bootstrap(fleet_dir)
    fleet.acquire_account_lock(
        fleet_dir,
        account_key="provider/account",
        owner_dispatch_id="dispatch-1",
    )
    return fleet_dir


def test_fleet_lock_release_contract_fault_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet_dir = _fleet_lock_fixture(tmp_path)
    monkeypatch.setattr(
        fleet_store,
        "release_account_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad fence")),
    )

    with pytest.raises(ValueError, match="bad fence"):
        fleet_watch.release_lock_on_confirmed_terminal(
            fleet_dir,
            "dispatch-1",
            "complete",
        )


def test_fleet_lock_release_transient_io_still_defers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet_dir = _fleet_lock_fixture(tmp_path)
    monkeypatch.setattr(
        fleet_store,
        "release_account_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("temporary lock I/O")),
    )

    assert (
        fleet_watch.release_lock_on_confirmed_terminal(
            fleet_dir,
            "dispatch-1",
            "complete",
        )
        is False
    )


def test_drain_stale_release_contract_fault_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatch.goalflight_capacity,
        "cmd_release_stale",
        lambda _args: (_ for _ in ()).throw(ValueError("bad release state")),
    )
    with pytest.raises(ValueError, match="bad release state"):
        dispatch._release_stale_capacity_for_drain()


def test_drain_stale_release_transient_io_still_defers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatch.goalflight_capacity,
        "cmd_release_stale",
        lambda _args: (_ for _ in ()).throw(OSError("temporary capacity I/O")),
    )
    assert dispatch._release_stale_capacity_for_drain() is None


@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("bad dashboard pidfile"), True), (OSError("temporary dashboard I/O"), False)),
)
def test_dashboard_pidfile_write_distinguishes_contract_from_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    project = tmp_path / "project"
    (project / "dashboard").mkdir(parents=True)
    pidfile = tmp_path / "refresh.pid"
    log_path = tmp_path / "refresh.log"
    monkeypatch.setattr(
        dispatch,
        "_dashboard_refresh_paths",
        lambda _project: (pidfile, log_path),
    )
    monkeypatch.setattr(
        dispatch,
        "_dashboard_refresh_pidfile_is_current",
        lambda *_args: (False, "missing"),
    )
    monkeypatch.setattr(dispatch, "_try_claim_dashboard_refresh_start", lambda _path: True)
    monkeypatch.setattr(
        dispatch.subprocess,
        "Popen",
        lambda *_args, **_kwargs: SimpleNamespace(pid=123),
    )
    monkeypatch.setattr(
        dispatch,
        "_write_dashboard_refresh_pidfile",
        lambda *_args: (_ for _ in ()).throw(error),
    )
    call = lambda: dispatch._start_dashboard_refresh_for_project(project)
    if raises:
        with pytest.raises(ValueError, match="bad dashboard pidfile"):
            call()
    else:
        assert call() is None


@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("bad task publish"), True), (OSError("temporary task-store I/O"), False)),
)
def test_task_publish_recovery_distinguishes_contract_from_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    marker = tmp_path / "publish-marker"
    marker.write_text("pending")

    class FailingStore:
        publish_marker_path = marker

        def __init__(self, _root: Path):
            pass

        def _recover_interrupted_publish_locked(self) -> None:
            raise error

        def load_items(self, *, recover_publish: bool):
            raise AssertionError("recovery failure should stop the store read")

    monkeypatch.setattr(sys.modules["goalflight_task"], "TaskStore", FailingStore)
    monkeypatch.setattr(
        dispatch,
        "_ledger_task_ids_advanced",
        lambda *_args, **_kwargs: (0, 0, "conclusive"),
    )
    call = lambda: dispatch._linked_task_truth(
        {"dispatch_id": "task-write", "task_ids": ["t-1"], "project_root": str(tmp_path)},
        task_store_locked=True,
    )
    if raises:
        with pytest.raises(ValueError, match="bad task publish"):
            call()
    else:
        assert call() == "indeterminate"


def test_acp_lease_attach_contract_fault_propagates() -> None:
    with pytest.raises(ValueError, match="bad capacity lease"):
        acp_run._attach_worker_state_before_running(
            lambda _pid: (_ for _ in ()).throw(ValueError("bad capacity lease")),
            123,
        )


def test_acp_lease_attach_transient_io_still_defers() -> None:
    assert (
        acp_run._attach_worker_state_before_running(
            lambda _pid: (_ for _ in ()).throw(OSError("temporary capacity I/O")),
            123,
        )
        is None
    )


@pytest.mark.parametrize(
    "error",
    (ValueError("bad lease detach"), OSError("temporary detach I/O")),
)
def test_acp_live_lease_detach_failure_is_nonfatal_and_visible(error: Exception) -> None:
    payload: dict[str, object] = {}
    acp_run._detach_live_worker_state(
        payload,
        lambda _pid, _reason: (_ for _ in ()).throw(error),
        123,
        "worker-live",
    )
    assert payload["lease_detach_error"] == {
        "type": type(error).__name__,
        "message": str(error),
    }


def _restore_txn() -> SimpleNamespace:
    return SimpleNamespace(
        queue_locked=True,
        ledger_locked=True,
        task_store_locked=False,
        entry={"dispatch_id": "restore-write"},
    )


@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("bad restore record"), True), (OSError("temporary restore I/O"), False)),
)
def test_restore_ledger_write_distinguishes_contract_from_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    claim = tmp_path / "restore.json.claimed-1"
    claim.write_text("{}")
    fresh = {"dispatch_id": "restore-write", "state": "claimed"}
    monkeypatch.setattr(dispatch, "_reconcile_transaction_still_valid", lambda *_args: True)
    monkeypatch.setattr(dispatch, "_find_dispatch_record", lambda _dispatch_id: None)
    monkeypatch.setattr(dispatch, "_entry_completion_authority", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        dispatch,
        "_new_reconciliation_record",
        lambda _entry: {"dispatch_id": "restore-write"},
    )
    monkeypatch.setattr(
        dispatch.goalflight_ledger,
        "write_record",
        lambda _record: (_ for _ in ()).throw(error),
    )
    call = lambda: dispatch._commit_restore_transaction(
        _restore_txn(),
        claim,
        fresh,
        increment_recovery_count=False,
        reason="test",
    )
    if raises:
        with pytest.raises(ValueError, match="bad restore record"):
            call()
    else:
        assert call() == (None, None)


@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("bad orphan record"), True), (OSError("temporary orphan I/O"), False)),
)
def test_orphan_stamp_distinguishes_contract_from_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    path = tmp_path / "orphan.json"
    path.write_text(json.dumps({"dispatch_id": "orphan-write", "state": "running"}))
    admission = object()
    txn = SimpleNamespace(
        ledger_locked=True,
        entry={"dispatch_id": "orphan-write"},
        admission=admission,
    )
    monkeypatch.setattr(dispatch.goalflight_ledger, "record_path", lambda _dispatch_id: path)
    monkeypatch.setattr(dispatch, "_dispatch_record_is_terminal", lambda _record: False)
    monkeypatch.setattr(dispatch, "_entry_with_record_identity", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(dispatch, "_reconcile_transaction_still_valid", lambda *_args: True)
    monkeypatch.setattr(
        dispatch,
        "classify_reconciliation_admission",
        lambda *_args, **_kwargs: admission,
    )
    monkeypatch.setattr(
        dispatch.goalflight_ledger,
        "write_record",
        lambda _record: (_ for _ in ()).throw(error),
    )
    call = lambda: dispatch._stamp_ledger_orphan_first_seen(
        {"dispatch_id": "orphan-write"},
        txn=txn,
    )
    if raises:
        with pytest.raises(ValueError, match="bad orphan record"):
            call()
    else:
        assert call() is None


def _terminal_winner(state: str) -> SimpleNamespace:
    return SimpleNamespace(
        terminal_state=state,
        terminal_at="2026-01-01T00:00:00+00:00",
        attempt_id="attempt",
        transition_id="transition",
        event_uuid="event",
    )


@pytest.mark.parametrize("existing", (False, True))
@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("bad terminal record"), True), (OSError("temporary terminal I/O"), False)),
)
def test_reconciled_terminal_write_distinguishes_contract_from_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    error: Exception,
    raises: bool,
) -> None:
    path = tmp_path / "terminal.json"
    state = "complete" if existing else "worker_dead"
    if existing:
        path.write_text(
            json.dumps(
                {
                    "dispatch_id": "terminal-write",
                    "state": "complete",
                    "terminal_state": "complete",
                }
            )
        )
    txn = SimpleNamespace(ledger_locked=True, task_store_locked=False, entry={})
    monkeypatch.setattr(dispatch.goalflight_ledger, "record_path", lambda _dispatch_id: path)
    monkeypatch.setattr(dispatch, "_is_task_linked", lambda *_args: False)
    monkeypatch.setattr(dispatch, "_reconcile_transaction_still_valid", lambda *_args: True)
    monkeypatch.setattr(dispatch, "_entry_completion_authority", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        dispatch,
        "_new_reconciliation_record",
        lambda _entry: {"dispatch_id": "terminal-write"},
    )
    monkeypatch.setattr(
        dispatch.goalflight_ledger,
        "commit_terminal_authority",
        lambda *_args, **_kwargs: SimpleNamespace(
            committed=True,
            value=_terminal_winner(state),
        ),
    )
    writes: list[dict] = []

    def fail_write(record: dict) -> None:
        writes.append(dict(record))
        raise error

    monkeypatch.setattr(
        dispatch.goalflight_ledger,
        "write_record",
        fail_write,
    )
    call = lambda: dispatch.commit_reconciled_terminal(
        txn,
        {"dispatch_id": "terminal-write"},
        {"state": state, "reason": "test"},
    )
    if raises:
        with pytest.raises(ValueError, match="bad terminal record"):
            call()
    else:
        result = call()
        assert result.kind is dispatch.TerminalCommitKind.DEFERRED
        assert result.committed is False
    assert writes
    if existing:
        assert writes[0]["ended_at"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("bad requeue intent"), True), (OSError("temporary requeue I/O"), False)),
)
def test_requeue_intent_write_distinguishes_contract_from_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    path = tmp_path / "requeue.json"
    path.write_text(
        json.dumps(
            {
                "dispatch_id": "requeue-write",
                "state": "blocked_auth",
                "effective_account": "provider/account",
            }
        )
    )
    txn = SimpleNamespace(queue_locked=True, ledger_locked=True)
    monkeypatch.setattr(
        dispatch.goalflight_ledger,
        "record_path",
        lambda *_args, **_kwargs: path,
    )
    monkeypatch.setattr(
        dispatch.goalflight_ledger,
        "write_record",
        lambda _record: (_ for _ in ()).throw(error),
    )
    call = lambda: dispatch._maybe_requeue_terminal_claim(
        txn,
        {"dispatch_id": "requeue-write", "request": {}},
        queue_dir=tmp_path,
        tail=tmp_path / "tail",
    )
    if raises:
        with pytest.raises(ValueError, match="bad requeue intent"):
            call()
    else:
        assert call() is False


@pytest.mark.parametrize(
    ("error", "raises"),
    ((ValueError("bad child envelope"), True), (OSError("temporary queue I/O"), False)),
)
def test_requeue_child_write_distinguishes_contract_from_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    raises: bool,
) -> None:
    path = tmp_path / "requeue-child-parent.json"
    path.write_text(
        json.dumps(
            {
                "dispatch_id": "requeue-child-parent",
                "state": "blocked_auth",
                "effective_account": "provider/account",
                "requeue": {"child_id": "requeue-child"},
            }
        )
    )
    txn = SimpleNamespace(queue_locked=True, ledger_locked=True)
    monkeypatch.setattr(
        dispatch.goalflight_ledger,
        "record_path",
        lambda *_args, **_kwargs: path,
    )
    monkeypatch.setattr(dispatch, "_requeue_child_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        dispatch,
        "_requeue_child_entry",
        lambda *_args, **_kwargs: {"dispatch_id": "requeue-child"},
    )
    monkeypatch.setattr(
        dispatch,
        "_write_json_exclusive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    call = lambda: dispatch._maybe_requeue_terminal_claim(
        txn,
        {"dispatch_id": "requeue-child-parent", "request": {}},
        queue_dir=tmp_path,
        tail=tmp_path / "tail",
    )
    if raises:
        with pytest.raises(ValueError, match="bad child envelope"):
            call()
    else:
        assert call() is False


def test_nested_shutdown_handlers_remain_visible_and_mutation_sensitive() -> None:
    """Restoring the old broad suppress at any nested changed site fails here."""
    dispatch_source = inspect.getsource(dispatch.main)
    acp_source = inspect.getsource(acp_run._run_acp_dispatch_impl)
    acp_attach_source = inspect.getsource(acp_run._attach_worker_state_before_running)
    acp_detach_source = inspect.getsource(acp_run._detach_live_worker_state)
    watch_source = inspect.getsource(watch.main)
    messages_source = inspect.getsource(messages.cmd_follow)
    listen_source = inspect.getsource(messages.cmd_listen)
    monitor_fault_source = inspect.getsource(
        messages._record_monitor_fault_before_final_event
    )

    assert '"write": "terminal_ledger"' in dispatch_source
    assert '"write": "capacity_release"' in dispatch_source
    assert "DISPATCH-FINALIZE-WARN" in dispatch_source
    assert "_attach_worker_state_before_running" in acp_source
    assert "_detach_live_worker_state" in acp_source
    assert "except OSError:" in acp_attach_source
    assert "lease_detach_error" in acp_detach_source
    assert "final state write failed" in watch_source
    assert "_record_monitor_fault_before_final_event" in messages_source
    assert "monitor fault state write failed" in monitor_fault_source
    assert "contextlib.suppress(OSError, ValueError)" in monitor_fault_source
    assert "contextlib.suppress(OSError, ValueError)" in inspect.getsource(
        messages._exit_listener_before_final_event
    )
    assert listen_source.count("_exit_listener_before_final_event(") == 3

    assert "with contextlib.suppress(Exception):\n                _finish_ledger(" not in dispatch_source
    assert "with contextlib.suppress(Exception):\n                _release_capacity(" not in dispatch_source
    assert "with contextlib.suppress(Exception):\n            attach_worker_to_lease(" not in acp_source
    assert "with contextlib.suppress(Exception):\n                detach_lease_to_worker(" not in acp_source
    assert "with contextlib.suppress(Exception):\n            write_payload(" not in watch_source
    assert "with contextlib.suppress(OSError, RuntimeError, ValueError)" not in messages_source
    assert dispatch_source.count("with contextlib.suppress(OSError, ValueError):") >= 2
    assert "with contextlib.suppress(OSError, ValueError):" in watch_source

    assert "except OSError:" in inspect.getsource(
        dispatch._start_dashboard_refresh_for_project
    )
    assert "except (ImportError, OSError):" in inspect.getsource(
        dispatch._linked_task_truth_detail
    )
    assert "except OSError as exc:" in inspect.getsource(
        dispatch._maybe_requeue_terminal_claim
    )
    assert "except OSError as exc:" in inspect.getsource(
        acp_client._release_quota_stuck_lease
    )
    assert "except (OSError, goalflight_journal.JournalUnavailable) as exc:" in inspect.getsource(
        acp_client._finish_quota_stuck_ledger
    )
    assert "except (OSError, goalflight_journal.JournalUnavailable):" in inspect.getsource(
        status._persist_draft_artifact_reconciliation
    )

    claim_cli_source = inspect.getsource(session_status.main)
    claim_cli_source = claim_cli_source.split("if args.claim_session:", 1)[1].split(
        "if args.controller_startup:", 1
    )[0]
    assert "except goalflight_journal.JournalError:" in claim_cli_source
    assert "except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as exc:" in claim_cli_source
    assert "except Exception as exc:" not in claim_cli_source
    assert "except _EXPECTED_OPTIONAL_ERRORS" not in claim_cli_source


def test_intentional_best_effort_sites_name_what_they_give_up() -> None:
    dispatch_source = (SCRIPTS / "goalflight_dispatch.py").read_text(encoding="utf-8")
    ledger_source = (SCRIPTS / "goalflight_ledger.py").read_text(encoding="utf-8")
    watch_source = (SCRIPTS / "goalflight_watch.py").read_text(encoding="utf-8")
    permits_source = (SCRIPTS / "goalflight_acp_client.py").read_text(encoding="utf-8")
    context_source = (SCRIPTS / "goalflight_context_meter.py").read_text(encoding="utf-8")

    assert "gives up status freshness" in dispatch_source
    assert "prelaunch mirror gives up only early status freshness" in dispatch_source
    assert "Projection is derived and retried by reconciliation" in ledger_source
    assert "convenience nudge, never task completion or terminal mail" in watch_source
    assert "gives up only stale-file cleanup" in permits_source
    assert "leaves only sweepable IPC cruft" in permits_source
    assert "optional context-usage telemetry" in context_source
    assert "Optional seat-cache freshness only" in dispatch_source
    assert "Optional seat-cache freshness only" in watch_source
