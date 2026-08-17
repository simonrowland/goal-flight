"""A GitHub repo is one repo however it reaches the disk.

Directory identity cannot see that: separate clones of one repo look like
peers, which is what filled the fleet console with 23 'projects' for ~6
repos. Identity therefore comes from the normalized origin remote, resolved
once at registry-write time and cached as a scalar.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import goalflight_task as task  # noqa: E402


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/timdrpp/battery-tool-v2.git", "github.com/timdrpp/battery-tool-v2"),
        ("git@github.com:timdrpp/battery-tool-v2.git", "github.com/timdrpp/battery-tool-v2"),
        ("ssh://git@github.com/timdrpp/battery-tool-v2", "github.com/timdrpp/battery-tool-v2"),
        ("https://user@github.com/TimDRPP/Battery-Tool-V2/", "github.com/timdrpp/battery-tool-v2"),
        ("", None),
        (None, None),
        ("not-a-url", None),
    ],
)
def test_every_spelling_of_one_repo_normalizes_to_one_identity(url, expected) -> None:
    assert task.normalize_repo_remote(url) == expected


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def test_separate_clones_of_one_repo_share_an_identity(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    first = tmp_path / "checkout-a"
    second = tmp_path / "checkout-b"
    for clone in (first, second):
        _git(tmp_path, "clone", str(origin), str(clone))
    # Directory identity says "two projects"; repo identity says one repo —
    # this is exactly the case --git-common-dir cannot unify.
    assert first != second
    assert task.git_repo_identity(first) == task.git_repo_identity(second)
    assert task.git_repo_identity(first) is not None


def test_a_worktree_shares_its_parents_identity(tmp_path: Path) -> None:
    origin = tmp_path / "origin2.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    main = tmp_path / "main-checkout"
    _git(tmp_path, "clone", str(origin), str(main))
    (main / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(main, "add", "seed.txt")
    _git(main, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-m", "seed")
    tree = tmp_path / "wt"
    _git(main, "worktree", "add", str(tree))
    assert task.git_repo_identity(tree) == task.git_repo_identity(main)


def test_a_checkout_without_an_origin_has_no_identity(tmp_path: Path) -> None:
    solo = tmp_path / "solo"
    solo.mkdir()
    _git(tmp_path, "init", "--initial-branch=main", str(solo))
    # Honest absence: callers fall back to path identity and must say so.
    assert task.git_repo_identity(solo) is None


def test_registry_caches_identity_so_consumers_never_shell_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = tmp_path / "origin3.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    checkout = tmp_path / "cached"
    _git(tmp_path, "clone", str(origin), str(checkout))
    entry = task._project_registry_entry(
        checkout, skill_version="test", now="2026-08-16T00:00:00+00:00"
    )
    assert entry["repo_identity"] == task.git_repo_identity(checkout)
    assert isinstance(entry["repo_identity"], str)


def _write_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, projects: list[dict]) -> Path:
    store = tmp_path / "state"
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(store))
    path = task.project_registry_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": task.PROJECT_REGISTRY_INDEX_SCHEMA,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "projects": projects,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _read_registry_projects() -> list[dict]:
    return task.read_project_registry()


def test_backfill_writes_identity_for_surviving_root_without_the_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = tmp_path / "origin-backfill.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    checkout = tmp_path / "surviving"
    _git(tmp_path, "clone", str(origin), str(checkout))
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(checkout),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            }
        ],
    )
    result = task.maintain_project_registry(now="2026-08-17T00:00:00+00:00")
    assert result["wrote"] is True
    assert result["backfilled"] == 1
    stored = _read_registry_projects()
    assert stored[0]["repo_identity"] == task.git_repo_identity(checkout)
    assert stored[0]["repo_identity"] is not None
    assert stored[0]["last_seen"] == "2026-01-01T00:00:00+00:00"


def test_backfill_does_not_guess_identity_from_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    solo = tmp_path / "looks-like-owner-name"
    solo.mkdir()
    _git(tmp_path, "init", "--initial-branch=main", str(solo))
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(solo),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            }
        ],
    )
    result = task.maintain_project_registry()
    stored = _read_registry_projects()[0]
    assert result["honest_none"] == 1
    assert stored["repo_identity"] is None
    assert stored["repo_identity"] != solo.name
    assert "looks-like-owner-name" not in str(stored["repo_identity"])


def test_backfill_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = tmp_path / "origin-idem.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    checkout = tmp_path / "idem"
    _git(tmp_path, "clone", str(origin), str(checkout))
    path = _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(checkout),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            }
        ],
    )
    first = task.maintain_project_registry(now="2026-08-17T00:00:00+00:00")
    after_first = path.read_text(encoding="utf-8")
    second = task.maintain_project_registry(now="2026-08-17T01:00:00+00:00")
    after_second = path.read_text(encoding="utf-8")
    assert first["wrote"] is True
    assert second["wrote"] is False
    assert second["unchanged"] is True
    assert second["backfilled"] == 0
    assert after_second == after_first


def test_unmounted_parent_is_not_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_parent = tmp_path / "volume-not-mounted" / "checkout"
    live = tmp_path / "still-here"
    live.mkdir()
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(missing_parent),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
            {
                "project_root": str(live),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
                "repo_identity": None,
            },
        ],
    )
    assert task.classify_registry_root(missing_parent) == "unreachable"
    result = task.maintain_project_registry(prune_ghosts=True)
    roots = {item["project_root"] for item in _read_registry_projects()}
    assert result["pruned"] == 0
    assert result["unreachable"] == 1
    assert str(missing_parent) in roots
    assert str(live) in roots


def test_deleted_checkout_is_pruned_when_parent_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "worktrees"
    parent.mkdir()
    (parent / "sibling-chunk").mkdir()
    ghost = parent / "deleted-chunk"
    live = tmp_path / "kept"
    live.mkdir()
    path = _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(ghost),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
            {
                "project_root": str(live),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
                "repo_identity": "github.com/example/kept",
            },
        ],
    )
    assert task.classify_registry_root(ghost) == "prunable"
    result = task.maintain_project_registry(
        prune_ghosts=True, now="2026-08-17T00:00:00+00:00"
    )
    stored = _read_registry_projects()
    assert result["pruned"] == 1
    assert result["backup"] is not None
    assert Path(result["backup"]).is_file()
    backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
    assert any(item.get("project_root") == str(ghost) for item in backup["projects"])
    assert [item["project_root"] for item in stored] == [str(live)]
    # The live row must not be rewritten just because a sibling was pruned.
    assert stored[0]["repo_identity"] == "github.com/example/kept"
    assert path.is_file()


def test_dangling_symlink_is_not_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "links"
    parent.mkdir()
    target = tmp_path / "unmounted-target" / "repo"
    link = parent / "maybe-live"
    link.symlink_to(target)
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(link),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            }
        ],
    )
    assert task.classify_registry_root(link) == "live"
    result = task.maintain_project_registry(prune_ghosts=True)
    assert result["pruned"] == 0
    assert _read_registry_projects()[0]["project_root"] == str(link)


def test_dry_run_prune_does_not_write_or_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "worktrees"
    parent.mkdir()
    (parent / "sibling-chunk").mkdir()
    ghost = parent / "gone"
    path = _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(ghost),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            }
        ],
    )
    before = path.read_text(encoding="utf-8")
    result = task.maintain_project_registry(prune_ghosts=True, dry_run=True)
    assert result["pruned"] == 1
    assert result["wrote"] is False
    assert result["backup"] is None
    assert path.read_text(encoding="utf-8") == before
    assert list(path.parent.glob("projects.json.bak-*")) == []


def test_existing_identity_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "named"
    checkout.mkdir()
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(checkout),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
                "repo_identity": "github.com/cached/already",
            }
        ],
    )
    result = task.maintain_project_registry()
    assert result["unchanged"] is True
    assert result["already_identified"] == 1
    assert _read_registry_projects()[0]["repo_identity"] == "github.com/cached/already"


def test_measure_registry_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "worktrees"
    parent.mkdir()
    (parent / "sibling-chunk").mkdir()
    ghost = parent / "gone"
    live = tmp_path / "live"
    live.mkdir()
    unmounted = tmp_path / "not-a-volume" / "repo"
    path = _write_registry(
        tmp_path,
        monkeypatch,
        [
            {"project_root": str(live), "repo_identity": "github.com/a/b"},
            {"project_root": str(ghost)},
            {"project_root": str(unmounted)},
        ],
    )
    before = path.read_text(encoding="utf-8")
    census = task.measure_project_registry()
    assert census["surviving"] == 1
    assert census["surviving_with_identity"] == 1
    assert census["surviving_without_identity"] == 0
    assert census["ghosts"] == 2
    assert census["prunable"] == 1
    assert census["unreachable"] == 1
    assert census["ambiguous"] == 0
    assert (
        census["prunable"] + census["unreachable"] + census["ambiguous"]
        == census["ghosts"]
    )
    assert path.read_text(encoding="utf-8") == before


def _first_existing_mount_container() -> Path | None:
    for raw in ("/Volumes", "/mnt", "/media", "/net"):
        path = Path(raw)
        try:
            os.lstat(path)
        except OSError:
            continue
        return path
    return None


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod 000 traversal")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory mode 000",
)
def test_eacces_on_existing_checkout_is_unreachable_not_prunable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout behind a mode-000 parent must not be pruned.

    Path.exists() swallows EACCES and returns False, which is the original
    bug: the classifier treated a still-present tree as a deleted checkout.
    This test chmods a real directory; it does not stub exists().
    """
    parent = tmp_path / "opaque"
    child = parent / "still-here"
    child.mkdir(parents=True)
    marker = child / "marker.txt"
    marker.write_text("keep\n", encoding="utf-8")
    live = tmp_path / "kept"
    live.mkdir()
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(child),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
            {
                "project_root": str(live),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
                "repo_identity": "github.com/example/kept",
            },
        ],
    )
    original_mode = parent.stat().st_mode
    os.chmod(parent, 0o000)
    try:
        try:
            os.lstat(child)
        except OSError as exc:
            assert exc.errno == errno.EACCES
        else:
            pytest.skip("filesystem does not enforce mode 000 on owner traversal")
        assert child.exists() is False
        assert task.classify_registry_root(child) == "unreachable"
        result = task.maintain_project_registry(prune_ghosts=True)
    finally:
        os.chmod(parent, original_mode)
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8") == "keep\n"
    roots = {item["project_root"] for item in _read_registry_projects()}
    assert result["pruned"] == 0
    assert result["unreachable"] == 1
    assert str(child) in roots
    assert str(live) in roots


