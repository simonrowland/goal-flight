#!/usr/bin/env python3
"""Readonly journal recovery and corruption-classification contracts."""

from __future__ import annotations

import contextlib
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
        assert not Path(f"{authority.path}{suffix}").exists()
    return project, authority


def _force_readonly_cantopen(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    calls: list[str] = []
    real_connect = journal._sqlite_connect

    def injected_connect(
        database: str | Path,
        *,
        uri: bool = False,
        timeout: float = 5.0,
        isolation_level: str | None = "",
    ) -> sqlite3.Connection:
        location = os.fspath(database)
        calls.append(location)
        if "?mode=ro" in location:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(
            database,
            uri=uri,
            timeout=timeout,
            isolation_level=isolation_level,
        )

    monkeypatch.setattr(journal, "_sqlite_connect", injected_connect)
    return calls


def test_quiesced_wal_reader_falls_back_and_preserves_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, _authority = _quiesced_journal(monkeypatch, tmp_path)
    calls = _force_readonly_cantopen(monkeypatch)

    reader = journal.Journal.open_reader(project)
    attempt = reader.attempt_for_dispatch("row-survives-quiescence")

    assert attempt is not None
    assert attempt.dispatch_id == "row-survives-quiescence"
    assert any("?mode=ro" in call for call in calls)
    assert any("?mode=rw" in call for call in calls)


def test_query_only_hardens_quiesced_wal_fallback_handle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, authority = _quiesced_journal(monkeypatch, tmp_path)
    calls = _force_readonly_cantopen(monkeypatch)
    reader = journal.Journal.open_reader(project)

    with contextlib.closing(reader._connect()) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                "DELETE FROM dispatch_attempts WHERE dispatch_id = ?",
                ("row-survives-quiescence",),
            )

    assert any("?mode=rw" in call for call in calls)
    assert authority.attempt_for_dispatch("row-survives-quiescence") is not None


def test_corrupt_file_still_reports_corruption_through_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, authority = _quiesced_journal(monkeypatch, tmp_path)
    with authority.path.open("r+b") as handle:
        handle.write(b"not-a-sqlite-header-overwrite!!!")
        handle.flush()
        os.fsync(handle.fileno())
    calls = _force_readonly_cantopen(monkeypatch)

    reader = journal.Journal.open_reader(project)
    with pytest.raises(
        journal.JournalIntegrityError,
        match=r"integrity check failed.*(file is not a database|malformed)",
    ):
        reader.epochs()

    assert any("?mode=rw" in call for call in calls)


def test_present_file_with_both_opens_failing_is_probe_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, _authority = _quiesced_journal(monkeypatch, tmp_path)

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
    with pytest.raises(
        journal.JournalUnavailable,
        match="probe unavailable/unreadable",
    ) as captured:
        reader.epochs()

    assert not isinstance(captured.value, journal.JournalIntegrityError)


def test_absent_reader_keeps_legacy_unavailable_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)

    with pytest.raises(journal.JournalUnavailable, match="journal database is absent"):
        journal.Journal.open_reader(project)


class _TargetedBusyConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        needle: str,
        hits: list[str],
    ) -> None:
        self._connection = connection
        self._needle = needle
        self._hits = hits

    def execute(self, sql: str, *args: object, **kwargs: object):
        normalized = " ".join(sql.split())
        if self._needle in normalized:
            self._hits.append(normalized)
            raise sqlite3.OperationalError("database is locked")
        return self._connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


@pytest.mark.parametrize(
    ("read_stage", "needle"),
    (
        ("read_all", "SELECT 1 AS injected_read"),
        ("cursor_peek", "BEGIN"),
    ),
)
def test_every_query_stage_busy_is_normalized_as_journal_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    read_stage: str,
    needle: str,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    claimed = authority.claim_or_renew_lease(
        "query-busy-reader",
        principal={"principal_id": "query-busy-reader"},
    )
    assert claimed.committed and claimed.value is not None
    reader = journal.Journal.open_reader(project, retry_budget_s=0)
    real_connect = journal.Journal._connect
    hits: list[str] = []

    def injected_connect(current: journal.Journal, **kwargs: object):
        return _TargetedBusyConnection(
            real_connect(current, **kwargs),
            needle=needle,
            hits=hits,
        )

    monkeypatch.setattr(journal.Journal, "_connect", injected_connect)
    with pytest.raises(journal.JournalBusy, match="remained busy"):
        if read_stage == "read_all":
            reader.read_all("SELECT 1 AS injected_read")
        else:
            reader.cursor_peek(
                claimed.value.label,
                nonce=claimed.value.nonce,
            )

    assert hits, f"{read_stage} query-stage injection did not bind"


def test_read_all_replays_generator_bindings_after_query_stage_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A busy first execute must not consume one-shot bindings for the retry."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    journal.Journal.create(project)
    reader = journal.Journal.open_reader(
        project,
        retry_budget_s=1.0,
        jitter_min_s=0,
        jitter_max_s=0,
    )
    real_connect = journal.Journal._connect
    observed_bindings: list[tuple[object, ...]] = []

    class BusyOnceConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, *args: object, **kwargs: object):
            if "SELECT ? AS replayed_value" in " ".join(sql.split()):
                bindings = tuple(args[0]) if args else ()
                observed_bindings.append(bindings)
                if len(observed_bindings) == 1:
                    raise sqlite3.OperationalError("database is locked")
            return self._connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    def injected_connect(current: journal.Journal, **kwargs: object):
        return BusyOnceConnection(real_connect(current, **kwargs))

    monkeypatch.setattr(journal.Journal, "_connect", injected_connect)
    rows = reader.read_all(
        "SELECT ? AS replayed_value",
        (value for value in (41,)),
    )

    assert [row["replayed_value"] for row in rows] == [41]
    assert observed_bindings == [(41,), (41,)]


@pytest.mark.parametrize(
    "failure",
    (
        journal.JournalDisappeared("injected arm disappearance"),
        journal.JournalIOError("injected arm path I/O failure"),
    ),
)
def test_domain_write_preserves_nonbusy_journal_failure_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: journal.JournalUnavailable,
) -> None:
    """Coverage-arm writes may retry busy, never disappearance or path I/O."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    claimed = authority.claim_or_renew_lease(
        "typed-arm-failure",
        principal={"principal_id": "typed-arm-failure"},
    )
    assert claimed.committed and claimed.value is not None
    monkeypatch.setattr(
        authority,
        "_connect",
        lambda **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(type(failure), match="injected arm"):
        authority.arm_listener(
            claimed.value.label,
            nonce=claimed.value.nonce,
            pid=os.getpid(),
            start_token="typed-arm-failure",
            parent_pid=os.getppid() or os.getpid(),
        )


def test_attention_items_use_one_bounded_journal_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Synthetic-envelope batching must not widen the 10-second operation bound."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    real_read_all = authority.read_all
    calls: list[str] = []

    def measured_read(sql: str, parameters=()):
        calls.append(" ".join(sql.split()))
        return real_read_all(sql, parameters)

    monkeypatch.setattr(authority, "read_all", measured_read)
    assert authority.attention_items() == []
    assert len(calls) == 1
    assert "UNION ALL" in calls[0]


def test_snapshot_and_restore_readers_share_query_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    snapshot = authority.snapshot(tmp_path / "snapshot.sqlite3")
    calls = _force_readonly_cantopen(monkeypatch)

    restored = journal.restore_snapshot(project, snapshot, i_understand=True)

    assert restored == authority.path
    assert sum("?mode=ro" in call for call in calls) >= 3
    assert sum("?mode=rw" in call for call in calls) >= 3
