#!/usr/bin/env python3
"""goalflight_dispatch.py — crash-safe worker dispatch with a decoupled watcher.

ONE command, run via the host's background-task mechanism, that dispatches a
worker AND reliably wakes the orchestrator on every terminal state. It fixes the
"orchestrator hangs because the worker crashed/hung and never sent a wakeup" class
(observed 2026-05-30).

Easy path (agent preset — the common case):
    python3 goalflight_dispatch.py --agent codex --prompt-file p.md --cwd .
    python3 goalflight_dispatch.py --agent codex --prompt-file p.md --read-only   # review/analysis
    python3 goalflight_dispatch.py --agent grok-code --prompt-file p.md --cwd .

Presets bake in the canonical NON-INTERACTIVE + SAFE flags per worker, so you
never spell them out (and cannot fat-finger `--dangerously-bypass`). Paths and a
dispatch id are auto-derived under the state dir; override with --tail /
--status-json / --dispatch-id if you want.

Escape hatch (any worker): pass the raw command after `--`:
    python3 goalflight_dispatch.py --agent custom --tail t --status-json s -- <cmd...>

How it stays crash-safe (validated):
  1. The worker is launched by a short detached helper, then reparented into its
     own session/process-group so launcher teardown cannot reap it.
  2. The worker is not this dispatcher's child after launch; the helper is
     reaped immediately and the platform supervisor reaps the worker on exit.
  3. The decoupled watcher (goalflight_watch.py) detects finished(0)/crashed(1)/
     hung(2)/controller-dead(3)/blocked(4) and we exit with ITS code UNCHANGED,
     so the host completion notification carries the real terminal state.

Cross-platform: pure stdlib; the watcher uses goalflight_compat.pid_alive, so
this is also the dispatch path on Windows (where the bash watcher is refused).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

import goalflight_compat
import goalflight_capacity
import goalflight_ledger
from goalflight_liveness import process_group_id, write_status
from goalflight_watch import _last_line_is_terminal_marker, _marker_state as _marker_state_for_terminal

SCRIPT_DIR = Path(__file__).resolve().parent
WATCH_PY = SCRIPT_DIR / "goalflight_watch.py"
WATCH_TAIL_SH = SCRIPT_DIR / "watch-dispatch-tail.sh"
DAEMON_SPAWN_ARG = "__goalflight_spawn_daemon"
PRESET_AGENTS = {"codex", "grok-code", "grok-research"}
STDIN_PROMPT_AGENTS = {"codex", "grok-code", "grok-research"}
DEFAULT_MAX_IDLE_SECS = 180.0
CODE_WRITER_MAX_IDLE_SECS = 600.0
CODE_WRITER_AGENTS = {"codex", "codex-acp", "grok-code", "grok-acp", "cursor", "cursor-agent"}
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
STEER_PROMPT_PREAMBLE = (
    "You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH "
    "ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages "
    "into your plan; ack each with `STEER-ACK: <seq>` on its own line; a steer may "
    "redirect or HALT you — honor it."
)
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
    if state == "idle_timeout":
        return 2
    if state in {"orphaned", "controller_dead"}:
        return 3
    if state == "blocked" or (isinstance(state, str) and state.startswith("blocked")):
        return 4
    return 1


def _is_status_terminal(state: object) -> bool:
    if state in {
        "complete",
        "worker_dead",
        "idle_timeout",
        "orphaned",
        "controller_dead",
        "watcher_stopped",
        "failed",
    }:
        return True
    return isinstance(state, str) and state.startswith("blocked")


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
    return f"worker still alive - re-attach via goalflight_status.py --done {dispatch_id}"


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
    terminal_marker = _last_line_is_terminal_marker(
        tail,
        ignore_prefix_lines=_ignore_prefix_lines(prompt_path),
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
        "reason": reason,
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
    while True:
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
            return _status_exit_code(repaired.get("state")), repaired, repaired.get("reason")
        time.sleep(sleep_s)


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
        return str(Path(args.prompt_file).expanduser())
    if args.prompt is not None:
        base.mkdir(parents=True, exist_ok=True)
        pf = base / f"{args.dispatch_id}.prompt"
        pf.write_text(args.prompt, encoding="utf-8")
        return str(pf)
    return None


def _project_root(args) -> Path:
    return Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()


def _state_dir() -> Path:
    return Path(
        os.environ.get("GOALFLIGHT_STATE_DIR", str(goalflight_compat.default_state_dir()))
    ).expanduser()


def _dispatch_base_dir() -> Path:
    return _state_dir() / "dispatch"


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
            "--controller-pid",
            str(controller_pid if controller_pid is not None else os.getpid()),
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


def _steer_file(dispatch_id: str) -> Path:
    return _dispatch_base_dir() / f"{dispatch_id}.steer.jsonl"


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


def _read_only_write_prompt_reason(args) -> str | None:
    if not getattr(args, "read_only", False) or not _prompt_requested(args):
        return None
    text = _read_prompt_for_guard(args)
    if not text:
        return None
    for label, pattern in READ_ONLY_WRITE_PROMPT_PATTERNS:
        if pattern.search(text):
            return label
    for _label, pattern in READ_ONLY_INLINE_RETURN_PROMPT_PATTERNS:
        if pattern.search(text):
            return None
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
    _guard_read_only_write_prompt(args)


def _nonterminal_dispatch_reuse_reason(dispatch_id: str) -> str | None:
    record = _find_dispatch_record(dispatch_id)
    if record is None:
        return None
    terminal = goalflight_ledger.terminal_state_for(
        record.get("state"),
        record.get("reason") or record.get("error"),
    )
    if terminal != "unknown":
        return None
    classification = goalflight_ledger.classify(record)
    return (
        f"classification={classification} state={record.get('state') or 'running'} "
        f"status={record.get('status_path') or '-'}"
    )


def _refuse_reused_nonterminal_dispatch_id(dispatch_id: str) -> None:
    reason = _nonterminal_dispatch_reuse_reason(dispatch_id)
    if not reason:
        return
    raise DispatchUsageError(
        f"dispatch id {dispatch_id!r} already has a non-terminal ledger record "
        f"({reason}). Use a unique --dispatch-id per parallel chunk; in shell "
        "loops, launch each dispatch as its own background command."
    )


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
    entries: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            seq = int(item.get("seq"))
        except (TypeError, ValueError):
            continue
        entries.append({
            "seq": seq,
            "ts": str(item.get("ts") or ""),
            "text": str(item.get("text") or ""),
        })
    return entries


def _read_steer_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        goalflight_compat.flock(f, goalflight_compat.LOCK_SH)
        try:
            return _parse_steer_lines(f.read().splitlines())
        finally:
            goalflight_compat.flock(f, goalflight_compat.LOCK_UN)


def _append_steer_message(dispatch_id: str, text: str) -> tuple[Path, dict]:
    path = _steer_file(dispatch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as f:
        goalflight_compat.flock(f, goalflight_compat.LOCK_EX)
        try:
            f.seek(0)
            existing = _parse_steer_lines(f.read().splitlines())
            seq = max((entry["seq"] for entry in existing), default=0) + 1
            entry = {
                "seq": seq,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "text": text,
            }
            f.seek(0, os.SEEK_END)
            f.write(json.dumps(entry, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
            return path, entry
        finally:
            goalflight_compat.flock(f, goalflight_compat.LOCK_UN)


def _acked_steer_seqs(record: dict) -> set[int]:
    acked: set[int] = set()
    for key in ("stdout_path", "status_path"):
        value = record.get(key)
        if not value:
            continue
        path = Path(str(value))
        if not path.exists():
            continue
        if key == "status_path":
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            markers = payload.get("markers") or []
            if isinstance(markers, dict):
                for value in markers.get("STEER-ACK") or []:
                    try:
                        acked.add(int(str(value or "").split()[0]))
                    except (IndexError, ValueError):
                        pass
            else:
                for marker in markers:
                    if not isinstance(marker, dict) or marker.get("kind") != "STEER-ACK":
                        continue
                    try:
                        acked.add(int(str(marker.get("text") or "").split()[0]))
                    except (IndexError, ValueError):
                        pass
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = STEER_ACK_RE.match(line.strip())
            if match:
                acked.add(int(match.group(1)))
    return acked


def _list_steer_messages(dispatch_id: str, record: dict) -> int:
    mailbox = _steer_file(dispatch_id)
    entries = _read_steer_entries(mailbox)
    acked = _acked_steer_seqs(record)
    print(f"steer mailbox: {mailbox}")
    if not entries:
        print("(empty)")
        return 0
    print("seq\tts\tacked\ttext")
    for entry in entries:
        print(
            f"{entry['seq']}\t{entry['ts']}\t"
            f"{str(entry['seq'] in acked).lower()}\t{entry['text']}"
        )
    return 0


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


def _worker_prompt_preamble(agent: str | None) -> str:
    preambles = [STEER_PROMPT_PREAMBLE]
    if agent in {"grok-code", "grok-research"}:
        preambles.append(GROK_EXECUTION_PREAMBLE)
    return "\n\n".join(preambles)


def _materialize_steer_prompt(
    prompt_path: str | None,
    base: Path,
    dispatch_id: str,
    *,
    agent: str | None = None,
) -> str | None:
    if not prompt_path:
        return None
    body_path = Path(prompt_path)
    body = body_path.read_text(encoding="utf-8", errors="replace")
    full_prompt = f"{_worker_prompt_preamble(agent)}\n\n{body}"
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
        ttl_s=lease_ttl_s,
        ram_mb=None,
        reserve_mb=goalflight_capacity.DEFAULT_RESERVE_MB,
        worst_worker_mb=goalflight_capacity.DEFAULT_WORST_WORKER_MB,
        hard_cap=goalflight_capacity.DEFAULT_HARD_CAP,
        max_total=None,
    )
    acquire_out = io.StringIO()
    with contextlib.redirect_stdout(acquire_out):
        rc = goalflight_capacity.cmd_acquire(acquire_args)
    try:
        payload = json.loads(acquire_out.getvalue() or "{}")
    except json.JSONDecodeError:
        payload = {"raw": acquire_out.getvalue()}
    if rc != 0:
        write_status(
            status_json,
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": args.dispatch_id,
                "agent": args.agent,
                "state": "blocked_capacity",
                "reason": payload,
                "worker_alive": False,
                "updated_at": int(time.time()),
            },
        )
        print("DISPATCH-BLOCKED " + json.dumps({"state": "blocked_capacity", "reason": payload}, sort_keys=True), flush=True)
        raise SystemExit(rc)
    return payload.get("lease", {}).get("lease_id")


def _record_ledger(args, *, project_root: Path, prompt_path: str | None, status_json: Path,
                   tail: Path, lease_id: str | None, worker_pid: int | None, state: str) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        goalflight_ledger.cmd_record(
            argparse.Namespace(
                dispatch_id=args.dispatch_id,
                prompt_id=None,
                prompt_path=prompt_path,
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
                os_sandbox_json=json.dumps({"shape": "bash", "read_only": bool(args.read_only)}, sort_keys=True),
                state=state,
                json=True,
            )
        )


def _attach_worker_to_lease(lease_id: str | None, worker_pid: int) -> None:
    if not lease_id:
        return
    with goalflight_capacity.StateLock():
        data = goalflight_capacity.load_state()
        lease = data.get("leases", {}).get(lease_id)
        if lease:
            lease["worker_pid"] = worker_pid
            goalflight_capacity.save_state(data)


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
    ident = None
    for _ in range(20):
        current = goalflight_ledger.process_identity(worker_pid)
        if current:
            ident = current
        if current and current.get("lstart") and current.get("comm"):
            break
        time.sleep(0.05)
    return ident


def _write_pidfile(args, *, worker_pid: int, pgid: int | None, identity: dict | None = None) -> Path | None:
    ident = identity or _process_identity_after_spawn(worker_pid)
    if not ident:
        return None
    pidfile_dir = _pidfile_dir()
    pidfile_dir.mkdir(parents=True, exist_ok=True)
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
    reason: str | None,
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


def _build_acp_cfg(args, *, status_json: Path):
    from goalflight_acp_run import (
        DEFAULT_MAX_TOOL_S,
        DEFAULT_REMOTE_TURN_CANCEL_GRACE_S,
        OS_SANDBOX_OFF,
        normalized_acp_dispatch_cfg,
    )

    project_root = _project_root(args)
    prompt_path = str(Path(args.prompt_file).expanduser()) if args.prompt_file else None
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
        prompt_id=None,
        prompt=prompt_path,
        prompt_text=None if prompt_path else args.prompt,
        prompt_b64=None,
        mode="one-shot",
        idle_timeout=float(args.max_idle_secs or 300.0),
        status_json=str(status_json),
        context_mode="enabled",
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
        cpu_epsilon=0.1,
        json=False,
    )
    return normalized_acp_dispatch_cfg(cfg)


def _default_max_idle_secs(args) -> float:
    if not getattr(args, "read_only", False) and str(getattr(args, "agent", "")) in CODE_WRITER_AGENTS:
        return CODE_WRITER_MAX_IDLE_SECS
    return DEFAULT_MAX_IDLE_SECS


def _apply_max_idle_default(args) -> None:
    if getattr(args, "max_idle_secs", None) is None:
        args.max_idle_secs = _default_max_idle_secs(args)


def _run_acp_shape(args, *, base: Path, account_env: dict[str, str]) -> int:
    from goalflight_acp_run import acp_dispatch_exit_code, run_acp_dispatch

    if not args.dispatch_id:
        args.dispatch_id = (
            _default_dispatch_id(args.agent)
            if goalflight_compat.is_windows()
            else _reserve_auto_dispatch_id(args.agent, base)
        )
    _refuse_reused_nonterminal_dispatch_id(args.dispatch_id)
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
    tail_path = Path(args.tail) if args.tail else base / f"{cfg.dispatch_id}.tail"
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


def build_worker(args, prompt_path, raw_argv: list[str]):
    """Return (argv, stdin_path). Explicit `-- <cmd>` overrides any preset.
    Presets encode the canonical SAFE, non-interactive invocation per worker.
    `prompt_path` is the already-materialized prompt file (or None for raw)."""
    if raw_argv:
        return raw_argv, None  # raw escape hatch; stdin = DEVNULL
    sandbox = "read-only" if args.read_only else "workspace-write"
    model = getattr(args, "model", None)
    if args.agent == "codex":
        argv = ["codex", "exec", "--skip-git-repo-check", "--sandbox", sandbox,
                "-c", "approval_policy=never"]
        if model:
            argv += ["--model", str(model)]
        if args.cwd:
            argv += ["-C", args.cwd]
        argv += ["-"]  # codex reads the prompt from stdin
        return argv, prompt_path
    if args.agent in ("grok-code", "grok-research"):
        # Read the prompt from a FILE, not argv `-p` — long goal-flight prompts
        # (5-20KB) would hit E2BIG / argv truncation (grok review #5).
        # grok-composer-2.5-fast for both grok-code and grok-research:
        # grok-build (grok.com's default; formerly grok-research's default) is
        # broken on this machine — grok-research dies at ~28s with empty output
        # under web-search; grok-composer-2.5-fast validated working live.
        default_model = "grok-composer-2.5-fast"
        argv = ["grok", "--prompt-file", str(prompt_path), "--permission-mode", "acceptEdits"]
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

    parser = argparse.ArgumentParser(
        description="Crash-safe worker dispatch: detached worker + decoupled watcher."
    )
    parser.add_argument("--agent", default="worker",
                        help="Preset (codex|grok-code|grok-research) OR a label when you pass `-- <cmd>`")
    parser.add_argument("--prompt-file", help="Prompt file (preset path)")
    parser.add_argument("--prompt", help="Inline prompt text (preset path; alternative to --prompt-file)")
    parser.add_argument("--cwd", help="Worker working directory")
    parser.add_argument("--model", default=None,
                        help="Worker model id (grok-code/grok-research/codex --model passthrough). "
                             "Default = agent label's own default.")
    parser.add_argument("--read-only", action="store_true",
                        help="Read-only sandbox (review/analysis dispatches)")
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
    parser.add_argument("--stats", nargs="?", const="7d", metavar="WINDOW",
                        help="No-worker stats view over dispatch history; WINDOW is <N>h, <N>d, or bare <N> days.")
    parser.add_argument("--json", action="store_true", help="With --stats, emit machine-readable JSON.")
    parser.add_argument("worker", nargs=argparse.REMAINDER,
                        help="Optional `-- <cmd...>` raw worker (overrides the preset)")
    args = parser.parse_args(argv)
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
            _guard_read_only_write_prompt(args)
            base = _dispatch_base_dir()
            if goalflight_compat.is_windows():
                return _run_acp_shape(args, base=base, account_env={})
            account_env = _resolve_account_env(args)
            return _run_acp_shape(args, base=base, account_env=account_env)
        except DispatchUsageError as e:
            print(f"goalflight_dispatch: {e}", file=sys.stderr)
            return 64

    if goalflight_compat.is_windows():
        return _refuse_windows_dispatch(args)

    try:
        _apply_max_idle_default(args)
        _validate_before_side_effects(args, raw)
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
        _refuse_reused_nonterminal_dispatch_id(args.dispatch_id)
    except DispatchUsageError as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64
    tail = Path(args.tail) if args.tail else base / f"{args.dispatch_id}.tail"
    status_json = Path(args.status_json) if args.status_json else base / f"{args.dispatch_id}.status.json"

    steer_file = _steer_file(args.dispatch_id)
    prompt_path = None if raw else _resolve_prompt_file(args, base)
    if prompt_path:
        prompt_path = _materialize_steer_prompt(prompt_path, base, args.dispatch_id, agent=args.agent)
    worker_argv, stdin_path = build_worker(args, prompt_path, raw)
    if not worker_argv:
        print("goalflight_dispatch: no worker — use `--agent codex --prompt-file X [--cwd .]` "
              "or `-- <cmd...>`", file=sys.stderr)
        return 64

    tail.parent.mkdir(parents=True, exist_ok=True)
    project_root = _project_root(args)
    worker_pid = None
    watcher_pid = None
    caffeinate_pid = None
    pidfile = None
    lease_id = None
    ledger_recorded = False
    final_state = "failed"
    final_reason = None
    worker_alive = None
    watch_rc = 1
    dispatch_started = time.time()
    summary_head = {
        "dispatch_id": args.dispatch_id,
        "agent": args.agent,
        "worker_pid": None,
        "tail": str(tail),
        "status_json": str(status_json),
    }

    try:
        lease_id = _acquire_capacity(args, project_root=project_root, status_json=status_json)
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
        ledger_recorded = True

        # 1. Launch the worker DETACHED, output -> tail (prompt -> stdin for codex).
        # Account guards ran before prompt/id/lease side effects; only apply the
        # resolved environment here.
        env = dict(os.environ)
        env.update(account_env)
        env["GOALFLIGHT_STEER_FILE"] = str(steer_file)
        if args.account and _account_engine(args.agent) == "grok":
            env.pop("GROK_API_KEY", None)
            env.pop("XAI_API_KEY", None)
        if args.account and _account_engine(args.agent) == "cursor":
            env.pop("CURSOR_API_KEY", None)
        if args.billing == "sub" and _account_engine(args.agent) == "codex":
            env.pop("OPENAI_API_KEY", None)  # subscription billing for the selected account, not the API

        worker_pid = _spawn_daemonized_process(
            worker_argv,
            env=env,
            stdin_path=stdin_path,
            stdout_path=tail,
            stdout_mode="wb",
            stderr="stdout",
            label="worker",
        )
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
        try:
            _attach_worker_to_lease(lease_id, worker_pid)
        except Exception as e:
            registration_errors.append(_registration_error("attach_worker_to_lease", e))
        try:
            pidfile = _write_pidfile(args, worker_pid=worker_pid, pgid=pgid, identity=worker_identity)
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
            controller_pid=_controller_pid(args),
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
        watch_identity_token = (
            worker_identity_token
            if worker_identity_token and worker_identity_token.get("lstart") and worker_identity_token.get("comm")
            else None
        )
        if watch_identity_token:
            watch_cmd += ["--worker-identity-json", json.dumps(watch_identity_token, sort_keys=True)]
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
                "pgid": pgid,
                "worker_alive": True,
                "worker_identity": worker_identity_token,
                "expected_worker_identity": worker_identity_token,
                "tail_path": str(tail),
                "state": "starting",
                "reason": "watcher_launching",
                "updated_at": int(time.time()),
            })
        watcher_pid = _spawn_daemonized_process(
            watch_cmd,
            env=os.environ.copy(),
            stdout_path=watch_log,
            stdout_mode="wb",
            stderr="stdout",
            label="watcher",
        )
        summary_head.update({"watcher_pid": watcher_pid, "watcher_log": str(watch_log)})
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

        # Read the terminal state the watcher recorded (best-effort). worker_still_alive
        # matters: a terminal marker is a NON-DESTRUCTIVE signal (we never kill the
        # worker), so if it is still alive the orchestrator should re-attach a watcher to
        # keep following it, not assume it is finished.
        state = None
        try:
            state = rec.get("state")
            worker_alive = rec.get("worker_alive")
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
        if ledger_recorded:
            with contextlib.suppress(Exception):
                final_worker_alive = worker_alive
                if final_worker_alive is None and worker_pid:
                    final_worker_alive = goalflight_compat.pid_alive(worker_pid)
                _finish_ledger(
                    args.dispatch_id,
                    final_state,
                    final_reason,
                    elapsed_s=round(time.time() - dispatch_started, 3),
                    worker_still_alive=final_worker_alive,
                )
        with contextlib.suppress(Exception):
            _release_capacity(lease_id, final_state, final_reason)
        _cleanup_pidfile_if_worker_dead(pidfile, worker_pid)


if __name__ == "__main__":
    raise SystemExit(main())
