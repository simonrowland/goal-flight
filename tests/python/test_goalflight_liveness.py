"""Focused tests for Phase 1 CPU/heartbeat liveness helpers."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("liveness tests use POSIX start_new_session process trees")

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from goalflight_liveness import (  # noqa: E402
    active_monotonic,
    heartbeat_wedge_decision,
    IdleLivenessGate,
    LivenessThresholds,
    classify_liveness,
    cpu_liveness_keep_waiting,
    parse_ps_pgroup_cpu,
    pgroup_cpu_pct,
    progress_stall_decision,
    system_sleep_pause_note,
    system_sleep_pause_s,
)
import goalflight_acp_client as acp_client  # noqa: E402
from goalflight_acp_client import AcpLivenessActivity  # noqa: E402


def skipif(condition: bool, reason: str):
    def _decorator(func):
        def _wrapped(*args, **kwargs):
            if condition:
                print(f"SKIP: {func.__name__}: {reason}")
                return None
            return func(*args, **kwargs)
        return _wrapped
    return _decorator


def _update(session_update: str, text: str = "x") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s",
            "update": {
                "sessionUpdate": session_update,
                "content": {"type": "text", "text": text},
            },
        },
    }


def _tool_update(tool_id: str = "t1", status: str = "running") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_id,
                "status": status,
            },
        },
    }


def test_busy_silent_worker_classifies_running_quiet() -> None:
    thresholds = LivenessThresholds(idle_timeout_s=10.0, cpu_epsilon_pct=0.1)
    state = classify_liveness(
        pid_alive=True,
        pgroup_cpu=12.5,
        seconds_since_event=30.0,
        thresholds=thresholds,
    )
    assert state == "running_quiet", state


def test_idle_silent_worker_classifies_wedged() -> None:
    thresholds = LivenessThresholds(idle_timeout_s=10.0, cpu_epsilon_pct=0.1)
    state = classify_liveness(
        pid_alive=True,
        pgroup_cpu=0.0,
        seconds_since_event=30.0,
        thresholds=thresholds,
    )
    assert state == "wedged", state


def test_none_cpu_idle_classifies_wedged() -> None:
    # CPU sample unavailable (ps failed) + idle-expired → wedged. This is the
    # single-sample conservative verdict; the transient-failure grace lives in
    # cpu_liveness_keep_waiting / the watcher confirm-streak, NOT in the pure
    # classifier (grok 2026-05-20 P2: assert the None+idle branch explicitly).
    thresholds = LivenessThresholds(idle_timeout_s=10.0, cpu_epsilon_pct=0.1)
    assert classify_liveness(True, None, 100.0, thresholds) == "wedged"


def test_heartbeat_dead_sample_decision_table() -> None:
    cases = [
        ("busy", 8.0, 5, 5, 0, 2, False, 0, False),
        ("idle", 0.0, 5, 5, 0, 2, True, 3, True),
        ("first-token-grace", 0.0, 0, 0, 0, 2, True, 3, False),
        ("outstanding-tool", 0.0, 5, 5, 1, 2, False, 0, False),
        ("none-cpu", None, 5, 5, 0, 2, False, 0, False),
        ("new-progress", 0.0, 6, 5, 0, 2, False, 0, False),
    ]
    for name, cpu, progress_seen, previous_progress_seen, outstanding, previous_dead, dead, streak, wedged in cases:
        decision = heartbeat_wedge_decision(
            pid_alive=True,
            pgroup_cpu=cpu,
            wedge_progress_seen=progress_seen,
            previous_wedge_progress_seen=previous_progress_seen,
            outstanding_count=outstanding,
            cpu_epsilon_pct=0.1,
            previous_dead_samples=previous_dead,
            wedge_samples=3,
        )
        assert decision.dead_sample is dead, name
        assert decision.dead_samples == streak, name
        assert decision.wedged is wedged, name


def test_heartbeat_first_token_grace_requires_progress_before_wedge() -> None:
    dead_samples = 0
    previous_progress_seen = 0
    decision = None

    for _ in range(4):
        decision = heartbeat_wedge_decision(
            pid_alive=True,
            pgroup_cpu=0.0,
            wedge_progress_seen=0,
            previous_wedge_progress_seen=previous_progress_seen,
            outstanding_count=0,
            cpu_epsilon_pct=0.1,
            previous_dead_samples=dead_samples,
            wedge_samples=3,
        )
        dead_samples = decision.dead_samples
        previous_progress_seen = 0

    assert decision is not None
    assert decision.dead_sample is True
    assert decision.dead_samples == 4
    assert decision.wedged is False


def test_heartbeat_wedges_after_first_progress_and_resets_on_new_progress() -> None:
    dead_samples = 0
    previous_progress_seen = 1
    decision = None

    for _ in range(3):
        decision = heartbeat_wedge_decision(
            pid_alive=True,
            pgroup_cpu=0.0,
            wedge_progress_seen=1,
            previous_wedge_progress_seen=previous_progress_seen,
            outstanding_count=0,
            cpu_epsilon_pct=0.1,
            previous_dead_samples=dead_samples,
            wedge_samples=3,
        )
        dead_samples = decision.dead_samples
        previous_progress_seen = 1

    assert decision is not None
    assert decision.wedged is True

    decision = heartbeat_wedge_decision(
        pid_alive=True,
        pgroup_cpu=0.0,
        wedge_progress_seen=2,
        previous_wedge_progress_seen=1,
        outstanding_count=0,
        cpu_epsilon_pct=0.1,
        previous_dead_samples=dead_samples,
        wedge_samples=3,
    )
    assert decision.dead_sample is False
    assert decision.dead_samples == 0
    assert decision.wedged is False


def test_progress_stall_ignores_raw_event_recency() -> None:
    activity = AcpLivenessActivity()
    activity.reset_progress_clock(0.0)
    activity.note_message(_update("_x.ai/vendor_progress"), 299.0)

    snapshot = activity.snapshot(301.0)

    assert snapshot["quiet_for_s"] == 2.0
    assert snapshot["progress_quiet_for_s"] == 301.0
    assert progress_stall_decision(
        pid_alive=True,
        progress_quiet_s=snapshot["progress_quiet_for_s"],
        progress_stall_s=300.0,
        outstanding_count=snapshot["outstanding_count"],
    ) is True


def test_progress_stall_resets_on_standard_progress() -> None:
    activity = AcpLivenessActivity()
    activity.reset_progress_clock(0.0)
    activity.note_message(_update("agent_message_chunk"), 250.0)

    snapshot = activity.snapshot(300.0)

    assert snapshot["progress_quiet_for_s"] == 50.0
    assert progress_stall_decision(
        pid_alive=True,
        progress_quiet_s=snapshot["progress_quiet_for_s"],
        progress_stall_s=300.0,
        outstanding_count=snapshot["outstanding_count"],
    ) is False


def test_progress_stall_allows_cursor_like_slow_first_token() -> None:
    activity = AcpLivenessActivity()
    activity.reset_progress_clock(0.0)

    before_first_token = activity.snapshot(90.0)
    assert progress_stall_decision(
        pid_alive=True,
        progress_quiet_s=before_first_token["progress_quiet_for_s"],
        progress_stall_s=300.0,
        outstanding_count=before_first_token["outstanding_count"],
    ) is False

    activity.note_message(_update("agent_message_chunk"), 90.0)
    after_first_token = activity.snapshot(120.0)
    assert progress_stall_decision(
        pid_alive=True,
        progress_quiet_s=after_first_token["progress_quiet_for_s"],
        progress_stall_s=300.0,
        outstanding_count=after_first_token["outstanding_count"],
    ) is False


def test_active_monotonic_is_monotonic_float() -> None:
    start = active_monotonic()
    end = active_monotonic()

    assert isinstance(start, float)
    assert isinstance(end, float)
    assert end >= start


def test_simulated_sleep_excluded_from_liveness_budgets() -> None:
    active_now = {"value": 100.0}
    original_clock = acp_client.active_monotonic
    acp_client.active_monotonic = lambda: active_now["value"]
    try:
        activity = AcpLivenessActivity(last_event_mono=100.0, last_progress_mono=100.0)
        activity.note_message(_tool_update(), None)

        wall_elapsed_s = 1808.0
        active_now["value"] = 110.0
        snapshot = activity.snapshot()
        timed_out_tool = activity.timed_out(active_now["value"], max_tool_s=1800.0)
    finally:
        acp_client.active_monotonic = original_clock

    assert wall_elapsed_s > 1800.0
    assert snapshot["quiet_for_s"] == 10.0
    assert snapshot["progress_quiet_for_s"] == 10.0
    assert timed_out_tool is None
    assert classify_liveness(
        pid_alive=True,
        pgroup_cpu=None,
        seconds_since_event=snapshot["quiet_for_s"],
        thresholds=LivenessThresholds(idle_timeout_s=300.0, cpu_epsilon_pct=0.1),
    ) == "running"
    assert progress_stall_decision(
        pid_alive=True,
        progress_quiet_s=snapshot["progress_quiet_for_s"],
        progress_stall_s=300.0,
        outstanding_count=snapshot["outstanding_count"],
    ) is False


def test_freeze_guard_skips_terminal_eval_then_resumes() -> None:
    prev_wall = 0.0
    prev_active = 0.0
    wall_now = 1808.0
    active_now = 8.0
    total_paused_s = 0.0
    terminal_evals = 0
    status_notes: list[str] = []

    activity = AcpLivenessActivity(last_event_mono=0.0, last_progress_mono=0.0)
    activity.note_message(_tool_update(), 0.0)

    freeze_s = system_sleep_pause_s(
        prev_wall=prev_wall,
        prev_active=prev_active,
        wall_now=wall_now,
        active_now=active_now,
        heartbeat_interval_s=0.1,
    )
    if freeze_s > 0:
        total_paused_s += freeze_s
        status_notes.append(system_sleep_pause_note(freeze_s, total_paused_s))
    else:
        terminal_evals += 1
        activity.timed_out(active_now, max_tool_s=1800.0)

    assert terminal_evals == 0
    assert status_notes == ["paused 1800s (system sleep/suspend); total_paused 1800s"]

    prev_wall, prev_active = wall_now, active_now
    wall_now = 1808.2
    active_now = 8.2
    freeze_s = system_sleep_pause_s(
        prev_wall=prev_wall,
        prev_active=prev_active,
        wall_now=wall_now,
        active_now=active_now,
        heartbeat_interval_s=0.1,
    )
    if freeze_s > 0:
        status_notes.append(system_sleep_pause_note(freeze_s, total_paused_s + freeze_s))
    else:
        terminal_evals += 1
        timed_out_tool = activity.timed_out(active_now, max_tool_s=1800.0)
        progress_stalled = progress_stall_decision(
            pid_alive=True,
            progress_quiet_s=activity.snapshot(active_now)["progress_quiet_for_s"],
            progress_stall_s=300.0,
            outstanding_count=activity.outstanding_count(active_now),
        )

    assert terminal_evals == 1
    assert timed_out_tool is None
    assert progress_stalled is False


def _scripted_sampler(values):
    """Async CPU sampler yielding a fixed sequence; repeats the last value once
    exhausted so tests need not match cpu_liveness_keep_waiting's attempt count."""
    seq = list(values)
    state = {"i": 0}

    async def sampler():
        i = state["i"]
        state["i"] = i + 1
        return seq[i] if i < len(seq) else seq[-1]

    return sampler


