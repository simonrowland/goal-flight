#!/usr/bin/env python3
"""Run one ACP prompt with compact status, capacity, and ledger records."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
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


def _acp_reexec_target() -> str | None:
    """Return the python path to re-exec into for acp, or None to stay put."""
    if importlib.util.find_spec("acp") is not None:
        return None
    override = os.environ.get("GOALFLIGHT_ACP_PYTHON")
    target = Path(override).expanduser() if override else Path.home() / ".goal-flight/venvs/acp-0.10/bin/python"
    if not target.exists():
        return None
    try:
        target_real = target.resolve()
        current_real = Path(sys.executable).resolve()
    except OSError:
        target_real = target
        current_real = Path(sys.executable)
    if target_real == current_real:
        return None
    return str(target)


def _ensure_acp_sdk_python() -> None:
    target = _acp_reexec_target()
    if target is not None:
        os.execv(target, [target, *sys.argv])


_ensure_acp_sdk_python()
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_capacity
import goalflight_ledger
from goalflight_acp_client import (
    AcpConnection,
    AcpError,
    AcpLivenessActivity,
    cleanup_ghosts,
    spawn_acp_connection,
)
from goalflight_liveness import (
    active_monotonic,
    heartbeat_wedge_decision,
    IdleLivenessGate,
    pgroup_cpu_pct,
    progress_stall_decision,
    process_group_id,
    system_sleep_pause_note,
    system_sleep_pause_s,
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
    # codex-acp needs the MCP-elicitation flag, but it is injected at the single
    # spawn boundary (ensure_codex_acp_elicitation, called by spawn_acp_connection)
    # so it covers pool/custom callers too -- not only this runner helper.
    return agent, []


def _now() -> int:
    return int(time.time())


def _event_kind(event: dict) -> str:
    if "_prompt_result" in event:
        return "prompt_result"
    return str(event.get("params", {}).get("update", {}).get("sessionUpdate") or event.get("method") or "event")


def decide_terminal_state(
    *,
    result_ok: bool,
    result_error: dict | None,
    heartbeat_outcome: str | None,
    killed_by_heartbeat: bool,
    cancelled_for_marker: bool,
    early_marker: str | None,
    heartbeat_error: dict | None,
) -> tuple[str, dict | None]:
    """Resolve the runner's terminal (state, error) from the prompt result and
    the heartbeat verdict, in priority order.

    A genuine end_turn (``result_ok``) refutes the SILENCE-class heartbeat
    terminals — the dead-sample wedge, ``progress_stall``, and ``max_quiet_s``,
    all reported as ``"wedged"`` and all gated on ``outstanding_count == 0``.
    Those fire on inactivity; the heartbeat loop keeps sampling until the outer
    ``finally`` cancels it, so a worker that has ALREADY completed its turn is
    briefly alive-and-silent (returned from the turn, waiting to be closed) and
    on an aggressive cadence one of them can trip in that tail AFTER end_turn was
    received. The worker *spoke* (a terminal end_turn), so it was not silently
    wedged: result_ok wins. This can never mask a real silence wedge, because a
    worker killed mid-turn cannot emit end_turn (the SDK rejects the pending
    prompt on the closed pipe), so a real wedge always has ``result_ok`` False.

    ``tool_timeout`` is NOT a silence signal: it fires while a tool is still
    OUTSTANDING (``outstanding_count > 0``) past its absolute wall
    (``--max-tool-s``). end_turn does not refute it — a worker that ends its turn
    leaving a tool it opened unresolved past the wall is a real anomaly the
    operator must see — so tool_timeout wins even over result_ok.

    A killed-but-no-recorded-outcome race defaults to the silence-class
    ``"wedged"`` (the dead-sample wedge is what the kill-without-outcome path is).
    """
    heartbeat_terminal = heartbeat_outcome or ("wedged" if killed_by_heartbeat else None)
    # tool_timeout is an outstanding-tool anomaly, not silence — never masked.
    if heartbeat_terminal == "tool_timeout":
        return "tool_timeout", heartbeat_error or {"code": -1, "message": "tool_timeout"}
    # Silence-class heartbeat terminals win only if the turn didn't complete;
    # a genuine end_turn refutes them.
    if heartbeat_terminal and not result_ok:
        return heartbeat_terminal, heartbeat_error or {"code": -1, "message": heartbeat_terminal}
    if cancelled_for_marker:
        return "blocked", {
            "code": 0,
            "message": "early_marker_cancelled",
            "marker": early_marker,
        }
    if result_ok:
        return "complete", result_error
    return "failed", result_error


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
    activity: AcpLivenessActivity | None = None,
    on_attempt: Callable[[int, asyncio.subprocess.Process], Any] | None = None,
    context_mode: bool = True,
    permission_mode: str = "auto",
    permission_dir: str | None = None,
    permission_inline_timeout_s: float | None = None,
    permission_user_timeout_s: float | None = None,
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
        conn = await spawn_acp_connection(
            command,
            acp_args,
            agent=agent,
            session_id=session_id,
            cwd=cwd,
            auto_allow_tools=auto_allow_tools,
            activity=activity,
            context_mode=context_mode,
            permission_mode=permission_mode,
            permission_dir=permission_dir,
            permission_inline_timeout_s=permission_inline_timeout_s,
            permission_user_timeout_s=permission_user_timeout_s,
        )
        proc = conn.proc
        if on_attempt is not None:
            maybe = on_attempt(attempt, proc)
            if inspect.isawaitable(maybe):
                await maybe
        try:
            await conn.initialize(timeout=handshake_timeout)
            await conn.new_session(cwd, timeout=handshake_timeout)
            return proc, conn
        except AcpError as e:
            last_err = e
            # Reap the wedged worker before respawning — never leave an
            # identity-matched PID alive.
            with contextlib.suppress(Exception):
                await conn.kill()
    raise AcpError(f"handshake failed after {max(1, attempts)} attempt(s): {last_err}")


async def run(args: argparse.Namespace) -> dict:
    if not hasattr(args, "progress_stall_s") or args.progress_stall_s is None:
        args.progress_stall_s = 300.0
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
        "acp_dropped_frames": 0,
        "last_event_at": None,
        "last_event_kind": None,
        "heartbeat_at": None,
        "progress_quiet_for_s": 0.0,
        "progress_stall_s": None,
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
    activity = AcpLivenessActivity()
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
        last_sample_progress_seen = 0
        prev_wall = time.time()
        prev_active = active_monotonic()
        total_paused_s = 0.0
        # Created only after a successful handshake, so it tracks the single
        # committed worker. Exit early once that worker exits (grok 2026-05-20
        # P2) rather than sampling a dead pid until the outer finally cancels us.
        while True:
            if proc is None or proc.returncode is not None:
                return
            wall_now = time.time()
            active_now = active_monotonic()
            freeze_s = system_sleep_pause_s(
                prev_wall=prev_wall,
                prev_active=prev_active,
                wall_now=wall_now,
                active_now=active_now,
                heartbeat_interval_s=args.heartbeat_interval,
            )
            if freeze_s > 0:
                total_paused_s += freeze_s
                await update_status(
                    note=system_sleep_pause_note(freeze_s, total_paused_s),
                    total_paused_s=round(total_paused_s, 3),
                    heartbeat_at=_now(),
                )
                prev_wall, prev_active = wall_now, active_now
                await asyncio.sleep(args.heartbeat_interval)
                continue
            prev_wall, prev_active = wall_now, active_now
            pgid = payload.get("pgid") or process_group_id(proc.pid) or proc.pid
            cpu_pct = await asyncio.to_thread(pgroup_cpu_pct, pgid)
            now_mono = active_now
            snapshot = activity.snapshot(now_mono)
            seen = int(snapshot["raw_events_seen"])
            progress_seen = int(snapshot["wedge_progress_seen"])
            outstanding_count = int(snapshot["outstanding_count"])
            dropped_frames = int(snapshot.get("dropped_frames", 0))
            quiet_for_s = float(snapshot["quiet_for_s"])
            progress_quiet_s = float(snapshot["progress_quiet_for_s"])
            pid_alive = proc.returncode is None
            # Absolute per-tool wall is checked EVERY tick, BEFORE the inline-hold
            # short-circuit, so the max_tool_s backstop for a never-answered inline
            # hold (added to activity.timed_out) is actually reachable. A tool (or
            # held permission) outstanding longer than --max-tool-s is stuck
            # regardless of CPU (it may be CPU-busy in a hung retry loop, or CPU may
            # be unsamplable). Gating it on CPU≤ε would let those hang forever
            # (codex 2026-05-20 P1). The grace (don't wedge while a tool is
            # outstanding) still applies UP TO the wall; past it, it's the wedge.
            timed_out_tool = activity.timed_out(now_mono, args.max_tool_s)
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
            if activity.has_inline_holds() and pid_alive:
                # The inline permission router is holding a request open awaiting a
                # controller/user decision (permission_mode="inline"). The worker is
                # paused by design; publish a visible state and skip the SILENCE-class
                # wedge checks (progress stall / max_quiet / CPU dead-samples) this
                # tick -- but NOT the absolute max_tool_s wall above, and the
                # handler's own inline timeout + finally-release still bound the hold.
                await update_status(
                    state="awaiting_permission",
                    worker_pid=proc.pid,
                    pgid=pgid,
                    worker_alive=pid_alive,
                    pgroup_cpu_pct=cpu_pct,
                    inline_held=int(snapshot.get("inline_held", 0)),
                    outstanding_tool_calls=outstanding_count,
                    heartbeat_at=_now(),
                )
                await asyncio.sleep(args.heartbeat_interval)
                continue
            if progress_stall_decision(
                pid_alive=pid_alive,
                progress_quiet_s=progress_quiet_s,
                progress_stall_s=args.progress_stall_s,
                outstanding_count=outstanding_count,
            ):
                await mark_heartbeat_terminal(
                    "wedged",
                    {
                        "code": -1,
                        "message": "progress_stall",
                        "progress_quiet_s": round(progress_quiet_s, 3),
                        "progress_stall_s": round(args.progress_stall_s, 3),
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
                wedge_progress_seen=progress_seen,
                previous_wedge_progress_seen=last_sample_progress_seen,
                outstanding_count=outstanding_count,
                cpu_epsilon_pct=args.cpu_epsilon,
                previous_dead_samples=dead_samples,
                wedge_samples=args.wedge_samples,
            )
            dead_samples = decision.dead_samples
            last_sample_progress_seen = progress_seen
            await update_status(
                worker_pid=proc.pid,
                pgid=pgid,
                worker_alive=pid_alive,
                pgroup_cpu_pct=cpu_pct,
                heartbeat_at=_now(),
                heartbeat_dead_samples=dead_samples,
                wedge_progress_seen=progress_seen,
                outstanding_tool_calls=outstanding_count,
                acp_dropped_frames=dropped_frames,
                quiet_for_s=round(quiet_for_s, 3),
                progress_quiet_for_s=round(progress_quiet_s, 3),
                progress_stall_s=args.progress_stall_s,
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
        now_mono = active_monotonic()
        idle_gate.note_event()  # any incoming SDK observer event keeps idle gate alive
        snapshot = activity.snapshot(now_mono)
        seen = int(snapshot["raw_events_seen"])
        progress_seen = int(snapshot["wedge_progress_seen"])
        outstanding_count = int(snapshot["outstanding_count"])
        dropped_frames = int(snapshot.get("dropped_frames", 0))
        progress_quiet_s = float(snapshot["progress_quiet_for_s"])
        await update_status(
            state="running",
            events_seen=seen,
            acp_dropped_frames=dropped_frames,
            wedge_progress_seen=progress_seen,
            progress_quiet_for_s=round(progress_quiet_s, 3),
            progress_stall_s=args.progress_stall_s,
            last_event_at=_now(),
            last_event_kind=str(snapshot.get("last_event_kind") or _event_kind(event)),
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
        cleanup_ghosts()
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
                activity=activity,
                on_attempt=mark_attempt,
                context_mode=(getattr(args, "context_mode", "enabled") != "disabled"),
                permission_mode=getattr(args, "permission_mode", "auto"),
                permission_dir=getattr(args, "permission_dir", None),
                permission_inline_timeout_s=getattr(args, "permission_inline_timeout_s", None),
                permission_user_timeout_s=getattr(args, "permission_user_timeout_s", None),
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
        activity.reset_progress_clock(active_monotonic())
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
        state, error = decide_terminal_state(
            result_ok=result.ok,
            result_error=result.error,
            heartbeat_outcome=terminal_by_heartbeat,
            killed_by_heartbeat=bool(getattr(conn, "killed_by_heartbeat", False)),
            cancelled_for_marker=result.cancelled_for_marker,
            early_marker=result.early_marker,
            heartbeat_error=terminal_error,
        )
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
            # Permission requests the controller router escalated to the user
            # (boundary crossings it would not auto-allow). state is "blocked"
            # (marker USER-CONFIRM); the controller surfaces these, gets a user
            # decision, and re-dispatches. None when nothing was escalated.
            "permission_pending": result.permission_escalations or None,
            # Informational -- inline permissions the controller auto-declined on
            # timeout; the worker continued without the tool (no re-dispatch).
            # Does NOT change state.
            "permission_auto_declined": result.permission_auto_declined or None,
            # Reconcile the heartbeat flags with the FINAL verdict. A tail-race
            # heartbeat may have written killed/wedged into the payload before
            # decide_terminal_state ruled the turn complete on a genuine
            # end_turn; without this the record would be self-contradictory
            # (state=complete, killed_by_heartbeat=true) and mislead a controller
            # keying retry off the flag.
            "killed_by_heartbeat": state in ("wedged", "tool_timeout"),
            "wedged_by_heartbeat": state == "wedged",
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
            snapshot = activity.snapshot(active_monotonic())
            payload.update(
                events_seen=int(snapshot["raw_events_seen"]),
                wedge_progress_seen=int(snapshot["wedge_progress_seen"]),
                outstanding_tool_calls=int(snapshot["outstanding_count"]),
                acp_dropped_frames=int(snapshot.get("dropped_frames", 0)),
                progress_quiet_for_s=round(float(snapshot["progress_quiet_for_s"]), 3),
                progress_stall_s=args.progress_stall_s,
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
        "--context-mode",
        choices=["enabled", "disabled"],
        default="enabled",
        help="codex-acp only: whether the context-mode MCP server is active for "
             "this worker. 'enabled' (default) routes its elicitation through the "
             "ACP permission channel (auto-approved when in-scope); 'disabled' "
             "turns context-mode off for this dispatch entirely (no MCP "
             "elicitation surface). No effect on other adapters.",
    )
    parser.add_argument(
        "--permission-mode",
        choices=["auto", "inline"],
        default="auto",
        help="Escalation transport for boundary-crossing permission requests. "
             "'auto' (default): answer with a cancel and surface permission_pending "
             "(USER-CONFIRM -> re-dispatch). 'inline': HOLD the worker open and "
             "authorize in place via the --permission-dir file IPC (an orchestrator "
             "drains the dir, optionally write_ack to defer to the user, writes a "
             "decision) -- it never re-dispatches. Two-phase awake-time timeout: if "
             "no ack/decision within --permission-inline-timeout-s, or no decision "
             "within --permission-user-timeout-s after an ack, the worker "
             "auto-declines that tool and CONTINUES. Inline across processes "
             "REQUIRES an explicit --permission-dir both sides share.",
    )
    parser.add_argument(
        "--permission-dir",
        default=None,
        help="Directory for inline permission request/decision files. Default: "
             "$GOAL_FLIGHT_PERMISSION_DIR or a PID-scoped temp dir (only "
             "discoverable in-process). Set explicitly so a separate orchestrator "
             "relay can find this worker's requests. No effect in 'auto' mode.",
    )
    parser.add_argument(
        "--permission-inline-timeout-s",
        type=float,
        default=None,
        help="Inline mode controller-responsiveness window: max awake-seconds to "
             "hold a permission waiting for the controller to ack-or-decide before "
             "auto-declining (worker continues; default 180 = 3 min). No effect in "
             "'auto' mode.",
    )
    parser.add_argument(
        "--permission-user-timeout-s", type=float, default=None,
        help="Inline mode: after the controller ACKs a permission (defer-to-user), "
             "max awake-seconds to wait for the user's decision before auto-declining "
             "(default 36000 = 10h). No effect in 'auto' mode.",
    )
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
        "--progress-stall-s",
        type=float,
        default=float(os.environ.get("GOALFLIGHT_PROGRESS_STALL_S", "300")),
        help="Standard-progress silence hard wall. Raw vendor events do not reset it.",
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
    if args.progress_stall_s <= 0:
        args.progress_stall_s = 300.0
    args.session_id = args.session_id or f"goalflight-{uuid.uuid4().hex[:8]}"
    payload = asyncio.run(run(args))
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{payload['dispatch_id']}: {payload['state']} status={args.status_json}")
    return 0 if payload.get("state") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
