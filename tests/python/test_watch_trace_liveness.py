#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("goalflight_watch", ROOT / "scripts" / "goalflight_watch.py")
assert SPEC and SPEC.loader
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
STATUS_SPEC = importlib.util.spec_from_file_location(
    "goalflight_status", ROOT / "scripts" / "goalflight_status.py"
)
assert STATUS_SPEC and STATUS_SPEC.loader
status = importlib.util.module_from_spec(STATUS_SPEC)
STATUS_SPEC.loader.exec_module(status)


class Result:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout
        self.returncode = 0


def _runner(stdout: str, calls: list[list[str]]):
    def run(argv, **_kwargs):
        calls.append(argv)
        return Result(stdout)

    return run


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=2)
    return process.pid


def _watcher(
    root: Path,
    *,
    worker_pid: int,
    trace: Path,
    controller_pid: int | None = None,
    long_running_secs: float = 3600,
    review_secs: float = 7200,
) -> tuple[subprocess.Popen, Path, Path]:
    tail = root / "worker.tail"
    status_path = root / "status.json"
    tail.write_text("", encoding="utf-8")
    status_path.write_text(json.dumps({"trace_path": str(trace)}), encoding="utf-8")
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(root / "state")
    argv = [
        sys.executable,
        str(ROOT / "scripts" / "goalflight_watch.py"),
        "--pid",
        str(worker_pid),
        "--tail",
        str(tail),
        "--status-json",
        str(status_path),
        "--poll-secs",
        "0.05",
        "--max-idle-secs",
        "0.4",
        "--trace-long-running-secs",
        str(long_running_secs),
        "--trace-review-secs",
        str(review_secs),
    ]
    if controller_pid is not None:
        argv.extend(["--controller-pid", str(controller_pid)])
    process = subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    return process, status_path, tail


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_pinned_dispatch_home_resolves_by_construction() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = root / "state" / "dispatch-homes" / "d-1" / "sessions" / "turn.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text("{}\n")
        calls: list[list[str]] = []
        channel = watch.TraceLiveness(
            dispatch_id="d-1",
            worker_pid=10,
            effective_account="seat-a",
            state_dir=root / "state",
            home=root / "home",
            started_mono=0,
            lsof_runner=_runner("", calls),
        )
        sample = channel.sample(now_epoch=trace.stat().st_mtime, now_mono=1, idle_threshold=30)
        assert sample["trace_path"] == str(trace.resolve())
        assert sample["trace_mtime"] == trace.stat().st_mtime
        assert sample["trace_active"] is True
        assert calls == []


def test_lsof_fallback_searches_process_tree_and_caches() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        trace = home / ".kimi-code" / "sessions" / "child.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text("{}\n")
        ps_calls: list[list[str]] = []
        lsof_calls: list[list[str]] = []
        channel = watch.TraceLiveness(
            dispatch_id="d-2",
            worker_pid=10,
            state_dir=Path(tmp) / "state",
            home=home,
            started_mono=0,
            ps_runner=_runner("10 1\n11 10\n12 11\n", ps_calls),
            lsof_runner=_runner(f"p12\nn{trace}\n", lsof_calls),
        )
        assert channel.sample(now_epoch=trace.stat().st_mtime, now_mono=1, idle_threshold=30)["trace_active"]
        assert lsof_calls[0][-1] == "10,11,12"
        channel.sample(now_epoch=trace.stat().st_mtime, now_mono=2, idle_threshold=30)
        assert len(lsof_calls) == 1
        assert len(ps_calls) == 1


def test_resolution_retries_then_gives_up() -> None:
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        channel = watch.TraceLiveness(
            dispatch_id="d-3",
            worker_pid=10,
            state_dir=Path(tmp) / "state",
            home=Path(tmp) / "home",
            started_mono=0,
            retry_secs=5,
            ps_runner=_runner("", []),
            lsof_runner=_runner("", calls),
        )
        assert channel.sample(now_epoch=1, now_mono=1, idle_threshold=30) == {}
        assert channel.sample(now_epoch=2, now_mono=2, idle_threshold=30) == {}
        assert channel.sample(now_epoch=7, now_mono=7, idle_threshold=30) == {}
        assert len(calls) == 2


def test_lsof_timeout_degrades_to_absent_channel() -> None:
    def timeout(_argv, **_kwargs):
        raise subprocess.TimeoutExpired("lsof", 1)

    with tempfile.TemporaryDirectory() as tmp:
        channel = watch.TraceLiveness(
            dispatch_id="d-timeout",
            worker_pid=10,
            state_dir=Path(tmp) / "state",
            home=Path(tmp) / "home",
            started_mono=0,
            ps_runner=_runner("", []),
            lsof_runner=timeout,
        )
        assert channel.sample(now_epoch=1, now_mono=1, idle_threshold=30) == {}


def test_unknown_root_is_never_adopted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        unknown = Path(tmp) / "other-worker.jsonl"
        unknown.write_text("{}\n")
        channel = watch.TraceLiveness(
            dispatch_id="d-4",
            worker_pid=10,
            state_dir=Path(tmp) / "state",
            home=Path(tmp) / "home",
            started_mono=0,
            ps_runner=_runner("", []),
            lsof_runner=_runner(f"p10\nn{unknown}\n", []),
        )
        assert channel.sample(now_epoch=1, now_mono=1, idle_threshold=30) == {}


def test_pinned_dispatch_id_cannot_escape_known_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        escaped = root / "state" / "escaped" / "sessions" / "other.jsonl"
        escaped.parent.mkdir(parents=True)
        escaped.write_text("{}\n")
        channel = watch.TraceLiveness(
            dispatch_id="../escaped",
            worker_pid=10,
            effective_account="seat-a",
            state_dir=root / "state",
            home=root / "home",
            started_mono=0,
            ps_runner=_runner("", []),
            lsof_runner=_runner("", []),
        )
        assert channel.sample(now_epoch=1, now_mono=1, idle_threshold=30) == {}


