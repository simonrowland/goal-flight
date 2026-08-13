#!/usr/bin/env python3
"""Per-project transactional state journal primitives.

P1 creates the epoch/fencing substrate and the smallest honest operator surface:
``inspect``, ``dump``, validated online ``snapshot``, and guarded ``restore``.
The full backup schedule, retention/RPO policy, restore drills, and outbox-aware
post-restore reconciliation arrive in P2+; this module does not pretend those
operational policies already exist.

Journal writes accept only pre-built declarative row mutations.  No caller code
runs after ``BEGIN IMMEDIATE``.  A transaction is limited to
``MAX_TRANSACTION_OPERATIONS`` statements and guarded by a SQLite progress
deadline plus checks before every statement and before commit.


Trust domain (review round 3, findings 1/2/4): the validation gates here are
ACCIDENT RAILS and cross-process protections, not a security boundary against
code already running in this interpreter. In-process code that registers a
global sqlite3 adapter, subclasses RowOperation with attribute interception,
or declares a hostile schema can bypass them -- and could equally write the
database file directly. Python offers no in-process sandbox; pretending
otherwise would be a false claim, not a defense.
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import datetime as dt
from enum import Enum
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from typing import Generic, TypeVar


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import goalflight_task  # noqa: E402


CURRENT_SCHEMA_EPOCH = 1
CURRENT_PROTOCOL_EPOCH = 1
CURRENT_REGISTRY_EPOCH = 1
CURRENT_READER_EPOCH = 1
CURRENT_WRITER_EPOCH = 1
JOURNAL_FILE_NAME = "state-journal.sqlite3"
JOURNAL_IDENTITY_KEY = "journal_identity"
JOURNAL_IDENTITY_VALUE = "goalflight.state-journal.v1"
MAX_TRANSACTION_OPERATIONS = 128
MAX_OPERATION_ROWS = 10_000
MAX_PARAMETER_VALUE_BYTES = 65_536
MAX_TRANSACTION_PARAMETER_BYTES = 1_048_576
_SQL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class JournalError(RuntimeError):
    """Base class for clear, operator-facing journal failures."""


class JournalIntegrityError(JournalError):
    """The authoritative journal failed its startup integrity check."""


class JournalUpgradeRequired(JournalError):
    """The client epochs are incompatible with the journal epochs."""


class JournalUnavailable(JournalError):
    """The journal could not complete required startup work in budget."""


class CASMismatch(Exception):
    """Internal signal for a declarative affected-row predicate loss."""


class WriteDisposition(str, Enum):
    COMMITTED = "committed"
    RETRYABLE = "retryable"
    CAS_LOST = "cas_lost"


T = TypeVar("T")


@dataclass(frozen=True)
class WriteResult(Generic[T]):
    disposition: WriteDisposition
    value: T | None = None
    attempts: int = 1
    reason: str | None = None

    @property
    def committed(self) -> bool:
        return self.disposition is WriteDisposition.COMMITTED

    @property
    def retryable(self) -> bool:
        return self.disposition is WriteDisposition.RETRYABLE

    @property
    def cas_lost(self) -> bool:
        return self.disposition is WriteDisposition.CAS_LOST


@dataclass(frozen=True)
class ClientEpochs:
    schema: int = CURRENT_SCHEMA_EPOCH
    protocol: int = CURRENT_PROTOCOL_EPOCH
    registry: int = CURRENT_REGISTRY_EPOCH
    reader: int = CURRENT_READER_EPOCH
    writer: int = CURRENT_WRITER_EPOCH


@dataclass(frozen=True)
class JournalEpochs:
    schema: int
    protocol: int
    registry: int
    minimum_reader: int
    minimum_writer: int


@dataclass(frozen=True)
class RowWrite:
    """Outcome for one declarative row mutation."""

    affected_rows: int
    last_row_id: int | None = None


@dataclass(frozen=True, init=False)
class RowOperation:
    """One bounded INSERT, UPDATE, or DELETE assembled before lock acquisition."""

    kind: str
    table: str
    values: tuple[tuple[str, object], ...] = ()
    where: tuple[tuple[str, object], ...] = ()
    expected_rows: int | None = None
    row_cap: int | None = None
    _sql: str = ""
    _parameters: tuple[object, ...] = ()
    _parameter_bytes: int = 0

    def __init__(
        self,
        kind: str,
        table: str,
        values: tuple[tuple[str, object], ...] = (),
        where: tuple[tuple[str, object], ...] = (),
        expected_rows: int | None = None,
        row_cap: int | None = None,
    ) -> None:
        sql, parameters, parameter_bytes = _compile_row_operation(
            kind=kind,
            table=table,
            values=values,
            where=where,
            expected_rows=expected_rows,
            row_cap=row_cap,
        )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "table", table)
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "where", where)
        object.__setattr__(self, "expected_rows", expected_rows)
        object.__setattr__(self, "row_cap", row_cap)
        object.__setattr__(self, "_sql", sql)
        object.__setattr__(self, "_parameters", parameters)
        object.__setattr__(self, "_parameter_bytes", parameter_bytes)

    @classmethod
    def insert(
        cls,
        table: str,
        values: Mapping[str, object],
        *,
        expected_rows: int | None = 1,
    ) -> "RowOperation":
        return cls("insert", table, tuple(values.items()), expected_rows=expected_rows)

    @classmethod
    def update(
        cls,
        table: str,
        values: Mapping[str, object],
        *,
        where: Mapping[str, object],
        row_cap: int,
        expected_rows: int | None = None,
    ) -> "RowOperation":
        return cls(
            "update",
            table,
            tuple(values.items()),
            tuple(where.items()),
            expected_rows,
            row_cap,
        )

    @classmethod
    def delete(
        cls,
        table: str,
        *,
        where: Mapping[str, object],
        row_cap: int,
        expected_rows: int | None = None,
    ) -> "RowOperation":
        return cls(
            "delete",
            table,
            where=tuple(where.items()),
            expected_rows=expected_rows,
            row_cap=row_cap,
        )

    def compiled(self) -> tuple[str, tuple[object, ...]]:
        return self._sql, self._parameters


def _require_identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _SQL_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be a simple SQL identifier")
    return value


def _quote_identifier(value: str) -> str:
    return f'"{_require_identifier(value, label="identifier")}"'


def _validated_pairs(
    pairs: tuple[tuple[str, object], ...], *, label: str
) -> tuple[tuple[str, object], ...]:
    seen: set[str] = set()
    for name, _value in pairs:
        _require_identifier(name, label=f"{label} column")
        if name in seen:
            raise ValueError(f"{label} contains duplicate column {name!r}")
        seen.add(name)
    return pairs


def _canonical_bind_value(value: object, *, path: str) -> tuple[object, int]:
    """Validate one SQLite bind value through the D4 canonical serializer."""
    import goalflight_messages

    if type(value) is bytes:
        if len(value) > MAX_PARAMETER_VALUE_BYTES:
            raise ValueError(
                f"{path}: byte value is {len(value)} bytes; limit is {MAX_PARAMETER_VALUE_BYTES}"
            )
        goalflight_messages._canonical_json_text(
            value.hex(),
            path=path,
            byte_limit=(MAX_PARAMETER_VALUE_BYTES * 2) + 2,
        )
        return value, len(value)
    if value is None or type(value) in {str, int, float, bool}:
        if type(value) is int and not -(2**63) <= value <= (2**63) - 1:
            raise ValueError(f"{path}: integer is outside SQLite's signed 64-bit range")
        try:
            serialized = goalflight_messages._canonical_json_text(
                value,
                path=path,
                byte_limit=MAX_PARAMETER_VALUE_BYTES,
            )
        except goalflight_messages.MessageError as exc:
            raise ValueError(str(exc)) from exc
        bound = int(value) if type(value) is bool else value
        return bound, len(serialized.encode("utf-8"))
    raise ValueError(
        f"{path}: SQLite parameter type {type(value).__name__} refused; "
        "bind values must be str, int, float, bytes, bool, or None"
    )


def _compile_row_operation(
    *,
    kind: str,
    table: str,
    values: tuple[tuple[str, object], ...],
    where: tuple[tuple[str, object], ...],
    expected_rows: int | None,
    row_cap: int | None,
) -> tuple[str, tuple[object, ...], int]:
    if kind not in {"insert", "update", "delete"}:
        raise ValueError(f"unsupported row operation: {kind!r}")
    _require_identifier(table, label="table")
    checked_values = _validated_pairs(values, label="values")
    checked_where = _validated_pairs(where, label="where")
    if expected_rows is not None and (
        not isinstance(expected_rows, int)
        or isinstance(expected_rows, bool)
        or expected_rows < 0
    ):
        raise ValueError("expected_rows must be a non-negative integer or None")
    if kind in {"update", "delete"} and (
        not isinstance(row_cap, int)
        or isinstance(row_cap, bool)
        or not 1 <= row_cap <= MAX_OPERATION_ROWS
    ):
        raise ValueError(
            f"{kind} requires an explicit row_cap in 1..{MAX_OPERATION_ROWS}"
        )
    if kind == "insert" and row_cap is not None:
        raise ValueError("insert does not accept row_cap")
    if expected_rows is not None and row_cap is not None and expected_rows > row_cap:
        raise ValueError("expected_rows cannot exceed row_cap")
    if kind in {"insert", "update"} and not checked_values:
        raise ValueError(f"{kind} requires at least one value")
    if kind in {"update", "delete"} and not checked_where:
        raise ValueError(f"{kind} requires a bounded equality predicate")

    normalized_values = tuple(
        (name, *_canonical_bind_value(value, path=f"values.{name}"))
        for name, value in checked_values
    )
    normalized_where = tuple(
        (name, *_canonical_bind_value(value, path=f"where.{name}"))
        for name, value in checked_where
    )
    parameter_bytes = sum(item[2] for item in (*normalized_values, *normalized_where))
    quoted_table = _quote_identifier(table)
    if kind == "insert":
        columns = ", ".join(_quote_identifier(name) for name, _value, _size in normalized_values)
        placeholders = ", ".join("?" for _ in normalized_values)
        return (
            f"INSERT INTO {quoted_table} ({columns}) VALUES ({placeholders})",
            tuple(value for _name, value, _size in normalized_values),
            parameter_bytes,
        )
    predicate = " AND ".join(
        f"{_quote_identifier(name)} IS ?" for name, _value, _size in normalized_where
    )
    bounded_predicate = (
        f"rowid IN (SELECT rowid FROM {quoted_table} WHERE {predicate} LIMIT ?)"
    )
    bounded_parameters = tuple(value for _name, value, _size in normalized_where) + (row_cap,)
    parameter_bytes += len(str(row_cap).encode("ascii"))
    if kind == "update":
        assignments = ", ".join(
            f"{_quote_identifier(name)} = ?" for name, _value, _size in normalized_values
        )
        return (
            f"UPDATE {quoted_table} SET {assignments} WHERE {bounded_predicate}",
            tuple(value for _name, value, _size in normalized_values) + bounded_parameters,
            parameter_bytes,
        )
    return (
        f"DELETE FROM {quoted_table} WHERE {bounded_predicate}",
        bounded_parameters,
        parameter_bytes,
    )


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def resolve_journal_path(project_root: Path | str) -> Path:
    """Resolve the journal with the task store's exact canonical project key."""
    root = goalflight_task.resolve_project_root(str(project_root))
    task_store = goalflight_task.resolve_task_store_dir(root)
    override = os.environ.get("GOALFLIGHT_JOURNAL_DIR", "").strip()
    state_base = Path(override).expanduser() if override else task_store.parents[1]
    return (state_base / "journals" / task_store.name / JOURNAL_FILE_NAME).resolve(
        strict=False
    )


