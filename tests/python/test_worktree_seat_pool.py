#!/usr/bin/env python3
"""Acceptance tests for the fixed local worktree seat pool."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("worktree seat leases require POSIX fcntl locks")

import contextlib
import datetime as dt
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_worktree_pool
import goalflight_seat_cow
import goalflight_compat
import goalflight_dispatch_paths
import goalflight_ledger
import goalflight_dispatch


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def git(repo: Path, *args: str) -> str:
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


class _AdvancingDeadlineClock:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.elapsed = 0.0
        self.requested_sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.started + self.elapsed

    def sleep(self, seconds: float) -> None:
        self.requested_sleeps.append(seconds)
        self.elapsed += max(0.0, seconds)

    def __getattr__(self, name: str):
        return getattr(time, name)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "goalflight-test@example.invalid")
    git(repo, "config", "user.name", "Goal Flight Test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "base")
    return repo


def make_large_ref_repo(root: Path) -> tuple[Path, str, str, str, str]:
    repo = make_repo(root)
    base = git(repo, "rev-parse", "main")

    git(repo, "checkout", "-q", "-b", "moving-covered", "main")
    (repo / "covered.txt").write_text("covered\n", encoding="utf-8")
    git(repo, "add", "covered.txt")
    git(repo, "commit", "-qm", "covered")
    covered = git(repo, "rev-parse", "HEAD")

    git(repo, "checkout", "-q", "-b", "moving", "main")
    (repo / "moving.txt").write_text("moving\n", encoding="utf-8")
    git(repo, "add", "moving.txt")
    git(repo, "commit", "-qm", "moving")
    moving = git(repo, "rev-parse", "HEAD")

    git(repo, "checkout", "-q", "-b", "detached-source", "main")
    (repo / "detached.txt").write_text("detached\n", encoding="utf-8")
    git(repo, "add", "detached.txt")
    git(repo, "commit", "-qm", "detached")
    detached = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-D", "detached-source")

    git(repo, "pack-refs", "--all", "--prune")
    packed_refs = Path(git(repo, "rev-parse", "--git-path", "packed-refs"))
    if not packed_refs.is_absolute():
        packed_refs = repo / packed_refs
    entries = {
        "refs/heads/main": base,
        "refs/heads/moving": moving,
        "refs/heads/moving-covered": covered,
    }
    for index in range(30_000):
        entries[f"refs/fixture/{index:05d}-{'x' * 80}"] = covered
    assert sum(len(ref) + 1 for ref in entries) > os.sysconf("SC_ARG_MAX")
    packed_refs.write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        + "".join(f"{entries[ref]} {ref}\n" for ref in sorted(entries)),
        encoding="utf-8",
    )
    return repo, base, covered, moving, detached


def add_read_only_base(repo: Path, index: int) -> str:
    path = repo / f"read-only-base-{index}.txt"
    path.write_text(f"base {index}\n", encoding="utf-8")
    git(repo, "add", path.name)
    git(repo, "commit", "-m", f"read-only base {index}")
    return git(repo, "rev-parse", "HEAD")


def read_only_worktrees(repo: Path) -> list[Path]:
    root = (repo / "worktrees" / ".goalflight-readonly").resolve()
    output = git(repo, "worktree", "list", "--porcelain")
    paths = []
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree ") :].strip()).resolve()
        if path.parent == root:
            paths.append(path)
    return sorted(paths)


def record_read_only_holder(dispatch_id: str, path: Path, *, state: str = "running") -> None:
    record = {
        "dispatch_id": dispatch_id,
        "state": state,
        "worker_cwd": str(path),
        "worktree_path": str(path),
    }
    if state == "complete":
        identity = {"pid": 2147483647, "start_token": "exited-test-worker"}
        record.update(worker_pid=identity["pid"], worker_identity=identity)
    goalflight_ledger.write_record(record)


def record_finished_holder(
    dispatch_id: str,
    identity: dict | None = None,
    *,
    state: str = "complete",
    status_path: Path | None = None,
    lock_holder: str | None = None,
    project_root: Path | None = None,
) -> None:
    """Record an exited synthetic worker and its released seat identity."""
    identity = identity or {"pid": 2147483647, "start_token": "exited-test-worker"}
    assert goalflight_compat.process_identity_matches(
        identity["pid"], identity["start_token"]
    ) is False
    record = {
        "dispatch_id": dispatch_id,
        "state": state,
        "worker_pid": identity["pid"],
        "worker_identity": identity,
    }
    if status_path is not None:
        record["status_path"] = str(status_path)
    if lock_holder is not None:
        record["lock_holder"] = lock_holder
    previous = goalflight_ledger.read_record(dispatch_id) or {}
    goalflight_ledger.write_record(record)
    root_value = previous.get("project_root")
    repo = project_root or (
        Path(root_value) if isinstance(root_value, str) and root_value else Path.cwd()
    )
    raw_path = previous.get("worktree_path") or previous.get("worker_cwd")
    seat_names = [Path(raw_path).name] if isinstance(raw_path, str) and raw_path else []
    if not seat_names:
        try:
            seat_lock_root = goalflight_worktree_pool._seat_lock_root(repo)
            for lock_path in seat_lock_root.iterdir():
                if not lock_path.name.endswith(".lock"):
                    continue
                try:
                    payload = json.loads(lock_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if payload.get("dispatch_id") == dispatch_id:
                    seat_names.append(lock_path.name.removesuffix(".lock"))
        except OSError:
            pass
    for seat_name in seat_names:
        _rewrite_seat_holder_as_dead(repo, seat_name, dispatch_id)


def finish_seat_holder(lease: goalflight_worktree_pool.WorktreeSeatLease) -> None:
    lease.release()
    _rewrite_seat_holder_as_dead(
        lease.path.parent.parent, lease.path.name, lease.dispatch_id
    )
    record_finished_holder(lease.dispatch_id, project_root=lease.path.parent.parent)


def finish_seat_holder_at(
    lease: goalflight_worktree_pool.WorktreeSeatLease,
    *,
    state: str,
    ended_at: str,
) -> None:
    lease.release()
    _rewrite_seat_holder_as_dead(
        lease.path.parent.parent, lease.path.name, lease.dispatch_id
    )
    record_finished_holder(
        lease.dispatch_id, state=state, project_root=lease.path.parent.parent
    )
    record = goalflight_ledger.read_record(lease.dispatch_id)
    assert record
    record["ended_at"] = ended_at
    goalflight_ledger.write_record(record)


def test_large_reset_attribute_check_returns_blocker_under_watchdog() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        bulk = repo / "bulk"
        bulk.mkdir()
        for index in range(20_000):
            name = f"{index:05d}-{'x' * 100}.txt"
            (bulk / name).write_text("tracked\n", encoding="utf-8")
        git(repo, "add", "--", ".")
        git(repo, "commit", "-m", "many tracked paths")

        seed = goalflight_worktree_pool.acquire_worktree_seat(repo, "large-seed")
        finish_seat_holder(seed)

        real_git = shutil.which("git")
        assert real_git is not None
        fake_git_dir = Path(td) / "fake-bin"
        fake_git_dir.mkdir()
        fake_git = fake_git_dir / "git"
        fake_git.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "import sys\n"
            "import time\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'check-attr':\n"
            "    sys.stderr.buffer.write(b'x' * 1_000_000)\n"
            "    sys.stderr.flush()\n"
            "    time.sleep(60)\n"
            "os.execv(os.environ['B450_REAL_GIT'], [os.environ['B450_REAL_GIT'], *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)

        child_code = "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
                "from pathlib import Path",
                "import goalflight_worktree_pool as pool",
                "setattr(pool, 'SEAT_RESET_GIT_TIMEOUT_S', 0.5)",
                "try:",
                "    pool.acquire_worktree_seat(Path(sys.argv[1]), 'large-reset')",
                "except pool.WorktreeSeatResetRefused as exc:",
                "    print('blocked: ' + str(exc), flush=True)",
                "else:",
                "    raise AssertionError('reset unexpectedly completed')",
            ]
        )
        child_env = os.environ.copy()
        child_env["B450_REAL_GIT"] = real_git
        child_env["PATH"] = f"{fake_git_dir}{os.pathsep}{child_env['PATH']}"
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(repo)],
            cwd=str(repo),
            env=child_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = child.communicate(timeout=30)
        except subprocess.TimeoutExpired as exc:
            with contextlib.suppress(OSError):
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise AssertionError(
                "reset-safety check did not return its bounded blocker under the watchdog"
            ) from exc
        result = subprocess.CompletedProcess(
            child.args, child.returncode, stdout=stdout, stderr=stderr
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.startswith("blocked: "), result.stdout
        assert "timed out" in result.stdout, result.stdout


def test_git_proc_uses_file_for_large_input() -> None:
    payload = "path-one\0path-two\n"
    observed: dict[str, object] = {}
    real_run = goalflight_worktree_pool.subprocess.run

    def capture_run(argv, **kwargs):
        observed.update(kwargs)
        assert "input" not in kwargs
        input_file = kwargs["stdin"]
        input_file.seek(0)
        assert input_file.read() == payload.encode("utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    goalflight_worktree_pool.subprocess.run = capture_run
    try:
        result = goalflight_worktree_pool._git_proc(
            Path("."), "check-attr", "--stdin", input_text=payload
        )
    finally:
        goalflight_worktree_pool.subprocess.run = real_run

    assert result is not None and result.returncode == 0
    assert observed["stdin"] is not None


def test_reset_safety_handles_refnames_larger_than_argv_limit() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo, base, covered, moving, detached = make_large_ref_repo(Path(td))
        moving_result = goalflight_worktree_pool.check_reset_preserves_commits(
            repo,
            start=moving,
            base_commit=base,
            moving_ref="refs/heads/moving",
        )
        assert moving_result["verdict"] == goalflight_worktree_pool.NO

        covered_result = goalflight_worktree_pool.check_reset_preserves_commits(
            repo,
            start=covered,
            base_commit=base,
            moving_ref="refs/heads/moving-covered",
        )
        assert covered_result["verdict"] == goalflight_worktree_pool.YES

        git(repo, "checkout", "-q", "--detach", detached)
        detached_result = goalflight_worktree_pool.check_reset_preserves_commits(
            repo,
            start=detached,
            base_commit=base,
            moving_ref=None,
        )
        assert detached_result["verdict"] == goalflight_worktree_pool.NO


def test_pin_unique_commits_handles_refnames_larger_than_argv_limit() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo, base, _covered, moving, _detached = make_large_ref_repo(Path(td))
        git(repo, "checkout", "-q", "moving")
        result = goalflight_worktree_pool.pin_unique_commits(
            repo,
            base_commit=base,
            moving_ref="refs/heads/moving",
            worktree_id="large-ref-pin",
        )
        assert result["verdict"] == goalflight_worktree_pool.YES
        keep_ref = result["keep_ref"]
        assert keep_ref
        assert git(repo, "rev-parse", "--verify", f"{keep_ref}^{{commit}}") == moving


@contextlib.contextmanager
def seat_limit(limit: int):
    name = goalflight_worktree_pool.WORKTREE_SEATS_ENV
    prior = os.environ.get(name)
    os.environ[name] = str(limit)
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prior


def configure_pool_test_environment(monkeypatch, root: Path, config: Path) -> None:
    values = {
        "GOALFLIGHT_CAPACITY_CONF": str(config),
        "GOALFLIGHT_STATE_DIR": str(root / "state"),
        "GOALFLIGHT_DISPATCH_DIR": str(root / "state" / "dispatch"),
        "GOALFLIGHT_JOURNAL_DIR": str(root / "journal"),
        "GOALFLIGHT_WAKE_LEDGER": str(root / "wake-ledger"),
        "GOALFLIGHT_MESSAGES_DIR": str(root / "messages"),
        "GOALFLIGHT_TASK_STORE": str(root / "task-store"),
        "GOALFLIGHT_TASK_STORE_DIR": str(root / "task-store"),
        "GOALFLIGHT_PIDFILE_DIR": str(root / "pids"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(root / "pids"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def isolate_pool_test_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_pool_test_environment(monkeypatch, tmp_path, Path(os.devnull))


def make_sparse_repo(root: Path) -> tuple[Path, str, Path]:
    repo = make_repo(root)
    excluded = repo / "docs-private" / "build" / "remote-pytest"
    excluded.mkdir(parents=True)
    (excluded / "evidence.txt").write_text("heavy evidence\n", encoding="utf-8")
    (repo / "included.txt").write_text("keep me\n", encoding="utf-8")
    git(repo, "add", "--", ".")
    git(repo, "commit", "-m", "sparse fixture")
    git(repo, "config", "extensions.worktreeConfig", "true")
    pattern = root / "seat-sparse.pattern"
    pattern.write_text("/*\n!/docs-private/build/remote-pytest/\n", encoding="utf-8")
    return repo, git(repo, "rev-parse", "HEAD"), pattern


def make_sparse_quarantine_repo(root: Path) -> tuple[Path, str, Path]:
    repo, _base, pattern = make_sparse_repo(root)
    excluded = repo / "docs-private" / "build" / "remote-pytest"
    (excluded / "other.txt").write_text("untouched evidence\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    git(repo, "add", "--", ".")
    git(repo, "commit", "-m", "sparse quarantine fixture")
    return repo, git(repo, "rev-parse", "HEAD"), pattern


def write_pool_config(
    root: Path, repo: Path, pattern: Path | None = None
) -> Path:
    payload = {"worktrees_per_repo": 1}
    if pattern is not None:
        payload["seat_sparse_pattern_files"] = {str(repo): str(pattern)}
    config = root / "capacity.local.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    return config


def assert_clean_seat(seat: Path, base: str) -> None:
    assert git(seat, "rev-parse", "HEAD") == base
    assert git(seat, "status", "--porcelain") == ""


def test_configured_new_seat_uses_sparse_checkout(tmp_path: Path, monkeypatch) -> None:
    repo, base, pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-create")
    try:
        assert_clean_seat(lease.path, base)
        assert (lease.path / "included.txt").is_file()
        assert not (lease.path / "docs-private" / "build" / "remote-pytest").exists()
        assert (repo / "docs-private" / "build" / "remote-pytest").is_dir()
        assert git(repo, "config", "--get", "core.bare") == "false"
    finally:
        lease.release()


def test_sparse_seat_is_recreated_at_capacity_for_dead_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")

    previous = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-previous")
    seat = previous.path
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name)
    finish_seat_holder(previous)
    occupancy_lock = goalflight_worktree_pool.worktree_path_lock_path(seat)
    git(repo, "worktree", "remove", "--force", str(seat))
    goalflight_worktree_pool.drop_one_stale_worktree_item(repo, occupancy_lock)

    with goalflight_worktree_pool.acquire_worktree_seat(
        repo, "sparse-recreated"
    ) as recreated:
        assert recreated.path == seat
    assert seat.is_dir()
    assert json.loads(lock_path.read_text(encoding="utf-8"))["dispatch_id"] == (
        "sparse-recreated"
    )
    lock_root = goalflight_worktree_pool._seat_lock_root(repo)
    assert goalflight_worktree_pool._ring_state_path(lock_root).is_file()
    assert goalflight_worktree_pool._read_ring_hwm(lock_root) == 1

def test_cow_opt_in_uses_full_profile_without_sparse_config(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    config = write_pool_config(tmp_path, repo)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["seat_cow_templates"] = {str(repo): True}
    config.write_text(json.dumps(payload), encoding="utf-8")
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    seat = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "cow-full-new", base=base
    )
    try:
        assert_clean_seat(seat.path, base)
        assert "S " not in git(seat.path, "ls-files", "-t")
        report = goalflight_seat_cow.read_seat_cow_report(seat.path)
        assert report is not None
        assert report["population"] == "cow"
        assert report["operation"] == "new"
    finally:
        seat.release()


def test_cow_full_profile_reuses_modes_for_current_umask(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    config = write_pool_config(tmp_path, repo)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["worktrees_per_repo"] = 2
    payload["seat_cow_templates"] = {str(repo): True}
    config.write_text(json.dumps(payload), encoding="utf-8")
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    previous = os.umask(0o022)
    try:
        first = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "cow-full-mode-022", base=base
        )
        first_report = goalflight_seat_cow.read_seat_cow_report(first.path)
        assert first_report is not None and first_report["population"] == "cow"
    finally:
        os.umask(previous)

    previous = os.umask(0o077)
    try:
        second = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "cow-full-mode-077", base=base
        )
        try:
            assert_clean_seat(second.path, base)
            assert stat.S_IMODE((second.path / "tracked.txt").stat().st_mode) == 0o600
            assert stat.S_IMODE(second.path.stat().st_mode) == 0o700
            report = goalflight_seat_cow.read_seat_cow_report(second.path)
            assert report is not None and report["population"] == "cow"
            assert report["generation"] == first_report["generation"]
        finally:
            second.release()
    finally:
        first.release()
        os.umask(previous)


def test_cow_full_seat_reuse_without_worktree_config_stays_cow(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    config = write_pool_config(tmp_path, repo)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["seat_cow_templates"] = {str(repo): True}
    config.write_text(json.dumps(payload), encoding="utf-8")
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    previous = os.umask(0o022)
    try:
        first = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "cow-no-worktree-config-022", base=base
        )
        first_path = first.path
        first_report = goalflight_seat_cow.read_seat_cow_report(first.path)
        assert first_report is not None and first_report["population"] == "cow"
        finish_seat_holder(first)
    finally:
        os.umask(previous)

    previous = os.umask(0o077)
    try:
        second = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "cow-no-worktree-config-077", base=base
        )
        try:
            assert second.path == first_path
            assert_clean_seat(second.path, base)
            assert stat.S_IMODE((second.path / "tracked.txt").stat().st_mode) == 0o600
            assert stat.S_IMODE(second.path.stat().st_mode) == 0o700
            report = goalflight_seat_cow.read_seat_cow_report(second.path)
            assert report is not None
            assert report["population"] == "cow"
            assert report["operation"] == "reuse"
            assert report["generation"] == first_report["generation"]
        finally:
            finish_seat_holder(second)
    finally:
        os.umask(previous)


def test_cow_gitlink_commit_reuse_stays_cow(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    module = repo / "only-module" / "mod"
    module.mkdir(parents=True)
    gitlink_oid = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{gitlink_oid},only-module/mod")
    git(repo, "commit", "-m", "add gitlink")
    base = git(repo, "rev-parse", "HEAD")
    config = write_pool_config(tmp_path, repo)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["seat_cow_templates"] = {str(repo): True}
    config.write_text(json.dumps(payload), encoding="utf-8")
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    first = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "cow-gitlink-first", base=base
    )
    first_path = first.path
    finish_seat_holder(first)
    second = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "cow-gitlink-second", base=base
    )
    try:
        assert second.path == first_path
        report = goalflight_seat_cow.read_seat_cow_report(second.path)
        assert report is not None
        assert report["population"] == "cow"
        assert report["operation"] == "reuse"
        assert (second.path / "only-module" / "mod").is_dir()
    finally:
        finish_seat_holder(second)


def test_plain_reuse_fallback_normalizes_modes_for_current_umask(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repo(tmp_path)
    (repo / ".gitattributes").write_text("tracked.txt text eol=lf\n", encoding="utf-8")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "add checkout conversion")
    base = git(repo, "rev-parse", "HEAD")
    config = write_pool_config(tmp_path, repo)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["seat_cow_templates"] = {str(repo): True}
    config.write_text(json.dumps(payload), encoding="utf-8")
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    previous = os.umask(0o022)
    try:
        first = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "plain-fallback-022", base=base
        )
        first_path = first.path
        first_report = goalflight_seat_cow.read_seat_cow_report(first.path)
        assert first_report is not None and first_report["population"] == "plain"
        finish_seat_holder(first)
    finally:
        os.umask(previous)

    previous = os.umask(0o077)
    try:
        second = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "plain-fallback-077", base=base
        )
        try:
            assert second.path == first_path
            assert_clean_seat(second.path, base)
            assert stat.S_IMODE((second.path / "tracked.txt").stat().st_mode) == 0o600
            assert stat.S_IMODE(second.path.stat().st_mode) == 0o700
            report = goalflight_seat_cow.read_seat_cow_report(second.path)
            assert report is not None and report["population"] == "plain"
        finally:
            finish_seat_holder(second)
    finally:
        os.umask(previous)


def test_shared_repository_modes_match_plain_new_and_reused_worktrees(
    tmp_path: Path, monkeypatch
) -> None:
    shared_modes = (None, "false", "umask", "group", "true", "all", "0660", "0640")

    def configure_shared(repo: Path, shared_mode: str | None) -> None:
        if shared_mode is None:
            subprocess.run(
                ["git", "config", "--local", "--unset", "core.sharedRepository"],
                cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        else:
            git(repo, "config", "core.sharedRepository", shared_mode)

    def setup_mode_repo(root: Path, enabled: bool) -> tuple[Path, str, Path]:
        root.mkdir()
        repo = make_repo(root)
        (repo / "nested").mkdir()
        (repo / "nested" / "tracked.txt").write_text("nested\n", encoding="utf-8")
        (repo / "linked.txt").symlink_to("tracked.txt")
        git(repo, "add", "nested/tracked.txt", "linked.txt")
        git(repo, "commit", "-m", "nested and symlink")
        base = git(repo, "rev-parse", "HEAD")
        config = write_pool_config(root, repo)
        payload = json.loads(config.read_text(encoding="utf-8"))
        if enabled:
            payload["seat_cow_templates"] = {str(repo): True}
        config.write_text(json.dumps(payload), encoding="utf-8")
        configure_pool_test_environment(monkeypatch, root, config)
        return repo, base, config

    def mode_snapshot(path: Path) -> dict[str, int]:
        entries = {
            "root": Path("."),
            "file": Path("tracked.txt"),
            "directory": Path("nested"),
            "nested-file": Path("nested/tracked.txt"),
            "symlink": Path("linked.txt"),
        }
        return {name: (path / entry).lstat().st_mode for name, entry in entries.items()}

    def assert_modes_match(cow_path: Path, plain_path: Path) -> None:
        cow_modes = mode_snapshot(cow_path)
        plain_modes = mode_snapshot(plain_path)
        assert cow_modes == plain_modes, {
            name: (oct(plain_modes[name]), oct(cow_modes[name]))
            for name in plain_modes
            if plain_modes[name] != cow_modes[name]
        }

    for shared_mode in shared_modes:
        for umask in (0o022, 0o077):
            case_name = shared_mode or "unset"
            new_root = tmp_path / f"new-{case_name}-{umask:o}"
            repo, base, _config = setup_mode_repo(new_root, enabled=True)
            previous = os.umask(umask)
            try:
                configure_shared(repo, shared_mode)
                reference = new_root / "reference"
                plain = subprocess.run(
                    ["git", "worktree", "add", "--detach", str(reference), base],
                    cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                assert plain.returncode == 0, plain.stderr.decode()
                first = goalflight_worktree_pool.acquire_worktree_seat(
                    repo, f"cow-new-{case_name}-{umask:o}", base=base
                )
                try:
                    assert_modes_match(first.path, reference)
                    report = goalflight_seat_cow.read_seat_cow_report(first.path)
                    assert report is not None
                    if shared_mode in (None, "false", "umask"):
                        assert report["population"] == "cow"
                    else:
                        assert report["population"] == "plain"
                        assert report["reason"] == "shared-repository-mode-unsupported"
                finally:
                    finish_seat_holder(first)
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(reference)],
                    cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            finally:
                os.umask(previous)

            reuse_root = tmp_path / f"reuse-{case_name}-{umask:o}"
            repo, base, _config = setup_mode_repo(reuse_root, enabled=True)
            reference = reuse_root / "reference"
            previous = os.umask(umask)
            try:
                configure_shared(repo, "umask")
                plain = subprocess.run(
                    ["git", "worktree", "add", "--detach", str(reference), base],
                    cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                assert plain.returncode == 0, plain.stderr.decode()
                first = goalflight_worktree_pool.acquire_worktree_seat(
                    repo, f"cow-group-{case_name}-{umask:o}", base=base
                )
                first_path = first.path
                first_report = goalflight_seat_cow.read_seat_cow_report(first.path)
                assert first_report is not None and first_report["population"] == "cow"
                finish_seat_holder(first)
            finally:
                os.umask(previous)

            previous = os.umask(umask)
            try:
                configure_shared(repo, shared_mode)
                plain_checkout = subprocess.run(
                    [
                        "git", "-C", str(reference), "checkout",
                        "--no-overwrite-ignore", "-f", "-B",
                        f"worktree/plain-{case_name}-{umask:o}", base,
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                assert plain_checkout.returncode == 0, plain_checkout.stderr.decode()
                second = goalflight_worktree_pool.acquire_worktree_seat(
                    repo, f"cow-reuse-{case_name}-{umask:o}", base=base
                )
                try:
                    assert second.path == first_path
                    assert_modes_match(second.path, reference)
                    report = goalflight_seat_cow.read_seat_cow_report(second.path)
                    assert report is not None
                    if shared_mode in (None, "false", "umask"):
                        assert report["population"] == "cow"
                    else:
                        assert report["population"] == "cow-retained"
                        assert report["reason"] == "shared-repository-mode-unsupported"
                        assert report["generation"] == first_report["generation"]
                finally:
                    finish_seat_holder(second)
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(reference)],
                    cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            finally:
                os.umask(previous)


def test_cow_sparse_degenerate_pattern_falls_back_to_full_seat(
    tmp_path: Path, monkeypatch
) -> None:
    repo, base, pattern = make_sparse_repo(tmp_path)
    pattern.write_text("/*\n!no-such-tracked-path\n", encoding="utf-8")
    config = write_pool_config(tmp_path, repo, pattern)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["seat_cow_templates"] = {str(repo): True}
    config.write_text(json.dumps(payload), encoding="utf-8")
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    lease = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "cow-sparse-degenerate", base=base
    )
    try:
        assert_clean_seat(lease.path, base)
        assert (lease.path / "tracked.txt").is_file()
        assert (lease.path / "included.txt").is_file()
        assert (
            lease.path / "docs-private" / "build" / "remote-pytest" / "evidence.txt"
        ).is_file()
        report = goalflight_seat_cow.read_seat_cow_report(lease.path)
        assert report is not None
        assert report["population"] == "plain"
        assert report["reason"].startswith("sparse-checkout-failed:")
    finally:
        lease.release()


def test_reused_seat_reclones_current_cow_template(
    tmp_path: Path, monkeypatch
) -> None:
    repo, base, pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["seat_cow_templates"] = {str(repo): True}
    config.write_text(json.dumps(payload), encoding="utf-8")
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    first = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "cow-reuse-first", base=base
    )
    first_path = first.path
    finish_seat_holder(first)
    second = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "cow-reuse-second", base=base
    )
    try:
        assert second.path == first_path
        assert_clean_seat(second.path, base)
        report = goalflight_seat_cow.read_seat_cow_report(second.path)
        assert report is not None
        assert report["population"] == "cow"
        assert report["operation"] == "reuse"
    finally:
        finish_seat_holder(second)

def test_dirty_sparse_seat_is_quarantined_and_stays_sparse(
    tmp_path: Path, monkeypatch
) -> None:
    repo, base, pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-old")
    (abandoned.path / "included.txt").write_text("abandoned edit\n", encoding="utf-8")
    finish_seat_holder(abandoned)

    with goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-next") as reused:
        assert reused.path.name == "s-1"
        assert_clean_seat(reused.path, base)
        assert (reused.path / "included.txt").read_text(encoding="utf-8") == "keep me\n"
        assert not (reused.path / "docs-private" / "build" / "remote-pytest").exists()
        assert (
            git(reused.path, "config", "--worktree", "--bool", "--get", "core.sparseCheckout")
            == "true"
        )
        branches = git(
            repo,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/goalflight/quarantine/",
        ).splitlines()
        assert len(branches) == 1
        assert git(repo, "show", f"{branches[0]}:included.txt") == "abandoned edit"


def test_sparse_excluded_tracked_edit_is_preserved_in_quarantine(
    tmp_path: Path, monkeypatch
) -> None:
    repo, base, pattern = make_sparse_quarantine_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-hidden")
    hidden = abandoned.path / "docs-private" / "build" / "remote-pytest" / "evidence.txt"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text("hidden edit\n", encoding="utf-8")
    (abandoned.path / "included.txt").write_text("ordinary edit\n", encoding="utf-8")
    finish_seat_holder(abandoned)

    with goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-hidden-next") as reused:
        assert_clean_seat(reused.path, base)
        assert not hidden.exists()
        branches = git(
            repo,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/goalflight/quarantine/",
        ).splitlines()
        assert len(branches) == 1
        branch = branches[0]
        assert git(repo, "show", f"{branch}:docs-private/build/remote-pytest/evidence.txt") == (
            "hidden edit"
        )
        assert git(repo, "show", f"{branch}:docs-private/build/remote-pytest/other.txt") == (
            "untouched evidence"
        )
        assert git(repo, "show", f"{branch}:included.txt") == "ordinary edit"


def test_sparse_excluded_untracked_file_is_quarantined(
    tmp_path: Path, monkeypatch
) -> None:
    repo, base, pattern = make_sparse_quarantine_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-untracked")
    artifact = abandoned.path / "docs-private" / "build" / "remote-pytest" / "new-artifact.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("new artifact\n", encoding="utf-8")
    finish_seat_holder(abandoned)

    with goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-untracked-next") as reused:
        assert_clean_seat(reused.path, base)
        assert not artifact.exists()
        branches = git(
            repo,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/goalflight/quarantine/",
        ).splitlines()
        assert len(branches) == 1
        assert git(repo, "show", f"{branches[0]}:docs-private/build/remote-pytest/new-artifact.txt") == (
            "new artifact"
        )


def test_sparse_quarantine_content_mismatch_retains_seat(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _base, pattern = make_sparse_quarantine_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-mismatch")
    edited = abandoned.path / "included.txt"
    edited.write_text("before staging\n", encoding="utf-8")
    finish_seat_holder(abandoned)
    real_git = goalflight_worktree_pool._git

    def inject_mismatch(cwd: Path, *args: str, **kwargs: object) -> str:
        result = real_git(cwd, *args, **kwargs)
        if cwd.resolve() == abandoned.path.resolve() and args == (
            "add",
            "--sparse",
            "-A",
            "--",
            ".",
        ):
            edited.write_text("after staging\n", encoding="utf-8")
        return result

    monkeypatch.setattr(goalflight_worktree_pool, "_git", inject_mismatch)
    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatResetRefused,
        match="does not match worktree path",
    ):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-mismatch-next")
    assert edited.read_text(encoding="utf-8") == "after staging\n"
    assert git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/goalflight/quarantine/",
    ) == ""


def test_sparse_quarantine_recreated_deleted_path_retains_seat(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _base, pattern = make_sparse_quarantine_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-recreated")
    edited = abandoned.path / "included.txt"
    edited.unlink()
    finish_seat_holder(abandoned)
    real_git = goalflight_worktree_pool._git

    def inject_recreated_path(cwd: Path, *args: str, **kwargs: object) -> str:
        result = real_git(cwd, *args, **kwargs)
        if cwd.resolve() == abandoned.path.resolve() and args == (
            "add",
            "--sparse",
            "-A",
            "--",
            ".",
        ):
            edited.write_text("recreated edit\n", encoding="utf-8")
        return result

    monkeypatch.setattr(goalflight_worktree_pool, "_git", inject_recreated_path)
    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatResetRefused,
        match="does not match worktree path",
    ):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-recreated-next")
    assert edited.read_text(encoding="utf-8") == "recreated edit\n"
    assert git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/goalflight/quarantine/",
    ) == ""


def test_sparse_quarantine_recreated_rename_source_retains_seat(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _base, pattern = make_sparse_quarantine_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-rename")
    original = abandoned.path / "included.txt"
    renamed = abandoned.path / "renamed.txt"
    git(abandoned.path, "mv", "included.txt", "renamed.txt")
    finish_seat_holder(abandoned)
    real_git = goalflight_worktree_pool._git

    def inject_rename_source(cwd: Path, *args: str, **kwargs: object) -> str:
        result = real_git(cwd, *args, **kwargs)
        if cwd.resolve() == abandoned.path.resolve() and args == (
            "add",
            "--sparse",
            "-A",
            "--",
            ".",
        ):
            original.write_text("recreated source\n", encoding="utf-8")
        return result

    monkeypatch.setattr(goalflight_worktree_pool, "_git", inject_rename_source)
    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatResetRefused,
        match="does not match worktree path",
    ):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-rename-next")
    assert original.read_text(encoding="utf-8") == "recreated source\n"
    assert renamed.read_text(encoding="utf-8") == "keep me\n"
    assert git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/goalflight/quarantine/",
    ) == ""


def test_sparse_skip_worktree_path_present_retains_seat(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _base, pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-present")
    hidden = abandoned.path / "docs-private" / "build" / "remote-pytest" / "evidence.txt"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text("hidden edit\n", encoding="utf-8")
    (abandoned.path / "included.txt").write_text("ordinary edit\n", encoding="utf-8")
    real_git_nul = goalflight_worktree_pool._git_nul

    def preserve_present_skip_worktree(cwd: Path, *args: str, **kwargs: object) -> str:
        listing = real_git_nul(cwd, *args, **kwargs)
        if args[:3] != ("ls-files", "-v", "-z"):
            return listing
        normal = "H docs-private/build/remote-pytest/evidence.txt\0"
        assert normal in listing
        return listing.replace(normal, "S docs-private/build/remote-pytest/evidence.txt\0", 1)

    monkeypatch.setattr(goalflight_worktree_pool, "_git_nul", preserve_present_skip_worktree)
    finish_seat_holder(abandoned)

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatResetRefused, match="hidden index entry"):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-present-next")
    assert hidden.read_text(encoding="utf-8") == "hidden edit\n"


def test_sparse_lowercase_index_tag_retains_seat(tmp_path: Path, monkeypatch) -> None:
    repo, _base, pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-lowercase")
    (abandoned.path / "included.txt").write_text("hidden edit\n", encoding="utf-8")
    git(abandoned.path, "update-index", "--assume-unchanged", "included.txt")
    (abandoned.path / "tracked.txt").write_text("ordinary edit\n", encoding="utf-8")
    finish_seat_holder(abandoned)

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatResetRefused, match="hidden index entry"):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-lowercase-next")


def test_sparse_skip_worktree_without_sparse_config_retains_seat(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _base, pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-disabled")
    git(abandoned.path, "config", "--worktree", "core.sparseCheckout", "false")
    (abandoned.path / "included.txt").write_text("ordinary edit\n", encoding="utf-8")
    finish_seat_holder(abandoned)

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatResetRefused, match="hidden index entry"):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-disabled-next")


def test_unconfigured_repo_keeps_full_new_seat(tmp_path: Path, monkeypatch) -> None:
    repo, base, _pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "full-create")
    try:
        assert_clean_seat(lease.path, base)
        assert (lease.path / "docs-private" / "build" / "remote-pytest" / "evidence.txt").is_file()
    finally:
        lease.release()


@pytest.mark.parametrize(
    "precondition", ["worktree_config_false", "core_worktree", "core_bare_true"]
)
def test_sparse_precondition_falls_back_without_shared_config_change(
    tmp_path: Path, monkeypatch, precondition: str
) -> None:
    repo, base, pattern = make_sparse_repo(tmp_path)
    if precondition == "worktree_config_false":
        git(repo, "config", "extensions.worktreeConfig", "false")
    elif precondition == "core_worktree":
        git(repo, "config", "core.worktree", str(repo))
    common_dir = Path(git(repo, "rev-parse", "--git-common-dir"))
    common_config = (common_dir if common_dir.is_absolute() else repo / common_dir) / "config"
    before = common_config.read_bytes()
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)
    if precondition == "core_bare_true":
        real_git_proc = goalflight_worktree_pool._git_proc

        def report_bare_true(
            cwd: Path, *args: str, **kwargs: object
        ) -> subprocess.CompletedProcess[str] | None:
            if args == (
                "config",
                "--file",
                str(common_config),
                "--bool",
                "--get",
                "core.bare",
            ):
                return subprocess.CompletedProcess(["git", *args], 0, "true\n", "")
            return real_git_proc(cwd, *args, **kwargs)

        monkeypatch.setattr(goalflight_worktree_pool, "_git_proc", report_bare_true)

    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, f"precondition-{precondition}")
    try:
        assert_clean_seat(lease.path, base)
        assert (lease.path / "docs-private" / "build" / "remote-pytest" / "evidence.txt").is_file()
    finally:
        lease.release()
    assert common_config.read_bytes() == before


def test_sparse_setup_failure_restores_full_clean_seat(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo, base, pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)
    real_git = goalflight_worktree_pool._git

    def fail_sparse_setup(cwd: Path, *args: str, **kwargs):
        if args[:3] == ("sparse-checkout", "set", "--no-cone"):
            raise goalflight_worktree_pool.WorktreeSeatError("injected sparse failure")
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(goalflight_worktree_pool, "_git", fail_sparse_setup)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-failure")
    try:
        assert_clean_seat(lease.path, base)
        assert (lease.path / "docs-private" / "build" / "remote-pytest" / "evidence.txt").is_file()
    finally:
        lease.release()
    assert "restored full seat" in capsys.readouterr().err


@pytest.mark.parametrize(
    "pattern_text",
    [
        "",
        "/*\n!tracked.txt\n!included.txt\n!/docs-private/\n",
    ],
)
def test_sparse_degenerate_pattern_falls_back_to_full_seat(
    tmp_path: Path, monkeypatch, pattern_text: str
) -> None:
    repo, base, pattern = make_sparse_repo(tmp_path)
    pattern.write_text(pattern_text, encoding="utf-8")
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)

    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-degenerate")
    try:
        assert_clean_seat(lease.path, base)
        assert (lease.path / "tracked.txt").is_file()
        assert (lease.path / "included.txt").is_file()
        assert (lease.path / "docs-private" / "build" / "remote-pytest" / "evidence.txt").is_file()
    finally:
        lease.release()


def test_sparse_rollback_ignores_stale_cleanup_error_after_full_verification(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo, base, pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo, pattern)
    configure_pool_test_environment(monkeypatch, tmp_path, config)
    real_git = goalflight_worktree_pool._git
    real_git_proc = goalflight_worktree_pool._git_proc
    failed_sparse_checkout = False

    def fail_sparse_checkout(cwd: Path, *args: str, **kwargs: object) -> str:
        nonlocal failed_sparse_checkout
        if (
            not failed_sparse_checkout
            and args[:3] == ("-c", "submodule.recurse=false", "checkout")
        ):
            failed_sparse_checkout = True
            disabled = real_git_proc(cwd, "sparse-checkout", "disable")
            assert disabled is not None and disabled.returncode == 0
            raise goalflight_worktree_pool.WorktreeSeatError("injected checkout failure")
        return real_git(cwd, *args, **kwargs)

    def fail_sparse_cleanup(
        cwd: Path, *args: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str] | None:
        if args[:2] == ("sparse-checkout", "disable"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "disable failed")
        if args[:4] == ("config", "--worktree", "--unset", "core.sparseCheckout"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "unset failed")
        return real_git_proc(cwd, *args, **kwargs)

    monkeypatch.setattr(goalflight_worktree_pool, "_git", fail_sparse_checkout)
    monkeypatch.setattr(goalflight_worktree_pool, "_git_proc", fail_sparse_cleanup)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "sparse-rollback")
    try:
        assert_clean_seat(lease.path, base)
        assert (lease.path / "docs-private" / "build" / "remote-pytest" / "evidence.txt").is_file()
    finally:
        lease.release()
    assert "restored full seat" in capsys.readouterr().err


def pooled_worktrees(repo: Path) -> list[Path]:
    root = repo.resolve() / "worktrees"
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_dir() and goalflight_worktree_pool.is_pool_seat_path(path)
    )


def start_seat_holder(repo: Path, dispatch_id: str) -> tuple[subprocess.Popen[str], Path]:
    child_code = "\n".join(
        [
            "import signal, sys",
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
            "from pathlib import Path",
            "import goalflight_worktree_pool as pool",
            "lease = pool.acquire_worktree_seat(Path(sys.argv[1]), sys.argv[2])",
            "print(lease.path, flush=True)",
            "signal.pause()",
        ]
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code, str(repo), dispatch_id],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    assert proc.stdout is not None
    held_path_text = proc.stdout.readline().strip()
    if not held_path_text:
        assert proc.stderr is not None
        raise AssertionError(
            f"holder {dispatch_id} failed before acquire: {proc.stderr.read()}"
        )
    return proc, Path(held_path_text)


def test_hard_ceiling_is_lazy_and_reuses_seats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_COMPLETE_RETENTION_GRACE_S", "0")
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))

        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "dispatch-one")
        assert_true("first seat name", first.path.name == "s-1")
        assert_true("one lazy checkout", [p.name for p in pooled_worktrees(repo)] == ["s-1"])

        second = goalflight_worktree_pool.acquire_worktree_seat(repo, "dispatch-two")
        assert_true("second seat name", second.path.name == "s-2")
        assert_true("distinct concurrent seats", first.path != second.path)
        assert_true(
            "pool grows with concurrency",
            [p.name for p in pooled_worktrees(repo)] == ["s-1", "s-2"],
        )

        try:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "dispatch-three")
        except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
            message = str(exc)
            assert_true(
                "ceiling names first occupant",
                "s-1" in message and "dispatch-one" in message,
            )
            assert_true(
                "ceiling names second occupant",
                "s-2" in message and "dispatch-two" in message,
            )
        else:
            raise AssertionError("third concurrent dispatch exceeded a two-seat ceiling")
        assert_true(
            "no seat N+1",
            not any(path.name == "s-3" for path in pooled_worktrees(repo)),
        )

        finish_seat_holder(first)
        finish_seat_holder(second)

        # Acceptance property: task count is unbounded but checkout count is not.
        # Three times N sequential dispatches must reuse the existing range.
        for index in range(6):
            lease = goalflight_worktree_pool.acquire_worktree_seat(
                repo, f"sequential-{index}"
            )
            assert_true("sequential reuse chooses existing seat", lease.path.name == "s-1")
            finish_seat_holder(lease)
            assert_true("sequential count stays bounded", len(pooled_worktrees(repo)) <= 2)
        assert_true(
            "ceiling remains exact",
            not any(path.name == "s-3" for path in pooled_worktrees(repo)),
        )


def test_process_concurrency_gets_distinct_seats_and_names_all_occupants() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        holders: list[subprocess.Popen[str]] = []
        try:
            first, first_path = start_seat_holder(repo, "process-one")
            holders.append(first)
            second, second_path = start_seat_holder(repo, "process-two")
            holders.append(second)
            assert_true("processes receive distinct seats", first_path != second_path)
            assert_true(
                "process concurrency fills exact range",
                {first_path.name, second_path.name} == {"s-1", "s-2"},
            )
            try:
                goalflight_worktree_pool.acquire_worktree_seat(repo, "process-three")
            except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
                detail = str(exc)
                assert_true(
                    "failure names process one",
                    "s-1" in detail and "process-one" in detail,
                )
                assert_true(
                    "failure names process two",
                    "s-2" in detail and "process-two" in detail,
                )
            else:
                raise AssertionError("third process exceeded a two-seat ceiling")
            assert_true(
                "process pressure creates no s-3",
                not any(path.name == "s-3" for path in pooled_worktrees(repo)),
            )
        finally:
            for proc in holders:
                if proc.poll() is None:
                    os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)


def test_ledger_record_persists_lock_holder(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    dispatch_id = "ledger-lock-holder"
    args = SimpleNamespace(
        dispatch_id=dispatch_id,
        prompt_id=None,
        prompt_path=None,
        agent="codex",
        engine="codex",
        shape="bash",
        account="default",
        transport="dispatch",
        project_root=str(repo),
        controller_pid=None,
        controller_session_id=None,
        controller_label=None,
        claimant_pid=None,
        worker_pid=None,
        acp_session_id=None,
        logical_session_id=dispatch_id,
        lease_id=None,
        stdout_path=None,
        stderr_path=None,
        status_path=None,
        os_sandbox_json=None,
        state="complete",
        lock_holder="native",
        json=True,
    )

    with contextlib.redirect_stdout(io.StringIO()):
        assert goalflight_ledger.cmd_record(args) == 0

    assert goalflight_ledger.read_record(dispatch_id)["lock_holder"] == "native"


def test_more_concurrent_launchers_than_seats_claims_capacity_without_lock_stacking(
    tmp_path: Path,
) -> None:
    with seat_limit(2):
        repo = make_repo(tmp_path)
        # Serialize first creation of the shared admission and mutation locks;
        # this regression is about concurrent seat claims, not legacy lock
        # registry initialization.
        lock_root = goalflight_worktree_pool._seat_lock_root(repo)
        lock_root.mkdir(parents=True, exist_ok=True)
        for lock_name in ("allocation.lock", "worktree-mutation.lock"):
            lock_path = lock_root / lock_name
            fd = goalflight_worktree_pool._open_registered_lock(
                lock_path,
                goalflight_worktree_pool._lock_open_flags(),
                registry_root=goalflight_worktree_pool._git_common_dir(repo),
                allow_create=True,
                allow_unregistered=True,
            )
            os.close(fd)
        gate = tmp_path / "start-launchers"
        release = tmp_path / "release-launchers"
        workers: list[subprocess.Popen[str]] = []
        result_paths = [tmp_path / f"launcher-{index}.json" for index in range(3)]
        script = "\n".join(
            [
                "import fcntl, json, os, sys, time",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
                "import goalflight_worktree_pool as pool",
                f"gate = Path({str(gate)!r})",
                f"release = Path({str(release)!r})",
                "tracked = {}",
                "held = set()",
                "peak = 0",
                "real_open = pool._open_registered_lock",
                "real_fdopen = os.fdopen",
                "real_flock = fcntl.flock",
                "def open_registered(path, *args, **kwargs):",
                "    fd = real_open(path, *args, **kwargs)",
                "    path = Path(path)",
                "    if path.name.startswith('s-') and path.name.endswith('.lock'):",
                "        tracked[fd] = str(path)",
                "    return fd",
                "class TrackedFile:",
                "    def __init__(self, file, fd): self.file, self.fd = file, fd",
                "    def close(self):",
                "        try: self.file.close()",
                "        finally: held.discard(self.fd); tracked.pop(self.fd, None)",
                "    def __enter__(self): return self",
                "    def __exit__(self, *args): self.close()",
                "    def __getattr__(self, name): return getattr(self.file, name)",
                "def tracked_fdopen(fd, *args, **kwargs):",
                "    file = real_fdopen(fd, *args, **kwargs)",
                "    return TrackedFile(file, fd) if fd in tracked else file",
                "def tracked_flock(fd, operation):",
                "    global peak",
                "    result = real_flock(fd, operation)",
                "    if fd in tracked and operation & fcntl.LOCK_UN:",
                "        held.discard(fd)",
                "    elif fd in tracked and operation & fcntl.LOCK_EX:",
                "        held.add(fd)",
                "        peak = max(peak, len(held))",
                "    return result",
                "pool._open_registered_lock = open_registered",
                "pool.os.fdopen = tracked_fdopen",
                "pool.fcntl.flock = tracked_flock",
                "while not gate.exists(): time.sleep(0.01)",
                "try:",
                "    lease = pool.acquire_worktree_seat(Path(sys.argv[1]), sys.argv[2])",
                "    result = {'seat': lease.path.name, 'peak': peak}",
                "except pool.WorktreeSeatUnavailable as exc:",
                "    lease = None",
                "    result = {'error': str(exc), 'peak': peak}",
                "except BaseException as exc:",
                "    lease = None",
                "    result = {'exception': f'{type(exc).__name__}: {exc}', 'peak': peak}",
                "Path(sys.argv[3]).write_text(json.dumps(result), encoding='utf-8')",
                "if lease is not None:",
                "    while not release.exists(): time.sleep(0.01)",
                "    lease.release()",
            ]
        )
        try:
            for index, result_path in enumerate(result_paths):
                workers.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            script,
                            str(repo),
                            f"parallel-launcher-{index}",
                            str(result_path),
                        ],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=os.environ.copy(),
                    )
                )
            gate.touch()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not all(
                result_path.exists() for result_path in result_paths
            ):
                time.sleep(0.02)
            assert all(path.exists() for path in result_paths), "launchers did not finish admission"
            outcomes = [json.loads(path.read_text(encoding="utf-8")) for path in result_paths]
            assert all("exception" not in item for item in outcomes), outcomes
            assert sum("seat" in item for item in outcomes) == 2, outcomes
            assert {item["seat"] for item in outcomes if "seat" in item} == {"s-1", "s-2"}
            assert all(item["peak"] <= 1 for item in outcomes), outcomes
            release.touch()
            for proc in workers:
                proc.wait(timeout=30)
        finally:
            release.touch()
            for proc in workers:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)


def test_allocation_lock_is_free_during_capacity_census(tmp_path: Path) -> None:
    with seat_limit(2):
        repo = make_repo(tmp_path)
        entered = tmp_path / "census-entered"
        release = tmp_path / "release-census"
        result_path = tmp_path / "census-result.json"
        script = "\n".join(
            [
                "import json, sys, time",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
                "import goalflight_worktree_pool as pool",
                "original = pool._repo_clean_report",
                f"entered = Path({str(entered)!r})",
                f"release = Path({str(release)!r})",
                "def paused_clean_report(root):",
                "    entered.touch()",
                "    deadline = time.monotonic() + 20",
                "    while not release.exists() and time.monotonic() < deadline:",
                "        time.sleep(0.01)",
                "    return original(root)",
                "pool._repo_clean_report = paused_clean_report",
                "lease = pool.acquire_worktree_seat(Path(sys.argv[1]), sys.argv[2])",
                "Path(sys.argv[3]).write_text(json.dumps({'seat': lease.path.name}))",
                "lease.release()",
            ]
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(repo), "census-holder", str(result_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not entered.exists():
                if proc.poll() is not None:
                    break
                time.sleep(0.01)
            assert entered.exists(), "launcher did not enter the capacity census"

            allocation_path = goalflight_worktree_pool._seat_lock_root(repo) / "allocation.lock"
            with allocation_path.open("r+", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    allocation_available = False
                else:
                    allocation_available = True
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            assert allocation_available, "capacity census still holds allocation.lock"
        finally:
            release.touch()
            if proc.poll() is None:
                try:
                    stdout, stderr = proc.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate(timeout=10)
                    raise AssertionError(
                        f"launcher did not leave the capacity census: {stdout}\n{stderr}"
                    )
            else:
                stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"{stdout}\n{stderr}"
        assert json.loads(result_path.read_text(encoding="utf-8")) == {"seat": "s-1"}


def test_allocation_lock_is_free_while_capacity_is_exhausted(tmp_path: Path) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "capacity-holder")
        entered = tmp_path / "capacity-report-entered"
        release = tmp_path / "release-capacity-report"
        result_path = tmp_path / "capacity-result.json"
        script = "\n".join(
            [
                "import json, sys, time",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
                "import goalflight_worktree_pool as pool",
                "original = pool._busy_worktree_message",
                f"entered = Path({str(entered)!r})",
                f"release = Path({str(release)!r})",
                "def paused_busy_message(*args, **kwargs):",
                "    entered.touch()",
                "    deadline = time.monotonic() + 20",
                "    while not release.exists() and time.monotonic() < deadline:",
                "        time.sleep(0.01)",
                "    return original(*args, **kwargs)",
                "pool._busy_worktree_message = paused_busy_message",
                "try:",
                "    pool.acquire_worktree_seat(Path(sys.argv[1]), sys.argv[2])",
                "except pool.WorktreeSeatUnavailable as exc:",
                "    result = {'error': str(exc)}",
                "else:",
                "    result = {'error': 'unexpected admission'}",
                "Path(sys.argv[3]).write_text(json.dumps(result), encoding='utf-8')",
            ]
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(repo), "capacity-waiter", str(result_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not entered.exists():
                if proc.poll() is not None:
                    break
                time.sleep(0.01)
            assert entered.exists(), "launcher did not reach the exhausted-capacity path"

            allocation_path = goalflight_worktree_pool._seat_lock_root(repo) / "allocation.lock"
            with allocation_path.open("r+", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    allocation_available = False
                else:
                    allocation_available = True
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            assert allocation_available, "capacity exhaustion still holds allocation.lock"
        finally:
            release.touch()
            if proc.poll() is None:
                try:
                    stdout, stderr = proc.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate(timeout=10)
                    raise AssertionError(
                        f"launcher did not leave capacity reporting: {stdout}\n{stderr}"
                    )
            else:
                stdout, stderr = proc.communicate(timeout=10)
            holder.release()
        assert proc.returncode == 0, f"{stdout}\n{stderr}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert "1/1 worktrees busy" in result["error"], result


def test_mutation_lock_wait_holds_no_candidate_seat_lock(tmp_path: Path) -> None:
    with seat_limit(2):
        repo = make_repo(tmp_path)
        seed = goalflight_worktree_pool.acquire_worktree_seat(repo, "mutation-seed")
        seat_path = seed.path
        occupancy_path = goalflight_worktree_pool.worktree_path_lock_path(seat_path)
        seed.release()
        record_finished_holder("mutation-seed", project_root=repo)
        git(repo, "worktree", "remove", "--force", str(seat_path))
        goalflight_worktree_pool._drop_occupancy_registration(
            occupancy_path,
            registry_root=goalflight_worktree_pool._git_common_dir(repo),
        )
        entered = tmp_path / "mutation-wait-entered"
        result_path = tmp_path / "mutation-result.json"
        script = "\n".join(
            [
                "import json, sys",
                "from contextlib import contextmanager",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
                "import goalflight_worktree_pool as pool",
                "real_hold = pool._hold_worktree_mutation_lock",
                f"entered = Path({str(entered)!r})",
                "@contextmanager",
                "def signal_then_hold(root, **kwargs):",
                "    entered.touch()",
                "    with real_hold(root, **kwargs) as release: yield release",
                "pool._hold_worktree_mutation_lock = signal_then_hold",
                "lease = pool.acquire_worktree_seat(Path(sys.argv[1]), sys.argv[2])",
                "Path(sys.argv[3]).write_text(json.dumps({'seat': lease.path.name}))",
                "lease.release()",
            ]
        )
        proc = None
        try:
            with goalflight_worktree_pool._hold_worktree_mutation_lock(repo):
                proc = subprocess.Popen(
                    [sys.executable, "-c", script, str(repo), "mutation-waiter", str(result_path)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=os.environ.copy(),
                )
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not entered.exists():
                    if proc.poll() is not None:
                        break
                    time.sleep(0.01)
                assert entered.exists(), "launcher did not reach the mutation-lock wait"

                seat_lock = goalflight_worktree_pool._seat_lock_root(repo) / "s-1.lock"
                with seat_lock.open("r+", encoding="utf-8") as handle:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        candidate_available = False
                    else:
                        candidate_available = True
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                assert candidate_available, (
                    "launcher holds s-1.lock while waiting for mutation.lock"
                )
            assert proc is not None
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            assert proc is not None
            proc.kill()
            stdout, stderr = proc.communicate(timeout=10)
            raise AssertionError(f"launcher did not finish admission: {stdout}\n{stderr}")
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
        assert proc is not None and proc.returncode == 0, f"{stdout}\n{stderr}"
        assert json.loads(result_path.read_text(encoding="utf-8")) == {"seat": "s-2"}


def test_dirty_seat_is_quarantined_then_reset_on_acquire() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "abandoned")
        (abandoned.path / "tracked.txt").write_text("abandoned edit\n", encoding="utf-8")
        (abandoned.path / "abandoned.txt").write_text("preserve me\n", encoding="utf-8")
        abandoned.release()
        _rewrite_seat_holder_as_dead(repo, abandoned.path.name, abandoned.dispatch_id)
        dead_identity = {"pid": 2147483647, "start_token": "exited-test-worker"}
        goalflight_ledger.write_record(
            {
                "dispatch_id": abandoned.dispatch_id,
                "state": "complete",
                "terminal_state": "complete",
                "worker_pid": dead_identity["pid"],
                "worker_identity": dead_identity,
                "worker_cwd": str(abandoned.path),
                "worktree_path": str(abandoned.path),
                "lock_holder": "native",
            }
        )
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                goalflight_worktree_pool,
                "_process_cwd_snapshot",
                lambda *, timeout_s: [],
            )
            reused = goalflight_worktree_pool.acquire_worktree_seat(repo, "next")
            try:
                assert_true("same seat reused", reused.path.name == "s-1")
                assert_true(
                    "tracked file reset to base",
                    (reused.path / "tracked.txt").read_text(encoding="utf-8") == "base\n",
                )
                assert_true("untracked file cleaned", not (reused.path / "abandoned.txt").exists())
                assert_true("seat clean after acquire", git(reused.path, "status", "--porcelain") == "")
                branches = git(
                    repo,
                    "for-each-ref",
                    "--format=%(refname:short)",
                    "refs/heads/goalflight/quarantine/",
                ).splitlines()
                assert_true("one visible quarantine branch", len(branches) == 1)
                branch = branches[0]
                assert_true("branch names seat", "s-1" in branch)
                assert_true(
                    "quarantine diff non-empty",
                    bool(git(repo, "diff", "--name-only", f"main..{branch}")),
                )
                assert_true(
                    "tracked edit preserved",
                    git(repo, "show", f"{branch}:tracked.txt") == "abandoned edit",
                )
                assert_true(
                    "untracked file preserved",
                    git(repo, "show", f"{branch}:abandoned.txt") == "preserve me",
                )
            finally:
                reused.release()


def test_sigkill_releases_kernel_lease_without_cleanup() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        child_code = "\n".join(
            [
                "import os, signal, sys, time",
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
                "from pathlib import Path",
                "import goalflight_worktree_pool as pool",
                "lease = pool.acquire_worktree_seat(Path(sys.argv[1]), 'killed-worker')",
                "print(lease.path, flush=True)",
                "signal.pause()",
            ]
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code, str(repo)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            assert proc.stdout is not None
            held_path = Path(proc.stdout.readline().strip())
            assert_true("child acquired seat", held_path.name == "s-1")
            try:
                goalflight_worktree_pool.acquire_worktree_seat(repo, "blocked-worker")
            except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
                assert_true("live occupant diagnosed", "killed-worker" in str(exc))
            else:
                raise AssertionError("live child did not hold the kernel lease")

            identity = goalflight_compat.process_start_identity(proc.pid)
            assert identity is not None
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)

            record_finished_holder(
                "killed-worker", identity, state="worker_dead", project_root=repo
            )

            replacement = goalflight_worktree_pool.acquire_worktree_seat(
                repo, "replacement-worker"
            )
            try:
                assert_true("killed worker frees same seat", replacement.path == held_path)
            finally:
                replacement.release()
        finally:
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)


def test_missing_status_file_uses_dead_ledger_identity_to_reuse_seat(
    tmp_path: Path,
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "missing-status-dead"
        )
        held_path = holder.path
        holder.release()
        status_path = tmp_path / "missing-status-dead.status.json"
        record_finished_holder(
            holder.dispatch_id,
            status_path=status_path,
            lock_holder="native",
            project_root=repo,
        )

        assert not status_path.exists()
        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "after-missing-status-dead"
        )
        try:
            assert replacement.path == held_path
        finally:
            replacement.release()


def test_missing_status_file_probes_and_blocks_live_ledger_identity(
    tmp_path: Path,
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "missing-status-live"
        )
        holder.release()
        identity = goalflight_compat.process_start_identity(os.getpid())
        assert identity and identity.get("start_token")
        status_path = tmp_path / "missing-status-live.status.json"
        goalflight_ledger.write_record(
            {
                "dispatch_id": holder.dispatch_id,
                "state": "complete",
                "worker_pid": os.getpid(),
                "worker_identity": identity,
                "status_path": str(status_path),
            }
        )

        _record, live = goalflight_worktree_pool._holder_record(holder.dispatch_id)
        assert live is True
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match=rf"worker_pid={os.getpid()}",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, "must-not-reuse-live-holder"
            )


def test_malformed_status_file_terminal_unknown_identity_retains_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "malformed-status-holder"
        )
        holder.release()
        status_path = tmp_path / "malformed-status-holder.status.json"
        status_path.write_text("{malformed", encoding="utf-8")
        record_finished_holder(
            holder.dispatch_id,
            status_path=status_path,
            lock_holder="native",
            project_root=repo,
        )

        _record, live = goalflight_worktree_pool._holder_record(holder.dispatch_id)
        assert live is None
        census_calls = []

        def empty_census(**_kwargs):
            census_calls.append(True)
            return []

        monkeypatch.setattr(
            goalflight_worktree_pool, "_process_cwd_snapshot", empty_census
        )
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, "retain-malformed-status-holder"
            )
        assert census_calls == []
        assert git(holder.path, "rev-parse", "--abbrev-ref", "HEAD") == (
            f"worktree/{holder.dispatch_id}"
        )


def test_terminal_unknown_identity_is_retained_when_seat_lock_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "terminal-unknown-free-lock"
        )
        held_path = holder.path
        holder.release()
        goalflight_ledger.write_record(
            {
                "dispatch_id": holder.dispatch_id,
                "state": "complete",
                "lock_holder": "native",
            }
        )

        census_calls = []

        def empty_census(**_kwargs):
            census_calls.append(True)
            return []

        monkeypatch.setattr(
            goalflight_worktree_pool, "_process_cwd_snapshot", empty_census
        )
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, "retain-terminal-unknown-free-lock"
            )
        assert census_calls == []
        assert git(held_path, "rev-parse", "--abbrev-ref", "HEAD") == (
            f"worktree/{holder.dispatch_id}"
        )


def test_terminal_unknown_identity_is_retained_when_seat_lock_is_held(
    tmp_path: Path,
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "terminal-unknown-held-lock"
        )
        goalflight_ledger.write_record(
            {"dispatch_id": holder.dispatch_id, "state": "complete"}
        )

        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match="terminal-unknown-held-lock",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, "retain-terminal-unknown-held-lock"
            )
        holder.release()


@pytest.mark.parametrize("lock_holder", ["wrapper", None, "unrecognized"])
def test_terminal_unknown_identity_legacy_holder_retains_without_cwd_census(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_holder: str | None,
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, f"terminal-unknown-{lock_holder or 'missing'}"
        )
        held_path = holder.path
        holder.release()
        record = {"dispatch_id": holder.dispatch_id, "state": "complete"}
        if lock_holder is not None:
            record["lock_holder"] = lock_holder
        goalflight_ledger.write_record(record)

        census_calls = []

        def empty_census(**_kwargs):
            census_calls.append(True)
            return []

        monkeypatch.setattr(
            goalflight_worktree_pool, "_process_cwd_snapshot", empty_census
        )
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, "retain-legacy-unknown-lifetime-lock-holder"
            )
        assert census_calls == []
        assert git(held_path, "rev-parse", "--abbrev-ref", "HEAD") == (
            f"worktree/{holder.dispatch_id}"
        )


def test_nonterminal_unknown_identity_is_retained_with_free_seat_lock(
    tmp_path: Path,
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "nonterminal-unknown-free-lock"
        )
        held_path = holder.path
        holder.release()
        goalflight_ledger.write_record(
            {"dispatch_id": holder.dispatch_id, "state": "running"}
        )

        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match="nonterminal-unknown-free-lock",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, "retain-nonterminal-unknown-free-lock"
            )
        assert held_path.exists()


def test_running_prelaunch_failure_does_not_prove_terminal_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatch_id = "running-prelaunch-failure"
    record = {
        "dispatch_id": dispatch_id,
        "state": "running",
        "prelaunch_failure": True,
    }
    monkeypatch.setattr(
        goalflight_worktree_pool, "_path_presence", lambda _path: "absent"
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_holder_record",
        lambda *_args, **_kwargs: (record, None),
    )

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
        goalflight_worktree_pool._validate_holder(
            tmp_path / "s-1", dispatch_id, project_root=tmp_path
        )


def test_no_ledger_uses_terminal_status_and_dead_launch_identity_to_reuse_seat(
    tmp_path: Path,
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "no-ledger-status-dead"
        )
        held_path = holder.path
        holder.release()
        assert goalflight_ledger.read_record(holder.dispatch_id) is None

        status_path = goalflight_dispatch_paths.dispatch_base_dir() / (
            f"{goalflight_dispatch_paths.safe_dispatch_filename(holder.dispatch_id)}.status.json"
        )
        status_path.parent.mkdir(parents=True, exist_ok=True)
        running_identity = goalflight_compat.process_start_identity(os.getpid())
        assert running_identity and running_identity.get("start_token")
        identity = {**running_identity, "pid": 2147483647}
        status_path.write_text(
            json.dumps(
                {
                    "dispatch_id": holder.dispatch_id,
                    "state": "blocked",
                    "worker_pid": identity["pid"],
                    "expected_worker_identity": identity,
                }
            ),
            encoding="utf-8",
        )
        _rewrite_seat_holder_as_dead(repo, held_path.name, holder.dispatch_id)

        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "after-no-ledger-status-dead"
        )
        try:
            assert replacement.path == held_path
        finally:
            replacement.release()


def test_no_ledger_status_still_blocks_a_live_launch_identity(tmp_path: Path) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "no-ledger-status-live"
        )
        holder.release()
        assert goalflight_ledger.read_record(holder.dispatch_id) is None

        status_path = goalflight_dispatch_paths.dispatch_base_dir() / (
            f"{goalflight_dispatch_paths.safe_dispatch_filename(holder.dispatch_id)}.status.json"
        )
        status_path.parent.mkdir(parents=True, exist_ok=True)
        identity = goalflight_compat.process_start_identity(os.getpid())
        assert identity and identity.get("start_token")
        status_path.write_text(
            json.dumps(
                {
                    "dispatch_id": holder.dispatch_id,
                    "state": "blocked",
                    "worker_pid": os.getpid(),
                    "worker_identity": identity,
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match=rf"worker_pid={os.getpid()}",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, "must-retain-no-ledger-status-live"
            )


@pytest.mark.parametrize(
    "evidence", ("foreign", "malformed", "malformed-token", "identityless")
)
def test_no_ledger_untrusted_status_evidence_retains_seat(
    tmp_path: Path, evidence: str
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, f"no-ledger-status-{evidence}"
        )
        holder.release()
        assert goalflight_ledger.read_record(holder.dispatch_id) is None

        status_path = goalflight_dispatch_paths.dispatch_base_dir() / (
            f"{goalflight_dispatch_paths.safe_dispatch_filename(holder.dispatch_id)}.status.json"
        )
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status = {
            "dispatch_id": holder.dispatch_id,
            "state": "blocked",
        }
        if evidence == "foreign":
            status["dispatch_id"] = "foreign-dispatch"
        elif evidence == "malformed":
            status_path.write_text("{malformed", encoding="utf-8")
        elif evidence == "malformed-token":
            status.update(
                {
                    "worker_pid": os.getpid(),
                    "expected_worker_identity": {
                        "pid": os.getpid(),
                        "start_token": "bogus",
                    },
                }
            )
        if evidence != "malformed":
            status_path.write_text(json.dumps(status), encoding="utf-8")

        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match="worker identity unknown",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, f"must-retain-no-ledger-status-{evidence}"
            )


def test_no_ledger_and_no_status_retains_seat(tmp_path: Path) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "no-ledger-no-status"
        )
        holder.release()
        assert goalflight_ledger.read_record(holder.dispatch_id) is None

        status_path = goalflight_dispatch_paths.dispatch_base_dir() / (
            f"{goalflight_dispatch_paths.safe_dispatch_filename(holder.dispatch_id)}.status.json"
        )
        assert not status_path.exists()
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match=r"journal=unreadable lock_pid=live",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, "must-retain-no-ledger-no-status"
            )


def test_path_lock_sigkill_releases_without_cleanup() -> None:
    with tempfile.TemporaryDirectory() as td:
        tree = Path(td) / "tree"
        tree.mkdir()
        child_code = "\n".join(
            [
                "import os, signal, sys",
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
                "from pathlib import Path",
                "import goalflight_worktree_pool as pool",
                "lock = pool.try_acquire_worktree_path_lock(Path(sys.argv[1]), 'killed-path')",
                "print(lock.fileno(), flush=True)",
                "signal.pause()",
            ]
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code, str(tree)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            assert proc.stdout is not None
            held_fd = proc.stdout.readline().strip()
            assert_true("child acquired path lock", held_fd.isdigit())
            try:
                goalflight_worktree_pool.try_acquire_worktree_path_lock(
                    tree, "blocked-path"
                )
            except goalflight_worktree_pool.WorktreePathLockBusy as exc:
                assert_true("live occupant diagnosed", "killed-path" in str(exc))
            else:
                raise AssertionError("live child did not hold the path lock")

            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
            replacement = goalflight_worktree_pool.try_acquire_worktree_path_lock(
                tree, "replacement-path"
            )
            replacement.release()
        finally:
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)


def test_path_locks_on_different_trees_do_not_serialize() -> None:
    with tempfile.TemporaryDirectory() as td:
        tree_a = Path(td) / "a"
        tree_b = Path(td) / "b"
        tree_a.mkdir()
        tree_b.mkdir()
        lock_a = goalflight_worktree_pool.try_acquire_worktree_path_lock(tree_a, "a")
        try:
            lock_b = goalflight_worktree_pool.try_acquire_worktree_path_lock(tree_b, "b")
            try:
                assert_true("distinct lock files", lock_a.path != lock_b.path)
            finally:
                lock_b.release()
        finally:
            lock_a.release()


def test_parent_release_keeps_inherited_worker_lease_until_worker_dies() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "inherited-worker")
        lock_fd = lease.fileno()
        child_code = "\n".join(
            [
                "import os, signal, sys",
                "os.fstat(int(sys.argv[1]))",
                "print('ready', flush=True)",
                "signal.pause()",
            ]
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code, str(lock_fd)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(lock_fd,),
        )
        try:
            assert proc.stdout is not None
            assert_true("worker inherited open lock fd", proc.stdout.readline().strip() == "ready")
            lease.release()
            try:
                goalflight_worktree_pool.acquire_worktree_seat(repo, "premature-reuse")
            except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
                assert_true("inherited worker remains occupant", "inherited-worker" in str(exc))
            else:
                raise AssertionError("parent release unlocked a live inherited worker seat")

            identity = goalflight_compat.process_start_identity(proc.pid)
            assert identity is not None
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
            record_finished_holder(
                "inherited-worker", identity, state="worker_dead", project_root=repo
            )
            replacement = goalflight_worktree_pool.acquire_worktree_seat(
                repo, "after-inherited-worker-kill"
            )
            replacement.release()
        finally:
            lease.release()
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)


def test_default_seat_count_is_not_a_per_controller_cap() -> None:
    assert_true(
        "default is the documented checkout ceiling",
        goalflight_worktree_pool.DEFAULT_WORKTREE_SEATS == 15,
    )
    assert_true(
        "wt-1 basename matches the legacy worktree name pattern",
        goalflight_worktree_pool.is_pool_seat_path("/repo/worktrees/wt-1"),
    )
    assert_true(
        "s-1 basename matches the captive worktree name pattern",
        goalflight_worktree_pool.is_pool_seat_path("/repo/worktrees/ctrl/s-1"),
    )
    assert_true(
        "ad-hoc task tree is not a pool worktree name",
        not goalflight_worktree_pool.is_pool_seat_path("/repo/worktrees/t-353-live"),
    )


def test_new_repo_cap_precedes_deprecated_seat_alias() -> None:
    prior_new = os.environ.get(goalflight_worktree_pool.WORKTREES_PER_REPO_ENV)
    prior_old = os.environ.get(goalflight_worktree_pool.WORKTREE_SEATS_ENV)
    try:
        os.environ[goalflight_worktree_pool.WORKTREE_SEATS_ENV] = "3"
        os.environ[goalflight_worktree_pool.WORKTREES_PER_REPO_ENV] = "5"
        assert goalflight_worktree_pool.configured_worktree_seats() == 5
        os.environ.pop(goalflight_worktree_pool.WORKTREES_PER_REPO_ENV)
        assert goalflight_worktree_pool.configured_worktree_seats() == 3
    finally:
        if prior_new is None:
            os.environ.pop(goalflight_worktree_pool.WORKTREES_PER_REPO_ENV, None)
        else:
            os.environ[goalflight_worktree_pool.WORKTREES_PER_REPO_ENV] = prior_new
        if prior_old is None:
            os.environ.pop(goalflight_worktree_pool.WORKTREE_SEATS_ENV, None)
        else:
            os.environ[goalflight_worktree_pool.WORKTREE_SEATS_ENV] = prior_old


def test_branch_reader_accepts_new_and_legacy_prefixes() -> None:
    assert goalflight_worktree_pool.is_worktree_branch("worktree/example")
    assert goalflight_worktree_pool.is_worktree_branch("seat/example")
    assert not goalflight_worktree_pool.is_worktree_branch("feature/example")


def test_read_only_worktree_is_shared_by_commit() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        nested_fixture = repo / "src" / "worktrees" / "fixture.txt"
        nested_fixture.parent.mkdir(parents=True)
        nested_fixture.write_text("visible\n", encoding="utf-8")
        first_path, first_base = goalflight_worktree_pool.shared_read_only_worktree(repo)
        second_path, second_base = goalflight_worktree_pool.shared_read_only_worktree(repo)
        assert_true("same read-only path", first_path == second_path)
        assert_true("same read-only base", first_base == second_base)
        assert_true("read-only checkout is detached", git(first_path, "branch", "--show-current") == "")
        assert_true("read-only checkout is not an exclusive pool slot", not (repo / "worktrees" / "s-1").exists())
        exclude_path = Path(git(repo, "rev-parse", "--git-path", "info/exclude"))
        if not exclude_path.is_absolute():
            exclude_path = repo / exclude_path
        assert_true(
            "managed worktree root excluded once",
            exclude_path.read_text(encoding="utf-8").splitlines().count("/worktrees/") == 1,
        )
        assert_true(
            "nested project worktrees remain visible",
            "?? src/worktrees/fixture.txt" in git(
                repo, "status", "--porcelain", "--untracked-files=all",
            ),
        )


@pytest.mark.parametrize("symlink_kind", ["exclude", "parent"])
def test_managed_worktree_exclude_skips_symlinks(
    tmp_path: Path, capsys, symlink_kind: str,
) -> None:
    repo = make_repo(tmp_path)
    tracked = repo / ".gitignore"
    tracked.write_text("tracked content\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "track ignore file")
    tracked_before = tracked.read_bytes()
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = repo / exclude
    if symlink_kind == "exclude":
        exclude.unlink()
        exclude.symlink_to(tracked)
    else:
        outside = tmp_path / "outside-git-dir"
        outside.mkdir()
        info_backup = tmp_path / "original-info"
        exclude.parent.rename(info_backup)
        exclude.parent.symlink_to(outside, target_is_directory=True)

    goalflight_worktree_pool._exclude_managed_worktrees(repo)

    assert "skipping unsafe Git exclude" in capsys.readouterr().err
    assert tracked.read_bytes() == tracked_before
    if symlink_kind == "exclude":
        assert exclude.is_symlink()
    else:
        assert not (outside / "exclude").exists()


@pytest.mark.parametrize("unsafe_kind", ["hardlink", "fifo"])
def test_managed_worktree_exclude_skips_linked_or_nonregular_files(
    tmp_path: Path, capsys, unsafe_kind: str,
) -> None:
    repo = make_repo(tmp_path)
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = repo / exclude

    if unsafe_kind == "hardlink":
        tracked = repo / ".gitignore"
        tracked.write_text("tracked content\n", encoding="utf-8")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-m", "track ignore file")
        tracked_before = tracked.read_bytes()
        exclude.unlink()
        os.link(tracked, exclude)
    else:
        exclude.unlink()
        os.mkfifo(exclude)

    goalflight_worktree_pool._exclude_managed_worktrees(repo)

    assert "skipping unsafe Git exclude" in capsys.readouterr().err
    if unsafe_kind == "hardlink":
        assert tracked.read_bytes() == tracked_before
    else:
        assert stat.S_ISFIFO(exclude.stat().st_mode)


def test_read_only_path_probe_oserror_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    read_only_root = goalflight_worktree_pool.read_only_worktree_root(repo)
    managed_root = goalflight_worktree_pool.repository_worktree_root(repo)
    real_resolve = Path.resolve

    def denied(path: Path, *args, **kwargs):
        if path in {read_only_root, managed_root}:
            raise OSError("permission denied")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", denied)
    read_only_verdict, _ = goalflight_worktree_pool.read_only_worktree_path_verdict(
        read_only_root / "candidate", project_root=repo
    )
    managed_verdict, _ = goalflight_worktree_pool.managed_worktree_path_verdict(
        managed_root / "s-1", project_root=repo
    )
    assert read_only_verdict == goalflight_worktree_pool.UNKNOWN
    assert managed_verdict == goalflight_worktree_pool.UNKNOWN


def test_read_only_allocation_lock_wait_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    lock_path = goalflight_worktree_pool.read_only_allocation_lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        # Allocation waits through its positive budget, then reports timeout.
        clock = _AdvancingDeadlineClock()
        monkeypatch.setattr(goalflight_worktree_pool, "time", clock)
        lock_attempts: list[int] = []
        original_flock = fcntl.flock

        def count_nonblocking_flock(fd: int, operation: int) -> None:
            if operation & fcntl.LOCK_NB:
                lock_attempts.append(operation)
            original_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", count_nonblocking_flock)
        with pytest.raises(
            goalflight_worktree_pool.WorktreeReadOnlyLockTimeout,
            match=r"read-only allocation lock wait expired after 0\.05s",
        ) as exc_info:
            with goalflight_worktree_pool._read_only_allocation_lock(
                repo, timeout_s=0.05
            ):
                pass
        assert "read-only allocation lock wait expired after 0.05s" in str(
            exc_info.value
        )
        assert clock.elapsed == pytest.approx(0.05)
        assert sum(clock.requested_sleeps) == pytest.approx(0.05)
        assert sum(clock.requested_sleeps) <= 0.05
        assert len(lock_attempts) == 2


def test_read_only_reaper_honors_transaction_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    import goalflight_worktree_gc

    monkeypatch.setattr(goalflight_worktree_pool, "READ_ONLY_WORKTREE_KEEP", 0)
    root = goalflight_worktree_pool.read_only_worktree_root(repo)
    root.mkdir(parents=True)
    paths = [root / f"old-{index}" for index in range(5)]
    for path in paths:
        path.mkdir()
        os.utime(path, (1, 1))
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_read_only_registered_worktrees",
        lambda _repo, **_kwargs: paths,
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "read_only_worktree_usage",
        lambda _path, **_kwargs: {"verdict": goalflight_worktree_pool.YES},
    )
    monkeypatch.setattr(
        goalflight_worktree_gc,
        "check_clean",
        lambda *_args, **_kwargs: {"verdict": goalflight_worktree_pool.YES},
    )
    clock = [0.0]
    monkeypatch.setattr(goalflight_worktree_pool.time, "monotonic", lambda: clock[0])
    pinned: list[Path] = []

    def pin(_repo: Path, path: Path, **_kwargs):
        pinned.append(path)
        clock[0] += 1.0
        return "refs/goalflight/keep/test", None

    monkeypatch.setattr(goalflight_worktree_pool, "pin_worktree_head_before_remove", pin)
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_git",
        lambda *_args, **_kwargs: "",
    )
    goalflight_worktree_pool._reap_read_only_worktrees(
        repo, root=root, requested_path=None, deadline=2.0
    )
    assert pinned == paths


def test_read_only_reaper_retries_expired_scan_and_enforces_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    import goalflight_worktree_gc

    monkeypatch.setattr(goalflight_worktree_pool, "READ_ONLY_WORKTREE_KEEP", 4)
    root = goalflight_worktree_pool.read_only_worktree_root(repo)
    root.mkdir(parents=True)
    paths = [root / f"old-{index}" for index in range(6)]
    for index, path in enumerate(paths):
        path.mkdir()
        os.utime(path, (index + 1, index + 1))
    candidates = sorted(
        ((index + 1, path) for index, path in enumerate(paths)), reverse=True
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_read_only_registered_worktrees",
        lambda _repo, **_kwargs: paths,
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "read_only_worktree_usage",
        lambda _path, **_kwargs: {"verdict": goalflight_worktree_pool.YES},
    )
    monkeypatch.setattr(
        goalflight_worktree_gc,
        "check_clean",
        lambda *_args, **_kwargs: {"verdict": goalflight_worktree_pool.YES},
    )
    scan_deadlines: list[float] = []

    def scan(*_args, deadline: float, **_kwargs):
        scan_deadlines.append(deadline)
        if len(scan_deadlines) == 1:
            return [], ["read-only candidate scan deadline expired"], True
        return candidates, [], False

    monkeypatch.setattr(goalflight_worktree_pool, "_read_only_reap_candidates", scan)
    clock = [0.0]
    monkeypatch.setattr(goalflight_worktree_pool.time, "monotonic", lambda: clock[0])
    pinned: list[Path] = []

    def pin(_repo: Path, path: Path, **_kwargs):
        pinned.append(path)
        return "refs/goalflight/keep/test", None

    monkeypatch.setattr(goalflight_worktree_pool, "pin_worktree_head_before_remove", pin)
    monkeypatch.setattr(goalflight_worktree_pool, "_git", lambda *_args, **_kwargs: "")
    goalflight_worktree_pool._reap_read_only_worktrees(
        repo, root=root, requested_path=None, deadline=2.0
    )

    assert len(scan_deadlines) == 2
    assert scan_deadlines[1] > scan_deadlines[0]
    assert pinned == [path for _mtime, path in candidates[4:]]


def test_read_only_reaper_indexes_ledger_and_caches_foreign_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    foreign_parent = tmp_path / "foreign-parent"
    foreign_parent.mkdir()
    foreign = make_repo(foreign_parent)
    root = goalflight_worktree_pool.read_only_worktree_root(repo)
    root.mkdir(parents=True)
    paths = [root / f"old-{index}" for index in range(100)]
    for path in paths:
        path.mkdir()
        os.utime(path, (1, 1))
    for index in range(20):
        goalflight_ledger.write_record(
            {
                "dispatch_id": f"foreign-row-{index}",
                "state": "waiting_capacity",
                "project_root": str(foreign),
            }
        )

    monkeypatch.setattr(goalflight_worktree_pool, "READ_ONLY_WORKTREE_KEEP", 0)
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_read_only_registered_worktrees",
        lambda _repo, **_kwargs: paths,
    )

    import goalflight_worktree_gc

    original_read = goalflight_worktree_gc.read_ledger_records
    ledger_reads = {"count": 0}

    def count_ledger_reads(ledger_dir: Path, **_kwargs):
        ledger_reads["count"] += 1
        return original_read(ledger_dir)

    monkeypatch.setattr(
        goalflight_worktree_gc, "read_ledger_records", count_ledger_reads
    )
    original_common_dir = goalflight_worktree_pool._git_common_dir
    identity_calls: list[str] = []

    def count_identity_calls(
        path: Path, *, timeout: float | None = None, deadline: float | None = None
    ) -> Path:
        identity_calls.append(os.path.realpath(str(path)))
        return original_common_dir(path, timeout=timeout, deadline=deadline)

    monkeypatch.setattr(
        goalflight_worktree_pool, "_git_common_dir", count_identity_calls
    )
    removed: list[Path] = []
    original_git = goalflight_worktree_pool._git

    def remove_fake_checkout(
        cwd: Path, *args: str, **kwargs: object
    ) -> str:
        if args[:2] == ("worktree", "remove"):
            removed.append(Path(args[2]))
            return ""
        return original_git(cwd, *args, **kwargs)

    monkeypatch.setattr(goalflight_worktree_pool, "_git", remove_fake_checkout)
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "pin_worktree_head_before_remove",
        lambda *_args, **_kwargs: ("refs/goalflight/keep/test", None),
    )

    goalflight_worktree_pool._reap_read_only_worktrees(
        repo,
        root=root,
        requested_path=None,
        deadline=time.monotonic() + 20.0,
        process_cwd_census=lambda: [],
        fresh_process_cwd_census=lambda: [],
    )

    assert removed == paths
    assert ledger_reads["count"] == 1
    assert len(identity_calls) <= 2


def test_read_only_reaper_bails_when_ledger_lock_deadline_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    root = goalflight_worktree_pool.read_only_worktree_root(repo)
    root.mkdir(parents=True)
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_read_only_registered_worktrees",
        lambda _repo, **_kwargs: scanned.append(True) or [],
    )
    scanned: list[bool] = []
    clock = _AdvancingDeadlineClock()
    monkeypatch.setattr(goalflight_worktree_pool, "time", clock)
    monkeypatch.setattr(goalflight_ledger, "time", clock)
    lock_attempts: list[int] = []
    original_flock = goalflight_ledger.fcntl.flock

    def count_nonblocking_flock(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_NB:
            lock_attempts.append(operation)
        original_flock(fd, operation)

    monkeypatch.setattr(goalflight_ledger.fcntl, "flock", count_nonblocking_flock)
    started = clock.monotonic()
    with goalflight_ledger.StateLock():
        # The reaper waits through the transaction deadline, then retains.
        goalflight_worktree_pool._reap_read_only_worktrees(
            repo,
            root=root,
            requested_path=None,
            deadline=started + 0.05,
        )
    assert scanned == []
    assert clock.elapsed == pytest.approx(0.05)
    assert sum(clock.requested_sleeps) == pytest.approx(0.05)
    assert sum(clock.requested_sleeps) <= 0.05
    assert len(lock_attempts) == 6


def test_read_only_review_does_not_adopt_legacy_ring_seat(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "legacy-writer", controller_label="legacy"
    )
    seat = writer.path
    writer.release()
    global_lock = goalflight_worktree_pool.worktree_lock_path_for_path(repo, seat)
    legacy_path = repo / "worktrees" / "legacy" / "s-1"
    legacy_path.parent.mkdir(parents=True)
    git(repo, "worktree", "move", str(seat), str(legacy_path))
    legacy_lock = (
        goalflight_worktree_pool._seat_lock_root(repo, controller_label="legacy")
        / "s-1.lock"
    )
    legacy_lock.parent.mkdir(parents=True, exist_ok=True)
    global_lock.replace(legacy_lock)
    registry_path = goalflight_worktree_pool._lock_registry_path(
        goalflight_worktree_pool._git_common_dir(repo)
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["locks"].pop(
        goalflight_worktree_pool._lock_registry_key(global_lock), None
    )
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    path, selected, hold = goalflight_worktree_pool.bind_read_only_worktree(
        repo, "legacy-review", base=base
    )
    assert path != legacy_path
    assert path.parent.name == goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
    assert selected == base
    assert hold is None
    locks = json.loads(registry_path.read_text(encoding="utf-8"))["locks"]
    assert goalflight_worktree_pool._lock_registry_key(legacy_lock) not in locks
    assert read_only_worktrees(repo) == [path]


def test_read_only_review_reuses_clean_pooled_seat_without_checkout() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        base = add_read_only_base(repo, 0)
        writer = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "writer-finished", base=base
        )
        seat = writer.path
        finish_seat_holder(writer)
        record = goalflight_ledger.read_record("writer-finished")
        assert record
        record.update(
            {
                "project_root": str(repo),
                "worker_cwd": str(seat),
                "worktree_path": str(seat),
                "worktree_branch": git(seat, "rev-parse", "--abbrev-ref", "HEAD"),
                "worktree_head": git(seat, "rev-parse", "HEAD"),
            }
        )
        goalflight_ledger.write_record(record)

        path, selected, hold = goalflight_worktree_pool.bind_read_only_worktree(
            repo, "review-seat", base=base
        )
        try:
            assert path != seat
            assert path.parent.name == goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
            assert selected == base
            assert hold is None
            assert read_only_worktrees(repo) == [path]
            resume = goalflight_worktree_pool.acquire_worktree_seat(
                repo,
                "writer-resume",
                occupy_path=seat,
                reset=False,
                expected_prior_dispatch_id="writer-finished",
            )
            resume.release()
            assert git(seat, "rev-parse", "HEAD") == base
        finally:
            if hold is not None:
                hold.release()


@pytest.mark.parametrize("mutation", ["dirty", "head-mismatch"])
def test_read_only_review_falls_back_from_dirty_or_mismatched_seat(mutation: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        requested = add_read_only_base(repo, 0)
        seat_base = requested if mutation == "dirty" else add_read_only_base(repo, 1)
        writer = goalflight_worktree_pool.acquire_worktree_seat(
            repo, f"writer-{mutation}", base=seat_base
        )
        seat = writer.path
        if mutation == "dirty":
            (seat / "untracked-review-file").write_text("dirty\n", encoding="utf-8")
        finish_seat_holder(writer)

        path, selected, hold = goalflight_worktree_pool.bind_read_only_worktree(
            repo, f"review-{mutation}", base=requested
        )
        assert path != seat
        assert path.parent.name == goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
        assert selected == requested
        assert hold is None


def test_dirty_submodule_seat_falls_back_instead_of_being_shared(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    subrepo = tmp_path / "subrepo"
    git(tmp_path, "init", str(subrepo))
    git(subrepo, "config", "user.email", "goalflight-test@example.invalid")
    git(subrepo, "config", "user.name", "Goal Flight Test")
    (subrepo / "tracked.txt").write_text("submodule\n", encoding="utf-8")
    git(subrepo, "add", "tracked.txt")
    git(subrepo, "commit", "-m", "submodule base")
    git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(subrepo),
        "modules/sub",
    )
    git(repo, "commit", "-m", "add submodule")
    base = git(repo, "rev-parse", "HEAD")
    writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "writer-dirty-submodule", base=base
    )
    seat = writer.path
    git(
        seat,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
    )
    finish_seat_holder(writer)
    (seat / "modules" / "sub" / "local.txt").write_text("dirty\n", encoding="utf-8")

    path, _selected, hold = goalflight_worktree_pool.bind_read_only_worktree(
        repo, "review-dirty-submodule", base=base
    )
    try:
        assert path != seat
        assert hold is None
    finally:
        if hold is not None:
            hold.release()


def test_dirty_shared_read_only_fallback_fails_without_reset() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        base = add_read_only_base(repo, 0)
        path, _ = goalflight_worktree_pool.shared_read_only_worktree(repo, base=base)
        dirty_file = path / "untracked-review-file"
        dirty_file.write_text("keep\n", encoding="utf-8")

        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatError,
            match="shared read-only worktree .* is not clean",
        ):
            goalflight_worktree_pool.shared_read_only_worktree(repo, base=base)

        assert dirty_file.read_text(encoding="utf-8") == "keep\n"
        assert path.is_dir()


def test_shared_hold_recheck_uses_bounded_git_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        base = add_read_only_base(repo, 0)
        writer = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "writer-timeout", base=base
        )
        seat = writer.path
        finish_seat_holder(writer)
        real_git_proc = goalflight_worktree_pool._git_proc
        status_timeouts: list[float | None] = []

        def timed_status(cwd: Path, *args: str, **kwargs):
            if cwd.resolve() == seat.resolve() and args and args[0] == "status":
                status_timeouts.append(kwargs.get("timeout"))
                return subprocess.CompletedProcess(
                    ["git", *args], 124, "", "git command timed out"
                )
            return real_git_proc(cwd, *args, **kwargs)

        monkeypatch.setattr(goalflight_worktree_pool, "_git_proc", timed_status)
        hold = goalflight_worktree_pool._try_acquire_shared_read_only_seat(
            repo, seat, base, "review-timeout"
        )

        assert hold is None
        assert status_timeouts == [goalflight_worktree_pool.READ_ONLY_GIT_TIMEOUT_S]


def test_shared_hold_rejects_parent_lock_symlink(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink race test requires POSIX links")
    repo = make_repo(tmp_path)
    base = add_read_only_base(repo, 0)
    writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "writer-parent-lock-race", base=base
    )
    seat = writer.path
    finish_seat_holder(writer)
    lock_path = goalflight_worktree_pool.worktree_lock_path_for_path(repo, seat)
    parent = lock_path.parent
    backup = parent.with_name(parent.name + ".real")
    replacement = tmp_path / "unrelated-locks"
    replacement.mkdir()
    (replacement / lock_path.name).touch()
    parent.rename(backup)
    parent.symlink_to(replacement, target_is_directory=True)
    try:
        assert goalflight_worktree_pool._try_acquire_shared_read_only_seat(
            repo, seat, base, "review-parent-lock-race"
        ) is None
    finally:
        parent.unlink()
        backup.rename(parent)


def test_shared_hold_rejects_replaced_lock_parent_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("lock identity race test requires POSIX file replacement")
    repo = make_repo(tmp_path)
    base = add_read_only_base(repo, 0)
    writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "writer-parent-identity-race", base=base
    )
    seat = writer.path
    finish_seat_holder(writer)
    original = goalflight_worktree_pool._registered_pool_seat_lock_info
    swapped: dict[str, Path] = {}

    def race(path: str | Path, *, project_root: Path):
        info = original(path, project_root=project_root)
        if info[0] == goalflight_worktree_pool.YES:
            lock_path = info[2]
            assert lock_path is not None
            parent = lock_path.parent
            backup = parent.with_name(parent.name + ".real")
            parent.rename(backup)
            parent.mkdir()
            (parent / lock_path.name).touch()
            swapped.update(parent=parent, backup=backup)
        return info

    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_registered_pool_seat_lock_info",
        race,
    )
    try:
        hold = goalflight_worktree_pool._try_acquire_shared_read_only_seat(
            repo, seat, base, "review-parent-identity-race"
        )
        if hold is not None:
            hold.release()
        assert hold is None
    finally:
        swapped["parent"].joinpath(seat.name + ".lock").unlink()
        swapped["parent"].rmdir()
        swapped["backup"].rename(swapped["parent"])


def test_two_read_only_reviews_share_detached_checkout_without_holding_writer_seat() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        root = Path(td)
        repo = make_repo(root)
        base = add_read_only_base(repo, 0)
        writer = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "writer-shared", base=base
        )
        finish_seat_holder(writer)
        first_path, _selected, first_hold = goalflight_worktree_pool.bind_read_only_worktree(
            repo, "review-one", base=base
        )
        second_path, _selected, second_hold = goalflight_worktree_pool.bind_read_only_worktree(
            repo, "review-two", base=base
        )
        assert first_path == second_path
        assert first_path.parent.name == goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
        assert first_path != writer.path
        assert first_hold is None and second_hold is None
        try:
            assert git(writer.path, "rev-parse", "HEAD") == base
        finally:
            if first_hold is not None:
                first_hold.release()
            if second_hold is not None:
                second_hold.release()

        crash_code = (
            "import os, sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "from pathlib import Path; "
            "import goalflight_worktree_pool as p; "
            "h = p.try_acquire_read_only_pool_seat(Path(sys.argv[2]), Path(sys.argv[3]), 'review-crash', base_commit=sys.argv[4]); "
            "assert h is not None; os.kill(os.getpid(), 9)"
        )
        env = os.environ.copy()
        env["GOALFLIGHT_CAPACITY_CONF"] = os.devnull
        env["GOALFLIGHT_WORKTREE_SEATS"] = "1"
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                crash_code,
                str(ROOT / "scripts"),
                str(repo),
                str(writer.path),
                base,
            ],
            env=env,
        )
        assert child.wait(timeout=10) == -signal.SIGKILL
        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "writer-after-crash"
        )
        replacement.release()


def test_read_only_finished_checkouts_are_reaped_on_next_allocation() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        paths = []
        for index in range(7):
            path, _base = goalflight_worktree_pool.shared_read_only_worktree(
                repo, base=add_read_only_base(repo, index)
            )
            paths.append(path)
            record_read_only_holder(f"readonly-finished-{index}", path, state="complete")
            os.utime(path, (index + 1, index + 1))

        assert not paths[0].exists()
        assert not paths[1].exists()
        assert all(path.exists() for path in paths[2:])
        assert len(read_only_worktrees(repo)) == 5


def test_read_only_reaper_pins_detached_head_before_remove() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        base = add_read_only_base(repo, 0)
        path, _base = goalflight_worktree_pool.shared_read_only_worktree(
            repo, base=base
        )
        (path / "detached-only.txt").write_text("keep this commit\n")
        git(path, "add", "detached-only.txt")
        git(path, "commit", "-m", "detached-only")
        head = git(path, "rev-parse", "HEAD")
        record_read_only_holder("finished-pin", path, state="complete")
        os.utime(path, (1, 1))

        prior_keep = goalflight_worktree_pool.READ_ONLY_WORKTREE_KEEP
        goalflight_worktree_pool.READ_ONLY_WORKTREE_KEEP = 0
        try:
            goalflight_worktree_pool.reap_read_only_worktrees(repo)
        finally:
            goalflight_worktree_pool.READ_ONLY_WORKTREE_KEEP = prior_keep

        assert not path.exists()
        keep_refs = git(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            "refs/goalflight/keep/",
        ).splitlines()
        keep_refs = [ref for ref in keep_refs if ref.startswith("refs/goalflight/keep/gc-")]
        assert any(git(repo, "rev-parse", ref) == head for ref in keep_refs)


def test_read_only_in_use_checkout_survives_the_cap() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        paths = []
        for index in range(6):
            path, _base = goalflight_worktree_pool.shared_read_only_worktree(
                repo, base=add_read_only_base(repo, index)
            )
            paths.append(path)
            os.utime(path, (index + 1, index + 1))
            if index == 0:
                record_read_only_holder("readonly-live", path)

        assert paths[0].exists()
        assert len(read_only_worktrees(repo)) == 6


def test_read_only_allocation_grace_closes_ledger_registration_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        monkeypatch.setattr(goalflight_worktree_pool, "READ_ONLY_WORKTREE_KEEP", 0)
        first, _base = goalflight_worktree_pool.shared_read_only_worktree(
            repo, base=add_read_only_base(repo, 0)
        )
        stray = first.parent / "stray-not-registered"
        stray.mkdir()

        second, _base = goalflight_worktree_pool.shared_read_only_worktree(
            repo, base=add_read_only_base(repo, 1)
        )

        assert first.exists(), "fresh allocation must survive before ledger registration"
        assert second.exists()
        assert stray.is_dir(), "unregistered stray directories are never removed"


def test_unreadable_read_only_ledger_keeps_every_checkout() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        paths = []
        for index in range(5):
            path, _base = goalflight_worktree_pool.shared_read_only_worktree(
                repo, base=add_read_only_base(repo, index)
            )
            paths.append(path)
            os.utime(path, (index + 1, index + 1))
        runs = goalflight_ledger.runs_dir(create=True)
        (runs / "unreadable.json").write_text("{not json", encoding="utf-8")

        path, _base = goalflight_worktree_pool.shared_read_only_worktree(
            repo, base=add_read_only_base(repo, 5)
        )

        assert path.exists()
        assert all(item.exists() for item in paths)
        assert len(read_only_worktrees(repo)) == 6


def test_incomplete_nonterminal_ledger_keeps_every_checkout() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        paths = []
        for index in range(5):
            path, _base = goalflight_worktree_pool.shared_read_only_worktree(
                repo, base=add_read_only_base(repo, index)
            )
            paths.append(path)
            os.utime(path, (index + 1, index + 1))
        goalflight_ledger.write_record(
            {"dispatch_id": "incomplete-readonly-owner", "state": "running"}
        )

        path, _base = goalflight_worktree_pool.shared_read_only_worktree(
            repo, base=add_read_only_base(repo, 5)
        )

        assert path.exists()
        assert all(item.exists() for item in paths)
        assert len(read_only_worktrees(repo)) == 6


def test_dirty_read_only_checkout_is_kept_when_git_refuses_remove() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        paths = []
        for index in range(5):
            path, _base = goalflight_worktree_pool.shared_read_only_worktree(
                repo, base=add_read_only_base(repo, index)
            )
            paths.append(path)
            os.utime(path, (index + 1, index + 1))
        dirty_file = paths[0] / "uncommitted.txt"
        dirty_file.write_text("keep me\n", encoding="utf-8")

        goalflight_worktree_pool.shared_read_only_worktree(
            repo, base=add_read_only_base(repo, 5)
        )

        assert paths[0].is_dir()
        assert dirty_file.read_text(encoding="utf-8") == "keep me\n"


def test_read_only_reaper_stops_after_bounded_git_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        monkeypatch.setattr(goalflight_worktree_pool, "READ_ONLY_WORKTREE_KEEP", 0)
        root = repo / "worktrees" / goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
        root.mkdir(parents=True)
        paths = [root / "old-one", root / "old-two"]
        for path in paths:
            path.mkdir()
            os.utime(path, (1, 1))

        monkeypatch.setattr(
            goalflight_worktree_pool,
            "_read_only_registered_worktrees",
            lambda _repo, **_kwargs: paths,
        )
        monkeypatch.setattr(
            goalflight_worktree_pool,
            "read_only_worktree_usage",
            lambda _path, **_kwargs: {"verdict": goalflight_worktree_pool.YES},
        )
        calls: list[dict] = []

        def timed_out(_cwd: Path, *args: str, **kwargs):
            calls.append({"args": args, "timeout": kwargs.get("timeout")})
            raise goalflight_worktree_pool.WorktreeSeatError("git command timed out")

        monkeypatch.setattr(goalflight_worktree_pool, "_git", timed_out)
        monkeypatch.setattr(
            goalflight_worktree_pool,
            "pin_worktree_head_before_remove",
            lambda *_args, **_kwargs: ("refs/goalflight/keep/test", None),
        )
        goalflight_worktree_pool._reap_read_only_worktrees(
            repo, root=root, requested_path=None
        )

        assert len(calls) == 1
        assert calls[0]["args"] == ("worktree", "remove", str(paths[0]))
        assert 0 < calls[0]["timeout"] <= goalflight_worktree_pool.READ_ONLY_GIT_TIMEOUT_S
        assert all(path.is_dir() for path in paths)


def test_git_proc_subtracts_identity_guard_time_from_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, float | None]] = []
    clock = iter((100.0, 100.4))

    monkeypatch.setattr(
        goalflight_worktree_pool,
        "guard_worktree_mutation",
        lambda *_args, **kwargs: calls.append(("guard", kwargs["timeout"])) or None,
    )
    monkeypatch.setattr(
        goalflight_worktree_pool.time,
        "monotonic",
        lambda: next(clock),
    )
    monkeypatch.setattr(
        goalflight_worktree_pool.subprocess,
        "run",
        lambda *_args, **kwargs: (
            calls.append(("git", kwargs["timeout"]))
            or subprocess.CompletedProcess(["git"], 0, "", "")
        ),
    )

    result = goalflight_worktree_pool._git_proc(
        tmp_path, "checkout", "--detach", timeout=1.0
    )

    assert result is not None and result.returncode == 0
    assert calls[0] == ("guard", 1.0)
    assert calls[1][0] == "git"
    assert calls[1][1] is not None and 0.5 < calls[1][1] < 0.7


def test_guard_identity_checks_share_timeout_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    calls: list[float | None] = []
    clock = iter((100.0, 100.3, 100.6))

    monkeypatch.setattr(
        goalflight_worktree_pool.time,
        "monotonic",
        lambda: next(clock),
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_git_identity",
        lambda _cwd, *, timeout=None: (
            calls.append(timeout)
            or ("/git/worktree", "/git/common", "/git/top")
        ),
    )

    assert (
        goalflight_worktree_pool.guard_worktree_mutation(
            tmp_path, "worktree", "remove", str(target), timeout=1.0
        )
        is None
    )
    assert len(calls) == 2
    assert calls[0] is not None and 0.6 < calls[0] < 0.8
    assert calls[1] is not None and 0.3 < calls[1] < 0.5


@pytest.mark.parametrize("bind_step", ["create", "verify", "pin", "quarantine", "checkout"])
def test_failed_bind_tracks_holder_only_after_creation_or_checkout(
    bind_step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        if bind_step in {"pin", "quarantine", "checkout"}:
            seed = goalflight_worktree_pool.acquire_worktree_seat(repo, "seed")
            if bind_step == "pin":
                git(seed.path, "checkout", "--detach")
                (seed.path / "tracked.txt").write_text("unique\n", encoding="utf-8")
                git(seed.path, "add", "tracked.txt")
                git(seed.path, "commit", "-m", "unique")
            elif bind_step == "quarantine":
                (seed.path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            finish_seat_holder(seed)

        target = {
            "create": "_create_seat_worktree",
            "verify": "_verify_existing_seat",
            "pin": "pin_unique_commits",
            "quarantine": "_quarantine_dirty_worktree",
            "checkout": "_prepare_seat_checkout",
        }[bind_step]
        original = getattr(goalflight_worktree_pool, target)

        def fail(*_args, **_kwargs):
            if bind_step == "pin":
                return {"verdict": goalflight_worktree_pool.UNKNOWN, "reason": "injected pin failure", "keep_ref": None}
            raise RuntimeError(f"injected {bind_step} failure")

        lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, "s-1")
        prior_occupant = lock_path.read_text(encoding="utf-8") if lock_path.exists() else ""
        monkeypatch.setattr(goalflight_worktree_pool, target, fail)
        with pytest.raises(Exception, match=f"injected {bind_step} failure") as caught:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "failed-bind")
        monkeypatch.setattr(goalflight_worktree_pool, target, original)

        if bind_step == "verify":
            assert "seat_preparation_failed" in str(caught.value)
            assert json.loads(lock_path.read_text(encoding="utf-8"))["dispatch_id"] == (
                "failed-bind"
            )
            record_finished_holder("failed-bind", state="failed", project_root=repo)
        else:
            assert lock_path.read_text(encoding="utf-8") == prior_occupant
        retry = goalflight_worktree_pool.acquire_worktree_seat(repo, "retry-bind")
        try:
            assert retry.path.name == "s-1"
        finally:
            retry.release()


def test_registration_ignores_basename_without_a_lock() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = make_repo(root)
        adhoc = root / "wt-9"
        git(repo, "worktree", "add", "-q", "-b", "adhoc-wt9", str(adhoc))
        with seat_limit(2):
            verdict, reason = goalflight_worktree_pool.registered_pool_seat_verdict(
                adhoc, project_root=repo
            )
            assert_true(
                "ad-hoc wt-9 is not registered",
                verdict == "no",
            )
            assert_true(
                "reason names the managed root, not the basename",
                "managed worktree root" in reason,
            )
            lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "real-seat")
            try:
                yes, yes_reason = goalflight_worktree_pool.registered_pool_seat_verdict(
                    lease.path, project_root=repo
                )
                assert_true("acquired seat is registered", yes == "yes")
                assert_true("reason names the seat", "s-1" in yes_reason)
            finally:
                lease.release()
            still_yes, _ = goalflight_worktree_pool.registered_pool_seat_verdict(
                lease.path, project_root=repo
            )
            assert_true("released seat stays registered", still_yes == "yes")

            prior = os.environ.get(goalflight_worktree_pool.WORKTREE_SEATS_ENV)
            os.environ[goalflight_worktree_pool.WORKTREE_SEATS_ENV] = "bogus"
            try:
                unknown, unknown_reason = (
                    goalflight_worktree_pool.registered_pool_seat_verdict(
                        lease.path, project_root=repo
                    )
                )
            finally:
                if prior is None:
                    os.environ.pop(goalflight_worktree_pool.WORKTREE_SEATS_ENV, None)
                else:
                    os.environ[goalflight_worktree_pool.WORKTREE_SEATS_ENV] = prior
            assert_true("unreadable seat config is unknown", unknown == "unknown")
            assert_true(
                "unknown reason names configuration",
                "configuration unreadable" in unknown_reason,
            )


def test_notes_survive_acquire_reset_and_result_is_quarantined() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "notes-one")
        notes = first.path / ".goal-flight" / "seat" / "memory.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("keep me\n", encoding="utf-8")
        (first.path / "RESULT.md").write_text("old result\n", encoding="utf-8")
        finish_seat_holder(first)

        reused = goalflight_worktree_pool.acquire_worktree_seat(repo, "notes-two")
        try:
            assert_true("same captive seat", reused.path.name == "s-1")
            assert_true("notes survived reset", notes.is_file())
            assert_true(
                "notes content kept",
                notes.read_text(encoding="utf-8") == "keep me\n",
            )
            branches = git(
                repo,
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads/goalflight/quarantine/",
            ).splitlines()
            assert_true("quarantine captured RESULT.md", len(branches) == 1)
            assert_true(
                "untracked notes remain outside quarantine",
                ".goal-flight/seat/memory.md" not in _tree_names(repo, branches[0]),
            )
            assert_true("RESULT.md cleared", not (reused.path / "RESULT.md").exists())
            assert_true(
                "RESULT.md in quarantine",
                git(repo, "show", f"{branches[0]}:RESULT.md") == "old result",
            )
        finally:
            reused.release()


def test_unused_seat_precedes_blocked_seat_until_under_pressure() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        blocked = goalflight_worktree_pool.acquire_worktree_seat(repo, "blocked-owner")
        blocked_path = blocked.path
        finish_seat_holder_at(
            blocked,
            state="blocked",
            ended_at="2026-01-01T00:00:00+00:00",
        )

        unused = goalflight_worktree_pool.acquire_worktree_seat(repo, "unused-first")
        assert unused.path != blocked_path
        assert git(blocked_path, "rev-parse", "--abbrev-ref", "HEAD") == (
            "worktree/blocked-owner"
        )

        under_pressure = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "blocked-under-pressure"
        )
        try:
            assert under_pressure.path == blocked_path
            assert under_pressure.reclaimed_dispatch_id == "blocked-owner"
        finally:
            under_pressure.release()
            unused.release()


def test_completed_seat_precedes_older_blocked_seat() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        blocked = goalflight_worktree_pool.acquire_worktree_seat(repo, "older-blocked")
        completed = goalflight_worktree_pool.acquire_worktree_seat(repo, "completed")
        blocked_path = blocked.path
        completed_path = completed.path
        finish_seat_holder_at(
            blocked,
            state="blocked",
            ended_at="2026-01-01T00:00:00+00:00",
        )
        finish_seat_holder_at(
            completed,
            state="complete",
            ended_at="2026-01-02T00:00:00+00:00",
        )

        selected = goalflight_worktree_pool.acquire_worktree_seat(repo, "next-dispatch")
        try:
            assert selected.path == completed_path
            assert selected.reclaimed_dispatch_id == "completed"
            assert git(blocked_path, "rev-parse", "--abbrev-ref", "HEAD") == (
                "worktree/older-blocked"
            )
        finally:
            selected.release()


@pytest.mark.parametrize(
    ("grace_s", "complete_age_s", "expected_holder"),
    [(7200, 600, "older-blocked"), (600, 1800, "completed")],
)
def test_completed_seat_retention_grace_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
    grace_s: int,
    complete_age_s: int,
    expected_holder: str,
) -> None:
    monkeypatch.setenv(
        "GOALFLIGHT_WORKTREE_COMPLETE_RETENTION_GRACE_S", str(grace_s)
    )
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        blocked = goalflight_worktree_pool.acquire_worktree_seat(repo, "older-blocked")
        completed = goalflight_worktree_pool.acquire_worktree_seat(repo, "completed")
        expected_path = blocked.path if expected_holder == "older-blocked" else completed.path
        now = dt.datetime.now(dt.timezone.utc)
        finish_seat_holder_at(
            blocked,
            state="blocked",
            ended_at=(now - dt.timedelta(hours=1)).isoformat(),
        )
        finish_seat_holder_at(
            completed,
            state="complete",
            ended_at=(now - dt.timedelta(seconds=complete_age_s)).isoformat(),
        )

        selected = goalflight_worktree_pool.acquire_worktree_seat(repo, "next-dispatch")
        try:
            assert selected.path == expected_path
            assert selected.reclaimed_dispatch_id == expected_holder
        finally:
            selected.release()


def test_superseded_seat_precedes_older_blocked_seat() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        blocked = goalflight_worktree_pool.acquire_worktree_seat(repo, "older-blocked")
        superseded = goalflight_worktree_pool.acquire_worktree_seat(repo, "superseded")
        blocked_path = blocked.path
        superseded_path = superseded.path
        finish_seat_holder_at(
            blocked,
            state="blocked",
            ended_at="2026-01-01T00:00:00+00:00",
        )
        finish_seat_holder_at(
            superseded,
            state="superseded",
            ended_at="2026-01-02T00:00:00+00:00",
        )

        selected = goalflight_worktree_pool.acquire_worktree_seat(repo, "next-dispatch")
        try:
            assert selected.path == superseded_path
            assert selected.reclaimed_dispatch_id == "superseded"
            assert git(blocked_path, "rev-parse", "--abbrev-ref", "HEAD") == (
                "worktree/older-blocked"
            )
        finally:
            selected.release()


def test_oldest_resumable_terminal_holder_is_recycled_first() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        newer = goalflight_worktree_pool.acquire_worktree_seat(repo, "newer-blocked")
        older = goalflight_worktree_pool.acquire_worktree_seat(repo, "older-user-need")
        older_path = older.path
        finish_seat_holder_at(
            older,
            state="blocked",
            ended_at="2026-01-01T00:00:00+00:00",
        )
        finish_seat_holder_at(
            newer,
            state="blocked",
            ended_at="2026-01-02T00:00:00+00:00",
        )
        record = goalflight_ledger.read_record("older-user-need")
        assert record
        record["reason"] = {"marker_kind": "USER-NEED"}
        goalflight_ledger.write_record(record)

        selected = goalflight_worktree_pool.acquire_worktree_seat(repo, "next-dispatch")
        try:
            assert selected.path == older_path
            assert selected.reclaimed_dispatch_id == "older-user-need"
        finally:
            selected.release()


def test_free_seat_nearest_to_target_base_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_COMPLETE_RETENTION_GRACE_S", "0")
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        base = git(repo, "rev-parse", "HEAD")
        old = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "old-base", base=base
        )
        warm = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "warm-base", base=base
        )
        warm_path = warm.path
        finish_seat_holder(old)
        finish_seat_holder(warm)

        (repo / "target.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "target.txt")
        git(repo, "commit", "-m", "target")
        target = git(repo, "rev-parse", "HEAD")

        # Prepare s-2 at the target while s-1 remains on the old base. This
        # mirrors a free pinned seat left behind by a previous launch.
        exact = goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "warm-target",
            base=target,
            occupy_path=warm_path,
        )
        finish_seat_holder(exact)

        selected = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "target-dispatch", base=target
        )
        try:
            assert_true("nearest target-base seat wins", selected.path == warm_path)
        finally:
            selected.release()


def test_free_ancestor_base_beats_lower_slot_unrelated_base() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        ancestor = git(repo, "rev-parse", "HEAD")

        git(repo, "checkout", "-b", "unrelated")
        (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        git(repo, "add", "unrelated.txt")
        git(repo, "commit", "-m", "unrelated")
        unrelated = git(repo, "rev-parse", "HEAD")

        git(repo, "checkout", "main")
        (repo / "target.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "target.txt")
        git(repo, "commit", "-m", "target")
        target = git(repo, "rev-parse", "HEAD")

        lower_unrelated = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "lower-unrelated", base=unrelated
        )
        higher_ancestor = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "higher-ancestor", base=ancestor
        )
        lower_path = lower_unrelated.path
        higher_path = higher_ancestor.path
        finish_seat_holder(lower_unrelated)
        finish_seat_holder(higher_ancestor)

        selected = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "target-dispatch", base=target
        )
        try:
            assert_true("ancestor base beats unrelated lower slot", selected.path == higher_path)
            assert_true("lower slot remains unrelated", lower_path != selected.path)
        finally:
            selected.release()


def _exercise_failed_dirty_preparation_is_quarantined_and_reclaimable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        (repo / "staged.txt").write_text("base staged\n", encoding="utf-8")
        (repo / "deleted.txt").write_text("base deleted\n", encoding="utf-8")
        (repo / "rename-old.txt").write_text("base renamed\n", encoding="utf-8")
        (repo / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
        git(repo, "add", ".gitignore", "staged.txt", "deleted.txt", "rename-old.txt")
        git(repo, "commit", "-m", "quarantine fixture")
        base = git(repo, "rev-parse", "HEAD")
        previous = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "previous-occupant", base=base
        )
        prior_path = previous.path
        (prior_path / "tracked.txt").write_text("unstaged bytes\n", encoding="utf-8")
        (prior_path / "staged.txt").write_text("staged bytes\n", encoding="utf-8")
        git(prior_path, "add", "staged.txt")
        (prior_path / "deleted.txt").unlink()
        git(prior_path, "mv", "rename-old.txt", "rename-new.txt")
        (prior_path / "previous-untracked.txt").write_text(
            "untracked bytes\n", encoding="utf-8"
        )
        ignored = prior_path / "retained.ignored"
        ignored.write_bytes(b"ignored bytes\x00\xff\n")
        finish_seat_holder(previous)

        dispatch_id = "dirty-preparation-refusal"
        target_branch = goalflight_worktree_pool.seat_branch_name(dispatch_id)
        real_assert = goalflight_worktree_pool._assert_seat_on_named_branch

        def fail_after_switch(path: Path, *, seat_name: str, branch: str) -> str:
            if path == prior_path and branch == target_branch:
                raise goalflight_worktree_pool.WorktreeSeatError(
                    "injected preparation refusal"
                )
            return real_assert(path, seat_name=seat_name, branch=branch)

        monkeypatch.setattr(
            goalflight_worktree_pool, "_assert_seat_on_named_branch", fail_after_switch
        )
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatError,
            match="seat_preparation_failed:s-1; original preparation refusal: "
            "injected preparation refusal",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, dispatch_id, base=base
            )
        assert git(prior_path, "rev-parse", "--abbrev-ref", "HEAD") == target_branch
        assert git(prior_path, "rev-parse", "HEAD") == base
        assert not (repo / "worktrees" / "s-2").exists()

        quarantine = _quarantine_branch(repo)
        names = _tree_names(repo, quarantine)
        assert "deleted.txt" not in names
        assert "rename-old.txt" not in names
        assert "rename-new.txt" in names
        assert git(repo, "show", f"{quarantine}:tracked.txt") == "unstaged bytes"
        assert git(repo, "show", f"{quarantine}:staged.txt") == "staged bytes"
        assert git(repo, "show", f"{quarantine}:rename-new.txt") == "base renamed"
        assert git(repo, "show", f"{quarantine}:previous-untracked.txt") == "untracked bytes"
        assert ignored.read_bytes() == b"ignored bytes\x00\xff\n"
        assert not (prior_path / "previous-untracked.txt").exists()

        record_finished_holder(dispatch_id, state="failed", project_root=repo)
        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "different-dispatch", base=base
        )
        try:
            assert replacement.path == prior_path
            assert replacement.branch != target_branch
            assert ignored.read_bytes() == b"ignored bytes\x00\xff\n"
        finally:
            replacement.release()


def test_failed_dirty_preparation_is_quarantined_and_reclaimable() -> None:
    with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as monkeypatch:
        _exercise_failed_dirty_preparation_is_quarantined_and_reclaimable(
            Path(td), monkeypatch
        )


def _exercise_failed_clean_seat_preparation_stays_and_is_reclaimable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        previous = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "previous-occupant", base=base
        )
        prior_path = previous.path
        finish_seat_holder(previous)
        dispatch_id = "clean-preparation-refusal"
        target_branch = goalflight_worktree_pool.seat_branch_name(dispatch_id)
        real_assert = goalflight_worktree_pool._assert_seat_on_named_branch

        def fail_after_switch(path: Path, *, seat_name: str, branch: str) -> str:
            if path == prior_path and branch == target_branch:
                raise goalflight_worktree_pool.WorktreeSeatError(
                    "injected preparation refusal"
                )
            return real_assert(path, seat_name=seat_name, branch=branch)

        monkeypatch.setattr(
            goalflight_worktree_pool, "_assert_seat_on_named_branch", fail_after_switch
        )
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatError,
            match="seat_preparation_failed:s-1; original preparation refusal: "
            "injected preparation refusal",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, dispatch_id, base=base
            )
        assert git(prior_path, "rev-parse", "--abbrev-ref", "HEAD") == target_branch
        assert git(prior_path, "rev-parse", "HEAD") == base
        assert (prior_path / "tracked.txt").read_text(encoding="utf-8") == "base\n"
        assert not (repo / "worktrees" / "s-2").exists()

        record_finished_holder(dispatch_id, state="failed", project_root=repo)
        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "different-clean-dispatch", base=base
        )
        try:
            assert replacement.path == prior_path
            assert replacement.branch != target_branch
        finally:
            replacement.release()


def test_failed_clean_seat_preparation_stays_and_is_reclaimable() -> None:
    with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as monkeypatch:
        _exercise_failed_clean_seat_preparation_stays_and_is_reclaimable(
            Path(td), monkeypatch
        )


def _exercise_occupant_write_failure_preserves_prior_holder_and_reclaims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        previous = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "previous-occupant", base=base
        )
        seat = previous.path
        (seat / "tracked.txt").write_text("protected bytes\n", encoding="utf-8")
        finish_seat_holder(previous)
        real_write = goalflight_worktree_pool._write_occupant
        dispatch_id = "occupant-write-failure"

        def fail_occupant_write(lock_file, *, dispatch_id: str, **kwargs) -> None:
            if (
                dispatch_id == "occupant-write-failure"
                and git(seat, "rev-parse", "--abbrev-ref", "HEAD")
                == goalflight_worktree_pool.seat_branch_name(dispatch_id)
            ):
                raise goalflight_worktree_pool.WorktreeSeatError(
                    "injected occupant write failure"
                )
            real_write(lock_file, dispatch_id=dispatch_id, **kwargs)

        monkeypatch.setattr(
            goalflight_worktree_pool, "_write_occupant", fail_occupant_write
        )
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatError,
            match="seat_preparation_failed:s-1; original preparation refusal: "
            "injected occupant write failure",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, dispatch_id, base=base
            )
        quarantine = _quarantine_branch(repo)
        assert git(repo, "show", f"{quarantine}:tracked.txt") == "protected bytes"
        assert git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
            goalflight_worktree_pool.seat_branch_name("previous-occupant")
        )
        lock_path = goalflight_worktree_pool._seat_lock_root(repo) / "s-1.lock"
        occupant = json.loads(lock_path.read_text(encoding="utf-8"))
        assert occupant["dispatch_id"] == "previous-occupant"

        # The original occupant is terminal and both the branch and lock still
        # identify it, so the normal candidate scan can reclaim the seat.
        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "after-occupant-write-failure", base=base
        )
        try:
            assert replacement.path.resolve() == seat.resolve()
            assert replacement.branch != goalflight_worktree_pool.seat_branch_name(
                dispatch_id
            )
        finally:
            replacement.release()


def test_occupant_write_failure_preserves_prior_holder_and_reclaims() -> None:
    with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as monkeypatch:
        _exercise_occupant_write_failure_preserves_prior_holder_and_reclaims(
            Path(td), monkeypatch
        )


def test_failed_checkout_rollback_marks_seat_for_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        previous = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "rollback-previous", base=base
        )
        seat = previous.path
        finish_seat_holder(previous)
        old_branch = "worktree/rollback-previous"
        dispatch_id = "rollback-new-dispatch"
        target_branch = goalflight_worktree_pool.seat_branch_name(dispatch_id)
        real_write = goalflight_worktree_pool._write_occupant
        real_git = goalflight_worktree_pool._git

        def fail_publication(lock_file, *, dispatch_id: str, **kwargs) -> None:
            if (
                dispatch_id == "rollback-new-dispatch"
                and git(seat, "rev-parse", "--abbrev-ref", "HEAD") == target_branch
            ):
                raise goalflight_worktree_pool.WorktreeSeatError(
                    "injected occupant publication failure"
                )
            real_write(lock_file, dispatch_id=dispatch_id, **kwargs)

        def fail_rollback(cwd: Path, *args: str, **kwargs):
            if (
                Path(cwd) == seat
                and args[:4] == ("checkout", "--force", "-B", old_branch)
            ):
                raise goalflight_worktree_pool.WorktreeSeatError(
                    "injected checkout rollback failure"
                )
            return real_git(cwd, *args, **kwargs)

        monkeypatch.setattr(goalflight_worktree_pool, "_write_occupant", fail_publication)
        monkeypatch.setattr(goalflight_worktree_pool, "_git", fail_rollback)
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatError,
            match="checkout rollback failed: injected checkout rollback failure",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, dispatch_id, base=base
            )

        occupant = json.loads(
            goalflight_worktree_pool.worktree_seat_lock_path(
                repo, seat.name
            ).read_text(encoding="utf-8")
        )
        assert occupant["dispatch_id"] == dispatch_id
        assert occupant["reconciliation_required"] is True
        assert git(seat, "rev-parse", "--abbrev-ref", "HEAD") == target_branch


def _exercise_failed_new_seat_preparation_keeps_slot_for_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        dispatch_id = "new-seat-refusal"
        target_branch = goalflight_worktree_pool.seat_branch_name(dispatch_id)
        real_assert = goalflight_worktree_pool._assert_seat_on_named_branch

        def fail_after_create(path: Path, *, seat_name: str, branch: str) -> str:
            if branch == target_branch:
                raise goalflight_worktree_pool.WorktreeSeatError(
                    "injected preparation refusal"
                )
            return real_assert(path, seat_name=seat_name, branch=branch)

        monkeypatch.setattr(
            goalflight_worktree_pool, "_assert_seat_on_named_branch", fail_after_create
        )
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatError,
            match="seat_preparation_failed:s-1; original preparation refusal: "
            "injected preparation refusal",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, dispatch_id, base=base
            )

        seat = repo / "worktrees" / "s-1"
        assert seat.is_dir()
        assert git(seat, "rev-parse", "--abbrev-ref", "HEAD") == target_branch
        assert git(seat, "rev-parse", "HEAD") == base
        record_finished_holder(dispatch_id, state="failed", project_root=repo)
        _rewrite_seat_holder_as_dead(repo, seat.name, dispatch_id)
        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "new-id-reclaims-slot", base=base
        )
        try:
            assert replacement.path.resolve() == seat.resolve()
            assert replacement.branch != target_branch
        finally:
            replacement.release()


def test_failed_new_seat_preparation_keeps_slot_for_reclaim() -> None:
    with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as monkeypatch:
        _exercise_failed_new_seat_preparation_keeps_slot_for_reclaim(
            Path(td), monkeypatch
        )


def test_new_slot_holder_write_failure_is_reclaimed_from_branch() -> None:
    with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as monkeypatch:
        root = Path(td)
        with seat_limit(1):
            repo = make_repo(root)
            base = git(repo, "rev-parse", "HEAD")
            dispatch_id = "new-slot-holder-write-failure"
            real_write = goalflight_worktree_pool._write_occupant

            def fail_holder_write(lock_file, *, dispatch_id: str, **kwargs) -> None:
                if dispatch_id == "new-slot-holder-write-failure":
                    raise goalflight_worktree_pool.WorktreeSeatError(
                        "injected holder record failure"
                    )
                real_write(lock_file, dispatch_id=dispatch_id, **kwargs)

            monkeypatch.setattr(
                goalflight_worktree_pool, "_write_occupant", fail_holder_write
            )
            with pytest.raises(
                goalflight_worktree_pool.WorktreeSeatError,
                match="seat_preparation_failed:s-1; original preparation refusal:",
            ):
                goalflight_worktree_pool.acquire_worktree_seat(
                    repo, dispatch_id, base=base
                )

            seat = repo / "worktrees" / "s-1"
            assert seat.is_dir()
            assert git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
                goalflight_worktree_pool.seat_branch_name(dispatch_id)
            )
            lock_path = goalflight_worktree_pool._seat_lock_root(repo) / "s-1.lock"
            with lock_path.open("r+", encoding="utf-8") as lock_file:
                assert goalflight_worktree_pool._known_lock_dispatch_id(lock_file) is None

            goalflight_ledger.write_record(
                {"dispatch_id": dispatch_id, "state": "waiting_capacity"}
            )
            with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
                goalflight_worktree_pool.acquire_worktree_seat(
                    repo, "blocked-before-terminal", base=base
                )

            record_finished_holder(dispatch_id, state="failed", project_root=repo)
            replacement = goalflight_worktree_pool.acquire_worktree_seat(
                repo, "new-id-after-holder-write-failure", base=base
            )
            try:
                assert replacement.path.resolve() == seat.resolve()
                assert replacement.branch != goalflight_worktree_pool.seat_branch_name(
                    dispatch_id
                )
            finally:
                replacement.release()


def _exercise_worktree_add_branch_race_uses_named_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    branch = goalflight_worktree_pool.seat_branch_name("branch-race")
    target = repo / "worktrees" / "s-1"
    target.parent.mkdir(parents=True)
    external = tmp_path / "external"
    real_git = goalflight_worktree_pool._git
    raced = False

    def race_between_check_and_add(path: Path, *args: str, **kwargs) -> str:
        nonlocal raced
        if args[:2] == ("worktree", "add") and not raced:
            raced = True
            real_git(repo, "worktree", "add", "-b", branch, str(external), base)
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(
        goalflight_worktree_pool, "_git", race_between_check_and_add
    )
    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatError,
        match="dispatch_branch_in_use:external; dispatch again under a new id",
    ):
        goalflight_worktree_pool._create_seat_worktree(
            repo,
            target,
            branch=branch,
            base_commit=base,
            keep_id="branch-race",
        )
    assert raced
    assert not target.exists()
    assert external.is_dir()


def test_worktree_add_branch_race_uses_named_refusal() -> None:
    with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as monkeypatch:
        _exercise_worktree_add_branch_race_uses_named_refusal(Path(td), monkeypatch)


def _exercise_same_dispatch_branch_is_nonretryable_but_other_id_recycles_seat(
    tmp_path: Path, monkeypatch, captured_stdout: io.StringIO, captured_stderr: io.StringIO
) -> None:
    with seat_limit(1):
        monkeypatch.delenv(
            goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, raising=False
        )
        repo = make_repo(tmp_path)
        configure_pool_test_environment(monkeypatch, tmp_path, Path("/dev/null"))
        monkeypatch.chdir(repo)
        base = git(repo, "rev-parse", "HEAD")
        stale = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "stale-dispatch", base=base
        )
        stale_path = stale.path
        stale_branch = stale.branch
        stale_head = git(stale_path, "rev-parse", "HEAD")
        stale.release()
        goalflight_ledger.write_record(
            {"dispatch_id": "stale-dispatch", "state": "queued"}
        )
        original_git = goalflight_worktree_pool._git
        add_calls = []

        def record_add(path: Path, *args: str, **kwargs) -> str:
            if args[:2] == ("worktree", "add"):
                add_calls.append(args)
            return original_git(path, *args, **kwargs)

        monkeypatch.setattr(goalflight_worktree_pool, "_git", record_add)
        monkeypatch.setattr(goalflight_dispatch, "_resolve_launch_account_env", lambda _args: {})
        monkeypatch.setattr(goalflight_dispatch, "_acquire_capacity", lambda *_args, **_kwargs: "test-lease")
        monkeypatch.setattr(goalflight_dispatch, "_release_capacity", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(goalflight_dispatch.goalflight_capacity, "mark_lease_spawn_failed", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(goalflight_dispatch.goalflight_capacity, "mark_lease_spawning", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(goalflight_dispatch, "_install_capacity_lease_signal_guard", lambda _lease: lambda: None)
        monkeypatch.setattr(goalflight_dispatch, "_stamp_controller_session", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(goalflight_dispatch, "_prepare_attempt_controller_registration", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(goalflight_dispatch.goalflight_cursor, "cleanup_dispatch_data", lambda *_args, **_kwargs: None)
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(
            captured_stderr
        ):
            rc = goalflight_dispatch.main(
                [
                    "--agent", "codex", "--from-queue", "--dispatch-id", "stale-dispatch",
                    "--worktree", base, "--capacity-wait-s", "0", "--foreground",
                    "--unregistered-forced", "--", sys.executable, "-c", "pass",
                ]
            )
        assert rc == 64
        assert "dispatch_branch_in_use:s-1" in captured_stderr.getvalue(), (
            captured_stdout.getvalue(),
            captured_stderr.getvalue(),
        )
        assert '"permanent": true' in captured_stdout.getvalue(), (
            captured_stdout.getvalue(),
            captured_stderr.getvalue(),
        )
        assert add_calls == []
        assert git(stale_path, "rev-parse", "--abbrev-ref", "HEAD") == stale_branch
        assert git(stale_path, "rev-parse", "HEAD") == stale_head
        terminal = json.loads(
            goalflight_ledger.record_path("stale-dispatch").read_text(encoding="utf-8")
        )
        assert terminal["state"] == "failed_worktree"
        assert terminal["prelaunch_failure"] is True
        assert goalflight_ledger.terminal_state_for(
            terminal["state"], terminal.get("reason")
        ) == "error"
        _rewrite_seat_holder_as_dead(repo, stale_path.name, "stale-dispatch")

        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "different-dispatch", base=base
        )
        try:
            assert replacement.path == stale_path
            assert replacement.branch != stale_branch
        finally:
            replacement.release()


def test_same_dispatch_branch_is_nonretryable_but_other_id_recycles_seat() -> None:
    with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as monkeypatch:
        _exercise_same_dispatch_branch_is_nonretryable_but_other_id_recycles_seat(
            Path(td), monkeypatch, io.StringIO(), io.StringIO()
        )


def test_skip_reset_keeps_dirty_product_files() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-src")
        (first.path / "RESULT.md").write_text("in progress\n", encoding="utf-8")
        path = first.path
        finish_seat_holder(first)

        resumed = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "resume-dst", reset=False, occupy_path=path
        )
        try:
            assert_true("occupied same seat", resumed.path == path)
            assert_true(
                "dirty product survived skip-reset",
                (path / "RESULT.md").read_text(encoding="utf-8") == "in progress\n",
            )
        finally:
            resumed.release()


def test_two_controller_labels_share_repository_pool() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        a = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "ctrl-a", controller_label="alpha"
        )
        b = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "ctrl-b", controller_label="beta"
        )
        try:
            assert_true("distinct concurrent worktrees", a.path != b.path)
            assert_true("repository pool directory", a.path.parent.name == "worktrees")
            assert_true("alpha label is metadata only", a.path.parent == b.path.parent)
        finally:
            a.release()
            b.release()


def test_classify_dispatch_cwd_lock() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        lease = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "cwd-lock", controller_label="alpha"
        )
        try:
            assert_true(
                "project root is in-place",
                goalflight_worktree_pool.classify_dispatch_cwd(
                    repo, project_root=repo, controller_label="alpha"
                )
                == "in-place",
            )
            assert_true(
                "own seat is ring-seat",
                goalflight_worktree_pool.classify_dispatch_cwd(
                    lease.path, project_root=repo, controller_label="alpha"
                )
                == "ring-seat",
            )
            other = repo / ".cache" / "worktrees" / "foo"
            other.mkdir(parents=True)
            assert_true(
                "cache tree is refused",
                goalflight_worktree_pool.classify_dispatch_cwd(
                    other, project_root=repo, controller_label="alpha"
                )
                == "refuse",
            )
            foreign = goalflight_worktree_pool.classify_dispatch_cwd(
                lease.path, project_root=repo, controller_label="beta"
            )
            assert_true("other controller sees pooled worktree", foreign == "ring-seat")
            orphan = Path(td) / "nongit-project"
            orphan.mkdir()
            assert_true(
                "non-git project root is in-place",
                goalflight_worktree_pool.classify_dispatch_cwd(
                    orphan, project_root=orphan, controller_label="alpha"
                )
                == "in-place",
            )
        finally:
            lease.release()


@contextlib.contextmanager
def isolated_git_excludes(root: Path, excludes_text: str = ""):
    """Point git at an empty global exclude unless ``excludes_text`` is set.

    A developer global ignore that names ``.goal-flight`` would make the
    non-ignored control look ignored.
    """
    excludes = root / "excludes"
    excludes.write_text(excludes_text, encoding="utf-8")
    config = root / "gitconfig"
    system = root / "gitconfig-system"
    system.write_text("", encoding="utf-8")
    config.write_text(
        f"[core]\n\texcludesFile = {excludes}\n",
        encoding="utf-8",
    )
    keys = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")
    prior = {key: os.environ.get(key) for key in keys}
    os.environ["GIT_CONFIG_GLOBAL"] = str(config)
    os.environ["GIT_CONFIG_SYSTEM"] = str(system)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _quarantine_branch(repo: Path) -> str:
    branches = [
        line
        for line in git(
            repo,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/goalflight/quarantine/",
        ).splitlines()
        if line
    ]
    assert_true("one quarantine branch", len(branches) == 1)
    return branches[0]


def _tree_names(repo: Path, branch: str) -> set[str]:
    raw = git(repo, "ls-tree", "-r", "--name-only", branch)
    return {line for line in raw.splitlines() if line}


def _reclaim_dirty_seat(repo: Path, prepare) -> tuple[str, Path]:
    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "abandoned")
    try:
        prepare(abandoned.path)
        (abandoned.path / "abandoned.txt").write_text("preserve me\n", encoding="utf-8")
    finally:
        finish_seat_holder(abandoned)
    reused = goalflight_worktree_pool.acquire_worktree_seat(repo, "next")
    return _quarantine_branch(repo), reused.path


def test_quarantine_handles_reserved_paths_larger_than_argv_limit() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1), isolated_git_excludes(
        Path(td)
    ):
        repo = make_repo(Path(td))
        reserved_bytes = 0

        def prepare(seat: Path) -> None:
            nonlocal reserved_bytes
            notes = seat / ".goal-flight" / "seat"
            notes.mkdir(parents=True)
            for index in range(30_000):
                name = f"{index:05d}-{'x' * 80}.md"
                (notes / name).write_text("private\n", encoding="utf-8")
                reserved_bytes += len(f".goal-flight/seat/{name}") + 1
            (seat / "tracked.txt").write_text("abandoned edit\n", encoding="utf-8")

        branch, reused_path = _reclaim_dirty_seat(repo, prepare)
        assert reserved_bytes > os.sysconf("SC_ARG_MAX")
        names = _tree_names(repo, branch)
        assert_true("tracked edit quarantined", "tracked.txt" in names)
        assert_true(
            "reserved seat notes excluded from quarantine",
            not any(name.startswith(".goal-flight/seat/") for name in names),
        )
        assert_true(
            "seat reset",
            (reused_path / "tracked.txt").read_text(encoding="utf-8") == "base\n",
        )
        assert (reused_path / ".goal-flight" / "seat").is_dir()


def test_ignored_goal_flight_dir_does_not_block_quarantine() -> None:
    """An ignored ``.goal-flight/`` must not make ``git add`` fail the reclaim."""
    with tempfile.TemporaryDirectory() as td, seat_limit(1), isolated_git_excludes(Path(td)):
        repo = make_repo(Path(td))
        (repo / ".gitignore").write_text(".goal-flight/\n", encoding="utf-8")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-m", "ignore goal-flight")

        def prepare(seat: Path) -> None:
            notes = seat / ".goal-flight"
            notes.mkdir()
            (notes / "secret.txt").write_text("private\n", encoding="utf-8")
            (seat / "tracked.txt").write_text("abandoned edit\n", encoding="utf-8")

        branch, reused_path = _reclaim_dirty_seat(repo, prepare)
        names = _tree_names(repo, branch)
        assert_true("product file quarantined", "abandoned.txt" in names)
        assert_true("tracked edit quarantined", "tracked.txt" in names)
        assert_true(
            "ignored goal-flight not quarantined",
            ".goal-flight/secret.txt" not in names,
        )
        assert_true(
            "seat reset",
            (reused_path / "tracked.txt").read_text(encoding="utf-8") == "base\n",
        )


def test_empty_ignored_goal_flight_dir_does_not_block_quarantine() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1), isolated_git_excludes(Path(td)):
        repo = make_repo(Path(td))
        (repo / ".gitignore").write_text(".goal-flight/\n", encoding="utf-8")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-m", "ignore goal-flight")

        def prepare(seat: Path) -> None:
            (seat / ".goal-flight").mkdir()

        branch, _reused = _reclaim_dirty_seat(repo, prepare)
        names = _tree_names(repo, branch)
        assert_true("product file quarantined", "abandoned.txt" in names)
        assert_true(
            "empty ignored dir added nothing",
            not any(name.startswith(".goal-flight/") for name in names),
        )


def test_info_exclude_ignored_goal_flight_dir_does_not_block_quarantine() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1), isolated_git_excludes(Path(td)):
        repo = make_repo(Path(td))
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(".goal-flight/\n", encoding="utf-8")

        def prepare(seat: Path) -> None:
            notes = seat / ".goal-flight"
            notes.mkdir()
            (notes / "secret.txt").write_text("private\n", encoding="utf-8")

        branch, _reused = _reclaim_dirty_seat(repo, prepare)
        names = _tree_names(repo, branch)
        assert_true("product file quarantined", "abandoned.txt" in names)
        assert_true(
            "info/exclude goal-flight not quarantined",
            ".goal-flight/secret.txt" not in names,
        )


def test_global_exclude_ignored_goal_flight_dir_does_not_block_quarantine() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1), isolated_git_excludes(
        Path(td), ".goal-flight/\n"
    ):
        repo = make_repo(Path(td))

        def prepare(seat: Path) -> None:
            notes = seat / ".goal-flight"
            notes.mkdir()
            (notes / "secret.txt").write_text("private\n", encoding="utf-8")

        branch, _reused = _reclaim_dirty_seat(repo, prepare)
        names = _tree_names(repo, branch)
        assert_true("product file quarantined", "abandoned.txt" in names)
        assert_true(
            "global-exclude goal-flight not quarantined",
            ".goal-flight/secret.txt" not in names,
        )


def test_tracked_goal_flight_is_quarantined() -> None:
    """Tracked ``.goal-flight`` edits follow the same quarantine rule as product files."""
    with tempfile.TemporaryDirectory() as td, seat_limit(1), isolated_git_excludes(Path(td)):
        repo = make_repo(Path(td))
        notes = repo / ".goal-flight"
        notes.mkdir()
        (notes / "keep.txt").write_text("keep\n", encoding="utf-8")
        git(repo, "add", ".goal-flight/keep.txt")
        git(repo, "commit", "-m", "track goal-flight")

        def prepare(seat: Path) -> None:
            (seat / ".goal-flight" / "keep.txt").write_text("changed\n", encoding="utf-8")

        branch, reused = _reclaim_dirty_seat(repo, prepare)
        assert_true("product file quarantined", "abandoned.txt" in _tree_names(repo, branch))
        assert_true(
            "tracked goal-flight edit quarantined",
            git(repo, "show", f"{branch}:.goal-flight/keep.txt") == "changed",
        )
        assert_true(
            "tracked goal-flight file is back to HEAD after reset",
            (reused / ".goal-flight" / "keep.txt").read_text(encoding="utf-8") == "keep\n",
        )


def test_hwm_stays_after_release() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "hwm-a", controller_label="lab"
        )
        second = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "hwm-b", controller_label="lab"
        )
        assert_true("grew to two", {first.path.name, second.path.name} == {"s-1", "s-2"})
        finish_seat_holder(first)
        finish_seat_holder(second)
        third = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "hwm-c", controller_label="lab"
        )
        try:
            assert_true("reuses existing seat", third.path.name in {"s-1", "s-2"})
            assert_true("does not mint s-3", not (third.path.parent / "s-3").exists())
            assert_true("captive pair remains", len(pooled_worktrees(repo)) == 2)
        finally:
            third.release()


def _rewrite_seat_holder_as_dead(repo: Path, seat_name: str, dispatch_id: str) -> None:
    """Simulate a released seat whose recorded native identity is proven dead."""
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, seat_name)
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    payload["dispatch_id"] = dispatch_id
    payload["pid"] = 2147483647
    payload["start_token"] = "exited-test-worker"
    lock_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _registry_inode(repo: Path, lock_path: Path) -> int | None:
    registry = goalflight_worktree_pool._lock_registry_path(
        goalflight_worktree_pool._git_common_dir(repo)
    )
    payload = json.loads(registry.read_text(encoding="utf-8"))
    record = payload["locks"].get(goalflight_worktree_pool._lock_registry_key(lock_path))
    if not isinstance(record, dict):
        return None
    return int(record["st_ino"])


def test_gc_removed_seat_is_recreated_at_capacity_for_dead_holder() -> None:
    """A dead HWM seat can be rebuilt after GC removes its checkout locks."""
    import goalflight_worktree_gc

    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "gc-removed")
        removed = first.path
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            removed, "gc-removed"
        )
        occupancy.release()
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(removed)
        stale_key = goalflight_worktree_pool._lock_registry_key(lock_path)
        assert _registry_inode(repo, lock_path) == lock_path.stat().st_ino
        first.release()
        record_finished_holder("gc-removed", project_root=repo)
        assert goalflight_worktree_gc.main(
            [str(repo), "--into", "main", "--apply", "--json"]
        ) == 0
        assert not removed.exists()
        registry = goalflight_worktree_pool._lock_registry_path(
            goalflight_worktree_pool._git_common_dir(repo)
        )
        payload = json.loads(registry.read_text(encoding="utf-8"))
        assert stale_key not in payload["locks"]

        with goalflight_worktree_pool.acquire_worktree_seat(
            repo, "after-gc"
        ) as recreated:
            assert recreated.path == removed
        assert removed.is_dir()
        assert not (goalflight_worktree_pool.repository_worktree_root(repo) / "s-2").exists()
        lock_root = goalflight_worktree_pool._seat_lock_root(repo)
        assert goalflight_worktree_pool._ring_state_path(lock_root).is_file()
        assert goalflight_worktree_pool._read_ring_hwm(lock_root) == 1
        assert not (lock_root / "s-2.lock").exists()


def _remove_checkout_keep_registration(repo: Path, seat: Path) -> tuple[Path, int]:
    """Delete the checkout and its git admin dir. Leave the registry key."""
    lock_path = goalflight_worktree_pool.worktree_path_lock_path(seat)
    registered = _registry_inode(repo, lock_path)
    assert registered == lock_path.stat().st_ino
    admin = lock_path.parent
    shutil.rmtree(seat)
    shutil.rmtree(admin)
    assert not seat.exists()
    assert not lock_path.exists()
    assert _registry_inode(repo, lock_path) == registered
    return lock_path, registered


def test_stale_registration_skips_slot_and_admits_next() -> None:
    """A stale occupancy registration skips that slot and admits a present seat.

    The checkout and admin dir are gone; the registry key remains. The
    allocator does not recreate the slot or the lock. A seat that is already
    present is still allocated. At f293f97c the same state recreates s-1.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "stale-holder")
        stale_path = first.path
        present = goalflight_worktree_pool.acquire_worktree_seat(repo, "present-seat")
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            stale_path, "stale-holder"
        )
        occupancy.release()
        finish_seat_holder(first)
        finish_seat_holder(present)
        lock_path, registered = _remove_checkout_keep_registration(repo, stale_path)

        nxt = goalflight_worktree_pool.acquire_worktree_seat(repo, "after-stale")
        try:
            assert nxt.path.name == "s-2"
            assert not stale_path.exists()
            assert not lock_path.exists()
            assert _registry_inode(repo, lock_path) == registered
            for name, reason in nxt.skipped_slots:
                if name == "s-1":
                    assert "repo_not_clean" in reason
                    assert "--drop-stale" in reason
        finally:
            nxt.release()


