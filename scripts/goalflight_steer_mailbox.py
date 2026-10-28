"""Steer mailbox JSONL helpers.

Deploy this lock convention only after REV 5's zero-live-dispatch cutover gate.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import errno
import json
import logging
import math
import os
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

import goalflight_dispatch_paths
import goalflight_compat
import goalflight_terminal
from goalflight_liveness import active_monotonic, process_group_id


STEER_ACK_RE = goalflight_terminal.STEER_ACK_RE
TO_WORKER = "controller_to_worker"
TO_CONTROLLER = "worker_to_controller"
STEER_DIRECTIONS = frozenset({TO_WORKER, TO_CONTROLLER})
STEERING_KIND = "steering"
USER_CONFIRM_KIND = "user_confirm"
WORKER_WAIT_STARTED_KIND = "worker_wait_started"
WORKER_WAIT_ENDED_KIND = "worker_wait_ended"
WORKER_WAIT_REPLY_KIND = "worker_wait_reply"
WORKER_WAIT_SETTLED_DECISIONS = frozenset({"failed", "reply", "timeout"})
WORKER_WAIT_QUESTION_KINDS = goalflight_terminal.WORKER_WAIT_QUESTION_KINDS
USER_CONFIRM_DECISIONS = frozenset({"yes", "no"})
DEFAULT_WORKER_WAIT_TIMEOUT_SECS = 600.0
MAX_WORKER_WAIT_TIMEOUT_SECS = 4 * 3600.0
DEFAULT_WORKER_WAIT_POLL_SECS = 0.25
WORKER_QUESTION_SETTLEMENT_LOCK_TIMEOUT_SECS = 1.0
CONTROLLER_STEER_LOCK_TIMEOUT_SECS = 5.0
STEER_HOOK_MAX_MAILBOX_BYTES = 256 * 1024
STEER_HOOK_LOCK_TIMEOUT_SECS = 0.05
MAX_STEER_ROW_OVERHEAD_BYTES = 64 * 1024
_LOGGER = logging.getLogger(__name__)
LEGACY_STEER_KIND_ALIASES = {
    "steer": STEERING_KIND,
    "user_confirm_reply": USER_CONFIRM_KIND,
}


class WorkerWaitReplyPending(ValueError):
    """An earlier wait owns one durable typed reply not yet delivered."""

    def __init__(self, arm: dict, reply: dict):
        self.arm = arm
        self.reply = reply
        super().__init__(
            f"worker wait {str(arm.get('question_id') or '')!r} has a durable "
            "controller reply pending delivery"
        )


class WorkerQuestionOutboxWriteDenied(PermissionError):
    """The current sandbox cannot write its dispatch-scoped question outbox."""

    def __init__(self, path: Path):
        super().__init__(
            "worker question outbox is not writable; read-only native Codex "
            f"workers cannot ask ({path})"
        )


def _parse_aware_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def worker_wait_reply_output_lines(entry: dict) -> tuple[str, str]:
    """Return an unparsed payload followed by its bounded receipt marker.

    Only an exact typed wait reply may carry the STEER-REPLY prefix: the
    watcher matches that label against the armed wait id and reply sequence,
    and the renewal fallback collects it as the consumption receipt. Generic
    backlog rows are controller messages, not confirmations, so they report
    under a distinct backlog label that no consumer parses as a reply. The
    human payload precedes the receipt so a partial output failure cannot
    durably claim consumption before delivering the answer.
    """
    detail = {
        "decision": entry.get("decision"),
        "seq": entry.get("seq"),
        "text": entry.get("text"),
    }
    message_line = "STEER-MESSAGE: " + json.dumps(
        detail, ensure_ascii=False, sort_keys=True
    )
    if entry.get("kind") == WORKER_WAIT_REPLY_KIND:
        receipt = {
            "kind": entry.get("kind"),
            "reply_to": entry.get("reply_to"),
            "seq": entry.get("seq"),
        }
        return (
            message_line,
            "STEER-REPLY: "
            + json.dumps(receipt, ensure_ascii=False, sort_keys=True),
        )
    backlog = {
        "kind": entry.get("kind"),
        "seq": entry.get("seq"),
    }
    return (
        "STEER-BACKLOG: " + json.dumps(backlog, ensure_ascii=False, sort_keys=True),
        message_line,
    )


def _worker_wait_receipt_identity(value: object) -> tuple[str, int] | None:
    try:
        payload = json.loads(str(value or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != WORKER_WAIT_REPLY_KIND:
        return None
    wait_id = str(payload.get("reply_to") or "").strip()
    reply_seq = payload.get("seq")
    if (
        not wait_id
        or not isinstance(reply_seq, int)
        or isinstance(reply_seq, bool)
        or reply_seq <= 0
    ):
        return None
    return wait_id, reply_seq


def consumed_worker_wait_receipts(
    record: dict,
    *,
    marker_entries: list[dict] | None = None,
) -> set[tuple[str, int]]:
    """Return exact typed reply receipts from status and the worker tail."""
    receipts: set[tuple[str, int]] = set()

    def add(value: object) -> None:
        identity = _worker_wait_receipt_identity(value)
        if identity is not None:
            receipts.add(identity)

    status_value = record.get("status_path")
    if status_value:
        status_path = Path(str(status_value))
        if status_path.exists():
            try:
                payload = json.loads(
                    status_path.read_text(encoding="utf-8", errors="replace")
                )
            except (OSError, json.JSONDecodeError):
                payload = {}
            markers = payload.get("markers") or []
            if isinstance(markers, dict):
                for value in markers.get("STEER-REPLY") or []:
                    add(value)
            else:
                for marker in markers:
                    if isinstance(marker, dict) and marker.get("kind") == "STEER-REPLY":
                        add(marker.get("text"))

    for marker in marker_entries or []:
        if isinstance(marker, dict) and marker.get("kind") == "STEER-REPLY":
            add(marker.get("text"))
    return receipts


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
            if key == "kind" and isinstance(value, str):
                value = LEGACY_STEER_KIND_ALIASES.get(value, value)
            entry[key] = value
    delivered_at = item.get("delivered_at")
    if isinstance(delivered_at, str) and delivered_at:
        entry["delivered_at"] = delivered_at
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


def maximum_steer_row_bytes() -> int:
    """Bound an accepted projected row from the message schema limits."""
    messages = _carrier_module()
    return (
        messages.MAX_ENVELOPE_JSON_BYTES
        + messages.MAX_PAYLOAD_JSON_BYTES
        + MAX_STEER_ROW_OVERHEAD_BYTES
    )


def is_carrier_error(exc: BaseException) -> bool:
    """Recognize the lazy carrier module's public error without an import cycle."""
    error_type = getattr(_carrier_module(), "MessageError", None)
    return isinstance(error_type, type) and isinstance(exc, error_type)


def _parse_steer_carrier(
    path: Path,
    data: bytes,
    *,
    quarantine_errors: bool = True,
) -> list[dict]:
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
            location = str(path)
            if quarantine_errors:
                row = messages.record_carrier_quarantine(
                    path,
                    line_offset,
                    reason,
                    raw_line,
                )
                location = str(row["path"])
            print(
                f"WARNING: carrier corruption: {location}:{line_no}: {reason}",
                file=sys.stderr,
            )
    return entries


def read_steer_entries(
    path: Path,
    *,
    lock_timeout_secs: float = CONTROLLER_STEER_LOCK_TIMEOUT_SECS,
    quarantine_errors: bool = True,
    max_bytes: int | None = None,
) -> list[dict]:
    messages = _carrier_module()
    with messages.carrier_transaction(
        path,
        lock_timeout_secs=lock_timeout_secs,
    ) as carrier:
        if max_bytes is not None and carrier.path.stat().st_size > max_bytes:
            raise ValueError(f"steer mailbox exceeds the {max_bytes}-byte read limit")
        return _parse_steer_carrier(
            carrier.path,
            carrier.read_bytes(),
            quarantine_errors=quarantine_errors,
        )


def contiguous_acked_seq(acked_seqs: set[int]) -> int:
    highest = 0
    while highest + 1 in acked_seqs:
        highest += 1
    return highest


def read_steer_entries_tail(
    path: Path,
    *,
    max_bytes: int = STEER_HOOK_MAX_MAILBOX_BYTES,
    max_window_bytes: int | None = None,
    acked_seqs: set[int] | None = None,
    lock_timeout_secs: float = STEER_HOOK_LOCK_TIMEOUT_SECS,
) -> tuple[list[dict], bool]:
    """Parse the newest bounded mailbox window; return whether older bytes exist."""
    if max_window_bytes is None:
        max_window_bytes = maximum_steer_row_bytes()
    first_pending_seq = contiguous_acked_seq(acked_seqs or set()) + 1
    messages = _carrier_module()
    with messages.carrier_transaction(
        path,
        lock_timeout_secs=lock_timeout_secs,
    ) as carrier:
        window = max_bytes
        while True:
            data, truncated = carrier.read_tail_bytes(window)
            entries = _parse_steer_carrier(
                carrier.path,
                data,
                quarantine_errors=False,
            )
            reached_pending = bool(entries and entries[0]["seq"] <= first_pending_seq)
            if reached_pending or not truncated or window >= max_window_bytes:
                return entries, truncated
            window = min(max_window_bytes, max(window * 2, window + 1))


