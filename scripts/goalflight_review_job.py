#!/usr/bin/env python3
"""File-backed review runner with capacity/ledger integration."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat
import goalflight_capacity
import goalflight_ledger
from goalflight_liveness import (
    active_monotonic,
    pgroup_cpu_pct,
    process_group_id,
    system_sleep_pause_note,
    system_sleep_pause_s,
    write_status,
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _file_size(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_tail(path: Path, max_bytes: int = 65536) -> str:
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - max_bytes))
            return f.read(max_bytes).decode(errors="replace")
    except OSError:
        return ""


def _event_kind(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    for key in ("type", "event", "event_type", "name"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("msg", "message", "item", "delta", "payload"):
        nested = obj.get(key)
        nested_kind = _event_kind(nested)
        if nested_kind:
            return f"{key}.{nested_kind}"
    return None


class JsonlProgress:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.partial = ""
        self.events_seen = 0
        self.parse_errors = 0
        self.last_event_kind: str | None = None
        self.last_event_at: str | None = None

    def scan(self) -> bool:
        if not self.path.exists():
            return False
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                data = f.read()
                self.offset = f.tell()
        except OSError:
            return False
        if not data:
            return False

        text = data.decode(errors="replace")
        self.partial += text
        lines = self.partial.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.partial = lines.pop()
        else:
            self.partial = ""

        progressed = True
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                self.parse_errors += 1
                continue
            self.events_seen += 1
            self.last_event_at = _now()
            self.last_event_kind = _event_kind(obj)
        return progressed


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    return goalflight_compat.pid_alive(pid)


def _pgroup_has_live_processes(pgid: int | None) -> bool:
    if pgid is None:
        return False
    try:
        output = subprocess.check_output(
            ["ps", "-A", "-o", "pgid="],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    target = str(int(pgid))
    return any(line.strip() == target for line in output.splitlines())


def _terminate_process_group(proc: subprocess.Popen[str], pgid: int | None, grace_s: float = 5.0) -> bool:
    target = pgid or process_group_id(proc.pid) or proc.pid
    signalled = False
    try:
        os.killpg(target, signal.SIGTERM)
        signalled = True
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(Exception):
            proc.terminate()
    if proc.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=grace_s)
    elif signalled:
        time.sleep(min(0.25, grace_s))
    if proc.poll() is None or _pgroup_has_live_processes(target):
        try:
            os.killpg(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(Exception):
                proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=grace_s)
    deadline = time.time() + max(0.1, grace_s)
    while time.time() < deadline:
        if not _pgroup_has_live_processes(target):
            return True
        time.sleep(0.05)
    return not _pgroup_has_live_processes(target)


def _send_prompt(proc: subprocess.Popen[str], prompt_text: str) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(prompt_text)
        proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        with contextlib.suppress(Exception):
            proc.stdin.close()


def _start_prompt_writer(proc: subprocess.Popen[str], prompt_text: str) -> tuple[dict[str, Any], threading.Thread]:
    state: dict[str, Any] = {"done": False, "error": None}

    def _run() -> None:
        try:
            _send_prompt(proc, prompt_text)
        except Exception as e:  # pragma: no cover - defensive; _send_prompt is intentionally tolerant.
            state["error"] = f"{type(e).__name__}: {e}"
        finally:
            state["done"] = True

    thread = threading.Thread(target=_run, name=f"goalflight-review-stdin-{proc.pid}", daemon=True)
    thread.start()
    return state, thread


def _monitor_process(
    *,
    proc: subprocess.Popen[str],
    prompt_text: str,
    status_path: Path,
    payload: dict[str, Any],
    stdout_path: Path,
    stderr_path: Path,
    final_path: Path | None,
    timeout_s: int,
    max_quiet_s: float,
    max_total_s: float,
    heartbeat_interval: float,
    cpu_epsilon: float,
    started_wall: float,
) -> dict[str, Any]:
    prompt_state, prompt_thread = _start_prompt_writer(proc, prompt_text)

    pgid = process_group_id(proc.pid) or proc.pid
    stdout_progress = JsonlProgress(stdout_path)
    last_stdout_bytes = _file_size(stdout_path)
    last_stderr_bytes = _file_size(stderr_path)
    last_final_bytes = _file_size(final_path)
    progress_seen = 0
    if last_stdout_bytes or last_stderr_bytes or last_final_bytes:
        progress_seen = 1
    started_active = active_monotonic()
    last_progress_active = started_active
    last_progress_at = _now()
    prev_wall = time.time()
    prev_active = started_active
    total_paused_s = 0.0
    timed_out = False
    timeout_reason: str | None = None
    process_group_drained: bool | None = None
    last_cpu: float | None = None
    soft_timeout_elapsed = False
    heartbeat_interval = max(0.1, heartbeat_interval)

    while True:
        returncode = proc.poll()
        wall_now = time.time()
        active_now = active_monotonic()
        freeze_s = system_sleep_pause_s(
            prev_wall=prev_wall,
            prev_active=prev_active,
            wall_now=wall_now,
            active_now=active_now,
            heartbeat_interval_s=heartbeat_interval,
        )
        if freeze_s > 0:
            total_paused_s += freeze_s
            last_progress_active += freeze_s
        prev_wall, prev_active = wall_now, active_now

        stdout_changed = stdout_progress.scan()
        stdout_bytes = _file_size(stdout_path)
        stderr_bytes = _file_size(stderr_path)
        final_bytes = _file_size(final_path)
        byte_progress = (
            stdout_bytes != last_stdout_bytes
            or stderr_bytes != last_stderr_bytes
            or final_bytes != last_final_bytes
        )
        final_detected = final_path is not None and final_bytes > 0
        if stdout_changed or byte_progress:
            progress_seen += 1
            last_progress_active = active_now
            last_progress_at = _now()
            last_stdout_bytes = stdout_bytes
            last_stderr_bytes = stderr_bytes
            last_final_bytes = final_bytes

        worker_alive = returncode is None and _alive(proc.pid)
        last_cpu = pgroup_cpu_pct(pgid) if worker_alive else 0.0
        duration_s = max(0.0, wall_now - started_wall)
        quiet_for_s = max(0.0, active_now - last_progress_active)
        soft_timeout_elapsed = timeout_s > 0 and duration_s >= timeout_s
        cpu_active = last_cpu is not None and last_cpu > cpu_epsilon

        running_state = "running"
        if final_detected and worker_alive:
            running_state = "final_detected"
        elif soft_timeout_elapsed and worker_alive:
            running_state = "running_past_timeout" if (cpu_active or quiet_for_s < max_quiet_s) else "running_quiet"
        elif worker_alive and quiet_for_s >= max_quiet_s and cpu_active:
            running_state = "running_quiet"

        note = system_sleep_pause_note(freeze_s, total_paused_s) if freeze_s > 0 else None
        payload.update(
            {
                "state": running_state if worker_alive else "worker_exited",
                "returncode": returncode,
                "worker_pid": proc.pid,
                "pgid": pgid,
                "worker_alive": worker_alive,
                "pgroup_cpu_pct": last_cpu,
                "events_seen": stdout_progress.events_seen,
                "stdout_json_parse_errors": stdout_progress.parse_errors,
                "last_event_at": stdout_progress.last_event_at,
                "last_event_kind": stdout_progress.last_event_kind,
                "last_progress_at": last_progress_at,
                "progress_seen": progress_seen,
                "quiet_for_s": round(quiet_for_s, 3),
                "duration_s": round(duration_s, 3),
                "timeout_s": timeout_s,
                "soft_timeout_elapsed": soft_timeout_elapsed,
                "max_quiet_s": max_quiet_s,
                "max_total_s": max_total_s,
                "cpu_epsilon": cpu_epsilon,
                "total_paused_s": round(total_paused_s, 3),
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": stderr_bytes,
                "final_bytes": final_bytes,
                "final_detected": final_detected,
                "prompt_stdin_done": bool(prompt_state.get("done")),
                "prompt_stdin_error": prompt_state.get("error"),
                "heartbeat_at": _now(),
                "updated_at": _now(),
            }
        )
        if note:
            payload["note"] = note
        write_status(status_path, payload)

        if returncode is not None:
            if _pgroup_has_live_processes(pgid):
                timed_out = True
                timeout_reason = "process_group_alive_after_parent_exit"
                process_group_drained = _terminate_process_group(proc, pgid)
            break

        if max_total_s > 0 and duration_s >= max_total_s:
            timed_out = True
            timeout_reason = "max_total_s"
            process_group_drained = _terminate_process_group(proc, pgid)
            break

        if max_quiet_s > 0 and quiet_for_s >= max_quiet_s and not cpu_active:
            timed_out = True
            timeout_reason = "no_progress_timeout"
            process_group_drained = _terminate_process_group(proc, pgid)
            break

        time.sleep(heartbeat_interval)

    returncode = proc.poll()
    prompt_thread.join(timeout=0.1)
    payload.update(
        {
            "returncode": returncode,
            "timed_out": timed_out,
            "timeout_reason": timeout_reason,
            "process_group_drained": process_group_drained,
            "worker_alive": returncode is None and _alive(proc.pid),
            "pgroup_cpu_pct": last_cpu,
            "duration_s": round(time.time() - started_wall, 3),
            "soft_timeout_elapsed": soft_timeout_elapsed,
            "stdout_bytes": _file_size(stdout_path),
            "stderr_bytes": _file_size(stderr_path),
            "final_bytes": _file_size(final_path),
            "final_detected": final_path is not None and _file_size(final_path) > 0,
            "prompt_stdin_done": bool(prompt_state.get("done")),
            "prompt_stdin_error": prompt_state.get("error"),
            "heartbeat_at": _now(),
        }
    )
    write_status(status_path, payload)
    return payload


def classify(stderr: str, stdout: str, returncode: int | None, timed_out: bool, final_path: Path | None = None) -> str:
    if timed_out:
        return "inconclusive_timeout"
    if returncode == 0 and final_path is not None and (not final_path.exists() or final_path.stat().st_size == 0):
        return "inconclusive_no_final"
    if returncode == 0:
        return "complete"
    text = f"{stderr}\n{stdout}".lower()
    if "session limit" in text or "usage limit" in text:
        return "blocked_session_limit"
    auth_signals = (
        "unauthorized",
        "not authenticated",
        "authentication failed",
        "authentication error",
        "auth failed",
        "auth error",
        "login required",
        "please log in",
    )
    if any(signal in text for signal in auth_signals):
        return "blocked_auth"
    return "failed"


def command_for(args: argparse.Namespace) -> list[str]:
    if args.agent == "codex":
        final = Path(args.output_dir) / f"{args.name}.final.md"
        return [
            args.codex_bin,
            "exec",
            "--json",
            "-o",
            str(final),
            "-C",
            args.repo,
            "-s",
            "read-only",
            "-",
        ]
    if args.agent == "claude":
        return [
            args.claude_bin,
            "-p",
            "--model",
            args.model,
            "--output-format",
            "json",
            "--disable-slash-commands",
            "--tools",
            "",
            "--effort",
            args.effort,
        ]
    if args.command:
        return args.command
    raise ValueError(f"unsupported agent or missing --command: {args.agent}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight file-backed review job")
    parser.add_argument("--agent", choices=["codex", "claude", "custom"], required=True)
    parser.add_argument("--name", default="review")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--max-quiet-s", type=float, default=3600.0)
    parser.add_argument("--max-total-s", type=float, default=0.0)
    parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    parser.add_argument("--cpu-epsilon", type=float, default=0.1)
    parser.add_argument("--model", default="opus")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--codex-bin", default="/opt/homebrew/bin/codex")
    parser.add_argument("--claude-bin", default="/opt/homebrew/bin/claude")
    parser.add_argument("--command", nargs=argparse.REMAINDER)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / f"{args.name}.stdout.jsonl"
    stderr_path = out_dir / f"{args.name}.stderr.log"
    status_path = out_dir / f"{args.name}.status.json"
    dispatch_id = f"review-{args.name}-{uuid.uuid4().hex[:8]}"
    final_path = Path(args.output_dir) / f"{args.name}.final.md" if args.agent == "codex" else None

    lease_ttl_s = max(int(max(args.timeout_s, args.max_quiet_s, args.max_total_s or 0) * 4), 7200)
    acquire_argv = argparse.Namespace(
        agent=args.agent,
        dispatch_id=dispatch_id,
        prompt_id=args.name,
        project_root=args.repo,
        controller_pid=os.getpid(),
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
        rc = goalflight_capacity.cmd_acquire(acquire_argv)
    if rc != 0:
        try:
            acquire_payload = json.loads(acquire_out.getvalue() or "{}")
        except json.JSONDecodeError:
            acquire_payload = {"raw": acquire_out.getvalue()}
        payload = {
            "schema": "goalflight.review-job.v1",
            "dispatch_id": dispatch_id,
            "agent": args.agent,
            "name": args.name,
            "state": "blocked_capacity",
            "reason": acquire_payload,
            "prompt_path": args.prompt,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "final_path": str(final_path) if final_path else None,
        }
        write_status(status_path, payload)
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"{args.name}: blocked_capacity status={status_path}")
        return rc

    acquire_payload = json.loads(acquire_out.getvalue())
    lease_id = acquire_payload.get("lease", {}).get("lease_id")

    try:
        cmd = command_for(args)
        prompt_text = Path(args.prompt).read_text()
    except Exception as e:
        payload = {
            "schema": "goalflight.review-job.v1",
            "dispatch_id": dispatch_id,
            "lease_id": lease_id,
            "agent": args.agent,
            "name": args.name,
            "state": "failed",
            "error": f"{type(e).__name__}: {e}",
            "prompt_path": args.prompt,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "final_path": str(final_path) if final_path else None,
        }
        write_status(status_path, payload)
        if lease_id:
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_capacity.cmd_release(argparse.Namespace(lease_id=lease_id, state="failed", reason=payload["error"], keep=True))
        print(json.dumps(payload, sort_keys=True) if args.json else f"{args.name}: failed status={status_path}")
        return 1

    started = time.time()
    returncode: int | None = None
    ledger_recorded = False
    with stdout_path.open("w") as stdout_f, stderr_path.open("w") as stderr_f:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=args.repo,
                stdin=subprocess.PIPE,
                stdout=stdout_f,
                stderr=stderr_f,
                text=True,
                start_new_session=True,
            )
        except Exception as e:
            payload = {
                "schema": "goalflight.review-job.v1",
                "dispatch_id": dispatch_id,
                "lease_id": lease_id,
                "agent": args.agent,
                "name": args.name,
                "state": "failed",
                "error": f"{type(e).__name__}: {e}",
                "duration_s": round(time.time() - started, 3),
                "prompt_path": args.prompt,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "final_path": str(final_path) if final_path else None,
            }
            write_status(status_path, payload)
            if lease_id:
                with contextlib.redirect_stdout(io.StringIO()):
                    goalflight_capacity.cmd_release(argparse.Namespace(lease_id=lease_id, state="failed", reason=payload["error"], keep=True))
            print(json.dumps(payload, sort_keys=True) if args.json else f"{args.name}: failed status={status_path}")
            return 1
        with contextlib.redirect_stdout(io.StringIO()):
            goalflight_ledger.cmd_record(
                argparse.Namespace(
                    dispatch_id=dispatch_id,
                    prompt_id=args.name,
                    prompt_path=args.prompt,
                    agent=args.agent,
                    transport="file-backed-review",
                    project_root=args.repo,
                    controller_pid=os.getpid(),
                    worker_pid=proc.pid,
                    acp_session_id=None,
                    logical_session_id=None,
                    lease_id=lease_id,
                    stdout_path=str(stdout_path),
                    stderr_path=str(stderr_path),
                    status_path=str(status_path),
                    state="running",
                    json=True,
                )
            )
        ledger_recorded = True
        payload = {
            "schema": "goalflight.review-job.v1",
            "dispatch_id": dispatch_id,
            "lease_id": lease_id,
            "agent": args.agent,
            "name": args.name,
            "state": "running",
            "returncode": None,
            "timed_out": False,
            "timeout_reason": None,
            "process_group_drained": None,
            "worker_pid": proc.pid,
            "pgid": process_group_id(proc.pid) or proc.pid,
            "worker_alive": True,
            "pgroup_cpu_pct": None,
            "events_seen": 0,
            "stdout_json_parse_errors": 0,
            "last_event_at": None,
            "last_event_kind": None,
            "last_progress_at": _now(),
            "progress_seen": 0,
            "quiet_for_s": 0.0,
            "duration_s": 0.0,
            "timeout_s": args.timeout_s,
            "soft_timeout_elapsed": False,
            "max_quiet_s": args.max_quiet_s,
            "max_total_s": args.max_total_s,
            "cpu_epsilon": args.cpu_epsilon,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "final_bytes": 0,
            "final_detected": False,
            "prompt_stdin_done": False,
            "prompt_stdin_error": None,
            "prompt_path": args.prompt,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "final_path": str(final_path) if final_path else None,
            "updated_at": _now(),
            "heartbeat_at": _now(),
        }
        write_status(status_path, payload)
        monitor_payload = _monitor_process(
            proc=proc,
            prompt_text=prompt_text,
            status_path=status_path,
            payload=payload,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            final_path=final_path,
            timeout_s=args.timeout_s,
            max_quiet_s=args.max_quiet_s,
            max_total_s=args.max_total_s,
            heartbeat_interval=args.heartbeat_interval,
            cpu_epsilon=args.cpu_epsilon,
            started_wall=started,
        )
        returncode = monitor_payload.get("returncode")

    stderr = _read_tail(stderr_path)
    stdout = _read_tail(stdout_path, max_bytes=4000)
    timed_out = bool(monitor_payload.get("timed_out"))
    state = classify(stderr, stdout, returncode, timed_out, final_path)
    payload.update(
        {
            "state": state,
            "ok": state == "complete",
            "returncode": returncode,
            "timed_out": timed_out,
            "duration_s": round(time.time() - started, 3),
            "updated_at": _now(),
            "heartbeat_at": _now(),
        }
    )
    write_status(status_path, payload)
    if ledger_recorded:
        with contextlib.redirect_stdout(io.StringIO()):
            goalflight_ledger.cmd_finish(argparse.Namespace(dispatch_id=dispatch_id, state=state, reason=None))
    if lease_id:
        with contextlib.redirect_stdout(io.StringIO()):
            goalflight_capacity.cmd_release(argparse.Namespace(lease_id=lease_id, state=state, reason=None, keep=True))
    if state == "blocked_session_limit":
        with contextlib.redirect_stdout(io.StringIO()):
            goalflight_capacity.cmd_cooldown(argparse.Namespace(action="set", agent=args.agent, seconds=3600, reason="session_limit"))
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{args.name}: {state} rc={returncode} status={status_path}")
    return 0 if state == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