def journal_write_lock_path(journal_path: Path) -> Path:
    return journal_path.with_name(f".{journal_path.name}.write.lock")


def _is_busy(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc).lower() for marker in ("locked", "busy")
    )


class Journal:
    """Short-lived SQLite transactions with explicit retry/fence outcomes.

    Read helpers never issue ``BEGIN`` and always fetch their complete result
    before closing the connection.  Writes compile bounded row operations
    before ``BEGIN IMMEDIATE`` and may be retried after a SQLite busy response.
    """

    def __init__(
        self,
        project_root: Path | str,
        *,
        client_epochs: ClientEpochs | None = None,
        retry_budget_s: float = 1.0,
        transaction_budget_s: float = 1.0,
        jitter_min_s: float = 0.005,
        jitter_max_s: float = 0.050,
    ) -> None:
        self._configure(
            project_root,
            client_epochs=client_epochs,
            retry_budget_s=retry_budget_s,
            transaction_budget_s=transaction_budget_s,
            jitter_min_s=jitter_min_s,
            jitter_max_s=jitter_max_s,
        )
        self._require_existing_database()
        with goalflight_task.FileLock(journal_write_lock_path(self.path)):
            self._require_existing_database()
            self._open_validated(created_here=False)

    @classmethod
    def create(
        cls,
        project_root: Path | str,
        *,
        client_epochs: ClientEpochs | None = None,
        retry_budget_s: float = 1.0,
        transaction_budget_s: float = 1.0,
        jitter_min_s: float = 0.005,
        jitter_max_s: float = 0.050,
    ) -> "Journal":
        """Explicitly bootstrap a journal; ordinary construction never creates."""
        self = cls.__new__(cls)
        self._configure(
            project_root,
            client_epochs=client_epochs,
            retry_budget_s=retry_budget_s,
            transaction_budget_s=transaction_budget_s,
            jitter_min_s=jitter_min_s,
            jitter_max_s=jitter_max_s,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with goalflight_task.FileLock(journal_write_lock_path(self.path)):
            if os.path.lexists(self.path):
                raise JournalError(
                    f"journal init refused because the database already exists: {self.path}; "
                    "open it with Journal(...) instead"
                )
            self._claim_fresh_database_path()
            try:
                self._open_validated(created_here=True)
            except BaseException:
                for candidate in (
                    self.path,
                    Path(f"{self.path}-wal"),
                    Path(f"{self.path}-shm"),
                ):
                    candidate.unlink(missing_ok=True)
                raise
        return self

    def _configure(
        self,
        project_root: Path | str,
        *,
        client_epochs: ClientEpochs | None,
        retry_budget_s: float,
        transaction_budget_s: float,
        jitter_min_s: float,
        jitter_max_s: float,
    ) -> None:
        if retry_budget_s < 0:
            raise ValueError("retry_budget_s must be >= 0")
        if transaction_budget_s <= 0:
            raise ValueError("transaction_budget_s must be > 0")
        if not 0 <= jitter_min_s <= jitter_max_s:
            raise ValueError("journal jitter bounds are invalid")
        self.project_root = goalflight_task.resolve_project_root(str(project_root))
        self.path = resolve_journal_path(self.project_root)
        self.client_epochs = client_epochs or ClientEpochs()
        self.retry_budget_s = retry_budget_s
        self.transaction_budget_s = transaction_budget_s
        self.jitter_min_s = jitter_min_s
        self.jitter_max_s = jitter_max_s

    def _require_existing_database(self) -> None:
        if not os.path.lexists(self.path):
            raise JournalUnavailable(
                f"journal database is absent: {self.path}. Failing closed because streams "
                "cannot rebuild journal authority. Restore a validated WAL-safe backup; "
                "use the init verb only for an intentional first bootstrap."
            )
        if self.path.is_symlink():
            raise JournalIntegrityError(
                f"journal integrity check failed for {self.path}: symlinked journal refused; "
                "the journal is authoritative and streams cannot rebuild it"
            )

    def _open_validated(self, *, created_here: bool) -> None:
        self._startup_integrity_check()
        self._bootstrap_schema(created_here=created_here)
        # Enforced on open even though P1 has only epoch 1.  Reads repeat the
        # fence so a long-lived client cannot outlive a migration unnoticed.
        with contextlib.closing(self._connect()) as connection:
            self._assert_epoch_fence(connection, for_write=False)

    def _claim_fresh_database_path(self) -> None:
        """Atomically claim a path after the caller states bootstrap intent."""
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError:
            raise JournalError(f"journal init raced an existing database: {self.path}")
        except OSError as exc:
            raise JournalUnavailable(f"cannot create journal path {self.path}: {exc}") from exc
        else:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                connection = sqlite3.connect(
                    self.path.as_uri() + "?mode=rw",
                    uri=True,
                    timeout=0,
                    isolation_level=None,
                )
            except sqlite3.OperationalError as exc:
                if "unable to open database file" in str(exc).lower():
                    raise JournalUnavailable(
                        f"journal database became unavailable without creating a replacement: {self.path}"
                    ) from exc
                raise
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 0")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA synchronous = FULL")
                return connection
            except sqlite3.OperationalError as exc:
                connection.close()
                if not _is_busy(exc):
                    raise
                if not self._retry_delay(started):
                    raise JournalUnavailable(
                        f"journal connection remained busy after {attempts} attempts "
                        f"within {self.retry_budget_s:.3f}s: {self.path}"
                    ) from exc

    def _startup_integrity_check(self) -> None:
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                with contextlib.closing(
                    sqlite3.connect(
                        self.path.as_uri() + "?mode=rw",
                        uri=True,
                        timeout=0,
                        isolation_level=None,
                    )
                ) as connection:
                    rows = [
                        str(row[0])
                        for row in connection.execute("PRAGMA integrity_check")
                    ]
            except sqlite3.DatabaseError as exc:
                if _is_busy(exc) and self._retry_delay(started):
                    continue
                if _is_busy(exc):
                    raise JournalUnavailable(
                        f"journal integrity check remained busy after {attempts} attempts "
                        f"within {self.retry_budget_s:.3f}s: {self.path}"
                    ) from exc
                self._raise_integrity_failure(str(exc))
            if rows != ["ok"]:
                self._raise_integrity_failure("; ".join(rows) or "no result")
            return

    def _raise_integrity_failure(self, detail: str) -> None:
        raise JournalIntegrityError(
            f"journal integrity check failed for {self.path}: {detail}. "
            "Failing closed: this journal is authoritative and streams cannot rebuild it. "
            "Restore a validated WAL-safe backup or use audited repair; raw SQLite edits "
            "are unsupported."
        )

    def _bootstrap_schema(self, *, created_here: bool) -> None:
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                connection = self._connect()
            except JournalUnavailable as exc:
                raise JournalUnavailable(
                    f"journal startup could not open a configured connection: {self.path}"
                ) from exc
            try:
                connection.execute("BEGIN IMMEDIATE")
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                    if not str(row[0]).startswith("sqlite_")
                }
                required = {"journal_meta", "journal_epochs"}
                if required <= tables:
                    self._assert_identity(connection)
                    self._assert_epoch_fence(connection, for_write=False)
                    connection.rollback()
                    mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                    if mode != "wal":
                        self._assert_epoch_fence(connection, for_write=True)
                        connection.execute("PRAGMA journal_mode = WAL")
                    return
                if not created_here:
                    connection.rollback()
                    if self._retry_delay(started):
                        continue
                    missing = ", ".join(sorted(required - tables)) or "required rows"
                    self._raise_integrity_failure(
                        f"existing database is missing {missing}; refusing silent re-bootstrap"
                    )
                if tables:
                    connection.rollback()
                    self._raise_integrity_failure(
                        "newly claimed database unexpectedly contains tables: "
                        + ", ".join(sorted(tables))
                    )
                connection.execute(
                    """
                    CREATE TABLE journal_meta (
                        key TEXT PRIMARY KEY CHECK (key = 'journal_identity'),
                        value TEXT NOT NULL CHECK (value = 'goalflight.state-journal.v1'),
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE journal_epochs (
                        singleton INTEGER PRIMARY KEY
                            CHECK (typeof(singleton) = 'integer' AND singleton = 1),
                        schema_epoch INTEGER NOT NULL
                            CHECK (typeof(schema_epoch) = 'integer' AND schema_epoch >= 1),
                        protocol_epoch INTEGER NOT NULL
                            CHECK (typeof(protocol_epoch) = 'integer' AND protocol_epoch >= 1),
                        registry_epoch INTEGER NOT NULL
                            CHECK (typeof(registry_epoch) = 'integer' AND registry_epoch >= 1),
                        minimum_reader_epoch INTEGER NOT NULL
                            CHECK (typeof(minimum_reader_epoch) = 'integer' AND minimum_reader_epoch >= 1),
                        minimum_writer_epoch INTEGER NOT NULL
                            CHECK (typeof(minimum_writer_epoch) = 'integer' AND minimum_writer_epoch >= 1),
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO journal_meta (key, value, created_at) VALUES (?, ?, ?)",
                    (JOURNAL_IDENTITY_KEY, JOURNAL_IDENTITY_VALUE, utc_now()),
                )
                connection.execute(
                    """
                    INSERT INTO journal_epochs (
                        singleton, schema_epoch, protocol_epoch, registry_epoch,
                        minimum_reader_epoch, minimum_writer_epoch, updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        CURRENT_SCHEMA_EPOCH,
                        CURRENT_PROTOCOL_EPOCH,
                        CURRENT_REGISTRY_EPOCH,
                        CURRENT_READER_EPOCH,
                        CURRENT_WRITER_EPOCH,
                        utc_now(),
                    ),
                )
                connection.commit()
                connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if connection.in_transaction:
                    connection.rollback()
                if not _is_busy(exc) or not self._retry_delay(started):
                    if _is_busy(exc):
                        raise JournalUnavailable(
                            f"journal startup remained busy after {attempts} attempts "
                            f"within {self.retry_budget_s:.3f}s: {self.path}"
                        ) from exc
                    raise
            finally:
                connection.close()

    def _retry_delay(self, started: float) -> bool:
        remaining = self.retry_budget_s - (time.monotonic() - started)
        if remaining <= 0:
            return False
        delay = min(random.uniform(self.jitter_min_s, self.jitter_max_s), remaining)
        if delay > 0:
            time.sleep(delay)
        return time.monotonic() - started < self.retry_budget_s

    def _assert_identity(self, connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute(
                "SELECT value FROM journal_meta WHERE key = ?",
                (JOURNAL_IDENTITY_KEY,),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            self._raise_integrity_failure(f"journal identity schema is unreadable: {exc}")
        if row is None or str(row["value"]) != JOURNAL_IDENTITY_VALUE:
            self._raise_integrity_failure("required journal identity row is missing or invalid")

    def _epoch_row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        try:
            self._assert_identity(connection)
            row = connection.execute(
                """
                SELECT schema_epoch, protocol_epoch, registry_epoch,
                       minimum_reader_epoch, minimum_writer_epoch
                FROM journal_epochs WHERE singleton = 1
                """
            ).fetchone()
        except JournalIntegrityError:
            raise
        except sqlite3.DatabaseError as exc:
            self._raise_integrity_failure(f"journal epoch schema is unreadable: {exc}")
        if row is None:
            raise JournalIntegrityError(
                f"journal epoch row is missing for {self.path}; failing closed. "
                "Restore a validated WAL-safe backup or use audited repair."
            )
        return row

    def _assert_epoch_fence(
        self, connection: sqlite3.Connection, *, for_write: bool
    ) -> JournalEpochs:
        try:
            row = self._epoch_row(connection)
            raw_values = {
                field: row[column]
                for field, column in (
                    ("schema", "schema_epoch"),
                    ("protocol", "protocol_epoch"),
                    ("registry", "registry_epoch"),
                    ("minimum_reader", "minimum_reader_epoch"),
                    ("minimum_writer", "minimum_writer_epoch"),
                )
            }
            for label, value in raw_values.items():
                if not isinstance(value, int) or isinstance(value, bool):
                    raise TypeError(f"{label} epoch has non-integer storage class")
            stored = JournalEpochs(**{key: int(value) for key, value in raw_values.items()})
        except JournalIntegrityError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
            self._raise_integrity_failure(f"journal epoch row is invalid: {exc}")
        client = self.client_epochs
        mismatches = []
        for label, actual, expected in (
            ("schema", client.schema, stored.schema),
            ("protocol", client.protocol, stored.protocol),
            ("registry", client.registry, stored.registry),
        ):
            if actual != expected:
                mismatches.append(f"{label} client={actual} journal={expected}")
        minimum = stored.minimum_writer if for_write else stored.minimum_reader
        capability = client.writer if for_write else client.reader
        if capability < minimum:
            mismatches.append(
                f"{'writer' if for_write else 'reader'} client={capability} minimum={minimum}"
            )
        if mismatches:
            raise JournalUpgradeRequired(
                "UPGRADE_REQUIRED: journal epoch fence refused client: "
                + "; ".join(mismatches)
            )
        return stored

    def epochs(self) -> JournalEpochs:
        with contextlib.closing(self._connect()) as connection:
            return self._assert_epoch_fence(connection, for_write=False)

    def read_all(
        self, sql: str, parameters: Iterable[object] = ()
    ) -> list[sqlite3.Row]:
        """Fetch a bounded result without retaining a read transaction."""
        normalized = " ".join(sql.strip().split())
        upper = normalized.upper()
        if upper.startswith("PRAGMA "):
            pragma_tail = upper.removeprefix("PRAGMA ")
            pragma_name = pragma_tail.split("(", 1)[0].split()[0]
            allowed_pragmas = {
                "FOREIGN_KEY_LIST",
                "INDEX_LIST",
                "INTEGRITY_CHECK",
                "JOURNAL_MODE",
                "QUICK_CHECK",
                "SYNCHRONOUS",
                "TABLE_INFO",
            }
            read_only = "=" not in pragma_tail and pragma_name in allowed_pragmas
        else:
            read_only = upper.startswith("SELECT ") or upper.startswith("EXPLAIN ")
        if not read_only:
            raise ValueError("read_all accepts only read-only SELECT, EXPLAIN, and inspect PRAGMA statements")
        with contextlib.closing(self._connect()) as connection:
            self._assert_epoch_fence(connection, for_write=False)
            return list(connection.execute(sql, tuple(parameters)).fetchall())

    def write(self, operations: Iterable[RowOperation]) -> WriteResult[list[RowWrite]]:
        """Run declarative row operations in one bounded immediate transaction.

        Operations are compiled before lock acquisition.  Busy or deadline
        exhaustion returns ``RETRYABLE``; an ``expected_rows`` mismatch returns
        ``CAS_LOST``.  These outcomes never alias. The progress handler bounds
        SQLite VM work; stronger CPU-latency enforcement beyond that handler is
        explicitly P2 work.
        """
        if isinstance(operations, RowOperation):
            prepared_operations = (operations,)
        else:
            if callable(operations):
                raise TypeError("Journal.write accepts declarative RowOperation values, not callables")
            prepared_operations = tuple(operations)
        if not prepared_operations:
            raise ValueError("Journal.write requires at least one row operation")
        if len(prepared_operations) > MAX_TRANSACTION_OPERATIONS:
            raise ValueError(
                f"Journal.write is limited to {MAX_TRANSACTION_OPERATIONS} row operations"
            )
        compiled_items = []
        for index, operation in enumerate(prepared_operations):
            if not isinstance(operation, RowOperation):
                raise TypeError(
                    f"Journal.write item {index} must be a RowOperation, got "
                    f"{type(operation).__name__}"
                )
            if type(operation).__init__ is not RowOperation.__init__:
                raise TypeError("RowOperation subclasses may not override __init__()")
            compiled_method = getattr(operation.compiled, "__func__", None)
            if compiled_method is not RowOperation.compiled:
                raise TypeError("RowOperation subclasses may not override compiled()")
            compiled_items.append((operation, *operation.compiled()))
        compiled = tuple(compiled_items)
        parameter_bytes = sum(operation._parameter_bytes for operation in prepared_operations)
        if parameter_bytes > MAX_TRANSACTION_PARAMETER_BYTES:
            raise ValueError(
                f"Journal.write parameters total {parameter_bytes} bytes; "
                f"limit is {MAX_TRANSACTION_PARAMETER_BYTES}"
            )
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            write_lock = goalflight_task.FileLock.try_acquire(
                journal_write_lock_path(self.path),
                deadline_s=started + self.retry_budget_s,
            )
            if write_lock is None:
                return WriteResult(
                    WriteDisposition.RETRYABLE,
                    attempts=attempts,
                    reason=(
                        f"journal write lock timeout after {attempts} attempts within "
                        f"{self.retry_budget_s:.3f}s"
                    ),
                )
            try:
                connection = self._connect()
            except JournalUnavailable as exc:
                write_lock.release()
                return WriteResult(
                    WriteDisposition.RETRYABLE,
                    attempts=attempts,
                    reason=str(exc),
                )
            except BaseException:
                write_lock.release()
                raise
            try:
                transaction_started = time.monotonic()
                deadline = transaction_started + self.transaction_budget_s
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() >= deadline else 0,
                    1000,
                )
                connection.execute("BEGIN IMMEDIATE")
                self._assert_epoch_fence(connection, for_write=True)
                values: list[RowWrite] = []
                for operation, sql, parameters in compiled:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("journal transaction deadline reached")
                    cursor = connection.execute(sql, parameters)
                    affected = int(cursor.rowcount)
                    if operation.expected_rows is not None and affected != operation.expected_rows:
                        raise CASMismatch(
                            f"{operation.kind} on {operation.table} affected {affected} rows; "
                            f"expected {operation.expected_rows}"
                        )
                    values.append(RowWrite(affected, cursor.lastrowid))
                if time.monotonic() >= deadline:
                    raise TimeoutError("journal transaction deadline reached")
                connection.commit()
                return WriteResult(
                    WriteDisposition.COMMITTED,
                    value=values,
                    attempts=attempts,
                )
            except CASMismatch as exc:
                if connection.in_transaction:
                    connection.rollback()
                return WriteResult(
                    WriteDisposition.CAS_LOST,
                    attempts=attempts,
                    reason=str(exc) or "compare-and-swap predicate lost",
                )
            except TimeoutError as exc:
                if connection.in_transaction:
                    connection.rollback()
                return WriteResult(
                    WriteDisposition.RETRYABLE,
                    attempts=attempts,
                    reason=(
                        f"transaction exceeded {self.transaction_budget_s:.3f}s budget: {exc}"
                    ),
                )
            except sqlite3.OperationalError as exc:
                if connection.in_transaction:
                    connection.rollback()
                if "interrupted" in str(exc).lower():
                    return WriteResult(
                        WriteDisposition.RETRYABLE,
                        attempts=attempts,
                        reason=f"transaction exceeded {self.transaction_budget_s:.3f}s budget",
                    )
                if not _is_busy(exc):
                    raise
                if not self._retry_delay(started):
                    return WriteResult(
                        WriteDisposition.RETRYABLE,
                        attempts=attempts,
                        reason=(
                            f"journal busy timeout after {attempts} attempts within "
                            f"{self.retry_budget_s:.3f}s"
                        ),
                    )
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()
                write_lock.release()

    def inspect(self) -> dict[str, object]:
        with contextlib.closing(self._connect()) as connection:
            epochs = self._assert_epoch_fence(connection, for_write=False)
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                self._raise_integrity_failure("; ".join(integrity))
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            return {
                "path": str(self.path),
                "integrity": "ok",
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                "epochs": epochs.__dict__,
                "tables": tables,
            }

    def dump_sql(self) -> list[str]:
        with contextlib.closing(self._connect()) as connection:
            self._assert_epoch_fence(connection, for_write=False)
            rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            if rows != ["ok"]:
                self._raise_integrity_failure("; ".join(rows))
            return list(connection.iterdump())

    def snapshot(self, output: Path | str) -> Path:
        destination = Path(output).expanduser().resolve(strict=False)
        if destination == self.path:
            raise JournalError("snapshot destination must differ from the live journal")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.tmp-", dir=destination.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            with contextlib.closing(self._connect()) as source:
                self._assert_epoch_fence(source, for_write=False)
                rows = [str(row[0]) for row in source.execute("PRAGMA integrity_check")]
                if rows != ["ok"]:
                    self._raise_integrity_failure("; ".join(rows))
                with contextlib.closing(sqlite3.connect(tmp)) as target:
                    source.backup(target)
            _validate_snapshot_file(tmp)
            with tmp.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp, destination)
            _fsync_directory(destination.parent)
            return destination
        finally:
            tmp.unlink(missing_ok=True)


