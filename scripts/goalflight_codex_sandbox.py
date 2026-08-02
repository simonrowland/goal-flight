#!/usr/bin/env python3
"""Narrow Codex workspace-write grants for linked Git worktrees."""

from __future__ import annotations

import json
import os
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


def worker_channel_roots() -> list[str]:
    """Paths a sandboxed worker needs beyond its workspace.

    Two things were impossible without these, and BOTH were reported as
    mysterious "Operation not permitted" failures:

    - Posting mail. The worker's only channel was a marker scraped from its
      console log; writing an envelope failed on the messages lock file. That
      is why controllers invented side-channel files.
    - Running its own MANDATORY independent review. Nested codex could not
      initialize its app-server, so three workers correctly refused to commit
      and escalated BLOCKED, and their work sat staged for hours.

    The trap this closes: for a codex worker, --os-sandbox maps to CODEX'S OWN
    --sandbox, not to goal-flight's seatbelt profile, so grants added to
    goalflight_os_sandbox never applied here at all. The dispatch record shows
    os_sandbox={profile: None}, which reads as "no sandbox" when in fact codex
    is enforcing its own.

    Scope stays narrow: the messages tree and the codex dispatch homes. The
    fleet directory holds the registry and derived aggregate -- state a worker
    consumes but must never author -- and is deliberately absent.
    """
    home = Path.home()
    roots = [
        home / ".goal-flight" / "messages",
        home / ".goal-flight" / "dispatch-homes",
    ]
    configured_home = os.environ.get("CODEX_HOME")
    if configured_home:
        roots.append(Path(configured_home))
    seen: set[str] = set()
    out: list[str] = []
    for root in roots:
        text = str(root.expanduser())
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def codex_workspace_write_args(cwd: str | Path | None, profile: str | None) -> list[str]:
    """Build Codex config argv for a linked-worktree workspace-write sandbox."""
    if profile != WORKSPACE_WRITE:
        return []
    # The cwd guard governs WORKTREE roots only. Channel roots do not depend on
    # cwd at all, and gating them behind it silently granted nothing whenever
    # --cwd was omitted -- which is the default. A worker then failed to post
    # mail AND to start its own review, both reported as bare "Operation not
    # permitted", with no hint that the cause was an argument nobody passed.
    roots = list(linked_worktree_writable_roots(cwd)) if cwd else []
    roots += worker_channel_roots()
    if not roots:
        return []
    value = json.dumps(roots, separators=(",", ":"))
    return ["-c", f"sandbox_workspace_write.writable_roots={value}"]
