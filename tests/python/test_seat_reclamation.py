"""Seat reuse requires terminal state and a dead worker generation."""

import json
import asyncio
import fcntl
import hashlib
import os
import subprocess
import sys
import time
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_dispatch_worktree_pool import _make_repo, _git, _env, _dispatch_cmd
import goalflight_dispatch as dispatch
import goalflight_ledger as ledger
import goalflight_worktree_pool as pool


@pytest.fixture
def holder(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "1")
    repo = _make_repo(tmp_path)
    lease = pool.acquire_worktree_seat(repo, "old", controller_label="owner")
    path = lease.path
    lease.release()
    row = {"dispatch_id": "old", "state": "complete", "controller_label": "owner",
           "worker_pid": 34567, "worker_identity": {"pid": 34567, "start_token": "old-token"}}
    monkeypatch.setattr(ledger, "read_record", lambda ident: row if ident == "old" else None)
    monkeypatch.setattr(pool.goalflight_compat, "process_identity_matches", lambda pid, token: False)
    return repo, path, row


def test_terminal_holder_pins_head_and_dirty_files(holder):
    repo, path, row = holder
    (path / "tracked.txt").write_text("committed\n")
    _git(path, "commit", "-am", "worker change")
    head = _git(path, "rev-parse", "HEAD")
    (path / "tracked.txt").write_text("dirty\n")
    (path / "new.txt").write_text("untracked\n")
    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == path
        assert _git(repo, "rev-parse", "refs/goalflight/keep/old/head") == head
        refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/goalflight/keep/old/dirty-*").splitlines()
        assert len(refs) == 1
        assert _git(repo, "show", refs[0] + ":tracked.txt") == "dirty"
        assert _git(repo, "show", refs[0] + ":new.txt") == "untracked"
        assert _git(path, "status", "--porcelain") == ""


@pytest.mark.parametrize("live", [True, None])
def test_terminal_live_or_unknown_holder_retained(holder, monkeypatch, live):
    repo, path, row = holder
    monkeypatch.setattr(pool.goalflight_compat, "process_identity_matches", lambda pid, token: live)
    before = _git(path, "rev-parse", "HEAD")
    (path / "new.txt").write_text("untouched")
    with pytest.raises(pool.WorktreeSeatUnavailable):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "rev-parse", "HEAD") == before
    assert _git(path, "branch", "--show-current") == "worktree/old"
    assert (path / "new.txt").read_text() == "untouched"


@pytest.mark.parametrize("state", ["running", "unknown"])
def test_nonterminal_holder_retained(holder, state):
    repo, path, row = holder
    row["state"] = state
    with pytest.raises(pool.WorktreeSeatUnavailable):
        pool.acquire_worktree_seat(repo, "next")


def test_quarantine_failure_preserves_real_index(holder, monkeypatch):
    repo, path, row = holder
    (path / ".gitignore").write_text("*.bin\n")
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "ignore binary files")
    (path / "valuable.bin").write_text("staged ignored\n")
    _git(path, "add", "-f", "valuable.bin")
    (path / "tracked.txt").write_text("unstaged\n")
    original = _git(path, "diff", "--cached")
    real_git = pool._git
    def fail_ref(cwd, *args, **kwargs):
        if args[0] == "update-ref" and "dirty-" in args[1]:
            raise pool.WorktreeSeatError("pin failed")
        return real_git(cwd, *args, **kwargs)
    monkeypatch.setattr(pool, "_git", fail_ref)
    with pytest.raises(pool.WorktreeSeatError):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "diff", "--cached") == original
    assert (path / "tracked.txt").read_text() == "unstaged\n"


def test_quarantine_preserves_force_staged_ignored_file(holder):
    repo, path, row = holder
    (path / ".gitignore").write_text("*.bin\n")
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "ignore binary files")
    (path / "valuable.bin").write_text("keep this\n")
    _git(path, "add", "-f", "valuable.bin")
    (path / "tracked.txt").write_text("ordinary edit\n")

    with pool.acquire_worktree_seat(repo, "next") as lease:
        refs = _git(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            "refs/goalflight/keep/old/dirty-*",
        ).splitlines()
        assert len(refs) == 1
        assert _git(repo, "show", refs[0] + ":valuable.bin") == "keep this"


def test_quarantine_preserves_tracked_goalflight_edits(holder):
    repo, path, row = holder
    notes = path / ".goal-flight" / "seat" / "memory.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("committed\n")
    _git(path, "add", "-f", ".goal-flight/seat/memory.md")
    _git(path, "commit", "-m", "track seat note")
    notes.write_text("unique unstaged\n")
    staged = path / ".goal-flight" / "seat" / "staged.md"
    staged.write_text("keep staged\n")
    _git(path, "add", "-f", ".goal-flight/seat/staged.md")

    with pool.acquire_worktree_seat(repo, "next"):
        refs = _git(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            "refs/goalflight/keep/old/dirty-*",
        ).splitlines()
        assert len(refs) == 1
        assert _git(repo, "show", refs[0] + ":.goal-flight/seat/memory.md") == "unique unstaged"
        assert _git(repo, "show", refs[0] + ":.goal-flight/seat/staged.md") == "keep staged"


def test_ignored_collision_retains_seat(holder):
    repo, path, row = holder
    base_collision = repo / "generated"
    base_collision.mkdir()
    (base_collision / "out.bin").write_text("base\n")
    _git(repo, "add", "generated/out.bin")
    _git(repo, "commit", "-m", "add base collision")
    collision = path / "generated"
    (path / ".gitignore").write_text("generated/out.bin\n")
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "add ignored collision")
    collision.mkdir()
    (collision / "out.bin").write_text("must survive\n")

    with pytest.raises(pool.WorktreeSeatResetRefused, match="ignored path"):
        pool.acquire_worktree_seat(repo, "next")
    assert (collision / "out.bin").read_text() == "must survive\n"
    assert _git(path, "branch", "--show-current") == "worktree/old"


