"""File-IPC contract for INLINE ACP permission authorization.

When an ACP worker runs with ``permission_mode="inline"`` (AcpProcessPool /
managed_pool / spawn_acp_connection / GoalflightClient), the orchestrator's
permission router does NOT immediately deny+re-dispatch a boundary-crossing
request. Instead the worker's ``request_permission`` handler:

  1. writes a *request* file into a shared directory, then
  2. HOLDS the ACP permission open (the worker keeps its slot -- probe 14 of
     2026-05-21 confirmed codex-acp tolerates a long-held ``request_permission``)
     while it polls for a *decision* file the orchestrator writes after asking the
     user, then
  3. returns the REAL outcome (the chosen allow option, or a deny).

If no decision arrives within the inline timeout the worker FALLS BACK to the
auto path (deny + surface as USER-CONFIRM for re-dispatch) so the permission
channel can never wedge -- the "previously it hung" invariant holds even here.

This module is the pure, dependency-free contract BOTH sides share:
  - worker side  (goalflight_acp_client.request_permission):
        write_request -> poll read_decision -> clear
  - orchestrator side (orchestrator / goalflight_acp_run relay):
        list_requests -> write_ack (optional, defer to user) -> write_decision

Files (each written atomically via a same-dir temp + os.replace, so a reader
never sees a partial file):
    <dir>/<key>.request.json     written by worker,     read by orchestrator
    <dir>/<key>.decision.json    written by orchestrator, read by worker
where ``key = sanitize(session_id) + "." + sanitize(tool_call_id | uuid)``.

Directory selection (``permission_dir``):
  - explicit arg wins (the cross-process topology: a headless goalflight_acp_run
    worker and a separate orchestrator MUST pass the SAME --permission-dir);
  - else ``$GOAL_FLIGHT_PERMISSION_DIR``;
  - else ``<tmpdir>/goal-flight-perms.d/<pid>`` -- PID-scoped so the common
    in-process pool topology (orchestrator == relay == this process) auto-isolates
    concurrent orchestrators without cross-reading each other's requests. A
    cross-process relay can NOT discover a PID-scoped default, so that topology
    requires an explicit dir.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

REQUEST_SCHEMA = "goalflight.permission_request.v1"
DECISION_SCHEMA = "goalflight.permission_decision.v1"
ACK_SCHEMA = "goalflight.permission_ack.v1"
REQUEST_SUFFIX = ".request.json"
DECISION_SUFFIX = ".decision.json"
ACK_SUFFIX = ".ack.json"

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
_DECISIONS = (DECISION_ALLOW, DECISION_DENY)

ENV_PERMISSION_DIR = "GOAL_FLIGHT_PERMISSION_DIR"
_DEFAULT_DIRNAME = "goal-flight-perms.d"
_MAX_JSON_BYTES = 1024 * 1024

# Filename-safe key component. Keep it short so the full path stays well under
# any filesystem name limit even with both suffixes appended.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _sanitize(value: str, *, limit: int = 100) -> str:
    cleaned = _UNSAFE.sub("_", str(value)).strip("._")
    return cleaned[:limit] or "_"


def permission_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the permission IPC directory (see module docstring for policy)."""
    if explicit:
        return Path(explicit)
    env = os.environ.get(ENV_PERMISSION_DIR)
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / _DEFAULT_DIRNAME / str(os.getpid())


def make_key(session_id: str | None, tool_call_id: str | None) -> str:
    """Filename-safe key UNIQUE to one permission round-trip.

    Encodes session_id + tool_call_id (for human-readable correlation in the
    record/files) PLUS a fresh uuid suffix so the key is unique even when a worker
    reuses a tool_call_id across re-dispatches. Uniqueness matters: a decision the
    orchestrator writes a hair too late (after the worker already timed out + cleared
    and started a new cycle) would otherwise be read as the answer to the NEW
    request. A fresh key per call closes that stale-reply window. The orchestrator
    always uses ``record["key"]`` verbatim, so it does not need to reconstruct it."""
    sid = _sanitize(session_id or "nosession")
    tid = _sanitize(tool_call_id) if tool_call_id else "tc"
    return f"{sid}.{tid}.{uuid.uuid4().hex[:8]}"


def request_path(directory: str | os.PathLike[str], key: str) -> Path:
    return Path(directory) / f"{key}{REQUEST_SUFFIX}"


def decision_path(directory: str | os.PathLike[str], key: str) -> Path:
    return Path(directory) / f"{key}{DECISION_SUFFIX}"


def ack_path(directory: str | os.PathLike[str], key: str) -> Path:
    return Path(directory) / f"{key}{ACK_SUFFIX}"


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Temp name ends in .tmp (NOT in a *.request.json / *.decision.json suffix)
    # so an in-flight write is never globbed as a complete record. os.replace is
    # atomic on POSIX, so a reader sees the old file or the new one, never a
    # half-written one.
    tmp = path.parent / f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        # flush + fsync the temp file BEFORE the atomic rename: the rename is
        # metadata-atomic, but on filesystems that defer data a crash right after
        # write() could leave a renamed-but-empty/partial record. Liveness doesn't
        # depend on this (the inline timeout falls back to re-dispatch), but it
        # makes the durable-write contract correct by the book (codex review P2).
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, default=str))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_JSON_BYTES:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_request(directory: str | os.PathLike[str], record: dict[str, Any]) -> Path:
    """Worker side: publish a pending permission request. ``record`` MUST carry a
    ``key`` (use make_key); ``schema``/``created_at`` are stamped here."""
    key = record.get("key")
    if not key:
        raise ValueError("permission request record requires a 'key'")
    payload = {"schema": REQUEST_SCHEMA, "created_at": time.time(), **record}
    path = request_path(directory, key)
    _atomic_write_json(path, payload)
    return path


