#!/usr/bin/env python3
"""Audit recent Claude Code tool use for Goal Flight context discipline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


AGENT_TOOLS = {"Agent", "Task", "Explore"}
EDIT_TOOLS = {"Edit", "MultiEdit", "Write"}


def project_slug(cwd: Path) -> str:
    return str(cwd.resolve()).replace("/", "-")


def discover_session_log(cwd: Path) -> Path:
    project_dir = Path.home() / ".claude" / "projects" / project_slug(cwd)
    if not project_dir.is_dir():
        raise FileNotFoundError(f"Claude project directory not found: {project_dir}")
    logs = [p for p in project_dir.glob("*.jsonl") if p.is_file()]
    if not logs:
        raise FileNotFoundError(f"Claude session log not found under: {project_dir}")
    return max(logs, key=lambda p: p.stat().st_mtime)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def content_items(row: dict[str, Any]) -> Iterable[dict[str, Any]]:
    message = row.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    yield item
    content = row.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                yield item


def extract_tool_calls(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for row in rows:
        for item in content_items(row):
            if item.get("type") == "tool_use":
                calls.append({
                    "name": item.get("name") or "",
                    "input": item.get("input") if isinstance(item.get("input"), dict) else {},
                })
        direct_name = row.get("tool_name") or row.get("toolName")
        direct_input = row.get("tool_input") or row.get("input") or {}
        if isinstance(direct_name, str):
            calls.append({
                "name": direct_name,
                "input": direct_input if isinstance(direct_input, dict) else {},
            })
    return calls


def recent_rows(rows: list[dict[str, Any]], turns: int) -> tuple[list[dict[str, Any]], int]:
    turn_indexes = [
        idx for idx, row in enumerate(rows)
        if row.get("type") in {"user", "assistant"}
    ]
    if not turn_indexes:
        return rows, 0
    start_turn = turn_indexes[max(0, len(turn_indexes) - turns)]
    selected = rows[start_turn:]
    return selected, min(turns, len(turn_indexes))


def resolve_size(path_text: Any, cwd: Path) -> int:
    if not isinstance(path_text, str) or not path_text:
        return 0
    path = Path(path_text)
    if not path.is_absolute():
        path = cwd / path
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    return 0


def file_arg(call: dict[str, Any]) -> str | None:
    inp = call.get("input") if isinstance(call.get("input"), dict) else {}
    for key in ("file_path", "path", "file"):
        value = inp.get(key)
        if isinstance(value, str):
            return value
    return None


def read_without_edit_fraction(calls: list[dict[str, Any]]) -> float:
    read_indexes = [idx for idx, call in enumerate(calls) if call.get("name") == "Read"]
    if not read_indexes:
        return 0.0
    misses = 0
    for idx in read_indexes:
        target = file_arg(calls[idx])
        edited = False
        for follow in calls[idx + 1: idx + 3]:
            if follow.get("name") in EDIT_TOOLS and (target is None or file_arg(follow) == target):
                edited = True
                break
        if not edited:
            misses += 1
    return round(misses / len(read_indexes), 2)


def build_report(rows: list[dict[str, Any]], cwd: Path, turns: int) -> dict[str, Any]:
    selected, turn_count = recent_rows(rows, turns)
    calls = extract_tool_calls(selected)
    session_id = next((row.get("sessionId") for row in reversed(rows) if row.get("sessionId")), "")
    bash_calls = [call for call in calls if call.get("name") == "Bash"]
    agent_count = sum(1 for call in calls if call.get("name") in AGENT_TOOLS)
    bytes_read = sum(resolve_size(file_arg(call), cwd) for call in calls if call.get("name") == "Read")
    bytes_bashed_in = sum(
        len(str((call.get("input") or {}).get("command") or ""))
        for call in bash_calls
    )
    ratio = round(len(bash_calls) / max(agent_count, 1), 2)
    warning = ""
    if ratio > 10:
        warning = "bash:agent above 10:1 - consider delegation"
    elif read_without_edit_fraction(calls) > 0.5:
        warning = "read-without-edit above 50% - consider recon delegation"
    return {
        "session_id": session_id,
        "since_last_audit_turns": turn_count,
        "bytes_read": bytes_read,
        "bytes_bashed_in": bytes_bashed_in,
        "agents_dispatched": agent_count,
        "bash_to_agent_ratio": ratio,
        "read_without_edit_fraction": read_without_edit_fraction(calls),
        "warning": warning,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", action="store_true", help="emit one-line summary")
    parser.add_argument("--session-log", type=Path, help="read this JSONL session log")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="project cwd for Claude slug and relative paths")
    parser.add_argument("--turns", type=int, default=20, help="recent user/assistant turns to audit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        log_path = args.session_log if args.session_log else discover_session_log(args.cwd)
        rows = load_jsonl(log_path)
        report = build_report(rows, args.cwd, args.turns)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, separators=(",", ":")))
        return 1
    if args.text:
        print(
            "session={session_id} turns={since_last_audit_turns} "
            "read={bytes_read} bash_in={bytes_bashed_in} agents={agents_dispatched} "
            "bash_agent={bash_to_agent_ratio} read_no_edit={read_without_edit_fraction} "
            "warning={warning}".format(**report)
        )
    else:
        print(json.dumps(report, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