def read_steer_entries_read_only(path: Path) -> list[dict]:
    """Read a mailbox snapshot without opening or creating its writer lock."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    if data and not data.endswith(b"\n"):
        trailing = data.rsplit(b"\n", 1)[-1]
        try:
            json.loads(trailing.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            data = data[: -len(trailing)]
    return _parse_steer_carrier(
        path,
        data,
        quarantine_errors=False,
    )


def append_steer_entry(
    path: Path,
    message: str,
    *,
    seq: int | None = None,
    direction: str = TO_WORKER,
    dispatch_id: str | None = None,
    kind: str = STEERING_KIND,
    question_id: str | None = None,
    reply_to: str | None = None,
    decision: str | None = None,
    context: dict | None = None,
    sender: dict | None = None,
    cross_project: bool = False,
    awake_mono_ns: int | None = None,
    lock_timeout_secs: float = CONTROLLER_STEER_LOCK_TIMEOUT_SECS,
    validate_existing: Callable[[list[dict]], dict | None] | None = None,
) -> dict:
    if direction not in STEER_DIRECTIONS:
        raise ValueError(f"unsupported steer direction: {direction!r}")
    kind = LEGACY_STEER_KIND_ALIASES.get(kind, kind)
    if awake_mono_ns is not None and (
        not isinstance(awake_mono_ns, int)
        or isinstance(awake_mono_ns, bool)
        or awake_mono_ns <= 0
    ):
        raise ValueError("awake_mono_ns must be a positive integer")
    messages = _carrier_module()
    with messages.carrier_transaction(
        path,
        lock_timeout_secs=lock_timeout_secs,
    ) as carrier:
        existing = _parse_steer_carrier(
            carrier.path,
            carrier.read_bytes(),
            quarantine_errors=True,
        )
        if validate_existing is not None:
            existing_entry = validate_existing(existing)
            if existing_entry is not None:
                return existing_entry
        next_seq = max((entry["seq"] for entry in existing), default=0) + 1 if seq is None else seq
        entry = {
            "seq": next_seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "awake_mono_ns": awake_mono_ns or int(active_monotonic() * 1_000_000_000),
            "text": message,
            "kind": kind,
        }
        if direction != TO_WORKER:
            entry["direction"] = direction
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
        if sender:
            entry["sender"] = dict(sender)
        if cross_project:
            entry["cross_project"] = True
        try:
            encoded = (
                json.dumps(entry, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError(f"steer entry is not JSON-serializable: {exc}") from exc
        max_row_bytes = maximum_steer_row_bytes()
        if len(encoded) > max_row_bytes:
            projected = context.get("message_envelope") if isinstance(context, dict) else None
            message_id = (
                projected.get("id") if isinstance(projected, dict)
                else context.get("message_id") if isinstance(context, dict) else None
            )
            if not isinstance(message_id, str) or not message_id:
                raise ValueError(f"steer row exceeds the {max_row_bytes}-byte cap")
            read_command = shlex.join([
                goalflight_compat.python_executable(),
                str(Path(messages.__file__).resolve()),
                "read", "--dispatch-id", str(dispatch_id), "--json",
            ])
            preview = "".join(char if char.isprintable() else " " for char in message[:160])
            pointer_context = {
                "message_id": message_id,
                "original_encoded_bytes": len(encoded),
                "preview": preview,
                "read_command": read_command,
            }
            if isinstance(projected, dict):
                pointer_context["message_envelope"] = {"id": message_id}
            else:
                for key in ("late", "attempt_id"):
                    if key in context:
                        pointer_context[key] = context[key]
            entry.pop("sender", None)
            entry["context"] = pointer_context
            entry["text"] = (
                f"oversized: read via {read_command}; select message id {message_id}. "
                f"Preview: {preview}"
            )
            encoded = (
                json.dumps(entry, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
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
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if payload else str(envelope.get("type") or "message")
    source = envelope.get("source")
    source = source if isinstance(source, dict) else {}
    sender = source.get("sender") if isinstance(source.get("sender"), dict) else None
    if sender is None:
        sender = {
            key: source[key]
            for key in ("controller_label", "controller_pid", "project_root")
            if key in source
        }
    if not {
        "controller_label",
        "controller_pid",
        "project_root",
    }.issubset(sender or {}):
        payload_sender = payload.get("sender")
        sender = dict(payload_sender) if isinstance(payload_sender, dict) else None
    if sender and {
        "controller_label",
        "controller_pid",
        "project_root",
    }.issubset(sender):
        text = (
            f"{text} [sender "
            f"controller_label={sender['controller_label']} "
            f"controller_pid={sender['controller_pid']} "
            f"project_root={sender['project_root']}]"
        )
    else:
        sender = None
    cross_project = bool(
        source.get("cross_project") is True
        or payload.get("cross_project") is True
        or (isinstance(sender, dict) and sender.get("cross_project") is True)
    )
    path = steer_file(dispatch_id, state_dir=state_dir)
    message_id = envelope.get("id")

    def reuse_message(entries: list[dict]) -> dict | None:
        if not isinstance(message_id, str) or not message_id:
            return None
        for entry in entries:
            if entry.get("kind") != "message":
                continue
            context = entry.get("context")
            projected = context.get("message_envelope") if isinstance(context, dict) else None
            if isinstance(projected, dict) and projected.get("id") == message_id:
                return entry
        source = envelope.get("source")
        payload = envelope.get("payload")
        if (
            isinstance(source, dict)
            and source.get("transport") == "steer"
            and isinstance(payload, dict)
            and payload.get("reply_to") is None
        ):
            import goalflight_messages

            refusal = goalflight_messages._plain_steer_confirm_refusal(
                dispatch_id,
                str(payload.get("text") or ""),
                entries=entries,
            )
            if refusal is not None:
                raise goalflight_messages.MessageError(refusal)
        return None

    reply_to = payload.get("reply_to")
    decision = payload.get("decision")
    if reply_to is not None:
        return path, append_worker_wait_reply(
            path,
            dispatch_id=dispatch_id,
            wait_id=str(reply_to),
            text=str(text),
            decision=None if decision is None else str(decision),
            sender=sender,
            cross_project=cross_project,
            message_id=message_id if isinstance(message_id, str) else None,
            lock_timeout_secs=CONTROLLER_STEER_LOCK_TIMEOUT_SECS,
        )
    if decision is not None:
        raise ValueError("worker wait reply decision requires reply_to")
    return path, append_steer_entry(
        path,
        str(text),
        dispatch_id=dispatch_id,
        kind="message",
        context={"message_envelope": envelope},
        sender=sender,
        cross_project=cross_project,
        lock_timeout_secs=CONTROLLER_STEER_LOCK_TIMEOUT_SECS,
        validate_existing=reuse_message,
    )


def append_steer_message(
    dispatch_id: str,
    text: str,
    *,
    reply_to: str | None = None,
    decision: str | None = None,
) -> tuple[Path, dict]:
    path = steer_file(dispatch_id)
    kind = USER_CONFIRM_KIND if reply_to else STEERING_KIND
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
        kind=USER_CONFIRM_KIND,
        question_id=question_id,
        context=context,
    )


def worker_entries(entries: list[dict]) -> list[dict]:
    """Controller-authored entries eligible for delivery to the worker.

    Legacy rows have no direction on disk and parse as controller_to_worker.
    Worker-authored questions must never self-echo as authorization or steer.
    """
    return [entry for entry in entries if entry.get("direction", TO_WORKER) == TO_WORKER]


def pending_worker_entries(entries: list[dict], acked_seqs: set[int]) -> list[dict]:
    """Return controller-authored rows the worker has not acknowledged."""
    return [
        entry
        for entry in worker_entries(entries)
        if entry["seq"] not in acked_seqs
        and (
            entry.get("kind") != WORKER_WAIT_REPLY_KIND
            or (
                isinstance(entry.get("context"), dict)
                and entry["context"].get("late") is True
            )
        )
    ]


def _positive_finite_seconds(value: object, *, field: str, maximum: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{field} must be finite and > 0")
    if seconds > maximum:
        raise ValueError(f"{field} must be <= {maximum:g}")
    return seconds


def _validate_worker_question(question_kind: str, question_text: object) -> str:
    normalized_kind = str(question_kind or "").strip()
    if normalized_kind not in WORKER_WAIT_QUESTION_KINDS:
        valid = ", ".join(sorted(WORKER_WAIT_QUESTION_KINDS))
        raise ValueError(
            f"question_kind must be one of: {valid}"
        )
    normalized = str(question_text or "").strip()
    if not normalized:
        raise ValueError("question_text must not be empty")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("question_text must be one line")
    return normalized


WORKER_QUESTION_SCHEMA = "goalflight.worker-question.v1"
WORKER_QUESTION_SETTLEMENT_SCHEMA = "goalflight.worker-question-settlement.v1"


def write_worker_question_request(
    outbox_dir: Path,
    *,
    dispatch_id: str,
    attempt_id: str,
    wait_id: str,
    question_kind: str,
    question: str,
    timeout_s: float,
) -> dict:
    """Atomically create one sandbox-writable, dispatch-scoped question record."""
    dispatch_id = str(dispatch_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    wait_id = str(wait_id or "").strip()
    if not dispatch_id or not attempt_id or not wait_id:
        raise ValueError("worker question requires dispatch, attempt, and wait ids")
    if any(char in value for value in (dispatch_id, attempt_id, wait_id) for char in "\r\n"):
        raise ValueError("worker question identifiers must be single-line values")
    if any(char in dispatch_id + attempt_id + wait_id for char in "/\\"):
        raise ValueError("worker question identifiers must not contain path separators")
    question = _validate_worker_question(question_kind, question)
    timeout_s = _positive_finite_seconds(
        timeout_s,
        field="timeout_s",
        maximum=MAX_WORKER_WAIT_TIMEOUT_SECS,
    )
    created_at = datetime.now(timezone.utc)
    deadline_at = created_at + timedelta(seconds=timeout_s)
    payload = {
        "schema": WORKER_QUESTION_SCHEMA,
        "dispatch_id": dispatch_id,
        "attempt_id": attempt_id,
        "wait_id": wait_id,
        "question": question,
        "question_kind": str(question_kind).strip(),
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "timeout_s": timeout_s,
        "deadline_at": deadline_at.isoformat().replace("+00:00", "Z"),
    }
    outbox_dir = Path(outbox_dir)
    if not outbox_dir.is_dir():
        raise FileNotFoundError(f"worker question outbox does not exist: {outbox_dir}")
    destination = outbox_dir / f"{wait_id}.json"
    temporary = outbox_dir / f".{wait_id}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    descriptor = -1
    try:
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except PermissionError as exc:
            raise WorkerQuestionOutboxWriteDenied(outbox_dir) from exc
        encoded = (json.dumps(payload, allow_nan=False, sort_keys=True) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link publishes the complete file atomically and fails if a
        # request with this wait id already exists.
        try:
            os.link(temporary, destination)
        except PermissionError as exc:
            raise WorkerQuestionOutboxWriteDenied(outbox_dir) from exc
        directory_fd = os.open(outbox_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()
    return payload


def worker_question_settlement_path(outbox_dir: Path, wait_id: str) -> Path:
    return Path(outbox_dir) / f"{wait_id}.settled.json"


def worker_question_rejection_path(outbox_dir: Path, wait_id: str) -> Path:
    return Path(outbox_dir) / f"{wait_id}.rejected"


@contextlib.contextmanager
def _worker_question_settlement_lock(outbox_dir: Path, wait_id: str):
    lock_path = outbox_dir / f".{wait_id}.settled.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except PermissionError as exc:
        raise WorkerQuestionOutboxWriteDenied(outbox_dir) from exc
    with os.fdopen(descriptor, "a+b") as lock_file:
        deadline = time.monotonic() + WORKER_QUESTION_SETTLEMENT_LOCK_TIMEOUT_SECS
        while True:
            try:
                goalflight_compat.flock(
                    lock_file,
                    goalflight_compat.LOCK_EX | goalflight_compat.LOCK_NB,
                )
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "worker question settlement lock remained busy for "
                        f"{WORKER_QUESTION_SETTLEMENT_LOCK_TIMEOUT_SECS:.2f}s"
                    ) from exc
                time.sleep(min(0.025, remaining))
        try:
            yield
        finally:
            goalflight_compat.flock(lock_file, goalflight_compat.LOCK_UN)


def write_worker_question_settlement(
    outbox_dir: Path,
    *,
    dispatch_id: str,
    attempt_id: str,
    wait_id: str,
    state: str,
    deadline_at: str,
    reply_seq: int | None = None,
) -> dict:
    """Create durable settlement evidence for one question in this outbox."""
    if state not in {"reply", "timeout"}:
        raise ValueError("worker question settlement state must be reply or timeout")
    if state == "reply" and (
        isinstance(reply_seq, bool) or not isinstance(reply_seq, int) or reply_seq <= 0
    ):
        raise ValueError("worker question reply settlement requires a positive reply sequence")
    if state == "timeout" and reply_seq is not None:
        raise ValueError("worker question timeout settlement cannot carry a reply sequence")
    deadline_datetime = _parse_aware_utc_datetime(deadline_at)
    if deadline_datetime is None:
        raise ValueError("worker question settlement deadline_at must be a valid UTC timestamp")
    settled_datetime = datetime.now(timezone.utc)
    if state == "timeout" and settled_datetime < deadline_datetime:
        # The worker's timer uses monotonic time. If wall time moved backward,
        # retain the request's absolute deadline as the settlement instant.
        settled_datetime = deadline_datetime
    payload = {
        "schema": WORKER_QUESTION_SETTLEMENT_SCHEMA,
        "dispatch_id": str(dispatch_id),
        "attempt_id": str(attempt_id),
        "wait_id": str(wait_id),
        "state": state,
        "deadline_at": deadline_datetime.isoformat().replace("+00:00", "Z"),
        "settled_at": settled_datetime.isoformat().replace("+00:00", "Z"),
    }
    if reply_seq is not None:
        payload["reply_seq"] = reply_seq
    outbox_dir = Path(outbox_dir)
    with _worker_question_settlement_lock(outbox_dir, wait_id):
        return _write_worker_question_settlement_locked(outbox_dir, wait_id, payload)


def _write_worker_question_settlement_locked(
    outbox_dir: Path,
    wait_id: str,
    payload: dict,
) -> dict:
    state = payload["state"]
    destination = worker_question_settlement_path(outbox_dir, wait_id)
    temporary = outbox_dir / f".{wait_id}.settled.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    descriptor = -1
    try:
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except PermissionError as exc:
            raise WorkerQuestionOutboxWriteDenied(outbox_dir) from exc
        encoded = (json.dumps(payload, allow_nan=False, sort_keys=True) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            try:
                os.link(temporary, destination)
            except PermissionError as exc:
                raise WorkerQuestionOutboxWriteDenied(outbox_dir) from exc
        except FileExistsError:
            existing = read_worker_question_settlement(outbox_dir, payload)
            if existing is None:
                # All settlement writers take this wait's sidecar lock. Re-read
                # immediately before quarantine so a valid settlement that
                # appeared after the first read is never moved aside.
                existing = read_worker_question_settlement(outbox_dir, payload)
                if existing is None:
                    quarantine = outbox_dir / (
                        f".{wait_id}.settled.invalid.{uuid.uuid4().hex}"
                    )
                    try:
                        os.replace(destination, quarantine)
                    except FileNotFoundError:
                        pass
                    except PermissionError as exc:
                        raise WorkerQuestionOutboxWriteDenied(outbox_dir) from exc
                    try:
                        os.link(temporary, destination)
                    except PermissionError as exc:
                        raise WorkerQuestionOutboxWriteDenied(outbox_dir) from exc
                    except FileExistsError:
                        existing = read_worker_question_settlement(outbox_dir, payload)
                        if existing is None:
                            raise
            if existing is not None:
                if (
                    existing.get("state") == state
                    and existing.get("reply_seq") == payload.get("reply_seq")
                ):
                    return existing
                if existing.get("state") != "timeout" or state != "reply":
                    raise FileExistsError(destination)
                # A timed-out question may later be resumed with its exact late
                # reply. Replacing this one settlement record makes that receipt
                # durable without creating a second reply-consumption protocol.
                try:
                    os.replace(temporary, destination)
                except PermissionError as exc:
                    raise WorkerQuestionOutboxWriteDenied(outbox_dir) from exc
        directory_fd = os.open(outbox_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()
    return payload


def read_worker_question_settlement(
    outbox_dir: Path,
    request: dict,
) -> dict | None:
    path = worker_question_settlement_path(outbox_dir, str(request.get("wait_id") or ""))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _LOGGER.warning("ignoring unreadable worker question settlement %s: %s", path, exc)
        return None

    return _validate_worker_question_settlement(value, path, request)


def _validate_worker_question_settlement(
    value: object,
    path: Path,
    request: dict,
) -> dict | None:
    def invalid(reason: str) -> None:
        _LOGGER.warning("ignoring invalid worker question settlement %s: %s", path, reason)

    if (
        not isinstance(value, dict)
        or value.get("schema") != WORKER_QUESTION_SETTLEMENT_SCHEMA
        or value.get("dispatch_id") != request.get("dispatch_id")
        or value.get("attempt_id") != request.get("attempt_id")
        or value.get("wait_id") != request.get("wait_id")
        or not isinstance(value.get("state"), str)
        or value["state"] not in {"reply", "timeout"}
    ):
        invalid("identity or state mismatch")
        return None
    if request.get("_deadline_at_valid") is False:
        invalid("request has no valid deadline_at")
        return None
    recorded_deadline = _parse_aware_utc_datetime(value.get("deadline_at"))
    request_deadline = _parse_aware_utc_datetime(request.get("deadline_at"))
    settled_at = _parse_aware_utc_datetime(value.get("settled_at"))
    if (
        recorded_deadline is None
        or request_deadline is None
        or settled_at is None
        or recorded_deadline != request_deadline
    ):
        invalid("deadline_at or settled_at is missing, malformed, or inconsistent")
        return None
    if value["state"] == "timeout" and settled_at < request_deadline:
        invalid("timeout settled_at precedes the request deadline")
        return None
    if value["state"] == "reply" and (
        isinstance(value.get("reply_seq"), bool)
        or not isinstance(value.get("reply_seq"), int)
        or value["reply_seq"] <= 0
    ):
        invalid("reply settlement has an invalid reply sequence")
        return None
    if value["state"] == "timeout" and "reply_seq" in value:
        invalid("timeout settlement carries a reply sequence")
        return None
    return value


def worker_question_reply_receipts(outbox_dir: Path) -> set[tuple[str, int]]:
    """Read consumed exact replies from the dispatch outbox settlements."""
    receipts: set[tuple[str, int]] = set()
    outbox_dir = Path(outbox_dir)
    try:
        paths = outbox_dir.glob("*.settled.json")
    except OSError:
        return receipts
    for path in paths:
        try:
            wait_id = path.name.removesuffix(".settled.json")
            value = json.loads(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            _LOGGER.warning(
                "ignoring unreadable worker question settlement receipt %s: %s",
                path,
                exc,
            )
            continue
        except (OSError, json.JSONDecodeError) as exc:
            _LOGGER.warning(
                "ignoring unreadable worker question settlement receipt %s: %s",
                path,
                exc,
            )
            continue
        try:
            raw_request = json.loads(
                (outbox_dir / f"{wait_id}.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw_request, dict):
            continue
        dispatch_id = raw_request.get("dispatch_id")
        request = validate_worker_question_request(
            raw_request,
            dispatch_id=dispatch_id if isinstance(dispatch_id, str) else "",
        )
        if request is None or request["wait_id"] != wait_id:
            continue
        settlement = _validate_worker_question_settlement(value, path, request)
        reply_seq = settlement.get("reply_seq") if settlement else None
        if settlement and settlement.get("state") == "reply":
            receipts.add((wait_id, reply_seq))
    return receipts


def has_unsettled_worker_question(
    outbox_dir: Path,
    *,
    dispatch_id: str,
    attempt_id: str,
    wait_id: str | None = None,
) -> bool:
    """Fail closed when this attempt has a request without worker settlement."""
    outbox_dir = Path(outbox_dir)
    try:
        paths = list(outbox_dir.iterdir())
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        for path in paths:
            if not path.name.endswith(".json"):
                continue
            if path.name.endswith((".published", ".settled.json")):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except OSError:
                return True
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            request = validate_worker_question_request(raw, dispatch_id=dispatch_id)
            if (
                request is not None
                and path.name == f"{request['wait_id']}.json"
                and (not attempt_id or request["attempt_id"] == attempt_id)
                and (not wait_id or request["wait_id"] == wait_id)
                and not worker_question_rejection_path(
                    outbox_dir, request["wait_id"]
                ).exists()
                and read_worker_question_settlement(outbox_dir, request) is None
            ):
                return True
    except OSError:
        return True
    return False


def validate_worker_question_request(
    value: object,
    *,
    dispatch_id: str,
) -> dict | None:
    """Normalize a supported question request; ignore fields added by newer writers."""
    if not isinstance(value, dict) or value.get("schema") != WORKER_QUESTION_SCHEMA:
        return None
    if value.get("dispatch_id") != dispatch_id:
        return None
    attempt_id = value.get("attempt_id")
    wait_id = value.get("wait_id")
    if (
        not isinstance(attempt_id, str)
        or not attempt_id.strip()
        or not isinstance(wait_id, str)
        or not wait_id.strip()
        or any(char in attempt_id + wait_id for char in "\r\n/\\")
    ):
        return None
    try:
        question_kind_value = value.get("question_kind")
        question_kind = (
            str(question_kind_value).strip()
            if question_kind_value is not None
            else ""
        )
        if question_kind and question_kind not in WORKER_WAIT_QUESTION_KINDS:
            return None
        question = str(value.get("question") or "").strip()
        if not question or "\n" in question or "\r" in question:
            return None
        timeout_s = _positive_finite_seconds(
            value.get("timeout_s"),
            field="timeout_s",
            maximum=MAX_WORKER_WAIT_TIMEOUT_SECS,
        )
        created_at_value = str(value.get("created_at") or "")
        created_at = datetime.fromisoformat(created_at_value.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            return None
        deadline_at = _parse_aware_utc_datetime(value.get("deadline_at"))
        deadline_at_valid = deadline_at is not None
        if deadline_at_valid:
            deadline_delta = (deadline_at - created_at).total_seconds()
            deadline_at_valid = (
                math.isfinite(deadline_delta)
                and 0 < deadline_delta <= MAX_WORKER_WAIT_TIMEOUT_SECS
                and abs(deadline_delta - timeout_s) <= 0.001
            )
        if not deadline_at_valid:
            deadline_at = created_at + timedelta(seconds=timeout_s)
    except (TypeError, ValueError, OverflowError):
        return None
    return {
        "schema": WORKER_QUESTION_SCHEMA,
        "dispatch_id": dispatch_id,
        "attempt_id": attempt_id.strip(),
        "wait_id": wait_id.strip(),
        "question": question,
        "question_kind": question_kind,
        "created_at": created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "timeout_s": timeout_s,
        "deadline_at": deadline_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "_deadline_at_valid": deadline_at_valid,
    }


def write_worker_question_published_marker(path: Path) -> bool:
    """Create the wait-id dedupe marker after the watcher publishes its event."""
    return _write_worker_question_marker(path, "published\n")


def write_worker_question_rejected_marker(path: Path, reason: str) -> bool:
    """Durably record a permanent refusal to arm this question."""
    return _write_worker_question_marker(
        path,
        json.dumps({"state": "rejected", "reason": str(reason)}, sort_keys=True)
        + "\n",
    )


def _write_worker_question_marker(path: Path, text: str) -> bool:
    path = Path(path)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return True


def worker_wait_question_event_id(dispatch_id: str, wait_id: str) -> str:
    """Return the stable event id shared by the wait publisher and watcher fallback."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"goalflight:steer-wait:{dispatch_id}:{wait_id}",
        )
    )


