#!/usr/bin/env python3
"""Marker → message envelope conversion and dispatch inbox (Track C Phase 0)."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import uuid
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_CONTRACT = REPO_ROOT / "docs-private" / "architecture" / "contracts" / "goalflight.message.v1.json"
AGGREGATE_SCHEMA = "goalflight.fleet.register.aggregate.v1"

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
    base_source = {
        "node": "local",
        "adapter": "unknown",
        "transport": "controller",
    }
    if source:
        base_source.update(source)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with mail_lock(path):
        resolved_seq = seq if seq is not None else next_seq(path)
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


def _open_user_needs(envelopes: list[dict]) -> list[dict]:
    if _dispatch_complete(envelopes):
        return []
    open_items: list[dict] = []
    for env in envelopes:
        if env.get("type") in {"user_need", "user_confirm", "blocked"}:
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


def _open_controller_advisories(envelopes: list[dict]) -> list[dict]:
    if _dispatch_complete(envelopes):
        return []
    open_items: list[dict] = []
    for env in envelopes:
        if env.get("dispatch_id") == "controller-quota-advisory" and env.get("type") == "advisory":
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


def build_aggregate(
    *,
    messages_dir: Path,
    fleet_dir: Path | None = None,
    dispatch_ids: set[str] | None = None,
) -> dict:
    envelopes_by_dispatch: dict[str, list[dict]] = {}
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
        open_user_needs.extend(_open_user_needs(envelopes))
        open_advisories.extend(_open_controller_advisories(envelopes))

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
    path = inbox_path(args.messages_dir, args.dispatch_id)
    envelopes = read_envelopes(path, last_n=args.last)
    print(json.dumps(envelopes, indent=2 if args.json else None))
    return 0


def format_controller_relay(aggregate: dict) -> str | None:
    """One-line summary for orchestrator host when open user_needs exist."""
    needs = aggregate.get("open_user_needs") or []
    if not needs:
        return None
    parts: list[str] = []
    for item in needs:
        dispatch_id = item.get("dispatch_id", "?")
        kind = item.get("type", "user_need")
        text = (item.get("text") or "").strip()
        if len(text) > 120:
            text = text[:117] + "..."
        parts.append(f"[{dispatch_id}] {kind}: {text}")
    return "USER-NEED relay: " + " | ".join(parts)


def _clip(text: object, limit: int = 100) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _format_mail_hint(items: list[dict]) -> str:
    """Multi-line controller hint: a header plus one detail line per open item,
    each with the dispatch id, kind, and a clipped text so the controller can
    follow up straight from a status check."""
    head = f"\U0001f4ec mail: {len(items)} open item(s) from your worker(s) - run: goalflight_messages.py relay"
    lines = [head]
    for it in items[:5]:
        lines.append(f"    [{it['dispatch_id']}] {it['type']}: {it['text']}")
    if len(items) > 5:
        lines.append(f"    (+{len(items) - 5} more)")
    return "\n".join(lines)


def controller_mail_summary(
    *,
    owned_dispatch_ids: set[str] | None = None,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
) -> dict:
    """Structured "you have mail" summary for a controller's status output.

    Builds the inbox aggregate and returns OPEN user-needs (user_need /
    user_confirm / blocked) plus controller quota advisories with enough detail
    to act on from a status check.

    The mailbox is machine-global (shared across controllers), so when
    ``owned_dispatch_ids`` is provided only needs from THOSE dispatches — the
    controller's own workers — are surfaced; a controller must never see another
    controller's workers' needs. ``None`` means no ownership filter (e.g. an
    all-projects view). Returns ``{}`` when there is nothing to show.
    """
    # Read ONLY this controller's own inboxes: an unrelated controller's corrupt or
    # large inbox can then neither suppress (a parse error elsewhere) nor slow this
    # scoped status call. build_aggregate is also per-inbox tolerant as a backstop.
    aggregate = build_aggregate(
        messages_dir=messages_dir or default_messages_dir(),
        fleet_dir=fleet_dir if fleet_dir is not None else default_fleet_dir(),
        dispatch_ids=owned_dispatch_ids,
    )
    needs = list(aggregate.get("open_user_needs") or [])
    needs.extend(aggregate.get("open_advisories") or [])
    if owned_dispatch_ids is not None:
        needs = [
            n for n in needs
            if str(n.get("dispatch_id") or "") in owned_dispatch_ids
            or str(n.get("dispatch_id") or "") == "controller-quota-advisory"
        ]
    if not needs:
        return {}
    items = [
        {
            "dispatch_id": str(n.get("dispatch_id") or "?"),
            "type": str(n.get("type") or "user_need"),
            "seq": n.get("seq"),
            "text": _clip(n.get("text")),
        }
        for n in needs
    ]
    return {"count": len(items), "needs": items, "hint": _format_mail_hint(items)}


def cmd_relay(args: argparse.Namespace) -> int:
    aggregate = build_aggregate(messages_dir=args.messages_dir, fleet_dir=args.fleet_dir)
    line = format_controller_relay(aggregate)
    if line:
        print(line)
        return 2
    print("no open user_needs")
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
    read.set_defaults(func=cmd_read)

    status = sub.add_parser("status")
    status.add_argument("--write-aggregate", action="store_true")
    status.set_defaults(func=cmd_status)

    relay = sub.add_parser("relay")
    relay.set_defaults(func=cmd_relay)

    mirror = sub.add_parser("mirror")
    mirror.add_argument("--remote", type=Path, required=True, help="Remote *.jsonl inbox to merge")
    mirror.set_defaults(func=cmd_mirror)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
