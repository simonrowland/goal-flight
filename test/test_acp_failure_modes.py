#!/usr/bin/env python3
"""Failure-mode tests for SDK liveness and guard behavior."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
FAKE_AGENT = ROOT / "test/fixtures/acp_fake_agent.py"

from goalflight_acp_client import (  # noqa: E402
    AcpError,
    AcpLivenessActivity,
    AcpProcessPool,
    PoolExhaustedError,
)
from goalflight_acp_run import adapter_liveness_config, decide_terminal_state, spawn_and_handshake_with_retry  # noqa: E402
from goalflight_liveness import heartbeat_wedge_decision, progress_stall_decision  # noqa: E402


def _vendor_event() -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "_x.ai/vendor_progress",
                "content": {"type": "text", "text": "noise"},
            },
        },
    }


def case_vendor_flood_idle_waits_for_quiet_backstop() -> None:
    activity = AcpLivenessActivity()
    for i in range(10):
        activity.note_message(_vendor_event(), 100.0 + i)
    assert activity.raw_events_seen == 10
    assert activity.wedge_progress_seen == 0

    dead = 0
    previous_progress = 0
    for _ in range(3):
        decision = heartbeat_wedge_decision(
            pid_alive=True,
            pgroup_cpu=0.0,
            wedge_progress_seen=activity.wedge_progress_seen,
            previous_wedge_progress_seen=previous_progress,
            outstanding_count=activity.outstanding_count(),
            cpu_epsilon_pct=0.1,
            previous_dead_samples=dead,
            wedge_samples=3,
        )
        dead = decision.dead_samples
        previous_progress = activity.wedge_progress_seen
    assert decision.dead_samples == 3
    assert decision.wedged is False


def case_vendor_flood_cpu_busy_is_alive() -> None:
    activity = AcpLivenessActivity()
    activity.note_message(_vendor_event(), 100.0)
    decision = heartbeat_wedge_decision(
        pid_alive=True,
        pgroup_cpu=4.0,
        wedge_progress_seen=activity.wedge_progress_seen,
        previous_wedge_progress_seen=activity.wedge_progress_seen,
        outstanding_count=0,
        cpu_epsilon_pct=0.1,
        previous_dead_samples=2,
        wedge_samples=3,
    )
    assert decision.dead_sample is False
    assert decision.wedged is False


def case_standard_progress_resets_wedge_streak() -> None:
    activity = AcpLivenessActivity()
    activity.note_message(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hi"},
                },
            },
        },
        100.0,
    )
    decision = heartbeat_wedge_decision(
        pid_alive=True,
        pgroup_cpu=0.0,
        wedge_progress_seen=activity.wedge_progress_seen,
        previous_wedge_progress_seen=0,
        outstanding_count=0,
        cpu_epsilon_pct=0.1,
        previous_dead_samples=2,
        wedge_samples=3,
    )
    assert decision.dead_sample is False
    assert decision.dead_samples == 0


def case_permission_timeout_unblocks_wedge() -> None:
    activity = AcpLivenessActivity(permission_timeout_s=2.0)
    activity.note_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/request_permission",
            "params": {"toolCall": {"toolCallId": "p1"}},
        },
        10.0,
    )
    snapshot = activity.snapshot(13.0)
    assert snapshot["outstanding_count"] == 1
    assert "p1" in activity.pending_permissions
    timed_out_tool = activity.timed_out(13.0, max_tool_s=60.0)
    assert timed_out_tool is not None
    tool_id, age_s = timed_out_tool
    terminal_state = "tool_timeout"
    terminal_error = {
        "code": -1,
        "message": "tool_timeout",
        "toolCallId": tool_id,
        "age_s": round(age_s, 3),
    }
    assert terminal_state == "tool_timeout"
    assert terminal_error["toolCallId"] == "p1"
    assert terminal_error["age_s"] == 3.0
    assert activity.outstanding_count(13.0) == 0


def case_progress_stall_wall_ignores_raw_vendor_noise() -> None:
    activity = AcpLivenessActivity()
    activity.reset_progress_clock(0.0)
    activity.note_message(_vendor_event(), 299.0)

    snapshot = activity.snapshot(301.0)

    assert snapshot["quiet_for_s"] == 2.0
    assert progress_stall_decision(
        pid_alive=True,
        progress_quiet_s=snapshot["progress_quiet_for_s"],
        progress_stall_s=300.0,
        outstanding_count=snapshot["outstanding_count"],
    ) is True


def case_adapter_manifest_liveness_defaults() -> None:
    profile, remote_turn_silence_s = adapter_liveness_config("codex")
    assert profile == "remote_api"
    assert remote_turn_silence_s == 1200.0

    profile, remote_turn_silence_s = adapter_liveness_config("claude")
    assert profile == "remote_api"
    assert remote_turn_silence_s == 1200.0

    profile, remote_turn_silence_s = adapter_liveness_config("missing-agent")
    assert profile == "local_compute"
    assert remote_turn_silence_s == 1200.0


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _make_fake_agent_wrapper(tmp: Path, scenario: str | None = None) -> Path:
    wrapper = tmp / "fake-acp-agent"
    lines = ["#!/usr/bin/env bash"]
    if scenario is not None:
        # Bake the scenario into the wrapper so direct-spawn cases (which inherit
        # the test process env rather than passing an explicit subprocess env)
        # select it without mutating os.environ.
        lines.append(f"export GOALFLIGHT_FAKE_ACP_SCENARIO={shlex.quote(scenario)}")
    lines.append(f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_AGENT))}")
    wrapper.write_text("\n".join(lines) + "\n")
    wrapper.chmod(0o755)
    return wrapper


def _force_kill(pid: object) -> None:
    """Best-effort reap of a possibly-orphaned worker pid (test cleanup only)."""
    if not isinstance(pid, int):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGKILL)


def _kill_from_status(status: Path) -> None:
    try:
        payload = json.loads(status.read_text())
    except Exception:
        payload = {}
    pgid = payload.get("pgid")
    if isinstance(pgid, int):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    pid = payload.get("worker_pid")
    if isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _run_fake_runner(
    scenario: str,
    *,
    progress_stall_s: float,
    heartbeat_interval: float = 0.1,
    wedge_samples: int = 2,
    idle_timeout: float = 10.0,
    max_quiet_s: float = 10.0,
    max_tool_s: float = 10.0,
    liveness_profile: str | None = None,
    remote_turn_silence_s: float | None = None,
    remote_turn_cancel_grace_s: float = 0.1,
    extra_env: dict[str, str] | None = None,
    timeout_s: float = 8.0,
) -> tuple[int, dict, str, str]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status = tmp / f"{scenario}.status.json"
        wrapper = _make_fake_agent_wrapper(tmp)
        env = os.environ.copy()
        env.update(
            {
                "GOALFLIGHT_STATE_DIR": str(state_dir),
                "GOALFLIGHT_FAKE_ACP_SCENARIO": scenario,
                "GOALFLIGHT_FAKE_ACP_INTERVAL": "0.05",
                "GOALFLIGHT_ACP_PYTHON": sys.executable,
            }
        )
        if extra_env:
            env.update(extra_env)
        args = [
            sys.executable,
            "scripts/goalflight_acp_run.py",
            "--agent",
            str(wrapper),
            "--cwd",
            str(ROOT),
            "--prompt-text",
            "hello",
            "--status-json",
            str(status),
            "--heartbeat-interval",
            str(heartbeat_interval),
            "--wedge-samples",
            str(wedge_samples),
            "--progress-stall-s",
            str(progress_stall_s),
            "--idle-timeout",
            str(idle_timeout),
            "--max-quiet-s",
            str(max_quiet_s),
            "--max-tool-s",
            str(max_tool_s),
            "--json",
        ]
        if liveness_profile is not None:
            args.extend(["--liveness-profile", liveness_profile])
        if remote_turn_silence_s is not None:
            args.extend(["--remote-turn-silence-s", str(remote_turn_silence_s)])
            args.extend(["--remote-turn-cancel-grace-s", str(remote_turn_cancel_grace_s)])
        proc = subprocess.Popen(
            args,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_from_status(status)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            stdout, stderr = proc.communicate()
            raise AssertionError(f"{scenario} runner timed out\nstdout={stdout}\nstderr={stderr}")
        if not status.exists():
            raise AssertionError(f"{scenario} wrote no status\nstdout={stdout}\nstderr={stderr}")
        return proc.returncode, json.loads(status.read_text()), stdout, stderr


def case_runner_raw_vendor_flood_hits_progress_stall_and_reaps() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "raw_vendor_flood",
        progress_stall_s=0.5,
    )

    assert returncode != 0, stdout
    assert status["state"] == "wedged", status
    assert status["error"]["message"] == "progress_stall", status
    assert status["wedge_progress_seen"] == 0, status
    assert status["events_seen"] > 0, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_runner_progress_then_silent_wedges_and_reaps() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "progress_then_silent",
        progress_stall_s=30.0,
        liveness_profile="local_compute",
    )

    assert returncode != 0, stdout
    assert status["state"] == "wedged", status
    assert status["error"]["message"] == "wedged_by_heartbeat", status
    assert status["wedge_progress_seen"] >= 1, status
    assert status["heartbeat_dead_samples"] >= 2, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_runner_remote_long_reasoning_pause_survives_old_walls() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "long_reasoning_pause",
        progress_stall_s=0.3,
        idle_timeout=0.0,
        max_quiet_s=0.3,
        max_tool_s=30.0,
        liveness_profile="remote_api",
        remote_turn_silence_s=3.0,
        extra_env={"GOALFLIGHT_FAKE_ACP_LONG_PAUSE_S": "1.0"},
        timeout_s=8.0,
    )

    assert returncode == 0, (stdout, stderr, status)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status
    assert status["liveness_profile"] == "remote_api", status
    assert "finished" in (status.get("text_excerpt") or ""), status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_runner_remote_dead_silent_turn_hits_remote_wall() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "dead_silent_turn",
        progress_stall_s=30.0,
        heartbeat_interval=0.1,
        idle_timeout=0.0,
        max_quiet_s=30.0,
        max_tool_s=30.0,
        liveness_profile="remote_api",
        remote_turn_silence_s=0.4,
        remote_turn_cancel_grace_s=0.1,
        timeout_s=8.0,
    )

    assert returncode != 0, stdout
    assert status["state"] == "remote_turn_silence", status
    assert status["error"]["message"] == "remote_turn_silence", status
    assert status["liveness_profile"] == "remote_api", status
    assert status["turn_silent_for_s"] >= 0.4, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_runner_thought_stream_survives_progress_stall_wall() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "thought_stream_pause",
        progress_stall_s=0.3,
        heartbeat_interval=0.05,
        idle_timeout=0.0,
        max_quiet_s=30.0,
        extra_env={
            "GOALFLIGHT_FAKE_ACP_INTERVAL": "0.2",
            "GOALFLIGHT_FAKE_ACP_THOUGHT_CHUNKS": "5",
        },
        timeout_s=8.0,
    )

    assert returncode == 0, (stdout, stderr, status)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status
    assert status["wedge_progress_seen"] >= 1, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_terminal_state_endturn_beats_tail_race_wedge() -> None:
    # Classification precedence (pure): a genuine end_turn must beat a heartbeat
    # wedge that tripped in the completion tail, but a real wedge (no end_turn)
    # must still win. Deterministic guard for the runner's decide_terminal_state.

    # end_turn received + heartbeat wedged in the tail -> complete, not wedged.
    state, error = decide_terminal_state(
        result_ok=True,
        result_error=None,
        heartbeat_outcome="wedged",
        killed_by_heartbeat=True,
        cancelled_for_marker=False,
        early_marker=None,
        heartbeat_error={"code": -1, "message": "wedged_by_heartbeat"},
    )
    assert state == "complete", state
    assert error is None, error

    # Real wedge (no end_turn) -> the heartbeat verdict wins.
    state, error = decide_terminal_state(
        result_ok=False,
        result_error={"code": -1, "message": "agent_timeout (idle)"},
        heartbeat_outcome="wedged",
        killed_by_heartbeat=True,
        cancelled_for_marker=False,
        early_marker=None,
        heartbeat_error={"code": -1, "message": "wedged_by_heartbeat"},
    )
    assert state == "wedged", state
    assert error == {"code": -1, "message": "wedged_by_heartbeat"}, error

    # tool_timeout is an outstanding-tool anomaly, NOT a silence wedge -> end_turn
    # does NOT refute it; it wins even when result_ok is True.
    state, error = decide_terminal_state(
        result_ok=True,
        result_error=None,
        heartbeat_outcome="tool_timeout",
        killed_by_heartbeat=True,
        cancelled_for_marker=False,
        early_marker=None,
        heartbeat_error={"code": -1, "message": "tool_timeout", "toolCallId": "t1"},
    )
    assert state == "tool_timeout", state
    assert error["message"] == "tool_timeout", error

    # killed_by_heartbeat with no recorded outcome defaults to the silence wedge.
    state, error = decide_terminal_state(
        result_ok=False,
        result_error=None,
        heartbeat_outcome=None,
        killed_by_heartbeat=True,
        cancelled_for_marker=False,
        early_marker=None,
        heartbeat_error=None,
    )
    assert state == "wedged", state
    assert error == {"code": -1, "message": "wedged"}, error

    # Early marker cancel, no heartbeat -> blocked.
    state, error = decide_terminal_state(
        result_ok=False,
        result_error=None,
        heartbeat_outcome=None,
        killed_by_heartbeat=False,
        cancelled_for_marker=True,
        early_marker="BLOCKED",
        heartbeat_error=None,
    )
    assert state == "blocked", state
    assert error["marker"] == "BLOCKED", error

    # Plain failure (no end_turn, no heartbeat, no marker) -> failed.
    state, error = decide_terminal_state(
        result_ok=False,
        result_error={"code": -1, "message": "agent_timeout (idle)"},
        heartbeat_outcome=None,
        killed_by_heartbeat=False,
        cancelled_for_marker=False,
        early_marker=None,
        heartbeat_error=None,
    )
    assert state == "failed", state


def case_runner_idle_silent_idle_timeout_reaps() -> None:
    # IdleLivenessGate / on_idle path: a worker that emits nothing and never
    # responds is reaped by the run_prompt idle timeout (not the heartbeat —
    # the wedge backstop can't fire without a prior progress event). Keep the
    # heartbeat backstops well out of the way (30s) so only the idle path can
    # trip; the idle classifier writes state="failed" + agent_timeout(idle),
    # a signature distinct from the heartbeat "wedged" terminals.
    returncode, status, stdout, stderr = _run_fake_runner(
        "idle_silent",
        progress_stall_s=30.0,
        idle_timeout=0.5,
        max_quiet_s=30.0,
        max_tool_s=30.0,
        timeout_s=20.0,
    )

    assert returncode != 0, stdout
    assert status["state"] == "failed", status
    assert status["error"]["message"] == "agent_timeout (idle)", status
    assert status["wedge_progress_seen"] == 0, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_runner_oversized_frame_dropped_then_completes() -> None:
    # GuardedStreamReader drop-and-continue at the runner level: with a small
    # ACP frame limit the agent's over-limit frame is dropped (and counted in
    # acp_dropped_frames via the shared liveness activity) while the runner
    # reads past it to the following normal frame + end_turn and finishes clean.
    # (Malformed-but-in-limit JSON tolerance is the SDK's job, not the runner's,
    # so it isn't re-tested here — acp_dropped_frames is the runner-owned signal.)
    #
    # heartbeat_interval is kept far above the (sub-second) turn duration so this
    # wiring test stays deterministic and focused on the drop-and-continue path,
    # independent of heartbeat timing. (The classification race a fast heartbeat
    # used to expose — a tail-race wedge mislabeling a completed turn — is fixed
    # in decide_terminal_state and guarded deterministically by
    # case_terminal_state_endturn_beats_tail_race_wedge.)
    returncode, status, stdout, stderr = _run_fake_runner(
        "overlimit",
        progress_stall_s=30.0,
        heartbeat_interval=30.0,
        extra_env={"GOALFLIGHT_ACP_LIMIT": "4096"},
    )

    assert returncode == 0, (stdout, stderr)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status
    assert status["acp_dropped_frames"] >= 1, status
    assert "after-limit" in (status.get("text_excerpt") or ""), status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_runner_goal_mode_progress_stall_backstop() -> None:
    # idle-timeout=0 (goal mode: rely on PID liveness + terminal markers). The
    # run_prompt idle path is fully disabled, so the heartbeat progress-stall
    # wall is the only backstop — it must still reap a raw-vendor-noise flood
    # (vendor events never reset the standard-progress clock).
    returncode, status, stdout, stderr = _run_fake_runner(
        "raw_vendor_flood",
        progress_stall_s=0.5,
        idle_timeout=0.0,
    )

    assert returncode != 0, stdout
    assert status["state"] == "wedged", status
    assert status["error"]["message"] == "progress_stall", status
    assert status["wedge_progress_seen"] == 0, status
    assert status["events_seen"] > 0, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_runner_goal_mode_heartbeat_backstop() -> None:
    # idle-timeout=0 (goal mode), progress-stall wall held off (30s): the
    # heartbeat dead-sample wedge detector is the only backstop and must still
    # reap a worker that made one bit of progress then went silent.
    returncode, status, stdout, stderr = _run_fake_runner(
        "progress_then_silent",
        progress_stall_s=30.0,
        idle_timeout=0.0,
    )

    assert returncode != 0, stdout
    assert status["state"] == "wedged", status
    assert status["error"]["message"] == "wedged_by_heartbeat", status
    assert status["wedge_progress_seen"] >= 1, status
    assert status["heartbeat_dead_samples"] >= 2, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_runner_tool_timeout_reaps() -> None:
    # Per-tool absolute wall: a worker that opens a tool call and never resolves
    # it (no completed update, no end_turn) is reaped by tool_timeout. The
    # outstanding tool gates OFF the silence-class backstops, so tool_timeout is
    # the deterministic terminal — and decide_terminal_state surfaces it as a
    # real anomaly rather than masking it (end_turn never arrives here anyway,
    # but tool_timeout is exempt from result_ok masking by design).
    returncode, status, stdout, stderr = _run_fake_runner(
        "tool_stuck",
        progress_stall_s=30.0,
        max_tool_s=0.5,
    )

    assert returncode != 0, stdout
    assert status["state"] == "tool_timeout", status
    assert status["error"]["message"] == "tool_timeout", status
    assert status["killed_by_heartbeat"] is True, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_handshake_wedge_kills_before_respawn() -> None:
    # spawn_and_handshake_with_retry against a worker that spawns but never
    # answers initialize. Each attempt must hit the handshake_timeout, kill the
    # wedged worker BEFORE respawning (never leave an identity-matched PID
    # alive), and after exhausting attempts raise AcpError with no orphan left.
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            wrapper = _make_fake_agent_wrapper(Path(td), scenario="handshake_wedge")
            spawned: list[int] = []
            pid0_alive_at_respawn: dict[str, object] = {"value": None}

            def record(attempt: int, proc: object) -> None:
                spawned.append(proc.pid)  # type: ignore[attr-defined]
                if attempt == 1 and spawned:
                    # Captured at the moment the respawn spawns: attempt 0's
                    # worker must already be dead (kill-before-respawn).
                    pid0_alive_at_respawn["value"] = _pid_alive(spawned[0])

            try:
                raised: AcpError | None = None
                try:
                    await spawn_and_handshake_with_retry(
                        str(wrapper),
                        [],
                        agent="fake",
                        session_id="s",
                        cwd=str(ROOT),
                        attempts=2,
                        handshake_timeout=0.5,
                        on_attempt=record,
                    )
                except AcpError as e:
                    raised = e
                assert raised is not None, "expected AcpError after exhausting handshake attempts"
                assert "handshake failed after 2 attempt" in str(raised), str(raised)
                assert len(spawned) == 2, spawned
                assert spawned[0] != spawned[1], spawned
                assert pid0_alive_at_respawn["value"] is False, "attempt-0 worker not killed before respawn"
                for pid in spawned:
                    assert not _pid_alive(pid), f"handshake-wedged worker {pid} left alive"
            finally:
                for pid in spawned:
                    _force_kill(pid)

    asyncio.run(_run())


def case_pool_exhaustion_then_drain() -> None:
    # AcpProcessPool: get_or_create up to the ceiling, the next raises
    # PoolExhaustedError without spawning, and shutdown() drains every worker
    # (no surviving PIDs).
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            wrapper = _make_fake_agent_wrapper(Path(td))  # default handshake-only agent
            config = {"fake": {"command": str(wrapper), "acp_args": []}}
            pool = AcpProcessPool(config, max_processes=2, max_per_agent=10, auto_allow_tools=True)
            spawned: list[int] = []
            try:
                c1 = await pool.get_or_create("fake", "s1", cwd=str(ROOT))
                c2 = await pool.get_or_create("fake", "s2", cwd=str(ROOT))
                spawned += [c1.proc.pid, c2.proc.pid]
                assert pool.stats["total"] == 2, pool.stats
                assert _pid_alive(c1.proc.pid) and _pid_alive(c2.proc.pid), spawned

                raised = False
                try:
                    await pool.get_or_create("fake", "s3", cwd=str(ROOT))
                except PoolExhaustedError:
                    raised = True
                assert raised, "expected PoolExhaustedError at the pool ceiling"
                assert pool.stats["total"] == 2, pool.stats

                await pool.shutdown()
                assert pool.stats["total"] == 0, pool.stats
                for pid in spawned:
                    assert not _pid_alive(pid), f"worker {pid} survived pool drain"
            finally:
                with contextlib.suppress(Exception):
                    await pool.shutdown()
                for pid in spawned:
                    _force_kill(pid)

    asyncio.run(_run())


def main() -> None:
    case_vendor_flood_idle_waits_for_quiet_backstop()
    case_vendor_flood_cpu_busy_is_alive()
    case_standard_progress_resets_wedge_streak()
    case_permission_timeout_unblocks_wedge()
    case_progress_stall_wall_ignores_raw_vendor_noise()
    case_adapter_manifest_liveness_defaults()
    case_runner_raw_vendor_flood_hits_progress_stall_and_reaps()
    case_runner_progress_then_silent_wedges_and_reaps()
    case_runner_remote_long_reasoning_pause_survives_old_walls()
    case_runner_remote_dead_silent_turn_hits_remote_wall()
    case_runner_thought_stream_survives_progress_stall_wall()
    case_terminal_state_endturn_beats_tail_race_wedge()
    case_runner_idle_silent_idle_timeout_reaps()
    case_runner_oversized_frame_dropped_then_completes()
    case_runner_goal_mode_progress_stall_backstop()
    case_runner_goal_mode_heartbeat_backstop()
    case_runner_tool_timeout_reaps()
    case_handshake_wedge_kills_before_respawn()
    case_pool_exhaustion_then_drain()
    print("OK: ACP SDK failure-mode tests pass")


if __name__ == "__main__":
    main()
