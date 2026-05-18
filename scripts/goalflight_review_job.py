#!/usr/bin/env python3
"""File-backed review runner with capacity/ledger integration."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_capacity
import goalflight_ledger


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
    if "unauthorized" in text or "auth" in text and "error" in text:
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
        ttl_s=max(args.timeout_s * 2, 3600),
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
        status_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
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
        status_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if lease_id:
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_capacity.cmd_release(argparse.Namespace(lease_id=lease_id, state="failed", reason=payload["error"], keep=True))
        print(json.dumps(payload, sort_keys=True) if args.json else f"{args.name}: failed status={status_path}")
        return 1

    started = time.time()
    timed_out = False
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
            status_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
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
        try:
            proc.communicate(prompt_text, timeout=args.timeout_s)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.communicate()
            returncode = proc.returncode

    stderr = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
    stdout = stdout_path.read_text(errors="replace")[:4000] if stdout_path.exists() else ""
    state = classify(stderr, stdout, returncode, timed_out, final_path)
    payload = {
        "schema": "goalflight.review-job.v1",
        "dispatch_id": dispatch_id,
        "lease_id": lease_id,
        "agent": args.agent,
        "name": args.name,
        "state": state,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_s": round(time.time() - started, 3),
        "prompt_path": args.prompt,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "final_path": str(final_path) if final_path else None,
    }
    status_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
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
