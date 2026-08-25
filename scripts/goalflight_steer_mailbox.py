"""Steer mailbox JSONL helpers.

Deploy this lock convention only after REV 5's zero-live-dispatch cutover gate.
"""

from __future__ import annotations

import json
import math
import os
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
WORKER_WAIT_SETTLED_DECISIONS = frozenset({"reply"})
USER_CONFIRM_DECISIONS = frozenset({"yes", "no"})
DEFAULT_WORKER_WAIT_TIMEOUT_SECS = 3600.0
MAX_WORKER_WAIT_TIMEOUT_SECS = 4 * 3600.0
DEFAULT_WORKER_WAIT_POLL_SECS = 0.25
LEGACY_STEER_KIND_ALIASES = {
    "steer": STEERING_KIND,
    "user_confirm_reply": USER_CONFIRM_KIND,
}


def worker_wait_reply_output_lines(entry: dict) -> tuple[str, str]:
    """Return a bounded receipt marker plus an unparsed human-readable payload."""
    receipt = {
        "kind": entry.get("kind"),
        "reply_to": entry.get("reply_to"),
        "seq": entry.get("seq"),
    }
    detail = {
        "decision": entry.get("decision"),
        "seq": entry.get("seq"),
        "text": entry.get("text"),
    }
    return (
        "STEER-REPLY: "
        + json.dumps(receipt, ensure_ascii=False, sort_keys=True),
        "STEER-MESSAGE: "
        + json.dumps(detail, ensure_ascii=False, sort_keys=True),
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
    """Return exact typed reply receipts validated by the worker-tail scanner."""
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
    lock_timeout_secs: float | None = None,
    quarantine_errors: bool = True,
) -> list[dict]:
    messages = _carrier_module()
    with messages.carrier_transaction(
        path,
        lock_timeout_secs=lock_timeout_secs,
    ) as carrier:
        return _parse_steer_carrier(
            carrier.path,
            carrier.read_bytes(),
            quarantine_errors=quarantine_errors,
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
    awake_mono_ns: int | None = None,
    lock_timeout_secs: float | None = None,
    validate_existing: Callable[[list[dict]], None] | None = None,
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
            # A deadline-bounded caller must not enter a second, unbounded
            # quarantine-sidecar lock while it owns the mailbox carrier.
            quarantine_errors=lock_timeout_secs is None,
        )
        if validate_existing is not None:
            validate_existing(existing)
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
    reply_to = payload.get("reply_to")
    decision = payload.get("decision")
    if reply_to is not None:
        return path, append_worker_wait_reply(
            path,
            dispatch_id=dispatch_id,
            wait_id=str(reply_to),
            text=str(text),
            decision=None if decision is None else str(decision),
        )
    if decision is not None:
        raise ValueError("worker wait reply decision requires reply_to")
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
        and entry.get("kind") != WORKER_WAIT_REPLY_KIND
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
    if question_kind not in {"USER-NEED", "USER-CONFIRM"}:
        raise ValueError("question_kind must be USER-NEED or USER-CONFIRM")
    normalized = str(question_text or "").strip()
    if not normalized:
        raise ValueError("question_text must not be empty")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("question_text must be one line")
    return normalized


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
) -> list[dict]:
    return [
        entry
        for entry in entries
        if entry.get("seq", 0) > after_seq
        and entry.get("direction", TO_WORKER) == TO_WORKER
        and entry.get("dispatch_id") == dispatch_id
        and entry.get("kind") == WORKER_WAIT_REPLY_KIND
        and entry.get("reply_to") == wait_id
    ]