async def _noop_sleep(_seconds: float) -> None:
    return None


def _keep_waiting(values, epsilon=0.1, attempts=3):
    return asyncio.run(
        cpu_liveness_keep_waiting(
            _scripted_sampler(values),
            epsilon,
            attempts=attempts,
            resample_s=0.0,
            sleep=_noop_sleep,
        )
    )


def test_cpu_keep_waiting_busy_first_sample_keeps_waiting() -> None:
    keep, cpu = _keep_waiting([5.0])
    assert keep is True and cpu == 5.0, (keep, cpu)


def test_cpu_keep_waiting_all_idle_is_wedged() -> None:
    keep, cpu = _keep_waiting([0.0, 0.0, 0.0])
    assert keep is False and cpu == 0.0, (keep, cpu)


def test_cpu_keep_waiting_transient_none_then_busy_keeps_waiting() -> None:
    # First `ps` sample fails (None) but the worker is actually busy — the grace
    # re-samples and finds CPU, so we keep waiting (no false-positive cancel).
    keep, cpu = _keep_waiting([None, 7.5])
    assert keep is True and cpu == 7.5, (keep, cpu)


def test_cpu_keep_waiting_all_none_is_wedged() -> None:
    # ps permanently unavailable → every sample None → wedged: correct fallback
    # to the pre-Phase-1 event-gap cancel (never an infinite hang).
    keep, cpu = _keep_waiting([None, None, None])
    assert keep is False and cpu is None, (keep, cpu)