def test_stale_registration_is_never_admitted() -> None:
    """Repeated allocation never makes a stale slot admissible by itself.

    At f293f97c the first retry recreates the slot.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "stale-only")
        stale_path = first.path
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            stale_path, "stale-only"
        )
        occupancy.release()
        first.release()
        lock_path, registered = _remove_checkout_keep_registration(repo, stale_path)

        for dispatch_id in ("stale-retry-1", "stale-retry-2"):
            with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as caught:
                goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
            assert any(
                name == "s-1" and "repo_not_clean" in reason and "--drop-stale" in reason
                for name, reason in caught.value.skipped_slots
            )
            assert "repo_not_clean" in str(caught.value)
            assert "--drop-stale" in str(caught.value)
            assert not stale_path.exists()
            assert not lock_path.exists()
            assert _registry_inode(repo, lock_path) == registered


def test_open_occupancy_inode_is_not_replaced() -> None:
    """An open occupancy inode is not replaced when its directory entry is gone.

    The missing registered file keeps the repository unclean, so the next
    absent slot is not created either.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "open-fd")
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(holder.path)
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            holder.path, "open-fd"
        )
        held_inode = os.fstat(occupancy.fileno()).st_ino
        assert _registry_inode(repo, lock_path) == held_inode
        holder.release()
        lock_path.unlink()
        _rewrite_seat_holder_as_dead(repo, "s-1", "open-fd")
        try:
            with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
                goalflight_worktree_pool.acquire_worktree_seat(repo, "other-seat")
            assert not (repo / "worktrees" / "s-2").exists()
            assert not lock_path.exists()
            assert _registry_inode(repo, lock_path) == held_inode
            assert os.fstat(occupancy.fileno()).st_ino == held_inode
        finally:
            occupancy.release()


