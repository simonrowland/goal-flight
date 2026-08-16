#!/usr/bin/env python3
"""Marker → message envelope conversion and dispatch inbox (Track C Phase 0)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import datetime as dt
from enum import Enum
import functools
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
import sys
import time
from typing import TypeVar

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_CONTRACT = REPO_ROOT / "docs-private" / "architecture" / "contracts" / "goalflight.message.v1.json"
AGGREGATE_SCHEMA = "goalflight.fleet.register.aggregate.v1"
INGESTION_ORDER_FILE = ".ingestion-order"
INGESTION_IDENTITY_FILE = ".ingestion-identities.json"
INGESTION_IDENTITY_SCHEMA = "goalflight.ingestion-identities.v1"
_INBOX_CURSOR_KEY_FIELD = "_goalflight_inbox_cursor_key"
_INBOX_CURSOR_KEYS_FIELD = "_goalflight_inbox_cursor_keys"
_INBOX_SOURCE_PATHS_FIELD = "_goalflight_inbox_source_paths"
_INGESTION_ORDER_FIELD = "_goalflight_ingestion_order"
DEFAULT_RELAY_ITEM_LIMIT = 20
DEFAULT_RELAY_BYTE_LIMIT = 4096
TASKLESS_TERMINAL_STALE_AFTER = dt.timedelta(hours=24)
PROJECT_MAIL_ALIASES_ENV = "GOALFLIGHT_PROJECT_MAIL_ALIASES"
MIN_DERIVED_PROJECT_ALIAS_LEN = 4

sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat  # noqa: E402
import goalflight_steer_mailbox  # noqa: E402
import goalflight_wake  # noqa: E402
from goalflight_watch import BLOCKING_TERMINAL_MARKERS, SUCCESS_TERMINAL_MARKERS  # noqa: E402

_EXPECTED_OPTIONAL_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.DatabaseError,
    subprocess.SubprocessError,
)


@dataclass(frozen=True)
class EventTypeRegistration:
    schema: str
    wake_class: str
    authoritative_state_source: str
    dedupe_semantics: str
    claim_required: bool
    orphan_disposition: str
    retention_class: str


# D13 is deliberately data, not scattered conditionals. Canonical types and the
# finite measured compatibility vocabulary are registered before ingress
# validation, while every unmeasured type remains a hard error.
EVENT_TYPE_REGISTRY: dict[str, EventTypeRegistration] = {
    "status": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "quiet", "worker-status",
        "origin_node+event_uuid", False, "quiet-expire", "quiet-7d",
    ),
    "monitor": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "quiet", "worker-status",
        "origin_node+event_uuid", False, "quiet-expire", "quiet-7d",
    ),
    "user_need": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "journal.terminal-outbox+task-store",
        "attempt_id+transition_id|origin_node+event_uuid", True, "attention-item", "critical",
    ),
    "user_confirm": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "journal.terminal-outbox",
        "attempt_id+transition_id", True, "attention-item", "critical",
    ),
    "result": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "journal.terminal-outbox",
        "attempt_id+transition_id", True, "attention-item", "critical",
    ),
    "blocked": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "journal.terminal-outbox",
        "attempt_id+transition_id", True, "attention-item", "critical",
    ),
    "advisory": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "service-observation",
        "origin_node+event_uuid", False, "held-for-recipient", "controller-mail",
    ),
    "steering": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "fleet-steering-register",
        "origin_node+event_uuid", True, "attention-item", "critical",
    ),
    "controller-question": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "journal.attention-items",
        "origin_node+event_uuid", True, "attention-item", "critical",
    ),
    "controller-answer": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "controller-channel",
        "origin_node+event_uuid", False, "held-for-recipient", "controller-mail",
    ),
    "controller-notice": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "controller-channel",
        "origin_node+event_uuid", False, "held-for-recipient", "controller-mail",
    ),
    "controller-coordination": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "journal.attention-items",
        "origin_node+event_uuid", True, "attention-item", "critical",
    ),
    "coordination": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "journal.attention-items",
        "origin_node+event_uuid", True, "attention-item", "critical",
    ),
    "notice": EventTypeRegistration(
        "goalflight.message.v1/payload.object", "waking", "controller-channel",
        "origin_node+event_uuid", False, "held-for-recipient", "controller-mail",
    ),
}

CANONICAL_EVENT_TYPES = frozenset(EVENT_TYPE_REGISTRY)

# Finite compatibility vocabulary measured from the cross-repository carrier
# fleet at the D13 cutover.  Each legacy spelling inherits one canonical
# lifecycle contract; anything outside this map still fails closed at ingress.
EVENT_TYPE_COMPATIBILITY_ALIASES: dict[str, str] = {
    "ack": "advisory",
    "bug-fix-handoff": "advisory",
    "bug-haul": "advisory",
    "canonization-update": "advisory",
    "catalog-asks-response": "advisory",
    "catalog-handoff": "advisory",
    "catalog-handoff-ack": "advisory",
    "catalog-integration-ack": "advisory",
    "catalog-state-consolidation": "advisory",
    "catalog-update": "advisory",
    "consolidation-request": "advisory",
    "controller-handoff": "advisory",
    "controller-note": "advisory",
    "controller_coordination": "advisory",
    "controller_report": "advisory",
    "controller_request": "advisory",
    "coord": "advisory",
    "coordination-answer": "advisory",
    "coordination-update": "advisory",
    "corpus-merge-notice": "advisory",
    "defect-notice": "advisory",
    "fea-complete-ontario-handoff": "advisory",
    "fea-contract-ready": "advisory",
    "finding": "advisory",
    "fki-ref-fixed": "advisory",
    "forensics": "advisory",
    "idea-relay": "advisory",
    "merge-ack": "advisory",
    "merge-package-handoff": "advisory",
    "merge-request": "advisory",
    "note": "advisory",
    "ontario-division-ack": "advisory",
    "patch": "advisory",
    "qa-bug": "advisory",
    "qa-complete": "advisory",
    "qa-round": "advisory",
    "question": "advisory",
    "raise-coordination": "advisory",
    "reply": "advisory",
    "request": "advisory",
    "ruling": "advisory",
    "sequencing-decision": "advisory",
    "sequencing-reconcile": "advisory",
    "steer": "steering",
    "user_confirm_reply": "user_confirm",
    "web-fix-block-handoff": "advisory",
}
EVENT_TYPE_REGISTRY.update(
    {
        alias: EVENT_TYPE_REGISTRY[canonical]
        for alias, canonical in EVENT_TYPE_COMPATIBILITY_ALIASES.items()
    }
)


def canonical_event_type(msg_type: str) -> str:
    return EVENT_TYPE_COMPATIBILITY_ALIASES.get(msg_type, msg_type)


def event_type_registration(msg_type: object) -> EventTypeRegistration:
    if not isinstance(msg_type, str) or msg_type not in EVENT_TYPE_REGISTRY:
        raise MessageError(
            "unregistered message type; expected one of "
            + ", ".join(sorted(EVENT_TYPE_REGISTRY))
        )
    return EVENT_TYPE_REGISTRY[msg_type]


def event_wake_class(msg_type: str, payload: object = None) -> str:
    registration = event_type_registration(msg_type)
    if (
        msg_type == "user_need"
        and isinstance(payload, dict)
        and payload.get("nudge_kind") in TASK_STORE_STATUS_NUDGE_KINDS
    ):
        return "quiet"
    return registration.wake_class

MARKER_TO_TYPE: dict[str, str] = {
    "STATUS": "status",
    "STEER-ACK": "monitor",
    "USER-NEED": "user_need",
    "USER-CONFIRM": "user_confirm",
    **{kind: "result" for kind in SUCCESS_TERMINAL_MARKERS},
    **{kind: "blocked" for kind in BLOCKING_TERMINAL_MARKERS - {"USER-NEED", "USER-CONFIRM"}},
}

PRIORITY_BY_TYPE: dict[str, str] = {
    "user_need": "urgent",
    "user_confirm": "urgent",
    "blocked": "urgent",
}
# Typed controller channel. The unprefixed values are legacy envelopes that the
# aggregate already accepts; keeping one set prevents delivery and relay drift.
CONTROLLER_CHANNEL_TYPES = frozenset(
    {
        "controller-question",
        "controller-answer",
        "controller-notice",
        "controller-coordination",
        "coordination",
        "notice",
    }
)
CONTROLLER_LISTENER_ESCALATION_TYPES = frozenset({"user_need", "user_confirm", "blocked"})
TASK_STORE_STATUS_NUDGE_KINDS = frozenset({"parallel-ready", "resume-ready", "done-suggest"})
NON_ERROR_UNDELIVERED_STATUSES = frozenset({"terminal_recorded_only", "worker_view_queued"})
CONTROLLER_ADDRESSEE_KIND = "controller"
STREAM_TOKEN_MAX = 255
STREAM_TOKEN_RE = re.compile(
    rf"[A-Za-z0-9](?:[A-Za-z0-9._:@+\-]{{0,{STREAM_TOKEN_MAX - 2}}}[A-Za-z0-9])?\Z"
)
MESSAGE_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})\Z"
)
MESSAGE_PRIORITIES = frozenset({"normal", "urgent"})
MAX_SOURCE_VALUE_LENGTH = 128
MAX_PROJECT_ROOT_LENGTH = 4096
QUARANTINE_BYTES_LIMIT = 256
MAX_JSON_DEPTH = 32
MAX_ENVELOPE_JSON_BYTES = 1_048_576
MAX_PAYLOAD_JSON_BYTES = 786_432

REQUIRED_ENVELOPE_FIELDS = (
    "schema",
    "schema_version",
    "id",
    "dispatch_id",
    "seq",
    "ts",
    "source",
    "type",
    "payload",
)


class MessageError(Exception):
    pass


class CarrierReadStatus(str, Enum):
    OK = "ok"
    CORRUPT_RECORDS_QUARANTINED = "corrupt-records-quarantined"
    CARRIER_UNREADABLE = "CARRIER-UNREADABLE"


@dataclass(frozen=True)
class CarrierReadResult:
    status: CarrierReadStatus
    envelopes: tuple[dict, ...]
    errors: tuple[dict[str, object], ...]


def require_positive_int_seq(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MessageError(f"{path}: seq must be an integer >= 1")
    if value < 1:
        raise MessageError(f"{path}: seq must be an integer >= 1")
    return value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def default_messages_dir() -> Path:
    return goalflight_compat.resolve_env_path(
        "GOALFLIGHT_MESSAGES_DIR", Path.home() / ".goal-flight" / "messages"
    )


def default_fleet_dir() -> Path:
    return goalflight_compat.resolve_env_path(
        "GOALFLIGHT_FLEET_DIR", Path.home() / ".goal-flight" / "fleet"
    )


def validate_stream_id(dispatch_id: object, *, path: str = "dispatch_id") -> str:
    if not isinstance(dispatch_id, str) or not STREAM_TOKEN_RE.fullmatch(dispatch_id):
        raise MessageError(
            f"{path}: expected a 1..{STREAM_TOKEN_MAX} character stream token "
            "using letters, digits, '.', '_', ':', '@', '+', or '-'"
        )
    return dispatch_id


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _canonical_jsonl_path(
    path: Path,
    *,
    allow_quarantine: bool = False,
    verify_identity: bool = False,
) -> Path:
    lexical = _lexical_absolute(Path(path))
    if lexical.suffix != ".jsonl":
        raise MessageError(f"{path}: carrier must be a .jsonl file")
    if lexical.name.endswith(".quarantine.jsonl") and not allow_quarantine:
        raise MessageError(f"{path}: quarantine sidecar is not an event carrier")
    if lexical.is_symlink():
        raise MessageError(f"{path}: symlinked inbox refused")
    resolved_parent = lexical.parent.resolve(strict=False)
    if lexical.exists():
        try:
            mode = os.lstat(lexical).st_mode
        except OSError as exc:
            raise MessageError(f"{path}: cannot stat carrier: {exc}") from exc
        if not stat.S_ISREG(mode):
            raise MessageError(f"{path}: inbox is not a regular file; refusing access")
    # Resolve the parent but retain the lexical final component. Final-component
    # symlink authority belongs to O_NOFOLLOW + fstat at the actual open.
    resolved = resolved_parent / lexical.name
    if resolved.parent != resolved_parent:
        raise MessageError(f"{path}: resolved carrier escapes its stream directory")
    if verify_identity and lexical.exists() and resolved.exists():
        try:
            if os.lstat(lexical).st_ino != os.stat(resolved).st_ino:
                # The name changed identity between the regular-file check and
                # resolution (review round 3 finding 5's swap window). Refuse
                # rather than operate on whichever file won the race; the
                # O_NOFOLLOW + fstat checks at every open remain the final
                # authority on what actually gets read or written.
                raise MessageError(
                    f"{path}: carrier identity changed during resolution; refusing"
                )
        except OSError as exc:
            raise MessageError(f"{path}: cannot verify carrier identity: {exc}") from exc
    return resolved


def inbox_path(messages_dir: Path, dispatch_id: str) -> Path:
    token = validate_stream_id(dispatch_id)
    lexical_base = _lexical_absolute(Path(messages_dir))
    resolved_base = lexical_base.resolve(strict=False)
    candidate = _canonical_jsonl_path(lexical_base / f"{token}.jsonl")
    if candidate.parent != resolved_base:
        raise MessageError(f"dispatch_id: resolved inbox escapes messages directory: {dispatch_id!r}")
    return candidate


def mail_lock_path(path: Path) -> Path:
    lexical = _lexical_absolute(Path(path))
    resolved = lexical.parent.resolve(strict=False) / lexical.name
    return resolved.with_name(f".{resolved.name}.lock")


@contextlib.contextmanager
def mail_lock(path: Path):
    lock = mail_lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if lock.is_symlink():
        raise MessageError(f"{lock}: symlinked lock refused")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock, flags, 0o600)
    except OSError as exc:
        raise MessageError(f"{lock}: cannot open carrier lock: {exc}") from exc
    with os.fdopen(fd, "r+", encoding="utf-8") as fh:
        goalflight_compat.flock(fh, goalflight_compat.LOCK_EX)
        try:
            yield
        finally:
            goalflight_compat.flock(fh, goalflight_compat.LOCK_UN)


def _next_ingestion_order(messages_dir: Path) -> int:
    """Allocate a controller-local causal order that survives restarts and clock rollback."""
    path = messages_dir / INGESTION_ORDER_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with mail_lock(path):
        try:
            previous = max(0, int(path.read_text(encoding="utf-8").strip()))
        except (OSError, TypeError, ValueError, UnicodeDecodeError):
            previous = 0
        order = max(previous + 1, time.time_ns())
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(f"{order}\n", encoding="utf-8")
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
    return order


def _load_ingestion_identity_orders(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MessageError(f"{path}: invalid ingestion identity store: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != INGESTION_IDENTITY_SCHEMA
        or raw.get("schema_version") != 1
        or not isinstance(raw.get("orders"), dict)
    ):
        raise MessageError(f"{path}: unsupported ingestion identity store")
    orders: dict[str, int] = {}
    for identity, value in raw["orders"].items():
        if not isinstance(identity, str) or isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise MessageError(f"{path}: invalid ingestion identity entry")
        orders[identity] = value
    return orders


def _write_ingestion_identity_orders(path: Path, orders: dict[str, int]) -> None:
    document = {
        "schema": INGESTION_IDENTITY_SCHEMA,
        "schema_version": 1,
        "orders": dict(sorted(orders.items())),
    }
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _ingestion_order_for_envelope(messages_dir: Path, envelope: dict) -> int:
    """Assign one durable controller-local order per canonical event identity."""
    identity = _canonical_envelope_identity(envelope)
    path = messages_dir / INGESTION_IDENTITY_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with mail_lock(path):
        orders = _load_ingestion_identity_orders(path)
        existing = orders.get(identity)
        if existing is not None:
            return existing
        order = _next_ingestion_order(messages_dir)
        orders[identity] = order
        _write_ingestion_identity_orders(path, orders)
        return order


def _bounded_nonblank_string(value: object, *, path: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MessageError(f"{path}: expected 1..{limit} non-blank characters")
    return value


def _validate_rfc3339(value: object, *, path: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or not RFC3339_RE.fullmatch(value):
        raise MessageError(f"{path}: expected an RFC3339 timestamp with timezone")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise MessageError(f"{path}: invalid RFC3339 timestamp: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MessageError(f"{path}: RFC3339 timestamp must include a timezone")
    return value


def _validate_json_tree(
    value: object,
    *,
    path: str,
    depth: int = 0,
    ancestors: frozenset[int] = frozenset(),
) -> None:
    if depth > MAX_JSON_DEPTH:
        raise MessageError(f"{path}: JSON nesting exceeds maximum depth {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MessageError(f"{path}: non-finite JSON number refused")
        return
    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in ancestors:
            raise MessageError(f"{path}: cyclic JSON value refused")
        nested_ancestors = ancestors | {identity}
        if isinstance(value, dict):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise MessageError(f"{path}: JSON object keys must be strings")
                _validate_json_tree(
                    nested,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                    ancestors=nested_ancestors,
                )
        else:
            for index, nested in enumerate(value):
                _validate_json_tree(
                    nested,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    ancestors=nested_ancestors,
                )
        return
    raise MessageError(f"{path}: value of type {type(value).__name__} is not JSON-serializable")


def _canonical_json_text(value: object, *, path: str, byte_limit: int) -> str:
    _validate_json_tree(value, path=path)
    try:
        serialized = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise MessageError(f"{path}: JSON serialization refused: {type(exc).__name__}: {exc}") from exc
    size = len(serialized.encode("utf-8"))
    if size > byte_limit:
        raise MessageError(f"{path}: canonical JSON is {size} bytes; limit is {byte_limit}")
    return serialized


def validate_payload(payload: object, *, path: str = "payload") -> dict:
    if not isinstance(payload, dict):
        raise MessageError("payload must be an object")
    _canonical_json_text(payload, path=path, byte_limit=MAX_PAYLOAD_JSON_BYTES)
    return payload


@functools.lru_cache(maxsize=512)
def _canonical_project_root_text(project_root: str) -> str:
    return str(_canonical_project_root(Path(project_root)))


def validate_envelope(
    envelope: dict,
    *,
    path: str = "envelope",
    expected_dispatch_id: str | None = None,
) -> str:
    if not isinstance(envelope, dict):
        raise MessageError(f"{path}: expected object")
    for field in REQUIRED_ENVELOPE_FIELDS:
        if field not in envelope:
            raise MessageError(f"{path}: missing field: {field}")
    if envelope.get("schema") != "goalflight.message.v1":
        raise MessageError(f"{path}: schema must be goalflight.message.v1")
    if isinstance(envelope.get("schema_version"), bool) or not isinstance(
        envelope.get("schema_version"), int
    ) or envelope.get("schema_version") != 1:
        raise MessageError(f"{path}: unsupported schema_version")
    event_id = _bounded_nonblank_string(envelope.get("id"), path=f"{path}.id", limit=36)
    try:
        parsed_event_id = uuid.UUID(event_id)
    except ValueError as exc:
        raise MessageError(f"{path}.id: expected a UUID") from exc
    if str(parsed_event_id) != event_id:
        raise MessageError(f"{path}.id: expected a canonical lowercase UUID")
    dispatch_id = validate_stream_id(envelope.get("dispatch_id"), path=f"{path}.dispatch_id")
    if expected_dispatch_id is not None and dispatch_id != expected_dispatch_id:
        raise MessageError(
            f"{path}.dispatch_id: {dispatch_id!r} does not match stream {expected_dispatch_id!r}"
        )
    require_positive_int_seq(envelope.get("seq"), path=f"{path}.seq")
    _validate_rfc3339(envelope.get("ts"), path=f"{path}.ts")
    source = envelope.get("source")
    if not isinstance(source, dict):
        raise MessageError(f"{path}.source: expected object")
    for key in ("node", "adapter", "transport"):
        if key not in source:
            raise MessageError(f"{path}.source: missing {key}")
        _bounded_nonblank_string(
            source.get(key), path=f"{path}.source.{key}", limit=MAX_SOURCE_VALUE_LENGTH
        )
    msg_type = envelope.get("type")
    if not isinstance(msg_type, str) or not MESSAGE_TYPE_RE.fullmatch(msg_type):
        raise MessageError(f"{path}.type: expected a bounded message-type token")
    event_type_registration(msg_type)
    if "priority" in envelope and envelope.get("priority") not in MESSAGE_PRIORITIES:
        raise MessageError(
            f"{path}.priority: expected one of {', '.join(sorted(MESSAGE_PRIORITIES))}"
        )
    if not isinstance(envelope.get("payload"), dict):
        raise MessageError(f"{path}.payload: expected object")
    addressee = envelope.get("addressee")
    if addressee is not None:
        if not isinstance(addressee, dict):
            raise MessageError(f"{path}.addressee: expected object")
        if addressee.get("kind") != CONTROLLER_ADDRESSEE_KIND:
            raise MessageError(f"{path}.addressee.kind: unsupported addressee kind")
        _bounded_nonblank_string(
            addressee.get("label"), path=f"{path}.addressee.label", limit=64
        )
        project_root = _bounded_nonblank_string(
            addressee.get("project_root"),
            path=f"{path}.addressee.project_root",
            limit=MAX_PROJECT_ROOT_LENGTH,
        )
        if not Path(project_root).is_absolute():
            raise MessageError(f"{path}.addressee.project_root: expected an absolute path")
        canonical_root = _canonical_project_root_text(project_root)
        if project_root != canonical_root:
            raise MessageError(
                f"{path}.addressee.project_root: expected canonical root {canonical_root!r}"
            )
        if canonical_event_type(str(envelope.get("type") or "")) not in CONTROLLER_CHANNEL_TYPES:
            raise MessageError(
                f"{path}.addressee: controller addressing is only valid for controller-channel types"
            )
    return _canonical_json_text(envelope, path=path, byte_limit=MAX_ENVELOPE_JSON_BYTES)


def controller_addressee(label: str, *, project_root: Path | str) -> dict[str, str]:
    """Build the project-root + stable-name controller address."""
    resolved = str(label or "").strip()
    if not resolved or len(resolved) > 64:
        raise MessageError("controller addressee label must contain 1..64 non-blank characters")
    root = controller_address_project_root(project_root)
    return {
        "kind": CONTROLLER_ADDRESSEE_KIND,
        "label": resolved,
        "project_root": root,
    }


def controller_address_project_root(project_root: Path | str) -> str:
    return _canonical_project_root_text(str(project_root))


def controller_addressee_label(envelope: dict) -> str | None:
    addressee = envelope.get("addressee") if isinstance(envelope, dict) else None
    if not isinstance(addressee, dict) or addressee.get("kind") != CONTROLLER_ADDRESSEE_KIND:
        return None
    label = addressee.get("label")
    return label.strip() if isinstance(label, str) and label.strip() else None


def controller_addressee_project_root(envelope: dict) -> str | None:
    addressee = envelope.get("addressee") if isinstance(envelope, dict) else None
    if not isinstance(addressee, dict) or addressee.get("kind") != CONTROLLER_ADDRESSEE_KIND:
        return None
    root = addressee.get("project_root")
    return str(root).strip() if isinstance(root, str) and str(root).strip() else None


def controller_cursor_key(
    label: str,
    dispatch_id: str,
    project_root: str | Path | None = None,
    *,
    inbox_key: str | None = None,
) -> str:
    """Recipient-private cursor key; one controller cannot mark another's mail read."""
    cursor_dispatch_id = inbox_key or str(dispatch_id)
    identity = (
        [str(project_root), str(label), cursor_dispatch_id]
        if project_root is not None
        else [str(label), cursor_dispatch_id]
    )
    return "controller:" + json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def envelope_cursor_key(envelope: dict, *, inbox_key: str | None = None) -> str:
    dispatch_id = str(envelope.get("dispatch_id") or "")
    resolved_inbox_key = inbox_key or str(
        envelope.get(_INBOX_CURSOR_KEY_FIELD) or dispatch_id
    )
    label = controller_addressee_label(envelope)
    project_root = controller_addressee_project_root(envelope)
    return (
        controller_cursor_key(
            label,
            dispatch_id,
            project_root,
            inbox_key=resolved_inbox_key,
        )
        if label and project_root
        else resolved_inbox_key
    )