def test_idle_gate_busy_keeps_waiting() -> None:
    gate = IdleLivenessGate(0.1, hard_wall_s=100.0, now=lambda: 0.0)
    keep, cpu = asyncio.run(gate.keep_waiting(_scripted_sampler([5.0])))
    assert keep is True and cpu == 5.0, (keep, cpu)


def test_idle_gate_idle_is_wedged() -> None:
    gate = IdleLivenessGate(0.1, hard_wall_s=100.0, now=lambda: 0.0)
    keep, cpu = asyncio.run(gate.keep_waiting(_scripted_sampler([0.0])))
    assert keep is False and cpu == 0.0, (keep, cpu)


def test_idle_gate_hard_wall_fires_after_sustained_quiet() -> None:
    clock = {"t": 0.0}
    gate = IdleLivenessGate(0.1, hard_wall_s=10.0, now=lambda: clock["t"])
    keep, _ = asyncio.run(gate.keep_waiting(_scripted_sampler([5.0])))  # wall starts at t=0
    assert keep is True
    clock["t"] = 11.0  # busy but past the wall → give up so a spinner can't hang
    keep, _ = asyncio.run(gate.keep_waiting(_scripted_sampler([5.0])))
    assert keep is False, "hard wall should fire after sustained running_quiet"


def test_idle_gate_event_resets_hard_wall() -> None:
    clock = {"t": 0.0}
    gate = IdleLivenessGate(0.1, hard_wall_s=10.0, now=lambda: clock["t"])
    keep, _ = asyncio.run(gate.keep_waiting(_scripted_sampler([5.0])))  # wall starts at t=0
    assert keep is True
    clock["t"] = 9.0
    gate.note_event()  # real progress → reset the wall
    clock["t"] = 11.0  # 11s since start but only 2s since the reset
    keep, _ = asyncio.run(gate.keep_waiting(_scripted_sampler([5.0])))
    assert keep is True, "a real event must reset the running_quiet hard wall"


