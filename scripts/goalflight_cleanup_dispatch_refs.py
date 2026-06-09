#!/usr/bin/env python3
"""Prune stale/corrupt local dispatch refs before fleet git fetch."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


REF_PREFIX = "refs/heads/claude/"


def _git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _common_dir(repo_root: Path) -> Path:
    proc = _git(repo_root, ["rev-parse", "--git-common-dir"])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "not a git repository")
    raw = proc.stdout.strip()
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _worktree_branches(repo_root: Path) -> tuple[set[str], str | None]:
    proc = _git(repo_root, ["worktree", "list", "--porcelain"])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "git worktree list --porcelain failed"
        return set(), detail
    branches: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("branch "):
            branches.add(line.split(" ", 1)[1].strip())
    return branches, None


def _loose_claude_refs(repo_root: Path) -> set[str]:
    root = _common_dir(repo_root) / "refs" / "heads" / "claude"
    if not root.exists():
        return set()
    refs: set[str] = set()
    for path in root.rglob("*"):
        if path.is_file():
            refs.add(REF_PREFIX + str(path.relative_to(root)).replace("\\", "/"))
    return refs


def _for_each_claude_refs(repo_root: Path) -> set[str]:
    proc = _git(repo_root, ["for-each-ref", "--format=%(refname)", "refs/heads/claude"])
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip().startswith(REF_PREFIX)}


def _remote_refs(repo_root: Path) -> set[str]:
    proc = _git(repo_root, ["for-each-ref", "--format=%(refname)", "refs/remotes"])
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _ref_is_valid_commit(repo_root: Path, ref: str) -> bool:
    proc = _git(repo_root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return proc.returncode == 0


def _has_matching_remote(local_ref: str, remote_refs: set[str]) -> bool:
    branch = local_ref.removeprefix("refs/heads/")
    return any(ref.endswith(f"/{branch}") for ref in remote_refs)


def _delete_ref(repo_root: Path, ref: str) -> tuple[bool, str]:
    proc = _git(repo_root, ["update-ref", "-d", ref])
    if proc.returncode == 0:
        return True, ""

    loose = _common_dir(repo_root) / "refs" / ref.removeprefix("refs/")
    if loose.exists():
        try:
            loose.unlink()
            return True, "removed loose corrupt ref"
        except OSError as exc:
            return False, str(exc)
    return False, (proc.stderr or proc.stdout).strip()


def prune_dispatch_refs(repo_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    candidates = sorted(_for_each_claude_refs(repo_root) | _loose_claude_refs(repo_root))
    protected, protected_error = _worktree_branches(repo_root)
    remotes = _remote_refs(repo_root)
    deleted: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    if protected_error:
        errors.append(
            {
                "ref": "<worktree-branches>",
                "reason": "protected_branches_unavailable",
                "error": protected_error,
            }
        )
        return {
            "repo_root": str(repo_root),
            "candidate_count": len(candidates),
            "deleted": deleted,
            "kept": kept,
            "errors": errors,
            "aborted": True,
        }

    for ref in candidates:
        if ref in protected:
            kept.append({"ref": ref, "reason": "checked_out_worktree"})
            continue
        valid = _ref_is_valid_commit(repo_root, ref)
        reason = "corrupt" if not valid else "stale_no_remote"
        if valid and _has_matching_remote(ref, remotes):
            kept.append({"ref": ref, "reason": "has_remote"})
            continue
        if dry_run:
            deleted.append({"ref": ref, "reason": reason, "dry_run": "true"})
            continue
        ok, detail = _delete_ref(repo_root, ref)
        if ok:
            row = {"ref": ref, "reason": reason}
            if detail:
                row["detail"] = detail
            deleted.append(row)
        else:
            errors.append({"ref": ref, "reason": reason, "error": detail})

    return {
        "repo_root": str(repo_root),
        "candidate_count": len(candidates),
        "deleted": deleted,
        "kept": kept,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune stale/corrupt claude dispatch refs")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = prune_dispatch_refs(args.repo_root, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            f"claude refs: candidates{result['candidate_count']} "
            f"deleted{len(result['deleted'])} errors{len(result['errors'])}"
        )
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
