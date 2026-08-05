#!/usr/bin/env python3
"""Marker → message envelope conversion and dispatch inbox (Track C Phase 0)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_CONTRACT = REPO_ROOT / "docs-private" / "architecture" / "contracts" / "goalflight.message.v1.json"
AGGREGATE_SCHEMA = "goalflight.fleet.register.aggregate.v1"
READ_CURSOR_FILE = ".read-cursor.json"
ACK_CURSOR_FILE = ".ack-cursor.json"
BACKLOG_DIGEST_DIR = "backlog-digests"
DEFAULT_RELAY_ITEM_LIMIT = 20
DEFAULT_RELAY_BYTE_LIMIT = 4096
TASKLESS_TERMINAL_STALE_AFTER = dt.timedelta(hours=24)
PROJECT_MAIL_ALIASES_ENV = "GOALFLIGHT_PROJECT_MAIL_ALIASES"
MIN_DERIVED_PROJECT_ALIAS_LEN = 4

sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat  # noqa: E402
import goalflight_steer_mailbox  # noqa: E402
from goalflight_watch import BLOCKING_TERMINAL_MARKERS, SUCCESS_TERMINAL_MARKERS  # noqa: E402

MARKER_TO_TYPE: dict[str, str] = {
    "STATUS": "status",
    "STEER-ACK": "monitor",
    "USER-NEED": "user_need",
    "USER-CONFIRM": "user_confirm",
    **{kind: "result" for kind in SUCCESS_TERMINAL_MARKERS},
    **{kind: "blocked" for kind in BLOCKING_TERMINAL_MARKERS - {"USER-NEED", "USER-CONFIRM"}},
}

PRIORITY_BY_TYPE: dict[str, str] = {
    "user_need": "urgent",
    "user_confirm": "urgent",
    "blocked": "urgent",
}
# Typed controller channel. The unprefixed values are legacy envelopes that the
# aggregate already accepts; keeping one set prevents delivery and relay drift.
CONTROLLER_CHANNEL_TYPES = frozenset(
    {
        "controller-question",
        "controller-answer",
        "controller-notice",
        "controller-coordination",
        "coordination",
        "notice",
    }
)
CONTROLLER_LISTENER_ESCALATION_TYPES = frozenset({"user_need", "user_confirm", "blocked"})
TASK_STORE_STATUS_NUDGE_KINDS = frozenset({"parallel-ready", "resume-ready", "done-suggest"})
NON_ERROR_UNDELIVERED_STATUSES = frozenset({"terminal_recorded_only", "worker_view_queued"})
CONTROLLER_ADDRESSEE_KIND = "controller"

REQUIRED_ENVELOPE_FIELDS = (
    "schema",
    "schema_version",
    "id",
    "dispatch_id",
    "seq",
    "ts",
    "source",
    "type",
    "payload",
)


class MessageError(Exception):
    pass


def require_positive_int_seq(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MessageError(f"{path}: seq must be an integer >= 1")
    if value < 1:
        raise MessageError(f"{path}: seq must be an integer >= 1")
    return value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def default_messages_dir() -> Path:
    return goalflight_compat.resolve_env_path(
        "GOALFLIGHT_MESSAGES_DIR", Path.home() / ".goal-flight" / "messages"
    )


def default_fleet_dir() -> Path:
    return goalflight_compat.resolve_env_path(
        "GOALFLIGHT_FLEET_DIR", Path.home() / ".goal-flight" / "fleet"
    )


def inbox_path(messages_dir: Path, dispatch_id: str) -> Path:
    return messages_dir / f"{dispatch_id}.jsonl"


def mail_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextlib.contextmanager
def mail_lock(path: Path):
    lock = mail_lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock.open("w", encoding="utf-8") as fh:
        goalflight_compat.flock(fh, goalflight_compat.LOCK_EX)
        try:
            yield
        finally:
            goalflight_compat.flock(fh, goalflight_compat.LOCK_UN)


def read_cursor_path(messages_dir: Path) -> Path:
    return messages_dir / READ_CURSOR_FILE


def ack_cursor_path(messages_dir: Path) -> Path:
    return messages_dir / ACK_CURSOR_FILE


def load_read_cursor(path: Path) -> dict[str, int]:
    """Best-effort cursor load; absent/corrupt state means everything is unseen."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cursor: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        try:
            seq = int(value)
        except (TypeError, ValueError):
            continue
        cursor[key] = max(0, seq)
    return cursor


def write_read_cursor(path: Path, cursor: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    clean = {str(key): max(0, int(value)) for key, value in sorted(cursor.items())}
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(clean, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def advance_read_cursor(
    path: Path,
    advances: dict[str, int] | None = None,
    *,
    max_scan: Callable[[], dict[str, int]] | None = None,
) -> list[dict[str, object]]:
    """Advance cursor entries under lock; never rewind existing last-seen seq."""
    results: list[dict[str, object]] = []
    with mail_lock(path):
        resolved_advances = dict(advances or {})
        if max_scan is not None:
            for key, through in max_scan().items():
                old_target = int(resolved_advances.get(key, 0))
                resolved_advances[key] = max(old_target, int(through))
        cursor = load_read_cursor(path)
        for key, through in sorted(resolved_advances.items()):
            old = int(cursor.get(key, 0))
            new = max(old, int(through))
            cursor[key] = new
            results.append({"inbox": key, "old": old, "new": new, "advanced": new > old})
        write_read_cursor(path, cursor)
    return results


def validate_envelope(envelope: dict, *, path: str = "envelope") -> None:
    if not isinstance(envelope, dict):
        raise MessageError(f"{path}: expected object")
    for field in REQUIRED_ENVELOPE_FIELDS:
        if field not in envelope:
            raise MessageError(f"{path}: missing field: {field}")
    if envelope.get("schema") != "goalflight.message.v1":
        raise MessageError(f"{path}: schema must be goalflight.message.v1")
    if envelope.get("schema_version") != 1:
        raise MessageError(f"{path}: unsupported schema_version")
    require_positive_int_seq(envelope.get("seq"), path=f"{path}.seq")
    source = envelope.get("source")
    if not isinstance(source, dict):
        raise MessageError(f"{path}.source: expected object")
    for key in ("node", "adapter", "transport"):
        if key not in source:
            raise MessageError(f"{path}.source: missing {key}")
    addressee = envelope.get("addressee")
    if addressee is not None:
        if not isinstance(addressee, dict):
            raise MessageError(f"{path}.addressee: expected object")
        if addressee.get("kind") != CONTROLLER_ADDRESSEE_KIND:
            raise MessageError(f"{path}.addressee.kind: unsupported addressee kind")
        label = addressee.get("label")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 64:
            raise MessageError(f"{path}.addressee.label: expected 1..64 non-blank characters")
        project_root = addressee.get("project_root")
        if (
            not isinstance(project_root, str)
            or not project_root.strip()
            or not Path(project_root).is_absolute()
        ):
            raise MessageError(f"{path}.addressee.project_root: expected an absolute path")
        if envelope.get("type") not in CONTROLLER_CHANNEL_TYPES:
            raise MessageError(
                f"{path}.addressee: controller addressing is only valid for controller-channel types"
            )


def controller_addressee(label: str, *, project_root: Path | str) -> dict[str, str]:
    """Build the project-root + stable-name controller address."""
    resolved = str(label or "").strip()
    if not resolved or len(resolved) > 64:
        raise MessageError("controller addressee label must contain 1..64 non-blank characters")
    root = controller_address_project_root(project_root)
    return {
        "kind": CONTROLLER_ADDRESSEE_KIND,
        "label": resolved,
        "project_root": root,
    }


def controller_address_project_root(project_root: Path | str) -> str:
    return str(_canonical_project_root(Path(project_root)))


def controller_addressee_label(envelope: dict) -> str | None:
    addressee = envelope.get("addressee") if isinstance(envelope, dict) else None
    if not isinstance(addressee, dict) or addressee.get("kind") != CONTROLLER_ADDRESSEE_KIND:
        return None
    label = addressee.get("label")
    return label.strip() if isinstance(label, str) and label.strip() else None


def controller_addressee_project_root(envelope: dict) -> str | None:
    addressee = envelope.get("addressee") if isinstance(envelope, dict) else None
    if not isinstance(addressee, dict) or addressee.get("kind") != CONTROLLER_ADDRESSEE_KIND:
        return None
    root = addressee.get("project_root")
    return str(root).strip() if isinstance(root, str) and str(root).strip() else None


def controller_cursor_key(
    label: str,
    dispatch_id: str,
    project_root: str | Path | None = None,
) -> str:
    """Recipient-private cursor key; one controller cannot mark another's mail read."""
    identity = (
        [str(project_root), str(label), str(dispatch_id)]
        if project_root is not None
        else [str(label), str(dispatch_id)]
    )
    return "controller:" + json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def envelope_cursor_key(envelope: dict) -> str:
    dispatch_id = str(envelope.get("dispatch_id") or "")
    label = controller_addressee_label(envelope)
    project_root = controller_addressee_project_root(envelope)
    return (
        controller_cursor_key(label, dispatch_id, project_root)
        if label and project_root
        else dispatch_id
    )


def read_envelopes(path: Path, *, last_n: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    if not path.is_file():
        # Non-regular inbox (FIFO/device): read_text()'s open() would block forever.
        # is_file() is a non-blocking stat; treat a non-regular inbox as empty so no
        # reader (build_aggregate, next_seq, the watcher bridge) can hang on it.
        return []
    envelopes: list[dict] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            envelope = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise MessageError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        validate_envelope(envelope, path=f"{path}:{line_no}")
        envelopes.append(envelope)
    if last_n is not None and last_n >= 0:
        return envelopes[-last_n:] if last_n else []
    return envelopes


def serialize_envelope_line(envelope: dict) -> str:
    """Canonical single-line JSON bytes for register append (file or MCP)."""
    validate_envelope(envelope)
    return json.dumps(envelope, separators=(",", ":")) + "\n"


def append_envelope(path: Path, envelope: dict) -> None:
    line = serialize_envelope_line(envelope)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def rewrite_envelopes(path: Path, envelopes: list[dict]) -> None:
    lines = [serialize_envelope_line(envelope) for envelope in envelopes]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.writelines(lines)
    tmp.replace(path)


def post_message(
    *,
    dispatch_id: str,
    msg_type: str,
    payload: dict,
    messages_dir: Path,
    source: dict | None = None,
    seq: int | None = None,
    priority: str | None = None,
    fleet_dir: Path | None = None,
    update_aggregate: bool = False,
    deliver_to_worker: bool = False,
    retain_terminal_worker_view: bool = False,
    addressee: dict | None = None,
) -> dict:
    """Append one goalflight.message.v1 envelope; shared by CLI, MCP, and tests."""
    if not isinstance(payload, dict):
        raise MessageError("payload must be an object")
    path = inbox_path(messages_dir, dispatch_id)
    if path.exists() and not path.is_file():
        # Fail CLOSED on a non-regular inbox (FIFO/device) before any open():
        # open("a") below would block the caller forever. Centralised here so
        # CLI / MCP / direct writers are all protected, not just the watcher bridge.
        raise MessageError(f"{path}: inbox is not a regular file; refusing to write")
    provided_seq = require_positive_int_seq(seq, path="seq") if seq is not None else None
    base_source = {
        "node": "local",
        "adapter": "unknown",
        "transport": "controller",
    }
    if source:
        base_source.update(source)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with mail_lock(path):
        resolved_seq = require_positive_int_seq(
            provided_seq if provided_seq is not None else next_seq(path),
            path="seq",
        )
        envelope = {
            "schema": "goalflight.message.v1",
            "schema_version": 1,
            "id": str(uuid.uuid4()),
            "dispatch_id": dispatch_id,
            "seq": resolved_seq,
            "ts": utc_now(),
            "source": base_source,
            "type": msg_type,
            "priority": priority or PRIORITY_BY_TYPE.get(msg_type, "normal"),
            "payload": payload,
        }
        if addressee is not None:
            envelope["addressee"] = dict(addressee)
        line = serialize_envelope_line(envelope)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        # The messages lock orders both the canonical record and its materialized
        # worker view. Releasing it between the two writes lets concurrent posts
        # assign message seq 1/2 but append steer entries in the order 2/1.
        delivery = (
            _deliver_message_to_worker(
                dispatch_id,
                envelope,
                retain_terminal_worker_view=retain_terminal_worker_view,
            )
            if deliver_to_worker
            else {
                "requested": False,
                "delivered": False,
                "worker_view_written": False,
                "status": "record_only",
                "detail": "message recorded; worker delivery was not requested",
            }
        )
    if update_aggregate and fleet_dir is not None:
        refresh_aggregate(fleet_dir, messages_dir=messages_dir)
    return {
        "envelope": envelope,
        "line": line,
        "path": str(path),
        "recorded": True,
        "delivery": delivery,
    }


def _dispatch_record(dispatch_id: str) -> tuple[dict | None, str | None]:
    try:
        import goalflight_ledger  # type: ignore

        return (
            next(
                (record for record in goalflight_ledger.read_records() if record.get("dispatch_id") == dispatch_id),
                None,
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a delivery failure
        return None, f"{type(exc).__name__}: {exc}"


def _deliver_message_to_worker(
    dispatch_id: str,
    envelope: dict,
    *,
    retain_terminal_worker_view: bool = False,
) -> dict:
    record, lookup_error = _dispatch_record(dispatch_id)
    if lookup_error is not None:
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": False,
            "status": "worker_delivery_failed",
            "detail": f"message recorded but dispatch lookup failed: {lookup_error}",
        }
    if record is None:
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": False,
            "status": "recorded_only_no_dispatch",
            "detail": "message recorded; no matching dispatch record, so no worker delivery was attempted",
        }
    try:
        import goalflight_ledger  # type: ignore

        classification = goalflight_ledger.classify(record)
    except Exception as exc:  # noqa: BLE001 - surfaced as a delivery failure
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": False,
            "status": "worker_delivery_failed",
            "detail": f"message recorded but worker liveness classification failed: {type(exc).__name__}: {exc}",
        }
    # A detached worker intentionally survives controller death. Ledger
    # classification verifies its PID+identity and is authoritative over that
    # otherwise-terminal state label; all other terminal records remain record-only.
    detached_live = classification == "expected_live" and bool(record.get("detached")) and (
        record.get("state") == "controller_dead"
        or (
            record.get("state") == "orphaned"
            and (record.get("reason") or record.get("error")) == "controller_dead"
        )
    )
    if _record_is_terminal(record) and not detached_live:
        state = record.get("terminal_state") or record.get("state") or "terminal"
        if not retain_terminal_worker_view:
            return {
                "requested": True,
                "delivered": False,
                "worker_view_written": False,
                "status": "terminal_recorded_only",
                "dispatch_state": str(state),
                "detail": "message recorded for terminal dispatch; no worker will read it",
            }
        # Legacy `dispatch steer` keeps terminal rows in the view because a
        # restarted ACP session can reuse the dispatch id and must reject stale
        # confirmation replies by sequence/generation rather than lose them.
        try:
            steer_path, steer_entry = goalflight_steer_mailbox.append_message_view(dispatch_id, envelope)
        except (OSError, ValueError) as exc:
            return {
                "requested": True,
                "delivered": False,
                "worker_view_written": False,
                "status": "terminal_recorded_only",
                "dispatch_state": str(state),
                "detail": (
                    "message recorded for terminal dispatch; no worker will read it, and its retained "
                    f"worker view could not be written: {type(exc).__name__}: {exc}"
                ),
            }
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": True,
            "status": "terminal_recorded_only",
            "dispatch_state": str(state),
            "steer_path": str(steer_path),
            "steer_seq": steer_entry["seq"],
            "steer_entry": steer_entry,
            "detail": "message recorded for terminal dispatch and retained in its worker view; no current worker will read it",
        }
    if classification not in {"expected_live", "queued_capacity"}:
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": False,
            "status": "worker_unavailable",
            "dispatch_classification": classification,
            "detail": f"message recorded; dispatch is {classification}, so no worker delivery was attempted",
        }
    try:
        steer_path, steer_entry = goalflight_steer_mailbox.append_message_view(dispatch_id, envelope)
    except (OSError, ValueError) as exc:
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": False,
            "status": "worker_delivery_failed",
            "detail": f"message recorded but worker delivery failed: {type(exc).__name__}: {exc}",
        }
    if classification == "queued_capacity":
        return {
            "requested": True,
            "delivered": False,
            "worker_view_written": True,
            "status": "worker_view_queued",
            "dispatch_classification": classification,
            "steer_path": str(steer_path),
            "steer_seq": steer_entry["seq"],
            "steer_entry": steer_entry,
            "detail": "message recorded and queued in the worker-visible steer mailbox; no worker is running yet",
        }
    return {
        "requested": True,
        "delivered": True,
        "worker_view_written": True,
        "status": "worker_view_written",
        "dispatch_classification": classification,
        "steer_path": str(steer_path),
        "steer_seq": steer_entry["seq"],
        "steer_entry": steer_entry,
        "detail": "message recorded and written to the worker-visible steer mailbox",
    }


