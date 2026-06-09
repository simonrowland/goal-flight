#!/usr/bin/env python3
"""Tests for stale/corrupt claude dispatch ref cleanup."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_cleanup_dispatch_refs as cleanup


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def git_ok(repo: Path, *args: str) -> str:
    proc = git(repo, *args)
    if proc.returncode != 0:
        raise AssertionError((proc.stderr or proc.stdout).strip())
    return proc.stdout.strip()


def test_prune_stale_and_corrupt_claude_refs_preserves_checked_out_branch() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        git_ok(repo, "init", "-q")
        git_ok(repo, "config", "user.email", "test@example.com")
        git_ok(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("x\n")
        git_ok(repo, "add", "README.md")
        git_ok(repo, "commit", "-q", "-m", "init")
        git_ok(repo, "checkout", "-q", "-b", "claude/active")
        git_ok(repo, "branch", "claude/stale")

        common = Path(git_ok(repo, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = repo / common
        corrupt = common / "refs" / "heads" / "claude" / "corrupt"
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("ffffffffffffffffffffffffffffffffffffffff\n")

        result = cleanup.prune_dispatch_refs(repo)
        deleted = {row["ref"]: row["reason"] for row in result["deleted"]}
        kept = {row["ref"]: row["reason"] for row in result["kept"]}

        assert_true("active branch protected", kept["refs/heads/claude/active"] == "checked_out_worktree")
        assert_true("stale deleted", deleted["refs/heads/claude/stale"] == "stale_no_remote")
        assert_true("corrupt deleted", deleted["refs/heads/claude/corrupt"] == "corrupt")
        assert_true("no cleanup errors", result["errors"] == [])
        assert_true("active ref remains", git(repo, "rev-parse", "--verify", "refs/heads/claude/active").returncode == 0)
        assert_true("stale ref gone", git(repo, "rev-parse", "--verify", "refs/heads/claude/stale").returncode != 0)
        assert_true("corrupt loose ref gone", not corrupt.exists())


def test_prune_aborts_when_worktree_branches_cannot_be_determined() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        git_ok(repo, "init", "-q")
        git_ok(repo, "config", "user.email", "test@example.com")
        git_ok(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("x\n")
        git_ok(repo, "add", "README.md")
        git_ok(repo, "commit", "-q", "-m", "init")
        git_ok(repo, "branch", "claude/stale")

        real_git = cleanup._git

        def fail_worktree_list(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args == ["worktree", "list", "--porcelain"]:
                return subprocess.CompletedProcess(
                    ["git", "worktree", "list", "--porcelain"],
                    128,
                    "",
                    "worktree list unavailable",
                )
            return real_git(repo_root, args)

        cleanup._git = fail_worktree_list
        try:
            result = cleanup.prune_dispatch_refs(repo)
        finally:
            cleanup._git = real_git

        assert_true("prune aborted", result.get("aborted") is True)
        assert_true("deleted nothing", result["deleted"] == [])
        assert_true("clear error", result["errors"][0]["reason"] == "protected_branches_unavailable")
        assert_true("stale ref remains", git(repo, "rev-parse", "--verify", "refs/heads/claude/stale").returncode == 0)


def main() -> None:
    test_prune_stale_and_corrupt_claude_refs_preserves_checked_out_branch()
    test_prune_aborts_when_worktree_branches_cannot_be_determined()
    print("OK: cleanup dispatch refs tests pass")


if __name__ == "__main__":
    main()
