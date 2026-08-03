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
# Markers that mean "I need the controller", as opposed to "I am finished".
# The distinction decides whose word wins when a marker and a live worker
# disagree -- see attention_marker_present() below.
#
# FAILED belongs here, not with the completion markers: the marker contract
# (protocols/worker-markers.md) says FAILED stops the dispatch loop and
# surfaces to the controller. A worker that reports a real failure and then
# hangs in teardown is still a worker whose work has failed -- the live
# process does not invalidate the report, so liveness must not suppress it.
ATTENTION_MARKERS = {"BLOCKED", "USER-NEED", "USER-CONFIRM", "FAILED"}
TEMPLATE_GUARDED_MARKERS = {"BLOCKED", "USER-NEED", "USER-CONFIRM"}
TERMINAL_MARKERS = SUCCESS_TERMINAL_MARKERS | ATTENTION_MARKERS
TOKEN_COUNT_RE = re.compile(r"^\d[\d,]*$")
TEMPLATE_PLACEHOLDER_RE = re.compile(r"<[^<>\r\n]+>")
FENCE_RUN_RE = re.compile(r"^[ \t]*(?P<run>`{3,}|~{3,})(?P<rest>.*)$")
# One optional worker-marker sigil grammar shared by every parser. Keep this as
# a regex fragment so each consumer can preserve its existing markdown/fence/
# position rules around the marker token.
MARKER_SIGIL = "!"
MARKER_SIGIL_OPT_RE = rf"{re.escape(MARKER_SIGIL)}?"
STEER_ACK_RE = re.compile(
    rf"^\**{MARKER_SIGIL_OPT_RE}\**STEER-ACK:\**\s*(\d+)\b"
)


class MarkdownFenceTracker:
    """Track fenced regions without letting a different delimiter close them."""

    def __init__(self) -> None:
        self._delimiter = ""
        self._minimum_length = 0

    @property
    def in_fence(self) -> bool:
        return bool(self._delimiter)

    def consume_boundary(self, raw_line: str) -> bool:
        """Consume a matching fence boundary and report whether the line was one."""

        match = FENCE_RUN_RE.match(raw_line)
        if not match:
            return False
        run = match.group("run")
        rest = match.group("rest")
        if not self.in_fence:
            self._delimiter = run[0]
            self._minimum_length = len(run)
            return True
        if (
            run[0] == self._delimiter
            and len(run) >= self._minimum_length
            and not rest.strip()
        ):
            self._delimiter = ""
            self._minimum_length = 0
            return True
        return False


def marker_payload_has_template_placeholder(payload: object) -> bool:
    """Return whether a payload still contains a documented ``<...>`` token."""

    return bool(TEMPLATE_PLACEHOLDER_RE.search(str(payload or "")))


def marker_is_template_example(kind: object, payload: object) -> bool:
    """Reject unsubstituted escalation templates, never completion/failure markers."""

    return kind in TEMPLATE_GUARDED_MARKERS and marker_payload_has_template_placeholder(payload)


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
        if pattern in lowered:
            return pattern
    # Numeric HTTP statuses are context-sensitive in the provider-pressure
    # scanner, so they deliberately are not substring entries in
    # RATE_LIMIT_PATTERNS. The terminal-tail scanner still needs to recognize
    # their standalone token form. Keeping this outside the pattern loop is
    # load-bearing: the old in-loop special case was unreachable.
    for pattern in ("429", "529"):
        if re.search(rf"(?<!\d){re.escape(pattern)}(?!\d)", lowered):
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


def attention_marker_present(marker: object) -> bool:
    """True for a marker whose whole point is that the worker is still there.

    A live worker CONTRADICTS a completion marker -- ``COMPLETE:`` from a
    process that is still running is exactly the false-done case, so liveness
    has to outrank the marker there. A live worker CONFIRMS an attention
    marker: ``USER-CONFIRM:``/``USER-NEED:``/``BLOCKED:`` are emitted by a
    worker that is alive precisely because it stopped to wait for someone.
    Applying the completion rule to these suppressed the verdict and left
    ``--wait`` blocking on a worker that was asking for help.

    Markers are scraped from worker output, so ordinary text can forge one.
    That is tolerable here because the costs are asymmetric: waking a
    controller that did not need waking costs one status read, while failing
    to wake one that did costs the whole wait timeout with the worker parked.
    Bias toward waking.
    """
    return isinstance(marker, dict) and marker.get("kind") in ATTENTION_MARKERS


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
