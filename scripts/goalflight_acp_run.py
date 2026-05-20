#!/usr/bin/env python3
"""Run one ACP prompt with compact status, capacity, and ledger records."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass, field
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
    heartbeat_wedge_decision,
    IdleLivenessGate,
    pgroup_cpu_pct,
    process_group_id,
    write_status,
)
from goalflight_startup_gate import StartupGate
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


TERMINAL_TOOL_STATUSES = {"completed", "failed"}


def _tool_id(payload: dict) -> str | None:
    value = payload.get("toolCallId") or payload.get("id")
    return str(value) if value else None


def _tool_status(payload: dict) -> str | None:
    value = payload.get("status")
    return str(value).lower() if value is not None else None


@dataclass
class ToolActivity:
    events_seen: int = 0
    last_event_mono: float = field(default_factory=time.monotonic)
    outstanding_tools: dict[str, float] = field(default_factory=dict)
    pending_permissions: dict[str, float] = field(default_factory=dict)

    @property
    def outstanding_count(self) -> int:
        return len(self.outstanding_tools) + len(self.pending_permissions)

    def timed_out(self, now: float, max_tool_s: float) -> tuple[str, float] | None:
        if max_tool_s <= 0:
            return None
        for tool_id, started_at in {**self.outstanding_tools, **self.pending_permissions}.items():
            age = now - started_at
            if age >= max_tool_s:
                return tool_id, age
        return None


def _apply_tool_activity(event: dict, activity: ToolActivity, now: float) -> None:
    update = event.get("params", {}).get("update", {}) or {}
    kind = update.get("sessionUpdate")
    if kind == "tool_call":
        tool_id = _tool_id(update)
        status = _tool_status(update)
        if tool_id and status in TERMINAL_TOOL_STATUSES:
            activity.outstanding_tools.pop(tool_id, None)
        elif tool_id:
            activity.outstanding_tools.setdefault(tool_id, now)
    elif kind == "tool_call_update":
        tool_id = _tool_id(update)
        if tool_id and _tool_status(update) in TERMINAL_TOOL_STATUSES:
            activity.outstanding_tools.pop(tool_id, None)

    if event.get("method") == "session/request_permission":
        params = event.get("params", {}) or {}
        tool_call = params.get("toolCall", {}) or {}
        tool_id = _tool_id(tool_call) or _tool_id(params)
        status = _tool_status(tool_call) or _tool_status(params)
        if tool_id and status in (None, "pending"):
            activity.pending_permissions.setdefault(tool_id, now)
    elif activity.pending_permissions:
        # Permission requests are auto-allowed by AcpConnection. Any later event
        # means that transient liveness signal resolved; the real tool id remains
        # tracked independently when the adapter reports one.
        activity.pending_permissions.clear()


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
    # and free its capacity slot mid-run. The running_quiet hard wall is now
    # the separate --max-quiet-s knob; capacity TTL stays a lease concern.
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
    activity = ToolActivity()
    event_lock = asyncio.Lock()
    heartbeat_outcome: str | None = None
    heartbeat_error: dict[str, object] | None = None
    wedged_by_heartbeat = False
    # CPU-aware idle gate: keeps a busy-but-silent worker (running_quiet) but
    # enforces a hard wall (lease lifetime) so a pathological CPU spinner that
    # never emits an event can't hang the runner forever.
    idle_gate = IdleLivenessGate(args.cpu_epsilon, args.max_quiet_s)

    async def mark_heartbeat_terminal(outcome: str, error: dict[str, object]) -> None:
        nonlocal heartbeat_outcome, heartbeat_error, wedged_by_heartbeat
        if conn is not None:
            setattr(conn, "killed_by_heartbeat", True)
            setattr(conn, "heartbeat_outcome", outcome)
        async with status_lock:
            heartbeat_outcome = outcome
            heartbeat_error = error
            wedged_by_heartbeat = outcome == "wedged"
            payload.update(
                state=outcome,
                ok=False,
                error=error,
                killed_by_heartbeat=True,
                wedged_by_heartbeat=wedged_by_heartbeat,
                updated_at=_now(),
            )
            write_status(status_path, payload)

    async def heartbeat_loop() -> None:
        nonlocal heartbeat_outcome
        dead_samples = 0
        last_sample_events_seen = 0
        # Created only after a successful handshake, so it tracks the single
        # committed worker. Exit early once that worker exits (grok 2026-05-20
        # P2) rather than sampling a dead pid until the outer finally cancels us.
        while True:
            if proc is None or proc.returncode is not None:
                return
            pgid = payload.get("pgid") or process_group_id(proc.pid) or proc.pid
            cpu_pct = await asyncio.to_thread(pgroup_cpu_pct, pgid)
            now_mono = time.monotonic()
            async with event_lock:
                seen = activity.events_seen
                outstanding_count = activity.outstanding_count
                quiet_for_s = now_mono - activity.last_event_mono
                timed_out_tool = activity.timed_out(now_mono, args.max_tool_s)
            pid_alive = proc.returncode is None
            # Per-tool wall is ABSOLUTE: a tool outstanding longer than
            # --max-tool-s is stuck regardless of CPU (it may be CPU-busy in a
            # hung retry loop, or CPU may be unsamplable). Gating it on CPU≤ε
            # would let those hang forever (codex 2026-05-20 P1). The grace
            # (don't wedge while a tool is outstanding) still applies UP TO the
            # wall; past it, the tool itself is the wedge.
            if timed_out_tool is not None and pid_alive:
                tool_id, age_s = timed_out_tool
                await mark_heartbeat_terminal(
                    "tool_timeout",
                    {
                        "code": -1,
                        "message": "tool_timeout",
                        "toolCallId": tool_id,
                        "age_s": round(age_s, 3),
                    },
                )
                await conn.kill()
                return
            if (
                args.max_quiet_s > 0
                and outstanding_count == 0
                and quiet_for_s >= args.max_quiet_s
                and pid_alive
            ):
                await mark_heartbeat_terminal(
                    "wedged",
                    {"code": -1, "message": "max_quiet_s"},
                )
                await conn.kill()
                return
            decision = heartbeat_wedge_decision(
                pid_alive=pid_alive,
                pgroup_cpu=cpu_pct,
                events_seen=seen,
                previous_events_seen=last_sample_events_seen,
                outstanding_count=outstanding_count,
                cpu_epsilon_pct=args.cpu_epsilon,
                previous_dead_samples=dead_samples,
                wedge_samples=args.wedge_samples,
            )
            dead_samples = decision.dead_samples
            last_sample_events_seen = seen
            await update_status(
                worker_pid=proc.pid,
                pgid=pgid,
                worker_alive=pid_alive,
                pgroup_cpu_pct=cpu_pct,
                heartbeat_at=_now(),
                heartbeat_dead_samples=dead_samples,
                outstanding_tool_calls=outstanding_count,
                quiet_for_s=round(quiet_for_s, 3),
            )
            if decision.wedged:
                await mark_heartbeat_terminal(
                    "wedged",
                    {"code": -1, "message": "wedged_by_heartbeat"},
                )
                await conn.kill()
                return
            await asyncio.sleep(args.heartbeat_interval)

    async def note_event(event: dict) -> None:
        now_mono = time.monotonic()
        idle_gate.note_event()  # real progress → reset the running_quiet hard wall
        async with event_lock:
            activity.events_seen += 1
            activity.last_event_mono = now_mono
            _apply_tool_activity(event, activity, now_mono)
            seen = activity.events_seen
            outstanding_count = activity.outstanding_count
        await update_status(
            state="running",
            events_seen=seen,
            last_event_at=_now(),
            last_event_kind=_event_kind(event),
            outstanding_tool_calls=outstanding_count,
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
        # StartupGate serializes the heavy startup of fragile adapters (the
        # Claude TUI) so concurrent launches don't starve each other on init;
        # it releases the instant the handshake completes, so TURNS overlap.
        async with StartupGate(args.agent):
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
        async with status_lock:
            terminal_by_heartbeat = heartbeat_outcome or getattr(conn, "heartbeat_outcome", None)
            terminal_error = heartbeat_error
        if terminal_by_heartbeat or getattr(conn, "killed_by_heartbeat", False):
            state = terminal_by_heartbeat or "wedged"
            error = terminal_error or {"code": -1, "message": state}
        elif result.error and result.error.get("message") == "result_too_large":
            state = "result_too_large"
            error = result.error
        else:
            state = "complete" if result.ok else "failed"
            error = result.error
        if state == "complete" and (
            markers.get("BLOCKED") or markers.get("USER-NEED") or markers.get("USER-CONFIRM")
        ):
            state = "blocked"
        payload.update({
            "state": state,
            "ok": result.ok and state == "complete",
            "stop_reason": result.stop_reason,
            "error": error,
            "markers": markers,
            "last_marker": {kind: values[-1] for kind, values in markers.items() if values} or None,
            "text_excerpt": result.text[-4000:],
            "out_of_scope_writes": result.out_of_scope_writes,
        })
    except Exception as e:
        async with status_lock:
            terminal_by_heartbeat = heartbeat_outcome or (getattr(conn, "heartbeat_outcome", None) if conn else None)
            terminal_error = heartbeat_error
        if terminal_by_heartbeat or (getattr(conn, "killed_by_heartbeat", False) if conn else False):
            payload.update({
                "state": terminal_by_heartbeat or "wedged",
                "ok": False,
                "error": terminal_error or {"code": -1, "message": terminal_by_heartbeat or "wedged"},
            })
        else:
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
        "--wedge-samples",
        type=int,
        default=int(os.environ.get("GOALFLIGHT_WEDGE_SAMPLES", "4")),
        help="Consecutive heartbeat dead samples required before killing a wedged ACP worker.",
    )
    parser.add_argument(
        "--max-tool-s",
        type=float,
        default=float(os.environ.get("GOALFLIGHT_MAX_TOOL_S", "1800")),
        help="Maximum silent wall time for one outstanding ACP tool call before tool_timeout.",
    )
    parser.add_argument(
        "--max-quiet-s",
        type=float,
        default=float(os.environ.get("GOALFLIGHT_MAX_QUIET_S", "3600")),
        help="Absolute event-silence hard wall for CPU-busy quiet workers, independent of idle-timeout.",
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
    if args.wedge_samples <= 0:
        args.wedge_samples = 4
    if args.max_tool_s <= 0:
        args.max_tool_s = 1800.0
    if args.max_quiet_s <= 0:
        args.max_quiet_s = 3600.0
    args.session_id = args.session_id or f"goalflight-{uuid.uuid4().hex[:8]}"
    payload = asyncio.run(run(args))
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{payload['dispatch_id']}: {payload['state']} status={args.status_json}")
    return 0 if payload.get("state") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
