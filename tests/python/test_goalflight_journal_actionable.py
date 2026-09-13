#!/usr/bin/env python3
"""Journal refusals name the operator's next command (t-247)."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402


def _set_state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = {
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
        "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journal-state"),
        "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
        "GOALFLIGHT_STATE_DIR": str(tmp_path / "dispatch-state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp_path / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
        "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
        "GOALFLIGHT_DISABLE_NUDGES": "1",
        "GOALFLIGHT_TEST_MODE": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _project(tmp_path: Path, name: str = "project") -> Path:
    project = tmp_path / name
    project.mkdir()
    return project


def _quiesced_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, journal.Journal]:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    prepared = authority.prepare_attempt("row-survives-quiescence")
    assert prepared.committed and prepared.value is not None
    with contextlib.closing(
        sqlite3.connect(authority.path, timeout=0, isolation_level=None)
    ) as connection:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert checkpoint == (0, 0, 0)
    for suffix in ("-shm", "-wal"):
        Path(f"{authority.path}{suffix}").unlink(missing_ok=True)
    return project, authority


def test_epoch_fence_names_resume_before_arithmetic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    # Derive the journal's epoch from the client's rather than pinning a
    # literal. This test needs a journal exactly one epoch AHEAD of the
    # client; a hard-coded number silently becomes "equal" the moment the
    # client is bumped, and an equal epoch raises nothing, so the test fails
    # claiming the fence is broken when only the fixture went stale. That is
    # what happened when the seat-attribution columns moved the client 5 -> 6.
    ahead = journal.CURRENT_SCHEMA_EPOCH + 1
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            """UPDATE journal_epochs
               SET schema_epoch = ?, protocol_epoch = ?, registry_epoch = ?,
                   minimum_reader_epoch = ?, minimum_writer_epoch = ?
               WHERE singleton = 1""",
            (ahead, ahead, ahead, ahead, ahead),
        )

    with pytest.raises(journal.JournalUpgradeRequired) as captured:
        journal.Journal(project)

    message = str(captured.value)
    assert message.startswith(
        "UPGRADE_REQUIRED: restart this session onto the deployed skill: "
        "/goal-flight resume;"
    )
    assert f"schema client={journal.CURRENT_SCHEMA_EPOCH} journal={ahead}" in message
    assert message.count("\n") == 0
    assert not isinstance(captured.value, journal.JournalIntegrityError)
    assert journal.main(["--project-root", str(project), "inspect"]) == 2


def test_migration_required_names_migrate_command_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    monkeypatch.delenv(journal.ALLOW_MIGRATION_ENV, raising=False)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            """UPDATE journal_epochs
               SET schema_epoch = 4, protocol_epoch = 4, registry_epoch = 4,
                   minimum_reader_epoch = 4, minimum_writer_epoch = 4
               WHERE singleton = 1"""
        )

    with pytest.raises(journal.JournalUpgradeRequired) as captured:
        journal.Journal(project)

    message = str(captured.value)
    assert message.startswith("UPGRADE_REQUIRED: run ")
    assert "goalflight_journal.py" in message
    assert " migrate" in message
    assert "journal epochs=(4, 4, 4, 4, 4)" in message
    assert message.count("\n") == 0
    assert journal.main(["--project-root", str(project), "inspect"]) == 2


def _pending_equal_epoch_rewind_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, journal.Journal]:
    """Epoch-current journal missing the additive rewind table."""
    _set_state_env(monkeypatch, tmp_path)
    monkeypatch.delenv(journal.ALLOW_MIGRATION_ENV, raising=False)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    with sqlite3.connect(authority.path) as connection:
        connection.execute("DROP TABLE IF EXISTS controller_stream_rewinds")
        stored = tuple(
            connection.execute(
                """SELECT schema_epoch, protocol_epoch, registry_epoch,
                          minimum_reader_epoch, minimum_writer_epoch
                   FROM journal_epochs WHERE singleton = 1"""
            ).fetchone()
        )
    assert stored == (
        journal.CURRENT_SCHEMA_EPOCH,
        journal.CURRENT_PROTOCOL_EPOCH,
        journal.CURRENT_REGISTRY_EPOCH,
        journal.CURRENT_READER_EPOCH,
        journal.CURRENT_WRITER_EPOCH,
    )
    return project, authority


def test_pending_structural_migration_permits_snapshot_and_dump(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _authority = _pending_equal_epoch_rewind_journal(monkeypatch, tmp_path)
    snapshot = tmp_path / "pre-migrate.sqlite3"

    assert journal.main(
        ["--project-root", str(project), "snapshot", "--output", str(snapshot)]
    ) == 0
    assert snapshot.is_file()
    with sqlite3.connect(snapshot) as connection:
        assert [str(row[0]) for row in connection.execute("PRAGMA integrity_check")] == [
            "ok"
        ]
    assert journal._validate_snapshot_file(snapshot).schema == journal.CURRENT_SCHEMA_EPOCH

    capsys.readouterr()
    assert journal.main(["--project-root", str(project), "dump"]) == 0
    sql = capsys.readouterr().out
    assert "CREATE TABLE journal_meta" in sql
    rebuilt = tmp_path / "from-dump.sqlite3"
    with sqlite3.connect(rebuilt) as connection:
        connection.executescript(sql)
        assert [str(row[0]) for row in connection.execute("PRAGMA integrity_check")] == [
            "ok"
        ]

    assert journal.main(["--project-root", str(project), "inspect"]) == 0
    with pytest.raises(journal.JournalUpgradeRequired):
        journal.Journal(project, allow_migration=False)


def test_pending_structural_migration_refusal_names_change_and_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, _authority = _pending_equal_epoch_rewind_journal(monkeypatch, tmp_path)

    with pytest.raises(journal.JournalUpgradeRequired) as captured:
        journal.Journal(project, allow_migration=False)

    message = str(captured.value)
    assert message.startswith("UPGRADE_REQUIRED: run ")
    assert "goalflight_journal.py" in message
    assert " migrate" in message
    assert "adds table controller_stream_rewinds" in message
    assert "--controller-startup" in message
    assert '"claimed": true' in message
    assert "inspect" in message
    assert "journal epochs=" not in message
    assert "client epochs=" not in message
    assert message.count("\n") == 0


def test_dual_open_failure_names_doctor_and_journal_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, authority = _quiesced_journal(monkeypatch, tmp_path)

    def unavailable_connect(
        database: str | Path,
        *,
        uri: bool = False,
        timeout: float = 5.0,
        isolation_level: str | None = "",
    ) -> sqlite3.Connection:
        del database, uri, timeout, isolation_level
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(journal, "_sqlite_connect", unavailable_connect)
    reader = journal.Journal.open_reader(project, open_retry_budget_s=0.02)
    with pytest.raises(journal.JournalUnavailable) as captured:
        reader.epochs()

    message = str(captured.value)
    assert "probe unavailable/unreadable" in message
    assert str(authority.path) in message
    assert "next: run " in message
    assert "goalflight_doctor.py" in message
    assert f"inspect {authority.path}" in message
    assert message.count("\n") == 0
    assert not isinstance(captured.value, journal.JournalIntegrityError)
    # Dual-open is a reader fence; inspect uses the write constructor and
    # is a different path. The refusal is that epochs() still raises.
    with pytest.raises(journal.JournalUnavailable, match="probe unavailable/unreadable"):
        reader.epochs()


def test_attention_carrier_is_journal_prefixed_repair_pointer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        "actionable-ctl",
        principal={"principal_id": "actionable-principal"},
    )
    assert claimed.committed and claimed.value is not None
    assert authority.prepare_attempt("actionable-orphaned-work").committed
    armed = authority.arm_listener(
        "actionable-ctl",
        nonce=claimed.value.nonce,
        pid=os.getpid(),
        start_token="actionable-token",
        parent_pid=os.getppid() or os.getpid(),
    )
    assert armed.committed and armed.value is not None
    assert authority.exit_listener(
        str(armed.value["coverage_id"]), reason="orphaned"
    ).committed

    rows = authority.read_all(
        """
        SELECT carrier_path, event_type, wake_class
        FROM delivery_events WHERE event_type = 'controller_attention'
        """
    )
    assert len(rows) == 1
    carrier = str(rows[0]["carrier_path"])
    assert carrier.startswith("journal:")
    assert not carrier.startswith("journal:attention:")
    assert "goal-flight-resume" in carrier
    items = authority.attention_items()
    assert items
    text = str(json.loads(str(items[0]["payload_json"])).get("text") or "")
    assert "/goal-flight resume" in text
    assert rows[0]["wake_class"] == "quiet"
    assert rows[0]["event_type"] == "controller_attention"
