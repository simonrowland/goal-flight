#!/usr/bin/env python3
"""Acceptance tests for the fixed local worktree seat pool."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("worktree seat leases require POSIX fcntl locks")

import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_worktree_pool
import goalflight_compat
import goalflight_ledger


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
    goalflight_ledger.write_record(
        {
            "dispatch_id": dispatch_id,
            "state": state,
            "worker_cwd": str(path),
            "worktree_path": str(path),
        }
    )


def record_finished_holder(
    dispatch_id: str, identity: dict | None = None, *, state: str = "complete"
) -> None:
    """Record exited synthetic workers separately from releasing allocator locks."""
    identity = identity or {"pid": 2147483647, "start_token": "exited-test-worker"}
    assert goalflight_compat.process_identity_matches(
        identity["pid"], identity["start_token"]
    ) is False
    goalflight_ledger.write_record(
        {
            "dispatch_id": dispatch_id,
            "state": state,
            "worker_pid": identity["pid"],
            "worker_identity": identity,
        }
    )


def finish_seat_holder(lease: goalflight_worktree_pool.WorktreeSeatLease) -> None:
    lease.release()
    record_finished_holder(lease.dispatch_id)


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


def test_full_seat_runs_post_create_cleanup(tmp_path: Path, monkeypatch) -> None:
    repo, base, _pattern = make_sparse_repo(tmp_path)
    config = write_pool_config(tmp_path, repo)
    configure_pool_test_environment(monkeypatch, tmp_path, config)
    real_git = goalflight_worktree_pool._git

    def leave_full_seat_dirty(cwd: Path, *args: str, **kwargs: object) -> str:
        result = real_git(cwd, *args, **kwargs)
        if cwd.resolve() == repo.resolve() and args[:2] == ("worktree", "add"):
            Path(args[-2]).joinpath("leftover.txt").write_text("leftover\n", encoding="utf-8")
        return result

    monkeypatch.setattr(goalflight_worktree_pool, "_git", leave_full_seat_dirty)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "full-cleanup")
    try:
        assert_clean_seat(lease.path, base)
        assert not (lease.path / "leftover.txt").exists()
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


def test_hard_ceiling_is_lazy_and_reuses_seats() -> None:
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


def test_dirty_seat_is_quarantined_then_reset_on_acquire() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "abandoned")
        (abandoned.path / "tracked.txt").write_text("abandoned edit\n", encoding="utf-8")
        (abandoned.path / "abandoned.txt").write_text("preserve me\n", encoding="utf-8")
        finish_seat_holder(abandoned)

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

            record_finished_holder("killed-worker", identity, state="worker_dead")

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
            record_finished_holder("inherited-worker", identity, state="worker_dead")
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
        first_path, first_base = goalflight_worktree_pool.shared_read_only_worktree(repo)
        second_path, second_base = goalflight_worktree_pool.shared_read_only_worktree(repo)
        assert_true("same read-only path", first_path == second_path)
        assert_true("same read-only base", first_base == second_base)
        assert_true("read-only checkout is detached", git(first_path, "branch", "--show-current") == "")
        assert_true("read-only checkout is not an exclusive pool slot", not (repo / "worktrees" / "s-1").exists())


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


def test_read_only_allocation_lock_wait_is_bounded(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    lock_path = goalflight_worktree_pool.read_only_allocation_lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(
            goalflight_worktree_pool.WorktreeReadOnlyLockTimeout,
            match="read-only allocation lock wait expired",
        ):
            with goalflight_worktree_pool._read_only_allocation_lock(
                repo, timeout_s=0.05
            ):
                pass
        assert time.monotonic() - started < 1.0


def test_read_only_reaper_honors_transaction_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
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
    assert pinned == paths[:2]


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

    def count_identity_calls(path: Path, *, timeout: float | None = None) -> Path:
        identity_calls.append(os.path.realpath(str(path)))
        return original_common_dir(path, timeout=timeout)

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
        repo, root=root, requested_path=None, deadline=time.monotonic() + 20.0
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
        lambda _repo, **_kwargs: [],
    )
    started = time.monotonic()
    with goalflight_ledger.StateLock():
        goalflight_worktree_pool._reap_read_only_worktrees(
            repo,
            root=root,
            requested_path=None,
            deadline=started + 0.05,
        )
    assert time.monotonic() - started < 1.0


def test_clean_legacy_ring_seat_is_adopted_and_reused(tmp_path: Path) -> None:
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
    try:
        assert path == legacy_path
        assert selected == base
        assert hold is not None
        adopted = json.loads(registry_path.read_text(encoding="utf-8"))["locks"]
        record = adopted[goalflight_worktree_pool._lock_registry_key(legacy_lock)]
        identity = os.stat(legacy_lock)
        assert record == {"st_dev": identity.st_dev, "st_ino": identity.st_ino}
        assert read_only_worktrees(repo) == []
    finally:
        if hold is not None:
            hold.release()


def test_read_only_review_reuses_clean_pooled_seat_without_checkout() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        base = add_read_only_base(repo, 0)
        writer = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "writer-finished", base=base
        )
        seat = writer.path
        finish_seat_holder(writer)

        path, selected, hold = goalflight_worktree_pool.bind_read_only_worktree(
            repo, "review-seat", base=base
        )
        try:
            assert path == seat
            assert selected == base
            assert hold is not None
            assert read_only_worktrees(repo) == []
            with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
                goalflight_worktree_pool.acquire_worktree_seat(repo, "writer-next")
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


def test_read_only_review_rechecks_after_shared_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        base = add_read_only_base(repo, 0)
        writer = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "writer-recheck", base=base
        )
        finish_seat_holder(writer)
        calls = 0

        def matches(_path: Path, _base: str, **_kwargs: object) -> bool:
            nonlocal calls
            calls += 1
            return calls == 1

        monkeypatch.setattr(goalflight_worktree_pool, "_read_only_seat_matches", matches)
        path, _selected, hold = goalflight_worktree_pool.bind_read_only_worktree(
            repo, "review-recheck", base=base
        )
        assert hold is None
        assert path.parent.name == goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
        assert calls == 2


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


def test_two_read_only_reviews_share_one_seat_and_release_on_crash() -> None:
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
        assert first_path == second_path == writer.path
        assert first_hold is not None and second_hold is not None
        try:
            with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
                goalflight_worktree_pool.acquire_worktree_seat(repo, "writer-blocked")
        finally:
            first_hold.release()
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
def test_failed_bind_has_no_phantom_holder(
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
        with pytest.raises(Exception, match=f"injected {bind_step} failure"):
            goalflight_worktree_pool.acquire_worktree_seat(repo, "failed-bind")
        monkeypatch.setattr(goalflight_worktree_pool, target, original)

        assert lock_path.read_text(encoding="utf-8") == prior_occupant
        if bind_step == "verify":
            record_finished_holder("failed-bind", state="failed")
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


def test_free_seat_nearest_to_target_base_is_selected() -> None:
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


def test_exact_retry_base_skips_checkout() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        base = git(repo, "rev-parse", "HEAD")
        first = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "retry-dispatch", base=base
        )
        finish_seat_holder(first)

        real_git = goalflight_worktree_pool._git
        checkout_calls: list[tuple[str, ...]] = []

        def recording_git(worktree: Path, *args: str, **kwargs) -> str:
            if args and args[0] == "checkout":
                checkout_calls.append(args)
            return real_git(worktree, *args, **kwargs)

        goalflight_worktree_pool._git = recording_git
        try:
            retry = goalflight_worktree_pool.acquire_worktree_seat(
                repo, "retry-dispatch", base=base
            )
            retry.release()
        finally:
            goalflight_worktree_pool._git = real_git

        assert_true("pinned retry performs zero checkouts", not checkout_calls)


def test_skip_reset_keeps_dirty_product_files() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-src")
        (first.path / "RESULT.md").write_text("in progress\n", encoding="utf-8")
        path = first.path
        first.release()

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
    with tempfile.TemporaryDirectory() as td, seat_limit(4):
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


def main() -> None:
    tests = [
        test_hard_ceiling_is_lazy_and_reuses_seats,
        test_process_concurrency_gets_distinct_seats_and_names_all_occupants,
        test_dirty_seat_is_quarantined_then_reset_on_acquire,
        test_large_reset_attribute_check_returns_blocker_under_watchdog,
        test_git_proc_uses_file_for_large_input,
        test_reset_safety_handles_refnames_larger_than_argv_limit,
        test_pin_unique_commits_handles_refnames_larger_than_argv_limit,
        test_sigkill_releases_kernel_lease_without_cleanup,
        test_path_lock_sigkill_releases_without_cleanup,
        test_path_locks_on_different_trees_do_not_serialize,
        test_parent_release_keeps_inherited_worker_lease_until_worker_dies,
        test_default_seat_count_is_not_a_per_controller_cap,
        test_registration_ignores_basename_without_a_lock,
        test_free_ancestor_base_beats_lower_slot_unrelated_base,
        test_notes_survive_acquire_reset_and_result_is_quarantined,
        test_skip_reset_keeps_dirty_product_files,
        test_two_controller_labels_share_repository_pool,
        test_classify_dispatch_cwd_lock,
        test_quarantine_handles_reserved_paths_larger_than_argv_limit,
        test_ignored_goal_flight_dir_does_not_block_quarantine,
        test_empty_ignored_goal_flight_dir_does_not_block_quarantine,
        test_info_exclude_ignored_goal_flight_dir_does_not_block_quarantine,
        test_global_exclude_ignored_goal_flight_dir_does_not_block_quarantine,
        test_tracked_goal_flight_is_quarantined,
        test_hwm_stays_after_release,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