def inbox_cursor_keys(envelope: dict) -> list[str]:
    """Cursor domains attached to one logical envelope or projected item."""
    values = envelope.get(_INBOX_CURSOR_KEYS_FIELD)
    if isinstance(values, list):
        keys = [str(value) for value in values if str(value)]
        if keys:
            return list(dict.fromkeys(keys))
    key = str(
        envelope.get(_INBOX_CURSOR_KEY_FIELD)
        or envelope.get("dispatch_id")
        or ""
    )
    return [key] if key else []


def resolved_envelope_cursor_keys(envelope: dict) -> list[str]:
    """Recipient-aware cursor keys for every stream carrying an envelope."""
    return [
        envelope_cursor_key(envelope, inbox_key=inbox_key)
        for inbox_key in inbox_cursor_keys(envelope)
    ]


def _cursor_metadata(keys: list[str]) -> dict[str, object]:
    unique = list(dict.fromkeys(str(key) for key in keys if str(key)))
    return {
        _INBOX_CURSOR_KEY_FIELD: unique[0] if unique else None,
        _INBOX_CURSOR_KEYS_FIELD: unique,
    }


def _without_inbox_metadata(envelope: dict) -> dict:
    clean = dict(envelope)
    clean.pop(_INBOX_CURSOR_KEY_FIELD, None)
    clean.pop(_INBOX_CURSOR_KEYS_FIELD, None)
    clean.pop(_INBOX_SOURCE_PATHS_FIELD, None)
    return clean


def _canonical_envelope_identity(envelope: dict) -> str:
    """Full source content identity; controller ingestion metadata is transport-local."""
    clean = _without_inbox_metadata(envelope)
    clean.pop(_INGESTION_ORDER_FIELD, None)
    return _canonical_json_text(
        clean,
        path="envelope identity",
        byte_limit=MAX_ENVELOPE_JSON_BYTES,
    )


def _event_causal_sort_key(envelope: dict) -> tuple[object, ...]:
    """Order cross-carrier events only by controller order; legacy falls back deterministically."""
    ingestion_order = envelope.get(_INGESTION_ORDER_FIELD)
    identity = _canonical_envelope_identity(envelope)
    if isinstance(ingestion_order, int):
        return (1, ingestion_order, identity)
    stream_keys = tuple(sorted(inbox_cursor_keys(envelope)))
    return (0, stream_keys, int(envelope.get("seq", 0)), identity)


def quarantine_path(path: Path) -> Path:
    canonical = _canonical_jsonl_path(Path(path))
    return canonical.with_name(f"{canonical.name}.quarantine.jsonl")


def _require_carrier_path(path: Path) -> str:
    canonical = _canonical_jsonl_path(Path(path))
    return validate_stream_id(canonical.stem, path=f"{canonical}.stream")


