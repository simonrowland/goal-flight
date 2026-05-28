#!/usr/bin/env python3
"""goal-flight session activation status helper.

Answers the post-compaction question: "is a goal-flight session active in
this project, or am I in for routine coding?"

Three signals are unioned (see `protocols/state-handoff.md` activation
contract):

1. `state:` field in the newest `docs-private/goal-queue-*.md` frontmatter.
2. Dispatch ledger active leases scoped to this project_root (via
   `scripts/goalflight_capacity.py status`).
3. Newest `docs-private/RESUME-NOTES-*.md` "state" line (if present).

A session id ties a controller invocation to the run. The id lives in
`docs-private/.goal-flight-current-session.json` (per-terminal, gitignored).
The session id is also stamped into the active goal-queue frontmatter under
`current_session` (so multi-machine takeover is detectable) and appended to
`session_history` (audit trail).

Public CLI:

    --json                Emit JSON status (see `_to_json` for shape).
    --text                Emit a one-line plain-English verdict.
    --ensure-session      Read or generate `.goal-flight-current-session.json`,
                          print the session id.
    --claim --queue PATH  Stamp current session into the named goal-queue's
                          `current_session`. Refuses with diagnostic if the
                          queue is claimed by a different alive PID.
    --release [--queue P] Mark current session ended in current_session +
                          session_history; clear `.goal-flight-current-
                          session.json` if no `--queue` (terminal exit).
    --force-release-stale Clear current_session whose pid is dead. Useful
                          after a crash.

Exit codes:

    0  success
    2  refused (e.g., --claim hits a live different-pid owner)
    3  malformed queue frontmatter / fixture-only paths
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE_REL = Path("docs-private/.goal-flight-current-session.json")
QUEUE_GLOB = "docs-private/goal-queue-*.md"
RESUME_NOTES_GLOB = "docs-private/RESUME-NOTES-*.md"


# --- session id (per-terminal) ----------------------------------------------


def _session_file(project_root: Path) -> Path:
    return project_root / SESSION_FILE_REL


def ensure_session(project_root: Path, *, pid: int | None = None) -> dict:
    """Read or generate the per-terminal session id file.

    Returns dict with id/pid/started_at/hostname. If the file exists with
    a live pid, the existing record wins. If the recorded pid is dead, a
    fresh record is written.
    """
    pid = pid or os.getpid()
    path = _session_file(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = None
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = None
    if existing and isinstance(existing, dict) and _pid_alive(existing.get("pid")):
        return existing
    record = {
        "id": str(uuid.uuid4()),
        "pid": pid,
        "started_at": _now_iso(),
        "hostname": socket.gethostname(),
    }
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- queue frontmatter parsing ----------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) for a markdown file with optional
    YAML frontmatter. Empty dict if no frontmatter.

    We use a minimal YAML-subset parser to avoid the PyYAML dependency;
    this is intentional and matches `validate_no_host_tool_leaks`-style
    procedural parsing elsewhere.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    return _parse_yaml_subset(raw), body


def _parse_yaml_subset(raw: str) -> dict:
    """Tiny YAML-subset parser. Supports `key: value` flat lines,
    `key:` with a nested block of `  - item` or `  subkey: ...`, and
    skips empty lines + comments. Values are strings unless they parse
    as JSON literal.
    """
    out: dict = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                out[key] = _coerce_scalar(val)
                i += 1
                continue
            # Nested block starts on next line(s) — list or map.
            i += 1
            nested_lines: list[str] = []
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                nested_lines.append(lines[i])
                i += 1
            out[key] = _parse_nested(nested_lines)
            continue
        i += 1
    return out


def _parse_nested(lines: list[str]) -> list | dict:
    list_items: list = []
    map_items: dict = {}
    saw_dash = False
    saw_map = False
    current_item: dict | None = None
    for raw in lines:
        if not raw.strip():
            continue
        if raw.startswith("  - "):
            saw_dash = True
            tail = raw[4:].strip()
            if tail and ":" in tail and not tail.startswith("{"):
                # Inline `- key: val` starts a new map item.
                current_item = {}
                key, _, val = tail.partition(":")
                current_item[key.strip()] = _coerce_scalar(val.strip())
                list_items.append(current_item)
            else:
                list_items.append(_coerce_scalar(tail))
                current_item = None
        elif raw.startswith("    ") and current_item is not None:
            inner = raw.strip()
            if ":" in inner:
                key, _, val = inner.partition(":")
                current_item[key.strip()] = _coerce_scalar(val.strip())
        elif raw.startswith("  ") and ":" in raw:
            saw_map = True
            inner = raw[2:]
            key, _, val = inner.partition(":")
            map_items[key.strip()] = _coerce_scalar(val.strip())
    if saw_dash and not saw_map:
        return list_items
    if saw_map and not saw_dash:
        return map_items
    return list_items or map_items


def _coerce_scalar(val: str):
    if not val:
        return ""
    try:
        return json.loads(val)
    except (json.JSONDecodeError, ValueError):
        pass
    return val.strip().strip('"').strip("'")


def _dump_frontmatter(data: dict) -> str:
    """Emit the minimal-YAML form `_parse_yaml_subset` accepts. Order is
    preserved from dict insertion order so existing files stay stable.
    """
    out = ["---"]
    for key, value in data.items():
        out.extend(_dump_pair(key, value, 0))
    out.append("---")
    out.append("")
    return "\n".join(out)


def _dump_pair(key: str, value, depth: int) -> list[str]:
    indent = "  " * depth
    if isinstance(value, dict):
        lines = [f"{indent}{key}:"]
        for k, v in value.items():
            lines.extend(_dump_pair(k, v, depth + 1))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{indent}{key}: []"]
        lines = [f"{indent}{key}:"]
        for item in value:
            if isinstance(item, dict):
                first = True
                for k, v in item.items():
                    prefix = f"{indent}  - " if first else f"{indent}    "
                    lines.append(f"{prefix}{k}: {_dump_scalar(v)}")
                    first = False
            else:
                lines.append(f"{indent}  - {_dump_scalar(item)}")
        return lines
    return [f"{indent}{key}: {_dump_scalar(value)}"]


def _dump_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if any(c in s for c in [":", "#", "\n"]) or s.strip() != s:
        return json.dumps(s)
    return s


# --- queue discovery + state aggregation ------------------------------------


def find_queues(project_root: Path) -> list[Path]:
    return sorted(project_root.glob(QUEUE_GLOB))


def find_resume_notes(project_root: Path) -> list[Path]:
    return sorted(project_root.glob(RESUME_NOTES_GLOB))


def newest(paths: list[Path]) -> Path | None:
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def read_queue_state(path: Path) -> dict:
    """Return parsed frontmatter for a goal-queue file, or {} on parse error."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    front, _ = _parse_frontmatter(text)
    return front


