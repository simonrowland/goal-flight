#!/usr/bin/env python3
"""Shared dispatch state vocabulary for status mirrors and fleet controllers."""

from __future__ import annotations

TERMINAL_SUCCESS_STATES = frozenset({"complete"})

TERMINAL_FAILURE_STATES = frozenset(
    {
        "error",
        "failed",
        "wedged",
        "blocked",
        "blocked_adapter_gate",
        "blocked_auth",
        "inconclusive_timeout",
        "worker_dead",
        "tool_timeout",
        "stalled",
        "remote_turn_silence",
        "failed_worktree",
    }
)

TERMINAL_STATES = TERMINAL_SUCCESS_STATES | TERMINAL_FAILURE_STATES

SALVAGE_NEEDED_STATES = frozenset(
    {
        "salvage_needed",
        "cleanup_needed",
    }
)

RUNNING_STATES = frozenset(
    {
        "starting",
        "running",
        "running_quiet",
        "waiting",
    }
)

DISPATCH_STATE_ALIASES = {
    "waiting_capacity": "waiting",
    "handshaking": "starting",
    "watcher_stopped": "running_quiet",
    "idle_timeout": "inconclusive_timeout",
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


def normalize_dispatch_state(state: object) -> str | None:
    if not isinstance(state, str) or not state:
        return None
    if state.startswith("blocked"):
        return "blocked"
    return DISPATCH_STATE_ALIASES.get(state, state)


def is_terminal_state(state: str | None) -> bool:
    return bool(state and (state in TERMINAL_STATES or state in SALVAGE_NEEDED_STATES))


def is_running_state(state: str | None) -> bool:
    return bool(state and state in RUNNING_STATES)


def state_seq_rank(state: object) -> int:
    if not isinstance(state, str):
        return 0
    if state == "watcher_stopped":
        return 45
    if state == "controller_dead":
        return 90
    normalized = normalize_dispatch_state(state)
    if normalized is None:
        return 0
    if normalized in TERMINAL_STATES:
        return 90
    return STATE_SEQ_RANKS.get(normalized, 50)


def terminal_state_for(state: object, reason: object = None) -> str:
    normalized = normalize_dispatch_state(state)
    if normalized == "complete":
        return "complete"
    if normalized == "worker_dead":
        return "worker_dead"
    if normalized == "inconclusive_timeout":
        return "idle_timeout"
    if state == "watcher_stopped":
        return "watcher_stopped"
    if state == "controller_dead" or (state == "orphaned" and reason == "controller_dead"):
        return "controller_dead"
    if normalized == "blocked":
        return "blocked"
    if normalized in TERMINAL_ERROR_STATES:
        return "error"
    return "unknown"
