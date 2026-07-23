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
    python3 goalflight_dispatch.py --agent codex --prompt-file p.md --cwd . --foreground  # synchronous scripts/tests

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
import signal
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import goalflight_compat
import goalflight_capacity
import goalflight_dispatch_paths
import goalflight_dispatch_states
import goalflight_steer_mailbox
import goalflight_ledger
import goalflight_quota_stuck
import goalflight_terminal
from goalflight_codex_sandbox import codex_workspace_write_args
from goalflight_liveness import active_monotonic, process_group_id, write_status
from goalflight_watch import (
    SUCCESS_TERMINAL_MARKERS,
    _final_terminal_marker,
    _last_line_is_terminal_marker,
    _marker_state as _marker_state_for_terminal,
)

SCRIPT_DIR = Path(__file__).resolve().parent
WATCH_PY = SCRIPT_DIR / "goalflight_watch.py"
WATCH_TAIL_SH = SCRIPT_DIR / "watch-dispatch-tail.sh"
DAEMON_SPAWN_ARG = "__goalflight_spawn_daemon"
DISPATCH_QUEUE_SCHEMA = "goalflight.dispatch-queue.v1"
QUEUE_CLAIM_STALE_S = 300.0
LAUNCH_TIMEOUT_S = QUEUE_CLAIM_STALE_S
MAX_CLAIM_RECOVERY_REQUEUES = 1
RECONCILE_DOWNSTREAM_LOCK_BUDGET_S = 0.100
RECONCILE_LOCK_POLL_S = 0.010
PRELAUNCH_CANDIDATE_STATES = frozenset(
    {"queued", "waiting_capacity", "starting", "submitted", "claimed"}
)
QUEUE_PRIORITY_RANK = {lane: rank for rank, lane in enumerate(goalflight_capacity.PRIORITY_LANES)}
QUEUE_DEFAULT_PRIORITY = "normal"
PRESET_AGENTS = {"codex", "grok-code", "grok-research", "kimi"}
STDIN_PROMPT_AGENTS = {"codex", "grok-code", "grok-research", "kimi"}
DEFAULT_MAX_IDLE_SECS = 180.0
_DASHBOARD_REFRESH_SUBCOMMAND = "dashboard-refresh"
_DASHBOARD_REFRESH_MARKER = "goalflight-dashboard-refresh-v1"
_DASHBOARD_REFRESH_QUEUED_GRACE_S = 60 * 60
_DASHBOARD_REFRESH_MAX_LIFETIME_S = 4 * 60 * 60
_DASHBOARD_REFRESH_CLAIM_STALE_S = 30.0
CODE_WRITER_MAX_IDLE_SECS = 600.0
CODE_WRITER_AGENTS = {"codex", "codex-acp", "grok-code", "grok-acp", "kimi", "cursor", "cursor-agent"}


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
    DEFER_UNCHANGED = "defer_unchanged"


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
_CODEX_SANDBOX_VALUE = {
    "workspace-write": "workspace-write",
    "read-only": "read-only",
    "off": "danger-full-access",  # codex's sanctioned "no Seatbelt" sandbox value
}


def _effective_os_sandbox(args) -> str:
    """Resolve the effective OS sandbox profile (non-raising, safe anywhere).

    Precedence: explicit --os-sandbox > legacy --read-only alias > agent default
    (Kimi "off", otherwise "workspace-write"). Conflicts are surfaced separately by
    _validate_os_sandbox_conflict.
    """
    explicit = getattr(args, "os_sandbox", None)
    if explicit in OS_SANDBOX_PROFILES:
        return explicit
    if getattr(args, "read_only", False):
        return "read-only"
    if str(getattr(args, "agent", "")) == "kimi":
        return OS_SANDBOX_OFF
    return "workspace-write"


def _effective_read_only(args) -> bool:
    return _effective_os_sandbox(args) == "read-only"


def _validate_os_sandbox_conflict(args) -> None:
    explicit = getattr(args, "os_sandbox", None)
    if explicit and getattr(args, "read_only", False) and explicit != "read-only":
        raise DispatchUsageError(
            f"--read-only conflicts with --os-sandbox {explicit} "
            "(opposite write postures); pass only one."
        )


def _validate_agent_os_sandbox(args) -> None:
    profile = _effective_os_sandbox(args)
    if str(getattr(args, "agent", "")) == "kimi" and profile != OS_SANDBOX_OFF:
        raise DispatchUsageError(
            f"--agent kimi supports only --os-sandbox {OS_SANDBOX_OFF}; "
            f"requested profile {profile!r} is not enforced (b-079)"
        )


def _os_sandbox_warning(args) -> str | None:
    """Dispatch-time advisory: log when 'off' is selected (required), and when an
    explicit --os-sandbox is a no-op on a worker that can't honor it."""
    explicit = getattr(args, "os_sandbox", None)
    profile = _effective_os_sandbox(args)
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
    if explicit and not codex_bash:
        return (
            f"--os-sandbox {explicit} only affects bash-shape codex workers; "
            f"ignored for agent={getattr(args, 'agent', '?')} "
            f"shape={getattr(args, 'shape', '?')}."
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
GIT_BASE_PIN_RE = re.compile(r"(?<![A-Za-z0-9_./:-])([0-9A-Fa-f]{7,40})(?![A-Za-z0-9_./:-])")
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
# tool_output_error'd repeatedly); grok-research runs grok-build, the web
# model. Precision-first: signals are explicit web-research phrasings; bare
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
    "into your plan; ack each with `STEER-ACK: <seq>` on its own line; a steer may "
    "redirect or HALT you — honor it."
)
PROMPT_FILE_PREAMBLE = (
    "Your FULL original brief is at `$GOALFLIGHT_PROMPT_FILE`. Re-read it after any "
    "internal compaction/summarization, at the start of each long-run goal-loop "
    "iteration, and before final commit/exit; the disk file is authoritative over "
    "summarized memory."
)
PROJECT_ORIENTATION_RELATIVE = Path("docs-private") / "rag" / "ORIENTATION.md"
PROJECT_ORIENTATION_SCOPE_RULE = "Read it before starting; orientation only, never scope expansion."
WORKER_EXECUTION_PREAMBLE = (
    "Worker execution contract:\n"
    "- Use your available tools to actually perform the requested filesystem, shell, "
    "research, or analysis actions before answering. Do not only plan, summarize, or "
    "describe commands.\n"
    "- For successful completion, emit a final line outside any Markdown fence in this "
    "exact shape: `COMPLETE: <summary>`.\n"
    "- The `COMPLETE: <summary>` line must be the last non-empty line of your output. "
    "Do not print anything after it."
)
STEER_ACK_RE = re.compile(r"^\**STEER-ACK:\**\s*(\d+)\b")


class DispatchUsageError(Exception):
    pass


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
            if stdout_path:
                stdout_file = Path(stdout_path)
                stdout_file.parent.mkdir(parents=True, exist_ok=True)
                stdout_f = stack.enter_context(open(stdout_file, stdout_mode))
                if serialize_stdout and fcntl is not None:
                    # The child inherits this locked stdout file description.
                    # Reconciliation takes the same flock before its final
                    # completion read + durable write, so it cannot pass a
                    # COMPLETE still buffered in a live worker's stdout path.
                    fcntl.flock(stdout_f.fileno(), fcntl.LOCK_EX)
            else:
                stdout_f = subprocess.DEVNULL
            stderr_f = subprocess.STDOUT if stderr_mode == "stdout" else subprocess.DEVNULL
            child = subprocess.Popen(
                argv,
                stdin=stdin_f,
                stdout=stdout_f,
                stderr=stderr_f,
                start_new_session=True,
                close_fds=True,
            )
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
) -> int:
    """Spawn a child through the private daemon helper and return the child's pid."""
    spec = {
        "argv": argv,
        "stdin_path": stdin_path,
        "stdout_path": str(stdout_path) if stdout_path else None,
        "stdout_mode": stdout_mode,
        "stderr": stderr,
        "serialize_stdout": bool(stdout_path and serialize_stdout),
    }
    helper = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), DAEMON_SPAWN_ARG],
        input=json.dumps(spec, sort_keys=True),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
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
    if state == "idle_timeout":
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
    if state not in {"idle_timeout", "rate_limited", "worker_dead"}:
        return state, reason
    quota_reason = goalflight_quota_stuck.quota_limited_reason(
        agent=agent,
        tail=tail,
        previous_state=state,
        previous_reason=reason,
    )
    if not quota_reason:
        return state, reason
    return "rate_limited", quota_reason


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
    if terminal_state in {"idle_timeout", "watcher_stopped"} and worker_alive is True:
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
    terminal_marker = _last_line_is_terminal_marker(
        tail,
        ignore_prefix_lines=ignore_prefix_lines,
        kimi_output=getattr(args, "agent", None) == "kimi",
    )
    if not terminal_marker and not worker_is_alive:
        terminal_marker = _final_terminal_marker(
            tail,
            ignore_prefix_lines=ignore_prefix_lines,
            suppress_unfenced_prompt_markers=True,
            kimi_output=getattr(args, "agent", None) == "kimi",
        )
    if not terminal_marker:
        terminal_marker = payload.get("terminal_marker")
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


