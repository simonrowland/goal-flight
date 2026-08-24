#!/usr/bin/env python3
"""Reclaim code-indexer project stores whose source tree no longer exists.

The indexer keys its store by root path, so every git worktree becomes a separate
project. Worktrees are created and destroyed constantly; the stores they leave
behind are not. Measured 2026-08-23 on one box: 446 project stores totalling
122.6 GB, of which **352 stores holding 106.6 GB pointed at roots that no longer
existed** — 87% of the store was indexing directories that had been deleted.

Dry-run by default. `--apply` is required to delete anything.

An orphan is a store whose `project.txt` names a root that is absent from the
filesystem *right now*. That test is deliberately narrow:

  - It never removes a store whose root exists, however stale the index.
  - It never removes a store whose `project.txt` is missing or unreadable, because
    then the root is unknown and absence cannot be established.
  - It refuses roots on paths that may be temporarily absent rather than deleted
    (network/removable mounts), since "not mounted" is not "deleted".

The index is not a backup. It is derived data the indexer rebuilds on demand — but
when the source tree is gone the index is the last artifact of it, so deleting one
is irreversible in practice. That is why this defaults to reporting.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

# Roots under these prefixes can be absent because a volume is unmounted rather
# than because the tree was deleted. Absence there proves nothing, so skip them.
_UNSTABLE_PREFIXES = ("/Volumes/", "/net/", "/mnt/", "/media/")


def store_root() -> Path:
    return Path(os.environ.get("CODEDB_HOME") or (Path.home() / ".codedb")) / "projects"


def dir_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def classify(project_dir: Path) -> dict:
    """Classify one project store. Absence of proof is never proof of absence."""
    marker = project_dir / "project.txt"
    entry = {"store": str(project_dir), "root": None, "verdict": "keep", "why": ""}

    if not marker.exists():
        entry["why"] = "no project.txt - root unknown, so absence is unverifiable"
        return entry
    try:
        lines = marker.read_text(errors="replace").splitlines()
    except OSError as exc:
        entry["why"] = f"project.txt unreadable ({exc.__class__.__name__})"
        return entry
    root = lines[0].strip() if lines else ""
    entry["root"] = root
    if not root:
        entry["why"] = "project.txt empty - root unknown"
        return entry
    if root.startswith(_UNSTABLE_PREFIXES):
        entry["why"] = "root on a mount that may be detached rather than deleted"
        return entry
    # os.path.exists() answers False for BOTH "absent" and "I could not look" -
    # a root behind an ancestor we lack search permission on reads as missing,
    # and this tool deletes on that answer. stat() separates the two: only
    # FileNotFoundError (or a broken symlink) is evidence of absence; any other
    # OSError means the question is unanswered, and unanswered is not permission.
    try:
        os.stat(root)
    except FileNotFoundError:
        entry["verdict"] = "orphan"
        entry["why"] = "root no longer exists"
        return entry
    except OSError as exc:
        entry["why"] = (
            f"root could not be checked ({exc.__class__.__name__}), "
            "so absence is unverified"
        )
        return entry
    entry["why"] = "root still exists"
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report (or delete) indexer project stores whose root is gone.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete orphaned stores. Default is dry-run.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Delete at most N orphans (0 = no limit).")
    args = parser.parse_args(argv)

    store = store_root()
    if not store.is_dir():
        print(f"no indexer store at {store}")
        return 0

    orphans, kept = [], []
    for child in sorted(store.iterdir()):
        if not child.is_dir():
            continue
        entry = classify(child)
        (orphans if entry["verdict"] == "orphan" else kept).append(entry)

    for entry in orphans:
        entry["bytes"] = dir_bytes(Path(entry["store"]))
    reclaim = sum(e["bytes"] for e in orphans)

    deleted, failed = 0, []
    if args.apply:
        # A negative limit silently becomes a slice index: --limit -1 would
        # delete all but one instead of at most one. On a tool that deletes
        # irreversibly, a misread flag must refuse rather than guess.
        if args.limit < 0:
            print("--limit must be >= 0 (0 means no limit)", file=sys.stderr)
            return 1
        targets = orphans[: args.limit] if args.limit else orphans
        for entry in targets:
            # Re-verify immediately before deleting: the listing above is a
            # snapshot, and a root that reappeared must not be reaped.
            if classify(Path(entry["store"]))["verdict"] != "orphan":
                failed.append({"store": entry["store"], "error": "no longer an orphan"})
                continue
            try:
                shutil.rmtree(entry["store"])
                deleted += 1
            except OSError as exc:
                failed.append({"store": entry["store"], "error": str(exc)})

    report = {
        "store": str(store),
        "orphans": len(orphans),
        "kept": len(kept),
        "reclaimable_bytes": reclaim,
        "applied": bool(args.apply),
        "deleted": deleted,
        "failed": failed,
    }
    if args.json:
        report["orphan_detail"] = orphans
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"indexer store: {store}")
    print(f"  stores with a live root : {len(kept)}")
    print(f"  ORPHANED (root is gone) : {len(orphans)}")
    print(f"  reclaimable             : {reclaim / 2**30:.1f} GB")
    if orphans:
        print("\n  largest orphans:")
        for e in sorted(orphans, key=lambda x: -x["bytes"])[:8]:
            print(f"    {e['bytes'] / 2**20:8.1f} MB  {e['root'][:78]}")
    if args.apply:
        print(f"\n  deleted {deleted} store(s)")
        for f in failed:
            print(f"    FAILED {f['store']}: {f['error']}")
    elif orphans:
        print("\n  dry run - nothing deleted. Re-run with --apply to reclaim.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
