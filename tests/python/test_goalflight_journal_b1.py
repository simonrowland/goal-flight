"""Journal startup, holder, and integrity regressions."""

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


def _corrupt_table_root(path: Path, table: str) -> None:
    with contextlib.closing(sqlite3.connect(path)) as connection:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        root_page = int(
            connection.execute(
                "SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()[0]
        )
    with path.open("r+b") as handle:
        handle.seek((root_page - 1) * page_size)
        handle.write(b"\x00")
        handle.flush()
        os.fsync(handle.fileno())


def test_in_place_corruption_rechecks_within_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    assert authority.prepare_attempt("corrupt-bound").committed
    _checkpoint(authority.path)
    _corrupt_table_root(authority.path, "dispatch_attempts")
    key = authority._integrity_cache_key()
    journal._INTEGRITY_CHECKED_AT[key] = (
        journal.time.monotonic() - journal.INTEGRITY_CHECK_INTERVAL_S
    )

    with pytest.raises(journal.JournalIntegrityError, match="integrity check failed"):
        journal.Journal(project)


def test_in_place_corruption_is_reported_on_first_corrupt_read_and_refuses_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    assert authority.prepare_attempt("corrupt-read").committed
    _checkpoint(authority.path)
    _corrupt_table_root(authority.path, "dispatch_attempts")

    with pytest.raises(journal.JournalIntegrityError, match="integrity check failed"):
        authority.read_all("SELECT * FROM dispatch_attempts")
    with pytest.raises(journal.JournalIntegrityError, match="Failing closed"):
        authority.prepare_attempt("refused-after-corruption")


def test_unknown_future_user_version_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    with sqlite3.connect(authority.path) as connection:
        connection.execute("PRAGMA user_version = 7")

    with pytest.raises(journal.JournalUpgradeRequired, match="user_version=7"):
        journal.Journal(project)
    with contextlib.closing(sqlite3.connect(authority.path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7


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
