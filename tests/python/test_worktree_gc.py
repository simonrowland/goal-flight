"""The worktree GC predicate is a conjunction, and every conjunct retains alone.

Audit 2026-08-27: a merged-only sweep would have deleted four ACTIVE workers'
trees, because a worktree whose branch EQUALS main (the worker has not
committed yet) reads as "merged". The removal predicate here is a conjunction
of four independently load-bearing conditions — merged, clean, unowned by a
non-terminal dispatch, not the current checkout — and each test below pins one
condition by building a tree that ONLY that condition protects: revert the
condition and the test goes red.

Every precondition is built for real (b-235): real temp git repos, real
worktrees, a real ledger file under the isolated GOALFLIGHT_STATE_DIR. No
predicate answers are stubbed. Program exit codes are asserted on every run.
"""

from __future__ import annotations

import builtins
import fcntl
import fnmatch
import json
import os
from pathlib import Path
import random
import shutil
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_compat  # noqa: E402
import goalflight_dispatch  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_subprocess  # noqa: E402
import goalflight_worktree_gc  # noqa: E402
import goalflight_worktree_pool  # noqa: E402

SCRIPT = SCRIPTS / "goalflight_worktree_gc.py"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real git repo with one commit on main — worktrees branch from it."""
    root = tmp_path / "main-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("hello\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "init")
    return root


def _add_worktree(repo: Path, tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    _git(repo, "worktree", "add", "-q", "-b", name, str(path))
    return path


def _add_pool_seat(repo: Path, dispatch_id: str) -> Path:
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    path = lease.path
    lease.release()
    dead_pid = 2**30
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, path.name)
    with lock_path.open("r+", encoding="utf-8") as lock_file:
        metadata = goalflight_worktree_pool._lock_metadata(lock_file)
        metadata.update(pid=dead_pid, start_token="exited-test-worker")
        lock_file.seek(0)
        lock_file.truncate()
        json.dump(metadata, lock_file, sort_keys=True)
        lock_file.write("\n")
        lock_file.flush()
    _write_ledger(
        dispatch_id,
        "complete",
        path,
        project_root=str(repo),
        terminal_state="complete",
        worker_pid=dead_pid,
        worker_identity={"pid": dead_pid, "start_token": "exited-test-worker"},
    )
    return path


def _commit_in(worktree: Path, filename: str = "work.txt") -> None:
    (worktree / filename).write_text("progress\n")
    _git(worktree, "add", filename)
    _git(worktree, "commit", "-qm", "worker progress")


def _merge_into_main(repo: Path, branch: str) -> None:
    _git(repo, "merge", "-q", "--ff-only", branch)


def _add_read_only_checkout(repo: Path, index: int) -> Path:
    _commit_in(repo, f"read-only-base-{index}.txt")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    path, _ = goalflight_worktree_pool.shared_read_only_worktree(repo, base=base)
    os.utime(path, (1, 1))
    return path


def _add_unregistered_read_only_checkout(repo: Path, name: str) -> Path:
    root = goalflight_worktree_pool.read_only_worktree_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "initial")
    return path


def _write_ledger(dispatch_id: str, state: str, worker_cwd: Path | None, **extra: object) -> None:
    runs = goalflight_ledger.runs_dir(create=True)
    record = {
        "dispatch_id": dispatch_id,
        "state": state,
    }
    if (
        worker_cwd is not None
        and state in {"complete", "failed", "error", "cancelled", "idle_timeout"}
        and "worker_pid" not in extra
        and "worker_identity" not in extra
    ):
        dead_pid = 2**30
        record.update(
            worker_pid=dead_pid,
            worker_identity={"pid": dead_pid, "start_token": "exited-test-worker"},
        )
    if worker_cwd is not None:
        record["worker_cwd"] = str(worker_cwd)
        record["project_root"] = str(worker_cwd)
    record.update(extra)
    name = goalflight_compat.safe_dispatch_filename(dispatch_id)
    (runs / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


def _run(
    repo_arg: Path, *extra: str, seed_terminal: bool = True
) -> tuple[subprocess.CompletedProcess[str], dict]:
    if seed_terminal and not goalflight_ledger.runs_dir(create=False).exists():
        _write_ledger("test-terminal-ledger-row", "complete", None)
    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo_arg), "--json", *extra],
        capture_output=True, text=True,
    )
    assert done.returncode == 0, f"exit {done.returncode}: {done.stderr}"
    return done, json.loads(done.stdout)


def _entry(report: dict, path: Path) -> dict:
    wanted = os.path.realpath(path)
    for entry in report["entries"]:
        if os.path.realpath(entry["path"]) == wanted:
            return entry
    raise AssertionError(f"no entry for {path} in {report['entries']}")


def _isolate_dispatch_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "machine-state"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(tmp_path / "codex-state"))
    monkeypatch.setenv(
        "GOALFLIGHT_SEAT_ARCHIVE_DIR",
        str(tmp_path / "goal-flight-home" / ".goal-flight" / "seat-archive"),
    )
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(state / "dispatch"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journals"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER", str(tmp_path / "wake-ledger"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE", str(tmp_path / "task-store"))


def _worktree_paths(repo: Path) -> set[str]:
    done = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True, capture_output=True, text=True,
    )
    return {
        os.path.realpath(line.split(" ", 1)[1])
        for line in done.stdout.splitlines()
        if line.startswith("worktree ")
    }


def test_terminal_outcome_is_emitted_without_terminal_gc(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    dispatch_id = "terminal-outcome-order"
    events: list[str] = []
    real_print = builtins.print

    def record_print(*args: object, **kwargs: object) -> None:
        if args and isinstance(args[0], str) and args[0].startswith("DISPATCH-END "):
            events.append("outcome")
        real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", record_print)
    result = goalflight_dispatch.main(
        [
            "--unregistered-forced",
            "--in-place",
            "--agent",
            "test-dispatch",
            "--dispatch-id",
            dispatch_id,
            "--tail",
            str(tmp_path / f"{dispatch_id}.tail"),
            "--status-json",
            str(tmp_path / f"{dispatch_id}.status.json"),
            "--poll-secs",
            "0.02",
            "--max-idle-secs",
            "5",
            "--foreground",
            "--",
            sys.executable,
            "-c",
            f"print('COMPLETE: {dispatch_id} — ok', flush=True)",
        ]
    )

    assert result == 0
    assert events == ["outcome"]


def test_disk_floor_refuses_before_worker_spawn(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repo)
    _isolate_dispatch_state(tmp_path, monkeypatch)
    assert goalflight_dispatch.DISK_FREE_SPACE_FLOOR_BYTES == 20_000_000_000
    free = [goalflight_dispatch.DISK_FREE_SPACE_FLOOR_BYTES - 1_000_000_000]
    monkeypatch.setattr(
        goalflight_dispatch.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=free[0]),
    )
    marker = tmp_path / "must-not-spawn"
    before = _worktree_paths(repo)
    waited = []
    seat_calls = []
    real_seat_acquire = goalflight_worktree_pool.acquire_worktree_seat

    def track_capacity_wait(*_args, **_kwargs):
        waited.append(True)
        return "unexpected-lease"

    def track_seat(*args, **kwargs):
        seat_calls.append(True)
        return real_seat_acquire(*args, **kwargs)

    monkeypatch.setattr(goalflight_dispatch, "_acquire_capacity", track_capacity_wait)
    monkeypatch.setattr(goalflight_worktree_pool, "acquire_worktree_seat", track_seat)

    result = goalflight_dispatch.main(
        [
            "--unregistered-forced", "--worktree", "HEAD", "--agent", "test-dispatch",
            "--dispatch-id", "disk-floor-low", "--tail", str(tmp_path / "low.tail"),
            "--status-json", str(tmp_path / "low.status.json"), "--foreground", "--",
            sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ]
    )

    assert result == 75
    assert not waited
    assert not seat_calls
    assert not marker.exists()
    assert _worktree_paths(repo) == before
    error = capsys.readouterr().err
    assert "19.0 GB" in error
    assert "20 GB floor" in error


def test_disk_floor_is_not_rechecked_when_free_space_drops_during_seat_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    free = [goalflight_dispatch.DISK_FREE_SPACE_FLOOR_BYTES + 1]
    binds = []

    def busy(_args):
        binds.append(True)
        if len(binds) == 1:
            free[0] = goalflight_dispatch.DISK_FREE_SPACE_FLOOR_BYTES - 1
            raise goalflight_worktree_pool.WorktreeSeatUnavailable("seat busy")
        return None

    def floor_was_rechecked():
        raise AssertionError("disk floor was checked again during seat admission")

    monkeypatch.setattr(goalflight_dispatch, "_check_dispatch_disk_floor", floor_was_rechecked)
    monkeypatch.setattr(goalflight_dispatch, "_bind_dispatch_worktree", busy)
    monkeypatch.setattr(goalflight_dispatch, "_pinned_worktree_seat_name", lambda _args: None)
    monkeypatch.setattr(goalflight_dispatch.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(goalflight_dispatch, "_project_root", lambda _args: tmp_path)
    monkeypatch.setattr(goalflight_dispatch, "_worker_cwd", lambda _args: tmp_path)
    monkeypatch.setattr(goalflight_dispatch, "_prepare_attempt_worktree_occupancy", lambda *_args: None)
    args = SimpleNamespace(
        capacity_wait_s=1,
        from_queue=False,
        _worktree_wait_notified=True,
        _worktree_capacity_lease_active=False,
        in_place=True,
        cwd=str(tmp_path),
        read_only=False,
    )

    assert goalflight_dispatch._admit_dispatch_worktree(args) is None

    assert len(binds) == 2
    assert args._worktree_capacity_lease_active is False


def test_disk_floor_refuses_on_low_sample_even_when_another_path_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    floor = goalflight_dispatch.DISK_FREE_SPACE_FLOOR_BYTES
    paths = iter([tmp_path / "home", tmp_path / "state", tmp_path / "codex"])
    monkeypatch.setattr(goalflight_dispatch.Path, "home", classmethod(lambda _cls: next(paths)))
    monkeypatch.setattr(goalflight_compat, "resolve_state_dir", lambda: next(paths))
    monkeypatch.setattr(goalflight_compat, "nearest_existing_path", lambda path: path)

    def disk_usage(path):
        if path.name == "home":
            return SimpleNamespace(free=floor - 1)
        raise PermissionError("space query unavailable")

    monkeypatch.setattr(goalflight_dispatch.shutil, "disk_usage", disk_usage)

    allowed, detail = goalflight_dispatch._check_dispatch_disk_floor()

    assert not allowed
    assert "home=20.0 GB" in detail
    assert "below the 20 GB floor" in detail
    assert "PermissionError" in detail


def test_home_path_measurement_error_is_one_allow_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        goalflight_dispatch.Path,
        "home",
        classmethod(lambda _cls: (_ for _ in ()).throw(OSError("HOME unavailable"))),
    )
    monkeypatch.setattr(goalflight_compat, "resolve_state_dir", lambda: tmp_path)
    monkeypatch.setattr(goalflight_compat, "nearest_existing_path", lambda path: path)
    monkeypatch.setattr(
        goalflight_dispatch.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            free=goalflight_dispatch.DISK_FREE_SPACE_FLOOR_BYTES + 1
        ),
    )

    allowed, detail = goalflight_dispatch._check_dispatch_disk_floor()

    assert allowed
    assert detail.startswith("warning: could not measure free space")
    assert "home: OSError: HOME unavailable" in detail
    assert "20.0 GB" in detail


def test_dispatch_above_disk_floor_runs_worker(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    _isolate_dispatch_state(tmp_path, monkeypatch)
    free = goalflight_dispatch.DISK_FREE_SPACE_FLOOR_BYTES + 1_000_000_000
    monkeypatch.setattr(
        goalflight_dispatch.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=free),
    )
    marker = tmp_path / "spawned"
    dispatch_id = "disk-floor-high"

    result = goalflight_dispatch.main(
        [
            "--unregistered-forced", "--in-place", "--agent", "test-dispatch",
            "--dispatch-id", dispatch_id, "--tail", str(tmp_path / f"{dispatch_id}.tail"),
            "--status-json", str(tmp_path / f"{dispatch_id}.status.json"),
            "--poll-secs", "0.02", "--max-idle-secs", "5", "--foreground", "--",
            sys.executable, "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch(); "
            f"print('COMPLETE: {dispatch_id} — ok', flush=True)",
        ]
    )

    assert result == 0
    assert marker.is_file()


def test_acp_floor_refuses_before_seat_or_codex_home(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import goalflight_acp_run

    monkeypatch.chdir(repo)
    _isolate_dispatch_state(tmp_path, monkeypatch)
    dispatch_id = "acp-disk-floor-low"
    home = Path(os.environ["GOALFLIGHT_CODEX_STATE_DIR"]) / "dispatch-homes" / dispatch_id
    before = _worktree_paths(repo)
    bind_calls = []
    home_calls = []

    def refuse_bind(_args):
        bind_calls.append(True)
        raise AssertionError("disk refusal must happen before worktree binding")

    def create_home(_root, _account, candidate_id, **_kwargs):
        home_calls.append(candidate_id)
        home.mkdir(parents=True)
        return str(home), "seat-a"

    monkeypatch.setattr(
        goalflight_dispatch,
        "_check_dispatch_disk_floor",
        lambda: (False, "free space 19.0 GB is below the 20 GB floor"),
    )
    monkeypatch.setattr(goalflight_dispatch, "_bind_dispatch_worktree", refuse_bind)
    monkeypatch.setattr(goalflight_dispatch, "_codex_seat_api", lambda: object())
    monkeypatch.setattr(goalflight_dispatch, "select_codex_account", lambda **_kw: ("seat-a", []))
    monkeypatch.setattr(goalflight_dispatch, "resolve_codex_home", create_home)
    monkeypatch.setattr(goalflight_dispatch, "_stamp_controller_session", lambda *_args: {})
    monkeypatch.setattr(goalflight_dispatch, "_prepare_attempt_controller_registration", lambda *_args: None)
    monkeypatch.setattr(goalflight_acp_run, "agent_command", lambda *_args, **_kw: (sys.executable, []))
    monkeypatch.setattr(goalflight_acp_run, "validate_acp_dispatch_readiness", lambda *_args: None)
    prompt = tmp_path / "acp-prompt.md"
    prompt.write_text("test", encoding="utf-8")

    result = goalflight_dispatch.main(
        [
            "--unregistered-forced", "--agent", "codex", "--shape", "acp",
            "--account", "seat-a", "--worktree", "create", "--dispatch-id", dispatch_id,
            "--prompt-file", str(prompt), "--capacity-wait-s", "0", "--foreground",
        ]
    )

    assert result == 75
    assert bind_calls == []
    assert home_calls == []
    assert not home.exists()
    assert _worktree_paths(repo) == before
    assert "19.0 GB" in capsys.readouterr().err


def test_disk_space_measurement_error_warns_and_allows_worker(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repo)
    _isolate_dispatch_state(tmp_path, monkeypatch)

    def unreadable(_path):
        raise PermissionError("space query unavailable")

    monkeypatch.setattr(goalflight_dispatch.shutil, "disk_usage", unreadable)
    marker = tmp_path / "spawned-with-unknown-space"
    dispatch_id = "disk-floor-unknown"

    result = goalflight_dispatch.main(
        [
            "--unregistered-forced", "--in-place", "--agent", "test-dispatch",
            "--dispatch-id", dispatch_id, "--tail", str(tmp_path / f"{dispatch_id}.tail"),
            "--status-json", str(tmp_path / f"{dispatch_id}.status.json"),
            "--poll-secs", "0.02", "--max-idle-secs", "5", "--foreground", "--",
            sys.executable, "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch(); "
            f"print('COMPLETE: {dispatch_id} — ok', flush=True)",
        ]
    )

    assert result == 0
    assert marker.is_file()
    warnings = [line for line in capsys.readouterr().err.splitlines() if "warning: could not measure free space" in line]
    assert len(warnings) == 1


def test_gc_apply_honors_shared_deadline(repo: Path) -> None:
    entry = {"decision": "remove", "path": str(repo / "missing-worktree")}

    goalflight_worktree_gc.apply_removals(
        repo,
        [entry],
        into="main",
        ledger_dir=goalflight_ledger.runs_dir(create=False),
        main_path=goalflight_worktree_gc.main_worktree_path(repo),
        current_checkout=None,
        current_error=None,
        deadline=0.0,
    )

    assert entry["outcome"] == "retained", entry
    assert "deadline expired" in entry["reason"], entry


# --------------------------------------------------------------------------
# The incident, named after it.


def test_near_miss_2026_08_27_branch_equal_to_main_uncommitted_worker_is_retained(
    tmp_path: Path, repo: Path
) -> None:
    """Branch == main because the worker has not committed; dispatch owns it.

    This is the 2026-08-27 near-miss: condition (1) reports the tree as merged
    (it IS — the branch equals main), so a merged-only sweep deletes a live
    worker's tree. Only condition (3), the ledger claim, protects it. If the
    ownership check is dropped or treats unknown as a green light, this test
    goes red.
    """
    live = _add_worktree(repo, tmp_path, "t353-live")
    _write_ledger("t353-live-w1", "running", live)

    _done, report = _run(repo)
    entry = _entry(report, live)
    assert entry["decision"] == "retain", entry
    # The trap must be visible in the report: merged really does say yes.
    assert entry["conditions"]["merged"]["verdict"] == "yes", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert "t353-live-w1" in entry["conditions"]["unowned"]["reason"]
    assert live.is_dir(), "report-only mode must never touch the tree"


# --------------------------------------------------------------------------
# One case per condition: dropping it alone deletes something it must not.


def test_drop_merged_condition_would_delete_unmerged_work(tmp_path: Path, repo: Path) -> None:
    """Condition (1) alone protects this tree: clean, unowned, not current."""
    wt = _add_worktree(repo, tmp_path, "unmerged")
    _commit_in(wt)  # not merged into main

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["merged"]["verdict"] == "no"
    assert entry["conditions"]["clean"]["verdict"] == "unknown"
    assert entry["conditions"]["unowned"]["verdict"] == "yes"
    assert entry["conditions"]["not_current"]["verdict"] == "yes"


def test_cherry_picked_branch_counts_as_merged(tmp_path: Path, repo: Path) -> None:
    """Work already in main by patch-id frees its tree, even without ancestry.

    Ancestry answers "is this branch an ancestor of main?" but the question is
    "is this branch's work in main?". Every branch that lands by rebase,
    squash, or cherry-pick fails ancestry forever, so before this its worktree
    could never be reclaimed -- the mechanism behind dozens of immortal trees
    holding already-shipped work.
    """
    wt = _add_worktree(repo, tmp_path, "picked")
    _commit_in(wt)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout.strip()
    # Advance main first. Without this the cherry-pick reproduces an identical
    # sha (same tree, parent, message, and same-second timestamps), which would
    # make the branch a genuine ancestor and prove nothing.
    _commit_in(repo, "on-main.txt")
    # Cherry-pick, deliberately NOT merge: main gains the patch, not the commit.
    _git(repo, "cherry-pick", sha)

    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "picked", "main"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert ancestry.returncode == 1, "fixture must not be an ancestor, or it proves nothing"

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["conditions"]["merged"]["verdict"] == "yes", entry
    assert "patch-id equivalent" in entry["conditions"]["merged"]["reason"], entry
    assert entry["decision"] == "remove", entry


def test_unmerged_branch_with_one_unique_commit_is_still_retained(
    tmp_path: Path, repo: Path
) -> None:
    """The patch-id path must not become a blanket yes.

    A branch whose work is only partly upstream still holds unique commits and
    must stay protected; otherwise the equivalence check would delete real work.
    """
    wt = _add_worktree(repo, tmp_path, "partly")
    _commit_in(wt, "shared.txt")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout.strip()
    _git(repo, "cherry-pick", sha)
    _commit_in(wt, "unique.txt")  # this one never reaches main

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["conditions"]["merged"]["verdict"] == "no", entry
    assert entry["decision"] == "retain", entry


def test_drop_clean_condition_would_delete_dirty_worktree(tmp_path: Path, repo: Path) -> None:
    """Condition (2) alone protects this tree: merged, unowned, not current."""
    wt = _add_worktree(repo, tmp_path, "dirty")
    _commit_in(wt)
    _merge_into_main(repo, "dirty")
    (wt / "scratch.txt").write_text("uncommitted\n")

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["merged"]["verdict"] == "yes"
    assert entry["conditions"]["clean"]["verdict"] == "no"
    assert entry["conditions"]["unowned"]["verdict"] == "yes"
    assert entry["conditions"]["not_current"]["verdict"] == "yes"


def test_drop_unowned_condition_would_delete_owned_worktree(tmp_path: Path, repo: Path) -> None:
    """Condition (3) alone protects this tree: genuinely merged, clean."""
    wt = _add_worktree(repo, tmp_path, "owned")
    _commit_in(wt)
    _merge_into_main(repo, "owned")
    _write_ledger("t999-w1", "running", wt)

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["merged"]["verdict"] == "yes"
    assert entry["conditions"]["clean"]["verdict"] == "unknown"
    assert entry["conditions"]["unowned"]["verdict"] == "no"
    assert entry["conditions"]["not_current"]["verdict"] == "yes"


def test_ledger_path_index_matches_previous_symlink_and_missing_path_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("symlink classification test requires POSIX links")
    project = tmp_path / "project"
    project.mkdir()
    worktree = project / "worktree"
    (worktree / "child").mkdir(parents=True)
    worktree_link = tmp_path / "worktree-link"
    worktree_link.symlink_to(worktree, target_is_directory=True)
    missing = worktree / "missing" / "nested"
    dangling_link = tmp_path / "dangling-worktree-link"
    dangling_link.symlink_to(missing, target_is_directory=True)
    sibling = project / "worktree-near"
    sibling.mkdir()
    records = [
        {
            "dispatch_id": "symlink-owner",
            "state": "running",
            "worker_cwd": str(worktree_link / "child"),
        },
        {"dispatch_id": "missing-owner", "state": "running", "worker_cwd": str(missing)},
        {"dispatch_id": "dangling-owner", "state": "running", "worker_cwd": str(dangling_link)},
    ]
    ledger_index = goalflight_worktree_gc.LedgerIndex(records, [])

    def previous_match(
        record: dict, path: str, *, ledger_index=None,
    ) -> bool:
        target = goalflight_worktree_gc._resolve(path)
        candidates = ledger_index.cwd_paths(record)
        return any(
            goalflight_worktree_gc._same_path(candidate, target)
            or candidate == target
            or candidate.startswith(target + os.sep)
            for candidate in candidates
        )

    for path, expected in (
        (worktree, "no"),
        (worktree_link, "no"),
        (missing, "no"),
        (sibling, "unknown"),
    ):
        for record in records:
            assert goalflight_worktree_gc._record_cwd_matches(
                record, str(path), ledger_index=ledger_index,
            ) == previous_match(record, str(path), ledger_index=ledger_index)
        indexed = goalflight_worktree_gc.check_unowned(
            str(path), tmp_path / "ledger", ledger_index=ledger_index,
        )
        with monkeypatch.context() as reference:
            reference.setattr(goalflight_worktree_gc, "_record_cwd_matches", previous_match)
            previous = goalflight_worktree_gc.check_unowned(
                str(path), tmp_path / "ledger", ledger_index=ledger_index,
            )
        assert indexed == previous
        assert indexed["verdict"] == expected


def test_ledger_path_index_realpath_calls_scale_with_records_and_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_count = 80
    worktree_count = 24
    records = [
        {
            "dispatch_id": f"terminal-{index}",
            "state": "complete",
            "worker_cwd": str(tmp_path / "records" / str(index) / "checkout"),
        }
        for index in range(record_count)
    ]
    realpath = goalflight_worktree_gc.os.path.realpath
    realpath_calls = 0

    def counted_realpath(path):
        nonlocal realpath_calls
        realpath_calls += 1
        return realpath(path)

    monkeypatch.setattr(goalflight_worktree_gc.os.path, "realpath", counted_realpath)
    ledger_index = goalflight_worktree_gc.LedgerIndex(records, [])
    verdicts = [
        goalflight_worktree_gc.check_unowned(
            str(tmp_path / "worktrees" / str(index)),
            tmp_path / "ledger",
            ledger_index=ledger_index,
        )
        for index in range(worktree_count)
    ]

    assert all(verdict["verdict"] == "yes" for verdict in verdicts)
    assert realpath_calls <= 2 * (record_count + worktree_count)
    assert realpath_calls < record_count * worktree_count


def test_clean_check_is_skipped_for_unmerged_and_owned_worktrees(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _isolate_dispatch_state(tmp_path, monkeypatch)
    unmerged = _add_worktree(repo, tmp_path, "unmerged-no-status")
    _commit_in(unmerged, "unmerged.txt")
    owned = _add_worktree(repo, tmp_path, "owned-no-status")
    _commit_in(owned)
    _merge_into_main(repo, "owned-no-status")
    _write_ledger("owned-no-status-worker", "running", owned)

    status_paths: list[str] = []
    original_git = goalflight_worktree_gc._git

    def record_status(repo_arg: Path, *args: str, **kwargs: object):
        if args[:1] == ("status",):
            status_paths.append(str(repo_arg))
        return original_git(repo_arg, *args, **kwargs)

    monkeypatch.setattr(goalflight_worktree_gc, "_git", record_status)
    assert goalflight_worktree_gc.main([str(repo), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    for path in (unmerged, owned):
        entry = _entry(report, path)
        assert entry["decision"] == "retain", entry
        assert entry["conditions"]["clean"]["verdict"] == "unknown", entry
    assert status_paths == []


def test_drop_current_condition_would_delete_the_checkout(tmp_path: Path, repo: Path) -> None:
    """Condition (4) alone protects the tree we are standing in.

    Pointing the tool AT a linked worktree makes that worktree the current
    checkout; the main worktree is separately protected by the main-worktree
    guard, so only condition (4) keeps this tree.
    """
    wt = _add_worktree(repo, tmp_path, "current")
    _commit_in(wt)
    _merge_into_main(repo, "current")

    _done, report = _run(wt)  # repo argument is the linked worktree itself
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["merged"]["verdict"] == "yes"
    assert entry["conditions"]["clean"]["verdict"] == "unknown"
    assert entry["conditions"]["unowned"]["verdict"] == "yes"
    assert entry["conditions"]["not_current"]["verdict"] == "no"


# --------------------------------------------------------------------------
# Three-state discipline.


def test_unlistable_ledger_dir_retains_as_unknown_not_as_unowned(
    tmp_path: Path, repo: Path
) -> None:
    """C1: glob-swallowed PermissionError on the ledger dir is UNKNOWN, not unowned.

    Corrupt-but-listable JSON (the sibling test) cannot catch this: glob
    yields [] without raising, so unreadable stays empty and check_unowned
    used to return yes.
    """
    wt = _add_worktree(repo, tmp_path, "unlistable-ledger")
    _commit_in(wt)
    _merge_into_main(repo, "unlistable-ledger")
    _write_ledger("live-owner", "running", wt)
    runs = goalflight_ledger.runs_dir(create=False)
    os.chmod(runs, 0o000)
    try:
        _done, report = _run(repo)
    finally:
        os.chmod(runs, 0o700)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    unowned = entry["conditions"]["unowned"]
    assert unowned["verdict"] == "unknown", entry
    assert "unreadable" in unowned["reason"]


def test_unreadable_ledger_retains_as_unknown_not_as_unowned(
    tmp_path: Path, repo: Path
) -> None:
    """An unreadable ledger is UNKNOWN — retained, and visibly distinct from owned.

    Collapsing "could not read the ledger" into "no dispatch owns it" is the
    exact failure the conjunction exists to prevent.
    """
    wt = _add_worktree(repo, tmp_path, "orphan-looking")
    _commit_in(wt)
    _merge_into_main(repo, "orphan-looking")
    runs = goalflight_ledger.runs_dir(create=True)
    (runs / "corrupt.json").write_bytes(b"{not json")

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    unowned = entry["conditions"]["unowned"]
    assert unowned["verdict"] == "unknown", entry
    assert "unreadable" in unowned["reason"]
    # Distinct from retained-because-owned: no dispatch is claimed as owner.
    assert "non-terminal dispatch" not in unowned["reason"]


@pytest.mark.parametrize("ledger_state", ["absent", "empty"])
def test_missing_or_empty_ledger_retains_as_unknown(
    repo: Path, ledger_state: str
) -> None:
    wt = _add_read_only_checkout(repo, 7)
    runs = goalflight_ledger.runs_dir(create=False)
    if ledger_state == "empty":
        runs.mkdir(parents=True)

    _done, report = _run(repo, seed_terminal=False)

    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    unowned = entry["conditions"]["unowned"]
    assert unowned["verdict"] == "unknown", entry
    assert "unreadable or empty" in unowned["reason"]


def test_detached_head_is_unknown_and_retained(tmp_path: Path, repo: Path) -> None:
    """Detached HEAD: merge state cannot be evaluated, and unknown retains."""
    wt = tmp_path / "detached"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["merged"]["verdict"] == "unknown"


def test_read_only_checkout_is_gc_candidate_without_merge_condition(
    tmp_path: Path, repo: Path
) -> None:
    wt = _add_read_only_checkout(repo, 1)

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["read_only"] is True, entry
    assert entry["decision"] == "remove", entry
    assert "merged" not in entry["conditions"], entry

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["outcome"] == "removed", entry
    assert not wt.exists()
    assert os.path.realpath(wt) not in _worktree_paths(repo)


def test_manual_gc_still_removes_merged_non_pool_worktree(
    tmp_path: Path, repo: Path
) -> None:
    wt = _add_worktree(repo, tmp_path, "operator-owned")
    _commit_in(wt)
    _merge_into_main(repo, "operator-owned")

    done, report = _run(repo, "--apply")

    assert done.returncode == 0
    assert _entry(report, wt)["outcome"] == "removed"
    assert not wt.exists()


def test_read_only_gc_retains_checkout_inside_grace_window(repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    wt, _ = goalflight_worktree_pool.shared_read_only_worktree(repo, base=base)

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["grace"]["verdict"] == "no", entry

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert wt.is_dir()


def test_read_only_gc_uses_recorded_worktree_path_for_ownership(
    tmp_path: Path, repo: Path
) -> None:
    wt = _add_read_only_checkout(repo, 2)
    _write_ledger(
        "readonly-worktree-path-owner",
        "running",
        None,
        worktree_path=str(wt),
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert "readonly-worktree-path-owner" in entry["conditions"]["unowned"]["reason"]


def test_terminal_read_only_owner_does_not_pin_checkout_without_matching_report(
    repo: Path,
) -> None:
    wt = _add_read_only_checkout(repo, 21)
    _write_ledger(
        "readonly-terminal-owner",
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        read_only=True,
        resume_mode="resume",
        worktree_base="base",
        worktree_head="head",
    )

    _done, report = _run(repo, seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["conditions"]["unowned"]["verdict"] == "yes", entry


def test_terminal_read_only_owner_with_live_identity_still_pins_checkout(
    repo: Path,
) -> None:
    wt = _add_read_only_checkout(repo, 22)
    identity = goalflight_compat.process_start_identity(os.getpid())
    assert identity and identity.get("start_token"), identity
    _write_ledger(
        "readonly-terminal-live-owner",
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        read_only=True,
        worker_pid=os.getpid(),
        worker_identity=identity,
    )

    _done, report = _run(repo, seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry


def test_terminal_read_only_owner_with_unknown_identity_still_pins_checkout(
    repo: Path,
) -> None:
    wt = _add_read_only_checkout(repo, 23)
    _write_ledger(
        "readonly-terminal-unknown-owner",
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        read_only=True,
        worker_pid=2**30,
    )

    _done, report = _run(repo, seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert "identity-unknown" in entry["conditions"]["unowned"]["reason"]


def test_gc_reports_unregistered_read_only_directory(repo: Path) -> None:
    orphan = _add_unregistered_read_only_checkout(repo, "unregistered-clean")

    _done, report = _run(repo)
    entry = _entry(report, orphan)
    assert entry["kind"] == "unregistered-read-only", entry
    assert entry["decision"] == "retain", entry
    assert entry["size_bytes"] > 0
    assert "absent from git worktree list" in entry["reason"]


def test_gc_apply_never_removes_unregistered_read_only_directory(repo: Path) -> None:
    orphan = _add_unregistered_read_only_checkout(repo, "unregistered-retained")
    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, orphan)
    assert entry["decision"] == "retain", entry
    assert "absent from git worktree list" in entry["reason"]
    assert orphan.exists()


def test_gc_apply_retains_dirty_unregistered_read_only_directory(repo: Path) -> None:
    orphan = _add_unregistered_read_only_checkout(repo, "unregistered-dirty")
    (orphan / "untracked.txt").write_text("keep me\n", encoding="utf-8")

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, orphan)
    assert entry["decision"] == "retain", entry
    assert (orphan / "untracked.txt").exists()


def test_gc_apply_never_removes_invalid_unregistered_read_only_directories(
    repo: Path,
) -> None:
    root = goalflight_worktree_pool.read_only_worktree_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    empty = root / "invalid-empty"
    empty.mkdir()
    nonempty = root / "invalid-nonempty"
    nonempty.mkdir()
    (nonempty / "unknown.txt").write_text("keep me\n", encoding="utf-8")

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    empty_entry = _entry(report, empty)
    nonempty_entry = _entry(report, nonempty)
    assert empty_entry["decision"] == "retain", empty_entry
    assert empty.exists()
    assert nonempty_entry["decision"] == "retain", nonempty_entry
    assert (nonempty / "unknown.txt").exists()


def test_gc_apply_retains_live_owner_of_unregistered_read_only_directory(
    repo: Path,
) -> None:
    orphan = _add_unregistered_read_only_checkout(repo, "unregistered-live-owner")
    _write_ledger(
        "unregistered-read-only-live-owner",
        "running",
        orphan,
        project_root=str(repo),
    )

    done, report = _run(repo, "--apply", seed_terminal=False)
    assert done.returncode == 0
    entry = _entry(report, orphan)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert orphan.exists()


def test_gc_apply_retains_unregistered_directory_when_owner_is_unknown(repo: Path) -> None:
    orphan = _add_unregistered_read_only_checkout(repo, "unregistered-unknown-owner")
    _write_ledger(
        "unregistered-read-only-unknown-owner",
        "waiting_capacity",
        None,
        project_root=str(repo),
    )

    done, report = _run(repo, "--apply", seed_terminal=False)
    assert done.returncode == 0
    entry = _entry(report, orphan)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "unknown", entry
    assert orphan.exists()


def test_gc_apply_archives_ignored_payload_before_real_read_only_removal(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _add_read_only_checkout(repo, 24)
    (repo / ".git" / "info" / "exclude").write_text("payload/\n", encoding="utf-8")
    nested = wt / "payload" / "nested-repo"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q", "-b", "main")
    _git(nested, "config", "user.email", "test@example.invalid")
    _git(nested, "config", "user.name", "Test")
    (nested / "only-copy.txt").write_text("preserve me\n", encoding="utf-8")
    _git(nested, "add", "only-copy.txt")
    _git(nested, "commit", "-qm", "nested history")
    os.utime(wt, (1, 1))
    archive_dir = tmp_path / "seat-archive"
    monkeypatch.setenv("GOALFLIGHT_SEAT_ARCHIVE_DIR", str(archive_dir))
    _write_ledger(
        "read-only-archive-owner", "complete", wt, project_root=str(repo),
        terminal_state="complete", read_only=True,
        worker_pid=2**30,
        worker_identity={"pid": 2**30, "start_token": "exited-test-worker"},
    )

    done, report = _run(repo, "--apply", seed_terminal=False)
    assert done.returncode == 0, done.stderr
    entry = _entry(report, wt)
    assert entry.get("outcome") == "removed", entry
    assert not wt.exists()
    archives = list(archive_dir.glob("*.tar.gz"))
    assert len(archives) == 1
    listing = subprocess.run(
        ["tar", "-tzf", str(archives[0])], capture_output=True, text=True, check=True
    ).stdout
    assert "payload/nested-repo/only-copy.txt" in listing
    assert "payload/nested-repo/.git/objects/" in listing


def test_gc_apply_retains_read_only_checkout_with_assume_unchanged_edit(
    repo: Path,
) -> None:
    wt = _add_read_only_checkout(repo, 25)
    original = wt / "a.txt"
    original.write_text("hidden edit\n", encoding="utf-8")
    _git(wt, "update-index", "--assume-unchanged", "a.txt")
    _write_ledger(
        "read-only-hidden-edit-owner", "complete", wt, project_root=str(repo),
        terminal_state="complete", read_only=True,
        worker_pid=2**30,
        worker_identity={"pid": 2**30, "start_token": "exited-test-worker"},
    )

    done, report = _run(repo, "--apply", seed_terminal=False)
    assert done.returncode == 0, done.stderr
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert "assume-unchanged" in entry["conditions"]["clean"]["reason"]
    assert original.read_text(encoding="utf-8") == "hidden edit\n"
    assert wt.exists()


def test_read_only_reaper_keeps_four_and_removes_oldest_through_git(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [_add_read_only_checkout(repo, 30 + index) for index in range(8)]
    for index, path in enumerate(paths):
        os.utime(path, (index + 1, index + 1))
        identity = (
            goalflight_compat.process_start_identity(os.getpid())
            if index == 2 else None
        )
        if index == 2:
            assert identity and identity.get("start_token"), identity
        ledger_fields = {
            "project_root": str(repo),
            "terminal_state": "complete",
            "read_only": True,
        }
        if index == 2:
            ledger_fields.update(worker_pid=os.getpid(), worker_identity=identity)
        elif index == 3:
            ledger_fields["worker_pid"] = 2**30
        _write_ledger(
            f"read-only-keep-owner-{index}", "complete", path, **ledger_fields
        )
    (repo / ".git" / "info" / "exclude").write_text("payload/\n", encoding="utf-8")
    nested = paths[0] / "payload" / "nested-repo"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q", "-b", "main")
    _git(nested, "config", "user.email", "test@example.invalid")
    _git(nested, "config", "user.name", "Test")
    (nested / "only-copy.txt").write_text("preserve me\n", encoding="utf-8")
    _git(nested, "add", "only-copy.txt")
    _git(nested, "commit", "-qm", "nested history")
    hidden_edit = paths[1] / "a.txt"
    hidden_edit.write_text("hidden edit\n", encoding="utf-8")
    _git(paths[1], "update-index", "--assume-unchanged", "a.txt")
    for index, path in enumerate(paths):
        os.utime(path, (index + 1, index + 1))
    archive_dir = tmp_path / "pool-seat-archive"
    monkeypatch.setenv("GOALFLIGHT_SEAT_ARCHIVE_DIR", str(archive_dir))
    root = goalflight_worktree_pool.read_only_worktree_root(repo)
    ledger_dir = goalflight_ledger.runs_dir(create=False)
    lock = goalflight_ledger.StateLock.try_acquire(time.monotonic() + 10)
    assert lock is not None
    try:
        ledger_index = goalflight_worktree_gc.ledger_index_for_dir(ledger_dir)
        ledger_stamp = goalflight_worktree_pool._ledger_directory_stamp(ledger_dir)
    finally:
        lock.release()

    goalflight_worktree_pool._reap_read_only_worktrees(
        repo,
        root=root,
        requested_path=None,
        deadline=time.monotonic() + 30,
        ledger_index=ledger_index,
        ledger_stamp=ledger_stamp,
    )

    assert not paths[0].exists()
    assert all(path.exists() for path in paths[1:])
    assert hidden_edit.read_text(encoding="utf-8") == "hidden edit\n"
    archives = list(archive_dir.glob("*.tar.gz"))
    assert len(archives) == 1
    listing = subprocess.run(
        ["tar", "-tzf", str(archives[0])], capture_output=True, text=True, check=True
    ).stdout
    assert "payload/nested-repo/only-copy.txt" in listing
    assert "payload/nested-repo/.git/objects/" in listing


def test_read_only_reaper_rechecks_cwd_after_ignored_payload_archive(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _add_read_only_checkout(repo, 88)
    _write_ledger(
        "read-only-cwd-entered-during-archive",
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        read_only=True,
        worker_pid=2**30,
        worker_identity={"pid": 2**30, "start_token": "exited-test-worker"},
    )
    monkeypatch.setattr(goalflight_worktree_pool, "READ_ONLY_WORKTREE_KEEP", 0)
    (repo / ".git" / "info" / "exclude").write_text(
        "payload/\n", encoding="utf-8"
    )
    ignored = wt / "payload"
    ignored.mkdir()
    (ignored / "preserve.txt").write_text("archived\n", encoding="utf-8")
    os.utime(wt, (1, 1))
    archive_dir = tmp_path / "read-only-archive"
    monkeypatch.setenv("GOALFLIGHT_SEAT_ARCHIVE_DIR", str(archive_dir))

    original_archive = goalflight_worktree_gc._archive_ignored_payload
    child: subprocess.Popen | None = None
    ready_read: int | None = None
    fresh_scans = 0

    def archive_then_enter_checkout(path: Path, *, deadline: float):
        nonlocal child, ready_read
        result = original_archive(path, deadline=deadline)
        if path == wt and result[1] is None:
            ready_read, ready_write = os.pipe()
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os, time; os.write(int(os.environ['READY_FD']), b'ready'); time.sleep(60)",
                ],
                cwd=wt,
                env={**os.environ, "READY_FD": str(ready_write)},
                pass_fds=(ready_write,),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.close(ready_write)
            assert os.read(ready_read, 5) == b"ready"
            os.close(ready_read)
            ready_read = None
        return result

    def fresh_census():
        nonlocal fresh_scans
        fresh_scans += 1
        return goalflight_worktree_pool._process_cwd_snapshot(timeout_s=10.0)

    monkeypatch.setattr(
        goalflight_worktree_gc,
        "_archive_ignored_payload",
        archive_then_enter_checkout,
    )
    try:
        goalflight_worktree_pool._reap_read_only_worktrees(
            repo,
            root=goalflight_worktree_pool.read_only_worktree_root(repo),
            requested_path=None,
            fresh_process_cwd_census=fresh_census,
        )
    finally:
        if ready_read is not None:
            os.close(ready_read)
        if child is not None:
            child.terminate()
            child.wait(timeout=10)

    assert fresh_scans == 2
    assert wt.is_dir()
    assert list(archive_dir.glob("*.tar.gz"))


@pytest.mark.parametrize("record_fields", [{}, {"worker_cwd": "relative/missing"}])
def test_read_only_gc_retains_for_incomplete_nonterminal_ledger_row(
    tmp_path: Path, repo: Path, record_fields: dict[str, str]
) -> None:
    wt = _add_read_only_checkout(repo, 3)
    worker_cwd = (
        Path(record_fields["worker_cwd"])
        if "worker_cwd" in record_fields
        else None
    )
    _write_ledger("readonly-incomplete-owner", "running", worker_cwd)

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "unknown", entry
    assert "no usable worker_cwd" in entry["conditions"]["unowned"]["reason"]


def test_unowned_holds_pathless_nonterminal_until_closed(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_dispatch_state(tmp_path, monkeypatch)
    wt = _add_read_only_checkout(repo, 9)
    authority = goalflight_journal.Journal.create(repo)
    attempt = authority.prepare_attempt("pathless-never-launched")
    assert attempt.committed
    _write_ledger(
        "pathless-never-launched",
        "waiting_capacity",
        None,
        project_root=str(repo),
        started_at="2020-01-01T00:00:00Z",
        updated_at="2020-01-01T00:00:00Z",
        attempt_id=attempt.value.attempt_id,
        launch_token=attempt.value.launch_token,
        launch_epoch=0,
        agent="codex",
        engine="codex",
        account="default",
        transport="dispatch",
        hostname="fixture-host",
        logical_session_id="pathless-never-launched",
        stdout_path=str(tmp_path / "pathless-never-launched.tail"),
        status_path=str(tmp_path / "pathless-never-launched.status.json"),
    )
    ledger_dir = goalflight_ledger.runs_dir(create=False)
    index = goalflight_worktree_gc.ledger_index_for_dir(ledger_dir)

    unowned = goalflight_worktree_gc.check_unowned(
        str(wt), ledger_dir, project_root=repo, ledger_index=index
    )
    assert unowned["verdict"] == "unknown", unowned
    assert "no usable worker_cwd" in unowned["reason"], unowned
    _done, report = _run(repo, seed_terminal=False)
    assert _entry(report, wt)["decision"] == "retain", _entry(report, wt)

    record = json.loads(
        goalflight_ledger.record_path("pathless-never-launched").read_text(
            encoding="utf-8"
        )
    )
    goalflight_dispatch._commit_abandoned_dispatch(
        record,
        evaluation={"reason": "never_launched_no_final", "progress_age_s": 7200},
        state="inconclusive_no_final",
        reason="abandoned_without_verdict",
        marker=None,
    )
    index = goalflight_worktree_gc.ledger_index_for_dir(ledger_dir)
    closed = goalflight_worktree_gc.check_unowned(
        str(wt), ledger_dir, project_root=repo, ledger_index=index
    )
    assert closed["verdict"] == "yes", closed
    _done, report = _run(repo, seed_terminal=False)
    assert _entry(report, wt)["decision"] == "remove", _entry(report, wt)

    _write_ledger(
        "path-recorded-never-launched",
        "running",
        wt,
        project_root=str(repo),
    )
    index = goalflight_worktree_gc.ledger_index_for_dir(ledger_dir)
    retained = goalflight_worktree_gc.check_unowned(
        str(wt), ledger_dir, project_root=repo, ledger_index=index
    )
    assert retained["verdict"] == "no", retained
    assert "records this path as worker_cwd" in retained["reason"], retained


def test_read_only_gc_ignores_pathless_nonterminal_row_from_other_repository(
    tmp_path: Path, repo: Path
) -> None:
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-q", "-b", "main")
    wt = _add_read_only_checkout(repo, 8)
    _write_ledger(
        "foreign-project-waiting",
        "waiting_capacity",
        None,
        project_root=str(other_repo),
    )

    done, report = _run(repo, "--apply", seed_terminal=False)
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["conditions"]["unowned"]["verdict"] == "yes", entry
    assert entry["outcome"] == "removed", entry
    assert not wt.exists()


def test_read_only_gc_honors_argv_only_cwd_from_foreign_repository(
    tmp_path: Path, repo: Path
) -> None:
    other_repo = tmp_path / "other-repo-argv"
    other_repo.mkdir()
    _git(other_repo, "init", "-q", "-b", "main")
    wt = _add_read_only_checkout(repo, 81)
    _write_ledger(
        "foreign-project-argv-cwd",
        "waiting_capacity",
        None,
        project_root=str(other_repo),
        dispatch_argv=["--cwd", str(wt)],
    )

    done, report = _run(repo, "--apply", seed_terminal=False)
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert "foreign-project-argv-cwd" in entry["conditions"]["unowned"]["reason"]
    assert wt.is_dir()


def test_foreign_project_identity_failure_is_fail_closed(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _add_read_only_checkout(repo, 82)
    foreign_root = tmp_path / "not-a-git-repository"
    foreign_root.mkdir()
    _write_ledger(
        "foreign-project-unresolvable",
        "waiting_capacity",
        None,
        project_root=str(foreign_root),
    )

    _done, report = _run(repo, seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "unknown", entry

    wt2 = _add_read_only_checkout(repo, 83)
    _write_ledger(
        "foreign-project-timeout",
        "waiting_capacity",
        None,
        project_root=str(foreign_root),
    )
    original_common_dir = goalflight_worktree_pool._git_common_dir

    def timeout_common_dir(
        path: Path, *, timeout: float | None = None, deadline: float | None = None
    ) -> Path:
        assert timeout is not None
        raise goalflight_worktree_pool.WorktreeSeatError("git command timed out")

    monkeypatch.setattr(
        goalflight_worktree_pool, "_git_common_dir", timeout_common_dir
    )
    assert original_common_dir is not None
    _done, report = _run(repo, seed_terminal=False)
    entry = _entry(report, wt2)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "unknown", entry


def test_case_variant_repository_identities_never_alias_on_case_sensitive_fs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct case-variant Git common dirs must not collide in the cache."""
    upper = tmp_path / "Repo"
    lower = tmp_path / "repo"
    upper.mkdir()
    if lower.exists():
        pytest.skip("case-insensitive filesystem cannot represent case-variant roots")
    for root in (upper, lower):
        if root != upper:
            root.mkdir()
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "test@example.invalid")
        _git(root, "config", "user.name", "Test")
        (root / "a.txt").write_text("hello\n")
        _git(root, "add", "a.txt")
        _git(root, "commit", "-qm", "init")

    cache: dict[str, object] = {}
    foreign = {"project_root": str(upper)}
    assert goalflight_worktree_gc._record_project_root_matches(
        foreign, lower, identity_cache=cache
    ) is False


