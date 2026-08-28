#!/usr/bin/env python3
"""Read-only census of git worktrees and the indexer load they create.

Worktrees are cheap to make and invisible once made, so they accumulate silently.
Measured 2026-08-23 on one box: `battery-tool-v2` held 456 registered worktrees
with 605 MB of `.git/worktrees` admin metadata, and none of them were prunable —
git considered every one legitimate because the directories still existed.

The second-order cost is what actually broke things. A code indexer treats each
worktree as a separate project: the same box carried 428 codedb project indexes
totalling 122 GB, and the indexer's fan-out held ~221,000 open files — 95% of the
system file table (`kern.num_files` 259,036 of `kern.maxfiles` 491,520). At that
point opening a database intermittently fails with ENFILE, which is how three
wake components and another controller's listener all died inside one second
while their journals were provably intact (see t-308).

None of that was visible from inside any single repo. This tool makes it visible.

It is deliberately READ-ONLY. It reaps nothing and prunes nothing: deciding which
of 456 worktrees may die is a judgement call about someone's in-flight work, and
`goalflight_worktree_pool.py` already provides the bounded-seat discipline that
prevents the accumulation in the first place. This reports; you decide.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

# Why 8x the pool default: a repo honouring the pool plus a handful of
# long-lived side worktrees still lands well below that multiple, while genuine
# unbounded growth (observed 456) trips immediately. Reporting threshold only.
from goalflight_worktree_pool import DEFAULT_WORKTREE_SEATS  # noqa: E402

DEFAULT_BUSY_THRESHOLD = DEFAULT_WORKTREE_SEATS * 8

# Why 50%: ENFILE is raised against the whole-system table, so the useful alarm is
# a fraction of kern.maxfiles rather than an absolute count. The observed failure
# sat at 259,036 of 491,520 - 52.7% - and was still climbing when it began killing
# database opens, so the threshold must be BELOW that to have warned in time. An
# earlier version derived exactly that and then set 0.60, which would have
# reported the very state it cites as `ok`. 50% still leaves real headroom over an
# idle box (measured 1.7% with the indexer stopped).
FILE_TABLE_WARN_FRACTION = 0.50


def _git(root: Path, *args: str) -> str:
    """Run git in `root`, returning stdout; '' on any failure.

    Failure is not exceptional here: the census walks directories that may not be
    repositories at all, and one unreadable repo must not abort the whole census.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _dir_bytes(path: Path) -> int:
    """Total size of a directory tree, skipping anything unreadable."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def repo_identity(root: Path) -> str | None:
    """The shared git directory that all of a repo's worktrees point at.

    This is the deduplication key, and getting it wrong inflates every total.
    Scanning a directory of repositories also picks up the *worktrees* of those
    repositories, and `git worktree list` reports the whole set from any member —
    so a repo with 456 worktrees, three of which sit in the scan path, is counted
    456 three times over. Grouping by `--git-common-dir` collapses a worktree onto
    its parent so each set is counted exactly once.
    """
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    if not common:
        # Older git without --path-format: fall back to the relative answer
        # resolved against the repo root, which is what the flag would produce.
        rel = _git(root, "rev-parse", "--git-common-dir").strip()
        if not rel:
            return None
        common = str((root / rel).resolve()) if not os.path.isabs(rel) else rel
    try:
        return str(Path(common).resolve())
    except OSError:
        return common


def survey_repo(root: Path) -> dict | None:
    """Census one repository. Returns None if `root` is not a git repo."""
    porcelain = _git(root, "worktree", "list", "--porcelain")
    if not porcelain:
        return None

    paths: list[str] = []
    prunable = 0
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            paths.append(line[len("worktree "):].strip())
        elif line.startswith("prunable"):
            prunable += 1

    # A worktree whose directory is gone is dead weight git has not noticed yet;
    # it is a distinct condition from `prunable`, which git only sets once it has
    # looked. The observed 456 were all present on disk and none were prunable,
    # which is precisely why nothing flagged them.
    missing = [p for p in paths if not os.path.exists(p)]

    admin = root / ".git" / "worktrees"
    admin_bytes = _dir_bytes(admin) if admin.is_dir() else 0

    return {
        "root": str(root),
        "name": root.name,
        "worktrees": len(paths),
        "missing_on_disk": len(missing),
        "prunable": prunable,
        "admin_bytes": admin_bytes,
        "busy": len(paths) >= DEFAULT_BUSY_THRESHOLD,
    }


def file_table() -> dict:
    """System-wide open-file pressure, or {} where it cannot be read.

    Read via sysctl because the per-process view is the wrong one: ENFILE is a
    whole-system condition, and a per-process count looks healthy right up to the
    moment every open on the box starts failing.
    """
    def _sysctl(name: str) -> int | None:
        try:
            done = subprocess.run(
                ["sysctl", "-n", name], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        try:
            return int(done.stdout.strip())
        except ValueError:
            return None

    used, cap = _sysctl("kern.num_files"), _sysctl("kern.maxfiles")
    if used is None or not cap:
        return {}
    fraction = used / cap
    return {
        "open_files": used,
        "max_files": cap,
        "fraction": round(fraction, 4),
        "warn": fraction >= FILE_TABLE_WARN_FRACTION,
    }


def indexer_projects() -> dict:
    """How many separate project indexes the code indexer is carrying."""
    store = Path.home() / ".codedb" / "projects"
    if not store.is_dir():
        return {}
    try:
        count = sum(1 for _ in store.iterdir())
    except OSError:
        return {}
    return {"store": str(store), "projects": count}


def discover(search_roots: list[Path]) -> list[Path]:
    """Find candidate repositories one level below each search root."""
    found: list[Path] = []
    for base in search_roots:
        if not base.is_dir():
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for child in entries:
            if child.is_dir() and (child / ".git").exists():
                found.append(child)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only census of git worktrees and indexer load.")
    parser.add_argument(
        "roots", nargs="*", type=Path,
        help="Repositories to census. Default: repos found under --search.")
    parser.add_argument(
        "--search", type=Path, action="append", default=None,
        help="Directory to scan for repositories (repeatable).")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument(
        "--threshold", type=int, default=DEFAULT_BUSY_THRESHOLD,
        help=f"Flag repos at or above this worktree count (default {DEFAULT_BUSY_THRESHOLD}).")
    args = parser.parse_args(argv)

    if args.roots:
        candidates = list(args.roots)
    else:
        bases = args.search or [Path.home() / "Repos"]
        candidates = discover(bases)

    # Group by shared git dir so a repo and its own worktrees count once, not
    # once per worktree that happens to sit inside the scan path.
    by_identity: dict[str, dict] = {}
    for root in candidates:
        entry = survey_repo(root)
        if entry is None:
            continue
        entry["busy"] = entry["worktrees"] >= args.threshold
        key = repo_identity(root) or str(root)
        seen = by_identity.get(key)
        if seen is None:
            entry["aliases"] = []
            by_identity[key] = entry
            continue
        # Prefer the main worktree as the representative: its .git is a real
        # directory, a linked worktree's .git is a file pointing back at it.
        seen["aliases"].append(entry["name"])
        if (root / ".git").is_dir() and not Path(seen["root"], ".git").is_dir():
            entry["aliases"] = seen["aliases"]
            entry["admin_bytes"] = max(entry["admin_bytes"], seen["admin_bytes"])
            by_identity[key] = entry

    repos = sorted(by_identity.values(), key=lambda r: r["worktrees"], reverse=True)

    report = {
        "repos": repos,
        "total_worktrees": sum(r["worktrees"] for r in repos),
        "total_admin_bytes": sum(r["admin_bytes"] for r in repos),
        "busy_repos": [r["name"] for r in repos if r["busy"]],
        "file_table": file_table(),
        "indexer": indexer_projects(),
        "threshold": args.threshold,
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if not repos:
        print("no git repositories found")
        return 0

    print(f"{'REPO':<28}{'WORKTREES':>10}{'MISSING':>9}{'PRUNABLE':>10}{'ADMIN':>10}")
    for r in repos:
        mark = "  <-- busy" if r["busy"] else ""
        admin = f"{r['admin_bytes'] / 1048576:.0f}M" if r["admin_bytes"] else "-"
        print(f"{r['name'][:27]:<28}{r['worktrees']:>10}{r['missing_on_disk']:>9}"
              f"{r['prunable']:>10}{admin:>10}{mark}")

    print(f"\ntotal worktrees: {report['total_worktrees']}"
          f"   admin metadata: {report['total_admin_bytes'] / 1048576:.0f}M")

    idx = report["indexer"]
    if idx:
        print(f"indexer project indexes: {idx['projects']}")

    ft = report["file_table"]
    if ft:
        state = "WARN" if ft["warn"] else "ok"
        print(f"system file table: {ft['open_files']} / {ft['max_files']} "
              f"({ft['fraction'] * 100:.1f}%) {state}")
        if ft["warn"]:
            print("  file-table pressure high: database opens can fail with ENFILE "
                  "while the files themselves are perfectly healthy (see t-308).")

    if report["busy_repos"]:
        print(f"\nbusy repos (>= {args.threshold} worktrees): "
              f"{', '.join(report['busy_repos'])}")
        print("  goalflight_worktree_pool.py leases a bounded pool of reusable "
              "seats; unbounded per-dispatch worktrees are what accumulate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
