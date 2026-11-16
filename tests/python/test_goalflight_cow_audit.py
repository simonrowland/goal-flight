"""goalflight_cow_audit must report what deleting a checkout would free (APFS private size).

`du` counts a cloned block once per clone, so it overstates what deleting a clone frees; the audit
uses the APFS private size instead.  These tests build checkouts, `cp -c` clones, plain copies and
hard links in a temp dir and check the reported bytes.  APFS-dependent tests skip off macOS.
"""

from __future__ import annotations

import json
import ctypes
from contextlib import nullcontext
import errno
import os
import shlex
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_cow_audit as audit  # noqa: E402

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="APFS private size is macOS-only")


def _make_checkout(path: Path, small: int = 40, big_mib: int = 4) -> None:
    (path / ".git").mkdir(parents=True)
    if big_mib:
        (path / "big.bin").write_bytes(os.urandom(big_mib << 20))
    for i in range(small):
        (path / f"small{i}.txt").write_bytes(os.urandom(64 << 10))


def _rows(capsys) -> dict[str, dict]:
    return {Path(r["path"]).name: r for r in map(json.loads, capsys.readouterr().out.splitlines())}


@darwin_only
def test_clone_is_shared_and_copy_is_non_cow(tmp_path, capsys):
    src = tmp_path / "root" / "src"
    _make_checkout(src)
    clone = tmp_path / "root" / "clone"
    copy = tmp_path / "root" / "copy"
    if subprocess.run(["cp", "-c", "-R", str(src), str(clone)]).returncode != 0:
        pytest.skip("cp -c (clonefile) unavailable on this filesystem")
    subprocess.run(["cp", "-R", str(src), str(copy)], check=True)
    # cp -c succeeded, so this is APFS: a missing private size is a bug, not a skip.
    assert audit.PrivateSize()(str(copy / "big.bin")) is not None

    assert audit.main(["--root", str(tmp_path / "root"), "--json", "--exact"]) == 0
    rows = _rows(capsys)
    assert rows["copy"]["cow"] == "NON-COW" and rows["copy"]["private_gb"] > 0.006
    assert rows["clone"]["cow"] == "shared" and rows["clone"]["private_gb"] < 0.001
    assert rows["clone"]["api"] == "apfs"


@darwin_only
def test_sampling_with_few_small_files_still_reads_private(tmp_path, capsys):
    # 15 private small files and no big file: the default 1-in-16 sampling must still sample one.
    _make_checkout(tmp_path / "r" / "few", small=15, big_mib=0)
    assert audit.main(["--root", str(tmp_path / "r"), "--json"]) == 0
    row = _rows(capsys)["few"]
    assert row["cow"] == "NON-COW" and row["private_frac"] > 0.9


@darwin_only
def test_venv_inside_a_checkout_is_counted(tmp_path, capsys):
    co = tmp_path / "r" / "co"
    _make_checkout(co, small=0, big_mib=0)
    (co / ".venv").mkdir()
    (co / ".venv" / "lib.bin").write_bytes(os.urandom(8 << 20))
    assert audit.main(["--root", str(tmp_path / "r"), "--json", "--exact"]) == 0
    assert _rows(capsys)["co"]["private_gb"] > 0.008


@darwin_only
def test_hard_links_count_once_and_are_not_freeable(tmp_path, capsys):
    a = tmp_path / "r" / "a"
    _make_checkout(a, small=0, big_mib=4)
    os.link(a / "big.bin", a / "big-link.bin")
    assert audit.main(["--root", str(tmp_path / "r"), "--json", "--exact"]) == 0
    row = _rows(capsys)["a"]
    assert row["files"] == 1  # one inode
    assert row["private_gb"] < 0.001  # another link keeps the blocks alive
    assert row["hardlinked_gb"] > 0.004


def test_skip_name_is_never_touched(tmp_path, capsys, monkeypatch):
    skip_name = "zz-hidden"
    root = tmp_path / "r"
    keep = root / "keep"
    _make_checkout(keep, small=2, big_mib=0)
    alias_parent = tmp_path / "alias-parent"
    alias_parent.mkdir()
    bridge = alias_parent / "bridge"
    bridge.symlink_to(alias_parent / skip_name / "root", target_is_directory=True)
    alias = tmp_path / "alias"
    alias.symlink_to(alias_parent / skip_name / "root", target_is_directory=True)
    alias_after_missing = tmp_path / "alias-after-missing"
    alias_after_missing.symlink_to(alias_parent / "missing" / ".." / "bridge", target_is_directory=True)
    alias_after_bridge_parent = tmp_path / "alias-after-bridge-parent"
    alias_after_bridge_parent.symlink_to(
        alias_parent / "missing" / ".." / "bridge" / ".." / "target", target_is_directory=True)
    seat_parent = tmp_path / "seat-parents"
    seat_parent.mkdir()

    class SkippedEntry:
        def __init__(self, name):
            self.name = name

    touched: list[tuple[str, str]] = []
    real_scandir, real_lstat, real_stat = os.scandir, os.lstat, os.stat
    real_realpath = os.path.realpath

    def record(label, original, blocked, blocked_paths=()):
        def call(path, *args, **kwargs):
            value = os.fsdecode(os.fspath(path))
            touched.append((label, value))
            if skip_name in value:
                if blocked is None:
                    raise FileNotFoundError
                return blocked
            if value in blocked_paths:
                if blocked is None:
                    raise FileNotFoundError
                return blocked
            return original(path, *args, **kwargs)
        return call

    def safe_scandir(path):
        value = os.fsdecode(os.fspath(path))
        touched.append(("scandir", value))
        if skip_name in value:
            raise FileNotFoundError
        with real_scandir(path) as entries:
            result = list(entries)
        if value == str(root):
            result.append(SkippedEntry(skip_name))
        elif value == str(keep):
            result.extend((SkippedEntry(skip_name), SkippedEntry(skip_name + "-file")))
        elif value == str(seat_parent):
            result.append(SkippedEntry(skip_name))
        return nullcontext(result)

    monkeypatch.setattr(audit.os, "scandir", safe_scandir)
    monkeypatch.setattr(audit.os, "lstat", record("lstat", real_lstat, os.stat_result((0,) * 10)))
    monkeypatch.setattr(audit.os, "stat", record("stat", real_stat, None, (str(bridge),)))
    monkeypatch.setattr(audit.os.path, "realpath",
                        record("path.realpath", real_realpath, str(bridge), (str(bridge),)))
    monkeypatch.setattr(audit.PrivateSize, "__call__",
                        lambda self, p: (touched.append(("private-size", str(p))), 0)[1])
    seat_glob = str(seat_parent / "*" / "jobs" / "*")
    assert audit.main(["--root", str(root), "--root", str(alias), "--root", str(alias_after_missing),
                       "--seat-parent", seat_glob, "--skip-name", skip_name, "--json", "--exact"]) == 0
    assert list(_rows(capsys)) == ["keep"]
    assert audit._safe_realpath(str(alias_after_bridge_parent), (skip_name,)) is None

    cycle = alias_parent / "cycle"
    cycle.symlink_to(alias_parent / "missing" / ".." / "cycle", target_is_directory=True)
    assert audit._safe_realpath(str(cycle), ()) is None

    def vanished(_path):
        raise FileNotFoundError

    monkeypatch.setattr(audit.os, "readlink", vanished)
    assert audit._safe_realpath(str(alias), ()) is None
    assert not any(skip_name in path for _, path in touched)
    assert not any(label in {"stat", "path.realpath"} and path == str(bridge) for label, path in touched)

def test_measure_records_matching_skip_substrings_without_touching_entries(monkeypatch):
    class SkippedEntry:
        name = "amber-violet-entry"

        def is_dir(self, **_kwargs):
            pytest.fail("a skip-name entry must not be inspected")

        def is_file(self, **_kwargs):
            pytest.fail("a skip-name entry must not be inspected")

        def stat(self, **_kwargs):
            pytest.fail("a skip-name entry must not be stat'ed")

    monkeypatch.setattr(audit.os, "scandir", lambda _path: nullcontext([SkippedEntry()]))
    measured = audit.measure("/unused-root", ("amber", "violet"),
                             lambda _path: pytest.fail("private size must not be measured"),
                             big=1, every=1)
    assert measured["skipped"] == 1
    assert measured["skip_matches"] == ["amber", "violet"]


def test_measure_row_carries_matching_skip_substrings(monkeypatch):
    metrics = {"logical": 0, "allocated": 0, "private": 0, "hardlinked": 0, "files": 0,
               "newest": 0, "estimated": False, "fallback": False, "skipped": 1,
               "skip_matches": ["amber"], "nested": []}
    monkeypatch.setattr(audit, "_thread_priv", lambda: None)
    monkeypatch.setattr(audit, "_born", lambda _path: (None, "-"))
    monkeypatch.setattr(audit, "measure", lambda *_args, **_kwargs: metrics)
    row, nested = audit._measure_row("/unused-root", "dir", ("amber",), 1, 1)
    assert row["_skip_matches"] == ["amber"]
    assert nested == []