def queue_active_now(front: dict, *, ttl_days: int = 7) -> tuple[bool, str]:
    """Return (is_active, reason). Active iff `state: active` AND last-touched
    within ttl. `state: active` without last-touched is treated as active.
    """
    state = str(front.get("state", "")).lower()
    if state != "active":
        return False, f"state={state or 'missing'}"
    last = front.get("last-touched") or front.get("last_touched")
    if not last:
        return True, "active (no last-touched stamp)"
    try:
        ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True, "active (last-touched unparseable)"
    age_s = (datetime.now(timezone.utc) - ts).total_seconds()
    if age_s > ttl_days * 86400:
        return False, f"active but last-touched > {ttl_days}d ago (treat abandoned)"
    return True, f"active (last-touched {int(age_s)}s ago)"


def aggregate_status(project_root: Path, *, ttl_days: int = 7) -> dict:
    """Union the three signals (queue / leases / resume-notes) and return
    a single verdict with full breakdown. See module docstring.
    """
    queues = find_queues(project_root)
    newest_queue = newest(queues)
    queue_front: dict = {}
    queue_active = False
    queue_reason = "no queue files"
    if newest_queue is not None:
        queue_front = read_queue_state(newest_queue)
        queue_active, queue_reason = queue_active_now(queue_front, ttl_days=ttl_days)
    leases_for_project = _active_leases_for(project_root)
    notes = find_resume_notes(project_root)
    newest_notes = newest(notes)
    active = queue_active or bool(leases_for_project)
    return {
        "active": active,
        "queue_file": str(newest_queue.relative_to(project_root)) if newest_queue else None,
        "queue_state": queue_front.get("state"),
        "queue_reason": queue_reason,
        "queue_slug": queue_front.get("slug"),
        "queue_last_touched": queue_front.get("last-touched") or queue_front.get("last_touched"),
        "queue_current_session": queue_front.get("current_session"),
        "active_leases_in_project": len(leases_for_project),
        "active_lease_dispatch_ids": [l.get("dispatch_id") for l in leases_for_project],
        "newest_resume_notes": str(newest_notes.relative_to(project_root)) if newest_notes else None,
        "ttl_days": ttl_days,
    }