def _controller_delivery_requested(dispatch_id: str, msg_type: str) -> bool:
    """A worker posting to its own inbox is worker→controller sideband."""
    return os.environ.get("GOALFLIGHT_DISPATCH_ID") != dispatch_id and msg_type in CONTROLLER_CHANNEL_TYPES


def _controller_sender_session_id(dispatch_id: str) -> str | None:
    """Return the declared live controller that authored an outbound steer.

    Missing or ambiguous identity stays ``None``. Wake filtering treats that as
    unknown correspondence and wakes; it must never guess an author and silence
    mail that may have come from another controller.
    """
    record, _classification = _dispatch_record(dispatch_id)
    project_root = (record or {}).get("project_root")
    if not project_root:
        return None
    try:
        import goalflight_session_status  # type: ignore

        label = goalflight_session_status.resolve_controller_label()
        pid = goalflight_session_status.resolve_controller_pid()
        if label is None or pid is None:
            return None
        session = goalflight_session_status.live_session(
            Path(str(project_root)),
            label=label,
            pid=pid,
        )
    except Exception:
        return None
    if not session or session.get("conflicting_beacons") or not session.get("id"):
        return None
    return str(session["id"])


def post_result_is_error(result: dict) -> bool:
    delivery = result["delivery"]
    return bool(
        delivery["requested"]
        and not delivery["delivered"]
        and delivery["status"] not in NON_ERROR_UNDELIVERED_STATUSES
    )


def post_controller_steer(dispatch_id: str, text: str) -> dict:
    """Record a legacy steer command, then materialize its worker-visible view."""
    source = {"node": "local", "adapter": "goalflight-dispatch", "transport": "steer"}
    sender_session_id = _controller_sender_session_id(dispatch_id)
    if sender_session_id is not None:
        source["controller_session_id"] = sender_session_id
    return post_message(
        dispatch_id=dispatch_id,
        msg_type="controller-notice",
        payload={"text": text},
        messages_dir=default_messages_dir(),
        source=source,
        deliver_to_worker=True,
        retain_terminal_worker_view=True,
    )


MCP_TOOL_POST_MESSAGE = "goalflight_post_message"


def goalflight_post_message_tool(
    arguments: dict,
    *,
    messages_dir: Path,
    fleet_dir: Path | None = None,
    refresh_aggregate: bool = False,
) -> dict:
    """MCP tool handler — must write identical bytes as file append."""
    if not isinstance(arguments, dict):
        raise MessageError("arguments must be an object")
    dispatch_id = arguments.get("dispatch_id")
    msg_type = arguments.get("type")
    payload = arguments.get("payload")
    if not dispatch_id or not msg_type:
        raise MessageError("dispatch_id and type are required")
    if payload is None:
        payload = {}
    source = arguments.get("source")
    if source is not None and not isinstance(source, dict):
        raise MessageError("source must be an object when provided")
    return post_message(
        dispatch_id=str(dispatch_id),
        msg_type=str(msg_type),
        payload=payload,
        messages_dir=messages_dir,
        source=source,
        seq=arguments.get("seq"),
        priority=arguments.get("priority"),
        addressee=arguments.get("addressee"),
        fleet_dir=fleet_dir,
        update_aggregate=refresh_aggregate,
        deliver_to_worker=(
            arguments.get("addressee") is None
            and _controller_delivery_requested(str(dispatch_id), str(msg_type))
        ),
    )


def marker_type(marker_kind: str) -> str:
    return MARKER_TO_TYPE.get(marker_kind, "monitor")


def marker_payload(marker_kind: str, text: str) -> dict:
    if marker_kind == "COMPLETE":
        return {"complete": True, "text": text}
    if marker_kind in MARKER_TO_TYPE:
        return {"text": text}
    return {"unknown_marker": marker_kind, "text": text}


def markers_to_envelopes(
    markers: dict[str, list[str]],
    *,
    dispatch_id: str,
    seq_start: int = 1,
    source: dict | None = None,
    ts: str | None = None,
) -> list[dict]:
    """Convert extract_markers() output into goalflight.message.v1 envelopes."""
    base_source = {
        "node": "local",
        "adapter": "unknown",
        "transport": "tail_file",
    }
    if source:
        base_source.update(source)
    envelopes: list[dict] = []
    seq = seq_start
    stamp = ts or utc_now()
    for kind, values in markers.items():
        msg_type = marker_type(kind)
        for value in values:
            envelopes.append(
                {
                    "schema": "goalflight.message.v1",
                    "schema_version": 1,
                    "id": str(uuid.uuid4()),
                    "dispatch_id": dispatch_id,
                    "seq": seq,
                    "ts": stamp,
                    "source": dict(base_source),
                    "type": msg_type,
                    "priority": PRIORITY_BY_TYPE.get(msg_type, "normal"),
                    "payload": marker_payload(kind, value),
                }
            )
            seq += 1
    return envelopes


def markers_text_to_envelopes(text: str, *, dispatch_id: str, **kwargs) -> list[dict]:
    from acp_runner import extract_markers

    return markers_to_envelopes(extract_markers(text), dispatch_id=dispatch_id, **kwargs)


def _dispatch_complete(envelopes: list[dict]) -> bool:
    for env in envelopes:
        if env.get("type") == "result" and env.get("payload", {}).get("complete"):
            return True
    return False


def _open_user_needs(envelopes: list[dict], *, acked_through: int = 0) -> list[dict]:
    if _dispatch_complete(envelopes):
        return []
    open_items: list[dict] = []
    for env in envelopes:
        if (
            env.get("type") in {"user_need", "user_confirm", "blocked"}
            and int(env.get("seq", 0)) > acked_through
        ):
            payload = env.get("payload", {}) or {}
            open_items.append(
                {
                    "dispatch_id": env["dispatch_id"],
                    "seq": env["seq"],
                    "type": env["type"],
                    "nudge_kind": payload.get("nudge_kind"),
                    "ts": env["ts"],
                    "text": payload.get("text", ""),
                    "payload": payload,
                }
            )
    return open_items


def _open_controller_channel(
    envelopes: list[dict],
    *,
    acked_through: int = 0,
    ack_cursor: dict[str, int] | None = None,
    controller_label: str | None = None,
) -> list[dict]:
    """Unacked controller-addressed messages.

    Deliberately NOT gated on _dispatch_complete the way _open_user_needs is: a
    worker's need dies with its dispatch, but a message a peer controller wrote
    is still worth reading after the dispatch that carried it has finished.
    """
    open_items: list[dict] = []
    for env in envelopes:
        envelope_acked_through = acked_through
        addressee_label = controller_addressee_label(env)
        addressee_root = controller_addressee_project_root(env)
        if ack_cursor is not None and addressee_label and controller_label == addressee_label:
            envelope_acked_through = int(
                ack_cursor.get(
                    controller_cursor_key(
                        addressee_label,
                        str(env.get("dispatch_id") or ""),
                        addressee_root,
                    ),
                    0,
                )
            )
        if (
            env.get("type") in CONTROLLER_CHANNEL_TYPES
            and int(env.get("seq", 0)) > envelope_acked_through
        ):
            payload = env.get("payload", {}) or {}
            open_items.append(
                {
                    "dispatch_id": env["dispatch_id"],
                    "seq": env["seq"],
                    "type": env["type"],
                    "nudge_kind": payload.get("nudge_kind"),
                    "ts": env["ts"],
                    "text": payload.get("text", ""),
                    "payload": payload,
                    "addressee": env.get("addressee"),
                }
            )
    return open_items