def append_worker_wait_reply(
    path: Path,
    *,
    dispatch_id: str,
    wait_id: str,
    text: str,
    decision: str | None = None,
    lock_timeout_secs: float | None = None,
) -> dict:
    """Append one typed, exactly-correlated reply to one active worker wait."""
    wait_id = str(wait_id or "").strip()
    text = str(text or "").strip()
    if not wait_id:
        raise ValueError("worker wait reply requires wait_id")
    if not text or "\n" in text or "\r" in text:
        raise ValueError("worker wait reply text must be one non-empty line")
    normalized_decision = None if decision is None else str(decision).strip().lower()

    def validate(entries: list[dict]) -> None:
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
        deadline_ns = (
            context.get("deadline_awake_mono_ns")
            if isinstance(context, dict)
            else None
        )
        if (
            not isinstance(deadline_ns, int)
            or isinstance(deadline_ns, bool)
            or int(active_monotonic() * 1_000_000_000) >= deadline_ns
        ):
            raise ValueError(f"worker wait {wait_id!r} is expired")
        if _worker_wait_settlement(
            entries,
            wait_id=wait_id,
            after_seq=arm_seq,
        ) is not None:
            raise ValueError(f"worker wait {wait_id!r} is already settled")
        if _worker_wait_replies(
            entries,
            dispatch_id=dispatch_id,
            wait_id=wait_id,
            after_seq=arm_seq,
        ):
            raise ValueError(f"worker wait {wait_id!r} already has a reply")
        question_kind = context.get("question_kind") if isinstance(context, dict) else None
        if question_kind == "USER-CONFIRM":
            if normalized_decision not in USER_CONFIRM_DECISIONS:
                raise ValueError("USER-CONFIRM reply requires decision=yes or decision=no")
        elif question_kind == "USER-NEED":
            if normalized_decision is not None and normalized_decision not in USER_CONFIRM_DECISIONS:
                raise ValueError("worker wait reply decision must be yes or no")
        else:
            raise ValueError("worker wait arm has invalid question_kind")

    return append_steer_entry(
        path,
        text,
        dispatch_id=dispatch_id,
        kind=WORKER_WAIT_REPLY_KIND,
        reply_to=wait_id,
        decision=normalized_decision,
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
    deadline_mono: float | None = None,
    lock_timeout_secs: float | None = None,
    consumed_reply_receipts: set[tuple[str, int]] | None = None,
) -> dict:
    """Durably arm one bounded, lease-free worker wait in its steer mailbox."""
    timeout_secs = _positive_finite_seconds(
        timeout_secs,
        field="timeout_secs",
        maximum=MAX_WORKER_WAIT_TIMEOUT_SECS,
    )
    question_text = _validate_worker_question(question_kind, question_text)
    consumed_reply_receipts = consumed_reply_receipts or set()
    wait_id = uuid.uuid4().hex
    started_mono_ns = int(active_monotonic() * 1_000_000_000)
    if deadline_mono is None:
        deadline_mono_ns = started_mono_ns + int(timeout_secs * 1_000_000_000)
    else:
        deadline_mono_ns = int(float(deadline_mono) * 1_000_000_000)
        if deadline_mono_ns <= started_mono_ns:
            raise TimeoutError("worker wait deadline reached before arm")
        deadline_mono_ns = min(
            deadline_mono_ns,
            started_mono_ns + int(timeout_secs * 1_000_000_000),
        )
    identity = goalflight_compat.process_start_identity(os.getpid()) or {}
    waiter_pgid = process_group_id(os.getpid())
    context = {
        "deadline_awake_mono_ns": deadline_mono_ns,
        "timeout_secs": timeout_secs,
        "waiter_pid": os.getpid(),
        "question_kind": question_kind,
        "question_text": question_text,
    }
    if identity.get("start_token"):
        context["waiter_start_token"] = identity["start_token"]
    if waiter_pgid is not None:
        context["waiter_pgid"] = waiter_pgid

    def reject_unsettled_prior_wait(entries: list[dict]) -> None:
        prior = _latest_worker_wait_arm(entries, dispatch_id)
        if prior is None:
            return
        wait_id = str(prior.get("question_id") or "")
        try:
            prior_seq = int(prior["seq"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("prior worker wait arm has invalid seq") from exc
        if wait_id and _worker_wait_settlement(
            entries,
            wait_id=wait_id,
            after_seq=prior_seq,
        ) is not None:
            return
        replies = _worker_wait_replies(
            entries,
            dispatch_id=dispatch_id,
            wait_id=wait_id,
            after_seq=prior_seq,
        )
        if len(replies) == 1:
            reply_seq = replies[0].get("seq")
            if (
                isinstance(reply_seq, int)
                and not isinstance(reply_seq, bool)
                and (wait_id, reply_seq) in consumed_reply_receipts
            ):
                # A watched-tail STEER-REPLY is the durable fallback when the
                # best-effort worker_wait_ended append loses its short lock race.
                context["settled_prior_reply"] = {
                    "wait_id": wait_id,
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
        lock_timeout_secs=lock_timeout_secs,
        validate_existing=reject_unsettled_prior_wait,
    )


def worker_wait_question_marker_text(
    dispatch_id: str,
    question_text: str,
    wait_id: str,
) -> str:
    """Bind one emitted question marker to one durable wait arm."""
    return f"{dispatch_id} — {question_text} [wait-id:{wait_id}]"


def append_worker_wait_ended(
    path: Path,
    arm: dict,
    *,
    decision: str,
    reply_seq: int | None = None,
    lock_timeout_secs: float | None = None,
) -> dict:
    wait_id = str(arm.get("question_id") or "").strip()
    if not wait_id:
        raise ValueError("worker wait arm is missing question_id")
    if decision not in WORKER_WAIT_SETTLED_DECISIONS:
        raise ValueError("worker wait end requires a consumed reply")
    if isinstance(reply_seq, bool) or not isinstance(reply_seq, int) or reply_seq <= 0:
        raise ValueError("worker wait end requires a positive consumed reply_seq")
    context = {"reply_seq": reply_seq}

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
        )
        if len(replies) != 1 or replies[0].get("seq") != reply_seq:
            raise ValueError("worker wait end does not match one consumed typed reply")
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
        context=context,
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
    """Return the newest arm when it is valid, unended, and unexpired.

    The watcher re-validates and clamps the deadline instead of trusting a raw
    mailbox row. A malformed or hand-written arm therefore cannot exempt a
    worker from ordinary idle handling indefinitely.
    """
    now_ns = int((active_monotonic() if now_mono is None else now_mono) * 1_000_000_000)
    maximum_ns = int(MAX_WORKER_WAIT_TIMEOUT_SECS * 1_000_000_000)
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
            or deadline_ns <= started_ns
        ):
            return None
        effective_deadline_ns = min(deadline_ns, started_ns + maximum_ns)
        if now_ns >= effective_deadline_ns:
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
            question_kind not in {"USER-NEED", "USER-CONFIRM"}
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
        )
        if len(replies) > 1:
            return None
        reply = replies[0] if replies else None
        if reply is not None:
            decision = reply.get("decision")
            if question_kind == "USER-CONFIRM" and decision not in USER_CONFIRM_DECISIONS:
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
            "seq": arm_seq,
            "started_awake_mono_ns": started_ns,
            "deadline_awake_mono_ns": effective_deadline_ns,
            "remaining_secs": max(0.0, (effective_deadline_ns - now_ns) / 1_000_000_000),
            "question_kind": question_kind,
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
    timeout_secs: float = DEFAULT_WORKER_WAIT_TIMEOUT_SECS,
    poll_secs: float = DEFAULT_WORKER_WAIT_POLL_SECS,
    notify: Callable[[dict], None] | None = None,
) -> dict:
    """Poll the steer mailbox until backlog/reply or the independent deadline.

    This owns no journal lease and consumes no listener slot. The initial read
    precedes the arm so controller messages already present at arm time are
    reported rather than silently skipped.
    """
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
    question_text = _validate_worker_question(question_kind, question_text)

    def report(event: dict) -> None:
        if notify is not None:
            notify(event)

    deadline = active_monotonic() + timeout_secs

    def deadline_result(wait_id: str | None = None) -> dict:
        result = {"state": "deadline", "entries": []}
        if wait_id:
            result["wait_id"] = wait_id
        report(result)
        return result

    def remaining_secs() -> float:
        return max(0.0, deadline - active_monotonic())

    try:
        pending = pending_worker_entries(
            read_steer_entries(
                path,
                lock_timeout_secs=remaining_secs(),
                quarantine_errors=False,
            ),
            acked_seqs,
        )
    except TimeoutError:
        return deadline_result()
    if pending:
        result = {"state": "messages", "entries": pending, "arm_time_backlog": True}
        report(result)
        if question_kind == "USER-NEED":
            # Open-ended needs may be resolved or redirected by any pending
            # controller steer. Confirmation never is: only a correlated typed
            # reply carrying an explicit decision can settle USER-CONFIRM.
            return result

    remaining = remaining_secs()
    if remaining <= 0:
        return deadline_result()

    try:
        arm = append_worker_wait_started(
            path,
            dispatch_id=dispatch_id,
            timeout_secs=timeout_secs,
            question_kind=question_kind,
            question_text=question_text,
            deadline_mono=deadline,
            lock_timeout_secs=remaining,
            consumed_reply_receipts=consumed_reply_receipts,
        )
    except TimeoutError:
        return deadline_result()
    decision = "error"
    try:
        report(
            {
                "state": "armed",
                "arm": arm,
                "timeout_secs": timeout_secs,
                "question_kind": question_kind,
                "question_marker_text": worker_wait_question_marker_text(
                    dispatch_id,
                    question_text,
                    str(arm["question_id"]),
                ),
            }
        )
        while True:
            remaining = remaining_secs()
            final_deadline_read = remaining <= 0
            try:
                current_entries = read_steer_entries(
                    path,
                    lock_timeout_secs=(
                        min(DEFAULT_WORKER_WAIT_POLL_SECS, poll_secs)
                        if final_deadline_read
                        else remaining
                    ),
                    quarantine_errors=False,
                )
            except TimeoutError:
                decision = "deadline"
                return deadline_result(str(arm["question_id"]))
            replies = _worker_wait_replies(
                current_entries,
                dispatch_id=dispatch_id,
                wait_id=str(arm["question_id"]),
                after_seq=int(arm["seq"]),
            )
            if replies:
                decision = "reply"
                result = {
                    "state": "messages",
                    "entries": replies,
                    "arm_time_backlog": False,
                    "wait_id": arm["question_id"],
                }
                report(result)
                return result
            if final_deadline_read:
                decision = "deadline"
                return deadline_result(str(arm["question_id"]))
            remaining = remaining_secs()
            if remaining <= 0:
                continue
            time.sleep(min(poll_secs, remaining))
    finally:
        if decision == "reply":
            try:
                append_worker_wait_ended(
                    path,
                    arm,
                    decision=decision,
                    reply_seq=int(replies[0]["seq"]),
                    lock_timeout_secs=min(0.05, remaining_secs()),
                )
            except (OSError, RuntimeError, TimeoutError, ValueError):
                # The arm's own deadline is authoritative. Cleanup must never
                # mask an already-reported reply or strand the worker.
                pass


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
            f"{entry.get('direction', TO_WORKER)}\t{entry.get('kind', STEERING_KIND)}"
        )
    return 0