def test_unproven_missing_occupancy_lock_is_skipped_not_admitted() -> None:
    """A missing registered occupancy lock is not recreated.

    The registry inode stays. The repository is not clean, so the next absent
    slot is not created and the dirty checkout is left in place.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "still-live")
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(first.path)
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            first.path, "still-live"
        )
        occupancy.release()
        registered = _registry_inode(repo, lock_path)
        assert registered == lock_path.stat().st_ino
        first.release()
        lock_path.unlink()
        (first.path / "dirty.txt").write_text("keep\n", encoding="utf-8")

        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
            goalflight_worktree_pool.acquire_worktree_seat(repo, "next-seat")
        assert not (repo / "worktrees" / "s-2").exists()
        assert not lock_path.exists()
        assert _registry_inode(repo, lock_path) == registered
        assert (first.path / "dirty.txt").read_text(encoding="utf-8") == "keep\n"


def test_gc_registration_drop_failure_keeps_the_worktree() -> None:
    """A drop that fails leaves the checkout and the registry key in place.

    The process exits non-zero so the next pass still sees the worktree.
    """
    import goalflight_worktree_gc

    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "gc-drop-fail")
        removed = first.path
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            removed, "gc-drop-fail"
        )
        occupancy.release()
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(removed)
        registered = _registry_inode(repo, lock_path)
        assert registered == lock_path.stat().st_ino
        first.release()
        record_finished_holder("gc-drop-fail", project_root=repo)
        original = goalflight_worktree_pool._drop_occupancy_registration
        called = {"n": 0}

        def boom(*_args, **_kwargs):
            called["n"] += 1
            raise OSError("injected drop failure")

        goalflight_worktree_pool._drop_occupancy_registration = boom
        try:
            code = goalflight_worktree_gc.main(
                [str(repo), "--into", "main", "--apply", "--json"]
            )
            assert code != 0
            assert called["n"] == 1
            assert removed.exists()
            assert lock_path.is_file()
            assert _registry_inode(repo, lock_path) == registered
        finally:
            goalflight_worktree_pool._drop_occupancy_registration = original


def test_admin_name_collision_is_skipped() -> None:
    """A slot whose git admin dir is s-51 is skipped, not guessed as s-5.

    Git appends a numeric suffix when the basename admin dir already exists.
    The allocator reads gitdir files. At f293f97c prune removes the prunable
    admin dir and the next allocation recreates worktrees/s-5.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(6):
        repo = make_repo(Path(td))
        held = [
            goalflight_worktree_pool.acquire_worktree_seat(repo, f"hold-{index}")
            for index in range(1, 5)
        ]
        try:
            common = goalflight_worktree_pool._git_common_dir(repo)
            (common / "worktrees" / "s-5").mkdir(parents=True)
            seed = repo / "worktrees" / "s-5"
            git(
                repo,
                "worktree",
                "add",
                "-b",
                "worktree/collision-seed",
                str(seed),
                "HEAD",
            )
            gitdir_text = (seed / ".git").read_text(encoding="utf-8")
            assert "/worktrees/s-51" in gitdir_text
            occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
                seed, "collision-seed"
            )
            occupancy.release()
            lock_path = goalflight_worktree_pool.worktree_path_lock_path(seed)
            assert lock_path.parent.name == "s-51"
            shutil.rmtree(seed)
            assert (common / "worktrees" / "s-51" / "gitdir").is_file()
            guessed = (
                common
                / "worktrees"
                / "s-5"
                / goalflight_worktree_pool.OCCUPANCY_LOCK_NAME
            )
            with pytest.raises(
                goalflight_worktree_pool.WorktreeSeatUnavailable
            ) as caught:
                goalflight_worktree_pool.acquire_worktree_seat(
                    repo, "after-collision"
                )
            assert not seed.exists()
            assert not (repo / "worktrees" / "s-6").exists()
            assert (common / "worktrees" / "s-51" / "gitdir").is_file()
            assert not guessed.exists()
            assert any(
                name == "s-5"
                and "admin_dir" in reason
                and "s-51" in reason
                and "--drop-stale" in reason
                for name, reason in caught.value.skipped_slots
            )
            released = held.pop()
            released_path = released.path
            finish_seat_holder(released)
            nxt = goalflight_worktree_pool.acquire_worktree_seat(
                repo, "reuse-present"
            )
            try:
                assert nxt.path == released_path
                assert not seed.exists()
                assert not (repo / "worktrees" / "s-6").exists()
                assert (common / "worktrees" / "s-51" / "gitdir").is_file()
                assert not guessed.exists()
            finally:
                nxt.release()
        finally:
            for lease in held:
                lease.release()


