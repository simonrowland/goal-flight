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

A session id ties an orchestrator invocation to the run. The id lives in
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

import goalflight_compat

ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE_REL = Path("docs-private/.goal-flight-current-session.json")
QUEUE_GLOB = "docs-private/goal-queue-*.md"
RESUME_NOTES_GLOB = "docs-private/RESUME-NOTES-*.md"


# --- session id (per-terminal) ----------------------------------------------


def _session_file(project_root: Path) -> Path:
    return project_root / SESSION_FILE_REL


def ensure_session(project_root: Path, *, pid: int | None = None) -> dict:
    """Read or generate the per-terminal session id record.

    Per-terminal scope: the session record is keyed by `(project_root, pid)`.
    The file lives at `project_root/docs-private/.goal-flight-current-session.json`
    but the persisted shape is a MAP of `pid -> record`, so two terminals in
    the same project_root each have their own slot. Within a single PID the
    record persists across compactions; across PIDs they are independent.

    Returns dict with id/pid/started_at/hostname for the CURRENT PID. If the
    file already has a record for this PID (e.g., earlier command in the
    same terminal), returns it. Otherwise creates a fresh record.

    The session-file mutation runs under a session-file lock + atomic
    write with a unique temp path. Two concurrent ensure_session()s from
    different terminals serialize on the lock, then merge their writes
    safely (each adds its own pid slot without clobbering the other).
    """
    pid = pid or os.getpid()
    path = _session_file(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        data: dict[str, dict] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                raw = None
            # Back-compat: previous shape was a single record without a pid map.
            # If we find that, migrate it under its own pid key.
            if isinstance(raw, dict):
                if "id" in raw and "pid" in raw and not all(
                    isinstance(v, dict) for v in raw.values()
                ):
                    data = {str(raw.get("pid")): raw}
                else:
                    # Map-shape: keys are pid strings, values are records.
                    data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        key = str(pid)
        if key in data:
            # Existing record for this PID — return it as-is. Pruning of
            # dead-pid slots from OTHER terminals is a maintenance concern
            # handled by --force-release-stale, not the ensure_session path
            # (which is hot — runs on every CLI invocation in a goal-flight
            # terminal).
            result = data[key]
        else:
            result = {
                "id": str(uuid.uuid4()),
                "pid": pid,
                "started_at": _now_iso(),
                "hostname": socket.gethostname(),
            }
            data[key] = result
        # Atomic write via unique temp file rename. Unique suffix prevents
        # concurrent ensure_session()s from clobbering each other's temp
        # files (lock-serialized but defensive).
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)
    return result


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    return goalflight_compat.pid_alive(pid)


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
    notes_active, notes_reason = _resume_notes_active(newest_notes, ttl_days=ttl_days)
    active = queue_active or bool(leases_for_project) or notes_active
    backlog_counts, backlog_error = _task_backlog_counts(project_root)
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
        "resume_notes_active": notes_active,
        "resume_notes_reason": notes_reason,
        "backlog_counts": backlog_counts,
        "backlog_error": backlog_error,
        "ttl_days": ttl_days,
    }


def _task_backlog_counts(project_root: Path) -> tuple[dict[str, int] | None, str | None]:
    tasks_path = project_root / "docs-private" / "tasks.jsonl"
    if not tasks_path.exists():
        return {"deferred": 0, "held": 0, "blocked": 0}, None
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import goalflight_task

        rows = goalflight_task.list(project_root=project_root)
    except Exception as exc:
        return None, f"{tasks_path}: {exc}"

    def done_reviewed(row: dict) -> bool:
        return row.get("done_reviewed") is True or (row.get("kind") == "decision" and row.get("done") is True)

    counts = {"deferred": 0, "held": 0, "blocked": 0}
    for row in rows:
        if done_reviewed(row):
            continue
        lane = row.get("lane")
        if lane in ("deferred", "held"):
            counts[lane] += 1
            continue
        if row.get("derived_status") == "waiting":
            counts["blocked"] += 1
    return counts, None


def _backlog_counts_text(status: dict) -> str | None:
    if status.get("backlog_counts") is None and status.get("backlog_error"):
        return "backlog: store read degraded"
    counts = status.get("backlog_counts") or {}
    parts = []
    for label in ("deferred", "held", "blocked"):
        value = int(counts.get(label) or 0)
        if value > 0:
            parts.append(f"{value} {label}")
    return " · ".join(parts) if parts else None


