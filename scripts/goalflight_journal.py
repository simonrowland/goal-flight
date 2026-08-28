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
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shlex
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
import goalflight_wake  # noqa: E402


CURRENT_SCHEMA_EPOCH = 6
CURRENT_PROTOCOL_EPOCH = 6
CURRENT_REGISTRY_EPOCH = 6
CURRENT_READER_EPOCH = 6
CURRENT_WRITER_EPOCH = 6
CURRENT_SCHEMA_COLUMNS = {
    "journal_migrations": ("migration_id", "applied_at"),
    "dispatch_attempts": (
        "attempt_id", "dispatch_id", "project_root", "lifecycle_state",
        "launch_epoch", "launch_token", "worker_instance_json", "prepared_at",
        "state_updated_at", "start_deadline_at", "terminal_transition_id",
        "terminal_state", "terminal_outcome_json", "terminal_at",
        "owner_controller_label", "owner_session_digest",
        "effective_account", "engine",
    ),
    "dispatch_transitions": (
        "attempt_id", "transition_id", "from_state", "to_state",
        "terminal_state", "observation_json", "created_at",
    ),
    "terminal_outbox": (
        "attempt_id", "transition_id", "origin_node", "event_uuid", "recipient",
        "event_type", "payload_json", "created_at", "projected_at",
        "projection_attempts", "projection_error", "projection_retry_at",
        "projection_quarantined_at",
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
    "system_attention_items": (
        "item_id", "project_root", "item_type", "state", "reason",
        "payload_json", "wake_class", "created_at", "resolved_at",
    ),
}
_LEGACY_DISPATCH_ATTEMPTS_COLUMNS = CURRENT_SCHEMA_COLUMNS["dispatch_attempts"][:-4]
_EPOCH_FIVE_DISPATCH_ATTEMPTS_COLUMNS = CURRENT_SCHEMA_COLUMNS["dispatch_attempts"][:-2]
_LEGACY_TERMINAL_OUTBOX_COLUMNS = CURRENT_SCHEMA_COLUMNS["terminal_outbox"][:-2]
JOURNAL_FILE_NAME = "state-journal.sqlite3"
JOURNAL_IDENTITY_KEY = "journal_identity"
JOURNAL_IDENTITY_VALUE = "goalflight.state-journal.v1"
MAX_TRANSACTION_OPERATIONS = 128
MAX_OPERATION_ROWS = 10_000
MAX_PARAMETER_VALUE_BYTES = 65_536
MAX_TRANSACTION_PARAMETER_BYTES = 1_048_576
OUTBOX_MAX_PROJECTION_ATTEMPTS = 3
OUTBOX_RETRY_BASE_S = 1.0
# The live incident cleared within roughly one minute while the journal stayed
# healthy. Seventy-five seconds covers that measured minute plus 15 seconds of
# scheduler/load margin. A smaller bound repeats the observed false teardown;
# doubling it to 150 seconds would add 75 seconds of unwitnessed failure before
# a genuinely unreachable journal is reported and re-armed, with no measured
# recovery benefit. Per-process exponential jitter keeps the three witnesses'
# probes independent inside the shared bound.
JOURNAL_OPEN_RETRY_BUDGET_S = 75.0
JOURNAL_OPEN_RETRY_INITIAL_S = 0.050
JOURNAL_OPEN_RETRY_MAX_S = 5.0
# Writer-capable clients sit on durable launch, lifecycle, and cursor-CAS paths:
# failing them can poison an id or replay acknowledged-looking mail.  Under 64
# concurrent writers, successful *construction* measured 0.024-3.725s (N=7,
# median 0.202s); 5s covers that one observed tail plus 1.275s of load margin.
# N=7 makes p95 equal the max by construction — the 3.725s point is a single
# trial, not a distribution. Journal.write and post-open _read_with_retry on a
# writer instance inherit this default; those paths were not timed. General
# read clients retain the 1s responsiveness contract; stricter liveness probes
# opt into 0.05s.
JOURNAL_WRITER_RETRY_BUDGET_S = 5.0
JOURNAL_READER_RETRY_BUDGET_S = 1.0
ALLOW_MIGRATION_ENV = "GOALFLIGHT_ALLOW_JOURNAL_MIGRATION"
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
# Worker marker kinds whose payload is the human-facing outbox line. The
# machine-readable terminal_state stays on the envelope; only `text` changes.
OUTBOX_HEADLINE_MARKER_KINDS = frozenset(
    {
        "COMPLETE",
        "READY",
        "RESULT",
        "BLOCKED",
        "FAILED",
        "USER-NEED",
        "USER-CONFIRM",
    }
)


def outbox_headline_text(terminal_state: str, observation: object) -> str:
    """Human-facing outbox line: worker marker text, else the state string.

    Controllers drain this field. A completed worker's own COMPLETE/BLOCKED
    headline is the work; ``dispatch terminal: <state>`` is only the fallback
    when no marker was harvested. ``terminal_state`` on the same payload is
    unchanged. Completions travel on ``headline``, not ``outcome.error``.

    Attention headlines must already have passed
    ``goalflight_terminal.parse_own_signal_attention_line`` via watcher
    ``harvest_headline_marker`` and arrived here as ``observation.headline``.
    ``last_marker`` / ``terminal_marker`` attention is scrape vocabulary and
    is not promoted; SUCCESS markers may still headline from those keys.
    """
    import goalflight_terminal

    fallback = f"dispatch terminal: {terminal_state}"
    if not isinstance(observation, dict):
        return fallback
    headline = observation.get("headline")
    if isinstance(headline, str) and headline.strip():
        return headline.strip()
    for key in ("last_marker", "terminal_marker"):
        marker = observation.get(key)
        if not isinstance(marker, dict):
            continue
        kind = marker.get("kind")
        text = marker.get("text")
        if kind in goalflight_terminal.ATTENTION_MARKERS:
            continue
        if (
            kind in OUTBOX_HEADLINE_MARKER_KINDS
            and isinstance(text, str)
            and text.strip()
        ):
            return text.strip()
        for marker_kind, marker_text in marker.items():
            if marker_kind in goalflight_terminal.ATTENTION_MARKERS:
                continue
            if (
                marker_kind in OUTBOX_HEADLINE_MARKER_KINDS
                and isinstance(marker_text, str)
                and marker_text.strip()
            ):
                return marker_text.strip()
    # Pre-headline records stored the worker line on the error channel.
    outcome = observation.get("outcome")
    error = outcome.get("error") if isinstance(outcome, dict) else None
    if isinstance(error, dict):
        text = error.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return fallback


START_CLAIM_DEADLINE_S = 300.0
# Compatibility horizon for the legacy schema/API.  The derived deadline is
# telemetry only; held kernel locks are the sole lease-liveness authority.
DEFAULT_LEASE_HORIZON_S = 2 * 60 * 60.0
LEASE_ACTIVE = "ACTIVE"
LEASE_SUPERSEDED = "SUPERSEDED"
LEASE_EXPIRED = "EXPIRED"
LEASE_RETIRED = "RETIRED"
LEASE_ENDED_STATES = (LEASE_SUPERSEDED, LEASE_EXPIRED, LEASE_RETIRED)
COVERAGE_ARMED = "ARMED"
COVERAGE_EXITED = "EXITED"
LISTENER_EXIT_REASONS = frozenset(
    {
        "event",
        "timeout",
        "superseded",
        "orphaned",
        "stale-lease",
        "corrupt",
        "upgrade-required",
        "journal-unavailable",
        "journal-io-failure",
        "watchdog-dead",
        "signal",
    }
)


class JournalError(RuntimeError):
    """Base class for clear, operator-facing journal failures."""


class JournalIntegrityError(JournalError):
    """The authoritative journal failed its startup integrity check."""


class JournalUpgradeRequired(JournalError):
    """The client epochs are incompatible with the journal epochs."""


class JournalUnavailable(JournalError):
    """The journal could not complete a required bounded operation."""

    reason = "journal-unavailable"


class JournalBusy(JournalUnavailable):
    """Contention exhausted one journal operation's retry budget."""


class JournalDisappeared(JournalUnavailable):
    """The configured journal path is verifiably absent."""


class JournalIOError(JournalUnavailable):
    """The present journal path cannot be inspected or opened safely."""

    reason = "journal-io-failure"


# Operator-facing repair pointers. Keep these as the first clause of a refusal
# (the arithmetic is context). Commands named here must exist.
_RESUME_SKILL_COMMAND = "/goal-flight resume"
# Relay matches carrier_path.startswith("journal:"); a new kind must keep that
# prefix. Do not claim "attention" — name the repair.
_JOURNAL_CARRIER_PREFIX = "journal:"
_JOURNAL_RESUME_CARRIER_KIND = "goal-flight-resume"


def _doctor_command() -> str:
    return shlex.join([sys.executable, str(SCRIPT_DIR / "goalflight_doctor.py")])


def _upgrade_required_resume(detail: str) -> str:
    return (
        "UPGRADE_REQUIRED: restart this session onto the deployed skill: "
        f"{_RESUME_SKILL_COMMAND}; {detail}"
    )


def _dual_open_unavailable(path: Path, primary: BaseException, fallback: BaseException, *, stage: str) -> str:
    return (
        f"journal readonly probe unavailable/unreadable for {path}: "
        f"readonly open failed ({primary}); query-only fallback {stage} "
        f"failed ({fallback}); failing closed · next: run {_doctor_command()}; "
        f"inspect {path}"
    )


def _synthetic_journal_carrier(kind: str, item_id: str) -> str:
    return f"{_JOURNAL_CARRIER_PREFIX}{kind}:{item_id}"


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


def journal_terminal_at(value: object) -> str:
    """Project a journal ``terminal_at`` column without inventing the token ``None``.

    An idempotent reread of a NULL column used ``str(existing["terminal_at"])``,
    which becomes ``"None"``. Projectors then froze that string as ``ended_at``,
    and completion ordering treated a real terminal as timestamp-indeterminate.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text in {"", "None", "null"}:
        return ""
    return str(value)


@dataclass(frozen=True)
class TerminalCommit:
    attempt_id: str
    transition_id: str
    dispatch_id: str
    terminal_state: str
    event_uuid: str
    event_type: str
    observation: dict[str, object]
    terminal_at: str
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
class LeaseLivenessEvidence:
    generation: int
    nonce: str
    alive: bool | None


@dataclass(frozen=True)
class CursorPeek:
    label: str
    project_root: str
    registry_generation: int
    cursor_version: int
    stream_snapshots: dict[str, str]
    items: tuple[dict[str, object], ...]


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


def _next_lease_renewed_at(previous: object) -> str:
    """Return a timestamp that changes on every renewal, even within one second."""
    current = dt.datetime.now(dt.timezone.utc)
    parsed = _parse_utc(previous)
    if parsed is not None and current <= parsed:
        current = parsed + dt.timedelta(microseconds=1)
    return current.isoformat(timespec="microseconds")


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
    del now  # Kept for call compatibility; deadlines no longer decide liveness.
    return None


def journals_index_dir() -> Path:
    """Directory that holds per-project journal folders.

    ``GOALFLIGHT_JOURNAL_DIR`` is the state base (same as ``resolve_journal_path``),
    not the journals folder itself. Default is the durable state base.
    """
    override = os.environ.get("GOALFLIGHT_JOURNAL_DIR", "").strip()
    state_base = (
        Path(override).expanduser()
        if override
        else goalflight_task.resolve_state_base_dir()
    )
    return (state_base / "journals").resolve(strict=False)


def iter_journal_files() -> list[Path]:
    """Return every journal sqlite path under the journals index.

    Raises ``JournalIOError`` when the index exists but cannot be listed:
    an unreadable index is UNKNOWN, not an empty fleet. A genuinely absent
    index is an empty list. Callers must treat an unreadable *file* as its
    own unknown row rather than skipping the index.
    """
    base = journals_index_dir()
    # pathlib glob swallows PermissionError and yields nothing; iterdir
    # raises, keeping unreadable distinct from absent.
    try:
        children = list(base.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise JournalIOError(
            f"journals index is unreadable, so the fleet roster is unknown: "
            f"{base}: {exc}"
        ) from exc
    files: list[Path] = []
    for child in children:
        if not child.is_dir():
            continue
        candidate = child / JOURNAL_FILE_NAME
        try:
            names = {entry.name for entry in child.iterdir()}
        except OSError:
            # Unreadable per-project dir: the journal path is conventional,
            # so include it and let the caller's peek emit an unknown row.
            files.append(candidate)
            continue
        if JOURNAL_FILE_NAME in names:
            files.append(candidate)
    return sorted(files)


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


def _lstat_presence(path: Path) -> str:
    """Return ``present`` / ``absent`` / ``unknown``. Never ``lexists`` here.

    ``os.path.lexists`` is ``lstat`` wrapped in ``except (OSError, ValueError):
    return False``, so it answers False for both "not there" and "I could not
    look". Only FileNotFoundError is evidence of absence; any other OSError
    means presence could not be verified. Same contract as journal_gc's
    ``_presence``.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "present"


def _is_busy(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc).lower() for marker in ("locked", "busy")
    )


def _sqlite_connect(
    database: str | Path,
    *,
    uri: bool = False,
    timeout: float = 5.0,
    isolation_level: str | None = "",
) -> sqlite3.Connection:
    """Small injection seam for deterministic readonly-open failure tests."""
    return sqlite3.connect(
        database,
        uri=uri,
        timeout=timeout,
        isolation_level=isolation_level,
    )