def _read_nofollow_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise MessageError(f"{path}: unreadable carrier: {type(exc).__name__}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MessageError(f"{path}: inbox is not a regular file; refusing access")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise MessageError(f"cannot fsync carrier directory {path}: {exc}") from exc
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            raise MessageError(f"cannot fsync carrier directory {path}: {exc}") from exc
    finally:
        os.close(fd)


def _append_fsync(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise MessageError(f"{path}: symlinked file refused")
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise MessageError(f"{path}: cannot append carrier: {exc}") from exc
    except OSError as exc:
        raise MessageError(f"{path}: cannot append carrier: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MessageError(f"{path}: append target is not a regular file")
        with os.fdopen(fd, "ab", buffering=0) as handle:
            fd = -1
            handle.write(data)
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    if created:
        _fsync_directory(path.parent)


def _replace_fsync(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise MessageError(f"{path}: symlinked inbox refused")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


@dataclass
class CarrierTransaction:
    """The only raw JSONL write surface; callers already hold its canonical lock."""

    path: Path
    _read_succeeded: bool = False

    def read_bytes(self) -> bytes:
        self._read_succeeded = False
        data = _read_nofollow_bytes(self.path)
        self._read_succeeded = True
        return data

    def _require_read_before_write(self) -> None:
        if not self._read_succeeded:
            raise MessageError(
                f"CARRIER-UNREADABLE: retryable write refused until {self.path} "
                "has been read successfully under this transaction lock"
            )

    def append_bytes(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("carrier append requires bytes")
        self._require_read_before_write()
        _append_fsync(self.path, data)

    def replace_bytes(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("carrier replacement requires bytes")
        self._require_read_before_write()
        _replace_fsync(self.path, data)


@contextlib.contextmanager
def carrier_transaction(path: Path, *, quarantine_sidecar: bool = False):
    """Lock one canonical carrier, then re-resolve and validate its identity."""
    canonical = _canonical_jsonl_path(Path(path), allow_quarantine=quarantine_sidecar)
    canonical.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with mail_lock(canonical):
        locked_canonical = _canonical_jsonl_path(
            canonical,
            allow_quarantine=quarantine_sidecar,
            verify_identity=True,
        )
        yield CarrierTransaction(locked_canonical)


def _quarantine_row(path: Path, offset: int, reason: str, raw_line: bytes) -> dict:
    canonical = _canonical_jsonl_path(Path(path))
    return {
        "path": str(canonical),
        "offset": offset,
        "reason": reason,
        "hash": hashlib.sha256(raw_line).hexdigest(),
        "bytes": list(raw_line[:QUARANTINE_BYTES_LIMIT]),
    }


def _record_quarantine(row: dict) -> None:
    carrier = _canonical_jsonl_path(Path(str(row["path"])))
    canonical_row = {**row, "path": str(carrier)}
    sidecar = quarantine_path(carrier)
    identity = (canonical_row["path"], canonical_row["offset"], canonical_row["hash"])
    with carrier_transaction(sidecar, quarantine_sidecar=True) as transaction:
        existing = transaction.read_bytes()
        for raw in existing.splitlines():
            try:
                item = json.loads(raw)
            except (UnicodeDecodeError, ValueError, RecursionError):
                continue
            if not isinstance(item, dict):
                continue
            if (item.get("path"), item.get("offset"), item.get("hash")) == identity:
                return
        transaction.append_bytes(
            (json.dumps(canonical_row, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )


def record_carrier_quarantine(path: Path, offset: int, reason: str, raw_line: bytes) -> dict:
    """Record one malformed JSONL row using canonical path identity."""
    row = _quarantine_row(path, offset, reason, raw_line)
    _record_quarantine(row)
    return row


def _carrier_error(
    path: Path,
    *,
    line_no: int | None,
    offset: int | None,
    reason: str,
    raw_line: bytes | None,
    envelopes: list[dict],
) -> dict[str, object]:
    error: dict[str, object] = {
        "path": str(path),
        "line": line_no,
        "offset": offset,
        "error": f"{path}{f':{line_no}' if line_no is not None else ''}: {reason}",
        "reason": reason,
        "validated_envelopes": len(envelopes),
        "validated_through_seq": max(
            (int(env.get("seq", 0)) for env in envelopes), default=0
        ),
    }
    if raw_line is not None and offset is not None:
        row = _quarantine_row(path, offset, reason, raw_line)
        error.update({"hash": row["hash"], "bytes": row["bytes"]})
    return error


def _read_envelope_records(
    path: Path, *, tolerate_errors: bool, locked_data: bytes | None = None
) -> CarrierReadResult:
    path = _canonical_jsonl_path(Path(path))
    expected_dispatch_id = _require_carrier_path(path)
    try:
        data = _read_nofollow_bytes(path) if locked_data is None else locked_data
    except MessageError as exc:
        error = _carrier_error(
            path,
            line_no=None,
            offset=None,
            reason=str(exc),
            raw_line=None,
            envelopes=[],
        )
        error["carrier_status"] = CarrierReadStatus.CARRIER_UNREADABLE.value
        return CarrierReadResult(
            CarrierReadStatus.CARRIER_UNREADABLE,
            (),
            (error,),
        )
    envelopes: list[dict] = []
    errors: list[dict[str, object]] = []
    offset = 0
    for line_no, chunk in enumerate(data.splitlines(keepends=True), start=1):
        raw_line = chunk.rstrip(b"\r\n")
        line_offset = offset
        offset += len(chunk)
        if not raw_line.strip():
            continue
        reason: str | None = None
        envelope: object = None
        try:
            decoded = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            reason = f"invalid UTF-8: {exc}"
        if reason is None:
            try:
                envelope = json.loads(decoded)
            except (ValueError, RecursionError) as exc:
                reason = f"invalid JSON: {exc}"
        if reason is None:
            try:
                validate_envelope(
                    envelope,
                    path=f"{path}:{line_no}",
                    expected_dispatch_id=expected_dispatch_id,
                )
            except (MessageError, ValueError, RecursionError) as exc:
                reason = str(exc)
        if reason is not None:
            error = _carrier_error(
                path,
                line_no=line_no,
                offset=line_offset,
                reason=reason,
                raw_line=raw_line,
                envelopes=envelopes,
            )
            errors.append(error)
            _record_quarantine(
                _quarantine_row(path, line_offset, reason, raw_line)
            )
            if not tolerate_errors:
                break
            continue
        envelopes.append(envelope)  # type: ignore[arg-type]
    status = (
        CarrierReadStatus.CORRUPT_RECORDS_QUARANTINED
        if errors
        else CarrierReadStatus.OK
    )
    return CarrierReadResult(status, tuple(envelopes), tuple(errors))


def read_envelopes_result(
    path: Path, *, tolerate_errors: bool = True
) -> CarrierReadResult:
    """Return the explicit ok/quarantined/unreadable carrier read state."""
    return _read_envelope_records(path, tolerate_errors=tolerate_errors)


def _read_envelope_prefix(path: Path) -> tuple[list[dict], dict[str, object] | None]:
    """Return the validated prefix and first error for strict audit callers."""
    result = read_envelopes_result(path, tolerate_errors=False)
    return list(result.envelopes), result.errors[0] if result.errors else None


def _emit_carrier_error(error: dict[str, object], *, stream=None) -> None:
    # Controller watermarks materialize the same error as a stable waking event.
    print(
        f"WARNING: carrier corruption: {error.get('error')}",
        file=sys.stderr if stream is None else stream,
    )


def read_envelopes(path: Path, *, last_n: int | None = None) -> list[dict]:
    envelopes, error = _read_envelope_prefix(path)
    if error is not None:
        raise MessageError(str(error["error"]))
    if last_n is not None and last_n >= 0:
        return envelopes[-last_n:] if last_n else []
    return envelopes


def read_envelopes_tolerant(
    path: Path,
    *,
    last_n: int | None = None,
    carrier_errors: list[dict[str, object]] | None = None,
) -> list[dict]:
    result = read_envelopes_result(path, tolerate_errors=True)
    envelopes = list(result.envelopes)
    errors = list(result.errors)
    if result.status is CarrierReadStatus.CARRIER_UNREADABLE:
        detail = str(errors[0]["error"]) if errors else str(path)
        raise MessageError(f"CARRIER-UNREADABLE: retryable carrier read: {detail}")
    if carrier_errors is not None:
        carrier_errors.extend(errors)
    elif errors:
        for error in errors:
            _emit_carrier_error(error)
    if last_n is not None and last_n >= 0:
        return envelopes[-last_n:] if last_n else []
    return envelopes


def _read_envelopes_for_write(transaction: CarrierTransaction) -> list[dict]:
    try:
        locked_data = transaction.read_bytes()
    except MessageError as exc:
        raise MessageError(
            f"CARRIER-UNREADABLE: retryable carrier read refused write: {exc}"
        ) from exc
    result = _read_envelope_records(
        transaction.path,
        tolerate_errors=True,
        locked_data=locked_data,
    )
    for error in result.errors:
        _emit_carrier_error(error)
    return list(result.envelopes)


def serialize_envelope_line(envelope: dict) -> str:
    """Canonical single-line JSON bytes for register append (file or MCP)."""
    return validate_envelope(envelope) + "\n"


def _serialize_envelope_for_stream(path: Path, envelope: dict) -> bytes:
    dispatch_id = _require_carrier_path(path)
    serialized = validate_envelope(envelope, expected_dispatch_id=dispatch_id)
    return (serialized + "\n").encode("utf-8")


def _carrier_append_locked(transaction: CarrierTransaction, envelope: dict) -> None:
    transaction.append_bytes(_serialize_envelope_for_stream(transaction.path, envelope))


def _carrier_rewrite_locked(transaction: CarrierTransaction, envelopes: list[dict]) -> None:
    _require_carrier_path(transaction.path)
    data = b"".join(
        _serialize_envelope_for_stream(transaction.path, envelope) for envelope in envelopes
    )
    transaction.replace_bytes(data)


CarrierResult = TypeVar("CarrierResult")


def update_envelopes(
    path: Path,
    update: Callable[[list[dict]], tuple[list[dict] | None, CarrierResult]],
) -> CarrierResult:
    """Own one locked read-modify-write carrier transaction.

    Returning ``None`` as the replacement performs no write.  Replacements use
    a unique fsync'd temporary file and an fsync'd directory replace.
    """
    path = _canonical_jsonl_path(Path(path))
    _require_carrier_path(path)
    with carrier_transaction(path) as transaction:
        existing = _read_envelopes_for_write(transaction)
        replacement, result = update(list(existing))
        if replacement is not None:
            _carrier_rewrite_locked(transaction, replacement)
        return result


def post_message(
    *,
    dispatch_id: str,
    msg_type: str,
    payload: dict,
    messages_dir: Path,
    source: dict | None = None,
    seq: int | None = None,
    priority: str | None = None,
    fleet_dir: Path | None = None,
    update_aggregate: bool = False,
    deliver_to_worker: bool = False,
    retain_terminal_worker_view: bool = False,
    addressee: dict | None = None,
    skip_if: Callable[[dict], bool] | None = None,
    replace_if: Callable[[dict], bool] | None = None,
    event_id: str | None = None,
    event_ts: str | None = None,
) -> dict:
    """Admit one monotonic stream envelope; shared by CLI, MCP, and tests."""
    validate_payload(payload)
    path = inbox_path(messages_dir, dispatch_id)
    _require_carrier_path(path)
    provided_seq = require_positive_int_seq(seq, path="seq") if seq is not None else None
    base_source = {
        "node": "local",
        "adapter": "unknown",
        "transport": "controller",
    }
    if source is not None and not isinstance(source, dict):
        raise MessageError("source must be an object")
    if source:
        base_source.update(source)
    envelope = {
        "schema": "goalflight.message.v1",
        "schema_version": 1,
        "id": event_id or str(uuid.uuid4()),
        "dispatch_id": dispatch_id,
        "seq": provided_seq or 1,
        "ts": event_ts or utc_now(),
        "source": base_source,
        "type": msg_type,
        "priority": priority or PRIORITY_BY_TYPE.get(canonical_event_type(msg_type), "normal"),
        "payload": payload,
    }
    if addressee is not None:
        if not isinstance(addressee, dict):
            raise MessageError("addressee must be an object")
        envelope["addressee"] = dict(addressee)
    # Boundary validation, including canonical serialization, happens before any
    # carrier/ingestion state is touched. The final seq-bearing form is validated
    # and serialized again under the transaction lock.
    validate_envelope(envelope, expected_dispatch_id=dispatch_id)
    with carrier_transaction(path) as transaction:
        existing = _read_envelopes_for_write(transaction)
        same_identity = next(
            (
                item
                for item in existing
                if item.get("id") == envelope["id"]
                and isinstance(item.get("source"), dict)
                and item["source"].get("node") == envelope["source"].get("node")
            ),
            None,
        )
        if same_identity is not None:
            comparable_fields = (
                "schema",
                "schema_version",
                "id",
                "dispatch_id",
                "ts",
                "source",
                "type",
                "priority",
                "payload",
                "addressee",
            )
            if any(same_identity.get(key) != envelope.get(key) for key in comparable_fields):
                raise MessageError(
                    "event identity integrity conflict: same origin_node + event_uuid has different content"
                )
            assignment = _prepare_journal_delivery(same_identity, path)
            _mark_journal_delivery(assignment)
            return {
                "envelope": same_identity,
                "line": serialize_envelope_line(same_identity),
                "path": str(path),
                "recorded": False,
                "delivery": {
                    "requested": False,
                    "delivered": False,
                    "worker_view_written": False,
                    "status": "duplicate",
                    "detail": "matching event identity already exists",
                },
            }
        if skip_if is not None:
            duplicate = next((item for item in existing if skip_if(item)), None)
            if duplicate is not None:
                assignment = _prepare_journal_delivery(duplicate, path)
                _mark_journal_delivery(assignment)
                return {
                    "envelope": duplicate,
                    "line": serialize_envelope_line(duplicate),
                    "path": str(path),
                    "recorded": False,
                    "delivery": {
                        "requested": False,
                        "delivered": False,
                        "worker_view_written": False,
                        "status": "duplicate",
                        "detail": "matching carrier record already exists",
                    },
                }
        resolved_seq = require_positive_int_seq(
            _admit_stream_seq(provided_seq=provided_seq, envelopes=existing),
            path="seq",
        )
        envelope["seq"] = resolved_seq
        validate_envelope(envelope, expected_dispatch_id=dispatch_id)
        envelope[_INGESTION_ORDER_FIELD] = _ingestion_order_for_envelope(
            messages_dir,
            envelope,
        )
        line = serialize_envelope_line(envelope)
        replaced = [item for item in existing if replace_if is not None and replace_if(item)]
        assignment = _prepare_journal_delivery(
            envelope,
            path,
            replaced_envelopes=replaced,
        )
        if replace_if is not None:
            _carrier_rewrite_locked(
                transaction,
                [item for item in existing if item not in replaced] + [envelope],
            )
        else:
            _carrier_append_locked(transaction, envelope)
        _mark_journal_delivery(assignment)
        # The messages lock orders both the canonical record and its materialized
        # worker view. Releasing it between the two writes lets concurrent posts
        # assign message seq 1/2 but append steer entries in the order 2/1.
        delivery = (
            _deliver_message_to_worker(
                dispatch_id,
                envelope,
                retain_terminal_worker_view=retain_terminal_worker_view,
            )
            if deliver_to_worker
            else {
                "requested": False,
                "delivered": False,
                "worker_view_written": False,
                "status": "record_only",
                "detail": "message recorded; worker delivery was not requested",
            }
        )
    if update_aggregate and fleet_dir is not None:
        refresh_aggregate(fleet_dir, messages_dir=messages_dir)
    return {
        "envelope": envelope,
        "line": line,
        "path": str(path),
        "recorded": True,
        "delivery": delivery,
    }


def _dispatch_record(dispatch_id: str) -> tuple[dict | None, str | None]:
    try:
        import goalflight_ledger  # type: ignore
        path = goalflight_ledger.record_path(dispatch_id, create=False)
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except _EXPECTED_OPTIONAL_ERRORS as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(record, dict) or record.get("dispatch_id") != dispatch_id:
        return None, "dispatch record is malformed or bound to a different dispatch id"
    return record, None


def _journal_delivery_targets(envelope: dict) -> tuple[tuple[Path, str, bool], ...]:
    """Return root, recipient, and whether an exact roster choice must stay active."""
    addressee_label = controller_addressee_label(envelope)
    addressee_root = controller_addressee_project_root(envelope)
    if addressee_label and addressee_root:
        try:
            import goalflight_task  # type: ignore

            return (
                (
                    goalflight_task.resolve_project_root(addressee_root),
                    addressee_label,
                    False,
                ),
            )
        except _EXPECTED_OPTIONAL_ERRORS as exc:
            raise MessageError(f"cannot resolve controller delivery project: {exc}") from exc
    record, lookup_error = _dispatch_record(str(envelope.get("dispatch_id") or ""))
    if lookup_error is not None:
        raise MessageError(f"cannot resolve dispatch delivery assignment: {lookup_error}")
    if isinstance(record, dict) and record.get("project_root"):
        label = str(record.get("controller_label") or "").strip()
        # The ledger stores CANONICAL project roots at write time (the single-
        # canonicalizer invariant from the worktree-collapse fix), so the stored
        # value is trusted as-is here. Re-deriving it would spawn git on every
        # delivery-targeting call -- including the steer path, which must never
        # spawn anything (test_dispatch_steer enforces that contract).
        root = Path(str(record["project_root"]))
        if label:
            return ((root, label, False),)
        if canonical_event_type(str(envelope.get("type") or "")) in {
            "result",
            "blocked",
            "user_need",
            "user_confirm",
        }:
            try:
                import goalflight_journal  # type: ignore

                recipients = tuple(
                    str(row["label"])
                    for row in goalflight_journal.Journal.open_reader(root).lease_records()
                    if str(row.get("label") or "").strip()
                )
            except _EXPECTED_OPTIONAL_ERRORS as exc:
                raise MessageError(
                    f"cannot resolve unowned terminal recipients: {exc}"
                ) from exc
            # A terminal transition must remain deliverable even when the
            # controller roster is momentarily empty. The wildcard is the
            # durable orphan recipient observed by a later registration.
            # Exact labels came from a non-transactional roster snapshot.  The
            # assignment write revalidates each one under BEGIN IMMEDIATE and
            # degrades a retired choice to the durable wildcard.
            return tuple((root, recipient, True) for recipient in recipients) or (
                (root, "*", False),
            )
        return ()
    payload = envelope.get("payload")
    if isinstance(payload, dict) and payload.get("project_root"):
        try:
            import goalflight_task  # type: ignore

            return (
                (
                    goalflight_task.resolve_project_root(str(payload["project_root"])),
                    "*",
                    False,
                ),
            )
        except _EXPECTED_OPTIONAL_ERRORS as exc:
            raise MessageError(f"cannot resolve project-scoped delivery: {exc}") from exc
    return ()


def _prepare_journal_delivery(
    envelope: dict,
    path: Path,
    *,
    replaced_envelopes: Iterable[dict] = (),
) -> tuple[dict[str, object], ...]:
    targets = _journal_delivery_targets(envelope)
    if not targets:
        for replaced in replaced_envelopes:
            _withdraw_journal_delivery(replaced, path)
        return ()
    try:
        import goalflight_journal  # type: ignore

        source = envelope.get("source")
        origin_node = str(source.get("node") if isinstance(source, dict) else "local")
        replacement_keys_by_root: dict[Path, list[tuple[str, str, str]]] = {}
        for replaced in replaced_envelopes:
            replaced_source = replaced.get("source")
            replaced_origin = str(
                replaced_source.get("node")
                if isinstance(replaced_source, dict)
                else "local"
            )
            for (
                old_root,
                old_recipient,
                _require_active,
            ) in _journal_delivery_targets(replaced):
                replacement_keys_by_root.setdefault(old_root, []).append(
                    (
                        old_recipient,
                        replaced_origin,
                        str(replaced.get("id") or ""),
                    )
                )
        target_roots = {
            project_root for project_root, _recipient, _require_active in targets
        }
        if any(old_root not in target_roots for old_root in replacement_keys_by_root):
            raise MessageError(
                "journal replacement cannot move an assigned event between projects"
            )
        assignments: list[dict[str, object]] = []
        authorities: dict[Path, object] = {}
        for project_root, recipient_label, require_active_recipient in targets:
            authority = authorities.get(project_root)
            if authority is None:
                authority = goalflight_journal.open_or_create_journal(project_root)
                authorities[project_root] = authority
            recorded = authority.record_delivery_event(
                recipient_label=recipient_label,
                origin_node=origin_node,
                event_uuid=str(envelope.get("id") or ""),
                stream_id=str(envelope.get("dispatch_id") or ""),
                stream_seq=int(envelope.get("seq") or 0),
                carrier_path=path,
                event_type=str(envelope.get("type") or ""),
                wake_class=event_wake_class(
                    str(envelope.get("type") or ""),
                    envelope.get("payload"),
                ),
                created_at=str(envelope.get("ts") or ""),
                replaces=replacement_keys_by_root.get(project_root, ()),
                fallback_to_wildcard_if_inactive=require_active_recipient,
            )
            if not recorded.committed:
                raise MessageError(
                    "journal delivery assignment was not committed: "
                    + str(recorded.reason or recorded.disposition.value)
                )
            actual_recipient = str(
                (recorded.value or {}).get("recipient_label") or recipient_label
            )
            assignments.append(
                {
                    "authority": authority,
                    "recipient_label": actual_recipient,
                    "origin_node": origin_node,
                    "event_uuid": str(envelope["id"]),
                }
            )
        return tuple(assignments)
    except MessageError:
        raise
    except _EXPECTED_OPTIONAL_ERRORS as exc:
        raise MessageError(f"journal delivery assignment failed: {type(exc).__name__}: {exc}") from exc


def _mark_journal_delivery(assignments: Iterable[dict[str, object]]) -> None:
    for assignment in assignments:
        authority = assignment["authority"]
        result = authority.mark_delivery_projected(
            recipient_label=str(assignment["recipient_label"]),
            origin_node=str(assignment["origin_node"]),
            event_uuid=str(assignment["event_uuid"]),
        )
        if not result.committed:
            raise MessageError(
                "journal delivery projection was not committed: "
                + str(result.reason or result.disposition.value)
            )


def _withdraw_journal_delivery(envelope: dict, path: Path) -> None:
    for assignment in _prepare_journal_delivery(envelope, path):
        authority = assignment["authority"]
        result = authority.withdraw_delivery_event(
            recipient_label=str(assignment["recipient_label"]),
            origin_node=str(assignment["origin_node"]),
            event_uuid=str(assignment["event_uuid"]),
        )
        if not result.committed:
            raise MessageError(
                "journal delivery withdrawal was not committed: "
                + str(result.reason or result.disposition.value)
            )


def _deliver_message_to_worker(
    dispatch_id: str,
    envelope: dict,
    *,
    retain_terminal_worker_view: bool = False,
) -> dict:
    record, lookup_error = _dispatch_record(dispatch_id)
    if lookup_error is not None:
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": False,
            "status": "worker_delivery_failed",
            "detail": f"message recorded but dispatch lookup failed: {lookup_error}",
        }
    if record is None:
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": False,
            "status": "recorded_only_no_dispatch",
            "detail": "message recorded; no matching dispatch record, so no worker delivery was attempted",
        }
    try:
        import goalflight_ledger  # type: ignore

        classification = goalflight_ledger.classify(record)
    except _EXPECTED_OPTIONAL_ERRORS as exc:
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": False,
            "status": "worker_delivery_failed",
            "detail": f"message recorded but worker liveness classification failed: {type(exc).__name__}: {exc}",
        }
    # A detached worker intentionally survives controller death. Ledger
    # classification verifies its PID+identity and is authoritative over that
    # otherwise-terminal state label; all other terminal records remain record-only.
    detached_live = classification == "expected_live" and bool(record.get("detached")) and (
        record.get("state") == "controller_dead"
        or (
            record.get("state") == "orphaned"
            and (record.get("reason") or record.get("error")) == "controller_dead"
        )
    )
    if _record_is_terminal(record) and not detached_live:
        state = record.get("terminal_state") or record.get("state") or "terminal"
        if not retain_terminal_worker_view:
            return {
                "requested": True,
                "delivered": False,
                "worker_view_written": False,
                "status": "terminal_recorded_only",
                "dispatch_state": str(state),
                "detail": "message recorded for terminal dispatch; no worker will read it",
            }
        # Legacy `dispatch steer` keeps terminal rows in the view because a
        # restarted ACP session can reuse the dispatch id and must reject stale
        # confirmation replies by sequence/generation rather than lose them.
        try:
            steer_path, steer_entry = goalflight_steer_mailbox.append_message_view(dispatch_id, envelope)
        except (OSError, ValueError, MessageError) as exc:
            return {
                "requested": True,
                "delivered": False,
                "worker_view_written": False,
                "status": "terminal_recorded_only",
                "dispatch_state": str(state),
                "detail": (
                    "message recorded for terminal dispatch; no worker will read it, and its retained "
                    f"worker view could not be written: {type(exc).__name__}: {exc}"
                ),
            }
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": True,
            "status": "terminal_recorded_only",
            "dispatch_state": str(state),
            "steer_path": str(steer_path),
            "steer_seq": steer_entry["seq"],
            "steer_entry": steer_entry,
            "detail": "message recorded for terminal dispatch and retained in its worker view; no current worker will read it",
        }
    if classification not in {"expected_live", "queued_capacity"}:
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": False,
            "status": "worker_unavailable",
            "dispatch_classification": classification,
            "detail": f"message recorded; dispatch is {classification}, so no worker delivery was attempted",
        }
    try:
        steer_path, steer_entry = goalflight_steer_mailbox.append_message_view(dispatch_id, envelope)
    except (OSError, ValueError, MessageError) as exc:
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": False,
            "status": "worker_delivery_failed",
            "detail": f"message recorded but worker delivery failed: {type(exc).__name__}: {exc}",
        }
    if classification == "queued_capacity":
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": True,
            "status": "worker_view_queued",
            "dispatch_classification": classification,
            "steer_path": str(steer_path),
            "steer_seq": steer_entry["seq"],
            "steer_entry": steer_entry,
            "detail": "message recorded and queued in the worker-visible steer mailbox; no worker is running yet",
        }
    return {
        "requested": True,
        "delivered": True,
        "worker_view_written": True,
        "status": "worker_view_written",
        "dispatch_classification": classification,
        "steer_path": str(steer_path),
        "steer_seq": steer_entry["seq"],
        "steer_entry": steer_entry,
        "detail": "message recorded and written to the worker-visible steer mailbox",
    }


def _controller_delivery_requested(dispatch_id: str, msg_type: str) -> bool:
    """A worker posting to its own inbox is worker→controller sideband."""
    return (
        os.environ.get("GOALFLIGHT_DISPATCH_ID") != dispatch_id
        and canonical_event_type(msg_type) in CONTROLLER_CHANNEL_TYPES
    )


def _controller_sender_session_id(dispatch_id: str) -> str | None:
    """Return the declared live controller that authored an outbound steer.

    Missing or ambiguous identity stays ``None``. Wake filtering treats that as
    unknown correspondence and wakes; it must never guess an author and silence
    mail that may have come from another controller.
    """
    record, _classification = _dispatch_record(dispatch_id)
    project_root = (record or {}).get("project_root")
    if not project_root:
        return None
    try:
        import goalflight_session_status  # type: ignore

        label = goalflight_session_status.resolve_controller_label()
        pid = goalflight_session_status.resolve_controller_pid()
        if label is None or pid is None:
            return None
        session = goalflight_session_status.live_session(
            Path(str(project_root)),
            label=label,
            pid=pid,
        )
    except _EXPECTED_OPTIONAL_ERRORS:
        return None
    if not session or session.get("conflicting_beacons") or not session.get("id"):
        return None
    return str(session["id"])


def post_result_is_error(result: dict) -> bool:
    delivery = result["delivery"]
    return bool(
        delivery["requested"]
        and not delivery["delivered"]
        and delivery["status"] not in NON_ERROR_UNDELIVERED_STATUSES
    )


def post_controller_steer(dispatch_id: str, text: str) -> dict:
    """Record a legacy steer command, then materialize its worker-visible view."""
    source = {"node": "local", "adapter": "goalflight-dispatch", "transport": "steer"}
    sender_session_id = _controller_sender_session_id(dispatch_id)
    if sender_session_id is not None:
        source["controller_session_id"] = sender_session_id
    return post_message(
        dispatch_id=dispatch_id,
        msg_type="controller-notice",
        payload={"text": text},
        messages_dir=default_messages_dir(),
        source=source,
        deliver_to_worker=True,
        retain_terminal_worker_view=True,
    )


MCP_TOOL_POST_MESSAGE = "goalflight_post_message"


def goalflight_post_message_tool(
    arguments: dict,
    *,
    messages_dir: Path,
    fleet_dir: Path | None = None,
    refresh_aggregate: bool = False,
) -> dict:
    """MCP tool handler — must write identical bytes as file append."""
    if not isinstance(arguments, dict):
        raise MessageError("arguments must be an object")
    dispatch_id = arguments.get("dispatch_id")
    msg_type = arguments.get("type")
    payload = arguments.get("payload")
    if not dispatch_id or not msg_type:
        raise MessageError("dispatch_id and type are required")
    if payload is None:
        payload = {}
    source = arguments.get("source")
    if source is not None and not isinstance(source, dict):
        raise MessageError("source must be an object when provided")
    return post_message(
        dispatch_id=str(dispatch_id),
        msg_type=str(msg_type),
        payload=payload,
        messages_dir=messages_dir,
        source=source,
        seq=arguments.get("seq"),
        priority=arguments.get("priority"),
        addressee=arguments.get("addressee"),
        fleet_dir=fleet_dir,
        update_aggregate=refresh_aggregate,
        deliver_to_worker=(
            arguments.get("addressee") is None
            and _controller_delivery_requested(str(dispatch_id), str(msg_type))
        ),
    )


def marker_type(marker_kind: str) -> str:
    return MARKER_TO_TYPE.get(marker_kind, "monitor")


def marker_payload(marker_kind: str, text: str) -> dict:
    if marker_kind == "COMPLETE":
        return {"complete": True, "text": text}
    if marker_kind in MARKER_TO_TYPE:
        return {"text": text}
    return {"unknown_marker": marker_kind, "text": text}


def markers_to_envelopes(
    markers: dict[str, list[str]],
    *,
    dispatch_id: str,
    seq_start: int = 1,
    source: dict | None = None,
    ts: str | None = None,
) -> list[dict]:
    """Convert extract_markers() output into goalflight.message.v1 envelopes."""
    base_source = {
        "node": "local",
        "adapter": "unknown",
        "transport": "tail_file",
    }
    if source:
        base_source.update(source)
    envelopes: list[dict] = []
    seq = seq_start
    stamp = ts or utc_now()
    for kind, values in markers.items():
        msg_type = marker_type(kind)
        for value in values:
            envelopes.append(
                {
                    "schema": "goalflight.message.v1",
                    "schema_version": 1,
                    "id": str(uuid.uuid4()),
                    "dispatch_id": dispatch_id,
                    "seq": seq,
                    "ts": stamp,
                    "source": dict(base_source),
                    "type": msg_type,
                    "priority": PRIORITY_BY_TYPE.get(msg_type, "normal"),
                    "payload": marker_payload(kind, value),
                }
            )
            seq += 1
    return envelopes


def markers_text_to_envelopes(text: str, *, dispatch_id: str, **kwargs) -> list[dict]:
    from acp_runner import extract_markers

    return markers_to_envelopes(extract_markers(text), dispatch_id=dispatch_id, **kwargs)


def _dispatch_complete(envelopes: list[dict]) -> bool:
    for env in envelopes:
        if env.get("type") == "result" and env.get("payload", {}).get("complete"):
            return True
    return False


def _event_causally_at_or_after(candidate: dict, event: dict) -> bool:
    """Compare only same-stream sequence or controller-stamped ingestion order."""
    shared_streams = set(inbox_cursor_keys(candidate)) & set(inbox_cursor_keys(event))
    if shared_streams:
        return int(candidate.get("seq", 0)) >= int(event.get("seq", 0))
    candidate_order = candidate.get(_INGESTION_ORDER_FIELD)
    event_order = event.get(_INGESTION_ORDER_FIELD)
    if isinstance(candidate_order, int) and isinstance(event_order, int):
        return candidate_order >= event_order
    return False


def _closed_by_completion(envelope: dict, envelopes: list[dict]) -> bool:
    return any(
        candidate.get("type") == "result"
        and candidate.get("payload", {}).get("complete")
        and _event_causally_at_or_after(candidate, envelope)
        for candidate in envelopes
    )


def _open_user_needs(
    envelopes: list[dict],
) -> list[dict]:
    open_items: list[dict] = []
    for env in envelopes:
        cursor_keys = resolved_envelope_cursor_keys(env)
        if (
            env.get("type") in {"user_need", "user_confirm", "blocked"}
            and not _closed_by_completion(env, envelopes)
        ):
            payload = env.get("payload", {}) or {}
            open_items.append(
                {
                    "dispatch_id": env["dispatch_id"],
                    "seq": env["seq"],
                    "type": env["type"],
                    "nudge_kind": payload.get("nudge_kind"),
                    "ts": env["ts"],
                    "text": payload.get("text", ""),
                    "payload": payload,
                    _INGESTION_ORDER_FIELD: env.get(_INGESTION_ORDER_FIELD),
                    **_cursor_metadata(cursor_keys),
                }
            )
    return open_items


def _open_controller_channel(
    envelopes: list[dict],
    *,
    controller_label: str | None = None,
) -> list[dict]:
    """Controller-addressed messages present in the carrier projection.

    Deliberately NOT gated on _dispatch_complete the way _open_user_needs is: a
    worker's need dies with its dispatch, but a message a peer controller wrote
    is still worth reading after the dispatch that carried it has finished.
    """
    open_items: list[dict] = []
    for env in envelopes:
        cursor_keys = inbox_cursor_keys(env)
        addressee_label = controller_addressee_label(env)
        if addressee_label and controller_label == addressee_label:
            cursor_keys = resolved_envelope_cursor_keys(env)
        if canonical_event_type(str(env.get("type") or "")) in CONTROLLER_CHANNEL_TYPES:
            payload = env.get("payload", {}) or {}
            open_items.append(
                {
                    "dispatch_id": env["dispatch_id"],
                    "seq": env["seq"],
                    "type": env["type"],
                    "nudge_kind": payload.get("nudge_kind"),
                    "ts": env["ts"],
                    "text": payload.get("text", ""),
                    "payload": payload,
                    "addressee": env.get("addressee"),
                    _INGESTION_ORDER_FIELD: env.get(_INGESTION_ORDER_FIELD),
                    **_cursor_metadata(cursor_keys),
                }
            )
    return open_items


def _open_controller_advisories(
    envelopes: list[dict],
) -> list[dict]:
    open_items: list[dict] = []
    for env in envelopes:
        cursor_keys = resolved_envelope_cursor_keys(env)
        if (
            env.get("dispatch_id") == "controller-quota-advisory"
            and env.get("type") == "advisory"
            and not _closed_by_completion(env, envelopes)
        ):
            open_items.append(
                {
                    "dispatch_id": env["dispatch_id"],
                    "seq": env["seq"],
                    "type": env["type"],
                    "ts": env["ts"],
                    "text": env.get("payload", {}).get("text", ""),
                    _INGESTION_ORDER_FIELD: env.get(_INGESTION_ORDER_FIELD),
                    **_cursor_metadata(cursor_keys),
                }
            )
    return open_items


def _last_steering(envelopes_by_dispatch: dict[str, list[dict]]) -> dict | None:
    steering = [
        env
        for envelopes in envelopes_by_dispatch.values()
        for env in envelopes
        if canonical_event_type(str(env.get("type") or "")) == "steering"
    ]
    if not steering:
        return None

    latest = max(steering, key=_event_causal_sort_key)
    return {
        "dispatch_id": latest["dispatch_id"],
        "seq": latest["seq"],
        "ts": latest["ts"],
        "payload": latest.get("payload", {}),
    }


def collect_inbox_paths(
    messages_dir: Path,
    fleet_dir: Path | None = None,
    *,
    dispatch_ids: set[str] | None = None,
) -> list[Path]:
    # Only REGULAR files are inbox candidates. A non-regular `*.jsonl` entry (a
    # FIFO/device, accidental or hostile) would block a later read_text() open()
    # indefinitely — which on the read-side status mail check would HANG status
    # before its fail-open guard could fire. `is_file()` is a non-blocking stat()
    # (open() is what blocks on a FIFO), so this filter is safe and cheap.
    def _want(stem: str) -> bool:
        return dispatch_ids is None or stem in dispatch_ids or stem == "controller-quota-advisory"

    paths: dict[str, list[Path]] = {}
    if messages_dir.is_dir():
        for path in sorted(messages_dir.glob("*.jsonl")):
            if (
                path.is_file()
                and not path.is_symlink()
                and not path.name.endswith(".quarantine.jsonl")
                and STREAM_TOKEN_RE.fullmatch(path.stem)
                and _want(path.stem)
            ):
                paths.setdefault(path.stem, []).append(path.resolve(strict=False))
    if fleet_dir is not None:
        register_dir = fleet_dir / "register" / "dispatches"
        if register_dir.is_dir():
            for path in sorted(register_dir.glob("*.jsonl")):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and not path.name.endswith(".quarantine.jsonl")
                    and STREAM_TOKEN_RE.fullmatch(path.stem)
                    and _want(path.stem)
                ):
                    paths.setdefault(path.stem, []).append(path.resolve(strict=False))
    return [path for dispatch_id in sorted(paths) for path in paths[dispatch_id]]


