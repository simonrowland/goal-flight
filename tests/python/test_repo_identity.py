"""A GitHub repo is one repo however it reaches the disk.

Directory identity cannot see that: separate clones of one repo look like
peers, which is what filled the fleet console with 23 'projects' for ~6
repos. Identity therefore comes from the normalized origin remote, resolved
once at registry-write time and cached as a scalar.
"""

from __future__ import annotations

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


def main() -> None:
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    main()
