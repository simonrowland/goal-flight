#!/usr/bin/env python3
"""goalflight_dispatch.py — crash-safe worker dispatch with a decoupled watcher.

ONE command that dispatches a worker in the background by default, returns
immediately after launch, and leaves status/tail pointers for follow-up. Use
the obscure `--foreground` opt-in only for scripts/tests that need the dispatch
process to block until terminal state. It fixes the
"orchestrator hangs because the worker crashed/hung and never sent a wakeup" class
(observed 2026-05-30).

Easy path (agent preset — the common case):
    python3 goalflight_dispatch.py --agent codex --prompt-file p.md --cwd .      # background/default
    python3 goalflight_dispatch.py --agent codex --prompt-file p.md --read-only   # review/analysis
    python3 goalflight_dispatch.py --agent grok-code --prompt-file p.md --cwd .
    python3 goalflight_dispatch.py --agent grok-code --prompt-file p.md --cwd . --worktree HEAD  # pooled seat
    python3 goalflight_dispatch.py --agent codex --prompt-file p.md --cwd . --foreground  # synchronous scripts/tests

After launch, stderr is one line (dispatch id + status path). DISPATCH-LAUNCHED
JSON on stdout carries the rest. Pass --hints for the teaching block (status /
wait / done commands, and why not to hand-roll ps/tail -f/backgrounded
watchers — they race the worker and exit early). Argument errors are one line;
--help stays the verbose map.

Presets bake in the canonical NON-INTERACTIVE + SAFE flags per worker, so you
never spell them out (and cannot fat-finger `--dangerously-bypass`). Paths and a
dispatch id are auto-derived under the state dir; override with --tail /
--status-json / --dispatch-id if you want.

Durable queue path:
    python3 goalflight_dispatch.py --submit --drain-on-submit --agent codex --prompt-file p.md --cwd .

Escape hatch (any worker): pass the raw command after `--`:
    python3 goalflight_dispatch.py --agent custom --tail t --status-json s -- <cmd...>

How it stays crash-safe (validated):
  1. The worker is launched by a short detached helper, then reparented into its
     own session/process-group so launcher teardown cannot reap it.
  2. The worker is not this dispatcher's child after launch; the helper is
     reaped immediately and the platform supervisor reaps the worker on exit.
  3. The decoupled watcher (goalflight_watch.py) detects finished(0)/crashed(1)/
     hung(2)/controller-dead(3)/blocked(4). In default background mode, status
     tooling reports that result. With --foreground, this dispatcher waits and
     exits with the watcher's code unchanged for synchronous callers.

Cross-platform: pure stdlib; the watcher uses goalflight_compat.pid_alive, so
this is also the dispatch path on Windows (where the bash watcher is refused).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
import hashlib
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback path
    fcntl = None
import io
import json
import os
import re
import select
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import goalflight_compat
import goalflight_output_redact
import goalflight_capacity
import goalflight_codex_sessions
import goalflight_dispatch_paths
import goalflight_dispatch_states
import goalflight_engine_sessions
import goalflight_fleet_billing
import goalflight_fs
import goalflight_steer_mailbox
import goalflight_ledger
import goalflight_journal
import goalflight_quota_stuck
import goalflight_session_status
import goalflight_task
import goalflight_terminal
import goalflight_worktree_pool
from goalflight_agent_limits import moonshot_family
from goalflight_codex_sandbox import codex_workspace_write_args
from goalflight_liveness import (
    active_monotonic,
    process_group_id,
    resolve_indeterminate_timeout_s,
    write_status,
)
from goalflight_watch import (
    SUCCESS_TERMINAL_MARKERS,
    _final_terminal_marker,
    _last_line_is_terminal_marker,
    _marker_state as _marker_state_for_terminal,
    _terminal_marker_matches_dispatch,
    extract_markers,
)
import grok_permission_mode

SCRIPT_DIR = Path(__file__).resolve().parent
WATCH_PY = SCRIPT_DIR / "goalflight_watch.py"
# NOTE: no module-level watch-dispatch-tail.sh constant. It would name the
# GENERATING copy, and its only consumer now builds the path from the
# advertised skill root instead (see _status_reminder_lines). Re-adding a
# SCRIPT_DIR-derived constant here would quietly re-open that regression.
DAEMON_SPAWN_ARG = "__goalflight_spawn_daemon"
DISPATCH_QUEUE_SCHEMA = "goalflight.dispatch-queue.v1"
QUEUE_CLAIM_STALE_S = 300.0
LAUNCH_TIMEOUT_S = QUEUE_CLAIM_STALE_S
# Drain launch-confirmation wait. Per-entry subprocess.run used to use this
# as a serial timeout, so N hung entries took ~N*45s per pass. The pass
# budget is one confirmation window; leftover entries stay queued.
DRAIN_LAUNCH_CONFIRM_S = 45.0
DRAIN_LAUNCH_TIMEOUT_FLOOR_S = 20.0
LAUNCH_BACKOFF_INITIAL_S = 60.0
LAUNCH_BACKOFF_CAP_S = 900.0
# Three independent, provably pre-worker failures are enough to distinguish a
# dead carrier from a one-pass transient while keeping the retry window bounded.
# Capacity refusals are deferrals, not failed launch attempts, and do not spend
# this budget.
MAX_DRAIN_PRE_WORKER_FAILURES = 3
ABANDONED_RECONCILE_STALE_S = QUEUE_CLAIM_STALE_S
ABANDONED_RECONCILIATION_SCHEMA = "goalflight.abandoned-reconciliation.v1"
MAX_CLAIM_RECOVERY_REQUEUES = 1
MAX_COMPLETION_AUTHORITY_DEFERRALS = 1
# Durable skip of the expensive reconcile ladder for proven-dead carriers
# that recovery correctly refuses to reclaim (SC-153: missing task linkage
# is never orphan evidence). Without this, each drain pass re-pays the full
# per-carrier cost and recovery_s grows with the corpse set.
RECOVERY_PROBE_SCHEMA = "goalflight.recovery-probe.v1"
RECOVERY_UNRESOLVED_KEEP_REASONS = frozenset(
    {
        "unlinked_quarantine_deferred",
        "unlinked_restore_blocked",
        "unlinked_accounting_insufficient",
    }
)
# Event that releases a carrier parked because the task store or ledger could
# not be read. Recovery re-evaluates completion authority on every pass; a
# readable authority either restores for launch, supersedes, or parks again.
COMPLETION_AUTHORITY_UNPARK_EVENT = "completion_authority_readable"
# Missing/unparseable completion times cannot become readable on their own.
# First pass parks out loud; the next still-unreadable pass terminalizes to
# this blocked state instead of sitting in queued/claimed forever.
COMPLETION_AUTHORITY_TIMESTAMP_UNREADABLE = "completion_authority_timestamp_unreadable"
BLOCKED_COMPLETION_AUTHORITY_STATE = "blocked_completion_authority"
COMPLETION_AUTHORITY_PARK_REASONS = frozenset(
    {
        "completion_authority_unavailable",
        "completion_authority_token_indeterminate",
        COMPLETION_AUTHORITY_TIMESTAMP_UNREADABLE,
    }
)
RECONCILE_DOWNSTREAM_LOCK_BUDGET_S = 0.100
RECONCILE_LOCK_POLL_S = 0.010
PRELAUNCH_CANDIDATE_STATES = frozenset(
    {"queued", "waiting_capacity", "starting", "submitted", "claimed"}
)
# Ledger rows that have not crossed worker spawn. `starting` is excluded:
# it is recorded after capacity is acquired, immediately before spawn intent.
PRE_WORKER_LEDGER_STATES = frozenset(
    {"queued", "waiting_capacity", "submitted", "claimed"}
)
QUEUE_PRIORITY_RANK = {lane: rank for rank, lane in enumerate(goalflight_capacity.PRIORITY_LANES)}
QUEUE_DEFAULT_PRIORITY = "normal"
PRESET_AGENTS = {
    "codex",
    "grok-code",
    "grok-research",
    "moonshot",
    "cursor",
    "cursor-agent",
    "claude",
}
STDIN_PROMPT_AGENTS = {
    "codex",
    "grok-code",
    "grok-research",
    "moonshot",
    "cursor",
    "cursor-agent",
    "claude",
}
# Quiet is not death, and this threshold is a BACKSTOP, not a liveness test.
# Reaching it does not kill the worker -- the watcher records the verdict and
# exits while the process keeps running, which is how a fleet accumulates
# orphans that burn tokens with nobody listening. An abandoned live worker is
# strictly worse than a killed one, so the threshold must be generous.
#
# It can afford to be. Controllers are woken by delivered events, not by this
# expiring, so a long backstop costs latency nowhere: the only thing a short
# value buys is a false terminal verdict on a worker that was still thinking.
# Measured 2026-08-19: 21 live workers orphaned in one day at the old values,
# with ordinary deep-reading quiet periods exceeding ten minutes on a box
# running 20+ concurrent workers.
DEFAULT_MAX_IDLE_SECS = 900.0
_DASHBOARD_REFRESH_SUBCOMMAND = "dashboard-refresh"
_DASHBOARD_REFRESH_MARKER = "goalflight-dashboard-refresh-v1"
_DASHBOARD_REFRESH_QUEUED_GRACE_S = 60 * 60
_DASHBOARD_REFRESH_MAX_LIFETIME_S = 4 * 60 * 60
_DASHBOARD_REFRESH_CLAIM_STALE_S = 30.0
# Operator kill switch. Remove the file to re-enable the refresher fleet-wide.
_DASHBOARD_REFRESH_DISABLE_FLAG = (
    Path.home() / ".goal-flight" / "dashboard-refresh.disabled"
)
# Code writers read a corpus before their first edit, so their quiet periods are
# the longest in the fleet and the most often mistaken for death. One hour is a
# backstop for a genuinely wedged process, not a guess at how long thinking
# takes.
CODE_WRITER_MAX_IDLE_SECS = 3600.0
CODE_WRITER_AGENTS = {"codex", "codex-acp", "grok-code", "grok-acp", "moonshot", "cursor", "cursor-agent"}


class PreAdmitClass(Enum):
    LIVE = "live"
    INDETERMINATE = "indeterminate"
    CONFIRMED_DEAD = "confirmed_dead"
    PID_REUSED = "pid_reused"
    STALE_NO_SPAWN = "stale_no_spawn"
    NOT_STALE = "not_stale"
    REMOTE_AUTHORITY_REQUIRED = "remote_authority_required"


class AdmissionAction(Enum):
    ADMIT_TO_GATE = "admit_to_gate"
    DEFER_UNCHANGED = "defer_unchanged"


ADMISSION_DECISION = {
    PreAdmitClass.LIVE: AdmissionAction.DEFER_UNCHANGED,
    PreAdmitClass.INDETERMINATE: AdmissionAction.DEFER_UNCHANGED,
    PreAdmitClass.CONFIRMED_DEAD: AdmissionAction.ADMIT_TO_GATE,
    PreAdmitClass.PID_REUSED: AdmissionAction.ADMIT_TO_GATE,
    PreAdmitClass.STALE_NO_SPAWN: AdmissionAction.ADMIT_TO_GATE,
    PreAdmitClass.NOT_STALE: AdmissionAction.DEFER_UNCHANGED,
    PreAdmitClass.REMOTE_AUTHORITY_REQUIRED: AdmissionAction.DEFER_UNCHANGED,
}


class ReconciliationMode(Enum):
    LOCAL_FLOCK = "local_flock"
    FALLBACK_PID_IDENTITY = "fallback_pid_identity"
    DEFER_TO_NODE = "defer_to_node"
    FAIL_CLOSED_DEFER = "fail_closed_defer"


class FlockCapability(Enum):
    COHERENT_LOCAL = "coherent_local"
    COHERENT_CROSS_NODE = "coherent_cross_node"
    UNAVAILABLE = "unavailable"
    UNPROVEN = "unproven"


@dataclass(frozen=True)
class FilesystemIdentity:
    device: int | None
    mount_path: str
    filesystem_type: str
    locality: str


class ProducerSetState(Enum):
    LIVE = "live"
    DEAD = "dead"
    PID_REUSED = "pid_reused"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class ProducerIdentity:
    pid: int
    ppid: int
    pgid: int
    command: str
    identity: dict


@dataclass(frozen=True)
class ProducerSetResult:
    state: ProducerSetState
    members: tuple[ProducerIdentity, ...] = ()
    reason: str = ""


class ClaimCarrierKind(Enum):
    """Liveness of a queue envelope or its claimer, never inferred from PID alone."""

    NONE = "none"
    QUEUED = "queued"
    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClaimCarrierStatus:
    """Carrier presence plus claimer adjudication.

    Truthy when any ``.json`` / ``.claimed-*`` envelope exists, so mutation
    gates that only asked "is a carrier present?" keep failing closed. The
    kind distinguishes a live claimer from a dead one from one the marker
    cannot identify. PID-only markers are UNKNOWN: a recycled PID is not
    live, and an absent PID is not proof of death.
    """

    kind: ClaimCarrierKind = ClaimCarrierKind.NONE
    reason: str = ""
    path: str | None = None

    def __bool__(self) -> bool:
        return self.kind is not ClaimCarrierKind.NONE


class TerminalCommitKind(Enum):
    CREATED_TERMINAL = "created_terminal"
    UPDATED_TERMINAL = "updated_terminal"
    EXISTING_TERMINAL = "existing_terminal"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class TerminalCommitResult:
    kind: TerminalCommitKind
    durable_state: str | None
    committed: bool


class AcquireResult(Enum):
    ACQUIRED_ALL = "acquired_all"
    QUEUE_LOCK_DEFERRED = "queue_lock_deferred"
    TASK_STORE_LOCK_DEFERRED = "task_store_lock_deferred"
    LEDGER_LOCK_DEFERRED = "ledger_lock_deferred"
    LOCK_ERROR_DEFERRED = "lock_error_deferred"


class ReconcileRefusalReason(Enum):
    PRE_ADMISSION_DEFERRED = "pre_admission_deferred"
    RECONCILIATION_MODE_DEFERRED = "reconciliation_mode_deferred"
    TAIL_GATE_DEFERRED = "tail_gate_deferred"
    DOWNSTREAM_LOCK_DEFERRED = "downstream_lock_deferred"


@dataclass(frozen=True)
class _ReconcileRefusal:
    reason: ReconcileRefusalReason
    admission: PreAdmitClass
    detail: str
    mode: ReconciliationMode | None = None
    transport: str | None = None
    locality: str | None = None
    capability: FlockCapability | None = None
    filesystem: FilesystemIdentity | None = None

    def as_dict(self, dispatch_id: str) -> dict:
        filesystem = self.filesystem
        return {
            "dispatch_id": dispatch_id,
            "reason": self.reason.value,
            "detail": self.detail,
            "admission": self.admission.value,
            "mode": self.mode.value if self.mode is not None else None,
            "transport": self.transport,
            "locality": self.locality,
            "capability": self.capability.value if self.capability is not None else None,
            "filesystem": (
                {
                    "device": filesystem.device,
                    "mount_path": filesystem.mount_path,
                    "filesystem_type": filesystem.filesystem_type,
                    "locality": filesystem.locality,
                }
                if filesystem is not None
                else None
            ),
        }


@dataclass
class _ReconcileTransaction:
    entry: dict
    tail: Path
    admission: PreAdmitClass
    mode: ReconciliationMode
    filesystem: FilesystemIdentity
    flock_capability: FlockCapability
    tail_gate: object | None = None
    downstream: list[object] = field(default_factory=list)
    queue_locked: bool = False
    task_store_locked: bool = False
    ledger_locked: bool = False

    def release(self) -> None:
        for handle in reversed(self.downstream):
            with contextlib.suppress(Exception):
                handle.release()
        self.downstream.clear()
        if self.tail_gate is not None:
            with contextlib.suppress(Exception):
                self.tail_gate.__exit__(None, None, None)
            self.tail_gate = None


@dataclass(frozen=True)
class _BeginReconcileResult:
    transaction: _ReconcileTransaction | None = None
    refusal: _ReconcileRefusal | None = None

    def __post_init__(self) -> None:
        if (self.transaction is None) == (self.refusal is None):
            raise ValueError("reconcile begin result must contain exactly one outcome")


@dataclass(frozen=True)
class _ClaimReconcileResult:
    action: str
    pending_reason: str | None = None
    refusal: _ReconcileRefusal | None = None

    def __post_init__(self) -> None:
        if self.action == "pending":
            if (self.pending_reason is None) == (self.refusal is None):
                raise ValueError("pending reconciliation must contain exactly one reason")
        elif self.pending_reason is not None or self.refusal is not None:
            raise ValueError("non-pending reconciliation cannot contain a pending reason")

    def pending_detail(self, dispatch_id: str) -> dict | None:
        if self.action != "pending":
            return None
        if self.refusal is not None:
            return self.refusal.as_dict(dispatch_id)
        reason = self.pending_reason
        detail = {
            "dispatch_id": dispatch_id,
            "reason": reason,
        }
        detail.update(_completion_authority_park_fields(reason))
        return detail

# --os-sandbox: opt-in, per-dispatch OS sandbox profile for the bash-shape codex
# worker. Unset -> "workspace-write" for existing workers; Kimi is the explicit
# exception because its manifest supports only "off" until b-079 lands. "off"
# runs codex with its macOS Seatbelt sandbox
# DISABLED (codex --sandbox danger-full-access) so a TRUSTED LOCAL GPU/perf worker
# can (a) reach Metal/MPS for GPU verification and (b) write the linked worktree's
# .git/worktrees/<name> metadata to self-commit. "off" is a sanctioned adapter
# profile (adapters/codex.json os_sandbox.supported_profiles), DISTINCT from the
# always-forbidden --dangerously-*/--no-sandbox approval-and-sandbox bypass flags
# (which the presets never emit and this never enables). "read-only" == --read-only.
OS_SANDBOX_OFF = "off"
OS_SANDBOX_PROFILES = ("workspace-write", "read-only", OS_SANDBOX_OFF)


def _parse_os_sandbox_arg(value: str) -> str:
    """Accept hyphen/underscore/collapsed aliases of the sanctioned profiles.

    ``readonly`` / ``read_only`` are the same profile as ``read-only``; rejecting
    them was an error class, not a real ambiguity.
    """
    collapsed = (value or "").strip().lower().replace("_", "-")
    if collapsed.replace("-", "") == "workspacewrite":
        return "workspace-write"
    aliases = {
        "workspace-write": "workspace-write",
        "read-only": "read-only",
        "readonly": "read-only",
        "off": "off",
    }
    mapped = aliases.get(collapsed)
    if mapped is None:
        raise argparse.ArgumentTypeError(
            f"invalid choice: {value!r} (choose from {', '.join(OS_SANDBOX_PROFILES)})"
        )
    return mapped
_CODEX_SANDBOX_VALUE = {
    "workspace-write": "workspace-write",
    "read-only": "read-only",
    "off": "danger-full-access",  # codex's sanctioned "no Seatbelt" sandbox value
}


def _effective_os_sandbox(args) -> str:
    """Resolve the effective OS sandbox profile (non-raising, safe anywhere).

    Precedence: explicit --os-sandbox > legacy --read-only alias > agent default
    (Moonshot "off", otherwise "workspace-write"). Conflicts are surfaced separately by
    _validate_os_sandbox_conflict.
    """
    explicit = getattr(args, "os_sandbox", None)
    if explicit in OS_SANDBOX_PROFILES:
        return explicit
    if getattr(args, "read_only", False):
        return "read-only"
    if str(getattr(args, "agent", "")) == "moonshot":
        return OS_SANDBOX_OFF
    return "workspace-write"


def _effective_read_only(args) -> bool:
    return _effective_os_sandbox(args) == "read-only"


def _requested_os_sandbox(args) -> str | None:
    """Return only the posture the caller asked for, not a dispatcher default."""
    explicit = getattr(args, "os_sandbox", None)
    if explicit in OS_SANDBOX_PROFILES:
        return explicit
    if getattr(args, "read_only", False):
        return "read-only"
    return None


def _supported_os_sandbox_profile(args) -> str | None:
    """Return the posture this concrete bash launch path knows how to support."""
    if getattr(args, "worker", None):
        return None
    agent = str(getattr(args, "agent", ""))
    shape = getattr(args, "shape", "bash")
    if agent == "codex" and shape != "acp":
        return _effective_os_sandbox(args)
    if agent == "moonshot" and shape != "acp":
        return OS_SANDBOX_OFF
    return None


def _enforced_os_sandbox_profile(args, *, worker_pid: int | None) -> str | None:
    """Return measured launch posture; pre-launch records must remain unset."""
    if worker_pid is None or getattr(args, "worker", None):
        return None
    agent = str(getattr(args, "agent", ""))
    shape = getattr(args, "shape", "bash")
    if agent == "codex" and shape != "acp":
        return _effective_os_sandbox(args)
    if agent == "moonshot" and shape != "acp":
        return OS_SANDBOX_OFF
    return None


def _os_sandbox_posture(args, *, worker_pid: int | None) -> dict:
    """Keep request, support, and observed enforcement as separate facts."""
    return {
        "shape": getattr(args, "shape", "bash"),
        "requested_profile": _requested_os_sandbox(args),
        "supported_profile": _supported_os_sandbox_profile(args),
        "enforced_profile": _enforced_os_sandbox_profile(
            args, worker_pid=worker_pid
        ),
    }


def _validate_os_sandbox_conflict(args) -> None:
    explicit = getattr(args, "os_sandbox", None)
    if explicit and getattr(args, "read_only", False) and explicit != "read-only":
        raise DispatchUsageError(
            f"--read-only conflicts with --os-sandbox {explicit} "
            "(opposite write postures); pass only one."
        )


def _validate_os_sandbox_boundary(args) -> None:
    """Fail fast when the requested sandbox cannot draw a boundary around cwd.

    The sandbox refuses when cwd sits inside a temp root or an agent state root,
    because it cannot separate "the workspace" from "everywhere the worker is
    already allowed to write". That refusal is correct and stays. What was wrong
    is WHERE it surfaced: the dispatcher launched a detached worker, the worker
    hit the check on startup, and the run died as `blocked_os_sandbox` with the
    reason buried in its status file.

    Two controllers then misread that as "--os-sandbox / --read-only is broken
    for ACP shapes" and proposed rejecting those flags for ACP entirely -- which
    would break sandboxed ACP dispatches from ordinary worktrees, where they work
    fine. The trigger is the cwd LOCATION, nothing about the shape or the flag.

    Running the same check here turns a wasted dispatch into an immediate,
    accurate error naming the real cause and the two real remedies.
    """
    # Only an EXPLICIT request is checked here. `_effective_os_sandbox` falls
    # back to workspace-write when nothing was passed, so gating on it would
    # reject every dispatch from a temp cwd -- including the many that never
    # asked for a sandbox and are perfectly happy without one. Refusing those up
    # front would be a far worse regression than the late failure this fixes.
    explicit = getattr(args, "os_sandbox", None) or (
        "read-only" if getattr(args, "read_only", False) else None
    )
    if not explicit or explicit == "off":
        return
    profile = _effective_os_sandbox(args)
    if not profile or profile == "off":
        return
    cwd = getattr(args, "cwd", None)
    if not cwd:
        return
    # Function-local import, matching this module's convention for the readiness
    # helpers: dispatch must not fail to load if the sandbox module is absent.
    try:
        from goalflight_os_sandbox import OsSandboxError, macos_write_roots
    except ImportError:
        return
    try:
        macos_write_roots(
            str(cwd), profile, agent=getattr(args, "agent", None), command=""
        )
    except OsSandboxError as exc:
        raise DispatchUsageError(
            f"--os-sandbox {profile} cannot be enforced from this working directory: {exc} "
            "(this is about WHERE the worktree lives, not the agent or the shape: "
            "dispatch from a worktree outside the temp/agent-state roots, or pass "
            "--os-sandbox off and rely on the review/permission layer instead)"
        ) from exc


def _os_sandbox_enforced_by_launch(args) -> bool:
    """True when an explicit --os-sandbox actually constrains this launch path.

    Bash-shape grok/cursor/claude ignore the flag (no CLI/OS mapping). ACP
    honours it only when the adapter and platform can wrap the worker.
    An accepted-but-inert safety flag is worse than a refusal.
    """
    explicit = getattr(args, "os_sandbox", None)
    if explicit not in OS_SANDBOX_PROFILES:
        return True
    agent = str(getattr(args, "agent", "") or "")
    shape = getattr(args, "shape", "bash")
    if shape == "acp":
        from goalflight_adapter_readiness import validate_os_sandbox_request

        return validate_os_sandbox_request(agent, explicit) is None
    if agent == "codex":
        return True
    if agent == "moonshot":
        return explicit == OS_SANDBOX_OFF
    return False


def _validate_agent_os_sandbox(args) -> None:
    profile = _effective_os_sandbox(args)
    agent = str(getattr(args, "agent", "") or "")
    shape = getattr(args, "shape", "bash")
    if agent == "moonshot" and profile != OS_SANDBOX_OFF:
        raise UnsupportedAgentSandboxRequest(
            f"--agent moonshot supports only --os-sandbox {OS_SANDBOX_OFF}; "
            f"requested profile {profile!r} is not enforced and cannot be enforced; "
            "refusing before launch (b-079)"
        )
    explicit = getattr(args, "os_sandbox", None)
    if explicit not in OS_SANDBOX_PROFILES:
        return
    if shape == "acp":
        from goalflight_adapter_readiness import (
            os_sandbox_refusal_is_retryable,
            validate_os_sandbox_request,
        )

        gate = validate_os_sandbox_request(agent, explicit)
        if os_sandbox_refusal_is_retryable(gate):
            reason = str((gate or {}).get("reason") or "adapter_manifest_unreadable")
            raise DispatchUsageError(
                f"--os-sandbox {explicit} is undetermined for agent={agent} "
                f"shape={shape} ({reason}); not a permanent refusal — retry when "
                "the adapter manifest is readable"
            )
        if gate is not None:
            raise UnsupportedAgentSandboxRequest(
                f"--os-sandbox {explicit} is ignored for agent={agent} "
                f"shape={shape} ({gate.get('reason')}); refusing to launch "
                "with an inert safety flag. "
                "Use --read-only instead (grok: deny rules for Write/Edit/Bash; "
                "bash-shape codex: codex --sandbox read-only). --os-sandbox is "
                "honoured by bash-shape codex and by ACP shapes whose adapter "
                "and platform can enforce it."
            )
        return
    if not _os_sandbox_enforced_by_launch(args):
        raise UnsupportedAgentSandboxRequest(
            f"--os-sandbox {explicit} is ignored for agent={agent} "
            f"shape={shape}; refusing to launch with an inert safety flag. "
            "Use --read-only instead (grok: deny rules for Write/Edit/Bash; "
            "bash-shape codex: codex --sandbox read-only). --os-sandbox is "
            "honoured by bash-shape codex and by ACP shapes whose adapter "
            "and platform can enforce it."
        )


def _os_sandbox_warning(args) -> str | None:
    """Dispatch-time advisory: log when bash-shape codex selects 'off'."""
    profile = _effective_os_sandbox(args)
    if (
        getattr(args, "shape", "bash") == "acp"
        and _effective_read_only(args)
        and getattr(args, "os_sandbox", None) not in OS_SANDBOX_PROFILES
        and str(getattr(args, "agent", "")) in {"claude", "claude-acp"}
    ):
        from goalflight_acp_run import acp_permission_read_only_supported
        from goalflight_adapter_readiness import (
            os_sandbox_refusal_is_retryable,
            validate_os_sandbox_request,
        )

        sandbox_gate = validate_os_sandbox_request(getattr(args, "agent", None), profile)
        if (
            acp_permission_read_only_supported(getattr(args, "agent", None))
            and sandbox_gate is not None
            and not os_sandbox_refusal_is_retryable(sandbox_gate)
        ):
            return (
                "SANDBOX FALLBACK: requested=read-only -> applied=off -> "
                "enforcement=acp-permissions"
            )
    # Only the bash-shape codex worker maps a profile to codex --sandbox here.
    codex_bash = (
        str(getattr(args, "agent", "")) == "codex"
        and getattr(args, "shape", "bash") != "acp"
    )
    if profile == "off" and codex_bash:
        return (
            "OS SANDBOX DISABLED (--os-sandbox off): codex --sandbox "
            "danger-full-access — Seatbelt off for TRUSTED LOCAL GPU/perf work "
            "(Metal/MPS reachable; linked-worktree git metadata writable to "
            "self-commit). Commit-guard (explicit pathspecs, no auto-push) + "
            "capacity/ledger tracking unchanged."
        )
    return None


# SECURITY-2 structural fix: the unsandboxed browser daemon is a *granted*
# capability, not ambient. Default OFF — only --web-qa provisions the worker
# with GOALFLIGHT_WEB_QA + BROWSE_STATE_FILE. Without that grant,
# scripts/goalflight_webqa.sh fails closed, and a worker cannot self-grant by
# guessing the project state-file path (wrapper requires both the grant marker
# and an explicit BROWSE_STATE_FILE; dispatch strips ambient values when the
# flag is absent).
WEB_QA_GRANT_ENV = "GOALFLIGHT_WEB_QA"
WEB_QA_STATE_ENV = "BROWSE_STATE_FILE"


def _web_qa_env_plan(args, project_root: Path) -> tuple[dict[str, str], list[str]]:
    """Return (env_updates, env_remove) for controller-gated web-QA.

    Matches the --web-research-ok / --os-sandbox pattern: an explicit
    dispatch-level flag is the only opt-in; default is off.
    """
    if getattr(args, "web_qa", False):
        updates = {
            WEB_QA_GRANT_ENV: "1",
            WEB_QA_STATE_ENV: str(Path(project_root) / ".gstack" / "browse.json"),
        }
        browse = goalflight_compat.resolve_gstack_browse_bin()
        if browse is not None:
            updates["GSTACK_BROWSE_BIN"] = str(browse)
        return updates, []
    # Strip ambient grants inherited from a controller shell so a non-opt-in
    # dispatch cannot silently inherit web-QA.
    return {}, [WEB_QA_GRANT_ENV, WEB_QA_STATE_ENV]


def _apply_web_qa_env(env: dict, args, project_root: Path) -> dict:
    """Mutate worker env for --web-qa grant (or strip ambient when absent)."""
    updates, remove = _web_qa_env_plan(args, project_root)
    env.update(updates)
    for key in remove:
        env.pop(key, None)
    return env


ACCOUNT_ENGINE_BY_AGENT = {
    "codex": "codex",
    "codex-acp": "codex",
    "grok": "grok",
    "grok-code": "grok",
    "grok-research": "grok",
    "grok-acp": "grok",
    # Kimi is single-account by design; no rotation/profile knob is exposed.
    "cursor": "cursor",
    "cursor-agent": "cursor",
}
RETIRED_AGENT_LABELS = {
    "grok": "use --agent grok-code (coding) or --agent grok-research (web search)",
}
# t-356: a git base pin is an EXPLICIT marker, never a bare hex token. Briefs
# legitimately cite evidence commits as prose (measurement tables, receipts);
# scanning for bare hex read one of those citations as the intended base and
# warned GIT BASE PIN MISMATCH against a correct cwd HEAD -- pure noise that
# trained controllers to reach for --ignore-git-warn. The recognized markers,
# observed in real briefs:
#   - a frontmatter-style `base: <sha>` line (quoted YAML, `Base SHA:`,
#     markdown `**base:**`, bullets, and backticks around the sha are tolerated)
#   - the `branch <name> @ <sha>` header convention, and a line-level
#     `<name> @ <sha>` pin without the word `branch`
# Every other hex-shaped token in the text is prose.
GIT_BASE_PIN_BASE_LINE_RE = re.compile(
    r"^[ \t]*(?:[-#>*]+[ \t]*)*(?:\*\*)?base(?:\s+sha)?\s*:\s*(?:\*\*)?\s*"
    r"[`'\"]?([0-9A-Fa-f]{7,40})[`'\"]?(?![0-9A-Za-z])",
    re.IGNORECASE | re.MULTILINE,
)
GIT_BASE_PIN_BRANCH_AT_RE = re.compile(
    r"\bbranch\s+\S+?\s+@\s+`?([0-9A-Fa-f]{7,40})`?(?![0-9A-Za-z])",
    re.IGNORECASE,
)
GIT_BASE_PIN_NAME_AT_RE = re.compile(
    r"^[ \t]*(?:[-#>*]+[ \t]*)*\S+\s+@\s+`?([0-9A-Fa-f]{7,40})`?(?![0-9A-Za-z])",
    re.IGNORECASE | re.MULTILINE,
)
GIT_BASE_PIN_MARKER_RES = (
    GIT_BASE_PIN_BASE_LINE_RE,
    GIT_BASE_PIN_BRANCH_AT_RE,
    GIT_BASE_PIN_NAME_AT_RE,
)
TASK_ID_RE = re.compile(r"^[tb]-\d+$")
LOWER_BASE_SHA_RE = re.compile(r"[0-9a-f]{40}")
READ_ONLY_INLINE_RETURN_PROMPT_PATTERNS = (
    (
        "return inline",
        re.compile(
            r"\breturn\b.{0,120}\binline\b"
            r"|\binline\b.{0,80}\b(?:in\s+chat|final\s+response|response)\b",
            re.I | re.S,
        ),
    ),
    (
        "no file write",
        re.compile(
            r"\bdo\s+not\s+(?:write|create|save|append|update)\b.{0,80}"
            r"\b(?:any\s+)?(?:file|files|artifact|review|findings|report)\b"
            r"|\bno\s+(?:file\s+)?(?:write|writes|writing)\b"
            r"|\bwithout\s+(?:writing|creating|saving)\b.{0,80}\b(?:file|files)\b",
            re.I | re.S,
        ),
    ),
)
READ_ONLY_WRITE_PROMPT_PATTERNS = (
    (
        "write review artifact",
        re.compile(
            r"\b(?:write|save|create|append|update)\b.{0,80}"
            r"\b(?:review|findings|report|artifact|verdict)\b.{0,80}"
            r"\b(?:to|at|under|into|as)\b.{0,80}"
            r"\b(?:docs-private/|[~/./A-Za-z0-9_-][^ \t\r\n`'\"<>]*[.](?:md|json|log|txt))",
            re.I | re.S,
        ),
    ),
    (
        "write review file",
        re.compile(
            r"\b(?:write|save|create|append|update)\b.{0,80}"
            r"\b(?:review|findings|report|artifact|verdict)\b.{0,80}\bfile\b",
            re.I | re.S,
        ),
    ),
    (
        "READY artifact contract",
        re.compile(
            r"\bREADY:\s*(?:docs-private/|[~/./A-Za-z0-9_-][^ \t\r\n`'\"<>]*[.](?:md|json|log|txt))",
            re.I,
        ),
    ),
    ("shell output redirect", re.compile(r">\s*(docs-private|[^ \t\r\n]+[.](md|json|log))", re.I)),
)

# Web-research intent on a grok-code dispatch (B5c-style teaching guard).
# grok-code runs the composer CODING model, which reliably fails web_fetch /
# returns thin web results (observed live 2026-06-09: a research dispatch
# tool_output_error'd repeatedly); grok-research is the web-capable lane.
# NB: neither preset pins a model id any more -- both run grok's own current
# default so a new flagship is picked up without a code change. The retired
# pins (grok-build for research, grok-composer-2.5-fast for coding) survive
# only as history below; do not reintroduce one, and do not read this comment
# as naming a default. Precision-first: signals are explicit web-research phrasings; bare
# URLs and the word "research" alone must NOT trigger (code prompts cite repo
# links; "research the codebase" is repo reading). Suppressors win.
# Verb-led, live-action phrasings ONLY (review round 1 found 16+ coding false
# positives in noun forms): bare "web search"/"websearch" must NOT match
# (feature/module names), nor "web_fetch" as an implemented symbol, nor
# "literature review" as a document, nor "internet-facing".
RESEARCH_INTENT_PROMPT_PATTERNS = (
    ("web search ask", re.compile(r"\b(?:search(?:es|ing)?\s+(?:the\s+)?(?:web|internet(?!-))|web[-_ ]search\s+for|search\s+online)\b", re.I)),
    ("browse ask", re.compile(r"\b(?:browse\s+(?:the\s+)?(?:web|internet(?!-))|use\s+your\s+browser)\b", re.I)),
    ("web fetch ask", re.compile(r"\b(?:web[-_ ]?fetch\s+the\b|fetch\s+(?:the\s+)?(?:page|url)s?\s+(?:from|at)\b)", re.I)),
    ("cite source URL ask", re.compile(r"\bcite\b.{0,40}\bsource\s+urls?\b", re.I | re.S)),
    ("literature hunt ask", re.compile(r"\b(?:find|locate|survey|gather)\b.{0,60}\b(?:papers|publications|datasheets?|literature)\b.{0,30}\b(?:online|on\s+the\s+web)\b", re.I | re.S)),
    ("look up online ask", re.compile(r"\blook\s+up\b.{0,40}\bonline\b", re.I | re.S)),
    ("deep research ask", re.compile(r"\bdeep[- ]research\b", re.I)),
)
RESEARCH_INTENT_SUPPRESSOR_PATTERNS = (
    re.compile(r"\b(?:do\s+not|don'?t|never)\s+(?:use|access|touch)\s+(?:the\s+)?(?:web|internet|browser)\b", re.I),
    re.compile(r"\bno\s+(?:web|internet)(?:\s+access)?\b", re.I),
    # Scoped offline forms only — bare "offline" wrongly suppressed real
    # research prompts that mention it incidentally (review round 1).
    re.compile(r"\b(?:run|runs|work(?:ing)?|operate)\s+(?:fully\s+)?offline\b|\boffline\s+mode\b|\bfully\s+offline\b", re.I),
    # Review/meta context: controller prompts ABOUT web features or web-research
    # dispatches (inline-return reviews) are not live research.
    re.compile(r"\bINLINE-RETURN\b|\bverdict\s+as\s+text\b|\bno\s+file\s+writes\b", re.I),
)

STEER_PROMPT_PREAMBLE = (
    "You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH "
    "ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages "
    "into your plan; ack each with `!STEER-ACK: <seq>` on its own line; a steer may "
    "redirect or HALT you — honor it. If `$GOALFLIGHT_DISPATCH_SCRIPT` is set and "
    "you have nothing to do until the controller answers, use `python3 "
    "\"$GOALFLIGHT_DISPATCH_SCRIPT\" steer "
    "\"$GOALFLIGHT_DISPATCH_ID\" --wait --question-kind USER-NEED "
    "--timeout-secs <seconds> '<question>'` (or USER-CONFIRM) to emit the question "
    "and wait under a separate bounded deadline."
)
PROMPT_FILE_PREAMBLE = (
    "Your FULL original brief is at `$GOALFLIGHT_PROMPT_FILE`. Re-read it after any "
    "internal compaction/summarization, at the start of each long-run goal-loop "
    "iteration, and before final commit/exit; the disk file is authoritative over "
    "summarized memory."
)
# Cursor's CLI auto-reviews shell commands. Reading the prompt-file env var is
# one of the commands it escalates, and an unattended dispatch has nobody to
# approve it -- so the generic instruction above ("re-read $GOALFLIGHT_PROMPT_FILE")
# is unfollowable there. Workers obeyed it, retried the refusal, and burned the
# whole ACP event budget: observed twice, killed at the cap with nothing written.
# Give that lane an instruction it can actually follow.
# Deliberately does NOT name the prompt-file environment variable. An earlier
# version forbade it by name and workers went and read it anyway -- naming a
# variable in order to prohibit it appears to plant it. There is nothing for a
# worker to look up here: the brief is inline, so the instruction is stated
# positively and no variable name is offered.
CURSOR_PROMPT_FILE_PREAMBLE = (
    "Your FULL original brief is delivered inline above. It is complete and "
    "authoritative -- re-read it there after any internal compaction or "
    "summarization.\n"
    "- There is no separate copy to fetch and nothing to look up before starting. "
    "Do not run shell commands to locate, print, or inspect your own brief, prompt, "
    "or dispatch environment; those reads are escalated for approval on this "
    "transport and an unattended run has nobody watching to approve them. Begin the "
    "actual task with your first tool call."
)
CURSOR_TOOLING_PREAMBLE = (
    "Tooling on this transport:\n"
    "- Shell commands pass through an automatic reviewer. ONE simple command per "
    "call is what passes: `ls -1 somedir`, `python3 -c '...'`.\n"
    "- Do NOT combine commands. A single line joining several with `;`, `&&` or `|` "
    "is escalated for approval even when each part alone would run, and an "
    "unattended dispatch has nobody to approve it. Batching to save round-trips is "
    "a false economy here: it converts three cheap calls into one blocked one. "
    "`cat` and `echo` are also frequently rejected.\n"
    "- When you need several facts at once, make one `python3 -c '...'` call that "
    "gathers them and prints the result. That is a single command, so it passes, "
    "and it replaces the compound line you were reaching for.\n"
    "- Prefer your native file-read, search and list tools over shell equivalents. "
    "Use `python3 -c '...'` when you genuinely need computation such as counting or "
    "parsing; it is reliable and is the best escape hatch.\n"
    "- NEVER retry a rejected command in a loop. Looping on a refusal is the most "
    "common way a run on this transport dies: it exhausts the event budget and the "
    "run is killed with nothing to show, even when the task was easy. Take the "
    "refusal as final, use a different tool, or emit `!BLOCKED: <what was refused>` "
    "and stop."
)
PROJECT_ORIENTATION_RELATIVE = Path("docs-private") / "rag" / "ORIENTATION.md"
PROJECT_ORIENTATION_SCOPE_RULE = "Read it before starting; orientation only, never scope expansion."
WORKER_EXECUTION_PREAMBLE = (
    "Worker execution contract:\n"
    "- Use your available tools to actually perform the requested filesystem, shell, "
    "research, or analysis actions before answering. Do not only plan, summarize, or "
    "describe commands.\n"
    "- For successful completion, emit a final line outside any Markdown fence in this "
    "exact shape supplied by the dispatch-specific identity contract.\n"
    "- The `!COMPLETE:` line must be the last non-empty line of your output. "
    "Do not print anything after it.\n"
    "- Legacy unprefixed marker lines remain accepted; new emissions use the `!` prefix."
)
STEER_ACK_RE = goalflight_terminal.STEER_ACK_RE


class DispatchUsageError(Exception):
    pass


class _TerseArgumentParser(argparse.ArgumentParser):
    """One-line argument errors. ``--help`` stays the verbose path.

    Default argparse reprints the full usage stanza (1.6KB+ here) on every
    typo, burying the actual problem. Controllers re-run with ``--help`` when
    they want the map; a failure should name the problem and the correct form.
    """

    def __init__(self, *args, usage_hint: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usage_hint = usage_hint

    def error(self, message: str) -> None:
        hint = self.usage_hint or "see --help"
        self.exit(2, f"{self.prog}: {message}. {hint}\n")


_DISPATCH_USAGE_HINT = (
    "try --agent <preset> --prompt-file <path> [--cwd .] (or --help)"
)


class UnsupportedAgentSandboxRequest(DispatchUsageError):
    """A requested sandbox posture is outside the selected launch path."""


def _detached_popen_kwargs() -> dict:
    """Launch the worker in its own session/process-group so its tree is decoupled
    from this dispatcher (a lingering worker child must not keep our group alive)."""
    if os.name == "nt":  # pragma: no cover - Windows only
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def _cmd_spawn_daemon() -> int:
    """Private helper: spawn one long-lived child, print its pid, then exit.

    The parent invokes this helper with the final worker/watcher environment.
    The helper starts the child in a new session and exits immediately, so the
    child is reparented away from the dispatch launcher. The launcher reaps only
    this short helper, avoiding direct-child worker zombies.
    """
    try:
        spec = json.loads(sys.stdin.read() or "{}")
        argv = spec["argv"]
        stdin_path = spec.get("stdin_path")
        stdout_path = spec.get("stdout_path")
        serialize_stdout = bool(spec.get("serialize_stdout"))
        stdout_mode = spec.get("stdout_mode") or "wb"
        stderr_mode = spec.get("stderr") or "stdout"
        with contextlib.ExitStack() as stack:
            if stdin_path:
                stdin_f = stack.enter_context(open(stdin_path, "rb"))
            else:
                stdin_f = subprocess.DEVNULL
            filter_proc = None
            worker_stdout = subprocess.DEVNULL
            if stdout_path:
                stdout_file = Path(stdout_path)
                stdout_file.parent.mkdir(parents=True, exist_ok=True)
                # Redact by secret shape at write time. The filter process
                # owns the tail flock so reconciliation cannot pass a
                # COMPLETE still buffered here. Keying redaction on a
                # resolved seat would skip the capture when lookup failed.
                filter_argv = [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / "goalflight_output_redact.py"),
                ]
                if serialize_stdout:
                    filter_argv.append("--lock")
                if "a" in str(stdout_mode):
                    filter_argv.append("--append")
                filter_argv.append(str(stdout_file))
                ready_r, ready_w = os.pipe()
                filter_env = os.environ.copy()
                filter_env.pop(goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, None)
                filter_env.pop(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, None)
                filter_env[goalflight_output_redact.READY_FD_ENV] = str(ready_w)
                filter_proc = subprocess.Popen(
                    filter_argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=filter_env,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(ready_w,),
                )
                os.close(ready_w)
                try:
                    ready, _wp, _xp = select.select([ready_r], [], [], 5.0)
                    if not ready or not os.read(ready_r, 1):
                        raise RuntimeError(
                            "output redact filter did not become ready"
                        )
                except Exception:
                    if filter_proc.poll() is None:
                        filter_proc.kill()
                    raise
                finally:
                    os.close(ready_r)
                if filter_proc.poll() is not None:
                    raise RuntimeError("output redact filter exited before worker start")
                worker_stdout = filter_proc.stdin
            stderr_f = (
                subprocess.STDOUT if stderr_mode == "stdout" else subprocess.DEVNULL
            )
            child_cwd = spec.get("cwd")
            if not isinstance(child_cwd, str) or not child_cwd.strip():
                child_cwd = None
            try:
                child = subprocess.Popen(
                    argv,
                    stdin=stdin_f,
                    stdout=worker_stdout,
                    stderr=stderr_f,
                    start_new_session=True,
                    close_fds=True,
                    cwd=child_cwd,
                    pass_fds=goalflight_worktree_pool.pass_worktree_lock_fds(),
                )
            except Exception:
                if filter_proc is not None and filter_proc.stdin is not None:
                    filter_proc.stdin.close()
                raise
            if filter_proc is not None and filter_proc.stdin is not None:
                filter_proc.stdin.close()
        print(json.dumps({"pid": child.pid}, sort_keys=True), flush=True)
        return 0
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, sort_keys=True), file=sys.stderr)
        return 1


def _spawn_daemonized_process(
    argv: list[str],
    *,
    env: dict[str, str],
    stdin_path: str | None = None,
    stdout_path: Path | None = None,
    stdout_mode: str = "wb",
    stderr: str = "stdout",
    serialize_stdout: bool = False,
    label: str,
    cwd: str | None = None,
    inherit_occupancy_lock: bool = False,
) -> int:
    """Spawn a child through the private daemon helper and return the child's pid."""
    spec = {
        "argv": argv,
        "stdin_path": stdin_path,
        "stdout_path": str(stdout_path) if stdout_path else None,
        "stdout_mode": stdout_mode,
        "stderr": stderr,
        "serialize_stdout": bool(stdout_path and serialize_stdout),
        "cwd": cwd,
    }
    child_env = dict(env)
    inherited_lock_fds = goalflight_worktree_pool.pass_worktree_lock_fds(env)
    occupancy_fds: set[int] = set()
    for source in (os.environ, env):
        occupancy_raw = str(
            source.get(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV) or ""
        ).strip()
        if occupancy_raw:
            with contextlib.suppress(ValueError):
                occupancy_fds.add(int(occupancy_raw))
    if inherit_occupancy_lock:
        seen = set(inherited_lock_fds)
        extra: list[int] = []
        for fd in occupancy_fds:
            if fd in seen:
                continue
            try:
                os.fstat(fd)
            except OSError:
                continue
            extra.append(fd)
            seen.add(fd)
        if extra:
            inherited_lock_fds = tuple(inherited_lock_fds) + tuple(extra)
    else:
        inherited_lock_fds = tuple(
            fd for fd in inherited_lock_fds if fd not in occupancy_fds
        )
        child_env.pop(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, None)
    helper = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), DAEMON_SPAWN_ARG],
        input=json.dumps(spec, sort_keys=True),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        timeout=30,
        pass_fds=inherited_lock_fds,
        **_detached_popen_kwargs(),
    )
    if helper.returncode != 0:
        detail = (helper.stderr or helper.stdout or "").strip().splitlines()[-1:]
        raise RuntimeError(f"{label} daemon spawn failed: {detail[0] if detail else helper.returncode}")
    lines = [line for line in helper.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"{label} daemon spawn failed: missing pid")
    result = json.loads(lines[-1])
    pid = int(result["pid"])
    return pid


def _status_exit_code(state: object) -> int:
    if state == "complete":
        return 0
    if state == "worker_dead":
        return 1
    if state == "rate_limited":
        return 1
    if state in {"idle_timeout", "liveness_indeterminate"}:
        return 2
    if state in {"orphaned", "controller_dead"}:
        return 3
    if state == "blocked" or (isinstance(state, str) and state.startswith("blocked")):
        return 4
    return 1


def _quota_limited_state_reason(
    state: str | None,
    reason: object,
    tail: Path,
    *,
    agent: object,
) -> tuple[str | None, object]:
    if state not in goalflight_dispatch_states.LIMIT_RECONCILE_INPUT_STATES:
        return state, reason
    quota_reason = goalflight_quota_stuck.quota_limited_reason(
        agent=agent,
        tail=tail,
        previous_state=state,
        previous_reason=reason,
    )
    if not quota_reason:
        return state, reason
    # Write the MEASURED kind the reason already carries. Hardcoding the legacy
    # literal here kept minting new kind-unknown records from a path that had
    # just classified the kind.
    return (
        str(quota_reason.get("limit_state") or goalflight_dispatch_states.LIMIT_UNKNOWN_STATE),
        quota_reason,
    )


def _is_status_terminal(state: object) -> bool:
    if state == "orphaned":
        return True
    return goalflight_dispatch_states.is_terminal_state(state)


def _is_live_watcher_stopped(state: object, worker_alive: object) -> bool:
    return state == "watcher_stopped" and worker_alive is True


def _status_matches_current_launch(
    payload: dict,
    *,
    dispatch_id: str,
    worker_pid: int | None,
) -> bool:
    if payload.get("dispatch_id") != dispatch_id:
        return False
    if worker_pid is None:
        return payload.get("worker_pid") is None
    try:
        return int(payload.get("worker_pid")) == int(worker_pid)
    except (TypeError, ValueError):
        return False


def _reattach_hint(dispatch_id: str) -> str:
    return f"worker still alive - re-attach via goalflight_status.py --wait {dispatch_id}"


def _foreground_wait_interrupt_hint(dispatch_id: str) -> str:
    return (
        "interrupted — worker still running (detached); re-attach: "
        f"goalflight_status.py --wait {dispatch_id}"
    )


def _dispatch_end_reattach_hint(
    dispatch_id: str,
    *,
    terminal_state: str | None,
    worker_alive: object,
) -> str | None:
    if terminal_state in {
        "idle_timeout",
        "liveness_indeterminate",
        "watcher_stopped",
    } and worker_alive is True:
        return _reattach_hint(dispatch_id)
    return None


def _worker_alive_from_identity(worker_pid: int | None, identity: dict | None) -> tuple[bool, str]:
    if not worker_pid:
        return False, "no_pid"
    try:
        return goalflight_ledger.identity_matches(
            {"worker_pid": worker_pid, "worker_identity": identity or {}}
        )
    except Exception as e:
        return goalflight_compat.pid_alive(worker_pid), f"identity_check_error:{type(e).__name__}"


def _ignore_prefix_lines(prompt_path: str | None) -> list[str]:
    if not prompt_path:
        return []
    try:
        return [
            ln.strip()
            for ln in Path(prompt_path).read_text(encoding="utf-8", errors="replace").splitlines()
        ]
    except Exception:
        return []


def _repair_watcher_terminal_status(
    status_json: Path,
    *,
    args,
    tail: Path,
    worker_pid: int | None,
    worker_identity: dict | None,
    pgid: int | None,
    prompt_path: str | None,
    reason: str,
) -> dict:
    try:
        payload = json.loads(status_json.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    worker_is_alive, identity_reason = _worker_alive_from_identity(worker_pid, worker_identity)
    ignore_prefix_lines = _ignore_prefix_lines(prompt_path)
    expected_dispatch_id = str(getattr(args, "dispatch_id", "") or "").strip()
    terminal_marker = (
        _last_line_is_terminal_marker(
            tail,
            ignore_prefix_lines=ignore_prefix_lines,
            kimi_output=moonshot_family(getattr(args, "agent", None)),
            expected_dispatch_id=expected_dispatch_id,
        )
        if expected_dispatch_id
        else None
    )
    if not terminal_marker and not worker_is_alive and expected_dispatch_id:
        terminal_marker = _final_terminal_marker(
            tail,
            ignore_prefix_lines=ignore_prefix_lines,
            suppress_unfenced_prompt_markers=True,
            kimi_output=moonshot_family(getattr(args, "agent", None)),
            expected_dispatch_id=expected_dispatch_id,
        )
    if not terminal_marker:
        recorded_marker = payload.get("terminal_marker")
        if expected_dispatch_id and _terminal_marker_matches_dispatch(
            recorded_marker, expected_dispatch_id
        ):
            terminal_marker = recorded_marker
    terminal_pending_state = None
    if terminal_marker and isinstance(terminal_marker, dict):
        marker_state = _marker_state_for_terminal(terminal_marker)
        if marker_state == "complete" and worker_is_alive:
            state = "watcher_stopped"
            terminal_pending_state = marker_state
        else:
            state = marker_state
    elif worker_is_alive:
        state = "watcher_stopped"
    else:
        state = "worker_dead"
    state, final_reason, _rate_limited = goalflight_terminal.terminal_rate_limit_outcome(
        state,
        reason,
        tail,
        terminal_marker_present=goalflight_terminal.terminal_marker_present(terminal_marker),
    )
    payload.update({
        "schema": "goalflight.status.v1",
        "dispatch_id": args.dispatch_id,
        "agent": args.agent,
        "worker_pid": worker_pid,
        "pgid": pgid,
        "worker_alive": worker_is_alive,
        "worker_identity_reason": identity_reason,
        "expected_worker_identity": _identity_token(worker_identity),
        "tail_path": str(tail),
        "terminal_marker": terminal_marker if isinstance(terminal_marker, dict) else None,
        "state": state,
        "reason": final_reason,
        "liveness_state": goalflight_terminal.terminal_liveness_state(state),
        "updated_at": int(time.time()),
    })
    if terminal_pending_state:
        payload["terminal_pending_state"] = terminal_pending_state
    try:
        write_status(status_json, payload)
    except Exception as e:
        # This repair returns the failed mirror payload to its caller. Losing
        # the write gives up only watcher-status freshness; terminal authority
        # stays in the journal and the failure remains in status_write_error.
        payload["state"] = "failed"
        payload["reason"] = f"{reason};status_write_error:{type(e).__name__}: {e}"
        payload["status_write_error"] = f"{type(e).__name__}: {e}"
    return payload


def _wait_for_detached_watcher(
    *,
    status_json: Path,
    watcher_pid: int,
    poll_secs: float,
    args,
    tail: Path,
    worker_pid: int | None,
    worker_identity: dict | None,
    pgid: int | None,
    prompt_path: str | None,
) -> tuple[int, dict, str | None]:
    """Join this launch's watcher until it writes a terminal sidecar or dies.

    Sidecar ``state`` here is watcher liveness of the current launch, not
    dispatch lifecycle authority. ``cmd_finish`` does not write status.json;
    this wait is for the process the launcher spawned.
    """
    sleep_s = min(max(float(poll_secs or 1.0), 0.2), 2.0)
    last_payload: dict = {}
    project_root = _project_root(args)
    try:
        while True:
            _export_dashboard_status_for_project(project_root)
            try:
                payload = json.loads(status_json.read_text(encoding="utf-8", errors="replace"))
                if isinstance(payload, dict):
                    last_payload = payload
                    state = payload.get("state")
                    if _is_status_terminal(state):
                        if _status_matches_current_launch(
                            payload,
                            dispatch_id=args.dispatch_id,
                            worker_pid=worker_pid,
                        ):
                            terminal_marker = payload.get("terminal_marker")
                            marker_terminal = (
                                terminal_marker
                                and isinstance(terminal_marker, dict)
                                and state == "complete"
                            )
                            stale_complete = (
                                marker_terminal and payload.get("worker_alive") is True
                                and not (payload.get("reason") or "").endswith(":post_terminal_idle_timeout")
                            )
                            if not stale_complete:
                                _export_dashboard_status_for_project(project_root)
                                return _status_exit_code(state), payload, payload.get("reason")
                            # Stale 'complete' while the worker is still flagged alive:
                            # keep waiting for the real worker exit, but ONLY while the
                            # watcher is alive to refresh status. If the watcher has died
                            # (e.g. an unwritable status dir froze this payload), do not
                            # spin forever or trust the false-alive 'complete' -- fall
                            # through to the watcher-dead repair path below.
                            if goalflight_compat.pid_alive(watcher_pid):
                                time.sleep(sleep_s)
                                continue
                        elif goalflight_compat.pid_alive(watcher_pid):
                            time.sleep(sleep_s)
                            continue
            except Exception:
                pass
            if not goalflight_compat.pid_alive(watcher_pid):
                repaired = _repair_watcher_terminal_status(
                    status_json,
                    args=args,
                    tail=tail,
                    worker_pid=worker_pid,
                    worker_identity=worker_identity,
                    pgid=pgid,
                    prompt_path=prompt_path,
                    reason="watcher_dead_before_terminal_status",
                )
                _export_dashboard_status_for_project(project_root)
                return _status_exit_code(repaired.get("state")), repaired, repaired.get("reason")
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        print(_foreground_wait_interrupt_hint(args.dispatch_id), file=sys.stderr)
        return 130, last_payload, "foreground_wait_interrupted"


def _sidecar_env(env: dict[str, str]) -> dict[str, str]:
    """Env for helpers that must not inherit the worker's lock fds.

    After the launcher releases its copy, ``GOALFLIGHT_WORKTREE_LOCK_FD`` in
    the worker env names a closed descriptor. Passing that dict to caffeinate
    (or any other sidecar) makes the daemon helper raise WorktreeSeatError
    instead of starting. Occupancy is the same class of leak: a sidecar that
    keeps ``GOALFLIGHT_OCCUPANCY_LOCK_FD`` open holds the tree after the
    worker dies.
    """
    out = dict(env)
    out.pop(goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, None)
    out.pop(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, None)
    return out


def _start_caffeinate(worker_pid: int, *, env: dict[str, str], stdout_path: Path) -> tuple[int | None, str | None]:
    if sys.platform != "darwin":
        return None, "not_darwin"
    caffeinate = shutil.which("caffeinate")
    if not caffeinate:
        return None, "caffeinate_not_found"
    try:
        pid = _spawn_daemonized_process(
            [caffeinate, "-dimsu", "-w", str(worker_pid)],
            env=_sidecar_env(env),
            stdout_path=stdout_path,
            stdout_mode="wb",
            stderr="stdout",
            label="caffeinate",
        )
        return pid, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _resolve_prompt_file(args, base: Path) -> str | None:
    """Normalize --prompt/--prompt-file to a file path (for stdin-fed workers)."""
    if args.prompt_file:
        # Absolute path: workers re-read via $GOALFLIGHT_PROMPT_FILE from their
        # own cwd, so a relative path would resolve against the wrong root.
        return str(Path(args.prompt_file).expanduser().resolve())
    if args.prompt is not None:
        base.mkdir(parents=True, exist_ok=True)
        pf = base / f"{args.dispatch_id}.prompt"
        pf.write_text(args.prompt, encoding="utf-8")
        return str(pf)
    return None


def _main_repo_root_for_orientation(project_root: Path) -> Path:
    root = project_root.expanduser().resolve(strict=False)
    try:
        top_raw = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        common_raw = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(root),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return root
    top = Path(top_raw).expanduser()
    common = Path(common_raw).expanduser()
    if not common.is_absolute():
        # git emits --git-common-dir relative to the COMMAND CWD, not the
        # toplevel; resolving against top sends a subdirectory dispatch to the
        # wrong tree entirely (rE P1).
        common = root / common
    common = common.resolve(strict=False)
    if common.name == ".git":
        return common.parent
    return top.resolve(strict=False)


def _project_orientation_path(project_root: Path, *, disabled: bool = False) -> Path | None:
    if disabled:
        return None
    path = _main_repo_root_for_orientation(project_root) / PROJECT_ORIENTATION_RELATIVE
    if not path.is_file():
        return None
    return path.resolve(strict=False)


def _project_orientation_preamble(orientation_path: Path) -> str:
    return (
        "PROJECT ORIENTATION\n"
        f"Path: {orientation_path}\n"
        f"{PROJECT_ORIENTATION_SCOPE_RULE}"
    )


def _project_root(args) -> Path:
    return goalflight_task.resolve_project_root(args.cwd or str(Path.cwd()))


def _worker_cwd(args) -> Path:
    """Tree the worker process is rooted in.

    Distinct from ``_project_root``, which collapses worktrees so they share a
    task store. Feeding that collapsed root back as ``--cwd`` made queued and
    resumed workers launch with ``-C <main checkout>`` (b-217, b-227).
    task store. The stall detector must measure this cwd, not the canonical
    root (b-229): sibling worktrees keep the shared tree "active" forever.
    """
    raw = getattr(args, "cwd", None)
    if raw:
        return Path(str(raw)).expanduser().resolve(strict=False)
    return Path.cwd().resolve(strict=False)


def _requested_worktree_base(args) -> str | None:
    raw = getattr(args, "worktree", None)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _inherited_seat_lock_present() -> bool:
    """True when this process already holds a pooled-seat lock fd.

    Occupancy uses a different fd in the same inherited-fd helper. Treating
    occupancy as "seat already leased" would skip ``--worktree`` acquire after
    the occupancy bind, or skip it in a child that only inherited occupancy.
    """
    raw = os.environ.get(goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, "").strip()
    if not raw:
        return False
    try:
        fd = int(raw)
        os.fstat(fd)
    except (ValueError, OSError) as exc:
        raise goalflight_worktree_pool.WorktreeSeatError(
            f"{goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV} does not name an "
            f"open descriptor: {raw!r}"
        ) from exc
    return True


def _bind_dispatch_worktree(args) -> goalflight_worktree_pool.WorktreeSeatLease | None:
    """Acquire a pooled seat when ``--worktree`` is set. Never falls back to add.

    The seat lock fd is left open on the returned lease. The caller must put
    ``GOALFLIGHT_WORKTREE_LOCK_FD`` in the worker env and pass that fd through
    spawn, then ``release()`` this process's copy so the worker's lifetime is
    the lease lifetime.

    Occupancy must bind AFTER this so the kernel lock is on the seat path,
    not the project ``--cwd``. Caching the lease lets the launch path call
    this once before occupancy and again when wiring env/summary.
    """
    existing = getattr(args, "_worktree_seat", None)
    if existing is not None:
        return existing
    base = _requested_worktree_base(args)
    if base is None:
        return None
    if _inherited_seat_lock_present():
        # Fleet / parent already leased a seat and passed the fd. Re-acquire
        # would LOCK_EX-succeed in this process (flock is per-process) and
        # reset a tree the worker is already in.
        return None
    lease = goalflight_worktree_pool.acquire_worktree_seat(
        _project_root(args),
        str(args.dispatch_id),
        base=base,
    )
    args.cwd = str(lease.path)
    args._worktree_seat = lease
    return lease


def _parse_task_ids(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in value.split(","):
            task_id = part.strip()
            if not task_id:
                continue
            if not TASK_ID_RE.match(task_id):
                raise DispatchUsageError(f"--task expects comma-separated t-/b- ids; got {task_id!r}")
            if task_id not in out:
                out.append(task_id)
    return out


def _state_dir() -> Path:
    return goalflight_dispatch_paths.state_dir()


def _dispatch_base_dir() -> Path:
    return goalflight_dispatch_paths.dispatch_base_dir()


def _dispatch_queue_dir() -> Path:
    return goalflight_dispatch_paths.dispatch_queue_dir()


def _safe_dispatch_filename(dispatch_id: str) -> str:
    return goalflight_dispatch_paths.safe_dispatch_filename(dispatch_id)


def _queue_entry_path(dispatch_id: str, *, queue_dir: Path | None = None) -> Path:
    return goalflight_dispatch_paths.queue_entry_path(dispatch_id, queue_dir=queue_dir)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _dashboard_refresh_key(project_root: Path) -> str:
    return hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]


def _dashboard_refresh_paths(project_root: Path) -> tuple[Path, Path]:
    base = _dispatch_base_dir()
    key = _dashboard_refresh_key(project_root)
    return base / f"dashboard-refresh-{key}.pid", base / f"dashboard-refresh-{key}.log"


def _dashboard_refresh_claim_path(pidfile: Path) -> Path:
    return pidfile.with_name(f"{pidfile.stem}.claim")


def _read_dashboard_refresh_pidfile(pidfile: Path) -> dict:
    try:
        text = pidfile.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        try:
            return {"pid": int(text)}
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _dashboard_refresh_cmdline_matches(identity: dict, project_root: Path) -> bool:
    args = str(identity.get("args") or "")
    return (
        _DASHBOARD_REFRESH_SUBCOMMAND in args
        and "--project-root" in args
        and str(project_root.resolve()) in args
    )


def _dashboard_refresh_identity_matches(recorded: object, current: object, project_root: Path) -> bool:
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return False
    if not recorded.get("identity_available", True) or not current.get("identity_available", True):
        return False
    for key in ("lstart", "comm"):
        if not recorded.get(key) or not current.get(key) or recorded.get(key) != current.get(key):
            return False
    return _dashboard_refresh_cmdline_matches(current, project_root)


def _dashboard_refresh_pidfile_is_current(pidfile: Path, project_root: Path) -> tuple[bool, str]:
    payload = _read_dashboard_refresh_pidfile(pidfile)
    try:
        pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        return False, "missing_pid"
    if payload.get("marker") != _DASHBOARD_REFRESH_MARKER:
        return False, "missing_marker"
    if payload.get("subcommand") != _DASHBOARD_REFRESH_SUBCOMMAND:
        return False, "wrong_subcommand"
    if payload.get("project_key") != _dashboard_refresh_key(project_root):
        return False, "wrong_project_key"
    if payload.get("project_root") != str(project_root.resolve()):
        return False, "wrong_project_root"
    if not goalflight_compat.pid_alive(pid):
        return False, "dead"
    current = goalflight_ledger.process_identity(pid)
    if current is None:
        return False, "dead"
    if not _dashboard_refresh_identity_matches(payload.get("identity"), current, project_root):
        return False, "identity_mismatch"
    return True, "live"


def _try_claim_dashboard_refresh_start(claim_path: Path) -> bool:
    claim_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "created_at": time.time()}) + "\n")
    return True


def _remove_stale_dashboard_refresh_claim(claim_path: Path) -> bool:
    try:
        age_s = time.time() - claim_path.stat().st_mtime
    except OSError:
        return False
    if age_s <= _DASHBOARD_REFRESH_CLAIM_STALE_S:
        return False
    with contextlib.suppress(OSError):
        claim_path.unlink()
    return True


def _wait_for_dashboard_refresh_claim(pidfile: Path, claim_path: Path, project_root: Path) -> bool:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        ok, _reason = _dashboard_refresh_pidfile_is_current(pidfile, project_root)
        if ok:
            return True
        if not claim_path.exists():
            return False
        time.sleep(0.05)
    return False


def _write_dashboard_refresh_pidfile(pidfile: Path, project_root: Path, pid: int) -> None:
    identity = goalflight_ledger.process_identity(pid)
    payload = {
        "schema": 1,
        "marker": _DASHBOARD_REFRESH_MARKER,
        "subcommand": _DASHBOARD_REFRESH_SUBCOMMAND,
        "pid": pid,
        "identity": identity,
        "project_root": str(project_root.resolve()),
        "project_key": _dashboard_refresh_key(project_root),
        "started_at": goalflight_ledger.utc_now(),
    }
    _write_json_atomic(pidfile, payload)


def _export_dashboard_status_for_project(project_root: Path | None) -> None:
    if project_root is None:
        return
    try:
        import goalflight_status

        goalflight_status.export_dashboard_status(project_root)
    except Exception:
        # Derived dashboard projection only. Losing it gives up one refresh;
        # journal/ledger/status authorities remain unchanged.
        pass


def _upsert_project_registry_for_dispatch(project_root: Path | None) -> None:
    if project_root is None:
        return
    try:
        root = SCRIPT_DIR.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import goalflight_task  # type: ignore

        goalflight_task.upsert_project_registry(
            project_root,
            throttle_s=goalflight_task.PROJECT_REGISTRY_THROTTLE_S,
        )
    except Exception:
        # Discovery index only. Losing it gives up one dashboard project hint,
        # never dispatch ownership or lifecycle state.
        pass


def _dashboard_record_seen_at(record: dict) -> dt.datetime | None:
    for key in ("updated_at", "started_at"):
        parsed = goalflight_ledger.parse_utc(record.get(key))
        if parsed is not None:
            return parsed
    return None


def _dashboard_queued_record_within_grace(record: dict, *, now: dt.datetime) -> bool:
    seen_at = _dashboard_record_seen_at(record)
    if seen_at is None:
        return False
    return (now - seen_at).total_seconds() <= _DASHBOARD_REFRESH_QUEUED_GRACE_S


def _dashboard_refresh_record_counts_as_live(record: dict, *, now: dt.datetime, goalflight_status) -> bool:
    classification = goalflight_ledger.classify(record)
    queued_states = {"queued", "queued_capacity", "waiting_capacity"}
    if str(record.get("state") or "") in queued_states or str(classification or "") in queued_states:
        return _dashboard_queued_record_within_grace(record, now=now)
    terminal_state = record.get("terminal_state") or goalflight_ledger.terminal_state_for(
        record.get("state"),
        record.get("reason") or record.get("error"),
    )
    status_record = dict(record, classification=classification, terminal_state=terminal_state)
    return goalflight_status.done_code(status_record) == 1


def _dashboard_project_has_live_dispatch(project_root: Path) -> bool:
    try:
        import goalflight_status

        root = str(project_root.resolve())
        now = dt.datetime.now(dt.timezone.utc)
        records = [
            record
            for record in goalflight_ledger.read_records()
            if record.get("project_root") == root
        ]
    except Exception:
        return False
    return any(
        _dashboard_refresh_record_counts_as_live(record, now=now, goalflight_status=goalflight_status)
        for record in records
    )


def _dashboard_refresh_disabled() -> bool:
    """True while the operator kill switch file exists.

    A file rather than an environment variable: the refresher is spawned
    detached by whichever controller session happens to dispatch next, so an
    env var set in one session would not reach the others. Checking a path
    also lets daemons that are already running exit at their next cycle
    instead of having to be hunted down by pid.
    """
    return _DASHBOARD_REFRESH_DISABLE_FLAG.exists()


def _dashboard_refresh_loop(
    project_root: Path,
    *,
    interval_s: float,
    max_lifetime_s: float = _DASHBOARD_REFRESH_MAX_LIFETIME_S,
) -> int:
    if not (project_root / "dashboard").is_dir():
        return 0
    interval = min(max(float(interval_s or 15.0), 1.0), 15.0)
    started = time.monotonic()
    while True:
        if _dashboard_refresh_disabled():
            return 0
        _export_dashboard_status_for_project(project_root)
        if time.monotonic() - started >= float(max_lifetime_s):
            return 0
        if not _dashboard_project_has_live_dispatch(project_root):
            return 0
        time.sleep(interval)


def _cmd_dashboard_refresh(argv: list[str]) -> int:
    parser = _TerseArgumentParser(
        description=argparse.SUPPRESS,
        usage_hint="try dashboard-refresh --project-root <path> (or --help)",
    )
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--interval-s", type=float, default=15.0)
    parser.add_argument("--max-lifetime-s", type=float, default=_DASHBOARD_REFRESH_MAX_LIFETIME_S)
    args = parser.parse_args(argv)
    return _dashboard_refresh_loop(
        Path(args.project_root).resolve(),
        interval_s=args.interval_s,
        max_lifetime_s=args.max_lifetime_s,
    )


def _start_dashboard_refresh_for_project(project_root: Path | None) -> None:
    if project_root is None or not (project_root / "dashboard").is_dir():
        return
    if _dashboard_refresh_disabled():
        return
    pidfile, log_path = _dashboard_refresh_paths(project_root)
    claim_path = _dashboard_refresh_claim_path(pidfile)
    for _attempt in range(2):
        with contextlib.suppress(Exception):
            ok, _reason = _dashboard_refresh_pidfile_is_current(pidfile, project_root)
            if ok:
                return
        if not _try_claim_dashboard_refresh_start(claim_path):
            if _remove_stale_dashboard_refresh_claim(claim_path):
                continue
            with contextlib.suppress(Exception):
                if _wait_for_dashboard_refresh_claim(pidfile, claim_path, project_root):
                    return
            return
        try:
            with contextlib.suppress(Exception):
                ok, _reason = _dashboard_refresh_pidfile_is_current(pidfile, project_root)
                if ok:
                    return
                pidfile.unlink()
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with log_path.open("ab") as log:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        _DASHBOARD_REFRESH_SUBCOMMAND,
                        "--project-root",
                        str(project_root.resolve()),
                        "--interval-s",
                        "15",
                        "--max-lifetime-s",
                        str(_DASHBOARD_REFRESH_MAX_LIFETIME_S),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    **_detached_popen_kwargs(),
                )
            _write_dashboard_refresh_pidfile(pidfile, project_root, int(proc.pid))
            return
        except OSError:
            # Dashboard refresh is a derived monitor mirror. Transient spawn or
            # pidfile I/O can wait for the next caller; invalid pidfile payloads
            # and other contract faults must escape.
            return
        finally:
            with contextlib.suppress(OSError):
                claim_path.unlink()


@contextlib.contextmanager
def _queue_mutation_lock(queue_dir: Path):
    queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = queue_dir / ".submit.lock"
    if fcntl is not None:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return

    deadline = time.monotonic() + 30.0
    lock_fd = None
    while lock_fd is None:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for queue mutation lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(lock_fd)
        with contextlib.suppress(OSError):
            lock_path.unlink()


class _QueueTryLock:
    def __init__(self, path: Path, *, fh=None, fd: int | None = None):
        self.path = path
        self.fh = fh
        self.fd = fd

    def release(self) -> None:
        if self.fh is not None:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()
                self.fh = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            with contextlib.suppress(OSError):
                self.path.unlink()


def try_acquire_queue_lock(queue_dir: Path, *, deadline_s: float) -> _QueueTryLock | None:
    queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = queue_dir / ".submit.lock"
    if fcntl is not None:
        fh = path.open("a+", encoding="utf-8")
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return _QueueTryLock(path, fh=fh)
            except BlockingIOError:
                if time.monotonic() >= deadline_s:
                    fh.close()
                    return None
                time.sleep(min(RECONCILE_LOCK_POLL_S, max(0.0, deadline_s - time.monotonic())))
            except OSError:
                fh.close()
                raise
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            return _QueueTryLock(path, fd=fd)
        except FileExistsError:
            if time.monotonic() >= deadline_s:
                return None
            time.sleep(min(RECONCILE_LOCK_POLL_S, max(0.0, deadline_s - time.monotonic())))


def _tail_lock_path(tail: Path) -> Path:
    return tail.with_name(f".{tail.name}.completion.lock")


class _TailLockBusy(RuntimeError):
    """The worker still owns its stdout tail, so reconciliation must skip."""


@contextlib.contextmanager
def _tail_mutation_lock(tail: Path):
    """Serialize worker tail appends with completion decisions and writes."""
    tail.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        with tail.open("a+b") as tail_file:
            fcntl.flock(tail_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(tail_file.fileno(), fcntl.LOCK_UN)
        return

    lock_path = _tail_lock_path(tail)
    deadline = time.monotonic() + 30.0
    lock_fd = None
    while lock_fd is None:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for tail mutation lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(lock_fd)
        with contextlib.suppress(OSError):
            lock_path.unlink()


@contextlib.contextmanager
def _tail_reconciliation_lock(tail: Path):
    """Acquire a proven local worker-held tail flock, immediately or defer.

    Platform selection lives in ``resolve_reconciliation_mode``. The old
    O_EXCL fallback was not worker-held and therefore cannot establish EOF.
    """
    tail.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        raise _TailLockBusy(f"unavailable worker-held flock: {tail}")
    with tail.open("a+b") as tail_file:
        try:
            fcntl.flock(tail_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _TailLockBusy(str(tail)) from exc
        try:
            yield
        finally:
            fcntl.flock(tail_file.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _fallback_reconciliation_lock(tail: Path):
    """Serialize reconcilers only after whole-set producer death is proven."""
    path = _tail_lock_path(tail)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError as exc:
        raise _TailLockBusy(str(tail)) from exc
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()


_FLOCK_CAPABILITY_CACHE: dict[tuple, FlockCapability] = {}
_FS_IDENTITY_CACHE: dict[tuple, FilesystemIdentity] = {}
_MOUNT_TABLE_CACHE: str | None = None
_PASS_CACHE: dict | None = None


def _pass_time(key: str, elapsed: float, *, count_key: str | None = None) -> None:
    cache = _PASS_CACHE
    if not cache:
        return
    timing = cache["timing"]
    timing[key] = round(float(timing.get(key) or 0.0) + elapsed, 3)
    if count_key:
        timing[count_key] = int(timing.get(count_key) or 0) + 1


def _begin_recovery_pass_cache() -> dict:
    global _PASS_CACHE
    cache = {
        "timing": {
            "listing_s": 0.0,
            "claim_loop_s": 0.0,
            "ledger_lookup_s": 0.0,
            "ledger_lookup_n": 0,
            "ledger_snapshot_s": 0.0,
            "ledger_snapshot_n": 0,
            "fs_identity_s": 0.0,
            "fs_identity_n": 0,
            "flock_probe_s": 0.0,
            "flock_probe_n": 0,
            "carrier_index_s": 0.0,
            "carrier_index_lookups": 0,
            "ledger_orphans_s": 0.0,
            "resume_prepared_s": 0.0,
            "skip_s": 0.0,
            "skip_n": 0,
            "probe_stamp_n": 0,
            "claimed_carriers": 0,
        },
        "ledger_records": None,
        "carrier_index": None,
    }
    _PASS_CACHE = cache
    return cache


def _end_recovery_pass_cache() -> dict:
    global _PASS_CACHE
    cache = _PASS_CACHE
    _PASS_CACHE = None
    if not cache:
        return {}
    return cache["timing"]


def _pass_ledger_records() -> list[dict]:
    """One ledger snapshot per recovery pass; live read outside a pass."""
    cache = _PASS_CACHE
    if cache is None:
        return goalflight_ledger.read_records()
    if cache["ledger_records"] is None:
        t0 = time.monotonic()
        cache["ledger_records"] = goalflight_ledger.read_records()
        _pass_time("ledger_snapshot_s", time.monotonic() - t0)
        cache["timing"]["ledger_snapshot_n"] = len(cache["ledger_records"])
    return cache["ledger_records"]


def _darwin_mount_table() -> str:
    """Process-lifetime ``mount`` stdout. Identical for every local tail."""
    global _MOUNT_TABLE_CACHE
    if _MOUNT_TABLE_CACHE is not None:
        return _MOUNT_TABLE_CACHE
    try:
        mounts = subprocess.run(
            ["mount"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        _MOUNT_TABLE_CACHE = ""
        return _MOUNT_TABLE_CACHE
    _MOUNT_TABLE_CACHE = mounts.stdout if mounts.returncode == 0 else ""
    return _MOUNT_TABLE_CACHE


def _tail_filesystem_identity(tail: Path, *, locality: str) -> FilesystemIdentity:
    t0 = time.monotonic()
    try:
        resolved = tail.expanduser().resolve(strict=False)
        parent = resolved.parent
        probe_parent = parent
        while not probe_parent.exists() and probe_parent != probe_parent.parent:
            probe_parent = probe_parent.parent
        cache_key = (str(probe_parent), locality)
        cached = _FS_IDENTITY_CACHE.get(cache_key)
        if cached is not None:
            return cached
        try:
            stat_result = probe_parent.stat()
            device = int(stat_result.st_dev)
        except OSError:
            identity = FilesystemIdentity(None, str(parent), "unknown", "unknown")
            _FS_IDENTITY_CACHE[cache_key] = identity
            return identity
        fs_type = "unknown"
        identity_mount = f"dev:{device}"
        if sys.platform == "darwin":
            best: tuple[int, str, str] | None = None
            for line in _darwin_mount_table().splitlines():
                match = re.search(r" on (.+?) \(([^, )]+)", line)
                if not match:
                    continue
                mount_path, candidate_type = match.group(1), match.group(2)
                if str(probe_parent).startswith(mount_path.rstrip("/") + "/") or str(probe_parent) == mount_path:
                    candidate = (len(mount_path), candidate_type.lower(), mount_path)
                    if best is None or candidate[0] > best[0]:
                        best = candidate
            if best is not None:
                fs_type = best[1]
                identity_mount = best[2]
        commands = (["stat", "-f", "-c", "%T", str(probe_parent)],)
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=1.0,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0 and result.stdout.strip():
                fs_type = result.stdout.strip().lower()
                break
        shared_types = {"nfs", "nfs4", "smbfs", "cifs", "afpfs", "sshfs", "fuse.sshfs"}
        effective_locality = "shared" if any(name in fs_type for name in shared_types) else locality
        identity = FilesystemIdentity(device, identity_mount, fs_type, effective_locality)
        _FS_IDENTITY_CACHE[cache_key] = identity
        return identity
    finally:
        _pass_time("fs_identity_s", time.monotonic() - t0, count_key="fs_identity_n")


def _probe_flock_capability(
    *,
    transport: str,
    node: str | None,
    tail_path: Path,
    filesystem: FilesystemIdentity,
) -> FlockCapability:
    t0 = time.monotonic()
    try:
        if fcntl is None:
            return FlockCapability.UNAVAILABLE
        # Flock coherence is a filesystem property, not a per-tail one.
        # Keying on tail_path re-spawned two Python subprocesses per carrier.
        key = (
            transport,
            node,
            filesystem.device,
            filesystem.mount_path,
            filesystem.filesystem_type,
            filesystem.locality,
        )
        cached = _FLOCK_CAPABILITY_CACHE.get(key)
        if cached is not None:
            return cached
        if filesystem.device is None or filesystem.locality not in {"local", "shared"}:
            return FlockCapability.UNPROVEN
        probe_parent = tail_path.expanduser().resolve(strict=False).parent
        while not probe_parent.exists() and probe_parent != probe_parent.parent:
            probe_parent = probe_parent.parent
        probe = probe_parent / f".goalflight-flock-probe-{os.getpid()}"
        child = (
            "import fcntl,sys; f=open(sys.argv[1],'a+b'); "
            "\ntry: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
            "\nexcept BlockingIOError: sys.exit(0)"
            "\nelse: sys.exit(3)"
        )
        capability = FlockCapability.UNPROVEN
        try:
            with probe.open("a+b") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX)
                blocked = subprocess.run(
                    [sys.executable, "-c", child, str(probe)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                )
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            acquired = subprocess.run(
                [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1],'a+b'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)", str(probe)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            if blocked.returncode == 0 and acquired.returncode == 0:
                # Both contenders ran on this host. This can prove only local
                # coherence, even when the path happens to be a shared mount.
                # Cross-node authority requires an actual node-side probe.
                capability = FlockCapability.COHERENT_LOCAL
        except (OSError, subprocess.SubprocessError):
            capability = FlockCapability.UNPROVEN
        finally:
            with contextlib.suppress(OSError):
                probe.unlink()
        # Cache only a proven-coherent result. Caching UNPROVEN would poison
        # every later tail on the same volume for the rest of the process.
        if capability is FlockCapability.COHERENT_LOCAL:
            _FLOCK_CAPABILITY_CACHE[key] = capability
        return capability
    finally:
        _pass_time("flock_probe_s", time.monotonic() - t0, count_key="flock_probe_n")


def resolve_reconciliation_mode(
    *,
    transport: str,
    node: str | None,
    locality: str,
    tail_path: Path,
    tail_filesystem: FilesystemIdentity,
    flock_probe: FlockCapability,
    producer_set_authoritative: bool = False,
    node_authority_available: bool = False,
    worker_tail_lock_contract: bool = True,
) -> ReconciliationMode:
    """Fail-closed platform resolver, rebound and revalidated every attempt."""
    _ = tail_path
    if transport == "fleet" or locality == "remote":
        if (
            flock_probe is FlockCapability.COHERENT_CROSS_NODE
            and worker_tail_lock_contract
        ):
            return ReconciliationMode.LOCAL_FLOCK
        return ReconciliationMode.DEFER_TO_NODE if node and node_authority_available else ReconciliationMode.FAIL_CLOSED_DEFER
    if (
        transport in {"bash", "acp"}
        and tail_filesystem.locality == "local"
        and flock_probe is FlockCapability.COHERENT_LOCAL
        and worker_tail_lock_contract
    ):
        return ReconciliationMode.LOCAL_FLOCK
    if (
        transport in {"bash", "acp"}
        and flock_probe in {FlockCapability.UNAVAILABLE, FlockCapability.UNPROVEN}
        and producer_set_authoritative
    ):
        return ReconciliationMode.FALLBACK_PID_IDENTITY
    return ReconciliationMode.FAIL_CLOSED_DEFER


def _skill_root() -> Path:
    return goalflight_compat.advertised_skill_root(running_file=__file__)


def _status_reminder_lines(
    dispatch_id: str,
    *,
    status_json: Path | str,
    tail_path: Path | str,
    worker_pid: int,
    shape: str,
    skill_root: Path | None = None,
    agent: str | None = None,
    controller_pid: int | None = None,
    poll_secs: float | None = None,
    max_idle_secs: float | None = None,
    prompt_path: Path | str | None = None,
    hints: bool = False,
) -> list[str]:
    """Post-dispatch stderr reminder.

    Default is one line: dispatch id + status path. The DISPATCH-LAUNCHED JSON
    already carries wait/done/watch fields, so repeating them on every launch
    is wallpaper. Pass ``hints=True`` (CLI ``--hints``) for the teaching block
    that names the status/wait/done tools and why not to hand-roll
    ``ps`` / ``tail -f`` / backgrounded watchers — they race the worker and
    exit early. That warning lives here, in the module docstring, and in
    ``protocols/legacy/tail-f.md``; it does not belong on the hot-loop default.
    """
    status_path = Path(status_json).resolve()
    if not hints:
        return [f"[goal-flight] dispatched {dispatch_id}  status={status_path}"]
    root = Path(os.path.abspath(os.path.expanduser(str(skill_root or _skill_root()))))
    tail = Path(tail_path).resolve()
    status_py = root / "scripts" / "goalflight_status.py"
    watch_py = root / "scripts" / "goalflight_watch.py"
    messages_py = root / "scripts" / "goalflight_messages.py"
    # Every path below is shell-quoted: these lines are pasted verbatim into
    # someone else's shell, and an install path containing a space silently
    # tokenizes into two arguments (`/tmp/skill root/...` -> `/tmp/skill` +
    # `root/...`), producing a command that fails on a machine we never see.
    q = shlex.quote
    lines = [
        f"[goal-flight] dispatched {dispatch_id} ({shape}). Check status with the python "
        "tooling — do NOT hand-roll ps/tail -f/backgrounded watchers (they race the worker "
        "and exit early):",
        f"  status: python3 {q(str(status_py))} --dispatch {dispatch_id}",
        # The wait is shown in its BACKGROUNDED form on purpose. Printed as a
        # plain foreground command it reads as "block here for however long the
        # worker takes", which the >10s foreground rule forbids -- so controllers
        # resolved the contradiction by abandoning the event wait entirely and
        # polling on a timer instead. A timer cannot know when the work finished;
        # it only knows when the clock did. Backgrounding the wait keeps the
        # event as the wake signal AND keeps the turn free.
        f"  wait:   python3 {q(str(status_py))} --wait {dispatch_id}"
        "   # run this in the BACKGROUND; its completion wakes you on the",
        "          #   real event. Do not block the turn on it, and do not"
        "  replace it with a timer -- a timer wakes on the clock, not the work.",
        f"  done?:  python3 {q(str(status_py))} --done {dispatch_id}   "
        "# exit 0=terminal, 1=running, 2=ambiguous",
        f"  mail:   python3 {q(str(messages_py))} relay   # current project, open + unread",
    ]
    if shape == "acp":
        watch_parts = [
            "python3",
            str(watch_py),
            "--pid",
            str(worker_pid),
            "--tail",
            str(tail),
            "--status-json",
            str(status_path),
        ]
        if prompt_path is not None:
            watch_parts += ["--ignore-prompt-file", str(Path(prompt_path).resolve())]
        lines.append("  watch:  " + shlex.join(watch_parts))
    else:
        # Moonshot (kimi CLI) renderer normalization keys off the production
        # preset label; synthetic -bash-tail aliases are not first-class dispatch agents.
        agent_label = (
            "moonshot"
            if agent == "moonshot"
            else (f"{agent}-bash-tail" if agent else "worker-bash-tail")
        )
        watch_parts = [
            "bash",
            str(root / "scripts" / "watch-dispatch-tail.sh"),
            "--pid",
            str(worker_pid),
            "--tail",
            str(tail),
        ]
        if controller_pid is not None:
            watch_parts += ["--controller-pid", str(controller_pid)]
        watch_parts += [
            "--agent",
            agent_label,
            "--session-id",
            dispatch_id,
        ]
        if poll_secs is not None:
            watch_parts += ["--poll-secs", str(poll_secs)]
        if max_idle_secs is not None:
            watch_parts += ["--max-idle-secs", str(max_idle_secs)]
        if prompt_path is not None:
            watch_parts += ["--ignore-prompt-file", str(Path(prompt_path).resolve())]
        lines.append("  watch:  " + shlex.join(watch_parts))
    return lines


def _print_status_reminder(
    dispatch_id: str,
    *,
    status_json: Path | str,
    tail_path: Path | str,
    worker_pid: int,
    shape: str,
    skill_root: Path | None = None,
    agent: str | None = None,
    controller_pid: int | None = None,
    poll_secs: float | None = None,
    max_idle_secs: float | None = None,
    prompt_path: Path | str | None = None,
    hints: bool = False,
) -> None:
    for line in _status_reminder_lines(
        dispatch_id,
        status_json=status_json,
        tail_path=tail_path,
        worker_pid=worker_pid,
        shape=shape,
        skill_root=skill_root,
        agent=agent,
        controller_pid=controller_pid,
        poll_secs=poll_secs,
        max_idle_secs=max_idle_secs,
        prompt_path=prompt_path,
        hints=hints,
    ):
        print(line, file=sys.stderr, flush=True)


def _print_background_default_notice() -> None:
    print(
        "goalflight_dispatch: detached by default (was blocking); pass --foreground to wait for the worker.",
        file=sys.stderr,
        flush=True,
    )


def _steer_file(dispatch_id: str) -> Path:
    return goalflight_dispatch_paths.steer_file(dispatch_id)


def _raw_worker_args(args) -> list[str]:
    return args.worker[1:] if args.worker and args.worker[0] == "--" else args.worker


def _prompt_requested(args) -> bool:
    return bool(args.prompt_file) or args.prompt is not None


def _read_prompt_for_guard(args) -> str:
    if args.prompt is not None:
        return str(args.prompt)
    if args.prompt_file:
        try:
            return Path(args.prompt_file).expanduser().read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return ""
    return ""


def _extract_git_base_pins(text: str) -> list[str]:
    """Explicitly-marked git base pins, in document order. Bare SHAs are prose."""
    text = text or ""
    marked = [
        (match.start(), match.group(1).lower())
        for pattern in GIT_BASE_PIN_MARKER_RES
        for match in pattern.finditer(text)
    ]
    marked.sort()
    return [sha for _pos, sha in marked]


def _git_head_for_cwd(cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--verify", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    head = proc.stdout.strip().splitlines()[0].lower() if proc.stdout.strip() else ""
    return head if LOWER_BASE_SHA_RE.fullmatch(head) else None


def _valid_lower_base_sha(value: object) -> str | None:
    raw = str(value or "").strip()
    return raw if LOWER_BASE_SHA_RE.fullmatch(raw) else None


def _git_pin_warning(args) -> str | None:
    """Advisory git-base-pin check. A genuine mismatch WARNS and still launches.

    Refusing would be the stricter reading of the 2026-08-27 sweep (warn then
    start). Keep it advisory: a stale worktree base is sometimes intentional
    (reproduce a bug; finish a chunk started on an older merge), and
    --ignore-git-warn already exists as the silence hatch. Two writers in one
    tree (t-375) corrupt the filesystem; a mismatched pin does not. After
    t-356 this warning only fires on explicit markers, so it can be trusted
    instead of trained-ignored.
    """
    if getattr(args, "ignore_git_warn", False) or not _prompt_requested(args):
        return None
    # b-052: measure the tree the worker will actually run in (--cwd), not the
    # canonical project root.  resolve_project_root deliberately collapses a
    # worktree back to the registered root (so worktrees share one task store),
    # which made a CORRECT worktree pin warn against the main checkout's HEAD --
    # a warning that measures the wrong tree trains operators to ignore it.
    worker_tree = Path(args.cwd) if getattr(args, "cwd", None) else _project_root(args)
    head = _git_head_for_cwd(worker_tree)
    if not head:
        return None
    text = _read_prompt_for_guard(args)
    pins = _extract_git_base_pins(text)
    short_head = head[:7]
    if not pins:
        return (
            "WARN: prompt carries no git base pin; "
            f"HEAD is {short_head} - workers on stale clones will build on the wrong base; "
            f"mark the base explicitly with a 'base: {short_head}' line or a "
            f"'branch <name> @ {short_head}' header, or pass --ignore-git-warn"
        )
    mismatched_pins = [pin for pin in pins if not head.startswith(pin)]
    if not mismatched_pins:
        return None
    return (
        "WARN: GIT BASE PIN MISMATCH: "
        f"prompt pin {mismatched_pins[0]} does not match cwd HEAD {short_head}; "
        "stale brief or wrong repo state - workers on stale clones will build on the wrong base; "
        "advisory only, launch continues (a stale base is sometimes intentional); "
        "update the pin or pass --ignore-git-warn"
    )


def _grok_model_passthrough_warning(args) -> str | None:
    if args.agent not in {"grok-code", "grok-research"} or not getattr(args, "model", None):
        return None
    return (
        f"NOTE: --agent {args.agent} omits --model by default so grok's CLI "
        "default applies and auto-tracks; your explicit --model is honored — "
        "pass it only to pin a non-default grok model"
    )


def _dispatch_warnings(args, raw_argv: list[str]) -> list[str]:
    if raw_argv:
        return []
    warnings = []
    for warning in (
        _git_pin_warning(args),
        _grok_model_passthrough_warning(args),
        _os_sandbox_warning(args),
    ):
        if warning:
            warnings.append(warning)
    return warnings


def _emit_dispatch_warnings(
    warnings: list[str],
    *,
    tail_path: Path | None = None,
    reset_tail: bool = False,
) -> None:
    if not warnings:
        return
    tail_file = None
    if tail_path is not None:
        with contextlib.suppress(OSError):
            tail_path.parent.mkdir(parents=True, exist_ok=True)
            tail_file = tail_path.open("w" if reset_tail else "a", encoding="utf-8")
    try:
        for warning in warnings:
            line = f"goalflight_dispatch: {warning}"
            print(line, file=sys.stderr, flush=True)
            if tail_file is not None:
                print(line, file=tail_file, flush=True)
    finally:
        if tail_file is not None:
            tail_file.close()


def _read_only_write_prompt_reason(args) -> str | None:
    if not _effective_read_only(args) or not _prompt_requested(args):
        return None
    text = _read_prompt_for_guard(args)
    if not text:
        return None
    for _label, pattern in READ_ONLY_INLINE_RETURN_PROMPT_PATTERNS:
        if pattern.search(text):
            return None
    for label, pattern in READ_ONLY_WRITE_PROMPT_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _guard_read_only_write_prompt(args) -> None:
    reason = _read_only_write_prompt_reason(args)
    if not reason:
        return
    raise DispatchUsageError(
        "--read-only prompt appears to require writing a review artifact "
        f"(matched {reason}). Read-only workers cannot write review files; "
        "return findings inline in the final response, or use a writable "
        "sandbox/worktree."
    )


def _research_intent_reason(args) -> str | None:
    if args.agent != "grok-code" or getattr(args, "web_research_ok", False):
        return None
    # --read-only dispatches are review/analysis posture, not live web research
    # (the same inline-return lesson as the B5c guard). Genuine web research
    # belongs on --agent grok-research regardless.
    if _effective_read_only(args):
        return None
    if not _prompt_requested(args):
        return None
    text = _read_prompt_for_guard(args)
    if not text:
        return None
    for pattern in RESEARCH_INTENT_SUPPRESSOR_PATTERNS:
        if pattern.search(text):
            return None
    for label, pattern in RESEARCH_INTENT_PROMPT_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _guard_grok_code_research_prompt(args) -> None:
    reason = _research_intent_reason(args)
    if not reason:
        return
    raise DispatchUsageError(
        f"prompt looks like WEB RESEARCH (matched: {reason}) but --agent grok-code "
        "runs the composer CODING model, which reliably fails web_fetch/web_search "
        "(thin, error-prone results — observed live 2026-06-09). Re-dispatch with "
        "--agent grok-research (web model, web tools on), or pass --web-research-ok "
        "if this is genuinely a coding task that merely mentions the web."
    )


def _validate_before_side_effects(args, raw_argv: list[str]) -> None:
    if not raw_argv:
        retired = RETIRED_AGENT_LABELS.get(args.agent)
        if retired:
            raise DispatchUsageError(
                f"--agent {args.agent!r} is retired — {retired}"
            )
        if args.agent not in PRESET_AGENTS:
            raise DispatchUsageError(
                "no worker preset for --agent "
                f"{args.agent!r} — use --agent codex|grok-code|grok-research|moonshot|cursor|claude with "
                "--prompt/--prompt-file, or pass a raw worker after `-- <cmd...>`"
            )
        if args.agent in STDIN_PROMPT_AGENTS and not _prompt_requested(args):
            raise DispatchUsageError(
                f"--agent {args.agent} requires --prompt or --prompt-file; refusing to feed empty stdin"
            )
        if args.prompt_file and not Path(args.prompt_file).expanduser().exists():
            raise DispatchUsageError(f"prompt file not found: {args.prompt_file}")
        _guard_read_only_write_prompt(args)
        _guard_grok_code_research_prompt(args)
    # `--os-sandbox` is a dispatch-level safety claim even when `-- <cmd>`
    # overrides the preset. Skipping it here made a raw grok argv with an
    # inert profile launch as if the flag were honoured.
    _validate_os_sandbox_conflict(args)
    _validate_agent_os_sandbox(args)
    _validate_os_sandbox_boundary(args)


def _nonterminal_dispatch_reuse_reason(
    dispatch_id: str,
    *,
    allow_queued: bool = False,
) -> str | None:
    record = _find_dispatch_record(dispatch_id)
    if record is None:
        return None
    if (
        allow_queued
        and record.get("state") == "queued"
        and not record.get("worker_pid")
    ):
        return None
    terminal = goalflight_ledger.terminal_state_for(
        record.get("state"),
        record.get("reason") or record.get("error"),
    )
    if terminal != "unknown" and record.get("state") != "watcher_stopped":
        return None
    classification = goalflight_ledger.classify(record)
    if record.get("state") == "watcher_stopped" and classification == "watcher_stopped":
        return None
    return (
        f"classification={classification} state={record.get('state') or 'running'} "
        f"status={record.get('status_path') or '-'}"
    )


def _launch_authority_entry(args) -> dict:
    return {
        "dispatch_id": str(getattr(args, "dispatch_id", "") or ""),
        "task_ids": list(getattr(args, "task_ids", []) or []),
        "project_root": str(_project_root(args)),
        "created_at": goalflight_ledger.utc_now(),
        "dispatch_argv": list(getattr(args, "_original_argv", None) or []),
    }


def _refuse_launch_blocked_by_completion_authority(args) -> None:
    """Launch-path sibling/completion gate. Occupancy-forced does not bypass this.

    Occupancy is a worktree lock. Two workers on one task is a different hole:
    drain used to spawn without asking ``_entry_completion_authority``, whose
    production callers were restore / reconcile / terminal-commit only.

    ``--submit`` is not a spawn: occupancy-forced may still queue into an
    occupied tree. Drain consults this gate before the child runs.
    """
    if getattr(args, "submit", False):
        return
    if not getattr(args, "task_ids", None):
        return
    decision = _entry_completion_authority(_launch_authority_entry(args))
    if not _completion_decision_blocks_restore(decision):
        return
    assert isinstance(decision, dict)
    message = str(decision.get("reason") or "partial_task_supersession")
    print(
        DISPATCH_REFUSED_PREFIX
        + json.dumps(
            {
                "dispatch_id": getattr(args, "dispatch_id", None),
                "permanent": True,
                "reason": message,
                "state": str(decision.get("state") or "worker_dead"),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    raise DispatchUsageError(message)


def _refuse_reused_nonterminal_dispatch_id(
    dispatch_id: str,
    *,
    allow_queued: bool = False,
) -> None:
    reason = _nonterminal_dispatch_reuse_reason(dispatch_id, allow_queued=allow_queued)
    if not reason:
        return
    raise DispatchUsageError(
        f"dispatch id {dispatch_id!r} already has a non-terminal ledger record "
        f"({reason}). Use a unique --dispatch-id per parallel chunk; in shell "
        "loops, launch each dispatch as its own background command."
    )


def _refuse_reused_dispatch_id_for_launch(dispatch_id: str, *, allow_queued: bool = False) -> None:
    if allow_queued:
        _refuse_reused_nonterminal_dispatch_id(dispatch_id, allow_queued=True)
    else:
        _refuse_reused_nonterminal_dispatch_id(dispatch_id)


def _record_declared_read_only(record: dict) -> bool:
    """True when the incumbent's own recorded launch posture cannot write.

    Write-capability is the dispatch's enforced sandbox, not a process scan
    and not a bare --read-only declaration. A ``--`` worker that was asked
    to be read-only can still write; treating it as a reviewer would let a
    second writer in. Do not consult supported_profile: that field names
    what the launch path knows how to support, and can say read-only even
    for a writer. A top-level read_only flag with requested_profile
    workspace-write is a writer.
    """
    posture = record.get("os_sandbox")
    requested = None
    enforced = None
    if isinstance(posture, dict):
        requested = posture.get("requested_profile")
        enforced = posture.get("enforced_profile")
        if requested == "workspace-write":
            return False
        if enforced == "read-only":
            return True
    if requested != "read-only":
        return False
    agent = str(record.get("agent") or "")
    shape = str(record.get("shape") or "bash")
    if shape == "acp":
        return False
    return agent in {"grok-code", "grok-research", "codex"}


def _occupancy_exempt_read_only(args) -> bool:
    """True when this launch cannot write the tree, so occupancy does not apply.

    --read-only on a ``-- <cmd>`` worker is a declaration only: the command
    can still write. Occupancy skips only launch paths that actually enforce
    the posture (bash-shape grok deny-rules, bash-shape codex --sandbox).
    """
    if not _effective_read_only(args):
        return False
    if _raw_worker_args(args):
        return False
    agent = str(getattr(args, "agent", "") or "")
    shape = getattr(args, "shape", "bash")
    if shape == "acp":
        return False
    return agent in {"grok-code", "grok-research", "codex"}


def _same_worker_tree(record_cwd: object, target: Path) -> bool:
    try:
        candidate = os.path.realpath(str(record_cwd).strip())
    except (OSError, ValueError):
        return False
    return bool(candidate) and candidate == os.path.realpath(str(target))


def _occupancy_option_values(argv: list[str], flag: str) -> list[str]:
    """Every ``--flag`` / ``--flag=`` value in argv, including after ``--``.

    Occupancy has to see the path the worker received even when ``--cwd`` was
    recorded after the remainder split. Admission refuses a superset; missing
    a value is the bug.
    """
    prefix = flag + "="
    values: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == flag:
            if index + 1 < len(argv):
                nxt = argv[index + 1]
                if nxt and not nxt.startswith("-"):
                    values.append(nxt)
                    index += 2
                    continue
            index += 1
            continue
        if token.startswith(prefix):
            values.append(token[len(prefix) :])
        index += 1
    return values


def _occupancy_argv_from_record(record: dict) -> list[str]:
    """dispatch_argv from any of the shapes a queue/ledger row actually stores."""
    envelope = (
        record.get("request_envelope")
        if isinstance(record.get("request_envelope"), dict)
        else {}
    )
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    env_request = (
        envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    )
    for blob in (
        record.get("dispatch_argv"),
        envelope.get("dispatch_argv"),
        request.get("dispatch_argv"),
        env_request.get("dispatch_argv"),
    ):
        if isinstance(blob, list) and blob:
            return [str(part) for part in blob]
        if isinstance(blob, str) and blob.strip():
            try:
                return shlex.split(blob)
            except ValueError:
                return blob.split()
    return []


def _occupancy_cwd_raw_values(record: dict) -> list[str]:
    """Raw cwd strings from worker_cwd, dispatch_argv --cwd, and request.cwd.

    A row is nameless only when every source is absent or unusable. The three
    can disagree; occupancy occupies every path they resolve to.
    """
    values: list[str] = []
    seen: set[str] = set()

    def add(raw: object) -> None:
        if raw is None:
            return
        text = str(raw).strip()
        if not text or text in seen:
            return
        seen.add(text)
        values.append(text)

    add(record.get("worker_cwd"))
    for token in _occupancy_option_values(
        _occupancy_argv_from_record(record), "--cwd"
    ):
        add(token)
    envelope = (
        record.get("request_envelope")
        if isinstance(record.get("request_envelope"), dict)
        else {}
    )
    env_request = (
        envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    )
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    add(env_request.get("cwd"))
    add(request.get("cwd"))
    return values


def _resolve_occupancy_cwd(raw: str, project_root: object) -> Path | None:
    """Resolve a recorded cwd; relative strings join ``project_root`` first."""
    text = str(raw).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        root = (
            str(project_root).strip()
            if isinstance(project_root, str)
            else ""
        )
        if not root:
            return None
        path = Path(root).expanduser() / path
    try:
        return path.resolve(strict=False)
    except (OSError, ValueError):
        return None


def _occupancy_paths_from_record(record: dict) -> list[Path]:
    """Every cwd the record can name, for admission.

    Relative results are resolved against ``project_root``. Duplicate realpaths
    collapse. An empty list means the row is nameless.
    """
    root = record.get("project_root")
    found: list[Path] = []
    seen: set[str] = set()
    for raw in _occupancy_cwd_raw_values(record):
        resolved = _resolve_occupancy_cwd(raw, root)
        if resolved is None:
            continue
        try:
            key = os.path.realpath(str(resolved))
        except (OSError, ValueError):
            continue
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(resolved)
    return found


def _record_project_root_matches_target(record: dict, target: Path) -> bool:
    """True when the record's project_root is this tree's repo (not host-wide)."""
    raw = record.get("project_root")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        recorded = os.path.realpath(str(Path(raw.strip()).expanduser()))
        target_real = os.path.realpath(str(target))
    except (OSError, ValueError):
        return False
    if not recorded or not target_real:
        return False
    if recorded == target_real:
        return True
    try:
        rec_root = os.path.realpath(
            str(goalflight_task.resolve_project_root(raw))
        )
        tgt_root = os.path.realpath(
            str(goalflight_task.resolve_project_root(str(target)))
        )
    except (OSError, ValueError):
        return False
    return bool(rec_root) and rec_root == tgt_root


def _cwdless_nonterminal_warning(record: dict) -> str:
    """Operator-facing line for a readable non-terminal row that names no path.

    Surfaces on stderr of ``goalflight_dispatch`` (the launch operator, and
    drain which runs even when launched:0). Drain JSON ``attention`` carries
    the same dispatch id under ``cwdless_nonterminal`` for the controller
    reading the DRAIN line. Used on the skip path (dead / other-project
    nameless rows). A live nameless row that shares this project_root is
    occupancy UNKNOWN of the target, not a skip.
    """
    dispatch_id = str(record.get("dispatch_id") or record.get("path") or "unknown")
    state = str(record.get("state") or "running")
    return (
        f"goalflight_dispatch: WARNING — ledger dispatch {dispatch_id} "
        f"(state={state}) names no worker cwd; occupancy skip "
        f"(does not gate any worktree)"
    )


def _warn_cwdless_nonterminal(record: dict) -> None:
    print(_cwdless_nonterminal_warning(record), file=sys.stderr)


def _iter_cwdless_nonterminal_records(records, *, host: str | None = None):
    """Yield readable non-terminal rows occupancy would skip as nameless."""
    host = socket.gethostname() if host is None else host
    for record in records:
        if not isinstance(record, dict):
            continue
        if goalflight_ledger.record_is_unreadable(record):
            continue
        state = record.get("state")
        terminal = goalflight_ledger.terminal_state_for(
            state, record.get("reason") or record.get("error")
        )
        if terminal != "unknown":
            if not _is_live_watcher_stopped(state, record.get("worker_alive")):
                continue
        if record.get("transport") == "fleet-ssh":
            continue
        record_host = record.get("hostname")
        if isinstance(record_host, str) and record_host and record_host != host:
            continue
        if _occupancy_paths_from_record(record):
            continue
        yield record


def _worktree_incumbent_reason(args) -> tuple[str | None, str | None, str | None]:
    """(occupied_reason, unknown_reason, occupied_state) for this write tree.

    Occupancy is judged from the ledger's non-terminal set -- the lifecycle
    authority -- never from a process scan: a queued/starting dispatch owns its
    tree before any worker process exists for pgrep to see, and pid liveness
    cannot tell a searcher from a worker. ``occupied_state`` is the ledger
    state of a named occupant so a freshly acquired kernel lock can tell a
    queued owner (ledger-only claim) from a stale running row after SIGKILL.

    The predicate selects records that can be tied to THIS path. Cwd is
    taken from ``worker_cwd``, ``dispatch_argv --cwd`` (including after
    ``--``), and ``request.cwd``; a relative result is resolved against
    ``project_root``. A row is nameless only when all three are absent or
    unusable. Disagreeing sources that both resolve occupy every named
    path (refuse on a superset).

    A nameless non-terminal row is not occupancy of a specific tree, but
    this is a write-capable admission site: unknown must not be rendered
    as "does not occupy this path". Split on identity liveness (pid +
    start_token; never pgrep): proven-dead / no live identity skip+warn
    (reconcile already closes those); live identity on this host whose
    ``project_root`` matches the target tree's repo is occupancy UNKNOWN
    of this project (refuse; ``--occupied-worktree-forced`` is the hatch);
    missing ``project_root`` on a live nameless row is also UNKNOWN (cannot
    tell "other repo" from "this repo, unlabeled"); a *different* readable
    ``project_root`` skip+warn (cannot be this repo).
    ``project_root`` is never itself a path claim -- a matching
    ``worker_cwd`` still occupies when the root differs. Unreadable or
    unlistable records remain occupancy UNKNOWN: they might name this
    path, so the gate still refuses rather than reading as free.
    """
    target = _worker_cwd(args)
    try:
        records = goalflight_ledger.read_records()
    except OSError as exc:
        return None, f"dispatch ledger could not be read ({type(exc).__name__}: {exc})", None
    own_id = getattr(args, "dispatch_id", None)
    host = socket.gethostname()
    unknown: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        record_id = record.get("dispatch_id")
        if own_id and record_id == own_id:
            continue  # the drained/resumed launch re-enters with its own queued record
        if goalflight_ledger.record_is_unreadable(record):
            unknown.append(f"ledger record {record.get('path') or record_id} is unreadable")
            continue
        state = record.get("state")
        terminal = goalflight_ledger.terminal_state_for(
            state, record.get("reason") or record.get("error")
        )
        if terminal != "unknown":
            # Settled vocabulary (complete/failed/blocked/...) vacates the tree.
            # watcher_stopped is terminal in the ledger vocabulary, but a live
            # worker under that label is still writing -- reuse the resume
            # liveness reading instead of treating the tree as free.
            if not _is_live_watcher_stopped(state, record.get("worker_alive")):
                continue
        if record.get("transport") == "fleet-ssh":
            continue  # remote worker: its cwd is a path on another node's disk
        record_host = record.get("hostname")
        if isinstance(record_host, str) and record_host and record_host != host:
            continue  # recorded by a dispatch launched on another machine
        occupancy_paths = _occupancy_paths_from_record(record)
        if not occupancy_paths:
            # Nameless after every cwd source. Write admission must not
            # treat "cannot name the path" as "does not occupy this path".
            liveness, _liveness_reason = goalflight_ledger.worker_identity_liveness(
                record
            )
            if liveness != "live":
                _warn_cwdless_nonterminal(record)
                continue
            if _record_declared_read_only(record):
                _warn_cwdless_nonterminal(record)
                continue
            if not _record_project_root_matches_target(record, target):
                raw_root = record.get("project_root")
                if not isinstance(raw_root, str) or not raw_root.strip():
                    unknown.append(
                        f"live dispatch {record_id} (state={state or 'running'}) "
                        "names no worker cwd and no project_root"
                    )
                    continue
                _warn_cwdless_nonterminal(record)
                continue
            unknown.append(
                f"live dispatch {record_id} (state={state or 'running'}) "
                "names no worker cwd and shares this project_root"
            )
            continue
        if not any(_same_worker_tree(path, target) for path in occupancy_paths):
            continue
        if _record_declared_read_only(record):
            continue  # a reviewer shares the tree legitimately; it is not a writer
        return (
            f"worktree {target} is already owned by non-terminal dispatch "
            f"{record_id} (state={state or 'running'}); a second writer would "
            "share one filesystem tree with no merge discipline",
            None,
            str(state or "running"),
        )
    if unknown:
        detail = "; ".join(unknown[:3])
        if len(unknown) > 3:
            detail += f"; and {len(unknown) - 3} more"
        return None, detail, None
    return None, None, None


class _InheritedOccupancyLock:
    """A lock fd inherited from the parent launcher; this process must not close it."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def release(self) -> None:
        return None


def _bind_worktree_occupancy_lock(args, lock) -> None:
    """Keep the lock alive in this process and publish the fd for worker inherit."""
    args._worktree_occupancy_lock = lock
    fd = lock.fileno()
    os.set_inheritable(fd, True)
    os.environ[goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV] = str(fd)


def _release_worktree_occupancy_lock(args) -> None:
    """Drop this process's occupancy fd and the env name that pointed at it.

    Submit holds the kernel lock only while the submit process is writing the
    queue row. Leaving ``GOALFLIGHT_OCCUPANCY_LOCK_FD`` set after that fd is
    closed makes the next in-process launch treat occupancy as unknown.
    """
    lock = getattr(args, "_worktree_occupancy_lock", None)
    fd: int | None = None
    if lock is not None:
        with contextlib.suppress(Exception):
            fd = lock.fileno()
        with contextlib.suppress(Exception):
            lock.release()
        args._worktree_occupancy_lock = None
    raw = os.environ.get(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, "").strip()
    if raw and (fd is None or raw == str(fd)):
        os.environ.pop(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, None)


def _inherited_occupancy_lock():
    raw = os.environ.get(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, "").strip()
    if not raw:
        return None, None
    try:
        fd = int(raw)
        os.fstat(fd)
    except (ValueError, OSError) as exc:
        return None, (
            f"{goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV} does not name an "
            f"open descriptor: {raw!r} ({type(exc).__name__}: {exc})"
        )
    return _InheritedOccupancyLock(fd), None


def _occupancy_occupied_messages(reason: str) -> tuple[str, str]:
    refusal = "\n".join(
        [
            reason.rstrip(".") + ".",
            "Use --occupied-worktree-forced to override and launch a "
            "concurrent writer.",
        ]
    )
    forced_warning = (
        f"--occupied-worktree-forced accepted: {reason.rstrip('.')}; the two "
        "writers now share one filesystem tree with no merge discipline."
    )
    return refusal, forced_warning


def _occupancy_unknown_messages(args, detail: str) -> tuple[str, str]:
    target = _worker_cwd(args)
    refusal = "\n".join(
        [
            f"worktree occupancy of {target} is unknown ({detail}).",
            "Retry the dispatch; refusing before record or launch.",
            "Use --occupied-worktree-forced to override and launch without "
            "occupancy evidence.",
        ]
    )
    forced_warning = (
        f"--occupied-worktree-forced accepted: worktree occupancy of "
        f"{target} is unknown ({detail}); launching without occupancy evidence."
    )
    return refusal, forced_warning


def _finish_worktree_occupancy(args, *, refusal: str, forced_warning: str) -> str | None:
    if getattr(args, "occupied_worktree_forced", False):
        return forced_warning
    raise DispatchUsageError(refusal)


def _prepare_attempt_worktree_occupancy(args) -> str | None:
    """Refuse a second writer into an occupied worktree, or return the forced-path warning.

    Enforcement is an exclusive non-blocking kernel lock on the target
    worktree, inherited by the worker so the claim outlives this dispatcher
    and is released by the kernel on crash. The ledger is diagnostic: it
    names the incumbent and fail-closes on unlistable/unreadable records.
    A launch that actually cannot write (enforced read-only) is never
    refused here. --read-only on a write-capable ``--`` worker is not
    enough. --occupied-worktree-forced converts either refusal into a
    visible warning, matching the --unregistered-forced hatch.
    """
    if _occupancy_exempt_read_only(args):
        return None
    if getattr(args, "submit", False) and _requested_worktree_base(args):
        # A queued --worktree job does not have a seat yet. Occupying --cwd
        # (the project root) would serialize every pooled submit onto one
        # tree; drain binds the seat and then occupies that path.
        return None

    occupied, unknown, occupied_state = _worktree_incumbent_reason(args)
    lock = None
    lock_busy = None
    lock_unknown = None
    inherited, inherited_unknown = _inherited_occupancy_lock()
    if inherited_unknown:
        lock_unknown = inherited_unknown
    elif inherited is not None:
        lock = inherited
    else:
        target = _worker_cwd(args)
        dispatch_id = str(getattr(args, "dispatch_id", None) or "unknown-dispatch")
        try:
            lock = goalflight_worktree_pool.try_acquire_worktree_path_lock(
                target, dispatch_id
            )
        except goalflight_worktree_pool.WorktreePathLockBusy as exc:
            own = str(getattr(args, "dispatch_id", None) or "")
            if own and getattr(exc, "occupant_id", None) == own:
                # Same dispatch already occupies the tree (duplicate submit,
                # opportunistic drain). Not a second writer.
                return None
            lock_busy = str(exc)
        except goalflight_worktree_pool.WorktreePathLockUnknown as exc:
            lock_unknown = str(exc)
        except OSError as exc:
            lock_unknown = f"{type(exc).__name__}: {exc}"

    forced = bool(getattr(args, "occupied_worktree_forced", False))

    if lock is None:
        if lock_unknown is not None:
            detail = unknown or lock_unknown
            refusal, forced_warning = _occupancy_unknown_messages(args, detail)
            return _finish_worktree_occupancy(
                args, refusal=refusal, forced_warning=forced_warning
            )
        reason = occupied or lock_busy
        if reason is None:
            reason = (
                f"worktree {_worker_cwd(args)} is already owned by a live "
                "dispatch holding the worktree lock; a second writer would "
                "share one filesystem tree with no merge discipline"
            )
        refusal, forced_warning = _occupancy_occupied_messages(reason)
        return _finish_worktree_occupancy(
            args, refusal=refusal, forced_warning=forced_warning
        )

    # Kernel lock is held. Ledger unknown still fail-closes: an unlistable
    # ledger must not become a green light. A recorded occupant other than
    # this drain/resume id is the queued-owner case (no process holds a
    # lock). Drain of our own queued row skips own_id; a leftover sibling
    # queued row must not deadlock the holder of the live lock.
    if unknown is not None and occupied is None:
        if not forced:
            lock.release()
        refusal, forced_warning = _occupancy_unknown_messages(args, unknown)
        if forced:
            _bind_worktree_occupancy_lock(args, lock)
            return forced_warning
        return _finish_worktree_occupancy(
            args, refusal=refusal, forced_warning=forced_warning
        )
    if occupied is not None and not getattr(args, "from_queue", False):
        # A queued row occupies with no process. A running/starting row whose
        # worker already died (SIGKILL) leaves the kernel lock free; dropping
        # the lock we just won would recreate the dual-launch TOCTOU until
        # the watcher rewrites the ledger.
        if inherited is None and occupied_state in {"running", "starting"}:
            _bind_worktree_occupancy_lock(args, lock)
            return None
        if not forced:
            lock.release()
        refusal, forced_warning = _occupancy_occupied_messages(occupied)
        if forced:
            _bind_worktree_occupancy_lock(args, lock)
            return forced_warning
        return _finish_worktree_occupancy(
            args, refusal=refusal, forced_warning=forced_warning
        )

    _bind_worktree_occupancy_lock(args, lock)
    if occupied is not None and forced:
        refusal, forced_warning = _occupancy_occupied_messages(occupied)
        return forced_warning
    return None


def _write_windows_dispatch_refusal(args) -> tuple[dict, Path]:
    dispatch_id = args.dispatch_id or _default_dispatch_id(args.agent)
    args.dispatch_id = dispatch_id
    status_path = (
        Path(args.status_json)
        if args.status_json
        else _dispatch_base_dir() / f"{dispatch_id}.status.json"
    )
    payload = {
        "schema": "goalflight.status.v1",
        "dispatch_id": dispatch_id,
        "agent": args.agent,
        "shape": args.shape,
        "state": "blocked_windows_dispatch",
        "reason": goalflight_compat.windows_dispatch_refusal(),
        "next_step": "wsl --install; open an installed distro and run dispatch from the WSL checkout",
        "worker_pid": None,
        "worker_alive": False,
        "tail_path": str(Path(args.tail)) if args.tail else None,
        "status_path": str(status_path),
        "updated_at": int(time.time()),
    }
    write_status(status_path, payload)
    return payload, status_path


def _refuse_windows_dispatch(args) -> int:
    payload, status_path = _write_windows_dispatch_refusal(args)
    print(
        "DISPATCH-BLOCKED "
        + json.dumps(
            {
                "dispatch_id": payload["dispatch_id"],
                "agent": payload["agent"],
                "shape": payload["shape"],
                "state": payload["state"],
                "status_json": str(status_path),
                "reason": payload["reason"],
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
    return 2


def _find_dispatch_record(dispatch_id: str) -> dict | None:
    """Look up one ledger row. Never scans the whole runs dir.

    Recovery used to call ``read_records()`` here. A 2500-row ledger times
    ~16 lookups per carrier was the 10s/carrier drain cost.
    """
    t0 = time.monotonic()
    try:
        if not dispatch_id:
            return None
        try:
            return goalflight_ledger.read_record(dispatch_id)
        except OSError:
            # Unlistable ledger is occupancy UNKNOWN, not "this id is free".
            return None
    finally:
        _pass_time("ledger_lookup_s", time.monotonic() - t0, count_key="ledger_lookup_n")


def _worker_liveness_warning(record: dict) -> str | None:
    dispatch_id = record.get("dispatch_id") or "unknown"
    pid = record.get("worker_pid")
    if not pid:
        return f"WARN: dispatch {dispatch_id} has no worker pid; message appended but may not be observed"
    try:
        current = goalflight_ledger.process_identity(int(pid))
    except (TypeError, ValueError, OSError) as exc:
        return f"WARN: dispatch {dispatch_id} worker identity check failed: {exc}"
    if current is None:
        return f"WARN: dispatch {dispatch_id} worker pid {pid} is not alive; message appended but may not be observed"
    prior = record.get("worker_identity") or {}
    if goalflight_compat.is_windows() and not current.get("identity_available", True):
        return f"WARN: dispatch {dispatch_id} worker identity indeterminate; message appended"
    matched, reason = goalflight_ledger.compare_process_identities(
        int(pid), prior, current
    )
    if not matched:
        return (
            f"WARN: dispatch {dispatch_id} worker pid {pid} identity mismatch "
            f"({reason}); message appended but may target stale state"
        )
    return None


def _parse_steer_lines(lines: list[str]) -> list[dict]:
    return goalflight_steer_mailbox.parse_steer_lines(lines)


def _read_steer_entries(path: Path) -> list[dict]:
    return goalflight_steer_mailbox.read_steer_entries(path)


def _append_steer_message(
    dispatch_id: str,
    text: str,
    *,
    reply_to: str | None = None,
    decision: str | None = None,
) -> dict:
    import goalflight_messages

    return goalflight_messages.post_controller_steer(
        dispatch_id,
        text,
        reply_to=reply_to,
        decision=decision,
    )


def _report_steer_result(dispatch_id: str, result: dict) -> int:
    delivery = result["delivery"]
    if delivery["delivered"]:
        print(
            f"steer appended: dispatch_id={dispatch_id} seq={delivery['steer_seq']} "
            f"mailbox={delivery['steer_path']}"
        )
        return 0
    non_error = delivery["status"] in {"terminal_recorded_only", "worker_view_queued"}
    stream = sys.stdout if non_error else sys.stderr
    print(f"steer recorded: dispatch_id={dispatch_id}; {delivery['detail']}", file=stream)
    return 0 if non_error else 1


def _acked_steer_seqs(record: dict) -> set[int]:
    return goalflight_steer_mailbox.acked_steer_seqs(record)


def _consumed_worker_wait_receipts(dispatch_id: str, record: dict) -> set[tuple[str, int]]:
    marker_entries: list[dict] = []
    stdout_value = record.get("stdout_path")
    if stdout_value:
        try:
            marker_entries, _size = extract_markers(
                Path(str(stdout_value)),
                ignore_prefix_lines=_ignore_prefix_lines(record.get("prompt_path")),
            )
        except OSError:
            marker_entries = []
    return goalflight_steer_mailbox.consumed_worker_wait_receipts(
        record,
        marker_entries=marker_entries,
        mailbox_path=_steer_file(dispatch_id),
    )


def _list_steer_messages(dispatch_id: str, record: dict) -> int:
    return goalflight_steer_mailbox.list_steer_messages(dispatch_id, record)


def _worker_wait_mailbox(dispatch_id: str) -> Path:
    worker_dispatch_id = str(os.environ.get("GOALFLIGHT_DISPATCH_ID") or "").strip()
    if worker_dispatch_id != dispatch_id:
        raise DispatchUsageError(
            "steer --wait is worker-only and requires GOALFLIGHT_DISPATCH_ID "
            f"to equal {dispatch_id!r}"
        )
    configured = str(os.environ.get("GOALFLIGHT_STEER_FILE") or "").strip()
    if not configured:
        raise DispatchUsageError("steer --wait requires GOALFLIGHT_STEER_FILE")
    mailbox = Path(configured).expanduser().resolve(strict=False)
    expected = _steer_file(dispatch_id).expanduser().resolve(strict=False)
    if mailbox != expected:
        raise DispatchUsageError(
            f"GOALFLIGHT_STEER_FILE does not match dispatch {dispatch_id}: {mailbox}"
        )
    return mailbox


def _wait_event_reporter(
    dispatch_id: str,
):
    def report(event: dict) -> None:
        state = event.get("state")
        if state == "armed":
            print(
                f"!{event['question_kind']}: {event['question_marker_text']}",
                flush=True,
            )
            print(
                "STEER-WAIT: "
                f"dispatch_id={dispatch_id} armed timeout_secs={event['timeout_secs']:g}",
                flush=True,
            )
            return
        if state == "messages":
            for entry in event.get("entries") or []:
                for line in goalflight_steer_mailbox.worker_wait_reply_output_lines(
                    entry
                ):
                    print(line, flush=True)
            return
        if state == "deadline":
            print(f"STEER-WAIT: dispatch_id={dispatch_id} deadline reached", flush=True)

    return report


def _cmd_steer(argv: list[str]) -> int:
    parser = _TerseArgumentParser(
        prog=f"{Path(sys.argv[0]).name} steer",
        description="Append, list, or wait on mailbox steers for an existing dispatch.",
        usage_hint=(
            "try steer <dispatch-id> <message> | steer <dispatch-id> --list | "
            "steer <dispatch-id> --wait [--timeout-secs N]"
        ),
    )
    parser.add_argument("dispatch_id")
    parser.add_argument("message", nargs="?")
    parser.add_argument("--list", action="store_true", dest="list_messages")
    parser.add_argument("--wait", action="store_true", dest="wait_for_message")
    parser.add_argument(
        "--timeout-secs",
        type=float,
        default=goalflight_steer_mailbox.DEFAULT_WORKER_WAIT_TIMEOUT_SECS,
    )
    parser.add_argument(
        "--poll-secs",
        type=float,
        default=goalflight_steer_mailbox.DEFAULT_WORKER_WAIT_POLL_SECS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--question-kind", choices=("USER-NEED", "USER-CONFIRM"))
    parser.add_argument(
        "--reply-to",
        help="Correlate this typed controller reply to an active worker wait id.",
    )
    parser.add_argument(
        "--decision",
        choices=("yes", "no"),
        help="Explicit USER-CONFIRM decision; requires --reply-to.",
    )
    args = parser.parse_args(argv)

    record = _find_dispatch_record(args.dispatch_id)
    if record is None:
        print(f"goalflight_dispatch: no ledger record for dispatch {args.dispatch_id}", file=sys.stderr)
        return 64

    if args.list_messages and args.wait_for_message:
        print("goalflight_dispatch: steer --list and --wait are mutually exclusive", file=sys.stderr)
        return 64
    if args.list_messages and (args.reply_to or args.decision):
        print("goalflight_dispatch: steer --list cannot carry reply fields", file=sys.stderr)
        return 64
    if args.list_messages:
        return _list_steer_messages(args.dispatch_id, record)
    if args.wait_for_message:
        if args.reply_to or args.decision:
            print(
                "goalflight_dispatch: steer --wait cannot carry controller reply fields",
                file=sys.stderr,
            )
            return 64
        if not args.question_kind or not str(args.message or "").strip():
            print(
                "goalflight_dispatch: steer --wait requires question text and "
                "--question-kind",
                file=sys.stderr,
            )
            return 64
        try:
            result = goalflight_steer_mailbox.wait_for_worker_entries(
                _worker_wait_mailbox(args.dispatch_id),
                dispatch_id=args.dispatch_id,
                acked_seqs=_acked_steer_seqs(record),
                consumed_reply_receipts=_consumed_worker_wait_receipts(args.dispatch_id, record),
                question_kind=args.question_kind,
                question_text=args.message,
                timeout_secs=args.timeout_secs,
                poll_secs=args.poll_secs,
                notify=_wait_event_reporter(args.dispatch_id),
            )
        except (OSError, ValueError, DispatchUsageError) as exc:
            print(f"goalflight_dispatch: steer --wait failed: {exc}", file=sys.stderr)
            return 64 if isinstance(exc, (ValueError, DispatchUsageError)) else 1
        except Exception as exc:
            if not goalflight_steer_mailbox.is_carrier_error(exc):
                raise
            print(f"goalflight_dispatch: steer --wait failed: {exc}", file=sys.stderr)
            return 1
        return 0 if result["state"] == "messages" else 1
    if args.message is None:
        print("goalflight_dispatch: steer requires a message, --list, or --wait", file=sys.stderr)
        return 64
    if args.question_kind:
        print("goalflight_dispatch: --question-kind requires --wait", file=sys.stderr)
        return 64
    if args.decision and not args.reply_to:
        print("goalflight_dispatch: --decision requires --reply-to", file=sys.stderr)
        return 64

    shape = goalflight_ledger.infer_shape(record)
    if shape == "acp":
        warning = _worker_liveness_warning(record)
        if warning:
            print(warning, file=sys.stderr)
        return _report_steer_result(
            args.dispatch_id,
            _append_steer_message(
                args.dispatch_id,
                args.message,
                reply_to=args.reply_to,
                decision=args.decision,
            ),
        )
    if shape != "bash":
        print(f"goalflight_dispatch: dispatch {args.dispatch_id} has unsupported shape {shape!r}", file=sys.stderr)
        return 64

    warning = _worker_liveness_warning(record)
    if warning:
        print(warning, file=sys.stderr)
    return _report_steer_result(
        args.dispatch_id,
        _append_steer_message(
            args.dispatch_id,
            args.message,
            reply_to=args.reply_to,
            decision=args.decision,
        ),
    )


def _codex_dispatch_homes_dir() -> Path:
    state_dir = Path(
        os.environ.get("GOALFLIGHT_CODEX_STATE_DIR", "~/.goal-flight")
    ).expanduser()
    return state_dir.resolve(strict=False) / "dispatch-homes"


def _codex_resume_home(record: dict, dispatch_id: str) -> tuple[Path, str]:
    root = _codex_dispatch_homes_dir()
    home_owner_dispatch_id = record.get("codex_home_owner_dispatch_id") or dispatch_id
    if not isinstance(home_owner_dispatch_id, str) or not home_owner_dispatch_id:
        raise DispatchUsageError(
            f"dispatch {dispatch_id} has invalid recorded codex home owner"
        )
    recorded = record.get("codex_home")
    home = (
        Path(recorded).expanduser()
        if isinstance(recorded, str) and recorded
        else root / home_owner_dispatch_id
    )
    resolved = home.resolve(strict=False)
    if (
        resolved.parent != root
        or resolved.name != home_owner_dispatch_id
    ):
        raise DispatchUsageError(
            f"dispatch {dispatch_id} has invalid recorded codex home: {home}"
        )
    return resolved, home_owner_dispatch_id


def _codex_resume_status(record: dict, dispatch_id: str) -> dict | None:
    status_path = record.get("status_path")
    if not isinstance(status_path, str) or not status_path:
        return None
    try:
        status = json.loads(
            Path(status_path).expanduser().read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(status, dict) or status.get("dispatch_id") != dispatch_id:
        return None
    return status


def _assert_codex_resume_source_not_live(record: dict, dispatch_id: str) -> None:
    status = _codex_resume_status(record, dispatch_id) or {}
    record_pid = record.get("worker_pid")
    status_pid = status.get("worker_pid")
    worker_pid = record_pid or status_pid
    worker_identity = record.get("worker_identity") if record_pid else None
    if (
        not isinstance(worker_identity, dict)
        and status_pid
        and str(status_pid) == str(worker_pid)
    ):
        worker_identity = status.get("expected_worker_identity")
        if not isinstance(worker_identity, dict):
            worker_identity = status.get("worker_identity")
    identity_state, _identity_reason = _queue_claim_identity_status(
        worker_pid,
        worker_identity,
    )
    if identity_state == "live":
        raise DispatchUsageError(
            f"dispatch {dispatch_id} is still live; wait for terminal before resume"
        )
    if identity_state == "indeterminate":
        raise DispatchUsageError(
            f"dispatch {dispatch_id} liveness is indeterminate; refusing resume"
        )
    if identity_state == "dead":
        return

    source_state = status.get("state") or record.get("state")
    source_reason = status.get("reason") or record.get("reason")
    if status.get("worker_alive") is True or (
        goalflight_ledger.terminal_state_for(source_state, source_reason) == "unknown"
    ):
        raise DispatchUsageError(
            f"dispatch {dispatch_id} liveness is indeterminate; refusing resume"
        )


def _assert_no_nonterminal_codex_resume_child(
    *,
    dispatch_id: str,
    home: Path,
    session_id: str,
    exclude_dispatch_id: str | None = None,
    reconcile_dead_preclaim: bool = False,
) -> None:
    for candidate in goalflight_ledger.read_records():
        candidate_id = candidate.get("dispatch_id")
        if (
            candidate_id in {dispatch_id, exclude_dispatch_id}
            or not candidate.get("parent_dispatch_id")
        ):
            continue
        if (
            goalflight_engine_sessions.resume_engine(
                candidate.get("engine") or candidate.get("agent")
            )
            != "codex"
        ):
            continue
        if (
            goalflight_ledger.terminal_state_for(
                candidate.get("state"),
                candidate.get("reason") or candidate.get("error"),
            )
            != "unknown"
        ):
            continue
        candidate_session_id = goalflight_codex_sessions.valid_session_id(
            candidate.get("codex_session_id")
        )
        candidate_home = candidate.get("codex_home")
        if (
            candidate_session_id == session_id
            and isinstance(candidate_home, str)
            and Path(candidate_home).expanduser().resolve(strict=False) == home
        ):
            identity_matches, identity_reason = goalflight_ledger.identity_matches(
                candidate
            )
            confirmed_dead_preclaim = (
                candidate.get("state") == "waiting_capacity"
                and not candidate.get("worker_pid")
                and not identity_matches
                and (
                    identity_reason == "dead"
                    or str(identity_reason).startswith("pid_reused_")
                )
                and isinstance(candidate_id, str)
                and bool(candidate_id)
            )
            if confirmed_dead_preclaim:
                if reconcile_dead_preclaim:
                    _finish_ledger(
                        candidate_id,
                        "failed",
                        f"stale_codex_resume_preclaim:{identity_reason}",
                        worker_still_alive=False,
                    )
                continue
            raise DispatchUsageError(
                f"dispatch {dispatch_id} already has non-terminal resume child "
                f"{candidate_id} for session {session_id}"
            )


def _recorded_codex_session_id(record: dict, dispatch_id: str) -> str | None:
    candidates = [record.get("codex_session_id")]
    status = _codex_resume_status(record, dispatch_id)
    if status is not None:
        candidates.append(status.get("codex_session_id"))
    recorded: set[str] = set()
    for candidate in candidates:
        session_id = goalflight_codex_sessions.valid_session_id(candidate)
        if session_id is not None:
            recorded.add(session_id)
    if len(recorded) > 1:
        raise DispatchUsageError(
            f"dispatch {dispatch_id} has conflicting recorded codex session handles"
        )
    return next(iter(recorded), None)


def _recorded_engine_session_id(record: dict, dispatch_id: str) -> str | None:
    engine = goalflight_engine_sessions.resume_engine(
        record.get("engine") or record.get("agent")
    )
    if engine == "codex":
        return _recorded_codex_session_id(record, dispatch_id)
    candidates = [record.get("engine_session_id"), record.get("codex_session_id")]
    status = _codex_resume_status(record, dispatch_id)
    if status is not None:
        candidates.append(status.get("engine_session_id"))
        candidates.append(status.get("codex_session_id"))
    recorded: set[str] = set()
    for candidate in candidates:
        session_id = goalflight_engine_sessions.valid_session_id(engine, candidate)
        if session_id is not None:
            recorded.add(session_id)
    if len(recorded) > 1:
        raise DispatchUsageError(
            f"dispatch {dispatch_id} has conflicting recorded {engine} session handles"
        )
    return next(iter(recorded), None)


def _assert_no_nonterminal_resume_child(
    *,
    dispatch_id: str,
    engine: str,
    session_id: str,
    exclude_dispatch_id: str | None = None,
    reconcile_dead_preclaim: bool = False,
) -> None:
    """Refuse a second live launch against the same engine session."""
    if engine == "codex":
        return
    for candidate in goalflight_ledger.read_records():
        candidate_id = candidate.get("dispatch_id")
        if (
            candidate_id in {dispatch_id, exclude_dispatch_id}
            or not candidate.get("parent_dispatch_id")
        ):
            continue
        if (
            goalflight_engine_sessions.resume_engine(
                candidate.get("engine") or candidate.get("agent")
            )
            != engine
        ):
            continue
        if (
            goalflight_ledger.terminal_state_for(
                candidate.get("state"),
                candidate.get("reason") or candidate.get("error"),
            )
            != "unknown"
        ):
            continue
        candidate_session_id = goalflight_engine_sessions.valid_session_id(
            engine,
            candidate.get("engine_session_id") or candidate.get("codex_session_id"),
        )
        if candidate_session_id != session_id:
            continue
        identity_matches, identity_reason = goalflight_ledger.identity_matches(
            candidate
        )
        confirmed_dead_preclaim = (
            candidate.get("state") == "waiting_capacity"
            and not candidate.get("worker_pid")
            and not identity_matches
            and (
                identity_reason == "dead"
                or str(identity_reason).startswith("pid_reused_")
            )
            and isinstance(candidate_id, str)
            and bool(candidate_id)
        )
        if confirmed_dead_preclaim:
            if reconcile_dead_preclaim:
                _finish_ledger(
                    candidate_id,
                    "failed",
                    f"stale_resume_preclaim:{identity_reason}",
                    worker_still_alive=False,
                )
            continue
        raise DispatchUsageError(
            f"dispatch {dispatch_id} already has non-terminal resume child "
            f"{candidate_id} for session {session_id}"
        )


def _validate_resume_source(
    dispatch_id: str,
    *,
    exclude_dispatch_id: str | None = None,
    reconcile_dead_preclaim: bool = False,
) -> dict:
    """Validate a resume source for any wired worker CLI.

    Codex still needs its per-dispatch home and rollout. Other engines need a
    recorded native handle. Pre-capture dispatches refuse instead of starting
    a silent fresh session.
    """
    record = _find_dispatch_record(dispatch_id)
    if record is None:
        raise DispatchUsageError(f"no ledger record for dispatch {dispatch_id}")
    engine = goalflight_engine_sessions.resume_engine(
        record.get("engine") or record.get("agent")
    )
    if engine is None:
        label = record.get("engine") or record.get("agent") or "unknown"
        raise DispatchUsageError(
            f"dispatch {dispatch_id} engine {label!r} is not a resumable worker CLI"
        )
    _assert_codex_resume_source_not_live(record, dispatch_id)
    if engine == "codex":
        _record, home, session_id, home_owner_dispatch_id = (
            _validate_codex_resume_source(
                dispatch_id,
                exclude_dispatch_id=exclude_dispatch_id,
                reconcile_dead_preclaim=reconcile_dead_preclaim,
            )
        )
        return {
            "record": _record,
            "engine": engine,
            "agent": str(_record.get("agent") or "codex"),
            "shape": goalflight_ledger.infer_shape(_record),
            "session_id": session_id,
            "codex_home": home,
            "codex_home_owner_dispatch_id": home_owner_dispatch_id,
        }
    session_id = _recorded_engine_session_id(record, dispatch_id)
    if session_id is None:
        raise DispatchUsageError(
            f"dispatch {dispatch_id} has no recorded {engine} session handle"
        )
    _assert_no_nonterminal_resume_child(
        dispatch_id=dispatch_id,
        engine=engine,
        session_id=session_id,
        exclude_dispatch_id=exclude_dispatch_id,
        reconcile_dead_preclaim=reconcile_dead_preclaim,
    )
    return {
        "record": record,
        "engine": engine,
        "agent": str(record.get("agent") or engine),
        "shape": goalflight_ledger.infer_shape(record),
        "session_id": session_id,
        "codex_home": None,
        "codex_home_owner_dispatch_id": None,
    }


def _validate_codex_resume_source(
    dispatch_id: str,
    *,
    exclude_dispatch_id: str | None = None,
    reconcile_dead_preclaim: bool = False,
) -> tuple[dict, Path, str, str]:
    record = _find_dispatch_record(dispatch_id)
    if record is None:
        raise DispatchUsageError(f"no ledger record for dispatch {dispatch_id}")
    if goalflight_ledger.infer_engine(record.get("engine") or record.get("agent")) != "codex":
        raise DispatchUsageError(f"dispatch {dispatch_id} is not a codex dispatch")
    _assert_codex_resume_source_not_live(record, dispatch_id)
    session_id = _recorded_codex_session_id(record, dispatch_id)
    if session_id is None:
        raise DispatchUsageError(
            f"dispatch {dispatch_id} has no recorded codex session handle"
        )
    home, home_owner_dispatch_id = _codex_resume_home(record, dispatch_id)
    if not home.is_dir():
        raise DispatchUsageError(
            f"dispatch home missing for {dispatch_id}: {home}"
        )
    if goalflight_codex_sessions.rollout_path(home, session_id) is None:
        raise DispatchUsageError(
            f"rollout missing for dispatch {dispatch_id}: session {session_id} "
            f"under {home / 'sessions'}"
        )
    _assert_no_nonterminal_codex_resume_child(
        dispatch_id=dispatch_id,
        home=home,
        session_id=session_id,
        exclude_dispatch_id=exclude_dispatch_id,
        reconcile_dead_preclaim=reconcile_dead_preclaim,
    )
    return record, home, session_id, home_owner_dispatch_id


_CODEX_RESUME_CLAIM_VALIDATED_HOOK = None
_CODEX_RESUME_DURABLE_CLAIM_HOOK = None
_CODEX_RESUME_BEFORE_REPLACE_HOOK = None


def _codex_resume_lock_path(home: Path, session_id: str) -> Path:
    key = hashlib.sha256(
        f"{home.resolve(strict=False)}\0{session_id}".encode("utf-8")
    ).hexdigest()
    return home.parent / ".resume-locks" / f"{key}.lock"


@contextlib.contextmanager
def _codex_resume_lock(home: Path, session_id: str):
    """Serialize claims and home rebuilds for one recorded Codex rollout."""
    if fcntl is None:  # pragma: no cover - resume is refused on native Windows
        raise DispatchUsageError("codex resume requires local file locking")
    lock_path = _codex_resume_lock_path(home, session_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _engine_resume_lock_path(engine: str, session_id: str) -> Path:
    key = hashlib.sha256(f"{engine}\0{session_id}".encode("utf-8")).hexdigest()
    return _codex_dispatch_homes_dir() / ".resume-locks" / f"{key}.lock"


@contextlib.contextmanager
def _engine_resume_lock(engine: str, session_id: str):
    """Serialize claims for one recorded non-Codex engine session."""
    if fcntl is None:  # pragma: no cover - resume is refused on native Windows
        raise DispatchUsageError("resume requires local file locking")
    lock_path = _engine_resume_lock_path(engine, session_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _revalidate_resume_claim(
    *,
    parent_dispatch_id: str,
    expected_engine: str,
    expected_session_id: str,
    exclude_dispatch_id: str | None = None,
) -> None:
    source = _validate_resume_source(
        parent_dispatch_id,
        exclude_dispatch_id=exclude_dispatch_id,
        reconcile_dead_preclaim=True,
    )
    if (
        source["engine"] != expected_engine
        or source["session_id"] != expected_session_id
    ):
        raise DispatchUsageError(
            f"dispatch {parent_dispatch_id} resume lineage changed while claiming"
        )
    _assert_no_nonterminal_resume_child(
        dispatch_id=parent_dispatch_id,
        engine=expected_engine,
        session_id=expected_session_id,
        exclude_dispatch_id=exclude_dispatch_id,
        reconcile_dead_preclaim=True,
    )


def _revalidate_codex_resume_claim(
    *,
    parent_dispatch_id: str,
    expected_home: Path,
    expected_session_id: str,
    expected_home_owner_dispatch_id: str,
    exclude_dispatch_id: str | None = None,
) -> None:
    """Recheck lineage and exclusivity while the owner-home lock is held."""
    _record, home, session_id, home_owner_dispatch_id = (
        _validate_codex_resume_source(
            parent_dispatch_id,
            exclude_dispatch_id=exclude_dispatch_id,
            reconcile_dead_preclaim=True,
        )
    )
    if (
        home != expected_home
        or session_id != expected_session_id
        or home_owner_dispatch_id != expected_home_owner_dispatch_id
    ):
        raise DispatchUsageError(
            f"dispatch {parent_dispatch_id} codex resume lineage changed while claiming"
        )


def _restore_codex_resume_home(
    original_home: Path,
    saved_home: Path,
    *,
    failed_home_id: str,
) -> None:
    failed_home = saved_home.parent / failed_home_id
    if original_home.exists():
        original_home.replace(failed_home)
    saved_home.replace(original_home)
    cleanup_codex_dispatch_home(failed_home_id)


def _rebuild_codex_resume_home(
    project_root: Path,
    parent_dispatch_id: str,
    expected_home: Path,
    session_id: str,
    *,
    home_owner_dispatch_id: str | None = None,
) -> tuple[str, str]:
    """Refresh auth/config in the original home while preserving its rollout."""
    if not expected_home.is_dir():
        raise DispatchUsageError(
            f"dispatch home missing for {parent_dispatch_id}: {expected_home}"
        )
    if goalflight_codex_sessions.rollout_path(expected_home, session_id) is None:
        raise DispatchUsageError(
            f"rollout missing for dispatch {parent_dispatch_id}: session "
            f"{session_id} under {expected_home / 'sessions'}"
        )

    root = expected_home.parent
    saved_home_id = f"resume-save-{uuid.uuid4().hex}"
    failed_home_id = f"resume-failed-{uuid.uuid4().hex}"
    saved_home = root / saved_home_id
    if _CODEX_RESUME_BEFORE_REPLACE_HOOK is not None:
        _CODEX_RESUME_BEFORE_REPLACE_HOOK(
            expected_home,
            session_id,
            parent_dispatch_id,
        )
    expected_home.replace(saved_home)
    try:
        rebuilt_home, effective_account = resolve_codex_home(
            project_root,
            None,
            home_owner_dispatch_id or parent_dispatch_id,
        )
        if (
            rebuilt_home is None
            or effective_account is None
            or Path(rebuilt_home).resolve(strict=False) != expected_home
        ):
            raise DispatchUsageError(
                f"could not rebuild dispatch home for {parent_dispatch_id} "
                "with a healthy codex seat"
            )
        saved_sessions = saved_home / "sessions"
        rebuilt_sessions = expected_home / "sessions"
        if not saved_sessions.is_dir() or rebuilt_sessions.exists():
            raise DispatchUsageError(
                f"could not preserve rollout while rebuilding dispatch home "
                f"for {parent_dispatch_id}"
            )
        saved_sessions.replace(rebuilt_sessions)
    except BaseException:
        _restore_codex_resume_home(
            expected_home,
            saved_home,
            failed_home_id=failed_home_id,
        )
        raise
    cleanup_codex_dispatch_home(saved_home_id)
    return str(expected_home), effective_account


def _cmd_resume(argv: list[str]) -> int:
    parser = _TerseArgumentParser(
        prog=f"{Path(sys.argv[0]).name} resume",
        description="Resume a recorded worker-CLI session as a tracked dispatch.",
        usage_hint="try resume <dispatch-id> --prompt-file <path>",
    )
    parser.add_argument("dispatch_id")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument(
        "--controller-label",
        help="Controller label used to select a kernel-live project lease.",
    )
    parser.add_argument(
        "--controller-pid",
        type=int,
        help="Controller pid used to select a kernel-live project lease.",
    )
    parser.add_argument(
        "--controller-session-id",
        help="Controller lease nonce used to verify dispatch ownership.",
    )
    parser.add_argument(
        "--unregistered-forced",
        action="store_true",
        help="Resume without a registered controller and keep NULL ownership.",
    )
    parser.add_argument(
        "--cwd",
        help=(
            "Worker tree for this resume. Required when the parent record "
            "has no worker_cwd and no --cwd in dispatch_argv; the shared "
            "checkout is not a fallback."
        ),
    )
    args = parser.parse_args(argv)

    prompt_path = Path(args.prompt_file).expanduser()
    if not prompt_path.is_file():
        print(
            f"goalflight_dispatch: prompt file not found: {args.prompt_file}",
            file=sys.stderr,
        )
        return 64
    try:
        source = _validate_resume_source(args.dispatch_id)
        _resume_worker_cwd(
            source["record"], override=getattr(args, "cwd", None)
        )
        child_dispatch_id = _reserve_auto_dispatch_id(
            f"{source['engine']}-resume", _dispatch_base_dir()
        )
        launch_argv = _resume_launch_argv(
            source,
            child_dispatch_id=child_dispatch_id,
            prompt_path=prompt_path,
            resume_args=args,
        )
    except DispatchUsageError as exc:
        print(f"goalflight_dispatch: {exc}", file=sys.stderr)
        return 64
    return main(launch_argv)


def _dispatch_argv_from_record(record: dict) -> list[str]:
    envelope = (
        record.get("request_envelope")
        if isinstance(record.get("request_envelope"), dict)
        else {}
    )
    for blob in (record.get("dispatch_argv"), envelope.get("dispatch_argv")):
        if isinstance(blob, list) and blob:
            return [str(part) for part in blob]
    return []


def _resume_cwd_from_record(record: dict) -> Path | None:
    envelope = (
        record.get("request_envelope")
        if isinstance(record.get("request_envelope"), dict)
        else {}
    )
    env_request = (
        envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    )
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    recorded = _dispatch_argv_from_record(record)
    from_argv = (
        _option_value_before_worker_remainder(recorded, "--cwd") if recorded else None
    )
    for raw in (
        record.get("worker_cwd"),
        from_argv,
        env_request.get("cwd"),
        request.get("cwd"),
    ):
        if raw:
            return Path(str(raw)).expanduser().resolve(strict=False)
    return None


def _resume_worker_cwd(record: dict, *, override: str | None = None) -> Path:
    if override:
        return Path(str(override)).expanduser().resolve(strict=False)
    path = _resume_cwd_from_record(record)
    if path is not None:
        return path
    raise DispatchUsageError(
        "resume refused: record has no worker cwd evidence "
        "(missing worker_cwd and --cwd in dispatch_argv). "
        "Pass --cwd <path> to resume at an explicit tree "
        "(informed consent; the shared checkout is not a fallback)."
    )


def _synthesize_resume_base_argv(source: dict, *, cwd: Path) -> list[str]:
    """Fallback when an older record never stored dispatch_argv."""
    record = source["record"]
    shape = source["shape"] if source["shape"] in {"bash", "acp"} else "bash"
    argv = [
        "--agent",
        source["agent"],
        "--shape",
        shape,
        "--cwd",
        str(cwd),
    ]
    posture = record.get("os_sandbox")
    if isinstance(posture, dict):
        requested = posture.get("requested_profile")
        if isinstance(requested, str) and requested:
            argv += ["--os-sandbox", requested]
    task_ids = record.get("task_ids")
    if isinstance(task_ids, list):
        for task_id in task_ids:
            if isinstance(task_id, str) and task_id:
                argv.extend(["--task", task_id])
    return argv


def _resume_launch_argv(
    source: dict,
    *,
    child_dispatch_id: str,
    prompt_path: Path,
    resume_args,
) -> list[str]:
    record = source["record"]
    recorded = _dispatch_argv_from_record(record)
    cwd = _resume_worker_cwd(
        record, override=getattr(resume_args, "cwd", None)
    )
    base = recorded or _synthesize_resume_base_argv(source, cwd=cwd)
    replace = {
        "--dispatch-id": child_dispatch_id,
        "--prompt-file": str(prompt_path),
        "--parent-dispatch-id": str(resume_args.dispatch_id),
        "--engine-session-id": source["session_id"],
        "--cwd": str(cwd),
    }
    inject: list[str] = []
    if resume_args.unregistered_forced:
        inject.append("--unregistered-forced")
    for flag, value in (
        ("--controller-label", resume_args.controller_label),
        ("--controller-pid", resume_args.controller_pid),
        ("--controller-session-id", resume_args.controller_session_id),
    ):
        if value is not None:
            replace[flag] = str(value)
    if source["engine"] == "codex":
        replace["--codex-session-id"] = source["session_id"]
        replace["--codex-resume-home"] = str(source["codex_home"])
        replace["--codex-home-owner-dispatch-id"] = source[
            "codex_home_owner_dispatch_id"
        ]
    else:
        # Stay on the seat that owns the session files. Codex rebuilds a
        # per-dispatch home; grok/cursor/claude sessions live in the seat HOME.
        account = record.get("effective_account") or record.get("account")
        if isinstance(account, str) and account and account != "default":
            replace["--account"] = account
    argv = _reconstruct_launch_argv(
        base,
        replace=replace,
        inject=inject,
        strip_flags=_replay_strip_flags() + ("--unregistered-forced", "--occupied-worktree-forced"),
        strip_options=("--tail", "--status-json", "--prompt"),
    )
    if source["engine"] == "codex":
        argv = _remove_option_before_worker_remainder(argv, "--account")
    return argv


def _mark_reconciled_parent_resumed(
    parent_dispatch_id: str,
    child_dispatch_id: str,
) -> bool:
    """Replace an inferred-abandonment label with observed resume lineage.

    The old attempt remains terminal; the new child owns the live process.
    ``superseded`` states only the observed fact that a fresh tracked child has
    replaced that attempt, while the nested reconciliation record preserves
    why the old attempt was originally closed.
    """

    with goalflight_ledger.StateLock():
        parent = _find_dispatch_record(parent_dispatch_id)
        child = _find_dispatch_record(child_dispatch_id)
        if not isinstance(parent, dict) or not isinstance(child, dict):
            return False
        outcome = parent.get("outcome")
        reconciliation = outcome.get("reconciliation") if isinstance(outcome, dict) else None
        if not isinstance(reconciliation, dict) or reconciliation.get("basis") != "inferred_abandonment":
            return False
        if child.get("parent_dispatch_id") != parent_dispatch_id:
            return False
        if not goalflight_dispatch_states.is_running_state(child.get("state")):
            return False

        resumed_at = child.get("started_at") or goalflight_ledger.utc_now()
        resume_observation = {
            "dispatch_id": child_dispatch_id,
            "state": child.get("state"),
            "started_at": resumed_at,
        }
        updated_outcome = dict(outcome)
        updated_outcome["terminal_state"] = "superseded"
        updated_outcome["reason"] = "resumed_by_dispatch"
        updated_outcome["resume"] = resume_observation
        parent.update(
            {
                "state": "superseded",
                "terminal_state": "superseded",
                "liveness_state": goalflight_terminal.terminal_liveness_state("superseded"),
                "reason": "resumed_by_dispatch",
                "updated_at": resumed_at,
                "resumed_at": resumed_at,
                "resumed_by_dispatch_id": child_dispatch_id,
                "outcome": updated_outcome,
            }
        )
        goalflight_ledger.write_record(parent)
    return True


CURSOR_AGENTS = {"cursor", "cursor-agent"}


def _worker_prompt_preamble(agent: str | None, *, orientation_path: Path | None = None) -> str:
    is_cursor = agent in CURSOR_AGENTS
    preambles = [
        STEER_PROMPT_PREAMBLE,
        CURSOR_PROMPT_FILE_PREAMBLE if is_cursor else PROMPT_FILE_PREAMBLE,
    ]
    if orientation_path is not None:
        preambles.append(_project_orientation_preamble(orientation_path))
    if is_cursor:
        preambles.append(CURSOR_TOOLING_PREAMBLE)
    # Execution contract goes to the bash-tail agents only. ACP transports
    # (cursor, codex-acp, claude-acp) carry terminal state in the protocol, so
    # they do not need to be taught a text marker shape -- see
    # test_dispatch_steer.case_preamble_routing_matrix, which pins that split.
    if agent in {"grok-code", "grok-research", "moonshot"}:
        preambles.append(WORKER_EXECUTION_PREAMBLE)
    return "\n\n".join(preambles)


def _materialize_steer_prompt(
    prompt_path: str | None,
    base: Path,
    dispatch_id: str,
    *,
    agent: str | None = None,
    orientation_path: Path | None = None,
) -> str | None:
    if not prompt_path:
        return None
    body_path = Path(prompt_path)
    body = body_path.read_text(encoding="utf-8", errors="replace")
    preamble = _worker_prompt_preamble(agent, orientation_path=orientation_path)
    if agent not in {*CURSOR_AGENTS, "codex-acp", "claude-acp"}:
        preamble += (
            "\n\nTerminal evidence identity contract:\n"
            f"- Every terminal marker payload starts with the exact dispatch id `{dispatch_id}`.\n"
            f"- Successful final shape: `!COMPLETE: {dispatch_id} — <summary>`.\n"
            "- Use the same id prefix for READY, RESULT, FAILED, USER-NEED, "
            "USER-CONFIRM, or BLOCKED. A generic or foreign marker is ignored."
        )
    full_prompt = f"{preamble}\n\n{body}"
    # Lazy hardening at the other dispatch-dir writer (see the note in
    # _persist_acp_watcher_prompt); idempotent and marker-guarded.
    _prepare_private_dispatch_dir()
    assembled = base / f"{dispatch_id}.assembled.prompt"
    _atomic_write_private_text(assembled, full_prompt)
    return str(assembled)


def _default_dispatch_id(agent: str) -> str:
    return os.environ.get("GOALFLIGHT_DISPATCH_ID_SEED") or f"{agent}-{os.getpid()}-{int(time.time())}"


def _reserve_auto_dispatch_id(agent: str, base: Path) -> str:
    stem = _default_dispatch_id(agent)
    ids_dir = base / ".dispatch-ids"
    ids_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for attempt in range(1000):
        dispatch_id = stem if attempt == 0 else f"{stem}-{attempt + 1}"
        lock_path = ids_dir / f"{dispatch_id}.json"
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "dispatch_id": dispatch_id,
                    "agent": agent,
                    "reserved_at": int(time.time()),
                    "pid": os.getpid(),
                },
                fh,
                sort_keys=True,
            )
            fh.write("\n")
        return dispatch_id
    raise DispatchUsageError(f"could not reserve a dispatch id for stem {stem!r}")


@dataclass(frozen=True)
class _ControllerSessionLookup:
    sessions: list[dict] | None
    unreadable_reason: str | None = None


@dataclass(frozen=True)
class _ControllerDispatchIdentity:
    """One three-state registry snapshot for a single dispatch attempt.

    Stamp, resolve, prepare, and waiting_capacity record all thread this
    object so they cannot disagree about live / dead / unknown.
    """

    lookup: _ControllerSessionLookup
    self_resolved: dict | None = None


_CONTROLLER_DISPATCH_IDENTITY_ATTR = "_controller_dispatch_identity"


def _journal_unavailable_reason(
    exc: goalflight_journal.JournalUnavailable,
) -> str:
    if isinstance(exc, goalflight_journal.JournalBusy):
        kind = "journal busy"
    elif isinstance(exc, goalflight_journal.JournalIOError):
        kind = "journal I/O error"
    else:
        kind = f"journal unavailable ({type(exc).__name__})"
    detail = str(exc).strip()
    return f"{kind}: {detail}" if detail else kind


def _kernel_live_controller_sessions(
    project_root: Path,
) -> _ControllerSessionLookup:
    """Return kernel-live controller leases, preserving unreadable as unknown."""
    root = goalflight_task.resolve_project_root(str(project_root))
    try:
        authority = goalflight_journal.Journal.open_reader(root)
        sessions: list[dict] = []
        seen: set[tuple[str, int, str]] = set()
        for row in authority.lease_records():
            label = str(row.get("label") or "")
            nonce = str(row.get("nonce") or "")
            try:
                pid = int(row.get("pid"))
            except (TypeError, ValueError):
                continue
            if not label or not nonce:
                continue
            # Use the row this reader already returned. probe_live_session would
            # open the journal again at 0.05s and flap a still-held lease unknown.
            probe_state, session = _controller_lease_row_liveness(row, pid=pid)
            if probe_state == "unreadable":
                # Callers need to know whether any lease can own the dispatch.
                # Omitting one unreadable lease could turn an incumbent into a
                # false "none" or a false unique match, so the whole lookup is
                # unknown rather than a knowingly incomplete list.
                return _ControllerSessionLookup(
                    sessions=None,
                    unreadable_reason=(
                        f"controller lease {label!r} for pid {pid} was unreadable "
                        "within the liveness probe budget"
                    ),
                )
            if probe_state == "dead":
                continue
            if probe_state != "live":
                raise RuntimeError(
                    f"unexpected controller liveness probe state: {probe_state!r}"
                )
            if not isinstance(session, dict):
                raise RuntimeError("live controller liveness probe returned no session")
            if str(session.get("id") or "") != nonce:
                continue
            key = (label, pid, nonce)
            if key in seen:
                continue
            seen.add(key)
            sessions.append(dict(session))
        return _ControllerSessionLookup(
            sessions=sorted(
                sessions,
                key=lambda session: (
                    str(session.get("label") or ""),
                    int(session.get("pid") or 0),
                ),
            )
        )
    except goalflight_journal.JournalDisappeared:
        return _ControllerSessionLookup(sessions=[])
    except goalflight_journal.JournalUnavailable as exc:
        # Catch the ABC so a future subclass fails closed into unknown
        # rather than escaping. The reader-rule exemption is earned by
        # returning sessions=None with a reason derived from the exception,
        # not by this comment.
        return _ControllerSessionLookup(
            sessions=None,
            unreadable_reason=_journal_unavailable_reason(exc),
        )


def _controller_lease_row_liveness(
    row: dict[str, object],
    *,
    pid: int,
) -> tuple[str, dict | None]:
    """Classify one already-read ACTIVE lease without opening the journal again."""
    lease = goalflight_journal.Journal._lease_identity(row)
    liveness = goalflight_session_status._lease_holder_liveness(lease)
    if liveness is None or liveness.alive is None:
        return "unreadable", None
    if liveness.alive is not True:
        return "dead", None
    session = goalflight_session_status._session_dict_from_lease(lease, pid=pid)
    if not isinstance(session, dict):
        return "dead", None
    if str(session.get("id") or "") != str(lease.nonce):
        return "dead", None
    return "live", dict(session)


def _requested_controller_pid(args) -> int | None:
    for value in (
        getattr(args, "controller_pid", None),
        getattr(args, "controller_beacon_pid", None),
        os.environ.get("GOALFLIGHT_CONTROLLER_PID"),
    ):
        try:
            if value is not None and str(value).strip():
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _declared_controller_label(args, project_root: Path) -> str | None:
    value = getattr(args, "controller_label", None)
    if value is None:
        value = os.environ.get("GOALFLIGHT_CONTROLLER_LABEL")
    if not str(value or "").strip():
        return None
    return goalflight_session_status.resolve_controller_label(
        str(value),
        project_root=project_root,
    )


def _controller_session_is_in_ancestry(session: dict) -> bool:
    expected = session.get("process_identity")
    if not isinstance(expected, dict):
        return False
    expected_pid = expected.get("pid")
    expected_start = expected.get("start_token")
    if not isinstance(expected_pid, int) or not expected_start:
        return False
    ancestry = goalflight_session_status._controller_process_ancestry()
    return any(
        process.get("pid") == expected_pid
        and process.get("start_token") == expected_start
        for process in ancestry
    )


def _self_resolved_from_lookup(
    args,
    project_root: Path,
    lookup: _ControllerSessionLookup,
) -> dict | None:
    """Resolve one ancestry-proven live holder from an already-taken snapshot."""
    if lookup.sessions is None:
        return None
    label = _declared_controller_label(args, project_root)
    pid = _requested_controller_pid(args)
    matches = [
        session
        for session in lookup.sessions
        if (label is None or str(session.get("label") or "") == label)
        and (pid is None or int(session.get("pid") or 0) == pid)
        and _controller_session_is_in_ancestry(session)
    ]
    return dict(matches[0]) if len(matches) == 1 else None


def _self_resolved_controller_session(
    args,
    project_root: Path,
) -> dict | None:
    """Resolve one ancestry-proven live holder matching declared identity."""
    return _controller_dispatch_identity(args, project_root).self_resolved


def _controller_dispatch_identity(
    args,
    project_root: Path,
) -> _ControllerDispatchIdentity:
    """Return the one fused three-state lookup for this dispatch attempt."""
    cached = getattr(args, _CONTROLLER_DISPATCH_IDENTITY_ATTR, None)
    if isinstance(cached, _ControllerDispatchIdentity):
        return cached
    root = goalflight_task.resolve_project_root(str(project_root))
    lookup = _kernel_live_controller_sessions(root)
    ident = _ControllerDispatchIdentity(
        lookup=lookup,
        self_resolved=_self_resolved_from_lookup(args, root, lookup),
    )
    setattr(args, _CONTROLLER_DISPATCH_IDENTITY_ATTR, ident)
    return ident


def _remember_identified_controller_session(
    args,
    session: dict,
) -> None:
    """Keep prepare/record on the same snapshot after stamp identifies a holder."""
    current = getattr(args, _CONTROLLER_DISPATCH_IDENTITY_ATTR, None)
    session_copy = dict(session)
    sessions: list[dict]
    if (
        isinstance(current, _ControllerDispatchIdentity)
        and current.lookup.sessions is not None
    ):
        sessions = [dict(item) for item in current.lookup.sessions]
        session_id = str(session_copy.get("id") or "")
        if session_id and not any(
            str(item.get("id") or "") == session_id for item in sessions
        ):
            sessions.append(session_copy)
    else:
        sessions = [session_copy]
    self_resolved = (
        current.self_resolved
        if isinstance(current, _ControllerDispatchIdentity)
        else None
    )
    setattr(
        args,
        _CONTROLLER_DISPATCH_IDENTITY_ATTR,
        _ControllerDispatchIdentity(
            lookup=_ControllerSessionLookup(sessions=sessions),
            self_resolved=self_resolved,
        ),
    )


def _session_from_fused_lookup(
    lookup: _ControllerSessionLookup,
    *,
    label: str | None,
    pid: int | None,
    session_id: str | None = None,
) -> tuple[str, dict | None]:
    """Match a holder in the fused snapshot; unreadable stays unknown."""
    if lookup.sessions is None:
        return "unreadable", None
    candidates = list(lookup.sessions)
    if label is not None:
        candidates = [
            session
            for session in candidates
            if str(session.get("label") or "") == label
        ]
    if pid is not None:
        candidates = [
            session
            for session in candidates
            if int(session.get("pid") or 0) == pid
        ]
    if session_id is not None:
        candidates = [
            session
            for session in candidates
            if str(session.get("id") or "") == session_id
        ]
    if len(candidates) != 1:
        return "dead", None
    return "live", dict(candidates[0])


def _controller_child_role(args) -> bool:
    return bool(
        getattr(args, "from_queue", False)
        or getattr(args, "launch_detached", False)
        or getattr(args, "acp_detached_child", False)
    )


def _stamp_controller_session(args, project_root: Path) -> dict[str, object]:
    """Auto-claim a controller entry, or verify child ownership without renewal."""
    root = goalflight_task.resolve_project_root(str(project_root))
    declared_label = getattr(args, "controller_label", None)
    requested_label = goalflight_session_status.resolve_controller_label(
        declared_label,
        project_root=root,
    )
    declared_owner_pid = getattr(args, "controller_beacon_pid", None)
    requested_pid = goalflight_session_status.resolve_controller_pid(
        declared_owner_pid
    )
    requested_session_id = goalflight_session_status.resolve_controller_session_id(
        getattr(args, "controller_session_id", None)
    )
    explicit_session_id = str(
        getattr(args, "controller_session_id", None) or ""
    ).strip() or None
    args._requested_controller_label = _declared_controller_label(args, root)
    args._requested_controller_pid = _requested_controller_pid(args)
    args._requested_controller_session_id = requested_session_id
    ambient_lease_nonce = str(
        os.environ.get("GOALFLIGHT_CONTROLLER_LEASE_NONCE") or ""
    ).strip() or None
    carried_capabilities = {
        value for value in (requested_session_id, ambient_lease_nonce) if value
    }
    if len(carried_capabilities) > 1:
        args.controller_session_id = None
        args._controller_beacon_pid = None
        args.controller_label = None
        return {
            "claimed": False,
            "reason": "conflicting_controller_capabilities",
            "visible_warning": True,
        }
    if ambient_lease_nonce is not None:
        requested_session_id = ambient_lease_nonce
    child_role = _controller_child_role(args)
    ident = _controller_dispatch_identity(args, root)
    session: dict | None = None
    claim_result: dict[str, object]
    self_resolved = (
        ident.self_resolved
        if requested_session_id is None and not child_role
        else None
    )
    if isinstance(self_resolved, dict):
        session = self_resolved
        claim_result = {
            "claimed": False,
            "reason": "resolved_kernel_live_controller",
            "inherited": True,
        }
    elif requested_label and (
        ambient_lease_nonce is not None
        or (
            explicit_session_id is not None
            and not child_role
            and declared_owner_pid is None
        )
    ):
        verification_pid = (
            getattr(args, "_requested_controller_pid", None)
            if explicit_session_id is not None and ambient_lease_nonce is None
            else requested_pid
        )
        probe_state, candidate = _session_from_fused_lookup(
            ident.lookup,
            label=requested_label,
            pid=verification_pid,
            session_id=requested_session_id,
        )
        if probe_state == "live" and isinstance(candidate, dict):
            session = candidate
            claim_result = {
                "claimed": False,
                "reason": (
                    "inherited_controller_capability"
                    if ambient_lease_nonce is not None
                    else "verified_controller_capability"
                ),
                "inherited": True,
            }
        elif probe_state == "unreadable":
            # Unknown is not a capability miss. Prepare refuses with retry advice.
            claim_result = {
                "claimed": False,
                "reason": "controller_registry_unreadable",
            }
        else:
            claim_result = {
                "claimed": False,
                "reason": "controller_capability_mismatch",
                "visible_warning": True,
            }
    elif requested_label and not child_role and requested_pid is not None:
        claim_result = goalflight_session_status.claim_controller_startup(
            root,
            pid=requested_pid,
            label=requested_label,
            role="controller",
            session_id=requested_session_id,
            takeover=bool(getattr(args, "takeover", False)),
            hold_lock=True,
        )
        if claim_result.get("claimed"):
            candidate = claim_result.get("session")
            session = dict(candidate) if isinstance(candidate, dict) else None
        elif claim_result.get("reason") == "label_in_use":
            args.controller_session_id = None
            args._controller_beacon_pid = None
            args.controller_label = None
            return claim_result
        else:
            claim_result.setdefault("visible_warning", True)
    else:
        claim_result = {
            "claimed": False,
            "reason": (
                "role_does_not_claim"
                if child_role
                else "missing_session_beacon"
                if requested_label
                else "missing_controller_label"
            ),
            "role": "drainer" if child_role else "controller",
        }
        if requested_label and requested_session_id is not None:
            probe_state, candidate = _session_from_fused_lookup(
                ident.lookup,
                label=requested_label,
                pid=requested_pid,
                session_id=requested_session_id,
            )
            if probe_state == "live" and isinstance(candidate, dict):
                session = candidate
    session_id = session.get("id") if isinstance(session, dict) else None
    session_pid = session.get("pid") if isinstance(session, dict) else None
    session_label = session.get("label") if isinstance(session, dict) else None
    try:
        session_pid = int(session_pid) if session_pid is not None else None
    except (TypeError, ValueError):
        session_pid = None
    if not session_id or not session_label:
        session_id = None
        session_pid = None
        session_label = None
    args.controller_session_id = str(session_id) if session_id is not None else None
    args._controller_beacon_pid = session_pid
    args.controller_label = str(session_label) if session_label is not None else None
    if isinstance(session, dict) and session_id and session_label:
        _remember_identified_controller_session(args, session)
    return claim_result


def _controller_pid(args) -> int | None:
    session_id = getattr(args, "controller_session_id", None)
    pid = getattr(args, "_controller_beacon_pid", None)
    return pid if session_id and pid is not None else None


def _controller_session_id(args) -> str | None:
    value = getattr(args, "controller_session_id", None)
    return str(value) if value else None


def _controller_label(args) -> str | None:
    value = getattr(args, "controller_label", None)
    return str(value) if value and _controller_session_id(args) is not None else None


def _resolve_controller_dispatch_owner(
    args,
    project_root: Path,
) -> tuple[str, int, str] | None:
    """Return the kernel-live nonce + pid + label owner triple, if exact."""
    nonce = _controller_session_id(args)
    pid = _controller_pid(args)
    label = _controller_label(args)
    if nonce is None or pid is None or label is None:
        return None
    ident = _controller_dispatch_identity(args, project_root)
    probe_state, session = _session_from_fused_lookup(
        ident.lookup,
        label=label,
        pid=pid,
        session_id=nonce,
    )
    if probe_state != "live" or not isinstance(session, dict):
        return None
    try:
        session_pid = int(session.get("pid"))
    except (TypeError, ValueError):
        return None
    if (
        str(session.get("id") or "") != nonce
        or session_pid != pid
        or str(session.get("label") or "") != label
    ):
        return None
    return nonce, pid, label


def _unregistered_controller_warning() -> str:
    reseat = shlex.join(
        [
            "python3",
            str(goalflight_compat.advertised_script("goalflight_session_status.py")),
            "--controller-startup",
            "--controller-pid-from-ancestry",
        ]
    )
    return "\n".join(
        [
            "controller is not registered; the default path refuses before record or "
            "launch, while an --unregistered-forced dispatch is recorded with no "
            "owner and its terminal event will wake every controller in the project.",
            reseat,
            "Use --unregistered-forced to override and launch without a recorded owner.",
        ]
    )


def _controller_registry_unreadable_warning(reason: str) -> str:
    return "\n".join(
        [
            "controller registry could not be read; controller registration state "
            f"is unknown ({reason}).",
            "Retry the dispatch; refusing before record or launch.",
        ]
    )


def _controller_registry_unknown_forced_warning(reason: str) -> str:
    return "\n".join(
        [
            "controller registry could not be read; ownership could not be "
            f"determined ({reason}).",
            "--unregistered-forced accepted: recording this dispatch with no "
            "owner. Its terminal event will wake every controller in the project.",
        ]
    )


def _remove_option_before_worker_remainder(argv: list[str], flag: str) -> list[str]:
    return _drop_option_before_worker_remainder(argv, flag, takes_value=True)


def _registered_dispatch_command(args, session: dict) -> str:
    argv = list(getattr(args, "_original_argv", []) or [])
    argv = _remove_flag_before_worker_remainder(argv, "--unregistered-forced")
    argv = _remove_option_before_worker_remainder(argv, "--controller-beacon-pid")
    for flag, value in (
        ("--controller-label", session.get("label")),
        ("--controller-pid", session.get("pid")),
        ("--controller-session-id", session.get("id")),
    ):
        argv = _set_option_before_worker_remainder(argv, flag, str(value))
    return shlex.join(
        [
            "python3",
            str(
                goalflight_compat.advertised_script(
                    str(
                        getattr(
                            args,
                            "_controller_registration_script",
                            "goalflight_dispatch.py",
                        )
                    )
                )
            ),
            *argv,
        ]
    )


def _controller_not_in_ancestry_warning(session: dict) -> str:
    return "\n".join(
        [
            "controller "
            f"{session.get('label')} is kernel-live under pid {session.get('pid')}, "
            "but that holder is not in this invocation's process ancestry; refusing "
            "to attribute this dispatch to the incumbent.",
            "Use --unregistered-forced to override and launch without a recorded "
            "owner.",
        ]
    )


def _controller_registration_warning(
    args,
    project_root: Path,
    *,
    lookup: _ControllerSessionLookup | None = None,
) -> str:
    if lookup is None:
        lookup = _controller_dispatch_identity(args, project_root).lookup
    if lookup.sessions is None:
        return _controller_registry_unreadable_warning(
            lookup.unreadable_reason or "unclassified registry read failure"
        )
    sessions = lookup.sessions
    if not sessions:
        return _unregistered_controller_warning()

    requested_label = getattr(args, "_requested_controller_label", None)
    requested_pid = getattr(args, "_requested_controller_pid", None)
    same_label = [
        session
        for session in sessions
        if requested_label is not None
        and str(session.get("label") or "") == str(requested_label)
    ]
    if len(sessions) == 1:
        session = sessions[0]
        if requested_pid is not None and int(session.get("pid") or 0) != int(
            requested_pid
        ):
            return "\n".join(
                [
                    "controller "
                    f"{session.get('label')} is kernel-live under pid "
                    f"{session.get('pid')}, not requested pid {requested_pid}; "
                    "refusing to adopt or displace the incumbent.",
                    "Use --unregistered-forced to override and launch without a "
                    "recorded owner.",
                ]
            )
        if not _controller_session_is_in_ancestry(session):
            return _controller_not_in_ancestry_warning(session)
        return "\n".join(
            [
                "a kernel-live controller exists, but this dispatch invocation did "
                "not identify it; live controller label: "
                f"{session.get('label')}.",
                _registered_dispatch_command(args, session),
                "Use --unregistered-forced to override and launch without a "
                "recorded owner.",
            ]
        )

    if requested_pid is not None:
        incumbent = next(
            (
                session
                for session in same_label or sessions
                if int(session.get("pid") or 0) != int(requested_pid)
            ),
            None,
        )
        if incumbent is not None and (same_label or len(sessions) == 1):
            return "\n".join(
                [
                    "controller "
                    f"{incumbent.get('label')} is kernel-live under pid "
                    f"{incumbent.get('pid')}, not requested pid {requested_pid}; "
                    "refusing to adopt or displace the incumbent.",
                    "Use --unregistered-forced to override and launch without a "
                    "recorded owner.",
                ]
            )

    exact = [
        session
        for session in sessions
        if (requested_label is None or str(session.get("label")) == str(requested_label))
        and (requested_pid is None or int(session.get("pid") or 0) == int(requested_pid))
    ]
    if len(exact) == 1:
        session = exact[0]
        if not _controller_session_is_in_ancestry(session):
            return _controller_not_in_ancestry_warning(session)
        return "\n".join(
            [
                "a kernel-live controller exists, but this dispatch invocation did "
                "not identify it; live controller label: "
                f"{session.get('label')}.",
                _registered_dispatch_command(args, session),
                "Use --unregistered-forced to override and launch without a "
                "recorded owner.",
            ]
        )

    incumbents = ", ".join(
        f"{session.get('label')} (pid {session.get('pid')})" for session in sessions
    )
    return "\n".join(
        [
            "controller identity is ambiguous; multiple kernel-live controllers "
            f"exist for this project: {incumbents}.",
            "Refusing to guess an owner; identify one with --controller-label, "
            "--controller-pid, and --controller-session-id.",
            "Use --unregistered-forced to override and launch without a recorded "
            "owner.",
        ]
    )


def _prepare_attempt_controller_registration(
    args,
    project_root: Path,
) -> str | None:
    """Refuse an unowned attempt, or return the forced-path warning to emit."""
    ident = _controller_dispatch_identity(args, project_root)
    if _resolve_controller_dispatch_owner(args, project_root) is not None:
        return None
    lookup = ident.lookup
    warning = _controller_registration_warning(args, project_root, lookup=lookup)
    if lookup.sessions is None:
        # Default path: unknown is not "unregistered". Retry is the right
        # advice for a contended journal. --unregistered-forced still means
        # "launch with no recorded owner" when the registry cannot be read,
        # including a persistent unknown (ACTIVE SQL lease whose generation
        # lock cannot be opened). Distinguishing transient busy from that
        # persistent unknown would need a second classification path; the
        # existing flag already names the consent, so only the unforced path
        # refuses.
        if getattr(args, "unregistered_forced", False):
            return _controller_registry_unknown_forced_warning(
                lookup.unreadable_reason or "unclassified registry read failure"
            )
        raise DispatchUsageError(warning)
    if getattr(args, "unregistered_forced", False):
        return warning
    raise DispatchUsageError(warning)


def _account_engine(agent: str) -> str | None:
    return ACCOUNT_ENGINE_BY_AGENT.get(agent)


def _account_home(account: str, engine: str) -> Path:
    return Path.home() / ".goal-flight" / "accounts" / account / engine


def _apply_home_env(env: dict[str, str], home: Path) -> None:
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")


def _cursor_account_probe(env: dict[str, str]) -> tuple[bool, str | None]:
    try:
        proc = subprocess.run(
            ["cursor-agent", "status"],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    combined = f"{proc.stdout}\n{proc.stderr}".strip()
    negative = ("not logged in", "not authenticated", "login required", "please log in")
    positive = ("logged in", "login successful")
    lowered = combined.lower()
    if proc.returncode == 0 and any(term in lowered for term in positive) and not any(term in lowered for term in negative):
        return True, None
    return False, combined[-400:] or f"cursor-agent status exited {proc.returncode}"


_GROK_SELECTION_UNSET = object()


def grok_selected_account(args) -> str | None:
    """The grok seat auto-selected for an UNPINNED dispatch, or None for host.

    Mirrors what codex already does through the active-seat pointer: an unpinned
    dispatch still lands on a seat with headroom, while ``args.account`` stays
    None so the ledger records `account: "default"` (nothing was pinned) beside
    `effective_account: <seat>` (what actually got billed). Overwriting
    args.account instead would erase the difference between an operator pinning
    a seat and this picking one.

    Memoized on args: selection may refresh a cached probe, and two calls that
    straddled a refresh could disagree about which seat the dispatch used.
    """
    cached = getattr(args, "_grok_selected_account", _GROK_SELECTION_UNSET)
    if cached is not _GROK_SELECTION_UNSET:
        return cached
    selected = None
    if not getattr(args, "account", None) and _account_engine(args.agent) == "grok":
        try:
            import grok_seats

            selected = grok_seats.select_seat()
        except BaseException:
            # Selection is an optimisation; never let it fail a dispatch.
            selected = None
    args._grok_selected_account = selected
    return selected


def _resolve_account_env(args) -> dict[str, str]:
    account = args.account or grok_selected_account(args)
    if not account:
        return {}
    engine = _account_engine(args.agent)
    if not engine:
        raise DispatchUsageError(
            f"--account is not configured for --agent {args.agent!r}; refusing to bill the wrong account"
        )
    home = _account_home(account, engine)
    if engine == "codex":
        if not home.exists():
            raise DispatchUsageError(
                f"--account {account} not configured (expected {home}). "
                "Set that account's creds there, or omit --account for the host default. "
                "Refusing to bill the wrong account."
            )
        return {"CODEX_HOME": str(home)}
    if not home.exists():
        raise DispatchUsageError(
            f"--account {account} not configured for {engine} (expected HOME {home}). "
            "Refusing to bill the wrong account."
        )
    env = dict(os.environ)
    _apply_home_env(env, home)
    if engine == "grok":
        env.pop("GROK_API_KEY", None)
        env.pop("XAI_API_KEY", None)
        auth = home / ".grok" / "auth.json"
        if not auth.is_file() or auth.stat().st_size == 0:
            raise DispatchUsageError(
                f"--account {account} lacks grok creds (expected non-empty {auth}). "
                "Refusing to bill the wrong account."
            )
        return {key: env[key] for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")}
    if engine == "cursor":
        env.pop("CURSOR_API_KEY", None)
        ok, reason = _cursor_account_probe(env)
        if not ok:
            raise DispatchUsageError(
                f"--account {account} lacks cursor creds ({reason}). "
                "Refusing to bill the wrong account."
            )
        return {key: env[key] for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")}
    raise DispatchUsageError(f"--account unsupported for engine {engine!r}")


def _guard_grok_seat_permission_mode(
    args,
    account_env: dict[str, str],
    *,
    default_home: Path | None = None,
) -> str | None:
    """Refuse a grok launch whose selected home has no permission_mode.

    Only absence is a hard stop: a present value is returned and otherwise
    ignored. This does not add ``--permission-mode`` to argv and does not
    rewrite the operator's config.toml.
    """
    if _account_engine(getattr(args, "agent", None) or "") != "grok":
        return None
    home = grok_permission_mode.home_from_account_env(
        account_env,
        default_home=default_home,
    )
    inspection = grok_permission_mode.inspect_home(home)
    if inspection.status == "present":
        return inspection.mode
    raise DispatchUsageError(grok_permission_mode.refusal_message(inspection))


def _resolve_launch_account_env(args) -> dict[str, str]:
    """Resolve the seat HOME, then refuse a grok home with no permission_mode."""
    account_env = _resolve_account_env(args)
    _guard_grok_seat_permission_mode(args, account_env)
    return account_env


_CODEX_SEAT_API_UNSET = object()
_CODEX_SEAT_API_CACHE = _CODEX_SEAT_API_UNSET


def _maybe_mark_grok_quota_exhausted(
    dispatch_id: str | None = None,
    state: str | None = None,
    record: dict | None = None,
) -> None:
    """If this finish just proved a grok seat empty, tell the optional rotator.

    Without this, the next dispatch re-reads a cache that still calls the
    dead seat healthy for up to the probe TTL. A missing rotator is a
    no-op; this must never fail the dispatch.
    """
    try:
        if (
            state not in (None, "quota_exhausted")
            and (
                record is None
                or (
                    record.get("state") != "quota_exhausted"
                    and record.get("terminal_state") != "quota_exhausted"
                )
            )
        ):
            return
        import grok_seats

        if record is None:
            if not dispatch_id:
                return
            path = goalflight_ledger.record_path(dispatch_id, create=False)
            record = json.loads(path.read_text(encoding="utf-8"))
        grok_seats.note_exhausted_if_proven(record, state=state)
    except BaseException:
        # Optional seat-cache freshness only: the authoritative terminal state
        # is already committed, and a later probe repairs a missed cache flip.
        return


def _codex_seat_api():
    """Return the optional local launch library, or None on any import failure."""
    global _CODEX_SEAT_API_CACHE
    if _CODEX_SEAT_API_CACHE is not _CODEX_SEAT_API_UNSET:
        return _CODEX_SEAT_API_CACHE
    try:
        from ext import codex_seat_lib
    except BaseException:
        codex_seat_lib = None
    _CODEX_SEAT_API_CACHE = codex_seat_lib
    return _CODEX_SEAT_API_CACHE


def resolve_codex_home(
    project_root: Path | str,
    explicit_account: str | None,
    dispatch_id: str,
) -> tuple[str | None, str | None]:
    """Resolve one launch snapshot without ever failing the dispatch."""
    api = _codex_seat_api()
    if api is None:
        return None, None
    try:
        resolved = api.resolve_codex_seat(
            str(project_root),
            explicit_account,
            dispatch_id,
        )
        if not isinstance(resolved, tuple) or len(resolved) != 2:
            return None, None
        home, effective_account = resolved
        if home is None and effective_account is None:
            return None, None
        if not isinstance(home, str) or not home:
            return None, None
        if not isinstance(effective_account, str) or not effective_account:
            return None, None
        return home, effective_account
    except BaseException:
        return None, None


def cleanup_codex_dispatch_home(dispatch_id: str) -> None:
    """Best-effort cleanup through the same guarded optional-library seam."""
    api = _codex_seat_api()
    if api is None:
        return
    try:
        api.cleanup_dispatch_home(dispatch_id)
    except BaseException as exc:
        print(
            "goalflight_dispatch: per-dispatch home cleanup warning: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )


_CONTEXT_MODE_TABLE_RE = re.compile(
    r"""(?mx)
    ^\s*\[\s*mcp_servers\s*\.\s*
    (?:"context-mode"|'context-mode'|context-mode)
    \s*\]\s*(?:\#.*)?$
    """
)
_CONTEXT_MODE_DISABLE_KEY = "mcp_servers.context-mode.enabled=false"


def codex_context_mode_defined(env: dict[str, str]) -> bool:
    """Whether the effective worker home defines the server being disabled."""
    raw_home = env.get("CODEX_HOME")
    home = Path(raw_home).expanduser() if raw_home else Path.home() / ".codex"
    try:
        config = (home / "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return _CONTEXT_MODE_TABLE_RE.search(config) is not None


def _guard_codex_context_mode_disable(
    argv: list[str],
    env: dict[str, str],
) -> list[str]:
    """Drop only the disable override when its base config table is absent."""
    if codex_context_mode_defined(env):
        return argv
    guarded: list[str] = []
    index = 0
    while index < len(argv):
        if (
            argv[index] == "-c"
            and index + 1 < len(argv)
            and argv[index + 1] == _CONTEXT_MODE_DISABLE_KEY
        ):
            index += 2
            continue
        guarded.append(argv[index])
        index += 1
    return guarded


CAPACITY_WAIT_DEFAULTS_S = goalflight_capacity.CAPACITY_WAIT_DEFAULTS_S
_CAPACITY_WAIT_POLL_S = goalflight_capacity.CAPACITY_WAIT_POLL_S


def _capacity_wait_seconds(args) -> float:
    """Resolve the inline acquire budget before durable queue fallback."""
    if (
        not getattr(args, "foreground", False)
        and getattr(args, "capacity_wait_s", None) is None
        and "GOALFLIGHT_CAPACITY_WAIT_S" not in os.environ
    ):
        # Detached work is durable after a capacity refusal, so keeping its
        # launcher alive for the lane default only delays the queue handoff.
        # Explicit CLI/env budgets still opt into inline polling.
        return 0.0
    return goalflight_capacity.resolve_capacity_wait_s(
        lane=getattr(args, "priority", None) or "normal",
        wait_s=getattr(args, "capacity_wait_s", None),
        log_prefix="goalflight_dispatch",
    )


def _detached_capacity_queue_eligible(args) -> bool:
    """Whether this launcher may hand a capacity refusal to the durable queue."""
    return bool(
        not goalflight_compat.is_windows()
        and not getattr(args, "from_queue", False)
        and not getattr(args, "foreground", False)
    )


def _capacity_refusal_attempt_stays_prepared(args) -> bool:
    """Whether a durable carrier owns retry while this attempt stays PREPARED."""
    if goalflight_compat.is_windows() or getattr(args, "foreground", False):
        return False
    if not getattr(args, "from_queue", False):
        return True
    return bool(
        getattr(args, "queue_launch_token", None)
        and getattr(args, "queue_claim_path", None)
    )


def _is_capacity_refusal(payload: dict | None) -> bool:
    if not isinstance(payload, dict) or payload.get("state") != "blocked_capacity":
        return False
    reason = payload.get("reason")
    return not isinstance(reason, dict) or reason.get("reason") != "wait_interrupted"


def _queue_detached_capacity_refusal(args, payload: dict | None, *, base: Path) -> bool:
    """Apply the one durable fallback shared by bash and ACP launchers."""
    if not _detached_capacity_queue_eligible(args) or not _is_capacity_refusal(payload):
        return False
    return _submit_dispatch(args, _raw_worker_args(args), base=base) == 0


def _resolved_engine_session_id(args) -> str | None:
    engine = goalflight_engine_sessions.resume_engine(getattr(args, "agent", None))
    for raw in (
        getattr(args, "engine_session_id", None),
        getattr(args, "codex_session_id", None),
    ):
        handle = goalflight_engine_sessions.valid_session_id(engine, raw)
        if handle is not None:
            return handle
    return None


def _ensure_assigned_engine_session(args) -> str | None:
    """Name a new grok/claude session before spawn so resume has a handle.

    Resume launches already carry the recorded handle. Engines that cannot
    assign (kimi, cursor) stay blank until harvest; a blank handle refuses
    resume instead of starting fresh.
    """
    existing = _resolved_engine_session_id(args)
    if existing is not None:
        args.engine_session_id = existing
        return existing
    if getattr(args, "parent_dispatch_id", None):
        return None
    engine = goalflight_engine_sessions.resume_engine(getattr(args, "agent", None))
    if not goalflight_engine_sessions.can_assign_at_launch(engine):
        return None
    assigned = goalflight_engine_sessions.new_session_id(engine)
    args.engine_session_id = assigned
    return assigned


def _codex_home_owner_dispatch_id(args) -> str | None:
    recorded = getattr(args, "codex_home_owner_dispatch_id", None)
    if isinstance(recorded, str) and recorded:
        return recorded
    if (
        _account_engine(getattr(args, "agent", None)) == "codex"
        and not goalflight_compat.is_windows()
    ):
        return str(args.dispatch_id)
    return None


def _prelaunch_status_metadata(
    args,
    *,
    codex_home: str | None = None,
    codex_session_id: str | None = None,
) -> dict:
    metadata = {
        "schema": "goalflight.status.v1",
        "dispatch_id": args.dispatch_id,
        "agent": args.agent,
        "shape": args.shape,
        "controller_session_id": _controller_session_id(args),
        "controller_pid": _controller_pid(args),
        "controller_label": _controller_label(args),
    }
    task_ids = list(getattr(args, "task_ids", []) or [])
    if task_ids:
        metadata["task_ids"] = task_ids
    parent_dispatch_id = getattr(args, "parent_dispatch_id", None)
    if parent_dispatch_id:
        metadata["parent_dispatch_id"] = parent_dispatch_id
    resolved_session_id = (
        codex_session_id
        or _resolved_engine_session_id(args)
        or goalflight_codex_sessions.valid_session_id(
            getattr(args, "codex_session_id", None)
        )
    )
    if resolved_session_id:
        metadata["engine_session_id"] = resolved_session_id
        if goalflight_engine_sessions.resume_engine(getattr(args, "agent", None)) == "codex":
            metadata["codex_session_id"] = resolved_session_id
    resolved_home = codex_home or getattr(args, "codex_resume_home", None)
    if resolved_home:
        metadata["codex_home"] = str(resolved_home)
    home_owner_dispatch_id = _codex_home_owner_dispatch_id(args)
    if home_owner_dispatch_id:
        metadata["codex_home_owner_dispatch_id"] = home_owner_dispatch_id
    return metadata


def _acquire_capacity(args, *, project_root: Path, status_json: Path) -> str | None:
    # Lease TTL answers a DIFFERENT question from the idle backstop: idle asks
    # "how long may a worker be quiet", TTL asks "how long may a dead worker keep
    # holding capacity". Deriving one from the other couples them, so raising the
    # idle backstop silently multiplied capacity holding.
    #
    # Bounds, both load-bearing:
    #   lower  TTL > idle, or a legitimately quiet-but-alive worker loses its
    #          lease while still working — the opposite of the bug we are fixing.
    #   upper  capped, or a dead code writer would hold a lease for
    #          3600*4 = 14400s (4h). With ~127 leases live on this box that
    #          starves the pool.
    # 7200s satisfies both: 2x the 3600s code-writer backstop (a worker quiet a
    # full hour keeps its lease) while halving the dead-worker hold from 4h.
    # Sanity: default agent 900s -> max(3600,3600)=3600 -> unchanged at 1h.
    lease_ttl_s = min(max(int(args.max_idle_secs or 300) * 4, 3600), 7200)
    acquire_args = argparse.Namespace(
        agent=args.agent,
        dispatch_id=args.dispatch_id,
        prompt_id=None,
        project_root=str(project_root),
        worktree_path=None,
        worker_cwd=str(_worker_cwd(args)),
        controller_pid=_controller_pid(args),
        worker_pid=None,
        lease_id=None,
        mem_mb=None,
        agent_cap=None,
        priority=getattr(args, "priority", None) or "normal",
        ttl_s=lease_ttl_s,
        ram_mb=None,
        reserve_mb=goalflight_capacity.DEFAULT_RESERVE_MB,
        worst_worker_mb=goalflight_capacity.DEFAULT_WORST_WORKER_MB,
        hard_cap=goalflight_capacity.DEFAULT_HARD_CAP,
        max_total=None,
    )
    wait_budget_s = _capacity_wait_seconds(args)

    def _write_blocked(blocked_payload: dict) -> None:
        write_status(
            status_json,
            {
                **_prelaunch_status_metadata(args),
                "state": "blocked_capacity",
                "reason": blocked_payload,
                "worker_alive": False,
                "updated_at": int(time.time()),
            },
        )
        print("DISPATCH-BLOCKED " + json.dumps({"state": "blocked_capacity", "reason": blocked_payload}, sort_keys=True), flush=True)

    capacity_wait_started = active_monotonic()
    last_wait = {"attempt": 0, "remaining_s": wait_budget_s}

    def _on_wait(attempt: int, remaining_s: float, reason: dict) -> None:
        last_wait["attempt"] = attempt
        last_wait["remaining_s"] = remaining_s
        waited_s = round(max(0.0, wait_budget_s - remaining_s), 1)
        write_status(
            status_json,
            {
                **_prelaunch_status_metadata(args),
                "state": "waiting_capacity",
                "reason": reason,
                "waited_s": waited_s,
                "wait_budget_s": wait_budget_s,
                "worker_alive": False,
                "updated_at": int(time.time()),
            },
        )
        if attempt == 1 or attempt % 4 == 0:
            print(
                "CAPACITY-WAIT "
                + json.dumps(
                    {
                        "dispatch_id": args.dispatch_id,
                        "agent": args.agent,
                        "priority": acquire_args.priority,
                        "reason": reason.get("reason"),
                        "waited_s": waited_s,
                        "wait_budget_s": wait_budget_s,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    try:
        payload = goalflight_capacity.acquire_with_wait(
            acquire_args,
            lane=acquire_args.priority,
            wait_s=wait_budget_s,
            poll_s=_CAPACITY_WAIT_POLL_S,
            jitter=goalflight_capacity.CAPACITY_WAIT_JITTER_S,
            on_wait=_on_wait,
            install_signal_handlers=True,
        )
    except goalflight_capacity.CapacityWaitInterrupted as exc:
        _write_blocked(exc.payload)
        raise SystemExit(exc.exit_code or 1) from None
    if payload.get("decision") == "allow":
        return payload.get("lease", {}).get("lease_id")
    blocked_payload = dict(payload)
    if last_wait["attempt"]:
        blocked_payload.setdefault(
            "waited_s",
            round(max(0.0, active_monotonic() - capacity_wait_started), 1),
        )
        blocked_payload.setdefault("attempts", int(last_wait["attempt"]) + 1)
    else:
        blocked_payload.setdefault("waited_s", 0.0)
        blocked_payload.setdefault("attempts", 1)
    _write_blocked(blocked_payload)
    raise SystemExit(2)


def _queue_request_envelope(args) -> dict | None:
    """Copy the durable child-request inputs from a live queue claim."""
    if not (
        getattr(args, "from_queue", False)
        and getattr(args, "queue_claim_path", None)
    ):
        return None
    try:
        entry = json.loads(
            Path(str(args.queue_claim_path))
            .expanduser()
            .read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict):
        return None
    launch_token = getattr(args, "queue_launch_token", None)
    if launch_token and entry.get("queue_launch_token") != launch_token:
        return None
    dispatch_argv = entry.get("dispatch_argv")
    request = entry.get("request")
    if not isinstance(dispatch_argv, list) or not dispatch_argv:
        return None
    if not isinstance(request, dict):
        return None
    envelope = {
        key: entry[key]
        for key in (
            "schema",
            "dispatch_id",
            "agent",
            "shape",
            "project_root",
            "process_cwd",
            "base_sha",
            "task_ids",
            "requeued_from",
        )
        if key in entry
    }
    envelope["dispatch_argv"] = list(dispatch_argv)
    envelope["request"] = dict(request)
    return envelope


def _record_ledger(args, *, project_root: Path, prompt_path: str | None, status_json: Path,
                   tail: Path, lease_id: str | None, worker_pid: int | None, state: str,
                   effective_account: str | None = None,
                   codex_home: str | None = None,
                   request_envelope: dict | None = None) -> dict | None:
    """Record one lifecycle transition; never fail a spawned worker's launch.

    Returns None when the record committed. When the journal refuses the
    transition but a worker process was spawned, the refusal is a bookkeeping
    problem, not evidence about the worker: the status file is still written
    and the refusal is surfaced LOUDLY (stderr + the status file), and the
    unrecovered refusal payload is returned so launch-site callers can fold
    it into their own warnings. Only when no worker was spawned does a
    refusal raise, unchanged.
    """
    if state == "waiting_capacity":
        # Re-check the fused snapshot from stamp/prepare. Do not take a second
        # registry read: a later contended open is what turned an already
        # identified owner into "did not identify it" / unknown.
        _prepare_attempt_controller_registration(args, project_root)

    spawn_state = goalflight_ledger.worker_spawn_state(worker_pid)

    def _record_once() -> tuple[int, dict | None]:
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            code = goalflight_ledger.cmd_record(
                argparse.Namespace(
                    dispatch_id=args.dispatch_id,
                    prompt_id=None,
                    prompt_path=prompt_path,
                    task_ids=getattr(args, "task_ids", []),
                    agent=args.agent,
                    engine=_account_engine(args.agent) or args.agent,
                    shape=args.shape,
                    account=args.account or "default",
                    effective_account=effective_account,
                    request_envelope_json=(
                        json.dumps(request_envelope, sort_keys=True)
                        if request_envelope is not None
                        else None
                    ),
                    transport="dispatch",
                    project_root=str(project_root),
                    controller_pid=_controller_pid(args),
                    controller_session_id=_controller_session_id(args),
                    controller_label=_controller_label(args),
                    claimant_pid=os.getpid() if state == "waiting_capacity" else None,
                    worker_pid=worker_pid if spawn_state == "spawned" else None,
                    acp_session_id=None,
                    logical_session_id=args.dispatch_id,
                    engine_session_id=_resolved_engine_session_id(args),
                    codex_session_id=getattr(args, "codex_session_id", None),
                    codex_home=codex_home or getattr(args, "codex_resume_home", None),
                    codex_home_owner_dispatch_id=_codex_home_owner_dispatch_id(args),
                    parent_dispatch_id=getattr(args, "parent_dispatch_id", None),
                    worker_cwd=str(_worker_cwd(args)),
                    dispatch_argv=_canonical_replay_argv(
                        args,
                        _raw_worker_args(args)
                        if getattr(args, "worker", None) is not None
                        else [],
                        tail=tail,
                        status_json=status_json,
                    ),
                    lease_id=lease_id,
                    stdout_path=str(tail),
                    stderr_path=None,
                    status_path=str(status_json),
                    os_sandbox_json=json.dumps(
                        _os_sandbox_posture(args, worker_pid=worker_pid),
                        sort_keys=True,
                    ),
                    queue_launch_token=getattr(args, "queue_launch_token", None),
                    detached=bool(getattr(args, "launch_detached", False)),
                    state=state,
                    json=True,
                )
            )
        return code, goalflight_ledger.parse_record_refusal(capture.getvalue())

    record_code, refusal = _record_once()
    if (
        record_code != 0
        and spawn_state != "none"
        and goalflight_ledger.is_retryable_startup_race(refusal)
    ):
        # The worker claims RUNNING asynchronously after spawn, so "not yet"
        # becomes "yes" on its own within the worker's startup. Re-record
        # against a bounded deadline BEFORE deciding anything else. Budget
        # derivation: goalflight_ledger.RECORD_STARTUP_RACE_RETRY_BUDGET_S.
        record_code, refusal = goalflight_ledger.retry_record_after_startup_race(
            _record_once,
            record_code,
            refusal,
            project_root=project_root,
            dispatch_id=str(args.dispatch_id),
            timeout_s=goalflight_ledger.RECORD_STARTUP_RACE_RETRY_BUDGET_S,
        )
    warning = None
    if record_code != 0:
        if spawn_state == "none":
            # No worker process exists, so a refused transition is a genuine
            # launch failure. Unchanged behaviour.
            raise RuntimeError(
                f"journal attempt transition refused for {args.dispatch_id}: exit {record_code}"
            )
        # A worker was spawned (or spawn state is indeterminate, which takes
        # the same safe branch). Keep the liveness authority written and the
        # refusal visible; losing the warning is as bad as losing the status
        # file, so the refusal goes to BOTH stderr and the status payload.
        warning = {
            "kind": "journal_attempt_transition_refused",
            "exit_code": record_code,
            "disposition": (refusal or {}).get("disposition"),
            "retryable": (refusal or {}).get("retryable"),
            "error": (refusal or {}).get("error"),
            "state": state,
            "spawn_state": spawn_state,
            "detail": (
                "worker process was spawned but the journal transition was "
                "refused; bookkeeping is incomplete, the worker may be alive "
                "— do not blind-retry this dispatch"
            ),
        }
        worker_alive = None
        if spawn_state == "spawned":
            with contextlib.suppress(Exception):
                worker_alive = goalflight_compat.pid_alive(worker_pid)
        print(
            "DISPATCH-LEDGER-WARN "
            + json.dumps(
                {
                    "dispatch_id": args.dispatch_id,
                    "worker_pid": worker_pid,
                    "worker_alive": worker_alive,
                    "spawn_state": spawn_state,
                    "tail": str(tail),
                    "status_json": str(status_json),
                    "warning": warning,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        with contextlib.suppress(Exception):
            write_status(
                status_json,
                {
                    **_prelaunch_status_metadata(args),
                    "worker_pid": worker_pid,
                    "worker_alive": worker_alive,
                    "spawn_state": spawn_state,
                    "tail_path": str(tail),
                    "state": state,
                    "reason": "ledger_transition_refused",
                    "ledger_record_warning": warning,
                    "updated_at": int(time.time()),
                },
            )
    _export_dashboard_status_for_project(project_root)
    _upsert_project_registry_for_dispatch(project_root)
    if state in {"waiting_capacity", "starting", "running"}:
        _start_dashboard_refresh_for_project(project_root)
    return warning


_NATIVE_RECORD_LEDGER = _record_ledger


def _attempt_claiming_worker_argv(
    project_root: Path,
    dispatch_id: str,
    worker_argv: list[str],
) -> tuple[list[str], bool]:
    try:
        # Peek-only: the RUNNING CAS lives in the unsandboxed watcher. A writer
        # Journal() would take the construction write lock on the launch path.
        # Only a genuine FileNotFoundError (JournalDisappeared) is "no journal".
        # An unreadable present journal raises JournalIOError and must not skip
        # attempt fencing.
        attempt = goalflight_journal.Journal.open_reader(
            project_root
        ).attempt_for_dispatch(dispatch_id)
    except goalflight_journal.JournalDisappeared:
        # Embedders can replace the ledger-recording seam when they own launch
        # tracking. Preserve that pre-P2 seam without bootstrapping authority
        # behind the embedding's back.
        return worker_argv, False
    if attempt is None:
        if _record_ledger is not _NATIVE_RECORD_LEDGER:
            # Controller registration can create the journal independently of
            # an embedder's injected, non-persisting ledger seam.  Its mere
            # presence does not prove that seam prepared a launch attempt.
            return worker_argv, False
        raise RuntimeError(f"prepared attempt missing for {dispatch_id}")
    if attempt.lifecycle_state != goalflight_journal.ATTEMPT_STARTING:
        raise RuntimeError(
            f"prepared attempt {attempt.attempt_id} is {attempt.lifecycle_state}, expected STARTING"
        )
    # The unsandboxed watcher claims RUNNING after spawn. Do not wrap the
    # worker in launch_worker — that CAS was the only in-sandbox journal write.
    return worker_argv, True


def _record_queued_ledger_fast(args, *, project_root: Path, prompt_path: str | None, status_json: Path, tail: Path) -> None:
    dispatch_id = args.dispatch_id
    now = goalflight_ledger.utc_now()
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "prompt_id": None,
        "prompt_path": prompt_path,
        "prompt_sha256": goalflight_ledger.sha256_file(prompt_path),
        "agent": args.agent,
        "engine": _account_engine(args.agent) or args.agent,
        "shape": args.shape,
        "account": args.account or "default",
        "transport": "dispatch",
        "project_root": str(project_root),
        "controller_pid": _controller_pid(args),
        "controller_session_id": _controller_session_id(args),
        "controller_label": _controller_label(args),
        "controller_identity": None,
        "worker_pid": None,
        "worker_identity": None,
        "worker_pgid": None,
        "acp_session_id": None,
        "logical_session_id": dispatch_id,
        "lease_id": None,
        "remote_lease_id": None,
        "stdout_path": str(tail),
        "stderr_path": None,
        "status_path": str(status_json),
        "os_sandbox": _os_sandbox_posture(args, worker_pid=None),
        "worker_cwd": str(_worker_cwd(args)),
        "dispatch_argv": _canonical_replay_argv(
            args,
            _raw_worker_args(args) if getattr(args, "worker", None) is not None else [],
            tail=tail,
            status_json=status_json,
        ),
        "state": "queued",
        "terminal_state": goalflight_ledger.terminal_state_for("queued"),
        "started_at": now,
        "hostname": socket.gethostname(),
    }
    record.update(_prelaunch_status_metadata(args))
    record["schema"] = goalflight_ledger.SCHEMA
    task_ids = list(getattr(args, "task_ids", []) or [])
    if task_ids:
        record["task_ids"] = task_ids
    if getattr(args, "launch_detached", False):
        record["detached"] = True
    if getattr(args, "queue_launch_token", None):
        record["queue_launch_token"] = args.queue_launch_token
    with goalflight_ledger.StateLock():
        path = goalflight_ledger.record_path(dispatch_id)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if existing.get("started_at"):
                record["started_at"] = existing["started_at"]
        goalflight_ledger.write_record(record)
    _export_dashboard_status_for_project(project_root)
    _start_dashboard_refresh_for_project(project_root)


def _record_queued_status(args, *, project_root: Path, status_json: Path, tail: Path, queue_path: Path) -> None:
    write_status(
        status_json,
        {
            **_prelaunch_status_metadata(args),
            "state": "queued",
            "reason": "dispatch_queue",
            "queue_path": str(queue_path),
            "project_root": str(project_root),
            "worker_cwd": str(_worker_cwd(args)),
            "worker_pid": None,
            "worker_alive": False,
            "tail_path": str(tail),
            "os_sandbox": _os_sandbox_posture(args, worker_pid=None),
            "updated_at": int(time.time()),
        },
    )
    _record_queued_ledger_fast(
        args,
        project_root=project_root,
        prompt_path=str(Path(args.prompt_file).expanduser()) if args.prompt_file else None,
        status_json=status_json,
        tail=tail,
    )
    _export_dashboard_status_for_project(project_root)
    _start_dashboard_refresh_for_project(project_root)


DISPATCH_REFUSED_PREFIX = "DISPATCH-REFUSED "


def _permanent_sandbox_refusal_payload(
    args, error: UnsupportedAgentSandboxRequest
) -> dict:
    return {
        "dispatch_id": getattr(args, "dispatch_id", None),
        "permanent": True,
        "reason": str(error),
        "state": "blocked_os_sandbox",
    }


def _emit_permanent_sandbox_refusal(
    args, error: UnsupportedAgentSandboxRequest
) -> None:
    """Tell drain this refusal cannot become valid by waiting."""
    print(
        DISPATCH_REFUSED_PREFIX
        + json.dumps(_permanent_sandbox_refusal_payload(args, error), sort_keys=True),
        flush=True,
    )


def _permanent_pre_spawn_refusal_reason(
    proc: subprocess.CompletedProcess,
) -> str | None:
    """Child's own permanent-refusal text, or None for a transient pre-spawn miss.

    Capacity and busy restore-and-retry. An inert flag combination cannot.
    Drain must terminalize the latter; restoring it is the b-215/b-219 stick.
    """
    blob = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    prefix = DISPATCH_REFUSED_PREFIX
    for line in blob.splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        try:
            payload = json.loads(stripped[len(prefix) :])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not payload.get("permanent"):
            continue
        reason = str(payload.get("reason") or "").strip()
        if reason:
            return reason
    return None


def _pre_spawn_launch_failure_reason(proc: subprocess.CompletedProcess) -> str:
    """Specific, bounded child diagnostic for a retryable pre-spawn failure."""
    blob = goalflight_output_redact.redact_text(
        f"{proc.stderr or ''}\n{proc.stdout or ''}"
    )
    diagnostic = ""
    for line in reversed(blob.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        if candidate.startswith("goalflight_dispatch:"):
            candidate = candidate.split(":", 1)[1].strip()
        diagnostic = " ".join(candidate.split())[:300]
        if diagnostic:
            break
    if not diagnostic:
        diagnostic = "no_child_diagnostic"
    return f"launch_refused_pre_spawn:{proc.returncode}:{diagnostic}"


def _record_unsupported_sandbox_rejection(
    args, error: UnsupportedAgentSandboxRequest
) -> int:
    """Persist an honest terminal audit row without launching or taking capacity."""
    _emit_permanent_sandbox_refusal(args, error)
    reason = str(error)
    print(f"goalflight_dispatch: {reason}", file=sys.stderr)
    base = _dispatch_base_dir()
    if not args.dispatch_id:
        args.dispatch_id = _reserve_auto_dispatch_id(args.agent, base)
    try:
        _refuse_reused_dispatch_id_for_launch(
            args.dispatch_id,
            allow_queued=bool(
                getattr(args, "from_queue", False) or getattr(args, "submit", False)
            ),
        )
        tail = Path(args.tail) if args.tail else base / f"{args.dispatch_id}.tail"
        status_json = (
            Path(args.status_json)
            if args.status_json
            else base / f"{args.dispatch_id}.status.json"
        )
        project_root = _project_root(args)
        posture = _os_sandbox_posture(args, worker_pid=None)
        write_status(
            status_json,
            {
                "dispatch_id": args.dispatch_id,
                "agent": args.agent,
                "state": "blocked_os_sandbox",
                "reason": reason,
                "project_root": str(project_root),
                "worker_pid": None,
                "worker_alive": False,
                "tail_path": str(tail),
                "os_sandbox": posture,
                "updated_at": int(time.time()),
            },
        )
        _record_ledger(
            args,
            project_root=project_root,
            prompt_path=(
                str(Path(args.prompt_file).expanduser()) if args.prompt_file else None
            ),
            status_json=status_json,
            tail=tail,
            lease_id=None,
            worker_pid=None,
            state="blocked_os_sandbox",
        )
        _finish_ledger(args.dispatch_id, "blocked_os_sandbox", reason, elapsed_s=0.0)
        _export_dashboard_status_for_project(project_root)
    except (DispatchUsageError, OSError, RuntimeError):
        # Token + stderr already carry the child's refusal. Drain still
        # terminalizes the claim from DISPATCH-REFUSED even if this audit row
        # cannot land (queued id already present, journal busy, ...).
        return 64
    return 64


def _argv_split_worker_remainder(argv: list[str]) -> tuple[list[str], list[str]]:
    try:
        split = argv.index("--")
    except ValueError:
        split = len(argv)
    return list(argv[:split]), list(argv[split:])


def _insert_before_worker_remainder(argv: list[str], additions: list[str]) -> list[str]:
    head, tail = _argv_split_worker_remainder(argv)
    return head + list(additions) + tail


def _option_skip_count(head: list[str], index: int, flag: str, *, takes_value: bool) -> int:
    token = head[index]
    if token.startswith(flag + "="):
        return 1
    if token != flag:
        return 1
    if not takes_value:
        return 1
    if index + 1 < len(head):
        return 2
    return 1


def _drop_option_before_worker_remainder(
    argv: list[str],
    flag: str,
    *,
    takes_value: bool,
) -> list[str]:
    head, tail = _argv_split_worker_remainder(argv)
    out: list[str] = []
    index = 0
    while index < len(head):
        token = head[index]
        if token == flag or token.startswith(flag + "="):
            index += _option_skip_count(head, index, flag, takes_value=takes_value)
            continue
        out.append(token)
        index += 1
    return out + tail


def _remove_flag_before_worker_remainder(argv: list[str], flag: str) -> list[str]:
    return _drop_option_before_worker_remainder(argv, flag, takes_value=False)


def _set_option_before_worker_remainder(argv: list[str], flag: str, value: str) -> list[str]:
    argv = _drop_option_before_worker_remainder(argv, flag, takes_value=True)
    return _insert_before_worker_remainder(argv, [flag, str(value)])


def _option_value_before_worker_remainder(argv: list[str], flag: str) -> str | None:
    head, _tail = _argv_split_worker_remainder(argv)
    prefix = flag + "="
    index = 0
    while index < len(head):
        token = head[index]
        if token == flag:
            if index + 1 < len(head):
                return head[index + 1]
            return None
        if token.startswith(prefix):
            return token[len(prefix) :]
        index += 1
    return None


# Classification of every launch-parser option string. Preserve-class flags are
# carried by reconstructing from the recorded invocation. Replace/inject/strip
# are the per-attempt exceptions. A new add_argument must land in exactly one
# class or test_dispatch_argv_fidelity.test_every_launch_flag_is_classified fails.
LAUNCH_ARGV_CLASS: dict[str, str] = {
    "--agent": "preserve",
    "--prompt-file": "preserve",
    "--prompt": "preserve",
    "--task": "preserve",
    "--cwd": "preserve",
    "--worktree": "preserve",
    "--model": "preserve",
    "--read-only": "preserve",
    "--readonly": "preserve",
    "--os-sandbox": "preserve",
    "--priority": "preserve",
    "--fast": "preserve",
    "--web-research-ok": "preserve",
    "--web-qa": "preserve",
    "--ignore-git-warn": "preserve",
    "--no-orientation": "preserve",
    "--capacity-wait-s": "preserve",
    "--account": "preserve",
    "--billing": "preserve",
    "--shape": "preserve",
    "--interactive": "preserve",
    "--permission-mode": "preserve",
    "--permission-dir": "preserve",
    "--permission-inline-timeout-s": "preserve",
    "--permission-user-timeout-s": "preserve",
    "--permission-allow-tool-title-pattern": "preserve",
    "--poll-secs": "preserve",
    "--max-idle-secs": "preserve",
    "--controller-pid": "preserve",
    "--controller-label": "preserve",
    "--session-label": "preserve",
    "--unregistered-forced": "preserve",
    "--occupied-worktree-forced": "preserve",
    "--controller-beacon-pid": "preserve",
    "--controller-session-id": "preserve",
    "--parent-dispatch-id": "preserve",
    "--engine-session-id": "preserve",
    "--codex-session-id": "preserve",
    "--codex-resume-home": "preserve",
    "--codex-home-owner-dispatch-id": "preserve",
    "--dispatch-id": "replace",
    "--tail": "replace",
    "--status-json": "replace",
    "--submit": "strip",
    "--foreground": "strip",
    "--drain-on-submit": "strip",
    "--no-drain-on-submit": "strip",
    "--takeover": "strip",
    "--acp-detached-child": "strip",
    "--from-queue": "inject",
    "--queue-launch-token": "inject",
    "--queue-claim-path": "inject",
    "--launch-detached": "inject",
    "--stats": "ignore",
    "--json": "ignore",
    "--hints": "ignore",
    "--help": "ignore",
    "-h": "ignore",
}

_REPLAY_VALUE_OPTIONS = {
    "--agent",
    "--prompt-file",
    "--prompt",
    "--task",
    "--cwd",
    "--worktree",
    "--model",
    "--os-sandbox",
    "--priority",
    "--capacity-wait-s",
    "--account",
    "--billing",
    "--shape",
    "--permission-mode",
    "--permission-dir",
    "--permission-inline-timeout-s",
    "--permission-user-timeout-s",
    "--permission-allow-tool-title-pattern",
    "--poll-secs",
    "--max-idle-secs",
    "--controller-pid",
    "--controller-label",
    "--session-label",
    "--controller-beacon-pid",
    "--controller-session-id",
    "--parent-dispatch-id",
    "--engine-session-id",
    "--codex-session-id",
    "--codex-resume-home",
    "--codex-home-owner-dispatch-id",
    "--dispatch-id",
    "--tail",
    "--status-json",
    "--queue-launch-token",
    "--queue-claim-path",
}


def _reconstruct_launch_argv(
    recorded: list[str],
    *,
    replace: dict[str, str] | None = None,
    inject: list[str] | None = None,
    strip_flags: tuple[str, ...] = (),
    strip_options: tuple[str, ...] = (),
) -> list[str]:
    """Rebuild a launch argv from a recorded invocation.

    Preserve everything that is not stripped or replaced. Per-attempt identity
    (dispatch-id, tail, status-json) is replaced by the caller. Queue control
    flags are injected by the drain path, not inherited.

    Classification in LAUNCH_ARGV_CLASS is authoritative: strip/inject/ignore
    flags are dropped from the recorded argv before any caller inject/replace.
    Drain therefore cannot double-inject on replay.
    """
    argv = list(recorded)
    classified_strip = _replay_strip_flags()
    seen: set[str] = set()
    for flag in (*classified_strip, *strip_flags):
        if flag in seen:
            continue
        seen.add(flag)
        argv = _drop_option_before_worker_remainder(
            argv,
            flag,
            takes_value=flag in _REPLAY_VALUE_OPTIONS or flag == "--stats",
        )
    for flag in strip_options:
        argv = _drop_option_before_worker_remainder(argv, flag, takes_value=True)
    for flag, value in (replace or {}).items():
        argv = _set_option_before_worker_remainder(argv, flag, value)
    if inject:
        argv = _insert_before_worker_remainder(argv, list(inject))
    return argv


def _replay_strip_flags() -> tuple[str, ...]:
    return tuple(
        flag
        for flag, cls in LAUNCH_ARGV_CLASS.items()
        if cls in {"strip", "inject", "ignore"}
    )


def _canonical_replay_argv_from_original(
    args,
    raw_argv: list[str],
    *,
    tail: Path,
    status_json: Path,
) -> list[str]:
    source = list(getattr(args, "_original_argv", None) or [])
    replace = {
        "--dispatch-id": str(args.dispatch_id),
        "--cwd": str(_worker_cwd(args)),
        "--tail": str(tail),
        "--status-json": str(status_json),
    }
    if args.prompt_file:
        replace["--prompt-file"] = str(Path(args.prompt_file).expanduser())
    if getattr(args, "shape", None) and args.shape != "auto":
        replace["--shape"] = str(args.shape)
    argv = _reconstruct_launch_argv(
        source,
        replace=replace,
        strip_flags=_replay_strip_flags(),
    )
    controller_label = _controller_label(args)
    controller_beacon_pid = _controller_pid(args)
    controller_session_id = _controller_session_id(args)
    if controller_label is not None:
        argv = _set_option_before_worker_remainder(
            argv, "--controller-label", controller_label
        )
    if controller_beacon_pid is not None:
        argv = _set_option_before_worker_remainder(
            argv, "--controller-beacon-pid", str(controller_beacon_pid)
        )
    if controller_session_id is not None:
        argv = _set_option_before_worker_remainder(
            argv, "--controller-session-id", controller_session_id
        )
    if getattr(args, "unregistered_forced", False):
        if "--unregistered-forced" not in argv:
            argv = _insert_before_worker_remainder(argv, ["--unregistered-forced"])
    if getattr(args, "occupied_worktree_forced", False):
        if "--occupied-worktree-forced" not in argv:
            argv = _insert_before_worker_remainder(argv, ["--occupied-worktree-forced"])
    if raw_argv and "--" not in argv:
        argv += ["--", *raw_argv]
    return argv


def _canonical_replay_argv(args, raw_argv: list[str], *, tail: Path, status_json: Path) -> list[str]:
    if getattr(args, "_original_argv", None):
        return _canonical_replay_argv_from_original(
            args, raw_argv, tail=tail, status_json=status_json
        )
    argv = [
        "--agent", str(args.agent),
        "--dispatch-id", str(args.dispatch_id),
        "--cwd", str(_worker_cwd(args)),
        "--shape", str(args.shape),
        "--priority", str(args.priority),
        "--billing", str(args.billing),
        "--poll-secs", str(args.poll_secs),
        "--max-idle-secs", str(args.max_idle_secs),
        "--tail", str(tail),
        "--status-json", str(status_json),
    ]
    if args.prompt_file:
        argv += ["--prompt-file", str(Path(args.prompt_file).expanduser())]
    if args.prompt is not None:
        argv += ["--prompt", str(args.prompt)]
    if getattr(args, "task_ids", None):
        argv += ["--task", ",".join(args.task_ids)]
    if args.model:
        argv += ["--model", str(args.model)]
    if getattr(args, "os_sandbox", None):
        argv += ["--os-sandbox", str(args.os_sandbox)]
    elif args.read_only:
        argv.append("--read-only")
    if getattr(args, "fast", False):
        argv.append("--fast")
    if args.web_research_ok:
        argv.append("--web-research-ok")
    if getattr(args, "web_qa", False):
        argv.append("--web-qa")
    if args.ignore_git_warn:
        argv.append("--ignore-git-warn")
    if getattr(args, "no_orientation", False):
        argv.append("--no-orientation")
    worktree_base = _requested_worktree_base(args)
    if worktree_base:
        argv += ["--worktree", worktree_base]
    if args.capacity_wait_s is not None:
        argv += ["--capacity-wait-s", str(args.capacity_wait_s)]
    if args.account:
        argv += ["--account", str(args.account)]
    if args.interactive:
        argv.append("--interactive")
    if args.permission_mode:
        argv += ["--permission-mode", str(args.permission_mode)]
    if args.permission_dir:
        argv += ["--permission-dir", str(args.permission_dir)]
    if args.permission_inline_timeout_s is not None:
        argv += ["--permission-inline-timeout-s", str(args.permission_inline_timeout_s)]
    if args.permission_user_timeout_s is not None:
        argv += ["--permission-user-timeout-s", str(args.permission_user_timeout_s)]
    for pattern in getattr(args, "permission_allow_tool_title_pattern", None) or []:
        argv += ["--permission-allow-tool-title-pattern", str(pattern)]
    # Replay presents the controller capability plus its measured beacon
    # identity.  The child process's own PID never becomes the lease principal.
    controller_label = _controller_label(args)
    controller_beacon_pid = _controller_pid(args)
    controller_session_id = _controller_session_id(args)
    if controller_label is not None:
        argv += ["--controller-label", controller_label]
    if controller_beacon_pid is not None:
        argv += ["--controller-beacon-pid", str(controller_beacon_pid)]
    if controller_session_id is not None:
        argv += ["--controller-session-id", controller_session_id]
    if getattr(args, "unregistered_forced", False):
        argv.append("--unregistered-forced")
    if getattr(args, "occupied_worktree_forced", False):
        argv.append("--occupied-worktree-forced")
    engine_session_id = _resolved_engine_session_id(args)
    if engine_session_id is not None:
        argv += ["--engine-session-id", engine_session_id]
    if getattr(args, "parent_dispatch_id", None):
        argv += ["--parent-dispatch-id", str(args.parent_dispatch_id)]
    if raw_argv:
        argv += ["--", *raw_argv]
    return argv


def _existing_queue_entry_paths(queue_path: Path) -> list[Path]:
    """Live envelope and claim paths. Unreadable parent is not empty."""
    parent = queue_path.parent
    try:
        entries = list(parent.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise OSError(
            f"queue dir unreadable while looking for {queue_path.name}: "
            f"{type(exc).__name__}"
        ) from exc
    name = queue_path.name
    paths: list[Path] = []
    for path in sorted(entries):
        if path.name == name:
            paths.append(path)
        elif path.name.startswith(f"{name}.claimed-") and not path.name.endswith(
            ".failed"
        ):
            paths.append(path)
    return paths


def _queue_entry_counts_as_active(path: Path, entry: dict) -> bool:
    if path.name.endswith(".failed"):
        return False
    state = entry.get("state")
    return state in (None, "queued", "claimed")


def _cleanup_partial_submit(queue_path: Path, status_json: Path) -> None:
    prompt_sidecar = _acp_watcher_prompt_path(status_json)
    with contextlib.suppress(OSError):
        queue_path.unlink()
    with contextlib.suppress(OSError):
        # Retire the sidecar first: a crash may leave status + sidecar paired,
        # but must not leave prompt material orphaned after status disappears.
        prompt_sidecar.unlink()
    with contextlib.suppress(OSError):
        status_json.unlink()
    if prompt_sidecar.exists() and not status_json.exists():
        # Re-pair a pre-existing orphan, or retry a transient first unlink.
        with contextlib.suppress(OSError):
            prompt_sidecar.unlink()
    with contextlib.suppress(OSError):
        status_json.with_suffix(status_json.suffix + ".tmp").unlink()
    # glob-empty here only skips leftover ``*.tmp.*`` cleanup; it does not
    # delete live envelopes. Unreadable parent leaves tmps in place.
    for tmp in queue_path.parent.glob(f"{queue_path.name}.tmp.*"):
        with contextlib.suppress(OSError):
            tmp.unlink()


_TEST_RELEASE_TIMEOUT_S = 30.0


def _test_signal_file(env_name: str, value: str) -> Path | None:
    if os.environ.get("GOALFLIGHT_TEST_MODE") != "1":
        return None
    raw = os.environ.get(env_name)
    if not raw:
        return None
    path = Path(raw)
    path.write_text(value, encoding="utf-8")
    return path


def _test_wait_for_release(
    *,
    ready_env: str,
    release_env: str,
    label: str,
    ready_value: str | None = None,
) -> bool:
    if os.environ.get("GOALFLIGHT_TEST_MODE") != "1":
        return False
    release_raw = os.environ.get(release_env)
    if not release_raw:
        return False
    ready_path = _test_signal_file(ready_env, ready_value if ready_value is not None else label)
    if ready_path is None:
        raise RuntimeError(f"{label} test release barrier missing ready path")
    release_path = Path(release_raw)
    deadline = time.monotonic() + _TEST_RELEASE_TIMEOUT_S
    while not release_path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{label} test release barrier timed out after {_TEST_RELEASE_TIMEOUT_S:.0f}s; "
                f"ready={ready_path} release={release_path}"
            )
        time.sleep(0.01)
    return True


def _test_submit_status_delay() -> None:
    if _test_wait_for_release(
        ready_env="GOALFLIGHT_TEST_SUBMIT_STATUS_READY_FILE",
        release_env="GOALFLIGHT_TEST_SUBMIT_STATUS_RELEASE_FILE",
        label="submit status",
    ):
        return
    raw = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_TEST_SUBMIT_STATUS_DELAY_S",
        "",
        test_mode=True,
    )
    if not raw:
        return
    try:
        delay_s = float(raw)
    except ValueError:
        return
    if delay_s > 0:
        time.sleep(delay_s)


def _drain_on_submit(args, queue_path: Path) -> None:
    if not getattr(args, "drain_on_submit", True):
        return
    drain_args = argparse.Namespace(
        queue_dir=str(queue_path.parent),
        capacity_wait_s=0.0,
        claim_stale_s=QUEUE_CLAIM_STALE_S,
        limit=1,
    )
    try:
        # Fold other dispatches' no-op claim alerts into one summary line. A
        # submit walks the whole shared queue, and a wall of rows the caller
        # cannot act on is what makes this output easy to ignore.
        with _claim_alert_focus(str(getattr(args, "dispatch_id", "") or "")):
            payload = _drain_queue_once(drain_args)
    except Exception as exc:
        print(
            "goalflight_dispatch: drain-on-submit warning: "
            f"{type(exc).__name__}: {exc}; queued request remains durable (recoverable on a later drain pass)",
            file=sys.stderr,
        )
        return
    if int(payload.get("failed") or 0) > 0:
        print(
            "goalflight_dispatch: drain-on-submit warning: "
            f"{payload.get('failed')} drain failure(s); queued request remains durable (recoverable on a later drain pass)",
            file=sys.stderr,
        )
    _report_why_this_entry_did_not_launch(args, payload)
    _warn_if_stranded_without_drainer(queue_path)


# Reasons that mean the drain CONFIRMED a launch even though the entry's state
# is not "launched".
#
# Both are emitted inside `if ledger_confirmed:` after a token-matched record,
# immediately after `_alert_launched_carrier_pending`. They are held out of the
# `launched` COUNTER only because the claim carrier could not be cleared — that
# counter means "launched AND carrier resolved" — but the worker is running
# either way, and the carrier itself is already reported by that alert.
#
# Grounded in the drain's control flow, NOT in how the reasons are spelled. A
# name-based rule was tried here first ("launched_" reads as a completed launch,
# "launch_" as an attempt) and it had a hole exactly where the real bug was:
# `worker_record_present_carrier_cleanup_pending` is ledger-confirmed and
# contains no form of the word "launch". The convention was invented rather than
# observed — `goalflight_fleet_watch` uses `launch_recovered` and
# `launch_receipted` for successes — so a test enforcing it certified the
# invention instead of the code.
_DRAIN_CONFIRMED_LAUNCH_REASONS = frozenset({
    "launched_carrier_cleanup_pending",
    "worker_record_present_carrier_cleanup_pending",
})


def _drain_decision_detail(dispatch_id: str, decision: dict) -> dict:
    """Render a restore/completion decision onto the drain details path."""
    reason = str(decision.get("reason") or "completion_authority")
    state = str(decision.get("state") or "complete")
    if _completion_decision_is_deferred(decision):
        state = "claimed"
    detail = {
        "dispatch_id": dispatch_id,
        "state": state,
        "reason": reason,
    }
    detail.update(_completion_authority_park_fields(reason))
    return detail


def _drain_detail_is_a_confirmed_launch(entry: dict) -> bool:
    """True when THIS launch attempt is confirmed running.

    Every shape admitted here is bound to the current attempt by a
    token-matched ledger record, which is what makes suppressing its message
    safe.

    SUCCESS TERMINAL STATES ARE DELIBERATELY EXCLUDED, and the reason is worth
    keeping. A revision of this function also silenced `complete`/`released`,
    on the grounds that several detail sites copy a completion decision's
    `state` verbatim and a finished dispatch should not be called "not
    launched". That was unsafe: dispatch ids are reusable once terminal, and
    completion authority resolves a terminal record by dispatch id WITHOUT
    comparing `queue_launch_token`, so a `complete` from an EARLIER attempt can
    be attached to a new one. Suppressing it would turn a pre-spawn refusal of
    the current attempt into total silence — trading a misleading message for a
    vanished failure, which is the worse direction.

    The residual is known and NOT merely cosmetic, so do not "tidy" it by
    suppressing terminal states here. When a reused id refuses before its new
    ledger row is written, stale completion authority can restore-and-unlink
    the CURRENT carrier while the earlier terminal record still reads as
    authoritative. Printing "not launched: existing_terminal_record" is then
    the only surviving signal that something is wrong; silencing it would leave
    an unattended controller believing the current work was resolved. The real
    fix is to bind completion authority to `queue_launch_token` — a separate
    change with its own contract, since the recovery tests already require a
    token match before treating a terminal record as current evidence.
    """
    return (
        str(entry.get("state") or "") == "launched"
        or str(entry.get("reason") or "") in _DRAIN_CONFIRMED_LAUNCH_REASONS
    )


def _report_why_this_entry_did_not_launch(args, payload: dict) -> None:
    """Say WHY this dispatch is still queued, not merely that it is.

    The drain already computes a per-entry reason and returns it; printing only a
    failure count throws that away, leaving a controller unable to tell a
    capacity wait from a permanently parked entry without importing this module
    and calling the drain by hand. Observed 2026-08-24: three dispatches sat at
    `queued` behind `active_queue_carrier` — claim carriers whose claimant pids
    were all dead — and the submit output said only "1 drain failure(s)".

    Only this dispatch's own reason is printed. Other entries' reasons belong to
    whoever submitted them, and a wall of them is what makes the existing
    CLAIM-RECOVERY-ALERT output easy to ignore.
    """
    dispatch_id = str(getattr(args, "dispatch_id", "") or "").strip()
    if not dispatch_id:
        return
    # The per-entry list is `details`. An earlier version of this read a
    # `skipped` key that the payload has never contained, so it reported nothing
    # and the diagnostic was inert while looking present — worse than absent,
    # because silence then reads as "no reason to give". Its tests passed only
    # because they built the payload from the same wrong assumption.
    for entry in payload.get("details") or []:
        if str(entry.get("dispatch_id") or "") != dispatch_id:
            continue
        if _drain_detail_is_a_confirmed_launch(entry):
            # This entry LAUNCHED. Saying "not launched" here states the
            # opposite of what happened, which is worse than the silence this
            # function was written to replace: a controller reads it as a
            # refusal and goes looking for a blockage that does not exist.
            # Observed 2026-08-24 — a probe dispatch printed "not launched:
            # unspecified" and was running as pid 83652 at the time.
            return
        reason = str(entry.get("reason") or "unspecified")
        if reason == "not_before":
            detail = "; ".join(
                str(part)
                for part in (entry.get("not_before"), entry.get("headroom"))
                if part
            )
        else:
            detail = (
                entry.get("process_evidence")
                or entry.get("not_before")
                or entry.get("detail")
                or entry.get("unpark_event")
                or entry.get("park_exit_state")
                or ""
            )
        suffix = f" [{detail}]" if detail else ""
        print(
            f"goalflight_dispatch: {dispatch_id} not launched: {reason}{suffix}",
            file=sys.stderr,
        )
        return


def _warn_if_stranded_without_drainer(queue_path: Path) -> None:
    """Warn when THIS request is still queued and nothing will launch it.

    Ordering is load-bearing: this runs AFTER the entry is lodged and after the
    drain-on-submit pass. Checking earlier is useless — on an idle queue the
    depth is 0, so a queue-depth guard suppresses exactly the warning that
    matters. Evaluated here, `queue_path.exists()` is a precise "my entry is
    still queued" signal: a successful drain claims the file (renames it), so a
    surviving file means nothing launched it.

    Field evidence (2026-07-20): the launchd drainer was absent, three dispatches
    parked silently, and the pull-only status WARN never reached the controller —
    who was standing right here with the context to fix it.
    """
    if not queue_path.exists():
        return  # drain claimed it — nothing stranded
    try:
        import goalflight_status

        if goalflight_status._drainer_live():
            return  # a drainer exists; it will pick this up on its next pass
    except Exception:
        return  # never let an advisory check break a dispatch
    root = _skill_root()
    dispatch_py = root / "scripts" / "goalflight_dispatch.py"
    drainer_sh = root / "scripts" / "install-drainer.sh"
    print(
        "goalflight_dispatch: WARNING — request is STILL QUEUED after the "
        "drain-on-submit pass and no live drainer was detected, so nothing will "
        "launch it. (A peer drainer may still claim it momentarily.) Remedy:\n"
        f"  python3 {shlex.quote(str(dispatch_py))} drain --json   # launch it now\n"
        f"  bash {shlex.quote(str(drainer_sh))}                       # restore the standing drainer",
        file=sys.stderr,
    )


def _queue_launch_token(entry: dict | None = None) -> str:
    if isinstance(entry, dict):
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        dispatch_id = str(entry.get("dispatch_id") or "")
        project_root = entry.get("project_root") or request.get("cwd")
        if dispatch_id and project_root:
            resolved_root = goalflight_task.resolve_project_root(str(project_root))
            try:
                # Peek-only reuse of a still-PREPARED attempt. Read errors must
                # still escape before the carrier is claimed; open_reader does.
                # Only JournalDisappeared is absent; unreadable is JournalIOError.
                attempt = goalflight_journal.Journal.open_reader(
                    resolved_root
                ).attempt_for_dispatch(dispatch_id)
            except goalflight_journal.JournalDisappeared:
                attempt = None
            if (
                attempt is not None
                and attempt.lifecycle_state == goalflight_journal.ATTEMPT_PREPARED
            ):
                # A queue child refused before spawn. The durable claim
                # was restored, but its journal preparation is still the
                # same attempt. Reuse that fencing token so the next
                # drain can continue PREPARED -> STARTING. A journal read
                # error must escape before the carrier is claimed.
                return attempt.launch_token
    return uuid.uuid4().hex


def _queue_launch_token_from_entry(entry: dict) -> str | None:
    token = entry.get("queue_launch_token")
    if not token:
        return None
    return str(token)


def _queue_claim_launch_started(entry: dict) -> bool:
    return bool(entry.get("queue_launch_started") or entry.get("queue_launch_started_at"))


def _queue_claim_worker_spawned(entry: dict) -> bool:
    return bool(entry.get("queue_worker_pid") or entry.get("queue_worker_spawned_at"))


def _queue_claim_worker_spawn_intent(entry: dict) -> bool:
    return bool(entry.get("queue_worker_spawn_intent") or entry.get("queue_worker_spawn_intent_at"))


# Removed _queue_claim_launcher_alive: boolean liveness hid tri-state identity evidence required by reconciliation.


def _queue_claim_identity_status(
    pid_value: object,
    identity: object,
) -> tuple[str, str]:
    """Return (status, reason) for claim launcher/worker liveness.

    status is one of: live | dead | indeterminate | no_pid.
    Only confirmed-dead (or pid-reused) authorizes terminalization/relaunch;
    identity_indeterminate and identity-provider exceptions must preserve and
    alert (b-065 amendment C) — never map unknown/exception → dead.
    """
    try:
        pid = int(pid_value or 0)
    except (TypeError, ValueError):
        return "no_pid", "no_pid"
    if pid <= 0:
        return "no_pid", "no_pid"
    try:
        ok, reason = goalflight_ledger.identity_matches(
            {
                "worker_pid": pid,
                "worker_identity": identity if isinstance(identity, dict) else {},
            }
        )
    except Exception as exc:
        # Provider failure is indeterminate whether or not the PID is currently
        # alive — a transient probe error must never authorize terminalization.
        return "indeterminate", f"identity_provider_exception:{type(exc).__name__}"
    if reason == "identity_indeterminate" or str(reason).startswith("identity_check_error"):
        return "indeterminate", reason
    if ok:
        return "live", reason
    if reason in {"dead", "no_pid"} or str(reason).startswith("pid_reused"):
        return "dead", reason
    # Unknown identity story — preserve + alert, never treat as confirmed-dead.
    return "indeterminate", reason or "identity_unknown"


# Removed _queue_claim_worker_alive: boolean liveness treated indeterminate identity as dead instead of evidence.


def _restore_prepared_owner_state(entry: dict, record: dict | None) -> tuple[str, str]:
    """Tri-state liveness of the controller generation owning a restore_prepared entry.

    Returns ``(state, reason)`` with state in {"live", "dead", "unknown"}.
    The entry envelope itself carries no owner identity (the restore sanitize
    strips claimer fields), so the proof channel is the ledger record's
    ``controller_pid``/``controller_identity`` — the generation that submitted
    and owns this dispatch. Adjudication REUSES the claim-identity classifier,
    so proof-of-death is exactly as strict as everywhere else: only a gone pid
    or a start-token/lstart mismatch reports "dead". Anything that cannot be
    determined — no ledger record, no controller pid, no stored identity, a
    probe error — is "unknown", and unknown HOLDS: it is never evidence for
    expiry or attention.
    """
    if not isinstance(record, dict):
        return "unknown", "no_ledger_record"
    pid = record.get("controller_pid")
    identity = record.get("controller_identity")
    status, reason = _queue_claim_identity_status(pid, identity)
    if status == "dead":
        # A gone pid is proof of death even without a stored token.
        return "dead", reason
    if not isinstance(identity, dict) or not str(identity.get("start_token") or ""):
        # No stored generation token: a live pid cannot be told from a
        # recycled one — the same standard the claim-marker adjudicator
        # applies ("claim_identity_unavailable"). Only the dead branch above
        # is proof without it.
        return "unknown", "no_controller_pid" if status == "no_pid" else "controller_identity_unavailable"
    if status == "live":
        return "live", reason
    # Indeterminate probes fail closed to unknown: hold, never expire.
    return "unknown", reason


def _queue_claim_worker_status(entry: dict) -> tuple[str, str]:
    return _queue_claim_identity_status(
        entry.get("queue_worker_pid"),
        entry.get("queue_worker_identity"),
    )


def _queue_claim_launcher_status(entry: dict) -> tuple[str, str]:
    return _queue_claim_identity_status(
        entry.get("queue_launcher_pid"),
        entry.get("queue_launcher_identity"),
    )


def _entry_transport(entry: dict, record: dict | None = None) -> str:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    raw = str(
        entry.get("transport")
        or (record or {}).get("transport")
        or request.get("transport")
        or entry.get("shape")
        or request.get("shape")
        or "bash"
    ).lower()
    if raw in {"fleet", "fleet-ssh", "ssh", "remote"}:
        return "fleet"
    return "acp" if raw == "acp" or str(entry.get("shape") or "").lower() == "acp" else "bash"


def _entry_node(entry: dict, record: dict | None = None) -> str | None:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    raw = entry.get("node") or entry.get("remote_node") or (record or {}).get("node") or request.get("remote_node")
    return str(raw) if raw else None


def _entry_with_record_identity(
    entry: dict,
    record: dict | None,
    *,
    prefer_record: bool = False,
) -> dict:
    merged = dict(entry)
    if not isinstance(record, dict):
        return merged
    if entry.get("queue_launch_token") and record.get("queue_launch_token") not in {
        None,
        entry.get("queue_launch_token"),
    }:
        if prefer_record:
            merged["queue_launch_token"] = record.get("queue_launch_token")
        return merged
    mapping = {
        "queue_worker_pid": "worker_pid",
        "queue_worker_identity": "worker_identity",
        "queue_worker_pgid": "worker_pgid",
        "queue_worker_group_leader_identity": "worker_group_leader_identity",
        "queue_worker_identity_snapshot_at": "worker_identity_snapshot_at",
        "queue_producer_descendants": "producer_descendants",
    }
    for target, source in mapping.items():
        if (prefer_record or not merged.get(target)) and record.get(source) is not None:
            merged[target] = record[source]
    if prefer_record and record.get("queue_launch_token") is not None:
        merged["queue_launch_token"] = record.get("queue_launch_token")
    if merged.get("queue_worker_pgid"):
        merged.setdefault("queue_producer_group_contract", bool(record.get("producer_group_contract")))
        merged.setdefault(
            "queue_producer_group_contract_enforced",
            bool(record.get("producer_group_contract_enforced")),
        )
    return merged


def _process_snapshot() -> list[dict] | None:
    """Return one bounded process-table snapshot, or None when not authoritative."""
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    rows: list[dict] = []
    for raw in proc.stdout.splitlines():
        parts = raw.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, ppid, pgid = (int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            continue
        rows.append({"pid": pid, "ppid": ppid, "pgid": pgid, "command": parts[3] if len(parts) > 3 else ""})
    return rows


def _persisted_descendant_identities(entry: dict) -> list[dict]:
    raw = entry.get("queue_producer_descendants")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def enumerate_token_producers(
    entry: dict,
    process_snapshot: list[dict] | None = None,
) -> ProducerSetResult:
    """Conservatively classify the complete launch-token-bound producer set."""
    if not _queue_launch_token_from_entry(entry):
        return ProducerSetResult(ProducerSetState.INDETERMINATE, reason="missing_launch_token")
    if not bool(entry.get("queue_producer_group_contract")) or not bool(
        entry.get("queue_producer_group_contract_enforced")
    ):
        return ProducerSetResult(ProducerSetState.INDETERMINATE, reason="missing_group_contract")
    try:
        pgid = int(entry.get("queue_worker_pgid") or 0)
    except (TypeError, ValueError):
        pgid = 0
    if pgid <= 0:
        return ProducerSetResult(ProducerSetState.INDETERMINATE, reason="missing_worker_pgid")

    probes: list[tuple[str, str]] = []
    if entry.get("queue_launcher_pid"):
        probes.append(_queue_claim_launcher_status(entry))
    if entry.get("queue_worker_pid"):
        probes.append(_queue_claim_worker_status(entry))
    else:
        return ProducerSetResult(ProducerSetState.INDETERMINATE, reason="post_spawn_worker_pid_missing")
    descendants = _persisted_descendant_identities(entry)
    group_leader_identity = entry.get("queue_worker_group_leader_identity")
    if not isinstance(group_leader_identity, dict):
        return ProducerSetResult(ProducerSetState.INDETERMINATE, reason="missing_group_leader_identity")
    probes.append(_queue_claim_identity_status(pgid, group_leader_identity))
    for descendant in descendants:
        probes.append(
            _queue_claim_identity_status(descendant.get("pid"), descendant.get("identity"))
        )
    if any(status == "live" for status, _reason in probes):
        return ProducerSetResult(ProducerSetState.LIVE, reason="recorded_producer_live")
    if any(status == "indeterminate" for status, _reason in probes):
        return ProducerSetResult(ProducerSetState.INDETERMINATE, reason="producer_identity_indeterminate")

    snapshot = _process_snapshot() if process_snapshot is None else process_snapshot
    if snapshot is None:
        return ProducerSetResult(ProducerSetState.INDETERMINATE, reason="process_snapshot_unavailable")
    by_pid = {int(row["pid"]): row for row in snapshot if isinstance(row, dict) and row.get("pid")}
    group_rows = [row for row in snapshot if int(row.get("pgid") or 0) == pgid]
    seed_pids = {
        int(value)
        for value in (entry.get("queue_launcher_pid"), entry.get("queue_worker_pid"), pgid)
        if str(value or "").isdigit() and int(value) > 0
    }
    bound_pids = {int(row["pid"]) for row in group_rows}
    changed = True
    while changed:
        changed = False
        for row in snapshot:
            pid = int(row.get("pid") or 0)
            if pid in bound_pids:
                continue
            if int(row.get("ppid") or 0) in seed_pids | bound_pids:
                bound_pids.add(pid)
                changed = True
    if bound_pids:
        members: list[ProducerIdentity] = []
        for pid in sorted(bound_pids):
            row = by_pid.get(pid, {})
            identity = goalflight_ledger.process_identity(pid)
            if not isinstance(identity, dict) or identity.get("identity_available") is False:
                return ProducerSetResult(ProducerSetState.INDETERMINATE, reason="member_identity_unavailable")
            members.append(
                ProducerIdentity(
                    pid=pid,
                    ppid=int(row.get("ppid") or 0),
                    pgid=int(row.get("pgid") or 0),
                    command=str(row.get("command") or ""),
                    identity=identity,
                )
            )
        return ProducerSetResult(ProducerSetState.LIVE, tuple(members), "token_bound_member_live")

    reasons = [reason for status, reason in probes if status == "dead"]
    if not reasons:
        return ProducerSetResult(ProducerSetState.INDETERMINATE, reason="producer_seed_unproven")
    if all(str(reason).startswith("pid_reused") for reason in reasons):
        return ProducerSetResult(ProducerSetState.PID_REUSED, reason="whole_set_pid_reused")
    return ProducerSetResult(ProducerSetState.DEAD, reason="whole_set_dead")


def classify_reconciliation_admission(
    entry: dict,
    now_s: float,
    *,
    stale_s: float = QUEUE_CLAIM_STALE_S,
) -> PreAdmitClass:
    """Read-only layered liveness classification; elapsed time never proves death."""
    record = _find_dispatch_record(str(entry.get("dispatch_id") or ""))
    probe_entry = _entry_with_record_identity(entry, record)
    if _entry_transport(probe_entry, record) == "fleet" or _entry_node(probe_entry, record):
        return PreAdmitClass.REMOTE_AUTHORITY_REQUIRED

    launcher_status, launcher_reason = _queue_claim_launcher_status(probe_entry)
    worker_status, worker_reason = _queue_claim_worker_status(probe_entry)
    crossed_spawn = _queue_claim_worker_spawn_intent(probe_entry) or _queue_claim_worker_spawned(probe_entry)

    if launcher_status == "live" or worker_status == "live":
        return PreAdmitClass.LIVE
    if crossed_spawn and not probe_entry.get("queue_worker_pid"):
        # A detached helper may exist between durable spawn-intent and worker
        # PID publication, even after the launcher exits.
        return PreAdmitClass.INDETERMINATE
    if launcher_status == "indeterminate" or worker_status == "indeterminate":
        return PreAdmitClass.INDETERMINATE

    has_seed = any(
        probe_entry.get(key)
        for key in (
            "queue_launch_started",
            "queue_launch_started_at",
            "queue_worker_spawn_intent",
            "queue_worker_spawn_intent_at",
            "queue_worker_spawned_at",
            "queue_launcher_pid",
            "queue_worker_pid",
            "queue_worker_pgid",
        )
    )
    age_stamp = _launch_age_timestamp_s(probe_entry)
    age_s = max(0.0, float(now_s) - age_stamp) if age_stamp is not None else 0.0
    if not has_seed:
        return PreAdmitClass.STALE_NO_SPAWN if age_s >= max(0.0, stale_s) else PreAdmitClass.NOT_STALE
    reasons = [reason for status, reason in ((launcher_status, launcher_reason), (worker_status, worker_reason)) if status == "dead"]
    if not reasons:
        return PreAdmitClass.INDETERMINATE
    if all(str(reason).startswith("pid_reused") for reason in reasons):
        return PreAdmitClass.PID_REUSED
    return PreAdmitClass.CONFIRMED_DEAD


def _parse_timestamp_s(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
        except (TypeError, ValueError):
            return None
        # Accept unix seconds; reject clearly bogus values.
        if ts > 1_000_000_000:
            return ts
        return None
    if isinstance(value, str):
        # Bare integer/float strings (status updated_at style) — only used when
        # the field is a launch-progress stamp, never as the sole age authority
        # for heartbeat fields.
        stripped = value.strip()
        if stripped and stripped.replace(".", "", 1).isdigit():
            try:
                ts = float(stripped)
            except ValueError:
                ts = None
            if ts is not None and ts > 1_000_000_000:
                return ts
        parsed = goalflight_ledger.parse_utc(value)
        if parsed is not None:
            return parsed.timestamp()
    return None


def _launch_age_timestamp_s(payload: dict | None, *, carrier_mtime: float | None = None) -> float | None:
    """Newest trustworthy launch-progress time. NEVER uses updated_at (b-065 A).

    ``carrier_mtime`` is accepted for call-site compatibility but intentionally
    ignored: claim-file heartbeats rewrite mtime and would starve the 300s
    orphan clock if included in the newest-wins set. Age derives solely from
    immutable / first-seen stamps.
    """
    _ = carrier_mtime
    if not isinstance(payload, dict):
        payload = {}
    candidates: list[float] = []
    for key in (
        "queue_worker_spawned_at",
        "queue_worker_spawn_intent_at",
        "queue_launch_started_at",
        "started_at",
        "created_at",
        "orphan_first_seen_at",
    ):
        ts = _parse_timestamp_s(payload.get(key))
        if ts is not None:
            candidates.append(ts)
    # Nested request may carry started_at for legacy rows.
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    for key in ("started_at", "created_at"):
        ts = _parse_timestamp_s(request.get(key))
        if ts is not None:
            candidates.append(ts)
    if not candidates:
        return None
    return max(candidates)


def _claim_launch_age_s(entry: dict, claim: Path, *, now: float | None = None) -> float:
    """Age of a claim for orphan detection — ignores updated_at and carrier mtime."""
    _ = claim  # path kept for call-site compatibility; mtime is not an age source
    now_s = time.time() if now is None else float(now)
    stamp = _launch_age_timestamp_s(entry)
    if stamp is None:
        return 0.0
    return max(0.0, now_s - stamp)


def _entry_task_ids(entry: dict | None, record: dict | None = None) -> list[str]:
    ids: list[str] = []
    for source in (entry, record):
        if not isinstance(source, dict):
            continue
        raw = source.get("task_ids")
        if isinstance(raw, list):
            ids.extend(str(x) for x in raw if x)
        single = source.get("task_id")
        if single:
            ids.append(str(single))
        request = source.get("request") if isinstance(source.get("request"), dict) else {}
        raw_req = request.get("task_ids")
        if isinstance(raw_req, list):
            ids.extend(str(x) for x in raw_req if x)
    # Preserve order, drop empties/dupes.
    seen: set[str] = set()
    out: list[str] = []
    for item in ids:
        item = item.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _is_task_linked(entry: dict | None, record: dict | None = None) -> bool:
    return bool(_entry_task_ids(entry, record))


def _claim_recovery_count(entry: dict | None) -> int:
    if not isinstance(entry, dict):
        return 0
    try:
        return max(0, int(entry.get("claim_recovery_count") or 0))
    except (TypeError, ValueError):
        return 0


def _completion_authority_deferral_count(entry: dict | None) -> int:
    if not isinstance(entry, dict):
        return 0
    try:
        return max(0, int(entry.get("completion_authority_deferral_count") or 0))
    except (TypeError, ValueError):
        return 0


def _queue_quarantine_dir(queue_dir: Path) -> Path:
    path = queue_dir / "quarantine"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _quarantine_claim(
    claim: Path,
    entry: dict,
    *,
    reason: str,
    evidence: dict | None = None,
) -> Path | None:
    """Park an unlinked orphan claim out of the active launch glob. Never deletes."""
    queue_dir = claim.parent
    quarantine = _queue_quarantine_dir(queue_dir)
    entry = dict(entry)
    entry["state"] = "quarantined"
    entry["quarantine_reason"] = reason
    if evidence is not None:
        entry["quarantine_evidence"] = dict(evidence)
    entry["quarantined_at"] = goalflight_ledger.utc_now()
    entry["updated_at"] = entry["quarantined_at"]
    dest = quarantine / f"{claim.name}.quarantined"
    # Retry the same durable quarantine commit instead of minting duplicates
    # when the prior attempt committed the destination but crashed before
    # unlinking the claim carrier.
    if dest.exists():
        try:
            existing = json.loads(dest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        same_commit = bool(
            isinstance(existing, dict)
            and existing.get("dispatch_id") == entry.get("dispatch_id")
            and existing.get("queue_launch_token") == entry.get("queue_launch_token")
            and existing.get("quarantine_reason") == reason
        )
        if not same_commit:
            token = re.sub(r"[^A-Za-z0-9_.-]", "-", str(entry.get("queue_launch_token") or "collision"))
            dest = quarantine / f"{claim.name}.quarantined-{token[:16]}"
            if dest.exists():
                return None
    try:
        if not dest.exists():
            _write_json_atomic(dest, entry)
        claim.unlink()
    except OSError:
        return None
    _emit_claim_recovery_alert(
        {
            "dispatch_id": entry.get("dispatch_id"),
            "action": "quarantine",
            "reason": reason,
            "evidence": entry.get("quarantine_evidence"),
            "path": str(dest),
        },
    )
    return dest


def _quarantine_unlinked_claim_if_observed_orphan(
    claim: Path,
    entry: dict,
    record: dict | None,
    *,
    reason: str,
    stale_s: float,
) -> Path | None:
    """Quarantine only after durable, repeated orphan evidence.

    Missing task linkage alone is never orphan evidence: task linkage may land
    after the launch record. A live or non-terminal record therefore always
    defers. The only eligible cases are a token-matched terminal record whose
    worker identity is not live/indeterminate, or an unrecorded pre-spawn claim.
    Both must survive a first-observed orphan interval before quarantine.

    A ledger row that does not name this claim's launch token is not this
    claim's record. Restore sanitizes ``queue_launch_token`` off the queued
    row, so a later abandoned claim would otherwise bind to a different
    attempt and defer forever. No claim token while a row exists cannot
    bind either: that stays UNKNOWN and is retained.
    """
    now_s = time.time()
    claim_token = _queue_launch_token_from_entry(entry)
    record_token = (
        str(record.get("queue_launch_token") or "")
        if isinstance(record, dict)
        else None
    )
    record_for_this_claim = (
        isinstance(record, dict)
        and bool(claim_token)
        and record_token == claim_token
    )
    if _is_task_linked(entry, record if record_for_this_claim else None):
        return None

    worker_status = "no_record"
    worker_reason = "no_record"
    if record_for_this_claim:
        if not _dispatch_record_is_terminal(record):
            return None
        worker_status, worker_reason = _queue_claim_identity_status(
            record.get("worker_pid"),
            record.get("worker_identity"),
        )
        if worker_status in {"live", "indeterminate"}:
            return None
        basis = "observed_unlinked_terminal_record"
    else:
        if isinstance(record, dict) and not claim_token:
            return None
        if not _entry_pre_spawn(entry):
            return None
        if (
            classify_reconciliation_admission(entry, now_s, stale_s=stale_s)
            is not PreAdmitClass.STALE_NO_SPAWN
        ):
            return None
        basis = "observed_unlinked_stale_pre_spawn"

    orphan_stamp = _parse_timestamp_s(entry.get("orphan_first_seen_at"))
    if orphan_stamp is None:
        staged = dict(entry)
        staged["orphan_first_seen_at"] = _persist_orphan_first_seen(staged)
        staged["updated_at"] = goalflight_ledger.utc_now()
        try:
            _write_json_atomic(claim, staged)
        except OSError:
            return None
        return None

    orphan_age_s = max(0.0, now_s - orphan_stamp)
    if orphan_age_s < max(0.0, stale_s):
        return None

    evidence = {
        "basis": basis,
        "claim_orphan_first_seen_at": entry.get("orphan_first_seen_at"),
        "orphan_age_s": orphan_age_s,
        "required_stale_s": max(0.0, stale_s),
        "record_present": isinstance(record, dict),
        "record_state": record.get("state") if isinstance(record, dict) else None,
        "record_terminal_state": record.get("terminal_state") if isinstance(record, dict) else None,
        "record_token_matches_claim": bool(
            record is not None and claim_token and record_token == claim_token
        ),
        "worker_identity_status": worker_status,
        "worker_identity_reason": worker_reason,
    }
    return _quarantine_claim(
        claim,
        entry,
        reason=f"{reason}_observed_orphan",
        evidence=evidence,
    )


def _persist_orphan_first_seen(entry: dict, *, now_iso: str | None = None) -> str:
    """Pure staged value helper; caller owns any carrier/ledger write."""
    if entry.get("orphan_first_seen_at"):
        return str(entry["orphan_first_seen_at"])
    return now_iso or goalflight_ledger.utc_now()


# Which dispatch this process is submitting, while a drain pass runs.
#
# A drain walks the WHOLE shared queue, so it meets claim carriers belonging to
# every project on the box. Printing a row for each one buries the caller's own
# dispatch under a wall it cannot act on. That is the same reasoning already
# applied to per-entry drain reasons in `_report_why_this_entry_did_not_launch`:
# other entries' reasons belong to whoever submitted them.
#
# What still prints unconditionally: any alert that CHANGED state (quarantine),
# whoever owns it. Silently losing a state mutation is worse than a noisy line.
# Only pure `preserve*` no-ops -- where the drain looked, touched nothing, and
# said so -- are foldable, and only for dispatches that are not ours.
_ALERT_FOCUS: dict | None = None

# Reasons the drain emits after looking at a carrier and changing NOTHING.
# Only these fold; anything unlisted prints, so a new alert is loud by default.
#
# This is an explicit allowlist keyed on REASON, not a prefix match on the
# action string. An earlier version tested `action.startswith("preserve")`,
# which is a lexical proxy for a structural fact. It was wrong: both
# `identity_indeterminate` and `launched_carrier_cleanup_pending` are spelled
# action "preserve", but the latter is emitted after a launch ATTEMPT -- the
# carrier is untouched while a worker was actually started. Folding that would
# hide a launch. How an alert is spelled is not evidence of what the code did.
_FOLDABLE_CLAIM_ALERT_REASONS = frozenset(
    {
        "identity_indeterminate",
        "claim_carrier_missing_unlinked",
    }
)
# How many dispatch ids to keep per folded reason, so the summary stays
# self-diagnosing without growing back into a wall.
_FOLD_SAMPLE_LIMIT = 3


def _emit_claim_recovery_alert(payload: dict) -> None:
    """Print a claim alert, or fold it into this drain pass's summary."""
    focus = _ALERT_FOCUS
    reason = str(payload.get("reason") or "")
    foldable = reason in _FOLDABLE_CLAIM_ALERT_REASONS
    own = focus is not None and str(payload.get("dispatch_id") or "") in focus["own"]
    if focus is None or own or not foldable:
        print(
            "CLAIM-RECOVERY-ALERT " + json.dumps(payload, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
        return
    key = reason or "unknown"
    focus["folded"][key] = focus["folded"].get(key, 0) + 1
    sample = focus["samples"].setdefault(key, [])
    if len(sample) < _FOLD_SAMPLE_LIMIT:
        sample.append(str(payload.get("dispatch_id") or "?"))


@contextlib.contextmanager
def _claim_alert_focus(dispatch_id: str):
    """Fold foreign no-op claim alerts for the duration of one drain pass.

    A no-focus caller (the drain daemon, or a unit test driving these helpers
    directly) keeps the unfolded behaviour, so this only ever narrows what an
    interactive submit prints.
    """
    global _ALERT_FOCUS
    dispatch_id = str(dispatch_id or "").strip()
    if not dispatch_id:
        yield
        return
    if _ALERT_FOCUS is not None:
        # An outer pass already owns the fold, and it keeps owning it: an inner
        # exit publishing a partial summary would reset the counter the outer
        # pass is still accumulating into. But the inner dispatch IS ours, so
        # register it as own for the duration -- otherwise its own alerts fold
        # as if they belonged to somebody else.
        added = dispatch_id not in _ALERT_FOCUS["own"]
        if added:
            _ALERT_FOCUS["own"].add(dispatch_id)
        try:
            yield
        finally:
            if added and _ALERT_FOCUS is not None:
                _ALERT_FOCUS["own"].discard(dispatch_id)
        return
    _ALERT_FOCUS = {"own": {dispatch_id}, "folded": {}, "samples": {}}
    try:
        yield
    finally:
        focus, _ALERT_FOCUS = _ALERT_FOCUS, None
        folded = focus["folded"]
        if folded:
            print(
                "CLAIM-RECOVERY-SUMMARY "
                + json.dumps(
                    {
                        "folded": sum(folded.values()),
                        "reasons": folded,
                        # Bounded id samples, because the fold is otherwise
                        # unrecoverable. Do NOT point the reader at
                        # `drain --json` to see the rest: a drain CLAIMS queue
                        # entries and can LAUNCH WORKERS, so it is not a
                        # read-only diagnostic and would not reproduce this
                        # pass anyway.
                        "sample_ids": focus["samples"],
                        "detail": "no-op claim alerts for other dispatches",
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )


def _alert_identity_indeterminate(dispatch_id: str, *, where: str, reason: str) -> None:
    _emit_claim_recovery_alert(
        {
            "dispatch_id": dispatch_id,
            "action": "preserve",
            "reason": "identity_indeterminate",
            "where": where,
            "detail": reason,
        },
    )


def _alert_launched_carrier_pending(dispatch_id: str, *, where: str) -> None:
    _emit_claim_recovery_alert(
        {
            "dispatch_id": dispatch_id,
            "action": "preserve",
            "reason": "launched_carrier_cleanup_pending",
            "where": where,
        },
    )


def _submit_dispatch(args, raw_argv: list[str], *, base: Path) -> int:
    project_root = _project_root(args)
    submit_base_sha = _git_head_for_cwd(project_root)
    tail = Path(args.tail) if args.tail else base / f"{args.dispatch_id}.tail"
    status_json = Path(args.status_json) if args.status_json else base / f"{args.dispatch_id}.status.json"
    queue_path = _queue_entry_path(args.dispatch_id)
    dispatch_argv = _canonical_replay_argv(args, raw_argv, tail=tail, status_json=status_json)
    entry = {
        "schema": DISPATCH_QUEUE_SCHEMA,
        "state": "queued",
        "dispatch_id": args.dispatch_id,
        "agent": args.agent,
        "shape": args.shape,
        "project_root": str(project_root),
        "process_cwd": str(Path.cwd().resolve()),
        "worker_cwd": str(_worker_cwd(args)),
        "created_at": goalflight_ledger.utc_now(),
        "updated_at": goalflight_ledger.utc_now(),
        "queue_path": str(queue_path),
        "base_sha": submit_base_sha,
        "dispatch_argv": dispatch_argv,
        **({"task_ids": list(args.task_ids)} if getattr(args, "task_ids", None) else {}),
        "request": {
            "agent": args.agent,
            "prompt_file": str(Path(args.prompt_file).expanduser()) if args.prompt_file else None,
            "prompt": args.prompt,
            "task_ids": list(getattr(args, "task_ids", []) or []),
            "priority": args.priority,
            "fast": bool(getattr(args, "fast", False)),
            "dispatch_id": args.dispatch_id,
            "cwd": str(_worker_cwd(args)),
            "model": args.model,
            "shape": args.shape,
            "read_only": bool(args.read_only),
            "os_sandbox": getattr(args, "os_sandbox", None),
            "web_qa": bool(getattr(args, "web_qa", False)),
            "base_sha": submit_base_sha,
            "account": args.account,
            "billing": args.billing,
            "capacity_wait_s": args.capacity_wait_s,
            "tail": str(tail),
            "status_json": str(status_json),
            "poll_secs": args.poll_secs,
            "max_idle_secs": args.max_idle_secs,
            "permission_mode": args.permission_mode,
            "no_orientation": bool(getattr(args, "no_orientation", False)),
            "controller_label": _controller_label(args),
            "controller_beacon_pid": _controller_pid(args),
            "unregistered_forced": bool(
                getattr(args, "unregistered_forced", False)
            ),
            "raw_worker": raw_argv,
        },
    }
    duplicate_active = False
    try:
        with _queue_mutation_lock(queue_path.parent):
            existing_paths = _existing_queue_entry_paths(queue_path)
            if existing_paths:
                conflicts = []
                matches = []
                for existing_path in existing_paths:
                    try:
                        existing = json.loads(existing_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        conflicts.append(f"{existing_path.name}:unreadable:{type(exc).__name__}")
                        continue
                    if (
                        existing.get("dispatch_id") == args.dispatch_id
                        and existing.get("dispatch_argv") == dispatch_argv
                    ):
                        if _queue_entry_counts_as_active(existing_path, existing):
                            matches.append(existing_path)
                    else:
                        conflicts.append(existing_path.name)
                if matches and not conflicts:
                    duplicate_active = True
                elif matches or conflicts:
                    print(f"goalflight_dispatch: queued request already exists for {args.dispatch_id}", file=sys.stderr)
                    return 64
            if not duplicate_active:
                _write_json_atomic(queue_path, entry)
                _test_submit_status_delay()
                try:
                    _record_queued_status(
                        args,
                        project_root=project_root,
                        status_json=status_json,
                        tail=tail,
                        queue_path=queue_path,
                    )
                except (OSError, RuntimeError):
                    _cleanup_partial_submit(queue_path, status_json)
                    raise
    except (OSError, RuntimeError) as exc:
        print(
            f"goalflight_dispatch: submit failed for {args.dispatch_id}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    if duplicate_active:
        print(f"STATUS: queued already {args.dispatch_id}")
        _release_worktree_occupancy_lock(args)
        _drain_on_submit(args, queue_path)
        return 0
    print(
        "DISPATCH-QUEUED "
        + json.dumps(
            {
                "dispatch_id": args.dispatch_id,
                "agent": args.agent,
                "shape": args.shape,
                "queue_path": str(queue_path),
                "status_json": str(status_json),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    _release_worktree_occupancy_lock(args)
    _drain_on_submit(args, queue_path)
    return 0


def _attach_worker_to_lease(lease_id: str | None, worker_pid: int) -> None:
    if not lease_id:
        return
    with goalflight_capacity.StateLock():
        data = goalflight_capacity.load_state()
        lease = data.get("leases", {}).get(lease_id)
        if lease:
            lease["worker_pid"] = worker_pid
            goalflight_capacity.save_state(data)


def _detach_lease_to_worker(lease_id: str | None, worker_pid: int, reason: object) -> None:
    goalflight_capacity.detach_lease_to_worker(lease_id, worker_pid, reason)


def _pidfile_dir() -> Path:
    return Path(
        os.environ.get(
            "GOAL_FLIGHT_PIDFILE_DIR",
            goalflight_compat.temp_base() / "goal-flight-acp-pids.d",
        )
    )


def _identity_token(identity: dict | None) -> dict | None:
    if not identity:
        return None
    return {
        key: identity.get(key)
        for key in ("pid", "start_token", "lstart", "comm")
        if identity.get(key)
    }


def _watch_identity_token(identity: dict | None) -> dict | None:
    token = _identity_token(identity)
    if token and (token.get("start_token") or token.get("lstart")):
        return token
    return None


def _watcher_spawn_argv(
    *,
    worker_pid: int,
    tail: Path,
    status_json: Path,
    agent: str,
    poll_secs: float,
    max_idle_secs: float,
    dispatch_id: str,
    pgid: int,
    project_root: Path | None = None,
    worker_cwd: Path | None = None,
    task_ids: list[str] | None = None,
    worker_identity: dict | None = None,
    launch_detached: bool = False,
    codex_dispatch_home: str | None = None,
    codex_session_id: str | None = None,
    engine_session_id: str | None = None,
    parent_dispatch_id: str | None = None,
    codex_home_owner_dispatch_id: str | None = None,
    controller_pid: int | None = None,
    controller_session_id: str | None = None,
    controller_label: str | None = None,
    prompt_path: Path | None = None,
) -> list[str]:
    """Build the detached Python watcher's argv without spawning it.

    Keeping this assembly pure makes the prompt-echo exclusion a directly
    testable dispatch contract instead of an incidental branch in ``main``.
    """
    watch_cmd = [
        sys.executable,
        str(WATCH_PY),
        "--pid",
        str(worker_pid),
        "--tail",
        str(tail),
        "--status-json",
        str(status_json),
        "--agent",
        agent,
        "--poll-secs",
        str(poll_secs),
        "--max-idle-secs",
        str(max_idle_secs),
        "--dispatch-id",
        dispatch_id,
        "--pgid",
        str(pgid),
        "--stay-after-terminal",
    ]
    if project_root is not None:
        watch_cmd += ["--project-root", str(project_root)]
    if worker_cwd is not None:
        watch_cmd += ["--worker-cwd", str(worker_cwd)]
    if task_ids:
        if project_root is None:
            raise ValueError("project_root is required when task_ids are present")
        watch_cmd += [
            "--task-ids",
            ",".join(task_ids),
        ]
    watch_identity_token = _watch_identity_token(worker_identity)
    if watch_identity_token:
        watch_cmd += [
            "--worker-identity-json",
            json.dumps(watch_identity_token, sort_keys=True),
        ]
    if launch_detached:
        watch_cmd += ["--detached"]
    if codex_dispatch_home is not None:
        watch_cmd += [
            "--codex-dispatch-home-resolved",
            "--codex-dispatch-home",
            codex_dispatch_home,
        ]
    if codex_session_id is not None:
        watch_cmd += ["--codex-session-id", codex_session_id]
    if engine_session_id is not None:
        watch_cmd += ["--engine-session-id", engine_session_id]
    if parent_dispatch_id is not None:
        watch_cmd += ["--parent-dispatch-id", parent_dispatch_id]
    if codex_home_owner_dispatch_id is not None:
        watch_cmd += [
            "--codex-home-owner-dispatch-id",
            codex_home_owner_dispatch_id,
        ]
    if controller_pid is not None and controller_session_id is not None:
        watch_cmd += [
            "--controller-pid",
            str(controller_pid),
            "--controller-session-id",
            controller_session_id,
        ]
        if controller_label is not None:
            watch_cmd += ["--controller-label", controller_label]
    if prompt_path is not None:
        watch_cmd += ["--ignore-prompt-file", str(prompt_path)]
    return watch_cmd


def _process_identity_after_spawn(worker_pid: int) -> dict | None:
    # goalflight_ledger.process_identity() already performs a bounded retry
    # loop. Wrapping it in another 20-attempt loop can hold bash dispatch in
    # "starting" until short workers have already completed on platforms where
    # ps cannot provide every identity field.
    return goalflight_ledger.process_identity(worker_pid)


def _write_pidfile(
    args,
    *,
    worker_pid: int,
    pgid: int | None,
    identity: dict | None = None,
    detached: bool = False,
) -> Path | None:
    pidfile_dir = _pidfile_dir()
    pidfile_dir.mkdir(parents=True, exist_ok=True)
    ident = identity or _process_identity_after_spawn(worker_pid)
    if not ident:
        return None
    controller_pid = _controller_pid(args)
    controller_session_id = _controller_session_id(args)
    owner_key = str(controller_pid) if controller_pid is not None else "unowned"
    pidfile = pidfile_dir / f"{owner_key}.bashtail.{worker_pid}.jsonl"
    entry = {
        "controller_pid": controller_pid,
        "controller_session_id": controller_session_id,
        "controller_label": _controller_label(args),
        "pid": worker_pid,
        "pgid": int(pgid or worker_pid),
        "started_at": ident.get("lstart"),
        "cmd": ident.get("comm"),
        # The "-bash-tail" suffix is load-bearing: cleanup_ghosts() keys its
        # bash-tail branch (pgid!=pid -> kill the bare pid, not the group) on
        # ``agent.endswith("-bash-tail")``. Tag it so this dispatch's worker is
        # reachable by exactly that branch, matching the documented agent names
        # (codex-bash-tail / grok-bash-tail) in protocols/legacy/bash-tail.md.
        "agent": f"{args.agent}-bash-tail",
        "session_id": args.dispatch_id,
    }
    if detached:
        entry["detached"] = True
    pidfile.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
    return pidfile


def _reap_dead_worker_pgroup(pidfile: Path, worker_pid: int) -> None:
    """Best-effort reap of a DEAD worker's orphaned process group.

    The direct-dispatch worker runs in its own session/group
    (``_detached_popen_kwargs`` / ``start_new_session``), so a terminal marker is
    non-destructive: a worker that is still ALIVE is left untouched for re-attach.
    But once the worker (the group leader) has EXITED, any children that lingered
    in its group are orphans with no reaper -- and unlinking the pidfile (below)
    would discard the only record of their pgid, so even the opportunistic
    ``cleanup_ghosts`` sweep could never reach them. So, only when the leader is
    already dead, killpg the recorded group to clean them up first. Children that
    escaped to a NEW session (e.g. a helper that called ``setsid`` and reparented
    to launchd/init) are not in this group and remain an inherent limit; the
    ``kern.tty.ptmx_max`` backstop + agent choice mitigate that residual class.
    """
    pgid = None
    try:
        lines = pidfile.read_text(encoding="utf-8").splitlines()
        if lines:
            entry = json.loads(lines[0])
            if isinstance(entry, dict):
                pgid = int(entry.get("pgid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pgid = None
    if not pgid or pgid <= 1:
        return
    # Direct-dispatch invariant: the worker is its own session/group leader
    # (start_new_session), so pgid == worker_pid. If they disagree we cannot be
    # sure the group is the worker's -- be conservative and skip.
    if pgid != worker_pid:
        return
    with contextlib.suppress(OSError, AttributeError):
        if hasattr(os, "getpgrp") and pgid == os.getpgrp():
            return  # never signal the orchestrator's own process group
    # Re-check liveness immediately before signalling: if worker_pid was reused
    # and is now a live unrelated process, skip rather than risk a wrong target.
    if goalflight_compat.pid_alive(worker_pid):
        return
    # killpg the group DIRECTLY -- not via kill_pid, whose empty-group fallback
    # to a bare kill(worker_pid) could hit a reused pid. An empty/gone group
    # (ProcessLookupError) or a Windows-absent os.killpg degrades to a no-op.
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError, AttributeError):
        os.killpg(pgid, getattr(signal, "SIGTERM", 15))


def _mark_pidfile_detached(pidfile: Path) -> None:
    """Stamp ``detached: true`` on a bash-tail pidfile whose worker is still alive.

    Mirror of the ACP ``mark_connection_detached`` path. A non-terminal watcher
    exit can leave the worker running for re-attach. The marker makes that intent
    explicit for owned workers after their controller exits; unowned workers are
    independently preserved because missing ownership is not proof of abandonment.
    """
    try:
        lines = pidfile.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[0]) if lines else None
    except (OSError, ValueError, json.JSONDecodeError):
        return
    if not isinstance(entry, dict):
        return
    entry["detached"] = True
    with contextlib.suppress(OSError):
        pidfile.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")


def _cleanup_pidfile_if_worker_dead(pidfile: Path | None, worker_pid: int | None) -> None:
    if not pidfile or not worker_pid:
        return
    if goalflight_compat.pid_alive(worker_pid):
        # Still alive: non-destructive -- leave the pidfile for re-attach, but flag
        # it detached so cleanup_ghosts preserves the explicit re-attach intent for
        # owned workers after their controller exits (symmetry with ACP cleanup).
        _mark_pidfile_detached(pidfile)
        return
    _reap_dead_worker_pgroup(pidfile, worker_pid)
    with contextlib.suppress(OSError):
        pidfile.unlink(missing_ok=True)


def _finish_ledger(
    dispatch_id: str,
    state: str,
    reason: object,
    *,
    elapsed_s: float | None = None,
    worker_still_alive: bool | None = None,
) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        code = goalflight_ledger.cmd_finish(
            argparse.Namespace(
                dispatch_id=dispatch_id,
                state=state,
                reason=reason,
                terminal_state=None,
                elapsed_s=elapsed_s,
                worker_still_alive=worker_still_alive,
            )
        )
    if code != 0:
        raise RuntimeError(f"journal terminal emitter exited {code} for {dispatch_id}")
    _maybe_mark_grok_quota_exhausted(dispatch_id, state)


def _release_capacity(lease_id: str | None, state: str, reason: str | None) -> None:
    if not lease_id:
        return
    with contextlib.redirect_stdout(io.StringIO()):
        goalflight_capacity.cmd_release(argparse.Namespace(lease_id=lease_id, state=state, reason=reason, keep=True))


def _release_stale_capacity_for_drain() -> None:
    with contextlib.suppress(OSError), contextlib.redirect_stdout(io.StringIO()):
        goalflight_capacity.cmd_release_stale(
            argparse.Namespace(state="expired", reason="drain_stale_worker", keep=True)
        )


def _read_capacity_state_for_reconciliation() -> dict:
    """Read capacity state without converting corruption into an empty lease set."""

    try:
        payload = goalflight_capacity.load_state()
    except goalflight_capacity.CapacityStateUnreadable as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("leases"), dict):
        raise ValueError("capacity state has no readable lease map")
    return payload


def _abandoned_status_payload(record: dict) -> tuple[dict | None, str]:
    """Read status evidence, treating every unreadable case as indeterminate.

    Returns ``(payload, evidence)``. **Every failure returns ``None``, and
    ``None`` vetoes reconciliation** at the caller (``status_indeterminate``).
    That includes a recorded ``status_path`` whose file is missing:
    never-created, write-failed and unlinked are indistinguishable from here,
    so absence is "could not tell", never proof the dispatch is closable.

    Only a payload that reads, parses, and names this same ``dispatch_id`` is
    returned as evidence.
    """

    status_path = record.get("status_path")
    if status_path is None or status_path == "":
        return None, "status_path_absent"
    if not isinstance(status_path, str):
        return None, "status_path_invalid"
    try:
        payload = json.loads(Path(status_path).expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Recorded path, missing file: never-created vs write-failed vs
        # unlinked are indistinguishable here, so this vetoes rather than
        # licensing a close. A row whose status file was removed therefore
        # stays ineligible for reconciliation for good — acceptable only
        # because the ledger, not this file, is the authority on terminality.
        return None, "status_file_absent"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"status_unreadable:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "status_payload_invalid"
    if payload.get("dispatch_id") != record.get("dispatch_id"):
        return None, "status_dispatch_mismatch"
    return payload, "status_read"


def _abandoned_progress_snapshot(record: dict, status: dict) -> tuple[float | None, tuple]:
    """Return latest measured progress time plus a change-detection fingerprint."""

    timestamps = [
        _parse_timestamp_s(record.get("started_at")),
        _parse_timestamp_s(record.get("updated_at")),
        _parse_timestamp_s(status.get("updated_at")),
    ]
    fingerprints: list[tuple] = []
    for key in ("stdout_path", "stderr_path", "status_path"):
        raw = record.get(key)
        if not isinstance(raw, str) or not raw:
            fingerprints.append((key, None))
            continue
        path = Path(raw).expanduser()
        try:
            stat = path.stat()
        except OSError:
            fingerprints.append((key, str(path), None))
            continue
        timestamps.append(stat.st_mtime)
        fingerprints.append((key, str(path), stat.st_mtime_ns, stat.st_size))
    measured = [value for value in timestamps if value is not None]
    return (max(measured) if measured else None), tuple(fingerprints)


def _abandoned_output_evidence(record: dict) -> tuple[bool, str]:
    raw = record.get("stdout_path")
    if raw is None or raw == "":
        return False, "output_path_absent"
    if not isinstance(raw, str):
        return False, "output_path_invalid"
    path = Path(raw).expanduser()
    presence = goalflight_fs.path_presence(path)
    if presence == "absent":
        return True, "output_file_absent"
    if presence == "unknown":
        # exists() is False for a child of an unsearchable parent.
        return False, "output_unreadable:unsearchable"
    try:
        if not path.is_file():
            return False, "output_not_regular_file"
        with path.open("rb") as stream:
            stream.read(1)
    except FileNotFoundError:
        return True, "output_file_absent"
    except OSError as exc:
        return False, f"output_unreadable:{type(exc).__name__}"
    return True, "output_readable"


def _abandoned_changed_progress_keys(before: tuple, after: tuple) -> set[str]:
    before_by_key = {str(item[0]): item[1:] for item in before if item}
    after_by_key = {str(item[0]): item[1:] for item in after if item}
    return {
        key
        for key in before_by_key.keys() | after_by_key.keys()
        if before_by_key.get(key) != after_by_key.get(key)
    }


_PRODUCER_CONTRACT_FIELDS = (
    "worker_pgid",
    "worker_group_leader_identity",
    "producer_group_contract",
    "producer_group_contract_enforced",
)
_PRODUCER_SET_STRUCTURALLY_ABSENT = "producer_set:structurally_absent:no_group_contract"
_ELIGIBLE_NO_GROUP_CONTRACT = "worker_provably_gone:no_group_contract"


def _producer_set_contract_enumerable(record: dict) -> bool:
    """True when the record asserted a complete producer-set group contract.

    ``enumerate_token_producers`` can probe only when launch token, enforced
    group contract, pgid, group-leader identity, and worker pid are all
    present. Anything less is structurally unresolvable: those fields will
    not appear later, so a missing-contract INDETERMINATE can never clear.
    Bare ``worker_pgid`` is not a contract — typical launch writes the pgid
    from worker identity and never writes the contract flags.
    """

    if not record.get("queue_launch_token"):
        return False
    if not bool(record.get("producer_group_contract")):
        return False
    if not bool(record.get("producer_group_contract_enforced")):
        return False
    if not isinstance(record.get("worker_group_leader_identity"), dict):
        return False
    try:
        pgid = int(record.get("worker_pgid") or 0)
        worker_pid = int(record.get("worker_pid") or 0)
    except (TypeError, ValueError):
        return False
    return pgid > 0 and worker_pid > 0


def _abandoned_process_evidence(record: dict, status: dict) -> tuple[bool, str]:
    """Prove every locally recorded worker/claimant identity inactive.

    ``True`` means no recorded process can still own the dispatch. Identity
    provider errors and weak/unknown identities fail closed.

    Probe each recorded pid and start-token before treating a boolean
    ``worker_alive: true`` as authority. Confirmed-dead overrides that stale
    flag; a true flag with no pid to measure, or any live/indeterminate
    probe, stays not-abandoned. Status values outside ``{True, False, None}``
    are unreadable and fail closed before probes.

    A producer-set group contract is enumerated only when the record
    asserted a complete one (launch token, enforced contract, pgid, group
    leader identity, and worker pid). Bare ``worker_pgid`` or any other
    incomplete subset is structurally absent — those fields will not appear
    later — and does not veto once every pid-backed identity is confirmed
    dead. A present contract whose probe is unreadable still fails closed.
    Missing contract fields never rescue an unproven (absent or
    indeterminate) death.
    """

    if "worker_alive" in status and status.get("worker_alive") not in {True, False, None}:
        return False, "status_worker_alive:indeterminate"

    probes: list[tuple[str, object, object]] = [
        (
            "worker",
            record.get("worker_pid"),
            record.get("worker_identity"),
        ),
        (
            "status_worker",
            status.get("worker_pid"),
            status.get("expected_worker_identity") or status.get("worker_identity"),
        ),
        (
            "claimant",
            record.get("claimant_pid"),
            record.get("claimant_identity"),
        ),
    ]
    descendants = record.get("producer_descendants")
    if descendants is not None:
        if not isinstance(descendants, list):
            return False, "producer_descendants:invalid"
        for index, descendant in enumerate(descendants):
            if not isinstance(descendant, dict):
                return False, f"producer_descendant_{index}:invalid"
            probes.append(
                (
                    f"producer_descendant_{index}",
                    descendant.get("pid"),
                    descendant.get("identity"),
                )
            )

    evidence: list[str] = []
    for label, pid_value, identity in probes:
        if pid_value is None or pid_value == "":
            continue
        if isinstance(pid_value, bool):
            return False, f"{label}:invalid_pid"
        try:
            pid = int(pid_value)
        except (TypeError, ValueError):
            return False, f"{label}:invalid_pid"
        if pid <= 0:
            return False, f"{label}:invalid_pid"
        state, reason = _queue_claim_identity_status(pid, identity)
        evidence.append(f"{label}:{state}:{reason}")
        if state in {"live", "indeterminate"}:
            return False, ",".join(evidence)

    has_producer_fields = any(record.get(key) for key in _PRODUCER_CONTRACT_FIELDS)
    if has_producer_fields:
        if _producer_set_contract_enumerable(record):
            producer_entry = _entry_with_record_identity({}, record, prefer_record=True)
            producer_set = enumerate_token_producers(producer_entry)
            evidence.append(f"producer_set:{producer_set.state.value}:{producer_set.reason}")
            if producer_set.state not in {ProducerSetState.DEAD, ProducerSetState.PID_REUSED}:
                return False, ",".join(evidence)
        elif evidence:
            # Structurally absent contract. Every measured pid-backed identity
            # is already confirmed dead; the producer set cannot add
            # information those probes have not settled.
            evidence.append(_PRODUCER_SET_STRUCTURALLY_ABSENT)
        else:
            # Missing contract must never rescue an unproven death.
            if status.get("worker_alive") is True:
                return False, "status_worker_alive:true"
            return False, "producer_set:structurally_absent:unproven_pids"
    if not evidence:
        # No identity was measured. A true flag cannot be overridden without
        # confirmed death, so fail closed rather than treating absence as death.
        if status.get("worker_alive") is True:
            return False, "status_worker_alive:true"
        return True, "no_recorded_pid"
    return True, ",".join(evidence)


def _abandoned_lease_evidence(record: dict, capacity_state: dict) -> tuple[bool, str]:
    leases = capacity_state.get("leases")
    if not isinstance(leases, dict):
        return False, "lease_map_indeterminate"
    dispatch_id = str(record.get("dispatch_id") or "")
    recorded_ids = {
        str(value)
        for value in (record.get("lease_id"), record.get("remote_lease_id"))
        if value
    }
    matching: list[dict] = []
    for lease_id, lease in leases.items():
        key_matches = str(lease_id) in recorded_ids
        if not isinstance(lease, dict):
            if key_matches:
                return False, "matching_lease_malformed"
            continue
        if (
            key_matches
            or str(lease.get("lease_id") or "") in recorded_ids
            or str(lease.get("dispatch_id") or "") == dispatch_id
        ):
            matching.append(lease)
    if not matching:
        return True, "lease_absent"
    states = [str(lease.get("state") or "") for lease in matching]
    if all(state in goalflight_capacity.TERMINAL_LEASE_STATES for state in states):
        return True, "lease_terminal:" + ",".join(sorted(states))
    return False, "lease_nonterminal:" + ",".join(sorted(states))


def _abandoned_controller_indeterminate_unlock(record: dict) -> str:
    raw_label = record.get("controller_label")
    label = str(raw_label).strip() if isinstance(raw_label, str) else ""
    retire = (
        "python3 scripts/goalflight_session_status.py --retire "
        + (shlex.quote(label) if label else "<label>")
        + " --acknowledge-retirement"
    )
    return (
        "controller registry unreadable; reclaim stays held. "
        f"If the holder is dead, nonce-less {retire}; "
        "if the journal is unreadable, repair it (t-238)."
    )


def _abandoned_controller_evidence(record: dict) -> tuple[bool, str]:
    session_id = record.get("controller_session_id")
    controller_pid = record.get("controller_pid")
    controller_label = record.get("controller_label")
    if not controller_label and (not session_id or controller_pid is None):
        # Empty/missing controller identity is not "nobody to protect".
        return False, "controller_identity_absent"
    project_root = record.get("project_root")
    if not isinstance(project_root, str) or not project_root:
        return False, "controller_owner_project_indeterminate"
    try:
        state, live = goalflight_session_status.probe_live_session(
            Path(project_root).expanduser(),
            label=str(controller_label) if controller_label else None,
        )
    except Exception as exc:
        return False, f"controller_beacon_error:{type(exc).__name__}"
    if state == "unreadable":
        # A busy or lock-indeterminate registry is not "controller gone".
        return False, "controller_indeterminate"
    if state == "dead":
        return True, "controller_beacon_absent"
    if state != "live":
        # probe_live_session documents live|dead|unreadable only. Refuse to
        # treat an unexpected state as proven absence (sibling kernel lookup
        # raises; here the fail-closed arm is controller_beacon_error:).
        return False, f"controller_beacon_error:unexpected_probe_state:{state}"
    if not isinstance(live, dict):
        return False, "controller_beacon_error:live_session_missing"
    if live.get("conflicting_beacons"):
        return False, "controller_beacon_conflict"
    if controller_label:
        if str(live.get("label") or "") == str(controller_label):
            return False, "live_controller_label_owner"
        return False, "controller_label_identity_indeterminate"
    try:
        live_pid = int(live.get("pid"))
        recorded_pid = int(controller_pid)
    except (TypeError, ValueError):
        return False, "controller_beacon_identity_indeterminate"
    if str(live.get("id") or "") == str(session_id) and live_pid == recorded_pid:
        return False, "live_controller_owner"
    return True, "recorded_controller_beacon_absent"


def _evaluate_abandoned_dispatch(
    record: dict,
    *,
    queue_dir: Path,
    capacity_state: dict,
    now_s: float,
    stale_s: float,
) -> dict:
    dispatch_id = str(record.get("dispatch_id") or "")
    result = {"dispatch_id": dispatch_id, "state": record.get("state")}
    if not dispatch_id:
        return {**result, "eligible": False, "reason": "missing_dispatch_id"}
    if _dispatch_record_is_terminal(record):
        return {**result, "eligible": False, "reason": "already_terminal"}
    if not goalflight_dispatch_states.is_running_state(record.get("state")):
        return {**result, "eligible": False, "reason": "not_running_state"}
    if record.get("transport") != "dispatch":
        return {**result, "eligible": False, "reason": "nonlocal_transport"}
    hostname = record.get("hostname")
    if not isinstance(hostname, str) or hostname != socket.gethostname():
        return {**result, "eligible": False, "reason": "nonlocal_or_unknown_host"}
    carrier = _claim_has_active_carrier(queue_dir, dispatch_id)
    keep_reason = _abandoned_carrier_keep_reason(carrier)
    if keep_reason is not None:
        return {
            **result,
            "eligible": False,
            "reason": keep_reason,
            "claimer_status": carrier.kind.value,
            "claimer_evidence": carrier.reason,
        }

    status, status_evidence = _abandoned_status_payload(record)
    if status is None:
        return {
            **result,
            "eligible": False,
            "reason": "status_indeterminate",
            "status_evidence": status_evidence,
        }
    output_readable, output_evidence = _abandoned_output_evidence(record)
    if not output_readable:
        return {
            **result,
            "eligible": False,
            "reason": "output_indeterminate",
            "output_evidence": output_evidence,
        }
    process_inactive, process_evidence = _abandoned_process_evidence(record, status)
    if not process_inactive:
        return {
            **result,
            "eligible": False,
            "reason": "worker_live_or_indeterminate",
            "process_evidence": process_evidence,
        }
    lease_inactive, lease_evidence = _abandoned_lease_evidence(record, capacity_state)
    if not lease_inactive:
        return {
            **result,
            "eligible": False,
            "reason": "lease_live_or_indeterminate",
            "lease_evidence": lease_evidence,
        }
    controller_inactive, controller_evidence = _abandoned_controller_evidence(record)
    if not controller_inactive:
        held = {
            **result,
            "eligible": False,
            "reason": (
                "controller_indeterminate"
                if controller_evidence
                in {"controller_indeterminate", "controller_identity_absent"}
                else "controller_live_or_indeterminate"
            ),
            "controller_evidence": controller_evidence,
        }
        if controller_evidence == "controller_indeterminate":
            held["detail"] = _abandoned_controller_indeterminate_unlock(record)
        return held
    latest_progress_s, progress_fingerprint = _abandoned_progress_snapshot(record, status)
    if latest_progress_s is None:
        return {**result, "eligible": False, "reason": "progress_time_indeterminate"}
    progress_age_s = now_s - latest_progress_s
    if progress_age_s < stale_s:
        return {
            **result,
            "eligible": False,
            "reason": "recent_progress",
            "progress_age_s": round(progress_age_s, 3),
        }
    eligible_reason = "worker_provably_gone"
    if _PRODUCER_SET_STRUCTURALLY_ABSENT in {
        part.strip() for part in str(process_evidence).split(",")
    }:
        eligible_reason = _ELIGIBLE_NO_GROUP_CONTRACT
    return {
        **result,
        "eligible": True,
        "reason": eligible_reason,
        "process_evidence": process_evidence,
        "status_evidence": status_evidence,
        "output_evidence": output_evidence,
        "lease_evidence": lease_evidence,
        "controller_evidence": controller_evidence,
        "progress_age_s": round(progress_age_s, 3),
        "progress_fingerprint": progress_fingerprint,
    }


def _abandoned_terminal_outcome(record: dict) -> tuple[str, object, dict | None]:
    dispatch_id = str(record.get("dispatch_id") or "")
    tail = Path(str(record.get("stdout_path") or _dispatch_base_dir() / f"{dispatch_id}.tail"))
    prompt = record.get("prompt_path")
    state, reason, marker = _resolve_claim_terminal_outcome(
        record,
        reason="abandoned_without_verdict",
        tail=tail,
        ignore_prefix_lines=_ignore_prefix_lines(str(Path(prompt).expanduser()) if prompt else None),
        agent=str(record.get("agent") or "unknown"),
    )
    if marker is None:
        return "inconclusive_no_final", "abandoned_without_verdict", None
    return state, reason, marker


def _commit_abandoned_dispatch(
    record: dict,
    *,
    evaluation: dict,
    state: str,
    reason: object,
    marker: dict | None,
) -> None:
    terminal_state = goalflight_ledger.terminal_state_for(state, reason)
    if terminal_state in {"", "unknown", "watcher_stopped"}:
        raise ValueError(f"abandoned reconciliation produced non-terminal state: {state}")
    committed = goalflight_ledger.commit_terminal_authority(
        record,
        state=state,
        reason=reason,
        terminal_state=terminal_state,
        worker_still_alive=False,
    )
    if not committed.committed or committed.value is None:
        raise RuntimeError(
            f"terminal journal commit {committed.disposition.value}: {committed.reason}"
        )
    winner = committed.value
    terminal_state = winner.terminal_state
    ended_at = goalflight_ledger.preserve_first_terminal_time(
        record,
        winner.terminal_at,
    )
    basis = "observed_terminal_marker" if marker is not None else "inferred_abandonment"
    reconciliation = {
        "source": "goalflight_dispatch.drain",
        "basis": basis,
        "reason": evaluation.get("reason"),
        "process_evidence": evaluation.get("process_evidence"),
        "status_evidence": evaluation.get("status_evidence"),
        "output_evidence": evaluation.get("output_evidence"),
        "lease_evidence": evaluation.get("lease_evidence"),
        "controller_evidence": evaluation.get("controller_evidence"),
        "progress_age_s": evaluation.get("progress_age_s"),
        "checked_output": True,
        "observed_outcome": marker is not None,
    }
    if marker is not None:
        reconciliation["terminal_marker_kind"] = marker.get("kind")
        record["terminal_marker"] = marker
    record.update(
        {
            "state": state,
            "terminal_state": terminal_state,
            "liveness_state": goalflight_terminal.terminal_liveness_state(state),
            "worker_still_alive": False,
            "reason": reason,
            "outcome": {
                "terminal_state": terminal_state,
                "reason": reason,
                "reconciliation": reconciliation,
            },
        }
    )
    record["attempt_id"] = winner.attempt_id
    record["transition_id"] = winner.transition_id
    record["terminal_event_uuid"] = winner.event_uuid
    record.pop("error", None)
    elapsed_s = goalflight_ledger.elapsed_seconds(record, ended_at)
    if elapsed_s is not None:
        record["elapsed_s"] = elapsed_s
    goalflight_ledger.write_record(record)
    _maybe_mark_grok_quota_exhausted(record=record, state=state)


def _abandoned_status_entry(record: dict) -> dict:
    return {
        **record,
        "request": {
            "cwd": record.get("project_root"),
            "prompt_file": record.get("prompt_path"),
            "tail": record.get("stdout_path"),
            "status_json": record.get("status_path"),
        },
    }


def reconcile_abandoned_dispatches(
    *,
    queue_dir: Path | None = None,
    dry_run: bool = False,
    stale_s: float = ABANDONED_RECONCILE_STALE_S,
    now: dt.datetime | None = None,
) -> dict:
    """Close only local running records whose worker is provably gone."""

    resolved_queue_dir = (queue_dir or _dispatch_queue_dir()).expanduser()
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    now_s = current.timestamp()
    # Per-pass cost must track live work, not dispatch history. Terminal
    # rows are skipped before json.loads; see read_records(skip_terminal=True).
    initial_records = goalflight_ledger.read_records(skip_terminal=True)
    read_work = goalflight_ledger.last_read_work()
    with goalflight_capacity.StateLock():
        initial_capacity = _read_capacity_state_for_reconciliation()

    entries: list[dict] = []
    changed_projects: set[Path] = set()
    for initial in initial_records:
        initial_evaluation = _evaluate_abandoned_dispatch(
            initial,
            queue_dir=resolved_queue_dir,
            capacity_state=initial_capacity,
            now_s=now_s,
            stale_s=stale_s,
        )
        if not initial_evaluation.get("eligible"):
            if initial_evaluation.get("reason") != "already_terminal":
                entries.append(initial_evaluation)
            continue

        queue_lock = try_acquire_queue_lock(
            resolved_queue_dir,
            deadline_s=time.monotonic() + RECONCILE_DOWNSTREAM_LOCK_BUDGET_S,
        )
        if queue_lock is None:
            entries.append({**initial_evaluation, "eligible": False, "reason": "queue_lock_busy"})
            continue
        committed_record: dict | None = None
        final_evaluation: dict | None = None
        state: str | None = None
        reason: object = None
        marker: dict | None = None
        try:
            with goalflight_capacity.StateLock():
                fresh_capacity = _read_capacity_state_for_reconciliation()
                ledger_lock = try_acquire_ledger_lock(
                    deadline_s=time.monotonic() + RECONCILE_DOWNSTREAM_LOCK_BUDGET_S
                )
                if ledger_lock is None:
                    entries.append({**initial_evaluation, "eligible": False, "reason": "ledger_lock_busy"})
                    continue
                try:
                    fresh = _find_dispatch_record(str(initial.get("dispatch_id") or ""))
                    if not isinstance(fresh, dict):
                        entries.append({**initial_evaluation, "eligible": False, "reason": "record_disappeared"})
                        continue
                    final_evaluation = _evaluate_abandoned_dispatch(
                        fresh,
                        queue_dir=resolved_queue_dir,
                        capacity_state=fresh_capacity,
                        now_s=now_s,
                        stale_s=stale_s,
                    )
                    if not final_evaluation.get("eligible"):
                        entries.append(final_evaluation)
                        continue
                    if final_evaluation.get("progress_fingerprint") != initial_evaluation.get("progress_fingerprint"):
                        entries.append({**final_evaluation, "eligible": False, "reason": "progress_changed_before_close"})
                        continue
                    state, reason, marker = _abandoned_terminal_outcome(fresh)
                    post_status, post_status_evidence = _abandoned_status_payload(fresh)
                    if post_status is None:
                        entries.append(
                            {
                                **final_evaluation,
                                "eligible": False,
                                "reason": "status_changed_before_close",
                                "status_evidence": post_status_evidence,
                            }
                        )
                        continue
                    _post_progress_s, post_fingerprint = _abandoned_progress_snapshot(
                        fresh, post_status
                    )
                    changed_keys = _abandoned_changed_progress_keys(
                        final_evaluation.get("progress_fingerprint", ()),
                        post_fingerprint,
                    )
                    if changed_keys:
                        # A terminal marker can land in the tail between the
                        # final liveness evaluation and the outcome scan.  It
                        # is observed evidence, so accept it only after a
                        # second scan proves that the tail then stayed stable.
                        if changed_keys != {"stdout_path"} or marker is None:
                            entries.append(
                                {
                                    **final_evaluation,
                                    "eligible": False,
                                    "reason": "progress_changed_before_close",
                                    "changed_progress_keys": sorted(changed_keys),
                                }
                            )
                            continue
                        state, reason, marker = _abandoned_terminal_outcome(fresh)
                        _stable_progress_s, stable_fingerprint = (
                            _abandoned_progress_snapshot(fresh, post_status)
                        )
                        if stable_fingerprint != post_fingerprint or marker is None:
                            entries.append(
                                {
                                    **final_evaluation,
                                    "eligible": False,
                                    "reason": "progress_changed_before_close",
                                    "changed_progress_keys": ["stdout_path"],
                                }
                            )
                            continue
                    if not dry_run:
                        if state is None:
                            raise ValueError("abandoned reconciliation outcome missing")
                        _commit_abandoned_dispatch(
                            fresh,
                            evaluation=final_evaluation,
                            state=state,
                            reason=reason,
                            marker=marker,
                        )
                        committed_record = fresh
                finally:
                    ledger_lock.release()
        finally:
            queue_lock.release()

        if final_evaluation is None:
            continue
        if state is None:
            continue
        action = "would_close" if dry_run else "closed"
        entries.append(
            {
                **final_evaluation,
                "action": action,
                "closed_state": state,
                "closure_basis": (
                    "observed_terminal_marker" if marker is not None else "inferred_abandonment"
                ),
            }
        )
        if committed_record is not None:
            _write_reconciled_terminal_status(_abandoned_status_entry(committed_record), marker)
            project_root = committed_record.get("project_root")
            if isinstance(project_root, str) and project_root:
                changed_projects.add(Path(project_root).expanduser())

    for project_root in changed_projects:
        _export_dashboard_status_for_project(project_root)
    closed = [entry for entry in entries if entry.get("action") == "closed"]
    would_close = [entry for entry in entries if entry.get("action") == "would_close"]
    kept_reasons: dict[str, int] = {}
    for entry in entries:
        if entry.get("action"):
            continue
        key = str(entry.get("reason") or "unknown")
        kept_reasons[key] = kept_reasons.get(key, 0) + 1
    claimer_counts = _claimer_report_fields(resolved_queue_dir)
    report = {
        "schema": ABANDONED_RECONCILIATION_SCHEMA,
        "mode": "dry-run" if dry_run else "automatic",
        "stale_seconds": stale_s,
        "listed": read_work["listed"],
        "parsed": read_work["parsed"],
        "skipped_terminal": read_work["skipped_terminal"],
        "scanned": read_work["parsed"],
        "closed": len(closed),
        "would_close": len(would_close),
        "kept": len(entries) - len(closed) - len(would_close),
        "kept_reasons": kept_reasons,
        "dead_claimer": claimer_counts["dead_claimer"],
        "unknown_claimer": claimer_counts["unknown_claimer"],
        "live_claimer": claimer_counts["live_claimer"],
        "entries": entries,
    }
    if claimer_counts.get("queue_listing_error"):
        report["queue_listing_error"] = str(claimer_counts["queue_listing_error"])
    return report


def _reconcile_abandoned_for_drain(queue_dir: Path) -> dict:
    try:
        return reconcile_abandoned_dispatches(queue_dir=queue_dir)
    except Exception as exc:
        return {
            "schema": ABANDONED_RECONCILIATION_SCHEMA,
            "mode": "automatic",
            "closed": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _cmd_reconcile_abandoned(argv: list[str]) -> int:
    parser = _TerseArgumentParser(
        description="Dry-run abandoned dispatch reconciliation; never changes ledger records.",
        usage_hint="try reconcile-abandoned [--json] [--stale-s N] (or --help)",
    )
    parser.add_argument("--queue-dir")
    parser.add_argument("--stale-s", type=float, default=ABANDONED_RECONCILE_STALE_S)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = reconcile_abandoned_dispatches(
        queue_dir=Path(args.queue_dir).expanduser() if args.queue_dir else None,
        dry_run=True,
        stale_s=args.stale_s,
    )
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            "RECONCILE-ABANDONED dry-run: no ledger record was changed. "
            "This command has no apply flag; only drain writes. "
            + json.dumps(
                {
                    "mode": "dry-run",
                    "would_close": payload["would_close"],
                    "kept": payload["kept"],
                    "scanned": payload["scanned"],
                    "kept_reasons": payload["kept_reasons"],
                    "dead_claimer": payload["dead_claimer"],
                    "unknown_claimer": payload["unknown_claimer"],
                    "live_claimer": payload["live_claimer"],
                },
                sort_keys=True,
            )
        )
    return 0


def _reap_quota_stuck_before_bash_launch() -> None:
    with contextlib.suppress(Exception):
        import goalflight_acp_client

        result = goalflight_acp_client.reap_quota_stuck_workers()
        if result.get("reaped"):
            print(
                "QUOTA-STUCK-REAP "
                + json.dumps({"reaped": result.get("reaped")}, sort_keys=True),
                flush=True,
            )


def _dispatch_record_is_terminal(record: dict | None) -> bool:
    if not record:
        return False
    if goalflight_ledger.record_is_unreadable(record):
        return False
    terminal = record.get("terminal_state") or goalflight_ledger.terminal_state_for(
        record.get("state"),
        record.get("reason") or record.get("error"),
    )
    return terminal != "unknown" or goalflight_dispatch_states.is_terminal_state(record.get("state"))


def _dispatch_record_has_live_nonterminal_worker(record: dict | None) -> bool:
    if not (record and record.get("worker_pid")):
        return False
    if _dispatch_record_is_terminal(record):
        return False
    try:
        ok, _reason = goalflight_ledger.identity_matches(record)
    except Exception:
        return False
    return ok


def _dispatch_has_worker_record(
    dispatch_id: str,
    *,
    queue_launch_token: str | None = None,
    require_live_nonterminal: bool = False,
) -> bool:
    record = _find_dispatch_record(dispatch_id)
    if not record:
        return False
    if queue_launch_token is not None:
        if record.get("queue_launch_token") != queue_launch_token:
            return False
    if record.get("worker_pid"):
        if require_live_nonterminal and not _dispatch_record_has_live_nonterminal_worker(record):
            return False
        return True
    if record.get("transport") != "fleet-ssh":
        return False
    remote_receipt = record.get("remote_launch_receipt")
    # Only a durable remote launch receipt is positive launch evidence.
    # launch_unconfirmed is deliberately NOT counted: the launch command was
    # issued but no receipt confirms the remote process started, so the
    # dispatch may be live yet unaccounted — fail closed and retain the
    # carrier until a real receipt or a genuinely terminal record exists.
    has_remote_launch = bool(isinstance(remote_receipt, dict) and remote_receipt.get("remote_pid"))
    if not has_remote_launch:
        return False
    if require_live_nonterminal and _dispatch_record_is_terminal(record):
        return False
    return True


def _dispatch_has_terminal_record(dispatch_id: str) -> bool:
    return _dispatch_record_is_terminal(_find_dispatch_record(dispatch_id))


_CLAIMED_NAME_RE = re.compile(r"^(?P<stem>.+)\.json\.claimed-(?P<rest>.*)$")
_CLAIM_CARRIER_PREFERENCE = (
    ClaimCarrierKind.LIVE,
    ClaimCarrierKind.QUEUED,
    ClaimCarrierKind.UNKNOWN,
    ClaimCarrierKind.DEAD,
)


def _parse_claimed_filename_pid(name: str) -> int | None:
    """Best-effort PID from ``<id>.json.claimed-<pid>[-<ts>...]``.

    The filename PID is never identity. Callers that lack a start token must
    report UNKNOWN rather than treating this number as live or dead.
    """
    match = _CLAIMED_NAME_RE.match(name)
    if match is None:
        return None
    rest = str(match.group("rest") or "")
    if rest.endswith(".failed"):
        return None
    head = rest.split("-", 1)[0]
    try:
        pid = int(head)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _claim_payload_or_none(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _adjudicate_claim_marker(claim: Path, payload: dict | None = None) -> ClaimCarrierStatus:
    """Classify one claim marker as live, dead, or unknown.

    Start-token identity is required for LIVE or DEAD. Existing markers carry
    only ``claimed-<pid>-<ts>`` in the filename; that is UNKNOWN.
    """
    observed = payload if payload is not None else _claim_payload_or_none(claim)
    probes: list[tuple[object, object]] = []
    if isinstance(observed, dict):
        if observed.get("queue_claimer_pid") not in (None, ""):
            probes.append(
                (observed.get("queue_claimer_pid"), observed.get("queue_claimer_identity"))
            )
        if observed.get("queue_launcher_pid") not in (None, ""):
            probes.append(
                (observed.get("queue_launcher_pid"), observed.get("queue_launcher_identity"))
            )
    if not probes:
        probes.append((_parse_claimed_filename_pid(claim.name), None))

    saw_dead: ClaimCarrierStatus | None = None
    saw_unknown: ClaimCarrierStatus | None = None
    for pid_value, identity in probes:
        try:
            pid = int(pid_value or 0)
        except (TypeError, ValueError):
            saw_unknown = ClaimCarrierStatus(
                ClaimCarrierKind.UNKNOWN,
                "claim_pid_unparsable",
                str(claim),
            )
            continue
        if pid <= 0:
            saw_unknown = ClaimCarrierStatus(
                ClaimCarrierKind.UNKNOWN,
                "claim_pid_unparsable",
                str(claim),
            )
            continue
        if not isinstance(identity, dict) or not str(identity.get("start_token") or ""):
            saw_unknown = ClaimCarrierStatus(
                ClaimCarrierKind.UNKNOWN,
                "claim_identity_unavailable",
                str(claim),
            )
            continue
        status, reason = _queue_claim_identity_status(pid, identity)
        if status == "live":
            return ClaimCarrierStatus(ClaimCarrierKind.LIVE, reason, str(claim))
        if status == "dead":
            saw_dead = ClaimCarrierStatus(ClaimCarrierKind.DEAD, reason, str(claim))
            continue
        saw_unknown = ClaimCarrierStatus(ClaimCarrierKind.UNKNOWN, reason, str(claim))
    if saw_unknown is not None:
        return saw_unknown
    if saw_dead is not None:
        return saw_dead
    return ClaimCarrierStatus(
        ClaimCarrierKind.UNKNOWN,
        "claim_identity_unavailable",
        str(claim),
    )


def _prefer_claim_carrier(statuses: list[ClaimCarrierStatus]) -> ClaimCarrierStatus:
    if not statuses:
        return ClaimCarrierStatus()
    for kind in _CLAIM_CARRIER_PREFERENCE:
        for status in statuses:
            if status.kind is kind:
                return status
    return statuses[0]


def _queue_dir_listing(queue_dir: Path) -> list[Path]:
    """List queue-dir entries, raising on a failed listing.

    ``Path.glob`` swallows ``PermissionError`` and yields nothing, which
    renders an unreadable queue as an empty one — the collapse that licensed
    ``worker_provably_gone`` for an envelope still on disk. ``iterdir``
    raises. A directory that vanished between the ``is_dir`` probe and the
    listing is genuinely absent: no entries.
    """
    try:
        return list(queue_dir.iterdir())
    except FileNotFoundError:
        return []


def _summarize_claim_markers(queue_dir: Path) -> dict[str, int | str]:
    """Count leftover claim markers by claimer adjudication. Read-only.

    An unreadable queue dir is UNKNOWN, not all-zero counts: the report
    carries ``listing_error`` instead of pretending no markers exist.
    """
    summary: dict[str, int | str] = {"dead": 0, "unknown": 0, "live": 0}
    # Do not probe with ``is_dir()``: Path.is_dir returns False on OSError, so a
    # parent-unreadable queue would look absent (all-zero, no listing_error).
    try:
        entries = _queue_dir_listing(queue_dir)
    except OSError as exc:
        summary["listing_error"] = f"queue_dir_unreadable:{type(exc).__name__}"
        return summary
    for claim in entries:
        if ".json.claimed-" not in claim.name:
            continue
        if claim.name.endswith(".failed"):
            continue
        kind = _adjudicate_claim_marker(claim).kind
        if kind is ClaimCarrierKind.DEAD:
            summary["dead"] += 1
        elif kind is ClaimCarrierKind.LIVE:
            summary["live"] += 1
        else:
            summary["unknown"] += 1
    return summary


def _claimer_report_fields(queue_dir: Path) -> dict[str, int | str]:
    summary = _summarize_claim_markers(queue_dir)
    fields: dict[str, int | str] = {
        "dead_claimer": summary["dead"],
        "unknown_claimer": summary["unknown"],
        "live_claimer": summary["live"],
    }
    listing_error = summary.get("listing_error")
    if listing_error:
        fields["queue_listing_error"] = str(listing_error)
    return fields


def _abandoned_carrier_keep_reason(carrier: ClaimCarrierStatus) -> str | None:
    if not carrier:
        return None
    if carrier.kind is ClaimCarrierKind.DEAD:
        return "dead_claimer"
    if carrier.kind is ClaimCarrierKind.UNKNOWN:
        return "unknown_claimer"
    return "active_queue_carrier"


class _QueueCarrierIndex:
    """One listing of queue envelopes, keyed by dispatch_id.

    Orphan recon used to call ``_claim_has_active_carrier`` once per ledger
    row, re-reading every claim file. An unlistable queue is UNKNOWN for
    every id (truthy), never NONE.
    """

    def __init__(
        self,
        listing_error: ClaimCarrierStatus | None = None,
        by_id: dict[str, ClaimCarrierStatus] | None = None,
    ) -> None:
        self.listing_error = listing_error
        self.by_id = by_id or {}

    def status(self, dispatch_id: str) -> ClaimCarrierStatus:
        if self.listing_error is not None:
            return self.listing_error
        if not dispatch_id:
            return ClaimCarrierStatus()
        return self.by_id.get(dispatch_id) or ClaimCarrierStatus()


def _build_queue_carrier_index(
    queue_dir: Path,
    entries: list[Path] | None = None,
) -> _QueueCarrierIndex:
    t0 = time.monotonic()
    try:
        try:
            listing = entries if entries is not None else _queue_dir_listing(queue_dir)
        except OSError as exc:
            return _QueueCarrierIndex(
                listing_error=ClaimCarrierStatus(
                    ClaimCarrierKind.UNKNOWN,
                    f"queue_dir_unreadable:{type(exc).__name__}",
                    str(queue_dir),
                )
            )
        collected: dict[str, list[ClaimCarrierStatus]] = {}

        def _add(dispatch_id: str, status: ClaimCarrierStatus) -> None:
            if not dispatch_id:
                return
            collected.setdefault(dispatch_id, []).append(status)

        for path in listing:
            if not path.name.endswith(".json"):
                continue
            if path.name.endswith(".failed"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _add(
                    path.stem,
                    ClaimCarrierStatus(
                        ClaimCarrierKind.QUEUED,
                        "queued_envelope_unreadable",
                        str(path),
                    ),
                )
                continue
            if isinstance(payload, dict):
                _add(
                    str(payload.get("dispatch_id") or path.stem),
                    ClaimCarrierStatus(ClaimCarrierKind.QUEUED, "queued_envelope", str(path)),
                )
        for claim in listing:
            if ".json.claimed-" not in claim.name:
                continue
            if claim.name.endswith(".failed"):
                continue
            filename_id = claim.name.split(".claimed-", 1)[0].removesuffix(".json")
            try:
                payload = json.loads(claim.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _add(filename_id, _adjudicate_claim_marker(claim, None))
                continue
            if isinstance(payload, dict):
                _add(
                    str(payload.get("dispatch_id") or filename_id),
                    _adjudicate_claim_marker(claim, payload),
                )
        by_id = {
            dispatch_id: _prefer_claim_carrier(statuses)
            for dispatch_id, statuses in collected.items()
        }
        return _QueueCarrierIndex(by_id=by_id)
    finally:
        _pass_time("carrier_index_s", time.monotonic() - t0)


def _claim_has_active_carrier(
    queue_dir: Path,
    dispatch_id: str,
    *,
    index: _QueueCarrierIndex | None = None,
) -> ClaimCarrierStatus:
    """Adjudicate whether a queue envelope still exists for dispatch_id.

    A bare ``.json`` is a queued envelope (healthy carrier). A ``.claimed-*``
    marker is a claimer: LIVE, DEAD, or UNKNOWN via pid+start-token identity.
    The result is truthy whenever any matching file exists, so restore/close
    paths that used this as a presence gate still refuse to mutate. A queue
    dir that cannot be listed is UNKNOWN, never NONE: unreadable is not
    absent, and the abandoned gate must keep what it cannot verify.
    """
    if not dispatch_id:
        return ClaimCarrierStatus()
    if index is None:
        index = _build_queue_carrier_index(queue_dir)
    cache = _PASS_CACHE
    if cache is not None:
        cache["timing"]["carrier_index_lookups"] = int(
            cache["timing"].get("carrier_index_lookups") or 0
        ) + 1
    return index.status(dispatch_id)


def _scan_entry_completion_marker(entry: dict) -> dict | None:
    """Best-effort terminal-marker scan from claim/request tail path."""
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    dispatch_id = str(entry.get("dispatch_id") or "")
    tail_raw = request.get("tail")
    if not tail_raw and dispatch_id:
        tail_raw = str(_dispatch_base_dir() / f"{dispatch_id}.tail")
    if not tail_raw or not dispatch_id:
        return None
    tail = Path(str(tail_raw))
    prompt = request.get("prompt_file") or entry.get("prompt_file")
    ignore_prefix_lines = _ignore_prefix_lines(str(Path(prompt).expanduser()) if prompt else None)
    try:
        return _final_terminal_marker(
            tail,
            ignore_prefix_lines=ignore_prefix_lines,
            suppress_unfenced_prompt_markers=True,
            expected_dispatch_id=dispatch_id,
        )
    except Exception:
        return None


def _entry_tail_path(entry: dict, record: dict | None = None) -> Path:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    dispatch_id = str(entry.get("dispatch_id") or "")
    raw = (
        request.get("tail")
        or (record.get("stdout_path") if isinstance(record, dict) else None)
        or (record.get("tail_path") if isinstance(record, dict) else None)
        or (_dispatch_base_dir() / f"{dispatch_id}.tail")
    )
    return Path(str(raw)).expanduser()


# Local-path tokens in SUCCESS marker text (READY/COMPLETE/RESULT). Mirrors
# goalflight_status._extract_marker_paths — kept local so recovery never depends
# on status module load order inside the drain hot path.
_MARKER_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)
_PATH_EXT_RE = re.compile(r"[^/\\]\.[A-Za-z0-9]{1,8}$")


def _extract_marker_artifact_paths(marker_text: str) -> list[str]:
    if not marker_text:
        return []
    tokens = [m.group(1) for m in _MARKER_LINK_RE.finditer(marker_text)]
    tokens += re.split(r"[\s,;`\"'<>()\[\]]+", _MARKER_LINK_RE.sub(" ", marker_text))
    out: list[str] = []
    for tok in tokens:
        tok = tok.strip().strip("`\"'<>.,;")
        if not tok:
            continue
        if tok.startswith("file://"):
            tok = tok[len("file://") :]
        elif _URL_SCHEME_RE.match(tok):
            continue
        tok = tok.split("#", 1)[0]
        tok = re.sub(r":\d+(?:-\d+)?$", "", tok)
        if tok and _PATH_EXT_RE.search(tok) and tok not in out:
            out.append(tok)
    return out


def _direct_open_nonempty(path: Path) -> bool:
    """Confirm path by direct open + non-empty size. Never use git/ls/find."""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(1)
        if chunk:
            return True
        return path.stat().st_size > 0
    except OSError:
        return False


def _project_root_for_entry(entry: dict, record: dict | None = None) -> Path:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    raw = (
        entry.get("project_root")
        or request.get("cwd")
        or (record.get("project_root") if isinstance(record, dict) else None)
        or Path.cwd()
    )
    return Path(str(raw)).expanduser().resolve()


def _marker_declared_artifacts_ok(marker: dict | None, project_root: Path) -> bool:
    """True when marker is a SUCCESS kind and every declared artifact opens non-empty.

    Markers with no declared paths (bare COMPLETE) are valid without FS checks.
    Markers that declare paths require every path present and non-empty.
    """
    if not isinstance(marker, dict):
        return False
    if marker.get("kind") not in SUCCESS_TERMINAL_MARKERS:
        return False
    declared = _extract_marker_artifact_paths(str(marker.get("text") or ""))
    if not declared:
        return True
    for rel in declared:
        path = Path(rel).expanduser()
        if not path.is_absolute():
            path = project_root / rel
        if not _direct_open_nonempty(path):
            return False
    return True


def _task_row_durably_complete(row: dict | None) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("done_reviewed") is True:
        return True
    if row.get("done") is True:
        return True
    derived = str(row.get("derived_status") or "")
    return derived in {"done-reviewed", "awaiting-review", "worker-finished"}


def _task_row_completion_timestamp_s(row: dict | None) -> float | None:
    if not isinstance(row, dict):
        return None
    # done_at is the first durable completion. Review acceptance may happen
    # later, so done_reviewed_at is only the fallback for worker-finished rows
    # accepted without an explicit `done` transition.
    for key in ("done_at", "done_reviewed_at", "closed_at"):
        parsed = _parse_timestamp_s(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _completion_order(
    completion_timestamp_s: float | None,
    entry_created_timestamp_s: float | None,
) -> str:
    # Missing or malformed time is neither "older" nor "not older". Defer the
    # authority decision instead of guessing: treating a missing entry time as
    # old would silently drop deliberate follow-ups, while treating a missing
    # completion time as old could launch a genuine queued duplicate.
    if completion_timestamp_s is None or entry_created_timestamp_s is None:
        return "indeterminate"
    if completion_timestamp_s == entry_created_timestamp_s:
        # Equal wall-clock values do not prove event order. Current writers use
        # whole-second timestamps, and legacy records may have the same coarse
        # precision, so fail closed instead of superseding an ambiguous entry.
        return "equal"
    return "after" if completion_timestamp_s > entry_created_timestamp_s else "before"


def _ledger_task_ids_advanced(
    task_ids: list[str],
    *,
    self_dispatch_id: str,
    entry_created_timestamp_s: float | None = None,
    self_project_root: object | None = None,
) -> tuple[int, int, str]:
    """Return counts plus the typed reason ledger authority is inconclusive.

    Same-task completions in another project's ledger row are not this
    envelope's successor. Missing project_root on either side is not a match.
    """
    if not task_ids:
        return 0, 0, "conclusive"
    wanted = set(task_ids)
    complete_tasks: set[str] = set()
    advanced_tasks: set[str] = set()
    equal_completion_tasks: set[str] = set()
    indeterminate_completion_tasks: set[str] = set()
    try:
        records = _pass_ledger_records()
    except Exception:
        return 0, 0, "ledger_unavailable"
    for record in records:
        if not isinstance(record, dict):
            continue
        if goalflight_ledger.record_is_unreadable(record):
            # Placeholder has no task_ids. Skipping it would treat a corrupt
            # prior completion as "no completion" and launch a duplicate.
            return 0, 0, "ledger_unavailable"
        if str(record.get("dispatch_id") or "") == self_dispatch_id:
            continue
        rec_ids = set(_entry_task_ids(None, record))
        overlap = wanted & rec_ids
        if not overlap:
            continue
        other_root = _entry_owner_fields(None, record)[1]
        if not _project_roots_match(self_project_root, other_root):
            continue
        state = str(record.get("state") or "")
        terminal = str(
            record.get("terminal_state")
            or goalflight_ledger.terminal_state_for(state, record.get("reason") or record.get("error"))
            or ""
        )
        if (
            state in goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES
            or terminal in goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES
            or state == "complete"
            or terminal == "complete"
        ):
            completion_order = _completion_order(
                _parse_timestamp_s(record.get("ended_at")),
                entry_created_timestamp_s,
            )
            if completion_order == "equal":
                equal_completion_tasks |= overlap
                continue
            if completion_order == "indeterminate":
                indeterminate_completion_tasks |= overlap
                continue
            if completion_order != "after":
                continue
            complete_tasks |= overlap
            advanced_tasks |= overlap
            continue
        # Neutral reconciliation outcomes and live work both count as "advanced"
        # enough to block unsplit re-enqueue of this envelope.
        if state in {"superseded", "worker_dead"} or terminal in {"superseded", "worker_dead"}:
            advanced_tasks |= overlap
            continue
        if state and not goalflight_dispatch_states.is_terminal_state(state):
            if state not in {"queued", "waiting_capacity", "submitted"}:
                advanced_tasks |= overlap
    unresolved_indeterminate = indeterminate_completion_tasks - complete_tasks
    unresolved_equal = equal_completion_tasks - complete_tasks
    if unresolved_indeterminate:
        conclusion = "timestamp_indeterminate"
    elif unresolved_equal:
        conclusion = "timestamp_equal"
    else:
        conclusion = "conclusive"
    return len(complete_tasks), len(advanced_tasks), conclusion


def _linked_task_truth_detail(
    entry: dict,
    record: dict | None = None,
    *,
    task_store_locked: bool = False,
) -> tuple[str, str | None, tuple[str, ...]]:
    """Return task truth plus a typed indeterminate cause and its sources."""
    task_ids = _entry_task_ids(entry, record)
    if not task_ids:
        return "unlinked", None, ()
    project_root = _project_root_for_entry(entry, record)
    entry_created_timestamp_s = _parse_timestamp_s(entry.get("created_at"))
    store_complete = 0
    store_seen = 0
    store_issue: str | None = None
    try:
        root = SCRIPT_DIR.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        store = goalflight_task.TaskStore(project_root)
        if task_store_locked and store.publish_marker_path.exists():
            store._recover_interrupted_publish_locked()
        by_id = {
            str(item.get("id")): item
            for item in store.load_items(recover_publish=False)
        }
        for task_id in task_ids:
            row = by_id.get(task_id)
            if row is None:
                continue
            store_seen += 1
            if _task_row_durably_complete(row):
                completion_order = _completion_order(
                    _task_row_completion_timestamp_s(row),
                    entry_created_timestamp_s,
                )
                if completion_order == "equal":
                    if store_issue is None:
                        store_issue = "timestamp_equal"
                elif completion_order == "indeterminate":
                    store_issue = "timestamp_indeterminate"
                elif completion_order == "after":
                    store_complete += 1
    except (ImportError, OSError, goalflight_task.TaskError):
        # Missing bytes, or a corrupt/invalid tasks.jsonl row: we could not
        # find out. That is unavailable, not a settled complete/none verdict.
        # TaskError must not escape — one bad line used to abort the drain.
        store_complete = 0
        store_seen = 0
        store_issue = "task_store_unavailable"

    self_id = str(entry.get("dispatch_id") or (record or {}).get("dispatch_id") or "")
    self_root = _entry_owner_fields(entry, record)[1]
    ledger_complete, ledger_advanced, ledger_issue = _ledger_task_ids_advanced(
        task_ids,
        self_dispatch_id=self_id,
        entry_created_timestamp_s=entry_created_timestamp_s,
        self_project_root=self_root,
    )

    # Prefer explicit store truth when every linked id is present and complete.
    if store_seen == len(task_ids) and store_complete == len(task_ids):
        return "all_complete", None, ()
    if ledger_complete >= len(task_ids):
        return "all_complete", None, ()
    # Mix of store + ledger completions covering all ids.
    if store_complete + ledger_complete >= len(task_ids) and (store_complete > 0 or ledger_complete > 0):
        # Conservative: only when store_complete alone is full, or ledger alone.
        # Partial overlap of different ids is handled by some_advanced below.
        covered = store_complete  # already counted store-complete ids
        if covered >= len(task_ids):
            return "all_complete", None, ()

    advanced = max(store_complete, ledger_advanced, ledger_complete)
    # Also treat partial store completion as advanced.
    if store_complete > 0 and store_complete < len(task_ids):
        return "some_advanced", None, ()
    if 0 < advanced < len(task_ids) or (ledger_advanced > 0 and ledger_complete < len(task_ids)):
        return "some_advanced", None, ()
    if store_complete >= len(task_ids) or ledger_complete >= len(task_ids):
        return "all_complete", None, ()
    issues = tuple(
        issue
        for issue in (store_issue, ledger_issue if ledger_issue != "conclusive" else None)
        if issue is not None
    )
    if issues:
        if any(issue.endswith("_unavailable") for issue in issues):
            cause = "authority_unavailable"
        elif "timestamp_indeterminate" in issues:
            cause = "timestamp_indeterminate"
        else:
            cause = "timestamp_equal"
        return "indeterminate", cause, issues
    return "none", None, ()


def _linked_task_truth(
    entry: dict,
    record: dict | None = None,
    *,
    task_store_locked: bool = False,
) -> str:
    """Compatibility projection of the typed linked-task authority result."""
    truth, _cause, _sources = _linked_task_truth_detail(
        entry,
        record,
        task_store_locked=task_store_locked,
    )
    return truth


def _completion_authority_token_bind(entry: dict | None, record: dict | None) -> str:
    """Bind a terminal ledger row to this carrier: match, mismatch, or unknown.

    Dispatch ids are reusable once terminal. A new carrier with a new
    ``queue_launch_token`` must not inherit the previous attempt's complete
    row. Missing tokens cannot prove the bind, so the caller must not unlink.
    """
    entry_token = _queue_launch_token_from_entry(entry) if isinstance(entry, dict) else None
    raw = record.get("queue_launch_token") if isinstance(record, dict) else None
    record_token = str(raw) if raw not in (None, "") else None
    if not entry_token or not record_token:
        return "unknown"
    if entry_token == record_token:
        return "match"
    return "mismatch"


def _entry_completion_authority(
    entry: dict,
    record: dict | None = None,
    *,
    task_store_locked: bool = False,
) -> dict | None:
    """Full completion-authority ladder (design §Reconciliation).

    Returns a decision dict when ANY leg proves completion/finality:
      {"state": "complete"|"superseded", "reason": str, "marker": dict|None, "source": str}
    Returns None only when every leg is not-done (re-enqueue may still apply
    only if also provably pre-spawn).
    """
    dispatch_id = str(entry.get("dispatch_id") or "")
    if not isinstance(record, dict) and dispatch_id:
        try:
            record = _find_dispatch_record(dispatch_id)
        except Exception:
            return {
                "state": "deferred",
                "reason": "completion_authority_unavailable",
                "marker": None,
                "source": "authority",
                "indeterminate_cause": "authority_unavailable",
                "indeterminate_sources": ["ledger_record_unavailable"],
                "bounded_deferral": False,
            }

    # Leg 0: already-terminal ledger for this dispatch (first-terminal-wins).
    # Bind to queue_launch_token so a reused dispatch id cannot unlink the
    # current carrier on the strength of an earlier attempt's complete row.
    has_terminal_record = bool(dispatch_id and _dispatch_record_is_terminal(record))
    if has_terminal_record:
        existing = record or _find_dispatch_record(dispatch_id) or {}
        existing_state = str(existing.get("state") or existing.get("terminal_state") or "")
        if existing_state in {
            "complete",
            "superseded",
            "worker_dead",
        } | set(goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES):
            bind = _completion_authority_token_bind(entry, existing)
            if bind == "unknown":
                return {
                    "state": "deferred",
                    "reason": "completion_authority_token_indeterminate",
                    "marker": None,
                    "source": "ledger",
                    "indeterminate_cause": "queue_launch_token_unbound",
                    "indeterminate_sources": ["queue_launch_token"],
                    "bounded_deferral": False,
                }
            if bind == "match":
                return {
                    "state": existing_state,
                    "reason": "existing_terminal_record",
                    "marker": None,
                    "source": "ledger",
                }

    project_root = _project_root_for_entry(entry, record)
    marker = _scan_entry_completion_marker(entry)

    # Leg 1: worker terminal output marker (SUCCESS kinds).
    # Leg 2: direct-open of declared artifacts (required when paths are named).
    if marker and marker.get("kind") in SUCCESS_TERMINAL_MARKERS:
        if _marker_declared_artifacts_ok(marker, project_root):
            return {
                "state": "complete",
                "reason": f"marker:{marker.get('kind')}:final_reconciliation",
                "marker": marker,
                "source": "output",
            }
        # Declared artifacts missing → not completion authority; fall through.

    # Leg 3: linked task store / later-pass durable completion.
    task_truth, indeterminate_cause, indeterminate_sources = _linked_task_truth_detail(
        entry,
        record,
        task_store_locked=task_store_locked,
    )
    if task_truth == "all_complete":
        return {
            "state": "superseded",
            "reason": "task_store:all_complete",
            "marker": None,
            "source": "task_store",
            "resolution_source": "task_store",
        }
    if task_truth == "some_advanced":
        return {
            "state": "worker_dead",
            "reason": "partial_task_supersession",
            "marker": None,
            "source": "task_store",
            "salvage_required": True,
        }
    if task_truth == "indeterminate":
        bounded_equality = indeterminate_cause == "timestamp_equal"
        if bounded_equality and (
            _completion_authority_deferral_count(entry)
            >= MAX_COMPLETION_AUTHORITY_DEFERRALS
        ):
            # One locked re-read has already failed to order the events. No
            # fixed timestamp can make the next read more decisive, so restore
            # for launch: duplicate work is recoverable; a lost follow-up is not.
            return None
        if indeterminate_cause == "timestamp_indeterminate":
            # Missing/unparseable times are not equality: they have no bounded
            # restore, and they never become readable on their own. Park once
            # (visible, distinct reason). The next still-unreadable pass
            # terminalizes to blocked_completion_authority rather than stalling.
            if (
                str(entry.get("completion_authority_reason") or "")
                == COMPLETION_AUTHORITY_TIMESTAMP_UNREADABLE
            ):
                return {
                    "state": BLOCKED_COMPLETION_AUTHORITY_STATE,
                    "reason": COMPLETION_AUTHORITY_TIMESTAMP_UNREADABLE,
                    "marker": None,
                    "source": "authority",
                    "indeterminate_cause": indeterminate_cause,
                    "indeterminate_sources": list(indeterminate_sources),
                    "bounded_deferral": False,
                }
            return {
                "state": "deferred",
                "reason": COMPLETION_AUTHORITY_TIMESTAMP_UNREADABLE,
                "marker": None,
                "source": "authority",
                "indeterminate_cause": indeterminate_cause,
                "indeterminate_sources": list(indeterminate_sources),
                "bounded_deferral": False,
            }
        # An unreadable authority never consumes equality's escape budget. Its
        # carrier remains parked, and the unavailable reason is written onto the
        # claim, the status sidecar, and drain details. The park lifts when the
        # store or ledger can be read again (`completion_authority_readable`).
        reason = (
            "completion_authority_unavailable"
            if indeterminate_cause == "authority_unavailable"
            else "completion_authority_indeterminate"
        )
        return {
            "state": "deferred",
            "reason": reason,
            "marker": None,
            "source": "authority",
            "indeterminate_cause": indeterminate_cause,
            "indeterminate_sources": list(indeterminate_sources),
            "bounded_deferral": bounded_equality,
            **(
                {
                    "deferral_count": _completion_authority_deferral_count(entry),
                    "deferral_limit": MAX_COMPLETION_AUTHORITY_DEFERRALS,
                }
                if bounded_equality
                else {}
            ),
        }
    return None


def _completion_decision_blocks_restore(decision: dict | None) -> bool:
    if not isinstance(decision, dict):
        return False
    state = str(decision.get("state") or "")
    return state in {
        "complete",
        "superseded",
        "worker_dead",
        "deferred",
        BLOCKED_COMPLETION_AUTHORITY_STATE,
    } | set(goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES)


def _completion_decision_is_deferred(decision: dict | None) -> bool:
    return isinstance(decision, dict) and decision.get("state") == "deferred"


def _completion_decision_uses_bounded_deferral(decision: dict | None) -> bool:
    return _completion_decision_is_deferred(decision) and decision.get("bounded_deferral") is True


# Removed _task_store_mutation_lock: the bounded Q→S→L transaction now owns reconciliation lock ordering.


def try_acquire_task_store_lock(
    entry: dict,
    record: dict | None,
    *,
    deadline_s: float,
):
    root = SCRIPT_DIR.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import goalflight_task  # type: ignore

    store = goalflight_task.TaskStore(_project_root_for_entry(entry, record))
    return store.try_store_lock(deadline_s=deadline_s)


def try_acquire_ledger_lock(*, deadline_s: float):
    return goalflight_ledger.StateLock.try_acquire(deadline_s)


def acquire_reconcile_locks(
    txn: _ReconcileTransaction,
    *,
    queue_dir: Path,
    need_queue: bool,
    need_task_store: bool,
    need_ledger: bool,
) -> AcquireResult:
    """Acquire Q then S then L against one absolute 100 ms deadline."""
    deadline = time.monotonic() + RECONCILE_DOWNSTREAM_LOCK_BUDGET_S
    acquired: list[object] = []
    record = _find_dispatch_record(str(txn.entry.get("dispatch_id") or ""))
    try:
        if need_queue:
            handle = try_acquire_queue_lock(queue_dir, deadline_s=deadline)
            if handle is None:
                return AcquireResult.QUEUE_LOCK_DEFERRED
            acquired.append(handle)
            txn.queue_locked = True
        if need_task_store:
            handle = try_acquire_task_store_lock(txn.entry, record, deadline_s=deadline)
            if handle is None:
                return AcquireResult.TASK_STORE_LOCK_DEFERRED
            acquired.append(handle)
            txn.task_store_locked = True
        if need_ledger:
            handle = try_acquire_ledger_lock(deadline_s=deadline)
            if handle is None:
                return AcquireResult.LEDGER_LOCK_DEFERRED
            acquired.append(handle)
            txn.ledger_locked = True
        txn.downstream.extend(acquired)
        return AcquireResult.ACQUIRED_ALL
    except Exception:
        return AcquireResult.LOCK_ERROR_DEFERRED
    finally:
        if len(txn.downstream) < len(acquired):
            for handle in reversed(acquired):
                with contextlib.suppress(Exception):
                    handle.release()


def _producer_set_authoritative(entry: dict, admission: PreAdmitClass) -> bool:
    if admission is PreAdmitClass.STALE_NO_SPAWN:
        return True
    record = _find_dispatch_record(str(entry.get("dispatch_id") or ""))
    result = enumerate_token_producers(_entry_with_record_identity(entry, record))
    return result.state in {ProducerSetState.DEAD, ProducerSetState.PID_REUSED}


def _begin_reconcile_transaction(
    entry: dict,
    *,
    queue_dir: Path,
    stale_s: float,
    need_queue: bool,
    need_task_store: bool,
    need_ledger: bool,
    admission: PreAdmitClass | None = None,
) -> _BeginReconcileResult:
    admission = admission or classify_reconciliation_admission(entry, time.time(), stale_s=stale_s)
    if ADMISSION_DECISION[admission] is not AdmissionAction.ADMIT_TO_GATE:
        return _BeginReconcileResult(
            refusal=_ReconcileRefusal(
                ReconcileRefusalReason.PRE_ADMISSION_DEFERRED,
                admission,
                ADMISSION_DECISION[admission].value,
            )
        )
    record = _find_dispatch_record(str(entry.get("dispatch_id") or ""))
    tail = _entry_tail_path(entry, record)
    transport = _entry_transport(entry, record)
    node = _entry_node(entry, record)
    locality = "remote" if transport == "fleet" or node else "local"
    filesystem = _tail_filesystem_identity(tail, locality=locality)
    capability = _probe_flock_capability(
        transport=transport,
        node=node,
        tail_path=tail,
        filesystem=filesystem,
    )
    producer_authoritative = _producer_set_authoritative(entry, admission)
    mode = resolve_reconciliation_mode(
        transport=transport,
        node=node,
        locality=locality,
        tail_path=tail,
        tail_filesystem=filesystem,
        flock_probe=capability,
        producer_set_authoritative=producer_authoritative,
        node_authority_available=False,
        worker_tail_lock_contract=bool(entry.get("queue_tail_flock_contract", True)),
    )
    if mode in {ReconciliationMode.DEFER_TO_NODE, ReconciliationMode.FAIL_CLOSED_DEFER}:
        return _BeginReconcileResult(
            refusal=_ReconcileRefusal(
                ReconcileRefusalReason.RECONCILIATION_MODE_DEFERRED,
                admission,
                mode.value,
                mode=mode,
                transport=transport,
                locality=locality,
                capability=capability,
                filesystem=filesystem,
            )
        )
    txn = _ReconcileTransaction(entry, tail, admission, mode, filesystem, capability)
    gate = _tail_reconciliation_lock(tail) if mode is ReconciliationMode.LOCAL_FLOCK else _fallback_reconciliation_lock(tail)
    try:
        gate.__enter__()
    except (_TailLockBusy, OSError) as exc:
        return _BeginReconcileResult(
            refusal=_ReconcileRefusal(
                ReconcileRefusalReason.TAIL_GATE_DEFERRED,
                admission,
                type(exc).__name__,
                mode=mode,
                transport=transport,
                locality=locality,
                capability=capability,
                filesystem=filesystem,
            )
        )
    txn.tail_gate = gate
    acquire_result = acquire_reconcile_locks(
        txn,
        queue_dir=queue_dir,
        need_queue=need_queue,
        need_task_store=need_task_store,
        need_ledger=need_ledger,
    )
    if acquire_result is not AcquireResult.ACQUIRED_ALL:
        txn.release()
        return _BeginReconcileResult(
            refusal=_ReconcileRefusal(
                ReconcileRefusalReason.DOWNSTREAM_LOCK_DEFERRED,
                admission,
                acquire_result.value,
                mode=mode,
                transport=transport,
                locality=locality,
                capability=capability,
                filesystem=filesystem,
            )
        )
    return _BeginReconcileResult(transaction=txn)


def _reconcile_transaction_still_valid(
    txn: _ReconcileTransaction,
    fresh: dict,
) -> bool:
    if fresh.get("queue_launch_token") != txn.entry.get("queue_launch_token"):
        return False
    fresh_tail = _entry_tail_path(fresh, _find_dispatch_record(str(fresh.get("dispatch_id") or "")))
    if fresh_tail.expanduser().resolve(strict=False) != txn.tail.expanduser().resolve(strict=False):
        return False
    transport = _entry_transport(fresh)
    node = _entry_node(fresh)
    locality = "remote" if transport == "fleet" or node else "local"
    filesystem = _tail_filesystem_identity(fresh_tail, locality=locality)
    if filesystem != txn.filesystem:
        return False
    capability = _probe_flock_capability(
        transport=transport,
        node=node,
        tail_path=fresh_tail,
        filesystem=filesystem,
    )
    mode = resolve_reconciliation_mode(
        transport=transport,
        node=node,
        locality=locality,
        tail_path=fresh_tail,
        tail_filesystem=filesystem,
        flock_probe=capability,
        producer_set_authoritative=_producer_set_authoritative(fresh, txn.admission),
        node_authority_available=False,
        worker_tail_lock_contract=bool(fresh.get("queue_tail_flock_contract", True)),
    )
    if mode is not txn.mode:
        return False
    if mode is ReconciliationMode.FALLBACK_PID_IDENTITY:
        if txn.admission is PreAdmitClass.STALE_NO_SPAWN:
            return (
                classify_reconciliation_admission(fresh, time.time(), stale_s=0.0)
                is PreAdmitClass.STALE_NO_SPAWN
            )
        probe_entry = _entry_with_record_identity(
            fresh,
            _find_dispatch_record(str(fresh.get("dispatch_id") or "")),
        )
        first = enumerate_token_producers(probe_entry)
        second = enumerate_token_producers(probe_entry)
        if first.state not in {ProducerSetState.DEAD, ProducerSetState.PID_REUSED}:
            return False
        if second.state is not first.state or second.members != first.members:
            return False
    return True


# Removed _apply_completion_authority: its split compatibility path bypassed the single transaction owner.


def _entry_pre_spawn(entry: dict) -> bool:
    """True when the claim never crossed launch-start / spawn-intent / spawn."""
    return not (
        _queue_claim_launch_started(entry)
        or _queue_claim_worker_spawn_intent(entry)
        or _queue_claim_worker_spawned(entry)
    )


def _entry_pre_worker(entry: dict) -> bool:
    """True when no worker spawn was attempted.

    Launch-started is still pre-worker: the queue child stamps that *before*
    capacity acquire, so a capacity-blocked claim must remain restorable.
    """
    return not (
        _queue_claim_worker_spawn_intent(entry)
        or _queue_claim_worker_spawned(entry)
    )


def _ledger_is_restorable_prelaunch(record: dict | None) -> bool:
    """Whether a ledger row still names work that must return to QUEUED.

    `blocked_capacity` is a terminal *label* but a transient refusal: b-216
    made that state restorable so fire-and-forget does not discard the entry.
    """
    if not isinstance(record, dict):
        return False
    state = str(record.get("state") or "")
    if state == "blocked_capacity":
        return True
    if _dispatch_record_is_terminal(record):
        return False
    return state in PRE_WORKER_LEDGER_STATES


def _ledger_is_capacity_refusal(record: dict | None) -> bool:
    """True when the ledger names a transient capacity refusal, not a launch."""
    return isinstance(record, dict) and str(record.get("state") or "") in {
        "waiting_capacity",
        "blocked_capacity",
    }


def _claim_is_recovery_restorable(entry: dict, record: dict | None) -> bool:
    """Whether a stale claim may return to QUEUED without double-launch.

    Never-started claims stay restorable. Launch-started claims are restorable
    only as a capacity refusal: remote drain stamps launch-started *before*
    execute_dispatch, so treating every dead-launcher pre-worker claim as
    restorable would relaunch an in-flight remote worker.
    """
    if not list(entry.get("dispatch_argv") or []):
        return False
    if _claim_recovery_count(entry) >= MAX_CLAIM_RECOVERY_REQUEUES:
        return False
    if _entry_pre_spawn(entry):
        return True
    return _entry_pre_worker(entry) and _ledger_is_capacity_refusal(record)


def _controller_attribution_from_entry(
    entry: dict, record: dict | None = None
) -> dict:
    """Owner fields for a restored envelope or ledger row.

    Restore sanitizes claimer/launcher/worker identity. Drain's
    restore_prepared owner check and consent-based cleanup both read the
    ledger's controller_* fields, so dropping them mints an unattributable
    (hence immortal) queue entry.
    """
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    src = record if isinstance(record, dict) else {}

    def _first(*values: object) -> object:
        for value in values:
            if value is None:
                continue
            if isinstance(value, str) and not str(value).strip():
                continue
            return value
        return None

    label = _first(
        src.get("controller_label"),
        entry.get("controller_label"),
        request.get("controller_label"),
    )
    pid = _first(
        src.get("controller_pid"),
        entry.get("controller_pid"),
        request.get("controller_pid"),
        request.get("controller_beacon_pid"),
    )
    session = _first(
        src.get("controller_session_id"),
        entry.get("controller_session_id"),
        request.get("controller_session_id"),
    )
    identity = _first(
        src.get("controller_identity"),
        entry.get("controller_identity"),
        request.get("controller_identity"),
    )
    out: dict = {}
    if label is not None:
        out["controller_label"] = str(label)
    if pid is not None:
        try:
            out["controller_pid"] = int(pid)
        except (TypeError, ValueError):
            pass
    if session is not None:
        out["controller_session_id"] = str(session)
    if isinstance(identity, dict):
        out["controller_identity"] = identity
    return out


def _unlink_matching_restore_target(target: Path, fresh: dict) -> bool:
    """Drop a mid-restore or queued envelope for this dispatch. False on I/O conflict."""
    if not target.exists():
        return True
    try:
        abandoned = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not (
        isinstance(abandoned, dict)
        and abandoned.get("state") in {"restore_prepared", "queued"}
        and abandoned.get("dispatch_id") == fresh.get("dispatch_id")
        and abandoned.get("restore_txn_id")
    ):
        return True
    try:
        target.unlink()
    except OSError:
        return False
    return True


def _sanitize_restore_envelope(entry: dict, *, increment_recovery_count: bool) -> dict:
    restored = dict(entry)
    for key in (
        "queue_launch_token",
        "queue_launch_started",
        "queue_launch_started_at",
        "queue_claimer_pid",
        "queue_claimer_identity",
        "queue_launcher_pid",
        "queue_launcher_identity",
        "queue_worker_spawn_intent",
        "queue_worker_spawn_intent_at",
        "queue_worker_pid",
        "queue_worker_spawned_at",
        "queue_worker_identity",
        "queue_worker_pgid",
        "queue_worker_group_leader_identity",
        "queue_worker_identity_snapshot_at",
        "queue_producer_descendants",
        "queue_producer_group_contract",
        "queue_producer_group_contract_enforced",
        "queue_producer_group_contract_reason",
        "queue_tail_flock_contract",
        "completion_authority_reason",
        "completion_authority_parked_at",
        "unpark_event",
        "park_exit_state",
        "status_write_failed",
    ):
        restored.pop(key, None)
    if restored.get("reason") in COMPLETION_AUTHORITY_PARK_REASONS:
        restored.pop("reason", None)
    attribution = _controller_attribution_from_entry(restored)
    if attribution.get("controller_label"):
        restored["controller_label"] = attribution["controller_label"]
    if increment_recovery_count:
        restored["claim_recovery_count"] = _claim_recovery_count(entry) + 1
    return restored


def _completion_authority_park_fields(reason: str | None) -> dict:
    if reason == "completion_authority_unavailable":
        return {"unpark_event": COMPLETION_AUTHORITY_UNPARK_EVENT}
    if reason == COMPLETION_AUTHORITY_TIMESTAMP_UNREADABLE:
        return {"park_exit_state": BLOCKED_COMPLETION_AUTHORITY_STATE}
    return {}


def _write_parked_status(entry: dict, decision: dict) -> bool:
    """Write the parked-carrier status sidecar. False means the write did not land."""
    dispatch_id = str(entry.get("dispatch_id") or "")
    if not dispatch_id:
        return False
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    args = _queued_args_for_status(entry)
    status_json = Path(
        str(
            request.get("status_json")
            or _dispatch_base_dir() / f"{dispatch_id}.status.json"
        )
    )
    tail = Path(
        str(request.get("tail") or _dispatch_base_dir() / f"{dispatch_id}.tail")
    )
    reason = str(decision.get("reason") or "completion_authority_unavailable")
    try:
        write_status(
            status_json,
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": dispatch_id,
                "agent": args.agent,
                "shape": args.shape,
                "state": "queued",
                "reason": reason,
                **_completion_authority_park_fields(reason),
                "project_root": str(
                    entry.get("project_root") or request.get("cwd") or Path.cwd()
                ),
                "worker_pid": None,
                "worker_alive": False,
                "tail_path": str(tail),
                "status_path": str(status_json),
                "updated_at": int(time.time()),
                **({"task_ids": list(args.task_ids)} if getattr(args, "task_ids", None) else {}),
            },
        )
    except OSError:
        return False
    return True


def _persist_completion_authority_park(
    claim: Path,
    entry: dict,
    decision: dict,
) -> bool:
    """Persist an authority park onto the claim the operator reads.

    This is not equality's bounded deferral: it never consumes that budget.
    Unreadable store/ledger parks lift when those bytes can be read again
    (`unpark_event=completion_authority_readable`). Missing/unparseable times
    park once, then terminalize (`park_exit_state=blocked_completion_authority`).
    """
    reason = str(decision.get("reason") or "completion_authority_unavailable")
    staged = dict(entry)
    staged["reason"] = reason
    staged["completion_authority_reason"] = reason
    staged["completion_authority_parked_at"] = goalflight_ledger.utc_now()
    staged["updated_at"] = staged["completion_authority_parked_at"]
    staged.update(_completion_authority_park_fields(reason))
    try:
        _write_json_atomic(claim, staged)
    except OSError:
        return False
    if not _write_parked_status(staged, decision):
        staged["status_write_failed"] = True
        try:
            _write_json_atomic(claim, staged)
        except OSError:
            pass
    entry.clear()
    entry.update(staged)
    return True


def _persist_completion_authority_deferral(claim: Path, entry: dict) -> int | None:
    """Persist exact-timestamp-equality's bounded ambiguity budget."""
    staged = dict(entry)
    count = min(
        MAX_COMPLETION_AUTHORITY_DEFERRALS,
        _completion_authority_deferral_count(entry) + 1,
    )
    staged["completion_authority_deferral_count"] = count
    staged["completion_authority_deferred_at"] = goalflight_ledger.utc_now()
    staged["updated_at"] = staged["completion_authority_deferred_at"]
    try:
        _write_json_atomic(claim, staged)
    except OSError:
        return None
    entry.clear()
    entry.update(staged)
    return count


def _commit_restore_transaction(
    txn: _ReconcileTransaction,
    claim: Path,
    fresh: dict,
    *,
    increment_recovery_count: bool,
    reason: str,
) -> tuple[Path | None, dict | None]:
    """Prepared queue → queued ledger commit → publish queue → unlink claim."""
    if not (txn.queue_locked and txn.ledger_locked):
        return None, None
    if not _reconcile_transaction_still_valid(txn, fresh):
        return None, None
    target = claim.parent / claim.name.split(".claimed-", 1)[0]
    record = _find_dispatch_record(str(fresh.get("dispatch_id") or ""))
    decision = _entry_completion_authority(fresh, task_store_locked=txn.task_store_locked)
    if _completion_decision_is_deferred(decision):
        if not _completion_decision_uses_bounded_deferral(decision):
            persisted = False
            if str(decision.get("reason") or "") in COMPLETION_AUTHORITY_PARK_REASONS:
                persisted = _persist_completion_authority_park(claim, fresh, decision)
            return None, {**decision, "deferral_persisted": persisted}
        persisted_count = _persist_completion_authority_deferral(claim, fresh)
        return None, {
            **decision,
            "deferral_count": persisted_count,
            "deferral_persisted": persisted_count is not None,
        }
    if _completion_decision_blocks_restore(decision):
        if not _unlink_matching_restore_target(target, fresh):
            return None, None
        return None, decision
    if (
        record is not None
        and _dispatch_record_is_terminal(record)
        and str(record.get("state") or "") not in {"blocked_capacity"}
        and _completion_authority_token_bind(fresh, record) == "match"
    ):
        if not _unlink_matching_restore_target(target, fresh):
            return None, None
        return None, {
            "state": str(record.get("state") or record.get("terminal_state") or "complete"),
            "reason": "existing_terminal_record",
            "source": "ledger",
        }

    restore_txn_id: str | None = None
    prepared: dict
    if target.exists():
        try:
            prepared = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        if not isinstance(prepared, dict):
            return None, None
        restore_txn_id = str(prepared.get("restore_txn_id") or "") or None
        if not restore_txn_id:
            return None, None
        if prepared.get("state") == "queued":
            if not record or record.get("restore_txn_id") != restore_txn_id or record.get("state") != "queued":
                return None, None
            try:
                if claim.exists():
                    claim.unlink()
            except OSError:
                return None, None
            return target, None
        if prepared.get("state") != "restore_prepared":
            return None, None
    elif record is not None and str(record.get("state") or "") == "queued":
        # Ledger already names queued work: the restore commit point landed
        # (submit, or a previous release). Publishing restore_prepared here
        # is unadoptable — drain skips those files, and a new txn id used
        # to abort without flipping them to queued. Republish as queued.
        restore_txn_id = str(record.get("restore_txn_id") or "") or uuid.uuid4().hex
        prepared = _sanitize_restore_envelope(
            fresh,
            increment_recovery_count=increment_recovery_count,
        )
        prepared.update(
            {
                "state": "queued",
                "restore_txn_id": restore_txn_id,
                "restore_reason": reason,
                "updated_at": goalflight_ledger.utc_now(),
            }
        )
        try:
            _write_json_atomic(target, prepared)
        except OSError:
            return None, None
        if record.get("restore_txn_id") != restore_txn_id:
            ledger_record = dict(record)
            ledger_record.update(
                {
                    "restore_txn_id": restore_txn_id,
                    "restore_reason": reason,
                    "queue_path": str(target),
                    "claim_recovery_count": int(prepared.get("claim_recovery_count") or 0),
                }
            )
            for key, value in _controller_attribution_from_entry(fresh, record).items():
                if not ledger_record.get(key):
                    ledger_record[key] = value
            try:
                goalflight_ledger.write_record(ledger_record)
            except OSError:
                # Envelope is already launchable; the txn stamp can retry.
                pass
        try:
            if claim.exists():
                claim.unlink()
        except OSError:
            return target, None
        return target, None
    else:
        restore_txn_id = uuid.uuid4().hex
        prepared = _sanitize_restore_envelope(
            fresh,
            increment_recovery_count=increment_recovery_count,
        )
        prepared.update(
            {
                "state": "restore_prepared",
                "restore_txn_id": restore_txn_id,
                "restore_reason": reason,
                "updated_at": goalflight_ledger.utc_now(),
            }
        )
        try:
            _write_json_atomic(target, prepared)
        except OSError:
            return None, None

    # A prepared queue envelope is resumable. If a crash happened before the
    # ledger commit point, complete that same transaction id now. Under Q+L
    # this restorer owns the dispatch: a queued ledger row with a different
    # restore_txn_id is a *previous* restore of the same work, not a
    # concurrent foreign transaction, and must be advanced to queued.
    if not record or record.get("restore_txn_id") != restore_txn_id or record.get("state") != "queued":
        ledger_record = record or _new_reconciliation_record(fresh)
        if (
            _dispatch_record_is_terminal(ledger_record)
            and str(ledger_record.get("state") or "") not in {"blocked_capacity"}
            and _completion_authority_token_bind(fresh, ledger_record) == "match"
        ):
            with contextlib.suppress(OSError):
                target.unlink()
            return None, {
                "state": str(ledger_record.get("state") or ledger_record.get("terminal_state")),
                "reason": "existing_terminal_record",
                "source": "ledger",
            }
        ledger_record.update(
            {
                "state": "queued",
                "terminal_state": "unknown",
                "liveness_state": "queued",
                "worker_still_alive": False,
                "queue_launch_token": None,
                "queue_path": str(target),
                "restore_reason": reason,
                "restore_txn_id": restore_txn_id,
                "claim_recovery_count": int(prepared.get("claim_recovery_count") or 0),
            }
        )
        for key, value in _controller_attribution_from_entry(fresh, ledger_record).items():
            if not ledger_record.get(key):
                ledger_record[key] = value
        try:
            goalflight_ledger.write_record(ledger_record)
        except OSError:
            # Preserve the prepared carrier for a later retry on transient I/O.
            # Invalid ledger records are caller bugs and must escape.
            return None, None

    # Durable ledger row above is the cross-file commit point.
    try:
        published = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(published, dict) or published.get("restore_txn_id") != restore_txn_id:
            return None, None
        published["state"] = "queued"
        published["updated_at"] = goalflight_ledger.utc_now()
        if not published.get("controller_label"):
            label = _controller_attribution_from_entry(fresh, record).get("controller_label")
            if label:
                published["controller_label"] = label
        _write_json_atomic(target, published)
        if claim.exists():
            claim.unlink()
    except (OSError, json.JSONDecodeError):
        return None, None
    return target, None


def _bounded_restore_claim(claim: Path, entry: dict, queue_dir: Path) -> tuple[bool, dict | None]:
    """Stale recovery restore under frozen T→Q→S→L ordering."""
    target_name = claim.name.split(".claimed-", 1)[0]
    target = queue_dir / target_name
    if target.exists():
        try:
            existing_target = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, None
        if not isinstance(existing_target, dict) or existing_target.get("state") not in {"restore_prepared", "queued"}:
            return False, None
    if not list(entry.get("dispatch_argv") or []):
        return False, None
    if _claim_recovery_count(entry) >= MAX_CLAIM_RECOVERY_REQUEUES:
        return False, None
    begin = _begin_reconcile_transaction(
        entry,
        queue_dir=queue_dir,
        stale_s=0.0,
        need_queue=True,
        need_task_store=_is_task_linked(entry, _find_dispatch_record(str(entry.get("dispatch_id") or ""))),
        need_ledger=True,
    )
    txn = begin.transaction
    if txn is None:
        return False, None
    try:
        try:
            fresh = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, None
        if not isinstance(fresh, dict):
            return False, None
        if not _claim_is_recovery_restorable(fresh, _find_dispatch_record(str(fresh.get("dispatch_id") or ""))):
            return False, None
        restored, decision = _commit_restore_transaction(
            txn,
            claim,
            fresh,
            increment_recovery_count=True,
            reason="stale_claim_recovery",
        )
        return restored is not None, decision
    finally:
        txn.release()


def _restore_claim_if_incomplete(
    claim: Path,
    entry: dict,
    queue_dir: Path,
    *,
    stale_s: float = QUEUE_CLAIM_STALE_S,
) -> tuple[Path | None, dict | None]:
    """Normal-drain restore through the same held T→Q→S→L transaction."""
    try:
        observed = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(observed, dict) or observed.get("queue_launch_token") != entry.get("queue_launch_token"):
        return None, None
    begin = _begin_reconcile_transaction(
        observed,
        queue_dir=queue_dir,
        stale_s=0.0,
        need_queue=True,
        # Freeze a task link that may be landing even when the first read is
        # unlinked. Quarantine decisions later in this transaction depend on
        # absence of linkage, so absence needs the same S lock as presence.
        need_task_store=True,
        need_ledger=True,
    )
    txn = begin.transaction
    if txn is None:
        return None, None
    try:
        try:
            fresh = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        if not isinstance(fresh, dict) or fresh.get("queue_launch_token") != entry.get("queue_launch_token"):
            return None, None
        if not _reconcile_transaction_still_valid(txn, fresh):
            return None, None
        if classify_reconciliation_admission(fresh, time.time(), stale_s=0.0) is not txn.admission:
            return None, None
        restored, decision = _commit_restore_transaction(
            txn,
            claim,
            fresh,
            increment_recovery_count=False,
            reason="normal_drain_restore",
        )
        if decision is not None:
            if _completion_decision_is_deferred(decision):
                return None, decision
            record = _find_dispatch_record(str(fresh.get("dispatch_id") or ""))
            if not _is_task_linked(fresh, record):
                if _quarantine_unlinked_claim_if_observed_orphan(
                    claim,
                    fresh,
                    record,
                    reason="normal_restore_completion_unlinked",
                    stale_s=stale_s,
                ):
                    return None, {**decision, "_committed": True, "state": "quarantined"}
                return None, None
            result, _marker = _commit_claim_terminal_in_txn(
                txn,
                fresh,
                reason=str(decision.get("reason") or "completion_authority"),
                force_state=str(decision.get("state") or "complete"),
                salvage_required=bool(decision.get("salvage_required")),
                resolution_source=decision.get("resolution_source"),
            )
            if not result.committed:
                return None, None
            with contextlib.suppress(OSError):
                claim.unlink()
            return None, {**decision, "_committed": True, "state": result.durable_state}
        return restored, None
    finally:
        txn.release()


def _commit_claim_terminal_in_txn(
    txn: _ReconcileTransaction,
    entry: dict,
    *,
    reason: str,
    force_state: str | None = None,
    salvage_required: bool = False,
    resolution_source: str | None = None,
) -> tuple[TerminalCommitResult, dict | None]:
    args = _queued_args_for_status(entry)
    prompt_path = str(Path(args.prompt_file).expanduser()) if args.prompt_file else None
    tail = _entry_tail_path(entry)
    state, final_reason, marker = _resolve_claim_terminal_outcome(
        entry,
        reason=reason,
        tail=tail,
        ignore_prefix_lines=_ignore_prefix_lines(prompt_path),
        agent=args.agent,
    )
    if (
        force_state in {"complete", "superseded", BLOCKED_COMPLETION_AUTHORITY_STATE}
        and state == "worker_dead"
    ):
        state = force_state
        final_reason = reason
    result = commit_reconciled_terminal(
        txn,
        entry,
        {
            "state": state,
            "reason": final_reason,
            "salvage_required": salvage_required,
            "resolution_source": resolution_source,
        },
    )
    return result, marker


def _write_reconciled_terminal_status(entry: dict, marker: dict | None) -> None:
    """Idempotent post-commit mirror sourced only from durable ledger truth."""
    dispatch_id = str(entry.get("dispatch_id") or "")
    record = _find_dispatch_record(dispatch_id)
    if not record or not _dispatch_record_is_terminal(record):
        return
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    args = _queued_args_for_status(entry)
    project_root = goalflight_task.resolve_project_root(
        str(entry.get("project_root") or request.get("cwd") or Path.cwd())
    )
    status_json = Path(str(request.get("status_json") or record.get("status_path") or _dispatch_base_dir() / f"{dispatch_id}.status.json"))
    tail = Path(str(request.get("tail") or record.get("stdout_path") or _dispatch_base_dir() / f"{dispatch_id}.tail"))
    state = str(record.get("state") or record.get("terminal_state") or "worker_dead")
    reason = record.get("reason") or record.get("error") or "claim_reconciliation"
    # Derived post-commit mirror only: losing it gives up status freshness, not
    # the durable terminal journal row or its outbox event. Reconciliation will
    # rebuild it, so this must not unwind the already-committed queue cleanup.
    with contextlib.suppress(Exception):
        write_status(
            status_json,
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": dispatch_id,
                "agent": args.agent,
                "shape": args.shape,
                "state": state,
                "terminal_state": str(record.get("terminal_state") or goalflight_ledger.terminal_state_for(state, reason)),
                "reason": reason,
                "liveness_state": goalflight_terminal.terminal_liveness_state(state),
                "terminal_marker": marker,
                "project_root": str(project_root),
                "worker_pid": entry.get("queue_worker_pid"),
                "worker_alive": False,
                "tail_path": str(tail),
                "status_path": str(status_json),
                "updated_at": int(time.time()),
                "reconciliation": record.get("outcome", {}).get("reconciliation", {}),
                **({"task_ids": list(args.task_ids)} if getattr(args, "task_ids", None) else {}),
            },
        )


def _fleet_terminal_accounted_record(record: dict | None, token: str) -> bool:
    """Terminal fleet ledger record with a matching launch token.

    The remote node is the liveness authority for a fleet dispatch; once its
    ledger record is terminal the dispatch is provably finished and accounted
    for, so the claim carrier is a redundant tombstone.

    A "launch_unconfirmed" state is NOT terminal accounting: the launch was
    attempted but no receipt confirms it started, and the generic
    terminal_state_for fallback would misread that unknown state as terminal
    ("error"). Exclude it so an ambiguous, potentially still-live remote
    launch keeps its carrier until a real receipt or terminal record exists.
    """
    return bool(
        isinstance(record, dict)
        and record.get("transport") == "fleet-ssh"
        and record.get("queue_launch_token") == token
        and record.get("state") != "launch_unconfirmed"
        and _dispatch_record_is_terminal(record)
    )


def _positive_live_carrier_cleanup_result(
    claim: Path,
    entry: dict,
    queue_dir: Path,
    *,
    worker_record_sufficient: bool = False,
    stale_s: float = QUEUE_CLAIM_STALE_S,
) -> _ClaimReconcileResult:
    """Narrow positive-accounting exception: Q-only redundant-carrier cleanup
    after revalidation. A carrier is clearable only when its dispatch is
    provably accounted for by the ledger:

    - local: a token-matched live non-terminal worker record;
    - fleet: a token-matched fleet-ssh record that is non-terminal with a
      durable remote launch receipt (via _dispatch_has_worker_record), or
      genuinely terminal. An ambiguous launch (launch_unconfirmed, no
      receipt) is NOT accounting: it defers like any unaccounted dispatch.

    Anything less returns "pending" so a still-unaccounted dispatch — local
    or remote — keeps its carrier and can never look re-launchable.

    worker_record_sufficient is for the drain's own post-launch cleanup: the
    drain has just confirmed the launch against the ledger (token-matched
    worker/receipt record), so the carrier is redundant even when a fast
    worker and its launcher have already exited — that fresh ledger
    confirmation supersedes the local worker-liveness and admission
    re-checks, and reconcile terminalizes the zombie from the ledger.
    Recovery callers keep both strict gates, and an ambiguous fleet launch
    (launch_unconfirmed, no receipt) still never qualifies.
    """
    dispatch_id = str(entry.get("dispatch_id") or "")
    token = _queue_launch_token_from_entry(entry)
    record = _find_dispatch_record(dispatch_id) if dispatch_id else None
    if not (
        dispatch_id
        and token
        and (
            _dispatch_has_worker_record(
                dispatch_id,
                queue_launch_token=token,
                require_live_nonterminal=not worker_record_sufficient,
            )
            or _fleet_terminal_accounted_record(record, token)
        )
    ):
        return _ClaimReconcileResult("pending", pending_reason="positive_accounting_absent")
    try:
        handle = try_acquire_queue_lock(
            queue_dir,
            deadline_s=time.monotonic() + RECONCILE_DOWNSTREAM_LOCK_BUDGET_S,
        )
    except OSError:
        return _ClaimReconcileResult("pending", pending_reason="queue_lock_error_deferred")
    if handle is None:
        return _ClaimReconcileResult("pending", pending_reason="queue_lock_deferred")
    try:
        try:
            fresh = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _ClaimReconcileResult("pending", pending_reason="claim_read_deferred")
        if not isinstance(fresh, dict):
            return _ClaimReconcileResult("pending", pending_reason="claim_payload_invalid")
        if not worker_record_sufficient and classify_reconciliation_admission(
            fresh, time.time(), stale_s=0.0
        ) not in (
            PreAdmitClass.LIVE,
            PreAdmitClass.REMOTE_AUTHORITY_REQUIRED,
        ):
            return _ClaimReconcileResult("pending", pending_reason="admission_revalidation_deferred")
        if fresh.get("queue_launch_token") != token:
            return _ClaimReconcileResult("pending", pending_reason="launch_token_changed")
        if not (
            _dispatch_has_worker_record(
                dispatch_id,
                queue_launch_token=token,
                require_live_nonterminal=not worker_record_sufficient,
            )
            or _fleet_terminal_accounted_record(_find_dispatch_record(dispatch_id), token)
        ):
            return _ClaimReconcileResult("pending", pending_reason="positive_accounting_changed")
        record = _find_dispatch_record(dispatch_id)
        if not _is_task_linked(fresh, record):
            # Missing task linkage is not orphan evidence. When the exact
            # token-matched ledger worker is identity-live and non-terminal,
            # the claim is only a redundant launch carrier; clearing it cannot
            # replay or stop the worker. Any weaker accounting stays pending.
            if not (
                _dispatch_record_has_live_nonterminal_worker(record)
                or (
                    worker_record_sufficient
                    and record is not None
                    and not _dispatch_record_is_terminal(record)
                )
            ):
                return _ClaimReconcileResult("pending", pending_reason="unlinked_accounting_insufficient")
        claim.unlink()
        return _ClaimReconcileResult("cleared")
    except OSError:
        return _ClaimReconcileResult("pending", pending_reason="carrier_unlink_deferred")
    finally:
        handle.release()


def _positive_live_carrier_cleanup(
    claim: Path,
    entry: dict,
    queue_dir: Path,
    *,
    worker_record_sufficient: bool = False,
    stale_s: float = QUEUE_CLAIM_STALE_S,
) -> str:
    """Compatibility wrapper for callers that need only the cleanup action."""
    return _positive_live_carrier_cleanup_result(
        claim,
        entry,
        queue_dir,
        worker_record_sufficient=worker_record_sufficient,
        stale_s=stale_s,
    ).action


def _reconcile_claim_transaction(
    claim: Path,
    entry: dict,
    *,
    queue_dir: Path,
    reason: str,
    stale_s: float,
) -> _ClaimReconcileResult:
    admission = classify_reconciliation_admission(entry, time.time(), stale_s=stale_s)
    if admission is PreAdmitClass.LIVE:
        return _positive_live_carrier_cleanup_result(
            claim,
            entry,
            queue_dir,
            stale_s=stale_s,
        )
    if admission is PreAdmitClass.INDETERMINATE:
        _alert_identity_indeterminate(
            str(entry.get("dispatch_id") or claim.name),
            where="claim",
            reason="pre_admit_indeterminate",
        )
    if ADMISSION_DECISION[admission] is AdmissionAction.DEFER_UNCHANGED:
        if admission is PreAdmitClass.REMOTE_AUTHORITY_REQUIRED:
            # The remote node remains the liveness authority: never restore or
            # terminalize a remote-authority carrier from this ladder. But a
            # carrier whose fleet dispatch is positively accounted for (durable
            # remote launch receipt or terminal ledger record) is a redundant
            # tombstone — clear it under the same revalidation as the LIVE
            # case. Unaccounted carriers return "pending" here, which is
            # exactly DEFER_UNCHANGED.
            return _positive_live_carrier_cleanup_result(
                claim,
                entry,
                queue_dir,
                stale_s=stale_s,
            )
        return _ClaimReconcileResult(
            "pending",
            refusal=_ReconcileRefusal(
                ReconcileRefusalReason.PRE_ADMISSION_DEFERRED,
                admission,
                ADMISSION_DECISION[admission].value,
            ),
        )

    record = _find_dispatch_record(str(entry.get("dispatch_id") or ""))
    linked = _is_task_linked(entry, record)
    begin = _begin_reconcile_transaction(
        entry,
        queue_dir=queue_dir,
        stale_s=stale_s,
        need_queue=True,
        # A task link can land after claim creation. Hold S even while the
        # record is currently unlinked so a destructive orphan decision cannot
        # race the task-link transaction.
        need_task_store=True,
        # Absence of a ledger row is also evidence. Freeze both presence and
        # absence before any unlinked quarantine decision.
        need_ledger=True,
        admission=admission,
    )
    txn = begin.transaction
    if txn is None:
        assert begin.refusal is not None
        return _ClaimReconcileResult("pending", refusal=begin.refusal)
    mirror: tuple[dict, Path] | None = None
    terminal_mirror: tuple[dict, dict | None] | None = None
    try:
        try:
            fresh = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _ClaimReconcileResult("pending", pending_reason="claim_read_deferred")
        if not isinstance(fresh, dict) or not _reconcile_transaction_still_valid(txn, fresh):
            return _ClaimReconcileResult("pending", pending_reason="transaction_revalidation_deferred")
        if classify_reconciliation_admission(fresh, time.time(), stale_s=stale_s) is not admission:
            return _ClaimReconcileResult("pending", pending_reason="admission_changed_deferred")
        record = _find_dispatch_record(str(fresh.get("dispatch_id") or ""))
        linked = _is_task_linked(fresh, record)
        carrier_stamp = str(fresh.get("orphan_first_seen_at") or "") or None
        if (
            carrier_stamp
            and record is not None
            and not _dispatch_record_is_terminal(record)
            and not record.get("orphan_first_seen_at")
        ):
            if _stamp_ledger_orphan_first_seen(record, txn=txn, stamp=carrier_stamp) is None:
                return _ClaimReconcileResult("pending", pending_reason="ledger_orphan_stamp_deferred")
            record = _find_dispatch_record(str(fresh.get("dispatch_id") or ""))
        age_stamp = _launch_age_timestamp_s(fresh)
        if age_stamp is None and not (
            admission is PreAdmitClass.STALE_NO_SPAWN and stale_s <= 0
        ):
            stamp = _persist_orphan_first_seen(fresh)
            staged = dict(fresh)
            staged["orphan_first_seen_at"] = stamp
            staged["updated_at"] = goalflight_ledger.utc_now()
            try:
                _write_json_atomic(claim, staged)
            except OSError:
                return _ClaimReconcileResult("pending", pending_reason="carrier_orphan_stamp_write_deferred")
            if record is not None and txn.ledger_locked:
                if _stamp_ledger_orphan_first_seen(record, txn=txn, stamp=stamp) is None:
                    return _ClaimReconcileResult("pending", pending_reason="ledger_orphan_stamp_deferred")
            return _ClaimReconcileResult("pending", pending_reason="orphan_stale_window_started")
        if age_stamp is not None and max(0.0, time.time() - age_stamp) < max(0.0, stale_s):
            return _ClaimReconcileResult("pending", pending_reason="orphan_not_stale")
        if (
            record is not None
            and _dispatch_record_is_terminal(record)
            and not _ledger_is_restorable_prelaunch(record)
            and _completion_authority_token_bind(fresh, record) == "match"
        ):
            if not linked:
                quarantined_path = _quarantine_unlinked_claim_if_observed_orphan(
                    claim,
                    fresh,
                    record,
                    reason=f"{reason}_unlinked_terminal",
                    stale_s=stale_s,
                )
                return (
                    _ClaimReconcileResult("quarantined")
                    if quarantined_path
                    else _ClaimReconcileResult("pending", pending_reason="terminal_unlinked_quarantine_deferred")
                )
            try:
                claim.unlink()
            except OSError:
                return _ClaimReconcileResult("pending", pending_reason="terminal_carrier_unlink_deferred")
            return _ClaimReconcileResult("cleared")

        decision = _entry_completion_authority(
            fresh,
            record,
            task_store_locked=txn.task_store_locked,
        )
        if _completion_decision_blocks_restore(decision):
            if _completion_decision_is_deferred(decision):
                if not _completion_decision_uses_bounded_deferral(decision):
                    if str(decision.get("reason") or "") in COMPLETION_AUTHORITY_PARK_REASONS:
                        _persist_completion_authority_park(claim, fresh, decision)
                    return _ClaimReconcileResult(
                        "pending",
                        pending_reason=str(
                            decision.get("reason") or "completion_authority_deferred"
                        ),
                    )
                persisted_count = _persist_completion_authority_deferral(
                    claim,
                    fresh,
                )
                if persisted_count is None:
                    return _ClaimReconcileResult(
                        "pending",
                        pending_reason="completion_authority_deferral_write_deferred",
                    )
                return _ClaimReconcileResult("pending", pending_reason="completion_authority_deferred")
            result, _marker = _commit_claim_terminal_in_txn(
                txn,
                fresh,
                reason=str(decision.get("reason") or reason),
                force_state=str(decision.get("state") or "complete"),
                salvage_required=bool(decision.get("salvage_required")),
                resolution_source=decision.get("resolution_source"),
            )
            if not result.committed:
                return _ClaimReconcileResult("pending", pending_reason="terminal_commit_deferred")
            terminal_mirror = (fresh, _marker)
            if not linked:
                record = _find_dispatch_record(str(fresh.get("dispatch_id") or ""))
                quarantined_path = _quarantine_unlinked_claim_if_observed_orphan(
                    claim,
                    fresh,
                    record,
                    reason=f"{reason}_unlinked_completion",
                    stale_s=stale_s,
                )
                return (
                    _ClaimReconcileResult("quarantined")
                    if quarantined_path
                    else _ClaimReconcileResult("pending", pending_reason="completion_unlinked_quarantine_deferred")
                )
            try:
                claim.unlink()
            except OSError:
                return _ClaimReconcileResult("pending", pending_reason="completed_carrier_unlink_deferred")
            return _ClaimReconcileResult("cleared")

        restorable = _claim_is_recovery_restorable(fresh, record)
        # Unlinked never-started claims with no known dispatch stay on the
        # quarantine path (b-065). A waiting_capacity / blocked_capacity ledger
        # row is a known transient refusal: restore it so an UNLINKED capacity
        # refusal can become eligible again instead of pending forever.
        if restorable and (
            linked
            or _ledger_is_capacity_refusal(record)
            or (_entry_pre_spawn(fresh) and _ledger_is_restorable_prelaunch(record))
        ):
            restored, locked_decision = _restore_claimed_entry(
                claim,
                fresh,
                txn=txn,
                increment_recovery_count=True,
                reason=reason,
            )
            if restored is not None:
                mirror = (_sanitize_restore_envelope(fresh, increment_recovery_count=True), restored)
                return _ClaimReconcileResult("restored")
            if locked_decision is not None:
                if not linked:
                    quarantined_path = _quarantine_unlinked_claim_if_observed_orphan(
                        claim,
                        fresh,
                        record,
                        reason=f"{reason}_unlinked_restore_blocked",
                        stale_s=stale_s,
                    )
                    return (
                        _ClaimReconcileResult("quarantined")
                        if quarantined_path
                        else _ClaimReconcileResult("pending", pending_reason="unlinked_restore_blocked")
                    )
                result, _marker = _commit_claim_terminal_in_txn(
                    txn,
                    fresh,
                    reason=str(locked_decision.get("reason") or reason),
                    force_state=str(locked_decision.get("state") or "complete"),
                )
                if result.committed:
                    terminal_mirror = (fresh, _marker)
                    with contextlib.suppress(OSError):
                        claim.unlink()
                    return _ClaimReconcileResult("cleared")
                return _ClaimReconcileResult("pending", pending_reason="restore_terminal_commit_deferred")

        if not linked:
            quarantined_path = _quarantine_unlinked_claim_if_observed_orphan(
                claim,
                fresh,
                record,
                reason=f"{reason}_unlinked",
                stale_s=stale_s,
            )
            return (
                _ClaimReconcileResult("quarantined")
                if quarantined_path
                else _ClaimReconcileResult("pending", pending_reason="unlinked_quarantine_deferred")
            )

        target = queue_dir / claim.name.split(".claimed-", 1)[0]
        if target.exists():
            try:
                existing_target = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return _ClaimReconcileResult(
                    "pending", pending_reason="duplicate_target_unreadable"
                )
            # A published queued envelope is a true duplicate of this claim.
            # restore_prepared is a mid-restore transaction: unlinking the
            # claim would leave an envelope drain will never launch.
            if (
                isinstance(existing_target, dict)
                and existing_target.get("state") == "queued"
                and existing_target.get("dispatch_id") == fresh.get("dispatch_id")
            ):
                try:
                    claim.unlink()
                except OSError:
                    return _ClaimReconcileResult(
                        "pending", pending_reason="duplicate_target_unlink_deferred"
                    )
                return _ClaimReconcileResult("cleared")
            return _ClaimReconcileResult(
                "pending", pending_reason="restore_prepared_in_progress"
            )

        terminal_reason = (
            "claim_recovery_exhausted"
            if _entry_pre_worker(fresh) and _claim_recovery_count(fresh) >= MAX_CLAIM_RECOVERY_REQUEUES
            else reason
        )
        result, _marker = _commit_claim_terminal_in_txn(txn, fresh, reason=terminal_reason)
        if not result.committed:
            return _ClaimReconcileResult("pending", pending_reason="terminal_commit_deferred")
        terminal_mirror = (fresh, _marker)
        try:
            claim.unlink()
        except OSError:
            return _ClaimReconcileResult("pending", pending_reason="terminal_carrier_unlink_deferred")
        return _ClaimReconcileResult("cleared")
    finally:
        txn.release()
        if mirror is not None:
            mirror_entry, mirror_path = mirror
            _restore_queued_record_from_entry(mirror_entry, mirror_path)
        if terminal_mirror is not None:
            mirror_entry, marker = terminal_mirror
            _write_reconciled_terminal_status(mirror_entry, marker)


def _act_on_orphan_claim(
    claim: Path,
    entry: dict,
    *,
    queue_dir: Path,
    reason: str,
) -> str:
    """Compatibility wrapper for the single transaction owner."""
    return _reconcile_claim_transaction(
        claim,
        entry,
        queue_dir=queue_dir,
        reason=reason,
        stale_s=0.0,
    ).action


def _resume_restore_prepared_envelopes(queue_dir: Path) -> dict:
    """Finish mid-restore queue envelopes whose claim is gone.

    ``_commit_restore_transaction`` writes ``restore_prepared`` to the
    unclaimed path before the ledger commit. Drain skips those files as
    launch candidates, waiting for the claim-side retry. If the claim was
    cleared, that retry never runs. Completing the same transaction is not
    expiry: a queued ledger row is the commit point, a terminal ledger row
    drops the envelope, and an unreadable or incomplete authority still holds.
    """
    restored = 0
    cleared = 0
    pending = 0
    pending_reasons: list[dict] = []
    for path in sorted(queue_dir.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("state") != "restore_prepared":
            continue
        dispatch_id = str(entry.get("dispatch_id") or path.stem)
        if not list(entry.get("dispatch_argv") or []):
            continue
        begin = _begin_reconcile_transaction(
            entry,
            queue_dir=queue_dir,
            stale_s=0.0,
            need_queue=True,
            need_task_store=True,
            need_ledger=True,
        )
        txn = begin.transaction
        if txn is None:
            pending += 1
            pending_reasons.append(
                {
                    "dispatch_id": dispatch_id,
                    "reason": "restore_prepared_resume_deferred",
                }
            )
            continue
        restored_path: Path | None = None
        decision: dict | None = None
        try:
            resume_claim = path.parent / f"{path.name}.claimed-resume"
            restored_path, decision = _commit_restore_transaction(
                txn,
                resume_claim,
                entry,
                increment_recovery_count=False,
                reason="restore_prepared_resume",
            )
        finally:
            txn.release()
        if restored_path is not None:
            restored += 1
            _restore_queued_record_from_entry(entry, restored_path)
        elif not path.exists():
            cleared += 1
        else:
            pending += 1
            pending_reasons.append(
                {
                    "dispatch_id": dispatch_id,
                    "reason": str(
                        (decision or {}).get("reason")
                        or "restore_prepared_resume_uncommitted"
                    ),
                }
            )
    return {
        "restored": restored,
        "cleared": cleared,
        "pending": pending,
        "pending_reasons": pending_reasons,
    }


def _same_queue_dir(left: Path, right: Path) -> bool:
    """True when two queue directories name the same location after resolve."""
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return os.path.normpath(str(left.expanduser())) == os.path.normpath(
            str(right.expanduser())
        )


def _empty_queue_mutations() -> dict:
    return {
        "created": 0,
        "relocated": 0,
        "retained": 0,
        "by_controller": {},
        "details": [],
    }


def _merge_queue_mutations(dest: dict, src: dict | None) -> dict:
    if not isinstance(src, dict):
        return dest
    for key in ("created", "relocated", "retained"):
        dest[key] = int(dest.get(key) or 0) + int(src.get(key) or 0)
    by_controller = dest.setdefault("by_controller", {})
    incoming = src.get("by_controller") if isinstance(src.get("by_controller"), dict) else {}
    for label, counts in incoming.items():
        if not isinstance(counts, dict):
            continue
        slot = by_controller.setdefault(
            str(label), {"created": 0, "relocated": 0, "retained": 0}
        )
        for key in ("created", "relocated", "retained"):
            slot[key] = int(slot.get(key) or 0) + int(counts.get(key) or 0)
    dest.setdefault("details", []).extend(
        item for item in (src.get("details") or []) if isinstance(item, dict)
    )
    return dest


def _record_queue_mutation(
    mutations: dict,
    *,
    action: str,
    dispatch_id: str,
    reason: str,
    controller_label: str | None = None,
    project_root: str | None = None,
) -> None:
    if action not in {"created", "relocated", "retained"}:
        raise ValueError(f"unknown queue mutation action: {action}")
    mutations[action] = int(mutations.get(action) or 0) + 1
    label = str(controller_label or "").strip() or "unknown"
    slot = mutations.setdefault("by_controller", {}).setdefault(
        label, {"created": 0, "relocated": 0, "retained": 0}
    )
    slot[action] = int(slot.get(action) or 0) + 1
    detail = {
        "dispatch_id": dispatch_id,
        "action": action,
        "reason": reason,
        "controller_label": None if label == "unknown" else label,
    }
    if project_root:
        detail["project_root"] = str(project_root)
    mutations.setdefault("details", []).append(detail)


def _drain_dispatch_id_filter(args) -> set[str] | None:
    raw = getattr(args, "dispatch_ids", None)
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [raw]
    ids = {str(item).strip() for item in raw if str(item).strip()}
    return ids or None


def _drain_invoker_identity(args) -> tuple[str | None, Path | None]:
    label = getattr(args, "controller_label", None)
    if not str(label or "").strip():
        label = os.environ.get("GOALFLIGHT_CONTROLLER_LABEL")
    label_text = str(label).strip() if label else ""
    try:
        project = goalflight_task.resolve_project_root(str(Path.cwd()))
    except Exception:
        project = Path.cwd()
    return (label_text or None), project


def _entry_owner_fields(
    entry: dict | None, record: dict | None = None
) -> tuple[str | None, str | None]:
    payload = entry if isinstance(entry, dict) else {}
    attribution = _controller_attribution_from_entry(payload, record)
    raw_label = attribution.get("controller_label")
    label = str(raw_label).strip() if raw_label else ""
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    project = (
        payload.get("project_root")
        or (record.get("project_root") if isinstance(record, dict) else None)
        or request.get("cwd")
    )
    project_text = str(project).strip() if project else ""
    return (label or None), (project_text or None)


def _normalized_project_root(value: object) -> str | None:
    if not value:
        return None
    try:
        return str(Path(str(value)).expanduser().resolve())
    except OSError:
        return os.path.normpath(str(Path(str(value)).expanduser()))


def _project_roots_match(left: object, right: object) -> bool:
    a = _normalized_project_root(left)
    b = _normalized_project_root(right)
    return bool(a and b and a == b)


def _queue_path_is_elsewhere(queue_path: object, queue_dir: Path) -> bool:
    if not queue_path:
        return False
    try:
        parent = Path(str(queue_path)).expanduser().resolve().parent
    except OSError:
        return True
    return not _same_queue_dir(parent, queue_dir)


def _ledger_row_would_republish_into(
    record: dict,
    queue_dir: Path,
    *,
    stale_s: float,
    now_s: float,
) -> bool:
    """Read-only: the ledger restore path would write this row into queue_dir."""
    if not isinstance(record, dict):
        return False
    dispatch_id = str(record.get("dispatch_id") or "")
    if not dispatch_id:
        return False
    if record.get("transport") == "fleet-ssh":
        return False
    if _claim_has_active_carrier(queue_dir, dispatch_id):
        return False
    entry = _ledger_request_entry(record)
    if not (_ledger_is_restorable_prelaunch(record) and _entry_pre_worker(entry)):
        return False
    admission = classify_reconciliation_admission(entry, now_s, stale_s=stale_s)
    return ADMISSION_DECISION[admission] is AdmissionAction.ADMIT_TO_GATE


def _preview_scoped_ledger_retains(
    queue_dir: Path,
    *,
    stale_s: float,
    dispatch_ids: set[str] | None = None,
) -> dict:
    """Name ledger orphans a scoped drain refuses to materialize into queue_dir."""
    mutations = _empty_queue_mutations()
    now_s = time.time()
    try:
        records = goalflight_ledger.read_records()
    except Exception:
        return mutations
    for record in records:
        if not _ledger_row_would_republish_into(
            record, queue_dir, stale_s=stale_s, now_s=now_s
        ):
            continue
        dispatch_id = str(record.get("dispatch_id") or "")
        if dispatch_ids is not None and dispatch_id not in dispatch_ids:
            continue
        label, project = _entry_owner_fields(_ledger_request_entry(record), record)
        reason = "unknown_owner" if not label else "queue_dir_scope"
        _record_queue_mutation(
            mutations,
            action="retained",
            dispatch_id=dispatch_id,
            reason=reason,
            controller_label=label,
            project_root=project,
        )
    return mutations


def _scoped_launch_retain_reason(
    entry: dict | None,
    *,
    invoker_label: str | None,
    invoker_project: Path | None,
    allow_cross: bool,
    read_error: str | None = None,
) -> str | None:
    """Retain unless this invoker is proven to own the already-present envelope.

    ``None`` means launch or reconcile. A string is the retain reason.
    ``allow_cross`` is the explicit override. Missing, empty, or unreadable
    owner evidence is not permission to act.
    """
    if allow_cross:
        return None
    if read_error:
        return "unreadable_queue_entry"
    if not invoker_label:
        return "unknown_invoker_label"
    if entry is None:
        return "unknown_owner"
    owner_label, owner_project = _entry_owner_fields(entry)
    if not owner_label:
        return "unknown_owner"
    if owner_label != invoker_label:
        return "foreign_controller_label"
    if not owner_project or not invoker_project:
        return "unknown_project_root"
    if not _project_roots_match(owner_project, invoker_project):
        return "foreign_project_root"
    return None


def _warn_drain_queue_mutations(mutations: dict) -> None:
    created = int(mutations.get("created") or 0)
    relocated = int(mutations.get("relocated") or 0)
    retained = int(mutations.get("retained") or 0)
    if created == 0 and relocated == 0 and retained == 0:
        return
    by_controller = mutations.get("by_controller") if isinstance(mutations.get("by_controller"), dict) else {}
    owners = {
        str(label): {
            key: int(counts.get(key) or 0)
            for key in ("created", "relocated", "retained")
        }
        for label, counts in by_controller.items()
        if isinstance(counts, dict)
    }
    print(
        "goalflight_dispatch: drain queue mutations "
        + json.dumps(
            {
                "created": created,
                "relocated": relocated,
                "retained": retained,
                "by_controller": owners,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _recovery_identity_fingerprint(entry: dict) -> dict | None:
    """Launch token plus recorded (pid, start_token) pairs.

    A reused pid cannot match the stored start_token, so a cached dead
    verdict cannot attach to a new process. Filename-only PIDs have no
    start_token and are not cacheable (UNKNOWN, never proven-dead).
    """
    identities: list[dict] = []
    for pid_key, ident_key in (
        ("queue_claimer_pid", "queue_claimer_identity"),
        ("queue_launcher_pid", "queue_launcher_identity"),
        ("queue_worker_pid", "queue_worker_identity"),
    ):
        try:
            pid = int(entry.get(pid_key) or 0)
        except (TypeError, ValueError):
            pid = 0
        ident = entry.get(ident_key)
        start = ""
        if isinstance(ident, dict):
            start = str(ident.get("start_token") or "")
        if pid > 0 and start:
            identities.append(
                {"role": pid_key, "pid": pid, "start_token": start}
            )
    if not identities:
        return None
    return {
        "queue_launch_token": str(entry.get("queue_launch_token") or ""),
        "identities": identities,
    }


def _recovery_reprobe_interval_s(age_s: float) -> float:
    """Age-tier skip interval. Derivation:

    Drain fires ~every 60s. A claim younger than 120s may still be racing
    spawn-intent publication, so interval 0 (re-probe every pass). A
    50-hour corpse cannot become the original process; 1800s (~30 drain
    passes) is enough to notice a rewritten claim. Middle tiers taper
    cost as the evidence ages: 60s under 1h, 600s under 6h.
    """
    if age_s < 120.0:
        return 0.0
    if age_s < 3600.0:
        return 60.0
    if age_s < 6 * 3600.0:
        return 600.0
    return 1800.0


def _recovery_fingerprint_still_dead(fingerprint: dict) -> bool:
    for item in fingerprint.get("identities") or []:
        if not isinstance(item, dict):
            return False
        status, _reason = _queue_claim_identity_status(
            item.get("pid"),
            {"pid": item.get("pid"), "start_token": item.get("start_token")},
        )
        if status != "dead":
            return False
    return True


def _recovery_probe_may_skip(entry: dict, *, now_s: float) -> str | None:
    """Return the kept pending reason if the expensive ladder can be skipped.

    Skip never authorizes reclaim. Invalidation: identity fingerprint
    change, next_probe_at elapsed, or a cheap liveness probe that is not
    still dead (pid reuse with a new start-token is dead-for-this-claim).
    """
    probe = entry.get("recovery_probe")
    if not isinstance(probe, dict):
        return None
    if probe.get("schema") != RECOVERY_PROBE_SCHEMA:
        return None
    if probe.get("disposition") != "keep_unresolved":
        return None
    reason = str(probe.get("reason") or "")
    if reason not in RECOVERY_UNRESOLVED_KEEP_REASONS:
        return None
    fingerprint = _recovery_identity_fingerprint(entry)
    if fingerprint is None or probe.get("identity") != fingerprint:
        return None
    next_probe = _parse_timestamp_s(probe.get("next_probe_at"))
    if next_probe is None or now_s >= next_probe:
        return None
    if not _recovery_fingerprint_still_dead(fingerprint):
        return None
    return reason


def _recovery_probe_stamp(entry: dict, *, reason: str, now_s: float) -> dict | None:
    if reason not in RECOVERY_UNRESOLVED_KEEP_REASONS:
        return None
    fingerprint = _recovery_identity_fingerprint(entry)
    if fingerprint is None:
        return None
    if not _recovery_fingerprint_still_dead(fingerprint):
        return None
    age_stamp = _launch_age_timestamp_s(entry)
    age_s = max(0.0, now_s - age_stamp) if age_stamp is not None else 0.0
    interval = _recovery_reprobe_interval_s(age_s)
    if interval <= 0.0:
        return None
    probed_at = dt.datetime.fromtimestamp(now_s, tz=dt.timezone.utc).isoformat(
        timespec="seconds"
    )
    next_at = dt.datetime.fromtimestamp(
        now_s + interval, tz=dt.timezone.utc
    ).isoformat(timespec="seconds")
    return {
        "schema": RECOVERY_PROBE_SCHEMA,
        "disposition": "keep_unresolved",
        "reason": reason,
        "identity": fingerprint,
        "probed_at": probed_at,
        "next_probe_at": next_at,
        "age_s": round(age_s, 3),
        "interval_s": interval,
    }


def _persist_recovery_probe_stamp(claim: Path, entry: dict, stamp: dict) -> bool:
    staged = dict(entry)
    staged["recovery_probe"] = stamp
    staged["updated_at"] = goalflight_ledger.utc_now()
    try:
        _write_json_atomic(claim, staged)
    except OSError:
        return False
    return True


def _recover_claimed_queue_entries(
    queue_dir: Path,
    *,
    stale_s: float,
    restore_ledger_orphans: bool = True,
    dispatch_ids: set[str] | None = None,
    invoker_label: str | None = None,
    invoker_project: Path | None = None,
    allow_cross: bool = False,
    scoped_queue: bool = False,
) -> dict:
    """Run each carrier through the single PRE-ADMIT→T→Q→S→L owner.

    Scoped drain without ``--cross-project`` does not reconcile a claim
    whose owner cannot be proven. Requeue writes into ``queue_dir`` and
    unlinking the claim both require that proof.

    Proven-dead unresolvable carriers (SC-153 fail-closed) keep their
    claim. A durable ``recovery_probe`` stamp skips the expensive
    reconcile ladder until ``next_probe_at``; it never authorizes reclaim.
    """
    cache = _begin_recovery_pass_cache()
    restored = 0
    cleared = 0
    pending_launch = 0
    quarantined = 0
    pending_reasons: list[dict] = []
    queue_mutations = _empty_queue_mutations()
    now_s = time.time()
    try:
        t_list = time.monotonic()
        try:
            entries = _queue_dir_listing(queue_dir)
        except OSError as exc:
            # An unreadable queue is not an empty queue. Republish/terminalize
            # decisions need the carrier listing, so refuse the whole pass and
            # surface the failure instead of reconciling blind.
            listing_error = f"queue_dir_unreadable:{type(exc).__name__}"
            timing = _end_recovery_pass_cache()
            return {
                "restored": 0,
                "cleared": 0,
                "pending_launch": 0,
                "quarantined": 0,
                "ledger_terminalized": 0,
                "pending_reasons": [{"dispatch_id": "", "reason": listing_error}],
                "listing_error": listing_error,
                "queue_mutations": queue_mutations,
                "timing": timing,
            }
        _pass_time("listing_s", time.monotonic() - t_list)

        t_claims = time.monotonic()
        claimed_n = 0
        for claim in sorted(path for path in entries if ".json.claimed-" in path.name):
            if claim.name.endswith(".failed"):
                continue
            try:
                entry = json.loads(claim.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(entry, dict):
                continue
            claimed_n += 1
            claim_dispatch_id = str(entry.get("dispatch_id") or "")
            if dispatch_ids is not None and claim_dispatch_id not in dispatch_ids:
                continue
            if scoped_queue:
                retain_reason = _scoped_launch_retain_reason(
                    entry,
                    invoker_label=invoker_label,
                    invoker_project=invoker_project,
                    allow_cross=allow_cross,
                )
                if retain_reason is not None:
                    label, project = _entry_owner_fields(entry)
                    _record_queue_mutation(
                        queue_mutations,
                        action="retained",
                        dispatch_id=claim_dispatch_id or claim.name,
                        reason=retain_reason,
                        controller_label=label,
                        project_root=project,
                    )
                    continue
            t_skip = time.monotonic()
            skip_reason = _recovery_probe_may_skip(entry, now_s=now_s)
            if skip_reason is not None:
                pending_launch += 1
                pending_reasons.append(
                    {
                        "dispatch_id": claim_dispatch_id or claim.name,
                        "reason": skip_reason,
                        "recovery_probe_skipped": True,
                    }
                )
                _pass_time("skip_s", time.monotonic() - t_skip, count_key="skip_n")
                continue
            try:
                outcome = _reconcile_claim_transaction(
                    claim,
                    entry,
                    queue_dir=queue_dir,
                    reason=(
                        "stale_claim_pre_spawn"
                        if _entry_pre_worker(entry)
                        else "stale_claim_launch_token_lost"
                    ),
                    stale_s=stale_s,
                )
            except Exception as exc:
                pending_launch += 1
                pending_reasons.append(
                    {
                        "dispatch_id": str(entry.get("dispatch_id") or claim.name),
                        "reason": f"reconcile_fault:{type(exc).__name__}",
                    }
                )
                continue
            action = outcome.action
            if action == "restored":
                restored += 1
            elif action == "cleared":
                cleared += 1
            elif action == "quarantined":
                quarantined += 1
            else:
                pending_launch += 1
                detail = outcome.pending_detail(
                    str(entry.get("dispatch_id") or claim.name)
                )
                if detail is not None:
                    if claim.exists():
                        try:
                            parked = json.loads(claim.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            parked = None
                        if isinstance(parked, dict) and parked.get("status_write_failed"):
                            detail["status_write_failed"] = True
                        if isinstance(parked, dict):
                            entry = parked
                    pending_reasons.append(detail)
                    stamp = _recovery_probe_stamp(
                        entry,
                        reason=str(detail.get("reason") or ""),
                        now_s=now_s,
                    )
                    if stamp is not None and claim.exists():
                        if _persist_recovery_probe_stamp(claim, entry, stamp):
                            cache["timing"]["probe_stamp_n"] = int(
                                cache["timing"].get("probe_stamp_n") or 0
                            ) + 1
        cache["timing"]["claimed_carriers"] = claimed_n
        _pass_time("claim_loop_s", time.monotonic() - t_claims)

        ledger_stats: dict = {
            "terminalized": 0,
            "pending": 0,
            "quarantined": 0,
            "preserved": 0,
            "restored": 0,
            "pending_reasons": [],
            "queue_mutations": _empty_queue_mutations(),
        }
        if restore_ledger_orphans:
            t_orphans = time.monotonic()
            ledger_stats = _reconcile_ledger_prelaunch_orphans(
                queue_dir,
                stale_s=stale_s,
                now=now_s,
                dispatch_ids=dispatch_ids,
            )
            _pass_time("ledger_orphans_s", time.monotonic() - t_orphans)
        ledger_terminalized = int(ledger_stats.get("terminalized") or 0)
        restored += int(ledger_stats.get("restored") or 0)
        pending_launch += int(ledger_stats.get("pending") or 0)
        quarantined += int(ledger_stats.get("quarantined") or 0)
        pending_reasons.extend(ledger_stats.get("pending_reasons") or [])
        _merge_queue_mutations(queue_mutations, ledger_stats.get("queue_mutations"))
        t_resume = time.monotonic()
        resume_stats = _resume_restore_prepared_envelopes(queue_dir)
        _pass_time("resume_prepared_s", time.monotonic() - t_resume)
        restored += int(resume_stats.get("restored") or 0)
        cleared += int(resume_stats.get("cleared") or 0)
        pending_launch += int(resume_stats.get("pending") or 0)
        pending_reasons.extend(resume_stats.get("pending_reasons") or [])
        timing = _end_recovery_pass_cache()
        # cleared includes carriers removed after terminalization; quarantined is
        # reported separately so callers can distinguish park-vs-delete without
        # breaking older exact-dict assertions that only key restored/cleared/pending.
        return {
            "restored": restored,
            "cleared": cleared,
            "pending_launch": pending_launch,
            "quarantined": quarantined,
            "ledger_terminalized": ledger_terminalized,
            "pending_reasons": pending_reasons,
            "queue_mutations": queue_mutations,
            "timing": timing,
        }
    except Exception:
        _end_recovery_pass_cache()
        raise


def _ledger_request_entry(record: dict) -> dict:
    """Rebuild the queue-facing entry from the ledger's durable envelope."""
    envelope = (
        dict(record["request_envelope"])
        if isinstance(record.get("request_envelope"), dict)
        else {}
    )
    request = (
        dict(envelope["request"])
        if isinstance(envelope.get("request"), dict)
        else {}
    )
    request.setdefault("agent", record.get("agent"))
    request.setdefault("cwd", record.get("project_root"))
    request.setdefault(
        "tail", record.get("stdout_path") or record.get("tail_path")
    )
    request.setdefault("status_json", record.get("status_path"))
    request.setdefault("task_ids", list(record.get("task_ids") or []))
    entry = dict(envelope)
    entry.update(
        {
            "dispatch_id": str(record.get("dispatch_id") or ""),
            "agent": record.get("agent") or envelope.get("agent") or "unknown",
            "shape": record.get("shape") or envelope.get("shape") or "bash",
            "project_root": (
                record.get("project_root") or envelope.get("project_root")
            ),
            "queue_launch_token": record.get("queue_launch_token"),
            "queue_worker_pid": record.get("worker_pid"),
            "queue_worker_identity": record.get("worker_identity"),
            "queue_worker_pgid": record.get("worker_pgid"),
            "queue_worker_group_leader_identity": record.get(
                "worker_group_leader_identity"
            ),
            "queue_worker_identity_snapshot_at": record.get(
                "worker_identity_snapshot_at"
            ),
            "queue_producer_descendants": list(
                record.get("producer_descendants") or []
            ),
            "queue_producer_group_contract": bool(
                record.get("producer_group_contract")
            ),
            "queue_producer_group_contract_enforced": bool(
                record.get("producer_group_contract_enforced")
            ),
            "queue_tail_flock_contract": record.get("transport") != "fleet-ssh",
            "queue_worker_spawned_at": (
                record.get("queue_worker_spawned_at")
                or (
                    record.get("started_at")
                    if record.get("worker_pid")
                    or str(record.get("state") or "")
                    in {"starting", "running", "running_quiet"}
                    else None
                )
            ),
            "queue_launch_started": bool(
                record.get("worker_pid")
                or str(record.get("state") or "")
                not in {
                    "queued",
                    "waiting_capacity",
                    "submitted",
                    "claimed",
                    "blocked_capacity",
                }
            ),
            "queue_worker_spawn_intent": bool(
                record.get("worker_pid")
                or str(record.get("state") or "")
                in {"starting", "running", "running_quiet"}
            ),
            "task_ids": list(
                record.get("task_ids") or envelope.get("task_ids") or []
            ),
            "request": request,
            "dispatch_argv": list(
                envelope.get("dispatch_argv") or record.get("dispatch_argv") or []
            ),
            "claim_recovery_count": _claim_recovery_count(record),
            "orphan_first_seen_at": record.get("orphan_first_seen_at"),
            "started_at": record.get("started_at"),
        }
    )
    return entry


def _terminal_ledger_requeue_pending(
    record: dict,
    entry: dict,
    *,
    queue_dir: Path,
) -> bool:
    """Whether a terminal queue launch still needs its one child transaction."""
    if not isinstance(record.get("request_envelope"), dict):
        return False
    request = (
        entry.get("request") if isinstance(entry.get("request"), dict) else {}
    )
    if entry.get("requeued_from") or request.get("requeued_from"):
        return False
    effective_account = record.get("effective_account")
    if not isinstance(effective_account, str) or not effective_account:
        return False
    tail = _entry_tail_path(entry, record)
    if _requeue_failure_kind(record, tail) not in {"auth", "quota"}:
        return False
    intent = record.get("requeue")
    if _requeue_disposition_is_terminal(intent):
        return False
    child_id = intent.get("child_id") if isinstance(intent, dict) else None
    return not (
        isinstance(child_id, str)
        and child_id
        and _requeue_child_exists(queue_dir, child_id)
    )


def _republish_prelaunch_queue_carrier(
    record: dict,
    entry: dict,
    *,
    queue_dir: Path,
    reason: str,
) -> _ClaimReconcileResult:
    """Rebuild a QUEUED envelope from a carrier-less pre-worker ledger row."""
    dispatch_id = str(record.get("dispatch_id") or entry.get("dispatch_id") or "")
    argv = list(entry.get("dispatch_argv") or record.get("dispatch_argv") or [])
    if not dispatch_id or not argv:
        return _ClaimReconcileResult("pending", pending_reason="ledger_prelaunch_missing_argv")
    begin = _begin_reconcile_transaction(
        entry,
        queue_dir=queue_dir,
        stale_s=0.0,
        need_queue=True,
        need_task_store=True,
        need_ledger=True,
    )
    txn = begin.transaction
    if txn is None:
        assert begin.refusal is not None
        return _ClaimReconcileResult("pending", refusal=begin.refusal)
    published: dict | None = None
    target: Path | None = None
    try:
        fresh_record = _find_dispatch_record(dispatch_id)
        if not _ledger_is_restorable_prelaunch(fresh_record):
            return _ClaimReconcileResult("pending", pending_reason="ledger_prelaunch_state_changed")
        if _claim_has_active_carrier(queue_dir, dispatch_id):
            return _ClaimReconcileResult("pending", pending_reason="ledger_prelaunch_carrier_present")
        decision = _entry_completion_authority(
            entry,
            fresh_record,
            task_store_locked=txn.task_store_locked,
        )
        if _completion_decision_is_deferred(decision):
            return _ClaimReconcileResult(
                "pending",
                pending_reason=str(decision.get("reason") or "completion_authority_deferred"),
            )
        if _completion_decision_blocks_restore(decision):
            return _ClaimReconcileResult(
                "pending",
                pending_reason=str(decision.get("reason") or "completion_authority_blocks_restore"),
            )
        target = queue_dir / f"{goalflight_compat.safe_dispatch_filename(dispatch_id)}.json"
        restore_txn_id = uuid.uuid4().hex
        published = _sanitize_restore_envelope(entry, increment_recovery_count=False)
        published.update(
            {
                "state": "queued",
                "dispatch_id": dispatch_id,
                "dispatch_argv": argv,
                "restore_txn_id": restore_txn_id,
                "restore_reason": reason,
                "updated_at": goalflight_ledger.utc_now(),
            }
        )
        try:
            _write_json_atomic(target, published)
        except OSError:
            return _ClaimReconcileResult("pending", pending_reason="ledger_prelaunch_queue_write_deferred")
        ledger_record = dict(record)
        ledger_record.update(
            {
                "state": "queued",
                "terminal_state": "unknown",
                "liveness_state": "queued",
                "worker_still_alive": False,
                "queue_launch_token": None,
                "queue_path": str(target),
                "restore_reason": reason,
                "restore_txn_id": restore_txn_id,
            }
        )
        try:
            goalflight_ledger.write_record(ledger_record)
        except OSError:
            return _ClaimReconcileResult("pending", pending_reason="ledger_prelaunch_record_write_deferred")
        return _ClaimReconcileResult("restored")
    finally:
        txn.release()
        if published is not None and target is not None:
            _restore_queued_record_from_entry(published, target)


def _reconcile_ledger_prelaunch_orphans(
    queue_dir: Path,
    *,
    stale_s: float,
    now: float | None = None,
    dispatch_ids: set[str] | None = None,
) -> dict:
    """Reconcile carrier-less rows with the same typed admission and held gate."""
    now_s = time.time() if now is None else float(now)
    terminalized = 0
    pending = 0
    quarantined = 0
    preserved = 0
    restored = 0
    pending_reasons: list[dict] = []
    queue_mutations = _empty_queue_mutations()
    try:
        records = _pass_ledger_records()
    except Exception:
        return {
            "terminalized": 0,
            "pending": 0,
            "quarantined": 0,
            "preserved": 0,
            "restored": 0,
            "pending_reasons": [],
            "queue_mutations": queue_mutations,
        }

    carrier_index = _build_queue_carrier_index(queue_dir)
    cache = _PASS_CACHE
    if cache is not None:
        cache["carrier_index"] = carrier_index

    for record in records:
        if not isinstance(record, dict):
            continue
        dispatch_id = str(record.get("dispatch_id") or "")
        if not dispatch_id:
            continue
        if dispatch_ids is not None and dispatch_id not in dispatch_ids:
            continue

        def _note_republish(outcome: _ClaimReconcileResult, source: dict, rebuilt: dict) -> None:
            nonlocal restored, pending
            if outcome.action != "restored":
                pending += 1
                detail = outcome.pending_detail(dispatch_id)
                if detail is not None:
                    pending_reasons.append(detail)
                return
            restored += 1
            action = (
                "relocated"
                if _queue_path_is_elsewhere(source.get("queue_path"), queue_dir)
                else "created"
            )
            label, project = _entry_owner_fields(rebuilt, source)
            _record_queue_mutation(
                queue_mutations,
                action=action,
                dispatch_id=dispatch_id,
                reason="ledger_prelaunch_carrier_missing",
                controller_label=label,
                project_root=project,
            )

        entry = _ledger_request_entry(record)
        if (
            record.get("transport") != "fleet-ssh"
            and not _claim_has_active_carrier(
                queue_dir, dispatch_id, index=carrier_index
            )
            and _ledger_is_restorable_prelaunch(record)
            and _entry_pre_worker(entry)
        ):
            admission = classify_reconciliation_admission(entry, now_s, stale_s=stale_s)
            if ADMISSION_DECISION[admission] is AdmissionAction.ADMIT_TO_GATE:
                outcome = _republish_prelaunch_queue_carrier(
                    record,
                    entry,
                    queue_dir=queue_dir,
                    reason="ledger_prelaunch_carrier_missing",
                )
                _note_republish(outcome, record, entry)
                continue
            # Do not fall through to the terminal continue: blocked_capacity
            # is restorable but still terminal-labeled, and a silent skip
            # here stranded the exact fire-and-forget row.
            pending += 1
            pending_reasons.append(
                _ReconcileRefusal(
                    ReconcileRefusalReason.PRE_ADMISSION_DEFERRED,
                    admission,
                    ADMISSION_DECISION[admission].value,
                ).as_dict(dispatch_id)
            )
            continue
        if _dispatch_record_is_terminal(record):
            if (
                not _claim_has_active_carrier(
                    queue_dir, dispatch_id, index=carrier_index
                )
                and _is_task_linked(entry, record)
                and _terminal_ledger_requeue_pending(
                    record,
                    entry,
                    queue_dir=queue_dir,
                )
            ):
                mark_refusals: list[_ReconcileRefusal] = []
                if not _mark_claim_worker_dead(
                    entry,
                    reason="watcher_terminal_reconcile",
                    queue_dir=queue_dir,
                    stale_s=stale_s,
                    refusal_out=mark_refusals,
                ):
                    pending += 1
                    pending_reasons.append(
                        mark_refusals[0].as_dict(dispatch_id)
                        if mark_refusals
                        else {"dispatch_id": dispatch_id, "reason": "terminal_requeue_deferred"}
                    )
            continue
        state = str(record.get("state") or "")
        if state not in PRELAUNCH_CANDIDATE_STATES and state not in {"running", "running_quiet"}:
            # running with dead worker + no carrier is also a post-spawn orphan.
            if state not in {"running", "running_quiet", "starting", "queued", "waiting_capacity", "submitted"}:
                continue
        if record.get("transport") == "fleet-ssh":
            continue
        if _claim_has_active_carrier(queue_dir, dispatch_id, index=carrier_index):
            continue

        admission = classify_reconciliation_admission(entry, now_s, stale_s=stale_s)
        if admission is PreAdmitClass.INDETERMINATE:
            _alert_identity_indeterminate(dispatch_id, where="ledger", reason="pre_admit_indeterminate")
            pending += 1
            pending_reasons.append(
                {
                    "dispatch_id": dispatch_id,
                    "reason": ReconcileRefusalReason.PRE_ADMISSION_DEFERRED.value,
                    "detail": "pre_admit_indeterminate",
                    "admission": admission.value,
                }
            )
            continue
        if ADMISSION_DECISION[admission] is AdmissionAction.DEFER_UNCHANGED:
            pending += 1
            pending_reasons.append(
                _ReconcileRefusal(
                    ReconcileRefusalReason.PRE_ADMISSION_DEFERRED,
                    admission,
                    ADMISSION_DECISION[admission].value,
                ).as_dict(dispatch_id)
            )
            continue
        linked = _is_task_linked(entry, record)
        age_stamp = _launch_age_timestamp_s(entry)
        if age_stamp is None:
            begin = _begin_reconcile_transaction(
                entry,
                queue_dir=queue_dir,
                stale_s=stale_s,
                need_queue=False,
                need_task_store=False,
                need_ledger=True,
                admission=admission,
            )
            txn = begin.transaction
            if txn is None:
                pending += 1
                assert begin.refusal is not None
                pending_reasons.append(begin.refusal.as_dict(dispatch_id))
                continue
            try:
                stamped = _stamp_ledger_orphan_first_seen(record, txn=txn)
            finally:
                txn.release()
            if stamped is None:
                pending += 1
                pending_reasons.append(
                    {"dispatch_id": dispatch_id, "reason": "ledger_orphan_stamp_deferred"}
                )
                continue
            pending += 1
            pending_reasons.append(
                {"dispatch_id": dispatch_id, "reason": "orphan_stale_window_started"}
            )
            continue
        if max(0.0, now_s - age_stamp) < max(0.0, stale_s):
            pending += 1
            pending_reasons.append(
                {"dispatch_id": dispatch_id, "reason": "orphan_not_stale"}
            )
            continue
        if _ledger_is_restorable_prelaunch(record) and _entry_pre_worker(entry):
            outcome = _republish_prelaunch_queue_carrier(
                record,
                entry,
                queue_dir=queue_dir,
                reason="ledger_prelaunch_carrier_missing",
            )
            _note_republish(outcome, record, entry)
            continue
        if linked:
            mark_refusals = []
            if _mark_claim_worker_dead(
                entry,
                reason="claim_carrier_missing",
                queue_dir=queue_dir,
                stale_s=stale_s,
                refusal_out=mark_refusals,
            ):
                terminalized += 1
            else:
                pending += 1
                pending_reasons.append(
                    mark_refusals[0].as_dict(dispatch_id)
                    if mark_refusals
                    else {"dispatch_id": dispatch_id, "reason": "ledger_terminalization_deferred"}
                )
        else:
            # Unlinked ledger-only zombie: alert, do not auto-terminalize (E).
            _emit_claim_recovery_alert(
                {
                    "dispatch_id": dispatch_id,
                    "action": "preserve_unlinked_ledger_orphan",
                    "reason": "claim_carrier_missing_unlinked",
                    "state": state,
                },
            )
            # Counted as PRESERVED, not quarantined. This branch deliberately
            # leaves the orphan in place, and the caller documents `quarantined`
            # as the park-vs-delete discriminator -- so incrementing it here
            # made returned diagnostics claim a quarantine that never happened.
            preserved += 1
    return {
        "terminalized": terminalized,
        "pending": pending,
        "quarantined": quarantined,
        "preserved": preserved,
        "restored": restored,
        "pending_reasons": pending_reasons,
        "queue_mutations": queue_mutations,
    }


def _stamp_ledger_orphan_first_seen(
    record: dict,
    *,
    txn: _ReconcileTransaction,
    stamp: str | None = None,
) -> str | None:
    """Stamp an existing row under caller-held T→L; never creates a row."""
    if not txn.ledger_locked:
        return None
    dispatch_id = str(record.get("dispatch_id") or "")
    if not dispatch_id:
        return None
    if record.get("orphan_first_seen_at"):
        return str(record["orphan_first_seen_at"])
    stamp = stamp or goalflight_ledger.utc_now()
    path = goalflight_ledger.record_path(dispatch_id)
    if not path.exists():
        return None
    try:
        fresh = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if fresh.get("orphan_first_seen_at"):
        return str(fresh["orphan_first_seen_at"])
    if _dispatch_record_is_terminal(fresh):
        return None
    fresh_entry = _entry_with_record_identity(txn.entry, fresh, prefer_record=True)
    if not _reconcile_transaction_still_valid(txn, fresh_entry):
        return None
    if (
        classify_reconciliation_admission(fresh_entry, time.time(), stale_s=0.0)
        is not txn.admission
    ):
        return None
    fresh["orphan_first_seen_at"] = stamp
    try:
        goalflight_ledger.write_record(fresh)
    except OSError:
        return None
    return stamp


def _claim_queue_entry(path: Path) -> Path | None:
    claim = path.with_name(f"{path.name}.claimed-{os.getpid()}-{int(time.time() * 1000)}")
    try:
        path.rename(claim)
        return claim
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _queue_entry_priority(entry: dict | None) -> str:
    if not isinstance(entry, dict):
        return QUEUE_DEFAULT_PRIORITY
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    for raw_priority in (entry.get("priority"), request.get("priority")):
        if not isinstance(raw_priority, str):
            continue
        priority = raw_priority.strip().lower()
        if priority in QUEUE_PRIORITY_RANK:
            return priority
    return QUEUE_DEFAULT_PRIORITY


def _queue_entry_created_ts(entry: dict | None, path: Path) -> float:
    if isinstance(entry, dict):
        parsed = goalflight_ledger.parse_utc(entry.get("created_at"))
        if parsed is not None:
            return parsed.timestamp()
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")


def _queue_entry_last_attempted_ts(entry: dict | None) -> float:
    """Attempt-cursor sort key. Never-attempted entries are 0.0 (first).

    Chosen over mutating created_at: original FIFO identity stays, and
    never-attempted work of the same priority always precedes a dead letter
    whose backoff has expired. Bound: every same-priority queued id gets a
    turn within N passes.
    """
    if not isinstance(entry, dict):
        return 0.0
    parsed = goalflight_ledger.parse_utc(entry.get("launch_last_attempted_at"))
    return parsed.timestamp() if parsed is not None else 0.0


def _queue_entry_not_before_ts(entry: dict | None) -> float | None:
    if not isinstance(entry, dict):
        return None
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    raw = entry.get("not_before") or request.get("not_before")
    parsed = goalflight_ledger.parse_utc(raw)
    return parsed.timestamp() if parsed is not None else None


# Tests inject usage-shaped probe rows so drain re-derives not_before without
# touching a billing endpoint. None means "load seat-state / live usage".
_NOT_BEFORE_PROBE_ROWS: list | None = None


def _queue_entry_provider_account(
    entry: dict | None,
) -> tuple[str | None, str | None]:
    """Billing identity for a queued entry, used to re-derive not_before."""
    if not isinstance(entry, dict):
        return None, None
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    argv = list(entry.get("dispatch_argv") or [])
    agent = None
    for raw in (
        entry.get("agent"),
        request.get("agent"),
        _option_value_before_worker_remainder(argv, "--agent"),
    ):
        if isinstance(raw, str) and raw.strip():
            agent = raw.strip()
            break
    account = None
    for raw in (
        entry.get("effective_account"),
        request.get("effective_account"),
        entry.get("account"),
        request.get("account"),
        _option_value_before_worker_remainder(argv, "--account"),
    ):
        if isinstance(raw, str) and raw.strip() and raw.strip() != "default":
            account = raw.strip()
            break
    if account is None:
        requeued_from = entry.get("requeued_from") or request.get("requeued_from")
        if isinstance(requeued_from, str) and requeued_from:
            try:
                payload = json.loads(
                    goalflight_ledger.record_path(
                        requeued_from, create=False
                    ).read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                for key in ("effective_account", "account"):
                    raw = payload.get(key)
                    if isinstance(raw, str) and raw.strip() and raw.strip() != "default":
                        account = raw.strip()
                        break
                if agent is None:
                    raw = payload.get("agent")
                    if isinstance(raw, str) and raw.strip():
                        agent = raw.strip()
    if not agent:
        return None, account
    import goalflight_usage as usage

    provider = usage._record_provider({"agent": agent})
    return (str(provider) if provider else None), account


def _seat_entry_probe_row(
    provider: str,
    account: str | None,
    entry: dict,
    observed_at: float | None,
):
    """Turn one seat-state record into a usage row. Cooldown-only is not a probe."""
    import goalflight_usage as usage

    used = entry.get("used_percent")
    remaining_percent = entry.get("remaining_percent")
    flags: tuple[str, ...] = ()
    probe_state = None
    remaining_text = "unknown"
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        remaining_value = max(0.0, 100.0 - float(used))
        remaining_text = f"{usage._format_number(remaining_value)}%"
        if float(used) >= 100 or remaining_value <= 0:
            flags = ("walled",)
            probe_state = "walled"
        else:
            probe_state = "reported"
    elif isinstance(remaining_percent, (int, float)) and not isinstance(
        remaining_percent, bool
    ):
        remaining_text = f"{usage._format_number(float(remaining_percent))}%"
        if float(remaining_percent) <= 0:
            flags = ("walled",)
            probe_state = "walled"
        else:
            probe_state = "reported"
    elif entry.get("ok") is False:
        remaining_text = "unavailable"
        flags = ("unavailable",)
        probe_state = "unavailable"
    else:
        return None
    row = usage._row(
        provider,
        account=account,
        remaining=remaining_text,
        reset_at=usage.parse_reset(entry.get("reset_at")),
        flags=flags,
    )
    row["evidence"] = {
        "probe": {
            "source": usage.SOURCE_PROBE,
            "state": probe_state,
            "observed_at": observed_at,
        },
        "dispatch": None,
        "conflict": False,
        "verdict": usage.HEADROOM_UNKNOWN,
        "winner": None,
    }
    return row


def _quota_state_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for raw in (
        os.environ.get("GOALFLIGHT_CODEX_STATE_DIR"),
        os.environ.get("GOALFLIGHT_STATE_DIR"),
    ):
        if raw:
            path = Path(raw).expanduser()
            key = str(path)
            if key not in seen:
                seen.add(key)
                roots.append(path)
    if "PYTEST_CURRENT_TEST" in os.environ:
        return roots
    home_state = Path.home() / ".goal-flight"
    key = str(home_state)
    if key not in seen:
        roots.append(home_state)
    return roots


def _seat_state_as_usage_rows() -> list[dict]:
    rows: list[dict] = []
    for root in _quota_state_roots():
        for filename, provider in (
            ("grok-seat-states.json", "grok"),
            ("codex-seat-states.json", "codex"),
        ):
            path = root / filename
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(document, dict) or not isinstance(
                document.get("seats"), dict
            ):
                continue
            observed_at = document.get("updated_at")
            if not isinstance(observed_at, (int, float)):
                observed_at = None
            for key, entry in document["seats"].items():
                if not isinstance(entry, dict):
                    continue
                seat_observed = entry.get("probed_at") or entry.get("updated_at")
                if isinstance(seat_observed, (int, float)):
                    observed = float(seat_observed)
                else:
                    observed = float(observed_at) if observed_at is not None else None
                account = None if key in ("", None) else str(key)
                row = _seat_entry_probe_row(provider, account, entry, observed)
                if row is not None:
                    rows.append(row)
    return rows


def _probe_rows_for_not_before(*, providers: set[str] | None = None) -> list[dict]:
    if _NOT_BEFORE_PROBE_ROWS is not None:
        return list(_NOT_BEFORE_PROBE_ROWS)
    rows = _seat_state_as_usage_rows()
    # Tests inject rows or seat-state files. Never hit a billing endpoint
    # from drain under pytest — the autouse fixture does not isolate HOME,
    # so a live reader would use the operator login.
    if "PYTEST_CURRENT_TEST" in os.environ:
        return rows
    try:
        import goalflight_usage as usage

        specs = tuple(
            spec
            for spec in usage.READERS
            if providers is None or spec.provider in providers
        )
        live = usage.collect_usage(
            timeout_s=min(usage.DEFAULT_TIMEOUT_S, 8.0),
            reader_specs=specs or usage.READERS,
            ledger_records=[],
        )
        if live:
            return list(live)
    except Exception:
        pass
    return rows


def _headroom_for_queue_entry(
    entry: dict | None,
    *,
    now: float,
    probe_rows: list[dict],
    ledger_records: list,
) -> dict:
    import goalflight_usage as usage

    _ = now  # age is derived at the hold-summary call site, not minted here
    provider, account = _queue_entry_provider_account(entry)
    if not provider:
        return {"verdict": usage.HEADROOM_UNKNOWN, "winner": None}
    account_key = usage._label(account) or ""
    row = None
    for candidate in probe_rows:
        if str(candidate.get("provider") or "") != provider:
            continue
        if (usage._label(candidate.get("account")) or "") == account_key:
            row = dict(candidate)
            break
    if row is None:
        # A probe that was never taken has no observed_at. Stamping `now`
        # here used to print "age 0m" for a measurement that did not happen.
        row = usage._row(
            provider,
            account=account,
            remaining="unavailable",
            reset_at=None,
            flags=("unavailable",),
        )
        row["evidence"] = {
            "probe": {
                "source": usage.SOURCE_PROBE,
                "state": "unavailable",
            },
            "dispatch": None,
            "conflict": False,
            "verdict": usage.HEADROOM_UNKNOWN,
            "winner": None,
        }
    overlaid = usage.overlay_dispatch_evidence([row], ledger_records)
    evidence = overlaid[0].get("evidence") if overlaid else None
    return evidence if isinstance(evidence, dict) else {
        "verdict": usage.HEADROOM_UNKNOWN,
        "winner": None,
    }


def _not_before_headroom_fields(
    headroom: dict | None, *, now: float
) -> dict[str, object]:
    """Name the winning source and its age, matching the usage EVIDENCE column.

    Usage prints ``winner: probe → healthy`` plus ``as of <local>, age <delta>``.
    Drain hold details used to carry only the stored timestamp, so an operator
    staring at a gated queue entry could not tell a live probe from a stale
    record. Same fact, same vocabulary; the queue file is not rewritten.
    """
    import goalflight_usage as usage

    evidence = headroom if isinstance(headroom, dict) else {}
    winner = evidence.get("winner")
    if winner == usage.SOURCE_PROBE:
        source = "probe"
        observed_blob = evidence.get("probe")
    elif winner == usage.SOURCE_DISPATCH:
        source = "dispatch"
        observed_blob = evidence.get("dispatch")
    else:
        source = "none"
        observed_blob = evidence.get("probe")
    observed_at = (
        observed_blob.get("observed_at")
        if isinstance(observed_blob, dict)
        else None
    )
    as_of = usage._observed_text(observed_at, now=now)
    winner_text = usage._winner_text(evidence)
    return {
        "winner": source,
        "verdict": evidence.get("verdict") or usage.HEADROOM_UNKNOWN,
        "winner_age": as_of,
        "headroom": f"{winner_text} (as of {as_of})",
    }


def _queue_entry_drain_candidate(path: Path) -> tuple[tuple[int, float, float, str], Path, dict | None, str | None]:
    entry: dict | None = None
    read_error: str | None = None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            entry = payload
        else:
            read_error = "invalid_payload"
    except (OSError, json.JSONDecodeError) as exc:
        read_error = type(exc).__name__
    priority = _queue_entry_priority(entry)
    return (
        (
            QUEUE_PRIORITY_RANK.get(priority, QUEUE_PRIORITY_RANK[QUEUE_DEFAULT_PRIORITY]),
            _queue_entry_last_attempted_ts(entry),
            _queue_entry_created_ts(entry, path),
            path.name,
        ),
        path,
        entry,
        read_error,
    )


def _restore_claimed_entry(
    claim: Path,
    entry: dict,
    *,
    txn: _ReconcileTransaction,
    increment_recovery_count: bool = False,
    reason: str = "claim_restore",
) -> tuple[Path | None, dict | None]:
    """Leaf restore; caller must own the complete T→Q→S→L transaction."""
    return _commit_restore_transaction(
        txn,
        claim,
        entry,
        increment_recovery_count=increment_recovery_count,
        reason=reason,
    )


def _update_launch_owned_claim(
    claim: Path,
    token: str,
    updater,
    *,
    error_label: str,
    silent: bool = False,
) -> bool:
    """Q-only acting-owner mutation; releases Q before any reconcile path."""
    try:
        with _queue_mutation_lock(claim.parent):
            entry = json.loads(claim.read_text(encoding="utf-8"))
            if not isinstance(entry, dict) or entry.get("queue_launch_token") != token:
                if silent:
                    return False
                raise DispatchUsageError("queue claim launch token mismatch")
            updater(entry)
            _write_json_atomic(claim, entry)
        return True
    except DispatchUsageError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        if silent:
            return False
        raise DispatchUsageError(f"{error_label}: {type(exc).__name__}") from exc


def _mark_queue_claim_launch_started(args) -> None:
    if not (getattr(args, "from_queue", False) and getattr(args, "queue_claim_path", None)):
        return
    token = getattr(args, "queue_launch_token", None)
    if not token:
        return
    claim = Path(str(args.queue_claim_path)).expanduser()
    launcher_identity = goalflight_ledger.process_identity(os.getpid()) or {
        "pid": os.getpid(),
        "identity_available": False,
        "identity_source": "pid_probe_unavailable",
    }
    now = goalflight_ledger.utc_now()
    def update(entry: dict) -> None:
        entry["queue_launch_started"] = True
        entry["queue_launch_started_at"] = now
        entry["queue_launcher_pid"] = os.getpid()
        if launcher_identity:
            entry["queue_launcher_identity"] = launcher_identity
        entry["queue_tail_flock_contract"] = True
        entry["updated_at"] = now

    _update_launch_owned_claim(
        claim,
        str(token),
        update,
        error_label="queue claim launch marker failed",
    )


def _mark_queue_claim_worker_spawn_intent(args) -> None:
    if not (getattr(args, "from_queue", False) and getattr(args, "queue_claim_path", None)):
        return
    token = getattr(args, "queue_launch_token", None)
    if not token:
        return
    claim = Path(str(args.queue_claim_path)).expanduser()
    now = goalflight_ledger.utc_now()
    def update(entry: dict) -> None:
        entry["queue_worker_spawn_intent"] = True
        entry["queue_worker_spawn_intent_at"] = now
        entry["queue_tail_flock_contract"] = True
        entry["updated_at"] = now

    _update_launch_owned_claim(
        claim,
        str(token),
        update,
        error_label="queue claim worker-spawn marker failed",
    )


def _mark_queue_claim_worker_spawned(args, worker_pid: int | None) -> None:
    if not (getattr(args, "from_queue", False) and getattr(args, "queue_claim_path", None)):
        return
    token = getattr(args, "queue_launch_token", None)
    if not (token and worker_pid):
        return
    claim = Path(str(args.queue_claim_path)).expanduser()
    worker_pid_i = int(worker_pid)
    now = goalflight_ledger.utc_now()
    # b-065 C: capture process identity at spawn so recovery can use the same
    # PID/start-time/command test as the ledger (not bare-PID liveness).
    worker_identity = goalflight_ledger.process_identity(worker_pid_i) or {
        "pid": worker_pid_i,
        "identity_available": False,
        "identity_source": "pid_probe_unavailable",
    }
    pgid = process_group_id(worker_pid_i)
    group_leader_identity = None
    if pgid:
        group_leader_identity = (
            goalflight_ledger.process_identity(int(pgid))
            or {"pid": int(pgid), "identity_available": False, "identity_source": "pid_probe_unavailable"}
        )
    # PGID capture is evidence, not a no-daemon-escape guarantee. Current
    # launchers do not prevent a descendant from reparenting/changing groups,
    # so PID fallback must remain fail-closed if flock is unavailable.
    snapshot = _process_snapshot()
    descendants: list[dict] = []
    if snapshot is not None:
        member_pids = {worker_pid_i}
        changed = True
        while changed:
            changed = False
            for row in snapshot:
                pid = int(row.get("pid") or 0)
                if pid in member_pids:
                    continue
                if int(row.get("ppid") or 0) in member_pids:
                    member_pids.add(pid)
                    changed = True
        for pid in sorted(member_pids - {worker_pid_i}):
            identity = goalflight_ledger.process_identity(pid)
            if isinstance(identity, dict):
                descendants.append({"pid": pid, "identity": identity})
    def update(entry: dict) -> None:
        entry["queue_worker_pid"] = worker_pid_i
        entry["queue_worker_spawned_at"] = now
        entry["queue_worker_identity"] = worker_identity
        if pgid:
            entry["queue_worker_pgid"] = int(pgid)
            entry["queue_worker_group_leader_identity"] = group_leader_identity
        entry["queue_worker_identity_snapshot_at"] = now
        entry["queue_producer_group_contract"] = bool(pgid)
        entry["queue_producer_group_contract_enforced"] = False
        entry["queue_producer_group_contract_reason"] = "pgid_observed_no_escape_not_enforced"
        entry["queue_tail_flock_contract"] = True
        entry["queue_producer_descendants"] = descendants
        entry["updated_at"] = now

    _update_launch_owned_claim(
        claim,
        str(token),
        update,
        error_label="queue claim worker-spawn marker failed",
        silent=True,
    )


def _mark_claim_failed_locked(claim: Path, entry: dict, *, reason: str) -> bool:
    """Pre-spawn failure write; caller owns Q. Returns whether the claim cleared.

    Writing the `.failed` record and clearing the original carrier are separate
    outcomes: the unlink is best-effort, so the record can exist while the
    claimed carrier survives. Recovery ignores the `.failed` sibling and keeps
    scanning that surviving claim, so a caller reporting "failed" on the
    strength of the record alone would be describing a carrier that is still in
    play.
    """
    fresh = dict(entry)
    fresh["state"] = "failed"
    fresh["reason"] = reason
    fresh["updated_at"] = goalflight_ledger.utc_now()
    failed = claim.with_name(f"{claim.name}.failed")
    _write_json_atomic(failed, fresh)
    try:
        claim.unlink()
    except FileNotFoundError:
        return True          # already gone: the carrier is cleared either way
    except OSError:
        return False         # record written, carrier still present
    return True


def _mark_claim_failed(claim: Path, entry: dict, *, reason: str) -> bool:
    """Mark the claim failed. Returns whether the carrier is now durably failed.

    False means the carrier is still in play, for either of two reasons, and a
    caller announcing "failed" would in both cases describe a state the queue
    is not in:

    * a launch-token race, where this returns WITHOUT writing anything because
      the carrier on disk belongs to a different attempt;
    * a best-effort unlink that did not succeed, leaving the claimed carrier
      beside the `.failed` record. Recovery keeps scanning that carrier.
    """
    with _queue_mutation_lock(claim.parent):
        try:
            fresh = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fresh = dict(entry)
        if fresh.get("queue_launch_token") != entry.get("queue_launch_token"):
            return False
        return _mark_claim_failed_locked(claim, fresh, reason=reason)


class _RemoteDrainBlocked(RuntimeError):
    def __init__(self, message: str, *, code: str = "blocked") -> None:
        self.code = code
        super().__init__(message)


def _remote_drain_node(args) -> str | None:
    node = getattr(args, "remote_node", None)
    if node is None:
        return None
    node = str(node).strip()
    return node or None


def _remote_drain_fleet_dir(args) -> Path:
    raw = getattr(args, "fleet_dir", None)
    if raw:
        return Path(str(raw)).expanduser()
    import goalflight_fleet_store as fleet

    return fleet.default_fleet_dir()


def _validate_remote_drain_node(args) -> Path:
    node = _remote_drain_node(args)
    if not node:
        raise _RemoteDrainBlocked("--remote-node requires a non-empty node name", code="unknown_node")
    fleet_dir = _remote_drain_fleet_dir(args)
    fleet_path = fleet_dir / "fleet.json"
    if not fleet_path.exists():
        raise _RemoteDrainBlocked(f"fleet store missing: {fleet_path}", code="fleet_missing")
    try:
        import goalflight_fleet_store as fleet

        fleet_doc = fleet.read_json(fleet_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise _RemoteDrainBlocked(f"fleet store unreadable: {type(exc).__name__}", code="fleet_unreadable") from exc
    node_entry = (fleet_doc.get("nodes") or {}).get(node)
    if not isinstance(node_entry, dict):
        raise _RemoteDrainBlocked(f"unknown node: {node}", code="unknown_node")
    return fleet_dir


def _remote_drain_agent(entry: dict) -> str:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    raw = str(entry.get("agent") or request.get("agent") or "codex").strip().lower()
    aliases = {
        "worker": "codex-acp",
        "codex": "codex-acp",
        "codex-acp": "codex-acp",
        "grok": "grok-acp",
        "grok-code": "grok-acp",
        "grok-research": "grok-acp",
        "grok-acp": "grok-acp",
        "cursor": "cursor",
        "cursor-agent": "cursor",
        "claude": "claude-acp",
        "claude-acp": "claude-acp",
        "claude-code-cli-acp": "claude-acp",
    }
    agent = aliases.get(raw)
    if not agent:
        raise _RemoteDrainBlocked(f"unsupported remote drain agent: {raw}", code="unsupported_agent")
    return agent


def _remote_drain_prompt(entry: dict) -> str:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    prompt = request.get("prompt")
    if isinstance(prompt, str) and prompt:
        return prompt
    prompt_file = request.get("prompt_file")
    if not prompt_file:
        raise _RemoteDrainBlocked("remote drain requires a queued --prompt or --prompt-file", code="missing_prompt")
    path = Path(str(prompt_file)).expanduser()
    if not path.is_absolute():
        base = Path(str(entry.get("process_cwd") or entry.get("project_root") or Path.cwd()))
        path = base / path
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _RemoteDrainBlocked(f"prompt file unreadable: {type(exc).__name__}: {path}", code="prompt_unreadable") from exc


def _remote_drain_base_sha(entry: dict) -> str:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    for source, raw in (
        ("request.base_sha", request.get("base_sha")),
        ("entry.base_sha", entry.get("base_sha")),
    ):
        if not raw:
            continue
        value = str(raw).strip()
        if _valid_lower_base_sha(value):
            return value
        raise _RemoteDrainBlocked(f"invalid remote drain base sha in {source}: {raw}", code="invalid_base_sha")
    raise _RemoteDrainBlocked(
        "remote drain requires a queued submit-time base_sha; re-submit the entry from the intended base",
        code="base_sha_unavailable",
    )


def _remote_drain_billing_account(entry: dict) -> str | None:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    raw = request.get("billing_account") or entry.get("billing_account")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    account = request.get("account")
    if isinstance(account, str) and account.strip():
        mapped = account.strip()
        if "/" in mapped:
            return mapped
        raise _RemoteDrainBlocked(
            f"unsupported remote account mapping for queued --account {mapped!r}; persist a fleet billing_account",
            code="unsupported_account_mapping",
        )
    return None


def _drain_launch_remote_claim(
    args,
    entry: dict,
    *,
    dispatch_id: str,
    launch_token: str,
    claim: Path,
) -> subprocess.CompletedProcess[str]:
    node = _remote_drain_node(args)
    if not node:
        raise _RemoteDrainBlocked("--remote-node missing", code="unknown_node")
    fleet_dir = _validate_remote_drain_node(args)
    try:
        import goalflight_fleet_dispatch as fleet_dispatch
    except ImportError as exc:
        raise _RemoteDrainBlocked(f"fleet dispatch unavailable: {exc}", code="fleet_unavailable") from exc

    try:
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id=node,
            agent=_remote_drain_agent(entry),
            billing_account=_remote_drain_billing_account(entry),
            prompt=_remote_drain_prompt(entry),
            dispatch_id=dispatch_id,
            base_sha=_remote_drain_base_sha(entry),
            thin_mode=True,
        )
        # Stamp launch-started only once the preview gate has passed: the gate
        # fails closed before any remote side effect, so a blocked carrier must
        # stay pre-launch-shaped to remain restorable. The stamp still precedes
        # execute_dispatch, keeping the crash guard over every window in which
        # a remote launch may actually be in flight.
        _mark_queue_claim_launch_started(
            argparse.Namespace(
                from_queue=True,
                queue_claim_path=str(claim),
                queue_launch_token=launch_token,
            )
        )
        runner = getattr(args, "remote_runner", None)
        if runner is None:
            fleet_dispatch.assert_live_ssh_opt_in()
        result = fleet_dispatch.execute_dispatch(
            fleet_dir,
            preview,
            runner=runner,
            dispatch_mode="one-shot",
            tool_smoke_policy=getattr(args, "remote_tool_smoke", "auto"),
            queue_launch_token=launch_token,
        )
    except fleet_dispatch.DispatchGateError as exc:
        raise _RemoteDrainBlocked(str(exc), code=getattr(exc, "code", "blocked")) from exc
    except fleet_dispatch.DispatchError as exc:
        raise _RemoteDrainBlocked(str(exc), code="dispatch_error") from exc

    return subprocess.CompletedProcess(
        ["remote-drain", node, dispatch_id],
        0,
        stdout="DISPATCH-LAUNCHED "
        + json.dumps(
            {
                "dispatch_id": dispatch_id,
                "node": node,
                "transport": "fleet-ssh",
                "launch_unconfirmed": bool(result.get("launch_unconfirmed")),
            },
            sort_keys=True,
        )
        + "\n",
        stderr="",
    )


def _drain_launch_argv(
    dispatch_argv: list[str],
    *,
    capacity_wait_s: float,
    queue_launch_token: str | None = None,
    queue_claim_path: Path | None = None,
) -> list[str]:
    if not dispatch_argv:
        return []
    inject = ["--from-queue"]
    if queue_launch_token:
        inject += ["--queue-launch-token", queue_launch_token]
    if queue_claim_path is not None:
        inject += ["--queue-claim-path", str(queue_claim_path)]
    inject.append("--launch-detached")
    return _reconstruct_launch_argv(
        list(dispatch_argv),
        replace={"--capacity-wait-s": str(max(0.0, float(capacity_wait_s)))},
        inject=inject,
    )


def _queued_args_for_status(entry: dict):
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    return argparse.Namespace(
        dispatch_id=entry.get("dispatch_id"),
        agent=entry.get("agent") or request.get("agent"),
        shape=entry.get("shape") or request.get("shape") or "bash",
        prompt_file=request.get("prompt_file"),
        account=request.get("account"),
        read_only=bool(request.get("read_only")),
        os_sandbox=request.get("os_sandbox"),
        controller_pid=None,
        queue_launch_token=entry.get("queue_launch_token"),
        task_ids=list(entry.get("task_ids") or request.get("task_ids") or []),
    )


def _restore_queued_record_from_entry(entry: dict, queue_path: Path) -> None:
    """Post-commit queued status/dashboard mirror; ledger truth is already durable."""
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    args = _queued_args_for_status(entry)
    project_root = goalflight_task.resolve_project_root(
        str(entry.get("project_root") or request.get("cwd") or Path.cwd())
    )
    status_json = Path(str(request.get("status_json") or _dispatch_base_dir() / f"{args.dispatch_id}.status.json"))
    tail = Path(str(request.get("tail") or _dispatch_base_dir() / f"{args.dispatch_id}.tail"))
    lock = goalflight_ledger.StateLock.try_acquire(
        time.monotonic() + RECONCILE_DOWNSTREAM_LOCK_BUDGET_S
    )
    if lock is None:
        return
    try:
        record = _find_dispatch_record(str(args.dispatch_id or ""))
        if not record or _dispatch_record_is_terminal(record) or record.get("state") != "queued":
            return
        if record.get("restore_txn_id") != entry.get("restore_txn_id") and entry.get("restore_txn_id"):
            return
        write_status(
            status_json,
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": args.dispatch_id,
                "agent": args.agent,
                "shape": args.shape,
                "state": "queued",
                "reason": "dispatch_queue_restore",
                "queue_path": str(queue_path),
                "project_root": str(project_root),
                "worker_pid": None,
                "worker_alive": False,
                "tail_path": str(tail),
                "updated_at": int(time.time()),
                **({"task_ids": list(args.task_ids)} if getattr(args, "task_ids", None) else {}),
            },
        )
    finally:
        lock.release()
    _export_dashboard_status_for_project(project_root)
    _start_dashboard_refresh_for_project(project_root)


def _resolve_claim_terminal_outcome(
    entry: dict,
    *,
    reason: str,
    tail: Path,
    ignore_prefix_lines: list[str],
    agent: str,
) -> tuple[str, object, dict | None]:
    """Scan tail (with amendment-G recheck) and return (state, reason, marker)."""

    def _scan_marker() -> dict | None:
        expected_dispatch_id = str(entry.get("dispatch_id") or "").strip()
        if not expected_dispatch_id:
            return None
        try:
            return _final_terminal_marker(
                tail,
                ignore_prefix_lines=ignore_prefix_lines,
                suppress_unfenced_prompt_markers=True,
                kimi_output=moonshot_family(agent),
                expected_dispatch_id=expected_dispatch_id,
            )
        except Exception:
            return None

    terminal_marker = _scan_marker()
    # Final recheck immediately before outcome is used for a write (G).
    recheck = _scan_marker()
    if recheck is not None:
        terminal_marker = recheck
    state = _marker_state_for_terminal(terminal_marker) if terminal_marker else "worker_dead"
    reason_for_finish = (
        f"marker:{terminal_marker['kind']}:final_reconciliation" if terminal_marker else reason
    )
    state, final_reason, _rate_limited = goalflight_terminal.terminal_rate_limit_outcome(
        state,
        reason_for_finish,
        tail,
        terminal_marker_present=goalflight_terminal.terminal_marker_present(terminal_marker),
    )
    state, final_reason, _vetoed_marker = goalflight_terminal.final_reconciliation_error_veto_outcome(
        state,
        final_reason,
        tail,
        terminal_marker,
    )
    state, final_reason = _quota_limited_state_reason(state, final_reason, tail, agent=agent)
    return state or "worker_dead", final_reason, terminal_marker


def _new_reconciliation_record(entry: dict) -> dict:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    dispatch_id = str(entry.get("dispatch_id") or "")
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": entry.get("agent") or request.get("agent") or "unknown",
        "shape": entry.get("shape") or request.get("shape") or "bash",
        "transport": "dispatch",
        "project_root": str(entry.get("project_root") or request.get("cwd") or Path.cwd()),
        "stdout_path": str(request.get("tail") or _dispatch_base_dir() / f"{dispatch_id}.tail"),
        "status_path": str(request.get("status_json") or _dispatch_base_dir() / f"{dispatch_id}.status.json"),
        "task_ids": _entry_task_ids(entry),
        "queue_launch_token": entry.get("queue_launch_token"),
        "worker_pid": entry.get("queue_worker_pid"),
        "worker_identity": entry.get("queue_worker_identity"),
        "worker_pgid": entry.get("queue_worker_pgid"),
        "worker_group_leader_identity": entry.get("queue_worker_group_leader_identity"),
        "producer_descendants": list(entry.get("queue_producer_descendants") or []),
        "producer_group_contract": bool(entry.get("queue_producer_group_contract")),
        "producer_group_contract_enforced": bool(
            entry.get("queue_producer_group_contract_enforced")
        ),
        "started_at": entry.get("started_at") or entry.get("created_at") or goalflight_ledger.utc_now(),
    }
    record.update(_controller_attribution_from_entry(entry))
    return record


def commit_reconciled_terminal(
    txn: _ReconcileTransaction,
    entry: dict,
    decision: dict,
) -> TerminalCommitResult:
    """Create/update a terminal directly under caller-held T→[Q]→S→L."""
    if not txn.ledger_locked or (_is_task_linked(entry) and not txn.task_store_locked):
        return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)
    if not _reconcile_transaction_still_valid(txn, entry):
        return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)
    dispatch_id = str(entry.get("dispatch_id") or "")
    if not dispatch_id:
        return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)
    path = goalflight_ledger.record_path(dispatch_id)
    created = not path.exists()
    if created:
        record = _new_reconciliation_record(entry)
    else:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)
        if not isinstance(record, dict):
            return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)

    existing_terminal = goalflight_ledger._terminal_key(record)
    if existing_terminal not in {"", "unknown", "watcher_stopped"}:
        committed = goalflight_ledger.commit_terminal_authority(
            record,
            state=str(record.get("state") or existing_terminal),
            reason=record.get("reason") or record.get("error"),
            terminal_state=existing_terminal,
            worker_still_alive=record.get("worker_still_alive"),
        )
        if not committed.committed or committed.value is None:
            return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)
        record["attempt_id"] = committed.value.attempt_id
        record["transition_id"] = committed.value.transition_id
        record["terminal_event_uuid"] = committed.value.event_uuid
        goalflight_ledger.preserve_first_terminal_time(
            record,
            committed.value.terminal_at,
        )
        try:
            goalflight_ledger.write_record(record)
        except OSError:
            return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)
        existing_state = str(record.get("state") or existing_terminal)
        _maybe_mark_grok_quota_exhausted(record=record, state=existing_state)
        return TerminalCommitResult(
            TerminalCommitKind.EXISTING_TERMINAL,
            existing_state,
            True,
        )

    state = str(decision.get("state") or "worker_dead")
    reason = decision.get("reason") or "claim_reconciliation"
    authority = _entry_completion_authority(entry, record, task_store_locked=True)
    if authority is not None:
        if _completion_decision_is_deferred(authority):
            return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)
        state = str(authority.get("state") or state)
        reason = authority.get("reason") or reason
        decision = {**decision, **authority}
    terminal_state = goalflight_ledger.terminal_state_for(state, reason)
    if terminal_state in {"", "unknown", "watcher_stopped"}:
        return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)

    committed = goalflight_ledger.commit_terminal_authority(
        record,
        state=state,
        reason=reason,
        terminal_state=terminal_state,
        worker_still_alive=False,
    )
    if not committed.committed or committed.value is None:
        return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)
    winner = committed.value
    terminal_state = winner.terminal_state

    ended_at = goalflight_ledger.preserve_first_terminal_time(
        record,
        winner.terminal_at,
    )
    record.update(
        {
            "state": state,
            "terminal_state": terminal_state,
            "liveness_state": goalflight_terminal.terminal_liveness_state(state),
            "worker_still_alive": False,
        }
    )
    reconciliation = {
        "source": "drain_claim_recovery",
        "launch_timeout_s": LAUNCH_TIMEOUT_S,
        "queue_launch_token": entry.get("queue_launch_token") or record.get("queue_launch_token"),
        "claim_recovery_count": _claim_recovery_count(entry),
        "checked_output": True,
        "checked_task_ids": _entry_task_ids(entry, record),
    }
    if decision.get("resolution_source"):
        reconciliation["resolution_source"] = decision["resolution_source"]
    if decision.get("salvage_required"):
        reconciliation["salvage_required"] = True
        record["salvage_required"] = True
    record["outcome"] = {"terminal_state": terminal_state, "reconciliation": reconciliation}
    envelope = goalflight_ledger.failure_envelope(reason)
    if envelope:
        record.update(envelope)
        record["outcome"].update(envelope)
    record["attempt_id"] = winner.attempt_id
    record["transition_id"] = winner.transition_id
    record["terminal_event_uuid"] = winner.event_uuid
    try:
        goalflight_ledger.write_record(record)
    except OSError:
        return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)
    _maybe_mark_grok_quota_exhausted(record=record, state=state)
    return TerminalCommitResult(
        TerminalCommitKind.CREATED_TERMINAL if created else TerminalCommitKind.UPDATED_TERMINAL,
        state,
        True,
    )


def _finish_ledger_under_lock(
    txn: _ReconcileTransaction,
    entry: dict,
    decision: dict,
) -> TerminalCommitResult:
    """Leaf terminal commit; never acquires locks or reports apparent success."""
    return commit_reconciled_terminal(txn, entry, decision)


_CODEX_AUTH_FAILURE_RE = re.compile(
    r"""(?ix)
    (?:
        \b(?:error|failed|failure|fatal|http|status|unauthorized|authentication)\b
        [^\r\n]{0,160}\b401\b
      |
        \b401\b[^\r\n]{0,160}\b(?:unauthorized|authentication|token)\b
      |
        \b(?:access|bearer|authentication)\s+token\b[^\r\n]{0,120}
        \b(?:expired|invalid|rejected)\b
    )
    """
)


def _requeue_failure_kind(record: dict, tail: Path) -> str | None:
    state = str(record.get("state") or record.get("terminal_state") or "")
    # Any limit-family state is a quota-style requeue candidate. Matching only
    # the legacy literal made the measured kinds (quota_exhausted,
    # transient_throttle, limit_unknown) fall through to the auth-regex scan
    # and return None, so a limit death was never requeued at all.
    if goalflight_dispatch_states.is_limit_state(state):
        return "quota"
    if state == "blocked_auth":
        return "auth"
    if (
        state in goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES
        or state not in goalflight_dispatch_states.TERMINAL_FAILURE_STATES
    ):
        return None
    parts: list[str] = []
    for key in ("reason", "error", "outcome"):
        value = record.get(key)
        if value not in (None, ""):
            parts.append(
                json.dumps(value, sort_keys=True)
                if isinstance(value, (dict, list))
                else str(value)
            )
    parts.append(goalflight_terminal.read_tail_excerpt(tail, 8192))
    return "auth" if _CODEX_AUTH_FAILURE_RE.search("\n".join(parts)) else None


def _effective_account_cooldown(effective_account: str) -> str | None:
    """Read only the daemon's non-secret health record for quota scheduling."""
    configured = os.environ.get("GOALFLIGHT_CODEX_STATE_DIR")
    state_root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".goal-flight"
    )
    try:
        payload = json.loads(
            (state_root / "codex-seat-states.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("seats"), dict)
    ):
        return None
    record = payload["seats"].get(effective_account)
    if not isinstance(record, dict):
        return None
    cooldown = record.get("cooldown_until")
    return cooldown if isinstance(cooldown, str) and cooldown else None


REQUEUE_TERMINAL_DISPOSITIONS = frozenset({"satisfied", "abandoned", "expired"})
REQUEUE_MAX_ATTEMPTS = 3
REQUEUE_MAX_AGE_S = 24 * 60 * 60


def _requeue_disposition_is_terminal(intent: object) -> bool:
    return (
        isinstance(intent, dict)
        and intent.get("disposition") in REQUEUE_TERMINAL_DISPOSITIONS
    )


def _requeue_record_event_ts(record: dict) -> float | None:
    for key in ("ended_at", "started_at", "updated_at"):
        ts = _parse_timestamp_s(record.get(key))
        if ts is not None:
            return ts
    intent = record.get("requeue")
    if isinstance(intent, dict):
        ts = _parse_timestamp_s(intent.get("requeued_at"))
        if ts is not None:
            return ts
    return None


def _requeue_intent_age_s(intent: dict, *, now_s: float | None = None) -> float | None:
    ts = _parse_timestamp_s(intent.get("requeued_at"))
    if ts is None:
        return None
    now = time.time() if now_s is None else float(now_s)
    return max(0.0, now - ts)


def _requeue_attempt_count(intent: dict) -> int | None:
    raw = intent.get("attempt_count")
    if raw is None:
        return None
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return None
    if count < 0:
        return None
    return count


def _later_complete_successor_id(record: dict, entry: dict) -> str | None:
    """A later success-terminal dispatch for the same task_ids, if proven.

    Unlinked work, unlistable/unreadable ledger rows, and candidates whose
    timestamps cannot be ordered are not a determination of "no successor".
    They simply fail to prove one. Callers must retain the intent in those
    cases rather than treating absence of a return value as expiry.

    Task ids collide across projects on this box by design. A successor is
    only proven when both rows name a project_root and those roots match.
    Missing or empty project_root is UNKNOWN, not a host-wide match.
    """
    task_ids = set(_entry_task_ids(entry, record))
    if not task_ids:
        return None
    self_root = _entry_owner_fields(entry, record)[1]
    if not self_root:
        return None
    self_id = str(record.get("dispatch_id") or entry.get("dispatch_id") or "")
    self_ts = _requeue_record_event_ts(record)
    if self_ts is None:
        return None
    try:
        records = goalflight_ledger.read_records()
    except OSError:
        return None
    for other in records:
        if not isinstance(other, dict) or goalflight_ledger.record_is_unreadable(other):
            continue
        other_id = str(other.get("dispatch_id") or "")
        if not other_id or other_id == self_id:
            continue
        if not (task_ids & set(_entry_task_ids(None, other))):
            continue
        other_root = _entry_owner_fields(None, other)[1]
        if not _project_roots_match(self_root, other_root):
            continue
        state = str(other.get("state") or "")
        terminal = str(other.get("terminal_state") or "")
        if (
            state not in goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES
            and terminal not in goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES
        ):
            continue
        other_ts = _requeue_record_event_ts(other)
        if other_ts is None or other_ts < self_ts:
            continue
        return other_id
    return None


def _requeue_child_success_id(intent: dict) -> str | None:
    child_id = intent.get("child_id")
    if not isinstance(child_id, str) or not child_id:
        return None
    path = goalflight_ledger.record_path(child_id, create=False)
    try:
        if not path.exists():
            return None
        child = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(child, dict) or goalflight_ledger.record_is_unreadable(child):
        return None
    state = str(child.get("state") or "")
    terminal = str(child.get("terminal_state") or "")
    if (
        state in goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES
        or terminal in goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES
    ):
        return child_id
    return None


def _requeue_stop_decision(
    record: dict,
    entry: dict,
    *,
    regenerating: bool = False,
    now_s: float | None = None,
) -> dict | None:
    """Return a terminal disposition when the retry is proven obsolete.

    Missing timestamps or attempt counts are UNKNOWN and must retain.
    """
    successor = _later_complete_successor_id(record, entry)
    if successor:
        return {
            "disposition": "satisfied",
            "reason": "successor_complete",
            "satisfied_by": successor,
        }
    intent = record.get("requeue")
    if isinstance(intent, dict):
        child_done = _requeue_child_success_id(intent)
        if child_done:
            return {
                "disposition": "satisfied",
                "reason": "retry_complete",
                "satisfied_by": child_done,
            }
        age_s = _requeue_intent_age_s(intent, now_s=now_s)
        if age_s is not None and age_s >= REQUEUE_MAX_AGE_S:
            return {"disposition": "expired", "reason": "max_age"}
        if regenerating:
            attempts = _requeue_attempt_count(intent)
            if attempts is not None and attempts >= REQUEUE_MAX_ATTEMPTS:
                return {"disposition": "expired", "reason": "max_attempts"}
    return None


def _requeue_child_created_at(intent: dict) -> str:
    """Oldest known child created_at. Never invent a younger stamp on regen."""
    for key in ("child_created_at", "requeued_at"):
        value = intent.get(key)
        if isinstance(value, str) and value.strip() and _parse_timestamp_s(value) is not None:
            return value
    return goalflight_ledger.utc_now()


def _unlink_requeue_child_files(queue_dir: Path, child_id: str) -> None:
    # Only the queued envelope. A .claimed-* carrier may be a live launch;
    # unlinking it would discard work we have not proven is idle.
    queue_path = _queue_entry_path(child_id, queue_dir=queue_dir)
    with contextlib.suppress(OSError):
        queue_path.unlink()


def _commit_requeue_disposition(
    record: dict,
    intent: dict,
    decision: dict,
    *,
    queue_dir: Path,
) -> bool:
    now = goalflight_ledger.utc_now()
    updated = dict(intent)
    updated["disposition"] = decision["disposition"]
    updated["disposition_at"] = now
    updated["disposition_reason"] = decision.get("reason")
    if decision.get("satisfied_by"):
        updated["satisfied_by"] = decision["satisfied_by"]
    record["requeue"] = updated
    try:
        goalflight_ledger.write_record(record)
    except OSError as exc:
        print(
            "goalflight_dispatch: requeue disposition warning: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return False
    child_id = updated.get("child_id")
    if isinstance(child_id, str) and child_id:
        _unlink_requeue_child_files(queue_dir, child_id)
    return True


def _write_json_exclusive(path: Path, payload: dict) -> bool:
    """Durably create one queue entry without ever replacing an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return True


def _requeue_child_exists(queue_dir: Path, child_id: str) -> bool:
    queue_path = _queue_entry_path(child_id, queue_dir=queue_dir)
    try:
        if _existing_queue_entry_paths(queue_path):
            return True
    except OSError:
        # Unreadable queue is not "child gone". Keep: refuse to mint a duplicate.
        return True
    return goalflight_ledger.record_path(child_id, create=False).exists()


def _requeue_child_entry(
    entry: dict,
    *,
    child_id: str,
    requeued_from: str,
    queue_dir: Path,
    not_before: str | None,
    created_at: str | None = None,
    requeue_attempt: int | None = None,
) -> dict | None:
    dispatch_argv = list(entry.get("dispatch_argv") or [])
    if not dispatch_argv:
        return None
    base = _dispatch_base_dir()
    tail = base / f"{child_id}.tail"
    status_json = base / f"{child_id}.status.json"
    dispatch_argv = _set_option_before_worker_remainder(
        dispatch_argv, "--dispatch-id", child_id
    )
    dispatch_argv = _set_option_before_worker_remainder(
        dispatch_argv, "--tail", str(tail)
    )
    dispatch_argv = _set_option_before_worker_remainder(
        dispatch_argv, "--status-json", str(status_json)
    )
    now = goalflight_ledger.utc_now()
    child_created_at = created_at if isinstance(created_at, str) and created_at.strip() else now
    queue_path = _queue_entry_path(child_id, queue_dir=queue_dir)
    child = _sanitize_restore_envelope(entry, increment_recovery_count=False)
    request = (
        dict(child.get("request"))
        if isinstance(child.get("request"), dict)
        else {}
    )
    request.update(
        {
            "dispatch_id": child_id,
            "tail": str(tail),
            "status_json": str(status_json),
            "requeued_from": requeued_from,
        }
    )
    child.update(
        {
            "schema": DISPATCH_QUEUE_SCHEMA,
            "state": "queued",
            "dispatch_id": child_id,
            "created_at": child_created_at,
            "updated_at": now,
            "queue_path": str(queue_path),
            "dispatch_argv": dispatch_argv,
            "request": request,
            "requeued_from": requeued_from,
        }
    )
    if requeue_attempt is not None:
        child["requeue_attempt"] = requeue_attempt
        request["requeue_attempt"] = requeue_attempt
    for key in (
        "requeue",
        "restore_txn_id",
        "restore_reason",
        "claim_recovery_count",
        "orphan_first_seen_at",
    ):
        child.pop(key, None)
    if not_before:
        child["not_before"] = not_before
        request["not_before"] = not_before
    else:
        child.pop("not_before", None)
        request.pop("not_before", None)
    return child


def _maybe_requeue_terminal_claim(
    txn: _ReconcileTransaction,
    entry: dict,
    *,
    queue_dir: Path,
    tail: Path,
) -> bool:
    """Run the flag-first, fixed-child-id first-failure transaction.

    True means the old claim may be unlinked. False preserves it for recovery.
    """
    if not (txn.queue_locked and txn.ledger_locked):
        return False
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    if entry.get("requeued_from") or request.get("requeued_from"):
        return True
    dispatch_id = str(entry.get("dispatch_id") or "")
    if not dispatch_id:
        return True
    record_path = goalflight_ledger.record_path(dispatch_id, create=False)
    presence = goalflight_fs.path_presence(record_path)
    if presence == "unknown":
        return False
    if presence == "absent":
        return True
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except OSError:
        return False
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False

    intent = record.get("requeue")
    if _requeue_disposition_is_terminal(intent):
        return True
    would_requeue = _requeue_failure_kind(record, tail) in {"auth", "quota"}
    if isinstance(intent, dict) or would_requeue:
        decision = _requeue_stop_decision(record, entry, regenerating=False)
        if decision is not None:
            seed = intent if isinstance(intent, dict) else {}
            return _commit_requeue_disposition(
                record, seed, decision, queue_dir=queue_dir
            )
    if intent is None:
        effective_account = record.get("effective_account")
        if not isinstance(effective_account, str) or not effective_account:
            return True
        if _requeue_failure_kind(record, tail) not in {"auth", "quota"}:
            return True
        child_id = f"{dispatch_id}-retry-{uuid.uuid4().hex[:8]}"
        intent = {
            "child_id": child_id,
            "requeued_at": goalflight_ledger.utc_now(),
        }
        record["requeue"] = intent
        try:
            goalflight_ledger.write_record(record)
        except OSError as exc:
            print(
                "goalflight_dispatch: requeue intent warning: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            return False
    if not isinstance(intent, dict):
        return False
    child_id = intent.get("child_id")
    if not isinstance(child_id, str) or not child_id:
        return False
    if _requeue_child_exists(queue_dir, child_id):
        return True
    decision = _requeue_stop_decision(record, entry, regenerating=True)
    if decision is not None:
        return _commit_requeue_disposition(
            record, intent, decision, queue_dir=queue_dir
        )

    failure_kind = _requeue_failure_kind(record, tail)
    effective_account = record.get("effective_account")
    not_before = None
    if failure_kind == "quota":
        # Prefer the MEASURED retry policy over the blanket account cooldown:
        # an exhausted record carries its own reset (relaunch AT the reset,
        # not never and not constantly), a transient one is eligible soon.
        # A policy of None, or the hold_reset_unknown / legacy modes, mean the
        # measurement could not decide -- fall back to today's cooldown rather
        # than inventing eligibility.
        policy = goalflight_dispatch_states.retry_policy_for_record(record, now=time.time())
        mode = policy.get("mode") if isinstance(policy, dict) else None
        if mode == "retry_after_reset":
            not_before = policy.get("not_before")
        elif mode == "retry_soon":
            not_before = policy.get("not_before") or None
        elif isinstance(effective_account, str):
            not_before = _effective_account_cooldown(effective_account)
    created_at = _requeue_child_created_at(intent)
    prior_attempts = _requeue_attempt_count(intent)
    next_attempt = (prior_attempts or 0) + 1
    child = _requeue_child_entry(
        entry,
        child_id=child_id,
        requeued_from=dispatch_id,
        queue_dir=queue_dir,
        not_before=not_before,
        created_at=created_at,
        requeue_attempt=next_attempt,
    )
    if child is None:
        return False
    child_path = _queue_entry_path(child_id, queue_dir=queue_dir)
    try:
        created = _write_json_exclusive(child_path, child)
    except OSError as exc:
        print(
            "goalflight_dispatch: requeue child warning: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return False
    if created:
        intent["attempt_count"] = next_attempt
        intent["child_created_at"] = created_at
        intent["last_lodged_at"] = goalflight_ledger.utc_now()
        record["requeue"] = intent
        try:
            goalflight_ledger.write_record(record)
        except OSError as exc:
            print(
                "goalflight_dispatch: requeue attempt warning: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            # Child is already durable; keep the claim unlinked.
    return created or _requeue_child_exists(queue_dir, child_id)


def _mark_claim_worker_dead(
    entry: dict,
    *,
    reason: str,
    force_state: str | None = None,
    salvage_required: bool = False,
    resolution_source: str | None = None,
    claim: Path | None = None,
    queue_dir: Path | None = None,
    stale_s: float = 0.0,
    refusal_out: list[_ReconcileRefusal] | None = None,
) -> bool:
    """Terminalize a claim/ledger orphan. Sets record ``state`` (not only
    ``terminal_state``) so ``goalflight_status.py --wait`` resolves (b-065 B).

    Amendment G: re-scan completion evidence inside the ledger critical section
    before terminalizing so a late COMPLETE wins over worker_dead.

    ``force_state`` selects a system-owned terminal (``complete`` / ``superseded``)
    when the completion-authority ladder already decided; the locked finish path
    still rechecks the tail so a late COMPLETE can win over worker_dead.
    """
    dispatch_id = str(entry.get("dispatch_id") or "")
    if not dispatch_id:
        return False
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    args = _queued_args_for_status(entry)
    project_root = goalflight_task.resolve_project_root(
        str(entry.get("project_root") or request.get("cwd") or Path.cwd())
    )
    status_json = Path(str(request.get("status_json") or _dispatch_base_dir() / f"{dispatch_id}.status.json"))
    tail = Path(str(request.get("tail") or _dispatch_base_dir() / f"{dispatch_id}.tail"))
    ignore_prefix_lines = _ignore_prefix_lines(
        str(Path(args.prompt_file).expanduser()) if args.prompt_file else None
    )
    state, final_reason, terminal_marker = _resolve_claim_terminal_outcome(
        entry,
        reason=reason,
        tail=tail,
        ignore_prefix_lines=ignore_prefix_lines,
        agent=args.agent,
    )
    # Prefer a real SUCCESS tail scan over a forced state. Allow force_state for
    # complete/superseded only when the tail did not already produce a terminal
    # success (ladder proved task-store / system-owned outcome).
    if (
        force_state in {"complete", "superseded", BLOCKED_COMPLETION_AUTHORITY_STATE}
        and state == "worker_dead"
    ):
        state = force_state
        final_reason = reason
    queue_dir = (
        claim.parent
        if claim is not None
        else queue_dir or _dispatch_queue_dir()
    )
    begin = _begin_reconcile_transaction(
        entry,
        queue_dir=queue_dir,
        stale_s=stale_s,
        need_queue=True,
        need_task_store=_is_task_linked(entry, _find_dispatch_record(dispatch_id)),
        need_ledger=True,
    )
    txn = begin.transaction
    if txn is None:
        if refusal_out is not None and begin.refusal is not None:
            refusal_out.append(begin.refusal)
        return False
    try:
        fresh = entry
        if claim is not None:
            try:
                loaded = json.loads(claim.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            if not isinstance(loaded, dict):
                return False
            fresh = loaded
        result = _finish_ledger_under_lock(
            txn,
            fresh,
            {
                "state": state,
                "reason": final_reason,
                "salvage_required": salvage_required,
                "resolution_source": resolution_source,
            },
        )
        if not result.committed:
            return False
        durable_state = result.durable_state or state
        if not _maybe_requeue_terminal_claim(
            txn,
            fresh,
            queue_dir=queue_dir,
            tail=tail,
        ):
            return False
        if claim is not None:
            try:
                claim.unlink()
            except OSError:
                return False
    finally:
        txn.release()
    # Re-read for status mirror if finish preserved a prior terminal.
    with contextlib.suppress(Exception):
        record = _find_dispatch_record(dispatch_id)
        if record and record.get("state"):
            durable_state = str(record.get("state"))
            if record.get("terminal_state"):
                final_reason = record.get("reason") or record.get("error") or final_reason
    # Same derived-mirror rule as _write_reconciled_terminal_status: the
    # journal terminal and carrier decision are already durable here.
    with contextlib.suppress(Exception):
        write_status(
            status_json,
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": dispatch_id,
                "agent": args.agent,
                "shape": args.shape,
                "state": durable_state or "worker_dead",
                "terminal_state": goalflight_ledger.terminal_state_for(
                    durable_state or "worker_dead", final_reason
                ),
                "reason": final_reason,
                "liveness_state": goalflight_terminal.terminal_liveness_state(
                    durable_state or "worker_dead"
                ),
                "terminal_marker": terminal_marker,
                "project_root": str(project_root),
                "worker_pid": entry.get("queue_worker_pid"),
                "worker_alive": False,
                "tail_path": str(tail),
                "status_path": str(status_json),
                "updated_at": int(time.time()),
                "reconciliation": {
                    "source": "drain_claim_recovery",
                    "launch_timeout_s": LAUNCH_TIMEOUT_S,
                    "queue_launch_token": entry.get("queue_launch_token"),
                    "claim_recovery_count": _claim_recovery_count(entry),
                    "checked_output": True,
                    **({"resolution_source": resolution_source} if resolution_source else {}),
                    **({"salvage_required": True} if salvage_required else {}),
                },
                **({"task_ids": list(args.task_ids)} if getattr(args, "task_ids", None) else {}),
            },
        )
    return result.committed


def _drain_prelaunch_hook_path() -> Path:
    """Optional operator pre-launch hook, run once per local drain pass just before
    workers are launched. Almost always absent; an operator may install one to react
    to what is about to be launched. Configurable via GOALFLIGHT_DRAIN_PRELAUNCH_HOOK.
    """
    env = os.environ.get("GOALFLIGHT_DRAIN_PRELAUNCH_HOOK")
    if env:
        return Path(env).expanduser()
    return SCRIPT_DIR / "ext" / "drain-prelaunch-hook"


def _pass_agent_labels(entries) -> list[str]:
    """Best-effort distinct agent labels from the pre-scan of this drain pass, passed
    to the optional pre-launch hook so it can scope its work. Pre-scan may be partial,
    so this is advisory only."""
    labels: set[str] = set()
    for _sort_key, _path, scan_entry, _err in entries:
        if isinstance(scan_entry, dict) and scan_entry.get("agent"):
            labels.add(str(scan_entry["agent"]))
    return sorted(labels)


def _run_drain_prelaunch_hook(agents: list[str]) -> None:
    """Run the optional pre-launch hook if installed, passing the agent label(s) for
    this pass. No-op when absent; best-effort and time-bounded; never blocks or fails
    a drain (all errors swallowed)."""
    hook = _drain_prelaunch_hook_path()
    try:
        if not hook.exists():
            return
        subprocess.run(
            [str(hook), *agents],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        pass


class _DrainClaimGuard:
    """Own a drain claim until launch consumes it or finally restores it.

    Release is structural: ``__exit__`` always runs on return, continue, break,
    and exception. A trailing restore call cannot be skipped by a new early
    return. Pre-worker claims (no spawn intent) are restored to QUEUED;
    post-spawn carriers stay pending for recovery. ``consume()`` is only for
    paths that already resolved the carrier (launch cleanup, fail-mark).
    """

    def __init__(
        self,
        claim: Path,
        entry: dict,
        queue_dir: Path,
        *,
        stale_s: float,
        dispatch_id: str,
        acc: dict,
    ) -> None:
        self.claim = claim
        self.entry = entry
        self.queue_dir = queue_dir
        self.stale_s = stale_s
        self.dispatch_id = dispatch_id
        self.acc = acc
        self.consumed = False
        self.allow_restore = True
        self.launch_attempted = False
        self.release_reason = "claim_released_pre_worker"
        self.stamp_backoff = False
        self.count_launch_failure = False
        self.fail_reason = "launch_attempt_failed_unclassified"

    def consume(
        self,
        *,
        state: str,
        reason: str | None = None,
        extra: dict | None = None,
        launched: bool = False,
        left_queued: bool = False,
        failed: bool = False,
        pending: bool = False,
    ) -> None:
        self.consumed = True
        if launched:
            self.acc["launched"] += 1
            self.acc.setdefault("launched_task_ids", set()).update(
                _entry_task_ids(self.entry)
            )
        if pending and reason in _DRAIN_CONFIRMED_LAUNCH_REASONS:
            self.acc.setdefault("launched_task_ids", set()).update(
                _entry_task_ids(self.entry)
            )
        if left_queued:
            self.acc["left_queued"] += 1
        if failed:
            self.acc["failed"] += 1
        if pending:
            self.acc["pending_claims"] += 1
        detail = {"dispatch_id": self.dispatch_id, "state": state}
        if reason is not None:
            detail["reason"] = reason
        if extra:
            detail.update(extra)
        self.acc["details"].append(detail)

    def __enter__(self) -> "_DrainClaimGuard":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.consumed:
            return False
        if not self.claim.exists():
            self.acc["pending_claims"] += 1
            self.acc["details"].append(
                {
                    "dispatch_id": self.dispatch_id,
                    "state": "claimed",
                    "reason": "claim_missing_on_release",
                }
            )
            return False
        # TimeoutExpired SIGKILLs the child, so it can still restore a
        # pre-worker claim. Any other exception AFTER launch_attempted may
        # leave a live launcher; restoring that would replay it. Pre-worker
        # claims with no launch attempt must be released so a dead drain
        # cannot strand them.
        if (
            exc_type is not None
            and exc_type is not subprocess.TimeoutExpired
            and self.launch_attempted
        ):
            self.acc["pending_claims"] += 1
            self.acc["failed"] += 1
            self.acc["details"].append(
                {
                    "dispatch_id": self.dispatch_id,
                    "state": "claimed",
                    "reason": f"drain_exception_claim_held:{exc_type.__name__}",
                }
            )
            return False
        try:
            observed = json.loads(self.claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            observed = None
        if (
            not self.allow_restore
            or not isinstance(observed, dict)
            or not _entry_pre_worker(observed)
        ):
            self.acc["pending_claims"] += 1
            self.acc["failed"] += 1
            self.acc["details"].append(
                {
                    "dispatch_id": self.dispatch_id,
                    "state": "claimed",
                    "reason": self.release_reason,
                }
            )
            return False
        if self.launch_attempted and self.count_launch_failure:
            failure_count = _next_launch_failure_count(observed, self.entry)
            if failure_count >= MAX_DRAIN_PRE_WORKER_FAILURES:
                terminal_reason = (
                    f"launch_attempt_limit_exceeded:{self.fail_reason}"
                )
                committed, terminal_detail = _terminalize_pre_worker_launch_failure(
                    self.claim,
                    observed,
                    reason=terminal_reason,
                    failure_count=failure_count,
                )
                if committed:
                    self.acc["failed"] += 1
                    self.acc["details"].append(
                        {
                            "dispatch_id": self.dispatch_id,
                            "state": "failed",
                            "reason": terminal_reason,
                            "launch_timeout_count": failure_count,
                        }
                    )
                else:
                    self.acc["pending_claims"] += 1
                    self.acc["failed"] += 1
                    self.acc["details"].append(
                        {
                            "dispatch_id": self.dispatch_id,
                            "state": "claimed",
                            "reason": terminal_detail,
                            "launch_timeout_count": failure_count,
                        }
                    )
                return False
        restored, decision = _restore_claim_if_incomplete(
            self.claim,
            self.entry,
            self.queue_dir,
            stale_s=self.stale_s,
        )
        if decision is not None:
            self.acc["details"].append(_drain_decision_detail(self.dispatch_id, decision))
            if _completion_decision_is_deferred(decision):
                self.acc["pending_claims"] += 1
            return False
        if restored is None:
            if self.launch_attempted:
                _stamp_launch_attempt(
                    self.claim,
                    self.entry,
                    backoff=self.stamp_backoff,
                    failed=self.count_launch_failure,
                    fail_reason=self.fail_reason,
                )
            uncommitted = (
                "capacity_restore_uncommitted"
                if self.release_reason == "capacity_unavailable"
                else "remote_blocked_restore_uncommitted"
                if self.release_reason.startswith("remote_blocked:")
                else f"{self.release_reason}_uncommitted"
            )
            self.acc["pending_claims"] += 1
            self.acc["failed"] += 1
            self.acc["details"].append(
                {
                    "dispatch_id": self.dispatch_id,
                    "state": "claimed",
                    "reason": uncommitted,
                }
            )
            return False
        if self.launch_attempted:
            _stamp_launch_attempt(
                restored,
                self.entry,
                backoff=self.stamp_backoff,
                failed=self.count_launch_failure,
                fail_reason=self.fail_reason,
            )
        _restore_queued_record_from_entry(self.entry, restored)
        detail = {
            "dispatch_id": self.dispatch_id,
            "state": "queued",
            "reason": self.release_reason,
        }
        if self.entry.get("launch_backoff_until"):
            detail["launch_backoff_until"] = self.entry.get("launch_backoff_until")
        self.acc["left_queued"] += 1
        self.acc["details"].append(detail)
        return False


def _drain_project_key(entry: dict | None) -> str:
    """Stable project identity for per-pass journal isolation.

    Empty when the envelope has no project root: a skip then applies to that
    entry only, not to every other rootless envelope.
    """
    if not isinstance(entry, dict):
        return ""
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    raw = entry.get("project_root") or request.get("cwd")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        return str(goalflight_task.resolve_project_root(raw))
    except Exception:
        return raw


def _remember_drain_journal_skip(acc: dict, project_key: str, kind: str) -> None:
    if kind == "busy":
        acc["skipped_busy"] += 1
        if project_key:
            acc["skipped_projects_busy"].add(project_key)
    else:
        acc["skipped_error"] += 1
        if project_key:
            acc["skipped_projects_error"].add(project_key)


def _note_drain_journal_skip(
    acc: dict,
    *,
    dispatch_id: str,
    project_key: str,
    kind: str,
    exc: BaseException | None = None,
) -> None:
    """Record a per-project journal skip. Does not mutate queue files."""
    _remember_drain_journal_skip(acc, project_key, kind)
    if kind == "busy":
        reason = "journal_busy"
    else:
        reason = (
            f"journal_error:{type(exc).__name__}" if exc is not None else "journal_error"
        )
    acc["left_queued"] += 1
    detail = {
        "dispatch_id": dispatch_id,
        "state": "queued",
        "reason": reason,
    }
    if project_key:
        detail["project_root"] = project_key
    acc["details"].append(detail)


def _drain_launch_timeout_s(args) -> float:
    return max(
        DRAIN_LAUNCH_TIMEOUT_FLOOR_S,
        float(getattr(args, "capacity_wait_s", 0.0) or 0.0) + DRAIN_LAUNCH_CONFIRM_S,
    )


def _drain_launch_pass_budget_s(args) -> float:
    """Wall budget for launch-confirmation waits in one drain pass.

    Defaults to one confirmation timeout so N hung entries cannot take N
    timeouts. Tests inject ``args.launch_budget_s``.
    """
    override = getattr(args, "launch_budget_s", None)
    if override is not None:
        return max(0.05, float(override))
    return _drain_launch_timeout_s(args)


def _queue_entry_launch_backoff_ts(entry: dict | None) -> float | None:
    if not isinstance(entry, dict):
        return None
    parsed = goalflight_ledger.parse_utc(entry.get("launch_backoff_until"))
    return parsed.timestamp() if parsed is not None else None


def _launch_backoff_delay_s(timeout_count: int) -> float:
    exponent = max(0, int(timeout_count) - 1)
    return min(LAUNCH_BACKOFF_CAP_S, LAUNCH_BACKOFF_INITIAL_S * (2 ** exponent))


def _launch_attempt_consumed_budget(
    elapsed_s: float, pass_budget_s: float, remaining_after_s: float
) -> bool:
    """True when this attempt used a material slice of the pass launch budget.

    Discriminator is budget burn, not exception type. Fast refusals (missing
    argv, instant rc=2) leave leftover budget for the next entry this pass
    and must not stamp a 60s backoff — that would park capacity-waiting work.

    Derivation of material_s:
      production pass_budget ≈ DRAIN_LAUNCH_CONFIRM_S (45s)
        0.25 * 45 = 11.25; min(1.0, max(0.05, 11.25)) = 1.0s
      test pass_budget 0.40s
        0.25 * 0.40 = 0.10; min(1.0, max(0.05, 0.10)) = 0.10s
    A handshake that fails after ≥1s in production (or ≥0.10s in the probe-D
    budget) is a burn; a 20ms argv error is not. remaining_after_s <= 0 is
    always a burn: nothing else fits this pass.
    """
    if remaining_after_s <= 0:
        return True
    material_s = min(1.0, max(0.05, 0.25 * float(pass_budget_s)))
    return float(elapsed_s) >= material_s


def _mark_launch_budget_burn_if_material(
    lease: "_DrainClaimGuard",
    timing: dict,
    *,
    attempt_started: float,
    pass_budget_s: float,
    launch_deadline: float,
) -> None:
    elapsed_s = time.monotonic() - attempt_started
    remaining_after_s = launch_deadline - time.monotonic()
    if not _launch_attempt_consumed_budget(elapsed_s, pass_budget_s, remaining_after_s):
        return
    lease.stamp_backoff = True
    timing["launch_budget_burns"] = int(timing.get("launch_budget_burns") or 0) + 1


def _next_launch_failure_count(*entries: dict) -> int:
    return max(
        (
            int(entry.get("launch_timeout_count") or 0)
            for entry in entries
            if isinstance(entry, dict)
        ),
        default=0,
    ) + 1


def _stamp_launch_attempt(
    claim: Path,
    entry: dict,
    *,
    backoff: bool = False,
    failed: bool = False,
    fail_reason: str | None = None,
) -> None:
    """Persist the attempt cursor, failure count, and optional backoff.

    Distinct from not_before: usage-probe re-derivation must not clear these.
    last_attempted_at is the forward-progress cursor (never-attempted first).
    Backoff is the next-pass skip for a material budget burn. A fast failed
    launch still increments the failure count without parking the rest of the
    queue; capacity-only deferrals may back off without spending the terminal
    failure budget.
    """
    try:
        observed = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        observed = dict(entry)
    if not isinstance(observed, dict):
        observed = dict(entry)
    attempted_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
    observed["launch_last_attempted_at"] = attempted_at
    entry["launch_last_attempted_at"] = attempted_at
    count = int(
        observed.get("launch_timeout_count") or entry.get("launch_timeout_count") or 0
    )
    if failed:
        count += 1
        reason = fail_reason or "launch_attempt_failed_unclassified"
        observed["launch_timeout_count"] = count
        observed["launch_fail_reason"] = reason
        entry["launch_timeout_count"] = count
        entry["launch_fail_reason"] = reason
    if backoff:
        backoff_count = int(
            observed.get("launch_backoff_count")
            or entry.get("launch_backoff_count")
            or 0
        ) + 1
        delay_s = _launch_backoff_delay_s(backoff_count)
        until = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay_s)
        ).isoformat(timespec="seconds")
        reason = fail_reason or "launch_backoff_unclassified"
        observed["launch_backoff_count"] = backoff_count
        observed["launch_backoff_until"] = until
        observed["launch_fail_reason"] = reason
        entry["launch_backoff_count"] = backoff_count
        entry["launch_backoff_until"] = until
        entry["launch_fail_reason"] = reason
    with contextlib.suppress(OSError):
        _write_json_atomic(claim, observed)


def _terminalize_pre_worker_launch_failure(
    claim: Path,
    entry: dict,
    *,
    reason: str,
    failure_count: int,
) -> tuple[bool, str]:
    """Commit a proven pre-worker launch failure to ledger, status, and carrier."""
    dispatch_id = str(entry.get("dispatch_id") or claim.stem)
    try:
        _finish_ledger(
            dispatch_id,
            "failed",
            reason,
            worker_still_alive=False,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return False, f"launch_failure_terminal_commit_failed:{type(exc).__name__}"

    with _queue_mutation_lock(claim.parent):
        try:
            fresh = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fresh = dict(entry)
        if fresh.get("queue_launch_token") != entry.get("queue_launch_token"):
            return False, "launch_failure_terminal_claim_changed"
        fresh["launch_timeout_count"] = int(failure_count)
        fresh["launch_fail_reason"] = reason
        committed = _mark_claim_failed_locked(claim, fresh, reason=reason)
    if committed:
        _write_reconciled_terminal_status(fresh, None)
        return True, reason
    return False, "launch_failure_terminal_carrier_cleanup_pending"


def _drain_capacity_slots() -> dict:
    try:
        return goalflight_capacity.launch_slot_budget()
    except Exception:
        return {
            "unreadable": True,
            "operating_cap": 0,
            "active": 0,
            "global_remaining": 0,
            "by_pool": {},
        }


def _drain_launch_capacity_reason(
    returncode: int, stdout: str, stderr: str
) -> str | None:
    """Classify a drain-launch capacity refusal.

    Unreadable capacity state and genuine cap-reached both exit 2, and the
    launch stdout can carry ``blocked_capacity`` for both. Prefer
    ``capacity_state_unreadable`` so this cannot surface as
    ``capacity_unavailable`` (t-068).
    """
    if returncode != 2:
        return None
    launch_output = f"{stdout}{stderr}"
    if "capacity_state_unreadable" in launch_output:
        return "capacity_state_unreadable"
    if "blocked_capacity" in launch_output:
        return "capacity_unavailable"
    return None


def _drain_queue_once(args) -> dict:
    pass_started = time.monotonic()
    timing = {
        "pass_s": 0.0,
        "reconcile_s": 0.0,
        "recovery_s": 0.0,
        "recovery": {},
        "launch_s": 0.0,
        "journal_s": 0.0,
        "launch_timeouts": 0,
        "launch_budget_burns": 0,
        "launch_concurrency": 1,
        "capacity_slots": 0,
    }
    canonical_queue = _dispatch_queue_dir()
    queue_dir = Path(args.queue_dir).expanduser() if args.queue_dir else canonical_queue
    queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # --queue-dir is a SCOPE: drain envelopes already in that directory.
    # Ledger-wide restore into a foreign/empty directory is the b-276 trap.
    scoped_queue = not _same_queue_dir(queue_dir, canonical_queue)
    dispatch_ids = _drain_dispatch_id_filter(args)
    allow_cross = bool(getattr(args, "cross_project", False))
    invoker_label, invoker_project = _drain_invoker_identity(args)
    remote_node = _remote_drain_node(args)
    t_reconcile = time.monotonic()
    abandoned_reconciliation = {
        "schema": ABANDONED_RECONCILIATION_SCHEMA,
        "mode": "skipped",
        "closed": 0,
        "reason": "remote_drain",
    }
    if remote_node:
        _validate_remote_drain_node(args)
        if getattr(args, "remote_runner", None) is None:
            try:
                import goalflight_fleet_dispatch as fleet_dispatch

                fleet_dispatch.assert_live_ssh_opt_in()
            except Exception as exc:
                raise _RemoteDrainBlocked(str(exc), code="live_ssh_required") from exc
    elif scoped_queue:
        # Abandoned recon consults queue_dir for carriers. A private/empty
        # directory would look carrier-less and could close live work.
        abandoned_reconciliation = {
            "schema": ABANDONED_RECONCILIATION_SCHEMA,
            "mode": "skipped",
            "closed": 0,
            "reason": "queue_dir_scope",
        }
        _release_stale_capacity_for_drain()
    else:
        _release_stale_capacity_for_drain()
        abandoned_reconciliation = _reconcile_abandoned_for_drain(queue_dir)
    timing["reconcile_s"] = round(time.monotonic() - t_reconcile, 3)
    t_recovery = time.monotonic()
    recovery = _recover_claimed_queue_entries(
        queue_dir,
        stale_s=args.claim_stale_s,
        restore_ledger_orphans=not scoped_queue,
        dispatch_ids=dispatch_ids,
        invoker_label=invoker_label,
        invoker_project=invoker_project,
        allow_cross=allow_cross,
        scoped_queue=scoped_queue,
    )
    if recovery.get("listing_error"):
        # The queue could not be listed: every candidate is invisible, so a
        # "0 launched, 0 remaining" pass would be a false green. Fail the
        # drain with the listing error instead of reporting an empty queue.
        raise OSError(
            f"dispatch queue listing failed for {queue_dir}: {recovery['listing_error']}"
        )
    timing["recovery_s"] = round(time.monotonic() - t_recovery, 3)
    if isinstance(recovery.get("timing"), dict):
        timing["recovery"] = recovery["timing"]
    launched = 0
    left_queued = 0
    failed = 0
    pending_claims = 0
    details: list[dict] = []
    for pending in recovery.get("pending_reasons") or []:
        if not isinstance(pending, dict):
            continue
        row = dict(pending)
        row.setdefault("dispatch_id", str(pending.get("dispatch_id") or ""))
        row.setdefault("state", "claimed")
        row.update(_completion_authority_park_fields(str(row.get("reason") or "")))
        details.append(row)
    # restore_prepared envelopes are mid-restore transactions: only the
    # reconcile path (which owns the T→Q→S→L ordering) may complete them, so
    # they are never launch candidates. But dropping them SILENTLY was t-345:
    # a queue full of them reported launched:0 with no cause. Partition them
    # out and report each one with its owner-generation adjudication below.
    restore_prepared_candidates: list[tuple] = []
    launch_candidates: list[tuple] = []
    for path in _queue_dir_listing(queue_dir):
        if not path.name.endswith(".json"):
            continue
        candidate = _queue_entry_drain_candidate(path)
        if (
            isinstance(candidate[2], dict)
            and candidate[2].get("state") == "restore_prepared"
        ):
            restore_prepared_candidates.append(candidate)
        else:
            launch_candidates.append(candidate)
    entries = sorted(launch_candidates)
    queue_mutations = _empty_queue_mutations()
    _merge_queue_mutations(queue_mutations, recovery.get("queue_mutations"))
    if scoped_queue:
        _merge_queue_mutations(
            queue_mutations,
            _preview_scoped_ledger_retains(
                queue_dir,
                stale_s=args.claim_stale_s,
                dispatch_ids=dispatch_ids,
            ),
        )
    if dispatch_ids is not None:
        matched_ids: set[str] = set()
        filtered_entries = []
        for candidate in entries:
            _sort_key, path, entry, _read_error = candidate
            dispatch_id = (
                str(entry.get("dispatch_id") or path.stem)
                if isinstance(entry, dict)
                else path.stem
            )
            if dispatch_id in dispatch_ids:
                matched_ids.add(dispatch_id)
                filtered_entries.append(candidate)
        for missing_id in sorted(dispatch_ids - matched_ids):
            details.append(
                {
                    "dispatch_id": missing_id,
                    "state": "queued",
                    "reason": "not_in_queue",
                }
            )
        entries = filtered_entries
    if scoped_queue:
        kept_entries = []
        for candidate in entries:
            _sort_key, path, entry, read_error = candidate
            retain_reason = _scoped_launch_retain_reason(
                entry if isinstance(entry, dict) else None,
                invoker_label=invoker_label,
                invoker_project=invoker_project,
                allow_cross=allow_cross,
                read_error=read_error,
            )
            if retain_reason is None:
                kept_entries.append(candidate)
                continue
            dispatch_id = (
                str(entry.get("dispatch_id") or path.stem)
                if isinstance(entry, dict)
                else path.stem
            )
            label, project = _entry_owner_fields(
                entry if isinstance(entry, dict) else None
            )
            _record_queue_mutation(
                queue_mutations,
                action="retained",
                dispatch_id=dispatch_id,
                reason=retain_reason,
                controller_label=label,
                project_root=project,
            )
            left_queued += 1
            details.append(
                {
                    "dispatch_id": dispatch_id,
                    "state": "queued",
                    "reason": retain_reason,
                    "controller_label": label,
                    "project_root": project,
                }
            )
        entries = kept_entries
    now_s = time.time()
    # Re-derive not_before against current probe vs dispatch evidence rather
    # than freezing the reset predicted at requeue. A seat that now probes
    # healthy (and that probe is newer than the exhaustion record) is
    # eligible this pass; the queue file is not mutated.
    stored_gated = [
        candidate
        for candidate in entries
        if (
            _queue_entry_not_before_ts(candidate[2]) is not None
            and (_queue_entry_not_before_ts(candidate[2]) or 0) > now_s
        )
    ]
    deferred_entries: list = []
    entry_headroom = None
    if stored_gated:
        import goalflight_usage as usage

        probe_rows: list | None = None
        ledger_records: list | None = None
        headroom_cache: dict[tuple[str | None, str | None], dict] = {}

        def _entry_headroom(entry: dict | None) -> dict:
            nonlocal probe_rows, ledger_records
            key = _queue_entry_provider_account(entry)
            cached = headroom_cache.get(key)
            if cached is not None:
                return cached
            if probe_rows is None:
                providers = {
                    provider
                    for provider, _account in (
                        _queue_entry_provider_account(item[2])
                        for item in stored_gated
                    )
                    if provider
                }
                probe_rows = _probe_rows_for_not_before(
                    providers=providers or None
                )
            if ledger_records is None:
                try:
                    ledger_records = goalflight_ledger.read_records()
                except (OSError, ValueError):
                    ledger_records = []
            verdict = _headroom_for_queue_entry(
                entry,
                now=now_s,
                probe_rows=probe_rows,
                ledger_records=ledger_records,
            )
            headroom_cache[key] = verdict
            return verdict

        entry_headroom = _entry_headroom
        deferred_entries = [
            candidate
            for candidate in stored_gated
            if usage.not_before_still_gates(
                _queue_entry_not_before_ts(candidate[2]),
                now=now_s,
                headroom=_entry_headroom(candidate[2]),
            )
        ]
    entries = [candidate for candidate in entries if candidate not in deferred_entries]
    left_queued += len(deferred_entries)
    not_before_until: str | None = None
    not_before_until_ts: float | None = None
    not_before_winner: object = None
    not_before_winner_age: object = None
    for _sort_key, path, entry, _read_error in deferred_entries:
        not_before_ts = _queue_entry_not_before_ts(entry)
        row = {
            "dispatch_id": (
                str(entry.get("dispatch_id"))
                if isinstance(entry, dict) and entry.get("dispatch_id")
                else path.stem
            ),
            "state": "queued",
            "reason": "not_before",
            "not_before": (
                entry.get("not_before")
                if isinstance(entry, dict)
                else None
            ),
        }
        if entry_headroom is not None:
            row.update(
                _not_before_headroom_fields(
                    entry_headroom(entry if isinstance(entry, dict) else None),
                    now=now_s,
                )
            )
        details.append(row)
        if (
            isinstance(entry, dict)
            and not_before_ts is not None
            and (not_before_until_ts is None or not_before_ts < not_before_until_ts)
        ):
            not_before_until_ts = not_before_ts
            not_before_until = entry.get("not_before") or (
                entry.get("request") if isinstance(entry.get("request"), dict) else {}
            ).get("not_before")
            not_before_winner = row.get("winner")
            not_before_winner_age = row.get("winner_age")
    # Hold-reason aggregation (t-345): the summary must say WHY entries are
    # held, not only THAT launched:0. `until` is the EARLIEST pending
    # not_before — when this queue next becomes eligible to make progress.
    # Winner+age of that earliest row is the operator surface for launched:0
    # drains that only read the hold summary / compact DRAIN line.
    holds: dict = {
        "not_before": {
            "count": len(deferred_entries),
            "until": not_before_until,
            "winner": not_before_winner,
            "winner_age": not_before_winner_age,
        },
        "restore_prepared": {
            "count": len(restore_prepared_candidates),
            "owner_live": 0,
            "owner_dead": 0,
            "owner_unknown": 0,
        },
        "reconcile_pending": {},
        "quarantined": int(recovery.get("quarantined") or 0),
        "launch_backoff": {"count": 0, "until": None},
        "pass_launch_budget": {"count": 0},
    }
    for pending in recovery.get("pending_reasons") or []:
        if isinstance(pending, dict):
            reason = str(pending.get("reason") or "unspecified")
            holds["reconcile_pending"][reason] = holds["reconcile_pending"].get(reason, 0) + 1
    attention: list[dict] = []
    if restore_prepared_candidates:
        # One ledger scan per pass, not per entry. The record is the only
        # owner-identity channel: the restore envelope itself carries none.
        # An unreadable ledger fails closed: every entry reports unknown.
        try:
            records_by_id: dict[str, dict] | None = {
                str(record.get("dispatch_id") or ""): record
                for record in goalflight_ledger.read_records()
                if isinstance(record, dict)
            }
        except Exception:
            records_by_id = None
        for _sort_key, path, entry, _read_error in restore_prepared_candidates:
            dispatch_id = (
                str(entry.get("dispatch_id"))
                if isinstance(entry, dict) and entry.get("dispatch_id")
                else path.stem
            )
            owner_state, owner_reason = _restore_prepared_owner_state(
                entry if isinstance(entry, dict) else {},
                records_by_id.get(dispatch_id) if records_by_id is not None else None,
            )
            left_queued += 1
            holds["restore_prepared"][f"owner_{owner_state}"] += 1
            details.append(
                {
                    "dispatch_id": dispatch_id,
                    "state": "restore_prepared",
                    "reason": "awaiting_owner_reconcile",
                    "owner_state": owner_state,
                    "detail": f"owner_{owner_state}:{owner_reason}",
                }
            )
            if owner_state == "dead":
                # Proof-gated attention item (never an expiry): the owning
                # controller generation is provably dead, so no live pass will
                # complete this restore transaction. Surfaced for the operator;
                # the queue file itself is left untouched.
                item = {
                    "dispatch_id": dispatch_id,
                    "state": "restore_prepared",
                    "attention": "owner_generation_dead",
                    "owner_reason": owner_reason,
                }
                if isinstance(entry, dict):
                    if entry.get("project_root"):
                        item["project_root"] = str(entry.get("project_root"))
                    if entry.get("updated_at"):
                        item["updated_at"] = str(entry.get("updated_at"))
                attention.append(item)
    try:
        occupancy_records = list(goalflight_ledger.read_records())
    except OSError:
        occupancy_records = []
    for record in _iter_cwdless_nonterminal_records(occupancy_records):
        # Bookkeeping defect, not a hold: skip is correct, silence is not.
        # Compact DRAIN `cwdless_nonterminal` + stderr WARN are the surfaces
        # the launch operator and the periodic drainer already read.
        attention.append(
            {
                "dispatch_id": str(record.get("dispatch_id") or ""),
                "state": str(record.get("state") or "running"),
                "attention": "cwdless_nonterminal",
            }
        )
        _warn_cwdless_nonterminal(record)
    if args.limit and args.limit > 0:
        entries = entries[: args.limit]
    if not remote_node and entries:
        _run_drain_prelaunch_hook(_pass_agent_labels(entries))
    drain_acc = {
        "launched": launched,
        "left_queued": left_queued,
        "failed": failed,
        "pending_claims": pending_claims,
        "skipped_busy": 0,
        "skipped_error": 0,
        "skipped_projects_busy": set(),
        "skipped_projects_error": set(),
        "details": details,
        "launched_task_ids": set(),
    }
    timeout_s = _drain_launch_timeout_s(args)
    pass_budget_s = _drain_launch_pass_budget_s(args)
    slots = _drain_capacity_slots()
    timing["capacity_slots"] = int(slots.get("global_remaining") or 0)
    timing["capacity_unreadable"] = bool(slots.get("unreadable"))
    t_launch = time.monotonic()
    launch_deadline = t_launch + pass_budget_s
    for _sort_key, path, _scan_entry, _scan_read_error in entries:
        claim_error: Exception | None = None
        entry: dict | None = None
        project_key = _drain_project_key(_scan_entry)
        dispatch_id = (
            str(_scan_entry.get("dispatch_id") or path.stem)
            if isinstance(_scan_entry, dict)
            else path.stem
        )
        if _scan_read_error:
            # Unscoped drain used to claim then mutate unreadable JSON to
            # .failed. Scoped drain already retains; the canonical queue
            # must too.
            drain_acc["left_queued"] += 1
            details.append(
                {
                    "dispatch_id": dispatch_id,
                    "state": "queued",
                    "reason": "unreadable_queue_entry",
                }
            )
            continue
        if project_key and project_key in drain_acc["skipped_projects_busy"]:
            _note_drain_journal_skip(
                drain_acc,
                dispatch_id=dispatch_id,
                project_key=project_key,
                kind="busy",
            )
            continue
        if project_key and project_key in drain_acc["skipped_projects_error"]:
            _note_drain_journal_skip(
                drain_acc,
                dispatch_id=dispatch_id,
                project_key=project_key,
                kind="error",
            )
            continue
        backoff_ts = _queue_entry_launch_backoff_ts(
            _scan_entry if isinstance(_scan_entry, dict) else None
        )
        if backoff_ts is not None and backoff_ts > time.time():
            drain_acc["left_queued"] += 1
            until = (
                _scan_entry.get("launch_backoff_until")
                if isinstance(_scan_entry, dict)
                else None
            )
            details.append(
                {
                    "dispatch_id": dispatch_id,
                    "state": "queued",
                    "reason": "launch_backoff",
                    "launch_backoff_until": until,
                }
            )
            holds["launch_backoff"]["count"] = int(holds["launch_backoff"]["count"]) + 1
            if until and (
                holds["launch_backoff"].get("until") is None
                or str(until) < str(holds["launch_backoff"]["until"])
            ):
                holds["launch_backoff"]["until"] = until
            continue
        remaining_s = launch_deadline - time.monotonic()
        if remaining_s <= 0:
            drain_acc["left_queued"] += 1
            details.append(
                {
                    "dispatch_id": dispatch_id,
                    "state": "queued",
                    "reason": "pass_launch_budget",
                }
            )
            holds["pass_launch_budget"]["count"] = int(
                holds["pass_launch_budget"]["count"]
            ) + 1
            continue
        t_journal = time.monotonic()
        try:
            launch_token = _queue_launch_token(_scan_entry)
        except goalflight_journal.JournalBusy as exc:
            # Busy is retryable and per-project. Skipping this project for the
            # rest of the pass keeps one contended journal from denying every
            # other project's queued work.
            timing["journal_s"] += time.monotonic() - t_journal
            _note_drain_journal_skip(
                drain_acc,
                dispatch_id=dispatch_id,
                project_key=project_key,
                kind="busy",
                exc=exc,
            )
            continue
        except (
            goalflight_journal.JournalDisappeared,
            goalflight_journal.JournalIOError,
            goalflight_journal.JournalError,
        ) as exc:
            # Name the availability subtypes: a bare JournalError catch flattens
            # unreadable vs disappeared. Unreadable still fails closed here
            # (do not launch); the reason string keeps the concrete class.
            timing["journal_s"] += time.monotonic() - t_journal
            _note_drain_journal_skip(
                drain_acc,
                dispatch_id=dispatch_id,
                project_key=project_key,
                kind="error",
                exc=exc,
            )
            continue
        timing["journal_s"] += time.monotonic() - t_journal
        _test_signal_file(
            "GOALFLIGHT_TEST_DRAIN_BEFORE_CLAIM_FILE",
            str(path),
        )
        with _queue_mutation_lock(queue_dir):
            claim = _claim_queue_entry(path)
            if claim is not None:
                try:
                    payload = json.loads(claim.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("invalid_payload")
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    claim_error = exc
                    with contextlib.suppress(OSError):
                        _mark_claim_failed_locked(
                            claim,
                            {"path": str(claim)},
                            reason=f"unreadable:{type(exc).__name__}",
                        )
                else:
                    entry = payload
                    entry["state"] = "claimed"
                    entry["queue_launch_token"] = launch_token
                    entry["queue_claimer_pid"] = os.getpid()
                    claimer_identity = goalflight_ledger.process_identity(os.getpid())
                    if isinstance(claimer_identity, dict):
                        entry["queue_claimer_identity"] = claimer_identity
                    entry["updated_at"] = goalflight_ledger.utc_now()
                    try:
                        _write_json_atomic(claim, entry)
                    except OSError as exc:
                        claim_error = exc
        if claim is None:
            continue
        if claim_error is not None or entry is None:
            drain_acc["failed"] += 1
            if entry is not None:
                details.append(
                    {
                        "dispatch_id": str(entry.get("dispatch_id") or path.stem),
                        "state": "claimed",
                        "reason": f"claim_token_write_failed:{type(claim_error).__name__}",
                    }
                )
            continue
        dispatch_id = str(entry.get("dispatch_id") or path.stem)
        with _DrainClaimGuard(
            claim,
            entry,
            queue_dir,
            stale_s=args.claim_stale_s,
            dispatch_id=dispatch_id,
            acc=drain_acc,
        ) as lease:
            launch_task_ids = set(_entry_task_ids(entry))
            decision = _entry_completion_authority(entry)
            same_task_already_spawned = bool(
                launch_task_ids
                and launch_task_ids & drain_acc["launched_task_ids"]
            )
            if same_task_already_spawned or _completion_decision_blocks_restore(
                decision
            ):
                # Do not spawn. Leave the claim unconsumed so __exit__ restore
                # terminalizes or parks through the same authority path
                # reconcile already uses. Occupancy-forced does not bypass
                # this: it waives the worktree lock, not a second worker.
                # launched_task_ids covers the same-pass queued pair whose
                # sibling is still listed as queued until the child records
                # starting — counting queued in _ledger_task_ids_advanced
                # would deadlock restore of two parked envelopes.
                continue
            try:
                launch_argv = _drain_launch_argv(
                    list(entry.get("dispatch_argv") or []),
                    capacity_wait_s=args.capacity_wait_s,
                    queue_launch_token=launch_token,
                    queue_claim_path=claim,
                )
            except goalflight_journal.JournalBusy:
                lease.release_reason = "journal_busy"
                _remember_drain_journal_skip(drain_acc, project_key, "busy")
                continue
            except (
                goalflight_journal.JournalDisappeared,
                goalflight_journal.JournalIOError,
                goalflight_journal.JournalError,
            ) as exc:
                lease.release_reason = f"journal_error:{type(exc).__name__}"
                _remember_drain_journal_skip(drain_acc, project_key, "error")
                continue
            if not launch_argv:
                committed = _mark_claim_failed(claim, entry, reason="missing_dispatch_argv")
                # Every other refusal in this loop appends a detail; this one did
                # not, so the submitter saw only the generic failure COUNT and no
                # reason — the exact blindness the per-entry reporter exists to
                # remove. Report the DURABLE state: the mark writes "failed", but
                # it declines to write at all on a launch-token race, and claiming
                # "failed" then would describe a write that did not happen.
                lease.consume(
                    state="failed" if committed else "claimed",
                    reason="missing_dispatch_argv",
                    failed=True,
                    pending=not committed,
                )
                continue
            entry_timeout_s = min(
                timeout_s,
                max(0.05, launch_deadline - time.monotonic()),
            )
            attempt_started = time.monotonic()
            try:
                lease.launch_attempted = True
                if remote_node:
                    proc = _drain_launch_remote_claim(
                        args,
                        entry,
                        dispatch_id=dispatch_id,
                        launch_token=launch_token,
                        claim=claim,
                    )
                else:
                    drain_env = _sidecar_env(os.environ.copy())
                    proc = subprocess.run(
                        [sys.executable, str(Path(__file__).resolve()), *launch_argv],
                        cwd=str(Path(entry.get("process_cwd") or entry.get("project_root") or Path.cwd()).resolve()),
                        env=drain_env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=entry_timeout_s,
                    )
            except _RemoteDrainBlocked as exc:
                lease.release_reason = f"remote_blocked:{exc.code}:{exc}"
                lease.count_launch_failure = True
                lease.fail_reason = lease.release_reason
                _mark_launch_budget_burn_if_material(
                    lease,
                    timing,
                    attempt_started=attempt_started,
                    pass_budget_s=pass_budget_s,
                    launch_deadline=launch_deadline,
                )
                continue
            except subprocess.TimeoutExpired:
                timing["launch_timeouts"] += 1
                ledger_confirmed = _dispatch_has_worker_record(
                    dispatch_id,
                    queue_launch_token=launch_token,
                )
                if ledger_confirmed:
                    carrier_cleanup = _positive_live_carrier_cleanup(
                        claim,
                        entry,
                        queue_dir,
                        worker_record_sufficient=True,
                        stale_s=args.claim_stale_s,
                    )
                    if carrier_cleanup == "pending":
                        _alert_launched_carrier_pending(dispatch_id, where="drain")
                        lease.consume(
                            state="claimed",
                            reason="worker_record_present_after_launch_timeout_carrier_cleanup_pending",
                            pending=True,
                        )
                    else:
                        lease.consume(
                            state="launched",
                            reason="worker_record_present_after_launch_timeout",
                            launched=True,
                        )
                    continue
                lease.release_reason = "launch_timeout_pending_ledger"
                lease.stamp_backoff = True
                lease.count_launch_failure = True
                lease.fail_reason = (
                    "launch_confirmation_timeout:no_token_matched_worker_record"
                )
                continue
            stdout_launched = proc.returncode == 0 and "DISPATCH-LAUNCHED " in proc.stdout
            # Drain launch accounting: a token-matched worker_pid means launch occurred
            # (worker may already be dead). Recovery must NOT treat that weak presence
            # as "claim safe to drop without terminalizing" — see
            # _recover_claimed_queue_entries (require_live_nonterminal) + ledger-only
            # orphan scan. Clearing the claim here is fine because reconcile will
            # terminalize a task-linked zombie from the ledger.
            ledger_confirmed = _dispatch_has_worker_record(
                dispatch_id,
                queue_launch_token=launch_token,
            )
            capacity_reason = _drain_launch_capacity_reason(
                proc.returncode, proc.stdout, proc.stderr
            )
            if stdout_launched and ledger_confirmed:
                carrier_cleanup = _positive_live_carrier_cleanup(
                    claim,
                    entry,
                    queue_dir,
                    # This drain just ledger-confirmed the launch (token-matched
                    # record), so the carrier is redundant even if a fast worker
                    # already exited; reconcile terminalizes from the ledger.
                    worker_record_sufficient=True,
                    stale_s=args.claim_stale_s,
                )
                if carrier_cleanup == "pending":
                    # Launch is durably ledger-confirmed, but the carrier could
                    # not be cleared: surface it instead of silently reporting
                    # success. "launched" means launched AND carrier resolved, so
                    # a pending cleanup is counted as pending only — never as a
                    # launch.
                    _alert_launched_carrier_pending(dispatch_id, where="drain")
                    lease.consume(
                        state="claimed",
                        reason="launched_carrier_cleanup_pending",
                        pending=True,
                    )
                else:
                    lease.consume(state="launched", launched=True)
                continue
            if capacity_reason:
                lease.release_reason = capacity_reason
                lease.fail_reason = capacity_reason
                _mark_launch_budget_burn_if_material(
                    lease,
                    timing,
                    attempt_started=attempt_started,
                    pass_budget_s=pass_budget_s,
                    launch_deadline=launch_deadline,
                )
                continue
            if stdout_launched and not ledger_confirmed:
                # Stdout is not launch proof. Keep the carrier pending until a
                # token-matched ledger row exists; restoring would replay.
                lease.allow_restore = False
                lease.release_reason = f"launch_failed_pending_ledger:{proc.returncode}"
                continue
            if ledger_confirmed:
                carrier_cleanup = _positive_live_carrier_cleanup(
                    claim,
                    entry,
                    queue_dir,
                    # This drain just ledger-confirmed the launch (token-matched
                    # record), so the carrier is redundant even if a fast worker
                    # already exited; reconcile terminalizes from the ledger.
                    worker_record_sufficient=True,
                    stale_s=args.claim_stale_s,
                )
                if carrier_cleanup == "pending":
                    # Same accounting contract as the stdout_launched branch: a
                    # pending cleanup is pending, not a launch.
                    _alert_launched_carrier_pending(dispatch_id, where="drain")
                    lease.consume(
                        state="claimed",
                        reason="worker_record_present_carrier_cleanup_pending",
                        pending=True,
                    )
                else:
                    lease.consume(
                        state="launched",
                        reason="worker_record_present",
                        launched=True,
                    )
                continue
            try:
                observed_claim = json.loads(claim.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                observed_claim = None
            if (
                isinstance(observed_claim, dict)
                and observed_claim.get("queue_launch_token") == launch_token
                and _entry_pre_worker(observed_claim)
            ):
                if proc.returncode != 0:
                    permanent_reason = _permanent_pre_spawn_refusal_reason(proc)
                    if permanent_reason:
                        # Inert flag combo / unsupported sandbox: waiting cannot
                        # make it valid. Restore-to-queued is the silent stick.
                        committed = _mark_claim_failed(
                            claim, entry, reason=permanent_reason
                        )
                        lease.consume(
                            state="failed" if committed else "claimed",
                            reason=permanent_reason,
                            failed=True,
                            pending=not committed,
                        )
                        continue
                    lease.fail_reason = _pre_spawn_launch_failure_reason(proc)
                    lease.release_reason = (
                        f"launch_refused_pre_spawn:{proc.returncode}"
                    )
                else:
                    lease.fail_reason = (
                        "launch_confirmation_missing:exit_0_no_worker_record"
                    )
                    lease.release_reason = lease.fail_reason
                lease.count_launch_failure = True
                _mark_launch_budget_burn_if_material(
                    lease,
                    timing,
                    attempt_started=attempt_started,
                    pass_budget_s=pass_budget_s,
                    launch_deadline=launch_deadline,
                )
                continue
            lease.release_reason = f"launch_failed_pending_ledger:{proc.returncode}"
    # Launches already happened, so a listing failure here must not become the
    # error payload (which reports launched=0): report the count as unknown.
    queue_listing_error: str | None = None
    try:
        remaining: int | None = sum(
            1 for path in _queue_dir_listing(queue_dir) if path.name.endswith(".json")
        )
    except OSError as exc:
        remaining = None
        queue_listing_error = f"queue_dir_unreadable:{type(exc).__name__}"
    claimer_counts = _claimer_report_fields(queue_dir)
    if queue_listing_error is None and claimer_counts.get("queue_listing_error"):
        queue_listing_error = str(claimer_counts["queue_listing_error"])
    _warn_drain_queue_mutations(queue_mutations)
    if not int((holds.get("launch_backoff") or {}).get("count") or 0):
        holds.pop("launch_backoff", None)
    if not int((holds.get("pass_launch_budget") or {}).get("count") or 0):
        holds.pop("pass_launch_budget", None)
    timing["launch_s"] = round(time.monotonic() - t_launch, 3)
    timing["journal_s"] = round(float(timing["journal_s"]), 3)
    timing["pass_s"] = round(time.monotonic() - pass_started, 3)
    payload = {
        "schema": f"{DISPATCH_QUEUE_SCHEMA}.drain.v1",
        "queue_dir": str(queue_dir),
        "launched": drain_acc["launched"],
        "left_queued": drain_acc["left_queued"],
        "failed": drain_acc["failed"],
        "remaining": remaining,
        "pending_claims": drain_acc["pending_claims"],
        "skipped_busy": drain_acc["skipped_busy"],
        "skipped_error": drain_acc["skipped_error"],
        "dead_claimer": claimer_counts["dead_claimer"],
        "unknown_claimer": claimer_counts["unknown_claimer"],
        "live_claimer": claimer_counts["live_claimer"],
        "holds": holds,
        "attention": attention,
        "recovered_claims": recovery,
        "abandoned_reconciliation": abandoned_reconciliation,
        "details": details,
        "created": int(queue_mutations.get("created") or 0),
        "relocated": int(queue_mutations.get("relocated") or 0),
        "retained": int(queue_mutations.get("retained") or 0),
        "queue_mutations": queue_mutations,
        "queue_dir_scope": scoped_queue,
        "timing": timing,
    }
    if dispatch_ids is not None:
        payload["dispatch_ids"] = sorted(dispatch_ids)
    if queue_listing_error is not None:
        payload["queue_listing_error"] = queue_listing_error
    return payload


def _drain_error_payload(args, exc: BaseException) -> dict:
    queue_dir = Path(args.queue_dir).expanduser() if args.queue_dir else _dispatch_queue_dir()
    payload = {
        "schema": f"{DISPATCH_QUEUE_SCHEMA}.drain.v1",
        "queue_dir": str(queue_dir),
        "launched": 0,
        "left_queued": 0,
        "failed": 1,
        "remaining": 0,
        "pending_claims": 0,
        "skipped_busy": 0,
        "skipped_error": 0,
        "dead_claimer": 0,
        "unknown_claimer": 0,
        "live_claimer": 0,
        "holds": {},
        "attention": [],
        "recovered_claims": {"restored": 0, "failed": 0},
        "details": [],
        "created": 0,
        "relocated": 0,
        "retained": 0,
        "queue_mutations": _empty_queue_mutations(),
        "error": f"{type(exc).__name__}: {exc}",
    }
    if isinstance(exc, _RemoteDrainBlocked):
        payload["blocked"] = True
        payload["error"] = f"DISPATCH-BLOCKED {exc}"
        payload["code"] = exc.code
    return payload


def _cmd_drain(argv: list[str]) -> int:
    parser = _TerseArgumentParser(
        description="Drain queued goal-flight dispatch requests.",
        usage_hint="try drain [--json] [--limit N] (or --help)",
    )
    parser.add_argument(
        "--queue-dir",
        help=(
            "Drain only envelopes already in this directory. Does not restore "
            "ledger orphans into it (that was a destination trap). Default: "
            "the canonical shared dispatch-queue."
        ),
    )
    parser.add_argument(
        "--dispatch-id",
        action="append",
        dest="dispatch_ids",
        metavar="ID",
        help="Launch only this queued dispatch id (repeatable). Other entries are left untouched.",
    )
    parser.add_argument(
        "--cross-project",
        action="store_true",
        help=(
            "Allow claiming/launching envelopes in --queue-dir whose "
            "controller_label or project_root differs from this controller. "
            "Default: retain foreign-owned files already in a scoped directory."
        ),
    )
    parser.add_argument(
        "--controller-label",
        help="Invoking controller label (default: $GOALFLIGHT_CONTROLLER_LABEL).",
    )
    parser.add_argument("--capacity-wait-s", type=float, default=0.0)
    parser.add_argument("--claim-stale-s", type=float, default=QUEUE_CLAIM_STALE_S)
    parser.add_argument("--limit", type=int, default=0, help="maximum queue entries to inspect; 0 = all")
    parser.add_argument("--remote-node", help="Launch claimed queue entries on this fleet node instead of locally.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = _drain_queue_once(args)
    except _RemoteDrainBlocked as exc:
        payload = _drain_error_payload(args, exc)
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(payload["error"], file=sys.stderr)
        return 2
    except (OSError, RuntimeError) as exc:
        payload = _drain_error_payload(args, exc)
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"goalflight_dispatch: drain failed: {payload['error']}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        holds = payload.get("holds") if isinstance(payload.get("holds"), dict) else {}
        not_before_hold = holds.get("not_before") if isinstance(holds.get("not_before"), dict) else {}
        restore_hold = (
            holds.get("restore_prepared") if isinstance(holds.get("restore_prepared"), dict) else {}
        )
        print(
            "DRAIN "
            + json.dumps(
                {
                    "launched": payload["launched"],
                    "left_queued": payload["left_queued"],
                    "failed": payload["failed"],
                    "remaining": payload["remaining"],
                    "skipped_busy": payload.get("skipped_busy", 0),
                    "skipped_error": payload.get("skipped_error", 0),
                    "dead_claimer": payload.get("dead_claimer", 0),
                    "unknown_claimer": payload.get("unknown_claimer", 0),
                    "live_claimer": payload.get("live_claimer", 0),
                    "waiting_not_before": not_before_hold.get("count", 0),
                    "waiting_not_before_until": not_before_hold.get("until"),
                    "waiting_not_before_winner": not_before_hold.get("winner"),
                    "waiting_not_before_age": not_before_hold.get("winner_age"),
                    "awaiting_owner_reconcile": restore_hold.get("count", 0),
                    "owner_generation_dead": restore_hold.get("owner_dead", 0),
                    "quarantined": holds.get("quarantined", 0),
                    "attention": len(payload.get("attention") or []),
                    "cwdless_nonterminal": sum(
                        1
                        for item in (payload.get("attention") or [])
                        if isinstance(item, dict)
                        and item.get("attention") == "cwdless_nonterminal"
                    ),
                    "queue_dir": payload["queue_dir"],
                    "created": payload.get("created", 0),
                    "relocated": payload.get("relocated", 0),
                    "retained": payload.get("retained", 0),
                    "pass_s": (payload.get("timing") or {}).get("pass_s"),
                    "launch_s": (payload.get("timing") or {}).get("launch_s"),
                    "reconcile_s": (payload.get("timing") or {}).get("reconcile_s"),
                    "journal_s": (payload.get("timing") or {}).get("journal_s"),
                    "launch_timeouts": (payload.get("timing") or {}).get("launch_timeouts"),
                    "launch_budget_burns": (payload.get("timing") or {}).get("launch_budget_burns"),
                    "capacity_slots": (payload.get("timing") or {}).get("capacity_slots"),
                    "waiting_launch_backoff": (
                        holds.get("launch_backoff") or {}
                    ).get("count", 0)
                    if isinstance(holds.get("launch_backoff"), dict)
                    else 0,
                    "pass_launch_budget": (
                        holds.get("pass_launch_budget") or {}
                    ).get("count", 0)
                    if isinstance(holds.get("pass_launch_budget"), dict)
                    else 0,
                    "retained_by_controller": {
                        str(label): int((counts or {}).get("retained") or 0)
                        for label, counts in (
                            (payload.get("queue_mutations") or {}).get("by_controller") or {}
                        ).items()
                        if int((counts or {}).get("retained") or 0)
                    },
                },
                sort_keys=True,
            )
        )
    return 0 if payload["failed"] == 0 else 1


@contextlib.contextmanager
def _temporary_env(updates: dict[str, str], *, remove: list[str] | None = None):
    prior: dict[str, str | None] = {}
    for key in updates:
        prior[key] = os.environ.get(key)
        os.environ[key] = updates[key]
    for key in remove or []:
        if key not in prior:
            prior[key] = os.environ.get(key)
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _normalize_acp_agent(args) -> None:
    agent = str(args.agent or "").strip().lower()
    aliases = {
        "worker": "codex-acp",
        "codex": "codex-acp",
        "codex-acp": "codex-acp",
        "grok-acp": "grok-acp",
        "cursor": "cursor",
        "cursor-agent": "cursor",
        "claude": "claude",
        "claude-acp": "claude",
        "claude-code-cli-acp": "claude",
    }
    args.agent = aliases.get(agent, agent)
    if args.agent not in {"codex-acp", "grok-acp", "cursor", "claude"}:
        raise DispatchUsageError(
            "--shape acp v1 supports --agent codex-acp, grok-acp, cursor, or "
            f"claude-acp; got {agent!r}"
        )


def _acp_watcher_prompt_path(status_json: Path) -> Path:
    status_path = status_json.expanduser().resolve()
    name = status_path.name
    if name.endswith(".status.json"):
        stem = name[: -len(".status.json")]
    else:
        stem = status_path.stem
    return status_path.with_name(f"{stem}.assembled.prompt")


LEGACY_PROMPT_SWEEP_MARKER = ".legacy-assembled-prompts-private-v1"


def _atomic_write_private_text(path: Path, text: str) -> None:
    """Replace a text sidecar atomically without ever exposing its contents."""

    parent_was_missing = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent_was_missing:
        path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd: int | None = None
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = None
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()


def _prepare_private_dispatch_dir() -> Path:
    """Create the dispatch dir privately and repair legacy prompt sidecars."""

    base = _dispatch_base_dir()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        base.chmod(0o700)
    except FileNotFoundError:
        # A concurrent cleaner can delete the directory between mkdir and
        # chmod; recreate once and continue — hardening is best-effort and
        # must never abort dispatcher startup.
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            base.chmod(0o700)
    marker = base / LEGACY_PROMPT_SWEEP_MARKER
    marker_fd: int | None = None
    try:
        # Claim the version before scanning. O_EXCL makes concurrent dispatcher
        # startups observe the same one-shot decision instead of racing through
        # the directory together. A claimed-but-interrupted sweep is still a
        # completed best-effort compatibility attempt; new writes are private.
        marker_fd = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.write(marker_fd, b"legacy assembled prompt sweep v1 claimed\n")
        os.fsync(marker_fd)
    except FileExistsError:
        return base
    except OSError as exc:
        print(
            f"goalflight dispatch: legacy prompt sweep marker failed for {marker}: {exc}",
            file=sys.stderr,
        )
        return base
    finally:
        if marker_fd is not None:
            with contextlib.suppress(OSError):
                os.close(marker_fd)
    # One versioned compatibility sweep for sidecars created as 0644 by older
    # dispatchers. Every file is best-effort so one concurrent deletion or
    # permission error cannot abort a real dispatch.
    for prompt_path in base.glob("*.assembled.prompt"):
        try:
            prompt_path.chmod(0o600)
        except OSError as exc:
            print(
                f"goalflight dispatch: legacy prompt repair failed for {prompt_path}: {exc}",
                file=sys.stderr,
            )
    return base


def _persist_acp_watcher_prompt(
    *,
    status_json: Path,
    prompt_text: str,
) -> Path:
    """Persist the exact ACP prompt text used for prompt-echo suppression."""

    # Lazy hardening: the dir-privacy sweep runs only when something is
    # actually written into the dispatch dir — never as a main() side effect,
    # so a refused dispatch leaves zero state (the prompt-guard contract).
    _prepare_private_dispatch_dir()
    prompt_path = _acp_watcher_prompt_path(status_json)
    _atomic_write_private_text(prompt_path, prompt_text)
    return prompt_path


def _build_acp_cfg(args, *, status_json: Path, base: Path | None = None):
    from goalflight_acp_run import (
        DEFAULT_MAX_TOOL_S,
        DEFAULT_REMOTE_TURN_CANCEL_GRACE_S,
        OS_SANDBOX_OFF,
        normalized_acp_dispatch_cfg,
    )

    project_root = _project_root(args)
    prompt_path = _resolve_prompt_file(args, base or _dispatch_base_dir())
    orientation_path = _project_orientation_path(
        project_root,
        disabled=bool(getattr(args, "no_orientation", False)),
    )
    acp_prompt_path = prompt_path
    acp_prompt_text = None if prompt_path else args.prompt
    if orientation_path is not None and prompt_path:
        body = Path(prompt_path).read_text(encoding="utf-8", errors="replace")
        acp_prompt_path = None
        acp_prompt_text = f"{_project_orientation_preamble(orientation_path)}\n\n{body}"
    delivered_body = (
        acp_prompt_text
        if acp_prompt_text is not None
        else Path(prompt_path).read_text(encoding="utf-8", errors="replace")
    )
    delivered_prompt = (
        f"{PROMPT_FILE_PREAMBLE}\n\n{delivered_body}"
        if prompt_path
        else delivered_body
    )
    watcher_prompt_path = _persist_acp_watcher_prompt(
        status_json=status_json,
        prompt_text=delivered_prompt,
    )
    explicit_os_sandbox = getattr(args, "os_sandbox", None)
    if explicit_os_sandbox:
        os_sandbox = explicit_os_sandbox
    else:
        os_sandbox = (
            "read-only"
            if args.read_only
            and (goalflight_compat.is_macos() or args.agent == "claude")
            else OS_SANDBOX_OFF
        )
    liveness_profile = "remote_api" if args.agent in {"cursor", "claude"} else None
    cfg = argparse.Namespace(
        agent=args.agent,
        model=getattr(args, "model", None),
        install_slot=None,
        account=getattr(args, "account", None),
        cwd=str(
            _project_root(args)
            if _requested_worktree_base(args)
            else _worker_cwd(args)
        ),
        worktree="create" if _requested_worktree_base(args) else "off",
        worktree_base=_requested_worktree_base(args) or "HEAD",
        session_id=_resolved_engine_session_id(args),
        resume_session_id=(
            _resolved_engine_session_id(args)
            if getattr(args, "parent_dispatch_id", None)
            else None
        ),
        parent_dispatch_id=getattr(args, "parent_dispatch_id", None),
        dispatch_id=args.dispatch_id,
        task_ids=list(getattr(args, "task_ids", []) or []),
        priority=getattr(args, "priority", "normal"),
        # ACP and bash use the same inline-wait policy. Detached launchers
        # default to zero because the durable drainer owns retries; explicit
        # CLI and environment budgets remain opt-in inline waits.
        capacity_wait_s=_capacity_wait_seconds(args),
        preserve_capacity_refusal_attempt=_capacity_refusal_attempt_stays_prepared(args),
        prompt_id=None,
        prompt=acp_prompt_path,
        prompt_text=acp_prompt_text,
        prompt_b64=None,
        original_prompt_file=prompt_path,
        watcher_prompt_file=str(watcher_prompt_path),
        mode="one-shot",
        idle_timeout=float(args.max_idle_secs or 300.0),
        status_json=str(status_json),
        context_mode=_acp_context_mode_default(args),
        os_sandbox=os_sandbox,
        permission_mode=getattr(args, "permission_mode", "auto"),
        permission_dir=getattr(args, "permission_dir", None),
        permission_inline_timeout_s=getattr(args, "permission_inline_timeout_s", None),
        permission_user_timeout_s=getattr(args, "permission_user_timeout_s", None),
        read_only=_effective_read_only(args),
        interactive=bool(getattr(args, "interactive", False)),
        permission_allow_tool_title_pattern=list(
            getattr(args, "permission_allow_tool_title_pattern", None) or []
        ),
        heartbeat_interval=max(float(args.poll_secs or 0.0), 0.1),
        wedge_samples=4,
        max_tool_s=DEFAULT_MAX_TOOL_S,
        # Ordinary idle and indeterminate liveness answer different questions.
        # max_idle_secs bounds normal event silence; positive/unknown probes get
        # the shared generous floor before ACP may give up as indeterminate.
        max_quiet_s=resolve_indeterminate_timeout_s(
            max(float(args.max_idle_secs or 300.0), 1.0)
        ),
        progress_stall_s=300.0,
        liveness_profile=liveness_profile,
        # Remote-turn silence is another unverifiable-liveness bound. Keep it
        # distinct from the ordinary idle window and aligned with max_quiet_s.
        remote_turn_silence_s=resolve_indeterminate_timeout_s(
            max(float(args.max_idle_secs or 300.0), 1.0)
        ),
        remote_turn_cancel_grace_s=DEFAULT_REMOTE_TURN_CANCEL_GRACE_S,
        steer_file=str(_steer_file(args.dispatch_id)),
        queue_launch_token=getattr(args, "queue_launch_token", None),
        request_envelope=_queue_request_envelope(args),
        controller_session_id=_controller_session_id(args),
        controller_pid=_controller_pid(args),
        controller_label=_controller_label(args),
        unregistered_forced=bool(getattr(args, "unregistered_forced", False)),
        _controller_registration_script="goalflight_acp_run.py",
        cpu_epsilon=0.1,
        json=False,
    )
    return normalized_acp_dispatch_cfg(cfg)


def _default_max_idle_secs(args) -> float:
    if not _effective_read_only(args) and str(getattr(args, "agent", "")) in CODE_WRITER_AGENTS:
        return CODE_WRITER_MAX_IDLE_SECS
    return DEFAULT_MAX_IDLE_SECS


def _apply_max_idle_default(args) -> None:
    if getattr(args, "max_idle_secs", None) is None:
        args.max_idle_secs = _default_max_idle_secs(args)


def _run_test_acp_shape_if_requested(args, *, base: Path, status_json: Path, tail_path: Path) -> int | None:
    marker_path = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_TEST_ACP_DISPATCH_COMPLETE_FILE",
        "",
        test_mode=True,
    )
    if not marker_path:
        return None
    project_root = _project_root(args)
    worker_pid = os.getpid()
    tail_path.parent.mkdir(parents=True, exist_ok=True)
    with tail_path.open("ab") as tail_f:
        tail_f.write(b"COMPLETE: test acp dispatch\n")
    prompt_path = str(Path(args.prompt_file).expanduser()) if args.prompt_file else None
    for lifecycle_state in ("waiting_capacity", "starting"):
        _record_ledger(
            args,
            project_root=project_root,
            prompt_path=prompt_path,
            status_json=status_json,
            tail=tail_path,
            lease_id=None,
            worker_pid=None,
            state=lifecycle_state,
        )
    authority = goalflight_journal.Journal(project_root)
    attempt = authority.attempt_for_dispatch(str(args.dispatch_id))
    if attempt is None:
        raise RuntimeError(f"prepared test attempt missing for {args.dispatch_id}")
    running = authority.mark_attempt_running(
        attempt.attempt_id,
        attempt.launch_token,
        launch_epoch=attempt.launch_epoch,
        worker_instance=goalflight_ledger.process_identity(worker_pid) or {"pid": worker_pid},
    )
    if not running.committed:
        raise RuntimeError(f"test attempt RUNNING CAS lost: {running.reason}")
    _record_ledger(
        args,
        project_root=project_root,
        prompt_path=prompt_path,
        status_json=status_json,
        tail=tail_path,
        lease_id=None,
        worker_pid=worker_pid,
        state="running",
    )
    sleep_after_running_s = 0.0
    with contextlib.suppress(ValueError):
        sleep_after_running_s = float(
            goalflight_compat.allowed_env_override(
                "GOALFLIGHT_TEST_ACP_DISPATCH_SLEEP_AFTER_RUNNING_S",
                "0",
                test_mode=True,
            )
            or 0.0
        )
    release_path = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_TEST_ACP_DISPATCH_RELEASE_FILE",
        "",
        test_mode=True,
    )
    if sleep_after_running_s > 0 or release_path:
        write_status(
            status_json,
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": args.dispatch_id,
                "agent": args.agent,
                "shape": "acp",
                "state": "running",
                "terminal_state": None,
                "worker_pid": worker_pid,
                "worker_alive": True,
                "tail_path": str(tail_path),
                "status_path": str(status_json),
                "updated_at": int(time.time()),
            },
        )
        _export_dashboard_status_for_project(project_root)
        _start_dashboard_refresh_for_project(project_root)
        if release_path:
            os.environ["GOALFLIGHT_TEST_ACP_DISPATCH_READY_FILE"] = marker_path
            _test_wait_for_release(
                ready_env="GOALFLIGHT_TEST_ACP_DISPATCH_READY_FILE",
                release_env="GOALFLIGHT_TEST_ACP_DISPATCH_RELEASE_FILE",
                label="ACP dispatch",
                ready_value=str(worker_pid),
            )
        else:
            with contextlib.suppress(Exception):
                Path(marker_path).write_text(str(worker_pid), encoding="utf-8")
            time.sleep(sleep_after_running_s)
    _finish_ledger(
        str(args.dispatch_id),
        "complete",
        None,
        elapsed_s=0.0,
        worker_still_alive=False,
    )
    write_status(
        status_json,
        {
            "schema": "goalflight.status.v1",
            "dispatch_id": args.dispatch_id,
            "agent": args.agent,
            "shape": "acp",
            "state": "complete",
            "terminal_state": "complete",
            "worker_pid": worker_pid,
            "worker_alive": False,
            "tail_path": str(tail_path),
            "status_path": str(status_json),
            "updated_at": int(time.time()),
        },
    )
    _export_dashboard_status_for_project(project_root)
    with contextlib.suppress(Exception):
        Path(marker_path).write_text(str(worker_pid), encoding="utf-8")
    print(
        "DISPATCH-END "
        + json.dumps(
            {
                "dispatch_id": args.dispatch_id,
                "agent": args.agent,
                "shape": "acp",
                "worker_pid": worker_pid,
                "status_json": str(status_json),
                "terminal_state": "complete",
                "worker_still_alive": False,
                "elapsed_s": 0.0,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def _acp_detached_child_argv(args) -> list[str]:
    argv = list(getattr(args, "_original_argv", []) or sys.argv[1:])
    argv = _remove_flag_before_worker_remainder(argv, "--launch-detached")
    for flag, value in (
        ("--controller-label", _controller_label(args)),
        ("--controller-beacon-pid", _controller_pid(args)),
        ("--controller-session-id", _controller_session_id(args)),
    ):
        if value is not None:
            argv = _set_option_before_worker_remainder(argv, flag, str(value))
    if (
        getattr(args, "unregistered_forced", False)
        and "--unregistered-forced" not in argv
    ):
        argv = _insert_before_worker_remainder(argv, ["--unregistered-forced"])
    if (
        getattr(args, "occupied_worktree_forced", False)
        and "--occupied-worktree-forced" not in argv
    ):
        argv = _insert_before_worker_remainder(argv, ["--occupied-worktree-forced"])
    if "--acp-detached-child" not in argv:
        argv = _insert_before_worker_remainder(argv, ["--acp-detached-child"])
    return argv


@contextlib.contextmanager
def _forward_detached_launcher_signals(child_pid: int):
    """Forward operator interrupts to the daemonized ACP dispatch child."""
    received: list[int] = []
    previous: dict[int, object] = {}

    def forward(signum, _frame) -> None:
        received.append(int(signum))
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(child_pid, signum)

    for signum in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if not isinstance(signum, int):
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, forward)
        except (OSError, ValueError):
            previous.pop(signum, None)
    try:
        yield received
    finally:
        for signum, handler in previous.items():
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signum, handler)


def _run_acp_detached_launcher(
    args,
    *,
    status_json: Path,
    tail_path: Path,
    account_env: dict[str, str],
    env_remove: list[str],
    capacity_wait_s: float,
) -> int:
    _mark_queue_claim_worker_spawn_intent(args)
    env = os.environ.copy()
    env.update(account_env)
    for key in env_remove:
        env.pop(key, None)
    _apply_web_qa_env(env, args, _project_root(args))
    child_argv = [sys.executable, str(Path(__file__).resolve()), *_acp_detached_child_argv(args)]
    child_pid = _spawn_daemonized_process(
        child_argv,
        env=env,
        stdout_path=tail_path,
        stdout_mode="ab",
        stderr="stdout",
        serialize_stdout=True,
        label="acp",
        inherit_occupancy_lock=True,
    )
    _mark_queue_claim_worker_spawned(args, child_pid)

    def report_queued() -> None:
        print(
            "DISPATCH-QUEUED "
            + json.dumps(
                {
                    "dispatch_id": args.dispatch_id,
                    "agent": args.agent,
                    "shape": "acp",
                    "queue_path": str(_queue_entry_path(str(args.dispatch_id))),
                    "status_json": str(status_json),
                },
                sort_keys=True,
            ),
            flush=True,
        )

    wait_s = max(20.0, float(capacity_wait_s) + 20.0)
    deadline = time.time() + wait_s
    last_state = None
    with _forward_detached_launcher_signals(child_pid) as forwarded_signals:
        while time.time() < deadline:
            record = _find_dispatch_record(args.dispatch_id)
            if (
                not forwarded_signals
                and record
                and record.get("worker_pid")
                and record.get("queue_launch_token")
                == getattr(args, "queue_launch_token", None)
            ):
                if getattr(args, "background_default_notice", False):
                    _print_background_default_notice()
                print(
                    "DISPATCH-LAUNCHED "
                    + json.dumps(
                        {
                            "dispatch_id": args.dispatch_id,
                            "agent": args.agent,
                            "shape": "acp",
                            "worker_pid": record.get("worker_pid"),
                            "launcher_pid": child_pid,
                            "status_json": str(status_json),
                            "state": "running",
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return 0
            with contextlib.suppress(OSError, json.JSONDecodeError):
                status_payload = json.loads(status_json.read_text(encoding="utf-8"))
                last_state = status_payload.get("state")
                if (
                    not forwarded_signals
                    and last_state == "queued"
                    and not getattr(args, "from_queue", False)
                ):
                    if getattr(args, "background_default_notice", False):
                        _print_background_default_notice()
                    report_queued()
                    return 0
                if str(last_state).startswith("blocked_capacity"):
                    # A direct detached child may still be converting this
                    # refusal into a durable queue entry. Only report terminal
                    # refusal once that child exits.
                    if goalflight_compat.pid_alive(child_pid):
                        time.sleep(0.2)
                        continue
                    if (
                        not forwarded_signals
                        and _queue_entry_path(str(args.dispatch_id)).exists()
                    ):
                        report_queued()
                        return 0
                    print(
                        "DISPATCH-BLOCKED "
                        + json.dumps(
                            {
                                "dispatch_id": args.dispatch_id,
                                "agent": args.agent,
                                "shape": "acp",
                                "state": last_state,
                                "status_json": str(status_json),
                                "reason": status_payload.get("reason"),
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    return (
                        128 + forwarded_signals[-1]
                        if forwarded_signals
                        else 2
                    )
            if not goalflight_compat.pid_alive(child_pid):
                break
            time.sleep(0.2)
    if forwarded_signals:
        return 128 + forwarded_signals[-1]
    print(
        "goalflight_dispatch: ACP detached launch did not publish a running ledger "
        f"for {args.dispatch_id} (last_state={last_state!r})",
        file=sys.stderr,
    )
    return 1


def _run_acp_shape(args, *, base: Path, account_env: dict[str, str]) -> int:
    from goalflight_acp_run import (
        _commit_prelaunch_terminal,
        acp_dispatch_exit_code,
        run_acp_dispatch,
    )

    if not args.dispatch_id:
        args.dispatch_id = (
            _default_dispatch_id(args.agent)
            if goalflight_compat.is_windows()
            else _reserve_auto_dispatch_id(args.agent, base)
        )
    _refuse_reused_dispatch_id_for_launch(
        args.dispatch_id,
        allow_queued=getattr(args, "from_queue", False),
    )
    status_json = Path(args.status_json) if args.status_json else base / f"{args.dispatch_id}.status.json"
    cfg = _build_acp_cfg(args, status_json=status_json, base=base)
    env_remove = []
    if args.billing == "sub":
        engine = _account_engine(args.agent)
        if engine == "codex":
            env_remove.append("OPENAI_API_KEY")
        elif engine == "cursor":
            env_remove.append("CURSOR_API_KEY")
        elif args.agent == "claude":
            env_remove.append("ANTHROPIC_API_KEY")

    tail_path = Path(args.tail) if args.tail else base / f"{args.dispatch_id}.tail"
    _emit_dispatch_warnings(
        getattr(args, "dispatch_warnings", []),
        tail_path=tail_path,
        reset_tail=True,
    )
    if getattr(args, "launch_detached", False) and not getattr(args, "acp_detached_child", False):
        return _run_acp_detached_launcher(
            args,
            status_json=status_json,
            tail_path=tail_path,
            account_env=account_env,
            env_remove=env_remove,
            capacity_wait_s=float(cfg.capacity_wait_s or 0.0),
        )
    test_rc = _run_test_acp_shape_if_requested(args, base=base, status_json=status_json, tail_path=tail_path)
    if test_rc is not None:
        return test_rc
    if goalflight_compat.is_windows():
        payload = asyncio.run(run_acp_dispatch(cfg))
        print(
            "DISPATCH-BLOCKED "
            + json.dumps(
                {
                    "dispatch_id": payload.get("dispatch_id", cfg.dispatch_id),
                    "agent": payload.get("agent", cfg.agent),
                    "shape": "acp",
                    "state": payload.get("state"),
                    "status_json": str(status_json),
                    "reason": payload.get("error") or payload.get("reason"),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return acp_dispatch_exit_code(payload)

    started = time.time()
    print(
        "DISPATCH-START "
        + json.dumps(
            {
                "dispatch_id": cfg.dispatch_id,
                "agent": cfg.agent,
                "shape": "acp",
                "worker_pid": None,
                "status_json": str(status_json),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    web_qa_updates, web_qa_remove = _web_qa_env_plan(args, _project_root(args))
    acp_env = {**account_env, **web_qa_updates}
    acp_remove = list(env_remove) + list(web_qa_remove)
    with _temporary_env(acp_env, remove=acp_remove):
        payload = asyncio.run(run_acp_dispatch(cfg))
    if _detached_capacity_queue_eligible(args) and _is_capacity_refusal(payload):
        if _queue_detached_capacity_refusal(args, payload, base=base):
            return 0
        # The ACP runner left this attempt PREPARED solely for the queue
        # handoff. If that durable write fails, close the attempt instead of
        # leaving an invisible prepared launch with no carrier.
        _commit_prelaunch_terminal(payload, project_root=_project_root(args))
        write_status(status_json, payload)
    worker_pid = payload.get("worker_pid")
    if worker_pid:
        _print_status_reminder(
            cfg.dispatch_id,
            status_json=status_json,
            tail_path=tail_path,
            worker_pid=int(worker_pid),
            shape="acp",
            prompt_path=getattr(cfg, "watcher_prompt_file", None),
            hints=bool(getattr(args, "hints", False)),
        )
    end_payload = {
        "dispatch_id": payload.get("dispatch_id", cfg.dispatch_id),
        "agent": payload.get("agent", cfg.agent),
        "shape": "acp",
        "worker_pid": payload.get("worker_pid"),
        "status_json": str(status_json),
        "terminal_state": payload.get("state"),
        "worker_still_alive": payload.get("worker_alive"),
        "reason": payload.get("error") or payload.get("reason"),
        "elapsed_s": round(time.time() - started, 3),
    }
    hint = _dispatch_end_reattach_hint(
        str(end_payload["dispatch_id"]),
        terminal_state=end_payload["terminal_state"],
        worker_alive=end_payload["worker_still_alive"],
    )
    if hint:
        end_payload["hint"] = hint
    print("DISPATCH-END " + json.dumps(end_payload, sort_keys=True), flush=True)
    return acp_dispatch_exit_code(payload)


def _registration_error(step: str, exc: Exception) -> dict:
    return {"step": step, "reason": f"{type(exc).__name__}: {exc}"}


def _codex_context_mode_enabled() -> bool:
    """Whether a dispatched codex worker should load the context-mode MCP server.

    Default OFF: context-mode's elicitation (ctx_index 'Approve Index Content')
    issues request_user_input, which codex `exec` does not support -> the worker
    wedges. Headless workers don't need it. Opt back in (rarely wanted) with
    GOALFLIGHT_CODEX_CONTEXT_MODE in {1,true,yes,enabled,on}.
    """
    raw = os.environ.get("GOALFLIGHT_CODEX_CONTEXT_MODE", "").strip().lower()
    return raw in {"1", "true", "yes", "enabled", "on"}


def _cursor_acp_enabled() -> bool:
    """Whether cursor may use the acp shape. Default OFF, blocked pending a fix.

    Cursor's auto-resolved shape used to be acp. bash-tail is measured good for
    it — five of five trials correct by parsed value, exit 0, 49-50s each, both
    artifacts written and a clean terminal marker every run — so bash is now the
    default and acp is refused rather than silently chosen.

    Blocking beats defaulting away: a shape that stays reachable by an explicit
    flag will be reached, and the refusal says what to use instead. Re-enable
    with GOALFLIGHT_CURSOR_ACP once acp is repaired.
    """
    raw = os.environ.get("GOALFLIGHT_CURSOR_ACP", "").strip().lower()
    return raw in {"1", "true", "yes", "enabled", "on"}


def _cursor_context_mode_enabled() -> bool:
    """Whether a dispatched cursor worker should load the context-mode MCP server.

    Default OFF, for the same reason as codex but a different mechanism. Cursor
    routes tool calls through an automatic reviewer that refuses the ctx tools;
    the worker reports "The MCP batch tool was refused", retries, and burns its
    whole event budget -- observed as runaway_event_cap with nothing written.
    Headless workers do not need it. Opt back in with
    GOALFLIGHT_CURSOR_CONTEXT_MODE in {1,true,yes,enabled,on}.
    """
    raw = os.environ.get("GOALFLIGHT_CURSOR_CONTEXT_MODE", "").strip().lower()
    return raw in {"1", "true", "yes", "enabled", "on"}


def _acp_context_mode_default(args) -> str:
    """Per-worker context-mode posture for acp engines.

    codex-acp: OFF -- exec-mode cannot answer context-mode's elicitation, so the
    worker wedges (opt in via GOALFLIGHT_CODEX_CONTEXT_MODE).

    cursor: OFF -- its command reviewer refuses the ctx tools and the worker
    loops on the refusal to the event cap (opt in via
    GOALFLIGHT_CURSOR_CONTEXT_MODE). This was previously left ON, which is why
    cursor workers wedged where codex workers did not, and why the only remedy
    was disabling the server by hand -- per project, since cursor's own disable
    is project-scoped and does not carry to the next checkout.

    grok-acp keeps it enabled: no wedge has been observed there, and turning it
    off for an engine that tolerates it would remove capability for no reason.
    """
    agent = str(getattr(args, "agent", "")).strip().lower()
    if agent in CURSOR_AGENTS and not _cursor_context_mode_enabled():
        return "disabled"
    if agent.startswith("codex") and not _codex_context_mode_enabled():
        return "disabled"
    return "enabled"


def _validate_claude_auth_before_attempt(
    args,
    account_env: dict[str, str],
    *,
    runner=None,
) -> None:
    """Fail closed before a Claude dispatch can create an attempt.

    ``claude-code-cli-acp`` completes its ACP turn cleanly when Claude Code's
    login has expired, returning the assistant text ``Login expired · Please
    run /login``. That is a successful protocol handshake but not a usable
    worker. Probe the same effective HOME/token environment the worker will
    receive, and refuse before queue/ledger mutation unless auth is provably
    ready. Explicit API billing with an API key remains a separate supported
    path; runtime authentication errors are still vetoed by the ACP terminal
    evidence guard.
    """
    if str(getattr(args, "agent", "")).strip().lower() != "claude":
        return
    probe_env = os.environ.copy()
    probe_env.update(account_env)
    if getattr(args, "billing", "sub") == "sub":
        probe_env.pop("ANTHROPIC_API_KEY", None)
    elif probe_env.get("ANTHROPIC_API_KEY"):
        return

    run_probe = runner or subprocess.run
    try:
        completed = run_probe(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            env=probe_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DispatchUsageError(
            "Claude auth preflight unavailable; refusing before recording an attempt: "
            f"{type(exc).__name__}"
        ) from exc

    status = goalflight_fleet_billing.interpret_auth_probe(
        "anthropic-session",
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    )
    if status == "green":
        return
    if status == "red":
        raise DispatchUsageError(
            "Claude worker is not authenticated (`claude auth status --json` "
            "reports loggedIn=false); run `claude auth login` or use --agent "
            "codex until Claude is authenticated"
        )
    raise DispatchUsageError(
        "Claude auth preflight was inconclusive; refusing before recording an "
        "attempt (verify with `claude auth status --json`)"
    )


def _apply_fast_mode(args) -> None:
    """Normalize --fast after arg parsing: force the urgent lane (critical
    priority -> skip the queue) for every engine/shape. Idempotent: runs again on
    detached/queue replay; the note prints only on the user's initial invocation
    to avoid tail noise."""
    if not getattr(args, "fast", False):
        return
    if getattr(args, "priority", None) != "critical":
        args.priority = "critical"
    if not (getattr(args, "from_queue", False)
            or getattr(args, "launch_detached", False)
            or getattr(args, "acp_detached_child", False)):
        print("FAST: urgent — forcing --priority critical to skip the queue", file=sys.stderr)


def build_worker(args, prompt_path, raw_argv: list[str]):
    """Return (argv, stdin_path). Explicit `-- <cmd>` overrides any preset.
    Presets encode the canonical SAFE, non-interactive invocation per worker.
    `prompt_path` is the already-materialized prompt file (or None for raw)."""
    if raw_argv:
        return raw_argv, None  # raw escape hatch; stdin = DEVNULL
    # codex's own --sandbox value for the effective OS sandbox profile.
    # "off" -> danger-full-access (Seatbelt disabled) for trusted local GPU/perf.
    sandbox = _CODEX_SANDBOX_VALUE[_effective_os_sandbox(args)]
    model = getattr(args, "model", None)
    if args.agent == "codex":
        argv = ["codex", "exec", "--skip-git-repo-check", "--sandbox", sandbox,
                "-c", "approval_policy=never"]
        argv += codex_workspace_write_args(args.cwd, _effective_os_sandbox(args))
        if not _codex_context_mode_enabled():
            # Disable context-mode at the worker boundary (see _codex_context_mode_enabled);
            # leaves the user's ~/.codex/config.toml untouched for interactive use.
            argv += ["-c", "mcp_servers.context-mode.enabled=false"]
        if model:
            argv += ["--model", str(model)]
        if args.cwd:
            argv += ["-C", args.cwd]
        codex_session_id = goalflight_codex_sessions.valid_session_id(
            getattr(args, "codex_session_id", None)
        )
        if getattr(args, "parent_dispatch_id", None):
            if codex_session_id is None:
                raise DispatchUsageError(
                    "codex resume launch requires a recorded session handle"
                )
            if not prompt_path:
                raise DispatchUsageError("codex resume launch requires a prompt file")
            # `-` makes codex read the prompt from stdin, same as the non-resume
            # path below. Passing the text in argv instead would re-introduce the
            # E2BIG/truncation failure this file already records for grok:
            # revision prompts carry gate failures plus review findings and are
            # exactly the long kind. Returning prompt_path also keeps stdin fed
            # from a real file rather than DEVNULL, so codex never waits on EOF.
            argv += ["resume", codex_session_id, "-"]
            return argv, prompt_path
        argv += ["-"]  # codex reads the prompt from stdin
        return argv, prompt_path
    if args.agent in ("grok-code", "grok-research"):
        # Read the prompt from a FILE, not argv `-p` — long goal-flight prompts
        # (5-20KB) would hit E2BIG / argv truncation (grok review #5).
        # Model PER TASK — inject NO --model for either preset: grok's own CLI
        # default applies (grok-4.5 as of 2026-07-08, per `grok models` "Default
        # model"), so the flagship is used without pinning a version that goes
        # stale when grok ships the next default. This retires the old explicit
        # pins — grok-build for research (now unlisted by `grok models`) and the
        # coding-specialised grok-composer-2.5-fast for coding; either remains
        # reachable via an explicit --model. Web tools stay at grok's CLI default
        # (ON) for both presets; the coding-vs-research split is the agent label
        # + research-intent guard, not a --disable-web-search difference.
        # Trade-off: the dispatch ledger no longer records which grok model ran
        # (it's grok's default), in exchange for auto-tracking + no stale pin.
        default_model = None
        # PERMISSION MODE — pass NO `--permission-mode` flag (do not "fix" by
        # swapping the value). Omitting is the only setting verified to work on
        # BOTH grok versions measured so far, which is why it stays.
        #
        # grok 0.2.39 (verified 2026-06-10): in single-turn `--prompt-file` mode
        # EVERY value stopped the file-write tool — none produced the file; only
        # OMITTING did (probe: grok-composer-2.5-fast, write-a-file prompt):
        #   omit-flag    -> file written + DONE marker, rc=0   (the only one that worked)
        #   default      -> 1-byte no-op, no file
        #   acceptEdits  -> 1-byte no-op, no file   (this is the value we shipped)
        #   auto         -> 0-byte no-op, no file
        #   dontAsk      -> prints a normal completion marker but STILL skips the write
        # The empty no-ops (default/acceptEdits/auto) make the watcher record
        # worker_dead_no_terminal_marker — how the shipped acceptEdits killed 4
        # grok-research dispatches (~18-25s, empty tails) on 2026-06-10; dontAsk is
        # worse, faking a clean finish with no artifact.
        #
        # grok 1.0.0 (re-measured 2026-08-07, same probe shape): the flag is still
        # not safe, but the failure set MOVED — proof that pinning a value would
        # have silently rotted:
        #   omit-flag    -> file written + DONE marker, rc=0   (still correct)
        #   auto         -> NOW WRITES (it did not on 0.2.39)
        #   acceptEdits  -> no file, and the tail narrates "Creating `artifact.txt`
        #                   with the requested contents." — prose describing a write
        #                   it never performs, the worst failure mode of the set
        #   default      -> no file, empty tail
        #   dontAsk      -> no file, empty tail
        # So `auto` became viable while `acceptEdits` stayed broken and grew a
        # misleading narration. Omitting is unchanged across both versions.
        #
        # All values are listed in `grok --help` in both releases, so this is a CLI
        # behaviour regression, not a parse error. bypassPermissions is too broad,
        # and a per-dispatch healthcheck would tax every dispatch's critical path —
        # so the fix is the deterministic omit, locked by a regression test
        # (tests/python/test_acp_model_passthrough.py). Same lesson as the stale
        # model note above: grok flags drift; re-validate before trusting one.
        argv = ["grok", "--prompt-file", str(prompt_path)]
        if _effective_read_only(args):
            # Grok has no OS sandbox here (--os-sandbox is a codex-bash knob,
            # and is now REFUSED at dispatch rather than accepted and ignored)
            # and its ACP adapter bypasses the permission gate for writes, so
            # until 2026-08-25 a "read-only" grok review was enforced by nothing
            # but brief discipline. --deny rules are the mechanism the CLI
            # actually honours, and they hold against the model's own bypass
            # attempts: probed on grok 1.0.0 and re-validated on 1.0.5 (write
            # prompt, seat `info`) the worker tried the write tool, then a shell
            # command, then a subagent, and reported honestly that writes were
            # blocked. The broken --permission-mode flag documented above stays
            # omitted — deny rules are a different, measured surface. If a new
            # write-capable tool ships, the deny list must grow with it; the
            # probe in tests/python/test_grok_read_only_deny.py is the tripwire.
            argv += ["--deny", "Write", "--deny", "Edit", "--deny", "Bash"]
        # Only pin a model when one is EXPLICITLY requested; otherwise omit the
        # flag entirely and let grok's CLI default (grok-4.5) apply.
        selected_model = str(model) if model else default_model
        if selected_model:
            argv += ["--model", selected_model]
        if args.cwd:
            argv += ["--cwd", args.cwd]
        grok_session_id = _resolved_engine_session_id(args)
        if grok_session_id:
            # RESUME_FORK_POLICY = reuse. Never --fork-session (t-288 steer 1).
            argv += goalflight_engine_sessions.session_argv(
                "grok",
                grok_session_id,
                resume=bool(getattr(args, "parent_dispatch_id", None)),
            )
        return argv, None
    if args.agent == "moonshot":
        # kimi has no --cwd, is off-PATH, takes the prompt as an argv value, and -p auto-runs
        # tools (no --auto/-y — those are rejected with -p). Resolve the binary and cd in a
        # login shell, exec kimi so the pid IS kimi (bash-tail pgid handling unaffected).
        prompt_text = Path(prompt_path).read_text()
        script = ('bin="$(command -v kimi || printf %s "$HOME/.kimi-code/bin/kimi")"; '
                  'test -x "$bin" || { echo "kimi binary not found/executable: $bin" >&2; exit 127; }; '
                  'prompt=$1; shift; cd "$0" && exec "$bin" -p "$prompt" --output-format text "$@"')
        resolved_cwd = os.path.abspath(args.cwd or ".")
        extra = (["--model", args.model] if getattr(args, "model", None) else []) \
                + ["--add-dir", resolved_cwd]
        kimi_session_id = _resolved_engine_session_id(args)
        if kimi_session_id and getattr(args, "parent_dispatch_id", None):
            extra += goalflight_engine_sessions.session_argv(
                "moonshot", kimi_session_id, resume=True
            )
        argv = ["/bin/sh", "-lc", script, resolved_cwd, prompt_text, *extra]
        return argv, None
    if args.agent in CURSOR_AGENTS:
        if not prompt_path:
            raise DispatchUsageError("cursor resume/launch requires a prompt file")
        argv = [
            "cursor-agent",
            "-p",
            "--force",
            "--trust",
            "--output-format",
            "text",
        ]
        cursor_session_id = _resolved_engine_session_id(args)
        if cursor_session_id and getattr(args, "parent_dispatch_id", None):
            argv += goalflight_engine_sessions.session_argv(
                "cursor", cursor_session_id, resume=True
            )
        if args.cwd:
            argv += ["--workspace", os.path.abspath(args.cwd)]
        if model:
            argv += ["--model", str(model)]
        # Prompt via stdin: the file can be 5-20KB and argv would truncate.
        return argv, prompt_path
    if args.agent == "claude":
        if not prompt_path:
            raise DispatchUsageError("claude resume/launch requires a prompt file")
        argv = ["claude", "-p", "--output-format", "text"]
        claude_session_id = _resolved_engine_session_id(args)
        if claude_session_id:
            argv += goalflight_engine_sessions.session_argv(
                "claude",
                claude_session_id,
                resume=bool(getattr(args, "parent_dispatch_id", None)),
            )
        if model:
            argv += ["--model", str(model)]
        # claude -p has no --cwd flag. The daemon helper Popen cwd is the
        # leased seat (or --cwd) so the process inherits it; raw `--` uses
        # the same path.
        return argv, prompt_path
    return None, None  # unknown preset + no raw command


# Subcommands are matched positionally BEFORE argparse is constructed, so
# argparse never learns they exist and they get no --help entry. That is not a
# cosmetic gap: `resume` was invisible to `--help`, and controllers looking for
# a resume capability the usual way concluded there was none and redispatched
# instead, discarding accumulated worker context. Listed here so the top-level
# help can name them; `test_dispatch_subcommands_are_discoverable` pins this
# list against the dispatch table so a new subcommand cannot be added silently.
_SUBCOMMAND_HELP: tuple[tuple[str, str], ...] = (
    ("steer", "append, list, or wait on a live worker's mailbox"),
    ("resume", "continue a recorded worker session as a tracked dispatch"),
    ("reconcile-abandoned", "dry-run report of abandoned dispatches; drain writes"),
    ("drain", "launch queued dispatch requests"),
    ("dashboard-refresh", "rebuild the dashboard projection"),
)


def _existing_cwd_arg(value: str) -> str:
    """--cwd must name a real directory at argument-parse time (t-349).

    A nonexistent --cwd used to flow into resolve_project_root, whose
    not-a-checkout fallback renders the path as its own project root. The
    dispatch then landed on an empty controller registry and refused with
    "controller is not registered", recommending --unregistered-forced -- an
    operator following that advice launched an unowned dispatch into a phantom
    project root for what was actually a path typo (t337-w3/w4/w5). Refusing
    here fires before any registry lookup, so that advice is never reached.
    """
    expanded = Path(str(value)).expanduser()
    if not expanded.exists():
        raise argparse.ArgumentTypeError(f"cwd does not exist: {value}")
    if not expanded.is_dir():
        raise argparse.ArgumentTypeError(f"cwd is not a directory: {value}")
    return value


def _build_launch_parser() -> argparse.ArgumentParser:
    parser = _TerseArgumentParser(
        description=(
            "Crash-safe worker dispatch: detached worker + decoupled watcher.\n"
            "\n"
            "Subcommands (these are positional, NOT flags -- they are matched\n"
            "before this parser is built, so they have no --help entry of their\n"
            "own here; run `<subcommand> --help` for each):\n"
            + "\n".join(f"  {name:<20} {purpose}"
                        for name, purpose in _SUBCOMMAND_HELP)
        ),
        # Default argparse re-wraps the description into one paragraph, which
        # turns an aligned subcommand table into an unreadable run-on. The
        # point of listing them is that a reader can SEE them.
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage_hint=_DISPATCH_USAGE_HINT,
    )
    parser.add_argument("--agent", default="worker",
                        help="Preset (codex|grok-code|grok-research|moonshot) OR a label when you pass `-- <cmd>`")
    parser.add_argument("--prompt-file", help="Prompt file (preset path)")
    parser.add_argument("--prompt", help="Inline prompt text (preset path; alternative to --prompt-file)")
    parser.add_argument(
        "--task",
        dest="tasks",
        action="append",
        default=[],
        help="Comma-separated linked task/bug ids (t-/b-). May be repeated.",
    )
    parser.add_argument("--cwd", type=_existing_cwd_arg, help="Worker working directory")
    parser.add_argument(
        "--worktree",
        metavar="BASE",
        help=(
            "Acquire one pooled worktree seat prepared at git ref BASE "
            "(HEAD, main, a commit) and run the worker there. The seat lease "
            "fd is inherited by the worker; exhaustion refuses with every held "
            "seat named and never falls back to `git worktree add`. Additive: "
            "omit this flag and --cwd behaves as before."
        ),
    )
    parser.add_argument("--model", default=None,
                        help="Worker model id (grok-code/grok-research/moonshot/codex --model passthrough). "
                             "Default = agent label's own default.")
    parser.add_argument("--read-only", "--readonly", action="store_true",
                        help="Read-only sandbox (review/analysis dispatches). Equivalent to "
                             "--os-sandbox read-only.")
    parser.add_argument("--os-sandbox", type=_parse_os_sandbox_arg, default=None,
                        metavar="{workspace-write,read-only,off}",
                        help="OS sandbox profile. Honoured by bash-shape codex (maps to "
                             "codex --sandbox) and by ACP shapes whose adapter and platform "
                             "can wrap the worker (macOS sandbox-exec). Unset = workspace-write "
                             "for bash-shape codex. An accepted-but-inert request is refused; "
                             "grok bash should pass --read-only instead. 'off' disables codex's "
                             "Seatbelt sandbox (codex --sandbox danger-full-access) for TRUSTED "
                             "LOCAL GPU/perf work. Sanctioned adapter profile, DISTINCT from the "
                             "always-forbidden --dangerously-*/--no-sandbox bypass flags.")
    parser.add_argument("--priority", choices=["critical", "normal", "bulk"], default="normal",
                        help="Capacity lane. bulk = review storms / batch work (reserves the last "
                             "machine+pool slots for others); critical = fix dispatches (may borrow "
                             "beyond the operating cap, never past the RAM ceiling). Default normal.")
    parser.add_argument("--fast", action="store_true",
                        help="Urgent: forces --priority critical (skip the queue) for a SINGLE urgent "
                             "dispatch. NOTE: critical may borrow beyond the operating cap (never past "
                             "the RAM ceiling) — do NOT use across a wide fan-out, or every worker "
                             "borrows past the cap and starves normal/bulk work.")
    parser.add_argument("--web-research-ok", action="store_true",
                        help="Override the grok-code research-intent guard: confirm this prompt is "
                             "a coding task that merely mentions the web (web research belongs on "
                             "--agent grok-research, whose model can actually drive web tools).")
    parser.add_argument(
        "--web-qa",
        action="store_true",
        default=False,
        help=(
            "Opt-in web-QA capability for this dispatch only (DEFAULT OFF). Provisions "
            "GOALFLIGHT_WEB_QA=1 and BROWSE_STATE_FILE so scripts/goalflight_webqa.sh can "
            "drive the gstack headless browser. Without this flag the wrapper fails closed "
            "and workers do not receive the browser state-file path — browser access is a "
            "controller-granted capability, not ambient (SECURITY-2)."
        ),
    )
    parser.add_argument("--ignore-git-warn", action="store_true",
                        help="Suppress advisory git-base-pin warnings for git-repo cwd prompts.")
    parser.add_argument("--no-orientation", action="store_true",
                        help="Do not auto-add the docs-private/rag/ORIENTATION.md pointer preamble.")
    parser.add_argument("--capacity-wait-s", type=float, default=None,
                        help="How long to poll inline for a capacity slot "
                             "(re-attempts acquire every ~15s; sleep-excluding clock). Detached "
                             "dispatches default to 0 and fall back to the durable queue; foreground "
                             "defaults by lane: bulk 900 / normal 600 / critical 120. 0 = fail instantly. "
                             "Env override: GOALFLIGHT_CAPACITY_WAIT_S.")
    dispatch_mode = parser.add_mutually_exclusive_group()
    dispatch_mode.add_argument("--submit", action="store_true",
                               help="Write a durable dispatch request to the queue and exit without acquiring capacity.")
    dispatch_mode.add_argument(
        "--foreground",
        action="store_true",
        default=False,
        help=(
            "Block until the worker reaches a terminal state and return its exit code "
            "(synchronous; for scripts/tests). Default is detached/background — the "
            "worker is launched and the dispatcher returns immediately."
        ),
    )
    drain_submit = parser.add_mutually_exclusive_group()
    drain_submit.add_argument("--drain-on-submit", dest="drain_on_submit", action="store_true",
                              help="After --submit writes the durable request, run one non-blocking "
                                   "drain pass for up to one queued entry (default).")
    drain_submit.add_argument("--no-drain-on-submit", dest="drain_on_submit", action="store_false",
                              help="With --submit, only write the durable request; wait for the scheduled drainer.")
    parser.add_argument("--account",
                        help="Which subscription account/profile to bill the worker to (shared remote "
                             "worker pools). Codex resolves to CODEX_HOME=~/.goal-flight/accounts/<name>/codex; "
                             "grok/cursor resolve to HOME=~/.goal-flight/accounts/<name>/<engine>. "
                             "Default: the host's logged-in account.")
    parser.add_argument("--billing", choices=["sub", "api"], default="sub",
                        help="ALWAYS 'sub' (subscription) in normal use — the default; 'sub' strips "
                             "OPENAI_API_KEY so codex uses the selected account's Pro plan, never the API. "
                             "'api' is a de-emphasized by-request escape hatch (bills the API) — never the "
                             "default, not used by the maintainer; present only for users who explicitly want it.")
    parser.add_argument("--shape", choices=["auto", "bash", "acp"], default="auto",
                        help="Comms shape. 'auto' picks the best per engine (codex/grok->bash, "
                             "cursor/claude->acp). v1 ACP routing supports codex-acp, "
                             "grok-acp, cursor, and claude-acp.")
    parser.add_argument("--interactive", action="store_true",
                        help="Sugar for --shape acp --permission-mode inline (codex-acp inline relay).")
    parser.add_argument("--permission-mode", choices=["auto", "inline"], default="auto",
                        help="ACP permission mode. 'auto' cancels escalations for re-dispatch; "
                             "'inline' relays permission request files to an in-run policy decision.")
    parser.add_argument("--permission-dir",
                        help="Directory for inline permission request/decision files. Default is the "
                             "runner's PID-scoped temp dir.")
    parser.add_argument("--permission-inline-timeout-s", type=float,
                        help="Inline controller-responsiveness timeout before worker fallback auto-decline.")
    parser.add_argument("--permission-user-timeout-s", type=float,
                        help="Inline post-ack user-decision timeout before worker fallback auto-decline.")
    parser.add_argument(
        "--permission-allow-tool-title-pattern",
        action="append",
        default=[],
        metavar="REGEX",
        help="Controller-discretion auto-allow shortcut (R26). Regex pattern (Python "
             "re.search semantics) matched against the request_permission tool-call "
             "title. When the title matches any provided pattern, the runner "
             "auto-approves the request before falling through to the default "
             "scope-aware policy. Repeatable: pass multiple --permission-allow-tool-"
             "title-pattern flags to add patterns. Use to pre-authorize the worker's "
             "in-scope tool uses (e.g., a chunk authorized to run "
             "'./tests/run.sh' can pass '^./tests/run\\.sh$'). Does NOT bypass the "
             "destructive-op fail-closed checks in the base policy when patterns "
             "don't match — those still escalate to USER-CONFIRM.",
    )
    parser.add_argument("--tail", help="Worker output sink (auto: <state>/dispatch/<id>.tail)")
    parser.add_argument("--status-json", help="Watcher status file (auto: <state>/dispatch/<id>.status.json)")
    parser.add_argument("--dispatch-id", help="Slug for auto paths (auto-generated if omitted)")
    parser.add_argument("--poll-secs", type=float, default=2.0)
    parser.add_argument(
        "--max-idle-secs",
        type=float,
        default=None,
        help=(
            "Watcher idle window. Default: 600s for write-capable code workers, "
            "180s for read-only/research/custom workers."
        ),
    )
    parser.add_argument(
        "--controller-pid",
        type=int,
        help=(
            "Controller pid used to select a kernel-live project lease. The "
            "matching nonce is resolved automatically when ancestry proves the "
            "holder."
        ),
    )
    parser.add_argument(
        "--controller-label",
        "--session-label",
        dest="controller_label",
        help=(
            "Controller-declared name used to select its live beacon. "
            "Default: GOALFLIGHT_CONTROLLER_LABEL."
        ),
    )
    parser.add_argument(
        "--unregistered-forced",
        action="store_true",
        help=(
            "Override the registered-controller launch refusal. The dispatch is "
            "recorded with no owner, so its terminal event wakes every controller "
            "in the project."
        ),
    )
    parser.add_argument(
        "--occupied-worktree-forced",
        action="store_true",
        help=(
            "Override the occupied-worktree launch refusal: launch even though a "
            "kernel lock or a non-terminal dispatch already owns --cwd (the two "
            "writers then share one filesystem tree with no merge discipline), "
            "or when occupancy cannot be evaluated."
        ),
    )
    parser.add_argument(
        "--takeover",
        action="store_true",
        help=(
            "deliberately supersede a live different holder of --controller-label; "
            "dead holders are replaced automatically"
        ),
    )
    parser.add_argument("--controller-beacon-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--controller-session-id",
        help="Controller lease nonce used to verify dispatch ownership.",
    )
    parser.add_argument("--from-queue", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--queue-launch-token", help=argparse.SUPPRESS)
    parser.add_argument("--queue-claim-path", help=argparse.SUPPRESS)
    parser.add_argument("--launch-detached", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--acp-detached-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent-dispatch-id", help=argparse.SUPPRESS)
    parser.add_argument("--engine-session-id", help=argparse.SUPPRESS)
    parser.add_argument("--codex-session-id", help=argparse.SUPPRESS)
    parser.add_argument("--codex-resume-home", help=argparse.SUPPRESS)
    parser.add_argument("--codex-home-owner-dispatch-id", help=argparse.SUPPRESS)
    parser.add_argument("--stats", nargs="?", const="7d", metavar="WINDOW",
                        help="No-worker stats view over dispatch history; WINDOW is <N>h, <N>d, or bare <N> days.")
    parser.add_argument("--json", action="store_true", help="With --stats, emit machine-readable JSON.")
    parser.add_argument(
        "--hints",
        action="store_true",
        help=(
            "Print the post-launch teaching block (status/wait/done/mail commands "
            "and why not to hand-roll ps/tail -f/backgrounded watchers). "
            "Default is one line: dispatch id + status path."
        ),
    )
    parser.add_argument("worker", nargs=argparse.REMAINDER,
                        help="Optional `-- <cmd...>` raw worker (overrides the preset)")
    parser.set_defaults(drain_on_submit=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == DAEMON_SPAWN_ARG:
        return _cmd_spawn_daemon()
    option_argv = argv[: argv.index("--")] if "--" in argv else argv
    # The dir-privacy sweep is invoked lazily by dispatch-dir writers (see
    # _persist_acp_watcher_prompt and the launch path), never here: a refused
    # dispatch must leave zero side effects before its guards run.
    try:
        import goalflight_messages

        goalflight_messages.emit_wake_entry_notice(
            project_root=goalflight_task.resolve_project_root(str(Path.cwd())),
            stream=sys.stderr,
        )
    except Exception:
        pass
    if argv and argv[0] == "steer":
        return _cmd_steer(argv[1:])
    if argv and argv[0] == "resume":
        return _cmd_resume(argv[1:])
    if argv and argv[0] == "reconcile-abandoned":
        return _cmd_reconcile_abandoned(argv[1:])
    if argv and argv[0] == "drain":
        return _cmd_drain(argv[1:])
    if argv and argv[0] == "dashboard-refresh":
        return _cmd_dashboard_refresh(argv[1:])

    parser = _build_launch_parser()
    args = parser.parse_args(argv)
    try:
        args.task_ids = _parse_task_ids(args.tasks)
    except DispatchUsageError as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64
    args._original_argv = list(argv)
    _apply_fast_mode(args)  # --fast -> critical priority (skip queue)
    if args.stats is not None:
        try:
            payload = goalflight_ledger.stats_payload(args.stats)
        except ValueError as e:
            print(f"goalflight_dispatch: {e}", file=sys.stderr)
            return 64
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(goalflight_ledger.format_stats_table(payload))
        return 0
    raw = _raw_worker_args(args)
    if args.interactive:
        if args.shape not in {"auto", "acp"}:
            print("goalflight_dispatch: --interactive conflicts with --shape bash", file=sys.stderr)
            return 64
        args.shape = "acp"
        args.permission_mode = "inline"

    # Resolve comms shape. 'auto' = best per engine.
    shape = args.shape
    if shape == "auto":
        shape = "acp" if args.agent in ("claude-acp", "claude") else "bash"
    args.shape = shape
    if args.agent in CURSOR_AGENTS and shape == "acp" and not _cursor_acp_enabled():
        print(
            "goalflight_dispatch: cursor over acp is blocked pending a fix; use the "
            "bash shape, which is now the default for cursor. Measured 2026-08-24: "
            "5/5 correct on bash-tail, ~50s each. Override with "
            "GOALFLIGHT_CURSOR_ACP=1 once acp is repaired.",
            file=sys.stderr,
        )
        return 64
    _ensure_assigned_engine_session(args)
    if (
        getattr(args, "parent_dispatch_id", None)
        and _resolved_engine_session_id(args) is None
        and goalflight_codex_sessions.valid_session_id(
            getattr(args, "codex_session_id", None)
        )
        is None
    ):
        engine = goalflight_engine_sessions.resume_engine(args.agent) or args.agent
        print(
            f"goalflight_dispatch: {engine} resume launch requires a recorded session handle",
            file=sys.stderr,
        )
        return 64
    if not goalflight_compat.is_windows():
        controller_claim = _stamp_controller_session(args, _project_root(args))
        if controller_claim.get("reason") == "label_in_use":
            print(
                "goalflight_dispatch: "
                + str(controller_claim.get("message") or "label in use; rerun with --takeover"),
                file=sys.stderr,
            )
            return 73
        if controller_claim.get("visible_warning"):
            print(
                "goalflight_dispatch: controller auto-claim unavailable: "
                + str(controller_claim.get("reason") or "unknown"),
                file=sys.stderr,
            )
    if shape == "acp":
        try:
            _normalize_acp_agent(args)
            _apply_max_idle_default(args)
            if not _prompt_requested(args):
                raise DispatchUsageError("--shape acp requires --prompt or --prompt-file")
            if args.prompt_file and not Path(args.prompt_file).expanduser().exists():
                raise DispatchUsageError(f"prompt file not found: {args.prompt_file}")
            _validate_os_sandbox_conflict(args)
            _validate_agent_os_sandbox(args)
            _validate_os_sandbox_boundary(args)
            _guard_read_only_write_prompt(args)
            dispatch_warnings = _dispatch_warnings(args, raw)
            args.dispatch_warnings = dispatch_warnings
            base = _dispatch_base_dir()
            if not args.dispatch_id:
                args.dispatch_id = (
                    _default_dispatch_id(args.agent)
                    if goalflight_compat.is_windows()
                    else _reserve_auto_dispatch_id(args.agent, base)
                )
            _refuse_reused_dispatch_id_for_launch(
                args.dispatch_id,
                allow_queued=args.from_queue or args.submit,
            )
            if not getattr(args, "submit", False):
                try:
                    _bind_dispatch_worktree(args)
                except goalflight_worktree_pool.WorktreeSeatUnavailable as e:
                    print(f"goalflight_dispatch: {e}", file=sys.stderr)
                    print(
                        "goalflight_dispatch: refusing to git worktree add; "
                        "wait for a seat or raise GOALFLIGHT_WORKTREE_SEATS",
                        file=sys.stderr,
                    )
                    return 2
                except goalflight_worktree_pool.WorktreeSeatError as e:
                    print(
                        f"goalflight_dispatch: worktree seat error: {e}",
                        file=sys.stderr,
                    )
                    return 1
            occupancy_warning = _prepare_attempt_worktree_occupancy(args)
            if occupancy_warning is not None:
                args.dispatch_warnings = [
                    *getattr(args, "dispatch_warnings", []),
                    occupancy_warning,
                ]
            _refuse_launch_blocked_by_completion_authority(args)
            account_env = (
                {} if goalflight_compat.is_windows() else _resolve_launch_account_env(args)
            )
            if not goalflight_compat.is_windows():
                _validate_claude_auth_before_attempt(args, account_env)
            if args.submit:
                try:
                    return _submit_dispatch(args, raw, base=base)
                finally:
                    _release_worktree_occupancy_lock(args)
            if (
                not args.foreground
                and not getattr(args, "launch_detached", False)
                and not getattr(args, "acp_detached_child", False)
                and not goalflight_compat.is_windows()
            ):
                args.launch_detached = True
                args.background_default_notice = True
            if goalflight_compat.is_windows():
                return _run_acp_shape(args, base=base, account_env={})
            registration_warning = _prepare_attempt_controller_registration(
                args,
                _project_root(args),
            )
            if registration_warning is not None:
                args.dispatch_warnings = [
                    *getattr(args, "dispatch_warnings", []),
                    registration_warning,
                ]
            _mark_queue_claim_launch_started(args)
            return _run_acp_shape(args, base=base, account_env=account_env)
        except UnsupportedAgentSandboxRequest as e:
            try:
                return _record_unsupported_sandbox_rejection(args, e)
            except DispatchUsageError as record_error:
                print(f"goalflight_dispatch: {record_error}", file=sys.stderr)
                return 64
        except DispatchUsageError as e:
            print(f"goalflight_dispatch: {e}", file=sys.stderr)
            return 64

    if goalflight_compat.is_windows():
        return _refuse_windows_dispatch(args)

    try:
        _apply_max_idle_default(args)
        _validate_before_side_effects(args, raw)
        dispatch_warnings = _dispatch_warnings(args, raw)
    except UnsupportedAgentSandboxRequest as e:
        try:
            return _record_unsupported_sandbox_rejection(args, e)
        except DispatchUsageError as record_error:
            print(f"goalflight_dispatch: {record_error}", file=sys.stderr)
            return 64
    except DispatchUsageError as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64

    # Auto-derive id + paths so the common call is one line.
    base = _dispatch_base_dir()
    if not args.dispatch_id:
        try:
            args.dispatch_id = _reserve_auto_dispatch_id(args.agent, base)
        except DispatchUsageError as e:
            print(f"goalflight_dispatch: {e}", file=sys.stderr)
            return 64
    try:
        _refuse_reused_dispatch_id_for_launch(
            args.dispatch_id,
            allow_queued=args.from_queue or args.submit,
        )
        if not getattr(args, "submit", False):
            _bind_dispatch_worktree(args)
        occupancy_warning = _prepare_attempt_worktree_occupancy(args)
        if occupancy_warning is not None:
            dispatch_warnings = [*dispatch_warnings, occupancy_warning]
        # Occupancy-forced is a worktree hatch. Same-task live siblings still
        # refuse here so a second direct launch cannot spawn just because the
        # tree lock was waived. --submit is excluded: queuing is not a spawn.
        _refuse_launch_blocked_by_completion_authority(args)
    except goalflight_worktree_pool.WorktreeSeatUnavailable as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        print(
            "goalflight_dispatch: refusing to git worktree add; "
            "wait for a seat or raise GOALFLIGHT_WORKTREE_SEATS",
            file=sys.stderr,
        )
        return 2
    except goalflight_worktree_pool.WorktreeSeatError as e:
        print(f"goalflight_dispatch: worktree seat error: {e}", file=sys.stderr)
        return 1
    except DispatchUsageError as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64
    # Occupancy is the filesystem-corruption gate; it must refuse a second
    # writer before account/auth work that could spawn or misdirect the
    # operator. Same order as the ACP branch.
    try:
        account_env = _resolve_launch_account_env(args)
        _validate_claude_auth_before_attempt(args, account_env)
    except UnsupportedAgentSandboxRequest as e:
        try:
            return _record_unsupported_sandbox_rejection(args, e)
        except DispatchUsageError as record_error:
            print(f"goalflight_dispatch: {record_error}", file=sys.stderr)
            return 64
    except DispatchUsageError as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64
    if args.submit:
        try:
            return _submit_dispatch(args, raw, base=base)
        finally:
            _release_worktree_occupancy_lock(args)
    tail = Path(args.tail) if args.tail else base / f"{args.dispatch_id}.tail"
    status_json = Path(args.status_json) if args.status_json else base / f"{args.dispatch_id}.status.json"

    steer_file = _steer_file(args.dispatch_id)
    # Create the mailbox NOW, empty, so the path the briefing advertises is
    # real from the worker's first iteration.
    #
    # The steer preamble tells the worker "You have a steer mailbox at
    # $GOALFLIGHT_STEER_FILE. Read it AT THE TOP OF EACH ITERATION" -- an
    # assertion of existence. The file used to be created only when the first
    # steer was sent, so a worker obeying that instruction on a dispatch that
    # never received one got "No such file or directory" and reasonably read
    # an advertised-but-absent channel as a fault. Observed 2026-08-24: a
    # worker listed exactly that as one of its blockers and stopped.
    #
    # An empty carrier parses as zero messages, so this changes nothing for a
    # dispatch that does get steered; it only makes "no steers yet" look like
    # an empty mailbox instead of a missing one.
    try:
        steer_file.parent.mkdir(parents=True, exist_ok=True)
        steer_file.touch(mode=0o600, exist_ok=True)
    except OSError as exc:
        # Never fail a dispatch over the mailbox: a worker with no steer file
        # loses steering, while a worker that never launches loses everything.
        print(
            f"goalflight_dispatch: could not pre-create steer mailbox {steer_file}: "
            f"{type(exc).__name__}: {exc}; steering will start on first message",
            file=sys.stderr,
        )
    prompt_path = None if raw else _resolve_prompt_file(args, base)
    original_prompt_path = prompt_path
    orientation_path = None if raw else _project_orientation_path(
        _project_root(args),
        disabled=bool(getattr(args, "no_orientation", False)),
    )
    if prompt_path:
        prompt_path = _materialize_steer_prompt(
            prompt_path,
            base,
            args.dispatch_id,
            agent=args.agent,
            orientation_path=orientation_path,
        )
    try:
        worker_argv, stdin_path = build_worker(args, prompt_path, raw)
    except DispatchUsageError as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64
    if not worker_argv:
        print("goalflight_dispatch: no worker — use `--agent codex --prompt-file X [--cwd .]` "
              "or `-- <cmd...>`", file=sys.stderr)
        return 64

    project_root = _project_root(args)
    try:
        registration_warning = _prepare_attempt_controller_registration(
            args,
            project_root,
        )
    except DispatchUsageError as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64
    if registration_warning is not None:
        dispatch_warnings = [*dispatch_warnings, registration_warning]
    try:
        _mark_queue_claim_launch_started(args)
    except DispatchUsageError as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64

    tail.parent.mkdir(parents=True, exist_ok=True)
    _emit_dispatch_warnings(dispatch_warnings, tail_path=tail, reset_tail=True)
    worker_stdout_mode = "ab" if dispatch_warnings else "wb"
    _reap_quota_stuck_before_bash_launch()
    worker_pid = None
    watcher_pid = None
    caffeinate_pid = None
    pidfile = None
    lease_id = None
    worktree_seat = None
    ledger_recorded = False
    queue_capacity_refused = False
    detached_launched = False
    codex_dispatch_home = None
    engine_session_id = _resolved_engine_session_id(args)
    resume_engine = goalflight_engine_sessions.resume_engine(args.agent)
    codex_session_id = goalflight_codex_sessions.valid_session_id(
        getattr(args, "codex_session_id", None)
    ) or (
        engine_session_id
        if resume_engine == "codex"
        else None
    )
    effective_account = None
    request_envelope = None
    final_state = "failed"
    final_reason = None
    final_terminal_marker_present = False
    worker_alive = None
    watch_rc = 1
    dispatch_started = time.time()
    background_default_notice = bool(
        not args.foreground
        and not args.submit
        and not getattr(args, "from_queue", False)
        and not getattr(args, "launch_detached", False)
    )
    background_launch = bool(args.launch_detached or not args.foreground)
    summary_head = {
        "dispatch_id": args.dispatch_id,
        "agent": args.agent,
        "engine": _account_engine(args.agent) or args.agent,
        "worker_pid": None,
        "tail": str(tail),
        "status_json": str(status_json),
    }
    if getattr(args, "task_ids", None):
        summary_head["task_ids"] = list(args.task_ids)
    if getattr(args, "parent_dispatch_id", None):
        summary_head["parent_dispatch_id"] = args.parent_dispatch_id
    if engine_session_id is not None:
        summary_head["engine_session_id"] = engine_session_id
    if codex_session_id is not None:
        summary_head["codex_session_id"] = codex_session_id
    codex_home_owner_dispatch_id = _codex_home_owner_dispatch_id(args)
    if codex_home_owner_dispatch_id is not None:
        summary_head["codex_home_owner_dispatch_id"] = (
            codex_home_owner_dispatch_id
        )

    try:
        assert not args.submit, "--submit must bypass inline capacity acquisition"
        # Pre-record the ledger BEFORE the capacity queue: the (possibly
        # minutes-long) wait window must be visible to goalflight_status
        # (classified queued_capacity = live) and must trip the reused-id
        # guard for a duplicate explicit --dispatch-id (review P1: the guard
        # is ledger-based, so a ledger-less wait window was bypassable).
        # Back-compat: ledger consumers now see one extra pre-spawn row per
        # dispatch; the existing running row is still written after capacity
        # is acquired and the worker is spawned.
        if getattr(args, "parent_dispatch_id", None) and resume_engine == "codex":
            resume_home = Path(args.codex_resume_home).expanduser().resolve(
                strict=False
            )
            with _codex_resume_lock(resume_home, args.codex_session_id):
                _revalidate_codex_resume_claim(
                    parent_dispatch_id=args.parent_dispatch_id,
                    expected_home=resume_home,
                    expected_session_id=args.codex_session_id,
                    expected_home_owner_dispatch_id=codex_home_owner_dispatch_id,
                )
                if _CODEX_RESUME_CLAIM_VALIDATED_HOOK is not None:
                    _CODEX_RESUME_CLAIM_VALIDATED_HOOK(
                        resume_home,
                        args.codex_session_id,
                        args.parent_dispatch_id,
                    )
                _record_ledger(
                    args,
                    project_root=project_root,
                    prompt_path=prompt_path,
                    status_json=status_json,
                    tail=tail,
                    lease_id=None,
                    worker_pid=None,
                    state="waiting_capacity",
                )
                if _CODEX_RESUME_DURABLE_CLAIM_HOOK is not None:
                    _CODEX_RESUME_DURABLE_CLAIM_HOOK(
                        resume_home,
                        args.codex_session_id,
                        args.dispatch_id,
                    )
        elif getattr(args, "parent_dispatch_id", None) and resume_engine and engine_session_id:
            with _engine_resume_lock(resume_engine, engine_session_id):
                _revalidate_resume_claim(
                    parent_dispatch_id=args.parent_dispatch_id,
                    expected_engine=resume_engine,
                    expected_session_id=engine_session_id,
                )
                _record_ledger(
                    args,
                    project_root=project_root,
                    prompt_path=prompt_path,
                    status_json=status_json,
                    tail=tail,
                    lease_id=None,
                    worker_pid=None,
                    state="waiting_capacity",
                )
        else:
            _record_ledger(
                args,
                project_root=project_root,
                prompt_path=prompt_path,
                status_json=status_json,
                tail=tail,
                lease_id=None,
                worker_pid=None,
                state="waiting_capacity",
            )
        ledger_recorded = True
        try:
            lease_id = _acquire_capacity(args, project_root=project_root, status_json=status_json)
        except (SystemExit, KeyboardInterrupt) as exc:
            # Queue exhausted or interrupted: the status file already says
            # blocked_capacity; make the ledger finish agree instead of the
            # generic "failed", mirroring the status plane's specific reason
            # (wait_interrupted vs machine_worker_cap vs agent_worker_cap ...).
            final_state = "blocked_capacity"
            final_reason = "capacity_wait"
            blocked = {}
            with contextlib.suppress(Exception):
                blocked = json.loads(Path(status_json).read_text(encoding="utf-8"))
                specific = (blocked.get("reason") or {}).get("reason")
                if specific:
                    final_reason = f"capacity_wait:{specific}"
            capacity_refused = bool(
                isinstance(exc, SystemExit)
                and exc.code == 2
                and _is_capacity_refusal(blocked)
            )
            queue_capacity_refused = bool(
                capacity_refused
                and args.from_queue
                and args.queue_launch_token
                and args.queue_claim_path
            )
            if capacity_refused:
                # Reuse the canonical queue writer. It rewrites the live
                # waiting_capacity ledger row to exact state="queued", which
                # is the state allow_queued accepts on the drain replay. The
                # PREPARED journal attempt stays non-terminal and its launch
                # token is reused by the drain. Keep the finally block from
                # terminalizing the new queued ledger row.
                if _queue_detached_capacity_refusal(args, blocked, base=base):
                    ledger_recorded = False
                    return 0
            raise
        if _account_engine(args.agent) == "grok" and not args.account:
            # Same contract as the codex pointer: args.account stays None so the
            # ledger records "default" (nothing pinned), while effective_account
            # names the seat this dispatch actually billed.
            effective_account = grok_selected_account(args)
        if (
            getattr(args, "parent_dispatch_id", None)
            and resume_engine not in {None, "codex"}
            and engine_session_id
        ):
            with _engine_resume_lock(resume_engine, engine_session_id):
                _revalidate_resume_claim(
                    parent_dispatch_id=args.parent_dispatch_id,
                    expected_engine=resume_engine,
                    expected_session_id=engine_session_id,
                    exclude_dispatch_id=args.dispatch_id,
                )
        if (
            _account_engine(args.agent) == "codex"
            and not goalflight_compat.is_windows()
        ):
            if getattr(args, "parent_dispatch_id", None):
                resume_home = Path(args.codex_resume_home).expanduser().resolve(
                    strict=False
                )
                with _codex_resume_lock(resume_home, args.codex_session_id):
                    _revalidate_codex_resume_claim(
                        parent_dispatch_id=args.parent_dispatch_id,
                        expected_home=resume_home,
                        expected_session_id=args.codex_session_id,
                        expected_home_owner_dispatch_id=codex_home_owner_dispatch_id,
                        exclude_dispatch_id=args.dispatch_id,
                    )
                    codex_dispatch_home, effective_account = (
                        _rebuild_codex_resume_home(
                            project_root,
                            args.parent_dispatch_id,
                            resume_home,
                            args.codex_session_id,
                            home_owner_dispatch_id=codex_home_owner_dispatch_id,
                        )
                    )
            else:
                try:
                    codex_dispatch_home, effective_account = resolve_codex_home(
                        project_root,
                        args.account,
                        args.dispatch_id,
                    )
                except BaseException:
                    codex_dispatch_home, effective_account = None, None
        request_envelope = _queue_request_envelope(args)
        worktree_seat = _bind_dispatch_worktree(args)
        if worktree_seat is not None:
            worker_argv, stdin_path = build_worker(args, prompt_path, raw)
            summary_head["worktree_seat"] = worktree_seat.seat_name
            summary_head["worktree_path"] = str(worktree_seat.path)
            summary_head["worktree_branch"] = worktree_seat.branch
            summary_head["worktree_base"] = _requested_worktree_base(args)
        _record_ledger(
            args,
            project_root=project_root,
            prompt_path=prompt_path,
            status_json=status_json,
            tail=tail,
            lease_id=lease_id,
            worker_pid=None,
            state="starting",
            effective_account=effective_account,
            codex_home=codex_dispatch_home,
            request_envelope=request_envelope,
        )

        # 1. Launch the worker DETACHED, output -> tail (prompt -> stdin for codex).
        # Account guards ran before prompt/id/lease side effects; only apply the
        # resolved environment here.
        env = dict(os.environ)
        env.update(account_env)
        if codex_dispatch_home is not None:
            env["CODEX_HOME"] = codex_dispatch_home
            summary_head["codex_home"] = codex_dispatch_home
        env["GOALFLIGHT_STEER_FILE"] = str(steer_file)
        env["GOALFLIGHT_DISPATCH_SCRIPT"] = str(Path(__file__).resolve())
        # A worker's only channel used to be markers scraped out of its console
        # log, which the watcher bridges into mail. That makes every message a
        # side effect of stdout: it cannot be addressed, cannot be typed, and
        # arrives only if the scrape recognises it. Telling the worker its own
        # dispatch id gives it a first-class SIDEBAND -- it can post a real
        # envelope with goalflight_messages.py while still writing prose to its
        # log for the human reading the tail.
        env["GOALFLIGHT_DISPATCH_ID"] = str(args.dispatch_id)
        if worktree_seat is not None:
            env[goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV] = str(
                worktree_seat.fileno()
            )
        if original_prompt_path:
            env["GOALFLIGHT_PROMPT_FILE"] = str(original_prompt_path)
        else:
            env.pop("GOALFLIGHT_PROMPT_FILE", None)
        # An auto-selected seat is still a seat: the API-key scrub must key on
        # the account actually resolved, or a GROK_API_KEY in the environment
        # would quietly bill the API instead of the subscription seat we chose.
        if (args.account or grok_selected_account(args)) and _account_engine(
            args.agent
        ) == "grok":
            env.pop("GROK_API_KEY", None)
            env.pop("XAI_API_KEY", None)
        if _account_engine(args.agent) == "grok":
            # Grok refuses an untrusted cwd by exiting in seconds with nothing on
            # stdout, which through the dispatcher is indistinguishable from a
            # worker that launched and died. A per-account home starts with an
            # EMPTY trust list and every worktree is its own directory, so a
            # controller dispatching into a fresh worktree hits this every time.
            # Register the cwd we are about to hand the worker -- the operator
            # already chose it; the alternative is a silent death.
            # One handler, and it must not reference a name bound inside the
            # try: if the import fails, evaluating `except grok_seats.X` raises
            # NameError THROUGH the remaining handlers and kills the dispatch,
            # which is the one thing this block may never do.
            try:
                import grok_seats

                added = grok_seats.ensure_project_trusted(
                    Path(env.get("HOME") or Path.home()), _project_root(args)
                )
                if added:
                    print(
                        "goalflight_dispatch: registered "
                        f"{_project_root(args)} as grok-trusted for this worker home",
                        file=sys.stderr,
                    )
            except BaseException as exc:
                detail = (
                    str(exc)
                    if type(exc).__name__ == "TrustRefused"
                    else f"{type(exc).__name__}; worker may exit immediately"
                )
                print(
                    f"goalflight_dispatch: WARN: grok folder trust not registered: {detail}",
                    file=sys.stderr,
                )
        if args.account and _account_engine(args.agent) == "cursor":
            env.pop("CURSOR_API_KEY", None)
        if args.billing == "sub" and _account_engine(args.agent) == "codex":
            env.pop("OPENAI_API_KEY", None)  # subscription billing for the selected account, not the API
        _apply_web_qa_env(env, args, project_root)
        if _account_engine(args.agent) == "codex":
            worker_argv = _guard_codex_context_mode_disable(worker_argv, env)

        _mark_queue_claim_worker_spawn_intent(args)
        worker_argv, wait_for_worker_claim = _attempt_claiming_worker_argv(
            project_root,
            str(args.dispatch_id),
            worker_argv,
        )
        worker_pid = _spawn_daemonized_process(
            worker_argv,
            env=env,
            stdin_path=stdin_path,
            stdout_path=tail,
            stdout_mode=worker_stdout_mode,
            stderr="stdout",
            serialize_stdout=True,
            label="worker",
            cwd=str(_worker_cwd(args)),
            inherit_occupancy_lock=True,
        )
        if worktree_seat is not None:
            # Worker inherited the fd. Drop this process's copy so the seat
            # lifetime is the worker's, not the launcher's. Sidecars must not
            # see the now-closed fd number in their environment.
            worktree_seat.release()
            worktree_seat = None
            env.pop(goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, None)
        _mark_queue_claim_worker_spawned(args, worker_pid)
        started = time.time()
        registration_errors = []

        worker_identity = None
        try:
            worker_identity = _process_identity_after_spawn(worker_pid)
        except Exception as e:
            registration_errors.append(_registration_error("process_identity", e))
        worker_identity_token = _identity_token(worker_identity)

        pgid = worker_pid
        try:
            pgid = process_group_id(worker_pid) or worker_pid
        except Exception as e:
            registration_errors.append(_registration_error("process_group_id", e))
        caffeinate_log = base / f"{args.dispatch_id}.caffeinate.log"
        caffeinate_pid, caffeinate_reason = _start_caffeinate(
            worker_pid,
            env=env,
            stdout_path=caffeinate_log,
        )
        if caffeinate_pid:
            summary_head["caffeinate_pid"] = caffeinate_pid
        elif sys.platform == "darwin":
            registration_errors.append(_registration_error("caffeinate", RuntimeError(caffeinate_reason or "failed")))
        # Record worker_pid on the lease FIRST and unconditionally: stale-release
        # treats a live worker_pid as authoritative, so this must persist even if
        # the detached reparent below fails (otherwise release-stale could free a
        # live detached worker's slot when its controller is dead or unknown).
        try:
            _attach_worker_to_lease(lease_id, worker_pid)
        except Exception as e:
            registration_errors.append(_registration_error("attach_worker_to_lease", e))
        if background_launch:
            try:
                _detach_lease_to_worker(
                    lease_id,
                    worker_pid,
                    "bash_launch_detached" if args.launch_detached else "bash_background_default",
                )
            except Exception as e:
                registration_errors.append(_registration_error("detach_lease_to_worker", e))
        try:
            pidfile = _write_pidfile(
                args,
                worker_pid=worker_pid,
                pgid=pgid,
                identity=worker_identity,
                detached=background_launch,
            )
        except Exception as e:
            registration_errors.append(_registration_error("write_pidfile", e))
            pidfile = None
        ledger_running_warning = None
        try:
            if wait_for_worker_claim:
                goalflight_ledger.claim_attempt_running(
                    project_root,
                    str(args.dispatch_id),
                    worker_pid,
                )
            ledger_running_warning = _record_ledger(
                args,
                project_root=project_root,
                prompt_path=prompt_path,
                status_json=status_json,
                tail=tail,
                lease_id=lease_id,
                worker_pid=worker_pid,
                state="running",
                effective_account=effective_account,
                codex_home=codex_dispatch_home,
                request_envelope=request_envelope,
            )
            if getattr(args, "parent_dispatch_id", None):
                _mark_reconciled_parent_resumed(
                    args.parent_dispatch_id,
                    args.dispatch_id,
                )
        except Exception as e:
            registration_errors.append(_registration_error("record_ledger_running", e))
        if ledger_running_warning:
            # _record_ledger already warned loudly and wrote the status file;
            # fold the refusal into the launch registration warnings too so
            # every operator surface carries it.
            registration_errors.append(
                {
                    "step": "record_ledger_running",
                    "reason": (
                        "journal attempt transition refused: "
                        f"{ledger_running_warning.get('disposition') or 'unknown'} "
                        f"({ledger_running_warning.get('error') or 'no detail'})"
                    ),
                }
            )

        summary_head.update({"worker_pid": worker_pid, "worker_identity": worker_identity_token})
        if effective_account:
            summary_head["effective_account"] = effective_account
        if registration_errors:
            summary_head["registration_errors"] = registration_errors
            with contextlib.suppress(Exception):
                print("DISPATCH-REGISTRATION-WARN " + json.dumps({
                    "dispatch_id": args.dispatch_id,
                    "errors": registration_errors,
                }, sort_keys=True), file=sys.stderr, flush=True)
        with contextlib.suppress(Exception):
            print("DISPATCH-START " + json.dumps(summary_head, sort_keys=True), flush=True)
        _print_status_reminder(
            args.dispatch_id,
            status_json=status_json,
            tail_path=tail,
            worker_pid=worker_pid,
            shape="bash",
            agent=args.agent,
            controller_pid=_controller_pid(args),
            poll_secs=args.poll_secs,
            max_idle_secs=args.max_idle_secs,
            prompt_path=prompt_path,
            hints=bool(getattr(args, "hints", False)),
        )

        # 2. Run the decoupled watcher detached from this launcher. We still poll
        # status and return when it records a terminal state, but launcher teardown
        # no longer tears down the watcher with the worker.
        controller_pid = _controller_pid(args)
        controller_session_id = _controller_session_id(args)
        watch_cmd = _watcher_spawn_argv(
            worker_pid=worker_pid,
            tail=tail,
            status_json=status_json,
            agent=args.agent,
            poll_secs=args.poll_secs,
            max_idle_secs=args.max_idle_secs,
            dispatch_id=args.dispatch_id,
            pgid=pgid,
            project_root=project_root,
            worker_cwd=_worker_cwd(args),
            task_ids=getattr(args, "task_ids", None),
            worker_identity=worker_identity_token,
            launch_detached=bool(args.launch_detached),
            codex_dispatch_home=codex_dispatch_home,
            codex_session_id=codex_session_id,
            engine_session_id=engine_session_id,
            parent_dispatch_id=getattr(args, "parent_dispatch_id", None),
            codex_home_owner_dispatch_id=codex_home_owner_dispatch_id,
            controller_pid=controller_pid,
            controller_session_id=controller_session_id,
            controller_label=_controller_label(args),
            prompt_path=prompt_path,
        )

        watch_log = base / f"{args.dispatch_id}.watcher.log"
        # The watcher is the authoritative status writer once spawned. This
        # prelaunch mirror gives up only early status freshness on failure.
        with contextlib.suppress(Exception):
            write_status(status_json, {
                **_prelaunch_status_metadata(
                    args,
                    codex_home=codex_dispatch_home,
                    codex_session_id=codex_session_id,
                ),
                "worker_pid": worker_pid,
                "detached": bool(args.launch_detached),
                "pgid": pgid,
                "worker_alive": True,
                "worker_identity": worker_identity_token,
                "expected_worker_identity": worker_identity_token,
                "tail_path": str(tail),
                "state": "starting",
                "reason": "watcher_launching",
                "updated_at": int(time.time()),
                **(
                    {"ledger_record_warning": ledger_running_warning}
                    if ledger_running_warning
                    else {}
                ),
            })
        _export_dashboard_status_for_project(project_root)
        _start_dashboard_refresh_for_project(project_root)
        watcher_pid = _spawn_daemonized_process(
            watch_cmd,
            env=_sidecar_env(os.environ.copy()),
            stdout_path=watch_log,
            stdout_mode="wb",
            stderr="stdout",
            label="watcher",
        )
        summary_head.update({"watcher_pid": watcher_pid, "watcher_log": str(watch_log)})
        if background_launch:
            detached_launched = True
            if background_default_notice:
                _print_background_default_notice()
            with contextlib.suppress(Exception):
                print(
                    "DISPATCH-LAUNCHED "
                    + json.dumps(
                        {
                            **summary_head,
                            "lease_id": lease_id,
                            "state": "running",
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            return 0
        watch_rc, rec, final_reason = _wait_for_detached_watcher(
            status_json=status_json,
            watcher_pid=watcher_pid,
            poll_secs=args.poll_secs,
            args=args,
            tail=tail,
            worker_pid=worker_pid,
            worker_identity=worker_identity,
            pgid=pgid,
            prompt_path=str(prompt_path) if prompt_path else None,
        )
        if watch_rc == 130 and final_reason == "foreground_wait_interrupted":
            # Preserve only the pidfile protection; capacity/ledger stay live for re-attach.
            with contextlib.suppress(Exception):
                _cleanup_pidfile_if_worker_dead(pidfile, worker_pid)
            detached_launched = True
            final_state = "interrupted"
            return 130

        # Read the terminal state the watcher recorded (best-effort). worker_still_alive
        # matters: a terminal marker is a NON-DESTRUCTIVE signal (we never kill the
        # worker), so if it is still alive the orchestrator should re-attach a watcher to
        # keep following it, not assume it is finished.
        state = None
        try:
            state = rec.get("state")
            worker_alive = rec.get("worker_alive")
            codex_session_id = (
                goalflight_codex_sessions.valid_session_id(
                    rec.get("codex_session_id")
                )
                or codex_session_id
            )
            final_terminal_marker_present = goalflight_terminal.terminal_marker_present(
                rec.get("terminal_marker")
            )
        except Exception:
            pass
        if not final_reason and watch_log.exists():
            try:
                lines = watch_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                if lines:
                    final_reason = json.loads(lines[-1]).get("reason")
            except Exception:
                final_reason = lines[-1] if "lines" in locals() and lines else None
        if not final_reason and watch_rc != 0:
            final_reason = f"watcher_exit_{watch_rc}"
        if watch_rc != 0:
            terminal_state = goalflight_ledger.terminal_state_for(state, final_reason)
            final_state = (state or "failed") if terminal_state not in {"complete", "unknown"} else "failed"
        else:
            final_state = state or "complete"

        # 3. Emit a summary and propagate the watcher's REAL exit code (no masking).
        end_payload = {
            **summary_head,
            "watcher_exit": watch_rc,
            "terminal_state": final_state,
            "worker_still_alive": worker_alive,  # True on a marker => signal-to-review, NOT done; re-attach
            "reason": final_reason,
            "elapsed_s": round(time.time() - started, 1),
        }
        hint = _dispatch_end_reattach_hint(
            args.dispatch_id,
            terminal_state=final_state,
            worker_alive=worker_alive,
        )
        if hint:
            end_payload["hint"] = hint
        print("DISPATCH-END " + json.dumps(end_payload, sort_keys=True), flush=True)
        return watch_rc
    except SystemExit:
        raise
    except goalflight_worktree_pool.WorktreeSeatUnavailable as e:
        final_state = "failed_worktree"
        final_reason = str(e)
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        print(
            "goalflight_dispatch: refusing to git worktree add; "
            "wait for a seat or raise GOALFLIGHT_WORKTREE_SEATS",
            file=sys.stderr,
        )
        return 2
    except goalflight_worktree_pool.WorktreeSeatError as e:
        final_state = "failed_worktree"
        final_reason = str(e)
        print(f"goalflight_dispatch: worktree seat error: {e}", file=sys.stderr)
        return 1
    except DispatchUsageError as e:
        final_state = "failed"
        final_reason = str(e)
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64
    except Exception as e:
        final_state = "failed"
        final_reason = f"{type(e).__name__}: {e}"
        print("DISPATCH-ERROR " + json.dumps({"state": final_state, "reason": final_reason}, sort_keys=True), file=sys.stderr, flush=True)
        return 1
    finally:
        if worktree_seat is not None:
            worktree_seat.release()
            worktree_seat = None
        final_worker_alive = worker_alive
        if final_worker_alive is None and worker_pid:
            final_worker_alive = goalflight_compat.pid_alive(worker_pid)
        keep_live_watcher_open = _is_live_watcher_stopped(final_state, final_worker_alive)
        capacity_state, capacity_reason = _quota_limited_state_reason(
            final_state,
            final_reason,
            tail,
            agent=args.agent,
        )
        if (
            ledger_recorded
            and not detached_launched
            and not keep_live_watcher_open
            and not queue_capacity_refused
        ):
            try:
                final_state, ledger_reason, _rate_limited = goalflight_terminal.terminal_rate_limit_outcome(
                    final_state,
                    final_reason,
                    tail,
                    terminal_marker_present=final_terminal_marker_present,
                )
                if _rate_limited:
                    final_reason = ledger_reason
                _finish_ledger(
                    args.dispatch_id,
                    str(capacity_state or final_state),
                    capacity_reason,
                    elapsed_s=round(time.time() - dispatch_started, 3),
                    worker_still_alive=final_worker_alive,
                )
            except Exception as exc:
                # Finalization must not mask the launch/watch outcome, but a
                # missing terminal journal transition is never silent.
                with contextlib.suppress(OSError, ValueError):
                    print(
                        "DISPATCH-FINALIZE-WARN "
                        + json.dumps(
                            {
                                "dispatch_id": args.dispatch_id,
                                "write": "terminal_ledger",
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
        if not detached_launched and not keep_live_watcher_open:
            try:
                _release_capacity(lease_id, str(capacity_state or final_state), capacity_reason)
            except Exception as exc:
                # Capacity release is cleanup after the death/result record.
                # Keep that primary outcome intact and report the leaked lease.
                with contextlib.suppress(OSError, ValueError):
                    print(
                        "DISPATCH-FINALIZE-WARN "
                        + json.dumps(
                            {
                                "dispatch_id": args.dispatch_id,
                                "write": "capacity_release",
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
            _cleanup_pidfile_if_worker_dead(pidfile, worker_pid)
            _export_dashboard_status_for_project(project_root)
        if (
            codex_dispatch_home is not None
            and not detached_launched
            and final_worker_alive is not True
            and codex_session_id is None
        ):
            cleanup_codex_dispatch_home(args.dispatch_id)


def _ensure_acp_sdk_interpreter(argv: list[str] | None = None) -> None:
    """Re-exec into the ACP SDK venv before any ACP work begins.

    goalflight_acp_run guards this in its own __main__, but the dispatch path
    IMPORTS that module (lazily, inside _run_acp_shape) rather than executing
    it, so that guard never fires here. The SDK import in goalflight_acp_client
    then fails under the system interpreter and every ACP dispatch dies with
    "SDK missing" even though the SDK is installed correctly in the venv.

    Done at __main__ entry, before main() parses args or takes any lease, so
    the execv restart repeats no side effects. Only fires for acp-shaped
    invocations, so bash-shape dispatch is untouched. UNAVAILABLE is not
    fail-closed here: main() must still queue, refuse a permanent os-sandbox
    mismatch, and honour the test ACP seam. Spawn-time require_acp_sdk is
    the fail-closed gate for a real ACP session.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    def _opt(name: str) -> str | None:
        for i, tok in enumerate(argv):
            if tok == name and i + 1 < len(argv):
                return argv[i + 1]
            if tok.startswith(name + "="):
                return tok.split("=", 1)[1]
        return None

    shape = _opt("--shape") or "auto"
    if shape == "auto":
        agent = _opt("--agent")
        shape = (
            "acp"
            if agent in ("claude-acp", "claude") or "--interactive" in argv
            else "bash"
        )
    if shape != "acp":
        return
    try:
        import goalflight_acp_run  # noqa: PLC0415
        from goalflight_acp_client import ACP_SDK_REEXEC  # noqa: PLC0415
    except ImportError:
        # The SDK genuinely is not importable here. Proceeding silently is
        # correct and deliberate: main() still handles submit, the test ACP
        # seam, and permanent os-sandbox refusal, and a real ACP session imports
        # goalflight_acp_run again at _run_acp_shape WITHOUT this guard, so a
        # broken SDK still fails there rather than being papered over.
        return
    except Exception as exc:
        # Anything that is NOT an ImportError means the probe could not find
        # out -- an OSError, a TypeError from a half-initialised module. That is
        # UNKNOWN, and silently returning renders it as "no re-exec needed",
        # which is the error-swallowing-lookup shape this codebase keeps
        # shipping. Proceed (fail-open is right on this path, per above) but say
        # so, so the operator is not left to infer it from behaviour.
        #
        # BaseException is deliberately NOT caught: KeyboardInterrupt and
        # SystemExit are the operator stopping us, and swallowing them here made
        # the launch path un-interruptible during startup.
        print(
            "goalflight_dispatch: WARN: ACP SDK re-exec probe could not complete "
            f"({type(exc).__name__}: {exc}); continuing without re-exec, so an ACP "
            "session may resolve a different interpreter than expected.",
            file=sys.stderr,
        )
        return
    # Re-exec here so a later SDK import sees the venv. Do not fail-close on
    # UNAVAILABLE: main() still has submit, the test ACP seam, and permanent
    # os-sandbox refusal to handle. Spawn-time require_acp_sdk fail-closes.
    if goalflight_acp_run._acp_reexec_target().state != ACP_SDK_REEXEC:
        return
    goalflight_acp_run._ensure_acp_sdk_python()


if __name__ == "__main__":
    _ensure_acp_sdk_interpreter()
    raise SystemExit(main())