def test_nested_checkout_own_row_and_parent_git_counted(tmp_path, capsys):
    parent = tmp_path / "r" / "parent"
    _make_checkout(parent, small=3, big_mib=0)
    (parent / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _make_checkout(parent / "worktrees" / "child", small=3, big_mib=0)
    assert audit.main(["--root", str(tmp_path / "r"), "--json", "--exact"]) == 0
    rows = _rows(capsys)
    assert set(rows) == {"parent", "child"}
    assert rows["parent"]["files"] == rows["child"]["files"] + 1  # parent's own .git/HEAD counted; child excluded


def test_checkout_nested_deeper_than_max_depth_still_gets_a_row(tmp_path, capsys):
    top = tmp_path / "r" / "top"
    _make_checkout(top, small=1, big_mib=0)
    _make_checkout(top / "a" / "b" / "c" / "deep", small=1, big_mib=0)
    assert audit.main(["--root", str(tmp_path / "r"), "--json", "--exact", "--max-depth", "1"]) == 0
    assert set(_rows(capsys)) == {"top", "deep"}


def test_empty_skip_name_is_refused():
    with pytest.raises(SystemExit):
        audit.main(["--skip-name", ""])


# ---- safety verdicts, folders, parallelism, delete plan -------------------------------------------

REAL_OPEN_PATHS = audit._open_paths
DAY = 86400


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch, tmp_path):
    # The host git config must not leak into the repos built here or into the audit's own git calls.
    for k, v in {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    home = tmp_path.parent / "isolated-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    # A global open-file snapshot is shared by each audit; tests opt into it only when needed.
    monkeypatch.setattr(audit, "_open_paths", lambda: set())


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _age(path: Path, days: float = 30) -> None:
    t = time.time() - days * DAY
    for d, _dirs, files in os.walk(path):
        for f in files:
            os.utime(os.path.join(d, f), (t, t), follow_symlinks=False)


def _repo(path: Path, remote: Path | None = None, age: bool = True) -> Path:
    """A committed repo (1 MiB of private bytes); pushed to a fresh bare `remote` when given."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "data.bin").write_bytes(os.urandom(1 << 20))
    (path / "a.txt").write_text("a\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    if remote is not None:
        # Only network remotes count, so the remote is an https URL that is never contacted, plus
        # the tracking ref a real push would leave behind (the audit trusts tracking refs).
        _git(path, "remote", "add", "origin", f"https://example.invalid/{Path(remote).name}")
        _push(path)
    if age:
        _age(path)
    return path


def _push(co: Path) -> None:
    _git(co, "update-ref", "refs/remotes/origin/main", "HEAD")


def _cmds(plan: str) -> list[str]:
    """The command lines of a delete plan (no comments, no leading `unset` of GIT_* variables)."""
    return [ln for ln in plan.splitlines() if ln and not ln.startswith(("#", "unset GIT_DIR "))]


def _audit(capsys, *args: str) -> dict[str, dict]:
    assert audit.main([*args, "--json", "--exact"]) == 0
    return _rows(capsys)


def test_clean_pushed_old_repo_is_safe(tmp_path, capsys):
    _repo(tmp_path / "r" / "co", tmp_path / "remote.git")
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["verdict"], row["reasons"]) == ("SAFE", "")
    assert (row["dirty"], row["unpushed"], row["remotes"], row["stash"], row["open"]) == (0, 0, 1, 0, 0)
    assert (row["link"], row["branch"]) == ("repo", "main") and row["last_commit_utc"] != "-"


def test_review_reasons_for_unsaved_work(tmp_path, capsys):
    r = tmp_path / "r"
    dirty = _repo(r / "dirty", tmp_path / "d.git", age=False)
    (dirty / "new.txt").write_text("x")
    unpushed = _repo(r / "unpushed", tmp_path / "u.git", age=False)
    (unpushed / "a.txt").write_text("b\n")
    _git(unpushed, "commit", "-q", "-am", "local only")
    _repo(r / "noremote", age=False)
    stash = _repo(r / "stash", tmp_path / "s.git", age=False)
    (stash / "a.txt").write_text("stashed\n")
    _git(stash, "stash", "-q")
    recent = _repo(r / "recent", tmp_path / "rc.git", age=False)
    for p in (dirty, unpushed, r / "noremote", stash):
        _age(p)
    rows = _audit(capsys, "--root", str(r))
    assert {n: rows[n]["reasons"] for n in rows} == {
        "dirty": "dirty", "unpushed": "unpushed", "noremote": "no-remote", "stash": "unpushed,stash",
        "recent": "recent"}  # refs/stash is a ref, so `--all` also counts the stash commit as unpushed
    assert rows["dirty"]["dirty"] == 1 and rows["unpushed"]["unpushed"] == 1 and rows["stash"]["stash"] == 1
    assert recent.exists() and all(row["verdict"] == "REVIEW" for row in rows.values())


def test_pinned_ref_in_disposable_clone_is_not_safekeeping(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "co", tmp_path / "remote.git", age=False)
    _git(co, "tag", "pin")  # same commit as origin/main: not unpushed
    (co / "a.txt").write_text("b\n")
    _git(co, "commit", "-q", "-am", "pinned work")
    _git(co, "tag", "pin2")
    _git(co, "reset", "-q", "--hard", "origin/main")  # only the tag holds the new commit
    _age(co)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert row["reasons"] == "unpushed" and row["unpushed"] == 1


def test_orphan_worktree_whose_main_repo_is_gone(tmp_path, capsys):
    main = _repo(tmp_path / "main")
    _git(main, "worktree", "add", "-q", "-b", "feat", str(tmp_path / "r" / "wt"))
    _age(tmp_path / "r")
    shutil.rmtree(main)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["wt"]
    assert (row["kind"], row["link"], row["verdict"], row["reasons"]) == ("worktree", "orphan", "REVIEW", "orphan")


def test_linked_worktree_branch_commits_do_not_block_but_detached_commits_do(tmp_path, capsys):
    main = _repo(tmp_path / "main", tmp_path / "remote.git")
    feat, det = tmp_path / "r" / "feat", tmp_path / "r" / "det"
    _git(main, "worktree", "add", "-q", "-b", "feat", str(feat))
    (feat / "a.txt").write_text("feat\n")
    _git(feat, "commit", "-q", "-am", "on a branch: lives in the main repo")
    _git(feat, "stash", "list")
    _git(main, "worktree", "add", "-q", "--detach", str(det))
    (det / "a.txt").write_text("det\n")
    _git(det, "commit", "-q", "-am", "reachable only from this HEAD")
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["feat"]["unpushed"] >= 1 and (rows["feat"]["verdict"], rows["feat"]["link"]) == ("SAFE", "linked")
    assert rows["det"]["detached"] == 1 and rows["det"]["reasons"] == "detached-commits"


def test_repo_hosting_a_live_worktree_is_review_and_not_planned(tmp_path, capsys):
    main = _repo(tmp_path / "r" / "main", tmp_path / "remote.git")
    _git(main, "worktree", "add", "-q", "-b", "feat", str(tmp_path / "elsewhere"))
    _age(main)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["main"]
    assert (row["verdict"], row["reasons"]) == ("REVIEW", "hosts-worktrees")
    assert audit.main(["--root", str(tmp_path / "r"), "--delete-plan"]) == 0
    assert str(main) not in capsys.readouterr().out


def test_remote_ci_seat_is_left_to_goal_flight(tmp_path, capsys):
    # Any path with a remote-ci-managed component, not only seats/s-N/checkout itself.
    seat = _repo(tmp_path / "r" / "remote-ci-managed" / "node1" / "other" / "checkout",
                 tmp_path / "remote.git")
    row = _audit(capsys, "--root", str(tmp_path / "r"))["checkout"]
    assert (row["verdict"], row["reasons"]) == ("REVIEW", "remote-ci-seat")
    assert audit.main(["--root", str(tmp_path / "r"), "--delete-plan"]) == 0
    assert str(seat) not in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof unavailable")
def test_open_file_is_seen(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(audit, "_open_paths", REAL_OPEN_PATHS)
    co = _repo(tmp_path / "r" / "co", tmp_path / "remote.git")
    with open(co / "a.txt") as held:
        row = _audit(capsys, "--root", str(tmp_path / "r"), "--open-scan", "lsof")["co"]
    assert held.closed and row["open"] >= 1 and row["reasons"] == "open"


def test_unknown_open_state_is_review(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(audit, "_open_paths", lambda: None)
    _repo(tmp_path / "r" / "co", tmp_path / "remote.git")
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert row["open"] == "-" and row["reasons"] == "open-unknown"


def test_partial_lsof_output_marks_snapshot_incomplete(tmp_path, capsys, monkeypatch):
    _repo(tmp_path / "r" / "co", tmp_path / "remote.git")
    real_run = audit.subprocess.run

    def partial_lsof(command, *args, **kwargs):
        if command and command[0] == "lsof":
            return subprocess.CompletedProcess(
                command, 1, stdout="p123\nctest\nn/tmp/partial-open-file\n", stderr="partial output\n")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(audit.subprocess, "run", partial_lsof)
    monkeypatch.setattr(audit, "_open_paths", lambda scan=None: audit._lsof_open_paths())
    assert audit.main(["--root", str(tmp_path / "r"), "--open-scan", "lsof", "--json"]) == 0
    captured = capsys.readouterr()
    row = json.loads(captured.out)
    assert row["open"] == "-" and "open-unknown" in row["reasons"].split(",")
    assert row["tier"] == "HOLD"
    assert "# open-scan: 1 processes scanned, 0 not visible (EPERM), 1 incomplete" in captured.err


def test_incomplete_open_snapshot_holds_every_checkout_and_reports_count(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(audit, "_open_paths", lambda: audit._OpenPaths(
        complete=False, scanned_processes=7, not_visible_processes=3, incomplete_processes=2))
    _repo(tmp_path / "r" / "co1", tmp_path / "one.git")
    _repo(tmp_path / "r" / "co2", tmp_path / "two.git")
    assert audit.main(["--root", str(tmp_path / "r"), "--json", "--exact"]) == 0
    captured = capsys.readouterr()
    rows = {Path(row["path"]).name: row for row in map(json.loads, captured.out.splitlines())}
    assert {row["open"] for row in rows.values()} == {"-"}
    assert {row["reasons"] for row in rows.values()} == {"open-unknown"}
    assert "# open-scan: 7 processes scanned, 3 not visible (EPERM), 2 incomplete" in captured.err


def test_single_incomplete_process_preserves_open_results_below_threshold(tmp_path, capsys, monkeypatch):
    root = tmp_path / "repos"
    first = _repo(root / "first", tmp_path / "first-remote")
    second = _repo(root / "second", tmp_path / "second-remote")
    opens = audit._OpenPaths(complete=True, scanned_processes=19, not_visible_processes=3,
                             incomplete_processes=1)
    opens.add(str(first))
    monkeypatch.setattr(audit, "_open_paths", lambda: opens)
    monkeypatch.setattr(audit, "_skip_names", lambda cli: tuple(cli))
    monkeypatch.setattr(audit, "_seat_candidates", lambda *_args: [])
    assert audit.main(["--root", str(root), "--primary-root", str(root), "--json"]) == 0
    captured = capsys.readouterr()
    rows = {Path(row["path"]).name: row for row in map(json.loads, captured.out.splitlines())}
    assert rows[first.name]["open"] == 1
    assert rows[second.name]["open"] == 0
    assert all("open-unknown" not in row["reasons"] for row in rows.values())
    assert "# open-scan: 19 processes scanned, 3 not visible (EPERM), 1 incomplete" in captured.err


def test_git_status_never_runs_on_a_checkout_holding_skipped_names(tmp_path, capsys, monkeypatch):
    r = tmp_path / "r"
    blind = _repo(r / "blind", tmp_path / "b.git")
    (blind / "zz-hidden-data").mkdir()
    (blind / "zz-hidden-data" / "x.bin").write_bytes(b"x")
    keep = _repo(r / "keep", tmp_path / "k.git")
    calls: list[list[str]] = []
    real_run = subprocess.run
    monkeypatch.setattr(audit.subprocess, "run", lambda cmd, *a, **k: (calls.append(list(cmd)), real_run(cmd, *a, **k))[1])
    touched: list[str] = []
    real_scandir, real_lstat = os.scandir, os.lstat
    monkeypatch.setattr(audit.os, "scandir", lambda p: (touched.append(str(p)), real_scandir(p))[1])
    monkeypatch.setattr(audit.os, "lstat", lambda p, *a, **k: (touched.append(str(p)), real_lstat(p, *a, **k))[1])
    rows = _audit(capsys, "--root", str(r), "--skip-name", "hidden")
    status_paths = [c[c.index("-C") + 1] for c in calls if "status" in c]
    assert status_paths == [str(keep)]  # the recorder sees status calls, just not on `blind`
    assert rows["blind"]["dirty"] is None and rows["blind"]["reasons"] == "has-skipped-names"
    assert rows["keep"]["verdict"] == "SAFE"
    assert not [p for p in touched if "hidden" in p]


def test_audit_does_not_rewrite_the_index(tmp_path, capsys):
    # _age makes every file's mtime differ from the index's stat cache, so a plain `git status`
    # would refresh and rewrite the index; the audit must leave it byte- and mtime-identical.
    co = _repo(tmp_path / "r" / "co", tmp_path / "remote.git")
    index = co / ".git" / "index"
    before = (index.stat().st_mtime_ns, index.read_bytes())
    assert _audit(capsys, "--root", str(tmp_path / "r"))["co"]["verdict"] == "SAFE"
    assert (index.stat().st_mtime_ns, index.read_bytes()) == before


def test_dirs_rows_count_everything_including_checkouts(tmp_path, capsys):
    plain = tmp_path / "top" / "plain"
    plain.mkdir(parents=True)
    (plain / "blob.bin").write_bytes(os.urandom(8 << 20))
    _repo(plain / "inner", tmp_path / "remote.git")
    (tmp_path / "top" / "zz-hidden-dir").mkdir()
    rows = _audit(capsys, "--dirs", str(tmp_path / "top"), "--skip-name", "hidden")
    assert set(rows) == {"plain", "inner"}  # folder and nested checkout each have one row
    row, inner = rows["plain"], rows["inner"]
    assert row["kind"] == "dir" and row["verdict"] == "-" and row["dirty"] is None
    assert row["private_gb"] > 0.008  # the 8 MiB blob; the checkout is measured separately
    assert inner["kind"] != "dir" and inner["private_gb"] > 0.001


def test_checkout_overlapping_root_and_dirs_is_counted_once_in_summary(tmp_path, capsys, monkeypatch):
    root = tmp_path / "root"
    checkout = root / "checkout"
    _make_checkout(checkout, small=0, big_mib=0)
    with (checkout / "payload.bin").open("wb") as payload:
        payload.truncate(128 << 20)
    monkeypatch.setattr(audit.PrivateSize, "__call__", lambda self, path: os.stat(path).st_size)

    assert audit.main(["--root", str(root), "--dirs", str(root), "--json", "--exact"]) == 0
    captured = capsys.readouterr()
    rows = [json.loads(line) for line in captured.out.splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] != "dir"
    summary = next(line for line in captured.err.splitlines() if "freeable (private) total" in line)
    reported = float(summary.split("freeable (private) total ", 1)[1].split(" GB", 1)[0])
    assert reported == round(sum(row["private_gb"] for row in rows), 1)


def test_dir_summary_matches_distinct_bytes_with_nested_checkout(tmp_path, capsys, monkeypatch):
    job = tmp_path / "top" / "job"
    job.mkdir(parents=True)
    (job / "plain.bin").write_bytes(b"p" * (2 << 20))
    co = _repo(job / "checkout", tmp_path / "remote.git")
    monkeypatch.setattr(audit.PrivateSize, "__call__", lambda self, path: os.stat(path).st_blocks * 512)
    assert audit.main(["--dirs", str(tmp_path / "top"), "--json", "--exact"]) == 0
    captured = capsys.readouterr()
    rows = {Path(row["path"]).name: row for row in map(json.loads, captured.out.splitlines())}
    actual = sum(row["private_gb"] for row in rows.values())
    seen, distinct = set(), 0
    for current, _dirs, files in os.walk(job):
        for name in files:
            path = os.path.join(current, name)
            st = os.stat(path, follow_symlinks=False)
            key = (st.st_dev, st.st_ino)
            if key not in seen:
                seen.add(key)
                distinct += st.st_blocks * 512
    assert abs(actual - distinct / 1e9) < 1e-9
    assert rows["job"]["private_gb"] < distinct / 1e9
    assert rows["checkout"]["path"] == str(co)
    assert "nested checkouts are counted in their own rows" in captured.err
    summary = next(line for line in captured.err.splitlines() if line.startswith("# 1 dirs; private total"))
    assert float(summary.split("private total ", 1)[1].split(" GB", 1)[0]) == round(rows["job"]["private_gb"], 1)


def test_parallel_jobs_give_identical_rows(tmp_path, capsys):
    r = tmp_path / "r"
    for i in range(5):
        _repo(r / f"co{i}", tmp_path / f"remote{i}.git")
    _make_checkout(r / "co0" / "nested" / "deep", small=3, big_mib=1)
    (r / "co1" / "dirty.txt").write_text("x")
    assert audit.main(["--root", str(r), "--json", "--jobs", "1"]) == 0
    one = capsys.readouterr().out
    assert audit.main(["--root", str(r), "--json", "--jobs", "4"]) == 0
    assert capsys.readouterr().out == one and len(one.splitlines()) == 6


def test_docs_private_deep_checkouts_and_paths_only(tmp_path, capsys):
    dp = tmp_path / "repo" / "docs-private"
    deep = _repo(dp / "a" / "b" / "c" / "d" / "e" / "f" / "safe-seat", tmp_path / "s.git")
    dirty = _repo(dp / "a" / "dirty-seat", tmp_path / "d.git")
    (dirty / "wip.txt").write_text("x")
    _age(dirty)
    assert audit.main(["--root", str(dp), "--older-than-days", "7", "--verdict", "SAFE", "--paths-only"]) == 0
    assert capsys.readouterr().out.splitlines() == [str(deep)]


def test_delete_plan_lists_only_safe_full_bytes_rows_quoted(tmp_path, capsys):
    r = tmp_path / "r"
    main = _repo(tmp_path / "main")  # outside the root, so it is not a row
    wt = r / "it's a seat"
    _git(main, "worktree", "add", "-q", "-b", "feat", str(wt))
    _age(wt)
    full = _repo(r / "full seat", tmp_path / "f.git")
    held = _repo(r / "held", tmp_path / "d.git", age=False)  # recent: HOLD, never planned
    clone = None
    if sys.platform == "darwin":
        src = _repo(r / "src", tmp_path / "c.git")
        clone = r / "clone"
        if subprocess.run(["cp", "-c", "-R", str(src), str(clone)]).returncode != 0:
            clone = None
        else:
            _age(clone)
    assert audit.main(["--root", str(r), "--delete-plan"]) == 0
    plan = capsys.readouterr().out
    cmds = _cmds(plan)
    wt_line = f"git -C {shlex.quote(os.path.realpath(main))} worktree remove -- {shlex.quote(str(wt))}"
    assert sorted(cmds) == sorted([wt_line, f"rm -rf -- {shlex.quote(str(full))}"])
    assert "# 2 rows" in plan and plan.rstrip().splitlines()[-1].startswith("# ") and "df" in plan.splitlines()[-1]
    assert "# Clones borrowing objects from outside the scanned roots were not checked." in plan.splitlines()
    if clone is not None:
        assert str(clone) not in plan and str(r / "src") not in plan  # shared clones free ~nothing
    assert wt.exists() and full.exists()  # the tool printed a plan; it deleted nothing
    subprocess.run(["sh", "-c", wt_line], check=True, capture_output=True)
    assert not wt.exists()
    assert os.path.realpath(wt) not in _git(main, "worktree", "list", "--porcelain")
    assert not (main / ".git" / "worktrees").exists() or not os.listdir(main / ".git" / "worktrees")


def _repo_ignoring(path: Path, remote: Path, patterns: str, files: list[str]) -> Path:
    co = _repo(path, remote, age=False)
    (co / ".gitignore").write_text(patterns)
    _git(co, "add", ".gitignore")
    _git(co, "commit", "-q", "-m", "ignore")
    _push(co)
    for rel in files:
        (co / rel).parent.mkdir(parents=True, exist_ok=True)
        (co / rel).write_text("x")
    _age(co)
    return co


def test_ignored_research_is_salvaged_or_held_by_size(tmp_path, capsys):
    co = _repo_ignoring(tmp_path / "r" / "co", tmp_path / "remote.git", "docs-private/\n",
                        ["docs-private/notes.md"])
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["verdict"], row["reasons"], row["ignored"], row["dirty"]) == ("REVIEW", "ignored-data", 1, 0)
    assert row["tier"] == "SALVAGE"  # small: tarred on the way out
    salv = tmp_path / "salvage"
    assert audit.main(["--root", str(tmp_path / "r"), "--delete-plan", "--salvage-dir", str(salv)]) == 0
    cmds = _cmds(capsys.readouterr().out)
    tar = next(i for i, c in enumerate(cmds) if "ignored.tgz" in c)
    assert "./docs-private" in cmds and cmds[tar].endswith("ignored.list &&") and cmds[-1] == f"rm -rf -- {co}"
    # Above --salvage-max-gb the same data is HOLD and the row is never planned.
    row = _audit(capsys, "--root", str(tmp_path / "r"), "--salvage-max-gb", "0.0000000001")["co"]
    assert (row["tier"], row["reasons"]) == ("HOLD", "ignored-data,ignored-data-large")
    assert audit.main(["--root", str(tmp_path / "r"), "--delete-plan", "--salvage-max-gb", "0.0000000001",
                       "--salvage-dir", str(salv)]) == 0
    assert str(co) not in capsys.readouterr().out


def test_ignored_caches_and_environments_stay_safe(tmp_path, capsys):
    _repo_ignoring(tmp_path / "r" / "co", tmp_path / "remote.git", ".venv/\n__pycache__/\n*.pyc\n",
                   [".venv/lib/site.py", "pkg/__pycache__/m.cpython-312.pyc", "pkg/stray.pyc"])
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["verdict"], row["reasons"], row["ignored"]) == ("SAFE", "", 0)


# ---- adversarial-review fixes ------------------------------------------------------------------


def test_commits_held_only_by_pin_refs_or_notes_are_unpushed(tmp_path, capsys):
    pin = _repo(tmp_path / "r" / "pin", tmp_path / "p.git", age=False)
    (pin / "a.txt").write_text("pinned\n")
    _git(pin, "commit", "-q", "-am", "held only by a pin")
    _git(pin, "update-ref", "refs/goalflight/keep/x", "HEAD")
    _git(pin, "reset", "-q", "--hard", "origin/main")
    notes = _repo(tmp_path / "r" / "notes", tmp_path / "n.git", age=False)
    _git(notes, "notes", "add", "-m", "only here")
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["pin"]["reasons"] == "unpushed" and rows["notes"]["reasons"] == "unpushed"


def test_two_stashes_are_counted(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "co", tmp_path / "remote.git", age=False)
    for i in range(2):
        (co / "a.txt").write_text(f"wip {i}\n")
        _git(co, "stash", "-q")
    _age(co)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert row["stash"] == 2 and "stash" in row["reasons"].split(",")


def _with_submodule(tmp_path: Path, attrs: str | None = None) -> tuple[Path, Path, Path]:
    """parent (network remote, pushed) with submodule `sub` cloned from a local bare source; the
    submodule's own origin is then pointed at a network URL, keeping its tracking refs."""
    src = _repo(tmp_path / "src", age=False)
    if attrs:
        (src / ".gitattributes").write_text(attrs)
        _git(src, "add", ".gitattributes")
        _git(src, "commit", "-q", "-m", "attrs")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(tmp_path / "src.git")], check=True)
    _git(src, "push", "-q", str(tmp_path / "src.git"), "main")
    parent = _repo(tmp_path / "r" / "parent", tmp_path / "parent.git", age=False)
    _git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(tmp_path / "src.git"), "sub")
    _git(parent, "commit", "-q", "-m", "add sub")
    _push(parent)
    sub = parent / "sub"
    _git(sub, "remote", "set-url", "origin", "https://example.invalid/src")
    return src, parent, sub


