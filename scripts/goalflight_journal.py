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
import base64
import contextlib
from dataclasses import dataclass
import datetime as dt
from enum import Enum
import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import re
import secrets
import socket
import sqlite3
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Generic, TypeVar


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import goalflight_compat  # noqa: E402
import goalflight_task  # noqa: E402


CURRENT_SCHEMA_EPOCH = 3
CURRENT_PROTOCOL_EPOCH = 3
CURRENT_REGISTRY_EPOCH = 3
CURRENT_READER_EPOCH = 3
CURRENT_WRITER_EPOCH = 3
CURRENT_SCHEMA_COLUMNS = {
    "journal_migrations": ("migration_id", "applied_at"),
    "dispatch_attempts": (
        "attempt_id", "dispatch_id", "project_root", "lifecycle_state",
        "launch_epoch", "launch_token", "worker_instance_json", "prepared_at",
        "state_updated_at", "start_deadline_at", "terminal_transition_id",
        "terminal_state", "terminal_outcome_json", "terminal_at",
    ),
    "dispatch_transitions": (
        "attempt_id", "transition_id", "from_state", "to_state",
        "terminal_state", "observation_json", "created_at",
    ),
    "terminal_outbox": (
        "attempt_id", "transition_id", "origin_node", "event_uuid", "recipient",
        "event_type", "payload_json", "created_at", "projected_at",
        "projection_attempts", "projection_error",
    ),
    "controller_leases": (
        "project_root", "label", "generation", "nonce", "principal_json", "pid",
        "start_token", "state", "claimed_at", "renewed_at", "renew_deadline_at",
        "ended_at", "ended_reason",
    ),
    "controller_cursors": (
        "project_root", "label", "registry_generation", "cursor_version",
        "backlog_pending", "updated_at", "advanced_at", "advanced_by",
    ),
    "controller_stream_cursors": (
        "project_root", "label", "stream_id", "position", "updated_at",
    ),
    "listener_coverage": (
        "coverage_id", "project_root", "label", "lease_generation", "lease_nonce",
        "pid", "start_token", "parent_pid", "armed_at", "state", "exited_at",
        "exit_reason",
    ),
    "delivery_events": (
        "project_root", "recipient_label", "origin_node", "event_uuid", "stream_id",
        "stream_seq", "carrier_path", "event_type", "wake_class", "created_at",
        "projected_at", "withdrawn_at",
    ),
    "attention_items": (
        "item_id", "project_root", "item_type", "state", "source_label",
        "source_generation", "trigger_side", "reason", "payload_json", "wake_class",
        "created_at", "resolved_at",
    ),
    "journal_secrets": ("singleton", "cursor_token_secret", "created_at"),
}
JOURNAL_FILE_NAME = "state-journal.sqlite3"
JOURNAL_IDENTITY_KEY = "journal_identity"
JOURNAL_IDENTITY_VALUE = "goalflight.state-journal.v1"
MAX_TRANSACTION_OPERATIONS = 128
MAX_OPERATION_ROWS = 10_000
MAX_PARAMETER_VALUE_BYTES = 65_536
MAX_TRANSACTION_PARAMETER_BYTES = 1_048_576
_SQL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_STATE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_IDENTITY_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,254}\Z")
ATTEMPT_PREPARED = "PREPARED"
ATTEMPT_STARTING = "STARTING"
ATTEMPT_RUNNING = "RUNNING"
ATTEMPT_TERMINAL = "TERMINAL"
ATTEMPT_ABANDONED = "ABANDONED"
ATTEMPT_LIVE_STATES = (ATTEMPT_PREPARED, ATTEMPT_STARTING, ATTEMPT_RUNNING)
ATTEMPT_FINAL_STATES = (ATTEMPT_TERMINAL, ATTEMPT_ABANDONED)
TERMINAL_EVENT_TYPES = frozenset({"result", "blocked", "user_need", "user_confirm"})
START_CLAIM_DEADLINE_S = 300.0
DEFAULT_LEASE_HORIZON_S = 15 * 60.0
LEASE_ACTIVE = "ACTIVE"
LEASE_SUPERSEDED = "SUPERSEDED"
LEASE_EXPIRED = "EXPIRED"
LEASE_RETIRED = "RETIRED"
LEASE_ENDED_STATES = (LEASE_SUPERSEDED, LEASE_EXPIRED, LEASE_RETIRED)
COVERAGE_ARMED = "ARMED"
COVERAGE_EXITED = "EXITED"
LISTENER_EXIT_REASONS = frozenset(
    {
        "batch",
        "timeout",
        "superseded",
        "orphaned",
        "stale-lease",
        "corrupt",
        "upgrade-required",
        "journal-unavailable",
    }
)


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


@dataclass(frozen=True)
class AttemptIdentity:
    attempt_id: str
    dispatch_id: str
    launch_token: str
    launch_epoch: int
    lifecycle_state: str


@dataclass(frozen=True)
class TerminalCommit:
    attempt_id: str
    transition_id: str
    dispatch_id: str
    terminal_state: str
    event_uuid: str
    event_type: str
    observation: dict[str, object]
    idempotent: bool = False


@dataclass(frozen=True)
class OutboxProjection:
    attempt_id: str
    transition_id: str
    event_uuid: str
    recorded: bool
    path: str


@dataclass(frozen=True)
class LeaseIdentity:
    label: str
    project_root: str
    generation: int
    nonce: str
    state: str
    claimed_at: str
    renewed_at: str
    renew_deadline_at: str
    principal: dict[str, object]


@dataclass(frozen=True)
class CursorBatch:
    label: str
    project_root: str
    registry_generation: int
    cursor_version: int
    items: tuple[dict[str, object], ...]
    more_pending: bool
    wake_pending: bool
    token: str


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


