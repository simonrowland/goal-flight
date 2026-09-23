"""Journal startup, holder, and integrity regressions."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_journal as journal  # noqa: E402
import goalflight_wake_supervise as supervise  # noqa: E402


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


def test_current_schema_fast_path_repairs_non_wal_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    with contextlib.closing(
        sqlite3.connect(authority.path, timeout=0, isolation_level=None)
    ) as connection:
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)

    with contextlib.closing(sqlite3.connect(authority.path)) as connection:
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "delete"

    journal.Journal(project)

    with contextlib.closing(sqlite3.connect(authority.path)) as connection:
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"


def test_supervisor_holder_holds_wal_sidecars_before_any_mail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    holder = supervise._JournalWALHolder(authority.path)
    try:
        assert holder.connection is not None
        assert not holder.connection.in_transaction
        assert holder.connection.execute("PRAGMA busy_timeout").fetchone() == (0,)
        wal = Path(f"{authority.path}-wal")
        shm = Path(f"{authority.path}-shm")
        assert wal.exists() and shm.exists()
        # Keep descriptors open so an unlinked inode cannot be reused and
        # accidentally make the identity assertion pass after recreation.
        with wal.open("rb") as wal_fd, shm.open("rb") as shm_fd:
            subprocess.run(
                [sys.executable, "-c", """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import goalflight_journal as journal
project, path = Path(sys.argv[2]), Path(sys.argv[3])
for index in range(64):
    writer = journal.Journal(project)
    assert writer.prepare_attempt(f"holder-{index}").committed
    for suffix, inode in zip(("-wal", "-shm"), sys.argv[4:]):
        assert Path(f"{path}{suffix}").stat().st_ino == int(inode)
""", str(ROOT / "scripts"), str(project), str(authority.path),
                 str(os.fstat(wal_fd.fileno()).st_ino),
                 str(os.fstat(shm_fd.fileno()).st_ino)],
                check=True, timeout=30,
            )
            assert os.fstat(wal_fd.fileno()).st_nlink == 1
            assert os.fstat(shm_fd.fileno()).st_nlink == 1
        with contextlib.closing(sqlite3.connect(authority.path, timeout=0, isolation_level=None)) as connection:
            assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        assert not holder.connection.in_transaction
    finally:
        holder.close()
    assert holder.connection is None


def test_supervisor_holder_retries_busy_open_and_reopens_replaced_inode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    authority = journal.Journal.create(_project(tmp_path))
    with contextlib.closing(sqlite3.connect(authority.path, isolation_level=None)) as blocker:
        blocker.execute("PRAGMA journal_mode = DELETE")
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        holder = supervise._JournalWALHolder(authority.path)
        assert time.monotonic() - started < 0.5
        assert holder.connection is None
        blocker.rollback()
        blocker.execute("PRAGMA journal_mode = WAL")
    try:
        host = supervise.RealHost.__new__(supervise.RealHost)
        host._stop = False
        host._journal_holder = holder
        assert host.running()
        old = holder.connection
        assert old is not None
        replacement = tmp_path / "replacement.sqlite3"
        with contextlib.closing(sqlite3.connect(replacement)) as destination:
            old.backup(destination)
        os.replace(replacement, authority.path)
        assert host.running()
        assert holder.connection is not None
        assert holder.connection is not old
        assert not holder.connection.in_transaction
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            old.execute("SELECT 1")
    finally:
        holder.close()


@pytest.mark.skipif(
    os.environ.get("GOALFLIGHT_RUN_JOURNAL_BENCHMARK") != "1",
    reason="large-journal benchmark is opt-in",
)
def test_large_journal_open_benchmark(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Print reproducible bcf75a1-style versus cached-open measurements.

    Run with ``pytest -s tests/python/test_goalflight_journal_b1.py -k benchmark``.
    The baseline clears the process cache before every open, matching the old
    per-open integrity check; the cached run clears it once, then reuses the
    fifteen-minute identity cache. ``integrity_bytes_read`` is the logical
    journal bytes traversed by each full integrity check, independent of the
    host page cache.
    """
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    with contextlib.closing(sqlite3.connect(authority.path)) as connection:
        connection.execute("CREATE TABLE synthetic_padding (payload BLOB NOT NULL)")
        connection.execute(
            "INSERT INTO synthetic_padding(payload) VALUES (zeroblob(?))",
            (85 * 1024 * 1024,),
        )
        connection.commit()
    _checkpoint(authority.path)
    journal_bytes = authority.path.stat().st_size
    opens = int(os.environ.get("GOALFLIGHT_JOURNAL_BENCHMARK_OPENS", "5"))
    if opens < 2:
        raise ValueError("GOALFLIGHT_JOURNAL_BENCHMARK_OPENS must be at least 2")

    real_uncached = journal.Journal._startup_integrity_check_uncached

    def measure(*, clear_each_open: bool) -> tuple[int, float]:
        checks = 0

        def counted(current: journal.Journal, **kwargs: object) -> None:
            nonlocal checks
            checks += 1
            real_uncached(current, **kwargs)

        monkeypatch.setattr(journal.Journal, "_startup_integrity_check_uncached", counted)
        journal._INTEGRITY_CHECKED_AT.clear()
        journal._INTEGRITY_FAILURES.clear()
        started = time.process_time()
        for _ in range(opens):
            if clear_each_open:
                journal._INTEGRITY_CHECKED_AT.clear()
            journal.Journal(project)
        cpu_ms_per_open = (time.process_time() - started) * 1000 / opens
        return checks, cpu_ms_per_open

    baseline_checks, baseline_cpu = measure(clear_each_open=True)
    cached_checks, cached_cpu = measure(clear_each_open=False)
    print(
        "JOURNAL_OPEN_BENCHMARK "
        f"journal_bytes={journal_bytes} opens={opens} "
        f"before_bcf75a1_integrity_checks={baseline_checks} "
        f"before_bcf75a1_integrity_bytes_read={baseline_checks * journal_bytes} "
        f"before_bcf75a1_integrity_bytes_read_per_open="
        f"{baseline_checks * journal_bytes / opens:.0f} "
        f"before_bcf75a1_cpu_ms_per_open={baseline_cpu:.3f} "
        f"after_integrity_checks={cached_checks} "
        f"after_integrity_bytes_read={cached_checks * journal_bytes} "
        f"after_integrity_bytes_read_per_open={cached_checks * journal_bytes / opens:.0f} "
        f"after_cpu_ms_per_open={cached_cpu:.3f}",
        flush=True,
    )
