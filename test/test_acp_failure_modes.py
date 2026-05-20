#!/usr/bin/env python3
"""Failure-mode tests for SDK liveness and guard behavior."""

from __future__ import annotations

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

from goalflight_acp_client import AcpLivenessActivity  # noqa: E402
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


def _make_fake_agent_wrapper(tmp: Path) -> Path:
    wrapper = tmp / "fake-acp-agent"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_AGENT))}\n"
    )
    wrapper.chmod(0o755)
    return wrapper


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
            "10",
            "--max-quiet-s",
            "10",
            "--max-tool-s",
            "10",
            "--json",
        ]
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
    )

    assert returncode != 0, stdout
    assert status["state"] == "wedged", status
    assert status["error"]["message"] == "wedged_by_heartbeat", status
    assert status["wedge_progress_seen"] >= 1, status
    assert status["heartbeat_dead_samples"] >= 2, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def main() -> None:
    case_vendor_flood_idle_waits_for_quiet_backstop()
    case_vendor_flood_cpu_busy_is_alive()
    case_standard_progress_resets_wedge_streak()
    case_permission_timeout_unblocks_wedge()
    case_progress_stall_wall_ignores_raw_vendor_noise()
    case_runner_raw_vendor_flood_hits_progress_stall_and_reaps()
    case_runner_progress_then_silent_wedges_and_reaps()
    print("OK: ACP SDK failure-mode tests pass")


if __name__ == "__main__":
    main()
