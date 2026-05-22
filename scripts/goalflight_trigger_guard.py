#!/usr/bin/env python3
"""Git metadata/content guard for known host-routing trigger tokens."""

from __future__ import annotations

import argparse
import base64
from pathlib import Path
import os
import re
import subprocess
import sys


ENCODED_PATTERNS = (
    "aGVybWVz",
    "b3BlbmNsYXc=",
    "b3Blbi1jbGF3",
)


class GuardError(RuntimeError):
    pass


def _decode_patterns() -> list[str]:
    raw = os.environ.get("GOALFLIGHT_TRIGGER_GUARD_PATTERNS_B64")
    encoded = tuple(item.strip() for item in raw.split(",") if item.strip()) if raw else ENCODED_PATTERNS
    return [
        base64.b64decode(value.encode("ascii")).decode("utf-8").casefold()
        for value in encoded
    ]


def _run_git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise GuardError(result.stderr.decode("utf-8", errors="replace").strip())
    return result


def _repo_root(raw: str | None) -> Path:
    if raw:
        return Path(raw).resolve()
    result = _run_git(Path.cwd(), ["rev-parse", "--show-toplevel"])
    return Path(result.stdout.decode("utf-8").strip()).resolve()


def _staged_paths(repo: Path) -> list[tuple[str, str]]:
    result = _run_git(repo, ["diff", "--cached", "--name-status", "-z"])
    parts = [item.decode("utf-8", errors="replace") for item in result.stdout.split(b"\0") if item]
    paths: list[tuple[str, str]] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if not status:
            continue
        if status[0] in {"R", "C"} and index + 1 < len(parts):
            index += 1
            paths.append((status, parts[index]))
            index += 1
        elif index < len(parts):
            paths.append((status, parts[index]))
            index += 1
    return paths


def _staged_blob(repo: Path, path: str) -> bytes | None:
    result = _run_git(repo, ["show", f":{path}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def _match(text: str, patterns: list[str]) -> str | None:
    folded = text.casefold()
    for pattern in patterns:
        if pattern in folded:
            return pattern
    return None


def _redact(text: str, patterns: list[str]) -> str:
    redacted = text
    for pattern in patterns:
        redacted = re.sub(re.escape(pattern), "[redacted-trigger]", redacted, flags=re.IGNORECASE)
    return redacted


def scan_staged(repo: Path) -> list[str]:
    patterns = _decode_patterns()
    findings: list[str] = []
    for status, path in _staged_paths(repo):
        if status == "D":
            continue
        if _match(path, patterns):
            findings.append(f"staged path: {_redact(path, patterns)}")
            continue
        blob = _staged_blob(repo, path)
        if blob is None:
            continue
        text = blob.decode("utf-8", errors="ignore")
        if _match(text, patterns):
            findings.append(f"staged content: {path}")
    return findings


def scan_message(path: Path) -> list[str]:
    patterns = _decode_patterns()
    text = path.read_text(encoding="utf-8", errors="ignore")
    if _match(text, patterns):
        return [f"commit message: {path}"]
    return []


def report(findings: list[str]) -> int:
    if not findings:
        return 0
    print("Goal Flight trigger guard blocked this commit.", file=sys.stderr)
    print("Remove host-routing trigger tokens from git-visible paths, staged content, or message text.", file=sys.stderr)
    for finding in findings:
        print(f"- {finding}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Block known host-routing trigger tokens before commit.")
    parser.add_argument("--repo", help="repository root")
    parser.add_argument("--staged", action="store_true", help="scan staged paths and blobs")
    parser.add_argument("--commit-msg", help="scan commit message file")
    args = parser.parse_args(argv)

    findings: list[str] = []
    if args.staged:
        findings.extend(scan_staged(_repo_root(args.repo)))
    if args.commit_msg:
        findings.extend(scan_message(Path(args.commit_msg)))
    if not args.staged and not args.commit_msg:
        parser.error("pass --staged and/or --commit-msg")
    return report(findings)


if __name__ == "__main__":
    raise SystemExit(main())
