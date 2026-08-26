#!/usr/bin/env python3
"""Idle watchdog must not kill a worker that is quiet but actually working.

Tail-byte silence became a false idle signal once capture started buffering
until a newline. These tests drive the real watcher against a real process
tree: a child that sleeps without printing, and a worker that writes its
own worktree without narrating. They do not inject a CPU/idle verdict.
"""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("idle activity tests use POSIX process trees")

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WATCH = ROOT / "scripts" / "goalflight_watch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat  # noqa: E402
import goalflight_watch  # noqa: E402


def _watcher_env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_TEST_MODE"] = "1"
    env.pop("GOALFLIGHT_TEST_PGROUP_CPU_PCT", None)
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "dispatch")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    return env


def _watcher_cmd(
    *,
    tail: Path,
    status: Path,
    worker_pid: int,
    dispatch_id: str,
    project_root: Path | None = None,
    worker_cwd: Path | None = None,
    poll_secs: str = "0.2",
    max_idle_secs: str = "1",
) -> list[str]:
    cmd = [
        sys.executable,
        str(WATCH),
        "--pid",
        str(worker_pid),
        "--tail",
        str(tail),
        "--status-json",
        str(status),
        "--dispatch-id",
        dispatch_id,
        "--poll-secs",
        poll_secs,
        "--max-idle-secs",
        max_idle_secs,
    ]
    if project_root is not None:
        cmd += ["--project-root", str(project_root)]
    if worker_cwd is not None:
        cmd += ["--worker-cwd", str(worker_cwd)]
    return cmd


def test_live_descendant_count_none_when_ps_unavailable() -> None:
    def _runner(*_args, **_kwargs):
        raise OSError("ps missing")

    assert goalflight_watch.live_descendant_count(1, ps_runner=_runner) is None


def test_live_descendant_count_walks_grandchildren() -> None:
    class _Result:
        stdout = "10 1\n11 10\n12 11\n13 2\n"
        returncode = 0

    def _runner(*_args, **_kwargs):
        return _Result()

    assert goalflight_watch.live_descendant_count(10, ps_runner=_runner) == 2


def _read_status(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def test_quiet_worker_with_sleeping_child_is_not_idle_killed(tmp_path: Path) -> None:
    tail = tmp_path / "worker.tail"
    tail.write_text("worker started\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    child_pid_file = tmp_path / "child.pid"
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, subprocess, time\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            f"Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
            "try:\n"
            "    time.sleep(60)\n"
            "finally:\n"
            "    child.kill()\n",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    watcher = None
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        assert child_pid_file.is_file(), "fixture never spawned the silent child"
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
        assert goalflight_compat.pid_alive(child_pid), child_pid
        count = goalflight_watch.live_descendant_count(worker.pid)
        assert count is not None and count >= 1, (
            f"precondition failed: worker {worker.pid} has no live child "
            f"(count={count}, child={child_pid})"
        )
        time.sleep(0.5)

        watcher = subprocess.Popen(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="quiet-child",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_watcher_env(tmp_path),
        )
        seen_descendants = False
        watch_deadline = time.monotonic() + 4.0
        payload: dict = {}
        while time.monotonic() < watch_deadline:
            if watcher.poll() is not None:
                break
            payload = _read_status(status)
            descendants = payload.get("live_descendants")
            if isinstance(descendants, int) and descendants >= 1:
                seen_descendants = True
                break
            time.sleep(0.1)
        assert watcher.poll() is None, (
            "watcher exited while the silent child was still running: "
            f"rc={watcher.returncode} status={payload} "
            f"stdout={(watcher.stdout.read() if watcher.stdout else '')} "
            f"stderr={(watcher.stderr.read() if watcher.stderr else '')}"
        )
        time.sleep(2.0)
        assert watcher.poll() is None, (
            "watcher idle-killed a quiet-but-working child: "
            f"rc={watcher.returncode} status={_read_status(status)}"
        )
        payload = _read_status(status)
        descendants = payload.get("live_descendants")
        assert isinstance(descendants, int) and descendants >= 1, payload
        assert seen_descendants or descendants >= 1, payload
        assert payload.get("state") not in {"idle_timeout", "wedged"}, payload
        assert payload.get("liveness_state") == "running_quiet", payload
        assert goalflight_compat.pid_alive(child_pid)
    finally:
        if watcher is not None and watcher.poll() is None:
            watcher.terminate()
            try:
                watcher.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                watcher.kill()
                watcher.communicate(timeout=5)
        worker.kill()
        worker.wait(timeout=5)


def test_quiet_worker_writing_worktree_is_not_idle_killed(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    worker_cwd = tmp_path / "worktree"
    project_root.mkdir()
    worker_cwd.mkdir()
    progress = worker_cwd / "progress.bin"
    tail = tmp_path / "worker.tail"
    tail.write_text("worker started\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\n"
            "from pathlib import Path\n"
            f"path = Path({str(progress)!r})\n"
            "n = 0\n"
            "while True:\n"
            "    n += 1\n"
            "    path.write_bytes(str(n).encode())\n"
            "    time.sleep(0.2)\n",
        ],
        cwd=str(worker_cwd),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    watcher = None
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not progress.exists():
            time.sleep(0.05)
        assert progress.is_file(), "fixture never wrote the worktree"
        env = _watcher_env(tmp_path)
        # CPU is not the subject: pin it idle so the watcher has to consult
        # the real worktree mtime. The file writes themselves are genuine.
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        watcher = subprocess.Popen(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="quiet-tree",
                project_root=project_root,
                worker_cwd=worker_cwd,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        watch_deadline = time.monotonic() + 4.0
        payload: dict = {}
        while time.monotonic() < watch_deadline:
            if watcher.poll() is not None:
                break
            payload = _read_status(status)
            tree_age = payload.get("idle_tree_age_s")
            if isinstance(tree_age, (int, float)) and tree_age < 1.0:
                break
            time.sleep(0.1)
        assert watcher.poll() is None, (
            "watcher exited while the worker was writing its tree: "
            f"rc={watcher.returncode} status={payload}"
        )
        time.sleep(2.0)
        assert watcher.poll() is None, (
            "watcher idle-killed a worktree-writing worker: "
            f"rc={watcher.returncode} status={_read_status(status)}"
        )
        payload = _read_status(status)
        assert payload.get("state") not in {"idle_timeout", "wedged"}, payload
        assert payload.get("liveness_state") == "running_quiet", payload
        tree_age = payload.get("idle_tree_age_s")
        assert isinstance(tree_age, (int, float)) and tree_age < 1.0, payload
    finally:
        if watcher is not None and watcher.poll() is None:
            watcher.terminate()
            try:
                watcher.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                watcher.kill()
                watcher.communicate(timeout=5)
        worker.kill()
        worker.wait(timeout=5)


def test_quiet_worker_without_children_still_idle_times_out(tmp_path: Path) -> None:
    tail = tmp_path / "worker.tail"
    tail.write_text("worker started\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        count = goalflight_watch.live_descendant_count(worker.pid)
        assert count == 0, f"precondition failed: idle fixture has children count={count}"
        proc = subprocess.run(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="truly-idle",
            ),
            capture_output=True,
            text=True,
            timeout=15,
            env=_watcher_env(tmp_path),
        )
        payload = _read_status(status)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr, payload)
        assert payload.get("state") == "idle_timeout", payload
        assert payload.get("live_descendants") in (0, None), payload
    finally:
        worker.kill()
        worker.wait(timeout=5)
