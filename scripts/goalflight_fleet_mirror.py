#!/usr/bin/env python3
"""Read and validate fleet dispatch status mirrors (Track A goal 9a).

Mirrors must be complete JSON documents with schema ``goalflight.acp-run.v1`` and a
strictly increasing ``seq`` when compared against a previously observed sequence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATUS_MIRROR_SCHEMA = "goalflight.acp-run.v1"
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

    schema = payload.get("schema")
    if schema != STATUS_MIRROR_SCHEMA:
        return MirrorReadResult(
            ok=False,
            error=ERROR_SCHEMA_MISMATCH,
            detail=f"expected schema {STATUS_MIRROR_SCHEMA!r}, got {schema!r}",
        )

    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        return MirrorReadResult(
            ok=False,
            error=ERROR_SCHEMA_MISMATCH,
            detail=f"missing required fields: {', '.join(missing)}",
        )

    seq = _coerce_seq(payload.get("seq"))
    if seq is None or seq < 0:
        return MirrorReadResult(
            ok=False,
            error=ERROR_SCHEMA_MISMATCH,
            detail="seq must be a non-negative integer",
        )

    if last_seq is not None and seq <= last_seq:
        return MirrorReadResult(
            ok=False,
            error=ERROR_SEQ_REGRESSION,
            payload=payload,
            last_seq=seq,
            detail=f"seq {seq} is not strictly greater than last_seq {last_seq}",
        )

    return MirrorReadResult(ok=True, payload=payload, last_seq=seq)