def test_occupied_seat_is_still_refused() -> None:
    """A live occupancy flock refuses that seat.

    Preservation control: the same refusal holds at f293f97c.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "live-occupancy")
        branch = git(holder.path, "rev-parse", "--abbrev-ref", "HEAD")
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            holder.path, "live-occupancy"
        )
        try:
            holder.release()
            with pytest.raises(
                goalflight_worktree_pool.WorktreeSeatUnavailable
            ) as caught:
                goalflight_worktree_pool.acquire_worktree_seat(repo, "intruder")
            assert git(holder.path, "rev-parse", "--abbrev-ref", "HEAD") == branch
            assert ("s-1", "stale_registration") not in getattr(
                caught.value, "skipped_slots", ()
            )
        finally:
            occupancy.release()


def test_nonmanaged_branch_does_not_create_occupancy_lock() -> None:
    """A dead seat on a non-managed branch stays missing its occupancy lock.

    Preservation control: no lock is created. At f293f97c the repair path
    does not exist, so the file stays absent there too.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "topic-holder")
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(holder.path)
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            holder.path, "topic-holder"
        )
        occupancy.release()
        registered = _registry_inode(repo, lock_path)
        git(holder.path, "checkout", "-b", "topic")
        holder.release()
        lock_path.unlink()
        _rewrite_seat_holder_as_dead(repo, "s-1", "topic-holder")
        for dispatch_id in ("first-try", "second-try"):
            with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
                goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
            assert not lock_path.exists()
            assert _registry_inode(repo, lock_path) == registered


