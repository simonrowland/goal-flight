#!/usr/bin/env python3
"""Read and validate fleet dispatch status mirrors (Track A goal 9a).

Mirrors accept the canonical fleet schema ``goalflight.acp-run.v1`` and the
live dispatch schema ``goalflight.status.v1``. Both are normalized to the fleet
schema with a strictly increasing ``seq`` inside one epoch when compared
against a previously observed sequence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import goalflight_dispatch_states as dispatch_states

STATUS_MIRROR_SCHEMA = "goalflight.acp-run.v1"
DISPATCH_STATUS_SCHEMA = "goalflight.status.v1"
ACCEPTED_STATUS_SCHEMAS = (STATUS_MIRROR_SCHEMA, DISPATCH_STATUS_SCHEMA)
REQUIRED_FIELDS = ("schema", "seq", "dispatch_id", "state")

ERROR_MISSING_FILE = "missing_file"
ERROR_PARTIAL_JSON = "partial_json"
ERROR_SCHEMA_MISMATCH = "schema_mismatch"
ERROR_SEQ_REGRESSION = "seq_regression"
LEGACY_EPOCH = 0
EpochValue = int | str


@dataclass(frozen=True)
class MirrorReadResult:
    ok: bool
    error: str | None = None
    payload: dict[str, Any] | None = None
    last_seq: int | None = None
    epoch: EpochValue = LEGACY_EPOCH
    detail: str | None = None
    lineage_identity: dict[str, Any] | None = None


def _coerce_seq(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _coerce_epoch(value: object) -> EpochValue | None:
    if value is None:
        return LEGACY_EPOCH
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
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
    return dispatch_states.state_seq_rank(state)


def _synthesized_seq(payload: dict[str, Any]) -> int | None:
    seq = _coerce_seq(payload.get("seq"))
    if seq is not None:
        return seq
    events_seen = _coerce_seq(payload.get("events_seen"))
    if events_seen is not None:
        return events_seen * 100 + _state_seq_rank(payload.get("state"))
    updated_at = _coerce_updated_at(payload.get("updated_at"))
    if updated_at is not None:
        return updated_at * 100 + _state_seq_rank(payload.get("state"))
    return None


def _first_non_empty_string(values: list[object]) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _lineage_identity_for(payload: dict[str, Any]) -> dict[str, Any] | None:
    worker_identity = payload.get("worker_identity")
    if not isinstance(worker_identity, dict):
        worker_identity = {}
    expected_identity = payload.get("expected_worker_identity")
    if not isinstance(expected_identity, dict):
        expected_identity = {}

    pid = _coerce_seq(payload.get("worker_pid"))
    if pid is None:
        pid = _coerce_seq(payload.get("pid"))
    if pid is None:
        pid = _coerce_seq(worker_identity.get("pid"))
    if pid is None:
        pid = _coerce_seq(expected_identity.get("pid"))
    if pid is None:
        return None

    identity: dict[str, Any] = {"worker_pid": pid}
    started_at = _first_non_empty_string(
        [
            payload.get("worker_start_time"),
            payload.get("worker_lstart"),
            payload.get("process_start_time"),
            payload.get("lstart"),
            worker_identity.get("lstart"),
            worker_identity.get("start_time"),
            worker_identity.get("started_at"),
            expected_identity.get("lstart"),
            expected_identity.get("start_time"),
            expected_identity.get("started_at"),
        ]
    )
    if started_at is not None:
        identity["worker_start_time"] = started_at
    return identity


def _normalize_lineage_identity(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return _lineage_identity_for(value)


def _lineage_identity_changed(
    incoming: dict[str, Any] | None,
    previous: dict[str, Any] | None,
) -> bool:
    if incoming is None or previous is None:
        return False
    incoming_pid = _coerce_seq(incoming.get("worker_pid"))
    previous_pid = _coerce_seq(previous.get("worker_pid"))
    if incoming_pid is None or previous_pid is None:
        return False
    if incoming_pid != previous_pid:
        return True
    incoming_start = _first_non_empty_string([incoming.get("worker_start_time")])
    previous_start = _first_non_empty_string([previous.get("worker_start_time")])
    return incoming_start is not None and previous_start is not None and incoming_start != previous_start


def _legacy_lineage_reset_allowed(
    payload: dict[str, Any],
    *,
    raw_had_epoch: bool,
    seq: int,
    last_seq: int,
    epoch: EpochValue,
    baseline_epoch: EpochValue,
    lineage_identity: dict[str, Any] | None,
    last_lineage_identity: object,
) -> bool:
    """Accept legacy status-file recreation while still rejecting stale replays.

    Pre-epoch workers cannot mint a new epoch after status-file recreation, so a
    reboot can reset ``events_seen`` and produce a lower same-epoch seq forever.
    Treat that as a new lineage only when the incoming legacy record has no
    epoch, targets the same epoch-0 dispatch, has a lower seq, and carries a
    birth signal that differs from the durable last-seen lineage identity.
    Ambiguous or missing identity evidence fails closed as a stale replay.
    """
    if raw_had_epoch or epoch != LEGACY_EPOCH or baseline_epoch != LEGACY_EPOCH or seq >= last_seq:
        return False
    return _lineage_identity_changed(lineage_identity, _normalize_lineage_identity(last_lineage_identity))


def _normalize_dispatch_state(state: object) -> str | None:
    return dispatch_states.normalize_dispatch_state(state)


def _terminal_state_for(state: object, reason: object = None) -> str:
    return dispatch_states.terminal_state_for(state, reason)


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

    epoch = _coerce_epoch(normalized.get("epoch"))
    if epoch is None:
        return MirrorReadResult(
            ok=False,
            error=ERROR_SCHEMA_MISMATCH,
            detail="epoch must be a non-empty string or integer",
        )
    normalized["epoch"] = epoch

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

    return MirrorReadResult(
        ok=True,
        payload=normalized,
        last_seq=normalized_seq,
        epoch=epoch,
        lineage_identity=_lineage_identity_for(normalized),
    )


def read_status_mirror(
    path: Path,
    *,
    last_seq: int | None = None,
    last_epoch: object = LEGACY_EPOCH,
    last_lineage_identity: object = None,
) -> MirrorReadResult:
    """Read one status mirror file with schema + same-epoch monotonic seq checks."""
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

    raw_had_epoch = "epoch" in payload
    normalized = normalize_status_payload(payload)
    if not normalized.ok:
        return normalized

    assert normalized.payload is not None
    seq = int(normalized.last_seq or 0)
    lineage_identity = normalized.lineage_identity
    baseline_epoch = _coerce_epoch(last_epoch)
    if baseline_epoch is None:
        baseline_epoch = LEGACY_EPOCH
    if last_seq is not None and normalized.epoch == baseline_epoch and seq <= last_seq:
        if _legacy_lineage_reset_allowed(
            normalized.payload,
            raw_had_epoch=raw_had_epoch,
            seq=seq,
            last_seq=last_seq,
            epoch=normalized.epoch,
            baseline_epoch=baseline_epoch,
            lineage_identity=lineage_identity,
            last_lineage_identity=last_lineage_identity,
        ):
            return MirrorReadResult(
                ok=True,
                payload=normalized.payload,
                last_seq=seq,
                epoch=normalized.epoch,
                detail="legacy epoch-0 lineage reset accepted because worker birth identity changed",
                lineage_identity=lineage_identity,
            )
        return MirrorReadResult(
            ok=False,
            error=ERROR_SEQ_REGRESSION,
            payload=normalized.payload,
            last_seq=seq,
            epoch=normalized.epoch,
            lineage_identity=lineage_identity,
            detail=(
                f"seq {seq} is not strictly greater than last_seq {last_seq} "
                f"within epoch {normalized.epoch!r}"
            ),
        )

    return normalized
