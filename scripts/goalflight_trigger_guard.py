#!/usr/bin/env python3
"""Git metadata/content guard for known host-routing trigger tokens."""

from __future__ import annotations

import argparse
import base64
from pathlib import Path
import os
import re
import shlex
import subprocess
import sys

TRIGGER_PATTERN_OVERRIDE_GATE = "GOALFLIGHT_ALLOW_TRIGGER_GUARD_PATTERN_OVERRIDE"

ENCODED_PATTERNS = (
    "aGVybWVz",
    "b3BlbmNsYXc=",
    "b3Blbi1jbGF3",
)
BACKGROUND_URL = (
    "https://www.reddit.com/r/ClaudeAI/search/"
    "?q=Claude%20Code%20git-visible%20billing%20trigger&restrict_sr=1"
)


class GuardError(RuntimeError):
    pass


def _warning(env_name: str, action: str, reason: str, *, source: str, count: int | None = None) -> None:
    parts = [
        "GOALFLIGHT_ENV_OVERRIDE",
        f"env={shlex.quote(env_name)}",
        f"action={shlex.quote(action)}",
        f"reason={shlex.quote(reason)}",
        f"source={shlex.quote(source)}",
    ]
    if count is not None:
        parts.append(f"pattern_count={shlex.quote(str(count))}")
    print(" ".join(parts), file=sys.stderr)


def _decode_env_patterns(raw: str) -> list[str]:
    encoded = tuple(item.strip() for item in raw.split(",") if item.strip())
    return [
        base64.b64decode(value.encode("ascii")).decode("utf-8").casefold()
        for value in encoded
    ]


def _decode_patterns(patterns_file: Path | None = None) -> list[str]:
    if patterns_file is not None:
        patterns = [
            line.strip().casefold()
            for line in patterns_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        _warning(
            "GOALFLIGHT_TRIGGER_GUARD_PATTERNS_FILE",
            "active",
            "cli_patterns_file",
            source=str(patterns_file),
            count=len(patterns),
        )
        return patterns
    raw = os.environ.get("GOALFLIGHT_TRIGGER_GUARD_PATTERNS_B64")
    if raw:
        if os.environ.get(TRIGGER_PATTERN_OVERRIDE_GATE) == "1":
            patterns = _decode_env_patterns(raw)
            _warning(
                "GOALFLIGHT_TRIGGER_GUARD_PATTERNS_B64",
                "active",
                f"{TRIGGER_PATTERN_OVERRIDE_GATE}=1",
                source="env",
                count=len(patterns),
            )
            return patterns
        _warning(
            "GOALFLIGHT_TRIGGER_GUARD_PATTERNS_B64",
            "ignored",
            f"{TRIGGER_PATTERN_OVERRIDE_GATE}_not_1",
            source="env",
        )
    encoded = ENCODED_PATTERNS
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


def scan_staged(repo: Path, patterns_file: Path | None = None) -> list[str]:
    patterns = _decode_patterns(patterns_file)
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


def scan_message(path: Path, patterns_file: Path | None = None) -> list[str]:
    patterns = _decode_patterns(patterns_file)
    text = path.read_text(encoding="utf-8", errors="ignore")
    if _match(text, patterns):
        return [f"commit message: {path}"]
    return []


def report(findings: list[str]) -> int:
    if not findings:
        return 0
    print("Goal Flight trigger guard blocked this commit.", file=sys.stderr)
    print("Remove host-routing trigger tokens from git-visible paths, staged content, or message text.", file=sys.stderr)
    print(
        "Why: these encoded tokens are reported to make Claude Code treat a Pro session as API-billed when they appear in git-visible metadata.",
        file=sys.stderr,
    )
    print(f"Background for the next human/agent: {BACKGROUND_URL}", file=sys.stderr)
    for finding in findings:
        print(f"- {finding}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Block known host-routing trigger tokens before commit.")
    parser.add_argument("--repo", help="repository root")
    parser.add_argument("--staged", action="store_true", help="scan staged paths and blobs")
    parser.add_argument("--commit-msg", help="scan commit message file")
    parser.add_argument(
        "--patterns-file",
        help="Plain-text pattern file, one pattern per non-comment line. Explicit CLI override.",
    )
    args = parser.parse_args(argv)

    findings: list[str] = []
    patterns_file = Path(args.patterns_file) if args.patterns_file else None
    if args.staged:
        findings.extend(scan_staged(_repo_root(args.repo), patterns_file))
    if args.commit_msg:
        findings.extend(scan_message(Path(args.commit_msg), patterns_file))
    if not args.staged and not args.commit_msg:
        parser.error("pass --staged and/or --commit-msg")
    return report(findings)


if __name__ == "__main__":
    raise SystemExit(main())
