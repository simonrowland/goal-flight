#!/usr/bin/env python3
"""Marker → message envelope conversion and dispatch inbox (Track C Phase 0)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import contextlib
import datetime as dt
import json
import os
import subprocess
import uuid
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_CONTRACT = REPO_ROOT / "docs-private" / "architecture" / "contracts" / "goalflight.message.v1.json"
AGGREGATE_SCHEMA = "goalflight.fleet.register.aggregate.v1"
READ_CURSOR_FILE = ".read-cursor.json"
ACK_CURSOR_FILE = ".ack-cursor.json"
DEFAULT_RELAY_ITEM_LIMIT = 20
DEFAULT_RELAY_BYTE_LIMIT = 4096
TASKLESS_TERMINAL_STALE_AFTER = dt.timedelta(hours=24)

sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat  # noqa: E402
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
        line = serialize_envelope_line(envelope)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    if update_aggregate and fleet_dir is not None:
        refresh_aggregate(fleet_dir, messages_dir=messages_dir)
    return {"envelope": envelope, "line": line, "path": str(path)}


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
        fleet_dir=fleet_dir,
        update_aggregate=refresh_aggregate,
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
        unseen = [env for env in envelopes if int(env.get("seq", 0)) > int(cursor.get(key, 0))]
        counts[key] = len(unseen)
        if last_n is not None and last_n >= 0:
            unseen = unseen[-last_n:] if last_n else []
        shown.extend(unseen)
        if unseen:
            ack_advances[key] = max(int(env.get("seq", 0)) for env in unseen)
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
) -> dict:
    envelopes_by_dispatch: dict[str, list[dict]] = {}
    ack_cursor = load_read_cursor(ack_cursor_path(messages_dir))
    for path in collect_inbox_paths(messages_dir, fleet_dir, dispatch_ids=dispatch_ids):
        try:
            envelopes_by_dispatch[path.stem] = read_envelopes(path)
        except MessageError:
            # One malformed/unreadable inbox must NOT suppress everyone else's mail
            # (a scoped status reads only its own inbox, but be tolerant regardless).
            continue

    open_user_needs: list[dict] = []
    open_advisories: list[dict] = []
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

    return {
        "schema": AGGREGATE_SCHEMA,
        "schema_version": 1,
        "min_reader_version": 1,
        "updated_at": utc_now(),
        "open_user_needs": open_user_needs,
        "open_advisories": open_advisories,
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
    result = post_message(
        dispatch_id=args.dispatch_id,
        msg_type=args.type,
        payload=payload,
        messages_dir=args.messages_dir,
        source=source,
        fleet_dir=args.fleet_dir,
        update_aggregate=args.refresh_aggregate,
    )
    print(json.dumps(result, indent=2 if args.json else None))
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


def _task_store_dispatch_id(project_root: Path) -> str | None:
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import goalflight_task  # type: ignore

        # Resolve the CANONICAL project root (git common-dir parent) exactly
        # like the nudge WRITER does — a raw worktree path hashes to a
        # different slug and the reader would silently watch the wrong inbox
        # (live consumption-gap regression caught 2026-07-02). Anchored git
        # discovery covers ANY linked worktree, not just managed ones.
        root = Path(project_root)
        try:
            import subprocess

            common_raw = subprocess.check_output(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=str(root),
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            common = Path(common_raw)
            if not common.is_absolute():
                common = (root / common).resolve()
            root = common.parent
        except Exception:
            root = Path(goalflight_task.resolve_project_root(str(project_root)))
        return goalflight_task._next_nudge_dispatch_id(root)
    except Exception:
        return None


def _owned_with_task_store(
    owned_dispatch_ids: set[str] | None,
    task_store_project_root: Path | None,
) -> set[str] | None:
    if owned_dispatch_ids is None or task_store_project_root is None:
        return owned_dispatch_ids
    dispatch_id = _task_store_dispatch_id(task_store_project_root)
    if not dispatch_id:
        return owned_dispatch_ids
    scoped = set(owned_dispatch_ids)
    scoped.add(dispatch_id)
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

        return dispatch_states.is_terminal_state(record.get("state")) or dispatch_states.is_terminal_state(
            record.get("terminal_state")
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


def _filter_task_store_nudges(items: list[dict], task_store_project_root: Path | None) -> list[dict]:
    if task_store_project_root is None:
        return items
    dispatch_id = _task_store_dispatch_id(task_store_project_root)
    if not dispatch_id:
        return items
    return [
        item
        for item in items
        if str(item.get("dispatch_id") or "") != dispatch_id
        or _task_store_nudge_is_current(item, task_store_project_root)
    ]


def _format_mail_hint(items: list[dict]) -> str:
    """Multi-line controller hint: a header plus one detail line per open item,
    each with the dispatch id, kind, and a clipped text so the controller can
    follow up straight from a status check."""
    head = (
        f"\U0001f4ec mail: {len(items)} open unread item(s) from your worker(s)/task store "
        "- read headlines: goalflight_messages.py relay --new (--ack to mark read)"
    )
    lines = [head]
    for it in items[:5]:
        lines.append(
            f"    [{sanitize_display(it.get('dispatch_id') or '?', limit=64)}] "
            f"{sanitize_display(it.get('type') or 'user_need', limit=32)}: "
            f"{sanitize_display(it.get('text') or '', limit=120)}"
        )
    if len(items) > 5:
        lines.append(f"    (+{len(items) - 5} more)")
    return "\n".join(lines)


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
    user_confirm / blocked) plus controller quota advisories with enough detail
    to act on from a status check.

    The mailbox is machine-global (shared across controllers), so when
    ``owned_dispatch_ids`` is provided only needs from THOSE dispatches — the
    controller's own workers — plus ``task_store_project_root``'s pseudo-inbox are
    surfaced; a controller must never see another controller's workers' needs.
    ``None`` means no ownership filter (e.g. an all-projects view). Returns ``{}``
    when there is nothing to show.
    """
    # Read ONLY this controller's own inboxes: an unrelated controller's corrupt or
    # large inbox can then neither suppress (a parse error elsewhere) nor slow this
    # scoped status call. build_aggregate is also per-inbox tolerant as a backstop.
    scoped_dispatch_ids = _owned_with_task_store(owned_dispatch_ids, task_store_project_root)
    resolved_messages_dir = messages_dir or default_messages_dir()
    aggregate = build_aggregate(
        messages_dir=resolved_messages_dir,
        fleet_dir=fleet_dir if fleet_dir is not None else default_fleet_dir(),
        dispatch_ids=scoped_dispatch_ids,
    )
    needs = list(aggregate.get("open_user_needs") or [])
    needs.extend(aggregate.get("open_advisories") or [])
    if scoped_dispatch_ids is not None:
        needs = [
            n for n in needs
            if str(n.get("dispatch_id") or "") in scoped_dispatch_ids
            or str(n.get("dispatch_id") or "") == "controller-quota-advisory"
        ]
    needs = _filter_task_store_nudges(needs, task_store_project_root)
    if unread_only:
        read_cursor = load_read_cursor(read_cursor_path(resolved_messages_dir))
        needs = [
            item
            for item in needs
            if not isinstance(item.get("seq"), int)
            or int(item["seq"]) > int(read_cursor.get(str(item.get("dispatch_id") or ""), 0))
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
        }
        for n in needs
    ]
    return {"count": len(items), "needs": items, "hint": _format_mail_hint(items)}


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
    scoped_dispatch_ids = _owned_with_task_store(owned_dispatch_ids, project_root) or set()
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
        scoped_dispatch_ids = _owned_with_task_store(owned_dispatch_ids, project_root)
        paths = collect_inbox_paths(
            args.messages_dir,
            args.fleet_dir,
            dispatch_ids=scoped_dispatch_ids,
        )
        cursor_path = read_cursor_path(args.messages_dir)
        cursor = load_read_cursor(cursor_path)
        envelopes, counts, ack_advances = unseen_envelopes_for_paths(
            paths,
            cursor=cursor,
            tolerate_errors=True,
        )
        if getattr(args, "bodies", False):
            print(json.dumps(envelopes))
        else:
            headlines = format_envelope_headlines(envelopes)
            if headlines:
                print(headlines)
                print("bodies: re-run with --bodies, or read one inbox with `read`")
        print(format_unseen_counts(counts))
        if args.ack:
            try:
                advance_read_cursor(cursor_path, ack_advances)
            except (OSError, ValueError, TypeError) as exc:
                warn_cursor_not_advanced(exc)
                return 1
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

    mirror = sub.add_parser("mirror")
    mirror.add_argument("--remote", type=Path, required=True, help="Remote *.jsonl inbox to merge")
    mirror.set_defaults(func=cmd_mirror)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
