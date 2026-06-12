#!/usr/bin/env python3
"""Read and validate fleet dispatch status mirrors (Track A goal 9a).

Mirrors accept the canonical fleet schema ``goalflight.acp-run.v1`` and the
live dispatch schema ``goalflight.status.v1``. Both are normalized to the fleet
schema with a strictly increasing ``seq`` when compared against a previously
observed sequence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_MIRROR_SCHEMA = "goalflight.acp-run.v1"
DISPATCH_STATUS_SCHEMA = "goalflight.status.v1"
ACCEPTED_STATUS_SCHEMAS = (STATUS_MIRROR_SCHEMA, DISPATCH_STATUS_SCHEMA)
REQUIRED_FIELDS = ("schema", "seq", "dispatch_id", "state")

ERROR_MISSING_FILE = "missing_file"
ERROR_PARTIAL_JSON = "partial_json"
ERROR_SCHEMA_MISMATCH = "schema_mismatch"
ERROR_SEQ_REGRESSION = "seq_regression"


@dataclass(frozen=True)
class MirrorReadResult:
    ok: bool
    error: str | None = None
    payload: dict[str, Any] | None = None
    last_seq: int | None = None
    detail: str | None = None


def _coerce_seq(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _coerce_updated_at(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return None


def _state_seq_rank(state: object) -> int:
    if not isinstance(state, str):
        return 0
    if state.startswith("blocked"):
        return 90
    return {
        "waiting_capacity": 10,
        "waiting": 10,
        "handshaking": 20,
        "starting": 20,
        "running": 30,
        "running_quiet": 40,
        "watcher_stopped": 45,
        "complete": 90,
        "failed": 90,
        "wedged": 90,
        "worker_dead": 90,
        "idle_timeout": 90,
        "inconclusive_timeout": 90,
        "controller_dead": 90,
    }.get(state, 50)


def _synthesized_seq(payload: dict[str, Any]) -> int | None:
    seq = _coerce_seq(payload.get("seq"))
    if seq is not None:
        return seq
    events_seen = _coerce_seq(payload.get("events_seen"))
    if events_seen is not None:
        return events_seen
    updated_at = _coerce_updated_at(payload.get("updated_at"))
    if updated_at is not None:
        return updated_at * 100 + _state_seq_rank(payload.get("state"))
    return None


def _normalize_dispatch_state(state: object) -> str | None:
    if not isinstance(state, str) or not state:
        return None
    if state.startswith("blocked"):
        return "blocked"
    return {
        "waiting_capacity": "waiting",
        "handshaking": "starting",
        "watcher_stopped": "running_quiet",
        "idle_timeout": "inconclusive_timeout",
    }.get(state, state)


def _terminal_state_for(state: object, reason: object = None) -> str:
    if state == "complete":
        return "complete"
    if state == "worker_dead":
        return "worker_dead"
    if state in {"idle_timeout", "inconclusive_timeout"}:
        return "idle_timeout"
    if state == "watcher_stopped":
        return "watcher_stopped"
    if state == "controller_dead" or (state == "orphaned" and reason == "controller_dead"):
        return "controller_dead"
    if isinstance(state, str) and state.startswith("blocked"):
        return "blocked"
    if state in {"failed", "wedged"}:
        return "error"
    return "unknown"


def _liveness_state_for(payload: dict[str, Any], *, terminal_state: str) -> str:
    if terminal_state != "unknown" and payload.get("state") != "watcher_stopped":
        return "terminal"
    worker_alive = payload.get("worker_alive")
    if worker_alive is True:
        return "live"
    if worker_alive is False:
        return "dead"
    return "unknown"


def normalize_status_payload(payload: dict[str, Any]) -> MirrorReadResult:
    """Normalize one accepted remote status schema into the fleet mirror schema."""
    schema = payload.get("schema")
    if schema not in ACCEPTED_STATUS_SCHEMAS:
        return MirrorReadResult(
            ok=False,
            error=ERROR_SCHEMA_MISMATCH,
            detail=(
                "expected schema "
                f"{STATUS_MIRROR_SCHEMA!r} or {DISPATCH_STATUS_SCHEMA!r}, got {schema!r}"
            ),
        )

    normalized = dict(payload)
    source_state = payload.get("state")
    if schema == DISPATCH_STATUS_SCHEMA:
        normalized["schema"] = STATUS_MIRROR_SCHEMA
        normalized["source_schema"] = DISPATCH_STATUS_SCHEMA
        if isinstance(source_state, str):
            normalized["source_state"] = source_state
        normalized_state = _normalize_dispatch_state(source_state)
        if normalized_state is not None:
            normalized["state"] = normalized_state

    seq = _synthesized_seq(normalized)
    if seq is not None:
        normalized["seq"] = seq

    terminal_state = _terminal_state_for(source_state or normalized.get("state"), normalized.get("reason"))
    normalized.setdefault("liveness_state", _liveness_state_for(payload, terminal_state=terminal_state))
    if terminal_state != "unknown":
        normalized.setdefault("terminal_state", terminal_state)

    terminal_marker = normalized.get("terminal_marker")
    if isinstance(terminal_marker, dict):
        marker_type = terminal_marker.get("type")
        if isinstance(marker_type, str) and marker_type:
            normalized.setdefault("marker_state", marker_type.lower())

    identity = normalized.get("worker_identity") or normalized.get("expected_worker_identity")
    if isinstance(identity, dict):
        normalized["worker_identity"] = dict(identity)
        normalized.setdefault("expected_worker_identity", dict(identity))

    missing = [field for field in REQUIRED_FIELDS if field not in normalized]
    if missing:
        return MirrorReadResult(
            ok=False,
            error=ERROR_SCHEMA_MISMATCH,
            detail=f"missing required fields: {', '.join(missing)}",
        )

    normalized_seq = _coerce_seq(normalized.get("seq"))
    if normalized_seq is None or normalized_seq < 0:
        return MirrorReadResult(
            ok=False,
            error=ERROR_SCHEMA_MISMATCH,
            detail="seq must be a non-negative integer",
        )
    normalized["seq"] = normalized_seq

    return MirrorReadResult(ok=True, payload=normalized, last_seq=normalized_seq)


def read_status_mirror(path: Path, *, last_seq: int | None = None) -> MirrorReadResult:
    """Read one status mirror file with schema + monotonic seq checks."""
    if not path.exists():
        return MirrorReadResult(
            ok=False,
            error=ERROR_MISSING_FILE,
            detail=f"mirror file not found: {path}",
        )

    try:
        raw = path.read_text()
    except OSError as exc:
        return MirrorReadResult(
            ok=False,
            error=ERROR_MISSING_FILE,
            detail=f"cannot read mirror file: {exc}",
        )

    if not raw.strip():
        return MirrorReadResult(
            ok=False,
            error=ERROR_PARTIAL_JSON,
            detail="mirror file is empty",
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return MirrorReadResult(
            ok=False,
            error=ERROR_PARTIAL_JSON,
            detail=str(exc),
        )

    if not isinstance(payload, dict):
        return MirrorReadResult(
            ok=False,
            error=ERROR_SCHEMA_MISMATCH,
            detail="mirror root must be a JSON object",
        )

    normalized = normalize_status_payload(payload)
    if not normalized.ok:
        return normalized

    assert normalized.payload is not None
    seq = int(normalized.last_seq or 0)
    if last_seq is not None and seq <= last_seq:
        return MirrorReadResult(
            ok=False,
            error=ERROR_SEQ_REGRESSION,
            payload=normalized.payload,
            last_seq=seq,
            detail=f"seq {seq} is not strictly greater than last_seq {last_seq}",
        )

    return normalized
