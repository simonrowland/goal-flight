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
    registered = sessions.register_controller(root, "engine", session_id="engine-nonce")
    assert registered["registered"] is True
    past = datetime.now(timezone.utc) - timedelta(minutes=70)
    _set_renew_deadline(root, "engine", past)
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
    finally:
        lock_path.chmod(0o600)
    assert result["retired"] is True
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