def test_ignored_head_collision_retains_seat(holder):
    repo, path, row = holder
    artifact = path / "artifact"
    artifact.mkdir()
    (artifact / "file").write_text("tracked\n")
    _git(path, "add", "artifact/file")
    _git(path, "commit", "-m", "track artifact")
    (path / ".gitignore").write_text("artifact\n")
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "ignore artifact")
    (artifact / "file").unlink()
    artifact.rmdir()
    artifact.write_text("must survive reset\n")

    with pytest.raises(pool.WorktreeSeatResetRefused, match="ignored path"):
        pool.acquire_worktree_seat(repo, "next")
    assert artifact.read_text() == "must survive reset\n"
    assert _git(path, "branch", "--show-current") == "worktree/old"
    assert _git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/goalflight/keep/old",
        "refs/heads/goalflight/quarantine",
    ) == ""


@pytest.mark.parametrize(
    ("ignore_pattern", "tracked_path", "ignored_path", "ignored_is_file", "reuses"),
    [
        (
            "__pycache__/",
            "rf/coupling/tracked.py",
            "rf/coupling/__pycache__",
            False,
            True,
        ),
        ("build/", "build/x", "build", False, False),
        ("out", "out/x", "out", True, False),
    ],
)
def test_ignored_paths_match_tree_collisions_exactly(
    holder, ignore_pattern, tracked_path, ignored_path, ignored_is_file, reuses
):
    repo, path, row = holder
    (path / ".gitignore").write_text(ignore_pattern + "\n")
    if ignored_path == "rf/coupling/__pycache__":
        anchor = path / "rf/coupling/existing.py"
        anchor.parent.mkdir(parents=True)
        anchor.write_text("existing\n")
        _git(path, "add", ".gitignore", "rf/coupling/existing.py")
    else:
        _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "ignore reset fixture")

    target = repo / tracked_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("target\n")
    if reuses:
        (repo / ".gitignore").write_text(ignore_pattern + "\n")
        _git(repo, "add", ".gitignore", tracked_path)
    else:
        _git(repo, "add", tracked_path)
    _git(repo, "commit", "-m", "add reset fixture target")

    ignored = path / ignored_path
    if ignored_is_file:
        ignored.write_text("must survive reset\n")
    else:
        ignored.mkdir(parents=True)
        (ignored / "payload").write_text("must survive reset\n")

    if reuses:
        with pool.acquire_worktree_seat(repo, "next") as lease:
            assert lease.path == path
            assert ignored.exists()
    else:
        with pytest.raises(pool.WorktreeSeatResetRefused, match="ignored path"):
            pool.acquire_worktree_seat(repo, "next")
        assert ignored.exists()
        assert _git(path, "branch", "--show-current") == "worktree/old"


def test_ignored_tracked_file_parent_type_clash_is_collision(tmp_path, monkeypatch):
    def fake_git_nul(_cwd, *args):
        return "parent/child\0" if args[0] == "ls-files" else "parent\0"

    monkeypatch.setattr(pool, "_git_nul", fake_git_nul)
    with pytest.raises(pool.WorktreeSeatResetRefused, match="ignored path"):
        pool._refuse_ignored_tree_collisions(
            tmp_path, head="HEAD", target="target"
        )


def test_casefolded_ignored_collision_retains_seat(holder):
    repo, path, row = holder
    _git(repo, "config", "core.ignorecase", "true")
    (path / ".gitignore").write_text("foo\n")
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "ignore case collision")
    (repo / "Foo").write_text("target\n")
    _git(repo, "add", "Foo")
    _git(repo, "commit", "-m", "add case collision target")
    ignored = path / "foo"
    ignored.write_text("must survive\n")

    with pytest.raises(pool.WorktreeSeatResetRefused, match="ignored path"):
        pool.acquire_worktree_seat(repo, "next")
    assert ignored.read_text() == "must survive\n"
    assert _git(path, "branch", "--show-current") == "worktree/old"


def test_retained_legacy_ring_seat_counts_toward_cap(holder):
    repo, path, row = holder
    global_lock = pool.worktree_lock_path_for_path(repo, path)
    legacy_path = repo / "worktrees" / "legacy" / "s-1"
    legacy_path.parent.mkdir(parents=True)
    _git(repo, "worktree", "move", str(path), str(legacy_path))
    legacy_lock = pool._seat_lock_root(repo, controller_label="legacy") / "s-1.lock"
    legacy_lock.parent.mkdir(parents=True)
    global_lock.replace(legacy_lock)
    global_ring = pool._ring_state_path(pool._seat_lock_root(repo))
    if global_ring.exists():
        global_ring.unlink()

    (legacy_path / ".gitignore").write_text("build/\n")
    _git(legacy_path, "add", ".gitignore")
    _git(legacy_path, "commit", "-m", "ignore retained legacy fixture")
    target = repo / "build" / "x"
    target.parent.mkdir()
    target.write_text("target\n")
    _git(repo, "add", "build/x")
    _git(repo, "commit", "-m", "add retained legacy target")
    collision = legacy_path / "build"
    collision.mkdir()
    (collision / "payload").write_text("must survive\n")

    with pytest.raises(pool.WorktreeSeatResetRefused, match="all available worktrees"):
        pool.acquire_worktree_seat(repo, "next")
    assert not (repo / "worktrees" / "s-1").exists()
    assert (collision / "payload").read_text() == "must survive\n"


