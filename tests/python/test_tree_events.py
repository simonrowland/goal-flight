"""TreeEventMonitor must answer exactly what a fresh tree walk would.

These run against real FSEvents on temporary directories; they skip where
FSEvents is unavailable (non-macOS). Event delivery is asynchronous, so each
comparison polls until the monitor converges on the walk or a deadline passes.
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_tree_events as tree_events  # noqa: E402
import goalflight_watch  # noqa: E402

pytestmark = pytest.mark.skipif(
    tree_events._CoreServices.get() is None, reason="FSEvents unavailable"
)

SKIP = goalflight_watch._TREE_SKIP_DIR_NAMES


def _walk_map(root: Path) -> tuple[dict[str, float], bool]:
    """Reference: the watcher walks' view of ``root`` (canonical paths)."""
    top = tree_events.canonical_dir(root)
    mtimes: dict[str, float] = {}
    failed = False
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SKIP]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                mtimes[path] = os.stat(path).st_mtime
            except OSError:
                failed = True
    return mtimes, failed


def _converges(monitor: tree_events.TreeEventMonitor, root: Path, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        snap = monitor.snapshot()
        assert snap is not None
        got = (dict(snap.mtimes), snap.stat_failed)
        want = _walk_map(root)
        if got == want or time.monotonic() > deadline:
            assert got == want
            return snap
        time.sleep(0.1)


@pytest.fixture
def tree(tmp_path: Path):
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("a\n")
    (root / ".git").write_text("gitdir: elsewhere\n")  # linked-worktree file
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("x\n")
    monitor = tree_events.TreeEventMonitor.start(root, skip_names=SKIP)
    assert monitor is not None
    yield root, monitor
    monitor.close()


def test_seed_matches_walk_and_prunes_skip_dirs(tree) -> None:
    root, monitor = tree
    snap = _converges(monitor, root)
    names = {os.path.relpath(path, tree_events.canonical_dir(root)) for path in snap.mtimes}
    assert names == {"src/a.py", ".git"}


def test_in_place_append_is_seen(tree) -> None:
    # The case a directory mtime misses: an existing file edited in place.
    root, monitor = tree
    target = root / "src" / "a.py"
    os.utime(target, (1_000_000_000, 1_000_000_000))
    _converges(monitor, root)
    old_newest = monitor.snapshot().newest()
    with target.open("a") as handle:
        handle.write("more\n")
    snap = _converges(monitor, root)
    assert snap.newest() > old_newest
    assert snap.count_newer_than(time.time() - 60) >= 1


def test_writes_inside_skip_dirs_are_ignored(tree) -> None:
    root, monitor = tree
    before = dict(_converges(monitor, root).mtimes)
    (root / "node_modules" / "pkg" / "new.js").write_text("y\n")
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "a.pyc").write_bytes(b"\0")
    time.sleep(0.5)
    snap = _converges(monitor, root)
    assert dict(snap.mtimes) == before


def test_create_delete_and_directory_moves(tree, tmp_path: Path) -> None:
    root, monitor = tree
    _converges(monitor, root)
    (root / "src" / "b.py").write_text("b\n")
    _converges(monitor, root)
    (root / "src" / "a.py").unlink()
    _converges(monitor, root)
    # A subtree moved in from outside arrives as one directory event.
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "c.py").write_text("c\n")
    os.rename(outside, root / "moved")
    snap = _converges(monitor, root)
    assert any(path.endswith("moved/deep/c.py") for path in snap.mtimes)
    shutil.rmtree(root / "moved")
    snap = _converges(monitor, root)
    assert not any("moved" in path for path in snap.mtimes)


def test_symlinks_match_the_walk(tree, tmp_path: Path) -> None:
    root, monitor = tree
    (tmp_path / "real_dir").mkdir()
    (tmp_path / "real_dir" / "inside.py").write_text("i\n")
    os.symlink(tmp_path / "real_dir", root / "linked_dir")  # listed, not followed
    os.symlink(root / "src" / "a.py", root / "link_to_file")  # stat follows
    _converges(monitor, root)
    os.symlink(root / "nowhere", root / "broken")
    snap = _converges(monitor, root)
    assert snap.stat_failed


