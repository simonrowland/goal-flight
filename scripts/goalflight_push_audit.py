#!/usr/bin/env python3
"""Audit remote branch advancement against local push and commit reflogs.

Detects the unauthorized-origin-advance class: origin moved to a SHA with no
corresponding local push reflog entry (e.g. a push from another worktree or
machine, or a commit constructed + pushed via an out-of-band API such as the
GitHub Git Data API). Read-only: runs only cat-file / ls-remote / rev-parse /
show + reflog reads, never pushes or mutates. The `git push -f` in ADVICE is a
printed suggestion string, never executed.

Limitation — do not over-trust `aligned`: a rogue push from the controller's
OWN clone writes a normal push reflog entry, so it reads as `aligned` here.
That same-clone case is covered by the commit guard + the worker-escalate-not-
bypass discipline, not by this reflog-based audit. `aligned` means "no remote
advance lacks a local push reflog entry", not "every advance was authorized".
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import subprocess
import sys
from typing import Any


ADVICE = "Investigate worker activity; consider git push -f to revert if state is unauthorized."


@dataclass(frozen=True)
class ReflogEntry:
    ref: str
    sha: str
    selector: str
    subject: str

    @property
    def date_estimate(self) -> str | None:
        marker = "@{"
        if marker not in self.selector or not self.selector.endswith("}"):
            return None
        body = self.selector.split(marker, 1)[1][:-1]
        return body if body and not body.isdigit() else None


class GitError(RuntimeError):
    def __init__(self, args: list[str], proc: subprocess.CompletedProcess[str]) -> None:
        self.args_list = args
        self.returncode = proc.returncode
        self.stdout = proc.stdout.strip()
        self.stderr = proc.stderr.strip()
        super().__init__(self.stderr or self.stdout or f"git exited {proc.returncode}")

    def to_json(self) -> dict[str, Any]:
        return {
            "command": ["git", *self.args_list],
            "returncode": self.returncode,
            "stderr": self.stderr[:500],
            "stdout": self.stdout[:500],
        }


def run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(args, proc)
    return proc


def empty_payload(remote: str, branch: str) -> dict[str, Any]:
    return {
        "remote": remote,
        "branch": branch,
        "remote_sha": None,
        "local_branch_sha": None,
        "aligned": True,
        "unauthorized_advances": [],
        "out_of_band_commits": [],
        "warning": None,
    }


def local_branch_sha(branch: str) -> str | None:
    proc = run_git(["rev-parse", "--verify", f"{branch}^{{commit}}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def remote_branch_sha(remote: str, branch: str) -> str | None:
    ref = f"refs/heads/{branch}"
    proc = run_git(["ls-remote", remote, ref], check=False)
    if proc.returncode != 0:
        raise GitError(["ls-remote", remote, ref], proc)
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == ref:
            return parts[0]
    return None


def reflog_entries(ref: str, *, limit: int = 200) -> list[ReflogEntry]:
    proc = run_git(
        [
            "reflog",
            "show",
            "--date=iso-strict",
            f"--max-count={limit}",
            "--format=%H%x00%gD%x00%gs",
            ref,
        ],
        check=False,
    )
    if proc.returncode != 0:
        return []

    rows: list[ReflogEntry] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\0")
        if len(parts) != 3:
            continue
        sha, selector, subject = parts
        if sha:
            rows.append(ReflogEntry(ref=ref, sha=sha, selector=selector, subject=subject))
    return rows


def unique_refs(refs: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for ref in refs:
        if ref and ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return ordered


def relevant_reflogs(remote: str, branch: str) -> tuple[list[ReflogEntry], list[ReflogEntry]]:
    branch_refs = unique_refs([branch, f"refs/heads/{branch}"])
    push_refs = unique_refs(
        [
            branch,
            f"refs/heads/{branch}",
            f"{remote}/{branch}",
            f"refs/remotes/{remote}/{branch}",
        ]
    )

    branch_entries: list[ReflogEntry] = []
    push_entries: list[ReflogEntry] = []
    for ref in branch_refs:
        branch_entries.extend(reflog_entries(ref))
    for ref in push_refs:
        push_entries.extend(reflog_entries(ref))
    return branch_entries, push_entries


def is_push_entry(entry: ReflogEntry) -> bool:
    return "push" in entry.subject.lower()


def commit_exists(sha: str) -> bool:
    if not sha:
        return False
    proc = run_git(["cat-file", "-e", f"{sha}^{{commit}}"], check=False)
    return proc.returncode == 0


def commit_time(sha: str) -> str | None:
    if not commit_exists(sha):
        return None
    proc = run_git(["show", "-s", "--format=%cI", sha], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def estimate_time(sha: str, entries: list[ReflogEntry]) -> str:
    for entry in entries:
        if entry.sha == sha and entry.date_estimate:
            return entry.date_estimate
    return commit_time(sha) or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def has_commit_reflog_event(sha: str, entries: list[ReflogEntry]) -> bool:
    prefix = "commit"
    return any(entry.sha == sha and entry.subject.lower().startswith(prefix) for entry in entries)


def audit(remote: str, branch: str, *, no_network: bool = False) -> dict[str, Any]:
    payload = empty_payload(remote, branch)
    payload["local_branch_sha"] = local_branch_sha(branch)

    remote_sha: str | None = None
    if not no_network:
        remote_sha = remote_branch_sha(remote, branch)
        payload["remote_sha"] = remote_sha

    branch_entries, push_entries = relevant_reflogs(remote, branch)

    if remote_sha:
        matching_push = any(entry.sha == remote_sha and is_push_entry(entry) for entry in push_entries)
        if not matching_push:
            payload["unauthorized_advances"].append(
                {
                    "sha": remote_sha,
                    "advanced_at_estimate": estimate_time(remote_sha, push_entries + branch_entries),
                    "no_matching_reflog_push": True,
                    "advice": ADVICE,
                }
            )
            payload["warning"] = f"{remote} advanced to {remote_sha} without local push reflog entry"

    candidate_shas: list[str] = []
    for sha in (payload["local_branch_sha"], remote_sha):
        if sha and sha not in candidate_shas and commit_exists(sha):
            candidate_shas.append(sha)

    for sha in candidate_shas:
        if has_commit_reflog_event(sha, branch_entries):
            continue
        payload["out_of_band_commits"].append(
            {
                "sha": sha,
                "no_matching_reflog_commit": True,
                "advice": "Inspect local refs and worker logs; recreate through normal git commit flow if unauthorized.",
            }
        )
        if payload["warning"] is None:
            payload["warning"] = f"local commit {sha} has no commit reflog entry"
        break

    payload["aligned"] = not payload["unauthorized_advances"] and not payload["out_of_band_commits"]
    return payload


def emit_text(payload: dict[str, Any]) -> None:
    target = f"{payload['remote']}/{payload['branch']}"
    if payload.get("error"):
        print(f"ERROR {target}: {payload['warning']}")
        return
    if payload["aligned"]:
        sha = payload.get("remote_sha") or payload.get("local_branch_sha") or "unknown"
        print(f"OK {target} aligned at {sha}")
        return
    print(f"WARN {target}: {payload['warning']}")
    for item in payload["unauthorized_advances"]:
        print(f"- unauthorized advance {item['sha']}: no matching push reflog")
    for item in payload["out_of_band_commits"]:
        print(f"- out-of-band commit {item['sha']}: no commit reflog")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="audit goal-flight branch push provenance")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true")
    mode.add_argument("--text", action="store_true")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--no-network", action="store_true")
    args = parser.parse_args(argv)

    try:
        payload = audit(args.remote, args.branch, no_network=args.no_network)
    except GitError as exc:
        payload = empty_payload(args.remote, args.branch)
        payload["aligned"] = False
        payload["warning"] = str(exc)
        payload["error"] = exc.to_json()
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            emit_text(payload)
        return 1

    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        emit_text(payload)
    return 0 if payload["aligned"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