def test_gc_lock_path_inspection_error_is_a_failure() -> None:
    """A lock-path inspection error does not remove the checkout.

    At 3a78ff76 the error becomes "no lock", removal proceeds, and the
    registry key is left behind with exit 0.
    """
    import goalflight_worktree_gc

    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "gc-inspect")
        removed = first.path
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            removed, "gc-inspect"
        )
        occupancy.release()
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(removed)
        registered = _registry_inode(repo, lock_path)
        first.release()
        record_finished_holder("gc-inspect", project_root=repo)
        original = goalflight_worktree_pool.worktree_path_lock_path

        def boom(_target):
            raise goalflight_worktree_pool.WorktreeSeatError(
                "injected path inspection failure"
            )

        goalflight_worktree_pool.worktree_path_lock_path = boom
        try:
            code = goalflight_worktree_gc.main(
                [str(repo), "--into", "main", "--apply", "--json"]
            )
        finally:
            goalflight_worktree_pool.worktree_path_lock_path = original
        assert code != 0
        assert removed.exists()
        assert lock_path.is_file()
        assert _registry_inode(repo, lock_path) == registered


def test_gc_deadline_after_registration_drop_is_a_failure() -> None:
    """A remove that times out after the registry drop is a counted failure.

    The checkout and lock file stay, the key is gone, and the next allocation
    adopts that file. At 3a78ff76 the same timeout exits 0.
    """
    import goalflight_worktree_gc

    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "gc-deadline")
        removed = first.path
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            removed, "gc-deadline"
        )
        occupancy.release()
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(removed)
        registered = _registry_inode(repo, lock_path)
        assert registered == lock_path.stat().st_ino
        first.release()
        record_finished_holder("gc-deadline", project_root=repo)
        original = goalflight_worktree_gc._remove_worktree

        def expired(_repo, _path, *, deadline=None):
            return False, "git worktree remove deadline expired; retaining checkout"

        goalflight_worktree_gc._remove_worktree = expired
        try:
            code = goalflight_worktree_gc.main(
                [str(repo), "--into", "main", "--apply", "--json"]
            )
        finally:
            goalflight_worktree_gc._remove_worktree = original
        assert code != 0
        assert removed.exists()
        assert lock_path.is_file()
        assert _registry_inode(repo, lock_path) is None
        adopted = goalflight_worktree_pool.acquire_worktree_seat(repo, "after-deadline")
        try:
            assert adopted.path.name == "s-1"
            assert _registry_inode(repo, lock_path) == lock_path.stat().st_ino
        finally:
            adopted.release()