def read_request(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    return _read_json(Path(path))


def list_requests(directory: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Orchestrator side: every pending request, oldest-first. Skips requests that
    already have a decision (the worker hasn't cleared them yet) so a relay does
    not double-answer. Each record carries its ``key`` for write_decision."""
    d = Path(directory)
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in d.glob(f"*{REQUEST_SUFFIX}"):
        rec = _read_json(p)
        if rec is None:
            continue
        key = rec.get("key") or p.name[: -len(REQUEST_SUFFIX)]
        if read_decision(d, key) is not None:
            continue  # already answered, awaiting worker cleanup
        rec["key"] = key
        rec["acked"] = read_ack(d, key)
        out.append(rec)
    out.sort(key=lambda r: (float(r.get("created_at") or 0.0), str(r.get("key"))))
    return out


def write_decision(
    directory: str | os.PathLike[str],
    key: str,
    decision: str,
    option_id: str | None = None,
) -> Path:
    """Orchestrator side: answer a pending request. ``decision`` is 'allow' (with an
    optional ``option_id`` naming the offered allow option) or 'deny'.

    First writer wins for regular decision files. A pre-existing non-regular
    entry is replaced so reads cannot wedge on an invalid decision path.
    """
    if decision not in _DECISIONS:
        raise ValueError(f"decision must be one of {_DECISIONS!r}, got {decision!r}")
    payload = {
        "schema": DECISION_SCHEMA,
        "key": key,
        "decision": decision,
        "option_id": option_id,
        "decided_at": time.time(),
    }
    path = decision_path(directory, key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # First writer wins ONLY for a legitimate regular decision file.
    with contextlib.suppress(OSError):
        st = os.lstat(path)
        if stat.S_ISREG(st.st_mode):
            if read_decision(directory, key) is not None:
                return path
            with contextlib.suppress(OSError):
                os.unlink(path)

    # A pre-existing NON-regular entry (FIFO/symlink) is not a legit decision and
    # would force every read to fail until timeout; remove it (we own the 0700 dir)
    # so the real decision can be published. Leave a directory alone (don't rmtree);
    # the exclusive create below will simply fail and we return.
    with contextlib.suppress(OSError):
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) and not stat.S_ISDIR(st.st_mode):
            os.unlink(path)

    data = json.dumps(payload, default=str)
    tmp = path.parent / f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)  # atomic + exclusive (fails if path exists)
        except FileExistsError:
            return path  # a regular decision landed first
        except OSError:
            # No-hardlink filesystem: exclusive create directly. Accept a tiny
            # partial-read window (read_decision validates JSON and keeps polling).
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return path
            try:
                os.write(fd, data.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        return path
    finally:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()


def read_decision(directory: str | os.PathLike[str], key: str) -> dict[str, Any] | None:
    """Worker side: the orchestrator's decision for ``key``, or None if not yet
    written / malformed. A malformed file returns None (the worker keeps polling
    until the inline timeout, then falls back) rather than mis-resolving."""
    rec = _read_json(decision_path(directory, key))
    if (
        rec is not None
        and rec.get("schema") == DECISION_SCHEMA
        and rec.get("key") == key
        and rec.get("decision") in _DECISIONS
    ):
        return rec
    return None


def write_ack(directory: str | os.PathLike[str], key: str) -> Path:
    """Orchestrator side: signal 'request received, presenting to the user' so the
    worker extends its hold to the user-decision window. Idempotent (re-ack is a
    no-op overwrite)."""
    payload = {"schema": ACK_SCHEMA, "acked_at": time.time(), "key": key}
    p = ack_path(directory, key)
    _atomic_write_json(p, payload)
    return p


def read_ack(directory: str | os.PathLike[str], key: str) -> bool:
    """Worker side: True if a valid ack file exists for key."""
    rec = _read_json(ack_path(directory, key))
    return bool(
        rec is not None
        and rec.get("schema") == ACK_SCHEMA
        and rec.get("key") == key
    )


def clear(directory: str | os.PathLike[str], key: str) -> None:
    """Worker side: remove the request + decision files after resolving (or after
    falling back). Best-effort; missing files are fine."""
    for path in (request_path(directory, key), decision_path(directory, key), ack_path(directory, key)):
        with contextlib.suppress(OSError):
            path.unlink()


# Files older than this are swept as cruft. Far larger than any sane hold
# (DEFAULT_INLINE_PERMISSION_TIMEOUT_S is minutes), so a live round-trip is never
# swept; this only reaps leftovers from crashes or the narrow timeout/late-write
# race (an orphan decision for an already-cleared key is harmless either way --
# keys are unique per call, so it can never be mis-read as a new request's answer).
DEFAULT_SWEEP_AGE_S = 3600.0


def sweep(directory: str | os.PathLike[str], max_age_s: float = DEFAULT_SWEEP_AGE_S) -> int:
    """Best-effort cruft removal: delete request/decision/temp files in `directory`
    older than `max_age_s`. Returns the count removed. Safe to call opportunistically
    (one cheap directory listing); never touches files younger than the cutoff, so a
    live hold is untouched."""
    d = Path(directory)
    if not d.is_dir():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for path in d.iterdir():
        name = path.name
        if not (
            name.endswith(REQUEST_SUFFIX)
            or name.endswith(DECISION_SUFFIX)
            or name.endswith(ACK_SUFFIX)
            or name.endswith(".tmp")
        ):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
