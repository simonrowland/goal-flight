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
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WATCH = ROOT / "scripts" / "goalflight_watch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat  # noqa: E402
import goalflight_capacity  # noqa: E402
import goalflight_acp_run  # noqa: E402
import goalflight_ledger  # noqa: E402
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
    liveness_indeterminate_secs: str | None = None,
    stay_after_terminal: bool = False,
    detached: bool = False,
    worker_identity: dict | None = None,
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
    if liveness_indeterminate_secs is not None:
        cmd += ["--liveness-indeterminate-secs", liveness_indeterminate_secs]
    if stay_after_terminal:
        cmd.append("--stay-after-terminal")
    if detached:
        cmd.append("--detached")
    if worker_identity is not None:
        cmd += ["--worker-identity-json", json.dumps(worker_identity)]
    return cmd


def _descendant_ps_fail_bindir(tmp: Path) -> Path:
    """PATH prefix whose `ps` fails the descendant walk and proxies the rest.

    The walk is `ps -axo pid=,ppid=,state=`. CPU sampling uses a different
    format (`ps -A -o pgid=,pid=,time=`), so this is a real enumeration
    failure, not a double that returns "no children".
    """
    bindir = tmp / "ps-fail-bin"
    bindir.mkdir()
    real_ps = shutil.which("ps") or "/bin/ps"
    stub = bindir / "ps"
    stub.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    pid=,ppid=*)\n"
        "      echo 'ps: enumeration failed' >&2\n"
        "      exit 1\n"
        "      ;;\n"
        "  esac\n"
        "done\n"
        f"exec {real_ps} \"$@\"\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def _descendant_ps_table_bindir(tmp: Path, table: Path) -> Path:
    """PATH prefix exposing an asserted real parent/child pair to the ps parser."""
    bindir = tmp / "ps-table-bin"
    bindir.mkdir()
    real_ps = shutil.which("ps") or "/bin/ps"
    stub = bindir / "ps"
    stub.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    pid=,ppid=*)\n"
        f"      cat {str(table)!r}\n"
        "      exit $?\n"
        "      ;;\n"
        "  esac\n"
        "done\n"
        f"exec {real_ps} \"$@\"\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bindir


def test_live_descendant_count_none_when_ps_unavailable() -> None:
    def _runner(*_args, **_kwargs):
        raise OSError("ps missing")

    assert goalflight_watch.live_descendant_count(1, ps_runner=_runner) is None


def test_live_descendant_count_none_when_ps_nonzero() -> None:
    """Failed ps stays unknown even when stdout looks like a process table.

    This must fail if a later zombie-filter simplification treats unparsable
    or failed samples as no-children.
    """
    class _Result:
        stdout = "10 1 S\n11 10 Z\n"
        returncode = 1

    def _runner(*_args, **_kwargs):
        return _Result()

    assert goalflight_watch.live_descendant_count(10, ps_runner=_runner) is None