def test_submodule_is_checked_like_a_repo_and_blocks_its_parent(tmp_path, capsys):
    src, parent, sub = _with_submodule(tmp_path)
    head = _git(sub, "rev-parse", "HEAD").strip()
    _git(sub, "switch", "-q", "-c", "side")
    (sub / "a.txt").write_text("side\n")
    _git(sub, "commit", "-q", "-am", "unpushed in the submodule")
    _git(sub, "switch", "-q", "--detach", head)  # parent still sees the recorded commit: clean
    (sub / "a.txt").write_text("stash me\n")
    _git(sub, "stash", "-q")
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["sub"]["link"] == "separate" and {"unpushed", "stash"} <= set(rows["sub"]["reasons"].split(","))
    assert rows["parent"]["reasons"] == "nested-review"
    assert audit.main(["--root", str(tmp_path / "r"), "--delete-plan"]) == 0
    assert "rm -rf" not in capsys.readouterr().out and src.exists()


def test_hidden_index_flags_are_review(tmp_path, capsys):
    au = _repo(tmp_path / "r" / "au", tmp_path / "au.git", age=False)
    _git(au, "update-index", "--assume-unchanged", "a.txt")
    sw = _repo(tmp_path / "r" / "sw", tmp_path / "sw.git", age=False)
    _git(sw, "update-index", "--skip-worktree", "a.txt")
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["au"]["reasons"] == "hidden-index-flags" and rows["sw"]["reasons"] == "hidden-index-flags"


def test_paths_with_newlines_are_never_printed_for_cleanup(tmp_path, capsys):
    _repo(tmp_path / "r" / "bad\nseat", tmp_path / "b.git")
    _repo(tmp_path / "r" / "bad\rseat2", tmp_path / "b2.git")
    good = _repo(tmp_path / "r" / "good", tmp_path / "g.git")
    assert audit.main(["--root", str(tmp_path / "r"), "--paths-only", "--verdict", "SAFE"]) == 0
    cap = capsys.readouterr()
    assert cap.out.splitlines() == [str(good)] and "unsafe-path: 2" in cap.err
    assert audit.main(["--root", str(tmp_path / "r"), "--delete-plan"]) == 0
    cap = capsys.readouterr()
    assert "bad" not in cap.out and "unsafe-path: 2" in cap.err
    assert _cmds(cap.out) == [f"rm -rf -- {good}"]
    assert all(str(tmp_path) not in ln for ln in cap.out.splitlines() if ln.startswith("#"))


def test_per_worktree_refs_and_locks_are_review(tmp_path, capsys):
    main = _repo(tmp_path / "main")
    refs, locked = tmp_path / "r" / "refs", tmp_path / "r" / "locked"
    _git(main, "worktree", "add", "-q", "-b", "a", str(refs))
    _git(refs, "update-ref", "refs/worktree/keep", "HEAD")
    _git(main, "worktree", "add", "-q", "-b", "b", str(locked))
    _git(main, "worktree", "lock", str(locked))
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["refs"]["reasons"] == "worktree-refs" and rows["locked"]["reasons"] == "locked"


def test_locked_worktree_with_missing_path_still_counts_as_hosted(tmp_path, capsys):
    main = _repo(tmp_path / "r" / "main", tmp_path / "remote.git")
    gone = tmp_path / "usb" / "wt"
    _git(main, "worktree", "add", "-q", "-b", "feat", str(gone))
    _git(main, "worktree", "lock", str(gone))
    shutil.rmtree(gone)  # e.g. on an unmounted volume: the lock says keep it
    _age(main)
    assert _audit(capsys, "--root", str(tmp_path / "r"))["main"]["reasons"] == "hosts-worktrees"


def test_alternates_source_is_review(tmp_path, capsys):
    src = _repo(tmp_path / "r" / "src", tmp_path / "remote.git", age=False)
    subprocess.run(["git", "clone", "-q", "--shared", str(src), str(tmp_path / "r" / "borrower")], check=True)
    _git(tmp_path / "r" / "borrower", "remote", "set-url", "origin", "https://example.invalid/src")
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["src"]["reasons"] == "alternates-source" and rows["borrower"]["verdict"] == "SAFE"


def test_protect_paths(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "skill", tmp_path / "remote.git")
    row = _audit(capsys, "--root", str(tmp_path / "r"), "--protect", str(co))["skill"]
    assert row["reasons"] == "protected"
    with pytest.raises(SystemExit):
        audit.main(["--help"])
    help_text = capsys.readouterr().out
    assert all(p in help_text for p in audit.DEFAULT_PROTECT)


def test_installed_agent_tooling_is_protected_by_default(tmp_path, capsys, monkeypatch):
    # gstack's skill-pack checkouts live under ~/.gstack and ~/.claude/skills: tooling, never seats.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    gs = _repo(home / ".gstack" / "repos" / "gstack", tmp_path / "g.git")
    sk = _repo(home / ".claude" / "skills" / "pack", tmp_path / "s.git")
    rows = _audit(capsys, "--root", str(home / ".gstack"), "--root", str(home / ".claude"))
    assert "protected" in rows["gstack"]["reasons"] and rows["gstack"]["tier"] == "HOLD"
    assert "protected" in rows["pack"]["reasons"]


def test_local_config_lists_merge_with_defaults_and_cli(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_path = home / ".config" / "goalflight" / "cow-audit.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({
        "protect": ["~/config-protected"],
        "seat_parents": ["~/config-seats/*"],
        "expected": ["~/config-expected/*"],
        "skip_names": ["config-hidden"],
    }))
    _repo(home / "config-protected" / "seed", tmp_path / "config.git")
    _repo(home / "cli-protected" / "seed-cli", tmp_path / "cli.git")
    _repo(home / "Repos" / "default-expected", tmp_path / "default.git")
    _repo(home / "config-expected" / "configured", tmp_path / "configured.git")
    _repo(home / "cli-expected" / "cli-expected", tmp_path / "expected.git")
    _repo(home / "config-hidden", tmp_path / "hidden.git")
    _repo(home / "cli-hidden", tmp_path / "cli-hidden.git")
    for parent, name in ((home / "scratch", "default-seat"),
                         (home / "config-seats", "config-seat"),
                         (home / "cli-seats", "cli-seat")):
        (parent / name).mkdir(parents=True)
        (parent / name / "output.bin").write_bytes(b"output")

    assert audit.main(["--root", str(home), "--protect", "~/cli-protected",
                       "--seat-parent", "~/cli-seats/*", "--expected", "~/cli-expected/*",
                       "--skip-name", "cli-hidden", "--json", "--exact"]) == 0
    rows = _rows(capsys)
    assert rows["seed"]["reasons"] == "protected"
    assert rows["seed-cli"]["reasons"] == "protected"
    assert rows["default-expected"]["placement"] == "expected"
    assert rows["configured"]["placement"] == "expected"
    assert rows["cli-expected"]["placement"] == "expected"
    assert {rows[name]["kind"] for name in ("default-seat", "config-seat", "cli-seat")} == {"dir"}
    assert "config-hidden" not in rows and "cli-hidden" not in rows


def test_blinded_git_paths_are_never_followed(tmp_path, capsys, monkeypatch):
    import builtins
    r = tmp_path / "r"
    keep = _repo(r / "keep", tmp_path / "k.git")
    blind = _repo(r / "blind", tmp_path / "b.git")
    (blind / "zz-hidden-data").mkdir()
    (blind / "zz-hidden-data" / "x.bin").write_bytes(b"x")
    main = _repo(tmp_path / "zz-hidden-main")  # a worktree whose main repo is blinded
    _git(main, "worktree", "add", "-q", "-b", "wt", str(r / "wt"))
    _age(r / "wt")
    host = _repo(r / "host", tmp_path / "h.git")  # a repo hosting a worktree at a blinded path
    _git(host, "worktree", "add", "-q", "-b", "x", str(tmp_path / "zz-hidden-place" / "wt2"))
    _age(host)
    touched: list[str] = []

    def spy(mod, name):
        real = getattr(mod, name)
        monkeypatch.setattr(mod, name, lambda p, *a, **k: (touched.append(str(p)), real(p, *a, **k))[1])

    for mod, name in [(builtins, "open"), (os.path, "realpath"), (os.path, "exists"), (os.path, "lexists"),
                      (os.path, "isdir"), (os, "stat"), (os, "lstat"), (os, "scandir"), (os, "readlink")]:
        spy(mod, name)
    calls: list[list[str]] = []
    real_run = subprocess.run
    monkeypatch.setattr(audit.subprocess, "run", lambda cmd, *a, **k: (calls.append(list(cmd)), real_run(cmd, *a, **k))[1])
    rows = _audit(capsys, "--root", str(r), "--skip-name", "hidden")
    for name in ("blind", "wt", "host"):
        assert "has-skipped-names" in rows[name]["reasons"].split(",")
    assert rows["keep"]["verdict"] == "SAFE"
    git_dirs = {c[c.index("-C") + 1] for c in calls if c[:1] == ["git"]}
    assert str(r / "wt") not in git_dirs and str(r / "host") not in git_dirs  # no git at all
    assert [c[c.index("-C") + 1] for c in calls if "status" in c] == [str(keep)]
    assert not [p for p in touched if "hidden" in p]
    assert not [c for c in calls if any("hidden" in a for a in c)]


def test_filter_drivers_in_repo_config_never_run(tmp_path, capsys):
    marker = tmp_path / "MARKER"
    for name, key in (("clean", "clean"), ("proc", "process")):
        co = _repo(tmp_path / "r" / name, tmp_path / f"{name}.git", age=False)
        (co / ".gitattributes").write_text("*.txt filter=probe\n")
        _git(co, "add", ".gitattributes")
        _git(co, "commit", "-q", "-m", "attrs")
        _push(co)
        _git(co, "config", f"filter.probe.{key}", f"sh -c 'touch {marker}; cat'")
        (co / "a.txt").write_text("a\n")  # same content, new stat: status must re-hash it
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert not marker.exists()
    assert rows["clean"]["reasons"] == rows["proc"]["reasons"] == "filter-driver"


def test_ignored_build_output_is_data(tmp_path, capsys):
    _repo_ignoring(tmp_path / "r" / "co", tmp_path / "remote.git", "build/\nenv/\n", ["build/results.h5"])
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert row["reasons"] == "ignored-data" and row["ignored"] == 1


def test_symlinked_dot_git_is_a_checkout_and_symlinked_roots_resolve(tmp_path, capsys):
    real = _repo(tmp_path / "store" / "real", tmp_path / "remote.git", age=False)
    co = tmp_path / "r" / "co"
    co.mkdir(parents=True)
    os.symlink(real / ".git", co / ".git")
    (tmp_path / "link").symlink_to(tmp_path / "r")
    _age(tmp_path / "r")
    assert audit.main(["--root", str(tmp_path / "link"), "--json"]) == 0
    rows = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert [r["path"] for r in rows] == [os.path.realpath(co)] and rows[0]["link"] == "separate"


