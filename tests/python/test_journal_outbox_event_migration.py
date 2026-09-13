"""Legacy terminal-event constraints must upgrade without losing authority."""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

from test_goalflight_p3 import _project, _set_state_env
from test_ledger_sidecar_terminal_gate import (
    _ledger_row,
    _mark_attempt_running,
    _write_ledger_record,
)

import goalflight_dispatch as dispatch
import goalflight_journal as journal
import goalflight_ledger as ledger
import goalflight_watch as watch


@pytest.fixture
def legacy_outbox(tmp_path, monkeypatch):
    _set_state_env(monkeypatch, tmp_path)
    monkeypatch.delenv(journal.ALLOW_MIGRATION_ENV, raising=False)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    for event_type, terminal in (("result", "complete"), ("blocked", "blocked")):
        attempt = authority.prepare_attempt(f"historical-{event_type}").value
        assert attempt is not None
        assert authority.commit_terminal(
            attempt.attempt_id, terminal_state=terminal,
            observation={"state": terminal}, event_type=event_type,
        ).committed
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            "UPDATE terminal_outbox SET projected_at = created_at, "
            "projection_attempts = 2, projection_error = 'historical retry', "
            "projection_retry_at = created_at, projection_quarantined_at = created_at "
            "WHERE event_type = 'result'"
        )
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'terminal_outbox'"
        ).fetchone()[0]
        old_sql = sql.replace(
            "('result', 'blocked', 'user_need', 'user_confirm')", "('result', 'blocked')"
        )
        assert old_sql != sql
        connection.execute(old_sql.replace("terminal_outbox", "legacy_outbox", 1))
        connection.execute("INSERT INTO legacy_outbox SELECT * FROM terminal_outbox")
        connection.execute("DROP TABLE terminal_outbox")
        connection.execute("ALTER TABLE legacy_outbox RENAME TO terminal_outbox")
        connection.execute(
            "CREATE INDEX terminal_outbox_pending_idx ON terminal_outbox "
            "(projected_at, created_at, attempt_id, transition_id)"
        )
    return project, authority


def test_legacy_event_constraint_requires_explicit_migration(legacy_outbox):
    project, authority = legacy_outbox
    with sqlite3.connect(authority.path) as connection:
        before = list(connection.iterdump())
    # Read-only status can still inspect the legacy journal, without upgrading it.
    assert journal.Journal.open_reader(project).epochs().schema == 6
    with pytest.raises(journal.JournalUpgradeRequired, match=r"UPGRADE_REQUIRED:.*migrate"):
        journal.Journal(project, allow_migration=False)
    with sqlite3.connect(authority.path) as connection:
        assert list(connection.iterdump()) == before


def test_interrupted_event_migration_rolls_back_schema_and_rows(legacy_outbox, monkeypatch):
    project, authority = legacy_outbox
    with sqlite3.connect(authority.path) as connection:
        before = list(connection.iterdump())
    original_connect = sqlite3.connect
    dropped = []

    def interrupted_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)

        def authorize(action, arg1, arg2, _database, _source):
            if action == sqlite3.SQLITE_DROP_TABLE:
                dropped.append(arg1)
            if action == sqlite3.SQLITE_ALTER_TABLE:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(sqlite3, "connect", interrupted_connect)
    with pytest.raises((journal.JournalError, sqlite3.DatabaseError), match="not authorized"):
        journal.Journal(project, allow_migration=True)
    assert "terminal_outbox" in dropped, "interruption must follow the old table's drop"
    with sqlite3.connect(authority.path) as connection:
        assert list(connection.iterdump()) == before


@pytest.mark.parametrize("epoch", [4, 6])
def test_legacy_event_migration_preserves_rows_and_is_idempotent(legacy_outbox, epoch):
    project, authority = legacy_outbox
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            "UPDATE journal_epochs SET schema_epoch = ?, protocol_epoch = ?, "
            "registry_epoch = ?, minimum_reader_epoch = ?, minimum_writer_epoch = ?",
            (epoch,) * 5,
        )
        before = connection.execute("SELECT * FROM terminal_outbox ORDER BY event_type").fetchall()
    migrated = journal.Journal(project, allow_migration=True)
    # Exercise every declared event, including the previously working kinds.
    for event_type in ("result", "blocked", "user_need", "user_confirm"):
        attempt = migrated.prepare_attempt(f"new-{event_type}").value
        assert attempt is not None
        assert migrated.commit_terminal(
            attempt.attempt_id, terminal_state="blocked", observation={}, event_type=event_type,
        ).committed
    with sqlite3.connect(authority.path) as connection:
        after = connection.execute(
            "SELECT * FROM terminal_outbox WHERE recipient LIKE 'historical-%' ORDER BY event_type"
        ).fetchall()
        assert after == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'terminal_outbox_pending_idx'"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute("UPDATE terminal_outbox SET event_type = 'invalid'")
        first = list(connection.iterdump())
    journal.Journal(project, allow_migration=True)
    with sqlite3.connect(authority.path) as connection:
        assert list(connection.iterdump()) == first


@pytest.mark.parametrize("marker_kind", ["USER-NEED", "USER-CONFIRM", "BLOCKED"])
def test_worker_exit_on_legacy_journal_stamps_ledger_and_admits_followup(
    legacy_outbox, tmp_path, marker_kind,
):
    project, authority = legacy_outbox
    dispatch_id = "terminal-worker"
    journal.Journal(project, allow_migration=True)
    tail = tmp_path / "worker.tail"
    with tail.open("w") as output:
        worker = subprocess.Popen(
            [sys.executable, "-c", "import sys; print(sys.argv[1], flush=True); sys.stdin.readline()",
             f"!{marker_kind}: {dispatch_id} — choose target"],
            stdin=subprocess.PIPE, stdout=output, text=True,
        )
    try:
        identity = ledger.process_identity(worker.pid)
        assert identity
        _mark_attempt_running(project, dispatch_id, identity)
        status_path = tmp_path / "worker.status.json"
        record = _write_ledger_record(
            project, dispatch_id=dispatch_id, status_path=status_path,
            worker_pid=worker.pid, worker_identity=identity,
        )
        record["task_ids"] = ["t-1"]
        ledger.write_record(record)
        # Same running ledger row still blocks a follow-up, even without a sidecar.
        assert dispatch._ledger_task_ids_advanced(
            ["t-1"], self_dispatch_id="followup", self_project_root=str(project),
        )[1] == 1
        worker.communicate("exit\n", timeout=10)
        assert worker.returncode == 0
        assert ledger.worker_identity_liveness(record)[0] == "dead"
        scan = watch.IncrementalTailScanner(tail).scan()
        assert scan.terminal is not None and scan.terminal["kind"] == marker_kind
        error = watch._finish_existing_ledger(
            dispatch_id, watch._marker_state(scan.terminal), f"marker:{marker_kind}",
            worker_still_alive=False, terminal_marker=scan.terminal,
        )
        assert error is None, error
        settled = _ledger_row(dispatch_id)
        assert settled["ended_at"]
        assert settled["state"] == settled["terminal_state"] == "blocked"
        assert dispatch._ledger_task_ids_advanced(
            ["t-1"], self_dispatch_id="followup", self_project_root=str(project),
        ) == (0, 0, "conclusive")
        outbox = authority.read_all(
            "SELECT event_type FROM terminal_outbox WHERE recipient = ?", (dispatch_id,),
        )
        assert [row["event_type"] for row in outbox] == [marker_kind.lower().replace("-", "_")]
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=10)
