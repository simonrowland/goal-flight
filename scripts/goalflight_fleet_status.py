#!/usr/bin/env python3
"""Pure dispatch row classification for fleet status (Track A goals 9b/9d).

Classifies in-flight dispatch rows from SSH reachability, mirrored status JSON,
lease snapshot, and PID hints without mutating locks or performing SSH I/O.

Release authority (plan of record: ``docs-private/plans/multi-workstation-acp-fleet.md``,
§ *Release predicate*, item **#2**):

    Never release on mirror stale alone while last-known PID/lease says active.

``may_release_locks`` implements the controller-side guard for reconcile/doctor (10b/10c).
Mirror-stale rows — ``quarantine_reason == mirror_stale`` — always return False even if
classification is ``quarantined`` or ``unknown``. Terminal remote status and reconciling
rows with unreadable mirror + dead PID may return True (prefigures 10b decision table).

See also: Phase 1 queue slice 9d in
``docs-private/goal-queue-phase1-remote-observe-dispatch-2026-05-24.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import goalflight_fleet_mirror as mirror

PidHint = Literal["alive", "dead", "unknown"]

TERMINAL_STATES = frozenset(
    {
        "complete",
        "error",
        "failed",
        "wedged",
        "blocked",
        "blocked_adapter_gate",
        "blocked_auth",
        "inconclusive_timeout",
        "worker_dead",
    }
)
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

QUARANTINE_SSH_PARTITION = "ssh_partition"
QUARANTINE_MIRROR_STALE = "mirror_stale"
QUARANTINE_MIRROR_UNREADABLE = "mirror_unreadable"
QUARANTINE_LEASE_MISSING = "lease_missing_incident"


@dataclass(frozen=True)
class DispatchClassification:
    state: str
    quarantine_reason: str | None = None


def is_terminal_state(state: str | None) -> bool:
    return bool(state and (state in TERMINAL_STATES or state in SALVAGE_NEEDED_STATES))


def is_running_state(state: str | None) -> bool:
    return bool(state and state in RUNNING_STATES)


def remote_state_from_mirror(result: mirror.MirrorReadResult | None) -> str | None:
    if result is None or not result.ok or not result.payload:
        return None
    state = result.payload.get("state")
    return state if isinstance(state, str) else None


def classify_dispatch_row(
    *,
    ssh_reachable: bool,
    mirror: mirror.MirrorReadResult | None = None,
    mirror_stale: bool = False,
    lease_active: bool = False,
    pid_hint: PidHint = "unknown",
) -> DispatchClassification:
    """Classify one in-flight dispatch row without mutating locks."""
    remote_state = remote_state_from_mirror(mirror)

    if not ssh_reachable:
        return DispatchClassification("unknown", quarantine_reason=QUARANTINE_SSH_PARTITION)

    if mirror_stale:
        if pid_hint == "alive" and is_running_state(remote_state):
            return DispatchClassification("quarantined", quarantine_reason=QUARANTINE_MIRROR_STALE)
        return DispatchClassification("unknown", quarantine_reason=QUARANTINE_MIRROR_STALE)

    if mirror is not None and not mirror.ok:
        if lease_active and pid_hint == "dead":
            return DispatchClassification("reconciling", quarantine_reason=QUARANTINE_MIRROR_UNREADABLE)
        if lease_active:
            return DispatchClassification("unknown", quarantine_reason=QUARANTINE_MIRROR_UNREADABLE)
        return DispatchClassification("unknown", quarantine_reason=QUARANTINE_MIRROR_UNREADABLE)

    if remote_state in SALVAGE_NEEDED_STATES:
        return DispatchClassification("salvage")

    if is_terminal_state(remote_state):
        return DispatchClassification("terminal")

    if is_running_state(remote_state):
        if lease_active and pid_hint == "alive":
            return DispatchClassification("running")
        if not lease_active and pid_hint == "alive":
            return DispatchClassification("quarantined", quarantine_reason=QUARANTINE_LEASE_MISSING)
        if lease_active and pid_hint == "dead":
            return DispatchClassification("reconciling", quarantine_reason=QUARANTINE_MIRROR_UNREADABLE)

    if lease_active and remote_state is None and pid_hint == "dead":
        return DispatchClassification("reconciling", quarantine_reason=QUARANTINE_MIRROR_UNREADABLE)

    if lease_active:
        return DispatchClassification("unknown", quarantine_reason=QUARANTINE_MIRROR_UNREADABLE)

    return DispatchClassification("unknown")


def may_release_locks(classification: DispatchClassification) -> bool:
    """Return whether reconcile/doctor may release locks for this classification.

    Enforces plan release predicate #2: deny when ``quarantine_reason`` is
    ``mirror_stale`` regardless of ``state``. Allow ``terminal`` and select
    ``reconciling`` + ``mirror_unreadable`` paths only.
    """
    if classification.state == "released":
        return True
    if classification.state == "salvage":
        return False
    if classification.state != "reconciling" and classification.state != "terminal":
        return False
    if classification.quarantine_reason == QUARANTINE_MIRROR_STALE:
        return False
    if classification.state == "terminal":
        return True
    if classification.state == "reconciling" and classification.quarantine_reason == QUARANTINE_MIRROR_UNREADABLE:
        return True
    return False