def test_symlink_roots_resolving_through_filtered_component_are_omitted(tmp_path, capsys, monkeypatch):
    target = tmp_path / "zz-hidden-parent" / "root"
    _repo(target / "seat", "c")
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    touched: list[str] = []
    real_ino, real_scandir = audit._ino, audit.os.scandir

    def track_ino(path):
        touched.append(str(path))
        return real_ino(path)

    def track_scandir(path):
        touched.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(audit, "_ino", track_ino)
    monkeypatch.setattr(audit.os, "scandir", track_scandir)
    assert audit.main(["--root", str(alias), "--dirs", str(alias), "--json", "--skip-name", "hidden"]) == 0
    captured = capsys.readouterr()
    assert not captured.out
    assert "omitted" in captured.err and str(target) not in captured.err
    assert not any(str(target) in path for path in touched)


def test_only_network_remotes_count(tmp_path, capsys):
    r = tmp_path / "r"
    local = _repo(r / "local", age=False)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(tmp_path / "l.git")], check=True)
    _git(local, "remote", "add", "origin", str(tmp_path / "l.git"))
    _git(local, "push", "-q", "-u", "origin", "main")  # a real push, but to this machine
    alias = _repo(r / "alias", age=False)
    _git(alias, "config", f"url.{tmp_path / 'l.git'}.insteadOf", "https://mirror.example/")
    _git(alias, "remote", "add", "origin", "https://mirror.example/x.git")  # resolves to a local path
    _push(alias)
    pushlocal = _repo(r / "pushlocal", "p", age=False)
    _git(pushlocal, "remote", "set-url", "--push", "origin", str(tmp_path / "l.git"))
    scp = _repo(r / "scp", age=False)
    _git(scp, "remote", "add", "origin", "git@example.invalid:team/scp.git")
    _push(scp)
    _repo(r / "https", "h", age=False)
    _age(r)
    rows = _audit(capsys, "--root", str(r))
    for name in ("local", "alias", "pushlocal"):
        assert (rows[name]["remotes"], rows[name]["reasons"]) == (0, "no-remote"), name
    for name in ("scp", "https"):
        assert (rows[name]["remotes"], rows[name]["verdict"]) == (1, "SAFE"), name


def test_skip_names_in_branch_and_remote_url(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "co", age=False)
    _git(co, "remote", "add", "origin", "https://example.invalid/zz-hidden.git")
    _push(co)
    _git(co, "switch", "-q", "-c", "zz-hidden-branch")
    _age(co)
    row = _audit(capsys, "--root", str(tmp_path / "r"), "--skip-name", "hidden")["co"]
    assert row["branch"] == "-" and row["remotes"] == 0 and "no-remote" in row["reasons"].split(",")


def test_bad_commondir_is_git_error_not_a_crash(tmp_path, capsys):
    main = _repo(tmp_path / "main")
    _git(main, "worktree", "add", "-q", "-b", "x", str(tmp_path / "r" / "wt"))
    (main / ".git" / "worktrees" / "wt" / "commondir").write_bytes(b"\xff\x00junk")
    _age(tmp_path / "r")
    assert _audit(capsys, "--root", str(tmp_path / "r"))["wt"]["reasons"] == "git-error"


def test_plan_drops_rows_inside_another_planned_row(tmp_path, capsys):
    outer = _repo(tmp_path / "r" / "outer", tmp_path / "o.git", age=False)
    (outer / ".gitignore").write_text("inner/\n")
    _git(outer, "add", ".gitignore")
    _git(outer, "commit", "-q", "-m", "ignore inner")
    _push(outer)
    _repo(outer / "inner", tmp_path / "i.git", age=False)
    _age(outer)
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["outer"]["verdict"] == rows["inner"]["verdict"] == "SAFE", rows
    assert audit.main(["--root", str(tmp_path / "r"), "--delete-plan"]) == 0
    cmds = _cmds(capsys.readouterr().out)
    assert cmds == [f"rm -rf -- {outer}"]


def test_stray_core_worktree_and_inherited_git_env_do_not_redirect(tmp_path, capsys, monkeypatch):
    co = _repo(tmp_path / "r" / "co", tmp_path / "remote.git", age=False)
    (tmp_path / "empty").mkdir()
    _git(co, "config", "core.worktree", str(tmp_path / "empty"))
    _age(co)
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "no-such-index"))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["verdict"], row["dirty"]) == ("SAFE", 0)


def test_nested_review_propagates_to_parent(tmp_path, capsys):
    parent = _repo(tmp_path / "r" / "parent", tmp_path / "p.git", age=False)
    (parent / ".gitignore").write_text("child/\n")
    _git(parent, "add", ".gitignore")
    _git(parent, "commit", "-q", "-m", "ignore child")
    _push(parent)
    child = _repo(parent / "child", tmp_path / "c.git", age=False)
    (child / "wip.txt").write_text("x")
    _age(parent)
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["child"]["reasons"] == "dirty" and rows["parent"]["reasons"] == "nested-review"


@darwin_only
def test_parallel_private_size_buffers_do_not_mix(tmp_path, capsys):
    # Alternate fully private rows with fully cloned rows (their clone source sits outside the
    # root), many small files each: a shared getattrlist buffer would leak one file's private size
    # into another row's total.
    r = tmp_path / "r"
    for i in range(8):
        co = r / f"co{i}"
        _make_checkout(co, small=0, big_mib=0)
        files = tmp_path / "src" / f"s{i}" if i % 2 else co
        files.mkdir(parents=True, exist_ok=True)
        for j in range(150):
            (files / f"f{j}.bin").write_bytes(os.urandom(16 << 10))
        if i % 2 and subprocess.run(["cp", "-c", "-R", f"{files}/.", str(co)]).returncode != 0:
            pytest.skip("cp -c (clonefile) unavailable on this filesystem")
    outs = []
    for jobs in ("1", "8"):
        assert audit.main(["--root", str(r), "--json", "--exact", "--jobs", jobs]) == 0
        outs.append(capsys.readouterr().out)
    rows = {Path(x["path"]).name: x for x in map(json.loads, outs[1].splitlines())}
    assert outs[0] == outs[1]
    for i in range(8):
        frac = rows[f"co{i}"]["private_frac"]
        assert (frac < 0.05) if i % 2 else (frac > 0.95), (i, frac)


def test_deinit_submodule_repo_left_in_modules_is_orphan_module(tmp_path, capsys):
    _src, parent, sub = _with_submodule(tmp_path)
    _git(sub, "switch", "-q", "-c", "side")
    (sub / "a.txt").write_text("side\n")
    _git(sub, "commit", "-q", "-am", "only in .git/modules/sub after deinit")
    _git(parent, "submodule", "deinit", "-q", "-f", "sub")
    assert (parent / ".git" / "modules" / "sub").is_dir() and not (sub / ".git").exists()
    _age(tmp_path / "r")
    assert _audit(capsys, "--root", str(tmp_path / "r"))["parent"]["reasons"] == "orphan-module"


def test_clean_submodule_is_never_planned_but_does_not_block_its_parent(tmp_path, capsys):
    _src, parent, sub = _with_submodule(tmp_path)
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["sub"]["reasons"] == "submodule" and rows["parent"]["verdict"] == "SAFE"
    assert audit.main(["--root", str(sub), "--delete-plan"]) == 0
    assert _cmds(capsys.readouterr().out) == []


def test_submodule_and_signature_config_never_runs_code(tmp_path, capsys):
    marker = tmp_path / "MARKER"
    _src, parent, sub = _with_submodule(tmp_path, attrs="*.txt filter=probe\n")
    # The filter exists only in the submodule's own config, which the parent's status must not use.
    _git(sub, "config", "filter.probe.clean", f"sh -c 'touch {marker}; cat'")
    _git(sub, "config", "filter.probe.process", f"sh -c 'touch {marker}'")
    # A signed commit plus log.showSignature would make `git log` run gpg.program.
    signed = _repo(tmp_path / "r" / "signed", "s", age=False)
    fake = tmp_path / "fake-gpg"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    fake.chmod(0o755)
    tree = _git(signed, "rev-parse", "HEAD^{tree}").strip()
    head = _git(signed, "rev-parse", "HEAD").strip()
    body = (f"tree {tree}\nparent {head}\nauthor t <t@example.invalid> 1700000000 +0000\n"
            f"committer t <t@example.invalid> 1700000000 +0000\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n"
            f" AAAA\n -----END PGP SIGNATURE-----\n\nsigned\n")
    sha = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=signed, input=body,
                         capture_output=True, text=True, check=True).stdout.strip()
    _git(signed, "update-ref", "refs/heads/main", sha)
    _push(signed)
    _git(signed, "config", "gpg.program", str(fake))
    _git(signed, "config", "log.showSignature", "true")
    for p in (sub / "a.txt", signed / "a.txt"):
        p.write_text("a\n")  # same content, new stat: a status would re-hash it
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert not marker.exists()
    assert rows["signed"]["verdict"] == "SAFE" and rows["sub"]["reasons"] == "submodule,filter-driver"


def test_alternates_are_followed_transitively(tmp_path, capsys):
    c = _repo(tmp_path / "r" / "C", "c", age=False)
    subprocess.run(["git", "clone", "-q", "--bare", "--shared", str(c), str(tmp_path / "B.git")], check=True)
    a = tmp_path / "r" / "A"
    subprocess.run(["git", "clone", "-q", "--shared", str(tmp_path / "B.git"), str(a)], check=True)
    _git(a, "remote", "set-url", "origin", "https://example.invalid/a")
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["C"]["reasons"] == "alternates-source" and rows["A"]["verdict"] == "SAFE"


def test_copied_worktree_admin_entries_do_not_make_a_clone_a_host(tmp_path, capsys):
    main = _repo(tmp_path / "r" / "main", "m", age=False)
    _git(main, "worktree", "add", "-q", "-b", "feat", str(tmp_path / "wt"))
    clone = tmp_path / "r" / "clone"
    if subprocess.run(["cp", "-c", "-R", str(main), str(clone)], capture_output=True).returncode != 0:
        subprocess.run(["cp", "-R", str(main), str(clone)], check=True)
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert rows["main"]["reasons"] == "hosts-worktrees" and rows["clone"]["verdict"] == "SAFE"


# ---- colour and READY TO CLEAN -------------------------------------------------------------------

def _fixture_row(name: str, cow: str, verdict: str, reasons: str = "", gb: float = 2.0) -> dict:
    return {"path": f"/seats/{name}", "kind": "repo", "logical_gb": gb, "allocated_gb": gb, "private_gb": gb,
            "hardlinked_gb": 0.0, "private_frac": 1.0 if cow == "NON-COW" else 0.0, "cow": cow, "files": 3,
            "estimated": False, "api": "apfs", "newest_utc": "2026-01-01T00:00Z", "_newest": 1767225600.0,
            "verdict": verdict, "reasons": reasons, "dirty": 0, "unpushed": 0, "remotes": 1, "stash": 0,
            "open": 0, "link": "repo", "branch": "main", "last_commit_utc": "2026-01-01T00:00Z", "detached": 0,
            "ignored": 0, "_main_repo": None, "born_utc": "-", "owner": "-", "class": "other"}


@pytest.fixture
def fixture_rows(monkeypatch):
    rows = [_fixture_row("ready", "NON-COW", "SAFE", gb=3.0), _fixture_row("held", "NON-COW", "REVIEW", "dirty"),
            _fixture_row("held2", "NON-COW", "REVIEW", "dirty,recent", gb=1.0),
            _fixture_row("clone", "shared", "SAFE", gb=0.5)]
    monkeypatch.setattr(audit, "audit", lambda *a, **k: [dict(r) for r in rows])
    return rows



# ---- tiers, salvage-then-delete, cleanup targets ----------------------------------------------

@pytest.mark.parametrize("reasons,tier", [
    ("", "SAFE"),
    ("dirty", "SALVAGE"), ("unpushed", "SALVAGE"), ("no-remote", "SALVAGE"), ("stash", "SALVAGE"),
    ("worktree-refs", "SALVAGE"), ("detached-commits", "SALVAGE"), ("ignored-data", "SALVAGE"),
    ("dirty,unpushed,ignored-data", "SALVAGE"),
    ("ignored-data,ignored-data-large", "HOLD"), ("hidden-index-flags", "HOLD"), ("orphan-module", "HOLD"),
    ("stash,stash-reflog", "HOLD"), ("unsafe-path", "HOLD"),
    ("open", "HOLD"), ("open-unknown", "HOLD"), ("recent", "HOLD"), ("remote-ci-seat", "HOLD"),
    ("protected", "HOLD"), ("locked", "HOLD"), ("hosts-worktrees", "HOLD"), ("has-skipped-names", "HOLD"),
    ("alternates-source", "HOLD"), ("submodule", "HOLD"), ("filter-driver", "HOLD"), ("git-error", "HOLD"),
    ("orphan", "HOLD"), ("nested-review", "HOLD"), ("dirty,open", "HOLD"),
])
def test_tier_for_each_reason_class(reasons, tier):
    assert audit.tier_of(set(filter(None, reasons.split(",")))) == tier
    assert set(audit.REASON_ORDER) >= set(filter(None, reasons.split(",")))


def _salvage_case(tmp_path: Path, tag: str = "") -> tuple[Path, Path]:
    """A checkout whose real remote is a local bare repo (for restoring), but whose configured
    origin is a network URL: one unpushed commit, a modified file, an untracked file and a small
    ignored results dir."""
    bare = tmp_path / f"real-remote{tag}.git"
    co = _repo(tmp_path / "r" / "co", age=False)
    (co / ".gitignore").write_text("results/\n")
    _git(co, "add", ".gitignore")
    _git(co, "commit", "-q", "-m", "ignore results")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    _git(co, "remote", "add", "origin", str(bare))
    _git(co, "push", "-q", "-u", "origin", "main")
    _git(co, "remote", "set-url", "origin", "https://example.invalid/co")
    (co / "a.txt").write_text("committed locally\n")
    _git(co, "commit", "-q", "-am", "unpushed")
    (co / "a.txt").write_text("modified, not committed\n")
    (co / "new.txt").write_text("untracked\n")
    (co / "results").mkdir()
    (co / "results" / "run.h5").write_bytes(b"data")
    _age(co)
    return co, bare


def test_salvage_plan_runs_end_to_end(tmp_path, capsys):
    co, bare = _salvage_case(tmp_path)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["tier"], row["reasons"]) == ("SALVAGE", "dirty,ignored-data,unpushed")
    salv = tmp_path / "salvage"
    assert audit.main(["--root", str(tmp_path / "r"), "--delete-plan", "--salvage-dir", str(salv)]) == 0
    plan = capsys.readouterr().out
    assert co.exists() and not salv.exists()  # the tool itself ran nothing
    subprocess.run(["sh", "-c", plan], check=True, capture_output=True)
    assert not co.exists()
    (saved,) = list(salv.iterdir())
    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(bare), str(fresh)], check=True)
    _git(fresh, "bundle", "verify", str(saved / "refs.bundle"))
    _git(fresh, "fetch", "-q", str(saved / "refs.bundle"), "refs/heads/*:refs/salvaged/*")
    _git(fresh, "checkout", "-q", (saved / "HEAD").read_text().strip())
    _git(fresh, "apply", str(saved / "worktree.patch"))
    assert (fresh / "a.txt").read_text() == "modified, not committed\n"
    assert "https://example.invalid/co" in (saved / "remotes").read_text()
    listing = lambda name: subprocess.run(["tar", "-tzf", str(saved / name)], capture_output=True, text=True,
                                          check=True).stdout.split()
    assert listing("untracked.tgz") == ["new.txt"] and "./results/run.h5" in listing("ignored.tgz")


