"""Focused tests for Phase 1 CPU/heartbeat liveness helpers."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from goalflight_liveness import (  # noqa: E402
    heartbeat_wedge_decision,
    IdleLivenessGate,
    LivenessThresholds,
    classify_liveness,
    cpu_liveness_keep_waiting,
    parse_ps_pgroup_cpu,
    pgroup_cpu_pct,
)


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
        ("outstanding-tool", 0.0, 5, 5, 1, 2, False, 0, False),
        ("none-cpu", None, 5, 5, 0, 2, False, 0, False),
        ("new-event", 0.0, 6, 5, 0, 2, False, 0, False),
    ]
    for name, cpu, events_seen, previous_events_seen, outstanding, previous_dead, dead, streak, wedged in cases:
        decision = heartbeat_wedge_decision(
            pid_alive=True,
            pgroup_cpu=cpu,
            events_seen=events_seen,
            previous_events_seen=previous_events_seen,
            outstanding_count=outstanding,
            cpu_epsilon_pct=0.1,
            previous_dead_samples=previous_dead,
            wedge_samples=3,
        )
        assert decision.dead_sample is dead, name
        assert decision.dead_samples == streak, name
        assert decision.wedged is wedged, name


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
