#!/usr/bin/env python3
"""Shared terminal status helpers for watcher and dispatcher finalization."""

from __future__ import annotations

from pathlib import Path
import re

import goalflight_dispatch_states
import goalflight_rate_pressure

RATE_LIMIT_TAIL_BYTES = 2048
FINAL_RECONCILIATION_TAIL_BYTES = 10 * 1024 * 1024
WORKER_DEATH_CAUSE_TAIL_BYTES = 16 * 1024

WORKER_DEATH_CAUSE_NO_EVIDENCE = "no_evidence"
_UPSTREAM_NETWORK_DEATH_SEQUENCE = (
    # Observed verbatim, in this order, in the 2026-08-24 network-death tail.
    "failed to lookup address information: nodename nor servname provided",
    "stream disconnected before completion",
    "error: reconnecting... 5/5",
)
# Lines that are MORE OF THE SAME network failure rather than the worker
# resuming work. Used only to decide whether the death evidence runs to the end
# of the tail; a line outside this set after the sequence completes means the
# worker carried on and died of something else.
_UPSTREAM_NETWORK_DEATH_NOISE = (
    "reconnecting",
    "stream disconnected before completion",
    "failed to lookup address information",
    "falling back from websockets",
)
_PROVIDER_LIMIT_DEATH_LINE_PATTERNS = (
    # Verbatim B054 provider tail fixture.
    re.compile(
        r"^error:\s*selected model is at capacity\. please try a different model\.$",
        re.IGNORECASE,
    ),
    # Verbatim usage-limit tail fixture; reset time is provider-controlled.
    re.compile(
        r"^(?:error:\s*)?you(?:'|’)ve hit your usage limit\. "
        r"please try again at\s+(?:"
        r"[0-9]{1,2}:[0-9]{2}\s*(?:am|pm)"
        r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+"
        r"[0-9]{1,2}(?:st|nd|rd|th)"
        r")\.?$",
        re.IGNORECASE,
    ),
)
_PROVIDER_LIMIT_DEATH_MENTION_FRAGMENT_GROUPS = (
    ("selected model is at capacity", "please try a different model"),
    ("you've hit your usage limit", "please try again at"),
    ("you’ve hit your usage limit", "please try again at"),
)
_PROVIDER_LIMIT_TOKEN_HEADER_PATTERN = re.compile(r"^tokens used:?$", re.IGNORECASE)
_PROVIDER_LIMIT_TOKEN_COUNT_PATTERN = re.compile(r"^[0-9][0-9,]*$")
# Compatibility import for callers that still name the historical umbrella.
RATE_LIMITED_STATE = goalflight_dispatch_states.LEGACY_RATE_LIMITED_STATE
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
# Own-signal attention is an ALLOWLIST. A line is the worker's own escalation
# only when the marker token sits at column 0 after these decorations:
# optional wrapping ` / * / **, optional ``!`` sigil, optional ``STATUS:``
# prefix, and (when *kimi_output*) the renderer bullet ``• `` or two-space
# continuation. Relay forms this repo actually produces — ``>``, ``- ``, ``* ``,
# ``+``, ``|``, ``1.``, tab, extra indent — are rejected because they are not
# on the list. Fence and hunk membership are caller context (a fenced
# ``BLOCKED:`` is byte-identical to an own-signal line).
#
# Start-anchored on purpose. A marker preceded by ANSI SGR codes or a
# timestamp is a PRE-EXISTING false negative; this predicate keeps those
# misses and does not add new ones.
_ATTENTION_KIND_ALTERNATION = "|".join(
    re.escape(kind) for kind in sorted(ATTENTION_MARKERS)
)
_OWN_SIGNAL_ATTENTION_RE = re.compile(
    rf"^`?\**{MARKER_SIGIL_OPT_RE}\**(?:STATUS:\s*)?"
    rf"({_ATTENTION_KIND_ALTERNATION}):(.*)$"
)


def _strip_marker_decoration(text: str) -> str:
    value = text.strip()
    while value.startswith("*") or value.startswith("`"):
        value = value[1:].lstrip()
    while value.endswith("*") or value.endswith("`"):
        value = value[:-1].rstrip()
    return value


def parse_own_signal_attention_line(
    raw_line: str,
    line_no: int = 0,
    *,
    kimi_output: bool = False,
) -> dict | None:
    """Parse *raw_line* as this worker's own attention emission, or None.

    This is the single form predicate for "did the sender emit this as its
    own terminal signal?". Verdict, harvest, and outbox must call it (or a
    scan that calls it) rather than growing per-surface rejectors.
    """
    if not raw_line:
        return None
    if raw_line.startswith("\t"):
        return None
    if raw_line.startswith(" "):
        kimi_continuation = (
            kimi_output
            and raw_line.startswith("  ")
            and not raw_line.startswith("   ")
        )
        if not kimi_continuation:
            return None
    stripped = raw_line.strip()
    if not stripped:
        return None
    if kimi_output and stripped.startswith("• "):
        stripped = stripped[2:].lstrip()
        if not stripped:
            return None
    match = _OWN_SIGNAL_ATTENTION_RE.match(stripped)
    if not match:
        return None
    kind = match.group(1)
    payload = match.group(2)
    if marker_is_template_example(kind, payload):
        return None
    return {
        "line": line_no,
        "kind": kind,
        "text": _strip_marker_decoration(payload)[:1000],
    }


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


