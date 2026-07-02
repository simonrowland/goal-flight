#!/usr/bin/env python3
"""Shared terminal status helpers for watcher and dispatcher finalization."""

from __future__ import annotations

from pathlib import Path
import re

import goalflight_rate_pressure

RATE_LIMIT_TAIL_BYTES = 2048
FINAL_RECONCILIATION_TAIL_BYTES = 10 * 1024 * 1024
RATE_LIMITED_STATE = "rate_limited"
SUCCESS_TERMINAL_MARKERS = {"COMPLETE", "READY", "RESULT"}
TERMINAL_MARKERS = SUCCESS_TERMINAL_MARKERS | {"FAILED", "BLOCKED", "USER-NEED", "USER-CONFIRM"}
TOKEN_COUNT_RE = re.compile(r"^\d[\d,]*$")


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


def _rate_limit_outcome_from_text(
    state: str | None,
    reason: object,
    text: str,
    *,
    reason_extra: dict | None = None,
) -> tuple[str | None, object, bool]:
    excerpt = text.strip()
    if not excerpt:
        return state, reason, False
    signature = rate_limit_signature_in_text(excerpt)
    if not signature:
        return state, reason, False
    probe = {"state": "worker_dead", "error": excerpt}
    if not goalflight_rate_pressure.detect_rate_limit_signature(probe, None):
        return state, reason, False
    payload = {
        "message": "dispatch_worker_rate_limited",
        "rate_limit_signature": signature,
        "tail_excerpt": excerpt[-RATE_LIMIT_TAIL_BYTES:],
        "reason": reason,
    }
    if reason_extra:
        payload.update(reason_extra)
    return RATE_LIMITED_STATE, payload, state != RATE_LIMITED_STATE


def terminal_success_marker_present(marker: object) -> bool:
    return isinstance(marker, dict) and marker.get("kind") in SUCCESS_TERMINAL_MARKERS


def terminal_marker_present(marker: object) -> bool:
    return isinstance(marker, dict) and marker.get("kind") in TERMINAL_MARKERS


def terminal_rate_limit_outcome(
    state: str | None,
    reason: object,
    tail: Path,
    *,
    success_marker_present: bool = False,
    terminal_marker_present: bool = False,
) -> tuple[str | None, object, bool]:
    if isinstance(reason, dict) and reason.get("message") == "dispatch_worker_rate_limited":
        return RATE_LIMITED_STATE, reason, state != RATE_LIMITED_STATE
    if terminal_marker_present or success_marker_present:
        return state, reason, False
    excerpt = read_tail_excerpt(tail).strip()
    return _rate_limit_outcome_from_text(state, reason, excerpt)


def _tokens_used_death_footer(nonempty_lines: list[str]) -> bool:
    return bool(
        len(nonempty_lines) >= 2
        and nonempty_lines[-2].lower() == "tokens used"
        and TOKEN_COUNT_RE.match(nonempty_lines[-1])
    )


def final_reconciliation_error_veto_outcome(
    state: str | None,
    reason: object,
    tail: Path,
    terminal_marker: object,
) -> tuple[str | None, object, bool]:
    if not terminal_success_marker_present(terminal_marker):
        return state, reason, False
    if "final_reconciliation" not in str(reason):
        return state, reason, False
    try:
        marker_line = int(terminal_marker.get("line") or 0)  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        return state, reason, False
    if marker_line <= 0:
        return state, reason, False
    text = read_tail_excerpt(tail, FINAL_RECONCILIATION_TAIL_BYTES)
    lines = text.splitlines()
    if marker_line >= len(lines):
        return state, reason, False
    after_marker = lines[marker_line:]
    nonempty_after_marker = [line.strip() for line in after_marker if line.strip()]
    if not _tokens_used_death_footer(nonempty_after_marker):
        return state, reason, False
    return _rate_limit_outcome_from_text(
        state,
        reason,
        "\n".join(after_marker),
        reason_extra={"vetoed_terminal_marker": terminal_marker},
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
