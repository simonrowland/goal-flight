#!/usr/bin/env python3
"""Shared dispatch state vocabulary for status mirrors and fleet controllers."""

from __future__ import annotations

SUCCESS_TERMINAL_RECORD_STATES = frozenset({"complete", "released"})

FAILURE_TERMINAL_RECORD_STATES = frozenset(
    {
        "error",
        "failed",
        "blocked",
        "blocked_adapter_gate",
        "blocked_auth",
        "blocked_capacity",
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
        "rate_limited",
        "superseded",
    }
)

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
        "rate_limited",
    }
)


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
    if state == "rate_limited":
        return "rate_limited"
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