def inbox_stream_key(path: Path, *, messages_dir: Path) -> str:
    """Stable cursor identity for one independently sequenced inbox stream."""
    canonical = Path(path).resolve(strict=False)
    source = "local" if canonical.parent == Path(messages_dir).resolve(strict=False) else "fleet"
    return json.dumps([source, canonical.stem], ensure_ascii=False, separators=(",", ":"))


def logical_envelopes_for_paths(
    paths: list[Path],
    *,
    messages_dir: Path | None = None,
    tolerate_errors: bool = True,
    envelope_filter: Callable[[dict], bool] | None = None,
    carrier_errors: list[dict[str, object]] | None = None,
) -> list[dict]:
    """Read logical events once while retaining every physical cursor domain."""
    logical: list[dict] = []
    by_identity: dict[str, dict] = {}
    for path in paths:
        inbox_key = (
            inbox_stream_key(path, messages_dir=messages_dir)
            if messages_dir is not None
            else path.stem
        )
        if tolerate_errors:
            envelopes = read_envelopes_tolerant(
                path,
                carrier_errors=carrier_errors,
            )
        else:
            envelopes = read_envelopes(path)
        for envelope in envelopes:
            if envelope_filter is not None and not envelope_filter(envelope):
                continue
            identity = _canonical_envelope_identity(envelope)
            existing = by_identity.get(identity)
            if existing is not None:
                keys = inbox_cursor_keys(existing)
                if inbox_key not in keys:
                    keys.append(inbox_key)
                    existing.update(_cursor_metadata(keys))
                source_paths = existing.setdefault(_INBOX_SOURCE_PATHS_FIELD, [])
                if str(path) not in source_paths:
                    source_paths.append(str(path))
                continue
            annotated = {
                **envelope,
                **_cursor_metadata([inbox_key]),
                _INBOX_SOURCE_PATHS_FIELD: [str(path)],
            }
            by_identity[identity] = annotated
            logical.append(annotated)
    logical.sort(key=_event_causal_sort_key)
    return logical


def format_pending_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "pending counts: none"
    return "pending counts: " + " ".join(
        f"{sanitize_display(key)}={value}" for key, value in sorted(counts.items())
    )


def build_aggregate(
    *,
    messages_dir: Path,
    fleet_dir: Path | None = None,
    dispatch_ids: set[str] | None = None,
    envelope_filter: Callable[[dict], bool] | None = None,
    controller_label: str | None = None,
    include_cursor_keys: bool = False,
) -> dict:
    envelopes_by_dispatch: dict[str, list[dict]] = {}
    paths = collect_inbox_paths(messages_dir, fleet_dir, dispatch_ids=dispatch_ids)
    carrier_errors: list[dict[str, object]] = []
    for envelope in logical_envelopes_for_paths(
        paths,
        messages_dir=messages_dir,
        tolerate_errors=True,
        envelope_filter=envelope_filter,
        carrier_errors=carrier_errors,
    ):
        dispatch_id = str(envelope.get("dispatch_id") or "")
        envelopes_by_dispatch.setdefault(dispatch_id, []).append(envelope)

    open_user_needs: list[dict] = []
    open_advisories: list[dict] = []
    open_controller_channel: list[dict] = []
    active_dispatches: list[str] = []
    for dispatch_id, envelopes in sorted(envelopes_by_dispatch.items()):
        if not envelopes:
            continue
        if not _dispatch_complete(envelopes):
            active_dispatches.append(dispatch_id)
        open_user_needs.extend(_open_user_needs(envelopes))
        open_advisories.extend(_open_controller_advisories(envelopes))
        open_controller_channel.extend(
            _open_controller_channel(
                envelopes,
                controller_label=controller_label,
            )
        )

    if not include_cursor_keys:
        for items in (open_user_needs, open_advisories, open_controller_channel):
            for item in items:
                item.pop(_INBOX_CURSOR_KEY_FIELD, None)
                item.pop(_INBOX_CURSOR_KEYS_FIELD, None)
                item.pop(_INGESTION_ORDER_FIELD, None)

    aggregate = {
        "schema": AGGREGATE_SCHEMA,
        "schema_version": 1,
        "min_reader_version": 1,
        "updated_at": utc_now(),
        "open_user_needs": open_user_needs,
        "open_advisories": open_advisories,
        "open_controller_channel": open_controller_channel,
        "active_dispatches": active_dispatches,
        "last_steering": _last_steering(envelopes_by_dispatch),
    }
    if carrier_errors:
        aggregate["carrier_errors"] = carrier_errors
    return aggregate