def test_suffix_collision_skip_survives_later_allocation() -> None:
    """A later acquire must still skip a slot whose suffixed admin dir remains.

    The acquire that takes another seat used to run ``git worktree prune`` and
    delete that admin dir. The third acquire then created the slot beside the
    inode a holder still had open.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(8):
        repo = make_repo(Path(td))
        held = [
            goalflight_worktree_pool.acquire_worktree_seat(repo, f"hold-{index}")
            for index in range(1, 5)
        ]
        occupancy = None
        first = None
        second = None
        third = None
        try:
            common = goalflight_worktree_pool._git_common_dir(repo)
            (common / "worktrees" / "s-5").mkdir(parents=True)
            seed = repo / "worktrees" / "s-5"
            git(
                repo,
                "worktree",
                "add",
                "-b",
                "worktree/collision-seed",
                str(seed),
                "HEAD",
            )
            assert "/worktrees/s-51" in (seed / ".git").read_text(encoding="utf-8")
            occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
                seed, "collision-seed"
            )
            held_inode = os.fstat(occupancy.fileno()).st_ino
            lock_path = goalflight_worktree_pool.worktree_path_lock_path(seed)
            assert lock_path.parent.name == "s-51"
            shutil.rmtree(seed)
            admin_gitdir = common / "worktrees" / "s-51" / "gitdir"
            assert admin_gitdir.is_file()

            def stale_skip(slots):
                return any(
                    name == "s-5"
                    and "admin_dir" in reason
                    and "s-51" in reason
                    and "--drop-stale" in reason
                    for name, reason in slots
                )

            with pytest.raises(
                goalflight_worktree_pool.WorktreeSeatUnavailable
            ) as first_caught:
                goalflight_worktree_pool.acquire_worktree_seat(
                    repo, "after-collision"
                )
            assert stale_skip(first_caught.value.skipped_slots)
            assert admin_gitdir.is_file()
            assert not seed.exists()
            assert os.fstat(occupancy.fileno()).st_ino == held_inode

            with pytest.raises(
                goalflight_worktree_pool.WorktreeSeatUnavailable
            ) as second_caught:
                goalflight_worktree_pool.acquire_worktree_seat(
                    repo, "second-acquire"
                )
            assert stale_skip(second_caught.value.skipped_slots)
            assert admin_gitdir.is_file()

            released = held.pop()
            released_path = released.path
            finish_seat_holder(released)
            third = goalflight_worktree_pool.acquire_worktree_seat(
                repo, "third-acquire"
            )
            assert third.path == released_path
            assert third.path.name != "s-5"
            assert not seed.exists()
            assert not (repo / "worktrees" / "s-6").exists()
            assert admin_gitdir.is_file()
            assert os.fstat(occupancy.fileno()).st_ino == held_inode
            assert _registry_inode(repo, lock_path) == held_inode
        finally:
            for lease in (third, second, first):
                if lease is not None:
                    lease.release()
            if occupancy is not None:
                occupancy.release()
            for lease in held:
                lease.release()


def test_registered_worktree_add_refusal_skips_slot() -> None:
    """Git's 'already registered worktree' refusal skips that slot.

    The pre-check is bypassed so ``worktree add`` is what refuses. Prune used
    to delete the registration first and the add then succeeded.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(6), pytest.MonkeyPatch.context() as monkeypatch:
        repo = make_repo(Path(td))
        held = [
            goalflight_worktree_pool.acquire_worktree_seat(repo, f"hold-{index}")
            for index in range(1, 5)
        ]
        try:
            common = goalflight_worktree_pool._git_common_dir(repo)
            (common / "worktrees" / "s-5").mkdir(parents=True)
            seed = repo / "worktrees" / "s-5"
            git(
                repo,
                "worktree",
                "add",
                "-b",
                "worktree/registered-seed",
                str(seed),
                "HEAD",
            )
            assert "/worktrees/s-51" in (seed / ".git").read_text(encoding="utf-8")
            shutil.rmtree(seed)
            admin_gitdir = common / "worktrees" / "s-51" / "gitdir"
            assert admin_gitdir.is_file()
            monkeypatch.setattr(
                goalflight_worktree_pool,
                "_repo_clean_report",
                lambda project_root: goalflight_worktree_pool._RepoCleanReport(
                    [], repo=project_root
                ),
            )
            nxt = goalflight_worktree_pool.acquire_worktree_seat(
                repo, "after-registered-add"
            )
            try:
                assert nxt.path.name == "s-6"
                assert not seed.exists()
                assert admin_gitdir.is_file()
                assert any(
                    name == "s-5"
                    and "git " in reason
                    and "admin_dir:" not in reason
                    for name, reason in nxt.skipped_slots
                )
            finally:
                nxt.release()
        finally:
            for lease in held:
                lease.release()