def test_unregistered_live_legacy_ring_lock_reports_holder_and_counts_toward_cap(
    holder, monkeypatch
):
    repo, path, row = holder
    row["state"] = "running"
    monkeypatch.setattr(
        pool.goalflight_compat,
        "process_identity_matches",
        lambda pid, token: True,
    )
    global_lock = pool.worktree_lock_path_for_path(repo, path)
    legacy_path = repo / "worktrees" / "legacy" / "s-1"
    legacy_path.parent.mkdir(parents=True)
    _git(repo, "worktree", "move", str(path), str(legacy_path))
    legacy_lock = pool._seat_lock_root(repo, controller_label="legacy") / "s-1.lock"
    legacy_lock.parent.mkdir(parents=True)
    global_lock.replace(legacy_lock)

    registry_path = pool._lock_registry_path(pool._git_common_dir(repo))
    registry = json.loads(registry_path.read_text())
    registry["locks"].pop(pool._lock_registry_key(global_lock), None)
    registry["locks"].pop(
        pool._lock_registry_key(pool._seat_lock_root(repo) / "allocation.lock"),
        None,
    )
    registry_path.write_text(json.dumps(registry))

    with pytest.raises(pool.WorktreeSeatUnavailable) as caught:
        pool.acquire_worktree_seat(repo, "next")
    message = str(caught.value)
    assert "lock holder: s-1=unknown-dispatch" not in message
    assert "s-1=old controller=owner state=running worker_pid=34567" in message
    assert legacy_path.is_dir()


def test_unregistered_terminal_legacy_ring_lock_is_reused_with_free_slot(
    holder, monkeypatch
):
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    repo, path, _row = holder
    global_lock = pool.worktree_lock_path_for_path(repo, path)
    legacy_path = repo / "worktrees" / "legacy" / "s-1"
    legacy_path.parent.mkdir(parents=True)
    _git(repo, "worktree", "move", str(path), str(legacy_path))
    legacy_lock = pool._seat_lock_root(repo, controller_label="legacy") / "s-1.lock"
    legacy_lock.parent.mkdir(parents=True)
    global_lock.replace(legacy_lock)
    global_ring = pool._ring_state_path(pool._seat_lock_root(repo))
    if global_ring.exists():
        global_ring.unlink()

    registry_path = pool._lock_registry_path(pool._git_common_dir(repo))
    registry = json.loads(registry_path.read_text())
    registry["locks"].pop(pool._lock_registry_key(global_lock), None)
    registry["locks"].pop(
        pool._lock_registry_key(pool._seat_lock_root(repo) / "allocation.lock"),
        None,
    )
    registry_path.write_text(json.dumps(registry))

    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == legacy_path


def test_unregistered_terminal_legacy_ring_lock_is_reused_when_pool_is_full(
    holder, monkeypatch
):
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "1")
    repo, path, _row = holder
    global_lock = pool.worktree_lock_path_for_path(repo, path)
    legacy_path = repo / "worktrees" / "legacy" / "s-1"
    legacy_path.parent.mkdir(parents=True)
    _git(repo, "worktree", "move", str(path), str(legacy_path))
    legacy_lock = pool._seat_lock_root(repo, controller_label="legacy") / "s-1.lock"
    legacy_lock.parent.mkdir(parents=True)
    global_lock.replace(legacy_lock)
    global_ring = pool._ring_state_path(pool._seat_lock_root(repo))
    if global_ring.exists():
        global_ring.unlink()

    registry_path = pool._lock_registry_path(pool._git_common_dir(repo))
    registry = json.loads(registry_path.read_text())
    registry["locks"].pop(pool._lock_registry_key(global_lock), None)
    registry["locks"].pop(
        pool._lock_registry_key(pool._seat_lock_root(repo) / "allocation.lock"),
        None,
    )
    registry_path.write_text(json.dumps(registry))

    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == legacy_path


@pytest.mark.parametrize("metadata", ["live", "empty"])
def test_unregistered_missing_checkout_requires_holder_evidence(
    holder, monkeypatch, metadata
):
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    repo, path, row = holder
    global_lock = pool.worktree_lock_path_for_path(repo, path)
    _git(repo, "worktree", "remove", "--force", str(path))
    registry_path = pool._lock_registry_path(pool._git_common_dir(repo))
    registry = json.loads(registry_path.read_text())
    registry["locks"].pop(pool._lock_registry_key(global_lock), None)
    registry["locks"].pop(
        pool._lock_registry_key(pool._seat_lock_root(repo) / "allocation.lock"),
        None,
    )
    registry_path.write_text(json.dumps(registry))
    if metadata == "live":
        row["state"] = "running"
        monkeypatch.setattr(
            pool.goalflight_compat,
            "process_identity_matches",
            lambda pid, token: True,
        )
    else:
        global_lock.write_text("", encoding="utf-8")

    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == repo / "worktrees" / "s-2"


def test_unreadable_present_checkout_is_unknown_for_holder_validation(holder, monkeypatch):
    repo, path, row = holder
    _git(path, "checkout", "-q", "-b", "worktree/live")
    records = {
        "old": row,
        "live": {
            "dispatch_id": "live",
            "state": "running",
            "worker_pid": 45678,
            "worker_identity": {"pid": 45678, "start_token": "live-token"},
        },
    }
    monkeypatch.setattr(ledger, "read_record", lambda ident: records.get(ident))
    monkeypatch.setattr(
        pool.goalflight_compat,
        "process_identity_matches",
        lambda pid, token: token == "live-token",
    )

    parent = path.parent
    parent.chmod(0)
    try:
        try:
            os.stat(path)
        except PermissionError:
            pass
        else:
            pytest.skip("filesystem does not enforce mode 000 on parent traversal")
        with pytest.raises(pool.WorktreeSeatUnavailable, match="could not be inspected"):
            pool._validate_holder(path, "old")
    finally:
        parent.chmod(0o755)


