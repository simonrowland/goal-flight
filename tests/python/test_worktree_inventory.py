"""A worktree census must count each repository once, not once per worktree.

`git worktree list` reports the whole set from any member of that set. A scan
directory therefore contains both a repo and several of its own worktrees, and a
naive census counts the same 456 worktrees once for each member it happens to
walk past. The first run of this tool did exactly that: battery-tool-v2's 456
appeared three times, and the reported total was inflated by hundreds.

That matters because the total is the number a human uses to decide whether the
indexer fan-out is worth acting on, and an inflated one argues for action that
isn't needed while hiding which repo is actually responsible.
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

import goalflight_worktree_inventory as inv  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real git repo with one commit — worktrees need a commit to branch from."""
    root = tmp_path / "main-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("hello\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "init")
    return root


def test_repo_and_its_worktrees_are_counted_once(tmp_path: Path, repo: Path) -> None:
    """The regression: scanning a repo AND its worktrees must not multiply totals."""
    for name in ("wt-one", "wt-two"):
        _git(repo, "worktree", "add", "-q", "-b", name, str(tmp_path / name))

    # Every member of the set reports the same count...
    for member in (repo, tmp_path / "wt-one", tmp_path / "wt-two"):
        assert inv.survey_repo(member)["worktrees"] == 3

    # ...so they must share one identity and collapse to a single row.
    identities = {inv.repo_identity(p)
                  for p in (repo, tmp_path / "wt-one", tmp_path / "wt-two")}
    assert len(identities) == 1

    report = json.loads(_run_json(tmp_path))
    assert len(report["repos"]) == 1, report["repos"]
    assert report["total_worktrees"] == 3


def test_main_worktree_is_the_representative(tmp_path: Path, repo: Path) -> None:
    """Report the repo, not one of its worktrees, or the name misleads."""
    _git(repo, "worktree", "add", "-q", "-b", "side", str(tmp_path / "side"))
    report = json.loads(_run_json(tmp_path))
    row = report["repos"][0]
    assert row["name"] == "main-repo"
    assert "side" in row["aliases"]


def test_missing_worktree_directory_is_reported(tmp_path: Path, repo: Path) -> None:
    """A worktree whose directory is gone is dead weight git has not noticed."""
    _git(repo, "worktree", "add", "-q", "-b", "doomed", str(tmp_path / "doomed"))
    import shutil
    shutil.rmtree(tmp_path / "doomed")

    entry = inv.survey_repo(repo)
    assert entry["worktrees"] == 2
    assert entry["missing_on_disk"] == 1


def test_busy_threshold_flags_only_above_the_line(tmp_path: Path, repo: Path) -> None:
    _git(repo, "worktree", "add", "-q", "-b", "one", str(tmp_path / "one"))
    report = json.loads(_run_json(tmp_path, "--threshold", "2"))
    assert report["repos"][0]["busy"] is True
    assert report["busy_repos"] == ["main-repo"]

    report = json.loads(_run_json(tmp_path, "--threshold", "3"))
    assert report["repos"][0]["busy"] is False
    assert report["busy_repos"] == []


def test_non_repository_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "not-a-repo").mkdir()
    assert inv.survey_repo(tmp_path / "not-a-repo") is None
    assert inv.repo_identity(tmp_path / "not-a-repo") is None


def test_file_table_reports_a_usable_fraction() -> None:
    """Reads the real host; assert the shape and internal consistency only."""
    ft = inv.file_table()
    if not ft:
        pytest.skip("sysctl file-table counters unavailable on this host")
    assert 0 <= ft["fraction"] <= 1
    assert ft["open_files"] <= ft["max_files"]
    # The warn flag must agree with the fraction it was derived from, or the
    # alarm and the number a human reads can disagree.
    assert ft["warn"] == (ft["fraction"] >= inv.FILE_TABLE_WARN_FRACTION)


def _run_json(search: Path, *extra: str) -> str:
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_worktree_inventory.py"),
         "--search", str(search), "--json", *extra],
        capture_output=True, text=True, check=True,
    )
    return done.stdout


def test_warn_threshold_matches_its_derivation() -> None:
    """The threshold must fire at the state its own comment cites as the failure.

    An earlier version derived "at or below 52.7%" and then set 0.60, so the very
    incident it names would have been reported ok.
    """
    observed_failure = 259036 / 491520          # the measured incident, 52.7%
    assert inv.FILE_TABLE_WARN_FRACTION < observed_failure, (
        "threshold must warn at the level the derivation cites")