def _resume_notes_active(notes_path: Path | None, *, ttl_days: int = 7) -> tuple[bool, str]:
    """Read the newest RESUME-NOTES file and infer activation state from its
    front matter (if YAML) or its TL;DR section. Tolerant by design: if the
    file is unparseable or has no signal, returns (False, "no signal").

    Signals (any one is enough for active=True):
      - YAML frontmatter `state: active` (canonical)
      - First H1 / TL;DR section contains "**Status:** active" or
        "**Active**" or "in flight" line
      - File mtime within TTL AND title matches a date stamp (heuristic)

    Reads at most 2KB to avoid pulling in entire long notes files.
    """
    if notes_path is None or not notes_path.exists():
        return False, "no resume notes"
    try:
        head = notes_path.read_text(encoding="utf-8", errors="ignore")[:2048]
    except OSError:
        return False, "resume notes unreadable"
    # Try YAML frontmatter first.
    if head.startswith("---\n"):
        front, _ = _parse_frontmatter(head)
        state = str(front.get("state", "")).lower()
        if state == "active":
            return True, "frontmatter state: active"
        if state in ("complete", "done", "completed", "abandoned"):
            return False, f"frontmatter state: {state}"
    head_lower = head.lower()
    # Look for explicit active/complete signals in TL;DR-style prose.
    if "**status:** active" in head_lower or "status: active" in head_lower:
        return True, "TL;DR Status: active"
    if "in flight" in head_lower or "in-flight" in head_lower:
        return True, "TL;DR mentions in-flight"
    if (
        "**status:** complete" in head_lower
        or "all chunks done" in head_lower
        or "push pending" in head_lower
        or "all chunks committed" in head_lower
    ):
        return False, "TL;DR complete-state signal"
    return False, "no signal"