def refresh_aggregate(
    fleet_dir: Path,
    *,
    messages_dir: Path | None = None,
) -> dict:
    messages_dir = messages_dir or default_messages_dir()
    aggregate = build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
    out_path = fleet_dir / "register" / "aggregate.json"
    out_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(aggregate, indent=2) + "\n")
    tmp.replace(out_path)
    return aggregate


def cmd_from_text(args: argparse.Namespace) -> int:
    text = Path(args.text_file).read_text() if args.text_file else sys.stdin.read()
    envelopes = markers_text_to_envelopes(
        text,
        dispatch_id=args.dispatch_id,
        source={
            "node": args.node,
            "adapter": args.adapter,
            "transport": args.transport,
        },
    )
    if args.json:
        print(json.dumps(envelopes, indent=2))
    else:
        for env in envelopes:
            print(json.dumps(env))
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(args.payload) if args.payload else {"text": args.text or ""}
    except (ValueError, RecursionError) as exc:
        raise MessageError(f"payload is invalid JSON: {exc}") from exc
    if getattr(args, "subject", None) and isinstance(payload, dict):
        payload.setdefault("subject", args.subject)
    source = {
        "node": args.node,
        "adapter": args.adapter,
        "transport": args.transport,
    }
    addressee = None
    if getattr(args, "to_controller", None):
        addressed_root = getattr(args, "controller_project_root", None) or _current_project_root()
        if addressed_root is None:
            print(
                "post: --to-controller requires --controller-project-root outside a git project",
                file=sys.stderr,
            )
            return 2
        addressee = controller_addressee(
            args.to_controller,
            project_root=Path(addressed_root),
        )
    result = post_message(
        dispatch_id=args.dispatch_id,
        msg_type=args.type,
        payload=payload,
        messages_dir=args.messages_dir,
        source=source,
        addressee=addressee,
        fleet_dir=args.fleet_dir,
        update_aggregate=args.refresh_aggregate,
        deliver_to_worker=(
            addressee is None and _controller_delivery_requested(args.dispatch_id, args.type)
        ),
    )
    print(json.dumps(result, indent=2 if args.json else None))
    delivery = result["delivery"]
    if post_result_is_error(result):
        print(delivery["detail"], file=sys.stderr)
        return 1
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    """Read a carrier for diagnostics; journal delivery state is not mutated."""
    paths = collect_inbox_paths(
        args.messages_dir,
        args.fleet_dir,
        dispatch_ids={str(args.dispatch_id)},
    )
    carrier_errors: list[dict[str, object]] = []
    envelopes = logical_envelopes_for_paths(
        paths,
        messages_dir=args.messages_dir,
        tolerate_errors=True,
        carrier_errors=carrier_errors,
    )
    if args.last is not None and args.last >= 0:
        envelopes = envelopes[-args.last:] if args.last else []
    envelopes = [_without_inbox_metadata(envelope) for envelope in envelopes]
    print(json.dumps(envelopes, indent=2 if args.json else None))
    for error in carrier_errors:
        _emit_carrier_error(error)
    return 1 if carrier_errors else 0


def format_controller_relay(aggregate: dict) -> str | None:
    """One-line summary for orchestrator host when open user_needs exist."""
    needs = aggregate.get("open_user_needs") or []
    if not needs:
        return None
    parts: list[str] = []
    for item in needs:
        dispatch_id = sanitize_display(item.get("dispatch_id") or "?")
        kind = sanitize_display(item.get("type") or "user_need")
        text = sanitize_display(item.get("text") or "", limit=120)
        parts.append(f"[{dispatch_id}] {kind}: {text}")
    return "USER-NEED relay: " + " | ".join(parts)


def format_bounded_relay(
    items: list[dict],
    *,
    item_limit: int = DEFAULT_RELAY_ITEM_LIMIT,
    byte_limit: int = DEFAULT_RELAY_BYTE_LIMIT,
) -> str | None:
    """Newest-first causal relay with a hard UTF-8 output budget."""
    if not items:
        return None
    ordered = list(reversed(items))
    selected: list[str] = []
    total = len(ordered)
    for item in ordered:
        if len(selected) >= item_limit:
            break
        line = (
            f"[{sanitize_display(item.get('dispatch_id') or '', limit=64)}] "
            f"{sanitize_display(item.get('type') or 'user_need', limit=32)}: "
            f"{sanitize_display(item.get('text') or '', limit=200)}"
        )
        candidate = [*selected, line]
        omitted = total - len(candidate)
        if omitted:
            candidate.append(f"(+{omitted} more open item(s) elided)")
        rendered = "\n".join(candidate) + "\n"
        if len(rendered.encode("utf-8")) > byte_limit:
            break
        selected.append(line)
    omitted = total - len(selected)
    if omitted:
        selected.append(f"(+{omitted} more open item(s) elided)")
    return "\n".join(selected)


def _task_store_dispatch_id(
    project_root: Path,
    *,
    canonical_project_root: Path | None = None,
) -> str | None:
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import goalflight_task  # type: ignore

        # Resolve the CANONICAL project root (git common-dir parent) exactly
        # like the nudge WRITER does — a raw worktree path hashes to a
        # different slug and the reader would silently watch the wrong inbox
        # (live consumption-gap regression caught 2026-07-02). Anchored git
        # discovery covers ANY linked worktree, not just managed ones.
        root = (
            Path(canonical_project_root)
            if canonical_project_root is not None
            else _canonical_project_root(project_root)
        )
        return goalflight_task._next_nudge_dispatch_id(root)
    except _EXPECTED_OPTIONAL_ERRORS:
        return None


def _canonical_project_root(project_root: Path) -> Path:
    """Use the one worktree-collapsing project canonicalizer."""
    import goalflight_task  # type: ignore

    return goalflight_task.resolve_project_root(str(project_root))


