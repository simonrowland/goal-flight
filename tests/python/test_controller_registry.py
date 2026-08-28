#!/usr/bin/env python3
"""Journal-backed controller lease and roster contracts."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_task as task  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key, value in {
        "GOALFLIGHT_TASK_STORE_DIR": tmp_path / "task-store",
        "GOALFLIGHT_JOURNAL_DIR": tmp_path / "journal",
        "GOALFLIGHT_MESSAGES_DIR": tmp_path / "messages",
        "GOALFLIGHT_STATE_DIR": tmp_path / "state",
        "GOALFLIGHT_WAKE_LEDGER_DIR": tmp_path / "wake-ledger",
        "GOALFLIGHT_PIDFILE_DIR": tmp_path / "pidfiles",
        "GOAL_FLIGHT_PIDFILE_DIR": tmp_path / "pidfiles",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_register_conflict_and_explicit_join_create_one_active_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    first = sessions.register_controller(root, "engine", session_id="first-nonce")
    assert first["registered"] is True
    holder = wake.register_lease_holder(
        root,
        controller_label="engine",
        lease_nonce=first["session"]["lease_nonce"],
    )
    try:
        conflict = sessions.register_controller(root, "engine", session_id="second-nonce")
        assert conflict["registered"] is False
        assert conflict["reason"] == "label_in_use"
        joined = sessions.join_controller(
            root,
            "engine",
            session_id="second-nonce",
            acknowledge_conflict=True,
        )
    finally:
        holder.close()
    assert joined["joined"] is True and joined["succession"] is True
    authority = journal.Journal(root)
    assert authority.active_lease("engine").nonce == "second-nonce"
    assert sum(row["state"] == "ACTIVE" for row in authority.lease_records(include_ended=True)) == 1


def test_production_register_requires_a_session_beacon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    result = sessions.register_controller(
        root,
        "engine",
        session_id="engine-nonce",
        hold_lock=True,
    )
    assert result == {"registered": False, "reason": "missing_session_beacon"}
    assert not journal.resolve_journal_path(root).exists()


def test_roster_unread_is_journal_cursor_derived(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    registered = sessions.register_controller(root, "engine", session_id="engine-nonce")
    assert registered["registered"] is True
    messages.post_message(
        dispatch_id="roster-mail",
        msg_type="controller-notice",
        payload={"text": "pending"},
        messages_dir=Path(os.environ["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("engine", project_root=root),
    )
    roster = sessions.controller_roster(root, ledger_records=[])
    assert roster["controllers"][0]["unread_addressed_mail"] == 1
    authority = journal.Journal(root)
    lease = authority.active_lease("engine")
    peek = authority.cursor_peek("engine", nonce=lease.nonce, limit=10)
    assert authority.advance_cursor(
        "engine",
        nonce=lease.nonce,
        expected_cursor_version=peek.cursor_version,
        expected_stream_snapshots=peek.stream_snapshots,
        advances={"roster-mail": 1},
        actor="engine",
    ).committed
    assert (
        sessions.controller_roster(root, ledger_records=[])["controllers"][0][
            "unread_addressed_mail"
        ]
        == 0
    )


def test_retirement_releases_lease_without_legacy_mail_cursor_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    registered = sessions.register_controller(root, "engine", session_id="engine-nonce")
    assert registered["registered"] is True
    result = sessions.retire_controller(
        root,
        "engine",
        session_id="engine-nonce",
        acknowledge=True,
        ledger_records=[],
    )
    assert result["retired"] is True
    assert "release_reason" not in result
    assert journal.Journal(root).active_lease("engine") is None
    ended = journal.Journal(root).lease_records(include_ended=True)
    assert ended[0]["ended_reason"] == "retired"
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"] )
    assert not (messages_dir / ".read-cursor.json").exists()
    assert not (messages_dir / ".ack-cursor.json").exists()


def _hold_registered_lease(root: Path, label: str, session_id: str):
    registered = sessions.register_controller(root, label, session_id=session_id)
    assert registered["registered"] is True
    holder = wake.register_lease_holder(
        root,
        controller_label=label,
        lease_nonce=registered["session"]["lease_nonce"],
    )
    return registered, holder


def _set_renew_deadline(root: Path, label: str, when: datetime) -> str:
    authority = journal.Journal(root)
    lease = authority.active_lease(label)
    assert lease is not None
    deadline = when.astimezone(timezone.utc).isoformat(timespec="seconds")
    updated = authority.write(
        journal.RowOperation.update(
            "controller_leases",
            {"renew_deadline_at": deadline},
            where={
                "project_root": str(authority.project_root),
                "label": label,
                "generation": lease.generation,
            },
            row_cap=1,
            expected_rows=1,
        )
    )
    assert updated.committed
    return deadline


def _list_controllers_text(root: Path) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = sessions.main(["--project-root", str(root), "--list-controllers"])
    assert code == 0
    return buf.getvalue()


def test_incarnation_state_names_live_overdue_without_calling_it_dead() -> None:
    now = datetime(2026, 8, 27, 1, 36, tzinfo=timezone.utc)
    overdue = {"renew_deadline_at": "2026-08-27T00:26:00+00:00"}
    state, live = sessions._incarnation_state(overdue, lease_lock_live=True, now=now)
    assert (state, live) == ("live-overdue", True)

    healthy = {"renew_deadline_at": "2026-08-27T03:00:00+00:00"}
    state, live = sessions._incarnation_state(healthy, lease_lock_live=True, now=now)
    assert (state, live) == ("live-lock", True)

    state, live = sessions._incarnation_state(overdue, lease_lock_live=False, now=now)
    assert (state, live) == ("dead-lock", False)

    ended = {
        "retired_at": "2026-08-27T00:00:00+00:00",
        "renew_deadline_at": "2026-08-27T00:26:00+00:00",
    }
    state, live = sessions._incarnation_state(ended, lease_lock_live=True, now=now)
    assert (state, live) == ("ended", False)

    missing = sessions._incarnation_state({}, lease_lock_live=True, now=now)
    assert missing == ("live-lock", True)

    state, live = sessions._incarnation_state(overdue, lease_lock_live=None, now=now)
    assert (state, live) == ("unknown-lock", None)
    state, live = sessions._incarnation_state(healthy, lease_lock_live=None, now=now)
    assert (state, live) == ("unknown-lock", None)
    state, live = sessions._incarnation_state(ended, lease_lock_live=None, now=now)
    assert (state, live) == ("ended", False)


def test_list_controllers_shows_overdue_live_lock_and_names_renew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    _registered, holder = _hold_registered_lease(root, "engine", "engine-nonce")
    try:
        past = datetime.now(timezone.utc) - timedelta(minutes=70)
        _set_renew_deadline(root, "engine", past)
        roster = sessions.controller_roster(root, ledger_records=[])
        record = roster["controllers"][0]
        assert record["incarnation_state"] == "live-overdue"
        assert record["lease_lock_live"] is True
        line = sessions.controller_roster_lines(roster)[0]
        assert "live-overdue" in line
        assert "lease overdue" in line
        assert "renew" in line
        assert "--join" in line
        text = _list_controllers_text(root)
        assert "live-overdue" in text
        assert "lease overdue" in text
        assert "renew" in text
        json_buf = io.StringIO()
        with redirect_stdout(json_buf):
            code = sessions.main(
                ["--project-root", str(root), "--list-controllers", "--json"]
            )
        assert code == 0
        payload = json.loads(json_buf.getvalue())
        assert payload["controllers"][0]["incarnation_state"] == "live-overdue"
        assert payload["controllers"][0]["lease_lock_live"] is True
    finally:
        holder.close()


def test_list_controllers_inside_deadline_stays_live_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    _registered, holder = _hold_registered_lease(root, "engine", "engine-nonce")
    try:
        roster = sessions.controller_roster(root, ledger_records=[])
        record = roster["controllers"][0]
        assert record["incarnation_state"] == "live-lock"
        assert record["lease_lock_live"] is True
        line = sessions.controller_roster_lines(roster)[0]
        assert "live-lock" in line
        assert "overdue" not in line
        assert "renew" not in line
        text = _list_controllers_text(root)
        assert "live-lock" in text
        assert "overdue" not in text
        assert "renew" not in text
    finally:
        holder.close()


def test_list_controllers_dead_lock_stays_dead_when_deadline_elapsed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    _registered, holder = _hold_registered_lease(root, "engine", "engine-nonce")
    holder.close()
    past = datetime.now(timezone.utc) - timedelta(minutes=70)
    _set_renew_deadline(root, "engine", past)
    probe_state, _session = sessions.probe_live_session(root, label="engine")
    assert probe_state == "dead"
    roster = sessions.controller_roster(root, ledger_records=[])
    record = roster["controllers"][0]
    assert record["incarnation_state"] == "dead-lock"
    assert record["lease_lock_live"] is False
    line = sessions.controller_roster_lines(roster)[0]
    assert "dead-lock" in line
    assert "live-overdue" not in line
    assert "renew" not in line
    text = _list_controllers_text(root)
    assert "dead-lock" in text
    assert "live-overdue" not in text
    assert "renew" not in text


def test_roster_unreadable_lock_probe_is_unknown_not_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """open_reader succeeds; probe_live_session is unreadable → unknown-lock.

    Reverting the roster to ``live_session(...) is not None`` maps this
    fixture to ``dead-lock``. An overdue live holder must not become
    ``live-overdue`` either — that token requires a proven live lock.
    """
    root = _isolate(monkeypatch, tmp_path)
    registered, holder = _hold_registered_lease(root, "engine", "engine-nonce")
    nonce = registered["session"]["lease_nonce"]
    lock_path = None
    try:
        past = datetime.now(timezone.utc) - timedelta(minutes=70)
        _set_renew_deadline(root, "engine", past)
        lock_path = _make_generation_lock_unreadable(root, "engine", nonce)
        authority = journal.Journal.open_reader(root)
        with task.FileLock(journal.journal_write_lock_path(authority.path)):
            probe_state, session = sessions.probe_live_session(root, label="engine")
            assert probe_state == "unreadable"
            assert session is None
            assert authority.lease_records()
            roster = sessions.controller_roster(root, ledger_records=[])
            text = _list_controllers_text(root)
            json_buf = io.StringIO()
            with redirect_stdout(json_buf):
                code = sessions.main(
                    ["--project-root", str(root), "--list-controllers", "--json"]
                )
        assert code == 0
        assert roster["controllers"], "registry read must succeed under the write lock"
        record = roster["controllers"][0]
        assert record["incarnation_state"] == "unknown-lock"
        assert record["lease_lock_live"] is None
        assert record["incarnation_state"] != "dead-lock"
        assert record["incarnation_state"] != "live-overdue"
        line = sessions.controller_roster_lines(roster)[0]
        assert "unknown-lock" in line
        assert "dead-lock" not in line
        assert "live-overdue" not in line
        assert "unknown-lock" in text
        assert "dead-lock" not in text
        assert "no known controllers" not in text
        payload = json.loads(json_buf.getvalue())
        assert payload["controllers"][0]["incarnation_state"] == "unknown-lock"
        assert payload["controllers"][0]["lease_lock_live"] is None
    finally:
        if lock_path is not None:
            lock_path.chmod(0o600)
        holder.close()


def test_roster_busy_registry_is_unreadable_not_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Busy registry read must not render as an empty / 'no controllers' roster."""
    root = _isolate(monkeypatch, tmp_path)
    _registered, holder = _hold_registered_lease(root, "engine", "engine-nonce")
    try:
        authority = journal.Journal.open_reader(root)
        with sqlite3.connect(
            authority.path,
            timeout=0,
            isolation_level=None,
        ) as blocker:
            assert blocker.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
            blocker.execute("BEGIN EXCLUSIVE")
            roster = sessions.controller_roster(root, ledger_records=[])
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = sessions.main(
                    ["--project-root", str(root), "--list-controllers"]
                )
            text = buf.getvalue()
            json_buf = io.StringIO()
            with redirect_stdout(json_buf):
                json_code = sessions.main(
                    ["--project-root", str(root), "--list-controllers", "--json"]
                )
        assert code == 0
        assert json_code == 0
        registry = roster["measurements"]["controller_registry"]
        assert registry["measured"] is False
        assert registry["error"] == "JournalBusy"
        lines = sessions.controller_roster_lines(roster)
        assert lines
        assert "unreadable" in lines[0]
        assert "no known controllers" not in lines[0]
        assert "no known controllers" not in text
        assert "unreadable" in text
        payload = json.loads(json_buf.getvalue())
        assert payload["measurements"]["controller_registry"]["measured"] is False
        assert payload["measurements"]["controller_registry"]["error"] == "JournalBusy"
        assert "no known controllers" not in json_buf.getvalue()
    finally:
        holder.close()


