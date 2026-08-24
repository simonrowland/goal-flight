"""Reaping derived indexes must never act on an unverified absence.

The reaper deletes indexer project stores whose source tree is gone — 352 stores
holding 106.6 GB on the machine that motivated it. Deleting derived data is
normally cheap, but here the source tree no longer exists, so the index is its
last remaining artifact and removal is irreversible in practice.

Every test below is therefore about the *refusal* cases: absence must be proven,
not assumed. A store whose root is merely unknown, unreadable, or on a mount that
could be detached is kept, because none of those establish that anything was
deleted.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_reap_codedb_orphans as reap  # noqa: E402


def make_store(home: Path, name: str, root: str | None, *, size: int = 2048) -> Path:
    """Create one project store; `root=None` omits project.txt entirely."""
    d = home / "projects" / name
    d.mkdir(parents=True)
    if root is not None:
        (d / "project.txt").write_text(root + "\n")
    (d / "index.bin").write_bytes(b"x" * size)
    return d


def run_cli(home: Path, *args: str) -> dict:
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_reap_codedb_orphans.py"),
         "--json", *args],
        capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "CODEDB_HOME": str(home)},
    )
    return json.loads(done.stdout)


def test_live_root_is_kept(tmp_path: Path) -> None:
    live = tmp_path / "live-repo"
    live.mkdir()
    d = make_store(tmp_path / "home", "aaa", str(live))
    assert reap.classify(d)["verdict"] == "keep"


def test_missing_root_is_an_orphan(tmp_path: Path) -> None:
    d = make_store(tmp_path / "home", "bbb", str(tmp_path / "deleted-repo"))
    entry = reap.classify(d)
    assert entry["verdict"] == "orphan"
    assert entry["root"] == str(tmp_path / "deleted-repo")


@pytest.mark.parametrize("root,label", [
    (None, "no project.txt"),
    ("", "empty project.txt"),
])
def test_unknown_root_is_never_an_orphan(tmp_path: Path, root, label) -> None:
    """An unknown root means absence is unverifiable, which is not permission."""
    d = make_store(tmp_path / "home", "ccc", root)
    assert reap.classify(d)["verdict"] == "keep", label


@pytest.mark.parametrize("prefix", ["/Volumes/", "/net/", "/mnt/", "/media/"])
def test_detachable_mounts_are_never_orphans(tmp_path: Path, prefix: str) -> None:
    """'Not mounted' is not 'deleted' — an absent network root proves nothing."""
    d = make_store(tmp_path / "home", f"m{prefix.strip('/')}",
                   f"{prefix}drive/some-repo-that-does-not-exist")
    entry = reap.classify(d)
    assert entry["verdict"] == "keep"
    assert "detached" in entry["why"]


def test_unreadable_ancestor_is_not_absence(tmp_path: Path) -> None:
    """A root we cannot LOOK at is not a root that is GONE.

    `os.path.exists()` answers False for both, and this tool deletes on that
    answer, so a live repository behind an ancestor lacking search permission was
    classified `orphan` and removed. Found by adversarial review after the tool
    had already reclaimed 106 GB on a real machine.
    """
    import os
    home = tmp_path / "home"
    locked = tmp_path / "locked"
    locked.mkdir()
    live_root = locked / "real-repo"
    live_root.mkdir()
    store = make_store(home, "behind-lock", str(live_root))

    os.chmod(locked, 0o000)
    try:
        entry = reap.classify(store)
        assert entry["verdict"] == "keep", entry
        assert "unverified" in entry["why"], entry
    finally:
        os.chmod(locked, 0o755)


def test_genuine_absence_is_still_detected(tmp_path: Path) -> None:
    """The narrowing must not swallow the true positive."""
    store = make_store(tmp_path / "home", "really-gone", str(tmp_path / "never-existed"))
    assert reap.classify(store)["verdict"] == "orphan"


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    orphan = make_store(home, "gone", str(tmp_path / "absent"))
    report = run_cli(home)
    assert report["orphans"] == 1
    assert report["applied"] is False
    assert report["deleted"] == 0
    assert orphan.exists(), "dry run must not delete"


def test_apply_deletes_only_orphans(tmp_path: Path) -> None:
    home = tmp_path / "home"
    live_root = tmp_path / "live"
    live_root.mkdir()
    kept = make_store(home, "keep-me", str(live_root))
    orphan = make_store(home, "reap-me", str(tmp_path / "absent"))

    report = run_cli(home, "--apply")
    assert report["deleted"] == 1
    assert not orphan.exists()
    assert kept.exists(), "a store with a live root must survive --apply"


def test_limit_bounds_the_deletion(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for i in range(4):
        make_store(home, f"o{i}", str(tmp_path / f"absent{i}"))
    report = run_cli(home, "--apply", "--limit", "2")
    assert report["orphans"] == 4
    assert report["deleted"] == 2


def test_reverification_refuses_a_root_that_came_back(tmp_path: Path, monkeypatch) -> None:
    """The listing is a snapshot; a root present at delete time must be spared.

    Without the re-check, a tree restored between scan and delete loses its index
    even though it is live again.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("CODEDB_HOME", str(home))
    back = tmp_path / "restored"
    store = make_store(home, "flapping", str(back))

    calls = {"n": 0}
    real_classify = reap.classify

    def flapping_classify(path: Path) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:            # scan pass: root absent -> orphan
            return real_classify(path)
        back.mkdir(exist_ok=True)      # root restored before the delete pass
        return real_classify(path)

    monkeypatch.setattr(reap, "classify", flapping_classify)
    reap.main(["--apply"])

    assert store.exists(), "a root that reappeared must not be reaped"
