"""Steer mailbox JSONL helpers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import goalflight_compat
import goalflight_dispatch_paths
import goalflight_terminal
from goalflight_liveness import active_monotonic


STEER_ACK_RE = goalflight_terminal.STEER_ACK_RE
TO_WORKER = "controller_to_worker"
TO_CONTROLLER = "worker_to_controller"
STEER_DIRECTIONS = frozenset({TO_WORKER, TO_CONTROLLER})


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
        direction = str(item.get("direction") or TO_WORKER)
        if direction not in STEER_DIRECTIONS:
            continue
        entry = {
            "seq": seq,
            "ts": str(item.get("ts") or ""),
            "text": str(item.get("text") or ""),
        }
        awake_mono_ns = item.get("awake_mono_ns")
        if (
            isinstance(awake_mono_ns, int)
            and not isinstance(awake_mono_ns, bool)
            and awake_mono_ns > 0
        ):
            entry["awake_mono_ns"] = awake_mono_ns
        if "direction" in item:
            entry["direction"] = direction
        for key in ("dispatch_id", "kind", "question_id", "reply_to", "decision", "context"):
            value = item.get(key)
            if value is not None:
                entry[key] = value
        entries.append(entry)
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


def append_steer_entry(
    path: Path,
    message: str,
    *,
    seq: int | None = None,
    direction: str = TO_WORKER,
    dispatch_id: str | None = None,
    kind: str = "steer",
    question_id: str | None = None,
    reply_to: str | None = None,
    decision: str | None = None,
    context: dict | None = None,
) -> dict:
    if direction not in STEER_DIRECTIONS:
        raise ValueError(f"unsupported steer direction: {direction!r}")
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
                "awake_mono_ns": int(active_monotonic() * 1_000_000_000),
                "text": message,
            }
            if direction != TO_WORKER:
                entry["direction"] = direction
            if kind != "steer":
                entry["kind"] = kind
            if dispatch_id:
                entry["dispatch_id"] = dispatch_id
            if question_id:
                entry["question_id"] = question_id
            if reply_to:
                entry["reply_to"] = reply_to
            if decision:
                entry["decision"] = decision
            if context:
                entry["context"] = context
            f.seek(0, os.SEEK_END)
            f.write(json.dumps(entry, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
            return entry
        finally:
            goalflight_compat.flock(f, goalflight_compat.LOCK_UN)


def append_steer_message(
    dispatch_id: str,
    text: str,
    *,
    reply_to: str | None = None,
    decision: str | None = None,
) -> tuple[Path, dict]:
    path = steer_file(dispatch_id)
    kind = "user_confirm_reply" if reply_to else "steer"
    return path, append_steer_entry(
        path,
        text,
        dispatch_id=dispatch_id,
        kind=kind,
        reply_to=reply_to,
        decision=decision,
    )


def append_user_confirm(
    path: Path,
    *,
    dispatch_id: str,
    question_id: str,
    text: str,
    context: dict | None = None,
) -> dict:
    """Post a worker question without making it deliverable back to that worker."""
    return append_steer_entry(
        path,
        text,
        direction=TO_CONTROLLER,
        dispatch_id=dispatch_id,
        kind="user_confirm",
        question_id=question_id,
        context=context,
    )


def worker_entries(entries: list[dict]) -> list[dict]:
    """Controller-authored entries eligible for delivery to the worker.

    Legacy rows have no direction on disk and parse as controller_to_worker.
    Worker-authored questions must never self-echo as authorization or steer.
    """
    return [entry for entry in entries if entry.get("direction", TO_WORKER) == TO_WORKER]


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
    print("seq\tts\tacked\ttext\tdirection\tkind")
    for entry in entries:
        deliverable = entry.get("direction", TO_WORKER) == TO_WORKER
        print(
            f"{entry['seq']}\t{entry['ts']}\t"
            f"{str(deliverable and entry['seq'] in acked).lower()}\t{entry['text']}\t"
            f"{entry.get('direction', TO_WORKER)}\t{entry.get('kind', 'steer')}"
        )
    return 0