def test_ignored_listing_is_scoped_to_each_candidate_seat(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    repo = _make_repo(tmp_path)
    bad = pool.acquire_worktree_seat(repo, "bad")
    good = pool.acquire_worktree_seat(repo, "good")
    bad_path = bad.path
    good_path = good.path
    bad.release()
    good.release()

    (bad_path / ".gitignore").write_text("build/\n")
    _git(bad_path, "add", ".gitignore")
    _git(bad_path, "commit", "-m", "add ignored seat fixture")
    _git(repo, "merge", "--ff-only", "worktree/bad")
    target = repo / "build" / "x"
    target.parent.mkdir()
    target.write_text("target\n")
    _git(repo, "add", "-f", "build/x")
    _git(repo, "commit", "-m", "add ignored seat target")
    collision = bad_path / "build"
    collision.mkdir()
    (collision / "payload").write_text("must survive\n")

    ignored_listing_cwds = []
    real_git_nul = pool._git_nul

    def record_ignored_listing(cwd, *args, **kwargs):
        if args[:1] == ("ls-files",) and "--ignored" in args:
            ignored_listing_cwds.append(Path(cwd).resolve())
        return real_git_nul(cwd, *args, **kwargs)

    monkeypatch.setattr(pool, "_git_nul", record_ignored_listing)
    records = {
        ident: {
            "dispatch_id": ident,
            "state": "complete",
            "worker_pid": 34567,
            "worker_identity": {"pid": 34567, "start_token": f"{ident}-token"},
        }
        for ident in ("bad", "good")
    }
    monkeypatch.setattr(ledger, "read_record", lambda ident: records.get(ident))
    monkeypatch.setattr(
        pool.goalflight_compat,
        "process_identity_matches",
        lambda pid, token: False,
    )

    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == good_path
    assert ignored_listing_cwds[:2] == [bad_path.resolve(), good_path.resolve()]
    assert (collision / "payload").read_text() == "must survive\n"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("missing", [False, True])
def test_hidden_index_edit_retains_seat(holder, flag, missing):
    repo, path, row = holder
    tracked = path / "tracked.txt"
    tracked.write_text("hidden bytes\n")
    _git(path, "update-index", flag, "tracked.txt")
    if missing:
        tracked.unlink()

    with pytest.raises(pool.WorktreeSeatResetRefused, match="hidden index entry"):
        pool.acquire_worktree_seat(repo, "next")
    if missing:
        assert not tracked.exists()
    else:
        assert tracked.read_text() == "hidden bytes\n"


def test_newline_filename_is_quarantined(holder):
    repo, path, row = holder
    filename = "line\nname.txt"
    path.joinpath(filename).write_text("base newline\n")
    _git(path, "add", filename)
    _git(path, "commit", "-m", "track newline filename")
    path.joinpath(filename).write_text("changed newline\n")

    with pool.acquire_worktree_seat(repo, "next"):
        refs = _git(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            "refs/goalflight/keep/old/dirty-*",
        ).splitlines()
        assert len(refs) == 1
        names = set(
            _git(repo, "ls-tree", "-r", "-z", "--name-only", refs[0]).split("\0")
        )
        assert filename in names
    assert not path.joinpath(filename).exists()


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_quarantine_tree_check_accepts_deleted_and_renamed_paths(holder, operation):
    repo, path, row = holder
    if operation == "delete":
        _git(path, "rm", "tracked.txt")
    else:
        _git(path, "mv", "tracked.txt", "renamed.txt")

    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == path
    assert (path / "tracked.txt").read_text() == "base\n"
    assert not (path / "renamed.txt").exists()


def test_quarantine_refuses_worktree_encoding(holder):
    repo, path, row = holder
    (path / ".gitattributes").write_text("encoded.txt working-tree-encoding=UTF-16LE-BOM\n")
    _git(path, "add", ".gitattributes")
    _git(path, "commit", "-m", "add working tree encoding")
    encoded = path / "encoded.txt"
    encoded.write_bytes(b"\xff\xfe" + "before\n".encode("utf-16le"))
    _git(path, "add", "encoded.txt")
    _git(path, "commit", "-m", "track encoded file")
    encoded.write_bytes(b"\xff\xfe" + "secret\n".encode("utf-16le"))

    with pytest.raises(pool.WorktreeSeatResetRefused, match="working-tree-encoding"):
        pool.acquire_worktree_seat(repo, "next")
    assert encoded.read_bytes() == b"\xff\xfe" + "secret\n".encode("utf-16le")


def test_dirty_submodule_retains_seat(holder, tmp_path):
    repo, path, row = holder
    subrepo = tmp_path / "subrepo"
    _git(tmp_path, "init", str(subrepo))
    _git(subrepo, "config", "user.email", "goalflight-test@example.invalid")
    _git(subrepo, "config", "user.name", "Goal Flight Test")
    (subrepo / "tracked.txt").write_text("submodule\n")
    _git(subrepo, "add", "tracked.txt")
    _git(subrepo, "commit", "-m", "submodule base")
    _git(
        path,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(subrepo),
        "modules/sub",
    )
    _git(path, "commit", "-m", "add submodule")
    _git(path, "config", "submodule.recurse", "true")
    (path / "modules" / "sub" / "local.txt").write_text("local\n")

    with pytest.raises(pool.WorktreeSeatResetRefused, match="dirty submodule"):
        pool.acquire_worktree_seat(repo, "next")
    assert (path / "modules" / "sub" / "local.txt").read_text() == "local\n"


def test_quarantine_refuses_clean_filter_bytes(holder):
    repo, path, row = holder
    payload = path / "note.dat"
    payload.write_text("secret-bytes\n")
    (path / ".gitattributes").write_text("*.dat filter=pointer\n")
    _git(path, "config", "filter.pointer.clean", "printf POINTER")
    _git(path, "config", "filter.pointer.smudge", "cat")
    _git(path, "config", "filter.pointer.required", "true")
    _git(path, "add", ".gitattributes", "note.dat")
    _git(path, "commit", "-m", "track filtered note")
    assert _git(path, "status", "--porcelain=v1", "--untracked-files=all") == ""
    with pytest.raises(pool.WorktreeSeatResetRefused, match="active filter"):
        pool.acquire_worktree_seat(repo, "next")
    assert payload.read_text() == "secret-bytes\n"
    assert _git(path, "branch", "--show-current") == "worktree/old"


def _add_clean_lfs_file(
    path: Path, tmp_path: Path, filename: str, payload_bytes: bytes
) -> Path:
    payload_oid = hashlib.sha256(payload_bytes).hexdigest()
    clean_filter = tmp_path / "lfs-clean.py"
    clean_filter.write_text(
        "import hashlib, sys\n"
        "data = sys.stdin.buffer.read()\n"
        "oid = hashlib.sha256(data).hexdigest()\n"
        "sys.stdout.write(f'version https://git-lfs.github.com/spec/v1\\n"
        "oid sha256:{oid}\\nsize {len(data)}\\n')\n",
        encoding="utf-8",
    )
    smudge_filter = tmp_path / "lfs-smudge.py"
    smudge_filter.write_text(
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(b'smudged bytes\\n')\n",
        encoding="utf-8",
    )
    (path / ".gitattributes").write_text("*.dat filter=lfs\n")
    _git(path, "config", "filter.lfs.clean", f"{sys.executable} {clean_filter}")
    _git(path, "config", "filter.lfs.smudge", f"{sys.executable} {smudge_filter}")
    _git(path, "config", "filter.lfs.required", "true")
    (path / filename).write_bytes(payload_bytes)
    _git(path, "add", ".gitattributes", filename)
    _git(path, "commit", "-m", "track filtered file")
    common = Path(_git(path, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (path / common).resolve()
    lfs_object = common / "lfs" / "objects" / payload_oid[:2] / payload_oid[2:4] / payload_oid
    lfs_object.parent.mkdir(parents=True)
    lfs_object.write_bytes(payload_bytes)
    return lfs_object


@pytest.mark.parametrize("object_state", ["valid", "missing", "empty", "wrong"])
def test_smudged_clean_filter_seat_is_reusable(holder, tmp_path, object_state):
    repo, path, row = holder
    lfs_object = _add_clean_lfs_file(
        path, tmp_path, "valuable.dat", b"smudged bytes\n"
    )
    assert _git(path, "status", "--porcelain=v1", "--untracked-files=all") == ""
    if object_state == "missing":
        lfs_object.unlink()
    elif object_state == "empty":
        lfs_object.write_bytes(b"")
    elif object_state == "wrong":
        lfs_object.write_bytes(b"corrupt bytes\n")

    if object_state == "valid":
        with pool.acquire_worktree_seat(repo, "next") as lease:
            assert lease.path == path
            assert lfs_object.read_bytes() == b"smudged bytes\n"
    else:
        with pytest.raises(pool.WorktreeSeatResetRefused, match="LFS object"):
            pool.acquire_worktree_seat(repo, "next")
        assert _git(path, "branch", "--show-current") == "worktree/old"
        assert _git(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            "refs/goalflight/keep/old",
            "refs/heads/goalflight/quarantine",
        ) == ""


def test_lfs_worktree_bytes_must_match_pointer(holder, tmp_path):
    repo, path, row = holder
    payload = b"pointer bytes\n"
    payload_oid = hashlib.sha256(payload).hexdigest()
    _add_clean_lfs_file(path, tmp_path, "valuable.dat", payload)
    clean_filter = tmp_path / "lfs-clean.py"
    clean_filter.write_text(
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.write('version https://git-lfs.github.com/spec/v1\\n"
        f"oid sha256:{payload_oid}\\nsize {len(payload)}\\n')\n",
        encoding="utf-8",
    )
    (path / "valuable.dat").write_bytes(b"wrong bytes!!\n")
    _git(path, "add", "valuable.dat")
    assert _git(path, "status", "--porcelain=v1", "--untracked-files=all") == ""

    with pytest.raises(pool.WorktreeSeatResetRefused, match="LFS object"):
        pool.acquire_worktree_seat(repo, "next")
    assert (path / "valuable.dat").read_bytes() == b"wrong bytes!!\n"
    assert _git(path, "branch", "--show-current") == "worktree/old"


def test_clean_submodule_with_lfs_reuses_seat(holder, tmp_path):
    repo, path, row = holder
    subrepo = tmp_path / "subrepo"
    _git(tmp_path, "init", str(subrepo))
    _git(subrepo, "config", "user.email", "goalflight-test@example.invalid")
    _git(subrepo, "config", "user.name", "Goal Flight Test")
    (subrepo / "tracked.txt").write_text("submodule\n")
    _git(subrepo, "add", "tracked.txt")
    _git(subrepo, "commit", "-m", "submodule base")
    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(subrepo),
        "modules/sub",
    )
    _git(repo, "commit", "-m", "add target submodule")
    _git(
        path,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(subrepo),
        "modules/sub",
    )
    _git(path, "commit", "-m", "add clean submodule")
    lfs_object = _add_clean_lfs_file(
        path, tmp_path, "valuable.dat", b"submodule lfs\n"
    )
    assert _git(path, "status", "--porcelain=v1", "--untracked-files=all") == ""

    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == path
        assert lfs_object.read_bytes() == b"submodule lfs\n"


def test_quarantine_failure_skips_candidate_and_reuses_next(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    repo = _make_repo(tmp_path)
    bad = pool.acquire_worktree_seat(repo, "bad")
    good = pool.acquire_worktree_seat(repo, "good")
    nested = bad.path / "nested"
    nested.mkdir()
    _git(nested, "init")
    bad_path = bad.path
    good_path = good.path
    bad.release()
    good.release()

    records = {
        ident: {
            "dispatch_id": ident,
            "state": "complete",
            "worker_pid": 34567,
            "worker_identity": {"pid": 34567, "start_token": f"{ident}-token"},
        }
        for ident in ("bad", "good")
    }
    monkeypatch.setattr(ledger, "read_record", lambda ident: records.get(ident))
    monkeypatch.setattr(
        pool.goalflight_compat,
        "process_identity_matches",
        lambda pid, token: False,
    )

    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == good_path
    assert nested.exists()
    assert bad_path != good_path


def test_checkout_failure_skips_candidate_and_reuses_next(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    repo = _make_repo(tmp_path)
    bad = pool.acquire_worktree_seat(repo, "bad")
    good = pool.acquire_worktree_seat(repo, "good")
    bad_path = bad.path
    good_path = good.path
    bad.release()
    good.release()
    records = {
        ident: {
            "dispatch_id": ident,
            "state": "complete",
            "worker_pid": 34567,
            "worker_identity": {"pid": 34567, "start_token": f"{ident}-token"},
        }
        for ident in ("bad", "good")
    }
    monkeypatch.setattr(ledger, "read_record", lambda ident: records.get(ident))
    monkeypatch.setattr(
        pool.goalflight_compat,
        "process_identity_matches",
        lambda pid, token: False,
    )
    original = pool._prepare_seat_checkout

    def fail_bad_checkout(worktree_path, **kwargs):
        if worktree_path == bad_path:
            raise pool.WorktreeSeatError("injected checkout failure")
        return original(worktree_path, **kwargs)

    monkeypatch.setattr(pool, "_prepare_seat_checkout", fail_bad_checkout)
    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == good_path


def test_quarantine_refuses_separate_staged_and_working_versions(holder):
    repo, path, row = holder
    (path / "tracked.txt").write_text("staged\n")
    _git(path, "add", "tracked.txt")
    (path / "tracked.txt").write_text("working\n")
    with pytest.raises(pool.WorktreeSeatResetRefused, match="separate staged"):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "branch", "--show-current") == "worktree/old"
    assert _git(path, "diff", "--cached")
    assert (path / "tracked.txt").read_text() == "working\n"


def test_terminal_explicit_prelaunch_failure_reclaims_without_identity(holder):
    repo, path, row = holder
    row.pop("worker_pid")
    row.pop("worker_identity")
    row["prelaunch_failure"] = True
    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == path


def test_terminal_missing_identity_without_prelaunch_proof_is_retained(holder):
    repo, path, row = holder
    row.pop("worker_pid")
    row.pop("worker_identity")
    with pytest.raises(pool.WorktreeSeatUnavailable):
        pool.acquire_worktree_seat(repo, "next")


@pytest.mark.parametrize("wait", [0.05, 0])
def test_allocation_lock_wait_honors_deadline(holder, wait):
    repo, path, row = holder
    lock_path = pool._seat_lock_root(repo) / "allocation.lock"
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(pool.WorktreeSeatUnavailable, match="allocation lock"):
            pool.acquire_worktree_seat(
                repo,
                "next",
                capacity_deadline=started + wait if wait else 0,
            )
        assert time.monotonic() - started < 1.0
    finally:
        lock_file.close()
def test_message_uses_worker_and_ledger_owner(holder, monkeypatch):
    repo, path, row = holder
    monkeypatch.setattr(pool.goalflight_compat, "process_identity_matches", lambda pid, token: True)
    row["wrapper_pid"] = 45678
    message = pool._busy_worktree_message(repo, 1, [(str(path), {"dispatch_id": "old", "pid": 123})])
    assert "controller=owner" in message
    assert "state=complete" in message
    assert "worker_pid=34567" in message
    assert "wrapper_pid=45678" in message
    assert "pid=123" not in message


def test_local_config_cap_overrides_env(tmp_path, monkeypatch):
    config = tmp_path / "capacity.json"
    config.write_text(json.dumps({"worktrees_per_repo": 20}))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", str(config))
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    assert pool.configured_worktree_seats() == 20
    config.write_text("{}")
    assert pool.configured_worktree_seats() == 2
    monkeypatch.delenv("GOALFLIGHT_WORKTREES_PER_REPO")
    monkeypatch.delenv("GOALFLIGHT_WORKTREE_SEATS", raising=False)
    assert pool.configured_worktree_seats() == 15


@pytest.mark.parametrize("value", [0, -1, True, 2.5, "2"])
def test_invalid_local_config_cap_fails_loudly(tmp_path, monkeypatch, value):
    config = tmp_path / "capacity.json"
    config.write_text(json.dumps({"worktrees_per_repo": value}))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", str(config))
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    with pytest.raises(pool.WorktreeSeatError, match="worktrees_per_repo"):
        pool.configured_worktree_seats()


def test_unparseable_local_config_cap_fails_loudly(tmp_path, monkeypatch):
    config = tmp_path / "capacity.json"
    config.write_text("not json")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", str(config))
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    with pytest.raises(pool.WorktreeSeatError, match="invalid JSON"):
        pool.configured_worktree_seats()


def test_seat_wait_retries_without_terminal_record(monkeypatch):
    args = SimpleNamespace(capacity_wait_s=10)
    lease = object()
    bind = Mock(side_effect=[pool.WorktreeSeatUnavailable("full"), lease])
    monkeypatch.setattr(dispatch, "_bind_dispatch_worktree", bind)
    monkeypatch.setattr(dispatch.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(dispatch, "_prepare_attempt_worktree_occupancy", lambda args: None)
    assert dispatch._admit_dispatch_worktree(args) is lease
    assert bind.call_count == 2


def test_retained_seat_wait_retries_without_terminal_record(monkeypatch):
    args = SimpleNamespace(capacity_wait_s=10)
    lease = object()
    bind = Mock(
        side_effect=[
            pool.WorktreeSeatResetRefused("all available worktrees would lose work"),
            lease,
        ]
    )
    monkeypatch.setattr(dispatch, "_bind_dispatch_worktree", bind)
    monkeypatch.setattr(dispatch.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(dispatch, "_prepare_attempt_worktree_occupancy", lambda args: None)
    assert dispatch._admit_dispatch_worktree(args) is lease
    assert bind.call_count == 2


def test_seat_wait_expiry_is_admission_refusal(monkeypatch):
    args = SimpleNamespace(capacity_wait_s=None)
    def refuse(bind_args):
        raise pool.WorktreeSeatUnavailable(str(bind_args._worktree_capacity_deadline))
    monkeypatch.setattr(dispatch, "_bind_dispatch_worktree", refuse)
    with pytest.raises(pool.WorktreeSeatUnavailable, match="0.0"):
        dispatch._admit_dispatch_worktree(args)
    assert args._worktree_seat_refused


def test_resume_replacement_preserves_seat_wait_deadline(tmp_path, monkeypatch):
    deadline = time.monotonic() + 10
    args = SimpleNamespace(
        dispatch_id="replacement-child",
        _worktree_capacity_deadline=deadline,
    )
    lease = object()
    observed = {}
    monkeypatch.setattr(
        dispatch,
        "_find_dispatch_record",
        lambda _dispatch_id: {"dispatch_id": "replacement-parent"},
    )
    monkeypatch.setattr(
        dispatch,
        "_resume_worktree_branch_spec",
        lambda *_args: ("worktree/replacement-parent", "base-sha", None, None),
    )
    monkeypatch.setattr(
        dispatch, "_controller_ring_label", lambda *_args: "unlabeled"
    )

    def acquire(_project_root, _dispatch_id, **kwargs):
        observed.update(kwargs)
        return lease

    monkeypatch.setattr(pool, "acquire_worktree_seat", acquire)
    assert dispatch._resume_replacement_worktree(
        args,
        project_root=tmp_path,
        parent_dispatch_id="replacement-parent",
    ) is lease
    assert observed["capacity_deadline"] == deadline


@pytest.mark.parametrize("value", ["inf", "nan", "-inf"])
def test_capacity_wait_parser_rejects_nonfinite(value, capsys):
    with pytest.raises(SystemExit) as exc_info:
        dispatch._build_launch_parser().parse_args([f"--capacity-wait-s={value}"])
    assert exc_info.value.code == 2
    assert "--capacity-wait-s must be finite" in capsys.readouterr().err


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_admission_rejects_nonfinite_capacity_wait(value):
    with pytest.raises(dispatch.DispatchUsageError, match="must be finite"):
        dispatch._admit_dispatch_worktree(SimpleNamespace(capacity_wait_s=value))


@pytest.mark.parametrize("state", ["failed", "error", "cancelled", "worker_dead", "blocked_capacity", "blocked_task_breadcrumb"])
def test_terminal_dead_states_reuse(holder, state):
    repo, path, row = holder
    row["state"] = state
    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == path


def test_dead_status_keeps_launch_identity(holder, tmp_path):
    repo, path, row = holder
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"dispatch_id": "old", "state": "complete", "worker_pid": 34567,
                                  "worker_identity": {}, "expected_worker_identity": row["worker_identity"]}))
    row["status_path"] = str(status)
    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == path


def test_path_lock_prevents_reset(holder):
    repo, path, row = holder
    with pool.try_acquire_worktree_path_lock(path, "other"):
        with pytest.raises(pool.WorktreeSeatUnavailable):
            pool.acquire_worktree_seat(repo, "next")
        assert _git(path, "branch", "--show-current") == "worktree/old"


@pytest.mark.parametrize("wait_s", [None, 0.2])
def test_dispatch_refusal_leaves_no_row_and_emits_once(tmp_path, monkeypatch, wait_s):
    env = _env(tmp_path, seats=1)
    for key, value in env.items():
        if key.startswith("GOALFLIGHT_"):
            monkeypatch.setenv(key, value)
    repo = _make_repo(tmp_path)
    with pool.acquire_worktree_seat(repo, "held") as holder:
        command = _dispatch_cmd(tmp_path, repo, "waiting", sys.executable, "-c", "raise AssertionError('launched')")
        if wait_s is not None:
            command[command.index("--"):command.index("--")] = ["--capacity-wait-s", str(wait_s)]
        result = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 2, result.stdout + result.stderr
        assert result.stderr.count("1/1 worktrees busy") == 1
        assert "DISPATCH-END" not in result.stdout
        assert "DISPATCH-BLOCKED" not in result.stdout
        assert ledger.read_record("waiting") is None
        assert not (tmp_path / "waiting.status.json").exists()
        from test_worktree_seat_pool import finish_seat_holder
        finish_seat_holder(holder)
        retried = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=30)
        assert retried.returncode == 0, retried.stdout + retried.stderr
        assert "DISPATCH-LAUNCHED" in retried.stdout


def test_missing_holder_record_retains_tree(holder, monkeypatch):
    repo, path, row = holder
    monkeypatch.setattr(ledger, "read_record", lambda ident: None)
    with pytest.raises(pool.WorktreeSeatUnavailable):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "branch", "--show-current") == "worktree/old"


def test_live_resume_holder_retains_old_branch(holder, monkeypatch):
    repo, path, row = holder
    resumed = {**row, "dispatch_id": "resumed", "worker_pid": 67890,
               "worker_identity": {"pid": 67890, "start_token": "live"}}
    pool.worktree_lock_path_for_path(repo, path).write_text(json.dumps({"dispatch_id": "resumed"}))
    monkeypatch.setattr(ledger, "read_record", lambda ident: resumed if ident == "resumed" else row)
    monkeypatch.setattr(pool.goalflight_compat, "process_identity_matches", lambda pid, token: pid == 67890)
    with pytest.raises(pool.WorktreeSeatUnavailable):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "branch", "--show-current") == "worktree/old"
    message = pool._busy_worktree_message(repo, 1, [(str(path), {"dispatch_id": "resumed"})])
    assert "lock holder: s-1=resumed" in message
    assert "worker_pid=67890" in message


