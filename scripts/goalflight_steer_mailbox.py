"""Steer mailbox JSONL helpers.

Deploy this lock convention only after REV 5's zero-live-dispatch cutover gate.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import goalflight_dispatch_paths
import goalflight_terminal
from goalflight_liveness import active_monotonic


STEER_ACK_RE = goalflight_terminal.STEER_ACK_RE
TO_WORKER = "controller_to_worker"
TO_CONTROLLER = "worker_to_controller"
STEER_DIRECTIONS = frozenset({TO_WORKER, TO_CONTROLLER})


def steer_file(dispatch_id: str, state_dir: Path | str | None = None) -> Path:
    return goalflight_dispatch_paths.steer_file(dispatch_id, state_dir=state_dir)


def _normalize_steer_item(item: object) -> dict:
    if not isinstance(item, dict):
        raise ValueError("steer row must be an object")
    try:
        seq = int(item.get("seq"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("steer row seq must be an integer") from exc
    if seq < 1:
        raise ValueError("steer row seq must be >= 1")
    direction = str(item.get("direction") or TO_WORKER)
    if direction not in STEER_DIRECTIONS:
        raise ValueError(f"unsupported steer direction: {direction!r}")
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
    return entry


def parse_steer_lines(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entries.append(_normalize_steer_item(json.loads(line)))
        except (ValueError, RecursionError):
            continue
    return entries


def _carrier_module():
    # Lazy to avoid the intentional messages -> steer delivery import cycle.
    active_main = sys.modules.get("__main__")
    if active_main is not None and all(
        hasattr(active_main, name)
        for name in ("carrier_transaction", "record_carrier_quarantine")
    ):
        return active_main
    import goalflight_messages

    return goalflight_messages


def _parse_steer_carrier(path: Path, data: bytes) -> list[dict]:
    messages = _carrier_module()
    entries: list[dict] = []
    offset = 0
    for line_no, chunk in enumerate(data.splitlines(keepends=True), start=1):
        raw_line = chunk.rstrip(b"\r\n")
        line_offset = offset
        offset += len(chunk)
        if not raw_line.strip():
            continue
        try:
            entries.append(_normalize_steer_item(json.loads(raw_line.decode("utf-8"))))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            reason = f"invalid steer JSON: {type(exc).__name__}: {exc}"
            row = messages.record_carrier_quarantine(path, line_offset, reason, raw_line)
            print(
                f"WARNING: carrier corruption: {row['path']}:{line_no}: {reason}",
                file=sys.stderr,
            )
    return entries


def read_steer_entries(path: Path) -> list[dict]:
    messages = _carrier_module()
    with messages.carrier_transaction(path) as carrier:
        return _parse_steer_carrier(carrier.path, carrier.read_bytes())


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
    messages = _carrier_module()
    with messages.carrier_transaction(path) as carrier:
        existing = _parse_steer_carrier(carrier.path, carrier.read_bytes())
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
        try:
            encoded = (
                json.dumps(entry, allow_nan=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError(f"steer entry is not JSON-serializable: {exc}") from exc
        carrier.append_bytes(encoded)
        return entry


def append_message_view(
    dispatch_id: str,
    envelope: dict,
    *,
    state_dir: Path | str | None = None,
) -> tuple[Path, dict]:
    """Project a typed message envelope into the worker's steer mailbox."""
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("message envelope payload must be an object")
    text = payload.get("text")
    if text is None:
        text = json.dumps(payload, sort_keys=True) if payload else str(envelope.get("type") or "message")
    path = steer_file(dispatch_id, state_dir=state_dir)
    return path, append_steer_entry(
        path,
        str(text),
        dispatch_id=dispatch_id,
        kind="message",
        context={"message_envelope": envelope},
    )


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