def test_trace_veto_extends_idle_deadline() -> None:
    assert watch._trace_vetoes_idle(trace_active=True)
    assert not watch._trace_vetoes_idle(trace_active=False)


def test_growing_trace_caps_escalate_without_killing_and_notify() -> None:
    assert watch._trace_attention_state(
        trace_active=True,
        runtime_secs=12,
        long_running_secs=10,
        review_secs=20,
    ) == "long_running"
    assert watch._trace_attention_state(
        trace_active=True,
        runtime_secs=21,
        long_running_secs=10,
        review_secs=20,
    ) == "long_running_review"
    posted = []
    seen: set[str] = set()
    watch.post_trace_attention(
        "d-5",
        "long_running",
        seen,
        post_func=lambda **kwargs: posted.append(kwargs),
    )
    watch.post_trace_attention(
        "d-5",
        "long_running",
        seen,
        post_func=lambda **kwargs: posted.append(kwargs),
    )
    assert len(posted) == 1
    assert posted[0]["msg_type"] == "monitor"
    assert "remains live" in posted[0]["payload"]["text"]


def test_trace_stopping_resumes_normal_idle_classification() -> None:
    assert watch._trace_attention_state(
        trace_active=False,
        runtime_secs=100,
        long_running_secs=10,
        review_secs=20,
    ) is None
    verdict = watch.classify_liveness(
        True,
        0.0,
        31.0,
        watch.LivenessThresholds(idle_timeout_s=30.0, cpu_epsilon_pct=0.1),
    )
    assert verdict == "wedged"


def test_status_surfaces_quiet_console_active_trace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sidecar = Path(tmp) / "status.json"
        sidecar.write_text(
            '{"trace_path":"/known/session.jsonl","trace_mtime":10.0,'
            '"trace_active":true,"liveness_state":"running_via_trace",'
            '"state":"long_running"}\n'
        )
        row = status._decorate_trace_status({"status_path": str(sidecar)})
        assert row["trace_active"] is True
        assert row["trace_mtime"] == 10.0
        assert row["trace_attention_state"] == "long_running"
        assert status._signal(row) == "quiet console, active trace; long_running"


def test_channel_absent_keeps_verdict_identical() -> None:
    baseline = watch.classify_liveness(
        True,
        0.0,
        31.0,
        watch.LivenessThresholds(idle_timeout_s=30.0, cpu_epsilon_pct=0.1),
    )
    absent = watch.classify_liveness(
        True,
        0.0,
        31.0,
        watch.LivenessThresholds(idle_timeout_s=30.0, cpu_epsilon_pct=0.1),
    )
    assert baseline == absent == "wedged"


def test_status_channel_absent_is_byte_for_byte_equivalent() -> None:
    row = {"dispatch_id": "absent", "status_path": "/definitely/missing/status.json"}
    assert status._decorate_trace_status(row) == row


def test_dead_pid_fresh_trace_reverifies_then_stale_trace_terminalizes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = root / "state" / "dispatch-homes" / "dead-worker" / "sessions" / "turn.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text("{}\n", encoding="utf-8")
        process, status_path, _tail = _watcher(
            root,
            worker_pid=_dead_pid(),
            trace=trace,
        )
        try:
            assert _wait_for(
                lambda: status_path.exists()
                and _payload(status_path).get("reason")
                == "pid_resolved_dead_active_trace_reverify"
            ), _payload(status_path)
            assert process.poll() is None
            assert _payload(status_path)["state"] not in {
                "complete", "failed", "blocked", "worker_dead", "orphaned", "idle_timeout"
            }

            stale = time.time() - 2
            os.utime(trace, (stale, stale))
            assert process.wait(timeout=3) == 1
            assert _payload(status_path)["state"] == "worker_dead"
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)


def test_dead_pid_stale_trace_reaches_worker_dead_terminal_branch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = root / "state" / "dispatch-homes" / "stale-worker" / "sessions" / "turn.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text("{}\n", encoding="utf-8")
        stale = time.time() - 2
        os.utime(trace, (stale, stale))
        process, status_path, _tail = _watcher(
            root,
            worker_pid=_dead_pid(),
            trace=trace,
        )
        assert process.wait(timeout=3) == 1
        assert _payload(status_path)["state"] == "worker_dead"


def test_live_growing_trace_survives_caps_and_dead_controller_branch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = root / "state" / "dispatch-homes" / "live-worker" / "sessions" / "turn.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text("{}\n", encoding="utf-8")
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            start_new_session=True,
        )
        process, status_path, tail = _watcher(
            root,
            worker_pid=worker.pid,
            trace=trace,
            controller_pid=_dead_pid(),
            long_running_secs=0.01,
            review_secs=0.2,
        )
        try:
            assert _wait_for(
                lambda: status_path.exists()
                and _payload(status_path).get("state") == "long_running"
            ), _payload(status_path)
            assert process.poll() is None
            for _ in range(6):
                trace.write_text(trace.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
                time.sleep(0.05)
            assert _wait_for(
                lambda: _payload(status_path).get("state") == "long_running_review"
            ), _payload(status_path)
            payload = _payload(status_path)
            assert payload["state"] not in {
                "complete", "failed", "blocked", "worker_dead", "orphaned", "idle_timeout"
            }
            assert payload["worker_alive"] is True
            assert process.poll() is None
            tail.write_text("COMPLETE: test finished\n", encoding="utf-8")
            assert process.wait(timeout=3) == 0
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=2)


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("OK: watch trace liveness")