def test_failed_salvage_step_keeps_the_checkout(tmp_path, capsys):
    co, _bare = _salvage_case(tmp_path)
    salv = tmp_path / "salvage"
    salv.mkdir()
    assert audit.main(["--root", str(tmp_path / "r"), "--delete-plan", "--salvage-dir", str(salv)]) == 0
    plan = capsys.readouterr().out
    salv.chmod(0o555)  # mkdir of the row's salvage dir fails
    try:
        subprocess.run(["sh", "-c", plan], capture_output=True)
    finally:
        salv.chmod(0o755)
    assert co.exists() and (co / "new.txt").exists()


def test_salvage_dir_inside_an_audited_row_is_refused(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "co", "c")
    assert audit.main(["--root", str(tmp_path / "r"), "--delete-plan", "--salvage-dir", str(co / "s")]) == 2
    assert "must not be inside" in capsys.readouterr().err


def test_verdict_filter_accepts_tiers(tmp_path, capsys):
    _repo(tmp_path / "r" / "safe", "s")
    dirty = _repo(tmp_path / "r" / "dirty", "d", age=False)
    (dirty / "x.txt").write_text("x")
    _age(dirty)
    _repo(tmp_path / "r" / "recent", "n", age=False)
    got = {}
    for v in ("SAFE", "SALVAGE", "HOLD", "REVIEW"):
        assert audit.main(["--root", str(tmp_path / "r"), "--paths-only", "--verdict", v]) == 0
        got[v] = sorted(Path(p).name for p in capsys.readouterr().out.split())
    assert got == {"SAFE": ["safe"], "SALVAGE": ["dirty"], "HOLD": ["recent"], "REVIEW": ["dirty", "recent"]}


def _fixture_row(name: str, cow: str, tier: str, reasons: str = "", gb: float = 2.0) -> dict:
    return {"path": f"/seats/{name}", "kind": "repo", "logical_gb": gb, "allocated_gb": gb, "private_gb": gb,
            "hardlinked_gb": 0.0, "private_frac": 1.0 if cow == "NON-COW" else 0.0, "cow": cow, "files": 3,
            "estimated": False, "api": "apfs", "newest_utc": "2026-01-01T00:00Z", "_newest": 1767225600.0,
            "verdict": "SAFE" if tier == "SAFE" else "REVIEW", "reasons": reasons, "tier": tier, "dirty": 0,
            "unpushed": 0, "remotes": 1, "stash": 0, "open": 0, "link": "repo", "branch": "main",
            "last_commit_utc": "2026-01-01T00:00Z", "detached": 0, "ignored": 0, "_main_repo": None,
            "_tracking": [], "_ignored_entries": [],
            "placement": "expected", "_location": "-", "untracked_gb": None,
            "born_utc": "-", "owner": "-", "class": "other"}


@pytest.fixture
def fixture_rows(monkeypatch):
    rows = [_fixture_row("small-safe", "NON-COW", "SAFE", gb=1.5),
            _fixture_row("big-salvage", "NON-COW", "SALVAGE", "dirty,unpushed", gb=4.0),
            _fixture_row("unpushed", "NON-COW", "SALVAGE", "unpushed", gb=2.5),
            _fixture_row("open", "NON-COW", "HOLD", "open,recent", gb=3.0),
            _fixture_row("recent", "NON-COW", "HOLD", "recent", gb=0.5),
            _fixture_row("clone", "shared", "SAFE", gb=0.25)]
    monkeypatch.setattr(audit, "audit", lambda *a, **k: [dict(r) for r in rows])
    return rows