def test_read_only_gc_pin_does_not_hold_ledger_lock(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _add_read_only_checkout(repo, 851)
    _done, report = _run(repo)
    current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
    pin_started = threading.Event()
    release_pin = threading.Event()
    writer_done = threading.Event()

    def writer() -> None:
        lock = goalflight_ledger.StateLock.try_acquire(time.monotonic() + 0.5)
        if lock is None:
            return
        try:
            _write_ledger("ledger-writer-during-pin", "running", wt, project_root=str(repo))
            writer_done.set()
        finally:
            lock.release()

    def pin(_repo: Path, _path: str, **_kwargs: object) -> tuple[str, None]:
        pin_started.set()
        assert release_pin.wait(1.0)
        return "refs/goalflight/keep/ledger-race", None

    monkeypatch.setattr(goalflight_worktree_gc, "_pin_before_remove", pin)
    worker = threading.Thread(target=writer)
    monkeypatch.setattr(
        goalflight_worktree_gc,
        "_remove_worktree",
        lambda _repo, _path, **_kwargs: (True, ""),
    )
    apply_thread = threading.Thread(
        target=goalflight_worktree_gc.apply_removals,
        args=(repo, report["entries"]),
        kwargs={
            "into": "main",
            "ledger_dir": goalflight_ledger.runs_dir(create=False),
            "main_path": goalflight_worktree_gc.main_worktree_path(repo),
            "current_checkout": current_checkout,
            "current_error": current_error,
        },
    )
    apply_thread.start()
    assert pin_started.wait(1.0)
    worker.start()
    try:
        assert writer_done.wait(0.5), "pinning must not block the global ledger writer"
    finally:
        release_pin.set()
        apply_thread.join(2.0)
        worker.join(2.0)
    assert not apply_thread.is_alive()
    assert not worker.is_alive()
    assert _entry(report, wt)["outcome"] == "retained"
    assert wt.exists()


def test_resume_read_only_reservation_blocks_gc_until_child_row_exists(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _add_read_only_checkout(repo, 852)
    base = _git_head(repo)
    os.utime(wt, (1, 1))
    monkeypatch.setattr(
        goalflight_dispatch, "_prepare_read_only_admission", lambda *_args: None
    )
    args = type("Args", (), {})()
    args.parent_dispatch_id = "readonly-resume-parent-race"
    args.dispatch_id = "readonly-resume-child-race"
    args.agent = "codex"
    args.shape = "bash"
    args.read_only = True
    args.cwd = str(wt)
    args._worktree_base_commit = base

    goalflight_dispatch._prepare_read_only_resume_binding(args, repo)
    try:
        os.utime(wt, (1, 1))
        report = goalflight_worktree_gc.terminal_dry_run(
            repo, ledger_dir=goalflight_ledger.runs_dir(create=False)
        )
        current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
        goalflight_worktree_gc.apply_removals(
            repo,
            report["entries"],
            into="main",
            ledger_dir=goalflight_ledger.runs_dir(create=False),
            main_path=goalflight_worktree_gc.main_worktree_path(repo),
            current_checkout=current_checkout,
            current_error=current_error,
        )
        entry = _entry(report, wt)
        assert entry["decision"] == "retain", entry
        assert wt.exists()
    finally:
        goalflight_dispatch._release_read_only_resume_reservation(args)


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_quota_exhausted_resumable_terminal_row_keeps_read_only_checkout(
    repo: Path,
) -> None:
    wt = _add_read_only_checkout(repo, 853)
    _write_ledger(
        "quota-exhausted-resumable",
        "quota_exhausted",
        wt,
        project_root=str(repo),
        terminal_state="quota_exhausted",
        updated_at=goalflight_ledger.utc_now(),
    )
    os.utime(wt, (1, 1))

    _done, report = _run(repo, seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert "resumable" in entry["conditions"]["unowned"]["reason"]
    assert wt.exists()


def test_tilde_unknown_user_project_root_is_unknown(repo: Path) -> None:
    wt = _add_read_only_checkout(repo, 84)
    _write_ledger(
        "unknown-user-project-root",
        "waiting_capacity",
        None,
        project_root="~goalflight-user-that-does-not-exist-91638",
    )

    _done, report = _run(repo, seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "unknown", entry


def test_read_only_gc_serializes_ledger_writer_with_final_remove(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _add_read_only_checkout(repo, 85)
    _done, report = _run(repo)
    current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
    writer_started = threading.Event()
    writer_before_remove = {"value": False}
    removal_done = threading.Event()
    writer_thread: list[threading.Thread] = []

    def writer() -> None:
        writer_started.set()
        lock = goalflight_ledger.StateLock.try_acquire(time.monotonic() + 0.2)
        if lock is None:
            return
        try:
            _write_ledger(
                "ledger-writer-race",
                "running",
                wt,
                project_root=str(repo),
            )
            writer_before_remove["value"] = not removal_done.is_set()
        finally:
            lock.release()

    def pin(_repo: Path, _path: str, **_kwargs: object) -> tuple[str, None]:
        thread = threading.Thread(target=writer)
        writer_thread.append(thread)
        thread.start()
        assert writer_started.wait(1.0)
        thread.join(1.0)
        assert not thread.is_alive()
        return "refs/goalflight/keep/ledger-race", None

    original_remove = goalflight_worktree_gc._remove_worktree

    def remove(_repo: Path, _path: str, **_kwargs: object) -> tuple[bool, str]:
        result = original_remove(_repo, _path, **_kwargs)
        removal_done.set()
        return result

    monkeypatch.setattr(goalflight_worktree_gc, "_pin_before_remove", pin)
    monkeypatch.setattr(goalflight_worktree_gc, "_remove_worktree", remove)
    goalflight_worktree_gc.apply_removals(
        repo,
        report["entries"],
        into="main",
        ledger_dir=goalflight_ledger.runs_dir(create=False),
        main_path=goalflight_worktree_gc.main_worktree_path(repo),
        current_checkout=current_checkout,
        current_error=current_error,
    )
    for thread in writer_thread:
        thread.join(1.0)
    entry = _entry(report, wt)
    assert entry["outcome"] == "retained", entry
    assert writer_before_remove["value"] is True
    assert not removal_done.is_set()


def test_read_only_gc_retains_pathless_nonterminal_row_without_project_root(
    repo: Path,
) -> None:
    wt = _add_read_only_checkout(repo, 9)
    _write_ledger("missing-project-root-waiting", "waiting_capacity", None)

    done, report = _run(repo, seed_terminal=False)
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    reason = entry["conditions"]["unowned"]["reason"]
    assert entry["conditions"]["unowned"]["verdict"] == "unknown", entry
    assert "missing-project-root-waiting" in reason


def test_read_only_gc_retains_metadata_only_request_cwd_row(repo: Path) -> None:
    wt = _add_read_only_checkout(repo, 86)
    _write_ledger(
        "metadata-only-request-cwd",
        "waiting_capacity",
        None,
        request={"cwd": str(repo)},
    )

    done, report = _run(repo, seed_terminal=False)
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "unknown", entry
    assert "metadata-only-request-cwd" in entry["conditions"]["unowned"]["reason"]


def test_read_only_gc_retains_same_repository_pathless_rows_and_summarizes_ids(
    repo: Path,
) -> None:
    wt = _add_read_only_checkout(repo, 10)
    row_ids = [f"same-project-waiting-{index}" for index in range(4)]
    for dispatch_id in row_ids:
        _write_ledger(
            dispatch_id,
            "waiting_capacity",
            None,
            project_root=str(repo),
        )

    done, report = _run(repo, seed_terminal=False)
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    reason = entry["conditions"]["unowned"]["reason"]
    assert entry["conditions"]["unowned"]["verdict"] == "unknown", entry
    assert "count=4" in reason
    assert all(dispatch_id in reason for dispatch_id in row_ids[:3])
    assert row_ids[3] not in reason
    assert row_ids[0] in json.dumps(report)


def test_read_only_gc_matches_case_variant_ledger_path(
    tmp_path: Path, repo: Path
) -> None:
    wt = _add_read_only_checkout(repo, 4)
    _write_ledger(
        "readonly-case-owner",
        "running",
        Path(str(wt).swapcase()),
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry


def test_read_only_gc_retains_conflicting_nonterminal_ledger_state(
    repo: Path,
) -> None:
    wt = _add_read_only_checkout(repo, 6)
    _write_ledger(
        "readonly-conflicting-state",
        "running",
        None,
        terminal_state="complete",
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "unknown", entry


def test_read_only_gc_holds_allocator_lock_across_remove_recheck(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _add_read_only_checkout(repo, 5)
    _done, report = _run(repo)
    current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
    observed: dict[str, bool] = {}

    def remove_without_touching_tree(
        _repo: Path, _path: str, **_kwargs: object
    ) -> tuple[bool, str]:
        lock_path = goalflight_worktree_pool.read_only_allocation_lock_path(repo)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "r+", encoding="utf-8")
        try:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed["held"] = True
            else:
                observed["held"] = False
        finally:
            handle.close()
        return True, ""

    monkeypatch.setattr(goalflight_worktree_gc, "_remove_worktree", remove_without_touching_tree)
    goalflight_worktree_gc.apply_removals(
        repo,
        report["entries"],
        into="main",
        ledger_dir=goalflight_ledger.runs_dir(create=False),
        main_path=goalflight_worktree_gc.main_worktree_path(repo),
        current_checkout=current_checkout,
        current_error=current_error,
    )
    assert observed == {"held": True}
    assert wt.is_dir()


def test_read_only_gc_rejects_replaced_allocation_lock_identity(
    repo: Path
) -> None:
    with goalflight_worktree_pool._read_only_allocation_lock(repo):
        pass
    lock_path = goalflight_worktree_pool.read_only_allocation_lock_path(repo)
    parent = lock_path.parent
    backup = parent.with_name(parent.name + ".real")
    parent.rename(backup)
    parent.mkdir()
    (parent / lock_path.name).touch()
    try:
        handle, error = goalflight_worktree_gc._acquire_read_only_action_lock(repo)
        if handle is not None:
            handle.close()
        assert handle is None
        assert error and "identity" in error
    finally:
        (parent / lock_path.name).unlink()
        parent.rmdir()
        backup.rename(parent)


def test_read_only_gc_retains_on_common_dir_timeout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timed_out(
        _repo: Path, *, timeout: float | None = None, deadline: float | None = None
    ) -> Path:
        assert timeout is not None
        raise goalflight_worktree_pool.WorktreeSeatError(
            "git rev-parse timed out"
        )

    monkeypatch.setattr(goalflight_worktree_pool, "_git_common_dir", timed_out)
    handle, error = goalflight_worktree_gc._acquire_read_only_action_lock(
        repo, deadline=time.monotonic() + 1.0
    )
    if handle is not None:
        handle.close()
    assert handle is None
    assert error and "deadline expired" in error


def test_read_only_gc_rejects_replaced_unregistered_allocation_lock(
    repo: Path,
) -> None:
    import fcntl

    lock_path = goalflight_worktree_pool.read_only_allocation_lock_path(repo)
    parent = lock_path.parent
    parent.mkdir(parents=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    held = os.fdopen(fd, "r+", encoding="utf-8")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    # The allocator adopts the original pre-registry inode while holding it.
    # GC must then reject the replacement rather than trust the new path leaf.
    goalflight_worktree_pool._adopt_exclusive_lock(
        lock_path,
        held.fileno(),
        registry_root=goalflight_worktree_pool._git_common_dir(repo),
    )
    backup = parent.with_name(parent.name + ".real")
    parent.rename(backup)
    parent.mkdir()
    lock_path.touch()
    try:
        handle, error = goalflight_worktree_gc._acquire_read_only_action_lock(repo)
        if handle is not None:
            handle.close()
        assert handle is None
        assert error and "registered" in error
    finally:
        lock_path.unlink()
        parent.rmdir()
        backup.rename(parent)
        held.close()


def test_read_only_gc_adopts_pre_registry_lock_on_first_run(repo: Path) -> None:
    root = repo / "worktrees" / goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
    candidate = root / "host-state"
    root.mkdir(parents=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(candidate), "HEAD")
    os.utime(candidate, (1, 1))

    lock_path = goalflight_worktree_pool.read_only_allocation_lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    registry_path = goalflight_worktree_pool._lock_registry_path(
        goalflight_worktree_pool._git_common_dir(repo)
    )
    if registry_path.exists():
        registry_path.unlink()

    _done, report = _run(repo, "--apply")
    entry = _entry(report, candidate)
    assert entry["outcome"] == "removed", entry
    assert "changed_before_remove" not in json.dumps(entry)
    assert not candidate.exists()
    assert registry_path.exists()


def test_exclusive_open_adopts_pre_registry_lock_and_reuses_identity(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = goalflight_worktree_pool.read_only_allocation_lock_path(repo)
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    registry_path = goalflight_worktree_pool._lock_registry_path(
        goalflight_worktree_pool._git_common_dir(repo)
    )
    if registry_path.exists():
        registry_path.unlink()

    with goalflight_worktree_pool._read_only_allocation_lock(repo):
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
        record = payload["locks"][goalflight_worktree_pool._lock_registry_key(lock_path)]
        identity = os.stat(lock_path)
        assert record == {"st_dev": identity.st_dev, "st_ino": identity.st_ino}

    def compatibility_path_must_not_run(_path: Path):
        pytest.fail("second open used the unregistered compatibility path")

    monkeypatch.setattr(
        goalflight_worktree_pool, "_lock_path_identity", compatibility_path_must_not_run
    )
    with goalflight_worktree_pool._read_only_allocation_lock(repo):
        pass


def test_read_only_root_symlink_cannot_reap_pool_seat(
    tmp_path: Path, repo: Path
) -> None:
    if os.name == "nt":
        pytest.skip("symlink safety test requires POSIX links")
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "symlink-seat")
    seat = lease.path
    lease.release()
    root = repo / "worktrees" / goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
    root.symlink_to(repo / "worktrees", target_is_directory=True)

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
        goalflight_worktree_pool.shared_read_only_worktree(repo)
    goalflight_worktree_pool._reap_read_only_worktrees(
        repo, root=root, requested_path=root / "requested"
    )
    assert seat.is_dir()


# --------------------------------------------------------------------------
# Removal, prune, and reporting.


def test_merged_clean_terminal_owned_worktree_is_removed_with_apply(
    tmp_path: Path, repo: Path
) -> None:
    """The happy path: all four pass; a TERMINAL dispatch record does not retain."""
    wt = _add_worktree(repo, tmp_path, "done")
    _commit_in(wt)
    _merge_into_main(repo, "done")
    _write_ledger(
        "t111-w1",
        "complete",
        wt,
        terminal_state="complete",
        worker_pid=2147483647,
        worker_identity={"pid": 2147483647, "start_token": "exited-test-worker"},
        lock_holder="native",
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "remove", entry
    assert wt.is_dir(), "report mode must not remove"

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["outcome"] == "removed", entry
    assert not wt.exists()
    assert os.path.realpath(wt) not in _worktree_paths(repo)


def test_terminal_indeterminate_identity_retains_worktree(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal row with unknown liveness still owns its recorded cwd."""
    wt = _add_worktree(repo, tmp_path, "terminal-unknown")
    _commit_in(wt)
    _merge_into_main(repo, "terminal-unknown")
    _write_ledger(
        "terminal-unknown-w1",
        "complete",
        wt,
        terminal_state="complete",
        worker_pid=os.getpid(),
        worker_identity={"pid": os.getpid(), "start_token": "generation"},
    )
    monkeypatch.setattr(goalflight_worktree_gc, "_identity_live", lambda record: None)

    current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
    entry = goalflight_worktree_gc.classify(
        repo,
        {"path": str(wt), "branch": "terminal-unknown", "detached": False},
        into="main",
        ledger_dir=goalflight_ledger.runs_dir(create=False),
        main_path=goalflight_worktree_gc.main_worktree_path(repo),
        current_checkout=current_checkout,
        current_error=current_error,
    )
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert "identity" in entry["conditions"]["unowned"]["reason"]
    assert wt.is_dir()


def test_terminal_missing_pid_retains_and_reports_identity_unknown(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _add_pool_seat(repo, "terminal-missing-pid")
    _write_ledger(
        "terminal-missing-pid",
        "complete",
        wt,
        terminal_state="complete",
        worker_pid=None,
        worker_identity=None,
        lock_holder="native",
    )
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, wt.name)
    with lock_path.open("r+", encoding="utf-8") as lock_file:
        metadata = goalflight_worktree_pool._lock_metadata(lock_file)
        metadata.update(pid=None, start_token=None)
        lock_file.seek(0)
        lock_file.truncate()
        json.dump(metadata, lock_file, sort_keys=True)
        lock_file.write("\n")
        lock_file.flush()
    census_calls: list[bool] = []

    def fail_census(*, timeout_s: float):
        census_calls.append(True)
        raise AssertionError("unknown identity must retain without cwd census")

    monkeypatch.setattr(goalflight_worktree_pool, "_process_cwd_snapshot", fail_census)
    current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
    entry = goalflight_worktree_gc.classify(
        repo,
        {"path": str(wt), "branch": "terminal-missing-pid", "detached": False},
        into="main",
        ledger_dir=goalflight_ledger.runs_dir(create=False),
        main_path=goalflight_worktree_gc.main_worktree_path(repo),
        current_checkout=current_checkout,
        current_error=current_error,
    )

    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert "identity_unknown" in entry["conditions"]["unowned"]["reason"]
    assert census_calls == []
    assert wt.is_dir()


@pytest.mark.parametrize("lock_identity", ["live", "unknown"])
def test_gc_retains_dead_ledger_owner_when_pool_lock_identity_is_live_or_unknown(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_identity: str,
) -> None:
    dispatch_id = f"gc-lock-{lock_identity}"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    _merge_into_main(repo, f"worktree/{dispatch_id}")
    dead_pid = 2**30
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        worker_pid=dead_pid,
        worker_identity={"pid": dead_pid, "start_token": "exited-test-worker"},
    )

    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, wt.name)
    with lock_path.open("r+", encoding="utf-8") as lock_file:
        metadata = goalflight_worktree_pool._lock_metadata(lock_file)
        if lock_identity == "live":
            identity = goalflight_ledger.process_identity(os.getpid())
            assert identity is not None
            metadata.update(pid=os.getpid(), start_token=identity["start_token"])
        else:
            metadata.update(pid=None, start_token=None)
        lock_file.seek(0)
        lock_file.truncate()
        json.dump(metadata, lock_file, sort_keys=True)
        lock_file.write("\n")
        lock_file.flush()

    census_calls: list[float] = []

    def clear_census(*, timeout_s: float, deadline: float | None = None):
        census_calls.append(timeout_s)
        return []

    monkeypatch.setattr(
        goalflight_worktree_pool, "_process_cwd_snapshot", clear_census
    )
    current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
    entry = goalflight_worktree_gc.classify(
        repo,
        {"path": str(wt), "branch": f"worktree/{dispatch_id}", "detached": False},
        into="main",
        ledger_dir=goalflight_ledger.runs_dir(create=False),
        main_path=goalflight_worktree_gc.main_worktree_path(repo),
        current_checkout=current_checkout,
        current_error=current_error,
    )

    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    expected_reason = (
        "identity_live" if lock_identity == "live" else "identity_unknown"
    )
    assert expected_reason in entry["conditions"]["unowned"]["reason"], entry
    assert len(census_calls) == 1
    assert wt.is_dir()


def test_gc_retains_when_pool_branch_probe_times_out_within_budget(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatch_id = "gc-stalled-branch-probe"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    _merge_into_main(repo, f"worktree/{dispatch_id}")
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        worktree_branch=f"worktree/{dispatch_id}",
        worktree_head=head,
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        lambda *, timeout_s, deadline=None: [],
    )
    budget = 1.0
    monkeypatch.setattr(
        goalflight_worktree_gc, "_POOL_SEAT_CLASSIFICATION_S", budget
    )
    current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
    original_bounded_run = goalflight_subprocess.run_with_deadline
    probe_timeouts: list[float | None] = []

    def stall_branch_probe(args, **kwargs):
        if (
            kwargs.get("cwd") == str(wt)
            and args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
        ):
            timeout = kwargs.get("timeout")
            probe_timeouts.append(timeout)
            assert timeout is not None and 0 < timeout <= budget
            return original_bounded_run(
                [sys.executable, "-c", "import time; time.sleep(3)"],
                **kwargs,
            )
        return original_bounded_run(args, **kwargs)

    monkeypatch.setattr(
        goalflight_subprocess, "run_with_deadline", stall_branch_probe
    )
    started = time.monotonic()
    entry = goalflight_worktree_gc.classify(
        repo,
        {"path": str(wt), "branch": f"worktree/{dispatch_id}", "detached": False},
        into="main",
        ledger_dir=goalflight_ledger.runs_dir(create=False),
        main_path=goalflight_worktree_gc.main_worktree_path(repo),
        current_checkout=current_checkout,
        current_error=current_error,
    )
    elapsed = time.monotonic() - started

    assert probe_timeouts and probe_timeouts[0] is not None
    assert elapsed < budget + 0.25
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert "branch_probe_timeout" in entry["conditions"]["unowned"]["reason"]
    assert wt.is_dir()


def test_terminal_native_dead_identity_without_census_is_retained(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _add_pool_seat(repo, "terminal-native-no-census")
    _write_ledger(
        "terminal-native-no-census-w1",
        "complete",
        wt,
        terminal_state="complete",
        worker_pid=2**30,
        worker_identity={"pid": 2**30, "start_token": "missing:generation"},
        lock_holder="native",
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        lambda *, timeout_s: None,
    )
    current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
    entry = goalflight_worktree_gc.classify(
        repo,
        {"path": str(wt), "branch": "terminal-native-no-census", "detached": False},
        into="main",
        ledger_dir=goalflight_ledger.runs_dir(create=False),
        main_path=goalflight_worktree_gc.main_worktree_path(repo),
        current_checkout=current_checkout,
        current_error=current_error,
    )

    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert "worker_cwd_unknown" in entry["conditions"]["unowned"]["reason"]
    assert wt.is_dir()


def test_running_prelaunch_gc_row_retains_without_census(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _add_pool_seat(repo, "running-prelaunch-gc")
    _write_ledger(
        "running-prelaunch-gc-w1",
        "running",
        wt,
        terminal_state="unknown",
        prelaunch_failure=True,
        worker_pid=None,
        worker_identity=None,
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        lambda **_kwargs: pytest.fail("nonterminal row should not run cwd census"),
    )

    retained = goalflight_worktree_gc._record_owns_path(
        goalflight_ledger.read_record("running-prelaunch-gc-w1"), str(wt)
    )

    assert retained is True
    assert wt.is_dir()


def test_missing_directory_is_pruned_not_removed(tmp_path: Path, repo: Path) -> None:
    """Directory gone, admin entry left: pruned is a distinct outcome."""
    wt = _add_worktree(repo, tmp_path, "ghost")
    _commit_in(wt)
    _merge_into_main(repo, "ghost")
    shutil.rmtree(wt)

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "prune", entry
    assert entry["missing_on_disk"] is True

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["outcome"] == "pruned", entry
    assert os.path.realpath(wt) not in _worktree_paths(repo)


def test_apply_removes_only_the_named_admin_dir(tmp_path: Path, repo: Path) -> None:
    """GC --apply removes the admin dir of the worktree it prunes, and nothing else.

    A lock-only directory under .git/worktrees is not in the worktree list.
    ``git worktree prune`` would delete it along with the ghost entry.
    """
    ghost = _add_worktree(repo, tmp_path, "ghost-named")
    _commit_in(ghost)
    _merge_into_main(repo, "ghost-named")
    shutil.rmtree(ghost)
    common = goalflight_worktree_pool._git_common_dir(repo)
    lock_only = common / "worktrees" / "lock-only"
    lock_only.mkdir()
    kept = lock_only / goalflight_worktree_pool.OCCUPANCY_LOCK_NAME
    kept.write_text("keep\n", encoding="utf-8")

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, ghost)
    assert entry["outcome"] == "pruned", entry
    assert os.path.realpath(ghost) not in _worktree_paths(repo)
    assert kept.is_file()
    assert lock_only.is_dir()


def test_report_only_is_default_and_prints_retention_reasons(
    tmp_path: Path, repo: Path
) -> None:
    """No --apply: nothing is touched, survivors say WHY, exit code is 0."""
    removable = _add_worktree(repo, tmp_path, "sweepable")
    _commit_in(removable)
    _merge_into_main(repo, "sweepable")
    kept = _add_worktree(repo, tmp_path, "kept")
    _commit_in(kept, "other.txt")  # unmerged
    _write_ledger("test-terminal-ledger-row", "complete", None)

    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo)],
        capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr
    assert "would_remove" in done.stdout
    assert "why=" in done.stdout
    assert "commit(s) not in" in done.stdout
    assert "report only" in done.stdout
    assert removable.is_dir() and kept.is_dir()


def test_non_repository_exits_nonzero(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(plain)],
        capture_output=True, text=True,
    )
    assert done.returncode == 1
    assert "cannot list worktrees" in done.stderr


def test_registered_pool_worktree_is_reclaimed_only_after_full_gate(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered worktree still passes through the same four-part gate."""
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    wt = _add_pool_seat(repo, "register-seat")
    assert wt.name == "s-1"

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "remove", entry
    assert "pool seat is clean" in entry["reason"]
    assert entry.get("pool_seat", {}).get("verdict") == "yes", entry
    assert wt.is_dir()

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry.get("outcome") == "removed", entry
    assert entry.get("keep_ref", "").startswith("refs/goalflight/keep/")
    assert not wt.is_dir()
    assert os.path.realpath(wt) not in _worktree_paths(repo)


def test_pool_gc_removal_does_not_hold_allocation_lock(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    wt = _add_pool_seat(repo, "gc-lock-duration")
    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "remove", entry

    entered_remove = threading.Event()
    release_remove = threading.Event()
    errors: list[BaseException] = []

    def pause_remove(_repo: Path, _path: str, **_kwargs: object) -> tuple[bool, str]:
        entered_remove.set()
        if not release_remove.wait(timeout=10):
            return False, "test removal pause expired"
        return False, "test removal paused"

    monkeypatch.setattr(goalflight_worktree_gc, "_remove_worktree", pause_remove)
    current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)

    def apply() -> None:
        try:
            goalflight_worktree_gc.apply_removals(
                repo,
                [entry],
                into="main",
                ledger_dir=goalflight_ledger.runs_dir(create=False),
                main_path=goalflight_worktree_gc.main_worktree_path(repo),
                current_checkout=current_checkout,
                current_error=current_error,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=apply)
    worker.start()
    allocation_available = False
    try:
        assert entered_remove.wait(timeout=15), "GC did not reach worktree removal"
        allocation_path = goalflight_worktree_pool._seat_lock_root(repo) / "allocation.lock"
        with allocation_path.open("r+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                allocation_available = False
            else:
                allocation_available = True
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        assert allocation_available, "slow GC removal still holds allocation.lock"
    finally:
        release_remove.set()
        worker.join(timeout=15)
    assert not worker.is_alive(), "GC removal did not finish after release"
    assert not errors, errors


@pytest.mark.parametrize(
    "keep_ref",
    ["refs/goalflight/keep/{dispatch_id}/head", "refs/keep/{dispatch_id}/head"],
)
def test_clean_unmerged_pool_seat_with_keep_ref_is_reclaimed(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep_ref: str,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = "kept-unmerged"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    ref = keep_ref.format(dispatch_id=dispatch_id)
    _git(repo, "update-ref", ref, head)
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        worktree_head=head,
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "remove", entry
    assert "merged" not in entry["conditions"], entry
    assert entry["conditions"]["preserved"]["keep_ref"] == ref

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["outcome"] == "removed", entry
    assert entry["keep_ref"] == ref
    assert not wt.exists()


def test_fresh_cwd_census_retains_seat_entered_after_screening(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = "fresh-cwd-before-remove"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    _git(repo, "update-ref", f"refs/goalflight/keep/{dispatch_id}/head", head)
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        worktree_head=head,
    )

    current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
    entry = goalflight_worktree_gc.classify(
        repo,
        {"path": str(wt), "branch": f"worktree/{dispatch_id}", "detached": False},
        into="main",
        ledger_dir=goalflight_ledger.runs_dir(create=False),
        main_path=goalflight_worktree_gc.main_worktree_path(repo),
        current_checkout=current_checkout,
        current_error=current_error,
        process_cwd_census=lambda: [],
    )
    assert entry["decision"] == "remove", entry
    screening_scans = 0
    fresh_scans = 0

    def cached_census():
        nonlocal screening_scans
        screening_scans += 1
        return []

    ready_read, ready_write = os.pipe()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, time; os.write(int(os.environ['READY_FD']), b'ready'); time.sleep(60)",
        ],
        cwd=wt,
        env={**os.environ, "READY_FD": str(ready_write)},
        pass_fds=(ready_write,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(ready_write)
    try:
        assert os.read(ready_read, 5) == b"ready"
        snapshot = goalflight_worktree_pool._process_cwd_snapshot(timeout_s=10.0)
        assert goalflight_worktree_pool._process_cwd_under_seat(snapshot, wt) is True

        def fresh_census():
            nonlocal fresh_scans
            fresh_scans += 1
            return goalflight_worktree_pool._process_cwd_snapshot(timeout_s=10.0)

        current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
        goalflight_worktree_gc.apply_removals(
            repo,
            [entry],
            into="main",
            ledger_dir=goalflight_ledger.runs_dir(create=False),
            main_path=goalflight_worktree_gc.main_worktree_path(repo),
            current_checkout=current_checkout,
            current_error=current_error,
            process_cwd_census=cached_census,
            fresh_process_cwd_census=fresh_census,
        )
    finally:
        os.close(ready_read)
        child.terminate()
        child.wait(timeout=10)

    assert screening_scans >= 1
    assert fresh_scans == 1
    assert entry["outcome"] == "retained", entry
    assert "fresh process cwd census is occupied" in entry["reason"], entry
    assert wt.is_dir()


def test_dirty_unmerged_pool_seat_with_keep_ref_is_retained(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = "dirty-unmerged"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    ref = f"refs/goalflight/keep/{dispatch_id}/head"
    _git(repo, "update-ref", ref, head)
    (wt / "untracked-work.txt").write_text("keep me\n", encoding="utf-8")
    _write_ledger(dispatch_id, "complete", wt, project_root=str(repo))

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["clean"]["verdict"] == "no", entry
    assert wt.is_dir()


def test_pool_seat_with_hidden_untracked_file_is_retained(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = "hidden-untracked"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    _git(repo, "update-ref", f"refs/goalflight/keep/{dispatch_id}/head", head)
    _git(repo, "config", "status.showUntrackedFiles", "no")
    hidden = wt / "hidden-untracked.txt"
    hidden.write_text("preserve me\n", encoding="utf-8")
    _write_ledger(dispatch_id, "complete", wt, project_root=str(repo))

    _done, report = _run(repo, "--apply")
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["clean"]["verdict"] == "no", entry
    assert hidden.read_text(encoding="utf-8") == "preserve me\n"


@pytest.mark.parametrize(
    "index_flag", ["--assume-unchanged", "--skip-worktree"]
)
def test_pool_seat_with_hidden_index_modification_is_retained(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    index_flag: str,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = f"hidden-index-{index_flag[2:]}"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    _git(repo, "update-ref", f"refs/goalflight/keep/{dispatch_id}/head", head)
    tracked = wt / "a.txt"
    _git(wt, "update-index", index_flag, "a.txt")
    tracked.write_text("preserve changed content\n", encoding="utf-8")
    _write_ledger(dispatch_id, "complete", wt, project_root=str(repo))

    _done, report = _run(repo, "--apply")
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["clean"]["verdict"] == "no", entry
    assert tracked.read_text(encoding="utf-8") == "preserve changed content\n"


def test_unpinned_pool_seat_is_pinned_to_dispatch_head_before_reclaim(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = "pin-before-remove"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    _write_ledger(dispatch_id, "complete", wt, project_root=str(repo))

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "remove", entry
    assert entry["conditions"]["preserved"]["pin_required"] == "yes", entry

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["outcome"] == "removed", entry
    expected_ref = entry["keep_ref"]
    keep_prefix = f"refs/goalflight/keep/{dispatch_id}/head-"
    assert expected_ref.startswith(keep_prefix), entry
    stamp = expected_ref[len(keep_prefix):].split("-", 1)[0]
    assert stamp.isdecimal(), entry
    assert goalflight_worktree_pool._git(repo, "rev-parse", expected_ref) == head
    assert not wt.exists()


def test_conflicting_keep_ref_is_not_overwritten_and_recovery_discovers_pin(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = "conflicting-keep-ref"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    original = _git_head(repo)
    existing_ref = f"refs/goalflight/keep/{dispatch_id}/head"
    _git(repo, "update-ref", existing_ref, original)
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        worktree_branch=f"worktree/{dispatch_id}",
        worktree_head=head,
    )

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["outcome"] == "removed", entry
    assert entry["keep_ref"] != existing_ref
    assert goalflight_worktree_pool._git(repo, "rev-parse", existing_ref) == original
    assert goalflight_worktree_pool._git(repo, "rev-parse", entry["keep_ref"]) == head
    recovery_refs = goalflight_dispatch._resume_recovery_refs_for_parent(
        repo, dispatch_id
    )
    assert recovery_refs[0] == entry["keep_ref"]
    branch, branch_tip, keep_ref, quarantine_ref = (
        goalflight_dispatch._resume_worktree_branch_spec(
            repo,
            dispatch_id,
            {
                "worktree_branch": f"worktree/{dispatch_id}",
                "worktree_head": head,
            },
            recovery_refs=recovery_refs,
        )
    )
    assert branch == f"worktree/{dispatch_id}"
    assert branch_tip == head
    assert keep_ref == entry["keep_ref"]
    assert quarantine_ref is None


@pytest.mark.parametrize("state", ["running", "indeterminate"])
def test_live_or_indeterminate_pool_dispatch_is_retained(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = f"live-{state}"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    _merge_into_main(repo, f"worktree/{dispatch_id}")
    record_state = "running" if state == "running" else "complete"
    extra: dict[str, object] = {"project_root": str(repo)}
    if state == "indeterminate":
        extra.update(
            terminal_state="complete",
            worker_pid=os.getpid(),
            worker_identity={"pid": os.getpid()},
        )
    _write_ledger(dispatch_id, record_state, wt, **extra)

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert wt.is_dir()


def test_terminal_read_only_pool_seat_without_resume_hold_needs_report(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = "readonly-terminal"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text(f"!COMPLETE: {dispatch_id} — review report\n", encoding="utf-8")
    started_at = goalflight_ledger.utc_now()
    os.utime(tail, (time.time() + 1, time.time() + 1))
    attempt_id = "readonly-review-attempt"
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        started_at=started_at,
        resume_mode="resume",
        worktree_base=head,
        worktree_head=head,
        updated_at=goalflight_ledger.utc_now(),
        agent="codex",
        shape="bash",
        attempt_id=attempt_id,
        terminal_marker={
            "kind": "COMPLETE",
            "dispatch_id": dispatch_id,
            "text": f"{dispatch_id} — review report",
            "attempt_id": attempt_id,
        },
        os_sandbox={
            "requested_profile": "read-only",
            "enforced_profile": "read-only",
        },
        stdout_path=str(tail),
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "remove", entry
    assert entry["conditions"]["unowned"]["verdict"] == "yes", entry

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["outcome"] == "removed", entry
    assert not wt.exists()


def test_terminal_read_only_pool_seat_without_report_keeps_resume_hold(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = "readonly-no-report"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    _merge_into_main(repo, f"worktree/{dispatch_id}")
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        resume_mode="resume",
        worktree_base=head,
        worktree_head=head,
        updated_at=goalflight_ledger.utc_now(),
        agent="codex",
        shape="bash",
        os_sandbox={
            "requested_profile": "read-only",
            "enforced_profile": "read-only",
        },
        stdout_path=str(tmp_path / "missing.tail"),
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert "resumable terminal dispatch retains checkout" in entry["conditions"]["unowned"]["reason"]


@pytest.mark.parametrize(
    "evidence",
    [
        "partial-result",
        "foreign-tail",
        "stale-attempt",
        "stale-attempt-tail",
        "unattributed-marker",
        "stale-tail-only",
        "tail-only-current-attempt",
        "missing-attempt-identities",
    ],
)
def test_read_only_resume_hold_requires_final_report_for_current_attempt(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: str,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = f"report-proof-{evidence}"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    tail = tmp_path / f"{dispatch_id}.tail"
    extra: dict[str, object] = {}
    if evidence == "partial-result":
        extra["result_summary"] = "partial output, not a final report"
    elif evidence == "foreign-tail":
        tail.write_text("!COMPLETE: another-dispatch — unrelated\n", encoding="utf-8")
    elif evidence in {"stale-attempt", "stale-attempt-tail"}:
        extra.update(
            attempt_id="current-attempt",
            started_at=goalflight_ledger.utc_now(),
            terminal_marker={
                "kind": "COMPLETE",
                "text": f"{dispatch_id} — stale report",
                "attempt_id": "earlier-attempt",
            },
            result_summary="stale report text",
        )
        if evidence == "stale-attempt-tail":
            tail.write_text(
                f"!COMPLETE: {dispatch_id} — earlier attempt\n", encoding="utf-8"
            )
            os.utime(tail, (time.time() - 5, time.time() - 5))
    elif evidence == "unattributed-marker":
        extra.update(
            attempt_id="current-attempt",
            started_at=goalflight_ledger.utc_now(),
            terminal_marker={
                "kind": "COMPLETE",
                "text": f"{dispatch_id} — missing attempt id",
            },
        )
    elif evidence == "missing-attempt-identities":
        extra["terminal_marker"] = {
            "kind": "COMPLETE",
            "dispatch_id": dispatch_id,
            "text": f"{dispatch_id} — unattributed report",
        }
    elif evidence == "tail-only-current-attempt":
        tail.write_text(
            f"!COMPLETE: {dispatch_id} — earlier attempt\n", encoding="utf-8"
        )
        extra.update(
            attempt_id="current-attempt",
            started_at=goalflight_ledger.utc_now(),
        )
        os.utime(tail, (time.time() + 1, time.time() + 1))
    else:
        tail.write_text(
            f"!COMPLETE: {dispatch_id} — earlier attempt\n", encoding="utf-8"
        )
        extra.update(
            attempt_id="current-attempt",
            started_at=goalflight_ledger.utc_now(),
        )
        os.utime(tail, (time.time() - 5, time.time() - 5))
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        resume_mode="resume",
        worktree_base=head,
        worktree_head=head,
        updated_at=goalflight_ledger.utc_now(),
        agent="codex",
        shape="bash",
        os_sandbox={
            "requested_profile": "read-only",
            "enforced_profile": "read-only",
        },
        stdout_path=str(tail),
        **extra,
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert "resumable terminal dispatch retains checkout" in entry["conditions"]["unowned"]["reason"]


def test_ignored_pool_seat_payload_is_archived_before_removal(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_dispatch_state(tmp_path, monkeypatch)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.delenv("GOALFLIGHT_SEAT_ARCHIVE_DIR")
    durable_home = tmp_path / "durable-home"
    durable_home.mkdir()
    monkeypatch.setenv("HOME", str(durable_home))
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(
        ".goal-flight/\ndocs-private/\nignored.db\nvendor/\n"
        "node_modules\n.pytest_cache/\n*.pyc\n.DS_Store\n",
        encoding="utf-8",
    )
    dispatch_id = "ignored-payload-archive"
    wt = _add_pool_seat(repo, dispatch_id)
    (wt / ".goal-flight" / "seat").mkdir(parents=True)
    (wt / ".goal-flight" / "seat" / "note.md").write_text("keep this\n")
    (wt / "docs-private").mkdir()
    (wt / "docs-private" / "only-copy.md").write_text("private result\n")
    (wt / "ignored.db").write_bytes(b"database\0payload")
    (wt / "node_modules").write_text("not a cache directory\n")
    (wt / ".pytest_cache").mkdir()
    (wt / ".pytest_cache" / "cache.bin").write_bytes(b"regenerable")
    (wt / "scratch.pyc").write_bytes(b"regenerable")
    (wt / ".DS_Store").write_bytes(b"regenerable")
    nested = wt / "vendor" / "nested"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q", "-b", "main")
    _git(nested, "config", "user.email", "nested@example.invalid")
    _git(nested, "config", "user.name", "Nested")
    (nested / "only-copy.txt").write_text("nested commit\n")
    _git(nested, "add", "only-copy.txt")
    _git(nested, "commit", "-qm", "nested work")
    for ignored in (
        ".goal-flight/seat/note.md",
        "docs-private/only-copy.md",
        "ignored.db",
        "node_modules",
        "vendor/nested/",
        ".pytest_cache/cache.bin",
        "scratch.pyc",
        ".DS_Store",
    ):
        _git(wt, "check-ignore", "-q", ignored)
    head = _git_head(wt)
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
    )

    _done, report = _run(repo, "--apply", seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["outcome"] == "removed", entry
    assert not wt.exists()
    archive = Path(entry["ignored_archive"])
    assert archive.parent == durable_home / ".goal-flight" / "seat-archive"
    assert archive.parent != goalflight_ledger.state_dir() / "seat-archive"
    assert archive.name.startswith(f"{wt.name}-")
    assert archive.name.endswith(f"-{head[:12]}.tar.gz")
    import tarfile

    with tarfile.open(archive, "r:gz") as packed:
        names = set(packed.getnames())
        assert ".goal-flight/seat/note.md" in names
        assert "docs-private/only-copy.md" in names
        assert "ignored.db" in names
        assert "node_modules" in names
        assert "vendor/nested/.git/HEAD" in names
        assert not any(name.startswith(".pytest_cache/") for name in names)
        assert "scratch.pyc" not in names
        assert ".DS_Store" not in names
        assert packed.extractfile("docs-private/only-copy.md").read() == b"private result\n"


def test_ignored_payload_roots_match_previous_selection_for_nested_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "ignored-paths"
    worktree.mkdir()
    rng = random.Random(821)
    ignored = {
        "payload/",
        "payload/direct.txt",
        "a/b/",
        "a/b/child.txt",
        "a/bc/",
        "a/bc/child.txt",
        ".pytest_cache/",
        ".pytest_cache/cache.bin",
        "node_modules/",
        "node_modules/package.json",
        "vendor/cache.pyc",
        "folder.pyc/",
        ".DS_Store",
    }
    directory_names = (
        "src",
        "cache",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        "a",
        "b",
        "bc",
        "cache-neighbor",
    )
    for index in range(180):
        depth = rng.randint(1, 4)
        directories = [rng.choice(directory_names) for _ in range(depth)]
        leaf = rng.choice(
            (f"item-{index}.txt", f"item-{index}.pyc", ".DS_Store")
        )
        ignored.add("/".join((*directories, leaf)))

    for raw_item in ignored:
        item_path = worktree / raw_item.rstrip("/")
        if raw_item.endswith("/"):
            item_path.mkdir(parents=True, exist_ok=True)
        else:
            item_path.parent.mkdir(parents=True, exist_ok=True)
            item_path.write_bytes(b"payload")

    def old_roots() -> list[str]:
        protected: list[str] = []
        for raw_item in sorted(
            ignored, key=lambda value: (value.count("/"), value)
        ):
            item = raw_item.rstrip("/")
            parts = item.split("/")
            if item.startswith("/") or any(part == ".." for part in parts):
                raise AssertionError(f"unsafe test path: {item!r}")
            item_is_directory = False
            under_regenerable_directory = False
            for index, part in enumerate(parts):
                candidate = worktree.joinpath(*parts[: index + 1])
                is_directory = stat.S_ISDIR(candidate.lstat().st_mode)
                if (
                    is_directory
                    and part
                    in goalflight_worktree_gc._REGENERABLE_IGNORED_PATTERNS[
                        "directories"
                    ]
                ):
                    under_regenerable_directory = True
                    break
                if index == len(parts) - 1:
                    item_is_directory = is_directory
            if under_regenerable_directory:
                continue
            if not item_is_directory and any(
                fnmatch.fnmatchcase(
                    parts[-1], pattern
                )
                for pattern in goalflight_worktree_gc._REGENERABLE_IGNORED_PATTERNS[
                    "files"
                ]
            ):
                continue
            if not any(
                item == parent or item.startswith(parent + "/")
                for parent in protected
            ):
                protected.append(item)
        return protected

    def fake_git(_cwd: Path, *_args: str, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0, stdout="\0".join(ignored) + "\0", stderr=""
        )

    monkeypatch.setattr(goalflight_worktree_gc, "_git", fake_git)
    actual, error = goalflight_worktree_gc._ignored_payload_roots(
        worktree, deadline=None
    )

    assert error is None
    assert actual == old_roots()
    assert "a/b" in actual and "a/bc" in actual
    assert "payload" in actual and "payload/direct.txt" not in actual
    assert not any(path.startswith(".pytest_cache/") for path in actual)
    assert ".DS_Store" not in actual and "vendor/cache.pyc" not in actual


def test_ignored_path_protection_lookups_scale_with_path_depth() -> None:
    class CountingSet(set[str]):
        lookups = 0

        def __contains__(self, item: object) -> bool:
            self.lookups += 1
            return super().__contains__(item)

    protected = CountingSet()
    path_count = 200_000
    for index in range(path_count):
        item = f"payload/group-{index // 1000}/item-{index}.dat"
        assert not goalflight_worktree_gc._ignored_path_has_protected_ancestor(
            item, protected
        )
        protected.add(item)

    # Each two-level path checks itself and its two slash-delimited ancestors.
    assert protected.lookups == path_count * 3


@pytest.mark.parametrize("failure", ["size-limit", "archive-directory"])
def test_ignored_pool_seat_archive_failure_retains_checkout(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _isolate_dispatch_state(tmp_path, monkeypatch)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    (repo / ".git" / "info" / "exclude").write_text("precious.bin\n", encoding="utf-8")
    dispatch_id = f"ignored-payload-{failure}"
    wt = _add_pool_seat(repo, dispatch_id)
    payload = wt / "precious.bin"
    if failure == "size-limit":
        with payload.open("wb") as stream:
            stream.truncate(256 * 1024 * 1024 + 1)
    else:
        payload.write_bytes(b"precious")
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
    )
    if failure == "archive-directory":
        blocked_archive_dir = Path(os.environ["GOALFLIGHT_SEAT_ARCHIVE_DIR"])
        blocked_archive_dir.parent.mkdir(parents=True)
        blocked_archive_dir.write_text("not a directory\n", encoding="utf-8")

    _done, report = _run(repo, "--apply", seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["outcome"] == "retained", entry
    assert "ignored content could not be preserved" in entry["reason"]
    if failure == "size-limit":
        assert "exceeds 256 MiB" in entry["reason"]
    else:
        assert "archive failed" in entry["reason"]
    assert wt.is_dir()


@pytest.mark.parametrize("overlap", ["archive-inside-seat", "seat-inside-archive"])
def test_ignored_payload_archive_overlap_retains_seat(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    overlap: str,
) -> None:
    _isolate_dispatch_state(tmp_path, monkeypatch)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    (repo / ".git" / "info" / "exclude").write_text(
        "precious.bin\n", encoding="utf-8"
    )
    dispatch_id = f"archive-overlap-{overlap}"
    wt = _add_pool_seat(repo, dispatch_id)
    (wt / "precious.bin").write_bytes(b"only copy")
    archive_dir = (
        wt / "nested-archive"
        if overlap == "archive-inside-seat"
        else repo / "worktrees"
    )
    monkeypatch.setenv("GOALFLIGHT_SEAT_ARCHIVE_DIR", str(archive_dir))
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
    )

    _done, report = _run(repo, "--apply", seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["outcome"] == "retained", entry
    assert "archive path overlaps worktree" in entry["reason"]
    assert wt.is_dir()
    assert (wt / "precious.bin").read_bytes() == b"only copy"


def test_case_variant_ignored_payload_archive_overlap_retains_seat(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_dispatch_state(tmp_path, monkeypatch)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    (repo / ".git" / "info" / "exclude").write_text(
        "precious.bin\n", encoding="utf-8"
    )
    dispatch_id = "archive-overlap-case-variant"
    wt = _add_pool_seat(repo, dispatch_id)
    payload = wt / "precious.bin"
    payload.write_bytes(b"only copy")
    _git(wt, "check-ignore", "-q", "precious.bin")
    case_variant = wt.parent / wt.name.swapcase()
    try:
        same_directory = os.path.samefile(wt, case_variant)
    except OSError:
        same_directory = False
    if not same_directory:
        pytest.skip("test volume is case-sensitive")

    archive_dir = case_variant / "nested-archive"
    monkeypatch.setenv("GOALFLIGHT_SEAT_ARCHIVE_DIR", str(archive_dir))
    archive, error = goalflight_worktree_gc._archive_ignored_payload(wt, deadline=None)
    assert archive is None
    assert error is not None and "archive path overlaps worktree" in error
    assert wt.is_dir()
    assert payload.read_bytes() == b"only copy"


def test_ignored_payload_archive_publish_is_create_only(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_dispatch_state(tmp_path, monkeypatch)
    (repo / ".git" / "info" / "exclude").write_text(
        "precious.bin\n", encoding="utf-8"
    )
    wt = _add_pool_seat(repo, "archive-publish-race")
    (wt / "precious.bin").write_bytes(b"only copy")
    real_link = os.link
    raced_archives: list[Path] = []

    def create_existing_name_then_link(source, destination, *args, **kwargs):
        target = Path(destination)
        target.write_bytes(b"existing archive")
        raced_archives.append(target)
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(goalflight_worktree_gc.os, "link", create_existing_name_then_link)
    archive, error = goalflight_worktree_gc._archive_ignored_payload(wt, deadline=None)

    assert archive is None
    assert error and "already exists" in error
    assert raced_archives and raced_archives[0].read_bytes() == b"existing archive"
    assert (wt / "precious.bin").read_bytes() == b"only copy"


def test_ignored_cache_name_regular_file_is_archived(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_dispatch_state(tmp_path, monkeypatch)
    (repo / ".git" / "info" / "exclude").write_text(
        "node_modules\n", encoding="utf-8"
    )
    wt = _add_pool_seat(repo, "ignored-cache-name-file")
    (wt / "node_modules").write_text("preserve file", encoding="utf-8")

    archive, error = goalflight_worktree_gc._archive_ignored_payload(wt, deadline=None)

    assert error is None
    assert archive is not None
    import tarfile

    with tarfile.open(archive, "r:gz") as packed:
        assert packed.extractfile("node_modules").read() == b"preserve file"


def test_ignored_cache_name_symlink_is_archived(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_dispatch_state(tmp_path, monkeypatch)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    (repo / ".git" / "info" / "exclude").write_text(
        "node_modules\n", encoding="utf-8"
    )
    dispatch_id = "ignored-cache-name-symlink"
    wt = _add_pool_seat(repo, dispatch_id)
    target = tmp_path / "outside-target"
    target.write_text("preserve link", encoding="utf-8")
    (wt / "node_modules").symlink_to(target)
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
    )

    _done, report = _run(repo, "--apply", seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["outcome"] == "removed", entry
    import tarfile

    with tarfile.open(entry["ignored_archive"], "r:gz") as packed:
        member = packed.getmember("node_modules")
        assert member.issym()
        assert member.linkname == str(target)


def test_pool_seat_with_paused_rebase_is_retained(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    dispatch_id = "paused-rebase"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt, "first.txt")
    _commit_in(wt, "second.txt")
    editor = tmp_path / "sequence-editor.py"
    editor.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "todo = pathlib.Path(sys.argv[1])\n"
        "todo.write_text(todo.read_text().replace('pick ', 'edit ', 1))\n",
        encoding="utf-8",
    )
    editor.chmod(0o755)
    env = os.environ.copy()
    env["GIT_SEQUENCE_EDITOR"] = str(editor)
    rebase = subprocess.run(
        ["git", "-C", str(wt), "rebase", "-i", "HEAD~2"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rebase.returncode == 0, rebase.stderr
    git_dir_result = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--git-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    git_dir = Path(git_dir_result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = wt / git_dir
    assert (git_dir / "rebase-merge").is_dir()
    status = subprocess.run(
        ["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
    )

    _done, report = _run(repo, seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert "unfinished Git operation (rebase-merge)" in entry["reason"]
    assert wt.is_dir()


def test_pool_seat_with_clean_submodule_gitlink_is_explicitly_retained(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    subrepo = tmp_path / "subrepo"
    subrepo.mkdir()
    _git(subrepo, "init", "-q", "-b", "main")
    _git(subrepo, "config", "user.email", "submodule@example.invalid")
    _git(subrepo, "config", "user.name", "Submodule")
    (subrepo / "tracked.txt").write_text("submodule\n")
    _git(subrepo, "add", "tracked.txt")
    _git(subrepo, "commit", "-qm", "submodule base")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(subrepo), "modules/sub")
    _git(repo, "commit", "-qm", "add submodule")

    dispatch_id = "clean-submodule-gitlink"
    wt = _add_pool_seat(repo, dispatch_id)
    _git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "--init")
    status = subprocess.run(
        ["git", "-C", str(wt), "status", "--porcelain", "--ignore-submodules=none"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
    )

    _done, report = _run(repo, seed_terminal=False)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert "submodule gitlink" in entry["reason"]
    assert wt.is_dir()


@pytest.mark.parametrize(
    "agent,shape,argv_source,dispatch_argv,posture",
    [
        (
            "codex",
            "bash",
            "dispatch_argv",
            ["--agent", "codex", "--read-only", "--", "python", "-c", "write()"],
            {
                "requested_profile": "read-only",
                "enforced_profile": "read-only",
            },
        ),
        (
            "cursor",
            "acp",
            "dispatch_argv",
            ["--agent", "cursor", "--os-sandbox", "read-only"],
            {"requested_profile": "read-only"},
        ),
        (
            "codex",
            "bash",
            "request.dispatch_argv",
            ["--agent", "codex", "--read-only", "--", "python", "-c", "write()"],
            {
                "requested_profile": "read-only",
                "enforced_profile": "read-only",
            },
        ),
    ],
    ids=["raw-worker-command", "non-enforcing-agent", "nested-request-worker-command"],
)
def test_read_only_declaration_that_can_write_keeps_resume_hold(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    shape: str,
    argv_source: str,
    dispatch_argv: list[str],
    posture: dict[str, str],
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = f"writer-declaration-{agent}-{shape}"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text(f"!COMPLETE: {dispatch_id} — writer report\n", encoding="utf-8")
    started_at = goalflight_ledger.utc_now()
    os.utime(tail, (time.time() + 1, time.time() + 1))
    argv_record = (
        {"request": {"dispatch_argv": dispatch_argv}}
        if argv_source == "request.dispatch_argv"
        else {"dispatch_argv": dispatch_argv}
    )
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        resume_mode="resume",
        worktree_base=head,
        worktree_head=head,
        updated_at=goalflight_ledger.utc_now(),
        started_at=started_at,
        agent=agent,
        shape=shape,
        read_only=True,
        os_sandbox=posture,
        stdout_path=str(tail),
        **argv_record,
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert "resumable terminal dispatch retains checkout" in entry["conditions"]["unowned"]["reason"]


def test_terminal_writer_pool_seat_inside_resume_window_is_retained(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    dispatch_id = "writer-terminal"
    wt = _add_pool_seat(repo, dispatch_id)
    _commit_in(wt)
    head = _git_head(wt)
    _merge_into_main(repo, f"worktree/{dispatch_id}")
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text(f"!COMPLETE: {dispatch_id} — writer report\n", encoding="utf-8")
    _write_ledger(
        dispatch_id,
        "complete",
        wt,
        project_root=str(repo),
        terminal_state="complete",
        resume_mode="resume",
        worktree_base=head,
        worktree_head=head,
        updated_at=goalflight_ledger.utc_now(),
        os_sandbox={"requested_profile": "workspace-write"},
        stdout_path=str(tail),
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert "resumable terminal dispatch retains checkout" in entry["conditions"]["unowned"]["reason"]
    assert wt.is_dir()


def test_gc_revalidates_held_pool_lock_identity(
    tmp_path: Path, repo: Path
) -> None:
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "held-lock-owner")
    seat = lease.path
    lease.release()
    held, error = goalflight_worktree_gc._acquire_pool_action_lock(repo, str(seat))
    assert held is not None, error
    lock_path = goalflight_worktree_pool.worktree_lock_path_for_path(repo, seat)
    parent = lock_path.parent
    backup = parent.with_name(parent.name + ".real")
    parent.rename(backup)
    parent.mkdir()
    (parent / lock_path.name).touch()
    try:
        result = goalflight_worktree_gc.check_pool_unlocked(
            repo, str(seat), held_lock=held
        )
        assert result["verdict"] == goalflight_worktree_gc.UNKNOWN, result
    finally:
        held.close()
        (parent / lock_path.name).unlink()
        parent.rmdir()
        backup.rename(parent)


def test_gc_unregistered_pool_lock_requires_action_exclusion(
    repo: Path,
) -> None:
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "unregistered-gc")
    seat = lease.path
    lease.release()
    lock_path = goalflight_worktree_pool.worktree_lock_path_for_path(repo, seat)
    registry_path = goalflight_worktree_pool._lock_registry_path(
        goalflight_worktree_pool._git_common_dir(repo)
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["locks"].pop(
        goalflight_worktree_pool._lock_registry_key(lock_path), None
    )
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    handle, error = goalflight_worktree_gc._acquire_pool_action_lock(
        repo, str(seat)
    )
    if handle is not None:
        handle.close()
    assert handle is None
    assert error and "action lock unavailable" in error


def test_adhoc_worktree_named_wt_n_is_reclaimable(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Basename wt-9 without a seat lock is litter, not an immortal pool seat."""
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    wt = _add_worktree(repo, tmp_path, "wt-9")
    _commit_in(wt)
    _merge_into_main(repo, "wt-9")

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "remove", entry
    assert "pool seat" not in entry["reason"]

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry.get("outcome") == "removed", entry
    assert not wt.is_dir()
    assert os.path.realpath(wt) not in _worktree_paths(repo)


def test_unknown_pool_seat_registration_retains(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the pool cannot say whether a managed-looking path is registered, retain."""
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "not-a-number")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    managed = repo / "worktrees"
    managed.mkdir()
    wt = managed / "wt-1"
    _git(repo, "worktree", "add", "-q", "-b", "unknown-reg-wt1", str(wt))
    _commit_in(wt)
    _merge_into_main(repo, "unknown-reg-wt1")

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert "registration unknown" in entry["reason"]
    assert entry.get("pool_seat", {}).get("verdict") == "unknown", entry
    assert wt.is_dir()

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert wt.is_dir()
    assert os.path.realpath(wt) in _worktree_paths(repo)


def test_idle_timeout_identity_live_worker_is_not_reclaimed(
    tmp_path: Path, repo: Path
) -> None:
    """idle_timeout is a liveness verdict, not proof the process is gone."""
    wt = _add_worktree(repo, tmp_path, "idle-live")
    _commit_in(wt)
    _merge_into_main(repo, "idle-live")
    identity = goalflight_compat.process_start_identity(os.getpid())
    assert identity and identity.get("start_token"), identity
    _write_ledger(
        "idle-live-w1",
        "idle_timeout",
        wt,
        terminal_state="idle_timeout",
        worker_pid=os.getpid(),
        worker_identity=identity,
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["unowned"]["verdict"] == "no", entry
    assert "idle-live-w1" in entry["conditions"]["unowned"]["reason"]
    assert "identity-live" in entry["conditions"]["unowned"]["reason"]
    assert "non-terminal" not in entry["conditions"]["unowned"]["reason"]
    assert wt.is_dir()


def test_idle_timeout_dead_identity_does_not_own_the_tree(
    tmp_path: Path, repo: Path
) -> None:
    """Once pid+start_token prove the generation is gone, idle_timeout does not retain."""
    wt = _add_worktree(repo, tmp_path, "idle-dead")
    _commit_in(wt)
    _merge_into_main(repo, "idle-dead")
    _write_ledger(
        "idle-dead-w1",
        "idle_timeout",
        wt,
        terminal_state="idle_timeout",
        worker_pid=2**30,
        worker_identity={"pid": 2**30, "start_token": "missing:generation"},
    )

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "remove", entry
    assert entry["conditions"]["unowned"]["verdict"] == "yes", entry


def test_prelaunch_child_session_handles_do_not_keep_parent_checkout(
    repo: Path,
) -> None:
    seat = _add_pool_seat(repo, "prelaunch-parent")
    parent = {
        "dispatch_id": "prelaunch-parent",
        "project_root": str(repo),
        "state": "failed",
        "terminal_state": "failed",
        "worker_pid": None,
        "worker_cwd": str(seat),
        "worktree_path": str(seat),
    }
    child = {
        "dispatch_id": "prelaunch-child",
        "project_root": str(repo),
        "parent_dispatch_id": parent["dispatch_id"],
        "state": "failed",
        "terminal_state": "error",
        "prelaunch_failure": True,
        "worker_pid": None,
        "worker_identity": None,
        "codex_session_id": "inherited-parent-session",
        "engine_session_id": "inherited-parent-session",
        "dispatch_argv": ["--cwd", str(seat)],
        "ended_at": goalflight_ledger.utc_now(),
    }
    index = goalflight_worktree_gc.LedgerIndex([parent, child], [])

    assert goalflight_worktree_gc._record_cwd_matches(
        child, str(seat), ledger_index=index
    )
    assert not goalflight_worktree_gc._record_owns_path(
        child, str(seat), ledger_index=index
    )