def _start_caffeinate(worker_pid: int, *, env: dict[str, str], stdout_path: Path) -> tuple[int | None, str | None]:
    if sys.platform != "darwin":
        return None, "not_darwin"
    caffeinate = shutil.which("caffeinate")
    if not caffeinate:
        return None, "caffeinate_not_found"
    try:
        pid = _spawn_daemonized_process(
            [caffeinate, "-dimsu", "-w", str(worker_pid)],
            env=env,
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
    return Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()


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
        _export_dashboard_status_for_project(project_root)
        if time.monotonic() - started >= float(max_lifetime_s):
            return 0
        if not _dashboard_project_has_live_dispatch(project_root):
            return 0
        time.sleep(interval)


def _cmd_dashboard_refresh(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=argparse.SUPPRESS)
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
        except Exception:
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


def _tail_filesystem_identity(tail: Path, *, locality: str) -> FilesystemIdentity:
    resolved = tail.expanduser().resolve(strict=False)
    parent = resolved.parent
    probe_parent = parent
    while not probe_parent.exists() and probe_parent != probe_parent.parent:
        probe_parent = probe_parent.parent
    try:
        stat_result = probe_parent.stat()
        device = int(stat_result.st_dev)
    except OSError:
        return FilesystemIdentity(None, str(parent), "unknown", "unknown")
    fs_type = "unknown"
    identity_mount = f"dev:{device}"
    if sys.platform == "darwin":
        try:
            mounts = subprocess.run(
                ["mount"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            mounts = None
        best: tuple[int, str, str] | None = None
        if mounts is not None and mounts.returncode == 0:
            for line in mounts.stdout.splitlines():
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
    return FilesystemIdentity(device, identity_mount, fs_type, effective_locality)


def _probe_flock_capability(
    *,
    transport: str,
    node: str | None,
    tail_path: Path,
    filesystem: FilesystemIdentity,
) -> FlockCapability:
    if fcntl is None:
        return FlockCapability.UNAVAILABLE
    key = (
        transport,
        node,
        str(tail_path.expanduser().resolve(strict=False)),
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
    _FLOCK_CAPABILITY_CACHE[key] = capability
    return capability


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
    return SCRIPT_DIR.parent


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
) -> list[str]:
    """Terse post-dispatch status-tooling reminder (stderr only; path-not-payload)."""
    root = (skill_root or _skill_root()).resolve()
    status_path = Path(status_json).resolve()
    tail = Path(tail_path).resolve()
    status_py = root / "scripts" / "goalflight_status.py"
    watch_py = root / "scripts" / "goalflight_watch.py"
    lines = [
        f"[goal-flight] dispatched {dispatch_id} ({shape}). Check status with the python "
        "tooling — do NOT hand-roll ps/tail -f/backgrounded watchers (they race the worker "
        "and exit early):",
        f"  status: python3 {status_py} --dispatch {dispatch_id}",
        f"  wait:   python3 {status_py} --wait {dispatch_id}",
        f"  done?:  python3 {status_py} --done {dispatch_id}   "
        "# exit 0=terminal, 1=running, 2=ambiguous",
    ]
    if shape == "acp":
        lines.append(
            f"  watch:  python3 {watch_py} --pid {worker_pid} --tail {tail} "
            f"--status-json {status_path}"
        )
    else:
        # Kimi renderer normalization keys off the production preset label;
        # synthetic -bash-tail aliases are not first-class dispatch agents.
        agent_label = (
            "kimi"
            if agent == "kimi"
            else (f"{agent}-bash-tail" if agent else "worker-bash-tail")
        )
        watch_parts = [
            "bash",
            str(WATCH_TAIL_SH.resolve()),
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
        lines.append("  watch:  " + " ".join(watch_parts))
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
    return [m.group(1).lower() for m in GIT_BASE_PIN_RE.finditer(text or "")]


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
    if getattr(args, "ignore_git_warn", False) or not _prompt_requested(args):
        return None
    head = _git_head_for_cwd(_project_root(args))
    if not head:
        return None
    text = _read_prompt_for_guard(args)
    pins = _extract_git_base_pins(text)
    short_head = head[:7]
    if not pins:
        return (
            "WARN: prompt carries no git base pin; "
            f"HEAD is {short_head} - workers on stale clones will build on the wrong base; "
            f"add 'verify HEAD is {short_head}' or pass --ignore-git-warn"
        )
    mismatched_pins = [pin for pin in pins if not head.startswith(pin)]
    if not mismatched_pins:
        return None
    return (
        "WARN: GIT BASE PIN MISMATCH: "
        f"prompt pin {mismatched_pins[0]} does not match cwd HEAD {short_head}; "
        "stale brief or wrong repo state - workers on stale clones will build on the wrong base; "
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
    if raw_argv:
        return
    retired = RETIRED_AGENT_LABELS.get(args.agent)
    if retired:
        raise DispatchUsageError(
            f"--agent {args.agent!r} is retired — {retired}"
        )
    if args.agent not in PRESET_AGENTS:
        raise DispatchUsageError(
            "no worker preset for --agent "
            f"{args.agent!r} — use --agent codex|grok-code|grok-research|kimi with "
            "--prompt/--prompt-file, or pass a raw worker after `-- <cmd...>`"
        )
    if args.agent in STDIN_PROMPT_AGENTS and not _prompt_requested(args):
        raise DispatchUsageError(
            f"--agent {args.agent} requires --prompt or --prompt-file; refusing to feed empty stdin"
        )
    if args.prompt_file and not Path(args.prompt_file).expanduser().exists():
        raise DispatchUsageError(f"prompt file not found: {args.prompt_file}")
    _validate_os_sandbox_conflict(args)
    _validate_agent_os_sandbox(args)
    _guard_read_only_write_prompt(args)
    _guard_grok_code_research_prompt(args)


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
    for record in goalflight_ledger.read_records():
        if record.get("dispatch_id") == dispatch_id:
            return record
    return None


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
    for key in ("lstart", "comm"):
        if prior.get(key) and current.get(key) and prior[key] != current[key]:
            return (
                f"WARN: dispatch {dispatch_id} worker pid {pid} identity mismatch "
                f"({key}); message appended but may target stale state"
            )
    return None


def _parse_steer_lines(lines: list[str]) -> list[dict]:
    return goalflight_steer_mailbox.parse_steer_lines(lines)


def _read_steer_entries(path: Path) -> list[dict]:
    return goalflight_steer_mailbox.read_steer_entries(path)


def _append_steer_message(dispatch_id: str, text: str) -> tuple[Path, dict]:
    return goalflight_steer_mailbox.append_steer_message(dispatch_id, text)


def _acked_steer_seqs(record: dict) -> set[int]:
    return goalflight_steer_mailbox.acked_steer_seqs(record)


def _list_steer_messages(dispatch_id: str, record: dict) -> int:
    return goalflight_steer_mailbox.list_steer_messages(dispatch_id, record)


def _cmd_steer(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} steer",
        description="Append or list mailbox steers for an existing dispatch.",
    )
    parser.add_argument("dispatch_id")
    parser.add_argument("message", nargs="?")
    parser.add_argument("--list", action="store_true", dest="list_messages")
    args = parser.parse_args(argv)

    record = _find_dispatch_record(args.dispatch_id)
    if record is None:
        print(f"goalflight_dispatch: no ledger record for dispatch {args.dispatch_id}", file=sys.stderr)
        return 64

    if args.list_messages:
        return _list_steer_messages(args.dispatch_id, record)
    if args.message is None:
        print("goalflight_dispatch: steer requires a message or --list", file=sys.stderr)
        return 64

    shape = goalflight_ledger.infer_shape(record)
    if shape == "acp":
        warning = _worker_liveness_warning(record)
        if warning:
            print(warning, file=sys.stderr)
        path, entry = _append_steer_message(args.dispatch_id, args.message)
        print(f"steer appended: dispatch_id={args.dispatch_id} seq={entry['seq']} mailbox={path}")
        return 0
    if shape != "bash":
        print(f"goalflight_dispatch: dispatch {args.dispatch_id} has unsupported shape {shape!r}", file=sys.stderr)
        return 64

    warning = _worker_liveness_warning(record)
    if warning:
        print(warning, file=sys.stderr)
    path, entry = _append_steer_message(args.dispatch_id, args.message)
    print(f"steer appended: dispatch_id={args.dispatch_id} seq={entry['seq']} mailbox={path}")
    return 0


def _worker_prompt_preamble(agent: str | None, *, orientation_path: Path | None = None) -> str:
    preambles = [STEER_PROMPT_PREAMBLE, PROMPT_FILE_PREAMBLE]
    if orientation_path is not None:
        preambles.append(_project_orientation_preamble(orientation_path))
    if agent in {"grok-code", "grok-research", "kimi"}:
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
    full_prompt = f"{_worker_prompt_preamble(agent, orientation_path=orientation_path)}\n\n{body}"
    assembled = base / f"{dispatch_id}.assembled.prompt"
    assembled.parent.mkdir(parents=True, exist_ok=True)
    assembled.write_text(full_prompt, encoding="utf-8")
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


def _controller_pid(args) -> int:
    return int(args.controller_pid or os.getpid())


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


def _resolve_account_env(args) -> dict[str, str]:
    if not args.account:
        return {}
    engine = _account_engine(args.agent)
    if not engine:
        raise DispatchUsageError(
            f"--account is not configured for --agent {args.agent!r}; refusing to bill the wrong account"
        )
    home = _account_home(args.account, engine)
    if engine == "codex":
        if not home.exists():
            raise DispatchUsageError(
                f"--account {args.account} not configured (expected {home}). "
                "Set that account's creds there, or omit --account for the host default. "
                "Refusing to bill the wrong account."
            )
        return {"CODEX_HOME": str(home)}
    if not home.exists():
        raise DispatchUsageError(
            f"--account {args.account} not configured for {engine} (expected HOME {home}). "
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
                f"--account {args.account} lacks grok creds (expected non-empty {auth}). "
                "Refusing to bill the wrong account."
            )
        return {key: env[key] for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")}
    if engine == "cursor":
        env.pop("CURSOR_API_KEY", None)
        ok, reason = _cursor_account_probe(env)
        if not ok:
            raise DispatchUsageError(
                f"--account {args.account} lacks cursor creds ({reason}). "
                "Refusing to bill the wrong account."
            )
        return {key: env[key] for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")}
    raise DispatchUsageError(f"--account unsupported for engine {engine!r}")


_CODEX_SEAT_API_UNSET = object()
_CODEX_SEAT_API_CACHE = _CODEX_SEAT_API_UNSET


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
    """Resolve the queue budget: CLI flag > env > lane default."""
    return goalflight_capacity.resolve_capacity_wait_s(
        lane=getattr(args, "priority", None) or "normal",
        wait_s=getattr(args, "capacity_wait_s", None),
        log_prefix="goalflight_dispatch",
    )


def _acquire_capacity(args, *, project_root: Path, status_json: Path) -> str | None:
    lease_ttl_s = max(int(args.max_idle_secs or 300) * 4, 3600)
    acquire_args = argparse.Namespace(
        agent=args.agent,
        dispatch_id=args.dispatch_id,
        prompt_id=None,
        project_root=str(project_root),
        worktree_path=None,
        worker_cwd=str(project_root),
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
                "schema": "goalflight.status.v1",
                "dispatch_id": args.dispatch_id,
                "agent": args.agent,
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
                "schema": "goalflight.status.v1",
                "dispatch_id": args.dispatch_id,
                "agent": args.agent,
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
                   request_envelope: dict | None = None) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        goalflight_ledger.cmd_record(
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
                worker_pid=worker_pid,
                acp_session_id=None,
                logical_session_id=args.dispatch_id,
                lease_id=lease_id,
                stdout_path=str(tail),
                stderr_path=None,
                status_path=str(status_json),
                os_sandbox_json=json.dumps({"shape": "bash", "read_only": bool(args.read_only), "os_sandbox_profile": _effective_os_sandbox(args)}, sort_keys=True),
                queue_launch_token=getattr(args, "queue_launch_token", None),
                detached=bool(getattr(args, "launch_detached", False)),
                state=state,
                json=True,
            )
        )
    _export_dashboard_status_for_project(project_root)
    _upsert_project_registry_for_dispatch(project_root)
    if state in {"waiting_capacity", "starting", "running"}:
        _start_dashboard_refresh_for_project(project_root)


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
        "os_sandbox": {"shape": args.shape, "read_only": bool(args.read_only), "os_sandbox_profile": _effective_os_sandbox(args)},
        "state": "queued",
        "terminal_state": goalflight_ledger.terminal_state_for("queued"),
        "started_at": now,
        "hostname": socket.gethostname(),
    }
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
            "schema": "goalflight.status.v1",
            "dispatch_id": args.dispatch_id,
            "agent": args.agent,
            "shape": args.shape,
            "state": "queued",
            "reason": "dispatch_queue",
            "queue_path": str(queue_path),
            "project_root": str(project_root),
            "worker_pid": None,
            "worker_alive": False,
            "tail_path": str(tail),
            "updated_at": int(time.time()),
            **({"task_ids": list(args.task_ids)} if getattr(args, "task_ids", None) else {}),
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


def _insert_before_worker_remainder(argv: list[str], additions: list[str]) -> list[str]:
    try:
        split = argv.index("--")
    except ValueError:
        split = len(argv)
    return argv[:split] + additions + argv[split:]


def _remove_flag_before_worker_remainder(argv: list[str], flag: str) -> list[str]:
    try:
        split = argv.index("--")
    except ValueError:
        split = len(argv)
    return [part for part in argv[:split] if part != flag] + argv[split:]


def _set_option_before_worker_remainder(argv: list[str], flag: str, value: str) -> list[str]:
    try:
        split = argv.index("--")
    except ValueError:
        split = len(argv)
    head = argv[:split]
    tail = argv[split:]
    out: list[str] = []
    i = 0
    while i < len(head):
        if head[i] == flag:
            i += 2
            continue
        out.append(head[i])
        i += 1
    out += [flag, value]
    return out + tail


def _canonical_replay_argv(args, raw_argv: list[str], *, tail: Path, status_json: Path) -> list[str]:
    argv = [
        "--agent", str(args.agent),
        "--dispatch-id", str(args.dispatch_id),
        "--cwd", str(_project_root(args)),
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
    if args.controller_pid:
        argv += ["--controller-pid", str(args.controller_pid)]
    if raw_argv:
        argv += ["--", *raw_argv]
    return argv


def _existing_queue_entry_paths(queue_path: Path) -> list[Path]:
    paths = [queue_path] if queue_path.exists() else []
    paths.extend(
        path
        for path in sorted(queue_path.parent.glob(f"{queue_path.name}.claimed-*"))
        if not path.name.endswith(".failed")
    )
    return paths


def _queue_entry_counts_as_active(path: Path, entry: dict) -> bool:
    if path.name.endswith(".failed"):
        return False
    state = entry.get("state")
    return state in (None, "queued", "claimed")


def _cleanup_partial_submit(queue_path: Path, status_json: Path) -> None:
    with contextlib.suppress(OSError):
        queue_path.unlink()
    with contextlib.suppress(OSError):
        status_json.unlink()
    with contextlib.suppress(OSError):
        status_json.with_suffix(status_json.suffix + ".tmp").unlink()
    for tmp in queue_path.parent.glob(f"{queue_path.name}.tmp.*"):
        with contextlib.suppress(OSError):
            tmp.unlink()


def _test_submit_status_delay() -> None:
    raw = os.environ.get("GOALFLIGHT_TEST_SUBMIT_STATUS_DELAY_S")
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
    _warn_if_stranded_without_drainer(queue_path)


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
    print(
        "goalflight_dispatch: WARNING — request is STILL QUEUED after the "
        "drain-on-submit pass and no live drainer was detected, so nothing will "
        "launch it. (A peer drainer may still claim it momentarily.) Remedy:\n"
        "  python3 <skill-root>/scripts/goalflight_dispatch.py drain --json   # launch it now\n"
        "  bash <skill-root>/scripts/install-drainer.sh                       # restore the standing drainer",
        file=sys.stderr,
    )


def _queue_launch_token() -> str:
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


def _queue_claim_launcher_alive(entry: dict) -> bool:
    status, _reason = _queue_claim_identity_status(
        entry.get("queue_launcher_pid"),
        entry.get("queue_launcher_identity"),
    )
    # identity_indeterminate returns ok=True from identity_matches; treat as
    # alive for "still launching" protection so we never kill a maybe-live process.
    return status in {"live", "indeterminate"}


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


def _queue_claim_worker_alive(entry: dict) -> bool:
    status, _reason = _queue_claim_identity_status(
        entry.get("queue_worker_pid"),
        entry.get("queue_worker_identity"),
    )
    return status == "live"


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


def _queue_quarantine_dir(queue_dir: Path) -> Path:
    path = queue_dir / "quarantine"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _quarantine_claim(claim: Path, entry: dict, *, reason: str) -> Path | None:
    """Park an unlinked orphan claim out of the active launch glob. Never deletes."""
    queue_dir = claim.parent
    quarantine = _queue_quarantine_dir(queue_dir)
    entry = dict(entry)
    entry["state"] = "quarantined"
    entry["quarantine_reason"] = reason
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
    print(
        "CLAIM-RECOVERY-ALERT "
        + json.dumps(
            {
                "dispatch_id": entry.get("dispatch_id"),
                "action": "quarantine",
                "reason": reason,
                "path": str(dest),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
    return dest


def _persist_orphan_first_seen(entry: dict, *, now_iso: str | None = None) -> str:
    """Pure staged value helper; caller owns any carrier/ledger write."""
    if entry.get("orphan_first_seen_at"):
        return str(entry["orphan_first_seen_at"])
    return now_iso or goalflight_ledger.utc_now()


def _alert_identity_indeterminate(dispatch_id: str, *, where: str, reason: str) -> None:
    print(
        "CLAIM-RECOVERY-ALERT "
        + json.dumps(
            {
                "dispatch_id": dispatch_id,
                "action": "preserve",
                "reason": "identity_indeterminate",
                "where": where,
                "detail": reason,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )


def _alert_launched_carrier_pending(dispatch_id: str, *, where: str) -> None:
    print(
        "CLAIM-RECOVERY-ALERT "
        + json.dumps(
            {
                "dispatch_id": dispatch_id,
                "action": "preserve",
                "reason": "launched_carrier_cleanup_pending",
                "where": where,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
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
            "cwd": str(project_root),
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
    return {key: identity.get(key) for key in ("pid", "lstart", "comm") if identity.get(key)}


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
    pidfile = pidfile_dir / f"{controller_pid}.bashtail.{worker_pid}.jsonl"
    entry = {
        "controller_pid": controller_pid,
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

    Mirror of the ACP ``mark_connection_detached`` path. This dispatch process is
    EPHEMERAL: once a NON-terminal watcher exit returns (idle-timeout rc=2, or any
    exit with the worker still running for re-attach), the pidfile's recorded
    ``controller_pid`` is this soon-to-exit pid. Without a ``detached`` flag the
    next ``cleanup_ghosts`` sweep -- including a sibling project sharing the pidfile
    dir -- would see dead-controller + live-worker and SIGKILL the live worker's
    group, losing its uncommitted work. ``detached: true`` makes cleanup_ghosts
    SKIP it (exactly as it skips an intentionally-detached ACP worker), without
    weakening genuine-ghost reaping (dead controller + dead worker, not detached).
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
        # it detached so cleanup_ghosts protects this live worker after we exit
        # (symmetry with the ACP mark_connection_detached path). Without this, the
        # un-flagged pidfile + this ephemeral process's now-dead pid = a live worker
        # that the next ghost sweep would SIGKILL.
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
        goalflight_ledger.cmd_finish(
            argparse.Namespace(
                dispatch_id=dispatch_id,
                state=state,
                reason=reason,
                terminal_state=None,
                elapsed_s=elapsed_s,
                worker_still_alive=worker_still_alive,
            )
        )


def _release_capacity(lease_id: str | None, state: str, reason: str | None) -> None:
    if not lease_id:
        return
    with contextlib.redirect_stdout(io.StringIO()):
        goalflight_capacity.cmd_release(argparse.Namespace(lease_id=lease_id, state=state, reason=reason, keep=True))


def _release_stale_capacity_for_drain() -> None:
    with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
        goalflight_capacity.cmd_release_stale(
            argparse.Namespace(state="expired", reason="drain_stale_worker", keep=True)
        )


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


def _claim_has_active_carrier(queue_dir: Path, dispatch_id: str) -> bool:
    """True if a canonical queue envelope or live claim still exists for dispatch_id."""
    if not dispatch_id:
        return False
    safe = goalflight_compat.safe_dispatch_filename(dispatch_id)
    # Canonical name is usually "<dispatch_id>.json" but may be priority-prefixed.
    for path in queue_dir.glob("*.json"):
        if path.name.endswith(".failed"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if path.stem == dispatch_id or path.stem == safe:
                return True
            continue
        if isinstance(payload, dict) and str(payload.get("dispatch_id") or "") == dispatch_id:
            return True
    for claim in queue_dir.glob("*.json.claimed-*"):
        if claim.name.endswith(".failed"):
            continue
        try:
            payload = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if claim.name.split(".claimed-", 1)[0].removesuffix(".json") == dispatch_id:
                return True
            continue
        if isinstance(payload, dict) and str(payload.get("dispatch_id") or "") == dispatch_id:
            return True
    return False


def _scan_entry_completion_marker(entry: dict) -> dict | None:
    """Best-effort terminal-marker scan from claim/request tail path."""
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    dispatch_id = str(entry.get("dispatch_id") or "")
    tail_raw = request.get("tail")
    if not tail_raw and dispatch_id:
        tail_raw = str(_dispatch_base_dir() / f"{dispatch_id}.tail")
    if not tail_raw:
        return None
    tail = Path(str(tail_raw))
    prompt = request.get("prompt_file") or entry.get("prompt_file")
    ignore_prefix_lines = _ignore_prefix_lines(str(Path(prompt).expanduser()) if prompt else None)
    try:
        return _final_terminal_marker(
            tail,
            ignore_prefix_lines=ignore_prefix_lines,
            suppress_unfenced_prompt_markers=True,
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


def _ledger_task_ids_advanced(task_ids: list[str], *, self_dispatch_id: str) -> tuple[int, int, bool]:
    """Return counts plus whether ledger authority was read conclusively."""
    if not task_ids:
        return 0, 0, True
    wanted = set(task_ids)
    complete_tasks: set[str] = set()
    advanced_tasks: set[str] = set()
    try:
        records = goalflight_ledger.read_records()
    except Exception:
        return 0, 0, False
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("dispatch_id") or "") == self_dispatch_id:
            continue
        rec_ids = set(_entry_task_ids(None, record))
        overlap = wanted & rec_ids
        if not overlap:
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
    return len(complete_tasks), len(advanced_tasks), True


def _linked_task_truth(
    entry: dict,
    record: dict | None = None,
    *,
    task_store_locked: bool = False,
) -> str:
    """Return all_complete | some_advanced | none | indeterminate | unlinked."""
    task_ids = _entry_task_ids(entry, record)
    if not task_ids:
        return "unlinked"
    project_root = _project_root_for_entry(entry, record)
    store_complete = 0
    store_seen = 0
    store_conclusive = True
    try:
        root = SCRIPT_DIR.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import goalflight_task  # type: ignore

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
                store_complete += 1
    except Exception:
        store_complete = 0
        store_seen = 0
        store_conclusive = False

    self_id = str(entry.get("dispatch_id") or (record or {}).get("dispatch_id") or "")
    ledger_complete, ledger_advanced, ledger_conclusive = _ledger_task_ids_advanced(
        task_ids,
        self_dispatch_id=self_id,
    )

    # Prefer explicit store truth when every linked id is present and complete.
    if store_seen == len(task_ids) and store_complete == len(task_ids):
        return "all_complete"
    if ledger_complete >= len(task_ids):
        return "all_complete"
    # Mix of store + ledger completions covering all ids.
    if store_complete + ledger_complete >= len(task_ids) and (store_complete > 0 or ledger_complete > 0):
        # Conservative: only when store_complete alone is full, or ledger alone.
        # Partial overlap of different ids is handled by some_advanced below.
        covered = store_complete  # already counted store-complete ids
        if covered >= len(task_ids):
            return "all_complete"

    advanced = max(store_complete, ledger_advanced, ledger_complete)
    # Also treat partial store completion as advanced.
    if store_complete > 0 and store_complete < len(task_ids):
        return "some_advanced"
    if 0 < advanced < len(task_ids) or (ledger_advanced > 0 and ledger_complete < len(task_ids)):
        return "some_advanced"
    if store_complete >= len(task_ids) or ledger_complete >= len(task_ids):
        return "all_complete"
    if not store_conclusive or not ledger_conclusive:
        return "indeterminate"
    return "none"


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
                "reason": "ledger_authority_indeterminate",
                "marker": None,
                "source": "authority",
            }

    # Leg 0: already-terminal ledger for this dispatch (first-terminal-wins).
    has_terminal_record = bool(dispatch_id and _dispatch_record_is_terminal(record))
    if has_terminal_record:
        existing = record or _find_dispatch_record(dispatch_id) or {}
        existing_state = str(existing.get("state") or existing.get("terminal_state") or "")
        if existing_state in {
            "complete",
            "superseded",
            "worker_dead",
        } | set(goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES):
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
    task_truth = _linked_task_truth(entry, record, task_store_locked=task_store_locked)
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
        return {
            "state": "deferred",
            "reason": "completion_authority_indeterminate",
            "marker": None,
            "source": "authority",
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
    } | set(goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES)


def _completion_decision_is_deferred(decision: dict | None) -> bool:
    return isinstance(decision, dict) and decision.get("state") == "deferred"


@contextlib.contextmanager
def _task_store_mutation_lock(entry: dict, record: dict | None = None):
    """Freeze linked task truth while a completion decision is persisted."""
    if not _entry_task_ids(entry, record):
        yield
        return
    root = SCRIPT_DIR.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import goalflight_task  # type: ignore

    store = goalflight_task.TaskStore(_project_root_for_entry(entry, record))
    with store.store_lock():
        yield


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
                return AcquireResult.DEFER_UNCHANGED
            acquired.append(handle)
            txn.queue_locked = True
        if need_task_store:
            handle = try_acquire_task_store_lock(txn.entry, record, deadline_s=deadline)
            if handle is None:
                return AcquireResult.DEFER_UNCHANGED
            acquired.append(handle)
            txn.task_store_locked = True
        if need_ledger:
            handle = try_acquire_ledger_lock(deadline_s=deadline)
            if handle is None:
                return AcquireResult.DEFER_UNCHANGED
            acquired.append(handle)
            txn.ledger_locked = True
        txn.downstream.extend(acquired)
        return AcquireResult.ACQUIRED_ALL
    except Exception:
        return AcquireResult.DEFER_UNCHANGED
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
) -> _ReconcileTransaction | None:
    admission = admission or classify_reconciliation_admission(entry, time.time(), stale_s=stale_s)
    if ADMISSION_DECISION[admission] is not AdmissionAction.ADMIT_TO_GATE:
        return None
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
        return None
    txn = _ReconcileTransaction(entry, tail, admission, mode, filesystem, capability)
    gate = _tail_reconciliation_lock(tail) if mode is ReconciliationMode.LOCAL_FLOCK else _fallback_reconciliation_lock(tail)
    try:
        gate.__enter__()
    except (_TailLockBusy, OSError):
        return None
    txn.tail_gate = gate
    if acquire_reconcile_locks(
        txn,
        queue_dir=queue_dir,
        need_queue=need_queue,
        need_task_store=need_task_store,
        need_ledger=need_ledger,
    ) is not AcquireResult.ACQUIRED_ALL:
        txn.release()
        return None
    return txn


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


def _apply_completion_authority(
    entry: dict,
    decision: dict,
    *,
    claim: Path | None = None,
) -> str:
    """Compatibility entry routed through the single transaction owner."""
    if claim is not None:
        return _reconcile_claim_transaction(
            claim,
            entry,
            queue_dir=claim.parent,
            reason=str(decision.get("reason") or "completion_authority"),
            stale_s=0.0,
        )
    state = str(decision.get("state") or "complete")
    reason = decision.get("reason") or "completion_authority"
    if state in goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES or state in {
        "complete",
        "superseded",
        "worker_dead",
    }:
        # Reuse terminalize path (scans again + first-terminal-wins). For
        # superseded/complete we still go through _mark_claim_worker_dead which
        # re-reads the tail; override via reason + finish lock recheck.
        persisted = _mark_claim_worker_dead(
            entry,
            reason=str(reason),
            force_state=state if state in {"complete", "superseded"} else None,
            salvage_required=bool(decision.get("salvage_required")),
            resolution_source=decision.get("resolution_source"),
            claim=claim,
        )
        if not persisted:
            return "pending"
    return "cleared"


def _entry_pre_spawn(entry: dict) -> bool:
    """True when the claim never crossed launch-start / spawn-intent / spawn."""
    return not (
        _queue_claim_launch_started(entry)
        or _queue_claim_worker_spawn_intent(entry)
        or _queue_claim_worker_spawned(entry)
    )


def _sanitize_restore_envelope(entry: dict, *, increment_recovery_count: bool) -> dict:
    restored = dict(entry)
    for key in (
        "queue_launch_token",
        "queue_launch_started",
        "queue_launch_started_at",
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
    ):
        restored.pop(key, None)
    if increment_recovery_count:
        restored["claim_recovery_count"] = _claim_recovery_count(entry) + 1
    return restored


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
        return None, decision
    if _completion_decision_blocks_restore(decision):
        if target.exists():
            try:
                abandoned = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None, None
            if (
                isinstance(abandoned, dict)
                and abandoned.get("state") in {"restore_prepared", "queued"}
                and abandoned.get("dispatch_id") == fresh.get("dispatch_id")
                and abandoned.get("restore_txn_id")
            ):
                try:
                    target.unlink()
                except OSError:
                    return None, None
        return None, decision
    if (
        record is not None
        and _dispatch_record_is_terminal(record)
        and str(record.get("state") or "") not in {"blocked_capacity"}
    ):
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
                claim.unlink()
            except OSError:
                return None, None
            return target, None
        if prepared.get("state") != "restore_prepared":
            return None, None
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
    # ledger commit point, complete that same transaction id now. A different
    # queued transaction is not ours and must remain untouched.
    if not record or record.get("restore_txn_id") != restore_txn_id or record.get("state") != "queued":
        ledger_record = record or _new_reconciliation_record(fresh)
        if (
            _dispatch_record_is_terminal(ledger_record)
            and str(ledger_record.get("state") or "") not in {"blocked_capacity"}
        ):
            with contextlib.suppress(OSError):
                target.unlink()
            return None, {
                "state": str(ledger_record.get("state") or ledger_record.get("terminal_state")),
                "reason": "existing_terminal_record",
                "source": "ledger",
            }
        if record is not None and record.get("state") == "queued" and record.get("restore_txn_id"):
            return None, None
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
        try:
            goalflight_ledger.write_record(ledger_record)
        except Exception:
            return None, None

    # Durable ledger row above is the cross-file commit point.
    try:
        published = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(published, dict) or published.get("restore_txn_id") != restore_txn_id:
            return None, None
        published["state"] = "queued"
        published["updated_at"] = goalflight_ledger.utc_now()
        _write_json_atomic(target, published)
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
    txn = _begin_reconcile_transaction(
        entry,
        queue_dir=queue_dir,
        stale_s=0.0,
        need_queue=True,
        need_task_store=_is_task_linked(entry, _find_dispatch_record(str(entry.get("dispatch_id") or ""))),
        need_ledger=True,
    )
    if txn is None:
        return False, None
    try:
        try:
            fresh = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, None
        if not isinstance(fresh, dict):
            return False, None
        if _claim_recovery_count(fresh) >= MAX_CLAIM_RECOVERY_REQUEUES or not _entry_pre_spawn(fresh):
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
) -> tuple[Path | None, dict | None]:
    """Normal-drain restore through the same held T→Q→S→L transaction."""
    try:
        observed = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(observed, dict) or observed.get("queue_launch_token") != entry.get("queue_launch_token"):
        return None, None
    txn = _begin_reconcile_transaction(
        observed,
        queue_dir=queue_dir,
        stale_s=0.0,
        need_queue=True,
        need_task_store=_is_task_linked(
            observed,
            _find_dispatch_record(str(observed.get("dispatch_id") or "")),
        ),
        need_ledger=True,
    )
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
                return None, None
            if not _is_task_linked(fresh, _find_dispatch_record(str(fresh.get("dispatch_id") or ""))):
                if _quarantine_claim(claim, fresh, reason="normal_restore_completion_unlinked"):
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
    if force_state in {"complete", "superseded"} and state == "worker_dead":
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
    project_root = Path(str(entry.get("project_root") or request.get("cwd") or Path.cwd())).resolve()
    status_json = Path(str(request.get("status_json") or record.get("status_path") or _dispatch_base_dir() / f"{dispatch_id}.status.json"))
    tail = Path(str(request.get("tail") or record.get("stdout_path") or _dispatch_base_dir() / f"{dispatch_id}.tail"))
    state = str(record.get("state") or record.get("terminal_state") or "worker_dead")
    reason = record.get("reason") or record.get("error") or "claim_reconciliation"
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


def _positive_live_carrier_cleanup(
    claim: Path,
    entry: dict,
    queue_dir: Path,
    *,
    worker_record_sufficient: bool = False,
) -> str:
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
        return "pending"
    handle = try_acquire_queue_lock(
        queue_dir,
        deadline_s=time.monotonic() + RECONCILE_DOWNSTREAM_LOCK_BUDGET_S,
    )
    if handle is None:
        return "pending"
    try:
        try:
            fresh = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "pending"
        if not isinstance(fresh, dict):
            return "pending"
        if not worker_record_sufficient and classify_reconciliation_admission(
            fresh, time.time(), stale_s=0.0
        ) not in (
            PreAdmitClass.LIVE,
            PreAdmitClass.REMOTE_AUTHORITY_REQUIRED,
        ):
            return "pending"
        if fresh.get("queue_launch_token") != token:
            return "pending"
        if not (
            _dispatch_has_worker_record(
                dispatch_id,
                queue_launch_token=token,
                require_live_nonterminal=not worker_record_sufficient,
            )
            or _fleet_terminal_accounted_record(_find_dispatch_record(dispatch_id), token)
        ):
            return "pending"
        if not _is_task_linked(fresh, _find_dispatch_record(dispatch_id)):
            return "quarantined" if _quarantine_claim(
                claim,
                fresh,
                reason="live_worker_unlinked_claim_carrier",
            ) else "pending"
        claim.unlink()
        return "cleared"
    except OSError:
        return "pending"
    finally:
        handle.release()


def _reconcile_claim_transaction(
    claim: Path,
    entry: dict,
    *,
    queue_dir: Path,
    reason: str,
    stale_s: float,
) -> str:
    admission = classify_reconciliation_admission(entry, time.time(), stale_s=stale_s)
    if admission is PreAdmitClass.LIVE:
        return _positive_live_carrier_cleanup(claim, entry, queue_dir)
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
            return _positive_live_carrier_cleanup(claim, entry, queue_dir)
        return "pending"

    record = _find_dispatch_record(str(entry.get("dispatch_id") or ""))
    linked = _is_task_linked(entry, record)
    txn = _begin_reconcile_transaction(
        entry,
        queue_dir=queue_dir,
        stale_s=stale_s,
        need_queue=True,
        need_task_store=linked,
        need_ledger=linked or record is not None,
        admission=admission,
    )
    if txn is None:
        return "pending"
    mirror: tuple[dict, Path] | None = None
    terminal_mirror: tuple[dict, dict | None] | None = None
    try:
        try:
            fresh = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "pending"
        if not isinstance(fresh, dict) or not _reconcile_transaction_still_valid(txn, fresh):
            return "pending"
        if classify_reconciliation_admission(fresh, time.time(), stale_s=stale_s) is not admission:
            return "pending"
        record = _find_dispatch_record(str(fresh.get("dispatch_id") or ""))
        linked = _is_task_linked(fresh, record)
        carrier_stamp = str(fresh.get("orphan_first_seen_at") or "") or None
        if carrier_stamp and record is not None and not record.get("orphan_first_seen_at"):
            if _stamp_ledger_orphan_first_seen(record, txn=txn, stamp=carrier_stamp) is None:
                return "pending"
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
                return "pending"
            if record is not None and txn.ledger_locked:
                if _stamp_ledger_orphan_first_seen(record, txn=txn, stamp=stamp) is None:
                    return "pending"
            return "pending"
        if age_stamp is not None and max(0.0, time.time() - age_stamp) < max(0.0, stale_s):
            return "pending"
        if record is not None and _dispatch_record_is_terminal(record):
            if not linked:
                return "quarantined" if _quarantine_claim(
                    claim,
                    fresh,
                    reason=f"{reason}_unlinked_terminal",
                ) else "pending"
            try:
                claim.unlink()
            except OSError:
                return "pending"
            return "cleared"

        decision = _entry_completion_authority(
            fresh,
            record,
            task_store_locked=txn.task_store_locked,
        )
        if _completion_decision_blocks_restore(decision):
            if _completion_decision_is_deferred(decision):
                return "pending"
            result, _marker = _commit_claim_terminal_in_txn(
                txn,
                fresh,
                reason=str(decision.get("reason") or reason),
                force_state=str(decision.get("state") or "complete"),
                salvage_required=bool(decision.get("salvage_required")),
                resolution_source=decision.get("resolution_source"),
            )
            if not result.committed:
                return "pending"
            terminal_mirror = (fresh, _marker)
            if not linked:
                return "quarantined" if _quarantine_claim(
                    claim,
                    fresh,
                    reason=f"{reason}_unlinked_completion",
                ) else "pending"
            try:
                claim.unlink()
            except OSError:
                return "pending"
            return "cleared"

        if not linked:
            return "quarantined" if _quarantine_claim(claim, fresh, reason=f"{reason}_unlinked") else "pending"

        target = queue_dir / claim.name.split(".claimed-", 1)[0]
        pre_spawn = _entry_pre_spawn(fresh)
        if pre_spawn and list(fresh.get("dispatch_argv") or []) and _claim_recovery_count(fresh) < MAX_CLAIM_RECOVERY_REQUEUES:
            restored, locked_decision = _restore_claimed_entry(
                claim,
                fresh,
                txn=txn,
                increment_recovery_count=True,
                reason=reason,
            )
            if restored is not None:
                mirror = (_sanitize_restore_envelope(fresh, increment_recovery_count=True), restored)
                return "restored"
            if locked_decision is not None:
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
                    return "cleared"
                return "pending"
        if target.exists():
            try:
                claim.unlink()
            except OSError:
                return "pending"
            return "cleared"

        terminal_reason = (
            "claim_recovery_exhausted"
            if pre_spawn and _claim_recovery_count(fresh) >= MAX_CLAIM_RECOVERY_REQUEUES
            else reason
        )
        result, _marker = _commit_claim_terminal_in_txn(txn, fresh, reason=terminal_reason)
        if not result.committed:
            return "pending"
        terminal_mirror = (fresh, _marker)
        try:
            claim.unlink()
        except OSError:
            return "pending"
        return "cleared"
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
    )


def _recover_claimed_queue_entries(queue_dir: Path, *, stale_s: float) -> dict:
    """Run each carrier through the single PRE-ADMIT→T→Q→S→L owner."""
    restored = 0
    cleared = 0
    pending_launch = 0
    quarantined = 0
    for claim in sorted(queue_dir.glob("*.json.claimed-*")):
        if claim.name.endswith(".failed"):
            continue
        try:
            entry = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        action = _reconcile_claim_transaction(
            claim,
            entry,
            queue_dir=queue_dir,
            reason=(
                "stale_claim_pre_spawn"
                if _entry_pre_spawn(entry)
                else "stale_claim_launch_token_lost"
            ),
            stale_s=stale_s,
        )
        if action == "restored":
            restored += 1
        elif action == "cleared":
            cleared += 1
        elif action == "quarantined":
            quarantined += 1
        else:
            pending_launch += 1

    ledger_stats = _reconcile_ledger_prelaunch_orphans(queue_dir, stale_s=stale_s, now=time.time())
    ledger_terminalized = int(ledger_stats.get("terminalized") or 0)
    pending_launch += int(ledger_stats.get("pending") or 0)
    quarantined += int(ledger_stats.get("quarantined") or 0)
    # cleared includes carriers removed after terminalization; quarantined is
    # reported separately so callers can distinguish park-vs-delete without
    # breaking older exact-dict assertions that only key restored/cleared/pending.
    return {
        "restored": restored,
        "cleared": cleared,
        "pending_launch": pending_launch,
        "quarantined": quarantined,
        "ledger_terminalized": ledger_terminalized,
    }


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
                or record.get("started_at")
            ),
            "queue_launch_started": bool(
                record.get("worker_pid")
                or str(record.get("state") or "")
                not in {"queued", "waiting_capacity", "submitted"}
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
            "dispatch_argv": list(envelope.get("dispatch_argv") or []),
            "claim_recovery_count": int(
                record.get("claim_recovery_count")
                or MAX_CLAIM_RECOVERY_REQUEUES
            ),
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
    child_id = intent.get("child_id") if isinstance(intent, dict) else None
    return not (
        isinstance(child_id, str)
        and child_id
        and _requeue_child_exists(queue_dir, child_id)
    )


def _reconcile_ledger_prelaunch_orphans(
    queue_dir: Path,
    *,
    stale_s: float,
    now: float | None = None,
) -> dict:
    """Reconcile carrier-less rows with the same typed admission and held gate."""
    now_s = time.time() if now is None else float(now)
    terminalized = 0
    pending = 0
    quarantined = 0
    try:
        records = goalflight_ledger.read_records()
    except Exception:
        return {"terminalized": 0, "pending": 0, "quarantined": 0}

    for record in records:
        if not isinstance(record, dict):
            continue
        dispatch_id = str(record.get("dispatch_id") or "")
        if not dispatch_id:
            continue
        entry = _ledger_request_entry(record)
        if _dispatch_record_is_terminal(record):
            if (
                not _claim_has_active_carrier(queue_dir, dispatch_id)
                and _is_task_linked(entry, record)
                and _terminal_ledger_requeue_pending(
                    record,
                    entry,
                    queue_dir=queue_dir,
                )
            ):
                if not _mark_claim_worker_dead(
                    entry,
                    reason="watcher_terminal_reconcile",
                    queue_dir=queue_dir,
                    stale_s=stale_s,
                ):
                    pending += 1
            continue
        state = str(record.get("state") or "")
        if state not in PRELAUNCH_CANDIDATE_STATES and state not in {"running", "running_quiet"}:
            # running with dead worker + no carrier is also a post-spawn orphan.
            if state not in {"running", "running_quiet", "starting", "queued", "waiting_capacity", "submitted"}:
                continue
        if record.get("transport") == "fleet-ssh":
            continue
        if _claim_has_active_carrier(queue_dir, dispatch_id):
            continue

        admission = classify_reconciliation_admission(entry, now_s, stale_s=stale_s)
        if admission is PreAdmitClass.INDETERMINATE:
            _alert_identity_indeterminate(dispatch_id, where="ledger", reason="pre_admit_indeterminate")
            pending += 1
            continue
        if ADMISSION_DECISION[admission] is AdmissionAction.DEFER_UNCHANGED:
            pending += 1
            continue
        linked = _is_task_linked(entry, record)
        age_stamp = _launch_age_timestamp_s(entry)
        if age_stamp is None:
            txn = _begin_reconcile_transaction(
                entry,
                queue_dir=queue_dir,
                stale_s=stale_s,
                need_queue=False,
                need_task_store=False,
                need_ledger=True,
                admission=admission,
            )
            if txn is None:
                pending += 1
                continue
            try:
                stamped = _stamp_ledger_orphan_first_seen(record, txn=txn)
            finally:
                txn.release()
            if stamped is None:
                pending += 1
                continue
            pending += 1
            continue
        if max(0.0, now_s - age_stamp) < max(0.0, stale_s):
            pending += 1
            continue
        if linked:
            if _mark_claim_worker_dead(
                entry,
                reason="claim_carrier_missing",
                queue_dir=queue_dir,
                stale_s=stale_s,
            ):
                terminalized += 1
            else:
                pending += 1
        else:
            # Unlinked ledger-only zombie: alert, do not auto-terminalize (E).
            print(
                "CLAIM-RECOVERY-ALERT "
                + json.dumps(
                    {
                        "dispatch_id": dispatch_id,
                        "action": "preserve_unlinked_ledger_orphan",
                        "reason": "claim_carrier_missing_unlinked",
                        "state": state,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
            quarantined += 1
    return {"terminalized": terminalized, "pending": pending, "quarantined": quarantined}


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
    except Exception:
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


def _queue_entry_not_before_ts(entry: dict | None) -> float | None:
    if not isinstance(entry, dict):
        return None
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    raw = entry.get("not_before") or request.get("not_before")
    parsed = goalflight_ledger.parse_utc(raw)
    return parsed.timestamp() if parsed is not None else None


def _queue_entry_drain_candidate(path: Path) -> tuple[tuple[int, float, str], Path, dict | None, str | None]:
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


def _mark_claim_failed_locked(claim: Path, entry: dict, *, reason: str) -> None:
    """Pre-spawn failure write; caller owns Q."""
    fresh = dict(entry)
    fresh["state"] = "failed"
    fresh["reason"] = reason
    fresh["updated_at"] = goalflight_ledger.utc_now()
    failed = claim.with_name(f"{claim.name}.failed")
    _write_json_atomic(failed, fresh)
    with contextlib.suppress(OSError):
        claim.unlink()


def _mark_claim_failed(claim: Path, entry: dict, *, reason: str) -> None:
    with _queue_mutation_lock(claim.parent):
        try:
            fresh = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fresh = dict(entry)
        if fresh.get("queue_launch_token") != entry.get("queue_launch_token"):
            return
        _mark_claim_failed_locked(claim, fresh, reason=reason)


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
    argv = _set_option_before_worker_remainder(
        list(dispatch_argv),
        "--capacity-wait-s",
        str(max(0.0, float(capacity_wait_s))),
    )
    additions = ["--from-queue"]
    if queue_launch_token:
        additions += ["--queue-launch-token", queue_launch_token]
    if queue_claim_path is not None:
        additions += ["--queue-claim-path", str(queue_claim_path)]
    additions.append("--launch-detached")
    return _insert_before_worker_remainder(argv, additions)


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
    project_root = Path(str(entry.get("project_root") or request.get("cwd") or Path.cwd())).resolve()
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
        try:
            return _final_terminal_marker(
                tail,
                ignore_prefix_lines=ignore_prefix_lines,
                suppress_unfenced_prompt_markers=True,
                kimi_output=agent == "kimi",
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
    return {
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
        return TerminalCommitResult(
            TerminalCommitKind.EXISTING_TERMINAL,
            str(record.get("state") or existing_terminal),
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

    ended_at = goalflight_ledger.utc_now()
    record.update(
        {
            "state": state,
            "ended_at": ended_at,
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
    try:
        goalflight_ledger.write_record(record)
    except Exception:
        return TerminalCommitResult(TerminalCommitKind.DEFERRED, None, False)
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
    if state == "rate_limited":
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
    if queue_path.exists() or any(
        queue_path.parent.glob(f"{queue_path.name}.claimed-*")
    ):
        return True
    return goalflight_ledger.record_path(child_id, create=False).exists()


def _requeue_child_entry(
    entry: dict,
    *,
    child_id: str,
    requeued_from: str,
    queue_dir: Path,
    not_before: str | None,
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
            "created_at": now,
            "updated_at": now,
            "queue_path": str(queue_path),
            "dispatch_argv": dispatch_argv,
            "request": request,
            "requeued_from": requeued_from,
        }
    )
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
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return True
    if not isinstance(record, dict):
        return True

    intent = record.get("requeue")
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
        except BaseException as exc:
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

    failure_kind = _requeue_failure_kind(record, tail)
    effective_account = record.get("effective_account")
    not_before = (
        _effective_account_cooldown(effective_account)
        if failure_kind == "quota" and isinstance(effective_account, str)
        else None
    )
    child = _requeue_child_entry(
        entry,
        child_id=child_id,
        requeued_from=dispatch_id,
        queue_dir=queue_dir,
        not_before=not_before,
    )
    if child is None:
        return False
    child_path = _queue_entry_path(child_id, queue_dir=queue_dir)
    try:
        created = _write_json_exclusive(child_path, child)
    except BaseException as exc:
        print(
            "goalflight_dispatch: requeue child warning: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return False
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
    project_root = Path(str(entry.get("project_root") or request.get("cwd") or Path.cwd())).resolve()
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
    if force_state in {"complete", "superseded"} and state == "worker_dead":
        state = force_state
        final_reason = reason
    queue_dir = (
        claim.parent
        if claim is not None
        else queue_dir or _dispatch_queue_dir()
    )
    txn = _begin_reconcile_transaction(
        entry,
        queue_dir=queue_dir,
        stale_s=stale_s,
        need_queue=True,
        need_task_store=_is_task_linked(entry, _find_dispatch_record(dispatch_id)),
        need_ledger=True,
    )
    if txn is None:
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


def _drain_queue_once(args) -> dict:
    queue_dir = Path(args.queue_dir).expanduser() if args.queue_dir else _dispatch_queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    remote_node = _remote_drain_node(args)
    if remote_node:
        _validate_remote_drain_node(args)
        if getattr(args, "remote_runner", None) is None:
            try:
                import goalflight_fleet_dispatch as fleet_dispatch

                fleet_dispatch.assert_live_ssh_opt_in()
            except Exception as exc:
                raise _RemoteDrainBlocked(str(exc), code="live_ssh_required") from exc
    else:
        _release_stale_capacity_for_drain()
    recovery = _recover_claimed_queue_entries(queue_dir, stale_s=args.claim_stale_s)
    launched = 0
    left_queued = 0
    failed = 0
    pending_claims = 0
    details: list[dict] = []
    entries = sorted(
        candidate
        for path in queue_dir.glob("*.json")
        for candidate in (_queue_entry_drain_candidate(path),)
        if not (
            isinstance(candidate[2], dict)
            and candidate[2].get("state") == "restore_prepared"
        )
    )
    now_s = time.time()
    deferred_entries = [
        candidate
        for candidate in entries
        if (
            _queue_entry_not_before_ts(candidate[2]) is not None
            and _queue_entry_not_before_ts(candidate[2]) > now_s
        )
    ]
    entries = [candidate for candidate in entries if candidate not in deferred_entries]
    left_queued += len(deferred_entries)
    for _sort_key, path, entry, _read_error in deferred_entries:
        details.append(
            {
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
        )
    if args.limit and args.limit > 0:
        entries = entries[: args.limit]
    if not remote_node and entries:
        _run_drain_prelaunch_hook(_pass_agent_labels(entries))
    for _sort_key, path, _scan_entry, _scan_read_error in entries:
        claim_error: Exception | None = None
        entry: dict | None = None
        launch_token = _queue_launch_token()
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
                    entry["updated_at"] = goalflight_ledger.utc_now()
                    try:
                        _write_json_atomic(claim, entry)
                    except OSError as exc:
                        claim_error = exc
        if claim is None:
            continue
        if claim_error is not None or entry is None:
            failed += 1
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
        launch_argv = _drain_launch_argv(
            list(entry.get("dispatch_argv") or []),
            capacity_wait_s=args.capacity_wait_s,
            queue_launch_token=launch_token,
            queue_claim_path=claim,
        )
        if not launch_argv:
            failed += 1
            _mark_claim_failed(claim, entry, reason="missing_dispatch_argv")
            continue
        timeout_s = max(20.0, float(args.capacity_wait_s or 0.0) + 45.0)
        try:
            if remote_node:
                proc = _drain_launch_remote_claim(
                    args,
                    entry,
                    dispatch_id=dispatch_id,
                    launch_token=launch_token,
                    claim=claim,
                )
            else:
                proc = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), *launch_argv],
                    cwd=str(Path(entry.get("process_cwd") or entry.get("project_root") or Path.cwd()).resolve()),
                    env=os.environ.copy(),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout_s,
                )
        except _RemoteDrainBlocked as exc:
            restored, decision = _restore_claim_if_incomplete(claim, entry, queue_dir)
            if decision is not None:
                details.append(
                    {
                        "dispatch_id": dispatch_id,
                        "state": str(decision.get("state") or "complete"),
                        "reason": str(decision.get("reason") or "completion_authority"),
                    }
                )
                continue
            if restored is None:
                pending_claims += 1
                failed += 1
                details.append(
                    {
                        "dispatch_id": dispatch_id,
                        "state": "claimed",
                        "reason": "remote_blocked_restore_raced",
                    }
                )
                continue
            _restore_queued_record_from_entry(entry, restored)
            left_queued += 1
            details.append(
                {
                    "dispatch_id": dispatch_id,
                    "state": "queued",
                    "reason": f"remote_blocked:{exc.code}:{exc}",
                }
            )
            continue
        except subprocess.TimeoutExpired:
            pending_claims += 1
            failed += 1
            details.append(
                {
                    "dispatch_id": dispatch_id,
                    "state": "claimed",
                    "reason": "launch_timeout_pending_ledger",
                }
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
        no_capacity = proc.returncode == 2 and "blocked_capacity" in (proc.stdout + proc.stderr)
        if stdout_launched and ledger_confirmed:
            carrier_cleanup = _positive_live_carrier_cleanup(
                claim,
                entry,
                queue_dir,
                # This drain just ledger-confirmed the launch (token-matched
                # record), so the carrier is redundant even if a fast worker
                # already exited; reconcile terminalizes from the ledger.
                worker_record_sufficient=True,
            )
            if carrier_cleanup == "pending":
                # Launch is durably ledger-confirmed, but the carrier could
                # not be cleared: surface it instead of silently reporting
                # success. "launched" means launched AND carrier resolved, so
                # a pending cleanup is counted as pending only — never as a
                # launch.
                pending_claims += 1
                _alert_launched_carrier_pending(dispatch_id, where="drain")
                details.append(
                    {
                        "dispatch_id": dispatch_id,
                        "state": "claimed",
                        "reason": "launched_carrier_cleanup_pending",
                    }
                )
            else:
                launched += 1
                details.append({"dispatch_id": dispatch_id, "state": "launched"})
            continue
        if no_capacity:
            restored, decision = _restore_claim_if_incomplete(claim, entry, queue_dir)
            if decision is not None:
                details.append(
                    {
                        "dispatch_id": dispatch_id,
                        "state": str(decision.get("state") or "complete"),
                        "reason": str(decision.get("reason") or "completion_authority"),
                    }
                )
                continue
            if restored is None:
                pending_claims += 1
                failed += 1
                details.append(
                    {
                        "dispatch_id": dispatch_id,
                        "state": "claimed",
                        "reason": "capacity_restore_raced",
                    }
                )
                continue
            _restore_queued_record_from_entry(entry, restored)
            left_queued += 1
            details.append({"dispatch_id": dispatch_id, "state": "queued", "reason": "capacity_unavailable"})
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
            )
            if carrier_cleanup == "pending":
                # Same accounting contract as the stdout_launched branch: a
                # pending cleanup is pending, not a launch.
                pending_claims += 1
                _alert_launched_carrier_pending(dispatch_id, where="drain")
                details.append(
                    {
                        "dispatch_id": dispatch_id,
                        "state": "claimed",
                        "reason": "worker_record_present_carrier_cleanup_pending",
                    }
                )
            else:
                launched += 1
                details.append({"dispatch_id": dispatch_id, "state": "launched", "reason": "worker_record_present"})
            continue
        pending_claims += 1
        failed += 1
        details.append(
            {
                "dispatch_id": dispatch_id,
                "state": "claimed",
                "reason": f"launch_failed_pending_ledger:{proc.returncode}",
            }
        )
    remaining = len(list(queue_dir.glob("*.json")))
    return {
        "schema": f"{DISPATCH_QUEUE_SCHEMA}.drain.v1",
        "queue_dir": str(queue_dir),
        "launched": launched,
        "left_queued": left_queued,
        "failed": failed,
        "remaining": remaining,
        "pending_claims": pending_claims,
        "recovered_claims": recovery,
        "details": details,
    }


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
        "recovered_claims": {"restored": 0, "failed": 0},
        "details": [],
        "error": f"{type(exc).__name__}: {exc}",
    }
    if isinstance(exc, _RemoteDrainBlocked):
        payload["blocked"] = True
        payload["error"] = f"DISPATCH-BLOCKED {exc}"
        payload["code"] = exc.code
    return payload


def _cmd_drain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Drain queued goal-flight dispatch requests.")
    parser.add_argument("--queue-dir")
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
        print(
            "DRAIN "
            + json.dumps(
                {
                    "launched": payload["launched"],
                    "left_queued": payload["left_queued"],
                    "failed": payload["failed"],
                    "remaining": payload["remaining"],
                    "queue_dir": payload["queue_dir"],
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
    os_sandbox = "read-only" if args.read_only and goalflight_compat.is_macos() else OS_SANDBOX_OFF
    liveness_profile = "remote_api" if args.agent in {"cursor", "claude"} else None
    cfg = argparse.Namespace(
        agent=args.agent,
        model=getattr(args, "model", None),
        install_slot=None,
        account=getattr(args, "account", None),
        cwd=str(project_root),
        worktree="off",
        session_id=None,
        dispatch_id=args.dispatch_id,
        task_ids=list(getattr(args, "task_ids", []) or []),
        priority=getattr(args, "priority", "normal"),
        capacity_wait_s=getattr(args, "capacity_wait_s", None),
        prompt_id=None,
        prompt=acp_prompt_path,
        prompt_text=acp_prompt_text,
        prompt_b64=None,
        original_prompt_file=prompt_path,
        mode="one-shot",
        idle_timeout=float(args.max_idle_secs or 300.0),
        status_json=str(status_json),
        context_mode=_acp_context_mode_default(args),
        os_sandbox=os_sandbox,
        permission_mode=getattr(args, "permission_mode", "auto"),
        permission_dir=getattr(args, "permission_dir", None),
        permission_inline_timeout_s=getattr(args, "permission_inline_timeout_s", None),
        permission_user_timeout_s=getattr(args, "permission_user_timeout_s", None),
        read_only=bool(getattr(args, "read_only", False)),
        interactive=bool(getattr(args, "interactive", False)),
        permission_allow_tool_title_pattern=[],
        heartbeat_interval=max(float(args.poll_secs or 0.0), 0.1),
        wedge_samples=4,
        max_tool_s=DEFAULT_MAX_TOOL_S,
        max_quiet_s=max(float(args.max_idle_secs or 300.0), 1.0),
        progress_stall_s=300.0,
        liveness_profile=liveness_profile,
        remote_turn_silence_s=None,
        remote_turn_cancel_grace_s=DEFAULT_REMOTE_TURN_CANCEL_GRACE_S,
        steer_file=str(_steer_file(args.dispatch_id)),
        queue_launch_token=getattr(args, "queue_launch_token", None),
        request_envelope=_queue_request_envelope(args),
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


def _record_test_acp_running_fast(
    args,
    *,
    project_root: Path,
    prompt_path: str | None,
    status_json: Path,
    tail: Path,
    worker_pid: int,
) -> None:
    now = goalflight_ledger.utc_now()
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": args.dispatch_id,
        "prompt_id": None,
        "prompt_path": prompt_path,
        "prompt_sha256": goalflight_ledger.sha256_file(prompt_path),
        "agent": args.agent,
        "engine": _account_engine(args.agent) or args.agent,
        "shape": "acp",
        "account": args.account or "default",
        "transport": "dispatch",
        "project_root": str(project_root),
        "controller_pid": _controller_pid(args),
        "controller_identity": None,
        "worker_pid": worker_pid,
        "worker_identity": None,
        "worker_pgid": None,
        "acp_session_id": None,
        "logical_session_id": args.dispatch_id,
        "lease_id": None,
        "remote_lease_id": None,
        "stdout_path": str(tail),
        "stderr_path": None,
        "status_path": str(status_json),
        "os_sandbox": {"shape": "acp", "read_only": bool(args.read_only)},
        "state": "running",
        "terminal_state": goalflight_ledger.terminal_state_for("running"),
        "started_at": now,
        "hostname": socket.gethostname(),
    }
    if getattr(args, "queue_launch_token", None):
        record["queue_launch_token"] = args.queue_launch_token
    with goalflight_ledger.StateLock():
        path = goalflight_ledger.record_path(args.dispatch_id)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if existing.get("started_at"):
                record["started_at"] = existing["started_at"]
        goalflight_ledger.write_record(record)


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
    _record_test_acp_running_fast(
        args,
        project_root=project_root,
        prompt_path=str(Path(args.prompt_file).expanduser()) if args.prompt_file else None,
        status_json=status_json,
        tail=tail_path,
        worker_pid=worker_pid,
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
    if sleep_after_running_s > 0:
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
        with contextlib.suppress(Exception):
            Path(marker_path).write_text(str(worker_pid), encoding="utf-8")
        time.sleep(sleep_after_running_s)
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
    if "--acp-detached-child" not in argv:
        argv = _insert_before_worker_remainder(argv, ["--acp-detached-child"])
    return argv


def _run_acp_detached_launcher(
    args,
    *,
    status_json: Path,
    tail_path: Path,
    account_env: dict[str, str],
    env_remove: list[str],
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
    )
    _mark_queue_claim_worker_spawned(args, child_pid)
    wait_s = max(20.0, float(getattr(args, "capacity_wait_s", 0.0) or 0.0) + 20.0)
    deadline = time.time() + wait_s
    last_state = None
    while time.time() < deadline:
        record = _find_dispatch_record(args.dispatch_id)
        if (
            record
            and record.get("worker_pid")
            and record.get("queue_launch_token") == getattr(args, "queue_launch_token", None)
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
            if str(last_state).startswith("blocked_capacity"):
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
                return 2
        if not goalflight_compat.pid_alive(child_pid):
            break
        time.sleep(0.2)
    print(
        "goalflight_dispatch: ACP detached launch did not publish a running ledger "
        f"for {args.dispatch_id} (last_state={last_state!r})",
        file=sys.stderr,
    )
    return 1


def _run_acp_shape(args, *, base: Path, account_env: dict[str, str]) -> int:
    from goalflight_acp_run import acp_dispatch_exit_code, run_acp_dispatch

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
    cfg = _build_acp_cfg(args, status_json=status_json)
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
    worker_pid = payload.get("worker_pid")
    if worker_pid:
        _print_status_reminder(
            cfg.dispatch_id,
            status_json=status_json,
            tail_path=tail_path,
            worker_pid=int(worker_pid),
            shape="acp",
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


def _acp_context_mode_default(args) -> str:
    """codex-acp shares the exec-mode elicitation wedge risk, so default its
    context-mode posture OFF (opt back in via GOALFLIGHT_CODEX_CONTEXT_MODE).
    Other acp engines (grok-acp, cursor) keep context-mode enabled."""
    agent = str(getattr(args, "agent", ""))
    if agent.startswith("codex") and not _codex_context_mode_enabled():
        return "disabled"
    return "enabled"


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
        # swapping the value). grok 0.2.39 regression (verified 2026-06-10): in
        # single-turn `--prompt-file` mode, EVERY `--permission-mode` value stops
        # the file-write tool from writing — none produce the file; only OMITTING
        # the flag does. The tail varies by value (probe: grok-composer-2.5-fast,
        # write-a-file prompt):
        #   omit-flag    -> file written + DONE marker, rc=0   (the only one that works)
        #   default      -> 1-byte no-op, no file
        #   acceptEdits  -> 1-byte no-op, no file   (this is the value we shipped)
        #   auto         -> 0-byte no-op, no file
        #   dontAsk      -> prints a normal completion marker but STILL skips the write
        # The empty no-ops (default/acceptEdits/auto) make the watcher record
        # worker_dead_no_terminal_marker — how the shipped acceptEdits killed 4
        # grok-research dispatches (~18-25s, empty tails) on 2026-06-10; dontAsk is
        # worse, faking a clean finish with no artifact. All values are still listed
        # in `grok --help`, so this is a CLI regression, not a parse error. No safe
        # middle-ground value exists, bypassPermissions is too broad, and a
        # per-dispatch healthcheck would tax every dispatch's critical path — so the
        # fix is the deterministic omit, locked by a regression test
        # (tests/python/test_acp_model_passthrough.py). Same lesson as the stale
        # model note above: grok flags drift; re-validate before trusting one.
        argv = ["grok", "--prompt-file", str(prompt_path)]
        # Only pin a model when one is EXPLICITLY requested; otherwise omit the
        # flag entirely and let grok's CLI default (grok-4.5) apply.
        selected_model = str(model) if model else default_model
        if selected_model:
            argv += ["--model", selected_model]
        if args.cwd:
            argv += ["--cwd", args.cwd]
        return argv, None
    if args.agent == "kimi":
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
        argv = ["/bin/sh", "-lc", script, resolved_cwd, prompt_text, *extra]
        return argv, None
    return None, None  # unknown preset + no raw command


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == DAEMON_SPAWN_ARG:
        return _cmd_spawn_daemon()
    if argv and argv[0] == "steer":
        return _cmd_steer(argv[1:])
    if argv and argv[0] == "drain":
        return _cmd_drain(argv[1:])
    if argv and argv[0] == "dashboard-refresh":
        return _cmd_dashboard_refresh(argv[1:])

    parser = argparse.ArgumentParser(
        description="Crash-safe worker dispatch: detached worker + decoupled watcher."
    )
    parser.add_argument("--agent", default="worker",
                        help="Preset (codex|grok-code|grok-research|kimi) OR a label when you pass `-- <cmd>`")
    parser.add_argument("--prompt-file", help="Prompt file (preset path)")
    parser.add_argument("--prompt", help="Inline prompt text (preset path; alternative to --prompt-file)")
    parser.add_argument(
        "--task",
        dest="tasks",
        action="append",
        default=[],
        help="Comma-separated linked task/bug ids (t-/b-). May be repeated.",
    )
    parser.add_argument("--cwd", help="Worker working directory")
    parser.add_argument("--model", default=None,
                        help="Worker model id (grok-code/grok-research/kimi/codex --model passthrough). "
                             "Default = agent label's own default.")
    parser.add_argument("--read-only", action="store_true",
                        help="Read-only sandbox (review/analysis dispatches). Equivalent to "
                             "--os-sandbox read-only.")
    parser.add_argument("--os-sandbox", choices=list(OS_SANDBOX_PROFILES), default=None,
                        help="Opt-in OS sandbox profile for the bash-shape codex worker. Unset = "
                             "workspace-write (the unchanged default; existing dispatches are "
                             "unaffected). 'off' disables codex's Seatbelt sandbox "
                             "(codex --sandbox danger-full-access) for TRUSTED LOCAL GPU/perf work — "
                             "lets the worker reach Metal/MPS and write .git/worktrees/<name> to "
                             "self-commit. Sanctioned adapter profile, DISTINCT from the always-"
                             "forbidden --dangerously-*/--no-sandbox bypass flags. Commit-guard "
                             "(explicit pathspecs, no auto-push) + capacity/ledger tracking unchanged.")
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
                        help="How long to QUEUE for a capacity slot before DISPATCH-BLOCKED "
                             "(re-attempts acquire every ~15s; sleep-excluding clock). Default by "
                             "lane: bulk 900 / normal 600 / critical 120. 0 = fail instantly. "
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
    parser.add_argument("--controller-pid", type=int,
                        help="If set, watcher exits when this pid dies (orphan guard)")
    parser.add_argument("--from-queue", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--queue-launch-token", help=argparse.SUPPRESS)
    parser.add_argument("--queue-claim-path", help=argparse.SUPPRESS)
    parser.add_argument("--launch-detached", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--acp-detached-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stats", nargs="?", const="7d", metavar="WINDOW",
                        help="No-worker stats view over dispatch history; WINDOW is <N>h, <N>d, or bare <N> days.")
    parser.add_argument("--json", action="store_true", help="With --stats, emit machine-readable JSON.")
    parser.add_argument("worker", nargs=argparse.REMAINDER,
                        help="Optional `-- <cmd...>` raw worker (overrides the preset)")
    parser.set_defaults(drain_on_submit=True)
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
        shape = "acp" if args.agent in ("cursor", "claude-acp", "claude") else "bash"
    args.shape = shape
    if shape == "acp":
        try:
            _normalize_acp_agent(args)
            _apply_max_idle_default(args)
            if not _prompt_requested(args):
                raise DispatchUsageError("--shape acp requires --prompt or --prompt-file")
            if args.prompt_file and not Path(args.prompt_file).expanduser().exists():
                raise DispatchUsageError(f"prompt file not found: {args.prompt_file}")
            _validate_os_sandbox_conflict(args)
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
            _mark_queue_claim_launch_started(args)
            account_env = {} if goalflight_compat.is_windows() else _resolve_account_env(args)
            if args.submit:
                return _submit_dispatch(args, raw, base=base)
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
            return _run_acp_shape(args, base=base, account_env=account_env)
        except DispatchUsageError as e:
            print(f"goalflight_dispatch: {e}", file=sys.stderr)
            return 64

    if goalflight_compat.is_windows():
        return _refuse_windows_dispatch(args)

    try:
        _apply_max_idle_default(args)
        _validate_before_side_effects(args, raw)
        dispatch_warnings = _dispatch_warnings(args, raw)
        account_env = _resolve_account_env(args)
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
        _mark_queue_claim_launch_started(args)
    except DispatchUsageError as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64
    if args.submit:
        return _submit_dispatch(args, raw, base=base)
    tail = Path(args.tail) if args.tail else base / f"{args.dispatch_id}.tail"
    status_json = Path(args.status_json) if args.status_json else base / f"{args.dispatch_id}.status.json"

    steer_file = _steer_file(args.dispatch_id)
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
    worker_argv, stdin_path = build_worker(args, prompt_path, raw)
    if not worker_argv:
        print("goalflight_dispatch: no worker — use `--agent codex --prompt-file X [--cwd .]` "
              "or `-- <cmd...>`", file=sys.stderr)
        return 64

    tail.parent.mkdir(parents=True, exist_ok=True)
    _emit_dispatch_warnings(dispatch_warnings, tail_path=tail, reset_tail=True)
    worker_stdout_mode = "ab" if dispatch_warnings else "wb"
    project_root = _project_root(args)
    _reap_quota_stuck_before_bash_launch()
    worker_pid = None
    watcher_pid = None
    caffeinate_pid = None
    pidfile = None
    lease_id = None
    ledger_recorded = False
    detached_launched = False
    codex_dispatch_home = None
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
        "worker_pid": None,
        "tail": str(tail),
        "status_json": str(status_json),
    }
    if getattr(args, "task_ids", None):
        summary_head["task_ids"] = list(args.task_ids)

    try:
        # Pre-record the ledger BEFORE the capacity queue: the (possibly
        # minutes-long) wait window must be visible to goalflight_status
        # (classified queued_capacity = live) and must trip the reused-id
        # guard for a duplicate explicit --dispatch-id (review P1: the guard
        # is ledger-based, so a ledger-less wait window was bypassable).
        # Back-compat: ledger consumers now see one extra pre-spawn row per
        # dispatch; the existing running row is still written after capacity
        # is acquired and the worker is spawned.
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
        except (SystemExit, KeyboardInterrupt):
            # Queue exhausted or interrupted: the status file already says
            # blocked_capacity; make the ledger finish agree instead of the
            # generic "failed", mirroring the status plane's specific reason
            # (wait_interrupted vs machine_worker_cap vs agent_worker_cap ...).
            final_state = "blocked_capacity"
            final_reason = "capacity_wait"
            with contextlib.suppress(Exception):
                blocked = json.loads(Path(status_json).read_text(encoding="utf-8"))
                specific = (blocked.get("reason") or {}).get("reason")
                if specific:
                    final_reason = f"capacity_wait:{specific}"
            raise
        if (
            _account_engine(args.agent) == "codex"
            and not goalflight_compat.is_windows()
        ):
            try:
                codex_dispatch_home, effective_account = resolve_codex_home(
                    project_root,
                    args.account,
                    args.dispatch_id,
                )
            except BaseException:
                codex_dispatch_home, effective_account = None, None
        request_envelope = _queue_request_envelope(args)
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
            request_envelope=request_envelope,
        )

        # 1. Launch the worker DETACHED, output -> tail (prompt -> stdin for codex).
        # Account guards ran before prompt/id/lease side effects; only apply the
        # resolved environment here.
        env = dict(os.environ)
        env.update(account_env)
        if codex_dispatch_home is not None:
            env["CODEX_HOME"] = codex_dispatch_home
        env["GOALFLIGHT_STEER_FILE"] = str(steer_file)
        if original_prompt_path:
            env["GOALFLIGHT_PROMPT_FILE"] = str(original_prompt_path)
        else:
            env.pop("GOALFLIGHT_PROMPT_FILE", None)
        if args.account and _account_engine(args.agent) == "grok":
            env.pop("GROK_API_KEY", None)
            env.pop("XAI_API_KEY", None)
        if args.account and _account_engine(args.agent) == "cursor":
            env.pop("CURSOR_API_KEY", None)
        if args.billing == "sub" and _account_engine(args.agent) == "codex":
            env.pop("OPENAI_API_KEY", None)  # subscription billing for the selected account, not the API
        _apply_web_qa_env(env, args, project_root)
        if _account_engine(args.agent) == "codex":
            worker_argv = _guard_codex_context_mode_disable(worker_argv, env)

        _mark_queue_claim_worker_spawn_intent(args)
        worker_pid = _spawn_daemonized_process(
            worker_argv,
            env=env,
            stdin_path=stdin_path,
            stdout_path=tail,
            stdout_mode=worker_stdout_mode,
            stderr="stdout",
            serialize_stdout=True,
            label="worker",
        )
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
        # live detached worker's slot when controller_pid is the dead launcher).
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
        try:
            _record_ledger(
                args,
                project_root=project_root,
                prompt_path=prompt_path,
                status_json=status_json,
                tail=tail,
                lease_id=lease_id,
                worker_pid=worker_pid,
                state="running",
                effective_account=effective_account,
                request_envelope=request_envelope,
            )
        except Exception as e:
            registration_errors.append(_registration_error("record_ledger_running", e))

        summary_head.update({"worker_pid": worker_pid, "worker_identity": worker_identity_token})
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
            controller_pid=args.controller_pid,
            poll_secs=args.poll_secs,
            max_idle_secs=args.max_idle_secs,
        )

        # 2. Run the decoupled watcher detached from this launcher. We still poll
        # status and return when it records a terminal state, but launcher teardown
        # no longer tears down the watcher with the worker.
        watch_cmd = [
            sys.executable, str(WATCH_PY),
            "--pid", str(worker_pid),
            "--tail", str(tail),
            "--status-json", str(status_json),
            "--agent", args.agent,
            "--poll-secs", str(args.poll_secs),
            "--max-idle-secs", str(args.max_idle_secs),
            "--dispatch-id", args.dispatch_id,
            "--pgid", str(pgid),
            "--stay-after-terminal",
        ]
        if getattr(args, "task_ids", None):
            watch_cmd += ["--project-root", str(project_root), "--task-ids", ",".join(args.task_ids)]
        watch_identity_token = (
            worker_identity_token
            if worker_identity_token and worker_identity_token.get("lstart") and worker_identity_token.get("comm")
            else None
        )
        if watch_identity_token:
            watch_cmd += ["--worker-identity-json", json.dumps(watch_identity_token, sort_keys=True)]
        if args.launch_detached:
            watch_cmd += ["--detached"]
        if codex_dispatch_home is not None:
            watch_cmd += ["--codex-dispatch-home-resolved"]
        if args.controller_pid:
            watch_cmd += ["--controller-pid", str(args.controller_pid)]
        if prompt_path:
            watch_cmd += ["--ignore-prompt-file", str(prompt_path)]

        watch_log = base / f"{args.dispatch_id}.watcher.log"
        with contextlib.suppress(Exception):
            write_status(status_json, {
                "schema": "goalflight.status.v1",
                "dispatch_id": args.dispatch_id,
                "agent": args.agent,
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
                **({"task_ids": list(args.task_ids)} if getattr(args, "task_ids", None) else {}),
            })
        _export_dashboard_status_for_project(project_root)
        _start_dashboard_refresh_for_project(project_root)
        watcher_pid = _spawn_daemonized_process(
            watch_cmd,
            env=os.environ.copy(),
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
    except Exception as e:
        final_state = "failed"
        final_reason = f"{type(e).__name__}: {e}"
        print("DISPATCH-ERROR " + json.dumps({"state": final_state, "reason": final_reason}, sort_keys=True), file=sys.stderr, flush=True)
        return 1
    finally:
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
        if ledger_recorded and not detached_launched and not keep_live_watcher_open:
            with contextlib.suppress(Exception):
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
        if not detached_launched and not keep_live_watcher_open:
            with contextlib.suppress(Exception):
                _release_capacity(lease_id, str(capacity_state or final_state), capacity_reason)
            _cleanup_pidfile_if_worker_dead(pidfile, worker_pid)
            _export_dashboard_status_for_project(project_root)
        if (
            codex_dispatch_home is not None
            and not detached_launched
            and final_worker_alive is not True
        ):
            cleanup_codex_dispatch_home(args.dispatch_id)


if __name__ == "__main__":
    raise SystemExit(main())