def test_roster_disappeared_journal_is_honest_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    roster = sessions.controller_roster(root, ledger_records=[])
    assert roster["controllers"] == []
    registry = roster["measurements"]["controller_registry"]
    assert registry["measured"] is True
    assert registry["error"] is None
    assert sessions.controller_roster_lines(roster) == []
    text = _list_controllers_text(root)
    assert "no known controllers" in text
    assert "unreadable" not in text


def _reaped_principal() -> dict:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    identity = sessions.goalflight_compat.process_start_identity(proc.pid)
    assert identity is not None
    proc.kill()
    proc.wait(timeout=5)
    assert sessions.goalflight_compat.pid_liveness(proc.pid) is False
    return {"pid": int(identity["pid"]), "start_token": str(identity["start_token"])}


def _live_principal() -> dict:
    pid = os.getpid()
    identity = sessions.goalflight_compat.process_start_identity(pid)
    assert identity is not None
    assert sessions.goalflight_compat.pid_liveness(pid) is True
    return {"pid": pid, "start_token": str(identity["start_token"])}


def _claim_lease(root: Path, label: str, principal: dict, nonce: str):
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        label,
        principal=principal,
        nonce=nonce,
    )
    assert claimed.committed and claimed.value is not None
    return claimed.value


def _expire_past_dead_holder_margin(root: Path, label: str) -> str:
    past = datetime.now(timezone.utc) - timedelta(
        seconds=journal.DEFAULT_LEASE_HORIZON_S + 60
    )
    return _set_renew_deadline(root, label, past)