def _open_controller_advisories(envelopes: list[dict], *, acked_through: int = 0) -> list[dict]:
    if _dispatch_complete(envelopes):
        return []
    open_items: list[dict] = []
    for env in envelopes:
        if (
            env.get("dispatch_id") == "controller-quota-advisory"
            and env.get("type") == "advisory"
            and int(env.get("seq", 0)) > acked_through
        ):
            open_items.append(
                {
                    "dispatch_id": env["dispatch_id"],
                    "seq": env["seq"],
                    "type": env["type"],
                    "ts": env["ts"],
                    "text": env.get("payload", {}).get("text", ""),
                }
            )
    return open_items


def _last_steering(envelopes_by_dispatch: dict[str, list[dict]]) -> dict | None:
    latest: dict | None = None
    for envelopes in envelopes_by_dispatch.values():
        for env in envelopes:
            if env.get("type") != "steering":
                continue
            if latest is None or env["seq"] >= latest.get("seq", 0):
                latest = {
                    "dispatch_id": env["dispatch_id"],
                    "seq": env["seq"],
                    "ts": env["ts"],
                    "payload": env.get("payload", {}),
                }
    return latest


def collect_inbox_paths(
    messages_dir: Path,
    fleet_dir: Path | None = None,
    *,
    dispatch_ids: set[str] | None = None,
) -> list[Path]:
    # Only REGULAR files are inbox candidates. A non-regular `*.jsonl` entry (a
    # FIFO/device, accidental or hostile) would block a later read_text() open()
    # indefinitely — which on the read-side status mail check would HANG status
    # before its fail-open guard could fire. `is_file()` is a non-blocking stat()
    # (open() is what blocks on a FIFO), so this filter is safe and cheap.
    def _want(stem: str) -> bool:
        return dispatch_ids is None or stem in dispatch_ids or stem == "controller-quota-advisory"

    paths: dict[str, Path] = {}
    if messages_dir.is_dir():
        for path in sorted(messages_dir.glob("*.jsonl")):
            if path.is_file() and _want(path.stem):
                paths[path.stem] = path
    if fleet_dir is not None:
        register_dir = fleet_dir / "register" / "dispatches"
        if register_dir.is_dir():
            for path in sorted(register_dir.glob("*.jsonl")):
                if path.is_file() and _want(path.stem):
                    paths[path.stem] = path
    return list(paths.values())


def max_seq_by_inbox(
    *,
    messages_dir: Path,
    fleet_dir: Path | None = None,
    dispatch_ids: set[str] | None = None,
) -> dict[str, int]:
    maxes: dict[str, int] = {}
    for path in collect_inbox_paths(messages_dir, fleet_dir, dispatch_ids=dispatch_ids):
        try:
            envelopes = read_envelopes(path)
        except MessageError:
            continue
        maxes[path.stem] = max((int(env.get("seq", 0)) for env in envelopes), default=0)
    return maxes


def unseen_envelopes_for_paths(
    paths: list[Path],
    *,
    cursor: dict[str, int],
    last_n: int | None = None,
    tolerate_errors: bool = False,
    envelope_filter: Callable[[dict], bool] | None = None,
    cursor_key: Callable[[dict], str] | None = None,
) -> tuple[list[dict], dict[str, int], dict[str, int]]:
    shown: list[dict] = []
    counts: dict[str, int] = {}
    ack_advances: dict[str, int] = {}
    for path in paths:
        key = path.stem
        try:
            envelopes = read_envelopes(path)
        except MessageError:
            if tolerate_errors:
                continue
            raise
        if envelope_filter is not None:
            envelopes = [env for env in envelopes if envelope_filter(env)]
        key_for = cursor_key or (lambda _env: key)
        unseen = [
            env
            for env in envelopes
            if int(env.get("seq", 0)) > int(cursor.get(key_for(env), 0))
        ]
        counts[key] = len(unseen)
        if last_n is not None and last_n >= 0:
            unseen = unseen[-last_n:] if last_n else []
        shown.extend(unseen)
        for env in unseen:
            cursor_name = key_for(env)
            ack_advances[cursor_name] = max(
                int(ack_advances.get(cursor_name, 0)),
                int(env.get("seq", 0)),
            )
    return shown, counts, ack_advances


def format_unseen_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "unseen counts: none"
    return "unseen counts: " + " ".join(
        f"{sanitize_display(key)}={value}" for key, value in sorted(counts.items())
    )


def print_cursor_advances(results: list[dict[str, object]]) -> None:
    if not results:
        print("mark-read: no inboxes")
        return
    for result in results:
        status = "advanced" if result["advanced"] else "unchanged"
        print(f"mark-read: {result['inbox']} {result['old']}->{result['new']} ({status})")


def warn_cursor_not_advanced(exc: BaseException) -> None:
    print(f"WARNING: cursor not advanced: {type(exc).__name__}: {exc}", file=sys.stderr)


def build_aggregate(
    *,
    messages_dir: Path,
    fleet_dir: Path | None = None,
    dispatch_ids: set[str] | None = None,
    envelope_filter: Callable[[dict], bool] | None = None,
    controller_label: str | None = None,
) -> dict:
    envelopes_by_dispatch: dict[str, list[dict]] = {}
    ack_cursor = load_read_cursor(ack_cursor_path(messages_dir))
    for path in collect_inbox_paths(messages_dir, fleet_dir, dispatch_ids=dispatch_ids):
        try:
            envelopes = read_envelopes(path)
            if envelope_filter is not None:
                envelopes = [envelope for envelope in envelopes if envelope_filter(envelope)]
            envelopes_by_dispatch[path.stem] = envelopes
        except MessageError:
            # One malformed/unreadable inbox must NOT suppress everyone else's mail
            # (a scoped status reads only its own inbox, but be tolerant regardless).
            continue

    open_user_needs: list[dict] = []
    open_advisories: list[dict] = []
    open_controller_channel: list[dict] = []
    active_dispatches: list[str] = []
    for dispatch_id, envelopes in sorted(envelopes_by_dispatch.items()):
        if not envelopes:
            continue
        if not _dispatch_complete(envelopes):
            active_dispatches.append(dispatch_id)
        acked_through = int(ack_cursor.get(dispatch_id, 0))
        open_user_needs.extend(_open_user_needs(envelopes, acked_through=acked_through))
        open_advisories.extend(
            _open_controller_advisories(envelopes, acked_through=acked_through)
        )
        open_controller_channel.extend(
            _open_controller_channel(
                envelopes,
                acked_through=acked_through,
                ack_cursor=ack_cursor,
                controller_label=controller_label,
            )
        )

    return {
        "schema": AGGREGATE_SCHEMA,
        "schema_version": 1,
        "min_reader_version": 1,
        "updated_at": utc_now(),
        "open_user_needs": open_user_needs,
        "open_advisories": open_advisories,
        "open_controller_channel": open_controller_channel,
        "active_dispatches": active_dispatches,
        "last_steering": _last_steering(envelopes_by_dispatch),
    }


def refresh_aggregate(
    fleet_dir: Path,
    *,
    messages_dir: Path | None = None,
) -> dict:
    messages_dir = messages_dir or default_messages_dir()
    aggregate = build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
    out_path = fleet_dir / "register" / "aggregate.json"
    out_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(aggregate, indent=2) + "\n")
    tmp.replace(out_path)
    return aggregate


def cmd_from_text(args: argparse.Namespace) -> int:
    text = Path(args.text_file).read_text() if args.text_file else sys.stdin.read()
    envelopes = markers_text_to_envelopes(
        text,
        dispatch_id=args.dispatch_id,
        source={
            "node": args.node,
            "adapter": args.adapter,
            "transport": args.transport,
        },
    )
    if args.json:
        print(json.dumps(envelopes, indent=2))
    else:
        for env in envelopes:
            print(json.dumps(env))
    return 0


def cmd_append(args: argparse.Namespace) -> int:
    if args.envelope_file:
        envelope = json.loads(Path(args.envelope_file).read_text())
    else:
        envelope = json.loads(sys.stdin.read())
    path = inbox_path(args.messages_dir, args.dispatch_id)
    append_envelope(path, envelope)
    if args.refresh_aggregate:
        refresh_aggregate(args.fleet_dir, messages_dir=args.messages_dir)
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    payload = json.loads(args.payload) if args.payload else {"text": args.text or ""}
    if getattr(args, "subject", None) and isinstance(payload, dict):
        payload.setdefault("subject", args.subject)
    source = {
        "node": args.node,
        "adapter": args.adapter,
        "transport": args.transport,
    }
    addressee = None
    if getattr(args, "to_controller", None):
        addressed_root = getattr(args, "controller_project_root", None) or _current_project_root()
        if addressed_root is None:
            print(
                "post: --to-controller requires --controller-project-root outside a git project",
                file=sys.stderr,
            )
            return 2
        addressee = controller_addressee(
            args.to_controller,
            project_root=Path(addressed_root),
        )
    result = post_message(
        dispatch_id=args.dispatch_id,
        msg_type=args.type,
        payload=payload,
        messages_dir=args.messages_dir,
        source=source,
        addressee=addressee,
        fleet_dir=args.fleet_dir,
        update_aggregate=args.refresh_aggregate,
        deliver_to_worker=(
            addressee is None and _controller_delivery_requested(args.dispatch_id, args.type)
        ),
    )
    print(json.dumps(result, indent=2 if args.json else None))
    delivery = result["delivery"]
    if post_result_is_error(result):
        print(delivery["detail"], file=sys.stderr)
        return 1
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    if args.ack and not args.unseen:
        print("read --ack requires --unseen", file=sys.stderr)
        return 2
    path = inbox_path(args.messages_dir, args.dispatch_id)
    if args.unseen:
        cursor_path = read_cursor_path(args.messages_dir)
        cursor = load_read_cursor(cursor_path)
        envelopes, counts, ack_advances = unseen_envelopes_for_paths(
            [path],
            cursor=cursor,
            last_n=args.last,
        )
        print(json.dumps(envelopes, indent=2 if args.json else None))
        print(format_unseen_counts(counts))
        if args.ack:
            try:
                advance_read_cursor(cursor_path, ack_advances)
            except (OSError, ValueError, TypeError) as exc:
                warn_cursor_not_advanced(exc)
                return 1
        return 0
    envelopes = read_envelopes(path, last_n=args.last)
    print(json.dumps(envelopes, indent=2 if args.json else None))
    return 0


def cmd_mark_read(args: argparse.Namespace) -> int:
    if args.all and args.dispatch_id:
        print("mark-read: --all cannot be combined with --dispatch-id", file=sys.stderr)
        return 2
    if args.all and args.through is not None:
        print("mark-read: --through requires --dispatch-id", file=sys.stderr)
        return 2
    if not args.all and not args.dispatch_id:
        print("mark-read: provide --dispatch-id or --all", file=sys.stderr)
        return 2

    dispatch_id = str(args.dispatch_id) if args.dispatch_id else None
    scan: Callable[[], dict[str, int]] | None = None
    if args.all:
        advances = {}

        def scan() -> dict[str, int]:
            return max_seq_by_inbox(messages_dir=args.messages_dir, fleet_dir=args.fleet_dir)

    elif args.through is not None:
        advances = {str(dispatch_id): args.through}
    else:
        advances = {}

        def scan() -> dict[str, int]:
            current = max_seq_by_inbox(
                messages_dir=args.messages_dir,
                fleet_dir=args.fleet_dir,
                dispatch_ids={str(dispatch_id)},
            )
            return {str(dispatch_id): current.get(str(dispatch_id), 0)}

    try:
        results = advance_read_cursor(read_cursor_path(args.messages_dir), advances, max_scan=scan)
    except (OSError, ValueError, TypeError) as exc:
        warn_cursor_not_advanced(exc)
        return 1
    print_cursor_advances(results)
    return 0


def format_controller_relay(aggregate: dict) -> str | None:
    """One-line summary for orchestrator host when open user_needs exist."""
    needs = aggregate.get("open_user_needs") or []
    if not needs:
        return None
    parts: list[str] = []
    for item in needs:
        dispatch_id = sanitize_display(item.get("dispatch_id") or "?")
        kind = sanitize_display(item.get("type") or "user_need")
        text = sanitize_display(item.get("text") or "", limit=120)
        parts.append(f"[{dispatch_id}] {kind}: {text}")
    return "USER-NEED relay: " + " | ".join(parts)


