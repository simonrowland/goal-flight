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
from goalflight_liveness import active_monotonic, process_group_id, write_status
from goalflight_watch import (
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
QUEUE_PRIORITY_RANK = {lane: rank for rank, lane in enumerate(goalflight_capacity.PRIORITY_LANES)}
QUEUE_DEFAULT_PRIORITY = "normal"
PRESET_AGENTS = {"codex", "grok-code", "grok-research"}
STDIN_PROMPT_AGENTS = {"codex", "grok-code", "grok-research"}
DEFAULT_MAX_IDLE_SECS = 180.0
_DASHBOARD_REFRESH_SUBCOMMAND = "dashboard-refresh"
_DASHBOARD_REFRESH_MARKER = "goalflight-dashboard-refresh-v1"
_DASHBOARD_REFRESH_QUEUED_GRACE_S = 60 * 60
_DASHBOARD_REFRESH_MAX_LIFETIME_S = 4 * 60 * 60
_DASHBOARD_REFRESH_CLAIM_STALE_S = 30.0
CODE_WRITER_MAX_IDLE_SECS = 600.0
CODE_WRITER_AGENTS = {"codex", "codex-acp", "grok-code", "grok-acp", "cursor", "cursor-agent"}

# --os-sandbox: opt-in, per-dispatch OS sandbox profile for the bash-shape codex
# worker. Unset -> "workspace-write" (the unchanged default — every existing
# dispatch is unaffected). "off" runs codex with its macOS Seatbelt sandbox
# DISABLED (codex --sandbox danger-full-access) so a TRUSTED LOCAL GPU/perf worker
# can (a) reach Metal/MPS for GPU verification and (b) write the linked worktree's
# .git/worktrees/<name> metadata to self-commit. "off" is a sanctioned adapter
# profile (adapters/codex.json os_sandbox.supported_profiles), DISTINCT from the
# always-forbidden --dangerously-*/--no-sandbox approval-and-sandbox bypass flags
# (which the presets never emit and this never enables). "read-only" == --read-only.
OS_SANDBOX_PROFILES = ("workspace-write", "read-only", "off")
_CODEX_SANDBOX_VALUE = {
    "workspace-write": "workspace-write",
    "read-only": "read-only",
    "off": "danger-full-access",  # codex's sanctioned "no Seatbelt" sandbox value
}


def _effective_os_sandbox(args) -> str:
    """Resolve the effective OS sandbox profile (non-raising, safe anywhere).

    Precedence: explicit --os-sandbox > legacy --read-only alias > default
    "workspace-write". Conflicts are surfaced separately by
    _validate_os_sandbox_conflict.
    """
    explicit = getattr(args, "os_sandbox", None)
    if explicit in OS_SANDBOX_PROFILES:
        return explicit
    return "read-only" if getattr(args, "read_only", False) else "workspace-write"


def _effective_read_only(args) -> bool:
    return _effective_os_sandbox(args) == "read-only"


def _validate_os_sandbox_conflict(args) -> None:
    explicit = getattr(args, "os_sandbox", None)
    if explicit and getattr(args, "read_only", False) and explicit != "read-only":
        raise DispatchUsageError(
            f"--read-only conflicts with --os-sandbox {explicit} "
            "(opposite write postures); pass only one."
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
ACCOUNT_ENGINE_BY_AGENT = {
    "codex": "codex",
    "codex-acp": "codex",
    "grok": "grok",
    "grok-code": "grok",
    "grok-research": "grok",
    "grok-acp": "grok",
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
GROK_EXECUTION_PREAMBLE = (
    "Grok worker execution contract:\n"
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
    label: str,
) -> int:
    """Spawn a child through the private daemon helper and return the child's pid."""
    spec = {
        "argv": argv,
        "stdin_path": stdin_path,
        "stdout_path": str(stdout_path) if stdout_path else None,
        "stdout_mode": stdout_mode,
        "stderr": stderr,
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
    )
    if not terminal_marker and not worker_is_alive:
        terminal_marker = _final_terminal_marker(
            tail,
            ignore_prefix_lines=ignore_prefix_lines,
            suppress_unfenced_prompt_markers=True,
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
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
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
        agent_label = f"{agent}-bash-tail" if agent else "worker-bash-tail"
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
        f"WARN: --model with --agent {args.agent} is advisory-only; "
        "the harness wires grok model ids, so the flag is unnecessary and can break "
        "silently on provider id drift"
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
            f"{args.agent!r} — use --agent codex|grok-code|grok-research with "
            "--prompt/--prompt-file, or pass a raw worker after `-- <cmd...>`"
        )
    if args.agent in STDIN_PROMPT_AGENTS and not _prompt_requested(args):
        raise DispatchUsageError(
            f"--agent {args.agent} requires --prompt or --prompt-file; refusing to feed empty stdin"
        )
    if args.prompt_file and not Path(args.prompt_file).expanduser().exists():
        raise DispatchUsageError(f"prompt file not found: {args.prompt_file}")
    _validate_os_sandbox_conflict(args)
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
    if agent in {"grok-code", "grok-research"}:
        preambles.append(GROK_EXECUTION_PREAMBLE)
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


def _record_ledger(args, *, project_root: Path, prompt_path: str | None, status_json: Path,
                   tail: Path, lease_id: str | None, worker_pid: int | None, state: str) -> None:
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
    if args.web_research_ok:
        argv.append("--web-research-ok")
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
    try:
        launcher_pid = int(entry.get("queue_launcher_pid") or 0)
    except (TypeError, ValueError):
        return False
    if launcher_pid <= 0:
        return False
    try:
        ok, _reason = goalflight_ledger.identity_matches(
            {
                "worker_pid": launcher_pid,
                "worker_identity": entry.get("queue_launcher_identity") or {},
            }
        )
        return ok
    except Exception:
        return goalflight_compat.pid_alive(launcher_pid)


def _queue_claim_worker_alive(entry: dict) -> bool:
    try:
        worker_pid = int(entry.get("queue_worker_pid") or 0)
    except (TypeError, ValueError):
        return False
    return worker_pid > 0 and goalflight_compat.pid_alive(worker_pid)


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
            "dispatch_id": args.dispatch_id,
            "cwd": str(project_root),
            "model": args.model,
            "shape": args.shape,
            "read_only": bool(args.read_only),
            "os_sandbox": getattr(args, "os_sandbox", None),
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
    has_remote_launch = bool(
        record.get("launch_unconfirmed")
        or (isinstance(remote_receipt, dict) and remote_receipt.get("remote_pid"))
    )
    if not has_remote_launch:
        return False
    if require_live_nonterminal and _dispatch_record_is_terminal(record):
        return False
    return True


def _dispatch_has_terminal_record(dispatch_id: str) -> bool:
    return _dispatch_record_is_terminal(_find_dispatch_record(dispatch_id))


def _recover_claimed_queue_entries(queue_dir: Path, *, stale_s: float) -> dict:
    restored = 0
    cleared = 0
    pending_launch = 0
    now = time.time()
    for claim in sorted(queue_dir.glob("*.json.claimed-*")):
        if claim.name.endswith(".failed"):
            continue
        try:
            entry = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        dispatch_id = str(entry.get("dispatch_id") or "")
        target_name = claim.name.split(".claimed-", 1)[0]
        target = queue_dir / target_name
        launch_token = _queue_launch_token_from_entry(entry)
        if launch_token:
            try:
                age_s = now - claim.stat().st_mtime
            except OSError:
                continue
            is_stale = age_s >= max(0.0, stale_s)
            if dispatch_id and _dispatch_has_worker_record(dispatch_id, queue_launch_token=launch_token):
                with contextlib.suppress(OSError):
                    claim.unlink()
                cleared += 1
                continue
            launch_still_active = _queue_claim_launcher_alive(entry) or _queue_claim_worker_alive(entry)
            launch_marked = (
                _queue_claim_launch_started(entry)
                or _queue_claim_worker_spawn_intent(entry)
                or _queue_claim_worker_spawned(entry)
            )
            if launch_still_active or (launch_marked and not is_stale):
                pending_launch += 1
                continue
            if not launch_marked:
                if not is_stale:
                    pending_launch += 1
                    continue
                if target.exists():
                    with contextlib.suppress(OSError):
                        claim.unlink()
                    cleared += 1
                    continue
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
                ):
                    entry.pop(key, None)
                entry["state"] = "queued"
                entry["updated_at"] = goalflight_ledger.utc_now()
                with contextlib.suppress(OSError):
                    _write_json_atomic(target, entry)
                    claim.unlink()
                    restored += 1
                continue
            if dispatch_id and _dispatch_has_worker_record(dispatch_id, require_live_nonterminal=True):
                pending_launch += 1
                continue
            if dispatch_id and _dispatch_has_terminal_record(dispatch_id):
                with contextlib.suppress(OSError):
                    claim.unlink()
                cleared += 1
                continue
            _mark_claim_worker_dead(entry, reason="stale_claim_launch_token_lost")
            with contextlib.suppress(OSError):
                claim.unlink()
                cleared += 1
            continue
        if dispatch_id and _dispatch_has_worker_record(dispatch_id, require_live_nonterminal=True):
            pending_launch += 1
            continue
        try:
            age_s = now - claim.stat().st_mtime
        except OSError:
            continue
        if age_s < max(0.0, stale_s):
            continue
        if target.exists():
            with contextlib.suppress(OSError):
                claim.unlink()
            cleared += 1
            continue
        entry["state"] = "queued"
        entry["updated_at"] = goalflight_ledger.utc_now()
        with contextlib.suppress(OSError):
            _write_json_atomic(target, entry)
            claim.unlink()
            restored += 1
    return {"restored": restored, "cleared": cleared, "pending_launch": pending_launch}


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


def _restore_claimed_entry(claim: Path, entry: dict) -> Path:
    queue_dir = claim.parent
    target = queue_dir / claim.name.split(".claimed-", 1)[0]
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
    ):
        entry.pop(key, None)
    entry["state"] = "queued"
    entry["updated_at"] = goalflight_ledger.utc_now()
    _write_json_atomic(target, entry)
    with contextlib.suppress(OSError):
        claim.unlink()
    return target


def _mark_queue_claim_launch_started(args) -> None:
    if not (getattr(args, "from_queue", False) and getattr(args, "queue_claim_path", None)):
        return
    token = getattr(args, "queue_launch_token", None)
    if not token:
        return
    claim = Path(str(args.queue_claim_path)).expanduser()
    try:
        entry = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchUsageError(f"queue claim launch marker failed: {type(exc).__name__}")
    if entry.get("queue_launch_token") != token:
        raise DispatchUsageError("queue claim launch token mismatch")
    launcher_identity = goalflight_ledger.process_identity(os.getpid()) or {
        "pid": os.getpid(),
        "identity_available": False,
        "identity_source": "pid_probe_unavailable",
    }
    now = goalflight_ledger.utc_now()
    entry["queue_launch_started"] = True
    entry["queue_launch_started_at"] = now
    entry["queue_launcher_pid"] = os.getpid()
    if launcher_identity:
        entry["queue_launcher_identity"] = launcher_identity
    entry["updated_at"] = now
    try:
        _write_json_atomic(claim, entry)
    except OSError as exc:
        raise DispatchUsageError(f"queue claim launch marker failed: {type(exc).__name__}")


def _mark_queue_claim_worker_spawn_intent(args) -> None:
    if not (getattr(args, "from_queue", False) and getattr(args, "queue_claim_path", None)):
        return
    token = getattr(args, "queue_launch_token", None)
    if not token:
        return
    claim = Path(str(args.queue_claim_path)).expanduser()
    try:
        entry = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchUsageError(f"queue claim worker-spawn marker failed: {type(exc).__name__}")
    if entry.get("queue_launch_token") != token:
        raise DispatchUsageError("queue claim launch token mismatch")
    now = goalflight_ledger.utc_now()
    entry["queue_worker_spawn_intent"] = True
    entry["queue_worker_spawn_intent_at"] = now
    entry["updated_at"] = now
    try:
        _write_json_atomic(claim, entry)
    except OSError as exc:
        raise DispatchUsageError(f"queue claim worker-spawn marker failed: {type(exc).__name__}")


def _mark_queue_claim_worker_spawned(args, worker_pid: int | None) -> None:
    if not (getattr(args, "from_queue", False) and getattr(args, "queue_claim_path", None)):
        return
    token = getattr(args, "queue_launch_token", None)
    if not (token and worker_pid):
        return
    claim = Path(str(args.queue_claim_path)).expanduser()
    try:
        entry = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if entry.get("queue_launch_token") != token:
        return
    entry["queue_worker_pid"] = int(worker_pid)
    entry["queue_worker_spawned_at"] = goalflight_ledger.utc_now()
    entry["updated_at"] = goalflight_ledger.utc_now()
    with contextlib.suppress(OSError):
        _write_json_atomic(claim, entry)


def _mark_claim_failed(claim: Path, entry: dict, *, reason: str) -> None:
    entry["state"] = "failed"
    entry["reason"] = reason
    entry["updated_at"] = goalflight_ledger.utc_now()
    failed = claim.with_name(f"{claim.name}.failed")
    _write_json_atomic(failed, entry)
    with contextlib.suppress(OSError):
        claim.unlink()


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

    _mark_queue_claim_launch_started(
        argparse.Namespace(
            from_queue=True,
            queue_claim_path=str(claim),
            queue_launch_token=launch_token,
        )
    )
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
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    args = _queued_args_for_status(entry)
    project_root = Path(str(entry.get("project_root") or request.get("cwd") or Path.cwd())).resolve()
    status_json = Path(str(request.get("status_json") or _dispatch_base_dir() / f"{args.dispatch_id}.status.json"))
    tail = Path(str(request.get("tail") or _dispatch_base_dir() / f"{args.dispatch_id}.tail"))
    _record_queued_status(args, project_root=project_root, status_json=status_json, tail=tail, queue_path=queue_path)


def _mark_claim_worker_dead(entry: dict, *, reason: str) -> None:
    dispatch_id = str(entry.get("dispatch_id") or "")
    if not dispatch_id:
        return
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    args = _queued_args_for_status(entry)
    project_root = Path(str(entry.get("project_root") or request.get("cwd") or Path.cwd())).resolve()
    status_json = Path(str(request.get("status_json") or _dispatch_base_dir() / f"{dispatch_id}.status.json"))
    tail = Path(str(request.get("tail") or _dispatch_base_dir() / f"{dispatch_id}.tail"))
    if _find_dispatch_record(dispatch_id) is None:
        with contextlib.suppress(Exception):
            _record_queued_ledger_fast(
                args,
                project_root=project_root,
                prompt_path=str(Path(args.prompt_file).expanduser()) if args.prompt_file else None,
                status_json=status_json,
                tail=tail,
            )
    ignore_prefix_lines = _ignore_prefix_lines(str(Path(args.prompt_file).expanduser()) if args.prompt_file else None)
    terminal_marker = _final_terminal_marker(
        tail,
        ignore_prefix_lines=ignore_prefix_lines,
        suppress_unfenced_prompt_markers=True,
    )
    state = _marker_state_for_terminal(terminal_marker) if terminal_marker else "worker_dead"
    reason_for_finish = f"marker:{terminal_marker['kind']}:final_reconciliation" if terminal_marker else reason
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
    # Quota refinement (merge of the quota-stuck line): only rewrites
    # idle_timeout/rate_limited/worker_dead states, and only on a genuine
    # error-context quota signature. Surviving marker-reconciled completions
    # are never touched.
    state, final_reason = _quota_limited_state_reason(state, final_reason, tail, agent=args.agent)
    with contextlib.suppress(Exception):
        _finish_ledger(dispatch_id, state or "worker_dead", final_reason, worker_still_alive=False)
    with contextlib.suppress(Exception):
        write_status(
            status_json,
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": dispatch_id,
                "agent": args.agent,
                "shape": args.shape,
                "state": state or "worker_dead",
                "terminal_state": goalflight_ledger.terminal_state_for(state or "worker_dead", final_reason),
                "reason": final_reason,
                "liveness_state": goalflight_terminal.terminal_liveness_state(state or "worker_dead"),
                "terminal_marker": terminal_marker,
                "project_root": str(project_root),
                "worker_pid": entry.get("queue_worker_pid"),
                "worker_alive": False,
                "tail_path": str(tail),
                "status_path": str(status_json),
                "updated_at": int(time.time()),
                **({"task_ids": list(args.task_ids)} if getattr(args, "task_ids", None) else {}),
            },
        )


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
    entries = sorted(_queue_entry_drain_candidate(path) for path in queue_dir.glob("*.json"))
    if args.limit and args.limit > 0:
        entries = entries[: args.limit]
    if not remote_node and entries:
        _run_drain_prelaunch_hook(_pass_agent_labels(entries))
    for _sort_key, path, _scan_entry, _scan_read_error in entries:
        with _queue_mutation_lock(queue_dir):
            claim = _claim_queue_entry(path)
        if claim is None:
            continue
        # The pre-scan read (_scan_entry/_scan_read_error) is best-effort and
        # used ONLY for sort ordering: it can be stale if the entry was being
        # restored by a concurrent stale-claim recovery at scan time. Decide
        # readability authoritatively from the now-owned claim file, so a valid
        # entry is never tombstoned on a stale pre-scan error.
        try:
            entry = json.loads(claim.read_text(encoding="utf-8"))
            if not isinstance(entry, dict):
                raise ValueError("invalid_payload")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            failed += 1
            _mark_claim_failed(claim, {"path": str(claim)}, reason=f"unreadable:{type(exc).__name__}")
            continue
        dispatch_id = str(entry.get("dispatch_id") or path.stem)
        launch_token = _queue_launch_token()
        entry["state"] = "claimed"
        entry["queue_launch_token"] = launch_token
        entry["updated_at"] = goalflight_ledger.utc_now()
        try:
            _write_json_atomic(claim, entry)
        except OSError as exc:
            failed += 1
            details.append(
                {
                    "dispatch_id": dispatch_id,
                    "state": "claimed",
                    "reason": f"claim_token_write_failed:{type(exc).__name__}",
                }
            )
            continue
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
            restored = _restore_claimed_entry(claim, entry)
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
        ledger_confirmed = _dispatch_has_worker_record(dispatch_id, queue_launch_token=launch_token)
        no_capacity = proc.returncode == 2 and "blocked_capacity" in (proc.stdout + proc.stderr)
        if stdout_launched and ledger_confirmed:
            with contextlib.suppress(OSError):
                claim.unlink()
            launched += 1
            details.append({"dispatch_id": dispatch_id, "state": "launched"})
            continue
        if no_capacity:
            restored = _restore_claimed_entry(claim, entry)
            _restore_queued_record_from_entry(entry, restored)
            left_queued += 1
            details.append({"dispatch_id": dispatch_id, "state": "queued", "reason": "capacity_unavailable"})
            continue
        if ledger_confirmed:
            with contextlib.suppress(OSError):
                claim.unlink()
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
    child_argv = [sys.executable, str(Path(__file__).resolve()), *_acp_detached_child_argv(args)]
    child_pid = _spawn_daemonized_process(
        child_argv,
        env=env,
        stdout_path=tail_path,
        stdout_mode="ab",
        stderr="stdout",
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
    with _temporary_env(account_env, remove=env_remove):
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
        # Model PER TASK:
        #   grok-code     (coding, no web) -> grok-composer-2.5-fast
        #   grok-research (web search/fetch) -> grok-build (grok's purpose-built
        #                  web model and grok.com's default)
        # The earlier "grok-build dies at ~28s empty under web-search" note is
        # STALE: re-validated 2026-06-09, grok-build returned good, primary-source-
        # cited research in ~43s, recovering via web_search from the intermittent
        # `web_fetch` tool_output_error flakiness that hits BOTH models. composer
        # is a coding model and yields thin, weakly-sourced research, so it is the
        # wrong default for grok-research (observed live: composer web_fetch errors
        # + Wikipedia-only answers vs grok-build's nature.com primary source).
        default_model = "grok-build" if args.agent == "grok-research" else "grok-composer-2.5-fast"
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
        argv += ["--model", str(model) if model else default_model]
        if args.cwd:
            argv += ["--cwd", args.cwd]
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
                        help="Preset (codex|grok-code|grok-research) OR a label when you pass `-- <cmd>`")
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
                        help="Worker model id (grok-code/grok-research/codex --model passthrough). "
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
    parser.add_argument("--web-research-ok", action="store_true",
                        help="Override the grok-code research-intent guard: confirm this prompt is "
                             "a coding task that merely mentions the web (web research belongs on "
                             "--agent grok-research, whose model can actually drive web tools).")
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
        _record_ledger(
            args,
            project_root=project_root,
            prompt_path=prompt_path,
            status_json=status_json,
            tail=tail,
            lease_id=lease_id,
            worker_pid=None,
            state="starting",
        )

        # 1. Launch the worker DETACHED, output -> tail (prompt -> stdin for codex).
        # Account guards ran before prompt/id/lease side effects; only apply the
        # resolved environment here.
        env = dict(os.environ)
        env.update(account_env)
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

        _mark_queue_claim_worker_spawn_intent(args)
        worker_pid = _spawn_daemonized_process(
            worker_argv,
            env=env,
            stdin_path=stdin_path,
            stdout_path=tail,
            stdout_mode=worker_stdout_mode,
            stderr="stdout",
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


if __name__ == "__main__":
    raise SystemExit(main())