def _active_leases_for(project_root: Path) -> list[dict]:
    """Call goalflight_capacity.py status --json, return active leases
    whose project_root matches ours. Best-effort: empty list on any failure.
    """
    try:
        import subprocess

        out = subprocess.run(
            [goalflight_compat.python_executable(), str(ROOT / "scripts/goalflight_capacity.py"), "status", "--json"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return []
    # capacity status JSON: `{"active": [<lease>, ...]}` already filtered.
    active = data.get("active") or []
    target = str(project_root.resolve())
    matched: list[dict] = []
    for lease in active:
        if lease.get("state") and lease.get("state") != "active":
            continue
        lp = lease.get("project_root")
        if lp and str(Path(lp).resolve()) == target:
            matched.append(lease)
    return matched


def to_text(status: dict) -> str:
    counts_text = _backlog_counts_text(status)
    if not status["active"]:
        if status["queue_file"] is None:
            text = "no goal-flight queue files; not an active session"
        else:
            text = (
            f"no active goal-flight session (queue {status['queue_file']} "
            f"state={status['queue_state'] or 'unset'}; "
            f"{status['queue_reason']})"
            )
        return f"{text}; {counts_text}" if counts_text else text
    pieces = [
        f"active goal-flight session ({status['queue_slug'] or 'unnamed'})",
        f"queue={status['queue_file']}",
        f"leases={status['active_leases_in_project']}",
    ]
    if status["queue_last_touched"]:
        pieces.append(f"last-touched={status['queue_last_touched']}")
    if counts_text:
        pieces.append(counts_text)
    return "; ".join(pieces)


# --- claim / release --------------------------------------------------------


def _validate_queue_in_project(project_root: Path, queue: Path) -> Path | None:
    """Resolve queue path and ensure it lives under project_root/docs-private/.
    Returns the resolved path on success, None if it escapes scope.
    Review A P3: refuse out-of-scope --queue arguments.
    """
    target = queue.resolve()
    expected_root = (project_root / "docs-private").resolve()
    try:
        target.relative_to(expected_root)
    except ValueError:
        return None
    return target


def _file_lock(path: Path):
    """Per-file lock using fcntl.flock. Context-manager that opens
    `path.lock` and acquires an exclusive lock; releases on exit.
    Two concurrent claims on the same queue now serialize.

    Network filesystems (NFS, SMB) sometimes refuse `fcntl.flock` with
    `ENOLCK` or `EOPNOTSUPP`. We catch those, emit a single stderr
    diagnostic, and fall through to lock-free execution. The race is
    bounded by the atomic write semantics (temp + rename) so even
    without locking, two concurrent writers degrade to "last-writer-
    wins on the lost slot" rather than crash.
    """
    import contextlib
    import goalflight_compat as fcntl

    @contextlib.contextmanager
    def _ctx():
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError as exc:
            sys.stderr.write(
                f"goalflight_session_status: lock open failed for {lock_path} "
                f"({exc.__class__.__name__}: {exc}); proceeding lock-free. "
                "On a network FS without flock support, two concurrent "
                "writers can lose a slot; recover with --force-release-stale.\n"
            )
            yield
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                sys.stderr.write(
                    f"goalflight_session_status: flock unsupported on "
                    f"{lock_path} ({exc.__class__.__name__}); proceeding lock-free.\n"
                )
                yield
                return
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass  # already unlocked / unsupported
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    return _ctx()


def claim(project_root: Path, queue: Path, *, force: bool = False) -> tuple[bool, str]:
    """Stamp current session into queue frontmatter. Refuses on live owner.

    Uses an exclusive file lock to serialize concurrent claims. Reads + mutates
    + writes the queue inside the lock so two terminals' claims can't both
    win. Verifies the queue path is under project_root/docs-private (A P3).
    """
    if not queue.exists():
        return False, f"queue not found: {queue}"
    resolved = _validate_queue_in_project(project_root, queue)
    if resolved is None:
        return False, (
            f"queue {queue} is outside {project_root}/docs-private/; refusing"
        )
    queue = resolved
    with _file_lock(queue):
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
        _atomic_write(queue, _dump_frontmatter(front) + body)
    return True, f"claimed by session {session['id']}"


def _atomic_write(path: Path, content: str) -> None:
    """Write atomically via temp + rename — avoids torn writes if interrupted."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def release(project_root: Path, queue: Path | None, *, reason: str = "user-exit") -> tuple[bool, str]:
    """Mark session ended in queue frontmatter (if --queue) and clear the
    per-terminal session file.

    Compare-after-read: only releases when current_session.id matches THIS
    terminal's session id; refuses if a different session owns it. Use
    --force-release-stale or claim --force to take over instead.
    """
    msgs: list[str] = []
    if queue is not None and queue.exists():
        resolved = _validate_queue_in_project(project_root, queue)
        if resolved is None:
            return False, f"queue {queue} is outside {project_root}/docs-private/; refusing"
        queue = resolved
        my_session = ensure_session(project_root)
        with _file_lock(queue):
            text = queue.read_text()
            front, body = _parse_frontmatter(text)
            if front:
                current = front.get("current_session")
                if isinstance(current, dict) and current.get("id"):
                    if current.get("id") != my_session["id"]:
                        return False, (
                            f"queue current_session={current.get('id')} is not "
                            f"this terminal's session ({my_session['id']}); "
                            "refusing to release. Use --force-release-stale "
                            "or claim --force to take over."
                        )
                history = list(front.get("session_history") or [])
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
                _atomic_write(queue, _dump_frontmatter(front) + body)
                msgs.append(f"released queue {queue.name}")
    # Per-terminal session map: remove only this PID's slot, keep others.
    # Lock + atomic write — same discipline as ensure_session.
    sf = _session_file(project_root)
    if sf.exists():
        with _file_lock(sf):
            try:
                raw = json.loads(sf.read_text())
            except (json.JSONDecodeError, OSError):
                raw = None
            my_pid = str(os.getpid())
            if isinstance(raw, dict):
                if "id" in raw and "pid" in raw and not all(
                    isinstance(v, dict) for v in raw.values()
                ):
                    # Old single-record shape (back-compat) — drop the whole file.
                    sf.unlink()
                    msgs.append("cleared session file (back-compat)")
                else:
                    # Map shape: remove only this pid's slot.
                    if my_pid in raw:
                        del raw[my_pid]
                    if raw:
                        _atomic_write(sf, json.dumps(raw, indent=2) + "\n")
                    else:
                        sf.unlink()
                    msgs.append("cleared this terminal's session slot")
    if not msgs:
        return False, "nothing to release"
    return True, "; ".join(msgs)


def force_release_stale(project_root: Path) -> tuple[int, list[str]]:
    """Across all goal-queues, clear current_session where pid is dead.
    Locks each queue for the duration of its mutation."""
    cleared: list[str] = []
    for queue in find_queues(project_root):
        with _file_lock(queue):
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
                _atomic_write(queue, _dump_frontmatter(front) + body)
                cleared.append(queue.name)
    return len(cleared), cleared


# --- CLI --------------------------------------------------------------------


def _default_project_root() -> str:
    """Cwd-stable default for --project-root: prefer the git toplevel of
    the current working directory; fall back to cwd if not in a git repo.
    Sweep C P1 fix — invocations from subdirs now resolve to the repo
    root automatically.
    """
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    return str(Path.cwd())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight session status helper")
    parser.add_argument("--project-root", default=_default_project_root())
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
