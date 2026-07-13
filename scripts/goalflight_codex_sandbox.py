#!/usr/bin/env python3
"""Narrow Codex workspace-write grants for linked Git worktrees."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess


WORKSPACE_WRITE = "workspace-write"


def _git_path(cwd: Path, flag: str) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", flag],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def linked_worktree_git_dirs(cwd: str | Path) -> tuple[Path, Path] | None:
    """Return absolute (private git-dir, common-dir) for a linked worktree."""
    root = Path(cwd).expanduser().resolve()
    git_dir = _git_path(root, "--git-dir")
    common_dir = _git_path(root, "--git-common-dir")
    if git_dir is None or common_dir is None or git_dir == common_dir:
        return None
    return git_dir, common_dir


def linked_worktree_writable_roots(cwd: str | Path) -> list[str]:
    """Return only Git paths written by a normal linked-worktree commit.

    The private git-dir owns the worktree index, HEAD, and HEAD reflog. A
    branch commit additionally writes new objects, a loose local-branch ref,
    and its shared reflog in the common dir. ``packed-refs`` is read-only in
    this operation: updating a packed branch creates/updates its loose ref.
    """
    dirs = linked_worktree_git_dirs(cwd)
    if dirs is None:
        return []
    git_dir, common_dir = dirs
    common_roots = [
        (common_dir / "objects").resolve(),
        (common_dir / "refs" / "heads").resolve(),
        (common_dir / "logs" / "refs" / "heads").resolve(),
    ]
    if any(path != common_dir and common_dir not in path.parents for path in common_roots):
        return []
    return [str(git_dir), *(str(path) for path in common_roots)]


def codex_workspace_write_args(cwd: str | Path | None, profile: str | None) -> list[str]:
    """Build Codex config argv for a linked-worktree workspace-write sandbox."""
    if not cwd or profile != WORKSPACE_WRITE:
        return []
    roots = linked_worktree_writable_roots(cwd)
    if not roots:
        return []
    value = json.dumps(roots, separators=(",", ":"))
    return ["-c", f"sandbox_workspace_write.writable_roots={value}"]