def test_unmounted_volume_root_is_not_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The volume row itself must survive when only the parent container exists.

    classify(/Volumes/WorkSSD) was prunable because the parent exists and
    the root does not. That is also the shape of an unmounted disk.
    """
    container = _first_existing_mount_container()
    if container is None:
        pytest.skip("no conventional mount container present")
    volume = container / "gf-prune-safety-no-such-volume"
    assert not volume.exists()
    live = tmp_path / "kept"
    live.mkdir()
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(volume),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
            {
                "project_root": str(live),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
                "repo_identity": "github.com/example/kept",
            },
        ],
    )
    assert task.classify_registry_root(volume) == "ambiguous"
    assert task.classify_registry_root(volume / "repo") == "unreachable"
    result = task.maintain_project_registry(prune_ghosts=True)
    roots = {item["project_root"] for item in _read_registry_projects()}
    assert result["pruned"] == 0
    assert result["ambiguous"] == 1
    assert result["unreachable"] == 0
    assert str(volume) in roots
    assert str(live) in roots


def test_directory_named_volumes_is_not_itself_a_mount_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only conventional absolute mount containers refuse prune, not the name."""
    parent = tmp_path / "Volumes"
    parent.mkdir()
    (parent / "OtherDisk").mkdir()
    ghost = parent / "WorkSSD"
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(ghost),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            }
        ],
    )
    assert task.classify_registry_root(ghost) == "prunable"
    result = task.maintain_project_registry(prune_ghosts=True)
    assert result["pruned"] == 1
    assert result["ambiguous"] == 0
    assert _read_registry_projects() == []


