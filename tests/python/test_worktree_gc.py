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

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_compat  # noqa: E402
import goalflight_ledger  # noqa: E402
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


def _commit_in(worktree: Path, filename: str = "work.txt") -> None:
    (worktree / filename).write_text("progress\n")
    _git(worktree, "add", filename)
    _git(worktree, "commit", "-qm", "worker progress")


def _merge_into_main(repo: Path, branch: str) -> None:
    _git(repo, "merge", "-q", "--ff-only", branch)


def _write_ledger(dispatch_id: str, state: str, worker_cwd: Path, **extra: object) -> None:
    runs = goalflight_ledger.runs_dir(create=True)
    record = {
        "dispatch_id": dispatch_id,
        "state": state,
        "worker_cwd": str(worker_cwd),
        "project_root": str(worker_cwd),
    }
    record.update(extra)
    name = goalflight_compat.safe_dispatch_filename(dispatch_id)
    (runs / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


def _run(repo_arg: Path, *extra: str) -> tuple[subprocess.CompletedProcess[str], dict]:
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
    assert entry["conditions"]["clean"]["verdict"] == "yes"
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
    assert entry["conditions"]["clean"]["verdict"] == "yes"
    assert entry["conditions"]["unowned"]["verdict"] == "no"
    assert entry["conditions"]["not_current"]["verdict"] == "yes"


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
    assert entry["conditions"]["clean"]["verdict"] == "yes"
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


def test_detached_head_is_unknown_and_retained(tmp_path: Path, repo: Path) -> None:
    """Detached HEAD: merge state cannot be evaluated, and unknown retains."""
    wt = tmp_path / "detached"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert entry["conditions"]["merged"]["verdict"] == "unknown"


# --------------------------------------------------------------------------
# Removal, prune, and reporting.


def test_merged_clean_terminal_owned_worktree_is_removed_with_apply(
    tmp_path: Path, repo: Path
) -> None:
    """The happy path: all four pass; a TERMINAL dispatch record does not retain."""
    wt = _add_worktree(repo, tmp_path, "done")
    _commit_in(wt)
    _merge_into_main(repo, "done")
    _write_ledger("t111-w1", "complete", wt)  # terminal: no claim

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


def test_report_only_is_default_and_prints_retention_reasons(
    tmp_path: Path, repo: Path
) -> None:
    """No --apply: nothing is touched, survivors say WHY, exit code is 0."""
    removable = _add_worktree(repo, tmp_path, "sweepable")
    _commit_in(removable)
    _merge_into_main(repo, "sweepable")
    kept = _add_worktree(repo, tmp_path, "kept")
    _commit_in(kept, "other.txt")  # unmerged

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


def test_pool_seat_is_never_reclaimed_as_litter(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered seat is retained even when merged+clean+unowned."""
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "register-seat")
    wt = lease.path
    lease.release()
    assert wt.name == "s-1"

    _done, report = _run(repo)
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert "pool seat" in entry["reason"]
    assert entry.get("pool_seat", {}).get("verdict") == "yes", entry
    assert wt.is_dir()

    done, report = _run(repo, "--apply")
    assert done.returncode == 0
    entry = _entry(report, wt)
    assert entry["decision"] == "retain", entry
    assert wt.is_dir()
    assert os.path.realpath(wt) in _worktree_paths(repo)


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
