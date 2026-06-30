#!/usr/bin/env python3
"""Shared terminal status helpers for watcher and dispatcher finalization."""

from __future__ import annotations

from pathlib import Path
import re

import goalflight_rate_pressure

RATE_LIMIT_TAIL_BYTES = 2048
RATE_LIMITED_STATE = "rate_limited"
SUCCESS_TERMINAL_MARKERS = {"COMPLETE", "READY", "RESULT"}


def read_tail_excerpt(path: Path, max_bytes: int = RATE_LIMIT_TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - max_bytes))
            return f.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def rate_limit_signature_in_text(text: str) -> str | None:
    lowered = text.lower()
    for pattern in goalflight_rate_pressure.RATE_LIMIT_PATTERNS:
        if pattern in {"429", "529"}:
            if re.search(rf"(?<!\d){re.escape(pattern)}(?!\d)", lowered):
                return pattern
            continue
        if pattern in lowered:
            return pattern
    for pattern in goalflight_rate_pressure.MODEL_CAPACITY_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def terminal_success_marker_present(marker: object) -> bool:
    return isinstance(marker, dict) and marker.get("kind") in SUCCESS_TERMINAL_MARKERS


def terminal_rate_limit_outcome(
    state: str | None,
    reason: object,
    tail: Path,
    *,
    success_marker_present: bool = False,
) -> tuple[str | None, object, bool]:
    if isinstance(reason, dict) and reason.get("message") == "dispatch_worker_rate_limited":
        return RATE_LIMITED_STATE, reason, state != RATE_LIMITED_STATE
    if success_marker_present:
        return state, reason, False
    excerpt = read_tail_excerpt(tail).strip()
    if not excerpt:
        return state, reason, False
    signature = rate_limit_signature_in_text(excerpt)
    if not signature:
        return state, reason, False
    probe = {"state": "worker_dead", "error": excerpt}
    if not goalflight_rate_pressure.detect_rate_limit_signature(probe, None):
        return state, reason, False
    return (
        RATE_LIMITED_STATE,
        {
            "message": "dispatch_worker_rate_limited",
            "rate_limit_signature": signature,
            "tail_excerpt": excerpt,
            "reason": reason,
        },
        True,
    )


def terminal_liveness_state(state: object) -> str:
    if state == "complete":
        return "completed"
    if state == RATE_LIMITED_STATE:
        return RATE_LIMITED_STATE
    if state == "worker_dead":
        return "worker_dead"
    if isinstance(state, str) and state.startswith("blocked"):
        return "blocked"
    if state in {"orphaned", "controller_dead"}:
        return "controller_dead"
    if state == "idle_timeout":
        return "idle_timeout"
    return str(state or "terminal")