def _sqlite_primary_error_code(exc: BaseException) -> int | None:
    raw = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(raw, int):
        return None
    return raw & 0xFF


def _is_cantopen(exc: BaseException) -> bool:
    cantopen = getattr(sqlite3, "SQLITE_CANTOPEN", None)
    return (
        isinstance(exc, sqlite3.DatabaseError)
        and (
            (
                isinstance(cantopen, int)
                and _sqlite_primary_error_code(exc) == cantopen
            )
            or "unable to open database file" in str(exc).lower()
        )
    )


def _is_corruption_error(exc: BaseException) -> bool:
    corruption_codes = {
        code
        for code in (
            getattr(sqlite3, "SQLITE_CORRUPT", None),
            getattr(sqlite3, "SQLITE_NOTADB", None),
        )
        if isinstance(code, int)
    }
    sqlite_format = getattr(sqlite3, "SQLITE_FORMAT", None)
    if isinstance(sqlite_format, int):
        corruption_codes.add(sqlite_format)
    message = str(exc).lower()
    return _sqlite_primary_error_code(exc) in corruption_codes or any(
        marker in message
        for marker in (
            "database disk image is malformed",
            "malformed database schema",
            "file is not a database",
        )
    )


def _open_readonly_connection(
    path: Path,
    *,
    timeout: float = 5.0,
    isolation_level: str | None = "",
) -> sqlite3.Connection:
    """Open a query-only reader, unwedging a quiesced WAL when necessary."""
    readonly: sqlite3.Connection | None = None
    primary_failure: sqlite3.DatabaseError | None = None
    try:
        readonly = _sqlite_connect(
            path.as_uri() + "?mode=ro",
            uri=True,
            timeout=timeout,
            isolation_level=isolation_level,
        )
        # SQLite opens lazily. Touch the schema so CANTOPEN is attributed to
        # this readonly open instead of a later schema query being mislabeled.
        readonly.execute("PRAGMA schema_version").fetchone()
        return readonly
    except sqlite3.DatabaseError as primary_exc:
        primary_failure = primary_exc
        opened = readonly is not None
        if readonly is not None:
            readonly.close()
        if not os.path.lexists(path):
            # Preserve each caller's existing absent/disappeared-file handling.
            raise
        if not _is_cantopen(primary_exc):
            if _is_busy(primary_exc) or (
                opened and _is_corruption_error(primary_exc)
            ):
                # Parse/malformed evidence from an opened connection is real
                # corruption evidence; busy remains retryable. A mere open
                # failure never becomes a corruption verdict.
                raise
            raise JournalIOError(
                f"journal readonly probe unavailable/unreadable for {path}: "
                f"readonly open failed: {primary_exc}; failing closed"
            ) from primary_exc

    assert primary_failure is not None
    # A mode=ro connection cannot create the WAL shared-memory file after the
    # last connection closes on a fully-checkpointed quiesced database. A
    # mode=rw open may recreate -shm/-wal and un-wedge it; query_only is the
    # first statement so this handle cannot mutate journal data.
    fallback: sqlite3.Connection | None = None
    try:
        fallback = _sqlite_connect(
            path.as_uri() + "?mode=rw",
            uri=True,
            timeout=timeout,
            isolation_level=isolation_level,
        )
    except sqlite3.DatabaseError as fallback_exc:
        if _is_busy(fallback_exc):
            raise
        raise JournalIOError(
            _dual_open_unavailable(path, primary_failure, fallback_exc, stage="open")
        ) from fallback_exc
    try:
        fallback.execute("PRAGMA query_only = ON")
        fallback.execute("PRAGMA schema_version").fetchone()
        return fallback
    except sqlite3.DatabaseError as fallback_exc:
        fallback.close()
        if _is_corruption_error(fallback_exc) or _is_busy(fallback_exc):
            raise
        raise JournalIOError(
            _dual_open_unavailable(path, primary_failure, fallback_exc, stage="probe")
        ) from fallback_exc


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
        allow_migration: bool | None = None,
        retry_budget_s: float = JOURNAL_WRITER_RETRY_BUDGET_S,
        open_retry_budget_s: float = JOURNAL_OPEN_RETRY_BUDGET_S,
        transaction_budget_s: float = 1.0,
        jitter_min_s: float = 0.005,
        jitter_max_s: float = 0.050,
    ) -> None:
        self._configure(
            project_root,
            client_epochs=client_epochs,
            allow_migration=allow_migration,
            retry_budget_s=retry_budget_s,
            open_retry_budget_s=open_retry_budget_s,
            transaction_budget_s=transaction_budget_s,
            jitter_min_s=jitter_min_s,
            jitter_max_s=jitter_max_s,
        )
        self._require_existing_database()
        write_lock, deadline = self._acquire_construction_lock()
        try:
            self._require_existing_database()
            self._open_validated(created_here=False, busy_deadline_s=deadline)
        finally:
            write_lock.release()

    @classmethod
    def create(
        cls,
        project_root: Path | str,
        *,
        client_epochs: ClientEpochs | None = None,
        retry_budget_s: float = JOURNAL_WRITER_RETRY_BUDGET_S,
        open_retry_budget_s: float = JOURNAL_OPEN_RETRY_BUDGET_S,
        transaction_budget_s: float = 1.0,
        jitter_min_s: float = 0.005,
        jitter_max_s: float = 0.050,
    ) -> "Journal":
        """Explicitly bootstrap a journal; ordinary construction never creates."""
        self = cls.__new__(cls)
        self._configure(
            project_root,
            client_epochs=client_epochs,
            allow_migration=False,
            retry_budget_s=retry_budget_s,
            open_retry_budget_s=open_retry_budget_s,
            transaction_budget_s=transaction_budget_s,
            jitter_min_s=jitter_min_s,
            jitter_max_s=jitter_max_s,
        )
        presence = _lstat_presence(self.path)
        if presence == "unknown":
            raise JournalIOError(
                f"journal init refused because path presence is unreadable, so "
                f"absence is unverified: {self.path}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_lock, deadline = self._acquire_construction_lock()
        try:
            presence = _lstat_presence(self.path)
            if presence == "present":
                raise JournalError(
                    f"journal init refused because the database already exists: {self.path}; "
                    "open it with Journal(...) instead"
                )
            if presence == "unknown":
                raise JournalIOError(
                    f"journal init refused because path presence is unreadable, so "
                    f"absence is unverified: {self.path}"
                )
            self._claim_fresh_database_path()
            try:
                self._open_validated(created_here=True, busy_deadline_s=deadline)
            except BaseException:
                for candidate in (
                    self.path,
                    Path(f"{self.path}-wal"),
                    Path(f"{self.path}-shm"),
                ):
                    candidate.unlink(missing_ok=True)
                raise
        finally:
            write_lock.release()
        return self

    @classmethod
    def open_reader(
        cls,
        project_root: Path | str,
        *,
        client_epochs: ClientEpochs | None = None,
        retry_budget_s: float = JOURNAL_READER_RETRY_BUDGET_S,
        open_retry_budget_s: float = JOURNAL_OPEN_RETRY_BUDGET_S,
        transaction_budget_s: float = 1.0,
        jitter_min_s: float = 0.005,
        jitter_max_s: float = 0.050,
    ) -> "Journal":
        """Open a fenced read client without taking the journal write lock.

        Fast polling reads must not serialize terminal writers behind the
        whole-database startup integrity check and schema bootstrap performed by
        the ordinary constructor. Every read uses either a mode=ro connection or
        a mode=rw handle immediately hardened with query_only, then checks the
        live epoch fence in ``read_all``.
        """
        self = cls.__new__(cls)
        self._configure(
            project_root,
            client_epochs=client_epochs,
            allow_migration=False,
            retry_budget_s=retry_budget_s,
            open_retry_budget_s=open_retry_budget_s,
            transaction_budget_s=transaction_budget_s,
            jitter_min_s=jitter_min_s,
            jitter_max_s=jitter_max_s,
        )
        self._require_existing_database()
        self._read_only_client = True
        return self

    def _configure(
        self,
        project_root: Path | str,
        *,
        client_epochs: ClientEpochs | None,
        allow_migration: bool | None,
        retry_budget_s: float,
        open_retry_budget_s: float,
        transaction_budget_s: float,
        jitter_min_s: float,
        jitter_max_s: float,
    ) -> None:
        if not 0 <= retry_budget_s < float("inf"):
            raise ValueError("retry_budget_s must be finite and >= 0")
        if not 0 <= open_retry_budget_s < float("inf"):
            raise ValueError("open_retry_budget_s must be finite and >= 0")
        if transaction_budget_s <= 0:
            raise ValueError("transaction_budget_s must be > 0")
        if not 0 <= jitter_min_s <= jitter_max_s:
            raise ValueError("journal jitter bounds are invalid")
        self.project_root = goalflight_task.resolve_project_root(str(project_root))
        self.path = resolve_journal_path(self.project_root)
        self.client_epochs = client_epochs or ClientEpochs()
        self.allow_migration = (
            os.environ.get(ALLOW_MIGRATION_ENV) == "1"
            if allow_migration is None
            else bool(allow_migration)
        )
        self.retry_budget_s = retry_budget_s
        self.open_retry_budget_s = open_retry_budget_s
        self.transaction_budget_s = transaction_budget_s
        self.jitter_min_s = jitter_min_s
        self.jitter_max_s = jitter_max_s
        self._read_only_client = False
        self._file_identity: tuple[int, int] | None = None

    def _acquire_construction_lock(self) -> tuple[goalflight_task.FileLock, float]:
        """Bound construction flock to the same retry budget as the open stages.

        ``Journal.write`` already uses ``FileLock.try_acquire`` against one
        absolute deadline. Construction used a blocking ``LOCK_EX`` and then
        started a fresh busy window per integrity / bootstrap / epoch-fence
        stage, so the error text's ``within {retry_budget_s}s`` was not a
        wall-clock bound and queued constructors stacked behind the holder.
        """
        deadline = time.monotonic() + self.retry_budget_s
        write_lock = goalflight_task.FileLock.try_acquire(
            journal_write_lock_path(self.path),
            deadline_s=deadline,
        )
        if write_lock is None:
            raise JournalBusy(
                f"journal construction lock timeout within "
                f"{self.retry_budget_s:.3f}s: {self.path}"
            )
        return write_lock, deadline

    def _require_existing_database(self) -> None:
        presence = _lstat_presence(self.path)
        if presence == "absent":
            raise JournalDisappeared(
                f"journal database is absent: {self.path}. Failing closed because streams "
                "cannot rebuild journal authority. Restore a validated WAL-safe backup; "
                "use the init verb only for an intentional first bootstrap."
            )
        if presence == "unknown":
            # Unreadable is not absent: disappearance is unverified, and the
            # callers' unknown handling (probe "unreadable", roster error,
            # controller_indeterminate) keeps every gate shut.
            raise JournalIOError(
                f"journal path presence is unreadable, so disappearance is unverified: "
                f"{self.path}"
            )
        if self.path.is_symlink():
            raise JournalIntegrityError(
                f"journal integrity check failed for {self.path}: symlinked journal refused; "
                "the journal is authoritative and streams cannot rebuild it"
            )
        try:
            metadata = self.path.stat()
        except FileNotFoundError as exc:
            raise JournalDisappeared(
                f"journal database vanished without creating a replacement: {self.path}"
            ) from exc
        except OSError as exc:
            raise JournalIOError(
                f"journal path identity is unreadable, so disappearance is unverified: "
                f"{self.path}: {exc}"
            ) from exc
        identity = (int(metadata.st_dev), int(metadata.st_ino))
        if self._file_identity is None:
            self._file_identity = identity
        elif identity != self._file_identity:
            raise JournalIntegrityError(
                f"journal database was replaced at {self.path}; expected file identity "
                f"{self._file_identity}, observed {identity}. Failing closed because a "
                "different database cannot inherit this client's authority."
            )

    def _open_validated(
        self, *, created_here: bool, busy_deadline_s: float | None = None
    ) -> None:
        self._startup_integrity_check(busy_deadline_s=busy_deadline_s)
        self._bootstrap_schema(
            created_here=created_here, busy_deadline_s=busy_deadline_s
        )
        # Enforced on open even though P1 has only epoch 1.  Reads repeat the
        # fence so a long-lived client cannot outlive a migration unnoticed.
        self._read_with_retry(
            "journal open epoch fence",
            lambda connection: self._assert_epoch_fence(connection, for_write=False),
            busy_deadline_s=busy_deadline_s,
        )

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
            raise JournalIOError(f"cannot create journal path {self.path}: {exc}") from exc
        else:
            os.close(fd)

    def _connect(self, *, busy_deadline_s: float | None = None) -> sqlite3.Connection:
        started = time.monotonic()
        open_started = started
        attempts = 0
        open_failures = 0
        while True:
            attempts += 1
            self._require_existing_database()
            try:
                if self._read_only_client:
                    connection = _open_readonly_connection(
                        self.path,
                        timeout=0,
                        isolation_level=None,
                    )
                else:
                    connection = sqlite3.connect(
                        self.path.as_uri() + "?mode=rw",
                        uri=True,
                        timeout=0,
                        isolation_level=None,
                    )
            except (JournalDisappeared, JournalIOError) as exc:
                open_failures += 1
                self._raise_disappeared_or_unverified(exc)
                if self._open_retry_delay(open_started, open_failures):
                    continue
                raise self._open_io_failure(open_started, open_failures, exc) from exc
            except sqlite3.DatabaseError as exc:
                # Busy is stage- and client-agnostic: a read-write client can
                # hit it here at connect (WAL shared-memory recovery/checkpoint
                # contention) exactly as the pragma stage below, and escaping
                # raw would bypass every caller's JournalUnavailable handling —
                # including the write paths that document busy as RETRYABLE.
                if _is_busy(exc):
                    if self._retry_delay(started, deadline_s=busy_deadline_s):
                        continue
                    raise JournalBusy(
                        f"journal connection remained busy after {attempts} attempts "
                        f"within {self.retry_budget_s:.3f}s: {self.path}"
                    ) from exc
                if self._read_only_client and _is_corruption_error(exc):
                    self._raise_integrity_failure(f"journal reader parse failed: {exc}")
                if _is_cantopen(exc):
                    open_failures += 1
                    self._raise_disappeared_or_unverified(exc)
                    if self._open_retry_delay(open_started, open_failures):
                        continue
                    raise self._open_io_failure(open_started, open_failures, exc) from exc
                if self._read_only_client:
                    raise JournalIOError(
                        f"journal readonly probe unavailable/unreadable for {self.path}: "
                        f"{exc}; failing closed"
                    ) from exc
                raise
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 0")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA synchronous = FULL")
                self._require_existing_database()
                return connection
            except sqlite3.OperationalError as exc:
                connection.close()
                if self._read_only_client and _is_corruption_error(exc):
                    self._raise_integrity_failure(f"journal reader parse failed: {exc}")
                if not _is_busy(exc):
                    if _is_cantopen(exc):
                        open_failures += 1
                        self._raise_disappeared_or_unverified(exc)
                        if self._open_retry_delay(open_started, open_failures):
                            continue
                        raise self._open_io_failure(
                            open_started, open_failures, exc
                        ) from exc
                    self._raise_disappeared_or_unverified(exc)
                    if self._read_only_client:
                        raise JournalIOError(
                            f"journal readonly probe unavailable/unreadable for {self.path}: "
                            f"{exc}; failing closed"
                        ) from exc
                    raise
                if not self._retry_delay(started, deadline_s=busy_deadline_s):
                    raise JournalBusy(
                        f"journal connection remained busy after {attempts} attempts "
                        f"within {self.retry_budget_s:.3f}s: {self.path}"
                    ) from exc

    def _startup_integrity_check(self, *, busy_deadline_s: float | None = None) -> None:
        started = time.monotonic()
        deadline = (
            started + self.retry_budget_s
            if busy_deadline_s is None
            else busy_deadline_s
        )
        attempts = 0
        while True:
            attempts += 1
            try:
                with contextlib.closing(
                    self._connect(busy_deadline_s=deadline)
                ) as connection:
                    rows = [
                        str(row[0])
                        for row in connection.execute("PRAGMA integrity_check")
                    ]
            except sqlite3.DatabaseError as exc:
                if _is_busy(exc) and self._retry_delay(started, deadline_s=deadline):
                    continue
                if _is_busy(exc):
                    raise JournalBusy(
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

    def _bootstrap_schema(
        self, *, created_here: bool, busy_deadline_s: float | None = None
    ) -> None:
        started = time.monotonic()
        deadline = (
            started + self.retry_budget_s
            if busy_deadline_s is None
            else busy_deadline_s
        )
        attempts = 0
        while True:
            attempts += 1
            try:
                connection = self._connect(busy_deadline_s=deadline)
            except JournalBusy:
                raise
            except (JournalDisappeared, JournalIOError) as exc:
                raise type(exc)(
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
                    if self._retry_delay(started, deadline_s=deadline):
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
                self._install_p4_schema(connection)
                connection.commit()
                connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if connection.in_transaction:
                    connection.rollback()
                if not _is_busy(exc) or not self._retry_delay(
                    started, deadline_s=deadline
                ):
                    if _is_busy(exc):
                        raise JournalBusy(
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
            # The epoch fence is the first authority consulted after the epochs
            # table is readable.  A client refused by epoch must never reach
            # shape validation: schema deltas are expected across epochs and
            # must not be misreported to an old client as journal corruption.
            self._assert_epoch_fence(connection, for_write=False)
            missing_before, malformed_before = self._current_schema_issues(connection)
            outbox_columns = tuple(
                str(column[1])
                for column in connection.execute("PRAGMA table_info(terminal_outbox)")
            )
            legacy_outbox = outbox_columns == _LEGACY_TERMINAL_OUTBOX_COLUMNS
            attempt_columns = tuple(
                str(column[1])
                for column in connection.execute("PRAGMA table_info(dispatch_attempts)")
            )
            legacy_attempt_seat = (
                attempt_columns == _EPOCH_FIVE_DISPATCH_ATTEMPTS_COLUMNS
            )
            nonrepairable_malformed = sorted(
                set(malformed_before)
                - ({"terminal_outbox"} if legacy_outbox else set())
                - ({"dispatch_attempts"} if legacy_attempt_seat else set())
            )
            if nonrepairable_malformed:
                self._raise_integrity_failure(
                    "epoch-6 journal has structurally invalid tables: "
                    + ", ".join(nonrepairable_malformed)
                )
            repairable_shape = (
                bool(missing_before) or legacy_outbox or legacy_attempt_seat
            )
            if repairable_shape and not self.allow_migration:
                self._raise_migration_required(stored)
            retry_columns_migrated = self._install_outbox_retry_columns(connection)
            seat_columns_migrated = self._install_attempt_seat_columns(connection)
            missing, malformed = self._current_schema_issues(connection)
            if malformed:
                self._raise_integrity_failure(
                    "epoch-6 journal has structurally invalid tables: "
                    + ", ".join(malformed)
                )
            repaired = (
                retry_columns_migrated or seat_columns_migrated or bool(missing)
            )
            if repaired:
                # In-progress P3 builds could stamp epoch 3 before every final
                # P3 table existed.  The installer is idempotent and is the
                # only supported repair for that incomplete-but-valid state.
                self._install_p3_schema(connection)
                self._install_p4_schema(connection)
                missing, malformed = self._current_schema_issues(connection)
            if missing or malformed:
                details = []
                if missing:
                    details.append("missing tables: " + ", ".join(missing))
                if malformed:
                    details.append("structurally invalid tables: " + ", ".join(malformed))
                self._raise_integrity_failure(
                    "epoch-6 journal has incomplete schema after the idempotent installer; "
                    + "; ".join(details)
                )
            return repaired
        if stored not in {
            (1, 1, 1, 1, 1),
            (2, 2, 2, 2, 2),
            (3, 3, 3, 3, 3),
            (4, 4, 4, 4, 4),
            (5, 5, 5, 5, 5),
        }:
            return False
        if not self.allow_migration:
            self._raise_migration_required(stored)
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
        self._install_p4_schema(connection)
        self._install_attempt_owner_columns(connection)
        self._install_attempt_seat_columns(connection)
        self._install_outbox_retry_columns(connection)
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
        if stored in {(1, 1, 1, 1, 1), (2, 2, 2, 2, 2)}:
            connection.execute(
                """
                INSERT OR IGNORE INTO journal_migrations (migration_id, applied_at)
                VALUES ('p3-leases-listener-v3', ?)
                """,
                (now,),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO journal_migrations (migration_id, applied_at)
            VALUES ('p4-doorbell-cursor-cas-v4', ?)
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO journal_migrations (migration_id, applied_at)
            VALUES ('dispatch-attempt-owner-v1', ?)
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO journal_migrations (migration_id, applied_at)
            VALUES ('dispatch-attempt-seat-attribution-v1', ?)
            """,
            (now,),
        )
        return True

    def _raise_migration_required(self, stored: tuple[int, ...]) -> None:
        command = shlex.join(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--project-root",
                str(self.project_root),
                "migrate",
            ]
        )
        raise JournalUpgradeRequired(
            f"UPGRADE_REQUIRED: run {command}; journal migration is disabled "
            "for ordinary opens; "
            f"journal epochs={stored}, client epochs="
            f"{(CURRENT_SCHEMA_EPOCH, CURRENT_PROTOCOL_EPOCH, CURRENT_REGISTRY_EPOCH, CURRENT_READER_EPOCH, CURRENT_WRITER_EPOCH)}"
        )

    @staticmethod
    def _install_attempt_owner_columns(connection: sqlite3.Connection) -> bool:
        """Install nullable attempt ownership for the epoch-5 schema."""
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "dispatch_attempts" not in tables:
            return False
        columns = tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(dispatch_attempts)")
        )
        migrated = False
        if columns == _LEGACY_DISPATCH_ATTEMPTS_COLUMNS:
            connection.execute(
                "ALTER TABLE dispatch_attempts ADD COLUMN owner_controller_label TEXT NULL"
            )
            columns += ("owner_controller_label",)
            migrated = True
        if columns == _LEGACY_DISPATCH_ATTEMPTS_COLUMNS + ("owner_controller_label",):
            connection.execute(
                "ALTER TABLE dispatch_attempts ADD COLUMN owner_session_digest TEXT NULL"
            )
            migrated = True
        return migrated

    @staticmethod
    def _install_attempt_seat_columns(connection: sqlite3.Connection) -> bool:
        """Install nullable effective-seat attribution for the epoch-6 schema."""
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "dispatch_attempts" not in tables:
            return False
        columns = tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(dispatch_attempts)")
        )
        migrated = False
        if columns == _EPOCH_FIVE_DISPATCH_ATTEMPTS_COLUMNS:
            connection.execute(
                "ALTER TABLE dispatch_attempts ADD COLUMN effective_account TEXT NULL"
            )
            columns += ("effective_account",)
            migrated = True
        if columns == _EPOCH_FIVE_DISPATCH_ATTEMPTS_COLUMNS + ("effective_account",):
            connection.execute(
                "ALTER TABLE dispatch_attempts ADD COLUMN engine TEXT NULL"
            )
            migrated = True
        return migrated

    @staticmethod
    def _install_outbox_retry_columns(connection: sqlite3.Connection) -> bool:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "terminal_outbox" not in tables:
            return False
        columns = tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(terminal_outbox)")
        )
        if columns != _LEGACY_TERMINAL_OUTBOX_COLUMNS:
            return False
        connection.execute("ALTER TABLE terminal_outbox ADD COLUMN projection_retry_at TEXT")
        connection.execute(
            "ALTER TABLE terminal_outbox ADD COLUMN projection_quarantined_at TEXT"
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO journal_migrations (migration_id, applied_at)
            VALUES ('terminal-outbox-retry-quarantine-v1', ?)
            """,
            (utc_now(),),
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
                owner_controller_label TEXT NULL,
                owner_session_digest TEXT NULL,
                effective_account TEXT NULL,
                engine TEXT NULL,
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
                projection_retry_at TEXT,
                projection_quarantined_at TEXT,
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

    @staticmethod
    def _install_p4_schema(connection: sqlite3.Connection) -> None:
        """Delete the retired listener batch-token signing surface."""
        connection.execute(
            """CREATE TABLE IF NOT EXISTS system_attention_items (
                item_id TEXT PRIMARY KEY,
                project_root TEXT NOT NULL,
                item_type TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('OPEN', 'RESOLVED')),
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                wake_class TEXT NOT NULL CHECK (wake_class = 'waking'),
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS system_attention_items_open_idx
               ON system_attention_items (project_root, state, created_at, item_id)"""
        )
        connection.execute("DROP TABLE IF EXISTS journal_secrets")

    def _retry_delay(
        self,
        started: float,
        *,
        deadline_s: float | None = None,
    ) -> bool:
        deadline = (
            started + self.retry_budget_s
            if deadline_s is None
            else deadline_s
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        delay = min(random.uniform(self.jitter_min_s, self.jitter_max_s), remaining)
        if delay > 0:
            time.sleep(delay)
        return time.monotonic() < deadline

    def _raise_disappeared_or_unverified(self, cause: BaseException) -> None:
        """Reclassify a low-level open/read failure by path presence.

        Present: return, so the caller keeps its own retry/IO-failure path.
        Genuinely absent: JournalDisappeared. Unverifiable (unreadable):
        JournalIOError — an unreadable path is not absence, and the IO
        verdict is what the caller's budget-exhausted path would raise anyway.
        """
        presence = _lstat_presence(self.path)
        if presence == "absent":
            raise JournalDisappeared(
                f"journal database vanished without creating a replacement: {self.path}"
            ) from cause
        if presence == "unknown":
            raise JournalIOError(
                f"journal path is unreadable after a failure, so disappearance is "
                f"unverified: {self.path}"
            ) from cause

    def _open_retry_delay(self, started: float, failures: int) -> bool:
        remaining = self.open_retry_budget_s - (time.monotonic() - started)
        if remaining <= 0:
            return False
        exponent = min(max(0, failures - 1), 16)
        ceiling = min(
            JOURNAL_OPEN_RETRY_INITIAL_S * (2**exponent),
            JOURNAL_OPEN_RETRY_MAX_S,
        )
        # Full jitter avoids phase-locking the stream, backup, and watchdog.
        delay = min(random.uniform(ceiling / 2, ceiling), remaining)
        if delay > 0:
            time.sleep(delay)
        # Permit one final measured attempt at the deadline; the next failure
        # observes the exhausted bound and returns a durable IO verdict.
        return True

    def _open_io_failure(
        self,
        started: float,
        failures: int,
        cause: BaseException,
    ) -> JournalIOError:
        elapsed = time.monotonic() - started
        return JournalIOError(
            f"journal IO open failure after {failures} attempts within "
            f"{elapsed:.3f}s (budget {self.open_retry_budget_s:.3f}s); "
            f"journal path is still present: {self.path}: {cause}"
        )

    def _assert_identity(self, connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute(
                "SELECT value FROM journal_meta WHERE key = ?",
                (JOURNAL_IDENTITY_KEY,),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            if _is_busy(exc):
                raise
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
            if _is_busy(exc):
                raise
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
            if _is_busy(exc):
                raise
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
                _upgrade_required_resume(
                    "journal epoch fence refused client: " + "; ".join(mismatches)
                )
            )
        return stored

    def _read_with_retry(
        self,
        operation: str,
        action: Callable[[sqlite3.Connection], T],
        *,
        busy_deadline_s: float | None = None,
    ) -> T:
        """Run every SQL read stage within one bounded busy-classification path."""
        started = time.monotonic()
        deadline = (
            started + self.retry_budget_s
            if busy_deadline_s is None
            else busy_deadline_s
        )
        attempts = 0
        while True:
            attempts += 1
            try:
                with contextlib.closing(
                    self._connect(busy_deadline_s=deadline)
                ) as connection:
                    return action(connection)
            except JournalBusy:
                # _connect already spent this operation's retry budget. Starting
                # another full window here would silently double the contract.
                raise
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    self._raise_disappeared_or_unverified(exc)
                    raise
                if self._retry_delay(started, deadline_s=deadline):
                    continue
                raise JournalBusy(
                    f"{operation} remained busy after {attempts} attempts "
                    f"within {self.retry_budget_s:.3f}s: {self.path}"
                ) from exc

    def epochs(self) -> JournalEpochs:
        return self._read_with_retry(
            "journal epoch read",
            lambda connection: self._assert_epoch_fence(connection, for_write=False),
        )

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
        # A retry must replay the same bindings. Materialize one-shot iterables
        # before entering the retried closure so a busy first attempt cannot
        # exhaust a generator and change the query on the next connection.
        prepared_parameters = tuple(parameters)

        def action(connection: sqlite3.Connection) -> list[sqlite3.Row]:
            self._assert_epoch_fence(connection, for_write=False)
            return list(connection.execute(sql, prepared_parameters).fetchall())

        return self._read_with_retry("journal read query", action)

    def write(self, operations: Iterable[RowOperation]) -> WriteResult[list[RowWrite]]:
        """Run declarative row operations in one bounded immediate transaction.

        Operations are compiled before lock acquisition.  Busy or deadline
        exhaustion returns ``RETRYABLE``; an ``expected_rows`` mismatch returns
        ``CAS_LOST``.  These outcomes never alias. The progress handler bounds
        SQLite VM work; stronger CPU-latency enforcement beyond that handler is
        explicitly P2 work.
        """
        if self._read_only_client:
            raise JournalError("read-only journal client cannot write")
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
        operation_deadline = started + self.retry_budget_s
        attempts = 0
        while True:
            attempts += 1
            write_lock = goalflight_task.FileLock.try_acquire(
                journal_write_lock_path(self.path),
                deadline_s=operation_deadline,
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
                connection = self._connect(busy_deadline_s=operation_deadline)
            except JournalBusy as exc:
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
                deadline = min(
                    operation_deadline,
                    transaction_started + self.transaction_budget_s,
                )
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() >= deadline else 0,
                    1000,
                )
                if transaction_started >= deadline:
                    raise TimeoutError("journal operation deadline reached before transaction")
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
        operation_deadline = started + self.retry_budget_s
        attempts = 0
        while True:
            attempts += 1
            write_lock = goalflight_task.FileLock.try_acquire(
                journal_write_lock_path(self.path),
                deadline_s=operation_deadline,
            )
            if write_lock is None:
                return WriteResult(
                    WriteDisposition.RETRYABLE,
                    attempts=attempts,
                    reason=f"journal write lock timeout within {self.retry_budget_s:.3f}s",
                )
            try:
                connection = self._connect(busy_deadline_s=operation_deadline)
            except JournalBusy as exc:
                write_lock.release()
                return WriteResult(
                    WriteDisposition.RETRYABLE,
                    attempts=attempts,
                    reason=str(exc),
                )
            except (JournalDisappeared, JournalIOError):
                # Disappearance and path/open I/O failures are not contention.
                # Preserve their typed fatal contract instead of flattening
                # them into a RETRYABLE result that a caller may treat as busy.
                write_lock.release()
                raise
            except BaseException:
                write_lock.release()
                raise
            try:
                transaction_started = time.monotonic()
                deadline = min(
                    operation_deadline,
                    transaction_started + self.transaction_budget_s,
                )
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() >= deadline else 0,
                    1000,
                )
                if transaction_started >= deadline:
                    raise TimeoutError("journal operation deadline reached before transaction")
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
              AND e.event_type != 'controller_attention'
              AND e.projected_at IS NOT NULL
              AND e.withdrawn_at IS NULL
              AND e.stream_seq > COALESCE(c.position, 0)
            LIMIT 1
            """,
            (label, project_root, label),
        ).fetchone()
        return waking is not None

    @classmethod
    def _collapse_open_attention(
        cls,
        connection: sqlite3.Connection,
        *,
        project_root: str,
        label: str,
    ) -> dict[str, object] | None:
        """Keep one current orphan-work item; resolve historical generation noise."""
        rows = connection.execute(
            """
            SELECT * FROM attention_items
            WHERE project_root = ? AND source_label = ?
              AND item_type = 'orphaned_controller_work' AND state = 'OPEN'
            ORDER BY source_generation DESC, created_at DESC, item_id DESC
            """,
            (project_root, label),
        ).fetchall()
        if not rows:
            return None
        survivor = rows[0]
        now = utc_now()
        for stale in rows[1:]:
            connection.execute(
                """
                UPDATE attention_items
                SET state = 'RESOLVED', resolved_at = ?
                WHERE item_id = ? AND state = 'OPEN'
                """,
                (now, str(stale["item_id"])),
            )
            connection.execute(
                """
                UPDATE delivery_events
                SET withdrawn_at = ?
                WHERE project_root = ? AND origin_node = 'journal'
                  AND event_uuid = ? AND event_type = 'controller_attention'
                  AND withdrawn_at IS NULL
                """,
                (now, project_root, str(stale["item_id"])),
            )
        try:
            return json.loads(str(survivor["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"item_id": str(survivor["item_id"])}

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
        inherited = cls._collapse_open_attention(
            connection,
            project_root=project_root,
            label=label,
        )
        if inherited is not None:
            return inherited
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
            "text": (
                f"controller lease {label} generation {generation} needs "
                f"reassignment · next: {_RESUME_SKILL_COMMAND}"
            ),
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
                WHERE project_root = ? AND recipient_label = ? AND stream_id = 'attention'
                """,
                (project_root, label),
            ).fetchone()[0]
        )
        # Address the source controller and keep the doorbell quiet. A
        # waking `*` broadcast would pop a sibling's live slot; care
        # already ignores this event type, and operator surfaces read
        # attention_items rather than this wake class.
        connection.execute(
            """
            INSERT INTO delivery_events (
                project_root, recipient_label, origin_node, event_uuid,
                stream_id, stream_seq, carrier_path, event_type,
                wake_class, created_at, projected_at
            ) VALUES (?, ?, 'journal', ?, 'attention', ?, ?,
                      'controller_attention', 'quiet', ?, ?)
            """,
            (
                project_root,
                label,
                item_id,
                next_seq,
                _synthetic_journal_carrier(_JOURNAL_RESUME_CARRIER_KIND, item_id),
                now,
                now,
            ),
        )
        cls._invalidate_delivery_cursor_snapshots(
            connection,
            project_root=project_root,
            recipient_label=label,
            updated_at=now,
        )
        return payload

    def care_work_exists(self, label: str) -> bool:
        """True when this project still has live attempts or unread waking mail."""
        resolved_label = self._identity_token(label, label="controller label")

        def action(connection: sqlite3.Connection) -> bool:
            self._assert_epoch_fence(connection, for_write=False)
            return self._care_work_exists(
                connection,
                project_root=str(self.project_root),
                label=resolved_label,
            )

        return self._read_with_retry("journal care-work read", action)

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

    def _expire_active_lease(
        self,
        connection: sqlite3.Connection,
        *,
        label: str,
        generation: int,
        nonce: str,
        expected_renewed_at: str,
        ended_at: str,
    ) -> sqlite3.Row | None:
        cursor = connection.execute(
            """
            UPDATE controller_leases
            SET state = 'EXPIRED', ended_at = ?, ended_reason = 'holder-dead'
            WHERE project_root = ? AND label = ? AND generation = ?
              AND nonce = ? AND renewed_at = ? AND state = 'ACTIVE'
            """,
            (
                ended_at,
                str(self.project_root),
                label,
                generation,
                nonce,
                expected_renewed_at,
            ),
        )
        if cursor.rowcount == 0:
            return None
        connection.execute(
            """
            UPDATE listener_coverage
            SET state = 'EXITED', exited_at = ?, exit_reason = 'orphaned'
            WHERE project_root = ? AND label = ? AND lease_generation = ?
              AND state = 'ARMED'
            """,
            (ended_at, str(self.project_root), label, generation),
        )
        # ``horizon`` is the shipped schema's journal-side bucket; the
        # precise kernel cause is carried in ``reason``.
        self._materialize_attention(
            connection,
            project_root=str(self.project_root),
            label=label,
            generation=generation,
            trigger_side="horizon",
            reason="holder-dead",
        )
        return connection.execute(
            """SELECT * FROM controller_leases
               WHERE project_root = ? AND label = ? AND generation = ?""",
            (str(self.project_root), label, generation),
        ).fetchone()

    # Mechanism choice: sweep on claim/renew instead of mutating lease reads.
    # Claims already need a writable journal, active projects sweep naturally,
    # and exact generation+nonce+renewal CAS leaves concurrent claims untouched.
    def expire_stale_leases(self) -> WriteResult[list[dict[str, object]]]:
        proven_dead: list[tuple[str, int, str, str]] = []
        for row in self.lease_records():
            label = str(row["label"])
            nonce = str(row["nonce"])
            try:
                alive = goalflight_wake.lease_holder_alive(
                    self.project_root,
                    controller_label=label,
                    lease_nonce=nonce,
                    prune_dead=False,
                )
            except (OSError, RuntimeError, ValueError):
                # Probe failure is indeterminate. Keeping routing state is the
                # recoverable, fail-closed outcome.
                continue
            if alive is False:
                proven_dead.append(
                    (label, int(row["generation"]), nonce, str(row["renewed_at"]))
                )

        if not proven_dead:
            return WriteResult(WriteDisposition.COMMITTED, [], attempts=0)

        def action(connection: sqlite3.Connection) -> list[dict[str, object]]:
            ended_at = utc_now()
            expired: list[dict[str, object]] = []
            for label, generation, nonce, renewed_at in proven_dead:
                row = self._expire_active_lease(
                    connection,
                    label=label,
                    generation=generation,
                    nonce=nonce,
                    expected_renewed_at=renewed_at,
                    ended_at=ended_at,
                )
                if row is not None:
                    expired.append(dict(row))
            return expired

        return self._domain_write(action)

    def claim_or_renew_lease(
        self,
        label: str,
        *,
        principal: Mapping[str, object],
        nonce: str | None = None,
        horizon_s: float = DEFAULT_LEASE_HORIZON_S,
        takeover: bool = False,
        incumbent_liveness: LeaseLivenessEvidence | None = None,
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
        expiry = self.expire_stale_leases()
        if not expiry.committed:
            return WriteResult(
                expiry.disposition,
                attempts=expiry.attempts,
                reason=f"lease expiry sweep failed before claim: {expiry.reason}",
            )

        def action(connection: sqlite3.Connection) -> LeaseIdentity:
            now = utc_now()
            deadline = _utc_after(horizon_s)
            self._collapse_open_attention(
                connection,
                project_root=project_root,
                label=resolved_label,
            )
            replacing_generation = False
            replaced_nonce: str | None = None
            active = connection.execute(
                """
                SELECT * FROM controller_leases
                WHERE project_root = ? AND label = ? AND state = 'ACTIVE'
                """,
                (project_root, resolved_label),
            ).fetchone()
            if active is not None:
                same_principal = self._principal_matches(active, principal)
                # Process identity (pid + start token), or the stable principal_id
                # fallback, identifies the incumbent.  The nonce is a lease
                # capability returned to that principal, not a second identity
                # requirement: claim-or-renew must also work when a fresh helper
                # can re-measure its controller but cannot carry the nonce.
                if same_principal:
                    renewed_at = _next_lease_renewed_at(active["renewed_at"])
                    connection.execute(
                        """
                        UPDATE controller_leases
                        SET renewed_at = ?, renew_deadline_at = ?, principal_json = ?,
                            pid = ?, start_token = ?
                        WHERE project_root = ? AND label = ? AND generation = ?
                          AND nonce = ? AND state = 'ACTIVE'
                        """,
                        (
                            renewed_at,
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
                incumbent_proven_dead = bool(
                    incumbent_liveness is not None
                    and incumbent_liveness.alive is False
                    and incumbent_liveness.generation == int(active["generation"])
                    and incumbent_liveness.nonce == str(active["nonce"])
                )
                if incumbent_proven_dead:
                    expired = self._expire_active_lease(
                        connection,
                        label=resolved_label,
                        generation=int(active["generation"]),
                        nonce=str(active["nonce"]),
                        expected_renewed_at=str(active["renewed_at"]),
                        ended_at=now,
                    )
                    assert expired is not None
                    replaced_nonce = str(active["nonce"])
                    active = None
                    replacing_generation = True
                if not takeover:
                    if active is not None:
                        raise CASMismatch(
                            f"label in use: {resolved_label}; rerun goalflight_dispatch.py "
                            "with --takeover"
                        )
                if active is not None:
                    replacing_generation = True
                    replaced_nonce = str(active["nonce"])
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
            reuse_ended_capability = bool(
                replacing_generation
                and supplied_nonce is not None
                and supplied_nonce == replaced_nonce
            )
            allocated_nonce = (
                uuid.uuid4().hex if reuse_ended_capability else supplied_nonce or uuid.uuid4().hex
            )
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
            # A new generation has not consumed anything, but unread mail
            # already in the journal must stay pending until a drain acts.
            connection.execute(
                """
                UPDATE controller_cursors
                SET backlog_pending = ?
                WHERE project_root = ? AND label = ?
                """,
                (
                    1
                    if self._label_has_unread_delivery(
                        connection,
                        project_root=project_root,
                        label=resolved_label,
                    )
                    else 0,
                    project_root,
                    resolved_label,
                ),
            )
            row = connection.execute(
                """SELECT * FROM controller_leases
                   WHERE project_root = ? AND label = ? AND generation = ?""",
                (project_root, resolved_label, generation),
            ).fetchone()
            assert row is not None
            return self._lease_identity(row)

        return self._domain_write(action)

    # Removed expire_stale_leases: its silent no-op hid that t-297 needs explicit holder-death expiry.

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
            rehomed = self._rehome_retired_delivery_events(
                connection,
                project_root=project_root,
                retired_label=resolved_label,
                retired_at=now,
            )
            if rehomed:
                self._invalidate_delivery_cursor_snapshots(
                    connection,
                    project_root=project_root,
                    recipient_label="*",
                    updated_at=now,
                    exclude_label=resolved_label,
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

    @staticmethod
    def _rehome_retired_delivery_events(
        connection: sqlite3.Connection,
        *,
        project_root: str,
        retired_label: str,
        retired_at: str,
    ) -> int:
        """Move a retiring controller's unprocessed assignments to ``*``.

        This runs in the lease-retirement transaction.  Rows already at or
        below the retiring controller's stream cursor stay exact so retirement
        cannot replay processed mail.  An unprojected row is always moved: it
        may have committed immediately before retirement and has never been
        visible to the retiring controller.
        """
        candidate_sql = """
            SELECT exact.rowid
            FROM delivery_events AS exact
            LEFT JOIN controller_stream_cursors AS cursor
              ON cursor.project_root = exact.project_root
             AND cursor.label = ?
             AND cursor.stream_id = exact.stream_id
            WHERE exact.project_root = ?
              AND exact.recipient_label = ?
              AND exact.withdrawn_at IS NULL
              AND (
                    exact.projected_at IS NULL
                    OR exact.stream_seq > COALESCE(cursor.position, 0)
              )
        """
        parameters = (retired_label, project_root, retired_label)

        # A wildcard may already carry the same logical event or stream
        # position.  Preserve that durable copy and withdraw the redundant
        # exact row before changing recipient_label across the live unique
        # indexes.
        withdrawn = connection.execute(
            """
            UPDATE delivery_events
            SET withdrawn_at = ?
            WHERE rowid IN (
                SELECT candidate.rowid
                FROM ("""
            + candidate_sql
            + """) AS candidate
                JOIN delivery_events AS exact ON exact.rowid = candidate.rowid
                JOIN delivery_events AS wildcard
                  ON wildcard.project_root = exact.project_root
                 AND wildcard.recipient_label = '*'
                 AND wildcard.withdrawn_at IS NULL
                 AND (
                      (
                          wildcard.origin_node = exact.origin_node
                          AND wildcard.event_uuid = exact.event_uuid
                      )
                      OR (
                          wildcard.stream_id = exact.stream_id
                          AND wildcard.stream_seq = exact.stream_seq
                      )
                 )
            )
            """,
            (retired_at, *parameters),
        )
        moved = connection.execute(
            """
            UPDATE delivery_events
            SET recipient_label = '*'
            WHERE rowid IN ("""
            + candidate_sql
            + ")",
            parameters,
        )
        return int(withdrawn.rowcount) + int(moved.rowcount)

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
        fallback_to_wildcard_if_inactive: bool = False,
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
            effective_recipient = recipient
            if fallback_to_wildcard_if_inactive and recipient != "*":
                active = connection.execute(
                    """SELECT 1 FROM controller_leases
                       WHERE project_root = ? AND label = ? AND state = 'ACTIVE'
                       LIMIT 1""",
                    (project_root, recipient),
                ).fetchone()
                if active is None:
                    effective_recipient = "*"
            existing = connection.execute(
                """
                SELECT * FROM delivery_events
                WHERE project_root = ? AND recipient_label = ?
                  AND origin_node = ? AND event_uuid = ?
                """,
                (project_root, effective_recipient, origin, event_id),
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
                    effective_recipient,
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
            self._invalidate_delivery_cursor_snapshots(
                connection,
                project_root=project_root,
                recipient_label=effective_recipient,
                updated_at=utc_now(),
            )
            return {
                "project_root": project_root,
                "recipient_label": effective_recipient,
                "origin_node": origin,
                "event_uuid": event_id,
                **expected,
                "created_at": created_at,
            }

        return self._domain_write(action)

    @staticmethod
    def _label_has_unread_delivery(
        connection: sqlite3.Connection,
        *,
        project_root: str,
        label: str,
    ) -> bool:
        """True when peek-visible projected mail sits beyond this label's stream cursor."""
        row = connection.execute(
            """
            SELECT 1
            FROM delivery_events AS e
            LEFT JOIN controller_stream_cursors AS c
              ON c.project_root = e.project_root
             AND c.label = ?
             AND c.stream_id = e.stream_id
            WHERE e.project_root = ?
              AND e.recipient_label IN (?, '*')
              AND e.projected_at IS NOT NULL
              AND e.withdrawn_at IS NULL
              AND e.stream_seq > COALESCE(c.position, 0)
            LIMIT 1
            """,
            (label, project_root, label),
        ).fetchone()
        return row is not None

    @staticmethod
    def _cursor_labels_for_delivery_invalidation(
        connection: sqlite3.Connection,
        *,
        project_root: str,
        recipient_label: str,
        exclude_label: str | None = None,
    ) -> tuple[str, ...]:
        if recipient_label == "*":
            sql = "SELECT label FROM controller_cursors WHERE project_root = ?"
            parameters: list[object] = [project_root]
            if exclude_label is not None:
                sql += " AND label != ?"
                parameters.append(exclude_label)
            return tuple(str(row[0]) for row in connection.execute(sql, parameters))
        if exclude_label is not None and recipient_label == exclude_label:
            return ()
        exists = connection.execute(
            """
            SELECT 1 FROM controller_cursors
            WHERE project_root = ? AND label = ?
            """,
            (project_root, recipient_label),
        ).fetchone()
        return (recipient_label,) if exists is not None else ()

    @classmethod
    def _invalidate_delivery_cursor_snapshots(
        cls,
        connection: sqlite3.Connection,
        *,
        project_root: str,
        recipient_label: str,
        updated_at: str,
        exclude_label: str | None = None,
    ) -> int:
        """Bump snapshot tokens whose pending view can change for a delivery row.

        This is not consumption. Stream positions and ``advanced_by`` stay
        untouched; ``backlog_pending`` is refreshed from unread projected
        mail so a produce-path write cannot look like a drain.
        """
        labels = cls._cursor_labels_for_delivery_invalidation(
            connection,
            project_root=project_root,
            recipient_label=recipient_label,
            exclude_label=exclude_label,
        )
        updated = 0
        for label in labels:
            pending = (
                1
                if cls._label_has_unread_delivery(
                    connection,
                    project_root=project_root,
                    label=label,
                )
                else 0
            )
            changed = connection.execute(
                """
                UPDATE controller_cursors
                SET cursor_version = cursor_version + 1,
                    updated_at = ?,
                    backlog_pending = ?
                WHERE project_root = ? AND label = ?
                """,
                (updated_at, pending, project_root, label),
            )
            updated += int(changed.rowcount)
        return updated

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
            effective_recipient = recipient
            row = connection.execute(
                """
                SELECT * FROM delivery_events
                WHERE project_root = ? AND recipient_label = ?
                  AND origin_node = ? AND event_uuid = ?
                """,
                (project_root, recipient, origin, event_id),
            ).fetchone()
            if row is None and recipient != "*":
                # Retirement can commit after exact assignment and before this
                # projection step.  Follow the transactionally re-homed row so
                # the carrier append still makes the durable wildcard visible.
                row = connection.execute(
                    """
                    SELECT * FROM delivery_events
                    WHERE project_root = ? AND recipient_label = '*'
                      AND origin_node = ? AND event_uuid = ?
                      AND withdrawn_at IS NULL
                    """,
                    (project_root, origin, event_id),
                ).fetchone()
                if row is not None:
                    effective_recipient = "*"
            if row is None:
                raise CASMismatch("delivery projection lost: assignment row is absent")
            projected_at = str(row["projected_at"] or utc_now())
            updated = connection.execute(
                """
                UPDATE delivery_events SET projected_at = ?
                WHERE project_root = ? AND recipient_label = ?
                  AND origin_node = ? AND event_uuid = ? AND projected_at IS NULL
                """,
                (projected_at, project_root, effective_recipient, origin, event_id),
            )
            if updated.rowcount == 1:
                # Visibility is a second inbox-view mutation after the durable
                # assignment insert.  Invalidating both closes the prepare /
                # projection window for commands emitted concurrently.
                self._invalidate_delivery_cursor_snapshots(
                    connection,
                    project_root=project_root,
                    recipient_label=effective_recipient,
                    updated_at=projected_at,
                )
            cursor_rows = connection.execute(
                """
                SELECT c.label, c.registry_generation, c.cursor_version,
                       c.backlog_pending, c.updated_at
                FROM controller_leases AS l
                JOIN controller_cursors AS c
                  ON c.project_root = l.project_root
                 AND c.label = l.label
                 AND c.registry_generation = l.generation
                WHERE l.project_root = ? AND l.state = 'ACTIVE'
                  AND (? = '*' OR l.label = ?)
                ORDER BY c.label
                """,
                (project_root, effective_recipient, effective_recipient),
            ).fetchall()
            result = dict(row)
            result["projected_at"] = projected_at
            # Return the cursor state observed by the same transaction that
            # projected the event. Post reporting must not re-query after the
            # commit and accidentally describe a later delivery or drain.
            result["recipient_cursors"] = [dict(cursor) for cursor in cursor_rows]
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
            withdrawn = connection.execute(
                """
                UPDATE delivery_events SET withdrawn_at = ?
                WHERE project_root = ? AND recipient_label = ?
                  AND origin_node = ? AND event_uuid = ? AND withdrawn_at IS NULL
                """,
                (withdrawn_at, project_root, recipient, origin, event_id),
            )
            if withdrawn.rowcount:
                self._invalidate_delivery_cursor_snapshots(
                    connection,
                    project_root=project_root,
                    recipient_label=recipient,
                    updated_at=withdrawn_at,
                )
            result = dict(row)
            result["withdrawn_at"] = withdrawn_at
            return result

        return self._domain_write(action)

    @staticmethod
    def _cursor_stream_snapshot(
        connection: sqlite3.Connection,
        *,
        project_root: str,
        recipient_label: str,
        stream_id: str,
        requested_position: int,
        waking_only: bool = False,
    ) -> tuple[str, bool]:
        """Hash the recipient-visible live range through one stream position."""
        sql = """
            SELECT e.rowid, e.projected_at
            FROM delivery_events AS e
            LEFT JOIN controller_stream_cursors AS cursor
              ON cursor.project_root = e.project_root
             AND cursor.label = ?
             AND cursor.stream_id = e.stream_id
            WHERE e.project_root = ? AND e.recipient_label IN (?, '*')
              AND e.stream_id = ? AND e.withdrawn_at IS NULL
              AND e.stream_seq > COALESCE(cursor.position, 0)
              AND e.stream_seq <= ?
        """
        if waking_only:
            sql += " AND e.wake_class = 'waking'"
        sql += " ORDER BY e.rowid"
        rows = connection.execute(
            sql,
            (
                recipient_label,
                project_root,
                recipient_label,
                stream_id,
                requested_position,
            ),
        ).fetchall()
        material = "|".join(
            f"{int(row['rowid'])}:{1 if row['projected_at'] is not None else 0}"
            for row in rows
        )
        return hashlib.sha256(material.encode("ascii")).hexdigest(), any(
            row["projected_at"] is None for row in rows
        )

    @staticmethod
    def _begin_cursor_read_snapshot(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN")

    def cursor_peek(
        self,
        label: str,
        *,
        nonce: str | None = None,
        waking_only: bool = False,
        limit: int = 1000,
    ) -> CursorPeek:
        """Return one server-side cursor snapshot without advancing it."""
        resolved_label = self._identity_token(label, label="controller label")
        resolved_nonce = (
            self._identity_token(nonce, label="lease nonce") if nonce is not None else None
        )
        if not 1 <= limit <= 10_000:
            raise ValueError("cursor peek limit must be between 1 and 10000")
        project_root = str(self.project_root)

        def action(connection: sqlite3.Connection) -> CursorPeek:
            # Items and their per-stream safety tokens must describe one read
            # snapshot.  Autocommit SELECTs could otherwise straddle a writer
            # and bless a row the caller never received.
            self._begin_cursor_read_snapshot(connection)
            self._assert_epoch_fence(connection, for_write=False)
            lease = connection.execute(
                """SELECT * FROM controller_leases
                   WHERE project_root = ? AND label = ? AND state = 'ACTIVE'""",
                (project_root, resolved_label),
            ).fetchone()
            if lease is None or (
                resolved_nonce is not None and str(lease["nonce"]) != resolved_nonce
            ):
                raise CASMismatch("listener lease generation or nonce is no longer active")
            cursor = connection.execute(
                """SELECT registry_generation, cursor_version, backlog_pending
                   FROM controller_cursors
                   WHERE project_root = ? AND label = ?""",
                (project_root, resolved_label),
            ).fetchone()
            if cursor is None or int(cursor["registry_generation"]) != int(lease["generation"]):
                raise JournalIntegrityError("active lease is missing its generation-matched cursor")
            sql = """
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
            """
            parameters: list[object] = [resolved_label, project_root, resolved_label]
            if waking_only:
                sql += " AND e.wake_class = 'waking'"
            sql += " ORDER BY e.stream_id, e.stream_seq, e.rowid LIMIT ?"
            parameters.append(limit)
            rows = connection.execute(sql, parameters).fetchall()
            positions: dict[str, int] = {}
            for row in rows:
                stream_id = str(row["stream_id"])
                positions[stream_id] = max(
                    positions.get(stream_id, 0), int(row["stream_seq"])
                )
            stream_snapshots = {
                stream_id: self._cursor_stream_snapshot(
                    connection,
                    project_root=project_root,
                    recipient_label=resolved_label,
                    stream_id=stream_id,
                    requested_position=position,
                    waking_only=waking_only,
                )[0]
                for stream_id, position in positions.items()
            }
            return CursorPeek(
                resolved_label,
                project_root,
                int(cursor["registry_generation"]),
                int(cursor["cursor_version"]),
                stream_snapshots,
                tuple(dict(row) for row in rows),
            )

        return self._read_with_retry("journal cursor peek", action)

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

    @staticmethod
    def _cursor_snapshot_version_is_admissible(
        *,
        expected_cursor_version: int,
        current_cursor_version: int,
    ) -> bool:
        """Treat an older aggregate snapshot as a hint, never a global veto."""
        return expected_cursor_version <= current_cursor_version

    @staticmethod
    def _cursor_advance_has_unseen_at_or_below(
        connection: sqlite3.Connection,
        *,
        project_root: str,
        recipient_label: str,
        stream_id: str,
        requested_position: int,
        expected_stream_snapshot: str,
    ) -> bool:
        current_snapshot, has_unprojected = Journal._cursor_stream_snapshot(
            connection,
            project_root=project_root,
            recipient_label=recipient_label,
            stream_id=stream_id,
            requested_position=requested_position,
        )
        return has_unprojected or current_snapshot != expected_stream_snapshot

    def advance_cursor(
        self,
        label: str,
        *,
        nonce: str,
        expected_cursor_version: int,
        expected_stream_snapshots: Mapping[str, str],
        advances: Mapping[str, int],
        actor: str,
    ) -> WriteResult[dict[str, object]]:
        """Advance only to stream-safe positions in the authoritative journal."""
        project_root = str(self.project_root)
        resolved_label = self._identity_token(label, label="cursor label")
        resolved_nonce = self._identity_token(nonce, label="cursor lease nonce")
        actor_value = self._identity_token(actor, label="cursor actor")
        if (
            not isinstance(expected_cursor_version, int)
            or isinstance(expected_cursor_version, bool)
            or expected_cursor_version < 0
        ):
            raise ValueError("expected cursor version must be a non-negative integer")
        if not isinstance(advances, Mapping) or not advances:
            raise ValueError("cursor advances are empty or invalid")
        normalized_advances: dict[str, int] = {}
        for stream, position in advances.items():
            resolved_stream = self._identity_token(stream, label="cursor stream")
            if not isinstance(position, int) or isinstance(position, bool) or position < 1:
                raise ValueError("cursor position is invalid")
            normalized_advances[resolved_stream] = position
        if not isinstance(expected_stream_snapshots, Mapping):
            raise ValueError("expected stream snapshots are invalid")
        normalized_snapshots: dict[str, str] = {}
        for stream, snapshot in expected_stream_snapshots.items():
            resolved_stream = self._identity_token(stream, label="cursor snapshot stream")
            snapshot_value = str(snapshot)
            if re.fullmatch(r"[0-9a-f]{64}", snapshot_value) is None:
                raise ValueError("cursor stream snapshot is invalid")
            normalized_snapshots[resolved_stream] = snapshot_value
        if normalized_snapshots.keys() != normalized_advances.keys():
            raise ValueError("cursor advances and stream snapshots must name the same streams")

        def action(connection: sqlite3.Connection) -> dict[str, object]:
            lease = connection.execute(
                """SELECT generation, nonce, state FROM controller_leases
                   WHERE project_root = ? AND label = ? AND state = 'ACTIVE'""",
                (project_root, resolved_label),
            ).fetchone()
            if (
                lease is None
                or str(lease["nonce"]) != resolved_nonce
            ):
                raise CASMismatch("cursor CAS lost: lease generation changed")
            generation = int(lease["generation"])
            cursor = connection.execute(
                """SELECT registry_generation, cursor_version
                   FROM controller_cursors
                   WHERE project_root = ? AND label = ?""",
                (project_root, resolved_label),
            ).fetchone()
            if cursor is None or int(cursor["registry_generation"]) != generation:
                raise CASMismatch("cursor CAS lost: registry generation changed")
            current_cursor_version = int(cursor["cursor_version"])
            if not self._cursor_snapshot_version_is_admissible(
                expected_cursor_version=expected_cursor_version,
                current_cursor_version=current_cursor_version,
            ):
                raise CASMismatch("cursor CAS lost: cursor version is from the future")
            current_positions: dict[str, int] = {}
            for stream, position in normalized_advances.items():
                current = connection.execute(
                    """SELECT position FROM controller_stream_cursors
                       WHERE project_root = ? AND label = ? AND stream_id = ?""",
                    (project_root, resolved_label, stream),
                ).fetchone()
                if current is not None and int(current["position"]) >= position:
                    raise CASMismatch("cursor CAS lost: position is already advanced")
                current_positions[stream] = (
                    int(current["position"]) if current is not None else 0
                )
                known = connection.execute(
                    """SELECT 1 FROM delivery_events
                       WHERE project_root = ? AND recipient_label IN (?, '*')
                         AND stream_id = ? AND stream_seq = ?
                         AND projected_at IS NOT NULL AND withdrawn_at IS NULL
                       LIMIT 1""",
                    (project_root, resolved_label, stream, position),
                ).fetchone()
                if known is None:
                    raise CASMismatch(
                        f"cursor CAS lost: server has no pending position {stream}={position}"
                    )
                if self._cursor_advance_has_unseen_at_or_below(
                    connection,
                    project_root=project_root,
                    recipient_label=resolved_label,
                    stream_id=stream,
                    requested_position=position,
                    expected_stream_snapshot=normalized_snapshots[stream],
                ):
                    raise CASMismatch(
                        f"cursor CAS lost: unseen position exists at or below {stream}={position}"
                    )
            now = utc_now()
            adopted_wildcards = 0
            for stream, position in normalized_advances.items():
                current_position = current_positions[stream]
                # A recipient can already have an exact fanout row at the same
                # stream position as a wildcard (for example when another
                # snapshotted recipient retired).  The wildcard cannot be
                # renamed across the live stream-position uniqueness boundary,
                # so the first successful processor withdraws that duplicate.
                withdrawn = connection.execute(
                    """
                    UPDATE delivery_events
                    SET withdrawn_at = ?
                    WHERE rowid IN (
                        SELECT wildcard.rowid
                        FROM delivery_events AS wildcard
                        JOIN delivery_events AS exact
                          ON exact.project_root = wildcard.project_root
                         AND exact.recipient_label = ?
                         AND (
                              (
                                  exact.origin_node = wildcard.origin_node
                                  AND exact.event_uuid = wildcard.event_uuid
                              )
                              OR (
                                  exact.stream_id = wildcard.stream_id
                                  AND exact.stream_seq = wildcard.stream_seq
                                  AND exact.withdrawn_at IS NULL
                              )
                         )
                        WHERE wildcard.project_root = ?
                          AND wildcard.recipient_label = '*'
                          AND wildcard.stream_id = ?
                          AND wildcard.stream_seq > ? AND wildcard.stream_seq <= ?
                          AND wildcard.projected_at IS NOT NULL
                          AND wildcard.withdrawn_at IS NULL
                    )
                    """,
                    (
                        now,
                        resolved_label,
                        project_root,
                        stream,
                        current_position,
                        position,
                    ),
                )
                adopted_wildcards += int(withdrawn.rowcount)
                adopted = connection.execute(
                    """
                    UPDATE delivery_events
                    SET recipient_label = ?
                    WHERE project_root = ? AND recipient_label = '*'
                      AND stream_id = ? AND stream_seq > ? AND stream_seq <= ?
                      AND projected_at IS NOT NULL AND withdrawn_at IS NULL
                    """,
                    (
                        resolved_label,
                        project_root,
                        stream,
                        current_position,
                        position,
                    ),
                )
                adopted_wildcards += int(adopted.rowcount)
            if adopted_wildcards:
                # Adoption removes rows from every other controller's wildcard
                # view.  Invalidate their emitted commands in the same
                # transaction; the advancing controller performs its ordinary
                # version increment below.
                self._invalidate_delivery_cursor_snapshots(
                    connection,
                    project_root=project_root,
                    recipient_label="*",
                    updated_at=now,
                    exclude_label=resolved_label,
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
                    (project_root, resolved_label, stream, position, now),
                )
            pending = (
                1
                if self._label_has_unread_delivery(
                    connection,
                    project_root=project_root,
                    label=resolved_label,
                )
                else 0
            )
            updated = connection.execute(
                """
                UPDATE controller_cursors
                SET cursor_version = cursor_version + 1, updated_at = ?,
                    backlog_pending = ?, advanced_at = ?, advanced_by = ?
                WHERE project_root = ? AND label = ?
                  AND registry_generation = ?
                """,
                (
                    now,
                    pending,
                    now,
                    actor_value,
                    project_root,
                    resolved_label,
                    generation,
                ),
            )
            if updated.rowcount != 1:
                raise CASMismatch(
                    "cursor CAS lost: registry generation changed"
                )
            attributed = connection.execute(
                """
                SELECT advanced_by FROM controller_cursors
                WHERE project_root = ? AND label = ?
                """,
                (project_root, resolved_label),
            ).fetchone()
            if attributed is None or not str(attributed["advanced_by"] or "").strip():
                raise JournalIntegrityError(
                    "cursor consume-path write left advanced_by NULL"
                )
            return {
                "label": resolved_label,
                "registry_generation": generation,
                "previous_cursor_version": current_cursor_version,
                "cursor_version": current_cursor_version + 1,
                "advances": normalized_advances,
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
            result = dict(row)
            result.update(state=COVERAGE_EXITED, exited_at=now, exit_reason=reason)
            return result

        return self._domain_write(action)

    def attention_items(self, *, state: str = "OPEN") -> list[dict[str, object]]:
        resolved_state = self._state_token(state, label="attention state")
        rows = self.read_all(
            """
            SELECT item_id, project_root, item_type, state, source_label,
                   source_generation, trigger_side, reason, payload_json,
                   wake_class, created_at, resolved_at
            FROM attention_items
            WHERE project_root = ? AND state = ?
            UNION ALL
            SELECT item_id, project_root, item_type, state,
                   'journal-outbox' AS source_label,
                   0 AS source_generation,
                   'projection' AS trigger_side,
                   reason, payload_json, wake_class, created_at, resolved_at
            FROM system_attention_items
            WHERE project_root = ? AND state = ?
            ORDER BY created_at, item_id
            """,
            (
                str(self.project_root),
                resolved_state,
                str(self.project_root),
                resolved_state,
            ),
        )
        return [dict(row) for row in rows]

    def record_system_attention(
        self,
        *,
        item_type: str,
        reason: str,
        dedupe_namespace: str,
        dedupe_key: str,
        payload: Mapping[str, object],
    ) -> WriteResult[dict[str, object]]:
        """Record one operator-visible system anomaly, idempotently.

        The durable surface for journal-observed faults that are not tied to a
        controller lease; the outbox projector's ``terminal_outbox_quarantined``
        item is the in-module precedent. ``dedupe_namespace``/``dedupe_key``
        pin the item id, so a repeated observation lands exactly one OPEN row
        plus one quiet attention-stream delivery event. The delivery stays
        quiet on purpose: a waking ``*`` broadcast would pop a sibling's live
        slot, and operator surfaces read ``attention_items()`` rather than the
        wake class.
        """
        item_type = self._state_token(item_type, label="attention item_type")
        reason = self._identity_token(reason, label="attention reason")
        namespace = self._identity_token(dedupe_namespace, label="dedupe_namespace")
        key = str(dedupe_key or "")
        if not key or len(key) > 512:
            raise ValueError("dedupe_key must be non-empty and bounded")
        item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"goalflight:{namespace}:{key}"))
        payload_json = self._json_object(
            {**dict(payload), "item_id": item_id, "type": item_type},
            label="system_attention_payload",
        )
        now = utc_now()

        def action(connection: sqlite3.Connection) -> dict[str, object]:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO system_attention_items (
                    item_id, project_root, item_type, state, reason,
                    payload_json, wake_class, created_at
                ) VALUES (?, ?, ?, 'OPEN', ?, ?, 'waking', ?)
                """,
                (
                    item_id,
                    str(self.project_root),
                    item_type,
                    reason,
                    payload_json,
                    now,
                ),
            )
            next_seq = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(stream_seq), 0) + 1
                    FROM delivery_events
                    WHERE project_root = ? AND recipient_label = '*'
                      AND stream_id = 'attention'
                    """,
                    (str(self.project_root),),
                ).fetchone()[0]
            )
            delivered = connection.execute(
                """
                INSERT OR IGNORE INTO delivery_events (
                    project_root, recipient_label, origin_node, event_uuid,
                    stream_id, stream_seq, carrier_path, event_type,
                    wake_class, created_at, projected_at
                ) VALUES (?, '*', 'journal', ?, 'attention', ?, ?,
                          'controller_attention', 'quiet', ?, ?)
                """,
                (
                    str(self.project_root),
                    item_id,
                    next_seq,
                    f"journal:{namespace}:{item_id}",
                    now,
                    now,
                ),
            )
            if delivered.rowcount == 1:
                self._invalidate_delivery_cursor_snapshots(
                    connection,
                    project_root=str(self.project_root),
                    recipient_label="*",
                    updated_at=now,
                )
            return {"item_id": item_id, "created": bool(inserted.rowcount == 1)}

        return self._domain_write(action)

    def resolve_system_attention(
        self,
        *,
        item_type: str,
        dispatch_id: str,
        keep_reason: str | None = None,
    ) -> WriteResult[dict[str, object]]:
        """Resolve OPEN system attention items for one dispatch.

        Matches ``payload_json.dispatch_id``. When ``keep_reason`` is set,
        OPEN items whose reason still matches stay OPEN so a live hold is
        not collapsed while an earlier unknown hold for the same dispatch
        is retired. Withdraws the quiet attention-stream delivery event
        so a resolved item does not keep paging.
        """
        item_type = self._state_token(item_type, label="attention item_type")
        dispatch = str(dispatch_id or "")
        if not dispatch or len(dispatch) > 512:
            raise ValueError("dispatch_id must be non-empty and bounded")
        keep_reason_token = (
            self._identity_token(keep_reason, label="attention reason")
            if keep_reason is not None
            else None
        )
        now = utc_now()

        def action(connection: sqlite3.Connection) -> dict[str, object]:
            rows = connection.execute(
                """
                SELECT item_id, reason, payload_json
                FROM system_attention_items
                WHERE project_root = ? AND item_type = ? AND state = 'OPEN'
                """,
                (str(self.project_root), item_type),
            ).fetchall()
            resolved: list[str] = []
            for row in rows:
                if (
                    keep_reason_token is not None
                    and str(row["reason"]) == keep_reason_token
                ):
                    continue
                try:
                    payload = json.loads(str(row["payload_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if str(payload.get("dispatch_id") or "") != dispatch:
                    continue
                item_id = str(row["item_id"])
                updated = connection.execute(
                    """
                    UPDATE system_attention_items
                    SET state = 'RESOLVED', resolved_at = ?
                    WHERE item_id = ? AND state = 'OPEN'
                    """,
                    (now, item_id),
                )
                if updated.rowcount != 1:
                    continue
                connection.execute(
                    """
                    UPDATE delivery_events
                    SET withdrawn_at = ?
                    WHERE project_root = ? AND origin_node = 'journal'
                      AND event_uuid = ? AND event_type = 'controller_attention'
                      AND withdrawn_at IS NULL
                    """,
                    (now, str(self.project_root), item_id),
                )
                resolved.append(item_id)
            return {"resolved_item_ids": resolved, "resolved_at": now}

        return self._domain_write(action)

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
        owner_controller_label: str | None = None,
        owner_session_nonce: str | None = None,
        effective_account: str | None = None,
        engine: str | None = None,
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
        if (owner_controller_label is None) != (owner_session_nonce is None):
            raise ValueError(
                "owner_controller_label and owner_session_nonce must be supplied together"
            )
        if owner_controller_label is not None and (
            not isinstance(owner_controller_label, str) or not owner_controller_label
        ):
            raise ValueError("owner_controller_label must be a non-empty string")
        if owner_session_nonce is not None and (
            not isinstance(owner_session_nonce, str) or not owner_session_nonce
        ):
            raise ValueError("owner_session_nonce must be a non-empty string")
        owner_session_digest = goalflight_wake.controller_session_digest(
            owner_session_nonce
        )
        # Seat attribution is reporting evidence, not a launch gate. Empty or
        # malformed values stay NULL, and a seat without an engine is too
        # ambiguous to assert because account ids are provider-local.
        asserted_engine = (
            engine if isinstance(engine, str) and engine.strip() else None
        )
        asserted_effective_account = (
            effective_account
            if (
                asserted_engine is not None
                and isinstance(effective_account, str)
                and effective_account.strip()
            )
            else None
        )

        def action(connection: sqlite3.Connection) -> AttemptIdentity:
            existing = connection.execute(
                """
                SELECT attempt_id, dispatch_id, launch_token, launch_epoch, lifecycle_state,
                       owner_controller_label, owner_session_digest,
                       effective_account, engine
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
                if owner_controller_label is not None and (
                    existing["owner_controller_label"] != owner_controller_label
                    or existing["owner_session_digest"] != owner_session_digest
                ):
                    raise CASMismatch(
                        "dispatch already belongs to a different owner capability"
                    )
                if asserted_effective_account is not None and (
                    existing["effective_account"] is not None
                    and existing["effective_account"] != asserted_effective_account
                ):
                    raise CASMismatch(
                        "dispatch already belongs to a different effective account"
                    )
                if asserted_engine is not None and (
                    existing["engine"] is not None
                    and existing["engine"] != asserted_engine
                ):
                    raise CASMismatch("dispatch already belongs to a different engine")
                if (
                    existing["lifecycle_state"] == ATTEMPT_PREPARED
                    and (
                        (
                            existing["effective_account"] is None
                            and asserted_effective_account is not None
                        )
                        or (
                            existing["engine"] is None
                            and asserted_engine is not None
                        )
                    )
                ):
                    connection.execute(
                        """
                        UPDATE dispatch_attempts
                        SET effective_account = COALESCE(effective_account, ?),
                            engine = COALESCE(engine, ?)
                        WHERE attempt_id = ? AND lifecycle_state = 'PREPARED'
                        """,
                        (
                            asserted_effective_account,
                            asserted_engine,
                            identity.attempt_id,
                        ),
                    )
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
                    start_deadline_at, owner_controller_label, owner_session_digest,
                    effective_account, engine
                ) VALUES (?, ?, ?, 'PREPARED', 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    allocated_attempt,
                    dispatch,
                    str(self.project_root),
                    allocated_token,
                    now,
                    now,
                    resolved_start_deadline,
                    owner_controller_label,
                    owner_session_digest,
                    asserted_effective_account,
                    asserted_engine,
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
                       a.start_deadline_at, a.terminal_at,
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
                    attempt_id=attempt,
                    transition_id=str(existing["terminal_transition_id"]),
                    dispatch_id=str(existing["dispatch_id"]),
                    terminal_state=str(existing["terminal_state"]),
                    event_uuid=str(existing["event_uuid"]),
                    event_type=str(existing["event_type"]),
                    observation=json.loads(str(existing["terminal_outcome_json"])),
                    terminal_at=journal_terminal_at(existing["terminal_at"]),
                    idempotent=True,
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
            payload = {
                "attempt_id": attempt,
                "transition_id": transition_id,
                "dispatch_id": str(existing["dispatch_id"]),
                "terminal_state": terminal,
                "complete": resolved_event_type == "result",
                "text": outbox_headline_text(terminal, observation_value),
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
                attempt_id=attempt,
                transition_id=transition_id,
                dispatch_id=str(existing["dispatch_id"]),
                terminal_state=terminal,
                event_uuid=event_uuid,
                event_type=resolved_event_type,
                observation=json.loads(observation_json),
                terminal_at=now,
                idempotent=False,
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
        select = """
            SELECT attempt_id, transition_id, origin_node, event_uuid,
                   recipient, event_type, payload_json, created_at,
                   projection_attempts, projection_retry_at,
                   projection_quarantined_at
            FROM terminal_outbox
            WHERE projected_at IS NULL
              AND projection_quarantined_at IS NULL
        """
        fresh = self.read_all(
            select
            + """
              AND projection_attempts = 0
            ORDER BY created_at, attempt_id, transition_id
            LIMIT ?
            """,
            (limit,),
        )
        retries = self.read_all(
            select
            + """
              AND projection_attempts > 0
              AND (projection_retry_at IS NULL OR projection_retry_at <= ?)
            ORDER BY COALESCE(projection_retry_at, created_at),
                     created_at, attempt_id, transition_id
            LIMIT ?
            """,
            (utc_now(), limit),
        )
        # Alternate classes so a sustained fresh stream cannot starve retries,
        # while old poison retries can never monopolize the batch either.
        merged: list[dict[str, object]] = []
        fresh_rows = [dict(row) for row in fresh]
        retry_rows = [dict(row) for row in retries]
        for index in range(max(len(fresh_rows), len(retry_rows))):
            if index < len(fresh_rows):
                merged.append(fresh_rows[index])
            if len(merged) >= limit:
                break
            if index < len(retry_rows):
                merged.append(retry_rows[index])
            if len(merged) >= limit:
                break
        return merged

    def _record_outbox_projection_failure(
        self,
        row: Mapping[str, object],
        exc: BaseException,
    ) -> WriteResult[dict[str, object]]:
        attempts = int(row["projection_attempts"]) + 1
        error_text = f"{type(exc).__name__}: {exc}"[:2000]
        quarantined = attempts >= OUTBOX_MAX_PROJECTION_ATTEMPTS
        now = utc_now()
        retry_at = None if quarantined else _utc_after(
            OUTBOX_RETRY_BASE_S * (2 ** (attempts - 1))
        )

        def action(connection: sqlite3.Connection) -> dict[str, object]:
            cursor = connection.execute(
                """
                UPDATE terminal_outbox
                SET projection_attempts = ?, projection_error = ?,
                    projection_retry_at = ?, projection_quarantined_at = ?
                WHERE attempt_id = ? AND transition_id = ?
                  AND projected_at IS NULL AND projection_quarantined_at IS NULL
                """,
                (
                    attempts,
                    error_text,
                    retry_at,
                    now if quarantined else None,
                    str(row["attempt_id"]),
                    str(row["transition_id"]),
                ),
            )
            if cursor.rowcount != 1:
                raise CASMismatch("outbox projection failure lost to another projector")
            if not quarantined:
                return {"attempts": attempts, "retry_at": retry_at, "quarantined": False}

            item_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "goalflight:outbox-quarantine:"
                    + str(row["attempt_id"])
                    + ":"
                    + str(row["transition_id"]),
                )
            )
            payload = {
                "item_id": item_id,
                "type": "terminal_outbox_quarantined",
                "attempt_id": str(row["attempt_id"]),
                "transition_id": str(row["transition_id"]),
                "dispatch_id": str(row["recipient"]),
                "projection_attempts": attempts,
                "projection_error": error_text,
                "text": (
                    f"terminal outbox event for {row['recipient']} quarantined after "
                    f"{attempts} projection failures: {error_text}"
                ),
            }
            connection.execute(
                """
                INSERT OR IGNORE INTO system_attention_items (
                    item_id, project_root, item_type, state, reason,
                    payload_json, wake_class, created_at
                ) VALUES (?, ?, 'terminal_outbox_quarantined', 'OPEN',
                          'projection_retry_exhausted', ?, 'waking', ?)
                """,
                (
                    item_id,
                    str(self.project_root),
                    self._json_object(payload, label="outbox_quarantine_attention"),
                    now,
                ),
            )
            next_seq = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(stream_seq), 0) + 1
                    FROM delivery_events
                    WHERE project_root = ? AND recipient_label = '*'
                      AND stream_id = 'attention'
                    """,
                    (str(self.project_root),),
                ).fetchone()[0]
            )
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO delivery_events (
                    project_root, recipient_label, origin_node, event_uuid,
                    stream_id, stream_seq, carrier_path, event_type,
                    wake_class, created_at, projected_at
                ) VALUES (?, '*', 'journal', ?, 'attention', ?, ?,
                          'controller_attention', 'quiet', ?, ?)
                """,
                (
                    str(self.project_root),
                    item_id,
                    next_seq,
                    f"journal:outbox-quarantine:{item_id}",
                    now,
                    now,
                ),
            )
            if inserted.rowcount == 1:
                self._invalidate_delivery_cursor_snapshots(
                    connection,
                    project_root=str(self.project_root),
                    recipient_label="*",
                    updated_at=now,
                )
            return {"attempts": attempts, "retry_at": None, "quarantined": True}

        return self._domain_write(action)

    def project_terminal_outbox(
        self,
        *,
        messages_dir: Path,
        limit: int = 100,
    ) -> list[OutboxProjection]:
        """Project terminal events to carrier and delivery journal idempotently.

        Delivery completion belongs here, on the wake projection path, instead
        of in the fleet-console producer or the listener's idle poll.  The
        projector already runs only for pending outbox rows, so completing the
        delivery beside the carrier append avoids perpetual listener writes and
        keeps console installation irrelevant to correctness.  ``event_uuid``
        is the retry identity across both records.
        """
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
                    # Terminal attempt ownership, active-roster fanout, and
                    # partial retry healing are journal authority here. Do not
                    # expose a presentation-ledger target in the gap between
                    # the carrier append and authoritative completion.
                    project_journal_delivery=False,
                )
                # A carrier that returns an unexpected shape must degrade to a
                # recorded projection failure, never a KeyError that aborts the
                # whole loop and strands every remaining row.
                if not isinstance(result, dict) or "envelope" not in result or "path" not in result:
                    raise ValueError(
                        "carrier append returned no envelope/path for "
                        f"{row['event_uuid']}"
                    )
                self._complete_terminal_delivery(
                    row,
                    envelope=result["envelope"],
                    carrier_path=Path(str(result["path"])),
                )
            except (
                goalflight_messages.MessageError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                sqlite3.DatabaseError,
            ) as exc:
                self._record_outbox_projection_failure(row, exc)
                continue
            marked = self.write(
                RowOperation.update(
                    "terminal_outbox",
                    {
                        "projected_at": utc_now(),
                        "projection_attempts": int(row["projection_attempts"]) + 1,
                        "projection_error": None,
                        "projection_retry_at": None,
                        "projection_quarantined_at": None,
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

    def _complete_terminal_delivery(
        self,
        row: Mapping[str, object],
        *,
        envelope: Mapping[str, object],
        carrier_path: Path,
    ) -> None:
        """Ensure a carrier-projected terminal has a visible delivery row."""
        origin_node = str(row["origin_node"])
        event_uuid = str(row["event_uuid"])
        attempt = self.read_all(
            """SELECT owner_controller_label FROM dispatch_attempts
               WHERE attempt_id = ?""",
            (str(row["attempt_id"]),),
        )
        owner = str(attempt[0]["owner_controller_label"] or "") if attempt else ""
        if owner and self.active_lease(owner) is not None:
            # Attempt ownership is immutable and preserves the full controller
            # identity even when the presentation-only ledger label truncates
            # it. Revalidate the lease inside the assignment transaction so a
            # concurrent retirement falls back to the durable wildcard.
            recipients = ((owner, True),)
        else:
            # A retired or absent attempt owner is unowned work. Snapshot the
            # complete current roster on every retry and revalidate each label
            # transactionally; an empty or concurrently retiring roster falls
            # back to the durable wildcard.
            labels = tuple(
                str(lease["label"])
                for lease in self.lease_records()
                if str(lease.get("label") or "")
            )
            recipients = tuple((label, True) for label in labels) or (("*", False),)

        # Recompute the complete target set on every retry. A prior projection
        # may have committed only a prefix of this fanout before crashing;
        # record_delivery_event's recipient/origin/event UUID identity makes
        # the committed prefix idempotent while the missing suffix is filled.
        actual_recipients: set[str] = set()
        for recipient, require_active in recipients:
            recorded = self.record_delivery_event(
                recipient_label=recipient,
                origin_node=origin_node,
                event_uuid=event_uuid,
                stream_id=str(envelope.get("dispatch_id") or row["recipient"]),
                stream_seq=int(envelope.get("seq") or 0),
                carrier_path=carrier_path,
                event_type=str(row["event_type"]),
                wake_class="waking",
                created_at=str(row["created_at"]),
                fallback_to_wildcard_if_inactive=require_active,
            )
            if not recorded.committed or recorded.value is None:
                failure_type = JournalBusy if recorded.retryable else JournalError
                raise failure_type(
                    recorded.reason or "terminal delivery assignment was not committed"
                )
            actual_recipient = str(recorded.value["recipient_label"])
            marked = self.mark_delivery_projected(
                recipient_label=actual_recipient,
                origin_node=origin_node,
                event_uuid=event_uuid,
            )
            if not marked.committed or marked.value is None:
                failure_type = JournalBusy if marked.retryable else JournalError
                raise failure_type(
                    marked.reason or "terminal delivery projection was not committed"
                )
            # Retirement can transactionally rehome an exact row to wildcard
            # between assignment and projection. Reconcile against the row
            # that projection actually made visible.
            actual_recipients.add(str(marked.value["recipient_label"]))

        # post_message also assigns the carrier using presentation metadata.
        # Reconcile any stale or truncated carrier-side choice only after every
        # authoritative target is durable, so a crash can always retry safely.
        reconciled = self._reconcile_terminal_delivery_recipients(
            origin_node=origin_node,
            event_uuid=event_uuid,
            authoritative_recipients=actual_recipients,
        )
        if not reconciled.committed:
            failure_type = JournalBusy if reconciled.retryable else JournalError
            raise failure_type(
                reconciled.reason or "stale terminal delivery reconciliation was not committed"
            )

    def _reconcile_terminal_delivery_recipients(
        self,
        *,
        origin_node: str,
        event_uuid: str,
        authoritative_recipients: Iterable[str],
    ) -> WriteResult[tuple[str, ...]]:
        """Withdraw stale carrier targets without racing lease retirement."""
        origin = self._identity_token(origin_node, label="origin node")
        event_id = self._canonical_uuid(event_uuid, label="event_uuid")
        requested = {
            "*" if recipient == "*" else self._identity_token(recipient, label="recipient label")
            for recipient in authoritative_recipients
        }
        project_root = str(self.project_root)

        def action(connection: sqlite3.Connection) -> tuple[str, ...]:
            rows = connection.execute(
                """SELECT recipient_label FROM delivery_events
                   WHERE project_root = ? AND origin_node = ? AND event_uuid = ?
                     AND withdrawn_at IS NULL""",
                (project_root, origin, event_id),
            ).fetchall()
            live = {str(row["recipient_label"]) for row in rows}
            preserved = set(requested)
            # Lease retirement may rehome an exact assignment after its mark
            # commits but before reconciliation starts. If a requested exact
            # row disappeared and wildcard is now live, that wildcard is the
            # authoritative successor rather than stale carrier metadata.
            if "*" in live and any(recipient not in live for recipient in requested):
                preserved.add("*")
            stale = tuple(sorted(live - preserved))
            if not stale:
                return ()
            withdrawn_at = utc_now()
            for recipient in stale:
                connection.execute(
                    """UPDATE delivery_events SET withdrawn_at = ?
                       WHERE project_root = ? AND recipient_label = ?
                         AND origin_node = ? AND event_uuid = ?
                         AND withdrawn_at IS NULL""",
                    (withdrawn_at, project_root, recipient, origin, event_id),
                )
                self._invalidate_delivery_cursor_snapshots(
                    connection,
                    project_root=project_root,
                    recipient_label=recipient,
                    updated_at=withdrawn_at,
                )
            return stale

        return self._domain_write(action)

    def inspect(self) -> dict[str, object]:
        def action(connection: sqlite3.Connection) -> dict[str, object]:
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

        return self._read_with_retry("journal inspect", action)

    def dump_sql(self) -> list[str]:
        def action(connection: sqlite3.Connection) -> list[str]:
            self._assert_epoch_fence(connection, for_write=False)
            rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            if rows != ["ok"]:
                self._raise_integrity_failure("; ".join(rows))
            return list(connection.iterdump())

        return self._read_with_retry("journal SQL dump", action)

    def snapshot(self, output: Path | str) -> Path:
        destination = Path(output).expanduser().resolve(strict=False)
        if destination == self.path:
            raise JournalError("snapshot destination must differ from the live journal")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.tmp-", dir=destination.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            def copy_source(source: sqlite3.Connection) -> None:
                self._assert_epoch_fence(source, for_write=False)
                rows = [str(row[0]) for row in source.execute("PRAGMA integrity_check")]
                if rows != ["ok"]:
                    self._raise_integrity_failure("; ".join(rows))
                with contextlib.closing(sqlite3.connect(tmp)) as target:
                    source.backup(target)

            self._read_with_retry("journal snapshot read", copy_source)
            _validate_snapshot_file(tmp)
            with tmp.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp, destination)
            _fsync_directory(destination.parent)
            return destination
        finally:
            tmp.unlink(missing_ok=True)


def open_or_create_journal(
    project_root: Path | str,
    *,
    allow_migration: bool | None = None,
) -> Journal:
    """Open authority, explicitly bootstrapping only a truly absent path."""
    path = resolve_journal_path(project_root)
    presence = _lstat_presence(path)
    if presence == "unknown":
        # An unreadable present path must not fall through to create: the
        # bootstrap attempt would fail (or worse, partially claim) against a
        # database that may be live.
        raise JournalIOError(
            f"journal path presence is unreadable, so absence is unverified: {path}"
        )
    if presence == "present":
        return Journal(project_root, allow_migration=allow_migration)
    try:
        return Journal.create(project_root)
    except (JournalBusy, JournalDisappeared, JournalIOError):
        # Availability failures are not a create race. Preserve their concrete
        # operator verdict instead of retrying them merely because a path exists.
        raise
    except JournalError as exc:
        presence = _lstat_presence(path)
        if presence == "absent":
            raise
        if presence == "unknown":
            raise JournalIOError(
                f"journal create failed and the path is unreadable, so absence is "
                f"unverified: {path}"
            ) from exc
        return Journal(project_root, allow_migration=allow_migration)


def _validate_snapshot_file(path: Path) -> JournalEpochs:
    if path.is_symlink() or not path.is_file():
        raise JournalIntegrityError(f"snapshot is not a regular non-symlink file: {path}")
    try:
        with contextlib.closing(_open_readonly_connection(path)) as connection:
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
            _upgrade_required_resume(
                f"{subject} epoch fence refused restore before replacement: "
                + "; ".join(mismatches)
            )
        )


def restore_snapshot(
    project_root: Path | str,
    snapshot: Path | str,
    *,
    i_understand: bool,
) -> Path:
    source = Path(os.path.abspath(os.fspath(Path(snapshot).expanduser())))
    destination = resolve_journal_path(project_root)
    presence = _lstat_presence(destination)
    if presence == "absent":
        raise JournalDisappeared(
            f"restore target journal is absent: {destination}. Failing closed; use init only "
            "for an intentional bootstrap, then retry restore from the validated snapshot."
        )
    if presence == "unknown":
        raise JournalIOError(
            f"restore target journal is unreadable, so absence is unverified: "
            f"{destination}. Do not init over an unreadable live journal."
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
        locked_presence = _lstat_presence(destination)
        if locked_presence == "absent":
            raise JournalDisappeared(
                f"restore target journal disappeared before exclusion was acquired: {destination}"
            )
        if locked_presence == "unknown":
            raise JournalIOError(
                f"restore target journal is unreadable after exclusion: {destination}"
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
                failure_type = JournalBusy if _is_busy(exc) else JournalIOError
                raise failure_type(
                    f"restore could not checkpoint the excluded target {destination}: {exc}"
                ) from exc
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise JournalBusy(
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
                    _open_readonly_connection(source)
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
        raise JournalIOError(f"cannot fsync journal directory {path}: {exc}") from exc
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            raise JournalIOError(f"cannot fsync journal directory {path}: {exc}") from exc
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Flight state journal operator tools")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="explicitly create a first-bootstrap journal")
    sub.add_parser("migrate", help="explicitly migrate an existing journal")
    sub.add_parser("inspect", help="validate and describe the live journal")
    sub.add_parser("dump", help="validate and emit a logical SQL dump")
    snapshot_parser = sub.add_parser("snapshot", help="create a validated online SQLite backup")
    snapshot_parser.add_argument("--output", type=Path, required=True)
    restore_parser = sub.add_parser("restore", help="replace an offline journal from a validated snapshot")
    restore_parser.add_argument("--snapshot", type=Path, required=True)
    restore_parser.add_argument("--i-understand", action="store_true")
    args = parser.parse_args(argv)
    try:
        import goalflight_messages
    except ImportError:
        pass
    else:
        try:
            goalflight_messages.emit_wake_entry_notice(
                project_root=goalflight_task.resolve_project_root(str(args.project_root)),
                stream=sys.stderr,
            )
        except (
            goalflight_messages.MessageError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            sqlite3.DatabaseError,
        ):
            pass
    try:
        if args.command == "init":
            print(Journal.create(args.project_root).path)
        elif args.command == "migrate":
            migrated = Journal(args.project_root, allow_migration=True)
            print(json.dumps(migrated.inspect(), indent=2, sort_keys=True))
        elif args.command == "inspect":
            print(
                json.dumps(
                    Journal.open_reader(args.project_root).inspect(),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "dump":
            print("\n".join(Journal.open_reader(args.project_root).dump_sql()))
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
    except (JournalBusy, JournalDisappeared, JournalIOError) as exc:
        print(f"{args.command}: refused: {exc}", file=sys.stderr)
        return 2
    except (JournalError, OSError) as exc:
        print(f"{args.command}: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