def worker_wait_reply_command(
    dispatch_id: str,
    wait_id: str,
    question_kind: str,
) -> str:
    """Render the controller command that answers one wait."""
    command = (
        "goalflight_dispatch.py steer "
        + shlex.join([str(dispatch_id)])
        + ' "<answer>" --reply-to '
        + shlex.quote(str(wait_id))
    )
    if question_kind == "USER-CONFIRM":
        command += ' --decision "<yes|no>"'
    return command


def _latest_worker_wait_arm(entries: list[dict], dispatch_id: str) -> dict | None:
    for entry in reversed(entries):
        if (
            entry.get("direction", TO_WORKER) == TO_CONTROLLER
            and entry.get("kind") == WORKER_WAIT_STARTED_KIND
            and entry.get("dispatch_id") == dispatch_id
        ):
            return entry
    return None


def _worker_wait_settlement(
    entries: list[dict],
    *,
    wait_id: str,
    after_seq: int,
) -> dict | None:
    for entry in entries:
        if (
            entry.get("seq", 0) > after_seq
            and entry.get("direction", TO_WORKER) == TO_CONTROLLER
            and entry.get("kind") == WORKER_WAIT_ENDED_KIND
            and entry.get("reply_to") == wait_id
            and entry.get("decision") in WORKER_WAIT_SETTLED_DECISIONS
        ):
            if entry.get("decision") in {"failed", "timeout"}:
                return entry
            context = entry.get("context")
            reply_seq = context.get("reply_seq") if isinstance(context, dict) else None
            if (
                isinstance(reply_seq, int)
                and not isinstance(reply_seq, bool)
                and any(
                    reply.get("seq") == reply_seq
                    and reply.get("direction", TO_WORKER) == TO_WORKER
                    and reply.get("kind") == WORKER_WAIT_REPLY_KIND
                    and reply.get("reply_to") == wait_id
                    for reply in entries
                )
            ):
                return entry
    return None