def _normalize_project_mail_alias(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-")


def _project_mailbox_aliases(
    project_root: Path,
    *,
    canonical_project_root: Path | None = None,
) -> set[str]:
    """Address aliases: basename, safe leading segment, and explicit overrides."""
    root = canonical_project_root or _canonical_project_root(project_root)
    basename = _normalize_project_mail_alias(root.name) or "project"
    aliases = {basename}
    leading = basename.split("-", 1)[0]
    if leading != basename and len(leading) >= MIN_DERIVED_PROJECT_ALIAS_LEN:
        aliases.add(leading)
    for raw in os.environ.get(PROJECT_MAIL_ALIASES_ENV, "").split(","):
        alias = _normalize_project_mail_alias(raw)
        if alias:
            aliases.add(alias)
    return aliases


def _dispatch_id_addresses_project(dispatch_id: str, project_aliases: set[str]) -> bool:
    """Whether an inbox id carries one of this project's address aliases.

    Addressing rule: an alias must be the whole inbox id, its leading
    ``<alias>-`` token, or its trailing ``-<alias>`` token. Aliases comprise the
    canonical repository basename, a derived leading hyphen segment only when
    it is at least four characters, and optional comma-separated
    ``GOALFLIGHT_PROJECT_MAIL_ALIASES`` overrides. Edge matching supports both
    ``pm2-controller-note`` and ``controller-note-pm2`` without treating an
    arbitrary substring as an address. We use inbox ids rather than envelope
    bodies so scoped readers select paths before opening them; this preserves
    the no-cross-project-flood and corrupt-unrelated-inbox guarantees.
    """
    candidate = dispatch_id.casefold()
    aliases = {alias.casefold() for alias in project_aliases if alias}
    return any(
        candidate == alias
        or candidate.startswith(f"{alias}-")
        or candidate.endswith(f"-{alias}")
        for alias in aliases
    )


def _project_addressed_dispatch_ids(
    project_root: Path,
    *,
    messages_dir: Path,
    fleet_dir: Path | None,
    canonical_project_root: Path | None = None,
) -> set[str]:
    aliases = _project_mailbox_aliases(
        project_root,
        canonical_project_root=canonical_project_root,
    )
    return {
        path.stem
        for path in collect_inbox_paths(messages_dir, fleet_dir)
        if _dispatch_id_addresses_project(path.stem, aliases)
    }


def _owned_with_project_mail(
    owned_dispatch_ids: set[str] | None,
    task_store_project_root: Path | None,
    *,
    messages_dir: Path,
    fleet_dir: Path | None,
    canonical_project_root: Path | None = None,
) -> set[str] | None:
    if owned_dispatch_ids is None:
        return owned_dispatch_ids
    scoped = set(owned_dispatch_ids)
    if task_store_project_root is not None:
        dispatch_id = _task_store_dispatch_id(
            task_store_project_root,
            canonical_project_root=canonical_project_root,
        )
        if dispatch_id:
            scoped.add(dispatch_id)
        scoped.update(
            _project_addressed_dispatch_ids(
                task_store_project_root,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
                canonical_project_root=canonical_project_root,
            )
        )
    return scoped


def _current_project_root() -> Path | None:
    """Resolve the current project through the shared canonicalizer."""
    try:
        import goalflight_task  # type: ignore

        return goalflight_task.resolve_project_root(str(Path.cwd()))
    except _EXPECTED_OPTIONAL_ERRORS:
        pass
    return None


def _project_ledger_records(project_root: Path) -> list[dict]:
    try:
        import goalflight_ledger  # type: ignore

        root = str(project_root.resolve())
        return [
            record
            for record in goalflight_ledger.read_records()
            if record.get("project_root") == root
        ]
    except _EXPECTED_OPTIONAL_ERRORS:
        return []


def _verified_controller_identity(
    project_root: Path,
    records: list[dict],
    *,
    owned_dispatch_ids: set[str],
    controller_session_id: str | None = None,
) -> dict[str, object] | None:
    """Resolve the current incarnation, with the durable name as ownership."""
    try:
        import goalflight_session_status as sessions  # type: ignore

        declared_label = sessions.resolve_controller_label()
        declared_pid = sessions.resolve_controller_pid()
        if declared_label is not None or declared_pid is not None:
            if declared_label is None:
                return None
            live = sessions.live_session(
                project_root,
                label=declared_label,
                pid=declared_pid,
            )
            if (
                not isinstance(live, dict)
                or live.get("conflicting_beacons")
                or not live.get("id")
                or (controller_session_id and str(live.get("id")) != controller_session_id)
            ):
                return None
            return {
                "label": declared_label,
                "session_id": str(live["id"]),
                "pid": live.get("pid"),
            }
        label: str | None = None
        if controller_session_id:
            registry = next(
                (
                    record
                    for record in sessions._registered_controller_records(project_root)
                    if str(record.get("id") or "") == str(controller_session_id)
                ),
                None,
            )
            if registry is not None:
                label = str(registry.get("label") or "") or None
        if label is None:
            label = _controller_label_for_owned_dispatches(records, owned_dispatch_ids)
        if label is None:
            return None
        live = sessions.live_session(project_root, label=label)
        if (
            not isinstance(live, dict)
            or live.get("conflicting_beacons")
            or not live.get("id")
            or (
                controller_session_id
                and str(live.get("id") or "") != str(controller_session_id)
            )
        ):
            return None
        return {
            "label": label,
            "session_id": str(live["id"]),
            "pid": live.get("pid"),
        }
    except _EXPECTED_OPTIONAL_ERRORS:
        return None


def _controller_label_for_owned_dispatches(
    records: list[dict],
    owned_dispatch_ids: set[str],
) -> str | None:
    """Resolve one durable owner name even across incarnation succession."""
    if not owned_dispatch_ids:
        return None
    recorded_ids: set[str] = set()
    labels: set[str] = set()
    for record in records:
        dispatch_id = str(record.get("dispatch_id") or "")
        if dispatch_id not in owned_dispatch_ids:
            continue
        recorded_ids.add(dispatch_id)
        label = str(record.get("controller_label") or "").strip()
        if not label:
            return None
        labels.add(label)
    if recorded_ids != owned_dispatch_ids or len(labels) != 1:
        return None
    return next(iter(labels))


def _controller_scope_kind(
    envelope: dict,
    *,
    owned_dispatch_ids: set[str],
    legacy_addressed_dispatch_ids: set[str],
    task_store_dispatch_id: str | None,
    controller_label: str | None,
    controller_project_root: str,
) -> str | None:
    """Single authority for whether one envelope belongs to this controller."""
    dispatch_id = str(envelope.get("dispatch_id") or "")
    addressee_label = controller_addressee_label(envelope)
    if addressee_label is not None:
        addressee_root = controller_addressee_project_root(envelope)
        if (
            controller_label is not None
            and addressee_label == controller_label
            and addressee_root == controller_project_root
        ):
            return "controller"
        return None
    if dispatch_id in owned_dispatch_ids:
        return "worker"
    if dispatch_id == task_store_dispatch_id:
        return "task-store"
    if dispatch_id in legacy_addressed_dispatch_ids:
        return "legacy-controller"
    return None


def _controller_scope_inputs(
    project_root: Path,
    *,
    records: list[dict],
    owned_dispatch_ids: set[str],
    controller_session_id: str | None,
    messages_dir: Path,
    fleet_dir: Path | None,
    canonical_project_root: Path | None = None,
) -> dict[str, object]:
    canonical_project_root = canonical_project_root or _canonical_project_root(project_root)
    known_dispatch_ids = {
        str(record["dispatch_id"])
        for record in records
        if record.get("dispatch_id")
    }
    legacy_addressed_dispatch_ids = _project_addressed_dispatch_ids(
        project_root,
        messages_dir=messages_dir,
        fleet_dir=fleet_dir,
        canonical_project_root=canonical_project_root,
    ) - known_dispatch_ids
    identity = _verified_controller_identity(
        project_root,
        records,
        owned_dispatch_ids=owned_dispatch_ids,
        controller_session_id=controller_session_id,
    )
    owned_label = _controller_label_for_owned_dispatches(records, owned_dispatch_ids)
    if identity is not None and owned_label is not None and identity.get("label") != owned_label:
        identity = None
    return {
        "legacy_addressed_dispatch_ids": legacy_addressed_dispatch_ids,
        "task_store_dispatch_id": _task_store_dispatch_id(
            project_root,
            canonical_project_root=canonical_project_root,
        ),
        "controller_label": str(identity.get("label")) if identity else None,
        "controller_session_id": str(identity.get("session_id")) if identity else controller_session_id,
        "controller_project_root": str(canonical_project_root),
    }


def controller_pending_events(
    *,
    project_root: Path | str,
    controller_label: str,
    dispatch_ids: set[str] | None = None,
    waking_only: bool = True,
    limit: int = 1000,
) -> list[dict[str, object]]:
    """Read journal-authoritative pending assignments without advancing a cursor."""
    try:
        import goalflight_journal  # type: ignore
        import goalflight_task  # type: ignore

        root = goalflight_task.resolve_project_root(str(project_root))
        authority = goalflight_journal.Journal(root)
        return authority.pending_delivery_events(
            controller_label,
            waking_only=waking_only,
            stream_ids=dispatch_ids,
            limit=limit,
        )
    except goalflight_journal.JournalUnavailable:
        return []


def controller_cursor_peek(
    *,
    project_root: Path | str,
    controller_label: str,
    lease_nonce: str | None = None,
    limit: int,
):
    import goalflight_journal  # type: ignore
    import goalflight_task  # type: ignore

    root = goalflight_task.resolve_project_root(str(project_root))
    return goalflight_journal.Journal(root).cursor_peek(
        controller_label,
        nonce=lease_nonce,
        limit=limit,
    )


def _listener_envelope(authority, row: dict[str, object]) -> dict:
    carrier_path = str(row.get("carrier_path") or "")
    # Synthetic journal carriers ("journal:attention:", "journal:outbox-quarantine:",
    # …) have no .jsonl file; their payload lives in system_attention_items keyed by
    # event_uuid, inserted in the same transaction as the delivery event. Match on
    # the "journal:" prefix so a new synthetic stream cannot wedge the whole relay.
    if carrier_path.startswith("journal:"):
        item_id = str(row.get("event_uuid") or "")
        item = next(
            (value for value in authority.attention_items() if value.get("item_id") == item_id),
            None,
        )
        if item is None:
            raise MessageError("journal attention delivery points to a missing item")
        payload = json.loads(str(item["payload_json"]))
        return {
            "schema": "goalflight.message.v1",
            "schema_version": 1,
            "id": item_id,
            "dispatch_id": "attention",
            "seq": int(row["stream_seq"]),
            "ts": str(item["created_at"]),
            "source": {"node": "journal", "adapter": "lease-attention", "transport": "journal"},
            "type": "controller_attention",
            "priority": "critical",
            "payload": payload,
        }
    path = Path(carrier_path)
    result = read_envelopes_result(path)
    if result.status is not CarrierReadStatus.OK:
        raise MessageError(f"carrier is corrupt or unreadable: {path}")
    origin_node = str(row.get("origin_node") or "")
    event_uuid = str(row.get("event_uuid") or "")
    stream_seq = int(row.get("stream_seq") or 0)
    envelope = next(
        (
            item
            for item in result.envelopes
            if item.get("id") == event_uuid
            and int(item.get("seq") or 0) == stream_seq
            and isinstance(item.get("source"), dict)
            and str(item["source"].get("node") or "") == origin_node
        ),
        None,
    )
    if envelope is None:
        raise MessageError(
            f"journal delivery assignment has no projected carrier row: {path}:{stream_seq}"
        )
    return envelope


def _project_dispatch_ids(project_root: Path) -> set[str]:
    return {
        str(record["dispatch_id"])
        for record in _project_ledger_records(project_root)
        if record.get("dispatch_id")
    }


def _current_frontier_ids(project_root: Path) -> list[str] | None:
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import goalflight_task  # type: ignore

        return [str(row["id"]) for row in goalflight_task.TaskStore(Path(project_root)).next_frontier()]
    except _EXPECTED_OPTIONAL_ERRORS:
        return None


def _task_items_by_id(project_root: Path) -> dict[str, dict] | None:
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import goalflight_task  # type: ignore

        return {str(row.get("id") or ""): row for row in goalflight_task.list(project_root=Path(project_root))}
    except _EXPECTED_OPTIONAL_ERRORS:
        return None


def _done_reviewed(row: dict | None) -> bool:
    if not row:
        return True
    return row.get("done_reviewed") is True or (row.get("kind") == "decision" and row.get("done") is True)


def _record_task_ids(record: dict) -> list[str]:
    values = record.get("task_ids")
    raw = values if isinstance(values, list) else [values]
    task_ids: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            continue
        for part in value.split(","):
            task_id = part.strip()
            if task_id and task_id not in task_ids:
                task_ids.append(task_id)
    return task_ids


def _record_time(record: dict) -> dt.datetime:
    for key in ("started_at", "updated_at", "ended_at"):
        value = record.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def _record_is_terminal(record: dict) -> bool:
    try:
        import goalflight_dispatch_states as dispatch_states  # type: ignore

        return any(
            dispatch_states.is_terminal_state(record.get(key))
            for key in ("state", "terminal_state")
        )
    except _EXPECTED_OPTIONAL_ERRORS:
        return False


def _stale_dispatch_ids(project_root: Path, open_dispatch_ids: set[str]) -> set[str]:
    """Terminal escalations stale by task lifecycle or task-less expiry."""
    records = _project_ledger_records(project_root)
    rows_by_id = _task_items_by_id(project_root) or {}
    now = dt.datetime.now(dt.timezone.utc)
    unknown_time = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    stale: set[str] = set()
    for record in records:
        dispatch_id = str(record.get("dispatch_id") or "")
        task_ids = _record_task_ids(record)
        if (
            not dispatch_id
            or dispatch_id not in open_dispatch_ids
            or not _record_is_terminal(record)
        ):
            continue
        record_time = _record_time(record)
        if not task_ids:
            if (
                record_time != unknown_time
                and record_time < now - TASKLESS_TERMINAL_STALE_AFTER
            ):
                stale.add(dispatch_id)
            continue
        superseded = {
            task_id
            for other in records
            if str(other.get("dispatch_id") or "") != dispatch_id
            and _record_time(other) > record_time
            for task_id in _record_task_ids(other)
        }
        if all(
            bool((rows_by_id.get(task_id) or {}).get("done")) or task_id in superseded
            for task_id in task_ids
        ):
            stale.add(dispatch_id)
    return stale


def _task_store_nudge_is_current(item: dict, project_root: Path) -> bool:
    payload = item.get("payload") or {}
    nudge_kind = str(payload.get("nudge_kind") or "")
    if nudge_kind == "done-suggest":
        task_ids = [str(value) for value in (payload.get("task_ids") or [])]
        if not task_ids:
            return True
        rows_by_id = _task_items_by_id(project_root)
        if rows_by_id is None:
            return True
        return any(not _done_reviewed(rows_by_id.get(task_id)) for task_id in task_ids)
    if nudge_kind not in {"parallel-ready", "resume-ready"}:
        return True
    frontier_ids = _current_frontier_ids(project_root)
    if frontier_ids is None:
        return True
    payload_ids = [str(value) for value in (payload.get("frontier_ids") or [])]
    if not payload_ids:
        return True
    if nudge_kind == "parallel-ready":
        return len(frontier_ids) >= 2 and frontier_ids == payload_ids
    return bool(frontier_ids) and frontier_ids == payload_ids


def _filter_task_store_nudges(
    items: list[dict],
    task_store_project_root: Path | None,
    *,
    canonical_project_root: Path | None = None,
) -> list[dict]:
    if task_store_project_root is None:
        return items
    dispatch_id = _task_store_dispatch_id(
        task_store_project_root,
        canonical_project_root=canonical_project_root,
    )
    if not dispatch_id:
        return items
    return [
        item
        for item in items
        if str(item.get("dispatch_id") or "") != dispatch_id
        or _task_store_nudge_is_current(
            item,
            canonical_project_root or task_store_project_root,
        )
    ]


def format_mail_notice(count: int) -> str:
    """One body-free notice shared by every controller entry point."""
    return f"{count} new mail; peek: goalflight_messages.py relay --new"


def dispatch_mail_watermark(
    dispatch_ids: set[str] | list[str] | tuple[str, ...],
    *,
    messages_dir: Path | None = None,
) -> set[tuple[str, str]]:
    """Read waking event identities from only the requested dispatch carriers.

    Unclaimed fixed-set waits have no controller label, so their mail cannot be
    assigned to a controller journal cursor.  Their exact dispatch carriers are
    nevertheless authoritative for mail addressed to those waited ids.  This
    fallback deliberately performs no directory enumeration, cursor mutation,
    lock creation, or corruption-quarantine write.
    """
    root = messages_dir or default_messages_dir()
    identities: set[tuple[str, str]] = set()
    for dispatch_id in dict.fromkeys(str(value) for value in dispatch_ids):
        path = inbox_path(root, dispatch_id)
        data = _read_nofollow_bytes(path)
        for line_no, raw_line in enumerate(data.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                envelope = json.loads(raw_line.decode("utf-8"))
                validate_envelope(
                    envelope,
                    path=f"{path}:{line_no}",
                    expected_dispatch_id=dispatch_id,
                )
            except (UnicodeDecodeError, ValueError, RecursionError, MessageError) as exc:
                raise MessageError(f"{path}:{line_no}: invalid waited mail carrier: {exc}") from exc
            if event_wake_class(
                str(envelope.get("type") or ""),
                envelope.get("payload"),
            ) != "waking":
                continue
            source = envelope.get("source")
            origin = str(source.get("node") if isinstance(source, dict) else "local")
            identities.add((origin, str(envelope.get("id") or "")))
    return identities


def controller_mail_summary(
    *,
    owned_dispatch_ids: set[str] | None = None,
    task_store_project_root: Path | None = None,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
    controller_label: str | None = None,
) -> dict:
    """Summarize journal-pending assignments without advancing the cursor."""
    del owned_dispatch_ids, messages_dir, fleet_dir
    if task_store_project_root is None:
        return {}
    try:
        import goalflight_journal  # type: ignore
        import goalflight_session_status as sessions  # type: ignore
        import goalflight_task  # type: ignore

        root = goalflight_task.resolve_project_root(str(task_store_project_root))
        authority = goalflight_journal.Journal.open_reader(root)
        label = sessions.resolve_controller_label(
            controller_label,
            project_root=root,
        )
        if label is None:
            active = authority.lease_records()
            label = str(active[0]["label"]) if len(active) == 1 else None
        if label is None:
            return {}
        rows = authority.pending_delivery_events(label, waking_only=True, limit=1000)
    except (goalflight_journal.JournalUnavailable, ValueError):
        return {}

    items: list[dict[str, object]] = []
    carrier_errors: list[dict[str, object]] = []
    for row in rows:
        try:
            envelope = _listener_envelope(authority, row)
        except MessageError as exc:
            carrier_errors.append({"error": str(exc), "carrier_path": row.get("carrier_path")})
            continue
        payload = envelope.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        items.append(
            {
                "dispatch_id": str(envelope.get("dispatch_id") or row.get("stream_id") or "?"),
                "type": str(payload.get("nudge_kind") or envelope.get("type") or "event"),
                "seq": envelope.get("seq"),
                "ts": envelope.get("ts"),
                "text": sanitize_display(payload.get("text") or payload.get("reason") or "", limit=120),
                "addressee": envelope.get("addressee"),
            }
        )
    if not items and not carrier_errors:
        return {"count": 0, "needs": [], "controller_label": label}
    result: dict[str, object] = {
        "count": len(items),
        "needs": items,
        "hint": (
            format_mail_notice(len(items))
            if items
            else f"WARNING: {len(carrier_errors)} corrupt mail carrier(s)"
        ),
        "controller_label": label,
    }
    if carrier_errors:
        result["carrier_errors"] = carrier_errors
    return result


def listener_coverage_status(
    project_root: Path | str,
    controller_label: str,
    *,
    identity_probe: Callable[[int], dict | None] | None = None,
) -> dict[str, object]:
    """Return live wake coverage from the kernel-held waiter ledger."""
    import goalflight_task  # type: ignore

    root = goalflight_task.resolve_project_root(str(project_root))
    label = str(controller_label or "").strip()
    del identity_probe  # compatibility-only; lock state replaces process probing.
    return goalflight_wake.coverage_status(root, controller_label=label or None)


def listener_reminder_line(project_root: Path | str, controller_label: str) -> str:
    command = goalflight_wake.listener_start_command(
        _canonical_project_root(Path(project_root)),
        controller_label=controller_label,
    )
    return f"listener offline; start: {command}"


def _ambient_claimed_controller(
    project_root: Path,
    *,
    controller_label: str | None,
    mail_bearing: bool,
    require_live_holder: bool = True,
) -> dict[str, object]:
    """Resolve a capability-matched ambient controller without mutating state."""
    if not mail_bearing:
        return {"claimed": False, "reason": "not-mail-bearing"}
    role = str(os.environ.get("GOALFLIGHT_PROCESS_ROLE") or "controller").strip()
    if role != "controller":
        return {"claimed": False, "reason": "non-controller-role", "role": role}
    if str(os.environ.get("GOALFLIGHT_DISPATCH_ID") or "").strip():
        return {"claimed": False, "reason": "worker-dispatch"}

    carried_capabilities = {
        value
        for value in (
            str(os.environ.get("GOALFLIGHT_CONTROLLER_LEASE_NONCE") or "").strip(),
            str(os.environ.get("GOALFLIGHT_CONTROLLER_SESSION_ID") or "").strip(),
        )
        if value
    }
    if len(carried_capabilities) != 1:
        reason = (
            "missing-controller-capability"
            if not carried_capabilities
            else "conflicting-controller-capabilities"
        )
        return {"claimed": False, "reason": reason}
    capability = next(iter(carried_capabilities))

    # Keep the overwhelmingly common one-shot CLI path import-free.  A process
    # without a carried capability cannot be an ambient claimed controller and
    # must not pay journal/session startup cost merely to reach that conclusion.
    import goalflight_journal  # type: ignore
    import goalflight_session_status as sessions  # type: ignore

    label = sessions.resolve_controller_label(
        controller_label,
        project_root=project_root,
    )
    if not label:
        return {"claimed": False, "reason": "missing-controller-label"}
    try:
        authority = goalflight_journal.Journal.open_reader(project_root)
        lease = authority.active_lease(label)
    except goalflight_journal.JournalError:
        return {"claimed": False, "reason": "journal-unavailable", "label": label}
    if lease is None:
        return {"claimed": False, "reason": "no-active-controller-lease", "label": label}
    if lease.nonce != capability:
        return {"claimed": False, "reason": "controller-capability-mismatch", "label": label}
    holder_alive = goalflight_wake.lease_holder_alive(
        project_root,
        controller_label=label,
        lease_nonce=lease.nonce,
    )
    if require_live_holder and holder_alive is not True:
        reason = (
            "controller-lease-holder-dead"
            if holder_alive is False
            else "controller-lease-holder-unknown"
        )
        return {"claimed": False, "reason": reason, "label": label}
    return {
        "claimed": True,
        "reason": "ambient-controller-lease",
        "label": label,
        "lease_generation": lease.generation,
        "holder_alive": holder_alive,
    }


def emit_wake_entry_notice(
    *,
    project_root: Path | str,
    controller_label: str | None = None,
    owned_dispatch_ids: set[str] | None = None,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
    mail_bearing: bool = True,
    stream=None,
) -> dict[str, object]:
    """Bounded no-listener mail poll for one ambient claimed controller."""
    import goalflight_task  # type: ignore

    root = goalflight_task.resolve_project_root(str(project_root))
    ambient = _ambient_claimed_controller(
        root,
        controller_label=controller_label,
        mail_bearing=mail_bearing,
        require_live_holder=False,
    )
    label = str(ambient.get("label") or "").strip() or None

    def pending_probe() -> str | None:
        summary = controller_mail_summary(
            owned_dispatch_ids=owned_dispatch_ids,
            task_store_project_root=root,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            controller_label=label,
        )
        count = int(summary.get("count") or 0)
        return format_mail_notice(count) if count else None

    return goalflight_wake.check_tool_entry(
        root,
        controller_label=label,
        controller_claimed=ambient.get("claimed") is True,
        mail_bearing=mail_bearing,
        pending_probe=pending_probe,
        stream=stream,
    )


def emit_listener_reminder(
    *,
    project_root: Path | str | None,
    controller_label: str | None,
    exposure: int,
    stream=None,
    identity_probe: Callable[[int], dict | None] | None = None,
) -> str | None:
    """Warn a controller that nothing will wake it, when that actually costs it.

    ``exposure`` is what it stands to miss -- open dispatches plus pending mail.
    Gating on it is what keeps this from becoming a nag: a controller with
    nothing in flight loses nothing by having no listener, so it is not told to
    start one. That also removes any need for a throttle timestamp, which would
    be state to keep and to get wrong.

    TWO faults are reported, not one, because an unresolvable identity is itself
    the more serious state and is common in practice. Measured on this project
    while building this: all 26 owned dispatches carried no controller label and
    neither registered label had a live session, so the caller could not be named
    at all. Reporting only "you have no listener" would have stayed silent for
    precisely the controller that had none -- an unregistered controller is also
    the one nothing can be attributed to, it is invisible to peer discovery, and
    no listener can be matched to it. So:

      * identity known, no live listener -> start a listener
      * identity unknown                 -> register first; the listener cannot
                                            be attributed until you have a name
    """
    try:
        if project_root is None or exposure < 1:
            return None
        root = _canonical_project_root(Path(project_root))
        label = str(controller_label or "").strip()
        if not label:
            line = (
                "this controller is not registered for "
                f"{root}: peers cannot discover it, its dispatches are recorded "
                "with no owner, and no mail listener can be attributed to it. "
                "register with: python3 "
                f"{Path(__file__).resolve().parent / 'goalflight_session_status.py'} "
                "--controller-startup --controller-pid-from-ancestry"
            )
        else:
            coverage = listener_coverage_status(
                root,
                label,
                identity_probe=identity_probe,
            )
            if coverage.get("covered"):
                return None
            line = listener_reminder_line(root, label)
        print(line, file=sys.stderr if stream is None else stream)
        return line
    except _EXPECTED_OPTIONAL_ERRORS:
        return None


def emit_controller_mail_notice(
    *,
    owned_dispatch_ids: set[str] | None = None,
    project_root: Path | None = None,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
    stream=None,
) -> str | None:
    """Compute and print the shared one-line mail notice, always fail-open."""
    try:
        resolved_owned_dispatch_ids = owned_dispatch_ids
        if resolved_owned_dispatch_ids is None:
            resolved_owned_dispatch_ids = (
                _project_dispatch_ids(_canonical_project_root(project_root))
                if project_root is not None
                else set()
            )
        summary = controller_mail_summary(
            owned_dispatch_ids=resolved_owned_dispatch_ids,
            task_store_project_root=project_root,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        for error in summary.get("carrier_errors") or []:
            _emit_carrier_error(error, stream=stream)
        count = int(summary.get("count") or 0)
        if count < 1:
            return None
        notice = format_mail_notice(count)
        # STDERR by default: several callers' stdout is a DATA CONTRACT that
        # other tooling parses (goalflight_task.py `next` yields the task list;
        # `--json` modes emit documents). An advisory line on stdout corrupts
        # those consumers - test_next_frontier caught exactly that. Notices are
        # advice; stdout is data.
        print(notice, file=sys.stderr if stream is None else stream)
        return notice
    except _EXPECTED_OPTIONAL_ERRORS:
        return None


def emit_controller_milestone_notice(
    *,
    project_root: Path | None = None,
    stream=None,
) -> str | None:
    """Print a one-line milestone-sweep notice, but ONLY when a sweep is due.

    Mail gets acted on and milestone sweeps do not, and the reason is reach, not
    discipline: the mail notice is emitted from doctor/gate/messages/status/usage,
    while the milestone signal appeared in `status` alone. A controller running a
    gate or a usage check -- most of a run -- was never told a sweep had come due.

    So it rides the same carriers as mail. It stays silent unless `due`, because a
    line printed every time is a line nobody reads, and it goes to stderr for the
    same reason the mail notice does: several callers' stdout is a data contract.
    Fail-open throughout -- a monitoring nicety must never break a dispatch.
    """
    try:
        import goalflight_milestone

        status = goalflight_milestone.check_status(
            project_root=project_root or _current_project_root() or Path.cwd()
        )
        if not status.get("due"):
            return None
        line = goalflight_milestone.format_line(status)
        notice = f"{line}  # milestone sweep due: see protocols/milestone-review.md"
        print(notice, file=sys.stderr if stream is None else stream)
        return notice
    except _EXPECTED_OPTIONAL_ERRORS:
        return None


# Old pending bodies can dominate a repeated diagnostic peek. Anything older
# than this degrades to a headline even under --bodies.
STALE_BODY_AGE_S = 2 * 24 * 3600


def _envelope_age_s(envelope: dict, *, now: float | None = None) -> float | None:
    """Seconds since the envelope was posted, or None when unparseable."""
    ts = envelope.get("ts") if isinstance(envelope, dict) else None
    if not isinstance(ts, str) or not ts:
        return None
    try:
        posted = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=dt.timezone.utc)
    current = (
        dt.datetime.now(dt.timezone.utc)
        if now is None
        else dt.datetime.fromtimestamp(now, dt.timezone.utc)
    )
    return (current - posted).total_seconds()


def split_fresh_and_stale(
    envelopes: list, *, max_age_s: float = STALE_BODY_AGE_S, now: float | None = None
) -> tuple[list, list]:
    """Partition envelopes into (fresh enough for bodies, headline-only).

    An envelope with an unreadable timestamp is treated as FRESH: withholding a
    body we cannot date would silently hide new mail, which is the worse error.
    """
    fresh, stale = [], []
    for env in envelopes:
        if not isinstance(env, dict):
            continue
        age = _envelope_age_s(env, now=now)
        (stale if age is not None and age > max_age_s else fresh).append(env)
    return fresh, stale


HEADLINE_MAX = 96


def sanitize_display(value: object, *, limit: int | None = None) -> str:
    """Render and optionally truncate untrusted mail text for human display."""
    text = value if isinstance(value, str) else str(value)
    safe: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char in {"\r", "\n"} or codepoint in {0x85, 0x2028, 0x2029}:
            safe.append(" ")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            safe.append(f"\\x{codepoint:02x}")
        else:
            safe.append(char)
    rendered = " ".join("".join(safe).split())
    if limit is None or len(rendered) <= limit:
        return rendered
    if limit <= 0:
        return ""
    if limit <= 3:
        return "." * limit
    return rendered[: limit - 3] + "..."


def envelope_headline(envelope: dict) -> str:
    """One scannable line for an envelope.

    Mail is read by agents whose context is the scarce resource, so the default
    listing must be scannable rather than complete: an explicit subject when the
    sender set one, otherwise the first meaningful line of the body. Bodies are
    fetched deliberately, never dumped by default.
    """
    payload = envelope.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    subject = payload.get("subject")
    if isinstance(subject, str) and subject.strip():
        text = subject
    else:
        body = payload.get("text")
        body = body if isinstance(body, str) else ""
        # No subject: the first two meaningful lines usually carry the sender's
        # own headline, which beats one truncated line of a long opener.
        meaningful = []
        for line in body.splitlines():
            cleaned = sanitize_display(line)
            if cleaned:
                meaningful.append(cleaned)
        text = " / ".join(meaningful[:2])
    return sanitize_display(text, limit=HEADLINE_MAX) or "(no text)"


def envelope_from(envelope: dict) -> str:
    """Who sent it: the source node/adapter when recorded, else the inbox id."""
    source = envelope.get("source")
    source = source if isinstance(source, dict) else {}
    for key in ("adapter", "node"):
        value = source.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != "unknown":
            return sanitize_display(value)
    return sanitize_display(envelope.get("dispatch_id") or "?")


def format_envelope_headlines(envelopes: list) -> str:
    """Render unseen envelopes as one line each: FROM, subject, and body size."""
    lines = []
    for env in envelopes:
        if not isinstance(env, dict):
            continue
        payload = env.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        body = payload.get("text")
        size = len(body) if isinstance(body, str) else 0
        lines.append(
            f"{sanitize_display(env.get('dispatch_id', '?'))} "
            f"#{sanitize_display(env.get('seq', '?'))} "
            f"[{sanitize_display(env.get('type', '?'))}] from {envelope_from(env)}: "
            f"{envelope_headline(env)}  ({size}c)"
        )
    return "\n".join(lines)


def _cursor_positions(rows: list[dict] | tuple[dict, ...]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for row in rows:
        stream_id = str(row.get("stream_id") or "")
        if stream_id:
            positions[stream_id] = max(
                positions.get(stream_id, 0), int(row.get("stream_seq") or 0)
            )
    return positions


def _cursor_advance_command(
    *,
    project_root: Path | str,
    controller_label: str,
    lease_nonce: str,
    cursor_version: int,
    positions: dict[str, int],
    stream_snapshots: dict[str, str],
) -> str | None:
    """Return the exact per-stream snapshot-bound command, safely shell quoted."""
    if not positions:
        return None
    if stream_snapshots.keys() != positions.keys():
        raise ValueError("cursor positions and stream snapshots must name the same streams")
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "advance",
        "--project-root",
        str(project_root),
        "--controller-label",
        str(controller_label),
        "--lease-nonce",
        str(lease_nonce),
        "--cursor-version",
        str(cursor_version),
        "--json",
        "--stream-snapshot",
        *(f"{stream}={stream_snapshots[stream]}" for stream in sorted(stream_snapshots)),
        "--position",
        *(f"{stream}={positions[stream]}" for stream in sorted(positions)),
    ]
    return shlex.join(argv)


def cmd_relay(args: argparse.Namespace) -> int:
    """Peek at journal-pending assignments, optionally draining that snapshot."""
    drain = bool(getattr(args, "drain", False))
    project_root = _current_project_root()
    if project_root is None:
        print("relay: no current git project", file=sys.stderr)
        return 2
    try:
        import goalflight_journal  # type: ignore
        import goalflight_session_status as sessions  # type: ignore
        import goalflight_task  # type: ignore

        root = goalflight_task.resolve_project_root(str(project_root))
        authority = goalflight_journal.Journal(root)
        controller_label = sessions.resolve_controller_label(project_root=root)
        if controller_label is None:
            active = authority.lease_records()
            controller_label = str(active[0]["label"]) if len(active) == 1 else None
        if controller_label is None:
            if drain:
                if getattr(args, "json", False):
                    print(json.dumps({"drained": 0, "status": "no_mail"}, sort_keys=True))
                else:
                    print("no mail")
            else:
                print("no pending journal events")
            return 0
        lease = authority.active_lease(controller_label)
        if lease is None:
            raise MessageError("active controller lease is unavailable")
        peek = authority.cursor_peek(controller_label, nonce=lease.nonce, limit=1000)
        rows = list(peek.items)
        envelopes = [_listener_envelope(authority, row) for row in rows]
    except (goalflight_journal.JournalUnavailable, MessageError, ValueError) as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 2
    positions = _cursor_positions(rows)
    advance_command = _cursor_advance_command(
        project_root=root,
        controller_label=controller_label,
        lease_nonce=lease.nonce,
        cursor_version=peek.cursor_version,
        positions=positions,
        stream_snapshots=peek.stream_snapshots,
    )
    if drain:
        if not positions:
            if getattr(args, "json", False):
                print(
                    json.dumps(
                        {
                            "controller_label": controller_label,
                            "cursor_version": peek.cursor_version,
                            "drained": 0,
                            "status": "no_mail",
                        },
                        sort_keys=True,
                    )
                )
            else:
                print("no mail")
            return 0
        try:
            advanced = authority.advance_cursor(
                controller_label,
                nonce=lease.nonce,
                expected_cursor_version=peek.cursor_version,
                expected_stream_snapshots=peek.stream_snapshots,
                advances=positions,
                actor=f"controller:{os.getpid()}:relay-drain",
            )
        except goalflight_journal.CASMismatch as exc:
            advanced = None
            conflict_reason = str(exc)
        except goalflight_journal.JournalError as exc:
            print(f"relay: {exc}", file=sys.stderr)
            return 2
        else:
            conflict_reason = advanced.reason if not advanced.committed else None
        if advanced is None or not advanced.committed or advanced.value is None:
            if getattr(args, "json", False):
                print(
                    json.dumps(
                        {
                            "drained": 0,
                            "error": sanitize_display(
                                conflict_reason or "cursor CAS lost",
                                limit=240,
                            ),
                            "next": "retry relay --drain",
                            "status": "conflict",
                        },
                        sort_keys=True,
                    )
                )
            else:
                print("drain conflict · retry relay --drain", file=sys.stderr)
            return 3
        previous_version = int(advanced.value["previous_cursor_version"])
        cursor_version = int(advanced.value["cursor_version"])
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "controller_label": controller_label,
                        "cursor_version": cursor_version,
                        "drained": len(envelopes),
                        "previous_cursor_version": previous_version,
                        "status": "drained",
                    },
                    sort_keys=True,
                )
            )
        else:
            print(
                f"drained {len(envelopes)} · cursor "
                f"{previous_version}->{cursor_version}"
            )
        return 0
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "controller_label": controller_label,
                    "registry_generation": peek.registry_generation,
                    "cursor_version": peek.cursor_version,
                    "positions": positions,
                    "stream_snapshots": peek.stream_snapshots,
                    "advance_command": advance_command,
                    "items": envelopes,
                },
                sort_keys=True,
            )
        )
        return 0
    if getattr(args, "bodies", False):
        print(json.dumps(envelopes))
    else:
        headlines = format_envelope_headlines(envelopes)
        if headlines:
            print(headlines)
            print("bodies: re-run with --bodies")
    counts: dict[str, int] = {}
    for envelope in envelopes:
        dispatch_id = str(envelope.get("dispatch_id") or "unknown")
        counts[dispatch_id] = counts.get(dispatch_id, 0) + 1
    print(format_pending_counts(counts))
    return 0


