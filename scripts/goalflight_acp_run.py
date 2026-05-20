#!/usr/bin/env python3
"""Run one ACP prompt with compact status, capacity, and ledger records."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Callable
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_capacity
import goalflight_ledger
from acp_client import AcpConnection, AcpError
from goalflight_liveness import (
    IdleLivenessGate,
    pgroup_cpu_pct,
    process_group_id,
    write_status,
)
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


def _now() -> int:
    return int(time.time())


def _event_kind(event: dict) -> str:
    if "_prompt_result" in event:
        return "prompt_result"
    return str(event.get("params", {}).get("update", {}).get("sessionUpdate") or event.get("method") or "event")


async def spawn_and_handshake_with_retry(
    command: str,
    acp_args: list[str],
    *,
    agent: str,
    session_id: str,
    cwd: str,
    attempts: int = 2,
    handshake_timeout: float = 60.0,
    auto_allow_tools: bool = True,
    on_attempt: Callable[[int, asyncio.subprocess.Process], Any] | None = None,
) -> tuple[asyncio.subprocess.Process, AcpConnection]:
    """Spawn the worker and run the ACP handshake, retrying once on AcpError.

    The codex-acp wedge is INTERMITTENT — the worker spawns but never answers
    initialize/session_new (0% CPU, empty log, no status JSON), yet the bare
    handshake works in isolation. So a single kill+respawn usually clears it.
    The 0.4.2 handshake timeout is what makes this possible: it turns the
    otherwise-infinite `await fut` into a catchable AcpError.

    On each failed attempt the wedged worker is killed BEFORE respawning, so no
    identity-matched worker PID is ever left alive (the never-retry-while-old-
    PID-alive invariant from the converged design, applied at the handshake).

    on_attempt(attempt_index, proc): optional sync/async callback fired right
    after each spawn (before the handshake) so the caller can publish worker
    pid/pgid into its status JSON per attempt. Returns the (proc, conn) of the
    first successful handshake; raises AcpError if every attempt fails.
    """
    last_err: AcpError | None = None
    for attempt in range(max(1, attempts)):
        proc = await asyncio.create_subprocess_exec(
            command,
            *acp_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
            limit=8 * 1024 * 1024,
        )
        if on_attempt is not None:
            maybe = on_attempt(attempt, proc)
            if inspect.isawaitable(maybe):
                await maybe
        conn = AcpConnection(
            agent=agent, session_id=session_id, proc=proc, auto_allow_tools=auto_allow_tools
        )
        try:
            await conn.initialize(timeout=handshake_timeout)
            await conn.session_new(cwd, timeout=handshake_timeout)
            return proc, conn
        except AcpError as e:
            last_err = e
            # Reap the wedged worker before respawning — never leave an
            # identity-matched PID alive.
            with contextlib.suppress(Exception):
                await conn.kill()
    raise AcpError(f"handshake failed after {max(1, attempts)} attempt(s): {last_err}")


async def run(args: argparse.Namespace) -> dict:
    dispatch_id = args.dispatch_id or f"acp-{args.agent}-{uuid.uuid4().hex[:8]}"
    status_path = Path(args.status_json) if args.status_json else Path(args.cwd) / f".goalflight-{dispatch_id}.status.json"
    payload: dict = {
        "schema": "goalflight.acp-run.v1",
        "dispatch_id": dispatch_id,
        "lease_id": None,
        "agent": args.agent,
        "session_id": args.session_id,
        "state": "starting",
        "ok": False,
        "worker_pid": None,
        "pgid": None,
        "worker_alive": False,
        "pgroup_cpu_pct": None,
        "events_seen": 0,
        "last_event_at": None,
        "last_event_kind": None,
        "heartbeat_at": None,
        "updated_at": _now(),
    }
    status_lock = asyncio.Lock()

    async def update_status(**updates: object) -> None:
        async with status_lock:
            payload.update(updates)
            payload["updated_at"] = _now()
            write_status(status_path, payload)

    write_status(status_path, payload)
    try:
        prompt = Path(args.prompt).read_text() if args.prompt else args.prompt_text
        if not prompt:
            raise ValueError("--prompt or --prompt-text required")
    except Exception as e:
        payload.update({"state": "failed", "error": f"{type(e).__name__}: {e}"})
        write_status(status_path, payload)
        return payload

    # Lease TTL covers the worst-case run length. Derive from idle-timeout
    # (×4 headroom), but when idle-timeout is 0 (no-timeout, mode-dependent),
    # fall back to a generous goal/one-shot ceiling rather than the bare 300
    # default — otherwise a no-timeout 10h goal run would hold only a 1h lease
    # and free its capacity slot mid-run. The same value caps the running_quiet
    # hard wall (idle_gate below) so the runner never outlives its own lease.
    lease_ttl_s = max(int(args.idle_timeout or (36000 if args.mode == "goal" else 300)) * 4, 3600)
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
    if rc != 0:
        try:
            acquire_payload = json.loads(acquire_out.getvalue() or "{}")
        except json.JSONDecodeError:
            acquire_payload = {"raw": acquire_out.getvalue()}
        payload.update({"state": "blocked_capacity", "reason": acquire_payload})
        write_status(status_path, payload)
        return payload

    acquire_payload = json.loads(acquire_out.getvalue())
    lease_id = acquire_payload.get("lease", {}).get("lease_id")
    await update_status(lease_id=lease_id)

    command, acp_args = agent_command(args.agent)
    proc: asyncio.subprocess.Process | None = None
    conn: AcpConnection | None = None
    heartbeat_task: asyncio.Task | None = None
    ledger_recorded = False
    state = "failed"
    events_seen = 0
    # CPU-aware idle gate: keeps a busy-but-silent worker (running_quiet) but
    # enforces a hard wall (lease lifetime) so a pathological CPU spinner that
    # never emits an event can't hang the runner forever.
    idle_gate = IdleLivenessGate(args.cpu_epsilon, lease_ttl_s)

    async def heartbeat_loop() -> None:
        # Created only after a successful handshake, so it tracks the single
        # committed worker. Exit early once that worker exits (grok 2026-05-20
        # P2) rather than sampling a dead pid until the outer finally cancels us.
        while True:
            if proc is None or proc.returncode is not None:
                return
            pgid = payload.get("pgid") or process_group_id(proc.pid) or proc.pid
            cpu_pct = await asyncio.to_thread(pgroup_cpu_pct, pgid)
            await update_status(
                worker_pid=proc.pid,
                pgid=pgid,
                worker_alive=True,
                pgroup_cpu_pct=cpu_pct,
                heartbeat_at=_now(),
            )
            await asyncio.sleep(args.heartbeat_interval)

    async def note_event(event: dict) -> None:
        nonlocal events_seen
        events_seen += 1
        idle_gate.note_event()  # real progress → reset the running_quiet hard wall
        await update_status(
            state="running",
            events_seen=events_seen,
            last_event_at=_now(),
            last_event_kind=_event_kind(event),
            worker_pid=proc.pid if proc else None,
            pgid=payload.get("pgid"),
            worker_alive=(proc.returncode is None) if proc else False,
        )

    async def on_idle_check() -> bool:
        # Runner's CPU-aware liveness gate for session_prompt's idle path — the
        # codex P1 fix. A worker can be silent yet healthy (running_quiet):
        # grinding a long test/compile with no agent_message_chunks. The gate
        # samples process-group CPU (with the transient-ps-failure grace):
        # > epsilon ⇒ keep waiting (the false-positive killer); at/below epsilon
        # (or unsamplable after grace), OR past the running_quiet hard wall ⇒
        # wedged ⇒ let the runner cancel.
        if proc is None or proc.returncode is not None:
            return False
        pgid = payload.get("pgid") or process_group_id(proc.pid) or proc.pid
        keep_waiting, cpu = await idle_gate.keep_waiting(
            lambda: asyncio.to_thread(pgroup_cpu_pct, pgid)
        )
        await update_status(
            state="running_quiet" if keep_waiting else "wedged",
            pgid=pgid,
            pgroup_cpu_pct=cpu,
            worker_alive=(proc.returncode is None),
            heartbeat_at=_now(),
        )
        return keep_waiting

    async def mark_attempt(attempt: int, p: asyncio.subprocess.Process) -> None:
        nonlocal proc
        proc = p  # publish to heartbeat/note_event/on_idle closures + finally
        pgid = process_group_id(p.pid) or p.pid
        updates: dict[str, object] = dict(
            worker_pid=p.pid, pgid=pgid, worker_alive=True, state="handshaking"
        )
        if attempt > 0:
            updates["handshake_attempt"] = attempt + 1
        await update_status(**updates)

    try:
        # Spawn + handshake, retrying once on AcpError (the intermittent
        # codex-acp wedge). The helper kills a wedged worker before respawning,
        # so no identity-matched PID is ever left alive. Status progresses
        # starting → handshaking [→ handshake_attempt=2] → running.
        proc, conn = await spawn_and_handshake_with_retry(
            command,
            acp_args,
            agent=args.agent,
            session_id=args.session_id,
            cwd=args.cwd,
            on_attempt=mark_attempt,
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
        heartbeat_task = asyncio.create_task(heartbeat_loop())
        await update_status(state="running")
        result = await run_prompt(
            conn,
            prompt,
            idle_timeout=args.idle_timeout,
            on_event=note_event,
            on_idle=on_idle_check,
        )
        markers = extract_markers(result.text)
        state = "complete" if result.ok else "failed"
        if markers.get("BLOCKED") or markers.get("USER-NEED") or markers.get("USER-CONFIRM"):
            state = "blocked"
        payload.update({
            "state": state,
            "ok": result.ok,
            "stop_reason": result.stop_reason,
            "error": result.error,
            "markers": markers,
            "last_marker": {kind: values[-1] for kind, values in markers.items() if values} or None,
            "text_excerpt": result.text[-4000:],
            "out_of_scope_writes": result.out_of_scope_writes,
        })
    except Exception as e:
        payload.update({"state": "failed", "error": f"{type(e).__name__}: {e}"})
    finally:
        # State by exit path:
        #   success      → proc + conn = the committed worker.
        #   total-failure (both handshake attempts exhausted) → conn is None and
        #     proc is the LAST attempt (already reaped by the retry helper);
        #     sampling its now-dead CPU below is harmless and records which
        #     worker we gave up on.
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close_gracefully()
        if proc is not None:
            payload.update(
                worker_alive=proc.returncode is None,
                pgid=payload.get("pgid") or process_group_id(proc.pid) or proc.pid,
                heartbeat_at=_now(),
            )
            payload["pgroup_cpu_pct"] = pgroup_cpu_pct(payload.get("pgid"))
        if ledger_recorded:
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_ledger.cmd_finish(argparse.Namespace(dispatch_id=dispatch_id, state=payload.get("state", state), reason=payload.get("error")))
        if lease_id:
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_capacity.cmd_release(argparse.Namespace(lease_id=lease_id, state=payload.get("state", state), reason=payload.get("error"), keep=True))
    write_status(status_path, payload)
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
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=float(os.environ.get("GOALFLIGHT_HEARTBEAT_INTERVAL", "15")),
        help="Seconds between runner status heartbeat samples.",
    )
    parser.add_argument(
        "--cpu-epsilon",
        type=float,
        default=0.1,
        help="Process-group CPU percent above which an event-silent worker "
             "counts as running_quiet (alive) rather than wedged on the idle "
             "path. Matches goalflight_watch.py's default.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    # Derive idle-timeout from mode when not explicitly set. Goal-mode loops
    # run multi-hour and can go silent for long stretches between events; a
    # 5-minute idle ceiling would kill a healthy worker mid-test. 10h is a
    # safe wedge-detector ceiling (10h of TOTAL silence = genuinely stuck).
    if args.idle_timeout is None:
        args.idle_timeout = 36000.0 if args.mode == "goal" else 300.0
    if args.heartbeat_interval <= 0:
        args.heartbeat_interval = 15.0
    args.session_id = args.session_id or f"goalflight-{uuid.uuid4().hex[:8]}"
    payload = asyncio.run(run(args))
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{payload['dispatch_id']}: {payload['state']} status={args.status_json}")
    return 0 if payload.get("state") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