def _worker_wait_replies(
    entries: list[dict],
    *,
    dispatch_id: str,
    wait_id: str,
    after_seq: int,
    attempt_id: str | None = None,
) -> list[dict]:
    return [
        entry
        for entry in entries
        if entry.get("seq", 0) > after_seq
        and entry.get("direction", TO_WORKER) == TO_WORKER
        and entry.get("dispatch_id") == dispatch_id
        and entry.get("kind") == WORKER_WAIT_REPLY_KIND
        and entry.get("reply_to") == wait_id
        and (
            attempt_id is None
            or (
                isinstance(entry.get("context"), dict)
                and entry["context"].get("attempt_id") == attempt_id
            )
        )
    ]


def _recoverable_worker_wait_reply(
    entries: list[dict],
    *,
    dispatch_id: str,
    attempt_id: str,
    consumed_reply_receipts: set[tuple[str, int]],
    resume_wait_id: str | None = None,
    expected_question_kind: str | None = None,
    question_text: str | None = None,
) -> tuple[dict, dict] | None:
    """Return one exact wait's durable reply when delivery is unproven.

    The writer's typed reply row is the admission record: it is appended and
    fsynced while the mailbox lock still protects the one-reply validation.
    Recovery normally requires the caller to name the exact wait id being
    resumed. A live wait may also recover the latest prior wait's reply when
    renewal was refused and that prior wait remains unsettled.
    """
    arm = (
        next(
            (
                entry
                for entry in reversed(entries)
                if entry.get("direction", TO_CONTROLLER) == TO_CONTROLLER
                and entry.get("kind") == WORKER_WAIT_STARTED_KIND
                and entry.get("dispatch_id") == dispatch_id
                and entry.get("question_id") == resume_wait_id
            ),
            None,
        )
        if resume_wait_id
        else _latest_worker_wait_arm(entries, dispatch_id)
    )
    if arm is None:
        return None
    wait_id = str(arm.get("question_id") or "").strip()
    try:
        arm_seq = int(arm["seq"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not wait_id:
        return None
    arm_context = arm.get("context")
    if not isinstance(arm_context, dict):
        return None
    arm_attempt_id = str(arm_context.get("attempt_id") or "unknown")
    if not resume_wait_id and arm_attempt_id != attempt_id:
        return None
    replies = _worker_wait_replies(
        entries,
        dispatch_id=dispatch_id,
        wait_id=wait_id,
        after_seq=arm_seq,
        attempt_id=arm_attempt_id,
    )
    if len(replies) != 1:
        return None
    reply = replies[0]
    settlement = _worker_wait_settlement(
        entries,
        wait_id=wait_id,
        after_seq=arm_seq,
    )
    if settlement is not None and settlement.get("decision") != "timeout":
        return None
    reply_seq = reply.get("seq")
    if (
        not isinstance(reply_seq, int)
        or isinstance(reply_seq, bool)
        or reply_seq <= 0
        or (wait_id, reply_seq) in consumed_reply_receipts
    ):
        return None
    context = arm_context
    if isinstance(context, dict) and (
        (
            expected_question_kind is not None
            and context.get("question_kind") != expected_question_kind
        )
        or (question_text is not None and context.get("question_text") != question_text)
    ):
        return None
    question_kind = context.get("question_kind") if isinstance(context, dict) else None
    decision = reply.get("decision")
    if question_kind == "USER-CONFIRM":
        if decision not in USER_CONFIRM_DECISIONS:
            return None
    elif decision is not None and decision not in USER_CONFIRM_DECISIONS:
        return None
    return arm, reply


def append_worker_wait_reply(
    path: Path,
    *,
    dispatch_id: str,
    wait_id: str,
    text: str,
    decision: str | None = None,
    sender: dict | None = None,
    cross_project: bool = False,
    message_id: str | None = None,
    lock_timeout_secs: float = CONTROLLER_STEER_LOCK_TIMEOUT_SECS,
) -> dict:
    """Durably admit one typed, exactly-correlated reply to one active wait.

    The deadline, one-reply, and type checks run under the mailbox lock. The
    carrier then fsyncs the typed reply before this writer returns, making that
    row the durable admission authority even when the original waiter is late.
    """
    wait_id = str(wait_id or "").strip()
    text = str(text or "").strip()
    if not wait_id:
        raise ValueError("worker wait reply requires wait_id")
    if not text or "\n" in text or "\r" in text:
        raise ValueError("worker wait reply text must be one non-empty line")
    normalized_decision = None if decision is None else str(decision).strip().lower()
    # Keep the classification explicit on every admitted row; a timeout race
    # must never turn a late reply into an absent context field.
    reply_context: dict[str, object] = {
        "late": False,
        **({"message_id": message_id} if message_id else {}),
    }
    def validate(entries: list[dict]) -> dict | None:
        arm = next(
            (
                entry
                for entry in reversed(entries)
                if entry.get("direction", TO_WORKER) == TO_CONTROLLER
                and entry.get("kind") == WORKER_WAIT_STARTED_KIND
                and entry.get("dispatch_id") == dispatch_id
                and entry.get("question_id") == wait_id
            ),
            None,
        )
        if arm is None:
            raise ValueError(f"no worker wait {wait_id!r} for dispatch {dispatch_id}")
        try:
            arm_seq = int(arm["seq"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("worker wait arm has invalid seq") from exc
        context = arm.get("context")
        attempt_id = (
            str(context.get("attempt_id") or "unknown")
            if isinstance(context, dict)
            else "unknown"
        )
        reply_context["attempt_id"] = attempt_id
        deadline_ns = (
            context.get("deadline_awake_mono_ns")
            if isinstance(context, dict)
            else None
        )
        if (
            not isinstance(deadline_ns, int)
            or isinstance(deadline_ns, bool)
            or deadline_ns <= 0
        ):
            raise ValueError(f"worker wait {wait_id!r} has invalid deadline")
        replies = _worker_wait_replies(
            entries,
            dispatch_id=dispatch_id,
            wait_id=wait_id,
            after_seq=arm_seq,
            attempt_id=attempt_id,
        )
        existing_context = replies[0].get("context") if len(replies) == 1 else None
        if message_id and isinstance(existing_context, dict) and existing_context.get("message_id") == message_id:
            return replies[0]
        settlement = _worker_wait_settlement(
            entries,
            wait_id=wait_id,
            after_seq=arm_seq,
        )
        if settlement is not None:
            if settlement.get("decision") == "timeout":
                reply_context["late"] = True
            else:
                raise ValueError(f"worker wait {wait_id!r} is already settled")
        if replies:
            raise ValueError(f"worker wait {wait_id!r} already has a reply")
        question_kind = context.get("question_kind") if isinstance(context, dict) else None
        if question_kind == "USER-CONFIRM":
            if normalized_decision not in USER_CONFIRM_DECISIONS:
                raise ValueError("USER-CONFIRM reply requires decision=yes or decision=no")
        elif normalized_decision is not None and normalized_decision not in USER_CONFIRM_DECISIONS:
            raise ValueError("worker wait reply decision must be yes or no")
        waiter_pid = context.get("waiter_pid") if isinstance(context, dict) else None
        waiter_token = context.get("waiter_start_token") if isinstance(context, dict) else None
        if (
            isinstance(waiter_pid, int)
            and not isinstance(waiter_pid, bool)
            and isinstance(waiter_token, str)
            and waiter_token
            and goalflight_compat.process_identity_matches(waiter_pid, waiter_token) is False
        ):
            reply_context["late"] = True

    lock_timeout_secs = CONTROLLER_STEER_LOCK_TIMEOUT_SECS if lock_timeout_secs is None else lock_timeout_secs
    return append_steer_entry(
        path,
        text,
        dispatch_id=dispatch_id,
        kind=WORKER_WAIT_REPLY_KIND,
        reply_to=wait_id,
        decision=normalized_decision,
        context=reply_context or None,
        sender=sender,
        cross_project=cross_project,
        validate_existing=validate,
        lock_timeout_secs=lock_timeout_secs,
    )


def append_worker_wait_started(
    path: Path,
    *,
    dispatch_id: str,
    timeout_secs: float,
    question_kind: str,
    question_text: str,
    wait_id: str | None = None,
    attempt_id: str | None = None,
    waiter_pid: int | None = None,
    waiter_start_token: str | None = None,
    waiter_pgid: int | None = None,
    deadline_mono: float | None = None,
    deadline_at: str | None = None,
    lock_timeout_secs: float = CONTROLLER_STEER_LOCK_TIMEOUT_SECS,
    consumed_reply_receipts: set[tuple[str, int]] | None = None,
) -> dict:
    """Durably arm one bounded, lease-free worker wait in its steer mailbox."""
    timeout_secs = _positive_finite_seconds(
        timeout_secs,
        field="timeout_secs",
        maximum=MAX_WORKER_WAIT_TIMEOUT_SECS,
    )
    question_kind = str(question_kind or "").strip()
    question_text = _validate_worker_question(question_kind, question_text)
    attempt_id = str(attempt_id or "unknown")
    consumed_reply_receipts = consumed_reply_receipts or set()
    wait_id = str(wait_id or uuid.uuid4().hex).strip()
    if not wait_id or any(char in wait_id for char in "\r\n/\\"):
        raise ValueError("worker wait requires a valid wait_id")
    started_mono_ns = int(active_monotonic() * 1_000_000_000)
    if deadline_mono is None:
        deadline_mono_ns = started_mono_ns + int(timeout_secs * 1_000_000_000)
    else:
        deadline_mono_ns = int(float(deadline_mono) * 1_000_000_000)
        if deadline_mono_ns <= 0:
            raise ValueError("worker wait deadline must be positive")
        deadline_mono_ns = min(
            deadline_mono_ns,
            started_mono_ns + int(timeout_secs * 1_000_000_000),
        )
    if deadline_at:
        deadline_datetime = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
        if deadline_datetime.tzinfo is None:
            raise ValueError("worker wait deadline_at must include a timezone")
    else:
        deadline_datetime = datetime.now(timezone.utc) + timedelta(seconds=timeout_secs)
    waiter_pid = os.getpid() if waiter_pid is None else int(waiter_pid)
    identity = goalflight_compat.process_start_identity(waiter_pid) or {}
    waiter_start_token = str(waiter_start_token or identity.get("start_token") or "")
    waiter_pgid = process_group_id(waiter_pid) if waiter_pgid is None else int(waiter_pgid)
    context = {
        "deadline_awake_mono_ns": deadline_mono_ns,
        "deadline_at": deadline_datetime.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "timeout_secs": timeout_secs,
        "waiter_pid": waiter_pid,
        "question_kind": question_kind,
        "question_text": question_text,
    }
    if waiter_start_token:
        context["waiter_start_token"] = waiter_start_token
    if waiter_pgid is not None:
        context["waiter_pgid"] = waiter_pgid
    context["attempt_id"] = attempt_id

    lock_deadline = (
        active_monotonic() + max(0.0, float(lock_timeout_secs))
        if lock_timeout_secs is not None
        else None
    )

    def lock_budget() -> float | None:
        if lock_deadline is None:
            return None
        return max(0.0, lock_deadline - active_monotonic())

    def settle_stale_prior_wait() -> None:
        """Close a prior arm that can no longer suspend its worker."""
        entries = read_steer_entries(
            path,
            lock_timeout_secs=lock_budget(),
            quarantine_errors=False,
        )
        prior = _latest_worker_wait_arm(entries, dispatch_id)
        if prior is None:
            return
        wait_id = str(prior.get("question_id") or "")
        if not wait_id:
            return
        try:
            prior_seq = int(prior["seq"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("prior worker wait arm has invalid seq") from exc
        if _worker_wait_settlement(
            entries,
            wait_id=wait_id,
            after_seq=prior_seq,
        ) is not None:
            return
        prior_context = prior.get("context")
        started_ns = prior.get("awake_mono_ns")
        deadline_ns = (
            prior_context.get("deadline_awake_mono_ns")
            if isinstance(prior_context, dict)
            else None
        )
        if (
            not isinstance(started_ns, int)
            or isinstance(started_ns, bool)
            or started_ns <= 0
            or not isinstance(deadline_ns, int)
            or isinstance(deadline_ns, bool)
            or deadline_ns <= 0
        ):
            # An unverifiable arm stays unsettled. Renewal must not turn
            # malformed or incomplete identity into a fabricated timeout.
            return
        waiter_pid = prior_context.get("waiter_pid") if isinstance(prior_context, dict) else None
        waiter_start_token = (
            prior_context.get("waiter_start_token")
            if isinstance(prior_context, dict)
            else None
        )
        if (
            isinstance(waiter_pid, bool)
            or not isinstance(waiter_pid, int)
            or waiter_pid <= 0
            or not isinstance(waiter_start_token, str)
            or not waiter_start_token
        ):
            return
        identity_state = goalflight_compat.process_identity_matches(
            waiter_pid,
            waiter_start_token,
        )
        if identity_state is not False:
            # Only a verified dead prior waiter can be settled here. The
            # worker's outbox settlement marker owns the question deadline.
            return
        try:
            append_worker_wait_ended(
                path,
                prior,
                decision="failed",
                context={"reason": "prior_wait_not_active"},
                lock_timeout_secs=lock_budget(),
            )
        except WorkerWaitReplyPending:
            return
        except ValueError as exc:
            if "already settled" not in str(exc):
                raise

    settle_stale_prior_wait()

    def reject_unsettled_prior_wait(entries: list[dict]) -> dict | None:
        prior = _latest_worker_wait_arm(entries, dispatch_id)
        if prior is None:
            return
        if prior.get("question_id") == wait_id:
            prior_context = prior.get("context")
            if (
                isinstance(prior_context, dict)
                and prior_context.get("question_kind") == question_kind
                and prior_context.get("question_text") == question_text
                and prior_context.get("attempt_id") == (str(attempt_id) if attempt_id else None)
            ):
                return prior
            raise ValueError(f"worker wait id {wait_id!r} was reused with different content")
        prior_wait_id = str(prior.get("question_id") or "")
        try:
            prior_seq = int(prior["seq"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("prior worker wait arm has invalid seq") from exc
        if prior_wait_id and _worker_wait_settlement(
            entries,
            wait_id=prior_wait_id,
            after_seq=prior_seq,
        ) is not None:
            return
        recoverable = _recoverable_worker_wait_reply(
            entries,
            dispatch_id=dispatch_id,
            attempt_id=attempt_id,
            consumed_reply_receipts=consumed_reply_receipts,
            expected_question_kind=question_kind,
            question_text=question_text,
        )
        if recoverable is not None:
            raise WorkerWaitReplyPending(*recoverable)
        replies = _worker_wait_replies(
            entries,
            dispatch_id=dispatch_id,
            wait_id=prior_wait_id,
            after_seq=prior_seq,
            attempt_id=attempt_id,
        )
        if len(replies) == 1:
            reply_seq = replies[0].get("seq")
            if (
                isinstance(reply_seq, int)
                and not isinstance(reply_seq, bool)
                and (prior_wait_id, reply_seq) not in consumed_reply_receipts
            ):
                # Keep an older typed answer attached to its own wait until
                # that exact question is resumed and delivered.
                raise WorkerWaitReplyPending(prior, replies[0])
            if (
                isinstance(reply_seq, int)
                and not isinstance(reply_seq, bool)
                and (prior_wait_id, reply_seq) in consumed_reply_receipts
            ):
                # A watched-tail STEER-REPLY is the durable fallback while the
                # independent worker_wait_ended operation is absent.
                context["settled_prior_reply"] = {
                    "wait_id": prior_wait_id,
                    "reply_seq": reply_seq,
                }
                return
        raise ValueError(
            "worker wait renewal refused: the prior wait has no consumed "
            "controller reply or terminal settlement"
        )

    return append_steer_entry(
        path,
        "worker waiting for controller reply",
        direction=TO_CONTROLLER,
        dispatch_id=dispatch_id,
        kind=WORKER_WAIT_STARTED_KIND,
        question_id=wait_id,
        context=context,
        awake_mono_ns=started_mono_ns,
        lock_timeout_secs=lock_budget(),
        validate_existing=reject_unsettled_prior_wait,
    )


def worker_wait_question_marker_text(
    dispatch_id: str,
    question_text: str,
    wait_id: str,
    *,
    question_kind: str,
) -> str:
    """Bind one bounded marker to its durable outbox record."""
    prefix = f"{dispatch_id} — "
    suffix = f" [wait-id:{wait_id}]"
    marker_prefix = f"!{question_kind}: "
    budget = 1000 - len(marker_prefix) - len(prefix) - len(suffix)
    if budget < 0:
        raise ValueError("dispatch and wait ids exceed the terminal marker limit")
    return prefix + question_text[:budget] + suffix


def publish_worker_wait_question(event: dict) -> dict:
    """Publish one wait question before the worker enters its blocking loop."""
    import goalflight_messages

    return goalflight_messages.post_worker_wait_question(
        dispatch_id=str(event["dispatch_id"]),
        wait_id=str(event["wait_id"]),
        question_kind=str(event["question_kind"]),
        question_text=str(event["question_text"]),
        reply_command=str(event["reply_command"]),
        event_id=worker_wait_question_event_id(
            str(event["dispatch_id"]),
            str(event["wait_id"]),
        ),
    )


def append_worker_wait_ended(
    path: Path,
    arm: dict,
    *,
    decision: str,
    reply_seq: int | None = None,
    context: dict | None = None,
    lock_timeout_secs: float = CONTROLLER_STEER_LOCK_TIMEOUT_SECS,
) -> dict:
    lock_timeout_secs = CONTROLLER_STEER_LOCK_TIMEOUT_SECS if lock_timeout_secs is None else lock_timeout_secs
    wait_id = str(arm.get("question_id") or "").strip()
    if not wait_id:
        raise ValueError("worker wait arm is missing question_id")
    if decision not in WORKER_WAIT_SETTLED_DECISIONS:
        raise ValueError("worker wait end requires reply, timeout, or failed")
    if decision == "reply":
        if isinstance(reply_seq, bool) or not isinstance(reply_seq, int) or reply_seq <= 0:
            raise ValueError("worker wait reply end requires a positive reply_seq")
        end_context = dict(context or {})
        end_context["reply_seq"] = reply_seq
    else:
        if reply_seq is not None:
            raise ValueError("non-reply worker wait end cannot carry reply_seq")
        end_context = dict(context or {})
        end_context.setdefault(
            "settled_awake_mono_ns",
            int(active_monotonic() * 1_000_000_000),
        )
    arm_context = arm.get("context")
    if isinstance(arm_context, dict) and arm_context.get("attempt_id"):
        end_context["attempt_id"] = arm_context["attempt_id"]

    def validate(entries: list[dict]) -> None:
        try:
            arm_seq = int(arm["seq"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("worker wait arm has invalid seq") from exc
        replies = _worker_wait_replies(
            entries,
            dispatch_id=str(arm.get("dispatch_id") or ""),
            wait_id=wait_id,
            after_seq=arm_seq,
            attempt_id=(
                arm_context.get("attempt_id")
                if isinstance(arm_context, dict)
                else None
            ),
        )
        if decision == "reply":
            if len(replies) != 1 or replies[0].get("seq") != reply_seq:
                raise ValueError("worker wait end does not match one consumed typed reply")
        elif (
            decision in {"timeout", "failed"}
            and replies
            and not (decision == "timeout" and end_context.get("worker_settled") is True)
        ):
            # A controller reply won the settlement race. Let the caller
            # deliver it instead of recording a misleading terminal row.
            raise WorkerWaitReplyPending(arm, replies[0])
        if _worker_wait_settlement(
            entries,
            wait_id=wait_id,
            after_seq=arm_seq,
        ) is not None:
            raise ValueError("worker wait is already settled")

    return append_steer_entry(
        path,
        "worker reply wait ended",
        direction=TO_CONTROLLER,
        dispatch_id=str(arm.get("dispatch_id") or "") or None,
        kind=WORKER_WAIT_ENDED_KIND,
        reply_to=wait_id,
        decision=decision,
        context=end_context,
        lock_timeout_secs=lock_timeout_secs,
        validate_existing=validate,
    )


def active_worker_wait(
    entries: list[dict],
    *,
    dispatch_id: str,
    now_mono: float | None = None,
    worker_pid: int | None = None,
    worker_pgid: int | None = None,
) -> dict | None:
    """Return the newest valid unsettled arm owned by the tracked worker."""
    now_ns = int((active_monotonic() if now_mono is None else now_mono) * 1_000_000_000)
    entry = _latest_worker_wait_arm(entries, dispatch_id)
    if entry is not None:
        wait_id = str(entry.get("question_id") or "")
        if not wait_id:
            return None
        context = entry.get("context")
        started_ns = entry.get("awake_mono_ns")
        deadline_ns = context.get("deadline_awake_mono_ns") if isinstance(context, dict) else None
        if (
            not isinstance(started_ns, int)
            or isinstance(started_ns, bool)
            or started_ns <= 0
            or not isinstance(deadline_ns, int)
            or isinstance(deadline_ns, bool)
            or deadline_ns <= 0
        ):
            return None
        try:
            arm_seq = int(entry["seq"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if _worker_wait_settlement(
            entries,
            wait_id=wait_id,
            after_seq=arm_seq,
        ) is not None:
            return None
        waiter_pid = context.get("waiter_pid") if isinstance(context, dict) else None
        waiter_start_token = (
            context.get("waiter_start_token") if isinstance(context, dict) else None
        )
        waiter_pgid = context.get("waiter_pgid") if isinstance(context, dict) else None
        if (
            isinstance(waiter_pid, bool)
            or not isinstance(waiter_pid, int)
            or waiter_pid <= 0
            or not isinstance(waiter_start_token, str)
            or not waiter_start_token
            or goalflight_compat.process_identity_matches(
                waiter_pid,
                waiter_start_token,
            )
            is not True
        ):
            # PID alone is not an incarnation. A dead, reused, or unverifiable
            # waiter cannot grant a multi-hour idle exemption.
            return None
        current_waiter_pgid = process_group_id(waiter_pid)
        if worker_pgid is not None:
            if (
                isinstance(waiter_pgid, bool)
                or not isinstance(waiter_pgid, int)
                or waiter_pgid <= 0
                or waiter_pgid != worker_pgid
                or current_waiter_pgid != worker_pgid
            ):
                return None
        elif worker_pid is not None and waiter_pid != worker_pid:
            # Platforms without process-group identity cannot safely bind a
            # helper process to the tracked worker. Only an in-process waiter
            # may suspend idle accounting there.
            return None
        question_kind = context.get("question_kind")
        question_text = str(context.get("question_text") or "").strip()
        if (
            not isinstance(question_kind, str)
            or not question_kind
            or len(question_kind) > 64
            or any(char in question_kind for char in "\r\n")
            or not question_kind[0].isalpha()
            or any(not (char.isalnum() or char in "_-") for char in question_kind)
            or not question_text
            or "\n" in question_text
            or "\r" in question_text
        ):
            return None
        replies = _worker_wait_replies(
            entries,
            dispatch_id=dispatch_id,
            wait_id=wait_id,
            after_seq=arm_seq,
            attempt_id=(context.get("attempt_id") if isinstance(context, dict) else None),
        )
        if len(replies) > 1:
            return None
        reply = replies[0] if replies else None
        if reply is not None:
            decision = reply.get("decision")
            if question_kind == "USER-CONFIRM" and decision not in USER_CONFIRM_DECISIONS:
                return None
            if question_kind != "USER-CONFIRM" and decision is not None and decision not in USER_CONFIRM_DECISIONS:
                return None
            reply_awake_mono_ns = reply.get("awake_mono_ns")
            if (
                not isinstance(reply_awake_mono_ns, int)
                or isinstance(reply_awake_mono_ns, bool)
                or reply_awake_mono_ns <= 0
            ):
                return None
        else:
            reply_awake_mono_ns = None
        return {
            "wait_id": wait_id,
            "attempt_id": context.get("attempt_id") if isinstance(context, dict) else None,
            "seq": arm_seq,
            "started_awake_mono_ns": started_ns,
            "deadline_awake_mono_ns": deadline_ns,
            "remaining_secs": max(0.0, (deadline_ns - now_ns) / 1_000_000_000),
            "question_kind": question_kind,
            "question_text": question_text,
            "phase": "reply_pending" if reply is not None else "awaiting_reply",
            "reply_seq": reply.get("seq") if reply is not None else None,
            "reply_decision": reply.get("decision") if reply is not None else None,
            "reply_awake_mono_ns": reply_awake_mono_ns,
            "waiter_pid": waiter_pid,
            "waiter_start_token": waiter_start_token,
            "waiter_pgid": waiter_pgid,
            "question_marker_text": worker_wait_question_marker_text(
                dispatch_id,
                question_text,
                wait_id,
                question_kind=question_kind,
            ),
        }
    return None


def wait_for_worker_entries(
    path: Path,
    *,
    dispatch_id: str,
    acked_seqs: set[int],
    consumed_reply_receipts: set[tuple[str, int]] | None = None,
    question_kind: str,
    question_text: str,
    attempt_id: str = "unknown",
    resume_wait_id: str | None = None,
    outbox_dir: Path | None = None,
    settlement_outbox_dir: Path | None = None,
    timeout_secs: float = DEFAULT_WORKER_WAIT_TIMEOUT_SECS,
    poll_secs: float = DEFAULT_WORKER_WAIT_POLL_SECS,
    notify: Callable[[dict], None] | None = None,
) -> dict:
    """Create a question outbox record, then read the steer mailbox until reply or deadline."""
    timeout_secs = _positive_finite_seconds(
        timeout_secs,
        field="timeout_secs",
        maximum=MAX_WORKER_WAIT_TIMEOUT_SECS,
    )
    poll_secs = _positive_finite_seconds(
        poll_secs,
        field="poll_secs",
        maximum=MAX_WORKER_WAIT_TIMEOUT_SECS,
    )
    question_kind = str(question_kind or "").strip()
    question_text = _validate_worker_question(question_kind, question_text)
    consumed_reply_receipts = consumed_reply_receipts or set()
    if outbox_dir is None:
        outbox_dir = goalflight_dispatch_paths.worker_question_outbox_dir(
            dispatch_id,
            dispatch_dir=Path(path).parent,
        )
    else:
        outbox_dir = Path(outbox_dir)
    if settlement_outbox_dir is not None:
        settlement_outbox_dir = Path(settlement_outbox_dir)
    consumed_reply_receipts |= worker_question_reply_receipts(outbox_dir)

    def report(event: dict) -> None:
        if notify is not None:
            notify(event)

    deadline: float | None = None
    question_request: dict | None = None

    def remaining_secs() -> float:
        return max(0.0, deadline - active_monotonic()) if deadline is not None else timeout_secs

    def deliver_reply(reply: dict, wait_id: str, *, recovered: bool = False) -> dict:
        result = {
            "state": "messages",
            "entries": [reply],
            "arm_time_backlog": False,
            "wait_id": wait_id,
            "recovered_admitted_reply": recovered,
        }
        report(result)
        reply_context = reply.get("context")
        reply_attempt_id = (
            str(reply_context.get("attempt_id") or attempt_id)
            if isinstance(reply_context, dict)
            else attempt_id
        )
        write_worker_question_settlement(
            settlement_outbox_dir or outbox_dir,
            dispatch_id=dispatch_id,
            attempt_id=reply_attempt_id,
            wait_id=wait_id,
            state="reply",
            deadline_at=str((question_request or {}).get("deadline_at") or ""),
            reply_seq=int(reply.get("seq") or 0),
        )
        return result

    def deadline_result(wait_id: str = "") -> dict:
        result = {
            "state": "deadline",
            "entries": [],
            "timed_out": True,
            "question_text": question_text,
        }
        if wait_id:
            result["wait_id"] = wait_id
            write_worker_question_settlement(
                outbox_dir,
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                wait_id=wait_id,
                state="timeout",
                deadline_at=str((question_request or {}).get("deadline_at") or ""),
            )
        report(result)
        return result

    def deliver_pending_prior_reply(
        entries: list[dict],
        *,
        supersede_wait_id: str | None = None,
    ) -> dict | None:
        nonlocal question_request
        recoverable = _recoverable_worker_wait_reply(
            entries,
            dispatch_id=dispatch_id,
            attempt_id=attempt_id,
            consumed_reply_receipts=consumed_reply_receipts,
        )
        if recoverable is None:
            return None
        arm, reply = recoverable
        prior_wait_id = str(arm.get("question_id") or "")
        try:
            arm_seq = int(arm["seq"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if (
            not prior_wait_id
            or prior_wait_id == str((question_request or {}).get("wait_id") or "")
            or _worker_wait_settlement(
                entries,
                wait_id=prior_wait_id,
                after_seq=arm_seq,
            )
            is not None
        ):
            return None
        reply_seq = reply.get("seq")
        if isinstance(reply_seq, int) and not isinstance(reply_seq, bool) and reply_seq in acked_seqs:
            raise ValueError(f"worker wait {prior_wait_id!r} reply is already acknowledged")
        try:
            raw_request = json.loads(
                (outbox_dir / f"{prior_wait_id}.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        prior_request = validate_worker_question_request(
            raw_request,
            dispatch_id=dispatch_id,
        )
        arm_context = arm.get("context")
        if (
            prior_request is None
            or prior_request["wait_id"] != prior_wait_id
            or prior_request["attempt_id"] != attempt_id
            or not isinstance(arm_context, dict)
            or prior_request["question_kind"] != arm_context.get("question_kind")
            or prior_request["question"] != arm_context.get("question_text")
        ):
            return None
        if supersede_wait_id:
            with _worker_question_settlement_lock(outbox_dir, supersede_wait_id):
                rejection_path = worker_question_rejection_path(
                    outbox_dir,
                    supersede_wait_id,
                )
                if not rejection_path.exists():
                    current_entries = read_steer_entries_read_only(path)
                    if any(
                        entry.get("direction", TO_CONTROLLER) == TO_CONTROLLER
                        and entry.get("kind") == WORKER_WAIT_STARTED_KIND
                        and entry.get("dispatch_id") == dispatch_id
                        and entry.get("question_id") == supersede_wait_id
                        for entry in current_entries
                    ):
                        return None
                    write_worker_question_rejected_marker(
                        rejection_path,
                        f"superseded by recovered reply to prior wait {prior_wait_id}",
                    )
        question_request = prior_request
        return deliver_reply(reply, prior_wait_id, recovered=True)

    initial_entries = read_steer_entries_read_only(path)
    if resume_wait_id:
        try:
            raw_request = json.loads(
                (outbox_dir / f"{resume_wait_id}.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"worker wait {resume_wait_id!r} has no valid outbox request"
            ) from exc
        request = validate_worker_question_request(raw_request, dispatch_id=dispatch_id)
        if (
            request is None
            or request["wait_id"] != resume_wait_id
            or request["question_kind"] != question_kind
            or request["question"] != question_text
        ):
            raise ValueError(f"worker wait {resume_wait_id!r} does not match this question")
        arm = next(
            (
                entry
                for entry in reversed(initial_entries)
                if entry.get("direction", TO_CONTROLLER) == TO_CONTROLLER
                and entry.get("kind") == WORKER_WAIT_STARTED_KIND
                and entry.get("dispatch_id") == dispatch_id
                and entry.get("question_id") == resume_wait_id
                and isinstance(entry.get("context"), dict)
                and entry["context"].get("attempt_id") == request["attempt_id"]
            ),
            None,
        )
        if arm is None:
            raise ValueError(f"worker wait {resume_wait_id!r} has no matching mailbox arm")
        question_request = request
        recoverable = _recoverable_worker_wait_reply(
            initial_entries,
            dispatch_id=dispatch_id,
            attempt_id=attempt_id,
            consumed_reply_receipts=consumed_reply_receipts,
            resume_wait_id=resume_wait_id,
            expected_question_kind=question_kind,
            question_text=question_text,
        )
        if recoverable is None:
            raise ValueError(f"worker wait {resume_wait_id!r} has no pending reply to resume")
        _prior_arm, prior_reply = recoverable
        if int(prior_reply.get("seq") or 0) in acked_seqs:
            raise ValueError(f"worker wait {resume_wait_id!r} reply is already acknowledged")
        return deliver_reply(prior_reply, resume_wait_id, recovered=True)

    recovered_reply = deliver_pending_prior_reply(initial_entries)
    if recovered_reply is not None:
        return recovered_reply

    backlog = [
        entry
        for entry in pending_worker_entries(initial_entries, acked_seqs)
        if entry.get("kind") != WORKER_WAIT_REPLY_KIND
    ]
    if question_kind == "USER-NEED" and backlog:
        result = {
            "state": "messages",
            "entries": backlog,
            "arm_time_backlog": True,
            "wait_id": "",
        }
        report(result)
        return result
    if question_kind == "USER-CONFIRM" and backlog:
        report({"state": "backlog", "entries": backlog})
    remaining = remaining_secs()
    if remaining <= 0:
        return deadline_result()

    wait_id = uuid.uuid4().hex
    request = write_worker_question_request(
        outbox_dir,
        dispatch_id=dispatch_id,
        attempt_id=attempt_id,
        wait_id=wait_id,
        question_kind=question_kind,
        question=question_text,
        timeout_s=timeout_secs,
    )
    question_request = request
    absolute_deadline = datetime.fromisoformat(
        request["deadline_at"].replace("Z", "+00:00")
    )
    deadline = active_monotonic() + max(
        0.0,
        (absolute_deadline - datetime.now(timezone.utc)).total_seconds(),
    )
    question_marker_text = worker_wait_question_marker_text(
        dispatch_id,
        question_text,
        wait_id,
        question_kind=question_kind,
    )
    report(
        {
            "state": "armed",
            "wait_id": wait_id,
            "request": request,
            "timeout_secs": timeout_secs,
            "question_kind": question_kind,
            "question_marker_text": question_marker_text,
        }
    )
    while True:
        try:
            # Read once at the deadline boundary: a reply admitted before the
            # deadline remains consumable even when the worker wakes late.
            current_entries = read_steer_entries_read_only(path)
        except Exception as exc:
            if not isinstance(exc, OSError) and not is_carrier_error(exc):
                raise
            remaining = remaining_secs()
            if remaining <= 0:
                return deadline_result(wait_id)
            time.sleep(min(poll_secs, remaining))
            continue
        if not resume_wait_id:
            recovered_reply = deliver_pending_prior_reply(
                current_entries,
                supersede_wait_id=wait_id,
            )
            if recovered_reply is not None:
                return recovered_reply
        arm = next(
            (
                entry
                for entry in reversed(current_entries)
                if entry.get("direction", TO_CONTROLLER) == TO_CONTROLLER
                and entry.get("kind") == WORKER_WAIT_STARTED_KIND
                and entry.get("dispatch_id") == dispatch_id
                and entry.get("question_id") == wait_id
            ),
            None,
        )
        if arm is None:
            remaining = remaining_secs()
            if remaining <= 0:
                return deadline_result(wait_id)
            time.sleep(min(poll_secs, remaining))
            continue
        arm_context = arm.get("context")
        arm_attempt_id = (
            arm_context.get("attempt_id")
            if isinstance(arm_context, dict)
            else None
        )
        if arm_attempt_id != attempt_id:
            raise ValueError("worker wait arm belongs to a different attempt")
        replies = _worker_wait_replies(
            current_entries,
            dispatch_id=dispatch_id,
            wait_id=wait_id,
            after_seq=int(arm["seq"]),
            attempt_id=attempt_id,
        )
        if len(replies) == 1:
            reply = replies[0]
            reply_context = reply.get("context")
            reply_awake_ns = reply.get("awake_mono_ns")
            valid_decision = (
                question_kind != "USER-CONFIRM"
                or reply.get("decision") in USER_CONFIRM_DECISIONS
            )
            if not valid_decision:
                raise ValueError("worker wait reply has an invalid confirmation decision")
            if (
                not (isinstance(reply_context, dict) and reply_context.get("late") is True)
                and isinstance(reply_awake_ns, int)
                and not isinstance(reply_awake_ns, bool)
                and reply_awake_ns <= int(deadline * 1_000_000_000)
            ):
                return deliver_reply(reply, wait_id)
        if len(replies) > 1:
            raise ValueError("worker wait has multiple correlated replies")
        remaining = remaining_secs()
        if remaining <= 0:
            return deadline_result(wait_id)
        time.sleep(min(poll_secs, remaining))


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
    entries = read_steer_entries(
        mailbox,
        lock_timeout_secs=CONTROLLER_STEER_LOCK_TIMEOUT_SECS,
    )
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
            f"{entry.get('direction', TO_WORKER)}\t{entry.get('kind', STEERING_KIND)}"
        )
    return 0