def test_root_registered_in_other_case_still_sees_events(tmp_path: Path) -> None:
    root = tmp_path / "CaseRoot"
    root.mkdir()
    variant = Path(str(root).replace("CaseRoot", "caseroot"))
    if not variant.exists():
        pytest.skip("case-sensitive volume")
    monitor = tree_events.TreeEventMonitor.start(variant, skip_names=SKIP)
    assert monitor is not None
    try:
        _converges(monitor, root)
        (root / "x.txt").write_text("x\n")
        snap = _converges(monitor, root)
        assert len(snap.mtimes) == 1
    finally:
        monitor.close()


def test_dropped_events_force_a_reseed(tree) -> None:
    root, monitor = tree
    _converges(monitor, root)
    # Simulate fseventsd dropping events: change the tree, then discard the
    # real events and inject a drop flag in their place.
    (root / "src" / "unseen.py").write_text("u\n")
    time.sleep(0.3)
    monitor._drain()
    monitor._pending.clear()
    monitor.handle_event(monitor.root, tree_events._FLAG_USER_DROPPED)
    snap = _converges(monitor, root, timeout=0.5)
    assert any(path.endswith("unseen.py") for path in snap.mtimes)


def test_event_queue_overflow_forces_a_reseed(tree, monkeypatch) -> None:
    root, monitor = tree
    _converges(monitor, root)
    monkeypatch.setattr(tree_events, "MAX_PENDING_EVENTS", 3)
    for index in range(10):
        (root / "src" / f"f{index}.py").write_text(str(index))
    _converges(monitor, root)


def test_random_operations_stay_equal_to_the_walk(tree) -> None:
    root, monitor = tree
    rng = random.Random(20260923)
    dirs = [root / "src", root / "src" / "pkg", root / "docs"]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
    for step in range(40):
        directory = rng.choice(dirs)
        path = directory / f"f{rng.randrange(8)}.txt"
        op = rng.choice(["write", "append", "delete", "rename"])
        if op == "write":
            path.write_text(str(step))
        elif op == "append" and path.exists():
            with path.open("a") as handle:
                handle.write(str(step))
        elif op == "delete" and path.exists():
            path.unlink()
        elif op == "rename" and path.exists():
            path.rename(rng.choice(dirs) / f"r{step}.txt")
        if step % 8 == 7:
            _converges(monitor, root)
    _converges(monitor, root)


def test_start_returns_none_for_a_missing_root(tmp_path: Path) -> None:
    assert tree_events.TreeEventMonitor.start(tmp_path / "absent", skip_names=SKIP) is None


def test_directory_replaced_by_a_file_leaves_no_stale_entries(tree) -> None:
    root, monitor = tree
    (root / "d").mkdir()
    (root / "d" / "x.py").write_text("x\n")
    _converges(monitor, root)
    shutil.rmtree(root / "d")
    (root / "d").write_text("now a file\n")
    snap = _converges(monitor, root)
    assert not snap.stat_failed
    assert not any(path.endswith("/d/x.py") for path in snap.mtimes)


def test_root_renamed_away_stops_serving_the_old_map(tree, tmp_path: Path) -> None:
    root, monitor = tree
    _converges(monitor, root)
    os.rename(root, tmp_path / "renamed")
    deadline = time.monotonic() + 5.0
    snap = monitor.snapshot()
    while snap is not None and time.monotonic() < deadline:
        time.sleep(0.1)
        snap = monitor.snapshot()
    assert snap is None


def test_periodic_reseed_repairs_a_silent_gap(tree, monkeypatch) -> None:
    root, monitor = tree
    _converges(monitor, root)
    # Simulate a change FSEvents never reported (e.g. a symlink target
    # outside the tree): drop an entry from the map behind the monitor's back.
    victim = next(iter(monitor._mtimes))
    del monitor._mtimes[victim]
    assert victim not in monitor.snapshot().mtimes
    monkeypatch.setattr(tree_events, "RESEED_INTERVAL_S", 0.0)
    assert victim in monitor.snapshot().mtimes