def _parse_cursor_positions(
    values: list[str] | list[list[str]] | None,
) -> dict[str, int]:
    advances: dict[str, int] = {}
    for group in values or []:
        members = [group] if isinstance(group, str) else group
        if not isinstance(members, (list, tuple)):
            raise ValueError("cursor positions must be STREAM=SEQ values")
        for raw in members:
            stream, separator, position_text = str(raw).rpartition("=")
            stream = stream.strip()
            if not separator or not stream:
                raise ValueError("cursor position must use STREAM=SEQ")
            try:
                position = int(position_text)
            except ValueError as exc:
                raise ValueError("cursor position sequence must be an integer") from exc
            if position <= 0:
                raise ValueError("cursor position sequence must be positive")
            advances[stream] = max(advances.get(stream, 0), position)
    if not advances:
        raise ValueError("at least one --position STREAM=SEQ is required")
    return advances


def _parse_cursor_stream_snapshots(
    values: list[str] | list[list[str]] | None,
) -> dict[str, str]:
    snapshots: dict[str, str] = {}
    for group in values or []:
        members = [group] if isinstance(group, str) else group
        if not isinstance(members, (list, tuple)):
            raise ValueError("cursor stream snapshots must be STREAM=TOKEN values")
        for raw in members:
            stream, separator, snapshot = str(raw).rpartition("=")
            stream = stream.strip()
            snapshot = snapshot.strip()
            if not separator or not stream or re.fullmatch(r"[0-9a-f]{64}", snapshot) is None:
                raise ValueError("cursor stream snapshot must use STREAM=64_HEX_TOKEN")
            snapshots[stream] = snapshot
    if not snapshots:
        raise ValueError("at least one --stream-snapshot STREAM=TOKEN is required")
    return snapshots