# Compatibility export, not a second analyzer.
rate_limit_signature_in_text = goalflight_rate_pressure.rate_limit_signature_in_text


def classify_worker_death_text(text: object) -> str:
    """Classify explicit death evidence, failing closed on absence or conflict."""

    if not isinstance(text, str) or not text:
        return WORKER_DEATH_CAUSE_NO_EVIDENCE
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return WORKER_DEATH_CAUSE_NO_EVIDENCE
    lowered_lines = [line.casefold() for line in lines]
    matches: set[str] = set()
    # Each signature must appear WITHIN a line, and the signatures must appear
    # in order, in distinct lines.
    #
    # An earlier version required the last N lines to EQUAL the signatures.
    # That can never match real output: a real tail line carries a timestamp,
    # a module path and trailing context, so it contains the signature and is
    # never equal to it. The family it was written for -- the 2026-08-24
    # network death -- did not classify at all. The fixture agreed with the
    # implementation because both were built from a brief excerpt that had
    # been tidied down to bare signature lines, so the test could not catch it.
    #
    # The evidence must also run to the END of the tail. A worker that hits
    # network trouble, RECOVERS, and later dies of something else must not be
    # blamed on the network -- so once the sequence completes, every remaining
    # line has to be more of the same failure rather than the worker carrying
    # on. That anchoring is why the original suffix check existed; only its
    # use of equality was wrong.
    remaining = list(_UPSTREAM_NETWORK_DEATH_SEQUENCE)
    completed_at: int | None = None
    for index, lowered in enumerate(lowered_lines):
        if remaining and remaining[0] in lowered:
            remaining.pop(0)
            if not remaining:
                completed_at = index
                break
    if completed_at is not None and all(
        any(noise in lowered for noise in _UPSTREAM_NETWORK_DEATH_NOISE)
        for lowered in lowered_lines[completed_at + 1 :]
    ):
        matches.add("upstream_network")

    provider_line_indexes = [
        index
        for index, line in enumerate(lines)
        if any(pattern.fullmatch(line) for pattern in _PROVIDER_LIMIT_DEATH_LINE_PATTERNS)
    ]
    provider_suffix = lines[provider_line_indexes[-1] + 1 :] if provider_line_indexes else []
    provider_suffix_is_observed_footer = not provider_suffix or (
        len(provider_suffix) == 2
        and _PROVIDER_LIMIT_TOKEN_HEADER_PATTERN.fullmatch(provider_suffix[0])
        and _PROVIDER_LIMIT_TOKEN_COUNT_PATTERN.fullmatch(provider_suffix[1])
    )
    if provider_line_indexes and provider_suffix_is_observed_footer:
        matches.add("provider_limit")
    if len(matches) != 1:
        return WORKER_DEATH_CAUSE_NO_EVIDENCE
    return matches.pop()


def worker_death_causes_mentioned_in_text(text: object) -> set[str]:
    """Return known evidence families mentioned anywhere in untrusted prompt text."""

    if not isinstance(text, str) or not text:
        return set()
    lowered = re.sub(r"\s+", " ", text).casefold()
    causes: set[str] = set()
    if any(signature in lowered for signature in _UPSTREAM_NETWORK_DEATH_SEQUENCE):
        causes.add("upstream_network")
    if any(
        all(fragment in lowered for fragment in group)
        for group in _PROVIDER_LIMIT_DEATH_MENTION_FRAGMENT_GROUPS
    ):
        causes.add("provider_limit")
    return causes


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
    evidence = goalflight_rate_pressure.rate_limit_signature_in_text(excerpt)
    if not evidence:
        return state, reason, False
    probe = {"state": "worker_dead", "error": excerpt}
    if not goalflight_rate_pressure.detect_rate_limit_signature(probe, None):
        return state, reason, False
    limit_state = str(evidence.get("state") or goalflight_dispatch_states.LIMIT_UNKNOWN_STATE)
    payload = {
        "message": "dispatch_worker_limit_reached",
        "rate_limit_signature": evidence.get("limit_signature"),
        "limit_signature": evidence.get("limit_signature"),
        "limit_kind": evidence.get("limit_kind"),
        "limit_state": limit_state,
        "reset_at": evidence.get("reset_at"),
        "retry_after": evidence.get("retry_after"),
        "tail_excerpt": excerpt[-RATE_LIMIT_TAIL_BYTES:],
        "reason": reason,
    }
    if reason_extra:
        payload.update(reason_extra)
    return limit_state, payload, state != limit_state


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
    if isinstance(reason, dict) and reason.get("message") == "dispatch_worker_limit_reached":
        limit_state = str(reason.get("limit_state") or goalflight_dispatch_states.LIMIT_UNKNOWN_STATE)
        return limit_state, reason, state != limit_state
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
    if goalflight_dispatch_states.is_limit_state(state):
        return goalflight_dispatch_states.normalize_dispatch_state(state) or str(state)
    if state == "worker_dead":
        return "worker_dead"
    if isinstance(state, str) and state.startswith("blocked"):
        return "blocked"
    if state in {"orphaned", "controller_dead"}:
        return "controller_dead"
    if state == "idle_timeout":
        return "idle_timeout"
    return str(state or "terminal")
