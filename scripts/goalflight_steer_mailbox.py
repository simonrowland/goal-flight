"""Steer mailbox JSONL helpers."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import goalflight_compat
import goalflight_dispatch_paths


STEER_ACK_RE = re.compile(r"^\**STEER-ACK:\**\s*(\d+)\b")


def steer_file(dispatch_id: str, state_dir: Path | str | None = None) -> Path:
    return goalflight_dispatch_paths.steer_file(dispatch_id, state_dir=state_dir)


def parse_steer_lines(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            seq = int(item.get("seq"))
        except (TypeError, ValueError):
            continue
        entries.append({
            "seq": seq,
            "ts": str(item.get("ts") or ""),
            "text": str(item.get("text") or ""),
        })
    return entries


def read_steer_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        goalflight_compat.flock(f, goalflight_compat.LOCK_SH)
        try:
            return parse_steer_lines(f.read().splitlines())
        finally:
            goalflight_compat.flock(f, goalflight_compat.LOCK_UN)


def append_steer_entry(path: Path, message: str, *, seq: int | None = None) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as f:
        goalflight_compat.flock(f, goalflight_compat.LOCK_EX)
        try:
            f.seek(0)
            existing = parse_steer_lines(f.read().splitlines())
            next_seq = max((entry["seq"] for entry in existing), default=0) + 1 if seq is None else seq
            entry = {
                "seq": next_seq,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "text": message,
            }
            f.seek(0, os.SEEK_END)
            f.write(json.dumps(entry, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
            return entry
        finally:
            goalflight_compat.flock(f, goalflight_compat.LOCK_UN)


def append_steer_message(dispatch_id: str, text: str) -> tuple[Path, dict]:
    path = steer_file(dispatch_id)
    return path, append_steer_entry(path, text)


def acked_steer_seqs(record: dict) -> set[int]:
    acked: set[int] = set()
    for key in ("stdout_path", "status_path"):
        value = record.get(key)
        if not value:
            continue
        path = Path(str(value))
        if not path.exists():
            continue
        if key == "status_path":
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            markers = payload.get("markers") or []
            if isinstance(markers, dict):
                for value in markers.get("STEER-ACK") or []:
                    try:
                        acked.add(int(str(value or "").split()[0]))
                    except (IndexError, ValueError):
                        pass
            else:
                for marker in markers:
                    if not isinstance(marker, dict) or marker.get("kind") != "STEER-ACK":
                        continue
                    try:
                        acked.add(int(str(marker.get("text") or "").split()[0]))
                    except (IndexError, ValueError):
                        pass
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = STEER_ACK_RE.match(line.strip())
            if match:
                acked.add(int(match.group(1)))
    return acked


def list_steer_messages(dispatch_id: str, record: dict) -> int:
    mailbox = steer_file(dispatch_id)
    entries = read_steer_entries(mailbox)
    acked = acked_steer_seqs(record)
    print(f"steer mailbox: {mailbox}")
    if not entries:
        print("(empty)")
        return 0
    print("seq\tts\tacked\ttext")
    for entry in entries:
        print(
            f"{entry['seq']}\t{entry['ts']}\t"
            f"{str(entry['seq'] in acked).lower()}\t{entry['text']}"
        )
    return 0