def _validate_snapshot_file(path: Path) -> JournalEpochs:
    if path.is_symlink() or not path.is_file():
        raise JournalIntegrityError(f"snapshot is not a regular non-symlink file: {path}")
    try:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            if rows != ["ok"]:
                raise JournalIntegrityError(f"snapshot integrity check failed for {path}: {'; '.join(rows)}")
            identity = connection.execute(
                "SELECT value FROM journal_meta WHERE key = ?", (JOURNAL_IDENTITY_KEY,)
            ).fetchone()
            if identity is None or str(identity["value"]) != JOURNAL_IDENTITY_VALUE:
                raise JournalIntegrityError(f"snapshot identity row is missing or invalid: {path}")
            row = connection.execute(
                """
                SELECT schema_epoch, protocol_epoch, registry_epoch,
                       minimum_reader_epoch, minimum_writer_epoch
                FROM journal_epochs WHERE singleton = 1
                """
            ).fetchone()
            if row is None:
                raise JournalIntegrityError(f"snapshot epoch row is missing: {path}")
            values = [row[index] for index in range(5)]
            if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
                raise JournalIntegrityError(f"snapshot epoch row contains non-integer values: {path}")
            return JournalEpochs(*(int(value) for value in values))
    except JournalIntegrityError:
        raise
    except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
        raise JournalIntegrityError(f"snapshot validation failed for {path}: {exc}") from exc