def test_mtime_sample_empty_tree_is_available(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    sample = goalflight_watch.sample_newest_mtime_under(root)
    assert sample.available is True
    assert sample.newest is None


def test_mtime_sample_unavailable_when_walk_raises(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "cwd"
    root.mkdir()
    (root / "file.txt").write_text("x\n", encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise OSError("walk failed")

    monkeypatch.setattr(os, "walk", _boom)
    sample = goalflight_watch.sample_newest_mtime_under(root)
    assert sample.available is False
    assert goalflight_watch.newest_mtime_under(root) is None


def test_mtime_sample_unavailable_when_scandir_fails(tmp_path: Path) -> None:
    root = tmp_path / "cwd"
    root.mkdir()
    (root / "file.txt").write_text("x\n", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / "inner.txt").write_text("y\n", encoding="utf-8")
    nested.chmod(0)
    try:
        sample = goalflight_watch.sample_newest_mtime_under(root)
        # macOS owner-chmod-000 can still list; if the walk actually
        # finished, this is not the failure path we need.
        if sample.available:
            root.chmod(0)
            sample = goalflight_watch.sample_newest_mtime_under(root)
        assert sample.available is False, sample
    finally:
        nested.chmod(0o700)
        root.chmod(0o700)


def _make_newest_unstatable_file(root: Path) -> Path:
    """Create a newest directory entry whose real ``stat()`` follows nowhere."""
    seed = root / "older.txt"
    seed.write_text("old\n", encoding="utf-8")
    now_ns = time.time_ns()
    os.utime(seed, ns=(now_ns - 10_000_000_000, now_ns - 10_000_000_000))
    broken = root / "newest.txt"
    broken.symlink_to(root / "missing-target")
    os.utime(broken, ns=(now_ns, now_ns), follow_symlinks=False)
    try:
        broken.stat()
    except OSError:
        pass
    else:
        raise AssertionError("precondition failed: dangling newest file stat succeeded")
    assert broken.lstat().st_mtime_ns > seed.stat().st_mtime_ns
    return broken


def _ps_count_runner(stdout: str, returncode: int = 0):
    class _Result:
        def __init__(self) -> None:
            self.stdout = stdout
            self.returncode = returncode

    def _runner(*_args, **_kwargs):
        return _Result()

    return _runner


def test_live_descendant_count_walks_grandchildren() -> None:
    stdout = "10 1 S\n11 10 S\n12 11 S\n13 2 R\n"
    assert goalflight_watch.live_descendant_count(
        10, ps_runner=_ps_count_runner(stdout)
    ) == 2


def test_live_descendant_count_requests_state_in_the_same_sample() -> None:
    seen: dict[str, object] = {}

    class _Result:
        stdout = "10 1 S\n11 10 Z\n"
        returncode = 0

    def _runner(argv, **_kwargs):
        seen["argv"] = argv
        return _Result()

    assert goalflight_watch.live_descendant_count(10, ps_runner=_runner) == 0
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[-1] == goalflight_watch.PS_LIVE_DESCENDANT_FORMAT
    assert "state=" in str(argv[-1])


def test_live_descendant_count_none_when_ps_unparsable() -> None:
    """Garbage stdout must stay unknown, not simplify to zero children."""
    assert (
        goalflight_watch.live_descendant_count(
            10, ps_runner=_ps_count_runner("not-a-process-table\n")
        )
        is None
    )


def test_live_descendant_count_missing_state_is_not_a_zombie() -> None:
    # Absent state token is not evidence of death: count the child as live.
    stdout = "10 1\n11 10\n"
    assert goalflight_watch.live_descendant_count(
        10, ps_runner=_ps_count_runner(stdout)
    ) == 1


def test_live_descendant_count_filters_zombie_rows_at_ps_seam() -> None:
    zombie_only = "10 1 S\n11 10 Z\n12 2 R\n"
    assert goalflight_watch.live_descendant_count(
        10, ps_runner=_ps_count_runner(zombie_only)
    ) == 0
    defunct_token = "10 1 S\n11 10 <defunct>\n"
    assert goalflight_watch.live_descendant_count(
        10, ps_runner=_ps_count_runner(defunct_token)
    ) == 0
    mixed = "10 1 S\n11 10 S\n12 11 Z\n"
    assert goalflight_watch.live_descendant_count(
        10, ps_runner=_ps_count_runner(mixed)
    ) == 1


def _spawn_worker_with_zombie_child() -> tuple[subprocess.Popen, int]:
    """Fork a child that exits without being reaped so it is a real Z."""
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, sys, time\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    os._exit(0)\n"
            "print(child, flush=True)\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert worker.stdout is not None
    raw = worker.stdout.readline()
    try:
        zombie_pid = int(raw.strip())
    except ValueError as exc:
        worker.kill()
        worker.wait(timeout=5)
        raise AssertionError(f"zombie fixture did not print a pid: {raw!r}") from exc
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if goalflight_compat.pid_is_zombie(zombie_pid) is True:
            return worker, zombie_pid
        time.sleep(0.05)
    worker.kill()
    worker.wait(timeout=5)
    raise AssertionError(
        f"precondition failed: child {zombie_pid} of worker {worker.pid} "
        "never became a zombie"
    )


def test_zombie_descendant_counts_as_zero_live_work() -> None:
    worker = None
    try:
        worker, zombie_pid = _spawn_worker_with_zombie_child()
        assert goalflight_compat.pid_is_zombie(zombie_pid) is True, zombie_pid
        count = goalflight_watch.live_descendant_count(worker.pid)
        assert count == 0, (
            f"zombie-only tree must count as zero live descendants "
            f"(count={count}, worker={worker.pid}, zombie={zombie_pid})"
        )
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def _read_status(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _auto_reap_worker(worker: subprocess.Popen) -> None:
    """Match detached production, whose worker is promptly reaped by init."""
    def reap() -> None:
        while worker.poll() is None:
            time.sleep(0.02)

    threading.Thread(target=reap, daemon=True).start()


def _seed_managed_detached_lease(
    *,
    dispatch_id: str,
    lease_id: str,
    worker: subprocess.Popen,
    tail: Path,
    status: Path,
    project_root: Path,
) -> dict:
    identity = goalflight_ledger.process_identity(worker.pid)
    assert identity and identity.get("start_token"), identity
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "transport": "bash",
            "project_root": str(project_root),
            "worker_pid": worker.pid,
            "worker_identity": identity,
            "worker_pgid": identity.get("pgid") or worker.pid,
            "lease_id": lease_id,
            "detached": True,
            "stdout_path": str(tail),
            "status_path": str(status),
            "state": "running",
            "terminal_state": "unknown",
            "started_at": goalflight_ledger.utc_now(),
        }
    )
    capacity = goalflight_capacity.load_state()
    capacity["leases"][lease_id] = {
        "lease_id": lease_id,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "project_root": str(project_root),
        "worker_pid": worker.pid,
        "state": "active",
        "started_at": goalflight_capacity.iso(),
    }
    goalflight_capacity.save_state(capacity)
    return identity


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
    _auto_reap_worker(worker)
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
        assert payload.get("state") not in {
            "idle_timeout",
            "wedged",
            "liveness_indeterminate",
        }, payload
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
    _auto_reap_worker(worker)
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
        assert payload.get("state") not in {
            "idle_timeout",
            "wedged",
            "liveness_indeterminate",
        }, payload
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
    project_root = tmp_path / "repo"
    worker_cwd = tmp_path / "worktree"
    project_root.mkdir()
    worker_cwd.mkdir()
    tail = tmp_path / "worker.tail"
    tail.write_text("worker started\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(worker_cwd),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        count = goalflight_watch.live_descendant_count(worker.pid)
        assert count == 0, f"precondition failed: idle fixture has children count={count}"
        env = _watcher_env(tmp_path)
        # Known-idle CPU is the precondition: unknown CPU must wait, not
        # idle_timeout. Pin it so this test is "looked and found nothing".
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        proc = subprocess.run(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="truly-idle",
                project_root=project_root,
                worker_cwd=worker_cwd,
            ),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        payload = _read_status(status)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr, payload)
        assert payload.get("state") == "idle_timeout", payload
        assert payload.get("live_descendants") == 0, payload
    finally:
        worker.kill()
        worker.wait(timeout=5)


def test_zombie_descendant_reaches_idle_timeout(tmp_path: Path) -> None:
    """A zombie-only tree is not live work: take the ordinary idle path.

    The worktree mtime sample must still run (a positive zombie count used to
    skip it). Empty measured tree + idle CPU + zero live descendants = idle.
    """
    project_root = tmp_path / "repo"
    worker_cwd = tmp_path / "worktree"
    project_root.mkdir()
    worker_cwd.mkdir()
    tail = tmp_path / "worker.tail"
    tail.write_text("worker started\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    worker = None
    try:
        worker, zombie_pid = _spawn_worker_with_zombie_child()
        assert goalflight_compat.pid_is_zombie(zombie_pid) is True, zombie_pid
        count = goalflight_watch.live_descendant_count(worker.pid)
        assert count == 0, (
            f"precondition failed: zombie-only tree counted as live "
            f"(count={count}, worker={worker.pid}, zombie={zombie_pid})"
        )
        env = _watcher_env(tmp_path)
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        proc = subprocess.run(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="zombie-descendant",
                project_root=project_root,
                worker_cwd=worker_cwd,
            ),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        payload = _read_status(status)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr, payload)
        assert payload.get("state") == "idle_timeout", payload
        assert payload.get("live_descendants") == 0, payload
        assert payload.get("tree_probe") == "measured", payload
        assert payload.get("liveness_state") != "running_quiet", payload
        unknown = payload.get("liveness_unknown_probes") or []
        assert "descendants" not in unknown, payload
        assert "tree_mtime" not in unknown, payload
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def _run_quiet_sleeper(tmp_path: Path) -> subprocess.Popen:
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _auto_reap_worker(worker)
    return worker


def test_indeterminate_cleanup_rejects_wrong_process_group(tmp_path: Path) -> None:
    worker = _run_quiet_sleeper(tmp_path)
    sentinel = _run_quiet_sleeper(tmp_path)
    try:
        identity = goalflight_ledger.process_identity(worker.pid)
        assert identity and identity.get("start_token"), identity
        result = goalflight_watch.terminate_indeterminate_worker(
            worker.pid,
            sentinel.pid,
            identity,
            term_grace_s=0.05,
            kill_grace_s=0.05,
        )
        assert result["worker_disposition"] == "indeterminate_cleanup_failed", result
        assert result["worker_termination_signals"] == [], result
        assert "unverified worker process group" in result["worker_termination_error"], result
        assert worker.poll() is None, "wrong PGID check signaled the worker"
        assert sentinel.poll() is None, "wrong PGID check signaled the sentinel"
    finally:
        worker.kill()
        sentinel.kill()
        worker.wait(timeout=5)
        sentinel.wait(timeout=5)


def test_indeterminate_cleanup_retains_unverified_group_after_leader_exit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    lease_id = "leader-exit-race-lease"
    child_pid_file = tmp_path / "pinned-child.pid"
    ready = tmp_path / "pinned-ready"
    release = tmp_path / "release-leader"
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, signal, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen([\n"
            "    sys.executable, '-c',\n"
            "    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'\n"
            "])\n"
            f"Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
            f"Path({str(ready)!r}).write_text('ready')\n"
            f"release = Path({str(release)!r})\n"
            "while not release.exists():\n"
            "    time.sleep(0.02)\n"
            "os._exit(0)\n",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _auto_reap_worker(worker)
    child_pid = None
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        assert ready.exists(), "leader never established the resistant child"
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        identity = goalflight_ledger.process_identity(worker.pid)
        assert identity and identity.get("start_token"), identity
        capacity = goalflight_capacity.load_state()
        capacity["leases"][lease_id] = {
            "lease_id": lease_id,
            "dispatch_id": "leader-exit-race",
            "agent": "codex",
            "state": "active",
        }
        goalflight_capacity.save_state(capacity)
        goalflight_acp_run.attach_worker_to_capacity_lease(
            lease_id,
            worker.pid,
            worker.pid,
        )
        attached = goalflight_capacity.load_state()["leases"][lease_id]
        assert attached["worker_pid"] == worker.pid, attached
        assert attached["worker_pgid"] == worker.pid, attached

        release.write_text("go", encoding="utf-8")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and worker.poll() is None:
            time.sleep(0.02)
        assert worker.poll() is not None, "leader did not exit before cleanup entry"
        assert goalflight_compat.pid_alive(child_pid), child_pid

        # Reconciliation can race cleanup after the leader exits. The PGID
        # recorded at initial attachment must keep the lease visible before the
        # later indeterminate-retention reason has been persisted.
        rc = goalflight_capacity.main(
            ["release-stale", "--keep", "--reason", "test_pre_cleanup_race"]
        )
        assert rc == 0, rc
        pre_cleanup = goalflight_capacity.load_state()["leases"][lease_id]
        assert pre_cleanup["state"] == "active", pre_cleanup
        assert pre_cleanup.get("reason") != goalflight_capacity.INDETERMINATE_LIVE_REASON

        result = goalflight_watch.terminate_indeterminate_worker(
            worker.pid,
            worker.pid,
            identity,
            term_grace_s=0.1,
            kill_grace_s=0.1,
        )
        assert result["worker_disposition"] == "indeterminate_cleanup_failed", result
        assert result["worker_termination_signals"] == [], result
        assert "historical PGID retained but not signaled" in result["worker_termination_error"], result
        assert goalflight_compat.pid_alive(child_pid), child_pid
        capacity_result = goalflight_watch.release_indeterminate_capacity(
            {"lease_id": lease_id, "worker_pgid": worker.pid},
            worker_disposition=result,
            reason="liveness_indeterminate",
        )
        assert capacity_result["capacity_lease_disposition"] == "retained_worker_live", capacity_result
        retained = goalflight_capacity.load_state()["leases"][lease_id]
        assert retained["state"] == "active", retained
        assert retained["reason"] == "liveness_indeterminate_worker_live", retained
        assert retained.get("accounted_live_at"), retained
        assert retained.get("accounted_live_until"), retained
        assert retained.get("accounted_live_pgid") == worker.pid, retained

        # Exercise the same stale-reconciliation entry point used before every
        # local drain. The dead leader cannot free capacity while its resistant
        # child still occupies the unresolved historical group.
        rc = goalflight_capacity.main(
            ["release-stale", "--keep", "--reason", "test_release_stale"]
        )
        assert rc == 0, rc
        reconciled = goalflight_capacity.load_state()["leases"][lease_id]
        assert reconciled["state"] == "active", reconciled
        assert len(goalflight_capacity.active_leases(goalflight_capacity.load_state())) == 1

        # The reap deadline bounds cleanup latency, not accounting. Expire it
        # while the real resistant child still owns the historical group; both
        # the direct predicate and stale reconciliation must retain the lease.
        reconciled["accounted_live_until"] = "1970-01-01T00:00:00Z"
        capacity = goalflight_capacity.load_state()
        capacity["leases"][lease_id] = reconciled
        goalflight_capacity.save_state(capacity)
        assert goalflight_capacity.retained_live_scope_holds_capacity(reconciled)
        rc = goalflight_capacity.main(
            ["release-stale", "--keep", "--reason", "test_past_deadline_live"]
        )
        assert rc == 0, rc
        assert goalflight_capacity.load_state()["leases"][lease_id]["state"] == "active"

        os.kill(child_pid, 9)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and goalflight_watch._pgroup_alive(worker.pid):
            time.sleep(0.02)
        assert not goalflight_watch._pgroup_alive(worker.pid), (
            "historical process group remained live after resistant child exit"
        )
        rc = goalflight_capacity.main(
            ["release-stale", "--keep", "--reason", "test_release_stale"]
        )
        assert rc == 0, rc
        recovered = goalflight_capacity.load_state()["leases"][lease_id]
        assert recovered["state"] == "expired", recovered
        assert not goalflight_capacity.retained_live_scope_holds_capacity(recovered)
    finally:
        if child_pid and goalflight_compat.pid_alive(child_pid):
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=5)


def test_indeterminate_cleanup_requires_fine_identity(tmp_path: Path) -> None:
    worker = _run_quiet_sleeper(tmp_path)
    try:
        result = goalflight_watch.terminate_indeterminate_worker(
            worker.pid,
            worker.pid,
            {"pid": worker.pid},
            term_grace_s=0.05,
            kill_grace_s=0.05,
        )
        assert result["worker_disposition"] == "indeterminate_cleanup_failed", result
        assert result["worker_termination_signals"] == [], result
        assert result["worker_termination_identity_reason"] == "identity_indeterminate", result
        assert worker.poll() is None, "missing fine identity authorized a destructive signal"
    finally:
        worker.kill()
        worker.wait(timeout=5)


def test_indeterminate_cleanup_signal_error_retains_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    lease_id = "signal-error-lease"
    capacity = goalflight_capacity.load_state()
    capacity["leases"][lease_id] = {
        "lease_id": lease_id,
        "dispatch_id": "signal-error",
        "agent": "codex",
        "worker_pid": None,
        "state": "active",
    }
    goalflight_capacity.save_state(capacity)
    worker = _run_quiet_sleeper(tmp_path)
    try:
        identity = goalflight_ledger.process_identity(worker.pid)
        assert identity and identity.get("start_token"), identity
        with monkeypatch.context() as scoped:
            def deny_signal(*_args) -> None:
                raise PermissionError("denied")

            scoped.setattr(goalflight_watch.os, "killpg", deny_signal)
            result = goalflight_watch.terminate_indeterminate_worker(
                worker.pid,
                worker.pid,
                identity,
                term_grace_s=0.05,
                kill_grace_s=0.05,
            )
        assert result["worker_disposition"] == "indeterminate_cleanup_failed", result
        assert result["worker_alive"] is True, result
        assert result["worker_termination_signals"] == [], result
        assert "PermissionError" in result["worker_termination_error"], result
        assert worker.poll() is None, "failed signal path killed the worker"
        capacity_result = goalflight_watch.release_indeterminate_capacity(
            {"lease_id": lease_id},
            worker_disposition=result,
            reason="liveness_indeterminate",
        )
        assert capacity_result["capacity_lease_disposition"] == "retained_worker_live", capacity_result
        assert goalflight_capacity.load_state()["leases"][lease_id]["state"] == "active"
    finally:
        worker.kill()
        worker.wait(timeout=5)


def test_indeterminate_cleanup_sigkill_without_group_death_retains_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    lease_id = "sigkill-unconfirmed-lease"
    capacity = goalflight_capacity.load_state()
    capacity["leases"][lease_id] = {
        "lease_id": lease_id,
        "dispatch_id": "sigkill-unconfirmed",
        "agent": "codex",
        "worker_pid": None,
        "state": "active",
    }
    goalflight_capacity.save_state(capacity)
    worker = _run_quiet_sleeper(tmp_path)
    try:
        identity = goalflight_ledger.process_identity(worker.pid)
        assert identity and identity.get("start_token"), identity
        signals: list[int] = []

        def accepted_but_still_present(_pgid: int, sig: int) -> None:
            if sig != 0:
                signals.append(sig)
            # Signal-zero probes keep reporting the real group as allocated.
            return None

        with monkeypatch.context() as scoped:
            scoped.setattr(
                goalflight_watch.os,
                "killpg",
                accepted_but_still_present,
            )
            result = goalflight_watch.terminate_indeterminate_worker(
                worker.pid,
                worker.pid,
                identity,
                term_grace_s=0.0,
                kill_grace_s=0.0,
            )

        assert signals == [signal.SIGTERM, signal.SIGKILL], signals
        assert result["worker_termination_confirmed"] is False, result
        assert result["worker_alive"] is True, result
        assert result["worker_disposition"] == "indeterminate_cleanup_unconfirmed", result
        capacity_result = goalflight_watch.release_indeterminate_capacity(
            {"lease_id": lease_id, "worker_pgid": worker.pid},
            worker_disposition=result,
            reason="liveness_indeterminate",
        )
        assert capacity_result["capacity_lease_disposition"] == "retained_worker_live"
        retained = goalflight_capacity.load_state()["leases"][lease_id]
        assert retained["state"] == "active", retained
        assert retained["reason"] == goalflight_capacity.INDETERMINATE_LIVE_REASON
    finally:
        worker.kill()
        worker.wait(timeout=5)


def _assert_watcher_survives(
    watcher: subprocess.Popen, status: Path, *, hold_s: float = 2.0
) -> dict:
    watch_deadline = time.monotonic() + 4.0
    payload: dict = {}
    while time.monotonic() < watch_deadline:
        if watcher.poll() is not None:
            break
        payload = _read_status(status)
        if payload.get("liveness_unknown_probes") or payload.get("tree_probe"):
            break
        time.sleep(0.1)
    assert watcher.poll() is None, (
        "watcher exited while probes were unavailable: "
        f"rc={watcher.returncode} status={payload} "
        f"stdout={(watcher.stdout.read() if watcher.stdout else '')} "
        f"stderr={(watcher.stderr.read() if watcher.stderr else '')}"
    )
    time.sleep(hold_s)
    assert watcher.poll() is None, (
        "watcher idle-killed a worker whose probes failed: "
        f"rc={watcher.returncode} status={_read_status(status)}"
    )
    payload = _read_status(status)
    assert payload.get("state") not in {
        "idle_timeout",
        "wedged",
        "liveness_indeterminate",
    }, payload
    return payload


def test_failed_descendant_walk_does_not_idle_kill(tmp_path: Path) -> None:
    tail = tmp_path / "worker.tail"
    tail.write_text("worker started\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    worker = _run_quiet_sleeper(tmp_path)
    watcher = None
    try:
        env = _watcher_env(tmp_path)
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        env["PATH"] = str(_descendant_ps_fail_bindir(tmp_path)) + os.pathsep + env.get(
            "PATH", ""
        )
        assert (
            goalflight_watch.live_descendant_count(
                worker.pid,
                ps_runner=lambda *a, **k: subprocess.run(*a, **k, env=env),
            )
            is None
        ), "precondition failed: descendant walk did not fail"
        watcher = subprocess.Popen(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="ps-fail",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        payload = _assert_watcher_survives(watcher, status)
        assert payload.get("live_descendants") is None, payload
        assert "descendants" in (payload.get("liveness_unknown_probes") or []), payload
        assert payload.get("liveness_state") != "wedged", payload
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


def test_failed_mtime_probe_does_not_idle_kill(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    worker_cwd = tmp_path / "worktree"
    project_root.mkdir()
    worker_cwd.mkdir()
    (worker_cwd / "keep.txt").write_text("x\n", encoding="utf-8")
    worker_cwd.chmod(0)
    # os.walk default swallows scandir errors as an empty listing.
    # With onerror=raise, that is probe failure, not an empty tree.
    tail = tmp_path / "worker.tail"
    tail.write_text("worker started\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    worker = _run_quiet_sleeper(tmp_path)
    watcher = None
    try:
        count = goalflight_watch.live_descendant_count(worker.pid)
        assert count == 0, f"precondition failed: sleeper has children count={count}"
        sample = goalflight_watch.sample_newest_mtime_under(worker_cwd)
        assert sample.available is False, (
            f"precondition failed: mtime probe did not fail on chmod 000 cwd: {sample}"
        )
        env = _watcher_env(tmp_path)
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        watcher = subprocess.Popen(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="mtime-fail",
                project_root=project_root,
                worker_cwd=worker_cwd,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        payload = _assert_watcher_survives(watcher, status)
        assert payload.get("tree_probe") == "unavailable", payload
        assert "tree_mtime" in (payload.get("liveness_unknown_probes") or []), payload
        assert payload.get("liveness_state") != "wedged", payload
    finally:
        try:
            worker_cwd.chmod(0o700)
        except OSError:
            pass
        if watcher is not None and watcher.poll() is None:
            watcher.terminate()
            try:
                watcher.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                watcher.kill()
                watcher.communicate(timeout=5)
        worker.kill()
        worker.wait(timeout=5)


def test_newest_file_stat_failure_reaches_indeterminate(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    worker_cwd = tmp_path / "worktree"
    project_root.mkdir()
    worker_cwd.mkdir()
    newest = _make_newest_unstatable_file(worker_cwd)
    sample = goalflight_watch.sample_newest_mtime_under(worker_cwd)
    assert sample.available is False, (
        f"newest real stat failure was collapsed into an available sample: {sample}"
    )
    try:
        newest.stat()
    except OSError:
        pass
    else:
        raise AssertionError("precondition failed: newest file no longer fails stat")

    tail = tmp_path / "worker.tail"
    tail.write_text("worker started\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    worker = _run_quiet_sleeper(tmp_path)
    try:
        env = _watcher_env(tmp_path)
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        proc = subprocess.run(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="newest-stat-fail",
                project_root=project_root,
                worker_cwd=worker_cwd,
                poll_secs="0.1",
                max_idle_secs="0.4",
                liveness_indeterminate_secs="1.2",
            ),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        payload = _read_status(status)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr, payload)
        assert payload.get("state") == "liveness_indeterminate", payload
        assert payload.get("tree_probe") == "unavailable", payload
        assert "tree_mtime" in (payload.get("liveness_unknown_probes") or []), payload
        assert payload.get("worker_disposition") == "terminated_on_liveness_indeterminate", payload
        assert worker.poll() is not None, "outer bound must dispose of the unresolved worker"
    finally:
        worker.kill()
        worker.wait(timeout=5)


def test_canonical_root_writer_reaches_indeterminate_not_idle_timeout(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    progress = project_root / "progress.txt"
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
            "    pending = path.with_suffix('.tmp')\n"
            "    pending.write_text(str(n))\n"
            "    pending.replace(path)\n"
            "    time.sleep(0.05)\n",
        ],
        cwd=str(project_root),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _auto_reap_worker(worker)
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not progress.exists():
            time.sleep(0.05)
        assert progress.is_file(), "precondition failed: canonical-root worker wrote nothing"
        before = int(progress.read_text(encoding="utf-8"))
        env = _watcher_env(tmp_path)
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        proc = subprocess.run(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="canonical-root-writer",
                project_root=project_root,
                worker_cwd=project_root,
                poll_secs="0.1",
                max_idle_secs="0.4",
                liveness_indeterminate_secs="1.2",
            ),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        payload = _read_status(status)
        after = int(progress.read_text(encoding="utf-8"))
        assert after > before, (before, after)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr, payload)
        assert payload.get("state") == "liveness_indeterminate", payload
        assert payload.get("state") != "idle_timeout", payload
        assert payload.get("tree_probe") == "skipped", payload
        assert "tree_mtime" in (payload.get("liveness_unknown_probes") or []), payload
        assert payload.get("worker_disposition") == "terminated_on_liveness_indeterminate", payload
        assert worker.poll() is not None, "outer bound must dispose of the unresolved writer"
    finally:
        worker.kill()
        worker.wait(timeout=5)


def test_live_descendant_reaches_universal_outer_bound(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    worker_cwd = tmp_path / "worktree"
    project_root.mkdir()
    worker_cwd.mkdir()
    child_pid_file = tmp_path / "child.pid"
    process_table_file = tmp_path / "process-table.txt"
    tail = tmp_path / "worker.tail"
    tail.write_text("worker started\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, subprocess, time\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen([\n"
            "    __import__('sys').executable, '-c',\n"
            "    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'\n"
            "])\n"
            f"Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
            f"Path({str(process_table_file)!r}).write_text(f'{{os.getpid()}} 1\\n{{child.pid}} {{os.getpid()}}\\n')\n"
            "try:\n"
            "    time.sleep(60)\n"
            "finally:\n"
            "    child.kill()\n",
        ],
        cwd=str(worker_cwd),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _auto_reap_worker(worker)
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        assert child_pid_file.is_file(), "precondition failed: child was not spawned"
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        assert goalflight_compat.pid_alive(child_pid), child_pid
        process_rows = process_table_file.read_text(encoding="utf-8").splitlines()
        assert f"{child_pid} {worker.pid}" in process_rows, process_rows
        bindir = _descendant_ps_table_bindir(tmp_path, process_table_file)
        env = _watcher_env(tmp_path)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        count = goalflight_watch.live_descendant_count(
            worker.pid,
            ps_runner=lambda *a, **k: subprocess.run(*a, **k, env=env),
        )
        assert count is not None and count >= 1, count
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        proc = subprocess.run(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="positive-descendant-bound",
                project_root=project_root,
                worker_cwd=worker_cwd,
                poll_secs="0.1",
                max_idle_secs="0.4",
                liveness_indeterminate_secs="1.2",
            ),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        payload = _read_status(status)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr, payload)
        assert payload.get("state") == "liveness_indeterminate", payload
        assert int(payload.get("live_descendants") or 0) >= 1, payload
        assert "descendants" not in (payload.get("liveness_unknown_probes") or []), payload
        assert payload.get("worker_disposition") == "terminated_on_liveness_indeterminate", payload
        assert payload.get("worker_termination_signals") == ["SIGTERM", "SIGKILL"], payload
        assert worker.poll() is not None, payload
        deadline = time.monotonic() + 2.0
        while (
            time.monotonic() < deadline
            and goalflight_compat.pid_alive(child_pid)
            and goalflight_compat.pid_is_zombie(child_pid) is not True
        ):
            time.sleep(0.05)
        assert (
            not goalflight_compat.pid_alive(child_pid)
            or goalflight_compat.pid_is_zombie(child_pid) is True
        ), child_pid
    finally:
        worker.kill()
        worker.wait(timeout=5)


def test_post_terminal_unknown_probes_release_managed_capacity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dispatch_id = "post-terminal-unknown"
    lease_id = "post-terminal-unknown-lease"
    tail = tmp_path / "worker.tail"
    tail.write_text(f"COMPLETE: {dispatch_id} — done\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    worker = _run_quiet_sleeper(tmp_path)
    watcher = None
    try:
        env = _watcher_env(tmp_path)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        monkeypatch.setenv("GOALFLIGHT_TEST_PGROUP_CPU_PCT", "0.0")
        env["PATH"] = str(_descendant_ps_fail_bindir(tmp_path)) + os.pathsep + env.get(
            "PATH", ""
        )
        assert (
            goalflight_watch.live_descendant_count(
                worker.pid,
                ps_runner=lambda *a, **k: subprocess.run(*a, **k, env=env),
            )
            is None
        ), "precondition failed: descendant walk did not fail"
        identity = _seed_managed_detached_lease(
            dispatch_id=dispatch_id,
            lease_id=lease_id,
            worker=worker,
            tail=tail,
            status=status,
            project_root=tmp_path,
        )
        watcher = subprocess.Popen(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id=dispatch_id,
                poll_secs="0.1",
                max_idle_secs="0.4",
                liveness_indeterminate_secs="2.0",
                stay_after_terminal=True,
                detached=True,
                worker_identity=identity,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        # Poison control: after ordinary idle expiry, failed descendant
        # enumeration is still UNKNOWN. The worker and its detached managed
        # lease remain live until the universal outer bound.
        deadline = time.monotonic() + 1.5
        prebound = {}
        while time.monotonic() < deadline:
            prebound = _read_status(status)
            if (
                prebound.get("state") == "running_after_terminal"
                and "descendants" in (prebound.get("liveness_unknown_probes") or [])
            ):
                break
            time.sleep(0.05)
        assert prebound.get("state") == "running_after_terminal", prebound
        assert worker.poll() is None, "unknown evidence killed the worker before the outer bound"
        assert watcher.poll() is None, "watcher exited before the universal outer bound"
        active_lease = goalflight_capacity.load_state()["leases"][lease_id]
        assert active_lease["state"] == "active", active_lease

        stdout, stderr = watcher.communicate(timeout=15)
        payload = _read_status(status)
        assert watcher.returncode == 2, (watcher.returncode, stdout, stderr, payload)
        assert payload.get("state") == "liveness_indeterminate", payload
        assert payload.get("terminal_pending_state") == "complete", payload
        assert payload.get("terminal_marker", {}).get("kind") == "COMPLETE", payload
        assert "descendants" in (payload.get("liveness_unknown_probes") or []), payload
        assert payload.get("worker_disposition") == "terminated_on_liveness_indeterminate", payload
        assert "SIGTERM" in (payload.get("worker_termination_signals") or []), payload
        assert payload.get("capacity_lease_id") == lease_id, payload
        assert payload.get("capacity_lease_disposition") == "released", payload
        assert payload.get("capacity_lease_state") == "liveness_indeterminate", payload
        assert worker.poll() is not None, "terminal bound left the detached worker live"
        released_lease = goalflight_capacity.load_state()["leases"][lease_id]
        assert released_lease["state"] == "liveness_indeterminate", released_lease
        assert released_lease.get("released_at"), released_lease
        terminal_record = json.loads(
            goalflight_ledger.record_path(dispatch_id).read_text(encoding="utf-8")
        )
        assert terminal_record["state"] == "liveness_indeterminate", terminal_record
        assert terminal_record.get("worker_still_alive") is False, terminal_record
    finally:
        if watcher is not None and watcher.poll() is None:
            watcher.kill()
            watcher.communicate(timeout=5)
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=5)


def test_indeterminate_give_up_is_not_idle_timeout(tmp_path: Path) -> None:
    tail = tmp_path / "worker.tail"
    tail.write_text("worker started\n", encoding="utf-8")
    status = tmp_path / "worker.status.json"
    worker = _run_quiet_sleeper(tmp_path)
    try:
        env = _watcher_env(tmp_path)
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        env["PATH"] = str(_descendant_ps_fail_bindir(tmp_path)) + os.pathsep + env.get(
            "PATH", ""
        )
        proc = subprocess.run(
            _watcher_cmd(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id="never-knew",
                max_idle_secs="0.4",
                liveness_indeterminate_secs="1.2",
            ),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        payload = _read_status(status)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr, payload)
        assert payload.get("state") == "liveness_indeterminate", payload
        assert payload.get("state") != "idle_timeout", payload
        assert payload.get("live_descendants") is None, payload
        assert "descendants" in (payload.get("liveness_unknown_probes") or []), payload
        reason = str(payload.get("reason") or "")
        assert reason.startswith("liveness_indeterminate"), payload
        assert "idle_timeout" not in reason, payload
    finally:
        worker.kill()
        worker.wait(timeout=5)