def test_conventional_mount_parent_is_a_path_convention_not_a_name() -> None:
    assert task._is_conventional_mount_parent(Path("/Volumes")) is True
    assert task._is_conventional_mount_parent(Path("/mnt")) is True
    assert task._is_conventional_mount_parent(Path("/media")) is True
    assert task._is_conventional_mount_parent(Path("/net")) is True
    assert task._is_conventional_mount_parent(Path("/run/media/alice")) is True
    assert task._is_conventional_mount_parent(Path("/media/alice")) is True
    assert task._is_conventional_mount_parent(Path("/data")) is False
    assert task._is_conventional_mount_parent(Path("/opt/ssd")) is False
    assert task._is_conventional_mount_parent(Path("/run/media")) is False
    assert task._is_conventional_mount_parent(Path.home() / "mnt") is True
    assert task._is_conventional_mount_parent(Path.home() / "Volumes") is False


def test_home_mnt_volume_root_is_ambiguous() -> None:
    container = Path.home() / "mnt"
    try:
        os.lstat(container)
    except OSError:
        pytest.skip("$HOME/mnt is not present")
    volume = container / "gf-prune-safety-no-such-volume"
    if volume.exists() or volume.is_symlink():
        pytest.skip("unique test volume name unexpectedly exists")
    assert task.classify_registry_root(volume) == "ambiguous"
    assert task.classify_registry_root(volume / "repo") == "unreachable"