def _assert_snapshot_epoch_compatibility(
    epochs: JournalEpochs,
    *,
    client: ClientEpochs | None = None,
) -> None:
    capabilities = client or ClientEpochs()
    mismatches = []
    for label, actual, expected in (
        ("schema", capabilities.schema, epochs.schema),
        ("protocol", capabilities.protocol, epochs.protocol),
        ("registry", capabilities.registry, epochs.registry),
    ):
        if actual != expected:
            mismatches.append(f"{label} client={actual} snapshot={expected}")
    if capabilities.reader < epochs.minimum_reader:
        mismatches.append(
            f"reader client={capabilities.reader} minimum={epochs.minimum_reader}"
        )
    if capabilities.writer < epochs.minimum_writer:
        mismatches.append(
            f"writer client={capabilities.writer} minimum={epochs.minimum_writer}"
        )
    if mismatches:
        raise JournalUpgradeRequired(
            "UPGRADE_REQUIRED: snapshot epoch fence refused restore before replacement: "
            + "; ".join(mismatches)
        )


def restore_snapshot(
    project_root: Path | str,
    snapshot: Path | str,
    *,
    i_understand: bool,
) -> Path:
    source = Path(os.path.abspath(os.fspath(Path(snapshot).expanduser())))
    destination = resolve_journal_path(project_root)
    if not os.path.lexists(destination):
        raise JournalUnavailable(
            f"restore target journal is absent: {destination}. Failing closed; use init only "
            "for an intentional bootstrap, then retry restore from the validated snapshot."
        )
    if not i_understand:
        raise JournalError(
            "restore refused without --i-understand; stop journal users and verify the snapshot first"
        )
    if destination.is_symlink():
        raise JournalIntegrityError(f"restore target is a symlink: {destination}")
    if source == destination:
        raise JournalError("restore snapshot must differ from the live journal")

    with goalflight_task.FileLock(journal_write_lock_path(destination)):
        if not os.path.lexists(destination):
            raise JournalUnavailable(
                f"restore target journal disappeared before exclusion was acquired: {destination}"
            )
        if destination.is_symlink() or not destination.is_file():
            raise JournalIntegrityError(f"restore target is not a regular non-symlink file: {destination}")
        live_sidecars = [Path(f"{destination}{suffix}") for suffix in ("-wal", "-shm")]
        if any(sidecar.exists() for sidecar in live_sidecars):
            try:
                with contextlib.closing(
                    sqlite3.connect(
                        destination.as_uri() + "?mode=rw",
                        uri=True,
                        timeout=0,
                        isolation_level=None,
                    )
                ) as live_connection:
                    checkpoint = live_connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise JournalUnavailable(
                    f"restore could not checkpoint the excluded target {destination}: {exc}"
                ) from exc
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise JournalUnavailable(
                    f"restore target could not reach an offline checkpoint: {destination}"
                )
            for sidecar in live_sidecars:
                sidecar.unlink(missing_ok=True)

        copy_fd, copy_name = tempfile.mkstemp(
            prefix=f".{destination.name}.restore-copy-", dir=destination.parent
        )
        copied_snapshot = Path(copy_name)
        displaced: Path | None = None
        preserve_displaced = False
        try:
            os.close(copy_fd)
            copy_fd = -1
            if source.is_symlink() or not source.is_file():
                raise JournalIntegrityError(
                    f"snapshot is not a regular non-symlink file: {source}"
                )
            try:
                with contextlib.closing(
                    sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
                ) as source_connection, contextlib.closing(
                    sqlite3.connect(copied_snapshot)
                ) as copy_connection:
                    source_connection.backup(copy_connection)
            except sqlite3.DatabaseError as exc:
                raise JournalIntegrityError(
                    f"snapshot validation failed during copy for {source}: {exc}"
                ) from exc
            try:
                with contextlib.closing(
                    sqlite3.connect(copied_snapshot, timeout=0, isolation_level=None)
                ) as copy_connection:
                    copy_checkpoint = copy_connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise JournalIntegrityError(
                    f"snapshot validation failed while checkpointing copy {source}: {exc}"
                ) from exc
            if copy_checkpoint is None or int(copy_checkpoint[0]) != 0:
                raise JournalIntegrityError(
                    f"snapshot validation copy could not be checkpointed: {source}"
                )
            for suffix in ("-wal", "-shm"):
                Path(f"{copied_snapshot}{suffix}").unlink(missing_ok=True)
            with copied_snapshot.open("rb") as copy_handle:
                os.fsync(copy_handle.fileno())

            snapshot_epochs = _validate_snapshot_file(copied_snapshot)
            _assert_snapshot_epoch_compatibility(snapshot_epochs)
            for suffix in ("-wal", "-shm"):
                Path(f"{copied_snapshot}{suffix}").unlink(missing_ok=True)

            rollback_fd, rollback_name = tempfile.mkstemp(
                prefix=f".{destination.name}.restore-preimage-", dir=destination.parent
            )
            os.close(rollback_fd)
            displaced = Path(rollback_name)
            displaced.unlink()
            # Hard-link the pre-image aside instead of renaming it away: the
            # canonical path keeps its inode until the single atomic replace
            # below, so no crash instant leaves the journal ABSENT. A crash
            # before the replace leaves the old journal in place; after it,
            # the new one. Review round 3 finding 3: two renames had a window
            # where the authoritative path did not exist at all.
            os.link(destination, displaced)
            preserve_displaced = True
            try:
                os.replace(copied_snapshot, destination)
                _fsync_directory(destination.parent)
                installed_epochs = _validate_snapshot_file(destination)
                _assert_snapshot_epoch_compatibility(installed_epochs)
                displaced.unlink()
                preserve_displaced = False
            except BaseException as install_exc:
                try:
                    for sidecar in live_sidecars:
                        sidecar.unlink(missing_ok=True)
                    os.replace(displaced, destination)
                    preserve_displaced = False
                    _fsync_directory(destination.parent)
                except BaseException as rollback_exc:
                    preserve_displaced = True
                    raise JournalIntegrityError(
                        f"restore failed after displacement and rollback failed; pre-image kept at "
                        f"{displaced}: install={install_exc}; rollback={rollback_exc}"
                    ) from install_exc
                raise
            return destination
        finally:
            if copy_fd >= 0:
                os.close(copy_fd)
            copied_snapshot.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(f"{copied_snapshot}{suffix}").unlink(missing_ok=True)
            if displaced is not None and not preserve_displaced:
                displaced.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Flight state journal operator tools")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="explicitly create a first-bootstrap journal")
    sub.add_parser("inspect", help="validate and describe the live journal")
    sub.add_parser("dump", help="validate and emit a logical SQL dump")
    snapshot_parser = sub.add_parser("snapshot", help="create a validated online SQLite backup")
    snapshot_parser.add_argument("--output", type=Path, required=True)
    restore_parser = sub.add_parser("restore", help="replace an offline journal from a validated snapshot")
    restore_parser.add_argument("--snapshot", type=Path, required=True)
    restore_parser.add_argument("--i-understand", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            print(Journal.create(args.project_root).path)
        elif args.command == "inspect":
            print(json.dumps(Journal(args.project_root).inspect(), indent=2, sort_keys=True))
        elif args.command == "dump":
            print("\n".join(Journal(args.project_root).dump_sql()))
        elif args.command == "snapshot":
            print(Journal(args.project_root).snapshot(args.output))
        else:
            print(
                restore_snapshot(
                    args.project_root,
                    args.snapshot,
                    i_understand=bool(args.i_understand),
                )
            )
        return 0
    except (JournalError, OSError) as exc:
        print(f"{args.command}: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