def test_plain_table_includes_added_metadata_columns(fixture_rows, capsys):
    assert audit.main(["--root", "/x"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == ("private_gb\tallocated_gb\tprivate_frac\tcow\tkind\tfiles\tnewest_utc\tapi\thardlinked_gb\tpath\t"
                      "verdict\treasons\tdirty\tunpushed\tremotes\tstash\topen\tlink\tbranch\tlast_commit_utc\t"
                      "detached\tignored\ttier\tplacement\tuntracked_gb\tborn_utc\towner\tclass")
    assert out[1] == ("1.50\t1.50\t1.00\tNON-COW\trepo\t3\t2026-01-01T00:00Z\tapfs\t0.00\t/seats/small-safe\t"
                      "SAFE\t-\t0\t0\t1\t0\t0\trepo\tmain\t2026-01-01T00:00Z\t0\t0\tSAFE\texpected\t-\t-\t-\tother")
    assert not any(ln.startswith("#") for ln in out)  # the cleanup section is on stderr


def test_cleanup_targets_lists_safe_and_salvage_by_size_and_adds_up(fixture_rows, capsys):
    assert audit.main(["--root", "/x"]) == 0
    err = capsys.readouterr().err.splitlines()
    head = next(i for i, ln in enumerate(err) if ln.startswith("# CLEANUP TARGETS"))
    assert err[head] == "# CLEANUP TARGETS: 3 non-COW checkouts, 8.00 GB freeable (SAFE 1.50 GB, SALVAGE 6.50 GB)"
    listed = err[head + 1:head + 4]
    assert [ln.split()[-1] for ln in listed] == ["/seats/big-salvage", "/seats/unpushed", "/seats/small-safe"]
    assert "saves patch+untracked-tar+bundle" in listed[0] and listed[2].split()[1] == "SAFE"
    assert err[head + 4] == "# NOT TARGETS (HOLD): 2 checkouts, 3.50 GB (open 3.00, recent 0.50)"
    assert "--delete-plan > plan.sh" in err[head + 5]
    summary = next(ln for ln in err if ln.startswith("# NON-COW"))
    assert summary == ("# NON-COW 11.50 GB: SAFE 1.50 / SALVAGE 6.50 (dirty 4.00, unpushed 2.50) / "
                       "HOLD 3.50 (open 3.00, recent 0.50)")



# ---- round 5: salvage integrity, sparse seats, primaries, summaries ------------------------------

def _plan_for(capsys, root: Path, salv: Path, *extra: str) -> str:
    assert audit.main(["--root", str(root), "--delete-plan", "--salvage-dir", str(salv), *extra]) == 0
    return capsys.readouterr().out


def _run_plan(plan: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", plan], capture_output=True, text=True, **kw)


def _restore(tmp_path: Path, bare: Path, saved: Path, name: str = "fresh") -> Path:
    """Clone the real remote, fetch the bundle if any, check out the saved HEAD, apply patches."""
    fresh = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(bare), str(fresh)], check=True)
    if (saved / "refs.bundle").exists():
        _git(fresh, "bundle", "verify", str(saved / "refs.bundle"))
        _git(fresh, "fetch", "-q", str(saved / "refs.bundle"), "refs/heads/*:refs/salvaged/*")
    _git(fresh, "checkout", "-q", (saved / "HEAD").read_text().strip())
    for patch, flags in (("index.patch", ["--cached"]), ("worktree.patch", [])):
        if (saved / patch).exists() and (saved / patch).stat().st_size:
            _git(fresh, "apply", *flags, str(saved / patch))
    return fresh


def test_salvage_dir_reuse_never_overwrites_an_earlier_salvage(tmp_path, capsys):
    co, _ = _salvage_case(tmp_path)
    salv = tmp_path / "salvage"
    _run_plan(_plan_for(capsys, tmp_path / "r", salv), check=True)
    (saved,) = list(salv.iterdir())
    first = (saved / "HEAD").read_text()
    assert not co.exists()
    co, _ = _salvage_case(tmp_path, tag="2")  # a new checkout at the same path: same salvage name
    _run_plan(_plan_for(capsys, tmp_path / "r", salv))
    assert co.exists() and (saved / "HEAD").read_text() == first and list(salv.iterdir()) == [saved]


def test_bundle_keeps_a_branch_the_server_dropped(tmp_path, capsys):
    bare = tmp_path / "server.git"
    co = _repo(tmp_path / "r" / "co", age=False)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    _git(co, "remote", "add", "origin", str(bare))
    _git(co, "push", "-q", "-u", "origin", "main")
    _git(co, "switch", "-q", "-c", "feature")
    (co / "f.txt").write_text("feature work\n")
    _git(co, "add", "f.txt")
    _git(co, "commit", "-q", "-m", "feature")
    _git(co, "push", "-q", "-u", "origin", "feature")
    feature = _git(co, "rev-parse", "HEAD").strip()
    _git(co, "switch", "-q", "main")
    (co / "a.txt").write_text("main work\n")
    _git(co, "commit", "-q", "-am", "unpushed on main")
    _git(co, "remote", "set-url", "origin", "https://example.invalid/co")
    _git(bare, "branch", "-q", "-D", "feature")  # squash-merged and deleted upstream
    _age(co)
    assert _audit(capsys, "--root", str(tmp_path / "r"))["co"]["reasons"] == "unpushed"
    salv = tmp_path / "salvage"
    _run_plan(_plan_for(capsys, tmp_path / "r", salv), check=True)
    (saved,) = list(salv.iterdir())
    fresh = _restore(tmp_path, bare, saved)
    assert _git(fresh, "rev-parse", "refs/salvaged/feature").strip() == feature


def test_patches_are_canonical_and_index_only_changes_restore(tmp_path, capsys):
    bare = tmp_path / "server.git"
    co = _repo(tmp_path / "r" / "co", age=False)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    _git(co, "remote", "add", "origin", str(bare))
    _git(co, "push", "-q", "-u", "origin", "main")
    _git(co, "remote", "set-url", "origin", "https://example.invalid/co")
    for k, v in (("color.diff", "always"), ("color.ui", "always"), ("diff.noprefix", "true"),
                 ("diff.relative", "true"), ("diff.mnemonicPrefix", "true")):
        _git(co, "config", k, v)
    (co / "a.txt").write_text("staged\n")  # MM: staged, then changed again
    _git(co, "add", "a.txt")
    (co / "a.txt").write_text("staged then edited\n")
    (co / "new.txt").write_text("added only to the index\n")  # AD: added, then removed from disk
    _git(co, "add", "new.txt")
    (co / "new.txt").unlink()
    (co / "emptydir").mkdir()
    _age(co)
    assert _audit(capsys, "--root", str(tmp_path / "r"))["co"]["reasons"] == "dirty"
    salv = tmp_path / "salvage"
    plan = _plan_for(capsys, tmp_path / "r", salv)
    assert plan.splitlines()[1].startswith("unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE")
    assert all("--git-dir" in c and "--work-tree" in c for c in _cmds(plan) if c.startswith("git ") and " -C " in c
               and "worktree remove" not in c)
    _run_plan(plan, check=True)
    (saved,) = list(salv.iterdir())
    fresh = _restore(tmp_path, bare, saved)
    assert _git(fresh, "show", ":a.txt") == "staged\n" and (fresh / "a.txt").read_text() == "staged then edited\n"
    assert _git(fresh, "show", ":new.txt") == "added only to the index\n" and not (fresh / "new.txt").exists()
    assert (saved / "empty-dirs.txt").read_text().strip().endswith("/emptydir")


def test_failing_ls_files_stops_the_chain(tmp_path, capsys):
    co, _ = _salvage_case(tmp_path)
    plan = _plan_for(capsys, tmp_path / "r", tmp_path / "salvage")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git").write_text(f'#!/bin/sh\ncase " $* " in *" ls-files "*) exit 1;; esac\nexec {shutil.which("git")} "$@"\n')
    (shim / "git").chmod(0o755)
    _run_plan(plan, env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"})
    assert co.exists() and (co / "new.txt").exists()


def test_many_ignored_files_are_tarred_without_argument_limits(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "co", "c", age=False)
    (co / ".gitignore").write_text("*.dat\n")
    _git(co, "add", ".gitignore")
    _git(co, "commit", "-q", "-m", "ignore dat")
    _push(co)
    out = co / "out"
    out.mkdir()
    for i in range(60000):
        (out / f"r{i:05d}.dat").write_bytes(b"")
    _age(co)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["tier"], row["ignored"]) == ("SALVAGE", 60000)
    salv = tmp_path / "salvage"
    _run_plan(_plan_for(capsys, tmp_path / "r", salv), check=True)
    (saved,) = list(salv.iterdir())
    names = subprocess.run(["tar", "-tzf", str(saved / "ignored.tgz")], capture_output=True, text=True,
                           check=True).stdout.split()
    assert len(names) == 60000 and not co.exists()


def test_sparse_seat_is_not_held_for_skip_worktree(tmp_path, capsys):
    main = _repo(tmp_path / "main", "m")
    (main / "sub").mkdir()
    (main / "sub" / "x.txt").write_text("x\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "sub")
    _push(main)
    seat = tmp_path / "r" / "seat"
    _git(main, "worktree", "add", "-q", "--no-checkout", "--detach", str(seat))
    _git(seat, "sparse-checkout", "set", "--no-cone", "/a.txt", "/data.bin")
    _git(seat, "checkout", "-q", "--detach", "HEAD")
    assert not (seat / "sub").exists()
    _age(seat)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["seat"]
    assert "hidden-index-flags" not in row["reasons"] and row["tier"] == "SAFE", row["reasons"]
    # A changed file reappearing at a skip-worktree path must not be hidden: git itself drops the
    # bit for present files (reported as dirty); older git keeps it (hidden-index-flags).  Either way,
    # not SAFE.
    (seat / "sub").mkdir()
    (seat / "sub" / "x.txt").write_text("changed\n")
    _age(seat)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["seat"]
    assert row["tier"] != "SAFE" and {"dirty", "hidden-index-flags"} & set(row["reasons"].split(","))


def test_worktree_pin_on_a_pushed_commit_is_planned_and_removed(tmp_path, capsys):
    main = _repo(tmp_path / "main", "m")
    wt = tmp_path / "r" / "wt"
    _git(main, "worktree", "add", "-q", "-b", "a", str(wt))
    _git(wt, "update-ref", "refs/worktree/keep", "HEAD")
    _age(wt)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["wt"]
    assert (row["tier"], row["reasons"]) == ("SALVAGE", "worktree-refs")
    salv = tmp_path / "salvage"
    plan = _plan_for(capsys, tmp_path / "r", salv)
    assert "bundle create" not in plan and "refs.txt" in plan
    _run_plan(plan, check=True)
    (saved,) = list(salv.iterdir())
    assert not wt.exists() and "refs/worktree/keep" in (saved / "refs.txt").read_text()


def test_staged_gitlink_change_is_dirty(tmp_path, capsys):
    _src, parent, sub = _with_submodule(tmp_path)
    _git(sub, "switch", "-q", "-c", "moved")
    (sub / "a.txt").write_text("moved\n")
    _git(sub, "commit", "-q", "-am", "new submodule commit")
    _git(parent, "add", "sub")  # the superproject now records a different submodule commit
    _age(tmp_path / "r")
    rows = _audit(capsys, "--root", str(tmp_path / "r"))
    assert "dirty" in rows["parent"]["reasons"].split(",")


def test_primary_checkouts_are_held(tmp_path, capsys):
    repos = tmp_path / "Repos"
    src = _repo(repos / "source", "s")
    seat = _repo(repos / "source" / "seats" / "s-1", "x")  # nested seat: not primary
    (src / ".gitignore").write_text("seats/\n")
    _git(src, "add", ".gitignore")
    _git(src, "commit", "-q", "-m", "ignore seats")
    _push(src)
    _git(src, "worktree", "add", "-q", "-b", "w", str(repos / "linked"))  # linked child: not primary
    _age(repos)
    rows = _audit(capsys, "--root", str(repos), "--primary-root", str(repos))
    assert "primary" in rows["source"]["reasons"].split(",") and rows["source"]["tier"] == "HOLD"
    assert "primary" not in rows["s-1"]["reasons"] and "primary" not in rows["linked"]["reasons"]
    assert seat.exists()


def test_regenerable_opt_in_and_large_ignored_data_is_named(tmp_path, capsys):
    _repo_ignoring(tmp_path / "r" / "co", tmp_path / "remote.git", "build/\n", ["build/out.bin"])
    rows = _audit(capsys, "--root", str(tmp_path / "r"), "--regenerable", "build")
    assert rows["co"]["tier"] == "SAFE"
    assert audit.main(["--root", str(tmp_path / "r"), "--salvage-max-gb", "0.0000000001"]) == 0
    err = capsys.readouterr().err
    assert "#   ignored-data-large: 1 checkouts" in err and "--regenerable" in err


def test_hold_gb_goes_to_the_hold_reason_and_header_shows_tiers(monkeypatch, capsys):
    rows = [_fixture_row("a", "NON-COW", "HOLD", "no-remote,recent", gb=5.0),
            _fixture_row("b", "NON-COW", "HOLD", "dirty,ignored-data-large", gb=1.0),
            _fixture_row("c", "NON-COW", "SALVAGE", "no-remote", gb=2.0),
            _fixture_row("d", "NON-COW", "SAFE", gb=1.0)]
    monkeypatch.setattr(audit, "audit", lambda *a, **k: [dict(r) for r in rows])
    assert audit.main(["--root", "/x"]) == 0
    err = capsys.readouterr().err
    assert "# 4 checkouts (SAFE 1, SALVAGE 1, HOLD 2);" in err
    assert "HOLD 6.00 (recent 5.00, ignored-data-large 1.00)" in err
    assert "# NOT TARGETS (HOLD): 2 checkouts, 6.00 GB (recent 5.00, ignored-data-large 1.00)" in err


def test_every_hold_reason_has_an_importance_rank():
    assert set(audit.REASON_ORDER) - audit.SALVAGEABLE == set(audit.HOLD_ORDER)



def test_commit_only_on_a_pushed_non_default_branch_is_salvaged_into_a_bundle(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "co", "c", age=False)
    _git(co, "switch", "-q", "-c", "feat")
    (co / "a.txt").write_text("feature\n")
    _git(co, "commit", "-q", "-am", "only on origin/feat")
    _git(co, "update-ref", "refs/remotes/origin/feat", "HEAD")  # pushed, but not the default branch
    feat = _git(co, "rev-parse", "HEAD").strip()
    _git(co, "switch", "-q", "main")
    _age(co)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["tier"], row["reasons"], row["unpushed"]) == ("SALVAGE", "unpushed", 1)
    salv = tmp_path / "salvage"
    _run_plan(_plan_for(capsys, tmp_path / "r", salv), check=True)
    (saved,) = list(salv.iterdir())
    heads = subprocess.run(["git", "bundle", "list-heads", str(saved / "refs.bundle")], capture_output=True,
                           text=True, check=True).stdout
    assert f"{feat} refs/heads/feat" in heads and not co.exists()



def test_remote_head_symref_outside_the_remote_is_not_a_base(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "co", age=False)
    _git(co, "remote", "add", "origin", "https://example.invalid/co")
    _git(co, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/heads/main")  # hand-set, points at a local branch
    _age(co)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["tier"], row["reasons"], row["unpushed"]) == ("SALVAGE", "unpushed", 1)


def test_remote_head_symref_to_a_tracking_ref_is_the_base(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "co", "c", age=False)  # origin/main exists
    _git(co, "update-ref", "refs/remotes/origin/trunk", "HEAD")
    _git(co, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    _git(co, "update-ref", "-d", "refs/remotes/origin/main")
    _age(co)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["tier"], row["unpushed"]) == ("SAFE", 0)


def test_primary_includes_symlinked_dot_git_and_symlinked_in_repos(tmp_path, capsys):
    repos = tmp_path / "Repos"
    store = _repo(tmp_path / "store" / "real", "s")
    sep = repos / "sep"  # .git is a symlink into a separate store
    sep.mkdir(parents=True)
    os.symlink(store / ".git", sep / ".git")
    _git(sep, "checkout", "-q", "-f", "main")
    elsewhere = _repo(tmp_path / "elsewhere" / "linked-in", "e")
    os.symlink(elsewhere, repos / "linked-in")  # a repo that lives elsewhere, symlinked into Repos
    _age(repos)
    _age(elsewhere)
    rows = _audit(capsys, "--root", str(repos), "--root", str(tmp_path / "elsewhere"), "--primary-root", str(repos))
    assert rows["sep"]["link"] == "separate" and "primary" in rows["sep"]["reasons"].split(",")
    assert "primary" in rows["linked-in"]["reasons"].split(",")



# ---- --wide discovery and placement --------------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """HOME, /private/tmp and TMPDIR stand-ins, so --wide never walks the real machine."""
    home, ptmp, tmpdir = tmp_path / "home", tmp_path / "ptmp", tmp_path / "var-folders-T"
    for d in (home, ptmp, tmpdir):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(audit, "wide_roots", lambda: ["~", str(ptmp), str(tmpdir)])
    monkeypatch.setattr(audit, "tmp_roots", lambda: [str(ptmp), str(tmpdir)])
    return home, ptmp, tmpdir


def test_wide_finds_misplaced_checkouts_and_classifies_them(fake_home, tmp_path, capsys):
    home, ptmp, tmpdir = fake_home
    _repo(home / "Repos" / "good", "g")
    _repo(home / "Repos" / "good" / "worktrees" / "seat", "w")
    _repo(home / "Repos" / "good" / "docs-private" / "research" / "x", "d")
    _repo(home / "Downloads" / "x", "x")
    _repo(ptmp / "scratch", "t")
    _repo(tmpdir / "pytest-copy", "p")
    _repo(home / "Library" / "Caches" / "hidden-in-library", "l")
    rows = _audit(capsys, "--wide")
    assert "hidden-in-library" not in rows  # ~/Library is pruned
    assert rows["good"]["placement"] == rows["seat"]["placement"] == "expected"
    assert {n: rows[n]["placement"] for n in ("x", "scratch", "pytest-copy")} == dict.fromkeys(
        ("x", "scratch", "pytest-copy"), "misplaced")
    assert audit.main(["--wide"]) == 0
    err = capsys.readouterr().err
    assert "# MISPLACED: 4 checkouts" in err
    by = next(ln for ln in err.splitlines() if ln.startswith("#   by location:"))
    assert "docs-private 1" in by and "tmp 2" in by and "home-other 1" in by
    assert rows["x"]["tier"] in ("SAFE", "SALVAGE", "HOLD")  # placement never changes the tier


def test_nested_copy_inside_a_checkout_is_misplaced_nested(fake_home, capsys):
    home, _, _ = fake_home
    _repo(home / "Repos" / "good", "g")
    _repo(home / "Repos" / "good" / "tests" / "copy", "c")
    rows = _audit(capsys, "--wide")
    assert rows["copy"]["placement"] == "misplaced"
    assert audit.main(["--wide"]) == 0
    assert "nested-in-checkout 1" in capsys.readouterr().err


def test_custom_expected_adds_to_the_defaults(fake_home, capsys):
    home, _, _ = fake_home
    _repo(home / "Downloads" / "x", "x")
    _repo(home / "Repos" / "good", "g")
    rows = _audit(capsys, "--wide", "--expected", "~/Downloads/*")
    assert rows["x"]["placement"] == rows["good"]["placement"] == "expected"


def test_prune_and_no_misplaced_block_when_all_expected(fake_home, capsys):
    home, _, _ = fake_home
    _repo(home / "Repos" / "good", "g")
    _repo(home / "skipme" / "inner", "s")
    assert "inner" in _audit(capsys, "--wide")
    rows = _audit(capsys, "--wide", "--prune", "skipme")
    assert set(rows) == {"good"}
    assert audit.main(["--wide", "--prune", "skipme"]) == 0
    assert "MISPLACED" not in capsys.readouterr().err


def test_discovery_never_crosses_onto_another_filesystem(fake_home, capsys, monkeypatch):
    home, _, _ = fake_home
    _repo(home / "mnt" / "volume" / "repo", "v")
    _repo(home / "Repos" / "good", "g")
    real = audit._entry_dev
    monkeypatch.setattr(audit, "_entry_dev", lambda e: -1 if e.name == "mnt" else real(e))  # a mount point
    rows = _audit(capsys, "--wide")
    assert "repo" not in rows and "good" in rows
    rows = _audit(capsys, "--root", str(home / "mnt"))  # walked when given as a root itself
    assert "repo" in rows


def test_glob_match_is_per_component():
    assert audit._glob_match("/h/Repos/a", "/h/Repos/*")
    assert not audit._glob_match("/h/Repos/a/docs-private/x", "/h/Repos/*")
    assert audit._glob_match("/h/.goal-flight/remote-ci-managed/n1/mirror-x", "/h/.goal-flight/remote-ci-managed/*/mirror*")


def test_discovery_survives_an_entry_that_vanishes_before_stat(tmp_path, monkeypatch):
    # A directory that disappears between scandir and stat (routine in /tmp) must not hide its siblings.
    # The FIRST entry stat'ed fails, whatever the listing order, so the result is order-independent.
    root = tmp_path / "r"
    for name in ("a", "b", "c"):
        _make_checkout(root / name / "co", small=1, big_mib=0)
    real = audit._entry_dev
    calls = {"n": 0}

    def flaky(e):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError(e.path)
        return real(e)

    monkeypatch.setattr(audit, "_entry_dev", flaky)
    found = audit.find_checkouts([str(root)], (), 6)
    assert len(found) == 2  # only the vanished entry is lost, not its siblings



# ---- untracked size cap ---------------------------------------------------------------------------

def _untracked_case(tmp_path: Path, ignored: bool = False) -> Path:
    co = _repo(tmp_path / "r" / "co", "c", age=False)
    if ignored:
        (co / ".gitignore").write_text("cache-data/\n")
        _git(co, "add", ".gitignore")
        _git(co, "commit", "-q", "-m", "ignore")
        _push(co)
        (co / "cache-data").mkdir()
        (co / "cache-data" / "c.bin").write_bytes(b"c")
    (co / "results").mkdir()
    (co / "results" / "run.h5").write_bytes(b"r" * 1000)
    _age(co)
    return co


def _fake_sizes(monkeypatch, sizes: dict[str, int]) -> None:
    real = audit._tree_bytes
    monkeypatch.setattr(audit, "_tree_bytes", lambda root, cap, skip=(): next(
        (v for k, v in sizes.items() if os.path.basename(root) == k), None) or real(root, cap, skip))


def test_large_untracked_data_is_held_not_salvaged(tmp_path, capsys, monkeypatch):
    co = _untracked_case(tmp_path)
    _fake_sizes(monkeypatch, {"results": 2 * 10 ** 9})  # 2 GB of run results, without writing them
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["tier"], row["reasons"]) == ("HOLD", "dirty,untracked-large") and row["untracked_gb"] >= 1.0
    assert str(co) not in _plan_for(capsys, tmp_path / "r", tmp_path / "salvage")
    assert audit.main(["--root", str(tmp_path / "r")]) == 0
    assert "#   untracked-large: 1 checkouts" in capsys.readouterr().err


def test_small_untracked_data_stays_salvage(tmp_path, capsys):
    _untracked_case(tmp_path)
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["tier"], row["reasons"]) == ("SALVAGE", "dirty") and 0 < row["untracked_gb"] < 0.01


def test_untracked_plus_ignored_over_the_cap_is_salvage_large(tmp_path, capsys, monkeypatch):
    _untracked_case(tmp_path, ignored=True)
    _fake_sizes(monkeypatch, {"results": 6 * 10 ** 8, "cache-data": 6 * 10 ** 8})  # 0.6 + 0.6 GB > 1 GB
    row = _audit(capsys, "--root", str(tmp_path / "r"))["co"]
    assert (row["tier"], row["reasons"]) == ("HOLD", "dirty,ignored-data,salvage-large")



def test_changed_tracked_files_count_toward_the_salvage_cap(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "co", "c", age=False)
    (co / "big.bin").write_bytes(os.urandom(8000))
    _git(co, "add", "big.bin")
    _git(co, "commit", "-q", "-m", "big")
    _push(co)
    (co / "big.bin").write_bytes(os.urandom(8000))  # staged version
    _git(co, "add", "big.bin")
    (co / "big.bin").write_bytes(os.urandom(8000))  # and a different working-tree version
    _age(co)
    # 8 kB in the tree + 8 kB index blob = 16 kB of patch content, over a 10 kB cap.
    row = _audit(capsys, "--root", str(tmp_path / "r"), "--salvage-max-gb", "0.00001")["co"]
    assert (row["tier"], row["reasons"]) == ("HOLD", "dirty,salvage-large")
    assert _audit(capsys, "--root", str(tmp_path / "r"))["co"]["tier"] == "SALVAGE"  # under the default cap



# ---- firmlink / alias dedupe and package-manager protects ----------------------------------------

def _json_rows(capsys, *args: str) -> list[dict]:
    assert audit.main([*args, "--json", "--exact"]) == 0
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]