def test_unreadable_gitdir_skips_only_candidate_slots() -> None:
    """An unreadable gitdir makes the repository not clean.

    Absent slots stay absent. The skip names every unreadable admin directory.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(3):
        repo = make_repo(Path(td))
        common = goalflight_worktree_pool._git_common_dir(repo)
        worktrees = common / "worktrees"
        junk_gitdir = worktrees / "junk" / "gitdir"
        slot_gitdir = worktrees / "s-1" / "gitdir"
        junk_gitdir.parent.mkdir(parents=True)
        slot_gitdir.parent.mkdir(parents=True)
        junk_gitdir.write_text("not a checkout\n", encoding="utf-8")
        slot_gitdir.write_text("not a checkout\n", encoding="utf-8")
        junk_gitdir.chmod(0)
        slot_gitdir.chmod(0)
        try:
            with pytest.raises(
                goalflight_worktree_pool.WorktreeSeatUnavailable
            ) as caught:
                goalflight_worktree_pool.acquire_worktree_seat(
                    repo, "narrow-unknown"
                )
            assert not (repo / "worktrees" / "s-1").exists()
            assert not (repo / "worktrees" / "s-2").exists()
            assert not (repo / "worktrees" / "s-3").exists()
            text = str(caught.value)
            assert "repo_not_clean" in text
            assert "--drop-stale" not in text
            assert "operator must inspect" in text
            assert "gitdir_unreadable" in text
            assert "junk" in text
            assert "s-1" in text
        finally:
            junk_gitdir.chmod(0o644)
            slot_gitdir.chmod(0o644)


def test_unknown_path_skip_reaches_success_and_exhaustion() -> None:
    """An unreadable seat path is a skip on success and on the capacity refusal.

    ``_path_presence`` reports unknown for ``s-1``. The rest of the pool is
    still allocated, and a one-seat cap names that skip instead of dropping it.
    """
    real = goalflight_worktree_pool._path_presence

    def wrapped(path: Path) -> str:
        if Path(path).name == "s-1":
            return "unknown"
        return real(path)

    with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(goalflight_worktree_pool, "_path_presence", wrapped)
        open_root = Path(td) / "open"
        open_root.mkdir()
        with seat_limit(2):
            repo = make_repo(open_root)
            nxt = goalflight_worktree_pool.acquire_worktree_seat(
                repo, "after-unknown"
            )
            try:
                assert nxt.path.name == "s-2"
                reasons = [
                    reason for name, reason in nxt.skipped_slots if name == "s-1"
                ]
                assert reasons
                assert all(
                    reason.startswith("occupancy_unknown:") and "/s-1" in reason
                    for reason in reasons
                )
                buf = io.StringIO()
                args = SimpleNamespace(
                    dispatch_id="after-unknown",
                    agent="test",
                    shape="bash",
                    dispatch_warnings=[],
                )
                with contextlib.redirect_stderr(buf):
                    goalflight_dispatch._note_skipped_worktree_seats(
                        args, nxt.skipped_slots
                    )
                payload = json.loads(buf.getvalue().splitlines()[0])
                assert payload["skipped_worktree_seats"][0]["seat"] == "s-1"
                assert payload["skipped_worktree_seats"][0]["reason"].startswith(
                    "occupancy_unknown:"
                )
            finally:
                nxt.release()
        full_root = Path(td) / "full"
        full_root.mkdir()
        with seat_limit(1):
            repo = make_repo(full_root)
            with pytest.raises(
                goalflight_worktree_pool.WorktreeSeatUnavailable
            ) as caught:
                goalflight_worktree_pool.acquire_worktree_seat(repo, "unknown-full")
            reasons = [
                reason
                for name, reason in caught.value.skipped_slots
                if name == "s-1"
            ]
            assert reasons
            assert all(
                reason.startswith("occupancy_unknown:") and "/s-1" in reason
                for reason in reasons
            )
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                goalflight_dispatch._emit_skipped_worktree_seat_failure(caught.value)
            payload = json.loads(buf.getvalue().splitlines()[0])
            assert payload["skipped_worktree_seats"][0]["seat"] == "s-1"


def test_reset_refusal_keeps_earlier_skips() -> None:
    """A reset refusal still carries skips accumulated before that seat."""
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "stale-before-reset")
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            first.path, "stale-before-reset"
        )
        occupancy.release()
        first.release()
        second = goalflight_worktree_pool.acquire_worktree_seat(repo, "reset-refused")
        (second.path / ".gitignore").write_text("build/\n", encoding="utf-8")
        git(second.path, "add", ".gitignore")
        git(second.path, "commit", "-m", "ignore build")
        target = repo / "build" / "x"
        target.parent.mkdir()
        target.write_text("target\n", encoding="utf-8")
        git(repo, "add", "build/x")
        git(repo, "commit", "-m", "add build target")
        collision = second.path / "build"
        collision.mkdir()
        (collision / "payload").write_text("must survive\n", encoding="utf-8")
        finish_seat_holder(second)
        _remove_checkout_keep_registration(repo, repo / "worktrees" / "s-1")

        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatResetRefused
        ) as caught:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "after-reset-refusal")
        matched = [reason for name, reason in caught.value.skipped_slots if name == "s-1"]
        assert matched
        assert all(
            "repo_not_clean" in reason and "--drop-stale" in reason for reason in matched
        )
        assert "s-1=" in str(caught.value)
        assert "repo_not_clean" in str(caught.value)
        assert "s-2" in str(caught.value)
        assert (collision / "payload").read_text(encoding="utf-8") == "must survive\n"
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            goalflight_dispatch._emit_skipped_worktree_seat_failure(caught.value)
        payload = json.loads(buf.getvalue().splitlines()[0])
        assert payload["skipped_worktree_seats"][0]["seat"] == "s-1"
        assert "repo_not_clean" in payload["skipped_worktree_seats"][0]["reason"]
        assert "--drop-stale" in payload["skipped_worktree_seats"][0]["reason"]


def test_missing_gitdir_does_not_admit_replacement() -> None:
    """A missing gitdir file is stale. The registered inode is not replaced."""
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "missing-gitdir")
        seat = holder.path
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(seat)
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            seat, "missing-gitdir"
        )
        held_inode = os.fstat(occupancy.fileno()).st_ino
        registered = _registry_inode(repo, lock_path)
        holder.release()
        gitdir = lock_path.parent / "gitdir"
        shutil.rmtree(seat)
        gitdir.unlink()
        try:
            with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as caught:
                goalflight_worktree_pool.acquire_worktree_seat(repo, "replacement")
            message = str(caught.value)
            assert "repo_not_clean" in message
            assert "--drop-stale" not in message
            assert "operator must inspect" in message
            assert "gitdir_missing" in message or "file_missing" in message
            assert not seat.exists()
            assert not (seat.parent / "s-2").exists()
            assert os.fstat(occupancy.fileno()).st_ino == held_inode
            assert _registry_inode(repo, lock_path) == registered
        finally:
            occupancy.release()


def test_moved_worktree_unreadable_gitdir_does_not_admit_destination() -> None:
    """A moved worktree with an unreadable gitdir does not free its admin id."""
    with tempfile.TemporaryDirectory() as td, seat_limit(3):
        repo = make_repo(Path(td))
        holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "moved")
        holder.release()
        dest = repo / "worktrees" / "s-3"
        git(repo, "worktree", "move", str(holder.path), str(dest))
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(dest)
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(dest, "moved")
        held_inode = os.fstat(occupancy.fileno()).st_ino
        registered = _registry_inode(repo, lock_path)
        gitdir = lock_path.parent / "gitdir"
        shutil.rmtree(dest)
        gitdir.chmod(0)
        try:
            with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as caught:
                goalflight_worktree_pool.acquire_worktree_seat(repo, "beside")
            message = str(caught.value)
            assert "repo_not_clean" in message
            assert "--drop-stale" not in message
            assert "operator must inspect" in message
            assert "gitdir_unreadable" in message
            assert not (repo / "worktrees" / "s-1").exists()
            assert not (repo / "worktrees" / "s-2").exists()
            assert not dest.exists()
            assert os.fstat(occupancy.fileno()).st_ino == held_inode
            assert _registry_inode(repo, lock_path) == registered
        finally:
            gitdir.chmod(0o644)
            occupancy.release()


def test_full_pool_skips_absent_slot_and_uses_present_seat() -> None:
    """At capacity, an absent slot is skipped while an existing seat is reusable."""
    with tempfile.TemporaryDirectory() as td, seat_limit(2), pytest.MonkeyPatch.context() as monkeypatch:
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "seed-one")
        second = goalflight_worktree_pool.acquire_worktree_seat(repo, "seed-two")
        first_path = first.path
        second_path = second.path
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(first_path)
        admin = lock_path.parent
        key = goalflight_worktree_pool._lock_registry_key(lock_path)
        finish_seat_holder(first)
        finish_seat_holder(second)
        shutil.rmtree(first_path)
        shutil.rmtree(admin)
        registry = goalflight_worktree_pool._lock_registry_path(
            goalflight_worktree_pool._git_common_dir(repo)
        )
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["locks"].pop(key, None)
        registry.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        nxt = goalflight_worktree_pool.acquire_worktree_seat(repo, "use-present")
        try:
            assert nxt.path == second_path
            assert not first_path.exists()
        finally:
            nxt.release()


def test_pinned_allocation_keeps_discovered_skips() -> None:
    """A pinned allocation keeps skips found while the allocation lock was held."""
    real = goalflight_worktree_pool._path_presence

    def wrapped(path: Path) -> str:
        if Path(path).name == "s-1":
            return "unknown"
        return real(path)

    with tempfile.TemporaryDirectory() as td, seat_limit(2), pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(goalflight_worktree_pool, "_path_presence", wrapped)
        repo = make_repo(Path(td))
        created = goalflight_worktree_pool.acquire_worktree_seat(repo, "create-s2")
        try:
            assert created.path.name == "s-2"
        finally:
            finish_seat_holder(created)
        pinned = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "pin-s2", occupy_path=created.path, reset=False
        )
        try:
            assert pinned.path == created.path
            assert any(
                name == "s-1" and reason.startswith("occupancy_unknown:")
                for name, reason in pinned.skipped_slots
            )
        finally:
            pinned.release()


def test_drop_stale_removes_one_item_and_refuses_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator action drops one stale item and refuses a live one."""
    import goalflight_worktree_gc

    help_out = io.StringIO()
    with contextlib.redirect_stdout(help_out):
        with pytest.raises(SystemExit) as caught:
            goalflight_worktree_gc.main(["--help"])
    assert caught.value.code == 0
    help_text = help_out.getvalue()
    assert "--drop-stale" in help_text
    assert "after a reboot" in help_text

    def run_drop(repo: Path, item: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = goalflight_worktree_gc.main([str(repo), "--drop-stale", item])
        return code, out.getvalue(), err.getvalue()

    monkeypatch.setenv("GOALFLIGHT_WORKTREE_COMPLETE_RETENTION_GRACE_S", "3153600000")
    with tempfile.TemporaryDirectory() as td, seat_limit(3):
        repo = make_repo(Path(td))
        seats = [
            goalflight_worktree_pool.acquire_worktree_seat(repo, f"drop-{index}")
            for index in range(1, 4)
        ]
        locks = [
            goalflight_worktree_pool.worktree_path_lock_path(seat.path) for seat in seats
        ]
        for seat in seats:
            occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
                seat.path, seat.dispatch_id
            )
            occupancy.release()
        admins = [lock.parent for lock in locks]
        keys = [goalflight_worktree_pool._lock_registry_key(lock) for lock in locks]
        for seat in seats:
            finish_seat_holder_at(
                seat, state="complete", ended_at="2026-01-01T00:00:00Z"
            )
        shutil.rmtree(seats[0].path)
        shutil.rmtree(seats[1].path)
        assert admins[0].is_dir() and admins[1].is_dir() and seats[2].path.is_dir()

        code, out, _err = run_drop(repo, str(admins[0]))
        assert code == 0, out
        assert not admins[0].exists()
        assert admins[1].is_dir()
        assert seats[2].path.is_dir()
        registry = goalflight_worktree_pool._lock_registry_path(
            goalflight_worktree_pool._git_common_dir(repo)
        )
        payload = json.loads(registry.read_text(encoding="utf-8"))
        assert keys[0] not in payload["locks"]
        assert keys[1] in payload["locks"]
        assert keys[2] in payload["locks"]

        code, _out, err = run_drop(repo, str(admins[2]))
        assert code == 1
        assert seats[2].path.is_dir()
        assert admins[2].is_dir()
        assert "still exists" in err
        payload = json.loads(registry.read_text(encoding="utf-8"))
        assert keys[2] in payload["locks"]

        code, _out, err = run_drop(repo, str(locks[1]))
        assert code == 1
        assert locks[1].is_file()
        assert "still exists" in err
        payload = json.loads(registry.read_text(encoding="utf-8"))
        assert keys[1] in payload["locks"]

        shutil.rmtree(admins[1])
        assert not locks[1].exists()
        code, out, err = run_drop(repo, str(locks[1]))
        assert code == 0, err
        payload = json.loads(registry.read_text(encoding="utf-8"))
        assert keys[1] not in payload["locks"]
        assert keys[2] in payload["locks"]
        assert seats[2].path.is_dir()

        busy = goalflight_worktree_pool.acquire_worktree_seat(repo, "hold-live")
        try:
            assert busy.path == seats[2].path
            with goalflight_worktree_pool.acquire_worktree_seat(
                repo, "recreated"
            ) as recreated:
                assert recreated.path in {seats[0].path, seats[1].path}
            assert seats[2].path.is_dir()
        finally:
            busy.release()


