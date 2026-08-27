#!/usr/bin/env python3
"""Fleet-wide controller diagnostic: connected / idle / stale / disconnected."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
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

import goalflight_controllers as controllers  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_task as task  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key, value in {
        "GOALFLIGHT_TASK_STORE_DIR": tmp_path / "task-store",
        "GOALFLIGHT_TASK_STORE": tmp_path / "task-store",
        "GOALFLIGHT_JOURNAL_DIR": tmp_path / "journal",
        "GOALFLIGHT_MESSAGES_DIR": tmp_path / "messages",
        "GOALFLIGHT_STATE_DIR": tmp_path / "state",
        "GOALFLIGHT_WAKE_LEDGER_DIR": tmp_path / "wake-ledger",
        "GOALFLIGHT_PIDFILE_DIR": tmp_path / "pidfiles",
        "GOAL_FLIGHT_PIDFILE_DIR": tmp_path / "pidfiles",
    }.items():
        monkeypatch.setenv(key, str(value))
        Path(value).mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("GOALFLIGHT_WAKE_LEDGER", raising=False)
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    root = tmp_path / "project"
    root.mkdir()
    return root


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def _run(argv: list[str] | None = None) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = controllers.main(argv or [])
    return code, buf.getvalue()


def _hold_registered_lease(root: Path, label: str, session_id: str):
    registered = sessions.register_controller(root, label, session_id=session_id)
    assert registered["registered"] is True
    nonce = registered["session"]["lease_nonce"]
    holder = wake.register_lease_holder(
        root,
        controller_label=label,
        lease_nonce=nonce,
    )
    return registered, holder, nonce


def _arm_listener(root: Path, label: str, nonce: str):
    return wake.register_waiter(
        root,
        controller_label=label,
        kind="listener",
        generation_key=nonce,
    )


def _set_lease_times(
    root: Path,
    label: str,
    *,
    renewed_at: datetime,
    renew_deadline_at: datetime,
) -> None:
    authority = journal.Journal(root)
    lease = authority.active_lease(label)
    assert lease is not None
    updated = authority.write(
        journal.RowOperation.update(
            "controller_leases",
            {
                "renewed_at": renewed_at.astimezone(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "renew_deadline_at": renew_deadline_at.astimezone(
                    timezone.utc
                ).isoformat(timespec="seconds"),
            },
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


def _claim_lease(root: Path, label: str, principal: dict, nonce: str):
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        label,
        principal=principal,
        nonce=nonce,
    )
    assert claimed.committed and claimed.value is not None
    return claimed.value


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


def _expire_past_dead_holder_margin(root: Path, label: str) -> None:
    past = datetime.now(timezone.utc) - timedelta(
        seconds=journal.DEFAULT_LEASE_HORIZON_S + 60
    )
    _set_lease_times(
        root,
        label,
        renewed_at=past,
        renew_deadline_at=past,
    )


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


def _row_for(payload: dict, label: str) -> dict:
    matches = [
        row for row in payload["controllers"] if row.get("label") == label
    ]
    assert matches, f"missing controller {label}: {payload['controllers']}"
    return matches[0]


def test_json_keys_are_stable() -> None:
    assert controllers.JSON_ROW_KEYS == (
        "bucket",
        "label",
        "project",
        "project_root",
        "state",
        "idle_seconds",
        "idle",
        "unread",
        "last_drain_at",
        "last_drain_seconds",
        "owned",
        "supervisor",
        "wake_armed",
        "occupies",
        "retire_command",
        "unknown_reason",
        "retirement_eligible",
        "retirement_reason",
    )
    assert controllers.TABLE_COLUMNS == (
        "BUCKET",
        "LABEL",
        "PROJECT",
        "STATE",
        "IDLE",
        "UNREAD",
        "OWNED",
        "SUPERVISOR",
    )


def test_holder_state_never_collapses_unknown_into_dead() -> None:
    assert controllers.holder_state("live-lock") == "live"
    assert controllers.holder_state("live-overdue") == "live"
    assert controllers.holder_state("dead-lock") == "dead"
    assert controllers.holder_state("ended") == "dead"
    assert controllers.holder_state("unknown-lock") == "unknown"
    assert controllers.holder_state("garbled") == "unknown"
    assert controllers.holder_state(None) == "unknown"


def test_live_armed_renders_connected_with_raw_idle_age(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    root = _project(tmp_path, "alpha")
    registered, holder, nonce = _hold_registered_lease(root, "alice", "alice-nonce")
    del registered
    now = datetime.now(timezone.utc)
    _set_lease_times(
        root,
        "alice",
        renewed_at=now - timedelta(minutes=23, seconds=30),
        renew_deadline_at=now + timedelta(hours=2),
    )
    waiter = _arm_listener(root, "alice", nonce)
    try:
        code, text = _run(["--idle-hours", "4"])
        json_code, json_text = _run(["--json", "--idle-hours", "4"])
    finally:
        waiter.close()
        holder.close()
    assert code == 0
    assert json_code == 0
    payload = json.loads(json_text)
    row = _row_for(payload, "alice")
    assert row["bucket"] == "connected"
    assert row["state"] == "live"
    assert row["supervisor"] == "armed"
    assert row["idle"] == "idle 23m"
    assert row["retire_command"] is None
    assert "connected" in text
    assert "idle 23m" in text
    assert "alice" in text


def test_live_quiet_renders_idle_with_the_same_raw_age(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    root = _project(tmp_path, "alpha")
    _registered, holder, nonce = _hold_registered_lease(root, "alice", "alice-nonce")
    now = datetime.now(timezone.utc)
    _set_lease_times(
        root,
        "alice",
        renewed_at=now - timedelta(minutes=23, seconds=30),
        renew_deadline_at=now + timedelta(hours=2),
    )
    waiter = _arm_listener(root, "alice", nonce)
    try:
        connected_code, connected_text = _run(["--json", "--idle-hours", "4"])
        idle_code, idle_text = _run(["--json", "--idle-hours", "0.01"])
        table_code, table_text = _run(["--idle-hours", "0.01"])
    finally:
        waiter.close()
        holder.close()
    assert connected_code == idle_code == table_code == 0
    connected = _row_for(json.loads(connected_text), "alice")
    quiet = _row_for(json.loads(idle_text), "alice")
    assert connected["idle"] == quiet["idle"] == "idle 23m"
    assert connected["bucket"] == "connected"
    assert quiet["bucket"] == "idle"
    assert quiet["state"] == "live"
    assert "idle" in table_text
    assert "idle 23m" in table_text


def test_dead_holder_occupying_lease_renders_stale_with_retire_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    root = _project(tmp_path, "poisoned")
    lease = _claim_lease(root, "pm2", _reaped_principal(), "pm2-nonce")
    holder = wake.register_lease_holder(
        root,
        controller_label="pm2",
        lease_nonce=lease.nonce,
    )
    holder.close()
    _expire_past_dead_holder_margin(root, "pm2")
    probe_state, _session = sessions.probe_live_session(root, label="pm2")
    assert probe_state == "dead"
    assert journal.Journal.open_reader(root).active_lease("pm2") is not None
    code, text = _run(["--idle-hours", "4"])
    json_code, json_text = _run(["--json"])
    assert code == 0
    assert json_code == 0
    payload = json.loads(json_text)
    row = _row_for(payload, "pm2")
    assert row["bucket"] == "stale"
    assert row["state"] == "dead"
    assert row["occupies"] is True
    assert row["retire_command"] is not None
    assert "--retire" in row["retire_command"]
    assert "--acknowledge-retirement" in row["retire_command"]
    assert str(task.resolve_project_root(str(root))) in row["retire_command"]
    assert "stale" in text.lower()
    assert "retire (proof of death):" in text
    assert row["retire_command"] in text
    del lease


def test_unreadable_lease_renders_unknown_without_retire_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    root = _project(tmp_path, "alpha")
    registered, holder, nonce = _hold_registered_lease(root, "alice", "alice-nonce")
    lock_path = None
    try:
        lock_path = _make_generation_lock_unreadable(root, "alice", nonce)
        authority = journal.Journal.open_reader(root)
        with task.FileLock(journal.journal_write_lock_path(authority.path)):
            code, text = _run([])
            json_code, json_text = _run(["--json"])
        assert code == 0
        assert json_code == 0
        payload = json.loads(json_text)
        row = _row_for(payload, "alice")
        assert row["state"] == "unknown"
        assert row["retire_command"] is None
        assert row["unknown_reason"]
        assert "retirement refused" in row["unknown_reason"]
        assert "unknown" in text
        assert "retire (proof of death):" not in text
        assert "--retire" not in text
    finally:
        if lock_path is not None:
            lock_path.chmod(0o600)
        holder.close()
    del registered


def test_unreadable_project_does_not_drop_other_projects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    live_root = _project(tmp_path, "liveproj")
    busy_root = _project(tmp_path, "busyproj")
    registered, holder, nonce = _hold_registered_lease(live_root, "alice", "alice-nonce")
    waiter = _arm_listener(live_root, "alice", nonce)
    _claim_lease(busy_root, "bob", _reaped_principal(), "bob-nonce")
    busy_journal = journal.resolve_journal_path(busy_root)
    try:
        with sqlite3.connect(
            busy_journal,
            timeout=0,
            isolation_level=None,
        ) as blocker:
            assert blocker.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
            blocker.execute("BEGIN EXCLUSIVE")
            code, text = _run([])
            json_code, json_text = _run(["--json"])
        assert code == 0
        assert json_code == 0
        payload = json.loads(json_text)
        labels = {row.get("label") for row in payload["controllers"]}
        assert "alice" in labels
        unknown_rows = [
            row for row in payload["controllers"] if row["state"] == "unknown"
        ]
        assert unknown_rows
        assert all(row["retire_command"] is None for row in unknown_rows)
        assert "alice" in text
        assert "unknown" in text
    finally:
        waiter.close()
        holder.close()
    del registered


def test_disconnected_retired_row_holds_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    root = _project(tmp_path, "alpha")
    registered = sessions.register_controller(root, "old", session_id="old-nonce")
    assert registered["registered"] is True
    retired = sessions.retire_controller(
        root,
        "old",
        session_id="old-nonce",
        acknowledge=True,
        ledger_records=[],
    )
    assert retired["retired"] is True
    code, text = _run(["--json"])
    assert code == 0
    row = _row_for(json.loads(text), "old")
    assert row["bucket"] == "disconnected"
    assert row["occupies"] is False
    assert row["retire_command"] is None
    assert row["idle"] == "—"


def test_owned_unmeasured_renders_unknown_never_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    root = _project(tmp_path, "alpha")
    registered, holder, nonce = _hold_registered_lease(root, "alice", "alice-nonce")
    waiter = _arm_listener(root, "alice", nonce)

    import goalflight_ledger

    def boom():
        raise OSError("ledger unreadable")

    monkeypatch.setattr(goalflight_ledger, "read_records", boom)
    try:
        code, text = _run([])
        json_code, json_text = _run(["--json"])
    finally:
        waiter.close()
        holder.close()
    assert code == json_code == 0
    row = _row_for(json.loads(json_text), "alice")
    assert row["owned"] is None
    alice_lines = [line for line in text.splitlines() if "alice" in line]
    assert alice_lines
    assert "unknown" in alice_lines[-1]
    del registered


def test_command_is_read_only_under_held_write_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    root = _project(tmp_path, "alpha")
    registered, holder, nonce = _hold_registered_lease(root, "alice", "alice-nonce")
    waiter = _arm_listener(root, "alice", nonce)
    source = Path(controllers.__file__).read_text(encoding="utf-8")
    assert "controller_roster" in source
    assert "release_lease" not in source
    assert "retire_controller" not in source
    assert "Journal(" not in source
    authority = journal.Journal.open_reader(root)
    try:
        with task.FileLock(journal.journal_write_lock_path(authority.path)):
            code, text = _run([])
        assert code == 0
        assert "alice" in text
    finally:
        waiter.close()
        holder.close()
    del registered
    del nonce


def test_session_status_all_projects_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    root = _project(tmp_path, "alpha")
    registered, holder, nonce = _hold_registered_lease(root, "alice", "alice-nonce")
    waiter = _arm_listener(root, "alice", nonce)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = sessions.main(["--list-controllers", "--all-projects", "--json"])
    finally:
        waiter.close()
        holder.close()
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert payload["schema"] == controllers.SCHEMA
    assert _row_for(payload, "alice")["label"] == "alice"
    del registered


def test_empty_index_is_honest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    code, text = _run([])
    assert code == 0
    assert text.strip() == "no known controllers"
    json_code, json_text = _run(["--json"])
    assert json_code == 0
    payload = json.loads(json_text)
    assert payload["schema"] == controllers.SCHEMA
    assert payload["controllers"] == []
    assert payload["last_drain_available"] is True
    assert payload["idle_hours"] == 4.0


def test_last_drain_comes_from_journal_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    root = _project(tmp_path, "alpha")
    registered, holder, nonce = _hold_registered_lease(root, "alice", "alice-nonce")
    waiter = _arm_listener(root, "alice", nonce)
    messages.post_message(
        dispatch_id="fleet-mail",
        msg_type="controller-notice",
        payload={"text": "pending"},
        messages_dir=Path(os.environ["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("alice", project_root=root),
    )
    try:
        before = _row_for(json.loads(_run(["--json"])[1]), "alice")
        assert before["unread"] == 1
        assert before["last_drain_at"] is None
        authority = journal.Journal(root)
        lease = authority.active_lease("alice")
        peek = authority.cursor_peek("alice", nonce=lease.nonce, limit=10)
        assert authority.advance_cursor(
            "alice",
            nonce=lease.nonce,
            expected_cursor_version=peek.cursor_version,
            expected_stream_snapshots=peek.stream_snapshots,
            advances={"fleet-mail": 1},
            actor="alice",
        ).committed
        after = _row_for(json.loads(_run(["--json"])[1]), "alice")
        assert after["unread"] == 0
        assert after["last_drain_at"]
        assert after["last_drain_seconds"] is not None
        assert set(after.keys()) == set(controllers.JSON_ROW_KEYS)
    finally:
        waiter.close()
        holder.close()
    del registered
    del nonce
