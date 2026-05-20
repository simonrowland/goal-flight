#!/usr/bin/env python3
"""Run one ACP prompt with compact status, capacity, and ledger records."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_capacity
import goalflight_ledger
from acp_client import AcpConnection
from acp_runner import extract_markers, run_prompt


def agent_command(agent: str) -> tuple[str, list[str]]:
    if agent == "grok":
        binary = shutil.which("grok") or str(Path.home() / ".grok/bin/grok")
        return binary, ["agent", "stdio"]
    if agent == "cursor":
        return "cursor-agent", ["acp"]
    if agent == "claude":
        return "claude-code-cli-acp", []
    return agent, []


async def run(args: argparse.Namespace) -> dict:
    dispatch_id = args.dispatch_id or f"acp-{args.agent}-{uuid.uuid4().hex[:8]}"
    status_path = Path(args.status_json) if args.status_json else Path(args.cwd) / f".goalflight-{dispatch_id}.status.json"
    try:
        prompt = Path(args.prompt).read_text() if args.prompt else args.prompt_text
        if not prompt:
            raise ValueError("--prompt or --prompt-text required")
    except Exception as e:
        payload = {
            "schema": "goalflight.acp-run.v1",
            "state": "failed",
            "dispatch_id": dispatch_id,
            "agent": args.agent,
            "error": f"{type(e).__name__}: {e}",
        }
        status_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return payload

    acquire_args = argparse.Namespace(
        agent=args.agent,
        dispatch_id=dispatch_id,
        prompt_id=args.prompt_id,
        project_root=args.cwd,
        controller_pid=os.getpid(),
        worker_pid=None,
        lease_id=None,
        mem_mb=None,
        agent_cap=None,
        # Lease TTL covers the worst-case run length. Derive from idle-timeout
        # (×4 headroom), but when idle-timeout is 0 (no-timeout, mode-dependent),
        # fall back to a generous goal/one-shot ceiling rather than the bare 300
        # default — otherwise a no-timeout 10h goal run would hold only a 1h lease
        # and free its capacity slot mid-run.
        ttl_s=max(int(args.idle_timeout or (36000 if args.mode == "goal" else 300)) * 4, 3600),
        ram_mb=None,
        reserve_mb=goalflight_capacity.DEFAULT_RESERVE_MB,
        worst_worker_mb=goalflight_capacity.DEFAULT_WORST_WORKER_MB,
        hard_cap=goalflight_capacity.DEFAULT_HARD_CAP,
        max_total=None,
    )
    acquire_out = io.StringIO()
    with contextlib.redirect_stdout(acquire_out):
        rc = goalflight_capacity.cmd_acquire(acquire_args)
    if rc != 0:
        try:
            acquire_payload = json.loads(acquire_out.getvalue() or "{}")
        except json.JSONDecodeError:
            acquire_payload = {"raw": acquire_out.getvalue()}
        payload = {
            "schema": "goalflight.acp-run.v1",
            "state": "blocked_capacity",
            "dispatch_id": dispatch_id,
            "agent": args.agent,
            "reason": acquire_payload,
        }
        status_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return payload

    acquire_payload = json.loads(acquire_out.getvalue())
    lease_id = acquire_payload.get("lease", {}).get("lease_id")

    command, acp_args = agent_command(args.agent)
    proc = None
    ledger_recorded = False
    state = "failed"
    payload: dict = {
        "schema": "goalflight.acp-run.v1",
        "dispatch_id": dispatch_id,
        "lease_id": lease_id,
        "agent": args.agent,
        "session_id": args.session_id,
        "state": state,
        "ok": False,
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            *acp_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=args.cwd,
            start_new_session=True,
            limit=8 * 1024 * 1024,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            goalflight_ledger.cmd_record(
                argparse.Namespace(
                    dispatch_id=dispatch_id,
                    prompt_id=args.prompt_id,
                    prompt_path=args.prompt,
                    agent=args.agent,
                    transport="acp",
                    project_root=args.cwd,
                    controller_pid=os.getpid(),
                    worker_pid=proc.pid,
                    acp_session_id=args.session_id,
                    logical_session_id=args.session_id,
                    lease_id=lease_id,
                    stdout_path=None,
                    stderr_path=None,
                    status_path=str(status_path),
                    state="running",
                    json=True,
                )
            )
        ledger_recorded = True
        async with AcpConnection(agent=args.agent, session_id=args.session_id, proc=proc, auto_allow_tools=True) as conn:
            await conn.initialize()
            await conn.session_new(args.cwd)
            result = await run_prompt(conn, prompt, idle_timeout=args.idle_timeout)
            markers = extract_markers(result.text)
            state = "complete" if result.ok else "failed"
            if markers.get("BLOCKED") or markers.get("USER-NEED") or markers.get("USER-CONFIRM"):
                state = "blocked"
            payload = {
                "schema": "goalflight.acp-run.v1",
                "dispatch_id": dispatch_id,
                "lease_id": lease_id,
                "agent": args.agent,
                "session_id": args.session_id,
                "state": state,
                "ok": result.ok,
                "stop_reason": result.stop_reason,
                "error": result.error,
                "markers": markers,
                "text_excerpt": result.text[-4000:],
                "out_of_scope_writes": result.out_of_scope_writes,
            }
    except Exception as e:
        payload.update({"state": "failed", "error": f"{type(e).__name__}: {e}"})
    finally:
        if ledger_recorded:
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_ledger.cmd_finish(argparse.Namespace(dispatch_id=dispatch_id, state=payload.get("state", state), reason=payload.get("error")))
        if lease_id:
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_capacity.cmd_release(argparse.Namespace(lease_id=lease_id, state=payload.get("state", state), reason=payload.get("error"), keep=True))
    status_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight ACP runner")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--dispatch-id")
    parser.add_argument("--prompt-id")
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-text")
    parser.add_argument(
        "--mode",
        choices=["one-shot", "goal"],
        default="one-shot",
        help="Dispatch mode. 'goal' raises the default idle-timeout to tolerate the "
             "long silent stretches of multi-hour goal-mode loops (a worker churning "
             "through a big test/compile may emit no events for tens of minutes). "
             "'one-shot' keeps a tight default so a wedged short dispatch is caught fast.",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        help="Seconds of zero agent events before giving up. Idle, NOT total runtime — "
             "the timer resets on every event. If unset, derived from --mode: "
             "one-shot=300 (5min), goal=36000 (10h). Pass 0 for no idle timeout "
             "(rely on PID liveness + the worker's terminal marker instead).",
    )
    parser.add_argument("--status-json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    # Derive idle-timeout from mode when not explicitly set. Goal-mode loops
    # run multi-hour and can go silent for long stretches between events; a
    # 5-minute idle ceiling would kill a healthy worker mid-test. 10h is a
    # safe wedge-detector ceiling (10h of TOTAL silence = genuinely stuck).
    if args.idle_timeout is None:
        args.idle_timeout = 36000.0 if args.mode == "goal" else 300.0
    args.session_id = args.session_id or f"goalflight-{uuid.uuid4().hex[:8]}"
    payload = asyncio.run(run(args))
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{payload['dispatch_id']}: {payload['state']} status={args.status_json}")
    return 0 if payload.get("state") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
