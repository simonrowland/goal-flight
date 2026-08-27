#!/usr/bin/env python3
"""Journal GC must never reclaim an unverified absence.

Four states, not three: live / root-gone / empty / unknown. Unknown is never
reclaimed. Root-gone journals that still hold an ACTIVE lease with a live
holder, or a non-terminal dispatch, are retained with the reason printed.
Casualty sidecars are their own category. Report-only is the default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import goalflight_journal as journal  # noqa: E402
import goalflight_journal_gc as gc  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def _create(project: Path) -> journal.Journal:
    return journal.Journal.create(project)


def _claim(authority: journal.Journal, label: str = "controller") -> journal.LeaseIdentity:
    result = authority.claim_or_renew_lease(
        label,
        principal={"pid": os.getpid(), "start_token": "gc-test", "hostname": "test"},
    )
    assert result.committed and result.value is not None, result.reason
    return result.value


def _journal_dir(authority: journal.Journal) -> Path:
    return authority.path.parent


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("PATH", "/usr/bin:/bin")
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_journal_gc.py"), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=30,
    )


def _run_json(*args: str) -> dict:
    proc = _run("--json", *args)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def _entry_for(payload: dict, journal_dir: Path) -> dict:
    target = str(journal_dir)
    for entry in payload["entries"]:
        if entry["journal"] == target:
            return entry
    raise AssertionError(f"no entry for {target} in {payload['entries']}")


def test_live_root_is_kept(tmp_path: Path) -> None:
    project = _project(tmp_path, "live-repo")
    authority = _create(project)
    _claim(authority)
    entry = gc.classify(_journal_dir(authority))
    assert entry["state"] == "live"
    assert entry["reclaimable"] is False
    assert entry["root"] == str(project)
    assert "still exists" in entry["why"]


def test_missing_root_is_root_gone(tmp_path: Path) -> None:
    project = _project(tmp_path, "doomed-repo")
    authority = _create(project)
    _claim(authority)
    journal_dir = _journal_dir(authority)
    shutil.rmtree(project)
    entry = gc.classify(journal_dir)
    assert entry["state"] == "root_gone"
    assert entry["reclaimable"] is True
    assert entry["root"] == str(project)
    assert "no longer exists" in entry["why"]


def test_create_only_journal_with_unrecorded_root_is_unknown(tmp_path: Path) -> None:
    project = _project(tmp_path, "empty-repo")
    authority = _create(project)
    entry = gc.classify(_journal_dir(authority))
    assert entry["state"] == "unknown"
    assert entry["reclaimable"] is False
    assert "no recorded project_root" in entry["why"]
    assert project.is_dir()


def test_orphan_journal_dir_without_sqlite_is_empty(tmp_path: Path) -> None:
    store = gc.journals_store()
    store.mkdir(parents=True, exist_ok=True)
    orphan = store / "orphan-empty"
    orphan.mkdir()
    entry = gc.classify(orphan)
    assert entry["state"] == "empty"
    assert entry["reclaimable"] is True
    assert "holds no data" in entry["why"]


def test_unreadable_journal_is_unknown_never_reclaimed(tmp_path: Path) -> None:
    project = _project(tmp_path, "locked-repo")
    authority = _create(project)
    _claim(authority)
    sqlite_path = authority.path
    journal_dir = _journal_dir(authority)
    os.chmod(sqlite_path, 0o000)
    try:
        entry = gc.classify(journal_dir)
        assert entry["state"] == "unknown", entry
        assert entry["reclaimable"] is False
        assert "unreadable" in entry["why"]
    finally:
        os.chmod(sqlite_path, 0o644)


def test_unreadable_ancestor_is_not_absence(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    project = locked / "real-repo"
    project.mkdir()
    authority = _create(project)
    _claim(authority)
    journal_dir = _journal_dir(authority)
    os.chmod(locked, 0o000)
    try:
        entry = gc.classify(journal_dir)
        assert entry["state"] == "unknown", entry
        assert entry["reclaimable"] is False
        assert "unverified" in entry["why"], entry
    finally:
        os.chmod(locked, 0o755)


def test_non_path_root_is_unknown(tmp_path: Path) -> None:
    project = _project(tmp_path, "relative-root")
    authority = _create(project)
    _claim(authority)
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            "UPDATE controller_leases SET project_root = ?",
            ("not-an-absolute-path",),
        )
        connection.commit()
    entry = gc.classify(_journal_dir(authority))
    assert entry["state"] == "unknown"
    assert entry["reclaimable"] is False
    assert "not an absolute path" in entry["why"]


@pytest.mark.parametrize("prefix", ["/Volumes/", "/net/", "/mnt/", "/media/"])
def test_detachable_mounts_are_unknown(tmp_path: Path, prefix: str) -> None:
    project = _project(tmp_path, f"mount-{prefix.strip('/')}")
    authority = _create(project)
    _claim(authority)
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            "UPDATE controller_leases SET project_root = ?",
            (f"{prefix}drive/some-repo-that-does-not-exist",),
        )
        connection.commit()
    entry = gc.classify(_journal_dir(authority))
    assert entry["state"] == "unknown"
    assert entry["reclaimable"] is False
    assert "detached" in entry["why"]


def test_active_lease_with_live_holder_is_retained(tmp_path: Path) -> None:
    project = _project(tmp_path, "held-repo")
    authority = _create(project)
    lease = _claim(authority, "alice")
    holder = wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    )
    journal_dir = _journal_dir(authority)
    try:
        shutil.rmtree(project)
        proc = _run()
        assert proc.returncode == 0, proc.stderr
        assert journal_dir.is_dir()
        assert "ACTIVE lease with a live holder" in proc.stdout
        assert "alice" in proc.stdout
        payload = _run_json()
        entry = _entry_for(payload, journal_dir)
        assert entry["state"] == "root_gone"
        assert entry["reclaimable"] is False
        assert "live holder" in entry["why"]
    finally:
        holder.close()


def test_non_terminal_dispatch_is_retained(tmp_path: Path) -> None:
    project = _project(tmp_path, "running-repo")
    authority = _create(project)
    prepared = authority.prepare_attempt("still-running")
    assert prepared.committed
    journal_dir = _journal_dir(authority)
    shutil.rmtree(project)
    entry = gc.classify(journal_dir)
    assert entry["state"] == "root_gone"
    assert entry["reclaimable"] is False
    assert "non-terminal dispatch" in entry["why"]
    assert "still-running" in entry["why"]


def test_casualty_sidecar_is_its_own_category(tmp_path: Path) -> None:
    project = _project(tmp_path, "official-repo")
    authority = _create(project)
    _claim(authority)
    official = _journal_dir(authority)
    casualty = official.parent / f"{official.name}.dev-casualty-20260814-062226"
    shutil.copytree(official, casualty)
    entry = gc.classify(casualty)
    assert entry["state"] == "casualty"
    assert entry["reclaimable"] is False
    assert "sidecar" in entry["why"]
    payload = _run_json()
    assert payload["casualty"] >= 1
    reported = _entry_for(payload, casualty)
    assert reported["state"] == "casualty"
    assert reported["reclaimable"] is False
    assert casualty.is_dir()


def test_report_only_deletes_nothing(tmp_path: Path) -> None:
    project = _project(tmp_path, "empty-kept")
    authority = _create(project)
    journal_dir = _journal_dir(authority)
    orphan = gc.journals_store() / "orphan-empty"
    orphan.mkdir(parents=True, exist_ok=True)
    payload = _run_json()
    created = _entry_for(payload, journal_dir)
    assert created["reclaimable"] is False
    reported_orphan = _entry_for(payload, orphan)
    assert reported_orphan["state"] == "empty"
    assert reported_orphan["reclaimable"] is True
    assert payload["applied"] is False
    assert payload["deleted"] == 0
    assert journal_dir.is_dir()
    assert authority.path.is_file()
    assert orphan.is_dir()


def test_apply_deletes_only_reclaimable(tmp_path: Path) -> None:
    live = _project(tmp_path, "keep-live")
    live_auth = _create(live)
    _claim(live_auth)
    live_dir = _journal_dir(live_auth)

    gone = _project(tmp_path, "reap-gone")
    gone_auth = _create(gone)
    _claim(gone_auth)
    gone_dir = _journal_dir(gone_auth)
    shutil.rmtree(gone)

    empty = _project(tmp_path, "keep-empty-live")
    empty_auth = _create(empty)
    empty_dir = _journal_dir(empty_auth)

    orphan = gc.journals_store() / "orphan-empty"
    orphan.mkdir(parents=True, exist_ok=True)

    locked = _project(tmp_path, "keep-unreadable")
    locked_auth = _create(locked)
    _claim(locked_auth)
    locked_dir = _journal_dir(locked_auth)
    os.chmod(locked_auth.path, 0o000)
    try:
        payload = _run_json("--apply")
        assert payload["applied"] is True
        assert live_dir.is_dir()
        assert locked_dir.is_dir()
        assert empty_dir.is_dir()
        assert empty.is_dir()
        assert not gone_dir.exists()
        assert not orphan.exists()
        assert payload["deleted"] >= 2
    finally:
        if locked_auth.path.exists():
            os.chmod(locked_auth.path, 0o644)


def test_apply_does_not_delete_create_only_journal_of_live_project(tmp_path: Path) -> None:
    project = _project(tmp_path, "fresh-repo")
    authority = _create(project)
    journal_dir = _journal_dir(authority)
    payload = _run_json("--apply")
    entry = _entry_for(payload, journal_dir)
    assert entry["reclaimable"] is False
    assert payload["deleted"] == 0
    assert journal_dir.is_dir()
    assert authority.path.is_file()
    assert project.is_dir()


def test_reverification_refuses_a_root_that_came_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, "flapping-repo")
    authority = _create(project)
    _claim(authority)
    journal_dir = _journal_dir(authority)
    recorded_root = str(project)
    shutil.rmtree(project)
    assert gc.classify(journal_dir)["reclaimable"] is True

    calls = {"n": 0}
    real_classify = gc.classify

    def flapping_classify(path: Path) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_classify(path)
        Path(recorded_root).mkdir(exist_ok=True)
        return real_classify(path)

    monkeypatch.setattr(gc, "classify", flapping_classify)
    rc = gc.main(["--apply"])
    assert rc == 0
    assert journal_dir.is_dir(), "a root that reappeared must not be reaped"


def test_negative_limit_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path, "limit-repo")
    authority = _create(project)
    journal_dir = _journal_dir(authority)
    proc = _run("--apply", "--limit", "-1")
    assert proc.returncode != 0, proc.stdout
    assert journal_dir.is_dir()


def _expire_lease_at(authority: journal.Journal, root: Path | str) -> None:
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            "UPDATE controller_leases SET project_root = ?, state = ?, "
            "ended_at = ?, ended_reason = ?",
            (str(root), journal.LEASE_EXPIRED, "2026-01-01T00:00:00+00:00", "expired"),
        )
        connection.commit()


def _terminal_attempt(
    authority: journal.Journal, dispatch_id: str, *, project_root: Path | str | None = None
) -> None:
    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None, prepared.reason
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete"},
    )
    assert committed.committed, committed.reason
    if project_root is None:
        return
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            "UPDATE dispatch_attempts SET project_root = ?",
            (str(project_root),),
        )
        connection.commit()


def test_cross_table_gone_lease_and_live_attempt_is_unknown(tmp_path: Path) -> None:
    """EXPIRED lease at a missing path plus TERMINAL attempt at a live path.

    A worktree-stripped slug shares one journal across checkouts. Leases from a
    since-deleted worktree must not hide the still-live main-tree attempt root.
    """
    live = _project(tmp_path, "live-main")
    gone = _project(tmp_path, "gone-worktree")
    gone_root = str(gone)
    authority = _create(live)
    _claim(authority)
    _expire_lease_at(authority, gone_root)
    shutil.rmtree(gone)
    _terminal_attempt(authority, "cross-table-live")
    journal_dir = _journal_dir(authority)
    live_root = str(authority.project_root)

    entry = gc.classify(journal_dir)
    assert entry["state"] == "unknown", entry
    assert entry["reclaimable"] is False
    assert gone_root in entry["roots"]
    assert live_root in entry["roots"]
    assert "multiple recorded project_root" in entry["why"]

    stale = {
        "journal": str(journal_dir),
        "root": gone_root,
        "state": "root_gone",
        "reclaimable": True,
        "why": "root no longer exists",
    }
    deleted, failed = gc.apply_deletes([stale], limit=0)
    assert deleted == 0
    assert failed
    assert journal_dir.is_dir()
    assert live.is_dir()

    payload = _run_json("--apply")
    reported = _entry_for(payload, journal_dir)
    assert reported["state"] == "unknown"
    assert reported["reclaimable"] is False
    assert gone_root in reported["roots"]
    assert live_root in reported["roots"]
    assert journal_dir.is_dir()
    assert live.is_dir()

    proc = _run()
    assert proc.returncode == 0, proc.stderr
    assert gone_root in proc.stdout
    assert live_root in proc.stdout
    assert "roots=" in proc.stdout


def test_cross_table_live_lease_and_gone_attempt_is_unknown(tmp_path: Path) -> None:
    """The reverse shape: lease root still exists, attempt root is gone."""
    live = _project(tmp_path, "live-main-rev")
    gone = _project(tmp_path, "gone-worktree-rev")
    gone_root = str(gone)
    authority = _create(live)
    _claim(authority)
    _expire_lease_at(authority, authority.project_root)
    _terminal_attempt(authority, "cross-table-gone", project_root=gone_root)
    shutil.rmtree(gone)
    journal_dir = _journal_dir(authority)
    live_root = str(authority.project_root)

    entry = gc.classify(journal_dir)
    assert entry["state"] == "unknown", entry
    assert entry["reclaimable"] is False
    assert gone_root in entry["roots"]
    assert live_root in entry["roots"]

    payload = _run_json("--apply")
    reported = _entry_for(payload, journal_dir)
    assert reported["reclaimable"] is False
    assert journal_dir.is_dir()
    assert live.is_dir()


def test_cli_json_four_state_counts(tmp_path: Path) -> None:
    live = _project(tmp_path, "count-live")
    live_auth = _create(live)
    _claim(live_auth)

    gone = _project(tmp_path, "count-gone")
    gone_auth = _create(gone)
    _claim(gone_auth)
    shutil.rmtree(gone)

    empty = gc.journals_store() / "count-empty"
    empty.mkdir(parents=True, exist_ok=True)

    locked = _project(tmp_path, "count-unknown")
    locked_auth = _create(locked)
    _claim(locked_auth)
    os.chmod(locked_auth.path, 0o000)
    try:
        payload = _run_json()
        assert payload["schema"] == "goalflight.journal-gc.v1"
        assert payload["live"] >= 1
        assert payload["root_gone"] >= 1
        assert payload["empty"] >= 1
        assert payload["unknown"] >= 1
        assert payload["applied"] is False
        assert payload["deleted"] == 0
    finally:
        os.chmod(locked_auth.path, 0o644)
