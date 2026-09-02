#!/usr/bin/env python3
"""Dispatch --worktree acquires a pooled seat; exhaustion refuses; fd is inherited."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("worktree seat leases require POSIX fcntl locks")

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch  # noqa: E402
import goalflight_worktree_pool  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr or result.stdout}"
        )
    return result.stdout.strip()


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "goalflight-test@example.invalid")
    _git(repo, "config", "user.name", "Goal Flight Test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _env(tmp: Path, *, seats: int) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "dispatch")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_WAKE_LEDGER"] = str(tmp / "wake-ledger")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
    env["GOALFLIGHT_TASK_STORE"] = str(tmp / "task-store")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = os.devnull
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    env["GOALFLIGHT_WORKTREE_SEATS"] = str(seats)
    env["GOALFLIGHT_DISABLE_NUDGES"] = "1"
    env.pop("GOALFLIGHT_STEER_FILE", None)
    env.pop("GOALFLIGHT_WORKTREE_LOCK_FD", None)
    env.pop("GOALFLIGHT_OCCUPANCY_LOCK_FD", None)
    return env


def _launched_payload(stdout: str) -> dict:
    for prefix in ("DISPATCH-LAUNCHED ", "DISPATCH-START "):
        for line in stdout.splitlines():
            if line.startswith(prefix):
                return json.loads(line[len(prefix) :])
    return {}


def _dispatch_cmd(tmp: Path, repo: Path, dispatch_id: str, *worker: str) -> list[str]:
    return [
        sys.executable,
        str(DISPATCH),
        "--unregistered-forced",
        "--agent",
        "test-dispatch",
        "--dispatch-id",
        dispatch_id,
        "--launch-detached",
        "--poll-secs",
        "0.2",
        "--max-idle-secs",
        "20",
        "--tail",
        str(tmp / f"{dispatch_id}.tail"),
        "--status-json",
        str(tmp / f"{dispatch_id}.status.json"),
        "--",
        *worker,
    ]


def test_worktree_exhaustion_refuses_honestly_and_does_not_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "held-occupant")
    try:
        proc = subprocess.run(
            _dispatch_cmd(tmp_path, repo, "need-a-seat", sys.executable, "-c", "print('nope')"),
            cwd=str(repo),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 2, combined
        assert "all 1 worktree seats are held" in combined, combined
        assert "held-occupant" in combined, combined
        assert "s-1" in combined, combined
        assert "refusing to git worktree add" in combined, combined
        assert not (repo / "worktrees" / "repo" / "s-2").exists()
    finally:
        holder.release()


def test_seat_survives_for_worker_lifetime_then_frees_on_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "worker-ready"
    worker = "\n".join(
        [
            "import os, signal, sys",
            "from pathlib import Path",
            "fd = int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD'])",
            "os.fstat(fd)",
            "occ = int(os.environ['GOALFLIGHT_OCCUPANCY_LOCK_FD'])",
            "os.fstat(occ)",
            "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')",
            "signal.pause()",
        ]
    )
    proc = subprocess.run(
        _dispatch_cmd(
            tmp_path,
            repo,
            "inherit-seat",
            sys.executable,
            "-c",
            worker,
            str(marker),
        ),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    launched = _launched_payload(proc.stdout)
    assert launched.get("worktree_seat") == "s-1", launched
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    worker_pid = int(marker.read_text(encoding="utf-8"))
    try:
        try:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "blocked-while-live")
        except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
            assert "inherit-seat" in str(exc) or "s-1" in str(exc)
        else:
            raise AssertionError("live worker did not hold the kernel seat")
        os.kill(worker_pid, signal.SIGKILL)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "after-worker-death"
        )
        try:
            assert replacement.path.name == "s-1"
        finally:
            replacement.release()
    finally:
        try:
            os.kill(worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_resume_reacquires_exact_seat_and_blocks_fresh_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    parent.release()

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    resumed = goalflight_dispatch._bind_dispatch_worktree(args)
    assert resumed is not None
    try:
        assert resumed.path == seat
        assert args.cwd == str(seat)
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match="resume-child",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(repo, "fresh-dispatch")
        assert not (seat.parent / "s-2").exists()
    finally:
        resumed.release()


def test_resume_refuses_a_recorded_seat_reclaimed_by_another_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    parent.release()
    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "reclaimer")

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    try:
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match=rf"s-1=reclaimer pid={os.getpid()}",
        ):
            goalflight_dispatch._bind_dispatch_worktree(args)
    finally:
        reclaimer.release()

    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatUnavailable,
        match=r"reclaimed by reclaimer.*expected recorded holder resume-parent",
    ):
        goalflight_dispatch._bind_dispatch_worktree(args)
    assert args._worktree_seat is None


def test_dispatch_quarantines_dirty_seat_instead_of_destroying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "abandoned")
    (abandoned.path / "tracked.txt").write_text("abandoned edit\n", encoding="utf-8")
    (abandoned.path / "abandoned.txt").write_text("preserve me\n", encoding="utf-8")
    abandoned.release()

    marker = tmp_path / "second-ready"
    worker = (
        "from pathlib import Path; import os, time; "
        "os.fstat(int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD'])); "
        f"Path({str(marker)!r}).write_text('ready'); time.sleep(0.4); "
        "print('COMPLETE: pooled-reuse — ok', flush=True)"
    )
    proc = subprocess.run(
        _dispatch_cmd(
            tmp_path,
            repo,
            "reuse-dirty",
            sys.executable,
            "-c",
            worker,
        ),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    launched = _launched_payload(proc.stdout)
    assert launched.get("worktree_seat") == "s-1", launched
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    branches = _git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/goalflight/quarantine/",
    ).splitlines()
    assert len(branches) == 1, branches
    assert "s-1" in branches[0]
    assert _git(repo, "show", f"{branches[0]}:abandoned.txt") == "preserve me"


def test_cwd_without_worktree_does_not_acquire_a_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "cwd-only"
    proc = subprocess.run(
        [
            sys.executable,
            str(DISPATCH),
            "--unregistered-forced",
            "--agent",
            "test-dispatch",
            "--dispatch-id",
            "cwd-only",
            "--cwd",
            str(repo),
            "--launch-detached",
            "--tail",
            str(tmp_path / "cwd-only.tail"),
            "--status-json",
            str(tmp_path / "cwd-only.status.json"),
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ok')",
        ],
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists()
    assert not (repo / "worktrees").exists()


def test_sidecar_env_drops_closed_worktree_lock_fd() -> None:
    import goalflight_dispatch as D

    env = {
        "PATH": "/usr/bin",
        goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV: "5",
        goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV: "7",
    }
    sidecar = D._sidecar_env(env)
    assert goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV not in sidecar
    assert goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV not in sidecar
    assert env[goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV] == "5"
    assert env[goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV] == "7"
    assert sidecar["PATH"] == "/usr/bin"


def test_worktree_launch_does_not_fail_caffeinate_on_stale_lock_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "caf-ready"
    worker = (
        "from pathlib import Path; import os; "
        "os.fstat(int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD'])); "
        f"Path({str(marker)!r}).write_text('ok'); "
        "print('COMPLETE: caffeinate-sidecar — ok', flush=True)"
    )
    proc = subprocess.run(
        _dispatch_cmd(
            tmp_path,
            repo,
            "caf-sidecar",
            sys.executable,
            "-c",
            worker,
        ),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    assert "does not name an open descriptor" not in combined
    assert '"step": "caffeinate"' not in combined or "WorktreeSeatError" not in combined
    launched = _launched_payload(proc.stdout)
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        assert launched.get("caffeinate_pid"), combined


def test_two_worktree_launches_do_not_serialize_on_occupancy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pooled seats are distinct trees; occupancy must not lock the project root."""
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=2)
    release = tmp_path / "release-both"
    marker_a = tmp_path / "ready-a"
    marker_b = tmp_path / "ready-b"

    def worker(marker: Path) -> str:
        return (
            "import os, time\n"
            "from pathlib import Path\n"
            "os.fstat(int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD']))\n"
            "os.fstat(int(os.environ['GOALFLIGHT_OCCUPANCY_LOCK_FD']))\n"
            f"Path({str(marker)!r}).write_text(os.getcwd(), encoding='utf-8')\n"
            f"release = Path({str(release)!r})\n"
            "deadline = time.monotonic() + 20\n"
            "while not release.exists():\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise TimeoutError('release')\n"
            "    time.sleep(0.05)\n"
            "print('COMPLETE: wt-conc — ok', flush=True)\n"
        )

    pa = subprocess.Popen(
        _dispatch_cmd(tmp_path, repo, "wt-conc-a", sys.executable, "-c", worker(marker_a)),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pb = subprocess.Popen(
        _dispatch_cmd(tmp_path, repo, "wt-conc-b", sys.executable, "-c", worker(marker_b)),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not (marker_a.exists() and marker_b.exists()):
            time.sleep(0.05)
        assert marker_a.exists() and marker_b.exists(), (
            "pooled launches did not overlap",
            pa.poll(),
            pb.poll(),
        )
        cwd_a = marker_a.read_text(encoding="utf-8").strip()
        cwd_b = marker_b.read_text(encoding="utf-8").strip()
        assert cwd_a != cwd_b, (cwd_a, cwd_b)
        assert Path(cwd_a).name.startswith("s-")
        assert Path(cwd_b).name.startswith("s-")
        release.write_text("go", encoding="utf-8")
        out_a, err_a = pa.communicate(timeout=30)
        out_b, err_b = pb.communicate(timeout=30)
    finally:
        if pa.poll() is None:
            pa.kill()
            pa.wait(timeout=5)
        if pb.poll() is None:
            pb.kill()
            pb.wait(timeout=5)
    assert pa.returncode == 0, out_a + err_a
    assert pb.returncode == 0, out_b + err_b
    assert 64 not in {pa.returncode, pb.returncode}


def test_raw_worker_process_cwd_is_the_leased_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "raw-cwd"
    worker = (
        "from pathlib import Path; import os; "
        f"Path({str(marker)!r}).write_text(os.getcwd())"
    )
    proc = subprocess.run(
        _dispatch_cmd(
            tmp_path,
            repo,
            "raw-seat-cwd",
            sys.executable,
            "-c",
            worker,
        ),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    launched = _launched_payload(proc.stdout)
    seat = Path(launched["worktree_path"]).resolve()
    assert Path(marker.read_text(encoding="utf-8")).resolve() == seat


def _commit_in(worktree: Path, message: str, text: str = "unique work\n") -> str:
    (worktree / "tracked.txt").write_text(text, encoding="utf-8")
    _git(worktree, "add", "tracked.txt")
    _git(worktree, "commit", "-m", message)
    return _git(worktree, "rev-parse", "HEAD")


def _payloads(stdout: str) -> tuple[dict, dict]:
    started: dict = {}
    launched: dict = {}
    for line in stdout.splitlines():
        if line.startswith("DISPATCH-START "):
            started = json.loads(line[len("DISPATCH-START ") :])
        elif line.startswith("DISPATCH-LAUNCHED "):
            launched = json.loads(line[len("DISPATCH-LAUNCHED ") :])
    return started, launched


def test_acquire_checks_out_named_seat_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "named-one")
    try:
        abbrev = _git(lease.path, "rev-parse", "--abbrev-ref", "HEAD")
        assert abbrev == "seat/named-one", abbrev
        status = _git(lease.path, "status", "--branch", "--porcelain=v1")
        assert "HEAD (no branch)" not in status, status
        assert status.splitlines()[0].startswith("## seat/named-one"), status
        assert lease.branch == "seat/named-one"
    finally:
        lease.release()


def test_dispatch_payload_includes_worktree_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "branch-ready"
    worker = (
        "from pathlib import Path; import os; "
        "os.fstat(int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD'])); "
        f"Path({str(marker)!r}).write_text('ok'); "
        "print('COMPLETE: seat-branch — ok', flush=True)"
    )
    proc = subprocess.run(
        _dispatch_cmd(
            tmp_path,
            repo,
            "report-branch",
            sys.executable,
            "-c",
            worker,
        ),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    started, launched = _payloads(proc.stdout)
    assert started.get("worktree_branch") == "seat/report-branch", started
    assert launched.get("worktree_branch") == "seat/report-branch", launched
    assert launched.get("worktree_seat") == "s-1", launched
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined


def test_refuse_reset_when_detached_ahead_of_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real commit on a real detached seat must block acquire-time reset."""
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "detached-ahead")
    _git(lease.path, "checkout", "--detach")
    sha = _commit_in(lease.path, "unique detached commit")
    short = _git(lease.path, "rev-parse", "--short", "HEAD")
    lease.release()

    nxt = None
    try:
        nxt = goalflight_worktree_pool.acquire_worktree_seat(repo, "next-occupant")
    except Exception as exc:
        text = str(exc)
        assert "would lose" in text, text
        assert short in text or sha[:7] in text, text
        assert "detached HEAD" in text, text
        assert isinstance(exc, goalflight_worktree_pool.WorktreeSeatResetRefused)
    else:
        raise AssertionError(
            f"acquire reset a detached-ahead seat; unique commit {sha} "
            f"HEAD is now {_git(lease.path, 'rev-parse', 'HEAD')}"
        )
    finally:
        if nxt is not None:
            nxt.release()
    assert _git(lease.path, "rev-parse", "HEAD") == sha
    assert _git(lease.path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert (lease.path / "tracked.txt").read_text(encoding="utf-8") == "unique work\n"


def test_detached_ahead_seat_is_skipped_for_a_free_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    first = goalflight_worktree_pool.acquire_worktree_seat(repo, "keep-me")
    _git(first.path, "checkout", "--detach")
    sha = _commit_in(first.path, "do not clobber")
    first.release()

    second = goalflight_worktree_pool.acquire_worktree_seat(repo, "use-wt-2")
    try:
        assert second.path.name == "s-2"
        assert second.branch == "seat/use-wt-2"
        assert _git(first.path, "rev-parse", "HEAD") == sha
        assert _git(first.path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    finally:
        second.release()


def test_reuse_keeps_prior_named_branch_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    first = goalflight_worktree_pool.acquire_worktree_seat(repo, "worker-a")
    sha = _commit_in(first.path, "worker a finished")
    assert _git(first.path, "rev-parse", "--abbrev-ref", "HEAD") == "seat/worker-a"
    first.release()

    second = goalflight_worktree_pool.acquire_worktree_seat(repo, "worker-b")
    try:
        assert second.branch == "seat/worker-b"
        assert _git(second.path, "rev-parse", "--abbrev-ref", "HEAD") == "seat/worker-b"
        assert _git(repo, "rev-parse", "refs/heads/seat/worker-a") == sha
        assert _git(repo, "show", "seat/worker-a:tracked.txt") == "unique work"
    finally:
        second.release()


def test_refuse_reset_when_same_branch_uniquely_holds_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "same-id")
    sha = _commit_in(lease.path, "retry must not rewind this branch")
    short = _git(lease.path, "rev-parse", "--short", "HEAD")
    lease.release()

    nxt = None
    try:
        nxt = goalflight_worktree_pool.acquire_worktree_seat(repo, "same-id")
    except Exception as exc:
        text = str(exc)
        assert "would lose" in text, text
        assert short in text or sha[:7] in text, text
        assert isinstance(exc, goalflight_worktree_pool.WorktreeSeatResetRefused)
    else:
        raise AssertionError(
            f"acquire rewound unique branch seat/same-id; unique commit {sha} "
            f"HEAD is now {_git(lease.path, 'rev-parse', 'HEAD')}"
        )
    finally:
        if nxt is not None:
            nxt.release()
    assert _git(lease.path, "rev-parse", "HEAD") == sha
    assert _git(lease.path, "rev-parse", "--abbrev-ref", "HEAD") == "seat/same-id"


def test_saved_detached_commit_does_not_block_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "already-saved")
    _git(lease.path, "checkout", "--detach")
    sha = _commit_in(lease.path, "saved elsewhere")
    _git(lease.path, "branch", "rescue/already-saved")
    lease.release()

    reused = goalflight_worktree_pool.acquire_worktree_seat(repo, "after-rescue")
    try:
        assert _git(reused.path, "rev-parse", "--abbrev-ref", "HEAD") == "seat/after-rescue"
        assert _git(repo, "rev-parse", "refs/heads/rescue/already-saved") == sha
    finally:
        reused.release()


def test_claude_preset_has_no_cwd_flag_seat_is_process_cwd() -> None:
    import argparse
    import goalflight_dispatch as D

    argv, _stdin = D.build_worker(
        argparse.Namespace(
            agent="claude",
            cwd="/repo/worktrees/wt-1",
            model=None,
            parent_dispatch_id=None,
        ),
        "/tmp/prompt.md",
        [],
    )
    assert argv[:1] == ["claude"]
    assert "--cwd" not in argv
    assert "-C" not in argv


def test_default_dispatch_acquires_captive_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=2)
    marker = tmp_path / "default-cwd"
    worker = (
        "from pathlib import Path; import os; "
        f"Path({str(marker)!r}).write_text(os.getcwd())"
    )
    proc = subprocess.run(
        _dispatch_cmd(tmp_path, repo, "default-seat", sys.executable, "-c", worker),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    launched = _launched_payload(proc.stdout)
    assert launched.get("worktree_seat") == "s-1", launched
    seat = Path(launched["worktree_path"]).resolve()
    assert seat.name == "s-1"
    assert seat.parent.name == repo.name
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    assert Path(marker.read_text(encoding="utf-8")).resolve() == seat
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert str(seat) in listed
    assert "bt-" not in listed


def test_sequential_default_dispatch_reuses_one_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "4")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=4)
    paths = []
    for name in ("seq-a", "seq-b"):
        marker = tmp_path / f"{name}.cwd"
        worker = (
            "from pathlib import Path; import os; "
            f"Path({str(marker)!r}).write_text(os.getcwd()); "
            "print('COMPLETE: seq — ok', flush=True)"
        )
        proc = subprocess.run(
            _dispatch_cmd(tmp_path, repo, name, sys.executable, "-c", worker),
            cwd=str(repo),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        launched = _launched_payload(proc.stdout)
        paths.append(Path(launched["worktree_path"]).resolve())
        deadline = time.time() + 10
        while time.time() < deadline and not marker.exists():
            time.sleep(0.05)
    assert paths[0] == paths[1]
    assert paths[0].name == "s-1"
    assert not (paths[0].parent / "s-2").exists()


def test_cwd_to_cache_worktree_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=2)
    cache = repo / ".cache" / "worktrees" / "foo"
    cache.mkdir(parents=True)
    proc = subprocess.run(
        [
            sys.executable,
            str(DISPATCH),
            "--unregistered-forced",
            "--agent",
            "test-dispatch",
            "--dispatch-id",
            "cwd-refuse",
            "--cwd",
            str(cache),
            "--launch-detached",
            "--tail",
            str(tmp_path / "cwd-refuse.tail"),
            "--status-json",
            str(tmp_path / "cwd-refuse.status.json"),
            "--",
            sys.executable,
            "-c",
            "print('nope')",
        ],
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "not a seat" in combined or "refusing" in combined.lower()
    assert not (cache / ".git").exists()
    assert not (repo / "worktrees").exists()


def test_resume_injects_skip_seat_reset(tmp_path: Path) -> None:
    import argparse
    import goalflight_dispatch as D

    worktree = tmp_path / "historical-bt"
    worktree.mkdir()
    prompt = tmp_path / "resume.md"
    prompt.write_text("continue\n", encoding="utf-8")
    source = {
        "engine": "grok",
        "agent": "grok-code",
        "session_id": "sess",
        "shape": "bash",
        "record": {
            "worker_cwd": str(worktree),
            "dispatch_argv": [
                "--agent",
                "grok-code",
                "--cwd",
                str(worktree),
            ],
        },
    }
    resume_args = argparse.Namespace(
        dispatch_id="parent-resume",
        cwd=None,
        unregistered_forced=True,
        controller_label=None,
        controller_pid=None,
        controller_session_id=None,
    )
    argv = D._resume_launch_argv(
        source,
        child_dispatch_id="child-resume",
        prompt_path=prompt,
        resume_args=resume_args,
    )
    assert "--skip-seat-reset" in argv
    cwd = D._option_value_before_worker_remainder(argv, "--cwd")
    assert Path(str(cwd)).resolve() == worktree.resolve()
