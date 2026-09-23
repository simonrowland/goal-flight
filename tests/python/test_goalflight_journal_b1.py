"""B1 journal startup, holder, and settled-history regressions."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import sqlite3
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

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


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def _checkpoint(path: Path) -> None:
    with contextlib.closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as connection:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


def test_integrity_check_is_once_per_identity_and_rechecks_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    calls: list[bool] = []
    real_check = journal.Journal._startup_integrity_check

    def counted(current: journal.Journal, **kwargs: object) -> None:
        calls.append(bool(kwargs.get("force", False)))
        real_check(current, **kwargs)

    monkeypatch.setattr(journal.Journal, "_startup_integrity_check", counted)
    authority = journal.Journal.create(project)
    for _ in range(4):
        journal.Journal(project)
    assert calls == [False]

    _checkpoint(authority.path)
    replacement = tmp_path / "replacement.sqlite3"
    shutil.copyfile(authority.path, replacement)
    os.replace(replacement, authority.path)
    for suffix in ("-wal", "-shm"):
        Path(f"{authority.path}{suffix}").unlink(missing_ok=True)

    journal.Journal(project)
    assert calls == [False, False]


def test_current_schema_open_skips_construction_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    journal.Journal.create(project)

    def lock_must_not_be_taken(*_args: object, **_kwargs: object):
        raise AssertionError("current-schema open took the construction lock")

    monkeypatch.setattr(
        journal.goalflight_task.FileLock,
        "try_acquire",
        classmethod(lock_must_not_be_taken),
    )
    journal.Journal(project)


def test_persistent_reader_holds_wal_sidecars_for_short_lived_writers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    reader = journal.Journal.open_reader(project, persistent=True)
    assert reader._reader_connection is not None
    assert not reader._reader_connection.in_transaction
    try:
        first = authority.prepare_attempt("holder-first")
        assert first.committed
        wal = Path(f"{authority.path}-wal")
        shm = Path(f"{authority.path}-shm")
        assert wal.exists() and shm.exists()
        wal_identity = wal.stat().st_ino
        shm_identity = shm.stat().st_ino

        for index in range(20):
            writer = journal.Journal(project)
            result = writer.prepare_attempt(f"holder-{index}")
            assert result.committed
            assert wal.exists() and shm.exists()
            assert wal.stat().st_ino == wal_identity
            assert shm.stat().st_ino == shm_identity

        with contextlib.closing(sqlite3.connect(authority.path, timeout=0, isolation_level=None)) as connection:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        assert checkpoint is not None and checkpoint[0] == 0
        assert wal.stat().st_size < 8 * 1024 * 1024
    finally:
        reader._reader_connection.close()
        reader._reader_connection = None


def test_retention_deletes_settled_rows_but_keeps_live_and_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    live = authority.prepare_attempt("live-row")
    settled = authority.prepare_attempt("settled-row")
    unresolved = authority.prepare_attempt("unresolved-row")
    assert live.committed and settled.committed and unresolved.committed
    assert settled.value is not None and unresolved.value is not None
    settled_commit = authority.commit_terminal(
        settled.value.attempt_id,
        terminal_state="complete",
        observation={"text": "done"},
    )
    unresolved_commit = authority.commit_terminal(
        unresolved.value.attempt_id,
        terminal_state="complete",
        observation={"text": "waiting"},
    )
    assert settled_commit.committed and unresolved_commit.committed

    old = "2020-01-01T00:00:00+00:00"
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            "UPDATE dispatch_attempts SET terminal_at = ?, state_updated_at = ? "
            "WHERE dispatch_id IN (?, ?)",
            (old, old, "settled-row", "unresolved-row"),
        )
        connection.execute(
            "UPDATE terminal_outbox SET projected_at = ? WHERE attempt_id = ?",
            (old, settled.value.attempt_id),
        )

    result = authority.retain_settled_rows(
        older_than="2021-01-01T00:00:00+00:00",
        compact_threshold_bytes=10**12,
    )
    assert result["disposition"] == "committed"
    assert result["deleted"]["dispatch_attempts"] == 1

    with contextlib.closing(sqlite3.connect(authority.path)) as connection:
        rows = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT dispatch_id, lifecycle_state FROM dispatch_attempts"
            )
        }
    assert rows["live-row"] == "PREPARED"
    assert rows["unresolved-row"] == "TERMINAL"
    assert "settled-row" not in rows
