#!/usr/bin/env python3
"""Safe goal-flight source auto-update helper.

Default path is fetch + fast-forward-only merge when the local checkout is clean
and unambiguously behind. Divergence is skipped; explicit --rebase is the
operator-invoked path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat  # noqa: E402

SCHEMA = "goalflight.autoupdate.v1"


def _git_prefix() -> list[str]:
    if goalflight_compat.is_windows():
        return ["git", "-c", "core.autocrlf=false"]
    return ["git"]


def run_git(repo: Path, args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_git_prefix(), *args],
        cwd=str(repo),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _first_line(text: str | None) -> str:
    return ((text or "").strip().splitlines() or [""])[0]


def _cache_path(repo: Path, remote: str, branch: str) -> Path:
    key = hashlib.sha256(f"{repo.resolve()}:{remote}:{branch}".encode("utf-8")).hexdigest()[:16]
    return goalflight_compat.default_state_dir() / "autoupdate" / f"{key}.json"


def _session_key() -> str:
    return (
        os.environ.get("GOALFLIGHT_AUTOUPDATE_SESSION")
        or os.environ.get("GOALFLIGHT_SESSION_ID")
        or os.environ.get("TERM_SESSION_ID")
        or f"ppid-{os.getppid()}"
    )


def _head(repo: Path, ref: str = "HEAD") -> str | None:
    result = run_git(repo, ["rev-parse", "--verify", ref], timeout=10)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _cache_hit(repo: Path, remote: str, branch: str, *, force: bool) -> dict[str, Any] | None:
    if force or not goalflight_compat.is_windows():
        return None
    path = _cache_path(repo, remote, branch)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if cached.get("session_key") != _session_key():
        return None
    if cached.get("head") != _head(repo):
        return None
    return {
        "schema": SCHEMA,
        "state": "skipped_cached",
        "reason": "autoupdate already checked this checkout in this session",
        "repo": str(repo),
        "remote": remote,
        "branch": branch,
        "head": cached.get("head"),
        "cache_path": str(path),
    }


def _write_cache(repo: Path, remote: str, branch: str, payload: dict[str, Any]) -> None:
    if not goalflight_compat.is_windows():
        return
    path = _cache_path(repo, remote, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": SCHEMA,
        "session_key": _session_key(),
        "repo": str(repo),
        "remote": remote,
        "branch": branch,
        "head": _head(repo),
        "state": payload.get("state"),
        "checked_at": int(time.time()),
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["cache_path"] = str(path)


def _payload(repo: Path, state: str, **extra: Any) -> dict[str, Any]:
    out = {
        "schema": SCHEMA,
        "state": state,
        "repo": str(repo),
        "platform": {
            "is_windows": goalflight_compat.is_windows(),
            "git_crlf_guard": goalflight_compat.is_windows(),
        },
    }
    out.update(extra)
    return out


def update(repo: Path, *, remote: str, branch: str, rebase: bool, force: bool) -> dict[str, Any]:
    repo = repo.resolve()
    inside = run_git(repo, ["rev-parse", "--is-inside-work-tree"], timeout=10)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return _payload(repo, "skipped_non_git", reason=_first_line(inside.stderr or inside.stdout))

    cached = _cache_hit(repo, remote, branch, force=force)
    if cached is not None:
        return cached

    before = _head(repo)
    dirty = run_git(repo, ["status", "--porcelain", "--untracked-files=all"], timeout=10)
    if dirty.returncode != 0:
        return _payload(repo, "blocked_status_failed", head=before, error=_first_line(dirty.stderr or dirty.stdout))
    if dirty.stdout.strip():
        return _payload(
            repo,
            "skipped_dirty",
            head=before,
            reason="working tree has uncommitted changes",
            dirty_lines=dirty.stdout.strip().splitlines()[:20],
        )

    if rebase:
        pull = run_git(repo, ["pull", "--rebase", remote, branch], timeout=180)
        state = "updated_rebase" if pull.returncode == 0 else "blocked_rebase"
        payload = _payload(
            repo,
            state,
            head_before=before,
            head_after=_head(repo),
            stdout=_first_line(pull.stdout),
            stderr=_first_line(pull.stderr),
            returncode=pull.returncode,
        )
        _write_cache(repo, remote, branch, payload)
        return payload

    fetch = run_git(repo, ["fetch", remote, branch], timeout=120)
    if fetch.returncode != 0:
        payload = _payload(repo, "blocked_fetch", head=before, error=_first_line(fetch.stderr or fetch.stdout))
        _write_cache(repo, remote, branch, payload)
        return payload

    upstream = f"{remote}/{branch}"
    upstream_head = _head(repo, upstream)
    if not before or not upstream_head:
        payload = _payload(repo, "blocked_ref_resolution", head=before, upstream=upstream, upstream_head=upstream_head)
        _write_cache(repo, remote, branch, payload)
        return payload
    if before == upstream_head:
        payload = _payload(repo, "unchanged", head=before, upstream=upstream)
        _write_cache(repo, remote, branch, payload)
        return payload

    local_behind = run_git(repo, ["merge-base", "--is-ancestor", "HEAD", upstream], timeout=10)
    if local_behind.returncode == 0:
        merge = run_git(repo, ["merge", "--ff-only", upstream], timeout=120)
        payload = _payload(
            repo,
            "updated_ff" if merge.returncode == 0 else "blocked_ff",
            head_before=before,
            head_after=_head(repo),
            upstream=upstream,
            stdout=_first_line(merge.stdout),
            stderr=_first_line(merge.stderr),
            returncode=merge.returncode,
        )
        _write_cache(repo, remote, branch, payload)
        return payload

    remote_behind = run_git(repo, ["merge-base", "--is-ancestor", upstream, "HEAD"], timeout=10)
    state = "skipped_ahead" if remote_behind.returncode == 0 else "skipped_diverged"
    payload = _payload(
        repo,
        state,
        head=before,
        upstream=upstream,
        upstream_head=upstream_head,
        reason="automatic update is fast-forward-only; use --rebase deliberately",
    )
    _write_cache(repo, remote, branch, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="safe goal-flight auto-update helper")
    parser.add_argument("--repo", default=str(SCRIPT_DIR.parent))
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--rebase", action="store_true", help="operator-invoked rebase path")
    parser.add_argument("--force", action="store_true", help="bypass once-per-session Windows cache")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    payload = update(
        Path(args.repo),
        remote=args.remote,
        branch=args.branch,
        rebase=args.rebase,
        force=args.force,
    )
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        detail = payload.get("reason") or payload.get("error") or payload.get("stdout") or ""
        print(f"STATUS: autoupdate {payload['state']} {detail}".rstrip())
    return 1 if str(payload.get("state", "")).startswith("blocked_") else 0


if __name__ == "__main__":
    raise SystemExit(main())