@skipif(os.name == "nt", reason="POSIX process-group CPU sampler")
def test_cpu_keep_waiting_real_busy_subprocess_keeps_waiting() -> None:
    # End-to-end: REAL pgroup_cpu_pct sampling of a REAL CPU-spinning subprocess
    # → cpu_liveness_keep_waiting returns keep=True. Exercises the runner's
    # actual decision path (real ps, real process group), not a fake sampler.
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time\nend=time.time()+5\nx=0\nwhile time.time()<end:\n    x+=1\n"],
        start_new_session=True,  # worker is its own pgroup leader (mirrors the runner)
    )
    try:
        async def sampler():
            return await asyncio.to_thread(pgroup_cpu_pct, worker.pid)

        first_cpu = pgroup_cpu_pct(worker.pid)
        if first_cpu is None:
            print("SKIP: process-group CPU sampler unavailable")
            return
        keep, cpu = asyncio.run(cpu_liveness_keep_waiting(sampler, 0.1))
        assert keep is True, f"real CPU-busy worker should keep waiting (cpu={cpu})"
        assert cpu is not None and cpu > 0.1, cpu
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=2)
        except subprocess.TimeoutExpired:
            worker.kill()


def test_ps_output_parser_sums_matching_process_group() -> None:
    ps_output = """\
    501   0.0
   1200  13.5
   1200   2.5
   1300   7.0
"""
    assert parse_ps_pgroup_cpu(ps_output, 1200) == 16.0