def test_saved_head_is_never_overwritten(holder):
    repo, path, row = holder
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/goalflight/keep/old/head", base)
    (path / "tracked.txt").write_text("new commit\n")
    _git(path, "commit", "-am", "another commit")
    head = _git(path, "rev-parse", "HEAD")
    with pytest.raises(pool.WorktreeSeatResetRefused):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "rev-parse", "HEAD") == head
    assert _git(path, "rev-parse", "refs/goalflight/keep/old/head") == base


def test_unique_target_branch_is_pinned_before_reset(holder):
    repo, path, row = holder
    _git(repo, "checkout", "-b", "worktree/new")
    (repo / "tracked.txt").write_text("target-only\n")
    _git(repo, "commit", "-am", "unique target branch")
    target_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    with pool.acquire_worktree_seat(repo, "new") as lease:
        assert lease.path == path
        assert lease.branch == "worktree/new"
        refs = _git(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            "refs/goalflight/keep/new/prior-target-*",
        ).splitlines()
        assert len(refs) == 1
        assert _git(repo, "rev-parse", refs[0]) == target_sha


def test_full_pool_waits_then_launches_same_id(tmp_path, monkeypatch):
    env = _env(tmp_path, seats=1)
    for key, value in env.items():
        if key.startswith("GOALFLIGHT_"):
            monkeypatch.setenv(key, value)
    repo = _make_repo(tmp_path)
    holder = pool.acquire_worktree_seat(repo, "held")
    # This generation is intentionally gone, even though its PID was reused.
    ledger.record_path("held").write_text(json.dumps({"dispatch_id": "held", "state": "complete",
        "worker_pid": os.getpid(), "worker_identity": {"pid": os.getpid(), "start_token": "previous-generation"}}))
    marker = tmp_path / "launched"
    command = _dispatch_cmd(tmp_path, repo, "waiting", sys.executable, "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('launched')")
    command[command.index("--"):command.index("--")] = ["--capacity-wait-s", "10"]
    proc = subprocess.Popen(command, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while ledger.read_record("waiting") is None and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.poll() is None
        time.sleep(0.2)
        assert not marker.exists()
        assert ledger.read_record("waiting")["state"] == "waiting_capacity"
        holder.release()
        stdout, stderr = proc.communicate(timeout=20)
        assert proc.returncode == 0, stdout + stderr
        assert stdout.count("DISPATCH-LAUNCHED") == 1
        assert "DISPATCH-BLOCKED" not in stdout
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
    finally:
        holder.release()
        if proc.poll() is None:
            proc.terminate()
            proc.communicate(timeout=5)


# HOST-ONLY: launches a detached dispatcher; do not run in a worker worktree.
def test_retained_seat_waits_then_launches_same_id(tmp_path, monkeypatch):
    env = _env(tmp_path, seats=1)
    for key, value in env.items():
        if key.startswith("GOALFLIGHT_"):
            monkeypatch.setenv(key, value)
    repo = _make_repo(tmp_path)
    holder = pool.acquire_worktree_seat(repo, "held")
    seat = holder.path
    holder.release()
    ledger.record_path("held").write_text(
        json.dumps(
            {
                "dispatch_id": "held",
                "state": "complete",
                "worker_pid": os.getpid(),
                "worker_identity": {
                    "pid": os.getpid(),
                    "start_token": "previous-generation",
                },
            }
        )
    )

    (seat / ".gitignore").write_text("build/\n")
    _git(seat, "add", ".gitignore")
    _git(seat, "commit", "-m", "ignore retained-seat fixture")
    target = repo / "build" / "x"
    target.parent.mkdir()
    target.write_text("target\n")
    _git(repo, "add", "build/x")
    _git(repo, "commit", "-m", "add retained-seat target")
    collision = seat / "build"
    collision.mkdir()
    (collision / "payload").write_text("must survive until release\n")

    marker = tmp_path / "launched"
    command = _dispatch_cmd(
        tmp_path,
        repo,
        "waiting",
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('launched')",
    )
    command[command.index("--") : command.index("--")] = ["--capacity-wait-s", "5"]
    wait_started = time.monotonic()

    def release_collision() -> None:
        (collision / "payload").unlink()
        collision.rmdir()

    proc = subprocess.Popen(
        command,
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timer = threading.Timer(1.2, release_collision)
    timer.start()
    try:
        stdout, stderr = proc.communicate(timeout=20)
        waited = time.monotonic() - wait_started
        assert proc.returncode == 0, stdout + stderr
        assert waited >= 1.0
        assert stdout.count("DISPATCH-LAUNCHED") == 1
        assert "DISPATCH-BLOCKED" not in stdout
        assert marker.exists()
    finally:
        timer.cancel()
        timer.join()
        if proc.poll() is None:
            proc.terminate()
            proc.communicate(timeout=5)


@pytest.mark.parametrize("free_seat", [False, True])
def test_acp_uses_same_seat_wait_and_refusal(tmp_path, monkeypatch, free_seat):
    import goalflight_acp_run as acp
    from test_worktree_dispatch import runner_args, FakeProc, FakeConn, fake_run_prompt
    from test_worktree_seat_pool import record_finished_holder

    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "1")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    repo = _make_repo(tmp_path)
    holder = pool.acquire_worktree_seat(repo, "acp-held")
    record_finished_holder("acp-held")
    args = runner_args(repo, "acp-waiter", tmp_path / "acp.status.json")
    args.capacity_wait_s = 3 if free_seat else 0.05
    spawned = []

    async def spawn(*_args, **kwargs):
        spawned.append(kwargs)
        return FakeProc(), FakeConn()

    monkeypatch.setattr(acp, "agent_command", lambda *_args, **kwargs: ("fake-agent", []))
    monkeypatch.setattr(acp, "validate_acp_dispatch_readiness", lambda *args: None)
    monkeypatch.setattr(acp, "validate_os_sandbox_request", lambda *args: None)
    monkeypatch.setattr(acp, "preflight_os_sandbox", lambda *args: None)
    monkeypatch.setattr(acp, "cleanup_ghosts", lambda: None)
    monkeypatch.setattr(acp, "spawn_and_handshake_with_retry", spawn)
    monkeypatch.setattr(acp, "run_prompt", fake_run_prompt)
    timer = threading.Timer(0.5, holder.release) if free_seat else None
    if timer:
        timer.start()
    try:
        payload = asyncio.run(acp.run(args))
        if free_seat:
            assert payload["state"] == "complete", payload
            assert len(spawned) == 1
            assert Path(spawned[0]["cwd"]) == holder.path
        else:
            assert not spawned
            assert payload["state"] == "failed_worktree"
            assert "seat wait expired" in payload["error"]
            assert ledger.read_record("acp-waiter") is None
    finally:
        if timer:
            timer.cancel()
            timer.join()
        holder.release()
