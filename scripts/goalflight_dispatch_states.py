#!/usr/bin/env python3
"""Shared dispatch state vocabulary for status mirrors and fleet controllers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping


QUOTA_EXHAUSTED_STATE = "quota_exhausted"
TRANSIENT_THROTTLE_STATE = "transient_throttle"
LIMIT_UNKNOWN_STATE = "limit_unknown"
LEGACY_RATE_LIMITED_STATE = "rate_limited"

LIMIT_KIND_EXHAUSTED = "exhausted"
LIMIT_KIND_TRANSIENT = "transient"
LIMIT_KIND_UNKNOWN = "unknown"

LIMIT_STATE_FOR_KIND = {
    LIMIT_KIND_EXHAUSTED: QUOTA_EXHAUSTED_STATE,
    LIMIT_KIND_TRANSIENT: TRANSIENT_THROTTLE_STATE,
    LIMIT_KIND_UNKNOWN: LIMIT_UNKNOWN_STATE,
}
LIMIT_KIND_FOR_STATE = {
    QUOTA_EXHAUSTED_STATE: LIMIT_KIND_EXHAUSTED,
    TRANSIENT_THROTTLE_STATE: LIMIT_KIND_TRANSIENT,
    LIMIT_UNKNOWN_STATE: LIMIT_KIND_UNKNOWN,
    # Fleet ledgers already contain this value. It remains parseable history,
    # but it never acquires a more specific meaning retroactively.
    LEGACY_RATE_LIMITED_STATE: LIMIT_KIND_UNKNOWN,
}
LIMIT_TERMINAL_STATES = frozenset(LIMIT_KIND_FOR_STATE)

SUCCESS_TERMINAL_RECORD_STATES = frozenset({"complete", "released"})

FAILURE_TERMINAL_RECORD_STATES = frozenset(
    {
        "error",
        "failed",
        "blocked",
        "blocked_adapter_gate",
        "blocked_auth",
        "blocked_capacity",
        "blocked_completion_authority",
        "blocked_session_limit",
        "blocked_windows_dispatch",
        "inconclusive_timeout",
        "inconclusive_no_final",
        "worker_dead",
        "tool_timeout",
        "stalled",
        "remote_turn_silence",
        "failed_worktree",
        "controller_dead",
        "orphaned",
        "superseded",
    }
) | LIMIT_TERMINAL_STATES

WEDGED_TERMINAL_RECORD_STATES = frozenset({"idle_timeout", "wedged"})

TERMINAL_SUCCESS_STATES = SUCCESS_TERMINAL_RECORD_STATES

TERMINAL_FAILURE_STATES = FAILURE_TERMINAL_RECORD_STATES | WEDGED_TERMINAL_RECORD_STATES

TERMINAL_STATES = TERMINAL_SUCCESS_STATES | TERMINAL_FAILURE_STATES

SALVAGE_NEEDED_STATES = frozenset(
    {
        "salvage_needed",
        "cleanup_needed",
    }
)

# States a worker enters when it has STOPPED to wait for a person. These are
# written by the ACP runner from real protocol events -- a permission request,
# a routed confirmation -- so unlike a marker scraped out of worker output they
# cannot be forged by ordinary prose. They are terminal for waiting purposes:
# the worker will make no further progress until someone answers.
#
# They were previously in NO set at all -- not terminal, not running, not
# salvage -- so done_code() fell through to "ambiguous" and `--wait` polled
# until its timeout. Every ACP worker that asked for approval was invisible to
# the primitive whose whole job is to deliver that request.
ATTENTION_STATES = frozenset(
    {
        "running_user_confirm",
        "awaiting_user_confirm",
        "awaiting_permission",
    }
)


def is_attention_state(state: object) -> bool:
    """True when the worker is parked waiting for a human decision."""
    if isinstance(state, str) and state in ATTENTION_STATES:
        return True
    normalized = DISPATCH_STATE_ALIASES.get(state) if isinstance(state, str) else None
    return bool(normalized and normalized in ATTENTION_STATES)

RUNNING_STATES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "running_quiet",
        "waiting",
    }
)

DISPATCH_STATE_ALIASES = {
    "queued": "waiting",
    "waiting_capacity": "waiting",
    "handshaking": "starting",
    "idle_timeout": "inconclusive_timeout",
    "watcher_stopped": "running_quiet",
    # Normalized presentation must not imply that an old record measured a
    # transient throttle. The original spelling is still preserved by
    # terminal_state_for() when a ledger round-trips history.
    LEGACY_RATE_LIMITED_STATE: LIMIT_UNKNOWN_STATE,
}

TERMINAL_ERROR_STATES = frozenset(
    {
        "error",
        "failed",
        "wedged",
        "tool_timeout",
        "stalled",
        "remote_turn_silence",
        "failed_worktree",
    }
)

STATE_SEQ_RANKS = {
    "waiting": 10,
    "starting": 20,
    "running": 30,
    "running_quiet": 40,
}

AMBIGUOUS_LIVE_CLASSES = frozenset({"unknown_no_pid", "identity_indeterminate", "unknown"})

LIVENESS_RECHECK_STATES = frozenset({"idle_timeout", "watcher_stopped"})

OUTPUT_TAIL_RECONCILE_STATES = frozenset(
    {
        "worker_dead",
        "watcher_stopped",
        "idle_timeout",
        "inconclusive_timeout",
    }
) | LIMIT_TERMINAL_STATES

# Pre-limit states whose terminal tail may still supply the evidence needed to
# classify a limit. Kept here because this module is the state-set authority.
LIMIT_RECONCILE_INPUT_STATES = frozenset(
    {
        "idle_timeout",
        "inconclusive_timeout",
        "running_quiet",
        "stalled",
        "watcher_stopped",
        "wedged",
        "worker_dead",
    }
) | LIMIT_TERMINAL_STATES


def normalize_dispatch_state(state: object) -> str | None:
    if not isinstance(state, str) or not state:
        return None
    if state.startswith("blocked"):
        return "blocked"
    return DISPATCH_STATE_ALIASES.get(state, state)


def is_terminal_state(state: str | None) -> bool:
    if isinstance(state, str) and (state in TERMINAL_STATES or state in SALVAGE_NEEDED_STATES):
        return True
    normalized = normalize_dispatch_state(state)
    return bool(normalized and (normalized in TERMINAL_STATES or normalized in SALVAGE_NEEDED_STATES))


def is_running_state(state: str | None) -> bool:
    normalized = normalize_dispatch_state(state)
    return bool(normalized and normalized in RUNNING_STATES)


def is_limit_state(state: object) -> bool:
    return isinstance(state, str) and state in LIMIT_TERMINAL_STATES


def limit_state_for_kind(kind: object) -> str:
    return LIMIT_STATE_FOR_KIND.get(str(kind or ""), LIMIT_UNKNOWN_STATE)


def limit_kind_for_state(state: object) -> str | None:
    if not isinstance(state, str):
        return None
    return LIMIT_KIND_FOR_STATE.get(state)


def limit_kind_for_record(record: Mapping[str, object] | None) -> str | None:
    if not isinstance(record, Mapping):
        return None
    if any(
        record.get(key) == LEGACY_RATE_LIMITED_STATE
        for key in ("state", "terminal_state", "classification")
    ):
        return LIMIT_KIND_UNKNOWN
    for source in (
        record,
        record.get("reason"),
        record.get("error"),
        record.get("outcome"),
    ):
        if isinstance(source, Mapping):
            kind = source.get("limit_kind")
            if kind in LIMIT_STATE_FOR_KIND:
                return str(kind)
    for key in ("state", "terminal_state", "classification"):
        kind = limit_kind_for_state(record.get(key))
        if kind:
            return kind
    return None


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _evidence_value(record: Mapping[str, object], key: str) -> object:
    for source in (
        record,
        record.get("reason"),
        record.get("error"),
        record.get("outcome"),
    ):
        if isinstance(source, Mapping) and source.get(key) not in (None, ""):
            return source.get(key)
    return None


def retry_policy_for_record(
    record: Mapping[str, object] | None,
    *,
    now: float,
) -> dict[str, object] | None:
    """Return the measured retry decision for one terminal limit record.

    ``eligible`` is deliberately tri-state. ``None`` means the legacy/unknown
    cooldown policy remains authoritative; it is not evidence that immediate
    retry is safe.
    """

    kind = limit_kind_for_record(record)
    if kind is None or not isinstance(record, Mapping):
        return None

    reset_value = _evidence_value(record, "reset_at")
    reset_ts = _timestamp(reset_value)
    retry_after_value = _evidence_value(record, "retry_after")
    retry_after = None
    if isinstance(retry_after_value, (int, float)) and not isinstance(retry_after_value, bool):
        retry_after = max(0.0, float(retry_after_value))
    elif isinstance(retry_after_value, str):
        try:
            retry_after = max(0.0, float(retry_after_value.strip()))
        except ValueError:
            retry_after = None

    observed_ts = None
    for key in ("ended_at", "updated_at", "observed_at", "started_at"):
        observed_ts = _timestamp(record.get(key))
        if observed_ts is not None:
            break

    if kind == LIMIT_KIND_EXHAUSTED:
        return {
            "kind": kind,
            "eligible": bool(reset_ts is not None and now >= reset_ts),
            "not_before": reset_value if reset_ts is not None and now < reset_ts else None,
            "mode": "retry_after_reset" if reset_ts is not None else "hold_reset_unknown",
        }

    if kind == LIMIT_KIND_TRANSIENT:
        not_before_ts = (
            observed_ts + retry_after
            if retry_after is not None and observed_ts is not None
            else None
        )
        return {
            "kind": kind,
            "eligible": not_before_ts is None or now >= not_before_ts,
            "not_before": (
                datetime.fromtimestamp(not_before_ts, tz=timezone.utc).isoformat()
                if not_before_ts is not None and now < not_before_ts
                else None
            ),
            "mode": "retry_soon",
        }

    # Unknown includes historical rate_limited rows. Preserve the existing
    # cooldown path rather than upgrading ambiguity into either conclusion.
    cooldown = _evidence_value(record, "not_retryable_before")
    if cooldown in (None, ""):
        cooldown = reset_value
    return {
        "kind": LIMIT_KIND_UNKNOWN,
        "eligible": None,
        "not_before": cooldown,
        "mode": "legacy_cooldown",
    }


def state_seq_rank(state: object) -> int:
    if not isinstance(state, str):
        return 0
    if state == "watcher_stopped":
        return 45
    if state == "controller_dead":
        return 90
    if state in TERMINAL_STATES:
        return 90
    normalized = normalize_dispatch_state(state)
    if normalized is None:
        return 0
    if normalized in TERMINAL_STATES:
        return 90
    return STATE_SEQ_RANKS.get(normalized, 50)


def terminal_state_for(state: object, reason: object = None) -> str:
    if state in SUCCESS_TERMINAL_RECORD_STATES:
        return "complete"
    if state == "worker_dead":
        return "worker_dead"
    if is_limit_state(state):
        return str(state)
    if state == "idle_timeout" or state == "inconclusive_timeout":
        return "idle_timeout"
    if state == "watcher_stopped":
        return "watcher_stopped"
    if state == "controller_dead" or (state == "orphaned" and reason == "controller_dead"):
        return "controller_dead"
    if isinstance(state, str) and state.startswith("blocked"):
        return "blocked"
    if state in TERMINAL_ERROR_STATES:
        return "error"
    if state in FAILURE_TERMINAL_RECORD_STATES:
        return state
    return "unknown"