def test_two_roots_reaching_one_directory_give_one_row(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(audit, "_safe_realpath", lambda path, _skip: os.path.abspath(path))  # a firmlink: realpath keeps both spellings
    co = _repo(tmp_path / "a" / "co", "c")
    (tmp_path / "alias").symlink_to(tmp_path / "a")
    rows = _json_rows(capsys, "--root", str(tmp_path / "alias"), "--root", str(tmp_path / "a"))
    assert [r["path"] for r in rows] == [str(co)]  # one row, the shorter spelling
    # Different roots (so root dedupe can't merge them) whose walks reach the same checkout.
    rows = _json_rows(capsys, "--root", str(tmp_path / "alias"), "--root", str(co))
    assert [r["path"] for r in rows] == [str(co)]


def test_data_volume_prefix_is_stripped_when_it_is_the_same_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(audit, "_safe_realpath", lambda path, _skip: os.path.abspath(path))
    data = tmp_path / "sysdata"
    data.symlink_to("/")  # stand-in for /System/Volumes/Data: same directories under another prefix
    monkeypatch.setattr(audit, "FIRMLINK_DATA", str(data))
    co = _repo(tmp_path / "r" / "co", "c")
    _repo(co / "worktrees" / "inner", "i")
    rows = _json_rows(capsys, "--root", f"{data}{tmp_path / 'r'}", "--root", str(tmp_path / "r"))
    assert sorted(r["path"] for r in rows) == sorted([str(co), str(co / "worktrees" / "inner")])
    plan = _plan_for(capsys, Path(f"{data}{tmp_path / 'r'}"), tmp_path / "salvage")
    assert str(data) not in plan


def test_package_manager_installs_are_protected_with_their_taps(tmp_path, capsys):
    assert {"/opt/homebrew", "/usr/local/Homebrew"} <= set(audit.DEFAULT_PROTECT)
    brew = tmp_path / "homebrew"
    _repo(brew, "b")
    _repo(brew / "Library" / "Taps" / "org" / "homebrew-tap", "t")
    rows = _audit(capsys, "--root", str(tmp_path), "--protect", str(brew))
    assert "protected" in rows["homebrew"]["reasons"] and "protected" in rows["homebrew-tap"]["reasons"]


def test_created_since_filters_rows_and_reports_non_cow_totals(tmp_path, capsys):
    co = _repo(tmp_path / "r" / "co", "c", age=False)
    assert audit.main(["--root", str(tmp_path / "r"), "--json", "--created-since", "2000-01-01"]) == 0
    out = capsys.readouterr()
    rows = {Path(r["path"]).name: r for r in map(json.loads, out.out.splitlines())}
    assert set(rows) == {"co"} and rows["co"]["born_utc"] != "-"
    assert "# NON-COW created since 2000-01-01: 1," in out.err
    assert "NON-COW by class created since 2000-01-01" in out.err
    assert audit.main(["--root", str(tmp_path / "r"), "--json", "--created-since", "2999-01-01"]) == 0
    out = capsys.readouterr()
    assert not out.out
    assert "# NON-COW created since 2999-01-01: 0," in out.err
    assert co.exists()


def test_seat_parent_adds_deduped_non_checkout_folder_rows(tmp_path, capsys):
    parent = tmp_path / "scratch"
    job = parent / "job-a"
    job.mkdir(parents=True)
    (job / "output.bin").write_bytes(b"output")
    pattern = str(parent / "*")
    assert audit.main(["--root", str(tmp_path / "empty"), "--seat-parent", pattern, "--seat-parent", pattern,
                       "--json", "--exact"]) == 0
    rows = _rows(capsys)
    assert set(rows) == {"job-a"} and rows["job-a"]["kind"] == "dir"
    assert rows["job-a"]["born_utc"] != "-" and rows["job-a"]["tier"] == "-"


def test_skip_names_merge_environment_config_and_cli_without_printing_names(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "r"
    for name in ("env-hidden", "file-hidden", "cli-hidden", "keep"):
        _repo(root / name, name)
    cfg = home / ".config" / "goalflight" / "cow-skip.txt"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("file-hidden\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOALFLIGHT_COW_SKIP", "env-hidden")
    assert audit.main(["--root", str(root), "--json", "--skip-name", "cli-hidden"]) == 0
    captured = capsys.readouterr()
    rows = {Path(r["path"]).name: r for r in map(json.loads, captured.out.splitlines())}
    assert set(rows) == {"keep"}
    assert "# active skip names: 3" in captured.err
    assert not any(name in captured.err for name in ("env-hidden", "file-hidden", "cli-hidden"))


def test_skip_name_summary_groups_hold_rows_without_paths(monkeypatch, capsys):
    rows = [
        {"path": "/mock/repo-one", "kind": "checkout", "private_gb": 3.1,
         "cow": "NON-COW", "verdict": "REVIEW", "tier": "HOLD",
         "reasons": "has-skipped-names", "_reasons": ["has-skipped-names"],
         "_skip_matches": ["amber", "violet"]},
        {"path": "/mock/repo-two", "kind": "checkout", "private_gb": 70.0,
         "cow": "NON-COW", "verdict": "REVIEW", "tier": "HOLD",
         "reasons": "has-skipped-names", "_reasons": ["has-skipped-names"],
         "_skip_matches": ["violet"]},
        {"path": "/mock/repo-three", "kind": "checkout", "private_gb": 90.0,
         "cow": "shared", "verdict": "SAFE", "tier": "SAFE",
         "reasons": "", "_reasons": [], "_skip_matches": ["amber"]},
    ]
    monkeypatch.setattr(audit, "_skip_names", lambda cli: tuple(cli))
    monkeypatch.setattr(audit, "_seat_candidates", lambda *_args: [])
    monkeypatch.setattr(audit, "_owner_rules", lambda *_args: [])
    monkeypatch.setattr(audit, "audit", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(audit, "_tier_summary", lambda _rows: "")
    monkeypatch.setattr(audit, "_summary_groups", lambda *_args: None)
    monkeypatch.setattr(audit, "misplaced_block", lambda *_args: "")

    assert audit.main(["--root", "/mock/root", "--skip-name", "amber", "--skip-name", "violet",
                       "--json"]) == 0
    captured = capsys.readouterr()
    matches = [line for line in captured.err.splitlines() if line.startswith("skip-name matches:")]
    assert matches == ["skip-name matches: amber 1 rows 3.1 GB, violet 2 rows 73.1 GB"]


def test_delete_plan_includes_three_sibling_linked_worktrees(tmp_path, capsys):
    main = _repo(tmp_path / "main", "m")
    root = tmp_path / "r"
    seats = [root / f"seat-{i}" for i in range(3)]
    for seat in seats:
        _git(main, "worktree", "add", "-q", "--detach", str(seat))
    _age(root)
    plan = _plan_for(capsys, root, tmp_path / "salvage")
    removals = [line for line in _cmds(plan) if "worktree remove" in line]
    assert len(removals) == 3
    assert all(str(seat) in "\n".join(removals) for seat in seats)
    assert all(seat.exists() for seat in seats)


def test_linked_worktree_plan_uses_admin_gitdir_spelling(tmp_path, capsys, monkeypatch):
    main = _repo(tmp_path / "main", "m")
    real_root = tmp_path / "real"
    real_root.mkdir()
    alias_root = tmp_path / "data-volume-alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    seat = real_root / "worktrees" / "seat"
    _git(main, "worktree", "add", "-q", "--detach", str(seat))
    _age(real_root)
    monkeypatch.setattr(audit, "_safe_realpath", lambda path, _skip: os.path.abspath(path))
    plan = _plan_for(capsys, alias_root, tmp_path / "salvage")
    removals = [line for line in _cmds(plan) if "worktree remove" in line]
    assert removals == [f"git -C {shlex.quote(str(main))} worktree remove -- {shlex.quote(str(seat))}"]
    _run_plan(plan, check=True)
    assert not seat.exists()


def test_owner_map_and_checkout_class_are_reported(tmp_path, capsys):
    home = tmp_path / "home"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("HOME", str(home))
    try:
        repos = home / "Repos"
        co = _repo(repos / "primary", "primary")
        owners = home / "owners.tsv"
        owners.write_text(f"{co}\tproject-owner\n")
        assert audit.main(["--root", str(repos), "--json", "--exact", "--owners", str(owners),
                           "--primary-root", str(repos)]) == 0
        out = capsys.readouterr()
        rows = {Path(r["path"]).name: r for r in map(json.loads, out.out.splitlines())}
        assert rows["primary"]["owner"] == "project-owner"
        assert rows["primary"]["class"] == "primary"
        assert "NON-COW by owner" in out.err and "NON-COW by class" in out.err
    finally:
        monkeypatch.undo()


def test_owner_map_globs_match_displayed_paths_without_realpath(tmp_path, monkeypatch):
    repos = tmp_path / "Repos"
    repos.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(repos, target_is_directory=True)
    pattern = str(alias / "*")
    realpath = os.path.realpath
    calls = []

    def checked(path, *args, **kwargs):
        calls.append(str(path))
        assert str(path) != pattern
        return realpath(path, *args, **kwargs)

    monkeypatch.setattr(audit.os.path, "realpath", checked)
    assert audit._mapped_owner(str(repos / "project"), [(pattern, "alias-owner")], ()) is None
    assert pattern not in calls


def test_fleet_plan_files_are_exclusive_and_do_not_follow_symlinks(tmp_path):
    target = tmp_path / "new.sh"
    assert audit._write_plan_exclusive(str(target), "first\n", ())
    assert target.read_text() == "first\n" and target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        audit._write_plan_exclusive(str(target), "replacement\n", ())
    assert target.read_text() == "first\n"

    victim = tmp_path / "victim.sh"
    victim.write_text("keep\n")
    link = tmp_path / "link.sh"
    link.symlink_to(victim)
    with pytest.raises(FileExistsError):
        audit._write_plan_exclusive(str(link), "overwrite\n", ())
    assert victim.read_text() == "keep\n"


def test_fleet_plan_parent_alias_does_not_enter_a_skipped_directory(tmp_path, monkeypatch):
    alias = tmp_path / "alias"
    alias.symlink_to("/not-present/secret-hidden", target_is_directory=True)
    real_lstat = os.lstat
    touched = []

    def guarded(path, *args, **kwargs):
        touched.append(str(path))
        assert "hidden" not in str(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(audit.os, "lstat", guarded)
    assert not audit._write_plan_exclusive(str(alias / "plan.sh"), "plan\n", ("hidden",))
    assert touched[-1] == str(alias)


def test_checkout_classes_cover_each_location_type(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repos = home / "Repos"
    repo = repos / "primary"
    run = tmp_path / "test-runs" / "run-a"
    job = tmp_path / "scratch" / "job-a"
    rows = {
        repo: "primary",
        repo / "worktrees" / "seat": "repo-worktree",
        home / ".goal-flight" / "worktrees" / "seat": "pool",
        tmp_path / "remote-ci-managed" / "node" / "seats" / "s-1" / "checkout": "remote-ci-seat",
        job / "checkout": "job-tree",
        run / "source": "run-source",
        home / ".claude" / "skills" / "tool": "tool-install",
        tmp_path / "other" / "tmp" / "checkout": "scratch",
        Path("/goalflight-classify-test/other/checkout"): "other",
    }
    seats = [str(job)]
    tools = list(audit.DEFAULT_PROTECT)
    for path, expected in rows.items():
        assert audit._classify(str(path), [str(repos)], seats, tools, (), "repo") == expected
    assert audit._classify("/goalflight-classify-test/test-runs/run-a", [str(repos)], seats, tools, (), "dir") == "other"


class _FakeProcFunction:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


def _fake_libproc(monkeypatch, *, bsd_failure=0, fd_failure=0, fd_truncated=False,
                  fd_truncated_once=False, fd_truncated_pids=frozenset(), pid_count=1,
                  pid_list_failure=False, reuse=False, executable_path=None):
    state = {"bsd_calls": 0, "cwd_calls": 0, "pid_buffers": [], "fd_buffers": [],
             "fd_calls": {}, "region_calls": 0}

    def write_path(buffer, buffer_size, offset, path):
        raw = bytearray(buffer_size)
        encoded = os.fsencode(path) + b"\0"
        raw[offset:offset + len(encoded)] = encoded
        ctypes.memmove(buffer, bytes(raw), len(raw))
        return offset + len(encoded)

    def list_pids(kind, uid, buffer, buffer_size):
        assert kind == audit.PROC_UID_ONLY and uid == os.getuid()
        if buffer is None:
            return 0 if pid_list_failure else ctypes.sizeof(ctypes.c_int) * pid_count
        capacity = buffer_size // ctypes.sizeof(ctypes.c_int)
        state["pid_buffers"].append(capacity)
        if len(state["pid_buffers"]) == 1:
            return buffer_size  # full; the caller must grow and retry
        for index in range(pid_count):
            buffer[index] = 123 + index
        return ctypes.sizeof(ctypes.c_int) * pid_count

    def pid_info(pid, flavor, arg, buffer, buffer_size):
        if flavor == audit.PROC_PIDTBSDINFO:
            state["bsd_calls"] += 1
            if bsd_failure and state["bsd_calls"] == 1:
                ctypes.set_errno(bsd_failure)
                return 0
            raw = bytearray(buffer_size)
            struct.pack_into("=I", raw, audit.PBI_UID_OFFSET, os.getuid())
            struct.pack_into("=I", raw, audit.PBI_NFILES_OFFSET, 1)
            start = (10, 20) if not reuse or state["bsd_calls"] == 1 else (30, 40)
            struct.pack_into("=QQ", raw, audit.PBI_START_OFFSET, *start)
            raw[audit.PBI_COMM_OFFSET:audit.PBI_COMM_OFFSET + 8] = b"mockproc"
            ctypes.memmove(buffer, bytes(raw), len(raw))
            return audit.PBI_START_OFFSET + 16
        if flavor == audit.PROC_PIDVNODEPATHINFO:
            state["cwd_calls"] += 1
            generation = (state["cwd_calls"] if reuse else
                          pid if fd_truncated or fd_truncated_once or pid in fd_truncated_pids else 2)
            return write_path(buffer, buffer_size, audit.VNODE_PATH_OFFSET, f"/mock/cwd-{generation}")
        if flavor == audit.PROC_PIDLISTFDS:
            state["fd_buffers"].append(buffer_size)
            fd_calls = state["fd_calls"].get(pid, 0)
            state["fd_calls"][pid] = fd_calls + 1
            if (fd_truncated or pid in fd_truncated_pids
                    or (fd_truncated_once and fd_calls < audit.LIBPROC_BUFFER_RETRIES)):
                return buffer_size
            if fd_failure:
                ctypes.set_errno(fd_failure)
                return 0
            ctypes.memmove(buffer, struct.pack("=iI", 4, audit.PROX_FDTYPE_VNODE), 8)
            return 8
        if flavor == audit.PROC_PIDREGIONPATHINFO:
            state["region_calls"] += 1
            if arg != 0x2000:
                generation = state["cwd_calls"] if reuse else 2
                raw = bytearray(buffer_size)
                struct.pack_into("=QQ", raw, audit.REGION_INFO_SIZE - 16, 0x1000, 0x1000)
                path = os.fsencode(f"/mock/mapped-{generation}") + b"\0"
                raw[audit.REGION_PATH_OFFSET:audit.REGION_PATH_OFFSET + len(path)] = path
                ctypes.memmove(buffer, bytes(raw), len(raw))
                return audit.REGION_PATH_OFFSET + len(path)
            ctypes.set_errno(errno.EINVAL)
            return 0
        raise AssertionError(f"unexpected proc_pidinfo flavor {flavor}")

    def pid_path(pid, buffer, buffer_size):
        generation = state["cwd_calls"] if reuse else 2
        path = f"/mock/executable-{generation}" if reuse or executable_path is None else executable_path
        return write_path(buffer, buffer_size, 0, path)

    def fd_info(pid, fd, flavor, buffer, buffer_size):
        assert flavor == audit.PROC_PIDFDVNODEPATHINFO
        return write_path(buffer, buffer_size, audit.FD_VNODE_PATH_OFFSET, "/mock/fd-file")

    class Library:
        pass

    lib = Library()
    lib.proc_listpids = _FakeProcFunction(list_pids)
    lib.proc_pidinfo = _FakeProcFunction(pid_info)
    lib.proc_pidfdinfo = _FakeProcFunction(fd_info)
    lib.proc_pidpath = _FakeProcFunction(pid_path)
    monkeypatch.setattr(audit.sys, "platform", "darwin")
    monkeypatch.setattr(audit.ctypes.util, "find_library", lambda _name: "proc")
    monkeypatch.setattr(audit.ctypes, "CDLL", lambda *args, **kwargs: lib)
    return state


def test_libproc_offsets_match_arm64_sdk_literal_values():
    assert (
        audit.PBI_START_OFFSET,
        audit.REGION_INFO_SIZE,
        audit.REGION_INFO_SIZE - 16,
        audit.REGION_INFO_SIZE - 8,
        audit.REGION_PATH_OFFSET,
        audit.VNODE_PATH_OFFSET,
        audit.FD_VNODE_PATH_OFFSET,
    ) == (120, 96, 80, 88, 248, 152, 176)


def test_libproc_grows_full_buffers_and_finds_mapped_and_executable_paths(monkeypatch):
    state = _fake_libproc(monkeypatch)
    paths = audit._libproc_open_paths(thorough=True)
    assert paths is not None and paths.complete and paths.incomplete_processes == 0
    assert paths.scanned_processes == 1 and paths.not_visible_processes == 0
    assert {"/mock/cwd-2", "/mock/fd-file", "/mock/mapped-2", "/mock/executable-2"} <= paths
    assert state["pid_buffers"] == [17, 34] and state["fd_buffers"] == [8, 16]
    assert state["region_calls"] > 0


@pytest.mark.parametrize("stage", ["pid", "fd"])
@pytest.mark.parametrize("error", [errno.EPERM, errno.ESRCH])
def test_libproc_invisible_process_is_skipped(monkeypatch, stage, error):
    state = _fake_libproc(
        monkeypatch,
        bsd_failure=error if stage == "pid" else 0,
        fd_failure=error if stage == "fd" else 0,
    )
    paths = audit._libproc_open_paths()
    assert paths is not None and paths.complete and paths.incomplete_processes == 0
    assert paths.scanned_processes == 0
    assert paths.not_visible_processes == (1 if error == errno.EPERM else 0)
    assert state["region_calls"] == 0


def test_libproc_fd_buffer_truncation_exhaustion_makes_snapshot_incomplete(monkeypatch):
    state = _fake_libproc(monkeypatch, fd_truncated=True)
    paths = audit._libproc_open_paths()
    assert paths is not None and not paths.complete and paths.incomplete_processes == 1
    assert paths.scanned_processes == 0 and paths.not_visible_processes == 0
    assert any(path.endswith("cwd-123") for path in paths)
    assert state["fd_calls"][123] == 2 * audit.LIBPROC_BUFFER_RETRIES


def test_libproc_retries_an_incomplete_process_and_recovers(monkeypatch):
    state = _fake_libproc(monkeypatch, fd_truncated_once=True)
    paths = audit._libproc_open_paths()
    assert paths is not None and paths.complete and paths.incomplete_processes == 0
    assert paths.scanned_processes == 1
    assert state["fd_calls"][123] == audit.LIBPROC_BUFFER_RETRIES + 2


def test_libproc_keeps_partial_paths_at_the_five_percent_threshold(monkeypatch):
    state = _fake_libproc(monkeypatch, pid_count=20, fd_truncated_pids=frozenset({123}))
    paths = audit._libproc_open_paths()
    assert paths is not None and paths.complete
    assert paths.scanned_processes == 19 and paths.incomplete_processes == 1
    assert any(path.endswith("cwd-123") for path in paths)
    assert state["fd_calls"][123] == 2 * audit.LIBPROC_BUFFER_RETRIES


def test_libproc_marks_snapshot_incomplete_above_five_percent(monkeypatch):
    _fake_libproc(monkeypatch, pid_count=20, fd_truncated_pids=frozenset({123, 124}))
    paths = audit._libproc_open_paths()
    assert paths is not None and not paths.complete
    assert paths.scanned_processes == 18 and paths.incomplete_processes == 2


def test_libproc_pid_listing_failure_makes_snapshot_incomplete(monkeypatch):
    _fake_libproc(monkeypatch, pid_list_failure=True)
    paths = audit._libproc_open_paths()
    assert paths is not None and not paths.complete


@pytest.mark.parametrize("executable_path", [
    "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder",
    "/usr/bin/python3",
    "/Library/Apple/usr/bin/tool",
    "/Applications/Editor.app/Contents/MacOS/Editor",
])
def test_libproc_skips_region_walk_for_system_and_app_executables(monkeypatch, executable_path):
    state = _fake_libproc(monkeypatch, executable_path=executable_path)
    paths = audit._libproc_open_paths(thorough=True)
    assert paths is not None and paths.complete
    assert executable_path in paths
    assert state["region_calls"] == 0


def test_libproc_retries_when_pid_start_time_changes(monkeypatch):
    _fake_libproc(monkeypatch, reuse=True)
    paths = audit._libproc_open_paths(thorough=True)
    assert paths is not None and paths.complete
    assert {"/mock/cwd-2", "/mock/mapped-2", "/mock/executable-2"} <= paths
    assert not any(path.endswith("-1") for path in paths)


def test_open_scan_can_force_lsof(monkeypatch):
    expected = audit._OpenPaths()
    monkeypatch.setattr(audit, "_open_paths", REAL_OPEN_PATHS)
    monkeypatch.setattr(audit, "_libproc_open_paths", lambda: pytest.fail("libproc was used"))
    monkeypatch.setattr(audit, "_lsof_open_paths", lambda: expected)
    assert audit._open_paths("lsof") is expected


def test_open_scan_default_skips_regions_and_thorough_walks_them(monkeypatch):
    state = _fake_libproc(monkeypatch)
    monkeypatch.setattr(audit, "_open_paths", REAL_OPEN_PATHS)

    default = audit._open_paths()
    assert default is not None and default.complete
    assert {"/mock/cwd-2", "/mock/fd-file", "/mock/executable-2"} <= default
    assert "/mock/mapped-2" not in default
    assert state["region_calls"] == 0

    thorough = audit._open_paths("thorough")
    assert thorough is not None and thorough.complete
    assert "/mock/mapped-2" in thorough
    assert state["region_calls"] > 0


def test_libproc_detects_open_file_and_subprocess_working_directory(tmp_path, monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("libproc is macOS-only")
    co = _repo(tmp_path / "r" / "co", "c")
    monkeypatch.setattr(audit, "_open_paths", REAL_OPEN_PATHS)
    monkeypatch.setattr(audit, "_lsof_open_paths", lambda: pytest.fail("lsof fallback used on macOS"))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=co)
    try:
        pids = (os.getpid(), child.pid)
        real_cdll = ctypes.CDLL

        def restricted_cdll(*args, **kwargs):
            lib = real_cdll(*args, **kwargs)

            def list_pids(flavor, info, buffer, buffer_size):
                if flavor != audit.PROC_UID_ONLY or info != os.getuid():
                    return lib.proc_listpids(flavor, info, buffer, buffer_size)
                payload = (ctypes.c_int * len(pids))(*pids)
                size = ctypes.sizeof(payload)
                if buffer is None:
                    return size
                if buffer_size < size:
                    return buffer_size
                ctypes.memmove(buffer, payload, size)
                return size

            list_pids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
            list_pids.restype = ctypes.c_int

            class RestrictedLib:
                def __getattr__(self, name):
                    return list_pids if name == "proc_listpids" else getattr(lib, name)

            return RestrictedLib()

        monkeypatch.setattr(audit.ctypes, "CDLL", restricted_cdll)
        with open(co / "a.txt"):
            paths = audit._open_paths()
            assert paths is not None and paths.scanned_processes == 2
            assert os.path.realpath(co / "a.txt") in {os.path.realpath(p) for p in paths}
        paths = audit._open_paths()
        assert paths is not None and paths.scanned_processes == 2
        assert os.path.realpath(co) in {os.path.realpath(p) for p in paths}
    finally:
        child.terminate()
        child.wait(timeout=5)


def _fake_fleet_tools(tmp_path, monkeypatch, config):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_ssh = fakebin / "ssh"
    fake_scp = fakebin / "scp"
    remote_root = tmp_path / "remote"
    log_path = tmp_path / "fleet-log.jsonl"
    fake_scp.write_text("""#!/usr/bin/env python3
import json, os, shutil, sys, time
source, target = sys.argv[-2:]
host, _, remote = target.partition(':')
host_config = json.loads(os.environ['FAKE_SSH_CONFIG']).get(host, {})
with open(os.environ['FAKE_FLEET_LOG'], 'a') as log:
    log.write(json.dumps({'tool': 'scp', 'host': host}) + '\\n')
time.sleep(host_config.get('copy_delay', 0))
path = os.path.join(os.environ['FAKE_REMOTE_ROOT'], host, remote.lstrip('/'))
os.makedirs(os.path.dirname(path), exist_ok=True)
shutil.copyfile(source, path)
""")
    fake_ssh.write_text("""#!/usr/bin/env python3
import json, os, shlex, subprocess, sys, time
host, *command = sys.argv[1:]
config = json.loads(os.environ['FAKE_SSH_CONFIG'])
with open(os.environ['FAKE_FLEET_LOG'], 'a') as log:
    log.write(json.dumps({'tool': 'ssh', 'host': host, 'command': command}) + '\\n')
host_config = config.get(host, {})
if command == ['hostname', '-s']:
    time.sleep(host_config.get('hostname_delay', 0))
    if host_config.get('hostname_status', 0):
        raise SystemExit(host_config['hostname_status'])
    print(host_config.get('identity', host))
    raise SystemExit
time.sleep(host_config.get('audit_delay', 0))
if host_config.get('audit_status', 0):
    raise SystemExit(host_config['audit_status'])
argv = shlex.split(command[-1])
remote_path = argv[1]
argv[1] = os.path.join(os.environ['FAKE_REMOTE_ROOT'], host, remote_path.lstrip('/'))
argv[0] = sys.executable
result = subprocess.run(argv, text=True, capture_output=True)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
""")
    for executable in (fake_ssh, fake_scp):
        executable.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE_ROOT", str(remote_root))
    monkeypatch.setenv("FAKE_FLEET_LOG", str(log_path))
    monkeypatch.setenv("FAKE_SSH_CONFIG", json.dumps(config))
    monkeypatch.setenv("PATH", f"{fakebin}:{os.environ['PATH']}")
    return log_path, remote_root


def test_fleet_identity_failure_drops_rows_and_skips_audit_command(tmp_path, capsys, monkeypatch):
    root = tmp_path / "r"
    _repo(root / "co", "c")
    log_path, _remote_root = _fake_fleet_tools(
        tmp_path, monkeypatch, {"node-a": {"identity": "node-a"}, "node-b": {"identity": "wrong-node"}})
    assert audit.main(["--root", str(root), "--hosts", "node-a,node-b", "--json"]) == 2
    captured = capsys.readouterr()
    rows = [json.loads(line) for line in captured.out.splitlines()]
    assert len(rows) == 1 and rows[0]["host"] == "node-a"
    assert "# IDENTITY-FAIL node-b" in captured.err
    commands = [json.loads(line) for line in log_path.read_text().splitlines()]
    node_b = [entry for entry in commands if entry["host"] == "node-b"]
    assert [entry["tool"] for entry in node_b] == ["scp", "ssh"]
    assert node_b[1]["command"] == ["hostname", "-s"]


def test_fleet_merged_totals_include_all_successful_hosts(tmp_path, capsys, monkeypatch):
    root = tmp_path / "r"
    _repo(root / "co", "c")
    _fake_fleet_tools(tmp_path, monkeypatch, {"node-a": {}, "node-c": {}})
    assert audit.main(["--root", str(root), "--hosts", "node-a,node-c", "--json"]) == 0
    captured = capsys.readouterr()
    rows = [json.loads(line) for line in captured.out.splitlines()]
    assert {row["host"] for row in rows} == {"node-a", "node-c"}
    total = sum(row["private_gb"] for row in rows)
    noncow = sum(row["private_gb"] for row in rows if row["cow"] == "NON-COW")
    summary = next(line for line in captured.err.splitlines() if line.startswith("# FLEET:"))
    assert summary == (f"# FLEET: 2 hosts, 2 rows, {total:.2f} GB private total, "
                       f"{noncow:.2f} GB NON-COW")


def test_fleet_audit_failure_returns_nonzero_and_names_host(tmp_path, capsys, monkeypatch):
    root = tmp_path / "r"
    _repo(root / "co", "c")
    _fake_fleet_tools(tmp_path, monkeypatch, {"node-b": {"audit_status": 7}})
    assert audit.main(["--root", str(root), "--hosts", "node-b", "--json"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "# ERROR node-b" in captured.err and "# FLEET: 0 hosts, 0 rows" in captured.err


def test_fleet_honours_cumulative_per_host_timeout(tmp_path, capsys, monkeypatch):
    root = tmp_path / "r"
    _repo(root / "co", "c")
    log_path, _remote_root = _fake_fleet_tools(
        tmp_path, monkeypatch,
        {"slow-node": {"copy_delay": 0.08, "hostname_delay": 0.08,
                       "audit_delay": 0.35, "audit_status": 7}})
    started = time.monotonic()
    assert audit.main(["--root", str(root), "--hosts", "slow-node", "--host-timeout", "0.5", "--json"]) == 2
    elapsed = time.monotonic() - started
    captured = capsys.readouterr()
    assert elapsed < 1.0
    assert "# TIMEOUT slow-node" in captured.err
    commands = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [entry["tool"] for entry in commands] == ["scp", "ssh", "ssh"]
    assert commands[1]["command"] == ["hostname", "-s"]
    assert commands[2]["command"] != ["hostname", "-s"]