def _utc_after(seconds: float) -> str:
    return (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


def _parse_utc(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def listener_exit_reason(
    coverage: Mapping[str, object] | None,
    lease: Mapping[str, object] | None,
    *,
    current_parent_pid: int,
    identity_matches: bool,
    now: dt.datetime | None = None,
) -> str | None:
    """Pure listener self-check used by production and constructed-input tests."""
    if not isinstance(coverage, Mapping) or not coverage.get("coverage_id"):
        return "corrupt"
    if coverage.get("state") != COVERAGE_ARMED:
        return str(coverage.get("exit_reason") or "superseded")
    expected_parent = coverage.get("parent_pid")
    if not isinstance(expected_parent, int) or expected_parent <= 0:
        return "corrupt"
    if current_parent_pid != expected_parent:
        return "orphaned"
    if not identity_matches:
        return "corrupt"
    if not isinstance(lease, Mapping):
        return "orphaned"
    if lease.get("state") != LEASE_ACTIVE:
        return "superseded"
    if (
        lease.get("generation") != coverage.get("lease_generation")
        or lease.get("nonce") != coverage.get("lease_nonce")
    ):
        return "superseded"
    deadline = _parse_utc(lease.get("renew_deadline_at"))
    measured_now = now or dt.datetime.now(dt.timezone.utc)
    if deadline is None:
        return "corrupt"
    if deadline <= measured_now.astimezone(dt.timezone.utc):
        return "stale-lease"
    return None


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
                    migrated = self._migrate_to_current(connection)
                    if migrated:
                        connection.commit()
                    else:
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
                self._install_p2_schema(connection)
                self._install_p3_schema(connection)
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

    def _migrate_to_current(self, connection: sqlite3.Connection) -> bool:
        """Upgrade shipped journals while holding the exclusive write lock."""
        row = connection.execute(
            """
            SELECT schema_epoch, protocol_epoch, registry_epoch,
                   minimum_reader_epoch, minimum_writer_epoch
            FROM journal_epochs WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            self._raise_integrity_failure("journal epoch row is missing during migration")
        stored = tuple(int(row[index]) for index in range(5))
        if stored == (
            CURRENT_SCHEMA_EPOCH,
            CURRENT_PROTOCOL_EPOCH,
            CURRENT_REGISTRY_EPOCH,
            CURRENT_READER_EPOCH,
            CURRENT_WRITER_EPOCH,
        ):
            missing, malformed = self._current_schema_issues(connection)
            if malformed:
                self._raise_integrity_failure(
                    "epoch-3 journal has structurally invalid tables: "
                    + ", ".join(malformed)
                )
            repaired = bool(missing)
            if repaired:
                # In-progress P3 builds could stamp epoch 3 before every final
                # P3 table existed.  The installer is idempotent and is the
                # only supported repair for that incomplete-but-valid state.
                self._install_p3_schema(connection)
                missing, malformed = self._current_schema_issues(connection)
            if missing or malformed:
                details = []
                if missing:
                    details.append("missing tables: " + ", ".join(missing))
                if malformed:
                    details.append("structurally invalid tables: " + ", ".join(malformed))
                self._raise_integrity_failure(
                    "epoch-3 journal has incomplete schema after the idempotent P3 installer; "
                    + "; ".join(details)
                )
            return repaired
        if stored not in {(1, 1, 1, 1, 1), (2, 2, 2, 2, 2)}:
            return False
        client = self.client_epochs
        if (client.schema, client.protocol, client.registry, client.reader, client.writer) != (
            CURRENT_SCHEMA_EPOCH,
            CURRENT_PROTOCOL_EPOCH,
            CURRENT_REGISTRY_EPOCH,
            CURRENT_READER_EPOCH,
            CURRENT_WRITER_EPOCH,
        ):
            return False
        if stored == (1, 1, 1, 1, 1):
            self._install_p2_schema(connection)
        self._install_p3_schema(connection)
        now = utc_now()
        connection.execute(
            """
            UPDATE journal_epochs
            SET schema_epoch = ?, protocol_epoch = ?, registry_epoch = ?,
                minimum_reader_epoch = ?, minimum_writer_epoch = ?, updated_at = ?
            WHERE singleton = 1
            """,
            (
                CURRENT_SCHEMA_EPOCH,
                CURRENT_PROTOCOL_EPOCH,
                CURRENT_REGISTRY_EPOCH,
                CURRENT_READER_EPOCH,
                CURRENT_WRITER_EPOCH,
                now,
            ),
        )
        if stored == (1, 1, 1, 1, 1):
            connection.execute(
                """
                INSERT OR IGNORE INTO journal_migrations (migration_id, applied_at)
                VALUES ('p2-terminal-outbox-v2', ?)
                """,
                (now,),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO journal_migrations (migration_id, applied_at)
            VALUES ('p3-leases-listener-v3', ?)
            """,
            (now,),
        )
        return True

    @staticmethod
    def _current_schema_issues(
        connection: sqlite3.Connection,
    ) -> tuple[list[str], list[str]]:
        tables = {
            str(schema_row[0])
            for schema_row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = sorted(set(CURRENT_SCHEMA_COLUMNS) - tables)
        malformed = []
        for table, expected_columns in CURRENT_SCHEMA_COLUMNS.items():
            if table not in tables:
                continue
            actual_columns = tuple(
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({_quote_identifier(table)})"
                )
            )
            if actual_columns != expected_columns:
                malformed.append(table)
        return missing, sorted(malformed)

    def _install_p2_schema(self, connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS journal_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS dispatch_attempts (
                attempt_id TEXT PRIMARY KEY,
                dispatch_id TEXT NOT NULL UNIQUE,
                project_root TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL
                    CHECK (lifecycle_state IN ('PREPARED', 'STARTING', 'RUNNING', 'TERMINAL', 'ABANDONED')),
                launch_epoch INTEGER NOT NULL DEFAULT 0
                    CHECK (typeof(launch_epoch) = 'integer' AND launch_epoch >= 0),
                launch_token TEXT NOT NULL,
                worker_instance_json TEXT,
                prepared_at TEXT NOT NULL,
                state_updated_at TEXT NOT NULL,
                start_deadline_at TEXT,
                terminal_transition_id TEXT UNIQUE,
                terminal_state TEXT,
                terminal_outcome_json TEXT,
                terminal_at TEXT,
                CHECK (
                    (lifecycle_state IN ('TERMINAL', 'ABANDONED')
                     AND terminal_transition_id IS NOT NULL
                     AND terminal_state IS NOT NULL
                     AND terminal_outcome_json IS NOT NULL
                     AND terminal_at IS NOT NULL)
                    OR
                    (lifecycle_state NOT IN ('TERMINAL', 'ABANDONED')
                     AND terminal_transition_id IS NULL
                     AND terminal_state IS NULL
                     AND terminal_outcome_json IS NULL
                     AND terminal_at IS NULL)
                )
            )""",
            """CREATE TABLE IF NOT EXISTS dispatch_transitions (
                attempt_id TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                terminal_state TEXT,
                observation_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (attempt_id, transition_id),
                UNIQUE (transition_id),
                FOREIGN KEY (attempt_id) REFERENCES dispatch_attempts(attempt_id)
            )""",
            """CREATE TABLE IF NOT EXISTS terminal_outbox (
                attempt_id TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                origin_node TEXT NOT NULL,
                event_uuid TEXT NOT NULL,
                recipient TEXT NOT NULL,
                event_type TEXT NOT NULL
                    CHECK (event_type IN ('result', 'blocked', 'user_need', 'user_confirm')),
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                projected_at TEXT,
                projection_attempts INTEGER NOT NULL DEFAULT 0
                    CHECK (typeof(projection_attempts) = 'integer' AND projection_attempts >= 0),
                projection_error TEXT,
                PRIMARY KEY (attempt_id, transition_id),
                UNIQUE (origin_node, event_uuid, recipient),
                FOREIGN KEY (attempt_id, transition_id)
                    REFERENCES dispatch_transitions(attempt_id, transition_id)
            )""",
            """CREATE INDEX IF NOT EXISTS dispatch_attempts_pending_idx
                ON dispatch_attempts (lifecycle_state, state_updated_at, attempt_id)""",
            """CREATE INDEX IF NOT EXISTS terminal_outbox_pending_idx
                ON terminal_outbox (projected_at, created_at, attempt_id, transition_id)""",
        )
        for statement in statements:
            connection.execute(statement)

    def _install_p3_schema(self, connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS journal_secrets (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                cursor_token_secret TEXT NOT NULL
                    CHECK (length(cursor_token_secret) = 64),
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS controller_leases (
                project_root TEXT NOT NULL,
                label TEXT NOT NULL,
                generation INTEGER NOT NULL
                    CHECK (typeof(generation) = 'integer' AND generation >= 1),
                nonce TEXT NOT NULL,
                principal_json TEXT NOT NULL,
                pid INTEGER,
                start_token TEXT,
                state TEXT NOT NULL
                    CHECK (state IN ('ACTIVE', 'SUPERSEDED', 'EXPIRED', 'RETIRED')),
                claimed_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                renew_deadline_at TEXT NOT NULL,
                ended_at TEXT,
                ended_reason TEXT,
                PRIMARY KEY (project_root, label, generation),
                UNIQUE (project_root, label, nonce),
                CHECK (
                    (state = 'ACTIVE' AND ended_at IS NULL AND ended_reason IS NULL)
                    OR
                    (state != 'ACTIVE' AND ended_at IS NOT NULL AND ended_reason IS NOT NULL)
                )
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS controller_leases_one_active_idx
                ON controller_leases (project_root, label) WHERE state = 'ACTIVE'""",
            """CREATE INDEX IF NOT EXISTS controller_leases_expiry_idx
                ON controller_leases (state, renew_deadline_at, project_root, label)""",
            """CREATE TABLE IF NOT EXISTS controller_cursors (
                project_root TEXT NOT NULL,
                label TEXT NOT NULL,
                registry_generation INTEGER NOT NULL
                    CHECK (typeof(registry_generation) = 'integer' AND registry_generation >= 1),
                cursor_version INTEGER NOT NULL DEFAULT 0
                    CHECK (typeof(cursor_version) = 'integer' AND cursor_version >= 0),
                backlog_pending INTEGER NOT NULL DEFAULT 0
                    CHECK (backlog_pending IN (0, 1)),
                updated_at TEXT NOT NULL,
                advanced_at TEXT,
                advanced_by TEXT,
                PRIMARY KEY (project_root, label)
            )""",
            """CREATE TABLE IF NOT EXISTS controller_stream_cursors (
                project_root TEXT NOT NULL,
                label TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                position INTEGER NOT NULL
                    CHECK (typeof(position) = 'integer' AND position >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_root, label, stream_id),
                FOREIGN KEY (project_root, label)
                    REFERENCES controller_cursors(project_root, label) ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS listener_coverage (
                coverage_id TEXT PRIMARY KEY,
                project_root TEXT NOT NULL,
                label TEXT NOT NULL,
                lease_generation INTEGER NOT NULL,
                lease_nonce TEXT NOT NULL,
                pid INTEGER NOT NULL CHECK (typeof(pid) = 'integer' AND pid > 0),
                start_token TEXT NOT NULL,
                parent_pid INTEGER NOT NULL
                    CHECK (typeof(parent_pid) = 'integer' AND parent_pid > 0),
                armed_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('ARMED', 'EXITED')),
                exited_at TEXT,
                exit_reason TEXT,
                CHECK (
                    (state = 'ARMED' AND exited_at IS NULL AND exit_reason IS NULL)
                    OR
                    (state = 'EXITED' AND exited_at IS NOT NULL AND exit_reason IS NOT NULL)
                ),
                FOREIGN KEY (project_root, label, lease_generation)
                    REFERENCES controller_leases(project_root, label, generation)
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS listener_coverage_one_armed_idx
                ON listener_coverage (project_root, label, lease_generation)
                WHERE state = 'ARMED'""",
            """CREATE INDEX IF NOT EXISTS listener_coverage_lookup_idx
                ON listener_coverage (project_root, label, state, armed_at)""",
            """CREATE TABLE IF NOT EXISTS delivery_events (
                project_root TEXT NOT NULL,
                recipient_label TEXT NOT NULL,
                origin_node TEXT NOT NULL,
                event_uuid TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                stream_seq INTEGER NOT NULL
                    CHECK (typeof(stream_seq) = 'integer' AND stream_seq >= 1),
                carrier_path TEXT NOT NULL,
                event_type TEXT NOT NULL,
                wake_class TEXT NOT NULL CHECK (wake_class IN ('waking', 'quiet')),
                created_at TEXT NOT NULL,
                projected_at TEXT,
                withdrawn_at TEXT,
                PRIMARY KEY (project_root, recipient_label, origin_node, event_uuid)
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS delivery_events_live_stream_seq_idx
                ON delivery_events (project_root, recipient_label, stream_id, stream_seq)
                WHERE withdrawn_at IS NULL""",
            """CREATE INDEX IF NOT EXISTS delivery_events_pending_idx
                ON delivery_events (
                    project_root, recipient_label, wake_class,
                    created_at, event_uuid, stream_id, stream_seq
                )""",
            """CREATE TABLE IF NOT EXISTS attention_items (
                item_id TEXT PRIMARY KEY,
                project_root TEXT NOT NULL,
                item_type TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('OPEN', 'RESOLVED')),
                source_label TEXT NOT NULL,
                source_generation INTEGER NOT NULL,
                trigger_side TEXT NOT NULL CHECK (trigger_side IN ('listener', 'horizon')),
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                wake_class TEXT NOT NULL CHECK (wake_class = 'waking'),
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE (project_root, source_label, source_generation, item_type),
                FOREIGN KEY (project_root, source_label, source_generation)
                    REFERENCES controller_leases(project_root, label, generation)
            )""",
            """CREATE INDEX IF NOT EXISTS attention_items_open_idx
                ON attention_items (project_root, state, created_at, item_id)""",
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            """INSERT OR IGNORE INTO journal_secrets (
                   singleton, cursor_token_secret, created_at
               ) VALUES (1, ?, ?)""",
            (secrets.token_hex(32), utc_now()),
        )

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

    def _domain_write(self, action: Callable[[sqlite3.Connection], T]) -> WriteResult[T]:
        """Run one module-owned bounded transaction for P2 state machines."""
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
                    reason=f"journal write lock timeout within {self.retry_budget_s:.3f}s",
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
                deadline = time.monotonic() + self.transaction_budget_s
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() >= deadline else 0,
                    1000,
                )
                connection.execute("BEGIN IMMEDIATE")
                self._assert_epoch_fence(connection, for_write=True)
                value = action(connection)
                if time.monotonic() >= deadline:
                    raise TimeoutError("journal transaction deadline reached")
                connection.commit()
                return WriteResult(
                    WriteDisposition.COMMITTED,
                    value=value,
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
                    reason=f"transaction exceeded {self.transaction_budget_s:.3f}s budget: {exc}",
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
                        reason=f"journal busy timeout within {self.retry_budget_s:.3f}s",
                    )
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()
                write_lock.release()

    @staticmethod
    def _identity_token(value: object, *, label: str) -> str:
        text = str(value or "")
        if not _IDENTITY_TOKEN_RE.fullmatch(text):
            raise ValueError(f"{label} must be a bounded identity token")
        return text

    @staticmethod
    def _state_token(value: object, *, label: str) -> str:
        text = str(value or "")
        if not _STATE_TOKEN_RE.fullmatch(text):
            raise ValueError(f"{label} must be a bounded state token")
        return text

    @staticmethod
    def _canonical_uuid(value: object, *, label: str) -> str:
        try:
            parsed = uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"{label} must be a canonical UUID") from exc
        text = str(parsed)
        if text != str(value):
            raise ValueError(f"{label} must be a canonical UUID")
        return text

    @staticmethod
    def _json_object(value: Mapping[str, object] | None, *, label: str) -> str:
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ValueError(f"{label} must be an object")
        try:
            return json.dumps(
                dict(value),
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError(f"{label} must be canonical JSON: {exc}") from exc

    @staticmethod
    def _lease_identity(row: Mapping[str, object]) -> LeaseIdentity:
        return LeaseIdentity(
            str(row["label"]),
            str(row["project_root"]),
            int(row["generation"]),
            str(row["nonce"]),
            str(row["state"]),
            str(row["claimed_at"]),
            str(row["renewed_at"]),
            str(row["renew_deadline_at"]),
            json.loads(str(row["principal_json"])),
        )

    @staticmethod
    def _principal_matches(row: Mapping[str, object], principal: Mapping[str, object]) -> bool:
        stored_row = dict(row)
        stored_pid = stored_row.get("pid")
        stored_token = stored_row.get("start_token")
        supplied_pid = principal.get("pid")
        supplied_token = principal.get("start_token")
        if stored_pid is not None or stored_token is not None:
            return bool(
                isinstance(stored_pid, int)
                and stored_pid > 0
                and stored_pid == supplied_pid
                and stored_token
                and stored_token == supplied_token
            )
        try:
            stored = json.loads(str(stored_row["principal_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(stored, dict)
            and stored.get("principal_id")
            and stored.get("principal_id") == principal.get("principal_id")
        )

    @staticmethod
    def _care_work_exists(
        connection: sqlite3.Connection,
        *,
        project_root: str,
        label: str,
    ) -> bool:
        live_attempt = connection.execute(
            """
            SELECT 1 FROM dispatch_attempts
            WHERE project_root = ? AND lifecycle_state IN ('PREPARED', 'STARTING', 'RUNNING')
            LIMIT 1
            """,
            (project_root,),
        ).fetchone()
        if live_attempt is not None:
            return True
        waking = connection.execute(
            """
            SELECT 1
            FROM delivery_events AS e
            LEFT JOIN controller_stream_cursors AS c
              ON c.project_root = e.project_root
             AND c.label = ?
             AND c.stream_id = e.stream_id
            WHERE e.project_root = ?
              AND e.recipient_label IN (?, '*')
              AND e.wake_class = 'waking'
              AND e.projected_at IS NOT NULL
              AND e.withdrawn_at IS NULL
              AND e.stream_seq > COALESCE(c.position, 0)
            LIMIT 1
            """,
            (label, project_root, label),
        ).fetchone()
        return waking is not None

    @classmethod
    def _materialize_attention(
        cls,
        connection: sqlite3.Connection,
        *,
        project_root: str,
        label: str,
        generation: int,
        trigger_side: str,
        reason: str,
    ) -> dict[str, object] | None:
        if trigger_side not in {"listener", "horizon"}:
            raise ValueError("attention trigger_side must be listener or horizon")
        if not cls._care_work_exists(
            connection,
            project_root=project_root,
            label=label,
        ):
            return None
        existing = connection.execute(
            """
            SELECT * FROM attention_items
            WHERE project_root = ? AND source_label = ? AND source_generation = ?
              AND item_type = 'orphaned_controller_work'
            """,
            (project_root, label, generation),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        item_id = str(uuid.uuid4())
        now = utc_now()
        payload = {
            "item_id": item_id,
            "type": "orphaned_controller_work",
            "source_label": label,
            "source_generation": generation,
            "trigger_side": trigger_side,
            "reason": reason,
            "text": f"controller lease {label} generation {generation} needs reassignment",
        }
        connection.execute(
            """
            INSERT INTO attention_items (
                item_id, project_root, item_type, state, source_label,
                source_generation, trigger_side, reason, payload_json,
                wake_class, created_at
            ) VALUES (?, ?, 'orphaned_controller_work', 'OPEN', ?, ?, ?, ?, ?, 'waking', ?)
            """,
            (
                item_id,
                project_root,
                label,
                generation,
                trigger_side,
                reason,
                cls._json_object(payload, label="attention_payload"),
                now,
            ),
        )
        next_seq = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(stream_seq), 0) + 1
                FROM delivery_events
                WHERE project_root = ? AND recipient_label = '*' AND stream_id = 'attention'
                """,
                (project_root,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO delivery_events (
                project_root, recipient_label, origin_node, event_uuid,
                stream_id, stream_seq, carrier_path, event_type,
                wake_class, created_at, projected_at
            ) VALUES (?, '*', 'journal', ?, 'attention', ?, ?,
                      'controller_attention', 'waking', ?, ?)
            """,
            (
                project_root,
                item_id,
                next_seq,
                f"journal:attention:{item_id}",
                now,
                now,
            ),
        )
        return payload

    def active_lease(self, label: str) -> LeaseIdentity | None:
        resolved_label = self._identity_token(label, label="controller label")
        rows = self.read_all(
            """
            SELECT * FROM controller_leases
            WHERE project_root = ? AND label = ? AND state = 'ACTIVE'
            """,
            (str(self.project_root), resolved_label),
        )
        return self._lease_identity(rows[0]) if rows else None

    def lease_records(self, *, include_ended: bool = False) -> list[dict[str, object]]:
        sql = "SELECT * FROM controller_leases WHERE project_root = ?"
        parameters: tuple[object, ...] = (str(self.project_root),)
        if not include_ended:
            sql += " AND state = 'ACTIVE'"
        sql += " ORDER BY label, generation"
        return [dict(row) for row in self.read_all(sql, parameters)]

    def claim_or_renew_lease(
        self,
        label: str,
        *,
        principal: Mapping[str, object],
        nonce: str | None = None,
        horizon_s: float = DEFAULT_LEASE_HORIZON_S,
        takeover: bool = False,
    ) -> WriteResult[LeaseIdentity]:
        resolved_label = self._identity_token(label, label="controller label")
        if not isinstance(principal, Mapping):
            raise ValueError("lease principal must be an object")
        if not 0 < horizon_s <= 7 * 24 * 60 * 60:
            raise ValueError("lease horizon must be in (0, 604800] seconds")
        principal_json = self._json_object(principal, label="lease principal")
        pid = principal.get("pid")
        start_token = principal.get("start_token")
        if pid is not None and (not isinstance(pid, int) or pid <= 0):
            raise ValueError("lease principal pid must be a positive integer")
        if (pid is None) != (start_token is None):
            raise ValueError("lease principal pid and start_token must be supplied together")
        supplied_nonce = (
            self._identity_token(nonce, label="lease nonce") if nonce is not None else None
        )
        project_root = str(self.project_root)

        def action(connection: sqlite3.Connection) -> LeaseIdentity:
            now = utc_now()
            deadline = _utc_after(horizon_s)
            active = connection.execute(
                """
                SELECT * FROM controller_leases
                WHERE project_root = ? AND label = ? AND state = 'ACTIVE'
                """,
                (project_root, resolved_label),
            ).fetchone()
            if active is not None and str(active["renew_deadline_at"]) <= now:
                connection.execute(
                    """
                    UPDATE controller_leases
                    SET state = 'EXPIRED', ended_at = ?, ended_reason = 'renewal-horizon'
                    WHERE project_root = ? AND label = ? AND generation = ? AND state = 'ACTIVE'
                    """,
                    (now, project_root, resolved_label, int(active["generation"])),
                )
                self._materialize_attention(
                    connection,
                    project_root=project_root,
                    label=resolved_label,
                    generation=int(active["generation"]),
                    trigger_side="horizon",
                    reason="stale-lease",
                )
                active = None
            if active is not None:
                same_principal = self._principal_matches(active, principal)
                # Process identity (pid + start token), or the stable principal_id
                # fallback, identifies the incumbent.  The nonce is a lease
                # capability returned to that principal, not a second identity
                # requirement: claim-or-renew must also work when a fresh helper
                # can re-measure its controller but cannot carry the nonce.
                if same_principal:
                    connection.execute(
                        """
                        UPDATE controller_leases
                        SET renewed_at = ?, renew_deadline_at = ?, principal_json = ?,
                            pid = ?, start_token = ?
                        WHERE project_root = ? AND label = ? AND generation = ?
                          AND nonce = ? AND state = 'ACTIVE'
                        """,
                        (
                            now,
                            deadline,
                            principal_json,
                            pid,
                            start_token,
                            project_root,
                            resolved_label,
                            int(active["generation"]),
                            str(active["nonce"]),
                        ),
                    )
                    renewed = connection.execute(
                        """SELECT * FROM controller_leases
                           WHERE project_root = ? AND label = ? AND generation = ?""",
                        (project_root, resolved_label, int(active["generation"])),
                    ).fetchone()
                    assert renewed is not None
                    return self._lease_identity(renewed)
                if not takeover:
                    raise CASMismatch(
                        f"label in use: {resolved_label}; choose another label or take over explicitly"
                    )
                connection.execute(
                    """
                    UPDATE controller_leases
                    SET state = 'SUPERSEDED', ended_at = ?, ended_reason = 'explicit-takeover'
                    WHERE project_root = ? AND label = ? AND generation = ? AND state = 'ACTIVE'
                    """,
                    (now, project_root, resolved_label, int(active["generation"])),
                )
                connection.execute(
                    """
                    UPDATE listener_coverage
                    SET state = 'EXITED', exited_at = ?, exit_reason = 'superseded'
                    WHERE project_root = ? AND label = ? AND lease_generation = ?
                      AND state = 'ARMED'
                    """,
                    (now, project_root, resolved_label, int(active["generation"])),
                )
            generation = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(generation), 0) + 1 FROM controller_leases
                    WHERE project_root = ? AND label = ?
                    """,
                    (project_root, resolved_label),
                ).fetchone()[0]
            )
            allocated_nonce = supplied_nonce or uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO controller_leases (
                    project_root, label, generation, nonce, principal_json,
                    pid, start_token, state, claimed_at, renewed_at, renew_deadline_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
                """,
                (
                    project_root,
                    resolved_label,
                    generation,
                    allocated_nonce,
                    principal_json,
                    pid,
                    start_token,
                    now,
                    now,
                    deadline,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE controller_cursors
                SET registry_generation = ?, cursor_version = cursor_version + 1,
                    updated_at = ?, advanced_at = NULL, advanced_by = NULL
                WHERE project_root = ? AND label = ?
                """,
                (generation, now, project_root, resolved_label),
            )
            if cursor.rowcount == 0:
                connection.execute(
                    """
                    INSERT INTO controller_cursors (
                        project_root, label, registry_generation, cursor_version, updated_at
                    ) VALUES (?, ?, ?, 0, ?)
                    """,
                    (project_root, resolved_label, generation, now),
                )
            row = connection.execute(
                """SELECT * FROM controller_leases
                   WHERE project_root = ? AND label = ? AND generation = ?""",
                (project_root, resolved_label, generation),
            ).fetchone()
            assert row is not None
            return self._lease_identity(row)

        return self._domain_write(action)

    def expire_stale_leases(self, *, observed_at: str | None = None) -> WriteResult[list[dict[str, object]]]:
        parsed_cutoff = _parse_utc(observed_at or utc_now())
        if parsed_cutoff is None:
            raise ValueError("observed_at must be an RFC3339 timestamp")
        cutoff = parsed_cutoff.isoformat(timespec="seconds")
        project_root = str(self.project_root)

        def action(connection: sqlite3.Connection) -> list[dict[str, object]]:
            rows = connection.execute(
                """
                SELECT * FROM controller_leases
                WHERE project_root = ? AND state = 'ACTIVE' AND renew_deadline_at <= ?
                ORDER BY label, generation
                """,
                (project_root, cutoff),
            ).fetchall()
            expired: list[dict[str, object]] = []
            for row in rows:
                connection.execute(
                    """
                    UPDATE controller_leases
                    SET state = 'EXPIRED', ended_at = ?, ended_reason = 'renewal-horizon'
                    WHERE project_root = ? AND label = ? AND generation = ? AND state = 'ACTIVE'
                    """,
                    (cutoff, project_root, str(row["label"]), int(row["generation"])),
                )
                connection.execute(
                    """
                    UPDATE listener_coverage
                    SET state = 'EXITED', exited_at = ?, exit_reason = 'stale-lease'
                    WHERE project_root = ? AND label = ? AND lease_generation = ?
                      AND state = 'ARMED'
                    """,
                    (cutoff, project_root, str(row["label"]), int(row["generation"])),
                )
                attention = self._materialize_attention(
                    connection,
                    project_root=project_root,
                    label=str(row["label"]),
                    generation=int(row["generation"]),
                    trigger_side="horizon",
                    reason="stale-lease",
                )
                expired.append(
                    {
                        "label": str(row["label"]),
                        "generation": int(row["generation"]),
                        "attention_item": attention,
                    }
                )
            return expired

        return self._domain_write(action)

    def release_lease(
        self,
        label: str,
        *,
        nonce: str,
        reason: str = "released",
    ) -> WriteResult[LeaseIdentity]:
        resolved_label = self._identity_token(label, label="controller label")
        resolved_nonce = self._identity_token(nonce, label="lease nonce")
        resolved_reason = self._state_token(reason, label="lease release reason")
        project_root = str(self.project_root)

        def action(connection: sqlite3.Connection) -> LeaseIdentity:
            row = connection.execute(
                """SELECT * FROM controller_leases
                   WHERE project_root = ? AND label = ? AND nonce = ? AND state = 'ACTIVE'""",
                (project_root, resolved_label, resolved_nonce),
            ).fetchone()
            if row is None:
                raise CASMismatch("lease release lost: active generation or nonce changed")
            now = utc_now()
            connection.execute(
                """
                UPDATE controller_leases
                SET state = 'RETIRED', ended_at = ?, ended_reason = ?
                WHERE project_root = ? AND label = ? AND generation = ? AND state = 'ACTIVE'
                """,
                (now, resolved_reason, project_root, resolved_label, int(row["generation"])),
            )
            connection.execute(
                """
                UPDATE listener_coverage
                SET state = 'EXITED', exited_at = ?, exit_reason = 'orphaned'
                WHERE project_root = ? AND label = ? AND lease_generation = ? AND state = 'ARMED'
                """,
                (now, project_root, resolved_label, int(row["generation"])),
            )
            ended = dict(row)
            ended.update(state=LEASE_RETIRED, ended_at=now, ended_reason=resolved_reason)
            return self._lease_identity(ended)

        return self._domain_write(action)

    def record_delivery_event(
        self,
        *,
        recipient_label: str,
        origin_node: str,
        event_uuid: str,
        stream_id: str,
        stream_seq: int,
        carrier_path: Path | str,
        event_type: str,
        wake_class: str,
        created_at: str,
        replaces: Iterable[tuple[str, str, str]] = (),
    ) -> WriteResult[dict[str, object]]:
        recipient = (
            "*"
            if recipient_label == "*"
            else self._identity_token(recipient_label, label="recipient label")
        )
        origin = self._identity_token(origin_node, label="origin node")
        event_id = self._canonical_uuid(event_uuid, label="event_uuid")
        stream = self._identity_token(stream_id, label="stream id")
        kind = self._identity_token(event_type, label="event type")
        if not isinstance(stream_seq, int) or isinstance(stream_seq, bool) or stream_seq < 1:
            raise ValueError("stream_seq must be a positive integer")
        if wake_class not in {"waking", "quiet"}:
            raise ValueError("wake_class must be waking or quiet")
        if _parse_utc(created_at) is None:
            raise ValueError("created_at must be an RFC3339 timestamp")
        replacement_keys: list[tuple[str, str, str]] = []
        for replacement in replaces:
            try:
                old_recipient, old_origin, old_event_id = replacement
            except (TypeError, ValueError) as exc:
                raise ValueError("delivery replacement identity must have three fields") from exc
            normalized_key = (
                "*"
                if old_recipient == "*"
                else self._identity_token(old_recipient, label="replacement recipient label"),
                self._identity_token(old_origin, label="replacement origin node"),
                self._canonical_uuid(old_event_id, label="replacement event_uuid"),
            )
            if normalized_key == (recipient, origin, event_id):
                raise ValueError("delivery event cannot replace itself")
            if normalized_key not in replacement_keys:
                replacement_keys.append(normalized_key)
        path = str(Path(carrier_path).resolve(strict=False))
        project_root = str(self.project_root)

        def action(connection: sqlite3.Connection) -> dict[str, object]:
            existing = connection.execute(
                """
                SELECT * FROM delivery_events
                WHERE project_root = ? AND recipient_label = ?
                  AND origin_node = ? AND event_uuid = ?
                """,
                (project_root, recipient, origin, event_id),
            ).fetchone()
            expected = {
                "stream_id": stream,
                "stream_seq": stream_seq,
                "carrier_path": path,
                "event_type": kind,
                "wake_class": wake_class,
            }
            if existing is not None:
                if any(existing[key] != value for key, value in expected.items()):
                    raise JournalIntegrityError(
                        "delivery event identity conflict: same recipient/origin/event_uuid has different content"
                    )
            withdrawn_at = utc_now()
            for old_recipient, old_origin, old_event_id in replacement_keys:
                connection.execute(
                    """
                    UPDATE delivery_events
                    SET withdrawn_at = COALESCE(withdrawn_at, ?)
                    WHERE project_root = ? AND recipient_label = ?
                      AND origin_node = ? AND event_uuid = ?
                    """,
                    (
                        withdrawn_at,
                        project_root,
                        old_recipient,
                        old_origin,
                        old_event_id,
                    ),
                )
            if existing is not None:
                return dict(existing)
            connection.execute(
                """
                INSERT INTO delivery_events (
                    project_root, recipient_label, origin_node, event_uuid,
                    stream_id, stream_seq, carrier_path, event_type,
                    wake_class, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_root,
                    recipient,
                    origin,
                    event_id,
                    stream,
                    stream_seq,
                    path,
                    kind,
                    wake_class,
                    created_at,
                ),
            )
            return {
                "project_root": project_root,
                "recipient_label": recipient,
                "origin_node": origin,
                "event_uuid": event_id,
                **expected,
                "created_at": created_at,
            }

        return self._domain_write(action)

    def mark_delivery_projected(
        self,
        *,
        recipient_label: str,
        origin_node: str,
        event_uuid: str,
    ) -> WriteResult[dict[str, object]]:
        recipient = (
            "*"
            if recipient_label == "*"
            else self._identity_token(recipient_label, label="recipient label")
        )
        origin = self._identity_token(origin_node, label="origin node")
        event_id = self._canonical_uuid(event_uuid, label="event_uuid")
        project_root = str(self.project_root)

        def action(connection: sqlite3.Connection) -> dict[str, object]:
            row = connection.execute(
                """
                SELECT * FROM delivery_events
                WHERE project_root = ? AND recipient_label = ?
                  AND origin_node = ? AND event_uuid = ?
                """,
                (project_root, recipient, origin, event_id),
            ).fetchone()
            if row is None:
                raise CASMismatch("delivery projection lost: assignment row is absent")
            projected_at = str(row["projected_at"] or utc_now())
            connection.execute(
                """
                UPDATE delivery_events SET projected_at = ?
                WHERE project_root = ? AND recipient_label = ?
                  AND origin_node = ? AND event_uuid = ? AND projected_at IS NULL
                """,
                (projected_at, project_root, recipient, origin, event_id),
            )
            result = dict(row)
            result["projected_at"] = projected_at
            return result

        return self._domain_write(action)

    def withdraw_delivery_event(
        self,
        *,
        recipient_label: str,
        origin_node: str,
        event_uuid: str,
    ) -> WriteResult[dict[str, object] | None]:
        recipient = (
            "*"
            if recipient_label == "*"
            else self._identity_token(recipient_label, label="recipient label")
        )
        origin = self._identity_token(origin_node, label="origin node")
        event_id = self._canonical_uuid(event_uuid, label="event_uuid")
        project_root = str(self.project_root)

        def action(connection: sqlite3.Connection) -> dict[str, object] | None:
            row = connection.execute(
                """
                SELECT * FROM delivery_events
                WHERE project_root = ? AND recipient_label = ?
                  AND origin_node = ? AND event_uuid = ?
                """,
                (project_root, recipient, origin, event_id),
            ).fetchone()
            if row is None:
                return None
            withdrawn_at = str(row["withdrawn_at"] or utc_now())
            connection.execute(
                """
                UPDATE delivery_events SET withdrawn_at = ?
                WHERE project_root = ? AND recipient_label = ?
                  AND origin_node = ? AND event_uuid = ? AND withdrawn_at IS NULL
                """,
                (withdrawn_at, project_root, recipient, origin, event_id),
            )
            result = dict(row)
            result["withdrawn_at"] = withdrawn_at
            return result

        return self._domain_write(action)

    @staticmethod
    def _cursor_token_secret(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT cursor_token_secret FROM journal_secrets WHERE singleton = 1"
        ).fetchone()
        secret = str(row["cursor_token_secret"] if row is not None else "")
        if not re.fullmatch(r"[0-9a-f]{64}", secret):
            raise JournalIntegrityError("journal cursor-token secret is missing or corrupt")
        return secret

    @staticmethod
    def _encode_cursor_token(payload: Mapping[str, object], secret: str) -> str:
        raw = json.dumps(
            dict(payload),
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        signature = hmac.new(
            bytes.fromhex(secret),
            body.encode("ascii"),
            hashlib.sha256,
        ).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        return f"{body}.{encoded_signature}"

    @staticmethod
    def _decode_cursor_token(token: str, secret: str) -> dict[str, object]:
        try:
            body, encoded_signature = token.split(".", 1)
            expected = hmac.new(
                bytes.fromhex(secret),
                body.encode("ascii"),
                hashlib.sha256,
            ).digest()
            expected_signature = base64.urlsafe_b64encode(expected).decode("ascii").rstrip("=")
            if not hmac.compare_digest(encoded_signature, expected_signature):
                raise ValueError("cursor token integrity check failed")
            padded = body + "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("cursor token is corrupt") from exc
        if not isinstance(payload, dict):
            raise ValueError("cursor token must encode an object")
        return payload

    def cursor_batch(
        self,
        label: str,
        *,
        nonce: str,
        limit: int = 50,
    ) -> CursorBatch:
        resolved_label = self._identity_token(label, label="controller label")
        resolved_nonce = self._identity_token(nonce, label="lease nonce")
        if not 1 <= limit <= 1000:
            raise ValueError("listener batch limit must be between 1 and 1000")
        project_root = str(self.project_root)
        with contextlib.closing(self._connect()) as connection:
            self._assert_epoch_fence(connection, for_write=False)
            token_secret = self._cursor_token_secret(connection)
            lease = connection.execute(
                """SELECT * FROM controller_leases
                   WHERE project_root = ? AND label = ? AND state = 'ACTIVE'""",
                (project_root, resolved_label),
            ).fetchone()
            if lease is None or str(lease["nonce"]) != resolved_nonce:
                raise CASMismatch("listener lease generation or nonce is no longer active")
            if str(lease["renew_deadline_at"]) <= utc_now():
                raise CASMismatch("listener lease is stale")
            cursor = connection.execute(
                """SELECT registry_generation, cursor_version, backlog_pending
                   FROM controller_cursors
                   WHERE project_root = ? AND label = ?""",
                (project_root, resolved_label),
            ).fetchone()
            if cursor is None or int(cursor["registry_generation"]) != int(lease["generation"]):
                raise JournalIntegrityError("active lease is missing its generation-matched cursor")
            waking_event_pending = connection.execute(
                """
                SELECT 1
                FROM delivery_events AS e
                LEFT JOIN controller_stream_cursors AS c
                  ON c.project_root = e.project_root
                 AND c.label = ?
                 AND c.stream_id = e.stream_id
                WHERE e.project_root = ? AND e.recipient_label IN (?, '*')
                  AND e.wake_class = 'waking'
                  AND e.projected_at IS NOT NULL
                  AND e.withdrawn_at IS NULL
                  AND e.stream_seq > COALESCE(c.position, 0)
                LIMIT 1
                """,
                (resolved_label, project_root, resolved_label),
            ).fetchone() is not None
            rows = connection.execute(
                """
                SELECT e.*
                FROM delivery_events AS e
                LEFT JOIN controller_stream_cursors AS c
                  ON c.project_root = e.project_root
                 AND c.label = ?
                 AND c.stream_id = e.stream_id
                WHERE e.project_root = ? AND e.recipient_label IN (?, '*')
                  AND e.projected_at IS NOT NULL
                  AND e.withdrawn_at IS NULL
                  AND e.stream_seq > COALESCE(c.position, 0)
                ORDER BY e.stream_id, e.stream_seq, e.rowid
                LIMIT ?
                """,
                (resolved_label, project_root, resolved_label, limit + 1),
            ).fetchall()
        wake_pending = bool(rows) and (
            bool(cursor["backlog_pending"]) or waking_event_pending
        )
        selected = rows[:limit] if wake_pending else []
        more_pending = bool(wake_pending and len(rows) > limit)
        advances: dict[str, int] = {}
        for row in selected:
            stream = str(row["stream_id"])
            advances[stream] = max(advances.get(stream, 0), int(row["stream_seq"]))
        token_payload = {
            "project_root": project_root,
            "label": resolved_label,
            "registry_generation": int(cursor["registry_generation"]),
            "cursor_version": int(cursor["cursor_version"]),
            "lease_nonce": resolved_nonce,
            "advances": advances,
            "more_pending": more_pending,
        }
        token = self._encode_cursor_token(token_payload, token_secret) if selected else ""
        return CursorBatch(
            resolved_label,
            project_root,
            int(cursor["registry_generation"]),
            int(cursor["cursor_version"]),
            tuple(dict(row) for row in selected),
            more_pending,
            wake_pending,
            token,
        )

    def pending_delivery_events(
        self,
        label: str,
        *,
        waking_only: bool = False,
        stream_ids: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, object]]:
        resolved_label = self._identity_token(label, label="controller label")
        if not 1 <= limit <= 10_000:
            raise ValueError("pending delivery limit must be between 1 and 10000")
        streams = tuple(
            self._identity_token(value, label="stream id") for value in (stream_ids or ())
        )
        sql = """
            SELECT e.*
            FROM delivery_events AS e
            LEFT JOIN controller_stream_cursors AS c
              ON c.project_root = e.project_root
             AND c.label = ?
             AND c.stream_id = e.stream_id
            WHERE e.project_root = ? AND e.recipient_label IN (?, '*')
              AND e.projected_at IS NOT NULL AND e.withdrawn_at IS NULL
              AND e.stream_seq > COALESCE(c.position, 0)
        """
        parameters: list[object] = [resolved_label, str(self.project_root), resolved_label]
        if waking_only:
            sql += " AND e.wake_class = 'waking'"
        if streams:
            sql += " AND e.stream_id IN (" + ",".join("?" for _ in streams) + ")"
            parameters.extend(streams)
        sql += " ORDER BY e.rowid LIMIT ?"
        parameters.append(limit)
        return [dict(row) for row in self.read_all(sql, parameters)]

    def delivery_event_watermark(
        self,
        *,
        stream_ids: Iterable[str],
        waking_only: bool = True,
    ) -> set[tuple[str, str, str]]:
        streams = tuple(self._identity_token(value, label="stream id") for value in stream_ids)
        if not streams:
            return set()
        sql = """
            SELECT recipient_label, origin_node, event_uuid
            FROM delivery_events
            WHERE project_root = ? AND projected_at IS NOT NULL AND withdrawn_at IS NULL
              AND stream_id IN (
        """ + ",".join("?" for _ in streams) + ")"
        parameters: list[object] = [str(self.project_root), *streams]
        if waking_only:
            sql += " AND wake_class = 'waking'"
        rows = self.read_all(sql, parameters)
        return {
            (str(row["recipient_label"]), str(row["origin_node"]), str(row["event_uuid"]))
            for row in rows
        }

    def cursor_status(self, label: str) -> dict[str, object] | None:
        resolved_label = self._identity_token(label, label="controller label")
        rows = self.read_all(
            """
            SELECT * FROM controller_cursors
            WHERE project_root = ? AND label = ?
            """,
            (str(self.project_root), resolved_label),
        )
        if not rows:
            return None
        result = dict(rows[0])
        result["positions"] = {
            str(row["stream_id"]): int(row["position"])
            for row in self.read_all(
                """
                SELECT stream_id, position FROM controller_stream_cursors
                WHERE project_root = ? AND label = ? ORDER BY stream_id
                """,
                (str(self.project_root), resolved_label),
            )
        }
        return result

    def advance_cursor(
        self,
        token: str,
        *,
        actor: str,
    ) -> WriteResult[dict[str, object]]:
        with contextlib.closing(self._connect()) as connection:
            self._assert_epoch_fence(connection, for_write=False)
            token_secret = self._cursor_token_secret(connection)
        payload = self._decode_cursor_token(token, token_secret)
        project_root = str(payload.get("project_root") or "")
        if project_root != str(self.project_root):
            raise ValueError("cursor token belongs to another project")
        label = self._identity_token(payload.get("label"), label="cursor label")
        nonce = self._identity_token(payload.get("lease_nonce"), label="cursor lease nonce")
        actor_value = self._identity_token(actor, label="cursor actor")
        generation = payload.get("registry_generation")
        version = payload.get("cursor_version")
        advances = payload.get("advances")
        more_pending = payload.get("more_pending")
        if not isinstance(generation, int) or generation < 1:
            raise ValueError("cursor token registry_generation is invalid")
        if not isinstance(version, int) or version < 0:
            raise ValueError("cursor token cursor_version is invalid")
        if not isinstance(advances, dict) or not advances:
            raise ValueError("cursor token advances are empty or invalid")
        if not isinstance(more_pending, bool):
            raise ValueError("cursor token more_pending is invalid")
        normalized_advances: dict[str, int] = {}
        for stream, position in advances.items():
            resolved_stream = self._identity_token(stream, label="cursor stream")
            if not isinstance(position, int) or isinstance(position, bool) or position < 1:
                raise ValueError("cursor token position is invalid")
            normalized_advances[resolved_stream] = position

        def action(connection: sqlite3.Connection) -> dict[str, object]:
            lease = connection.execute(
                """SELECT generation, nonce, state, renew_deadline_at FROM controller_leases
                   WHERE project_root = ? AND label = ? AND state = 'ACTIVE'""",
                (project_root, label),
            ).fetchone()
            if (
                lease is None
                or int(lease["generation"]) != generation
                or str(lease["nonce"]) != nonce
                or str(lease["renew_deadline_at"]) <= utc_now()
            ):
                raise CASMismatch("cursor CAS lost: lease generation changed")
            now = utc_now()
            updated = connection.execute(
                """
                UPDATE controller_cursors
                SET cursor_version = cursor_version + 1, updated_at = ?,
                    backlog_pending = ?, advanced_at = ?, advanced_by = ?
                WHERE project_root = ? AND label = ?
                  AND registry_generation = ? AND cursor_version = ?
                """,
                (
                    now,
                    int(more_pending),
                    now,
                    actor_value,
                    project_root,
                    label,
                    generation,
                    version,
                ),
            )
            if updated.rowcount != 1:
                raise CASMismatch(
                    "cursor CAS lost: registry_generation or cursor_version changed"
                )
            for stream, position in normalized_advances.items():
                connection.execute(
                    """
                    INSERT INTO controller_stream_cursors (
                        project_root, label, stream_id, position, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(project_root, label, stream_id) DO UPDATE SET
                        position = MAX(position, excluded.position),
                        updated_at = excluded.updated_at
                    """,
                    (project_root, label, stream, position, now),
                )
            return {
                "label": label,
                "registry_generation": generation,
                "previous_cursor_version": version,
                "cursor_version": version + 1,
                "advances": normalized_advances,
                "more_pending": more_pending,
            }

        return self._domain_write(action)

    def arm_listener(
        self,
        label: str,
        *,
        nonce: str,
        pid: int,
        start_token: str,
        parent_pid: int,
    ) -> WriteResult[dict[str, object]]:
        resolved_label = self._identity_token(label, label="controller label")
        resolved_nonce = self._identity_token(nonce, label="lease nonce")
        resolved_start = self._identity_token(start_token, label="listener start token")
        if not isinstance(pid, int) or pid <= 0 or not isinstance(parent_pid, int) or parent_pid <= 0:
            raise ValueError("listener pid and parent_pid must be positive integers")
        project_root = str(self.project_root)

        def action(connection: sqlite3.Connection) -> dict[str, object]:
            lease = connection.execute(
                """SELECT * FROM controller_leases
                   WHERE project_root = ? AND label = ? AND state = 'ACTIVE'""",
                (project_root, resolved_label),
            ).fetchone()
            if lease is None or str(lease["nonce"]) != resolved_nonce:
                raise CASMismatch("listener arm lost: lease generation or nonce changed")
            if str(lease["renew_deadline_at"]) <= utc_now():
                raise CASMismatch("listener arm lost: lease is stale")
            now = utc_now()
            connection.execute(
                """
                UPDATE listener_coverage
                SET state = 'EXITED', exited_at = ?, exit_reason = 'superseded'
                WHERE project_root = ? AND label = ? AND lease_generation = ?
                  AND state = 'ARMED'
                """,
                (now, project_root, resolved_label, int(lease["generation"])),
            )
            coverage_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO listener_coverage (
                    coverage_id, project_root, label, lease_generation,
                    lease_nonce, pid, start_token, parent_pid, armed_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ARMED')
                """,
                (
                    coverage_id,
                    project_root,
                    resolved_label,
                    int(lease["generation"]),
                    resolved_nonce,
                    pid,
                    resolved_start,
                    parent_pid,
                    now,
                ),
            )
            return {
                "coverage_id": coverage_id,
                "project_root": project_root,
                "label": resolved_label,
                "lease_generation": int(lease["generation"]),
                "lease_nonce": resolved_nonce,
                "pid": pid,
                "start_token": resolved_start,
                "parent_pid": parent_pid,
                "armed_at": now,
                "state": COVERAGE_ARMED,
            }

        return self._domain_write(action)

    def coverage(self, coverage_id: str) -> dict[str, object] | None:
        resolved_id = self._canonical_uuid(coverage_id, label="coverage_id")
        rows = self.read_all(
            "SELECT * FROM listener_coverage WHERE coverage_id = ?",
            (resolved_id,),
        )
        return dict(rows[0]) if rows else None

    def active_coverage(self, label: str) -> dict[str, object] | None:
        resolved_label = self._identity_token(label, label="controller label")
        rows = self.read_all(
            """
            SELECT c.* FROM listener_coverage AS c
            JOIN controller_leases AS l
              ON l.project_root = c.project_root AND l.label = c.label
             AND l.generation = c.lease_generation
            WHERE c.project_root = ? AND c.label = ?
              AND c.state = 'ARMED' AND l.state = 'ACTIVE'
            """,
            (str(self.project_root), resolved_label),
        )
        return dict(rows[0]) if rows else None

    def exit_listener(
        self,
        coverage_id: str,
        *,
        reason: str,
    ) -> WriteResult[dict[str, object]]:
        resolved_id = self._canonical_uuid(coverage_id, label="coverage_id")
        if reason not in LISTENER_EXIT_REASONS:
            raise ValueError("listener exit reason is not registered")

        def action(connection: sqlite3.Connection) -> dict[str, object]:
            row = connection.execute(
                "SELECT * FROM listener_coverage WHERE coverage_id = ?",
                (resolved_id,),
            ).fetchone()
            if row is None:
                raise CASMismatch("listener exit lost: coverage row is absent")
            if str(row["state"]) == COVERAGE_EXITED:
                return dict(row)
            now = utc_now()
            connection.execute(
                """
                UPDATE listener_coverage
                SET state = 'EXITED', exited_at = ?, exit_reason = ?
                WHERE coverage_id = ? AND state = 'ARMED'
                """,
                (now, reason, resolved_id),
            )
            if reason in {"orphaned", "stale-lease"}:
                self._materialize_attention(
                    connection,
                    project_root=str(row["project_root"]),
                    label=str(row["label"]),
                    generation=int(row["lease_generation"]),
                    trigger_side="listener",
                    reason=reason,
                )
            if reason == "stale-lease":
                connection.execute(
                    """
                    UPDATE controller_leases
                    SET state = 'EXPIRED', ended_at = ?, ended_reason = 'renewal-horizon'
                    WHERE project_root = ? AND label = ? AND generation = ?
                      AND state = 'ACTIVE' AND renew_deadline_at <= ?
                    """,
                    (
                        now,
                        str(row["project_root"]),
                        str(row["label"]),
                        int(row["lease_generation"]),
                        now,
                    ),
                )
            result = dict(row)
            result.update(state=COVERAGE_EXITED, exited_at=now, exit_reason=reason)
            return result

        return self._domain_write(action)

    def attention_items(self, *, state: str = "OPEN") -> list[dict[str, object]]:
        resolved_state = self._state_token(state, label="attention state")
        rows = self.read_all(
            """
            SELECT * FROM attention_items
            WHERE project_root = ? AND state = ? ORDER BY created_at, item_id
            """,
            (str(self.project_root), resolved_state),
        )
        return [dict(row) for row in rows]

    def attempt_for_dispatch(self, dispatch_id: str) -> AttemptIdentity | None:
        dispatch = self._identity_token(dispatch_id, label="dispatch_id")
        rows = self.read_all(
            """
            SELECT attempt_id, dispatch_id, launch_token, launch_epoch, lifecycle_state
            FROM dispatch_attempts WHERE dispatch_id = ?
            """,
            (dispatch,),
        )
        if not rows:
            return None
        row = rows[0]
        return AttemptIdentity(
            str(row["attempt_id"]),
            str(row["dispatch_id"]),
            str(row["launch_token"]),
            int(row["launch_epoch"]),
            str(row["lifecycle_state"]),
        )

    def prepare_attempt(
        self,
        dispatch_id: str,
        *,
        attempt_id: str | None = None,
        launch_token: str | None = None,
        start_deadline_at: str | None = None,
        defer_start_deadline: bool = False,
    ) -> WriteResult[AttemptIdentity]:
        dispatch = self._identity_token(dispatch_id, label="dispatch_id")
        if defer_start_deadline and start_deadline_at is not None:
            raise ValueError(
                "defer_start_deadline cannot be combined with start_deadline_at"
            )
        supplied_attempt = (
            self._canonical_uuid(attempt_id, label="attempt_id") if attempt_id else None
        )
        supplied_token = (
            self._identity_token(launch_token, label="launch_token") if launch_token else None
        )

        def action(connection: sqlite3.Connection) -> AttemptIdentity:
            existing = connection.execute(
                """
                SELECT attempt_id, dispatch_id, launch_token, launch_epoch, lifecycle_state
                FROM dispatch_attempts WHERE dispatch_id = ?
                """,
                (dispatch,),
            ).fetchone()
            if existing is not None:
                identity = AttemptIdentity(
                    str(existing["attempt_id"]),
                    str(existing["dispatch_id"]),
                    str(existing["launch_token"]),
                    int(existing["launch_epoch"]),
                    str(existing["lifecycle_state"]),
                )
                if supplied_attempt and supplied_attempt != identity.attempt_id:
                    raise CASMismatch("dispatch already belongs to a different attempt_id")
                if supplied_token and supplied_token != identity.launch_token:
                    raise CASMismatch("dispatch already belongs to a different launch_token")
                return identity
            allocated_attempt = supplied_attempt or str(uuid.uuid4())
            allocated_token = supplied_token or uuid.uuid4().hex
            now = utc_now()
            resolved_start_deadline = (
                None
                if defer_start_deadline
                else start_deadline_at
                or (
                    dt.datetime.now(dt.timezone.utc)
                    + dt.timedelta(seconds=START_CLAIM_DEADLINE_S)
                ).isoformat(timespec="seconds")
            )
            connection.execute(
                """
                INSERT INTO dispatch_attempts (
                    attempt_id, dispatch_id, project_root, lifecycle_state,
                    launch_epoch, launch_token, prepared_at, state_updated_at,
                    start_deadline_at
                ) VALUES (?, ?, ?, 'PREPARED', 0, ?, ?, ?, ?)
                """,
                (
                    allocated_attempt,
                    dispatch,
                    str(self.project_root),
                    allocated_token,
                    now,
                    now,
                    resolved_start_deadline,
                ),
            )
            return AttemptIdentity(
                allocated_attempt,
                dispatch,
                allocated_token,
                0,
                ATTEMPT_PREPARED,
            )

        return self._domain_write(action)

    def start_attempt(
        self,
        attempt_id: str,
        launch_token: str,
        *,
        expected_launch_epoch: int = 0,
    ) -> WriteResult[AttemptIdentity]:
        attempt = self._canonical_uuid(attempt_id, label="attempt_id")
        token = self._identity_token(launch_token, label="launch_token")
        if expected_launch_epoch < 0:
            raise ValueError("expected_launch_epoch must be >= 0")

        def action(connection: sqlite3.Connection) -> AttemptIdentity:
            now = utc_now()
            deadline_override = goalflight_compat.allowed_env_override(
                "GOALFLIGHT_TEST_START_CLAIM_DEADLINE_S", "", test_mode=True
            )
            deadline_seconds = (
                float(deadline_override)
                if deadline_override
                else START_CLAIM_DEADLINE_S
            )
            start_deadline = (
                dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(seconds=deadline_seconds)
            ).isoformat(timespec="seconds")
            cursor = connection.execute(
                """
                UPDATE dispatch_attempts
                SET lifecycle_state = 'STARTING', launch_epoch = launch_epoch + 1,
                    state_updated_at = ?, start_deadline_at = ?
                WHERE attempt_id = ? AND launch_token = ?
                  AND lifecycle_state = 'PREPARED' AND launch_epoch = ?
                """,
                (now, start_deadline, attempt, token, expected_launch_epoch),
            )
            if cursor.rowcount != 1:
                raise CASMismatch(
                    "PREPARED -> STARTING lost: attempt, token, state, or launch_epoch changed"
                )
            row = connection.execute(
                """
                SELECT attempt_id, dispatch_id, launch_token, launch_epoch, lifecycle_state
                FROM dispatch_attempts WHERE attempt_id = ?
                """,
                (attempt,),
            ).fetchone()
            assert row is not None
            return AttemptIdentity(
                str(row["attempt_id"]),
                str(row["dispatch_id"]),
                str(row["launch_token"]),
                int(row["launch_epoch"]),
                str(row["lifecycle_state"]),
            )

        return self._domain_write(action)

    def mark_attempt_running(
        self,
        attempt_id: str,
        launch_token: str,
        *,
        launch_epoch: int,
        worker_instance: Mapping[str, object],
    ) -> WriteResult[AttemptIdentity]:
        attempt = self._canonical_uuid(attempt_id, label="attempt_id")
        token = self._identity_token(launch_token, label="launch_token")
        if launch_epoch < 1:
            raise ValueError("launch_epoch must be >= 1")
        worker_json = self._json_object(worker_instance, label="worker_instance")

        def action(connection: sqlite3.Connection) -> AttemptIdentity:
            cursor = connection.execute(
                """
                UPDATE dispatch_attempts
                SET lifecycle_state = 'RUNNING', worker_instance_json = ?, state_updated_at = ?
                WHERE attempt_id = ? AND launch_token = ?
                  AND lifecycle_state = 'STARTING' AND launch_epoch = ?
                """,
                (worker_json, utc_now(), attempt, token, launch_epoch),
            )
            if cursor.rowcount != 1:
                raise CASMismatch(
                    "STARTING -> RUNNING lost: attempt, token, state, or launch_epoch changed"
                )
            row = connection.execute(
                """
                SELECT attempt_id, dispatch_id, launch_token, launch_epoch, lifecycle_state
                FROM dispatch_attempts WHERE attempt_id = ?
                """,
                (attempt,),
            ).fetchone()
            assert row is not None
            return AttemptIdentity(
                str(row["attempt_id"]),
                str(row["dispatch_id"]),
                str(row["launch_token"]),
                int(row["launch_epoch"]),
                str(row["lifecycle_state"]),
            )

        return self._domain_write(action)

    def commit_terminal(
        self,
        attempt_id: str,
        *,
        terminal_state: str,
        observation: Mapping[str, object] | None = None,
        event_type: str | None = None,
        _deadline_at_or_before: str | None = None,
    ) -> WriteResult[TerminalCommit]:
        """CAS one terminal winner and its outbox event in the same transaction."""
        attempt = self._canonical_uuid(attempt_id, label="attempt_id")
        terminal = self._state_token(terminal_state, label="terminal_state")
        resolved_event_type = event_type or ("result" if terminal == "complete" else "blocked")
        if resolved_event_type not in TERMINAL_EVENT_TYPES:
            raise ValueError(
                "terminal event_type must be result, blocked, user_need, or user_confirm"
            )
        observation_json = self._json_object(observation, label="observation")

        def action(connection: sqlite3.Connection) -> TerminalCommit:
            existing = connection.execute(
                """
                SELECT a.dispatch_id, a.lifecycle_state, a.terminal_state,
                       a.terminal_transition_id, a.terminal_outcome_json,
                       a.start_deadline_at,
                       o.event_uuid, o.event_type
                FROM dispatch_attempts AS a
                LEFT JOIN terminal_outbox AS o
                  ON o.attempt_id = a.attempt_id
                 AND o.transition_id = a.terminal_transition_id
                WHERE a.attempt_id = ?
                """,
                (attempt,),
            ).fetchone()
            if existing is None:
                raise CASMismatch("terminal commit lost: attempt does not exist")
            if str(existing["lifecycle_state"]) in ATTEMPT_FINAL_STATES:
                if not existing["terminal_transition_id"] or not existing["event_uuid"]:
                    raise JournalIntegrityError(
                        "terminal attempt exists without its transition/outbox row"
                    )
                return TerminalCommit(
                    attempt,
                    str(existing["terminal_transition_id"]),
                    str(existing["dispatch_id"]),
                    str(existing["terminal_state"]),
                    str(existing["event_uuid"]),
                    str(existing["event_type"]),
                    json.loads(str(existing["terminal_outcome_json"])),
                    True,
                )
            if str(existing["lifecycle_state"]) not in ATTEMPT_LIVE_STATES:
                raise CASMismatch(
                    f"terminal commit lost: attempt state is {existing['lifecycle_state']}"
                )
            if _deadline_at_or_before is not None:
                if str(existing["lifecycle_state"]) not in {
                    ATTEMPT_PREPARED,
                    ATTEMPT_STARTING,
                }:
                    raise CASMismatch(
                        "expired launch abandonment lost: worker already claimed or attempt changed"
                    )
                deadline = existing["start_deadline_at"]
                if deadline is None or str(deadline) > _deadline_at_or_before:
                    raise CASMismatch(
                        "expired launch abandonment lost: start deadline is no longer expired"
                    )
            transition_id = str(uuid.uuid4())
            event_uuid = str(uuid.uuid4())
            now = utc_now()
            final_lifecycle = (
                ATTEMPT_ABANDONED if terminal == "abandoned" else ATTEMPT_TERMINAL
            )
            observation_value = json.loads(observation_json)
            outcome = (
                observation_value.get("outcome")
                if isinstance(observation_value, dict)
                else None
            )
            error = outcome.get("error") if isinstance(outcome, dict) else None
            marker_text = error.get("text") if isinstance(error, dict) else None
            payload = {
                "attempt_id": attempt,
                "transition_id": transition_id,
                "dispatch_id": str(existing["dispatch_id"]),
                "terminal_state": terminal,
                "complete": resolved_event_type == "result",
                "text": str(marker_text or f"dispatch terminal: {terminal}"),
                "observation": observation_value,
            }
            payload_json = self._json_object(payload, label="outbox_payload")
            if _deadline_at_or_before is None:
                cursor = connection.execute(
                    """
                    UPDATE dispatch_attempts
                    SET lifecycle_state = ?, terminal_transition_id = ?,
                        terminal_state = ?, terminal_outcome_json = ?, terminal_at = ?,
                        state_updated_at = ?
                    WHERE attempt_id = ?
                      AND lifecycle_state IN ('PREPARED', 'STARTING', 'RUNNING')
                    """,
                    (
                        final_lifecycle,
                        transition_id,
                        terminal,
                        observation_json,
                        now,
                        now,
                        attempt,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE dispatch_attempts
                    SET lifecycle_state = ?, terminal_transition_id = ?,
                        terminal_state = ?, terminal_outcome_json = ?, terminal_at = ?,
                        state_updated_at = ?
                    WHERE attempt_id = ?
                      AND lifecycle_state IN ('PREPARED', 'STARTING')
                      AND start_deadline_at IS NOT NULL
                      AND start_deadline_at <= ?
                    """,
                    (
                        final_lifecycle,
                        transition_id,
                        terminal,
                        observation_json,
                        now,
                        now,
                        attempt,
                        _deadline_at_or_before,
                    ),
                )
            if cursor.rowcount != 1:
                raise CASMismatch("terminal commit lost to another classifier")
            pause_file = goalflight_compat.allowed_env_override(
                "GOALFLIGHT_TEST_TERMINAL_PAUSE_FILE", "", test_mode=True
            )
            if pause_file:
                marker = Path(pause_file)
                marker.write_text("state-updated\n", encoding="utf-8")
                while marker.exists():
                    time.sleep(0.005)
            connection.execute(
                """
                INSERT INTO dispatch_transitions (
                    attempt_id, transition_id, from_state, to_state,
                    terminal_state, observation_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt,
                    transition_id,
                    str(existing["lifecycle_state"]),
                    final_lifecycle,
                    terminal,
                    observation_json,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO terminal_outbox (
                    attempt_id, transition_id, origin_node, event_uuid,
                    recipient, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt,
                    transition_id,
                    socket.gethostname(),
                    event_uuid,
                    str(existing["dispatch_id"]),
                    resolved_event_type,
                    payload_json,
                    now,
                ),
            )
            return TerminalCommit(
                attempt,
                transition_id,
                str(existing["dispatch_id"]),
                terminal,
                event_uuid,
                resolved_event_type,
                json.loads(observation_json),
                False,
            )

        return self._domain_write(action)

    def commit_expired_attempt(
        self,
        attempt_id: str,
        *,
        observed_at: str,
        terminal_state: str = "abandoned",
        observation: Mapping[str, object] | None = None,
    ) -> WriteResult[TerminalCommit]:
        """Commit a reconciled terminal only while its launch remains expired."""
        return self.commit_terminal(
            attempt_id,
            terminal_state=terminal_state,
            observation=observation,
            event_type="result" if terminal_state == "complete" else "blocked",
            _deadline_at_or_before=observed_at,
        )

    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 1000:
            raise ValueError("outbox limit must be between 1 and 1000")
        rows = self.read_all(
            """
            SELECT attempt_id, transition_id, origin_node, event_uuid,
                   recipient, event_type, payload_json, created_at,
                   projection_attempts
            FROM terminal_outbox
            WHERE projected_at IS NULL
            ORDER BY created_at, attempt_id, transition_id
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def project_terminal_outbox(
        self,
        *,
        messages_dir: Path,
        limit: int = 100,
    ) -> list[OutboxProjection]:
        """Project terminal events to the carrier; event UUID makes retries idempotent."""
        import goalflight_messages

        projected: list[OutboxProjection] = []
        for row in self.pending_outbox(limit=limit):
            payload = json.loads(str(row["payload_json"]))
            try:
                result = goalflight_messages.post_message(
                    dispatch_id=str(row["recipient"]),
                    msg_type=str(row["event_type"]),
                    payload=payload,
                    messages_dir=messages_dir,
                    source={
                        "node": str(row["origin_node"]),
                        "adapter": "journal-outbox",
                        "transport": "journal",
                    },
                    event_id=str(row["event_uuid"]),
                    event_ts=str(row["created_at"]),
                )
            except Exception as exc:
                self.write(
                    RowOperation.update(
                        "terminal_outbox",
                        {
                            "projection_attempts": int(row["projection_attempts"]) + 1,
                            "projection_error": f"{type(exc).__name__}: {exc}"[:2000],
                        },
                        where={
                            "attempt_id": str(row["attempt_id"]),
                            "transition_id": str(row["transition_id"]),
                        },
                        row_cap=1,
                    )
                )
                continue
            marked = self.write(
                RowOperation.update(
                    "terminal_outbox",
                    {
                        "projected_at": utc_now(),
                        "projection_attempts": int(row["projection_attempts"]) + 1,
                        "projection_error": None,
                    },
                    where={
                        "attempt_id": str(row["attempt_id"]),
                        "transition_id": str(row["transition_id"]),
                    },
                    row_cap=1,
                    expected_rows=1,
                )
            )
            if marked.committed:
                projected.append(
                    OutboxProjection(
                        str(row["attempt_id"]),
                        str(row["transition_id"]),
                        str(row["event_uuid"]),
                        bool(result.get("recorded")),
                        str(result["path"]),
                    )
                )
        return projected

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


def open_or_create_journal(project_root: Path | str) -> Journal:
    """Open authority, explicitly bootstrapping only a truly absent path."""
    path = resolve_journal_path(project_root)
    if os.path.lexists(path):
        return Journal(project_root)
    try:
        return Journal.create(project_root)
    except JournalError:
        if not os.path.lexists(path):
            raise
        return Journal(project_root)


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
    subject: str = "snapshot",
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
            f"UPGRADE_REQUIRED: {subject} epoch fence refused restore before replacement: "
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
        live_epochs = _validate_snapshot_file(destination)
        _assert_snapshot_epoch_compatibility(live_epochs, subject="live journal")
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
            except BaseException as install_exc:
                try:
                    for sidecar in live_sidecars:
                        sidecar.unlink(missing_ok=True)
                    os.replace(displaced, destination)
                    _fsync_directory(destination.parent)
                    preserve_displaced = False
                except BaseException as rollback_exc:
                    preserve_displaced = displaced.exists()
                    rollback_state = (
                        f"pre-image kept at {displaced}"
                        if preserve_displaced
                        else "pre-image restored but directory durability is unconfirmed"
                    )
                    raise JournalIntegrityError(
                        f"restore failed after displacement and rollback failed; {rollback_state}: "
                        f"install={install_exc}; rollback={rollback_exc}"
                    ) from install_exc
                raise
            displaced.unlink()
            preserve_displaced = False
            _fsync_directory(destination.parent)
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
    except OSError as exc:
        raise JournalUnavailable(f"cannot fsync journal directory {path}: {exc}") from exc
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            raise JournalUnavailable(f"cannot fsync journal directory {path}: {exc}") from exc
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