def _active_leases_for(project_root: Path) -> list[dict]:
    """Call goalflight_capacity.py status --json, return active leases
    whose project_root matches ours. Best-effort: empty list on any failure.
    """
    try:
        import subprocess

        out = subprocess.run(
            ["python3", str(ROOT / "scripts/goalflight_capacity.py"), "status", "--json"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return []
    leases = data.get("leases") or {}
    target = str(project_root.resolve())
    matched: list[dict] = []
    for _id, lease in leases.items():
        if lease.get("state") != "active":
            continue
        lp = lease.get("project_root")
        if lp and str(Path(lp).resolve()) == target:
            matched.append(lease)
    return matched


def to_text(status: dict) -> str:
    if not status["active"]:
        if status["queue_file"] is None:
            return "no goal-flight queue files; not an active session"
        return (
            f"no active goal-flight session (queue {status['queue_file']} "
            f"state={status['queue_state'] or 'unset'}; "
            f"{status['queue_reason']})"
        )
    pieces = [
        f"active goal-flight session ({status['queue_slug'] or 'unnamed'})",
        f"queue={status['queue_file']}",
        f"leases={status['active_leases_in_project']}",
    ]
    if status["queue_last_touched"]:
        pieces.append(f"last-touched={status['queue_last_touched']}")
    return "; ".join(pieces)


# --- claim / release --------------------------------------------------------


def claim(project_root: Path, queue: Path, *, force: bool = False) -> tuple[bool, str]:
    """Stamp current session into queue frontmatter. Refuses on live owner."""
    if not queue.exists():
        return False, f"queue not found: {queue}"
    text = queue.read_text()
    front, body = _parse_frontmatter(text)
    if not front:
        return False, f"queue {queue.name} has no frontmatter to stamp into"
    session = ensure_session(project_root)
    current = front.get("current_session")
    if isinstance(current, dict) and current.get("id") and current.get("id") != session["id"]:
        owner_alive = _pid_alive(current.get("pid"))
        if owner_alive and not force:
            return False, (
                f"queue already claimed by session {current.get('id')} "
                f"(pid {current.get('pid')} alive); pass --force to take over"
            )
    history = list(front.get("session_history") or [])
    history.append({
        "id": session["id"],
        "pid": session["pid"],
        "started_at": session["started_at"],
        "claimed_at": _now_iso(),
        "ended_at": None,
        "ended_reason": None,
    })
    front["current_session"] = {
        "id": session["id"],
        "pid": session["pid"],
        "started_at": session["started_at"],
        "hostname": session["hostname"],
    }
    front["session_history"] = history
    front["last-touched"] = _now_iso()
    queue.write_text(_dump_frontmatter(front) + body)
    return True, f"claimed by session {session['id']}"


def release(project_root: Path, queue: Path | None, *, reason: str = "user-exit") -> tuple[bool, str]:
    """Mark session ended in queue frontmatter (if --queue) and clear the
    per-terminal session file."""
    msgs: list[str] = []
    if queue is not None and queue.exists():
        text = queue.read_text()
        front, body = _parse_frontmatter(text)
        if front:
            history = list(front.get("session_history") or [])
            current = front.get("current_session")
            if isinstance(current, dict) and current.get("id"):
                session_id = current.get("id")
                for entry in reversed(history):
                    if isinstance(entry, dict) and entry.get("id") == session_id and entry.get("ended_at") is None:
                        entry["ended_at"] = _now_iso()
                        entry["ended_reason"] = reason
                        break
            front["current_session"] = None
            front["session_history"] = history
            front["last-touched"] = _now_iso()
            queue.write_text(_dump_frontmatter(front) + body)
            msgs.append(f"released queue {queue.name}")
    sf = _session_file(project_root)
    if sf.exists():
        sf.unlink()
        msgs.append("cleared session file")
    if not msgs:
        return False, "nothing to release"
    return True, "; ".join(msgs)


def force_release_stale(project_root: Path) -> tuple[int, list[str]]:
    """Across all goal-queues, clear current_session where pid is dead."""
    cleared: list[str] = []
    for queue in find_queues(project_root):
        front, body = _parse_frontmatter(queue.read_text())
        current = front.get("current_session")
        if isinstance(current, dict) and current.get("pid") and not _pid_alive(current.get("pid")):
            history = list(front.get("session_history") or [])
            for entry in reversed(history):
                if isinstance(entry, dict) and entry.get("id") == current.get("id") and entry.get("ended_at") is None:
                    entry["ended_at"] = _now_iso()
                    entry["ended_reason"] = "stale-pid"
                    break
            front["current_session"] = None
            front["session_history"] = history
            front["last-touched"] = _now_iso()
            queue.write_text(_dump_frontmatter(front) + body)
            cleared.append(queue.name)
    return len(cleared), cleared


# --- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight session status helper")
    parser.add_argument("--project-root", default=str(Path.cwd()))
    parser.add_argument("--ttl-days", type=int, default=7)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true")
    mode.add_argument("--text", action="store_true")
    mode.add_argument("--ensure-session", action="store_true")
    mode.add_argument("--claim", action="store_true")
    mode.add_argument("--release", action="store_true")
    mode.add_argument("--force-release-stale", action="store_true")
    parser.add_argument("--queue")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reason", default="user-exit")
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).resolve()

    if args.ensure_session:
        record = ensure_session(project_root)
        print(json.dumps(record))
        return 0

    if args.claim:
        if not args.queue:
            parser.error("--claim requires --queue")
        ok, msg = claim(project_root, Path(args.queue).resolve(), force=args.force)
        print(msg)
        return 0 if ok else 2

    if args.release:
        queue = Path(args.queue).resolve() if args.queue else None
        ok, msg = release(project_root, queue, reason=args.reason)
        print(msg)
        return 0 if ok else 2

    if args.force_release_stale:
        count, names = force_release_stale(project_root)
        print(json.dumps({"cleared": count, "files": names}))
        return 0

    status = aggregate_status(project_root, ttl_days=args.ttl_days)
    if args.json or not args.text:
        # Default to JSON for machine consumers; --text for humans.
        if args.text:
            print(to_text(status))
        else:
            print(json.dumps(status, indent=2))
        return 0
    print(to_text(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
