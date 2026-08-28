#!/usr/bin/env python3
"""Two-tier watchlist: cheap tail-stat, expensive conjunction, three-state verdict.

Real subprocesses supply idle vs busy CPU. A long-gate false positive is a
process that burns CPU (or writes the tree) with a frozen tail.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("wedge watchlist CPU samples use POSIX process groups")

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_wedge_watch as wedge  # noqa: E402
from goalflight_liveness import cputime_delta_seconds, pgroup_cputime_snapshot  # noqa: E402


def _classify(**overrides):
    kwargs = {
        "worker_alive": True,
        "quiet_s": 2000.0,
        "probation_s": 1080.0,
        "tail_delta_bytes": 0,
        "tree_writes": 0,
        "tree_available": True,
        "cpu_s": 0.0,
        "sample_interval_s": 5.0,
        "socket_state": wedge.SOCKET_NONE,
    }
    kwargs.update(overrides)
    return wedge.classify_wedge_watch(**kwargs)


def test_probation_is_above_measured_grok_p99() -> None:
    assert wedge.DEFAULT_PROBATION_S == 1080.0
    assert wedge.DEFAULT_PROBATION_S > 965.7


def test_cheap_path_below_probation_is_live_without_expensive_probes() -> None:
    verdict, reason = _classify(
        quiet_s=60.0,
        tree_writes=None,
        tree_available=False,
        cpu_s=None,
        sample_interval_s=None,
        socket_state=wedge.SOCKET_UNKNOWN,
    )
    assert verdict == wedge.VERDICT_LIVE
    assert reason is None


def test_observe_cheap_path_does_not_report_expensive_probes() -> None:
    obs = wedge.observe_wedge(
        dispatch_id="b273-wedge",
        worker_alive=True,
        quiet_s=30.0,
        tail_delta_bytes=12,
        probation_s=1080.0,
        tree_writes=9,
        tree_available=True,
        cpu_s=3.5,
        sample_interval_s=2.0,
        socket_state=wedge.SOCKET_NONE,
    )
    assert obs.verdict == wedge.VERDICT_LIVE
    assert obs.watchlisted is False
    assert obs.cpu_s is None
    assert obs.tree_writes is None
    assert obs.socket_state is None


def test_conjunction_with_no_provider_socket_is_wedged() -> None:
    verdict, reason = _classify()
    assert verdict == wedge.VERDICT_WEDGED
    assert reason is None


def test_established_provider_socket_is_unknown_not_wedged() -> None:
    verdict, reason = _classify(socket_state=wedge.SOCKET_PROVIDER)
    assert verdict == wedge.VERDICT_UNKNOWN
    assert reason == "waiting on provider"


def test_unknown_socket_is_unknown_not_wedged() -> None:
    verdict, reason = _classify(socket_state=wedge.SOCKET_UNKNOWN)
    assert verdict == wedge.VERDICT_UNKNOWN
    assert "socket" in (reason or "")


def test_missing_cpu_sample_is_unknown_not_wedged() -> None:
    verdict, reason = _classify(cpu_s=None, sample_interval_s=None)
    assert verdict == wedge.VERDICT_UNKNOWN
    assert "cpu_s" in (reason or "")


def test_unreadable_tree_is_unknown_not_wedged() -> None:
    verdict, reason = _classify(tree_writes=None, tree_available=False)
    assert verdict == wedge.VERDICT_UNKNOWN
    assert "tree" in (reason or "")


def test_tree_write_vetoes_wedge() -> None:
    verdict, reason = _classify(tree_writes=3)
    assert verdict == wedge.VERDICT_LIVE


def test_cpu_moving_vetoes_wedge() -> None:
    verdict, reason = _classify(cpu_s=1.25)
    assert verdict == wedge.VERDICT_LIVE


def test_status_line_names_unknown_reason() -> None:
    obs = wedge.observe_wedge(
        dispatch_id="b318-capture",
        worker_alive=True,
        quiet_s=11 * 60,
        tail_delta_bytes=0,
        probation_s=300.0,
        tree_writes=0,
        tree_available=True,
        cpu_s=0.0,
        sample_interval_s=30.0,
        socket_state=wedge.SOCKET_PROVIDER,
        watchlisted_s=6 * 60,
    )
    line = wedge.format_status_line(obs)
    assert line.startswith("b318-capture  UNKNOWN  quiet=11m  cpu_s=0.0  tree_writes=0  tail=+0B")
    assert "(watchlisted 6m)" in line
    assert "waiting on provider" in line


def test_lsof_provider_name_is_provider() -> None:
    names = ["127.0.0.1:12345->api.x.ai:443"]
    assert wedge.classify_socket_names(names) == wedge.SOCKET_PROVIDER


def test_lsof_remote_ip_is_unknown_not_none() -> None:
    names = ["10.0.0.2:54321->20.1.2.3:443"]
    assert wedge.classify_socket_names(names) == wedge.SOCKET_UNKNOWN


def test_lsof_loopback_only_is_none() -> None:
    names = ["127.0.0.1:9->127.0.0.1:80"]
    assert wedge.classify_socket_names(names) == wedge.SOCKET_NONE


def test_failed_lsof_is_unknown() -> None:
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("lsof")

    assert wedge.provider_socket_state(1, lsof_runner=boom) == wedge.SOCKET_UNKNOWN


def test_count_tree_writes_sees_real_new_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old = root / "old.py"
        old.write_text("x\n", encoding="utf-8")
        now = time.time()
        os.utime(old, (now - 400.0, now - 400.0))
        sample = wedge.count_tree_writes_since(root, since_mtime=now - 60.0)
        assert sample.available is True
        assert sample.count == 0
        recent = root / "recent.py"
        recent.write_text("y\n", encoding="utf-8")
        sample = wedge.count_tree_writes_since(root, since_mtime=now - 60.0)
        assert sample.available is True
        assert sample.count == 1


def test_git_dir_write_is_not_a_tree_write() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "file.py"
        src.write_text("x\n", encoding="utf-8")
        git_index = root / ".git" / "index"
        git_index.parent.mkdir()
        git_index.write_text("idx\n", encoding="utf-8")
        now = time.time()
        os.utime(src, (now - 400.0, now - 400.0))
        sample = wedge.count_tree_writes_since(root, since_mtime=now - 60.0)
        assert sample.available is True
        assert sample.count == 0


def _cpu_delta_for(code: str, window_s: float = 0.8) -> tuple[float | None, subprocess.Popen]:
    worker = subprocess.Popen(
        [sys.executable, "-c", code],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        first = pgroup_cputime_snapshot(worker.pid)
        time.sleep(window_s)
        second = pgroup_cputime_snapshot(worker.pid)
        if first is None or second is None:
            return None, worker
        return cputime_delta_seconds(first, second), worker
    except Exception:
        worker.kill()
        worker.wait(timeout=2)
        raise


def _reap(worker: subprocess.Popen) -> None:
    worker.terminate()
    try:
        worker.wait(timeout=2)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=2)


def test_real_sleeper_cpu_delta_is_near_zero() -> None:
    delta, worker = _cpu_delta_for("import time; time.sleep(8)", window_s=0.8)
    try:
        if delta is None:
            print("SKIP: test_real_sleeper_cpu_delta_is_near_zero: cpu snapshot unavailable")
            return
        assert delta <= wedge.CPU_EPSILON_S, delta
        verdict, _reason = _classify(cpu_s=delta, sample_interval_s=0.8)
        assert verdict == wedge.VERDICT_WEDGED, (verdict, delta)
    finally:
        _reap(worker)


def test_real_spinner_cpu_delta_is_positive() -> None:
    delta, worker = _cpu_delta_for(
        "end=__import__('time').time()+5\nx=0\nwhile __import__('time').time()<end:\n    x+=1\n",
        window_s=0.8,
    )
    try:
        if delta is None:
            print("SKIP: test_real_spinner_cpu_delta_is_positive: cpu snapshot unavailable")
            return
        assert delta > wedge.CPU_EPSILON_S, delta
        verdict, _reason = _classify(cpu_s=delta, sample_interval_s=0.8)
        assert verdict == wedge.VERDICT_LIVE, (verdict, delta)
    finally:
        _reap(worker)


def test_long_gate_real_cpu_is_not_wedged() -> None:
    """A worker quiet on the tail because it is running a long gate is live."""
    delta, worker = _cpu_delta_for(
        "end=__import__('time').time()+5\nx=0\nwhile __import__('time').time()<end:\n    x+=1\n",
        window_s=0.8,
    )
    try:
        if delta is None:
            print("SKIP: test_long_gate_real_cpu_is_not_wedged: cpu snapshot unavailable")
            return
        obs = wedge.observe_wedge(
            dispatch_id="gate-run",
            worker_alive=True,
            quiet_s=2000.0,
            tail_delta_bytes=0,
            probation_s=1080.0,
            tree_writes=0,
            tree_available=True,
            cpu_s=delta,
            sample_interval_s=0.8,
            socket_state=wedge.SOCKET_NONE,
            watchlisted_s=600.0,
        )
        assert obs.verdict == wedge.VERDICT_LIVE, (obs, delta)
        assert "wedged" not in wedge.format_status_line(obs)
    finally:
        _reap(worker)


def test_long_gate_real_tree_writes_are_not_wedged() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import pathlib, time\n"
                    f"p = pathlib.Path({str(root / 'gate.out')!r})\n"
                    "end = time.time() + 1.2\n"
                    "n = 0\n"
                    "while time.time() < end:\n"
                    "    n += 1\n"
                    "    p.write_text(str(n))\n"
                    "    time.sleep(0.05)\n"
                ),
            ],
            start_new_session=True,
        )
        try:
            time.sleep(0.4)
            sample = wedge.count_tree_writes_since(root, since_mtime=time.time() - 2.0)
            assert sample.available is True
            assert sample.count and sample.count > 0, sample
            verdict, _reason = _classify(tree_writes=sample.count, cpu_s=0.0)
            assert verdict == wedge.VERDICT_LIVE
        finally:
            _reap(worker)


def main() -> None:
    test_probation_is_above_measured_grok_p99()
    test_cheap_path_below_probation_is_live_without_expensive_probes()
    test_observe_cheap_path_does_not_report_expensive_probes()
    test_conjunction_with_no_provider_socket_is_wedged()
    test_established_provider_socket_is_unknown_not_wedged()
    test_unknown_socket_is_unknown_not_wedged()
    test_missing_cpu_sample_is_unknown_not_wedged()
    test_unreadable_tree_is_unknown_not_wedged()
    test_tree_write_vetoes_wedge()
    test_cpu_moving_vetoes_wedge()
    test_status_line_names_unknown_reason()
    test_lsof_provider_name_is_provider()
    test_lsof_remote_ip_is_unknown_not_none()
    test_lsof_loopback_only_is_none()
    test_failed_lsof_is_unknown()
    test_count_tree_writes_sees_real_new_file()
    test_git_dir_write_is_not_a_tree_write()
    test_real_sleeper_cpu_delta_is_near_zero()
    test_real_spinner_cpu_delta_is_positive()
    test_long_gate_real_cpu_is_not_wedged()
    test_long_gate_real_tree_writes_are_not_wedged()
    print("OK: wedge watchlist tests pass")


if __name__ == "__main__":
    main()
