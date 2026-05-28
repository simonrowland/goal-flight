#!/usr/bin/env python3
"""Hermetic regression: every tree reachable from HEAD must satisfy git's
canonical sort order.

Background (the 2026-05-28 push-rejected incident, commit 97b24e6 chunk-16):
worker amend cycles produced trees whose entries were sorted by pure
byte-by-byte name lex sort instead of git's spec, which treats directory
entries as if they had a trailing `/` for sort comparison. Examples:

  Pure lex sort:    docs  <  docs-private   (end-of-string < '-')
  Git tree sort:    docs-private  <  docs/  ('-' = 0x2D  <  '/' = 0x2F)

Local git is lenient and accepts both; GitHub's strict fsck rejects pushes
containing trees with the wrong sort, surfacing as `treeNotSorted`. The
incident cost a rebuild of three commits to land cleanly.

This test parses every tree reachable from HEAD via `git ls-tree -r -t`
and verifies the entry sort matches git's spec. Catches a recurrence at
`./tests/run.sh` time — before the push attempt fails on the remote.

Scope: only HEAD-reachable trees. Orphan trees in reflogs / stashes /
unreferenced packs are NOT checked here (they expire and never make it
to a push). The check is per-commit-history-integrity, not per-object-db.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


def _git(args: list[str]) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def _git_sort_key(name: str, is_tree: bool) -> bytes:
    """Git tree sort key: directory entry compares as `name + b"/"`.

    Files and submodules compare as their literal name. Trees (subdirs)
    behave as if a `/` byte (0x2F) were appended to the name.
    """
    if is_tree:
        return (name + "/").encode("utf-8")
    return name.encode("utf-8")


def _check_tree(tree_sha: str) -> list[str]:
    """Return a list of human-readable sort-violation messages for one
    tree, or [] if the entries are in canonical git order.
    """
    # `git cat-file -p` prints the tree entries in their stored order.
    # Lines: "<mode> <type> <sha>\t<name>"
    raw = subprocess.run(
        ["git", "cat-file", "-p", tree_sha],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    entries: list[tuple[str, bool]] = []
    for line in raw.splitlines():
        if not line:
            continue
        # Split on the tab; the field before is "<mode> <type> <sha>".
        meta, _, name = line.partition("\t")
        if not name:
            continue
        parts = meta.split()
        if len(parts) < 2:
            continue
        kind = parts[1]
        entries.append((name, kind == "tree"))
    violations: list[str] = []
    for i in range(1, len(entries)):
        prev_name, prev_tree = entries[i - 1]
        cur_name, cur_tree = entries[i]
        prev_key = _git_sort_key(prev_name, prev_tree)
        cur_key = _git_sort_key(cur_name, cur_tree)
        if prev_key >= cur_key:
            violations.append(
                f"  tree {tree_sha[:12]}: "
                f"{prev_name!r}{'/' if prev_tree else ''} "
                f"must come AFTER "
                f"{cur_name!r}{'/' if cur_tree else ''} per git tree sort"
            )
    return violations


def test_no_unsorted_trees_reachable_from_head() -> None:
    if not (ROOT / ".git").exists():
        # Not a git checkout (e.g. tarball install). Skip — there's nothing
        # to corrupt without a worktree history.
        print("SKIP: not a git repo")
        return
    # `git ls-tree -r -t HEAD` lists every tree reachable from HEAD's root
    # tree, recursively. Each line is "<mode> <type> <sha>\t<path>".
    raw = _git(["ls-tree", "-r", "-t", "HEAD"])
    tree_shas: set[str] = set()
    # Include the root tree explicitly — ls-tree -t lists subtrees but not
    # the top-level tree itself.
    tree_shas.add(_git(["rev-parse", "HEAD^{tree}"]).strip())
    for line in raw.splitlines():
        if not line:
            continue
        meta, _, _name = line.partition("\t")
        parts = meta.split()
        if len(parts) < 3:
            continue
        if parts[1] == "tree":
            tree_shas.add(parts[2])
    all_violations: list[str] = []
    for sha in sorted(tree_shas):
        all_violations.extend(_check_tree(sha))
    assert_true(
        "no HEAD-reachable trees have entries out of git-sort order:\n"
        + "\n".join(all_violations[:20])  # cap the diagnostic at 20 lines
        + (f"\n  ... and {len(all_violations) - 20} more" if len(all_violations) > 20 else ""),
        not all_violations,
    )


def main() -> int:
    tests = [test_no_unsorted_trees_reachable_from_head]
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as exc:
            print(f"FAIL {t.__name__}: {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001 — surface unexpected failures
            import traceback
            traceback.print_exc()
            print(f"FAIL {t.__name__}: {type(exc).__name__}: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