def test_drop_stale_refuses_unproven_admin_and_symlink() -> None:
    """Unknown checkout location and a symlinked admin are not droppable.

    A missing or empty gitdir does not prove the checkout is absent. A
    symlink under .git/worktrees is not clean and is left in place.
    """
    import goalflight_worktree_gc

    def run_drop(repo: Path, item: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = goalflight_worktree_gc.main([str(repo), "--drop-stale", item])
        return code, out.getvalue(), err.getvalue()

    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "present-gitdir")
        seat = holder.path
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(seat)
        admin = lock_path.parent
        gitdir = admin / "gitdir"
        holder.release()
        gitdir.unlink()
        code, _out, err = run_drop(repo, str(admin))
        assert code == 1, err
        assert "proven missing" in err
        assert admin.is_dir()
        assert seat.is_dir()
        assert "--drop-stale" not in goalflight_worktree_pool._repo_clean_report(repo).reason

        gitdir.write_text("\n", encoding="utf-8")
        shutil.rmtree(seat)
        code, _out, err = run_drop(repo, str(admin))
        assert code == 1, err
        assert "proven missing" in err
        assert admin.is_dir()
        assert not seat.exists()

        common = goalflight_worktree_pool._git_common_dir(repo)
        outside = Path(td) / "outside-admin"
        outside.mkdir()
        (outside / "gitdir").write_text(
            str(Path(td) / "outside-checkout" / ".git") + "\n", encoding="utf-8"
        )
        link = common / "worktrees" / "linked"
        link.symlink_to(outside, target_is_directory=True)
        report = goalflight_worktree_pool._repo_clean_report(repo)
        assert not report.clean
        assert any(
            item.startswith("admin_dir symlink ") and str(link) in item
            for item in report.items
        )
        assert "--drop-stale" not in report.reason
        assert "operator must inspect" in report.reason
        assert str(link) in report.reason
        code, _out, err = run_drop(repo, str(link))
        assert code == 1, err
        assert link.is_symlink()
        assert outside.is_dir()
        assert (outside / "gitdir").is_file()


def test_clean_recheck_blocks_admin_that_appears_before_add() -> None:
    """A stale admin that appears after the scan snapshot blocks creation.

    The mutation lock stays held across git worktree add and is released
    before checkout. The admission lock does not stay held: a zero wait
    budget must still fail fast when that lock is busy.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(1), pytest.MonkeyPatch.context() as monkeypatch:
        repo = make_repo(Path(td))
        common = goalflight_worktree_pool._git_common_dir(repo)
        worktrees = common / "worktrees"
        real_report = goalflight_worktree_pool._repo_clean_report
        state = {"calls": 0}

        def wrapped(root: Path):
            state["calls"] += 1
            if state["calls"] >= 2:
                (worktrees / "appeared").mkdir(parents=True, exist_ok=True)
            return real_report(root)

        monkeypatch.setattr(goalflight_worktree_pool, "_repo_clean_report", wrapped)
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as caught:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "after-appear")
        assert "repo_not_clean" in str(caught.value)
        assert not (repo / "worktrees" / "s-1").exists()
        assert (worktrees / "appeared").is_dir()

    with tempfile.TemporaryDirectory() as td, seat_limit(1), pytest.MonkeyPatch.context() as monkeypatch:
        repo = make_repo(Path(td))
        lock_root = goalflight_worktree_pool._seat_lock_root(repo)
        mutation_path = lock_root / "worktree-mutation.lock"
        admission_path = lock_root / "allocation.lock"
        real_git = goalflight_worktree_pool._git
        observed = {
            "mutation": None,
            "admission": None,
            "checkout_mutation": None,
        }

        def probe(path: Path) -> bool:
            fd = os.open(path, os.O_RDWR)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            finally:
                os.close(fd)

        def wrapped(cwd: Path, *args: str, **kwargs):
            if args[:2] == ("worktree", "add") and observed["mutation"] is None:
                observed["mutation"] = probe(mutation_path)
                observed["admission"] = probe(admission_path)
            if args[:1] == ("checkout",) or (
                len(args) >= 3 and args[0] == "-c" and args[2] == "checkout"
            ):
                if observed["checkout_mutation"] is None:
                    observed["checkout_mutation"] = probe(mutation_path)
            return real_git(cwd, *args, **kwargs)

        monkeypatch.setattr(goalflight_worktree_pool, "_git", wrapped)
        lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "lock-held-add")
        try:
            assert lease.path.name == "s-1"
            assert (lease.path / "tracked.txt").is_file()
        finally:
            lease.release()
        assert observed["mutation"] is True
        assert observed["admission"] is False
        assert observed["checkout_mutation"] is False


def test_failed_create_releases_mutation_before_admission_lock() -> None:
    """A failed absent create drops mutation before the next admission attempt.

    An admission holder may then wait for mutation. Retaking admission while
    the failed create still holds mutation would invert that lock order.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        pool = goalflight_worktree_pool
        started = threading.Event()
        admission_held = threading.Event()
        original = pool._create_seat_worktree
        holder_result: dict = {}

        def fail_add(*_args, **_kwargs):
            started.set()
            assert admission_held.wait(timeout=5), "admission holder did not arrive"
            raise pool.WorktreeSeatUnavailable(
                "worktree s-1 skipped (injected); refusing recreate",
                seat_name="s-1",
                reason="injected",
            )

        def hold_admission_then_mutation() -> None:
            try:
                assert started.wait(timeout=5), "create did not reach add"
                with pool._hold_worktree_allocation_lock(repo):
                    admission_held.set()
                    time.sleep(0.2)
                    deadline = time.monotonic() + 2.0
                    with pool._hold_worktree_mutation_lock(repo, deadline=deadline):
                        holder_result["got_mutation"] = True
            except Exception as exc:
                holder_result["error"] = repr(exc)
                holder_result.setdefault("got_mutation", False)

        pool._create_seat_worktree = fail_add
        holder = threading.Thread(target=hold_admission_then_mutation)
        holder.start()
        try:
            with pytest.raises(pool.WorktreeSeatError):
                pool.acquire_worktree_seat(repo, "create-fail")
        finally:
            pool._create_seat_worktree = original
            holder.join(timeout=8)
        assert not holder.is_alive(), "failed create deadlocked with the admission holder"
        assert holder_result.get("got_mutation") is True, holder_result


def test_present_checkout_without_occupant_names_an_operator_action() -> None:
    """A present seat with no dispatch branch is named for inspection.

    It stays counted, and the refusal does not offer a removal command.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        seat = repo / "worktrees" / "s-1"
        seat.parent.mkdir()
        git(repo, "worktree", "add", "--detach", "-q", str(seat), "HEAD")
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as caught:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "stuck-empty")
        text = str(caught.value)
        assert "operator must inspect" in text
        assert "s-1" in text
        assert "--drop-stale" not in text
        assert seat.is_dir()
        assert not (repo / "worktrees" / "s-2").exists()


def test_branch_owner_after_add_refusal_skips_slot() -> None:
    """A branch owner discovered after git refuses the add skips that slot."""
    with tempfile.TemporaryDirectory() as td, seat_limit(2), pytest.MonkeyPatch.context() as monkeypatch:
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "seed-absent")
        second = goalflight_worktree_pool.acquire_worktree_seat(repo, "seed-present")
        first_path = first.path
        second_path = second.path
        (second_path / "ahead.txt").write_text("ahead\n", encoding="utf-8")
        git(second_path, "add", "ahead.txt")
        git(second_path, "commit", "-m", "ahead of base")
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(first_path)
        admin = lock_path.parent
        key = goalflight_worktree_pool._lock_registry_key(lock_path)
        seat_lock = goalflight_worktree_pool.worktree_seat_lock_path(repo, first_path.name)
        seat_key = goalflight_worktree_pool._lock_registry_key(seat_lock)
        finish_seat_holder(first)
        finish_seat_holder(second)
        shutil.rmtree(first_path)
        shutil.rmtree(admin)
        seat_lock.unlink()
        registry = goalflight_worktree_pool._lock_registry_path(
            goalflight_worktree_pool._git_common_dir(repo)
        )
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["locks"].pop(key, None)
        payload["locks"].pop(seat_key, None)
        registry.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        real_git = goalflight_worktree_pool._git
        real_owner = goalflight_worktree_pool._worktree_checking_out_branch
        saw_add = {"hit": False}

        def wrapped_git(cwd: Path, *args: str, **kwargs):
            if args[:2] == ("worktree", "add") and any(
                str(arg).endswith("/s-1") or str(arg).endswith("s-1") for arg in args
            ):
                saw_add["hit"] = True
                raise goalflight_worktree_pool.WorktreeSeatError(
                    "fatal: refusing to add s-1"
                )
            return real_git(cwd, *args, **kwargs)

        def wrapped_owner(root: Path, branch: str):
            if saw_add["hit"]:
                return second_path
            return real_owner(root, branch)

        monkeypatch.setattr(goalflight_worktree_pool, "_git", wrapped_git)
        monkeypatch.setattr(
            goalflight_worktree_pool, "_worktree_checking_out_branch", wrapped_owner
        )
        nxt = goalflight_worktree_pool.acquire_worktree_seat(repo, "skip-owned-branch")
        try:
            assert saw_add["hit"]
            assert nxt.path == second_path
            assert any(
                name == "s-1" and "dispatch_branch_in_use:s-2" in reason
                for name, reason in nxt.skipped_slots
            ), nxt.skipped_slots
        finally:
            nxt.release()


def test_not_clean_identity_mismatch_has_no_drop_command() -> None:
    """A lock file whose registry inode differs is named for inspection only.

    The file is still present, so its checkout location is not proven absent
    and ``--drop-stale`` must not be offered. A further allocation does not
    open another seat beside it.
    """
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        holder = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "identity-mismatch"
        )
        seat = holder.path
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(seat)
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            seat, "identity-mismatch"
        )
        occupancy.release()
        finish_seat_holder(holder)
        registered = _registry_inode(repo, lock_path)
        assert registered == lock_path.stat().st_ino
        registry = goalflight_worktree_pool._lock_registry_path(
            goalflight_worktree_pool._git_common_dir(repo)
        )
        payload = json.loads(registry.read_text(encoding="utf-8"))
        key = goalflight_worktree_pool._lock_registry_key(lock_path)
        payload["locks"][key]["st_ino"] = registered + 1
        registry.write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )

        report = goalflight_worktree_pool._repo_clean_report(repo)
        assert not report.clean
        assert any("identity_differs" in item and key in item for item in report.items)
        assert "--drop-stale" not in report.reason
        assert "operator must inspect" in report.reason
        assert key in report.reason

        with pytest.raises(OSError) as caught:
            goalflight_worktree_pool.drop_one_stale_worktree_item(repo, str(lock_path))
        assert caught.value.errno == errno.EBUSY
        assert lock_path.is_file()
        assert _registry_inode(repo, lock_path) == registered + 1

        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as refused:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "no-new-seat")
        text = str(refused.value)
        assert "--drop-stale" not in text
        assert "operator must inspect" in text
        assert "identity_differs" in text
        assert seat.is_dir()
        assert not (repo / "worktrees" / "s-2").exists()
        assert lock_path.is_file()
        assert _registry_inode(repo, lock_path) == registered + 1


def test_present_seat_success_reports_not_clean_ordinary_and_pinned() -> None:
    """A present seat taken from a not-clean repo still reports that reason."""
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "stale-present")
        second = goalflight_worktree_pool.acquire_worktree_seat(repo, "free-present")
        stale_path = first.path
        present_path = second.path
        finish_seat_holder(first)
        finish_seat_holder(second)
        shutil.rmtree(stale_path)
        report = goalflight_worktree_pool._repo_clean_report(repo)
        assert not report.clean
        assert any("checkout_missing" in item for item in report.items)

        ordinary = goalflight_worktree_pool.acquire_worktree_seat(repo, "take-present")
        try:
            assert ordinary.path == present_path
            assert any(
                "repo_not_clean" in reason and "--drop-stale" in reason
                for _name, reason in ordinary.skipped_slots
            ), ordinary.skipped_slots
        finally:
            ordinary.release()

        finish_seat_holder(ordinary)

        pinned = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "pin-present", occupy_path=present_path, reset=False
        )
        try:
            assert pinned.path == present_path
            assert any(
                "repo_not_clean" in reason and "--drop-stale" in reason
                for _name, reason in pinned.skipped_slots
            ), pinned.skipped_slots
        finally:
            pinned.release()



def test_reused_seat_base_switch_removes_outgoing_files_after_cow_index_prime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        older_base = git(repo, "rev-parse", "HEAD")
        dropped_paths = [f"outgoing/{index:02}.txt" for index in range(24)]
        for path in dropped_paths:
            target = repo / path
            target.parent.mkdir(exist_ok=True)
            target.write_text(f"outgoing {path}\n", encoding="utf-8")
        git(repo, "add", "outgoing")
        git(repo, "commit", "-m", "add outgoing files")
        outgoing_base = git(repo, "rev-parse", "HEAD")

        previous = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "base-switch-outgoing", base=outgoing_base
        )
        seat = previous.path
        finish_seat_holder(previous)

        monkeypatch.setattr(
            goalflight_worktree_pool,
            "_cow_prepare_outside_locks",
            lambda *_args, **_kwargs: (True, None, "/*\n"),
        )
        primed: list[str] = []

        def prime_target_template(
            _project_root: Path,
            worktree_path: Path,
            *,
            base_commit: str,
            **_kwargs,
        ) -> dict:
            # This stub models only the index reset. It does not overlay a
            # template payload or configure sparse checkout, and uses the
            # requested base where real priming reads generation.commit.
            primed.append(base_commit)
            git(worktree_path, "read-tree", base_commit)
            return {"clone": "clonefile", "generation": "target-template"}

        monkeypatch.setattr(
            goalflight_seat_cow, "prime_sparse_worktree", prime_target_template
        )

        reused = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "base-switch-older", base=older_base
        )
        try:
            assert reused.path == seat
            assert git(reused.path, "rev-parse", "HEAD") == older_base
            assert primed == []
            assert git(
                reused.path,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ) == ""
            assert all(not (reused.path / path).exists() for path in dropped_paths)
        finally:
            finish_seat_holder(reused)


def test_reused_cow_seat_removes_force_added_ignored_path_after_tracked_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supported, reason, _filesystem = goalflight_seat_cow.clonefile_support(tmp_path)
    if not supported:
        pytest.skip(f"clonefile unavailable: {reason}")

    with seat_limit(1):
        repo = make_repo(tmp_path)
        (repo / ".gitignore").write_text("ignored-force.txt\n", encoding="utf-8")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-m", "ignore force-added fixture")
        base = git(repo, "rev-parse", "HEAD")

        config = write_pool_config(tmp_path, repo)
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["seat_cow_templates"] = {str(repo): True}
        config.write_text(json.dumps(payload), encoding="utf-8")
        configure_pool_test_environment(monkeypatch, tmp_path, config)

        first = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "cow-ignored-first", base=base
        )
        seat = first.path
        first_report = goalflight_seat_cow.read_seat_cow_report(seat)
        assert first_report is not None and first_report["population"] == "cow"
        assert git(seat, "rev-parse", "HEAD") == base
        ignored_path = seat / "ignored-force.txt"
        ignored_path.write_text("force-added ignored content\n", encoding="utf-8")
        git(seat, "add", "-f", "ignored-force.txt")
        assert any(
            "ignored-force.txt" in paths
            for _status, paths in goalflight_worktree_pool._status_records(
                seat, untracked="no"
            )
        )
        finish_seat_holder(first)

        reused = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "cow-ignored-second", base=base
        )
        try:
            assert reused.path == seat
            assert git(reused.path, "rev-parse", "HEAD") == base
            report = goalflight_seat_cow.read_seat_cow_report(reused.path)
            assert not ignored_path.exists(), {
                "report": report,
                "status": goalflight_worktree_pool._status_records(
                    reused.path, untracked="all"
                ),
            }
            assert report is not None
            assert report["cow_skipped"] is True
            assert report["reason"] == "tracked-changes"
            assert goalflight_worktree_pool._status_records(
                reused.path, untracked="all"
            ) == []
        finally:
            finish_seat_holder(reused)



def test_acquire_refusal_names_leftover_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seat_limit(1):
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        previous = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "leftover-path-previous", base=base
        )
        seat = previous.path
        finish_seat_holder(previous)
        prepare = goalflight_worktree_pool._prepare_seat_checkout

        def leave_leftover(path: Path, **kwargs) -> None:
            prepare(path, **kwargs)
            (path / "unexpected checkout output.txt").write_text(
                "leftover\n", encoding="utf-8"
            )

        monkeypatch.setattr(
            goalflight_worktree_pool, "_prepare_seat_checkout", leave_leftover
        )
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatError,
            match="unexpected checkout output[.]txt",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, "leftover-path-next", base=base
            )
        assert seat.name == "s-1"

def main() -> int:
    return pytest.main([str(Path(__file__))])


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