def cmd_advance_cursor(args: argparse.Namespace) -> int:
    """Advance the server cursor by CAS to validated journal positions."""
    import goalflight_journal  # type: ignore
    import goalflight_session_status as sessions  # type: ignore
    import goalflight_task  # type: ignore

    project_root = goalflight_task.resolve_project_root(
        args.project_root or str(Path.cwd())
    )
    label = str(
        args.controller_label
        or sessions.resolve_controller_label(project_root=project_root)
        or ""
    ).strip()
    nonce = str(
        args.lease_nonce
        or os.environ.get("GOALFLIGHT_CONTROLLER_LEASE_NONCE")
        or os.environ.get("GOALFLIGHT_CONTROLLER_SESSION_ID")
        or ""
    ).strip()
    try:
        advances = _parse_cursor_positions(args.position)
        stream_snapshots = _parse_cursor_stream_snapshots(args.stream_snapshot)
        if not label or not nonce:
            raise ValueError("active controller label and lease nonce are required")
        result = goalflight_journal.Journal(project_root).advance_cursor(
            label,
            nonce=nonce,
            expected_cursor_version=args.cursor_version,
            expected_stream_snapshots=stream_snapshots,
            advances=advances,
            actor=f"controller:{os.getpid()}",
        )
    except (ValueError, goalflight_journal.JournalError) as exc:
        print(f"advance: {exc}", file=sys.stderr)
        return 2
    if not result.committed or result.value is None:
        print(f"advance: {result.reason or 'cursor CAS lost'}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(result.value, sort_keys=True))
    else:
        print(
            f"cursor advanced {result.value['previous_cursor_version']}"
            f"->{result.value['cursor_version']}"
        )
    return 0


STEERING_DISPATCH_ID = "fleet-steering"


def steering_register_path(fleet_dir: Path) -> Path:
    return _canonical_jsonl_path(
        fleet_dir / "register" / "dispatches" / f"{STEERING_DISPATCH_ID}.jsonl"
    )


def _admit_stream_seq(*, provided_seq: int | None, envelopes: list[dict]) -> int:
    """Return a new position strictly above this carrier stream's high-water."""
    high_water = max((int(envelope.get("seq", 0)) for envelope in envelopes), default=0)
    if provided_seq is None or provided_seq <= high_water:
        return high_water + 1
    return provided_seq


def next_seq(path: Path, *, envelopes: list[dict] | None = None) -> int:
    if envelopes is None:
        envelopes = read_envelopes_tolerant(path)
    return _admit_stream_seq(provided_seq=None, envelopes=envelopes)


def write_steering_envelope(
    fleet_dir: Path,
    *,
    audit_id: str,
    proposal_id: str,
    patch: list[dict],
    after_hash: str,
    messages_dir: Path | None = None,
) -> dict:
    path = steering_register_path(fleet_dir)
    resolved_messages_dir = messages_dir or default_messages_dir()

    def update(existing: list[dict]) -> tuple[list[dict], dict]:
        envelope = {
            "schema": "goalflight.message.v1",
            "schema_version": 1,
            "id": str(uuid.uuid4()),
            "dispatch_id": STEERING_DISPATCH_ID,
            "seq": max((int(item.get("seq", 0)) for item in existing), default=0) + 1,
            "ts": utc_now(),
            "source": {"node": "local", "adapter": "fleet", "transport": "controller"},
            "type": "steering",
            "priority": "normal",
            "payload": {
                "audit_id": audit_id,
                "proposal_id": proposal_id,
                "patch": patch,
                "after_hash": after_hash,
            },
        }
        envelope[_INGESTION_ORDER_FIELD] = _ingestion_order_for_envelope(
            resolved_messages_dir,
            envelope,
        )
        return existing + [envelope], envelope

    envelope = update_envelopes(path, update)
    refresh_aggregate(fleet_dir, messages_dir=resolved_messages_dir)
    return envelope


def merge_remote_register(
    fleet_dir: Path,
    remote_jsonl: Path,
    *,
    messages_dir: Path | None = None,
) -> dict:
    """Merge remote dispatch jsonl into fleet register using monotonic seq rules."""
    if not remote_jsonl.exists():
        raise MessageError(f"remote file missing: {remote_jsonl}")
    remote_errors: list[dict[str, object]] = []
    remote = read_envelopes_tolerant(remote_jsonl, carrier_errors=remote_errors)
    dest = _canonical_jsonl_path(
        fleet_dir / "register" / "dispatches" / remote_jsonl.name
    )
    resolved_messages_dir = messages_dir or default_messages_dir()

    def update(existing: list[dict]) -> tuple[list[dict] | None, int]:
        appended_envelopes: list[dict] = []
        seen_seq = {int(env.get("seq", 0)) for env in existing}
        for env in remote:
            seq = int(env.get("seq", 0))
            if seq in seen_seq:
                continue
            ingested = dict(env)
            ingested[_INGESTION_ORDER_FIELD] = _ingestion_order_for_envelope(
                resolved_messages_dir,
                ingested,
            )
            appended_envelopes.append(ingested)
            seen_seq.add(seq)
        return (
            existing + appended_envelopes if appended_envelopes else None,
            len(appended_envelopes),
        )

    appended = update_envelopes(dest, update)
    aggregate = refresh_aggregate(fleet_dir, messages_dir=resolved_messages_dir)
    return {
        "merged_into": str(dest),
        "appended": appended,
        "quarantined": len(remote_errors),
        "open_user_needs": len(aggregate.get("open_user_needs") or []),
    }


def cmd_listen(args) -> int:
    """One-shot journal cursor listener; its exit is the wake."""
    import goalflight_journal  # type: ignore
    import goalflight_session_status as sessions  # type: ignore
    import goalflight_task  # type: ignore

    project_root = goalflight_task.resolve_project_root(args.project_root or str(Path.cwd()))
    label = str(
        args.controller_label
        or sessions.resolve_controller_label(project_root=project_root)
        or ""
    ).strip()
    nonce = str(
        args.lease_nonce
        or os.environ.get("GOALFLIGHT_CONTROLLER_LEASE_NONCE")
        or os.environ.get("GOALFLIGHT_CONTROLLER_SESSION_ID")
        or ""
    ).strip()
    if not label or not nonce:
        print(
            "listen: active controller label and lease nonce are required; claim the lease first",
            file=sys.stderr,
        )
        return 2
    test_mode = os.environ.get("GOALFLIGHT_TEST_MODE") == "1"
    poll = max(0.01 if test_mode else 0.5, float(args.poll_secs or 5.0))
    deadline = (
        time.monotonic() + float(args.timeout_s)
        if args.timeout_s and float(args.timeout_s) > 0
        else None
    )
    try:
        authority = goalflight_journal.Journal(project_root)
        test_start_token = (
            os.environ.get("GOALFLIGHT_TEST_LISTENER_START_TOKEN")
            if test_mode
            else None
        )
        identity = (
            {"pid": os.getpid(), "start_token": test_start_token}
            if test_start_token
            else goalflight_compat.process_start_identity(os.getpid())
        )
        if not isinstance(identity, dict) or not identity.get("start_token"):
            raise MessageError("listener process identity is unavailable")
        parent_pid = os.getppid()
    except (MessageError, goalflight_journal.JournalError, ValueError) as exc:
        print(f"listen: {exc}", file=sys.stderr)
        return 2

    # Acquire the kernel witness before superseding journal coverage.  If the
    # ledger is unavailable, the incumbent listener remains both ARMED and
    # locked instead of being displaced by a replacement that cannot stay live.
    try:
        waiter = goalflight_wake.register_waiter(
            project_root,
            controller_label=label,
            kind="listener",
            generation_key=nonce,
        )
    except BlockingIOError:
        print("listen: listener generation already has a live doorbell", file=sys.stderr)
        return 3
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"listen: wake ledger registration failed: {exc}", file=sys.stderr)
        return 2

    try:
        armed = authority.arm_listener(
            label,
            nonce=nonce,
            pid=os.getpid(),
            start_token=str(identity["start_token"]),
            parent_pid=parent_pid,
        )
        if not armed.committed or not armed.value:
            raise MessageError(armed.reason or "listener coverage arm lost")
        coverage = dict(armed.value)
    except (MessageError, goalflight_journal.JournalError, ValueError) as exc:
        waiter.close()
        print(f"listen: {exc}", file=sys.stderr)
        return 2

    coverage_id = str(coverage["coverage_id"])

    def finish(reason: str, *, code: int, detail: str | None = None) -> int:
        try:
            exited = authority.exit_listener(coverage_id, reason=reason)
            if not exited.committed:
                detail = detail or exited.reason or "coverage exit CAS lost"
            elif exited.value and exited.value.get("exit_reason"):
                reason = str(exited.value["exit_reason"])
            payload = {
                "kind": "exit",
                "reason": reason,
                "coverage_id": coverage_id,
                "detail": detail,
            }
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            elif detail:
                print(f"listen: {reason}: {detail}", file=sys.stderr)
            return code
        finally:
            if waiter is not None:
                waiter.close()

    emit_wake_entry_notice(
        project_root=project_root,
        controller_label=label,
        stream=sys.stderr,
    )

    # Opt-in arm-reports-pending: report the backlog and raise the ring threshold
    # to the arm-time high-water so only events BEYOND it ring. Without the
    # option, the pre-existing exit-driven contract remains intact: pending mail
    # immediately follows the ordinary one-line ring path below.
    arm_high: dict[str, int] = {}
    arm_snapshot = None
    if getattr(args, "report_pending", False):
        try:
            arm_snapshot = authority.cursor_peek(label, nonce=nonce, limit=1000)
        except goalflight_journal.JournalError:
            arm_snapshot = None
    if arm_snapshot is not None and arm_snapshot.items:
        arm_high = _cursor_positions(arm_snapshot.items)
        # The arm doubles as the peek: emit the same machine-readable snapshot
        # relay --new --json would, advance command included, so the awake
        # controller drains the backlog straight from this output without a
        # second CLI round-trip.
        arm_advance = _cursor_advance_command(
            project_root=project_root,
            controller_label=label,
            lease_nonce=nonce,
            cursor_version=arm_snapshot.cursor_version,
            positions=_cursor_positions(arm_snapshot.items),
            stream_snapshots=arm_snapshot.stream_snapshots,
        )
        arm_payload = {
            "kind": "pending-at-arm",
            "items": arm_snapshot.items,
            "cursor_version": arm_snapshot.cursor_version,
            "advance_command": arm_advance,
        }
        if args.json:
            print(
                json.dumps(arm_payload, sort_keys=True, default=str),
                flush=True,
            )
        else:
            for item in arm_snapshot.items:
                stream = str(item.get("stream_id") or "")
                seq = int(item.get("stream_seq") or 0)
                kind = str(item.get("event_type") or item.get("type") or "event")
                print(
                    f"pending-at-arm: [{kind}] {stream} seq={seq}",
                    flush=True,
                )
            print(
                "pending-at-arm-json: "
                + json.dumps(arm_payload, sort_keys=True, default=str),
                flush=True,
            )
            print(
                f"pending-at-arm: {len(arm_snapshot.items)} item(s) reported; "
                "listener stays armed and rings only for newer events; run the "
                "advance command above to drain the backlog",
                flush=True,
            )

    while True:
        if os.getppid() != parent_pid:
            return finish("orphaned", code=3, detail="listener parent changed")
        if deadline is not None and time.monotonic() >= deadline:
            return finish("timeout", code=1, detail="no waking event before timeout")
        try:
            stored_coverage = authority.coverage(coverage_id)
            lease = authority.active_lease(label)
            measured = (
                {"pid": os.getpid(), "start_token": test_start_token}
                if test_start_token
                else goalflight_compat.process_start_identity(os.getpid())
            )
            reason = goalflight_journal.listener_exit_reason(
                stored_coverage,
                lease.__dict__ if lease is not None else None,
                current_parent_pid=os.getppid(),
                identity_matches=bool(
                    isinstance(measured, dict)
                    and measured.get("start_token") == coverage.get("start_token")
                ),
            )
            if reason:
                return finish(reason, code=3, detail="listener self-check failed")
            # With an arm-time backlog the cheap limit-1 peek would forever
            # see the oldest (already-reported) item; peek wide and ring only
            # for events beyond the arm-time high-water.
            peek = authority.cursor_peek(
                label, nonce=nonce, limit=1000 if arm_high else 1
            )
            wakeable_items = bool(peek.items)
            if arm_high and wakeable_items:
                wakeable_items = any(
                    int(item.get("stream_seq") or 0)
                    > arm_high.get(str(item.get("stream_id") or ""), 0)
                    for item in peek.items
                )
        except goalflight_journal.CASMismatch as exc:
            lease = authority.active_lease(label)
            reason = "stale-lease" if lease is not None else "superseded"
            return finish(reason, code=3, detail=str(exc))
        except goalflight_journal.JournalUpgradeRequired as exc:
            return finish("upgrade-required", code=2, detail=str(exc))
        except goalflight_journal.JournalUnavailable as exc:
            return finish("journal-unavailable", code=2, detail=str(exc))
        except (goalflight_journal.JournalError, ValueError) as exc:
            return finish("corrupt", code=2, detail=str(exc))
        if wakeable_items:
            snapshot = authority.cursor_peek(label, nonce=nonce, limit=1000)
            positions = _cursor_positions(snapshot.items)
            advance_command = _cursor_advance_command(
                project_root=project_root,
                controller_label=label,
                lease_nonce=nonce,
                cursor_version=snapshot.cursor_version,
                positions=positions,
                stream_snapshots=snapshot.stream_snapshots,
            )
            if advance_command is None:
                return finish(
                    "corrupt",
                    code=2,
                    detail="waking cursor snapshot has no advance positions",
                )
            exited = authority.exit_listener(coverage_id, reason="event")
            if not exited.committed or not exited.value:
                return finish("superseded", code=3, detail=exited.reason)
            if exited.value.get("exit_reason") != "event":
                return finish(
                    "superseded",
                    code=3,
                    detail="listener coverage changed before doorbell exit",
                )
            payload = {
                "kind": "ring",
                "reason": "event",
                "coverage_id": coverage_id,
                "registry_generation": snapshot.registry_generation,
                "cursor_version": snapshot.cursor_version,
                "advance_command": advance_command,
            }
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(
                    "mail available; peek: goalflight_messages.py relay --new; "
                    f"advance after processing: {advance_command}"
                )
            waiter.close()
            return 0
        sleep_until = time.monotonic() + poll
        while time.monotonic() < sleep_until:
            if os.getppid() != parent_pid:
                return finish("orphaned", code=3, detail="listener parent changed")
            if deadline is not None and time.monotonic() >= deadline:
                return finish("timeout", code=1, detail="no waking event before timeout")
            time.sleep(min(0.25, max(0.0, sleep_until - time.monotonic())))


def cmd_listen_auto(args) -> int:
    """Resolve an existing ambient lease, then run the foreground listener."""
    import goalflight_journal  # type: ignore
    import goalflight_session_status as sessions  # type: ignore
    import goalflight_task  # type: ignore

    project_root = goalflight_task.resolve_project_root(args.project_root or str(Path.cwd()))
    label = sessions.resolve_controller_label(
        args.controller_label,
        project_root=project_root,
    )
    if not label:
        print("listen-auto: controller label is unavailable", file=sys.stderr)
        return 2
    ambient = _ambient_claimed_controller(
        project_root,
        controller_label=label,
        mail_bearing=True,
    )
    if not ambient.get("claimed"):
        print(
            "listen-auto: " + str(ambient.get("reason") or "ambient lease unavailable"),
            file=sys.stderr,
        )
        return 2
    authority = goalflight_journal.Journal(project_root)
    lease = authority.active_lease(label)
    if lease is None:
        print("listen-auto: active controller lease is unavailable", file=sys.stderr)
        return 2
    args.project_root = str(project_root)
    args.controller_label = label
    args.lease_nonce = lease.nonce
    return cmd_listen(args)


def cmd_mirror(args: argparse.Namespace) -> int:
    result = merge_remote_register(args.fleet_dir, args.remote, messages_dir=args.messages_dir)
    print(json.dumps(result, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    aggregate = build_aggregate(messages_dir=args.messages_dir, fleet_dir=args.fleet_dir)
    if args.write_aggregate:
        refresh_aggregate(args.fleet_dir, messages_dir=args.messages_dir)
    print(json.dumps(aggregate, indent=2))
    return 0


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Flight message envelopes")
    parser.add_argument("--messages-dir", type=Path, default=default_messages_dir())
    parser.add_argument("--fleet-dir", type=Path, default=default_fleet_dir())
    sub = parser.add_subparsers(dest="cmd", required=True)

    from_text = sub.add_parser("from-text")
    from_text.add_argument("--dispatch-id", required=True)
    from_text.add_argument("--text-file", type=Path)
    from_text.add_argument("--node", default="local")
    from_text.add_argument("--adapter", default="unknown")
    from_text.add_argument(
        "--transport",
        default="tail_file",
        choices=["acp", "tail_file", "mcp", "controller", "bash-tail"],
    )
    from_text.add_argument("--json", action="store_true")
    from_text.set_defaults(func=cmd_from_text)

    post = sub.add_parser("post", help="Append one envelope (canonical file path)")
    post.add_argument("--dispatch-id", required=True)
    post.add_argument("--type", required=True, choices=sorted(EVENT_TYPE_REGISTRY))
    post.add_argument("--payload", help="JSON object payload")
    post.add_argument("--text", help="Shorthand payload.text when --payload omitted")
    post.add_argument(
        "--subject",
        help="Short scannable subject; shown in relay listings instead of the first body line",
    )
    post.add_argument(
        "--to-controller",
        metavar="LABEL",
        help="address controller-channel mail to one durable controller registry name",
    )
    post.add_argument(
        "--controller-project-root",
        type=Path,
        help=(
            "project root that scopes --to-controller; defaults to the current git "
            "project, and is required for explicit cross-project addressing"
        ),
    )
    post.add_argument("--node", default="local")
    post.add_argument("--adapter", default="unknown")
    post.add_argument(
        "--transport",
        default="controller",
        choices=["acp", "tail_file", "mcp", "controller", "bash-tail"],
    )
    post.add_argument("--refresh-aggregate", action="store_true")
    post.add_argument("--json", action="store_true")
    post.set_defaults(func=cmd_post)

    read = sub.add_parser("read")
    read.add_argument("--dispatch-id", required=True)
    read.add_argument("--last", type=int, default=None)
    read.add_argument("--json", action="store_true")
    read.set_defaults(func=cmd_read)

    status = sub.add_parser("status")
    status.add_argument("--write-aggregate", action="store_true")
    status.set_defaults(func=cmd_status)

    relay = sub.add_parser("relay")
    relay.add_argument("--new", action="store_true", help="Peek at events pending after the journal cursor")
    relay.add_argument(
        "--drain",
        action="store_true",
        help="peek and CAS-advance that exact snapshot in one process",
    )
    relay.add_argument(
        "--bodies",
        action="store_true",
        help="With --new, print full envelope JSON instead of one headline per message",
    )
    relay.add_argument(
        "--json",
        action="store_true",
        help="Emit one cursor snapshot with items and server-known positions",
    )
    relay.set_defaults(func=cmd_relay)

    advance = sub.add_parser(
        "advance",
        help="CAS-advance the controller cursor to processed server-known positions",
    )
    advance.add_argument("--project-root", default=None)
    advance.add_argument("--controller-label", default=None)
    advance.add_argument("--lease-nonce", default=None)
    advance.add_argument("--cursor-version", type=int, required=True)
    advance.add_argument(
        "--stream-snapshot",
        action="append",
        nargs="+",
        default=[],
        metavar="STREAM=TOKEN",
        help="per-stream safety tokens emitted by relay --new --json",
    )
    advance.add_argument(
        "--position",
        action="append",
        nargs="+",
        default=[],
        metavar="STREAM=SEQ",
        help="one or more positions; repeat the flag or group values after one flag",
    )
    advance.add_argument("--json", action="store_true")
    advance.set_defaults(func=cmd_advance_cursor)

    def add_listen_arguments(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--project-root", default=None)
        command_parser.add_argument(
            "--controller-label",
            default=None,
            help="durable controller lease label",
        )
        command_parser.add_argument(
            "--lease-nonce",
            default=None,
            help="active lease capability; defaults to GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        )
        command_parser.add_argument("--poll-secs", type=float, default=5.0)
        command_parser.add_argument(
            "--timeout-s", type=float, default=0.0, help="0 = wait indefinitely"
        )
        command_parser.add_argument("--json", action="store_true")
        command_parser.add_argument(
            "--report-pending",
            action="store_true",
            help="report an arm-time backlog and stay armed for only newer events",
        )

    listen = sub.add_parser(
        "listen",
        help="one-shot journal cursor listener; its exit is the wake",
    )
    add_listen_arguments(listen)
    listen_auto = sub.add_parser(
        "listen-auto",
        help="resolve the ambient lease and start the one-shot listener",
    )
    add_listen_arguments(listen_auto)

    mirror = sub.add_parser("mirror")
    mirror.add_argument("--remote", type=Path, required=True, help="Remote *.jsonl inbox to merge")
    listen.set_defaults(func=cmd_listen)
    listen_auto.set_defaults(func=cmd_listen_auto)
    mirror.set_defaults(func=cmd_mirror)

    args = parser.parse_args(argv)
    role_by_command = {
        "listen": "listener",
        "listen-auto": "listener",
        "mirror": "mirror",
        "status": "dashboard",
        "relay": "dashboard",
        "from-text": "producer",
        "post": "producer",
    }
    role = role_by_command.get(args.cmd, "controller")
    if args.cmd not in {"listen", "listen-auto"} and not (
        args.cmd == "relay" and args.drain
    ):
        entry_root = getattr(args, "controller_project_root", None) or Path.cwd()
        emit_wake_entry_notice(
            project_root=entry_root,
            messages_dir=args.messages_dir,
            fleet_dir=args.fleet_dir,
            mail_bearing=role == "controller",
            stream=sys.stderr,
        )
    try:
        return args.func(args)
    except MessageError as exc:
        print(f"{args.cmd}: refused: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    """Run the messages CLI without leaking unhandled tracebacks to operators."""
    try:
        return _run_cli(argv)
    except Exception as exc:
        try:
            detail = sanitize_display(str(exc), limit=240) or "internal failure"
        except Exception:
            detail = "internal failure"
        print(
            f"goalflight_messages: {type(exc).__name__}: {detail} · "
            "next: run goalflight_messages.py --help",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