def _make_generation_lock_unreadable(root: Path, label: str, nonce: str) -> Path:
    path = wake._generation_lock_path(
        root,
        kind=wake.LEASE_KIND,
        label=wake._lease_lock_identity(label, nonce),
        generation_key=nonce,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(0o000)
    assert (
        wake.lease_holder_alive(root, controller_label=label, lease_nonce=nonce)
        is None
    )
    return path


def _owned_running_record(root: Path, label: str, dispatch_id: str) -> dict:
    resolved = sessions.goalflight_task.resolve_project_root(str(root))
    return {
        "controller_label": label,
        "project_root": str(resolved.resolve()),
        "dispatch_id": dispatch_id,
        "state": "running",
    }


def test_dead_holder_unreadable_lock_retires_without_nonce(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    lease = _claim_lease(root, "engine", _reaped_principal(), "engine-nonce")
    _expire_past_dead_holder_margin(root, "engine")
    lock_path = _make_generation_lock_unreadable(root, "engine", lease.nonce)
    try:
        result = sessions.retire_controller(
            root,
            "engine",
            acknowledge=True,
            ledger_records=[],
        )
        assert result["retired"] is True
        from goalflight_dispatch import _kernel_live_controller_sessions

        # RETIRED rows must drop out of the ACTIVE scan so this still-unreadable
        # lock cannot poison kernel lookup. include_ended would fail this pin.
        lookup = _kernel_live_controller_sessions(root)
        assert lookup.sessions is not None
        assert isinstance(lookup.sessions, list)
        assert lookup.unreadable_reason is None
    finally:
        lock_path.chmod(0o600)
    assert result["release_reason"] == "retired-dead-holder"
    ended = journal.Journal(root).lease_records(include_ended=True)
    assert len(ended) == 1
    assert ended[0]["state"] == "RETIRED"
    assert ended[0]["ended_reason"] == "retired-dead-holder"
    assert journal.Journal(root).active_lease("engine") is None


def test_dead_holder_inside_horizon_refuses_nonce_less_retirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    lease = _claim_lease(root, "engine", _reaped_principal(), "engine-nonce")
    lock_path = _make_generation_lock_unreadable(root, "engine", lease.nonce)
    try:
        result = sessions.retire_controller(
            root,
            "engine",
            acknowledge=True,
            ledger_records=[],
        )
    finally:
        lock_path.chmod(0o600)
    assert result["retired"] is False
    assert result["reason"] == "renew_deadline_not_past_horizon"
    assert journal.Journal(root).active_lease("engine") is not None


def test_indeterminate_principal_refuses_nonce_less_retirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    principal = _live_principal()
    lease = _claim_lease(root, "engine", principal, "engine-nonce")
    _expire_past_dead_holder_margin(root, "engine")
    stored_pid = int(principal["pid"])
    original_kill = sessions.goalflight_compat.os.kill

    def eperm_kill(pid, sig):
        if pid == stored_pid:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return original_kill(pid, sig)

    monkeypatch.setattr(sessions.goalflight_compat.os, "kill", eperm_kill)
    assert sessions.goalflight_compat.pid_liveness(stored_pid) is None
    result = sessions.retire_controller(
        root,
        "engine",
        acknowledge=True,
        ledger_records=[],
    )
    assert result["retired"] is False
    assert result["reason"] == "holder_liveness_indeterminate"
    message = str(result["message"])
    assert "indeterminate" in message
    assert "could not find out" in message
    assert journal.Journal(root).active_lease("engine") is not None
    assert journal.Journal(root).active_lease("engine").nonce == lease.nonce


def test_dead_holder_with_owned_dispatch_refuses_nonce_less_retirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    lease = _claim_lease(root, "engine", _reaped_principal(), "engine-nonce")
    _expire_past_dead_holder_margin(root, "engine")
    lock_path = _make_generation_lock_unreadable(root, "engine", lease.nonce)
    try:
        result = sessions.retire_controller(
            root,
            "engine",
            acknowledge=True,
            ledger_records=[_owned_running_record(root, "engine", "still-running")],
        )
    finally:
        lock_path.chmod(0o600)
    assert result["retired"] is False
    assert result["reason"] == "dead_holder_owns_nonterminal_dispatches"
    assert result["nonterminal_owned_dispatches"][0]["dispatch_id"] == "still-running"
    assert journal.Journal(root).active_lease("engine") is not None


def test_live_principal_without_nonce_refuses_exactly_as_today(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    lease = _claim_lease(root, "engine", _live_principal(), "engine-nonce")
    result = sessions.retire_controller(
        root,
        "engine",
        acknowledge=True,
        ledger_records=[],
    )
    assert result == {
        "retired": False,
        "reason": "retirer_not_incumbent",
        "message": "retirement requires the active lease nonce",
    }
    assert journal.Journal(root).active_lease("engine").nonce == lease.nonce


def test_missing_stored_principal_refuses_toward_manual_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    lease = _claim_lease(
        root,
        "engine",
        {"principal_id": "no-pid-principal"},
        "engine-nonce",
    )
    _expire_past_dead_holder_margin(root, "engine")
    result = sessions.retire_controller(
        root,
        "engine",
        acknowledge=True,
        ledger_records=[],
    )
    assert result["retired"] is False
    assert result["reason"] == "missing_stored_principal"
    message = str(result["message"]).lower()
    assert "manual" in message
    assert "t-238" in message
    assert journal.Journal(root).active_lease("engine").nonce == lease.nonce


def _raise_journal_init(error: Exception):
    def _init(self, *args, **kwargs):
        raise error

    return _init


@pytest.mark.parametrize(
    "error",
    (
        journal.JournalBusy("journal busy"),
        journal.JournalIOError("journal io"),
    ),
)
def test_retire_and_release_busy_or_io_is_unreadable_not_unregistered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    _claim_lease(root, "engine", _reaped_principal(), "engine-nonce")
    monkeypatch.setattr(
        sessions.goalflight_journal.Journal, "__init__", _raise_journal_init(error)
    )
    retired = sessions.retire_controller(
        root,
        "engine",
        acknowledge=True,
        ledger_records=[],
    )
    assert retired["retired"] is False
    assert retired["reason"] == "registry_unreadable"
    assert retired["error_type"] == type(error).__name__
    assert retired["message"]
    released = sessions.release_session(root, pid=os.getpid())
    assert released["released"] is False
    assert released["reason"] == "registry_unreadable"
    assert released["error_type"] == type(error).__name__
    assert released["message"]


def test_retire_and_release_disappeared_journal_is_not_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    _claim_lease(root, "engine", _reaped_principal(), "engine-nonce")
    monkeypatch.setattr(
        sessions.goalflight_journal.Journal,
        "__init__",
        _raise_journal_init(journal.JournalDisappeared("journal disappeared")),
    )
    retired = sessions.retire_controller(
        root,
        "engine",
        acknowledge=True,
        ledger_records=[],
    )
    assert retired == {"retired": False, "reason": "controller_not_registered"}
    released = sessions.release_session(root, pid=os.getpid())
    assert released == {"released": False, "reason": "controller_not_registered"}


def test_unmeasured_owned_dispatches_refuse_nonce_less_retirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    lease = _claim_lease(root, "engine", _reaped_principal(), "engine-nonce")
    _expire_past_dead_holder_margin(root, "engine")
    lock_path = _make_generation_lock_unreadable(root, "engine", lease.nonce)
    monkeypatch.setattr(
        sessions,
        "_nonterminal_owned_dispatches",
        lambda *args, **kwargs: (None, "OSError"),
    )
    try:
        result = sessions.retire_controller(
            root,
            "engine",
            acknowledge=True,
            ledger_records=[],
        )
    finally:
        lock_path.chmod(0o600)
    assert result["retired"] is False
    assert result["reason"] == "owned_dispatches_unmeasured"
    assert result["owned_dispatch_measurement_error"] == "OSError"
    message = str(result["message"]).lower()
    assert "unknown is not zero" in message
    assert journal.Journal(root).active_lease("engine") is not None


def test_missing_renew_deadline_refuses_toward_manual_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    lease = _claim_lease(root, "engine", _reaped_principal(), "engine-nonce")
    authority = journal.Journal(root)
    updated = authority.write(
        journal.RowOperation.update(
            "controller_leases",
            {"renew_deadline_at": ""},
            where={
                "project_root": str(authority.project_root),
                "label": "engine",
                "generation": lease.generation,
            },
            row_cap=1,
            expected_rows=1,
        )
    )
    assert updated.committed
    result = sessions.retire_controller(
        root,
        "engine",
        acknowledge=True,
        ledger_records=[],
    )
    assert result["retired"] is False
    assert result["reason"] == "renew_deadline_unreadable"
    message = str(result["message"]).lower()
    assert "t-238" in message
    assert "manual" in message
    assert "wait" not in message
    assert journal.Journal(root).active_lease("engine").nonce == lease.nonce


def _owned_records(root: Path, label: str, count: int) -> list[dict]:
    return [
        _owned_running_record(root, label, f"owned-{index}")
        for index in range(count)
    ]


def test_overdue_lease_with_owned_dispatches_names_quarantine_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A lapsed renew deadline holding work must name how many a roll would quarantine."""
    root = _isolate(monkeypatch, tmp_path)
    _registered, holder = _hold_registered_lease(root, "engine", "engine-nonce")
    try:
        past = datetime.now(timezone.utc) - timedelta(hours=12, minutes=30)
        _set_renew_deadline(root, "engine", past)
        roster = sessions.controller_roster(
            root, ledger_records=_owned_records(root, "engine", 10)
        )
        record = roster["controllers"][0]
        assert record["incarnation_state"] == "live-overdue"
        assert record["nonterminal_owned_dispatches"] == 10
        line = sessions.controller_roster_lines(roster)[0]
        assert "10" in line
        assert "quarantine" in line.lower()
        alerts = sessions.controller_alert_lines(record)
        joined = " ".join(alerts)
        assert "10" in joined
        assert "quarantine" in joined.lower()
        text = sessions.to_text(
            {
                "active": True,
                "queue_file": "docs-private/goal-queue-demo.md",
                "queue_slug": "demo",
                "queue_last_touched": None,
                "active_capacity_leases_in_project": 0,
                "backlog_counts": {},
                "ready_frontier": {"count": 0},
                "owner_alerts": alerts,
            }
        )
        assert "10" in text
        assert "quarantine" in text.lower()
    finally:
        holder.close()


def test_armed_but_dead_supervisor_is_distinct_from_armed_and_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """wake_armed is generation presence; wake_alive is observed recency."""
    root = _isolate(monkeypatch, tmp_path)
    registered, holder = _hold_registered_lease(root, "engine", "engine-nonce")
    nonce = registered["session"]["lease_nonce"]
    now_epoch = 1_800_000_000.0
    heartbeat_s = 120.0
    dead_after_s = 360.0
    try:
        monkeypatch.setattr(
            wake,
            "supervisor_generation_state",
            lambda *args, **kwargs: wake.SUPERVISOR_RUNNING,
        )
        wake.activate_monitor_state(
            root,
            controller_label="engine",
            lease_nonce=nonce,
            heartbeat_s=heartbeat_s,
            dead_after_s=dead_after_s,
            now_epoch=now_epoch,
        )
        wake.record_monitor_emit(
            root,
            controller_label="engine",
            lease_nonce=nonce,
            record_kind="heartbeat",
            now_epoch=now_epoch,
        )
        live_roster = sessions.controller_roster(
            root,
            ledger_records=_owned_records(root, "engine", 10),
            now=datetime.fromtimestamp(now_epoch, tz=timezone.utc),
        )
        live = live_roster["controllers"][0]
        assert live["wake_armed"] is True
        assert live["wake_alive"] is True
        live_line = sessions.controller_roster_lines(live_roster)[0]
        assert "wake DEAD" not in live_line
        assert "deaf" not in live_line.lower()

        wake.record_monitor_emit(
            root,
            controller_label="engine",
            lease_nonce=nonce,
            record_kind="heartbeat",
            now_epoch=now_epoch - dead_after_s - 1,
        )
        dead_roster = sessions.controller_roster(
            root,
            ledger_records=_owned_records(root, "engine", 10),
            now=datetime.fromtimestamp(now_epoch, tz=timezone.utc),
        )
        dead = dead_roster["controllers"][0]
        assert dead["wake_armed"] is True
        assert dead["wake_alive"] is False
        dead_line = sessions.controller_roster_lines(dead_roster)[0]
        assert "wake DEAD" in dead_line or "deaf" in dead_line.lower()
        alerts = sessions.controller_alert_lines(dead)
        joined = " ".join(alerts).lower()
        assert "armed" in joined
        assert "10" in joined
        assert live["wake_alive"] is not dead["wake_alive"]
        assert "wake DEAD" not in live_line
    finally:
        holder.close()


def test_unarmed_supervisor_with_owned_dispatches_is_an_alarm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    _registered, holder = _hold_registered_lease(root, "engine", "engine-nonce")
    try:
        monkeypatch.setattr(
            wake,
            "supervisor_generation_state",
            lambda *args, **kwargs: wake.SUPERVISOR_ABSENT,
        )
        monkeypatch.setattr(
            wake,
            "coverage_status",
            lambda *args, **kwargs: {
                "covered": False,
                "live_waiters": 0,
                "target_waiters": 4,
            },
        )
        roster = sessions.controller_roster(
            root, ledger_records=_owned_records(root, "engine", 10)
        )
        record = roster["controllers"][0]
        assert record["wake_armed"] is False
        line = sessions.controller_roster_lines(roster)[0]
        alerts = sessions.controller_alert_lines(record)
        blob = f"{line}\n" + "\n".join(alerts)
        assert "10" in blob
        assert "wake" in blob.lower()
    finally:
        holder.close()