def test_empty_parent_of_missing_checkout_is_ambiguous_not_prunable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux umount leaves the mountpoint behind as an empty directory.

    /mnt/nas/goal-flight is gone; /mnt/nas exists and is empty. The parent
    is not a conventional container (/mnt is), so the container-child rule
    does not fire. Classifying that shape as prunable deletes every registry
    row on the volume.
    """
    mountpoint = tmp_path / "nas"
    mountpoint.mkdir()
    checkout = mountpoint / "goal-flight"
    live = tmp_path / "kept"
    live.mkdir()
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(checkout),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
            {
                "project_root": str(live),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
                "repo_identity": "github.com/example/kept",
            },
        ],
    )
    assert list(mountpoint.iterdir()) == []
    assert task.classify_registry_root(checkout) == "ambiguous"
    result = task.maintain_project_registry(prune_ghosts=True)
    roots = {item["project_root"] for item in _read_registry_projects()}
    assert result["pruned"] == 0
    assert result["ambiguous"] == 1
    assert str(checkout) in roots
    assert str(live) in roots


def test_deleted_checkout_is_pruned_when_parent_holds_other_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-parent must not block prune when the sibling tree is intact."""
    parent = tmp_path / "worktrees"
    parent.mkdir()
    (parent / "still-here").mkdir()
    ghost = parent / "deleted-chunk"
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(ghost),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            }
        ],
    )
    assert task.classify_registry_root(ghost) == "prunable"
    result = task.maintain_project_registry(prune_ghosts=True)
    assert result["pruned"] == 1
    assert result["ambiguous"] == 0
    assert _read_registry_projects() == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod 0100 listing")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory mode 0100",
)
def test_unlistable_parent_of_missing_checkout_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent we cannot list is not proof the checkout was deleted."""
    parent = tmp_path / "opaque-mount"
    parent.mkdir()
    ghost = parent / "maybe-unmounted"
    live = tmp_path / "kept"
    live.mkdir()
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": str(ghost),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
            {
                "project_root": str(live),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
        ],
    )
    original_mode = parent.stat().st_mode
    os.chmod(parent, 0o100)
    try:
        try:
            os.lstat(ghost)
        except OSError as exc:
            assert exc.errno == errno.ENOENT
        try:
            os.scandir(parent).close()
        except OSError as exc:
            assert exc.errno == errno.EACCES
        else:
            pytest.skip("filesystem does not deny listing on mode 0100")
        assert task.classify_registry_root(ghost) == "unreachable"
        result = task.maintain_project_registry(prune_ghosts=True)
    finally:
        os.chmod(parent, original_mode)
    roots = {item["project_root"] for item in _read_registry_projects()}
    assert result["pruned"] == 0
    assert result["unreachable"] == 1
    assert str(ghost) in roots
    assert str(live) in roots


def test_unknown_user_tilde_path_is_unreachable_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mystery = "~nosuchuser_gf_prune_xyz/repo"
    with pytest.raises(RuntimeError):
        Path(mystery).expanduser()
    assert task.classify_registry_root(mystery) == "unreachable"
    live = tmp_path / "kept"
    live.mkdir()
    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {
                "project_root": mystery,
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
            {
                "project_root": str(live),
                "last_seen": "2026-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
        ],
    )
    result = task.maintain_project_registry(prune_ghosts=True)
    roots = {item["project_root"] for item in _read_registry_projects()}
    assert result["pruned"] == 0
    assert result["unreachable"] == 1
    assert mystery in roots
    assert str(live) in roots


def main() -> None:
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    main()