def test_pgroup_cpu_pct_returns_float_or_none() -> None:
    sample = pgroup_cpu_pct(1)
    assert sample is None or isinstance(sample, float), sample


@skipif(os.name == "nt", reason="POSIX watcher process-group CPU sampler")
def test_python_watcher_busy_silence_records_running_quiet() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "tail.txt"
        status = tmp / "status.json"
        tail.write_text("")
        worker = subprocess.Popen([
            sys.executable,
            "-c",
            "import time\n"
            "end = time.time() + 8\n"
            "x = 0\n"
            "while time.time() < end:\n"
            "    x += 1\n",
        ])
        if pgroup_cpu_pct(worker.pid) is None:
            print("SKIP: process-group CPU sampler unavailable")
            worker.terminate()
            worker.wait(timeout=2)
            return
        watcher = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "goalflight_watch.py"),
                "--pid",
                str(worker.pid),
                "--tail",
                str(tail),
                "--status-json",
                str(status),
                "--poll-secs",
                "0.5",
                "--max-idle-secs",
                "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            deadline = time.time() + 5
            saw_running_quiet = False
            while time.time() < deadline:
                if watcher.poll() is not None:
                    out, err = watcher.communicate()
                    raise AssertionError(f"watcher exited early rc={watcher.returncode} out={out!r} err={err!r}")
                if status.exists():
                    payload = json.loads(status.read_text())
                    if payload.get("state") == "running_quiet":
                        assert payload.get("liveness_state") == "running_quiet", payload
                        assert payload.get("pgroup_cpu_pct") is None or payload["pgroup_cpu_pct"] > 0
                        saw_running_quiet = True
                        break
                time.sleep(0.1)
            assert saw_running_quiet, status.read_text() if status.exists() else "no status written"
            tail.write_text("COMPLETE: done\n")
            assert watcher.wait(timeout=3) == 0
        finally:
            if watcher.poll() is None:
                watcher.terminate()
                try:
                    watcher.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    watcher.kill()
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    worker.kill()


def main() -> None:
    test_busy_silent_worker_classifies_running_quiet()
    test_idle_silent_worker_classifies_wedged()
    test_none_cpu_idle_classifies_wedged()
    test_heartbeat_dead_sample_decision_table()
    test_heartbeat_first_token_grace_requires_progress_before_wedge()
    test_heartbeat_wedges_after_first_progress_and_resets_on_new_progress()
    test_progress_stall_ignores_raw_event_recency()
    test_progress_stall_resets_on_standard_progress()
    test_progress_stall_allows_cursor_like_slow_first_token()
    test_active_monotonic_is_monotonic_float()
    test_simulated_sleep_excluded_from_liveness_budgets()
    test_freeze_guard_skips_terminal_eval_then_resumes()
    test_cpu_keep_waiting_busy_first_sample_keeps_waiting()
    test_cpu_keep_waiting_all_idle_is_wedged()
    test_cpu_keep_waiting_transient_none_then_busy_keeps_waiting()
    test_cpu_keep_waiting_all_none_is_wedged()
    test_idle_gate_busy_keeps_waiting()
    test_idle_gate_idle_is_wedged()
    test_idle_gate_hard_wall_fires_after_sustained_quiet()
    test_idle_gate_event_resets_hard_wall()
    test_cpu_keep_waiting_real_busy_subprocess_keeps_waiting()
    test_ps_output_parser_sums_matching_process_group()
    test_pgroup_cpu_pct_returns_float_or_none()
    test_python_watcher_busy_silence_records_running_quiet()
    print("OK: liveness helper tests pass")


if __name__ == "__main__":
    main()
