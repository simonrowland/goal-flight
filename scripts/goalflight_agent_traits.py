#!/usr/bin/env python3
"""Opt-in install of general agent-behavior traits into host global config."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

TRAITS_VERSION = 1

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CANONICAL_PATH = REPO_ROOT / "configs" / "agent-traits.md"

END_MARKER = "<!-- <<< goal-flight agent traits <<< -->"
BEGIN_RE = re.compile(
    r"<!-- >>> goal-flight agent traits \(v(\d+)\) >>> -->",
    re.MULTILINE,
)

HOST_TARGETS: dict[str, str] = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
}

HOST_HOME_SUBDIR: dict[str, str] = {
    "claude": ".claude",
    "codex": ".codex",
}


def canonical_text() -> str:
    raw = CANONICAL_PATH.read_text(encoding="utf-8")
    if raw.lstrip().startswith("<!--"):
        end = raw.find("-->")
        if end >= 0:
            raw = raw[end + 3 :]
    text = raw.lstrip("\n").rstrip("\n")
    return text + "\n" if text else ""


def installed_version(path: Path | str) -> int | None:
    found = _find_block(_read_text(Path(path).expanduser()))
    if found is None:
        return None
    return found[2]


def section_titles() -> list[str]:
    titles: list[str] = []
    for line in canonical_text().splitlines():
        if line.startswith("# "):
            titles.append(line[2:].strip())
    return titles


def default_target(host: str) -> Path | None:
    rel = HOST_TARGETS.get(host)
    sub = HOST_HOME_SUBDIR.get(host)
    if not rel or not sub:
        return None
    return (Path.home() / sub / rel).expanduser()


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _find_block(text: str) -> tuple[int, int, int | None, str] | None:
    end_idx = text.find(END_MARKER)
    if end_idx < 0:
        return None
    before = text[:end_idx]
    matches = list(BEGIN_RE.finditer(before))
    if not matches:
        return None
    begin = matches[-1]
    start = begin.start()
    end = end_idx + len(END_MARKER)
    version = int(begin.group(1))
    inner = text[begin.end() : end_idx]
    if inner.startswith("\n"):
        inner = inner[1:]
    return start, end, version, inner


def _marked_block() -> str:
    begin = f"<!-- >>> goal-flight agent traits (v{TRAITS_VERSION}) >>> -->"
    body = canonical_text().rstrip("\n")
    return f"{begin}\n{body}\n{END_MARKER}"


def status(path: Path | str) -> str:
    target = Path(path).expanduser()
    text = _read_text(target)
    found = _find_block(text)
    if found is None:
        return "absent"
    _start, _end, version, inner = found
    if version != TRAITS_VERSION:
        return "stale"
    if inner.rstrip("\n") != canonical_text().rstrip("\n"):
        return "stale"
    return "current"


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.goalflight-bak-{stamp}")


def _write_backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = _backup_path(path)
    shutil.copy2(path, backup)
    return backup


def install(path: Path | str, *, dry_run: bool = False) -> dict:
    target = Path(path).expanduser()
    current = status(target)
    action = "noop"
    backup: str | None = None

    if current == "current":
        return {
            "action": action,
            "backup": backup,
            "target": str(target),
            "version": TRAITS_VERSION,
        }

    text = _read_text(target)
    block = _marked_block()
    if current == "absent":
        action = "appended"
        if text and not text.endswith("\n"):
            text += "\n"
        if text:
            text += "\n"
        new_text = text + block + "\n"
    else:
        action = "updated"
        found = _find_block(text)
        if found is None:
            action = "appended"
            if text and not text.endswith("\n"):
                text += "\n"
            if text:
                text += "\n"
            new_text = text + block + "\n"
        else:
            start, end, _version, _inner = found
            new_text = text[:start] + block + text[end:]

    if dry_run:
        return {
            "action": action,
            "backup": backup,
            "target": str(target),
            "version": TRAITS_VERSION,
            "dry_run_text": new_text,
        }

    if target.exists():
        bak = _write_backup(target)
        backup = str(bak) if bak else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text, encoding="utf-8")
    return {
        "action": action,
        "backup": backup,
        "target": str(target),
        "version": TRAITS_VERSION,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight agent traits installer")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--host", default="claude", choices=sorted(HOST_TARGETS))
    parser.add_argument("--target", help="override default host global config path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.target:
        target = Path(args.target).expanduser()
    else:
        target = default_target(args.host)
        if target is None:
            print(f"no default target for host={args.host!r}", file=os.sys.stderr)
            return 1

    if args.status:
        result = {"status": status(target), "target": str(target), "version": TRAITS_VERSION}
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(result["status"])
        return 0

    if args.install:
        result = install(target, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(f"action={result['action']} target={result['target']}")
            if result.get("backup"):
                print(f"backup={result['backup']}")
        return 0

    parser.error("pass --status or --install")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())