def format_bounded_relay(
    items: list[dict],
    *,
    item_limit: int = DEFAULT_RELAY_ITEM_LIMIT,
    byte_limit: int = DEFAULT_RELAY_BYTE_LIMIT,
) -> str | None:
    """Newest-first one-line relay with a hard UTF-8 output budget."""
    if not items:
        return None
    ordered = sorted(
        items,
        key=lambda item: (
            str(item.get("ts") or ""),
            int(item.get("seq") or 0),
            str(item.get("dispatch_id") or ""),
        ),
        reverse=True,
    )
    selected: list[str] = []
    total = len(ordered)
    for item in ordered:
        if len(selected) >= item_limit:
            break
        line = (
            f"[{sanitize_display(item.get('dispatch_id') or '', limit=64)}] "
            f"{sanitize_display(item.get('type') or 'user_need', limit=32)}: "
            f"{sanitize_display(item.get('text') or '', limit=200)}"
        )
        candidate = [*selected, line]
        omitted = total - len(candidate)
        if omitted:
            candidate.append(f"(+{omitted} more open unread item(s) elided)")
        rendered = "\n".join(candidate) + "\n"
        if len(rendered.encode("utf-8")) > byte_limit:
            break
        selected.append(line)
    omitted = total - len(selected)
    if omitted:
        selected.append(f"(+{omitted} more open unread item(s) elided)")
    return "\n".join(selected)


def _task_store_dispatch_id(
    project_root: Path,
    *,
    canonical_project_root: Path | None = None,
) -> str | None:
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import goalflight_task  # type: ignore

        # Resolve the CANONICAL project root (git common-dir parent) exactly
        # like the nudge WRITER does — a raw worktree path hashes to a
        # different slug and the reader would silently watch the wrong inbox
        # (live consumption-gap regression caught 2026-07-02). Anchored git
        # discovery covers ANY linked worktree, not just managed ones.
        root = (
            Path(canonical_project_root)
            if canonical_project_root is not None
            else _canonical_project_root(project_root)
        )
        return goalflight_task._next_nudge_dispatch_id(root)
    except Exception:
        return None


def _canonical_project_root(project_root: Path) -> Path:
    """Resolve linked worktrees to the main repository root when possible."""
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            common = Path(result.stdout.strip())
            if not common.is_absolute():
                common = (root / common).resolve()
            return common.parent
    except Exception:
        pass
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import goalflight_task  # type: ignore

        return Path(goalflight_task.resolve_project_root(str(project_root)))
    except Exception:
        return root


def _normalize_project_mail_alias(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-")


def _project_mailbox_aliases(
    project_root: Path,
    *,
    canonical_project_root: Path | None = None,
) -> set[str]:
    """Address aliases: basename, safe leading segment, and explicit overrides."""
    root = canonical_project_root or _canonical_project_root(project_root)
    basename = _normalize_project_mail_alias(root.name) or "project"
    aliases = {basename}
    leading = basename.split("-", 1)[0]
    if leading != basename and len(leading) >= MIN_DERIVED_PROJECT_ALIAS_LEN:
        aliases.add(leading)
    for raw in os.environ.get(PROJECT_MAIL_ALIASES_ENV, "").split(","):
        alias = _normalize_project_mail_alias(raw)
        if alias:
            aliases.add(alias)
    return aliases


def _dispatch_id_addresses_project(dispatch_id: str, project_aliases: set[str]) -> bool:
    """Whether an inbox id carries one of this project's address aliases.

    Addressing rule: an alias must be the whole inbox id, its leading
    ``<alias>-`` token, or its trailing ``-<alias>`` token. Aliases comprise the
    canonical repository basename, a derived leading hyphen segment only when
    it is at least four characters, and optional comma-separated
    ``GOALFLIGHT_PROJECT_MAIL_ALIASES`` overrides. Edge matching supports both
    ``pm2-controller-note`` and ``controller-note-pm2`` without treating an
    arbitrary substring as an address. We use inbox ids rather than envelope
    bodies so scoped readers select paths before opening them; this preserves
    the no-cross-project-flood and corrupt-unrelated-inbox guarantees.
    """
    candidate = dispatch_id.casefold()
    aliases = {alias.casefold() for alias in project_aliases if alias}
    return any(
        candidate == alias
        or candidate.startswith(f"{alias}-")
        or candidate.endswith(f"-{alias}")
        for alias in aliases
    )


def _project_addressed_dispatch_ids(
    project_root: Path,
    *,
    messages_dir: Path,
    fleet_dir: Path | None,
    canonical_project_root: Path | None = None,
) -> set[str]:
    aliases = _project_mailbox_aliases(
        project_root,
        canonical_project_root=canonical_project_root,
    )
    return {
        path.stem
        for path in collect_inbox_paths(messages_dir, fleet_dir)
        if _dispatch_id_addresses_project(path.stem, aliases)
    }


def _owned_with_project_mail(
    owned_dispatch_ids: set[str] | None,
    task_store_project_root: Path | None,
    *,
    messages_dir: Path,
    fleet_dir: Path | None,
    canonical_project_root: Path | None = None,
) -> set[str] | None:
    if owned_dispatch_ids is None:
        return owned_dispatch_ids
    scoped = set(owned_dispatch_ids)
    if task_store_project_root is not None:
        dispatch_id = _task_store_dispatch_id(
            task_store_project_root,
            canonical_project_root=canonical_project_root,
        )
        if dispatch_id:
            scoped.add(dispatch_id)
        scoped.update(
            _project_addressed_dispatch_ids(
                task_store_project_root,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
                canonical_project_root=canonical_project_root,
            )
        )
    return scoped


def _current_project_root() -> Path | None:
    """Resolve the current git root exactly like goalflight_status."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except Exception:
        pass
    return None


def _project_ledger_records(project_root: Path) -> list[dict]:
    try:
        import goalflight_ledger  # type: ignore

        root = str(project_root.resolve())
        return [
            record
            for record in goalflight_ledger.read_records()
            if record.get("project_root") == root
        ]
    except Exception:
        return []


def _verified_controller_identity(
    project_root: Path,
    records: list[dict],
    *,
    owned_dispatch_ids: set[str],
    controller_session_id: str | None = None,
) -> dict[str, object] | None:
    """Resolve the current incarnation, with the durable name as ownership."""
    try:
        import goalflight_session_status as sessions  # type: ignore

        declared_label = sessions.resolve_controller_label()
        declared_pid = sessions.resolve_controller_pid()
        if declared_label is not None or declared_pid is not None:
            if declared_label is None:
                return None
            live = sessions.live_session(
                project_root,
                label=declared_label,
                pid=declared_pid,
            )
            if (
                not isinstance(live, dict)
                or live.get("conflicting_beacons")
                or not live.get("id")
                or (controller_session_id and str(live.get("id")) != controller_session_id)
            ):
                return None
            return {
                "label": declared_label,
                "session_id": str(live["id"]),
                "pid": live.get("pid"),
            }
        label: str | None = None
        if controller_session_id:
            registry = next(
                (
                    record
                    for record in sessions._registered_controller_records(project_root)
                    if str(record.get("id") or "") == str(controller_session_id)
                ),
                None,
            )
            if registry is not None:
                label = str(registry.get("label") or "") or None
        if label is None:
            label = _controller_label_for_owned_dispatches(records, owned_dispatch_ids)
        if label is None:
            return None
        live = sessions.live_session(project_root, label=label)
        if (
            not isinstance(live, dict)
            or live.get("conflicting_beacons")
            or not live.get("id")
            or (
                controller_session_id
                and str(live.get("id") or "") != str(controller_session_id)
            )
        ):
            return None
        return {
            "label": label,
            "session_id": str(live["id"]),
            "pid": live.get("pid"),
        }
    except Exception:
        return None


def _controller_label_for_owned_dispatches(
    records: list[dict],
    owned_dispatch_ids: set[str],
) -> str | None:
    """Resolve one durable owner name even across incarnation succession."""
    if not owned_dispatch_ids:
        return None
    recorded_ids: set[str] = set()
    labels: set[str] = set()
    for record in records:
        dispatch_id = str(record.get("dispatch_id") or "")
        if dispatch_id not in owned_dispatch_ids:
            continue
        recorded_ids.add(dispatch_id)
        label = str(record.get("controller_label") or "").strip()
        if not label:
            return None
        labels.add(label)
    if recorded_ids != owned_dispatch_ids or len(labels) != 1:
        return None
    return next(iter(labels))


def _controller_scope_kind(
    envelope: dict,
    *,
    owned_dispatch_ids: set[str],
    legacy_addressed_dispatch_ids: set[str],
    task_store_dispatch_id: str | None,
    controller_label: str | None,
    controller_project_root: str,
) -> str | None:
    """Single authority for whether one envelope belongs to this controller."""
    dispatch_id = str(envelope.get("dispatch_id") or "")
    addressee_label = controller_addressee_label(envelope)
    if addressee_label is not None:
        addressee_root = controller_addressee_project_root(envelope)
        if (
            controller_label is not None
            and addressee_label == controller_label
            and addressee_root == controller_project_root
        ):
            return "controller"
        return None
    if dispatch_id in owned_dispatch_ids:
        return "worker"
    if dispatch_id == task_store_dispatch_id:
        return "task-store"
    if dispatch_id in legacy_addressed_dispatch_ids:
        return "legacy-controller"
    return None


def _controller_scope_inputs(
    project_root: Path,
    *,
    records: list[dict],
    owned_dispatch_ids: set[str],
    controller_session_id: str | None,
    messages_dir: Path,
    fleet_dir: Path | None,
    canonical_project_root: Path | None = None,
) -> dict[str, object]:
    canonical_project_root = canonical_project_root or _canonical_project_root(project_root)
    known_dispatch_ids = {
        str(record["dispatch_id"])
        for record in records
        if record.get("dispatch_id")
    }
    legacy_addressed_dispatch_ids = _project_addressed_dispatch_ids(
        project_root,
        messages_dir=messages_dir,
        fleet_dir=fleet_dir,
        canonical_project_root=canonical_project_root,
    ) - known_dispatch_ids
    identity = _verified_controller_identity(
        project_root,
        records,
        owned_dispatch_ids=owned_dispatch_ids,
        controller_session_id=controller_session_id,
    )
    owned_label = _controller_label_for_owned_dispatches(records, owned_dispatch_ids)
    if identity is not None and owned_label is not None and identity.get("label") != owned_label:
        identity = None
    return {
        "legacy_addressed_dispatch_ids": legacy_addressed_dispatch_ids,
        "task_store_dispatch_id": _task_store_dispatch_id(
            project_root,
            canonical_project_root=canonical_project_root,
        ),
        "controller_label": str(identity.get("label")) if identity else None,
        "controller_session_id": str(identity.get("session_id")) if identity else controller_session_id,
        "controller_project_root": str(canonical_project_root),
    }


def _resolve_listener_session_id(project_root: Path, explicit_session_id: str | None) -> str:
    if explicit_session_id is not None:
        session_id = str(explicit_session_id).strip()
        if not session_id:
            raise MessageError("--session-id must not be empty")
        try:
            import goalflight_session_status  # type: ignore

            goalflight_session_status.touch_controller_heartbeat_by_session_id(
                project_root,
                session_id,
            )
        except Exception:
            pass
        return session_id
    try:
        import goalflight_session_status  # type: ignore

        session = goalflight_session_status.live_session(project_root)
    except Exception as exc:
        raise MessageError(f"cannot resolve live controller session: {exc}") from exc
    if not session or not session.get("id"):
        raise MessageError("no live controller session; claim one or pass --session-id")
    if session.get("conflicting_beacons"):
        raise MessageError("multiple live controller sessions; pass --session-id")
    try:
        goalflight_session_status.touch_controller_heartbeat(
            project_root,
            str(session.get("label") or ""),
            session_id=str(session["id"]),
            pid=session.get("pid") if isinstance(session.get("pid"), int) else None,
        )
    except Exception:
        pass
    return str(session["id"])


def _controller_wake_event(
    envelope: dict,
    *,
    scope_kind: str,
    controller_session_id: str | None,
) -> dict | None:
    """A new typed result/escalation or controller-channel envelope in this
    controller's scope earns an interrupt unless it is periodic status or provably
    self-authored controller mail; unknown authorship wakes."""
    dispatch_id = str(envelope.get("dispatch_id") or "")
    msg_type = str(envelope.get("type") or "")
    payload = envelope.get("payload") or {}
    if (
        scope_kind == "task-store"
        and msg_type == "user_need"
        and str(payload.get("nudge_kind") or "") in TASK_STORE_STATUS_NUDGE_KINDS
    ):
        return None
    if scope_kind == "worker":
        wakes = msg_type == "result" or msg_type in CONTROLLER_LISTENER_ESCALATION_TYPES
        wakes = wakes or msg_type in CONTROLLER_CHANNEL_TYPES
    else:
        wakes = (
            msg_type in CONTROLLER_LISTENER_ESCALATION_TYPES
            or msg_type in CONTROLLER_CHANNEL_TYPES
        )
    if not wakes:
        return None
    # Direction is authoritative only when the producer proved its session.
    # Unknown/missing authorship wakes. Escalations and worker results also wake
    # regardless of source metadata, so a spoofed/incorrect source can never
    # swallow BLOCKED, USER-NEED, USER-CONFIRM, or terminal worker mail.
    source = envelope.get("source") or {}
    source_session_id = str(source.get("controller_session_id") or "")
    if (
        msg_type in CONTROLLER_CHANNEL_TYPES
        and controller_session_id
        and source_session_id == controller_session_id
    ):
        return None
    return {
        "dispatch_id": dispatch_id,
        "type": msg_type,
        "seq": envelope.get("seq"),
        "ts": envelope.get("ts"),
        "text": sanitize_display(payload.get("text") or "", limit=120),
    }


def _controller_session_for_owned_dispatches(
    records: list[dict],
    owned_dispatch_ids: set[str],
) -> str | None:
    """Resolve one proven owner only when every requested dispatch agrees."""
    if not owned_dispatch_ids:
        return None
    recorded_ids: set[str] = set()
    session_ids: set[str] = set()
    for record in records:
        dispatch_id = str(record.get("dispatch_id") or "")
        if dispatch_id not in owned_dispatch_ids:
            continue
        recorded_ids.add(dispatch_id)
        session_id = str(record.get("controller_session_id") or "")
        if not session_id:
            return None
        session_ids.add(session_id)
    if recorded_ids != owned_dispatch_ids or len(session_ids) != 1:
        return None
    return next(iter(session_ids))


def controller_wake_watermark(
    *,
    project_root: Path,
    owned_dispatch_ids: set[str] | None = None,
    controller_session_id: str | None = None,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
) -> dict[tuple[str, object], dict]:
    """Wakeable mail for one controller, separate from unread/display state.

    When only ``controller_session_id`` is supplied, ledger ownership is
    recomputed on every call so listener dispatches created after its baseline
    join automatically. ``--wait`` supplies its exact dispatch ids and the same
    function derives their common owner when the ledger proves one.
    """
    resolved_messages_dir = messages_dir or default_messages_dir()
    resolved_fleet_dir = fleet_dir if fleet_dir is not None else default_fleet_dir()
    records = _project_ledger_records(project_root)
    if owned_dispatch_ids is None:
        identity = _verified_controller_identity(
            project_root,
            records,
            owned_dispatch_ids=set(),
            controller_session_id=controller_session_id,
        )
        owner_label = str(identity.get("label") or "") if identity else ""
        owned_dispatch_ids = {
            str(record["dispatch_id"])
            for record in records
            if record.get("dispatch_id")
            and (
                (
                    owner_label
                    and str(record.get("controller_label") or "") == owner_label
                )
                or (
                    not owner_label
                    and controller_session_id
                    and not record.get("controller_label")
                    and str(record.get("controller_session_id") or "")
                    == str(controller_session_id)
                )
            )
        }
    else:
        owned_dispatch_ids = {str(dispatch_id) for dispatch_id in owned_dispatch_ids}
    scope_inputs = _controller_scope_inputs(
        project_root,
        records=records,
        owned_dispatch_ids=owned_dispatch_ids,
        controller_session_id=controller_session_id,
        messages_dir=resolved_messages_dir,
        fleet_dir=resolved_fleet_dir,
    )
    legacy_addressed_dispatch_ids = set(scope_inputs["legacy_addressed_dispatch_ids"])
    task_store_dispatch_id = scope_inputs["task_store_dispatch_id"]
    controller_label = scope_inputs["controller_label"]
    controller_project_root = str(scope_inputs["controller_project_root"])
    controller_session_id = str(scope_inputs["controller_session_id"] or "") or None
    candidate_dispatch_ids = owned_dispatch_ids | legacy_addressed_dispatch_ids
    if task_store_dispatch_id:
        candidate_dispatch_ids.add(str(task_store_dispatch_id))

    events: dict[tuple[str, object], dict] = {}
    for path in collect_inbox_paths(
        resolved_messages_dir,
        resolved_fleet_dir,
        dispatch_ids=None if controller_label else candidate_dispatch_ids,
    ):
        try:
            envelopes = read_envelopes(path)
        except MessageError:
            continue
        for envelope in envelopes:
            scope_kind = _controller_scope_kind(
                envelope,
                owned_dispatch_ids=owned_dispatch_ids,
                legacy_addressed_dispatch_ids=legacy_addressed_dispatch_ids,
                task_store_dispatch_id=(str(task_store_dispatch_id) if task_store_dispatch_id else None),
                controller_label=(str(controller_label) if controller_label else None),
                controller_project_root=controller_project_root,
            )
            if scope_kind is None:
                continue
            event = _controller_wake_event(
                envelope,
                scope_kind=scope_kind,
                controller_session_id=controller_session_id,
            )
            if event is not None:
                events[(event["dispatch_id"], event["seq"])] = event
    return events


def controller_listener_watermark(
    *,
    controller_session_id: str,
    project_root: Path,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
) -> dict[tuple[str, object], dict]:
    """Compatibility name for the ownership-keyed listener's shared filter."""
    return controller_wake_watermark(
        controller_session_id=controller_session_id,
        project_root=project_root,
        messages_dir=messages_dir,
        fleet_dir=fleet_dir,
    )


def _project_dispatch_ids(project_root: Path) -> set[str]:
    return {
        str(record["dispatch_id"])
        for record in _project_ledger_records(project_root)
        if record.get("dispatch_id")
    }


def _current_frontier_ids(project_root: Path) -> list[str] | None:
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import goalflight_task  # type: ignore

        return [str(row["id"]) for row in goalflight_task.TaskStore(Path(project_root)).next_frontier()]
    except Exception:
        return None


def _task_items_by_id(project_root: Path) -> dict[str, dict] | None:
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import goalflight_task  # type: ignore

        return {str(row.get("id") or ""): row for row in goalflight_task.list(project_root=Path(project_root))}
    except Exception:
        return None


def _done_reviewed(row: dict | None) -> bool:
    if not row:
        return True
    return row.get("done_reviewed") is True or (row.get("kind") == "decision" and row.get("done") is True)


def _record_task_ids(record: dict) -> list[str]:
    values = record.get("task_ids")
    raw = values if isinstance(values, list) else [values]
    task_ids: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            continue
        for part in value.split(","):
            task_id = part.strip()
            if task_id and task_id not in task_ids:
                task_ids.append(task_id)
    return task_ids


def _record_time(record: dict) -> dt.datetime:
    for key in ("started_at", "updated_at", "ended_at"):
        value = record.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def _record_is_terminal(record: dict) -> bool:
    try:
        import goalflight_dispatch_states as dispatch_states  # type: ignore

        return any(
            dispatch_states.is_terminal_state(record.get(key))
            for key in ("state", "terminal_state")
        )
    except Exception:
        return False


def _stale_dispatch_ids(project_root: Path, open_dispatch_ids: set[str]) -> set[str]:
    """Terminal escalations stale by task lifecycle or task-less expiry."""
    records = _project_ledger_records(project_root)
    rows_by_id = _task_items_by_id(project_root) or {}
    now = dt.datetime.now(dt.timezone.utc)
    unknown_time = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    stale: set[str] = set()
    for record in records:
        dispatch_id = str(record.get("dispatch_id") or "")
        task_ids = _record_task_ids(record)
        if (
            not dispatch_id
            or dispatch_id not in open_dispatch_ids
            or not _record_is_terminal(record)
        ):
            continue
        record_time = _record_time(record)
        if not task_ids:
            if (
                record_time != unknown_time
                and record_time < now - TASKLESS_TERMINAL_STALE_AFTER
            ):
                stale.add(dispatch_id)
            continue
        superseded = {
            task_id
            for other in records
            if str(other.get("dispatch_id") or "") != dispatch_id
            and _record_time(other) > record_time
            for task_id in _record_task_ids(other)
        }
        if all(
            bool((rows_by_id.get(task_id) or {}).get("done")) or task_id in superseded
            for task_id in task_ids
        ):
            stale.add(dispatch_id)
    return stale


def _task_store_nudge_is_current(item: dict, project_root: Path) -> bool:
    payload = item.get("payload") or {}
    nudge_kind = str(payload.get("nudge_kind") or "")
    if nudge_kind == "done-suggest":
        task_ids = [str(value) for value in (payload.get("task_ids") or [])]
        if not task_ids:
            return True
        rows_by_id = _task_items_by_id(project_root)
        if rows_by_id is None:
            return True
        return any(not _done_reviewed(rows_by_id.get(task_id)) for task_id in task_ids)
    if nudge_kind not in {"parallel-ready", "resume-ready"}:
        return True
    frontier_ids = _current_frontier_ids(project_root)
    if frontier_ids is None:
        return True
    payload_ids = [str(value) for value in (payload.get("frontier_ids") or [])]
    if not payload_ids:
        return True
    if nudge_kind == "parallel-ready":
        return len(frontier_ids) >= 2 and frontier_ids == payload_ids
    return bool(frontier_ids) and frontier_ids == payload_ids


def _filter_task_store_nudges(
    items: list[dict],
    task_store_project_root: Path | None,
    *,
    canonical_project_root: Path | None = None,
) -> list[dict]:
    if task_store_project_root is None:
        return items
    dispatch_id = _task_store_dispatch_id(
        task_store_project_root,
        canonical_project_root=canonical_project_root,
    )
    if not dispatch_id:
        return items
    return [
        item
        for item in items
        if str(item.get("dispatch_id") or "") != dispatch_id
        or _task_store_nudge_is_current(
            item,
            canonical_project_root or task_store_project_root,
        )
    ]


def format_mail_notice(count: int) -> str:
    """One body-free notice shared by every controller entry point."""
    return (
        f"{count} new mail; read: "
        "goalflight_messages.py relay --new (--ack to mark read)"
    )


def controller_mail_summary(
    *,
    owned_dispatch_ids: set[str] | None = None,
    task_store_project_root: Path | None = None,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
    unread_only: bool = True,
) -> dict:
    """Structured "you have mail" summary for a controller's status output.

    Builds the inbox aggregate and returns OPEN user-needs (user_need /
    user_confirm / blocked) plus controller quota advisories.

    The mailbox is machine-global (shared across controllers), so when
    ``owned_dispatch_ids`` is provided only needs from THOSE dispatches — the
    controller's own workers — plus ``task_store_project_root``'s pseudo-inbox
    and explicitly project-addressed inbox ids are surfaced; a controller must
    never see another controller's workers' needs. ``None`` means no ownership
    filter (e.g. an all-projects view). Returns ``{}`` when there is nothing to
    show.
    """
    # Read ONLY this controller's own inboxes: an unrelated controller's corrupt or
    # large inbox can then neither suppress (a parse error elsewhere) nor slow this
    # scoped status call. build_aggregate is also per-inbox tolerant as a backstop.
    resolved_messages_dir = messages_dir or default_messages_dir()
    resolved_fleet_dir = fleet_dir if fleet_dir is not None else default_fleet_dir()
    canonical_project_root = (
        _canonical_project_root(task_store_project_root)
        if task_store_project_root is not None
        else None
    )
    controller_label: str | None = None
    envelope_filter: Callable[[dict], bool] | None = None
    scoped_dispatch_ids = _owned_with_project_mail(
        owned_dispatch_ids,
        task_store_project_root,
        messages_dir=resolved_messages_dir,
        fleet_dir=resolved_fleet_dir,
        canonical_project_root=canonical_project_root,
    )
    if owned_dispatch_ids is not None and task_store_project_root is not None:
        records = _project_ledger_records(task_store_project_root)
        scope_inputs = _controller_scope_inputs(
            task_store_project_root,
            records=records,
            owned_dispatch_ids={str(value) for value in owned_dispatch_ids},
            controller_session_id=None,
            messages_dir=resolved_messages_dir,
            fleet_dir=resolved_fleet_dir,
            canonical_project_root=canonical_project_root,
        )
        legacy_addressed_dispatch_ids = set(scope_inputs["legacy_addressed_dispatch_ids"])
        task_store_dispatch_id = scope_inputs["task_store_dispatch_id"]
        controller_label = (
            str(scope_inputs["controller_label"])
            if scope_inputs.get("controller_label")
            else None
        )
        controller_project_root = str(scope_inputs["controller_project_root"])

        def envelope_filter(envelope: dict) -> bool:
            return _controller_scope_kind(
                envelope,
                owned_dispatch_ids={str(value) for value in owned_dispatch_ids},
                legacy_addressed_dispatch_ids=legacy_addressed_dispatch_ids,
                task_store_dispatch_id=(str(task_store_dispatch_id) if task_store_dispatch_id else None),
                controller_label=controller_label,
                controller_project_root=controller_project_root,
            ) is not None

    aggregate = build_aggregate(
        messages_dir=resolved_messages_dir,
        fleet_dir=resolved_fleet_dir,
        dispatch_ids=None if controller_label else scoped_dispatch_ids,
        envelope_filter=envelope_filter,
        controller_label=controller_label,
    )
    needs = list(aggregate.get("open_user_needs") or [])
    needs.extend(aggregate.get("open_advisories") or [])
    # The controller-addressed channel. Without this the summary is only the
    # worker-marker stream, so a peer controller's question or notice was
    # never surfaced to anyone and a human had to carry it between sessions.
    needs.extend(aggregate.get("open_controller_channel") or [])
    if scoped_dispatch_ids is not None and envelope_filter is None:
        needs = [
            n for n in needs
            if str(n.get("dispatch_id") or "") in scoped_dispatch_ids
            or str(n.get("dispatch_id") or "") == "controller-quota-advisory"
        ]
    needs = _filter_task_store_nudges(
        needs,
        task_store_project_root,
        canonical_project_root=canonical_project_root,
    )
    if unread_only:
        read_cursor = load_read_cursor(read_cursor_path(resolved_messages_dir))
        needs = [
            item
            for item in needs
            if not isinstance(item.get("seq"), int)
            or int(item["seq"])
            > int(
                read_cursor.get(
                    controller_cursor_key(
                        str((item.get("addressee") or {}).get("label")),
                        str(item.get("dispatch_id") or ""),
                        str((item.get("addressee") or {}).get("project_root")),
                    )
                    if isinstance(item.get("addressee"), dict)
                    and (item.get("addressee") or {}).get("label")
                    and (item.get("addressee") or {}).get("project_root")
                    else str(item.get("dispatch_id") or ""),
                    0,
                )
            )
        ]
    if not needs:
        return {}
    items = [
        {
            "dispatch_id": str(n.get("dispatch_id") or "?"),
            "type": str(n.get("nudge_kind") or n.get("type") or "user_need"),
            "seq": n.get("seq"),
            "ts": n.get("ts"),
            "text": sanitize_display(n.get("text") or "", limit=120),
            "addressee": n.get("addressee"),
        }
        for n in needs
    ]
    return {"count": len(items), "needs": items, "hint": format_mail_notice(len(items))}


def emit_controller_mail_notice(
    *,
    owned_dispatch_ids: set[str] | None = None,
    project_root: Path | None = None,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
    stream=None,
) -> str | None:
    """Compute and print the shared one-line mail notice, always fail-open."""
    try:
        resolved_owned_dispatch_ids = owned_dispatch_ids
        if resolved_owned_dispatch_ids is None:
            resolved_owned_dispatch_ids = (
                _project_dispatch_ids(_canonical_project_root(project_root))
                if project_root is not None
                else set()
            )
        summary = controller_mail_summary(
            owned_dispatch_ids=resolved_owned_dispatch_ids,
            task_store_project_root=project_root,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        count = int(summary.get("count") or 0)
        if count < 1:
            return None
        notice = format_mail_notice(count)
        # STDERR by default: several callers' stdout is a DATA CONTRACT that
        # other tooling parses (goalflight_task.py `next` yields the task list;
        # `--json` modes emit documents). An advisory line on stdout corrupts
        # those consumers - test_next_frontier caught exactly that. Notices are
        # advice; stdout is data.
        print(notice, file=sys.stderr if stream is None else stream)
        return notice
    except Exception:
        return None


def emit_controller_milestone_notice(
    *,
    project_root: Path | None = None,
    stream=None,
) -> str | None:
    """Print a one-line milestone-sweep notice, but ONLY when a sweep is due.

    Mail gets acted on and milestone sweeps do not, and the reason is reach, not
    discipline: the mail notice is emitted from doctor/gate/messages/status/usage,
    while the milestone signal appeared in `status` alone. A controller running a
    gate or a usage check -- most of a run -- was never told a sweep had come due.

    So it rides the same carriers as mail. It stays silent unless `due`, because a
    line printed every time is a line nobody reads, and it goes to stderr for the
    same reason the mail notice does: several callers' stdout is a data contract.
    Fail-open throughout -- a monitoring nicety must never break a dispatch.
    """
    try:
        import goalflight_milestone

        status = goalflight_milestone.check_status(
            project_root=project_root or _current_project_root() or Path.cwd()
        )
        if not status.get("due"):
            return None
        line = goalflight_milestone.format_line(status)
        notice = f"{line}  # milestone sweep due: see protocols/milestone-review.md"
        print(notice, file=sys.stderr if stream is None else stream)
        return notice
    except Exception:
        return None


def _mail_scope(*, all_projects: bool) -> tuple[Path | None, set[str] | None]:
    if all_projects:
        return None, None
    project_root = _current_project_root()
    if project_root is None:
        raise MessageError("no current git project; pass --all-projects explicitly")
    return project_root, _project_dispatch_ids(project_root)


def _ack_dispatches(
    *,
    messages_dir: Path,
    items: list[dict],
    dispatch_ids: set[str],
) -> tuple[int, int]:
    advances: dict[str, int] = {}
    item_count = 0
    for item in items:
        dispatch_id = str(item.get("dispatch_id") or "")
        if dispatch_id not in dispatch_ids or not isinstance(item.get("seq"), int):
            continue
        advances[dispatch_id] = max(advances.get(dispatch_id, 0), int(item["seq"]))
        item_count += 1
    if advances:
        advance_read_cursor(ack_cursor_path(messages_dir), advances)
    return len(advances), item_count


def cmd_ack(args: argparse.Namespace) -> int:
    if bool(args.dispatch_id) == bool(args.stale):
        print("ack: provide one dispatch id or --stale", file=sys.stderr)
        return 2
    try:
        project_root, owned_dispatch_ids = _mail_scope(all_projects=False)
    except MessageError as exc:
        print(f"ack: {exc}", file=sys.stderr)
        return 2
    assert project_root is not None and owned_dispatch_ids is not None
    scoped_dispatch_ids = _owned_with_project_mail(
        owned_dispatch_ids,
        project_root,
        messages_dir=args.messages_dir,
        fleet_dir=args.fleet_dir,
    ) or set()
    if args.dispatch_id and args.dispatch_id not in scoped_dispatch_ids:
        print(f"ack: {args.dispatch_id} is not owned by the current project", file=sys.stderr)
        return 2
    summary = controller_mail_summary(
        owned_dispatch_ids=owned_dispatch_ids,
        task_store_project_root=project_root,
        messages_dir=args.messages_dir,
        fleet_dir=args.fleet_dir,
        unread_only=False,
    )
    items = list(summary.get("needs") or [])
    targets = (
        _stale_dispatch_ids(
            project_root,
            {str(item.get("dispatch_id") or "") for item in items},
        )
        if args.stale
        else {str(args.dispatch_id)}
    )
    try:
        dispatch_count, item_count = _ack_dispatches(
            messages_dir=args.messages_dir,
            items=items,
            dispatch_ids=targets,
        )
    except (OSError, ValueError, TypeError) as exc:
        print(f"WARNING: ack cursor not advanced: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    label = "ack --stale" if args.stale else f"ack {args.dispatch_id}"
    print(f"{label}: {dispatch_count} dispatch(es), {item_count} open item(s) acknowledged")
    return 0


# Bodies are for mail you have not seen yet. An unacked envelope reappears on
# every check, so a backlog that nobody acks re-floods the reader indefinitely -
# the headline listing fixed the per-message cost but not the per-check one.
# Anything older than this degrades to a headline even under --bodies.
STALE_BODY_AGE_S = 2 * 24 * 3600


def _envelope_age_s(envelope: dict, *, now: float | None = None) -> float | None:
    """Seconds since the envelope was posted, or None when unparseable."""
    ts = envelope.get("ts") if isinstance(envelope, dict) else None
    if not isinstance(ts, str) or not ts:
        return None
    try:
        posted = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=dt.timezone.utc)
    current = (
        dt.datetime.now(dt.timezone.utc)
        if now is None
        else dt.datetime.fromtimestamp(now, dt.timezone.utc)
    )
    return (current - posted).total_seconds()


def split_fresh_and_stale(
    envelopes: list, *, max_age_s: float = STALE_BODY_AGE_S, now: float | None = None
) -> tuple[list, list]:
    """Partition envelopes into (fresh enough for bodies, headline-only).

    An envelope with an unreadable timestamp is treated as FRESH: withholding a
    body we cannot date would silently hide new mail, which is the worse error.
    """
    fresh, stale = [], []
    for env in envelopes:
        if not isinstance(env, dict):
            continue
        age = _envelope_age_s(env, now=now)
        (stale if age is not None and age > max_age_s else fresh).append(env)
    return fresh, stale


HEADLINE_MAX = 96


def sanitize_display(value: object, *, limit: int | None = None) -> str:
    """Render and optionally truncate untrusted mail text for human display."""
    text = value if isinstance(value, str) else str(value)
    safe: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char in {"\r", "\n"} or codepoint in {0x85, 0x2028, 0x2029}:
            safe.append(" ")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            safe.append(f"\\x{codepoint:02x}")
        else:
            safe.append(char)
    rendered = " ".join("".join(safe).split())
    if limit is None or len(rendered) <= limit:
        return rendered
    if limit <= 0:
        return ""
    if limit <= 3:
        return "." * limit
    return rendered[: limit - 3] + "..."


def envelope_headline(envelope: dict) -> str:
    """One scannable line for an envelope.

    Mail is read by agents whose context is the scarce resource, so the default
    listing must be scannable rather than complete: an explicit subject when the
    sender set one, otherwise the first meaningful line of the body. Bodies are
    fetched deliberately, never dumped by default.
    """
    payload = envelope.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    subject = payload.get("subject")
    if isinstance(subject, str) and subject.strip():
        text = subject
    else:
        body = payload.get("text")
        body = body if isinstance(body, str) else ""
        # No subject: the first two meaningful lines usually carry the sender's
        # own headline, which beats one truncated line of a long opener.
        meaningful = []
        for line in body.splitlines():
            cleaned = sanitize_display(line)
            if cleaned:
                meaningful.append(cleaned)
        text = " / ".join(meaningful[:2])
    return sanitize_display(text, limit=HEADLINE_MAX) or "(no text)"


def envelope_from(envelope: dict) -> str:
    """Who sent it: the source node/adapter when recorded, else the inbox id."""
    source = envelope.get("source")
    source = source if isinstance(source, dict) else {}
    for key in ("adapter", "node"):
        value = source.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != "unknown":
            return sanitize_display(value)
    return sanitize_display(envelope.get("dispatch_id") or "?")


def format_envelope_headlines(envelopes: list) -> str:
    """Render unseen envelopes as one line each: FROM, subject, and body size."""
    lines = []
    for env in envelopes:
        if not isinstance(env, dict):
            continue
        payload = env.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        body = payload.get("text")
        size = len(body) if isinstance(body, str) else 0
        lines.append(
            f"{sanitize_display(env.get('dispatch_id', '?'))} "
            f"#{sanitize_display(env.get('seq', '?'))} "
            f"[{sanitize_display(env.get('type', '?'))}] from {envelope_from(env)}: "
            f"{envelope_headline(env)}  ({size}c)"
        )
    return "\n".join(lines)


def _registered_controller_addresses() -> set[tuple[str, str]]:
    """Durable, non-retired (project root, name) addresses."""
    try:
        import goalflight_task  # type: ignore
        import goalflight_session_status as sessions  # type: ignore
    except Exception:
        return set()
    roots: set[Path] = set()
    current_root = _current_project_root()
    if current_root is not None:
        roots.add(current_root)
    try:
        for project in goalflight_task.read_project_registry():
            root = project.get("project_root")
            if root:
                roots.add(Path(str(root)))
    except Exception:
        pass
    try:
        import goalflight_ledger  # type: ignore

        for record in goalflight_ledger.read_records():
            root = record.get("project_root")
            if root:
                roots.add(Path(str(root)))
    except Exception:
        pass
    addresses: set[tuple[str, str]] = set()
    for root in roots:
        try:
            canonical_root = controller_address_project_root(root)
            addresses.update(
                (canonical_root, label)
                for label in sessions.registered_controller_labels(root.resolve())
            )
        except Exception:
            continue
    return addresses


def unresolved_controller_envelopes(
    *,
    messages_dir: Path,
    fleet_dir: Path | None,
) -> list[dict]:
    """Unread named mail whose recipient has no active durable registration."""
    registered_addresses = _registered_controller_addresses()
    cursor = load_read_cursor(read_cursor_path(messages_dir))
    unresolved: list[dict] = []
    for path in collect_inbox_paths(messages_dir, fleet_dir):
        try:
            envelopes = read_envelopes(path)
        except MessageError:
            continue
        for envelope in envelopes:
            label = controller_addressee_label(envelope)
            project_root = controller_addressee_project_root(envelope)
            if not label or not project_root or (project_root, label) in registered_addresses:
                continue
            key = controller_cursor_key(
                label,
                str(envelope.get("dispatch_id") or path.stem),
                project_root,
            )
            if int(envelope.get("seq", 0)) > int(cursor.get(key, 0)):
                unresolved.append(envelope)
    unresolved.sort(key=lambda envelope: str(envelope.get("ts") or ""), reverse=True)
    return unresolved


def cmd_undeliverable(args: argparse.Namespace) -> int:
    envelopes = unresolved_controller_envelopes(
        messages_dir=args.messages_dir,
        fleet_dir=args.fleet_dir,
    )
    if not envelopes:
        print("no unresolved controller mail")
        return 0
    print(f"{len(envelopes)} unresolved controller envelope(s); correspondence remains unread")
    for envelope in envelopes[:DEFAULT_RELAY_ITEM_LIMIT]:
        label = controller_addressee_label(envelope) or "?"
        print(
            f"to {sanitize_display(label)}: "
            f"{sanitize_display(envelope.get('dispatch_id') or '?')} "
            f"#{sanitize_display(envelope.get('seq') or '?')}: "
            f"{envelope_headline(envelope)}"
        )
    if len(envelopes) > DEFAULT_RELAY_ITEM_LIMIT:
        print(f"(+{len(envelopes) - DEFAULT_RELAY_ITEM_LIMIT} elided)")
    return 2


def backlog_digest(
    *,
    messages_dir: Path,
    fleet_dir: Path | None,
    controller_label: str | None = None,
    controller_project_root: str | None = None,
) -> tuple[dict, dict[str, int]]:
    """Summarize the current unread snapshot without copying or deleting bodies."""
    cursor = load_read_cursor(read_cursor_path(messages_dir))
    items: list[dict] = []
    advances: dict[str, int] = {}
    counts_by_type: dict[str, int] = {}
    counts_by_addressee: dict[str, int] = {}
    for path in collect_inbox_paths(messages_dir, fleet_dir):
        try:
            envelopes = read_envelopes(path)
        except MessageError:
            continue
        for envelope in envelopes:
            if (
                controller_label is not None
                and (
                    controller_addressee_label(envelope) != controller_label
                    or controller_addressee_project_root(envelope)
                    != controller_project_root
                )
            ):
                continue
            key = envelope_cursor_key(envelope)
            seq = int(envelope.get("seq", 0))
            if seq <= int(cursor.get(key, 0)):
                continue
            msg_type = str(envelope.get("type") or "unknown")
            label = controller_addressee_label(envelope) or "legacy-unaddressed"
            counts_by_type[msg_type] = counts_by_type.get(msg_type, 0) + 1
            counts_by_addressee[label] = counts_by_addressee.get(label, 0) + 1
            advances[key] = max(int(advances.get(key, 0)), seq)
            items.append(
                {
                    "dispatch_id": str(envelope.get("dispatch_id") or path.stem),
                    "seq": seq,
                    "id": envelope.get("id"),
                    "ts": envelope.get("ts"),
                    "type": msg_type,
                    "addressee": envelope.get("addressee"),
                    "from": envelope_from(envelope),
                    "headline": envelope_headline(envelope),
                    "body_chars": len(str((envelope.get("payload") or {}).get("text") or "")),
                    "source_inbox": str(path),
                }
            )
    items.sort(key=lambda item: (str(item.get("ts") or ""), str(item.get("dispatch_id"))), reverse=True)
    return (
        {
            "schema": "goalflight.mail.backlog-digest.v1",
            "created_at": utc_now(),
            "envelope_count": len(items),
            "counts_by_type": dict(sorted(counts_by_type.items())),
            "counts_by_addressee": dict(sorted(counts_by_addressee.items())),
            "correspondence_retained": True,
            "retention_note": "Original JSONL inboxes are unchanged; use source_inbox + seq to read bodies.",
            "items": items,
        },
        advances,
    )


def retire_controller_mailbox(
    *,
    messages_dir: Path,
    fleet_dir: Path | None,
    controller_label: str,
    controller_project_root: str,
    controller_session_id: str,
    retired_at: str,
    retired_by: dict,
) -> dict:
    """Prepare one idempotent retirement digest without advancing cursors."""
    digest_dir = messages_dir / BACKLOG_DIGEST_DIR
    digest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", controller_label).strip("-") or "controller"
    transaction_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{controller_project_root}\0{controller_label}\0{controller_session_id}",
    ).hex[:16]
    path = digest_dir / f"retirement-{safe_label}-{transaction_id}.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        retirement = existing.get("retirement") if isinstance(existing, dict) else None
        if not isinstance(retirement, dict) or (
            retirement.get("controller_label") != controller_label
            or retirement.get("controller_project_root") != controller_project_root
            or retirement.get("controller_session_id") != controller_session_id
        ):
            raise MessageError(f"{path}: retirement digest identity mismatch")
        retirement = dict(retirement)
        retirement["retired_at"] = retired_at
        retirement["retired_by"] = retired_by
        existing["retirement"] = retirement
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return {
            "digest": str(path),
            "envelope_count": int(existing.get("envelope_count") or 0),
            "retired_at": retired_at,
            "correspondence_retained": True,
            "reused": True,
        }

    digest, advances = backlog_digest(
        messages_dir=messages_dir,
        fleet_dir=fleet_dir,
        controller_label=controller_label,
        controller_project_root=controller_project_root,
    )
    digest["retirement"] = {
        "controller_label": controller_label,
        "controller_project_root": controller_project_root,
        "controller_session_id": controller_session_id,
        "retired_at": retired_at,
        "retired_by": retired_by,
    }
    digest["cursor_advances"] = advances
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {
        "digest": str(path),
        "envelope_count": digest["envelope_count"],
        "retired_at": retired_at,
        "correspondence_retained": True,
        "reused": False,
    }


def finalize_controller_retirement_mailbox(
    *,
    messages_dir: Path,
    digest_path: str | Path,
) -> dict:
    """Idempotently advance exactly the cursor snapshot named by a digest."""
    digest_dir = (messages_dir / BACKLOG_DIGEST_DIR).resolve()
    path = Path(digest_path).resolve()
    if not path.is_relative_to(digest_dir):
        raise MessageError("retirement digest is outside the mailbox digest directory")
    digest = json.loads(path.read_text(encoding="utf-8"))
    raw_advances = digest.get("cursor_advances") if isinstance(digest, dict) else None
    if not isinstance(raw_advances, dict):
        raise MessageError(f"{path}: missing retirement cursor snapshot")
    advances = {
        str(key): require_positive_int_seq(value, path=f"{path}.cursor_advances.{key}")
        for key, value in raw_advances.items()
    }
    advance_read_cursor(read_cursor_path(messages_dir), advances)
    return {"cursor_entries_advanced": len(advances), "finalized": True}


def cmd_triage_backlog(args: argparse.Namespace) -> int:
    digest, advances = backlog_digest(
        messages_dir=args.messages_dir,
        fleet_dir=args.fleet_dir,
    )
    if not args.apply:
        print(
            json.dumps(
                {
                    "apply": False,
                    "envelope_count": digest["envelope_count"],
                    "counts_by_type": digest["counts_by_type"],
                    "counts_by_addressee": digest["counts_by_addressee"],
                    "note": "dry run; pass --apply to write a digest and advance only this snapshot",
                },
                sort_keys=True,
            )
        )
        return 0
    digest_dir = args.messages_dir / BACKLOG_DIGEST_DIR
    digest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = str(digest["created_at"]).replace(":", "").replace("+", "-")
    path = digest_dir / f"backlog-{stamp}-{uuid.uuid4().hex[:8]}.json"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    advance_read_cursor(read_cursor_path(args.messages_dir), advances)
    print(
        json.dumps(
            {
                "apply": True,
                "digest": str(path),
                "envelope_count": digest["envelope_count"],
                "correspondence_retained": True,
                "cursor_entries_advanced": len(advances),
            },
            sort_keys=True,
        )
    )
    return 0


def cmd_relay(args: argparse.Namespace) -> int:
    if args.ack and not args.new:
        print("relay --ack requires --new", file=sys.stderr)
        return 2
    if args.new and args.history:
        print("relay: --new cannot be combined with --history", file=sys.stderr)
        return 2
    try:
        project_root, owned_dispatch_ids = _mail_scope(all_projects=args.all_projects)
    except MessageError as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 2
    if args.new:
        scoped_dispatch_ids = _owned_with_project_mail(
            owned_dispatch_ids,
            project_root,
            messages_dir=args.messages_dir,
            fleet_dir=args.fleet_dir,
        )
        envelope_filter = None
        cursor_key = None
        controller_label = None
        if project_root is not None and owned_dispatch_ids is not None:
            records = _project_ledger_records(project_root)
            scope_inputs = _controller_scope_inputs(
                project_root,
                records=records,
                owned_dispatch_ids={str(value) for value in owned_dispatch_ids},
                controller_session_id=None,
                messages_dir=args.messages_dir,
                fleet_dir=args.fleet_dir,
            )
            legacy_addressed_dispatch_ids = set(scope_inputs["legacy_addressed_dispatch_ids"])
            task_store_dispatch_id = scope_inputs["task_store_dispatch_id"]
            controller_label = (
                str(scope_inputs["controller_label"])
                if scope_inputs.get("controller_label")
                else None
            )
            controller_project_root = str(scope_inputs["controller_project_root"])

            def envelope_filter(envelope: dict) -> bool:
                return _controller_scope_kind(
                    envelope,
                    owned_dispatch_ids={str(value) for value in owned_dispatch_ids},
                    legacy_addressed_dispatch_ids=legacy_addressed_dispatch_ids,
                    task_store_dispatch_id=(
                        str(task_store_dispatch_id) if task_store_dispatch_id else None
                    ),
                    controller_label=controller_label,
                    controller_project_root=controller_project_root,
                ) is not None

            def cursor_key(envelope: dict) -> str:
                label = controller_addressee_label(envelope)
                if label and label == controller_label:
                    return controller_cursor_key(
                        label,
                        str(envelope.get("dispatch_id") or ""),
                        controller_addressee_project_root(envelope),
                    )
                return str(envelope.get("dispatch_id") or "")

        paths = collect_inbox_paths(
            args.messages_dir,
            args.fleet_dir,
            dispatch_ids=None if controller_label else scoped_dispatch_ids,
        )
        cursor_path = read_cursor_path(args.messages_dir)
        cursor = load_read_cursor(cursor_path)
        envelopes, counts, ack_advances = unseen_envelopes_for_paths(
            paths,
            cursor=cursor,
            tolerate_errors=True,
            envelope_filter=envelope_filter,
            cursor_key=cursor_key,
        )
        if getattr(args, "bodies", False):
            fresh, stale = split_fresh_and_stale(envelopes)
            print(json.dumps(fresh))
            if stale:
                print(
                    f"{len(stale)} envelope(s) older than "
                    f"{int(STALE_BODY_AGE_S // 3600)}h shown as headlines only:"
                )
                print(format_envelope_headlines(stale))
                print("read one in full with: `read --dispatch-id <id>`")
        else:
            headlines = format_envelope_headlines(envelopes)
            if headlines:
                print(headlines)
                print("bodies: re-run with --bodies, or read one inbox with `read`")
        if args.ack:
            try:
                advance_read_cursor(cursor_path, ack_advances)
                cursor = load_read_cursor(cursor_path)
                _, counts, _ = unseen_envelopes_for_paths(
                    paths,
                    cursor=cursor,
                    tolerate_errors=True,
                    envelope_filter=envelope_filter,
                    cursor_key=cursor_key,
                )
            except (OSError, ValueError, TypeError) as exc:
                warn_cursor_not_advanced(exc)
                print(format_unseen_counts(counts))
                return 1
        print(format_unseen_counts(counts))
        return 0
    summary = controller_mail_summary(
        owned_dispatch_ids=owned_dispatch_ids,
        task_store_project_root=project_root,
        messages_dir=args.messages_dir,
        fleet_dir=args.fleet_dir,
        unread_only=not args.history,
    )
    items = list(summary.get("needs") or [])
    line = (
        format_controller_relay({"open_user_needs": items})
        if args.history
        else format_bounded_relay(items)
    )
    if line:
        print(line)
        return 2
    print("no open unread items" if not args.history else "no open user_needs")
    return 0


STEERING_DISPATCH_ID = "fleet-steering"


def steering_register_path(fleet_dir: Path) -> Path:
    return fleet_dir / "register" / "dispatches" / f"{STEERING_DISPATCH_ID}.jsonl"


def next_seq(path: Path) -> int:
    envelopes = read_envelopes(path) if path.exists() else []
    if not envelopes:
        return 1
    return max(int(env.get("seq", 0)) for env in envelopes) + 1


def write_steering_envelope(
    fleet_dir: Path,
    *,
    audit_id: str,
    proposal_id: str,
    patch: list[dict],
    after_hash: str,
    messages_dir: Path | None = None,
) -> dict:
    path = steering_register_path(fleet_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with mail_lock(path):
        envelope = {
            "schema": "goalflight.message.v1",
            "schema_version": 1,
            "id": str(uuid.uuid4()),
            "dispatch_id": STEERING_DISPATCH_ID,
            "seq": next_seq(path),
            "ts": utc_now(),
            "source": {"node": "local", "adapter": "fleet", "transport": "controller"},
            "type": "steering",
            "priority": "normal",
            "payload": {
                "audit_id": audit_id,
                "proposal_id": proposal_id,
                "patch": patch,
                "after_hash": after_hash,
            },
        }
        append_envelope(path, envelope)
    refresh_aggregate(fleet_dir, messages_dir=messages_dir or default_messages_dir())
    return envelope


def merge_remote_register(
    fleet_dir: Path,
    remote_jsonl: Path,
    *,
    messages_dir: Path | None = None,
) -> dict:
    """Merge remote dispatch jsonl into fleet register using monotonic seq rules."""
    if not remote_jsonl.exists():
        raise MessageError(f"remote file missing: {remote_jsonl}")
    remote = read_envelopes(remote_jsonl)
    dest = fleet_dir / "register" / "dispatches" / remote_jsonl.name
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    appended = 0
    with mail_lock(dest):
        existing = read_envelopes(dest) if dest.exists() else []
        seen_seq = {int(env.get("seq", 0)) for env in existing}
        for env in remote:
            seq = int(env.get("seq", 0))
            if seq in seen_seq:
                continue
            append_envelope(dest, env)
            seen_seq.add(seq)
            appended += 1
    aggregate = refresh_aggregate(fleet_dir, messages_dir=messages_dir or default_messages_dir())
    return {"merged_into": str(dest), "appended": appended, "open_user_needs": len(aggregate.get("open_user_needs") or [])}


def cmd_listen(args) -> int:
    """Block silently until new wakeable mail arrives for this controller.

    The point is the SILENCE. A controller told to background a long-poll is
    asleep by design; a listener that chatters costs it context for nothing, and
    one that returns immediately trains it to ignore the signal. This emits
    nothing at all until something arrives, so a controller can leave one call
    open across a long stretch of work without paying for the wait.

    Only mail that arrives AFTER the listener starts counts. Waking on the
    existing backlog would return instantly for any controller with unread mail
    -- and on this machine that backlog is 100+ envelopes, some 32 days old. Use
    `relay --new` to read what is already there; use this to be told about what
    is not there yet.

    Ownership is an exact controller-session match and is re-read every poll, so
    late dispatches join without re-arming. Owned terminal/result and escalation
    envelopes always wake. Controller-channel mail wakes unless its source proves
    this same controller session authored it; missing or ambiguous authorship wakes.
    Unowned or other-session workers, status/monitor traffic, quota advisories, and
    recurring task-store status nudges do not. Quiet mail remains in the normal
    unread count.

    Exits 0 when mail arrives, 1 on timeout. Fail-open: if the mailbox cannot be
    read at all, say so on stderr and exit 2 rather than blocking forever on a
    channel that will never deliver.
    """
    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    messages_dir = args.messages_dir or default_messages_dir()
    fleet_dir = args.fleet_dir if args.fleet_dir is not None else default_fleet_dir()
    poll = max(0.5, float(args.poll_secs or 5.0))
    deadline = None
    if args.timeout_s and float(args.timeout_s) > 0:
        deadline = time.monotonic() + float(args.timeout_s)

    try:
        controller_session_id = _resolve_listener_session_id(project_root, args.session_id)
    except MessageError as exc:
        print(f"listen: {exc}", file=sys.stderr)
        return 2

    def watermark():
        """(inbox, seq) pairs, not a count: acking lowers a count, so a count
        would read an ack as 'nothing new' and then miss the next arrival that
        merely restored the old number. Pairs only ever add."""
        try:
            return controller_wake_watermark(
                controller_session_id=controller_session_id,
                project_root=project_root,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            raise RuntimeError(str(exc)) from exc

    try:
        baseline = watermark()
    except RuntimeError as exc:
        print(f"listen: cannot read mailbox: {exc}", file=sys.stderr)
        return 2

    while True:
        if deadline is not None and time.monotonic() >= deadline:
            print("listen: timed out with no new mail", file=sys.stderr)
            return 1
        time.sleep(poll)
        try:
            current = watermark()
        except RuntimeError as exc:
            print(f"listen: cannot read mailbox: {exc}", file=sys.stderr)
            return 2
        fresh = [current[key] for key in current if key not in baseline]
        if not fresh:
            continue
        if args.json:
            print(json.dumps({"new_mail": len(fresh), "items": fresh}, sort_keys=True, default=str))
        else:
            print(format_mail_notice(len(fresh)))
            for item in fresh:
                print(f"  {item.get('dispatch_id')} [{item.get('type')}] "
                      f"{sanitize_display(item.get('text') or '', limit=140)}")
        return 0


def cmd_mirror(args: argparse.Namespace) -> int:
    result = merge_remote_register(args.fleet_dir, args.remote, messages_dir=args.messages_dir)
    print(json.dumps(result, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    aggregate = build_aggregate(messages_dir=args.messages_dir, fleet_dir=args.fleet_dir)
    if args.write_aggregate:
        refresh_aggregate(args.fleet_dir, messages_dir=args.messages_dir)
    print(json.dumps(aggregate, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Flight message envelopes")
    parser.add_argument("--messages-dir", type=Path, default=default_messages_dir())
    parser.add_argument("--fleet-dir", type=Path, default=default_fleet_dir())
    sub = parser.add_subparsers(dest="cmd", required=True)

    from_text = sub.add_parser("from-text")
    from_text.add_argument("--dispatch-id", required=True)
    from_text.add_argument("--text-file", type=Path)
    from_text.add_argument("--node", default="local")
    from_text.add_argument("--adapter", default="unknown")
    from_text.add_argument(
        "--transport",
        default="tail_file",
        choices=["acp", "tail_file", "mcp", "controller", "bash-tail"],
    )
    from_text.add_argument("--json", action="store_true")
    from_text.set_defaults(func=cmd_from_text)

    append = sub.add_parser("append")
    append.add_argument("--dispatch-id", required=True)
    append.add_argument("--envelope-file", type=Path)
    append.add_argument("--refresh-aggregate", action="store_true")
    append.set_defaults(func=cmd_append)

    post = sub.add_parser("post", help="Append one envelope (canonical file path)")
    post.add_argument("--dispatch-id", required=True)
    post.add_argument("--type", required=True)
    post.add_argument("--payload", help="JSON object payload")
    post.add_argument("--text", help="Shorthand payload.text when --payload omitted")
    post.add_argument(
        "--subject",
        help="Short scannable subject; shown in relay listings instead of the first body line",
    )
    post.add_argument(
        "--to-controller",
        metavar="LABEL",
        help="address controller-channel mail to one durable controller registry name",
    )
    post.add_argument(
        "--controller-project-root",
        type=Path,
        help=(
            "project root that scopes --to-controller; defaults to the current git "
            "project, and is required for explicit cross-project addressing"
        ),
    )
    post.add_argument("--node", default="local")
    post.add_argument("--adapter", default="unknown")
    post.add_argument(
        "--transport",
        default="controller",
        choices=["acp", "tail_file", "mcp", "controller", "bash-tail"],
    )
    post.add_argument("--refresh-aggregate", action="store_true")
    post.add_argument("--json", action="store_true")
    post.set_defaults(func=cmd_post)

    read = sub.add_parser("read")
    read.add_argument("--dispatch-id", required=True)
    read.add_argument("--last", type=int, default=None)
    read.add_argument("--json", action="store_true")
    read.add_argument("--unseen", action="store_true", help="Show only envelopes after this inbox's read cursor")
    read.add_argument("--ack", action="store_true", help="With --unseen, advance the cursor through shown envelopes")
    read.set_defaults(func=cmd_read)

    mark_read = sub.add_parser("mark-read", help="Advance read cursor state")
    mark_read.add_argument("--dispatch-id")
    mark_read.add_argument("--through", type=int)
    mark_read.add_argument("--all", action="store_true")
    mark_read.set_defaults(func=cmd_mark_read)

    ack = sub.add_parser("ack", help="Acknowledge open escalation lifecycle items")
    ack.add_argument("dispatch_id", nargs="?")
    ack.add_argument(
        "--stale",
        action="store_true",
        help="Acknowledge terminal task-linked escalations closed or superseded",
    )
    ack.set_defaults(func=cmd_ack)

    status = sub.add_parser("status")
    status.add_argument("--write-aggregate", action="store_true")
    status.set_defaults(func=cmd_status)

    relay = sub.add_parser("relay")
    relay.add_argument("--new", action="store_true", help="Show only envelopes after each inbox read cursor")
    relay.add_argument("--ack", action="store_true", help="With --new, advance cursors through shown envelopes")
    relay.add_argument(
        "--all-projects",
        action="store_true",
        help="Include inboxes outside the current project",
    )
    relay.add_argument(
        "--bodies",
        action="store_true",
        help="With --new, print full envelope JSON instead of one headline per message",
    )
    relay.add_argument(
        "--history",
        action="store_true",
        help="Include read open items and use the legacy unbounded summary",
    )
    relay.set_defaults(func=cmd_relay)

    undeliverable = sub.add_parser(
        "undeliverable",
        help="report unread named controller mail with no active registered recipient",
    )
    undeliverable.set_defaults(func=cmd_undeliverable)

    triage_backlog = sub.add_parser(
        "triage-backlog",
        help="digest the machine-wide unread snapshot without deleting correspondence",
    )
    triage_backlog.add_argument(
        "--apply",
        action="store_true",
        help="write the digest, then advance read cursors through exactly that snapshot",
    )
    triage_backlog.set_defaults(func=cmd_triage_backlog)

    listen = sub.add_parser(
        "listen",
        help="block SILENTLY until new mail arrives, then print it and exit",
    )
    listen.add_argument("--project-root", default=None)
    listen.add_argument(
        "--session-id",
        default=None,
        help="controller session id; default: the project's live session beacon",
    )
    listen.add_argument("--poll-secs", type=float, default=5.0)
    listen.add_argument("--timeout-s", type=float, default=0.0,
                        help="0 = wait indefinitely")
    listen.add_argument("--json", action="store_true")

    mirror = sub.add_parser("mirror")
    mirror.add_argument("--remote", type=Path, required=True, help="Remote *.jsonl inbox to merge")
    listen.set_defaults(func=cmd_listen)
    mirror.set_defaults(func=cmd_mirror)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
