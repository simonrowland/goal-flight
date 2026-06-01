#!/usr/bin/env python3
"""Goal-flight ACP SDK wrapper.

Owns only process management, SDK connection setup, liveness observation, and
pidfile cleanup. Runner policy stays in ``goalflight_acp_run.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import inspect
import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Protocol

import goalflight_compat
import goalflight_acp_permits as permits
from goalflight_adapter_readiness import validate_os_sandbox_request
from goalflight_liveness import active_monotonic
from goalflight_os_sandbox import OS_SANDBOX_OFF, canonical_os_sandbox, prepare_os_sandbox_command


log = logging.getLogger("goal-flight.acp_client")

_VERSION = "0.4.5-sdk"
DEFAULT_ACP_LIMIT = 32 * 1024 * 1024
DEFAULT_PERMISSION_TIMEOUT_S = 30.0
# Inline permission mode, phase 1: controller-responsiveness window. How long the
# worker HOLDS its ACP permission open (awake-time) waiting for the controller to
# ack OR decide before AUTO-DECLINING (deny + worker continues, no re-dispatch).
# 3 min assumes the controller polls+acks each turn (see acp_pool.managed_pool
# "CONTROLLER CONTRACT"); short enough that a rate-limited/asleep controller never
# blocks a worker on its own provider, long enough for the controller to get a turn.
DEFAULT_INLINE_PERMISSION_TIMEOUT_S = 180.0
# Post-ACK user-decision window (awake-time): how long the worker waits for the
# human after the controller acks, before auto-declining. Long by design (a
# coffee break); still bounded so a forgotten ack can't hold a slot forever.
DEFAULT_USER_PERMISSION_TIMEOUT_S = 36000.0  # 10h
INLINE_PERMISSION_POLL_S = 0.5
INLINE_HOLD_GRACE_S = 30.0
PERMISSION_MODE_AUTO = "auto"
PERMISSION_MODE_INLINE = "inline"
_PERMISSION_MODES = (PERMISSION_MODE_AUTO, PERMISSION_MODE_INLINE)
TERMINAL_TOOL_STATUSES = {"completed", "failed", "cancelled"}
WEDGE_PROGRESS_KINDS = {
    "agent_message_chunk",
    "agent_thought_chunk",
    "tool_call",
    "tool_call_update",
    "plan",
}
class _ClientBase(Protocol):
    pass


try:  # Import lazily enough that non-ACP commands still run without the SDK.
    from acp import (  # type: ignore
        Client as _SdkClient,
        PROTOCOL_VERSION,
        RequestError,
        connect_to_agent,
        text_block,
    )
    from acp.connection import StreamDirection, StreamEvent  # type: ignore
    from acp.schema import (  # type: ignore
        AllowedOutcome,
        ClientCapabilities,
        DeniedOutcome,
        Implementation,
        RequestPermissionResponse,
    )

    ACP_IMPORT_ERROR: BaseException | None = None
    ClientBase = _SdkClient
except BaseException as e:  # pragma: no cover - exercised by doctor/system python
    ACP_IMPORT_ERROR = e
    PROTOCOL_VERSION = 1
    ClientBase = _ClientBase

    class RequestError(Exception):  # type: ignore[no-redef]
        @classmethod
        def method_not_found(cls, method: str) -> "RequestError":
            return cls(f"Method not found: {method}")

        @classmethod
        def invalid_params(cls, data: dict[str, Any] | None = None) -> "RequestError":
            return cls(f"Invalid params: {data}")

    StreamDirection = None  # type: ignore[assignment]
    StreamEvent = object  # type: ignore[assignment]


def require_acp_sdk() -> None:
    if ACP_IMPORT_ERROR is None:
        return
    raise AcpError(
        "SDK missing -- run install: agent-client-protocol==0.10.* must be "
        "available in ~/.goal-flight/venvs/acp-0.10/"
    ) from ACP_IMPORT_ERROR


def acp_limit_from_env() -> int:
    raw = os.environ.get("GOALFLIGHT_ACP_LIMIT")
    if not raw:
        return DEFAULT_ACP_LIMIT
    value = raw.strip().lower()
    multiplier = 1
    for suffix, scale in (("mb", 1024 * 1024), ("m", 1024 * 1024), ("kb", 1024), ("k", 1024)):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            multiplier = scale
            break
    try:
        limit = int(float(value) * multiplier)
    except ValueError as e:
        raise AcpError(f"invalid GOALFLIGHT_ACP_LIMIT={raw!r}") from e
    if limit <= 0:
        raise AcpError("GOALFLIGHT_ACP_LIMIT must be positive")
    return limit


def _ps_meta(pid: int) -> tuple[str, str] | None:
    try:
        r = subprocess.run(
            ["ps", "-o", "lstart=,comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
        if r.returncode != 0:
            return None
        line = r.stdout.strip()
        if not line:
            return None
        parts = line.split(None, 5)
        if len(parts) < 6:
            return None
        return " ".join(parts[:5]), parts[5]
    except Exception:
        return None


def _same_process(
    started_meta: tuple[str, str] | None,
    live_meta: tuple[str, str] | None,
) -> bool:
    """Return false only when a live process has a different start time."""
    return started_meta is None or live_meta is None or started_meta[0] == live_meta[0]


class AcpError(Exception):
    pass


class PoolExhaustedError(AcpError):
    pass


_PIDFILE_DIR = Path(
    os.environ.get(
        "GOAL_FLIGHT_PIDFILE_DIR",
        goalflight_compat.temp_base() / "goal-flight-acp-pids.d",
    )
)
_live_connections: dict[int, "GoalflightAcpConnection"] = {}
_registry_lock = threading.Lock()


def _register_connection(conn: "GoalflightAcpConnection") -> None:
    with _registry_lock:
        _live_connections[conn.proc.pid] = conn
        _write_through_pidfile_locked()


def _unregister_connection(conn: "GoalflightAcpConnection") -> None:
    with _registry_lock:
        _live_connections.pop(conn.proc.pid, None)
        _write_through_pidfile_locked()


def mark_connection_detached(pid: int) -> bool:
    """Flag a live connection as intentionally detached (D2 non-destructive stall).

    Rewrites the controller's pidfile so the worker's entry carries ``detached:
    true`` -> cleanup_ghosts (this controller's next dispatch OR any sibling
    project sharing the pidfile dir) will SKIP it instead of SIGKILLing the
    still-running, intentionally-detached worker. Returns True if the pid was a
    live tracked connection.
    """
    with _registry_lock:
        conn = _live_connections.get(pid)
        if conn is None:
            return False
        conn._detached = True
        _write_through_pidfile_locked()
        return True


def _write_through_pidfile_locked() -> None:
    try:
        _PIDFILE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("could not create pidfile dir %s: %s", _PIDFILE_DIR, e)
        return
    own_pidfile = _PIDFILE_DIR / f"{os.getpid()}.jsonl"
    entries: list[str] = []
    for conn in _live_connections.values():
        if goalflight_compat.is_windows():
            identity = goalflight_compat.windows_process_identity(conn.proc.pid)
            if identity is None:
                continue
            entries.append(
                json.dumps(
                    {
                        "pid": conn.proc.pid,
                        "pgid": conn.verified_pgid,
                        "creation_time": identity.get("creation_time"),
                        "identity_source": identity.get("identity_source"),
                        "agent": conn.agent,
                        "session_id": conn.session_id,
                        "detached": conn._detached,
                    }
                )
            )
            continue
        meta = _ps_meta(conn.proc.pid)
        if meta is None:
            continue
        lstart, comm = meta
        entries.append(
            json.dumps(
                {
                    "pid": conn.proc.pid,
                    "pgid": conn.verified_pgid,
                    "started_at": lstart,
                    "cmd": comm,
                    "agent": conn.agent,
                    "session_id": conn.session_id,
                    "detached": conn._detached,
                }
            )
        )
    try:
        if entries:
            own_pidfile.write_text("\n".join(entries) + "\n")
        else:
            own_pidfile.unlink(missing_ok=True)
    except OSError as e:
        log.warning("pidfile write failed (%s): %s", own_pidfile, e)


def cleanup_ghosts(active_worker_pids: set[int] | None = None) -> int:
    """Reap workers recorded by dead controller pidfiles."""
    if not _PIDFILE_DIR.exists():
        return 0
    own_pid = os.getpid()
    own_worker_pids = active_worker_pids or set()
    killed = 0
    skipped_stale = 0
    skipped_live_controller = 0
    skipped_detached = 0
    for pf in _PIDFILE_DIR.glob("*.jsonl"):
        try:
            controller_pid = int(pf.stem.split(".", 1)[0])
        except ValueError:
            continue
        if controller_pid == own_pid:
            continue
        if _ps_meta(controller_pid) is not None or goalflight_compat.pid_alive(controller_pid):
            skipped_live_controller += 1
            continue
        try:
            lines = pf.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = entry.get("pid")
            if not isinstance(pid, int) or pid in own_worker_pids:
                continue
            if entry.get("detached"):
                # A worker the runner intentionally DETACHED on a non-destructive
                # stall (D2): the controller exited but deliberately left the
                # worker running for re-attach, so it is NOT a ghost. Reaping it
                # here would SIGKILL a live, intentional worker (and across
                # concurrent projects sharing this pidfile dir). Leave it; the
                # capacity lease's detached_* markers drive slot reclamation when
                # the worker actually dies (see goalflight_capacity stale-release).
                skipped_detached += 1
                continue
            if goalflight_compat.is_windows():
                if not goalflight_compat.pid_alive(pid):
                    continue
                if not entry.get("creation_time"):
                    skipped_stale += 1
                    log.warning(
                        "ghost_cleanup: windows pid=%d missing creation identity; "
                        "unlinking stale pidfile without killing",
                        pid,
                    )
                    continue
                hard_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
                if goalflight_compat.kill_pid(
                    pid,
                    hard_signal,
                    process_group=False,
                    expected_identity=entry,
                ):
                    killed += 1
                else:
                    skipped_stale += 1
                continue
            meta = _ps_meta(pid)
            if meta is None:
                continue
            live_lstart, live_comm = meta
            recorded_lstart = entry.get("started_at")
            recorded_comm = entry.get("cmd")
            recorded_meta = (
                (recorded_lstart, recorded_comm)
                if isinstance(recorded_lstart, str) and isinstance(recorded_comm, str)
                else None
            )
            if recorded_meta is None or not _same_process(recorded_meta, meta):
                skipped_stale += 1
                log.warning(
                    "ghost_cleanup: pid=%d stale live=%r recorded=%r",
                    pid,
                    (live_lstart, live_comm),
                    (recorded_lstart, recorded_comm),
                )
                continue
            meta2 = _ps_meta(pid)
            if meta2 is None or not _same_process(meta, meta2):
                skipped_stale += 1
                continue
            pgid = entry.get("pgid", pid)
            agent = entry.get("agent", "")
            is_bash_tail = str(agent).endswith("-bash-tail")
            hard_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
            if is_bash_tail and pgid != pid:
                if goalflight_compat.kill_pid(pid, hard_signal, process_group=False):
                    killed += 1
            elif goalflight_compat.kill_pid(pid, hard_signal, pgid=pgid, process_group=True):
                killed += 1
        pf.unlink(missing_ok=True)
    if killed or skipped_stale or skipped_live_controller or skipped_detached:
        log.info(
            "ghost_cleanup: killed=%d skipped_stale=%d skipped_live_controllers=%d "
            "skipped_detached=%d",
            killed,
            skipped_stale,
            skipped_live_controller,
            skipped_detached,
        )
    return killed


def classify_message(message: dict[str, Any]) -> str:
    method = message.get("method")
    if method == "session/update":
        update = ((message.get("params") or {}).get("update") or {})
        return str(update.get("sessionUpdate") or update.get("session_update") or method)
    if method == "session/request_permission":
        return "request_permission"
    if method:
        return str(method)
    if "error" in message:
        return "response_error"
    if "result" in message:
        return "response"
    return "event"


def _tool_id(payload: dict[str, Any]) -> str | None:
    value = (
        payload.get("toolCallId")
        or payload.get("tool_call_id")
        or payload.get("id")
        or payload.get("title")
    )
    return str(value) if value else None


def _tool_status(payload: dict[str, Any]) -> str | None:
    value = payload.get("status")
    return str(value).lower() if value is not None else None


@dataclass
class AcpLivenessActivity:
    permission_timeout_s: float = DEFAULT_PERMISSION_TIMEOUT_S
    raw_events_seen: int = 0
    wedge_progress_seen: int = 0
    last_event_mono: float = field(default_factory=active_monotonic)
    last_progress_mono: float = field(default_factory=active_monotonic)
    last_event_kind: str | None = None
    outstanding_tools: dict[str, float] = field(default_factory=dict)
    pending_permissions: dict[str, float] = field(default_factory=dict)
    # Permissions the inline router is deliberately HOLDING open while it asks the
    # controller/user (permission_mode="inline"). Tracked separately from
    # pending_permissions because the semantics differ: a held inline permission
    # is healthy (the worker is intentionally paused), so it counts toward
    # outstanding_count (granting the heartbeat's silence grace) but is EXEMPT
    # from the short permission_timeout_s expiry. Each value is the hold's own
    # deadline; a stuck hold is reaped only after that deadline plus grace.
    inline_held_permissions: dict[str, float] = field(default_factory=dict)
    dropped_frames: int = 0
    turn_started_mono: float | None = None
    turn_completed_mono: float | None = None
    turn_stop_reason: str | None = None
    turn_completed_count: int = 0

    def begin_turn(self, now: float | None = None) -> None:
        now = active_monotonic() if now is None else now
        self.turn_started_mono = now
        self.turn_completed_mono = None
        self.turn_stop_reason = None

    def finish_turn(self, stop_reason: str | None = None, now: float | None = None) -> None:
        now = active_monotonic() if now is None else now
        self.turn_completed_mono = now
        self.turn_stop_reason = stop_reason or "unknown"
        self.turn_completed_count += 1

    def turn_in_flight(self) -> bool:
        return self.turn_started_mono is not None and self.turn_stop_reason is None

    def turn_silent_for(self, now: float | None = None) -> float:
        if self.turn_started_mono is None:
            return 0.0
        now = active_monotonic() if now is None else now
        return max(0.0, now - max(self.turn_started_mono, self.last_event_mono))

    def note_message(self, message: dict[str, Any], now: float | None = None) -> str:
        now = active_monotonic() if now is None else now
        kind = classify_message(message)
        self.raw_events_seen += 1
        self.last_event_mono = now
        self.last_event_kind = kind
        if kind in WEDGE_PROGRESS_KINDS:
            self.wedge_progress_seen += 1
            self.last_progress_mono = now
        self._apply_tool_activity(message, kind, now)
        return kind

    def _apply_tool_activity(self, message: dict[str, Any], kind: str, now: float) -> None:
        if kind in {"tool_call", "tool_call_update"}:
            update = ((message.get("params") or {}).get("update") or {})
            tool_id = _tool_id(update)
            status = _tool_status(update)
            if tool_id and status in TERMINAL_TOOL_STATUSES:
                self.outstanding_tools.pop(tool_id, None)
            elif tool_id:
                self.outstanding_tools.setdefault(tool_id, now)
        elif kind == "request_permission":
            params = message.get("params") or {}
            tool_call = params.get("toolCall") or params.get("tool_call") or {}
            tool_id = _tool_id(tool_call) or _tool_id(params) or str(message.get("id") or "")
            if tool_id:
                self.pending_permissions.setdefault(tool_id, now)

    def note_dropped_frame(self) -> None:
        self.dropped_frames += 1

    def reset_progress_clock(self, now: float | None = None) -> None:
        self.last_progress_mono = active_monotonic() if now is None else now

    def resolve_permission(self, tool_id: str | None = None) -> None:
        if tool_id:
            self.pending_permissions.pop(tool_id, None)
        elif self.pending_permissions:
            self.pending_permissions.clear()

    def hold_inline_permission(self, tool_id: str, deadline: float) -> None:
        """Mark a permission as held-open by the inline router (healthy pause)."""
        if tool_id:
            self.inline_held_permissions.setdefault(tool_id, deadline)

    def extend_inline_hold(self, tool_id: str, deadline: float) -> None:
        """Push out an existing inline hold's deadline (controller acked; the worker
        is now waiting on the user). No-op if the hold isn't tracked."""
        if tool_id and tool_id in self.inline_held_permissions:
            self.inline_held_permissions[tool_id] = deadline

    def release_inline_permission(self, tool_id: str | None = None) -> None:
        if tool_id:
            self.inline_held_permissions.pop(tool_id, None)
        elif self.inline_held_permissions:
            self.inline_held_permissions.clear()

    def has_inline_holds(self) -> bool:
        return bool(self.inline_held_permissions)

    def _prune_permissions(self, now: float) -> None:
        if self.permission_timeout_s <= 0:
            self.pending_permissions.clear()
            return
        for key, _ in self._expired_permissions(now):
            self.pending_permissions.pop(key, None)

    def _expired_permissions(self, now: float) -> list[tuple[str, float]]:
        if self.permission_timeout_s <= 0:
            return []
        return [
            (key, started_at)
            for key, started_at in self.pending_permissions.items()
            if now - started_at >= self.permission_timeout_s
        ]

    def outstanding_count(self, now: float | None = None) -> int:
        return (
            len(self.outstanding_tools)
            + len(self.pending_permissions)
            + len(self.inline_held_permissions)
        )

    def timed_out(self, now: float, max_tool_s: float) -> tuple[str, float] | None:
        expired_permissions = self._expired_permissions(now)
        if expired_permissions:
            tool_id, started_at = expired_permissions[0]
            for expired_id, _ in expired_permissions:
                self.pending_permissions.pop(expired_id, None)
            return tool_id, now - started_at
        if self.permission_timeout_s <= 0:
            self.pending_permissions.clear()
        for tool_id, deadline in list(self.inline_held_permissions.items()):
            age_past_deadline = now - deadline
            if now >= deadline + INLINE_HOLD_GRACE_S:
                self.inline_held_permissions.pop(tool_id, None)
                return tool_id, age_past_deadline
        if max_tool_s <= 0:
            return None
        for tool_id, started_at in self.outstanding_tools.items():
            age = now - started_at
            if age >= max_tool_s:
                return tool_id, age
        for tool_id, started_at in list(self.pending_permissions.items()):
            age = now - started_at
            if age >= max_tool_s:
                self.pending_permissions.pop(tool_id, None)
                return tool_id, age
        return None

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = active_monotonic() if now is None else now
        turn_completed_for_s = (
            max(0.0, now - self.turn_completed_mono)
            if self.turn_completed_mono is not None
            else 0.0
        )
        return {
            "raw_events_seen": self.raw_events_seen,
            "wedge_progress_seen": self.wedge_progress_seen,
            "last_event_kind": self.last_event_kind,
            "quiet_for_s": now - self.last_event_mono,
            "progress_quiet_for_s": now - self.last_progress_mono,
            "outstanding_count": self.outstanding_count(now),
            "inline_held": len(self.inline_held_permissions),
            "dropped_frames": self.dropped_frames,
            "turn_in_flight": self.turn_in_flight(),
            "turn_silent_for_s": self.turn_silent_for(now),
            "turn_stop_reason": self.turn_stop_reason,
            "turn_completed_for_s": turn_completed_for_s,
            "turn_completed_count": self.turn_completed_count,
        }


def _looks_like_json_rpc_line(line: bytes) -> bool:
    stripped = line.lstrip()
    return bool(stripped) and stripped.startswith(b"{")


class JsonRpcLineFilterReader(asyncio.StreamReader):
    """Drop stdout lines that are not JSON-RPC objects.

    Some ACP workers (notably OpenCode with the LiteLLM plugin) print human
    diagnostics to stdout during startup. The ACP SDK expects newline-delimited
    JSON only; skipping non-object lines keeps the transport stable.
    """

    def __init__(self, inner: asyncio.StreamReader) -> None:
        super().__init__()
        self._inner = inner
        self.skipped_lines = 0

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        while True:
            line = await self._inner.readuntil(separator)
            if _looks_like_json_rpc_line(line):
                return line
            self.skipped_lines += 1
            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    "skipping non-json acp stdout: %r",
                    line.decode(errors="replace").rstrip()[:200],
                )

    async def read(self, n: int = -1) -> bytes:
        return await self._inner.read(n)


class GuardedStreamReader(asyncio.StreamReader):
    """StreamReader subclass that drops over-limit newline frames and continues."""

    def __init__(
        self,
        inner: asyncio.StreamReader,
        *,
        limit: int,
        on_drop: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(limit=limit)
        self._inner = inner
        self._limit = limit
        self._on_drop = on_drop
        self.dropped_frames = 0

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        while True:
            try:
                return await self._inner.readuntil(separator)
            except asyncio.LimitOverrunError as e:
                await self._drop_oversized_frame(e, separator)

    async def read(self, n: int = -1) -> bytes:
        # LimitOverrunError is newline-frame specific and is handled by
        # readuntil(); raw read(n) has no safe separator to drain toward.
        return await self._inner.read(n)

    async def _drop_oversized_frame(self, error: asyncio.LimitOverrunError, separator: bytes) -> None:
        self.dropped_frames += 1
        log.error("dropped over-limit ACP frame (%s)", error)
        if self._on_drop is not None:
            result = self._on_drop()
            if inspect.isawaitable(result):
                await result
        consumed = max(1, int(getattr(error, "consumed", 0) or 0))
        with contextlib.suppress(asyncio.IncompleteReadError):
            await self._inner.readexactly(consumed)
        while True:
            try:
                await self._inner.readuntil(separator)
                return
            except asyncio.LimitOverrunError as e:
                consumed = max(1, int(getattr(e, "consumed", 0) or 0))
                with contextlib.suppress(asyncio.IncompleteReadError):
                    await self._inner.readexactly(consumed)
            except asyncio.IncompleteReadError:
                return


def _tc_get(obj: Any, *names: str) -> Any:
    """Read a field from an ACP tool_call/location that may be an SDK object OR a
    raw dict (snake_case and camelCase keys). Returns the first present value, so
    the policy behaves the same whether codex-acp sent a typed object (prod) or a
    dict reaches us (tests, future transports)."""
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            val = getattr(obj, name, None)
            if val is not None:
                return val
    return None


def _tool_call_locations(tool_call: Any) -> list[str]:
    """Path strings from an ACP ToolCall/ToolCallUpdate ``locations`` array
    (each entry is {path, line?}), tolerating both SDK objects and dicts."""
    out: list[str] = []
    for loc in _tc_get(tool_call, "locations") or []:
        path = _tc_get(loc, "path")
        if path:
            out.append(str(path))
    return out


def _path_outside_cwd(raw: str, root: Path) -> bool:
    """True if ``raw`` is NOT within ``root`` (already ``Path(cwd).resolve()``)
    after symlink resolution -- the same resolve()+is_relative_to test the
    existing scope-leak audit (_scan_out_of_scope_paths) uses. Resolving BOTH
    sides makes a symlink cwd and a symlink-inside-cwd escape compare
    consistently (no case/spelling mismatch from mixing resolved vs lexical
    forms). Relative paths resolve against root. Fails CLOSED: an
    unresolvable/uncomparable path is treated as outside (escalate), never
    silently in-scope."""
    try:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        return not p.resolve().is_relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return True  # cannot classify -> fail closed (escalate)


def _targets_outside_cwd(tool_call: Any, cwd: str | None) -> list[str]:
    """Location paths that are NOT PROVABLY inside the worker's cwd (the worktree
    sandbox). Relative paths resolve against cwd. Fails CLOSED: when there are
    located targets but no usable boundary (cwd empty/unresolvable), EVERY target
    counts as outside -- we cannot prove in-scope, so we escalate rather than
    silently allow. No locations -> nothing to prove -> empty (benign)."""
    locs = _tool_call_locations(tool_call)
    if not locs:
        return []
    if not cwd:
        return list(locs)  # no boundary to prove against -> fail closed
    try:
        root = Path(cwd).resolve()
    except (OSError, ValueError, TypeError):
        return list(locs)  # unresolvable cwd -> fail closed
    return [raw for raw in locs if _path_outside_cwd(raw, root)]


def _same_dir(a: str | None, b: str | None) -> bool:
    """True if a and b resolve to the same directory. Fail-safe: unknown/
    unresolvable -> False (rebuild rather than reuse a wrong-cwd worker).
    Compares realpath at call time; assumes cwd is a stable directory, not a
    symlink retargeted mid-session (goal-flight cwds are worktrees, not mutable
    symlinks)."""
    if not a or not b:
        return False
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


# Permission router decisions. The controller (this client) auto-allows obvious
# in-scope work so the worker perceives no delay, denies hardcoded-dangerous
# operations, and ESCALATES genuinely boundary-crossing requests to the user.
PERMISSION_ALLOW = "allow"
PERMISSION_DENY = "deny"
PERMISSION_ESCALATE = "escalate"

# Tool kinds that MODIFY state (ACP ToolKind). A write-like permission whose
# targets we cannot see (no locations) cannot be proven in-worktree -> escalate.
_WRITE_KINDS = frozenset({"edit", "delete", "move"})
_READ_SAFE_KINDS = frozenset({"", "read", "search", "think"})


def default_permission_policy(tool_call: Any, options: list[Any], cwd: str | None) -> str:
    """Scope-aware default permission policy (controller-as-auto-mode router).

    Auto-allow only bounded in-worktree writes and read-safe/no-side-effect kinds.
    Escalate shell/network side effects (execute/fetch), unknown/future kinds, and
    writes whose targets cannot be proven in-worktree. This is a fail-closed
    allowlist:
      - any tool target NOT provably inside cwd          -> escalate
      - fetch / execute                                 -> escalate
      - edit/delete/move with in-cwd locations          -> allow
      - edit/delete/move with NO locations              -> escalate
      - "", read, search, think                         -> allow
      - other / switch_mode / unknown                   -> escalate

    Returns PERMISSION_ALLOW / PERMISSION_ESCALATE (a custom policy may also
    return PERMISSION_DENY). Replaceable per-dispatch via
    GoalflightClient(permission_policy=...) so the orchestrator can fold in chunk
    SCOPE/FORBIDDEN and re-dispatch decisions.

    NOTE (deliberate): a kindless request with no locations (kind == "") is
    AUTO-ALLOWED -- this is the shape of an in-workspace MCP elicitation (e.g.
    context-mode ctx_index 'Approve Index Content'), auto-allowed so the worker
    does not wedge. The residual risk (a misbehaving / non-codex adapter sending
    a state-changing action with NO kind AND NO locations would also auto-allow;
    codex-acp never does -- its edits carry kind+locations) is accepted and
    bounded by (a) a custom permission_policy for security-strict deployments and
    (b) the worker's OS sandbox. When OS sandbox is enabled, use
    permission_policy_for_dispatch() instead — it auto-allows in-worktree execute/
    fetch because sandbox-exec is the backstop. To fail closed on kindless, supply
    a policy that escalates when kind == "".
    """
    if _targets_outside_cwd(tool_call, cwd):
        return PERMISSION_ESCALATE
    kind = str(_tc_get(tool_call, "kind") or "")
    locations = _tool_call_locations(tool_call)
    if kind in {"fetch", "execute"}:
        return PERMISSION_ESCALATE
    if kind in _WRITE_KINDS:
        return PERMISSION_ESCALATE if not locations else PERMISSION_ALLOW
    if kind in _READ_SAFE_KINDS:
        return PERMISSION_ALLOW
    return PERMISSION_ESCALATE


def permission_policy_for_dispatch(
    os_sandbox: str,
    *,
    base: Callable[[Any, list[Any], str | None], str] | None = None,
) -> Callable[[Any, list[Any], str | None], str]:
    """Permission router for a dispatch, keyed on OS sandbox posture.

    Without OS sandbox, shell/network side effects (execute/fetch) escalate to the
    controller. With sandbox-exec wrapping the worker subprocess, in-worktree
    execute and fetch may auto-allow because the OS fence is the backstop.
    """
    base_policy = base or default_permission_policy
    sandbox_on = canonical_os_sandbox(os_sandbox) != OS_SANDBOX_OFF

    def policy(tool_call: Any, options: list[Any], cwd: str | None) -> str:
        if sandbox_on:
            if _targets_outside_cwd(tool_call, cwd):
                return PERMISSION_ESCALATE
            kind = str(_tc_get(tool_call, "kind") or "")
            if kind in {"fetch", "execute"}:
                return PERMISSION_ALLOW
        return base_policy(tool_call, options, cwd)

    return policy


class GoalflightClient(ClientBase):  # type: ignore[misc, valid-type]
    def __init__(
        self,
        *,
        activity: AcpLivenessActivity | None = None,
        auto_allow_tools: bool = True,
        turn_queue: asyncio.Queue[dict[str, Any]] | None = None,
        cwd: str | None = None,
        permission_policy: Callable[[Any, list[Any], str | None], str] | None = None,
        permission_mode: str = PERMISSION_MODE_AUTO,
        permission_dir: str | os.PathLike[str] | None = None,
        permission_inline_timeout_s: float | None = None,
        permission_user_timeout_s: float | None = None,
    ) -> None:
        self.activity = activity or AcpLivenessActivity()
        self.auto_allow_tools = auto_allow_tools
        self.turn_queue = turn_queue
        self.typed_updates: list[dict[str, Any]] = []
        # Permission router: cwd defines the worktree boundary; permission_policy
        # is the controller's decision function (default = scope-aware). Escalated
        # requests are recorded here for the runner to surface to the user.
        # A plain list is safe (no lock): request_permission (the appender) and
        # run_prompt (the reader/clearer) both run as coroutines on the SAME
        # asyncio event loop -- the acp SDK dispatches requests via asyncio tasks,
        # not a worker thread -- so append/list/clear never interleave mid-call.
        self.cwd = cwd
        self.permission_policy = permission_policy
        self.permission_escalations: list[dict[str, Any]] = []
        # Requests the controller auto-declined because it did not answer the
        # inline hold in time (or the IPC failed). Informational only -- the
        # worker was given a deny and CONTINUED; this does NOT trigger re-dispatch.
        self.permission_auto_declined: list[dict[str, Any]] = []
        # Escalation TRANSPORT. "auto" (default): an escalated request is answered
        # with a cancel immediately and surfaced via permission_escalations ->
        # USER-CONFIRM -> re-dispatch. "inline": HOLD the request open, publish it
        # via file IPC (goalflight_acp_permits), poll for the controller's decision,
        # and return the real outcome IN PLACE -- it never re-dispatches. Two-phase
        # awake-time timeout: controller window (permission_inline_timeout_s) then,
        # on a controller ack, user window (permission_user_timeout_s); each expiry
        # AUTO-DECLINES (deny + the worker continues, recorded in
        # permission_auto_declined). Inline requires the router
        # (auto_allow_tools=True); with auto_allow_tools=False every request is
        # denied before the router runs.
        self.permission_mode = (
            permission_mode if permission_mode in _PERMISSION_MODES else PERMISSION_MODE_AUTO
        )
        self.permission_dir = (
            permits.permission_dir(permission_dir)
            if self.permission_mode == PERMISSION_MODE_INLINE
            else None
        )
        if permission_inline_timeout_s is None:
            self.permission_inline_timeout_s = DEFAULT_INLINE_PERMISSION_TIMEOUT_S
        else:
            try:
                inline_timeout = float(permission_inline_timeout_s)
            except (TypeError, ValueError):
                log.warning(
                    "invalid inline permission timeout %r; using default %.0fs",
                    permission_inline_timeout_s,
                    DEFAULT_INLINE_PERMISSION_TIMEOUT_S,
                )
                inline_timeout = DEFAULT_INLINE_PERMISSION_TIMEOUT_S
            if not math.isfinite(inline_timeout) or inline_timeout <= 0:
                log.warning(
                    "invalid inline permission timeout %r; using default %.0fs",
                    permission_inline_timeout_s,
                    DEFAULT_INLINE_PERMISSION_TIMEOUT_S,
                )
                inline_timeout = DEFAULT_INLINE_PERMISSION_TIMEOUT_S
            self.permission_inline_timeout_s = inline_timeout
        if permission_user_timeout_s is None:
            self.permission_user_timeout_s = DEFAULT_USER_PERMISSION_TIMEOUT_S
        else:
            try:
                user_timeout = float(permission_user_timeout_s)
            except (TypeError, ValueError):
                log.warning(
                    "invalid user permission timeout %r; using default %.0fs",
                    permission_user_timeout_s,
                    DEFAULT_USER_PERMISSION_TIMEOUT_S,
                )
                user_timeout = DEFAULT_USER_PERMISSION_TIMEOUT_S
            if not math.isfinite(user_timeout) or user_timeout <= 0:
                log.warning(
                    "invalid user permission timeout %r; using default %.0fs",
                    permission_user_timeout_s,
                    DEFAULT_USER_PERMISSION_TIMEOUT_S,
                )
                user_timeout = DEFAULT_USER_PERMISSION_TIMEOUT_S
            self.permission_user_timeout_s = user_timeout
        self.permission_inline_poll_s = INLINE_PERMISSION_POLL_S

    def set_turn_queue(self, queue: asyncio.Queue[dict[str, Any]] | None) -> None:
        self.turn_queue = queue

    def observe_stream_event(self, event: Any) -> None:
        if StreamDirection is not None and event.direction != StreamDirection.INCOMING:
            return
        message = dict(event.message)
        kind = self.activity.note_message(message)
        if self.turn_queue is not None:
            self.turn_queue.put_nowait({"source": "observer", "kind": kind, "message": message})

    @staticmethod
    def _select_allow_option(options: list[Any]) -> str | None:
        """Pick the option id to auto-grant, or None if none is grantable.

        Prefer the least-privilege ALLOW (allow_once > allow_always), then ANY
        explicit allow_* kind (covers future allow_* kinds), in offered order.
        NEVER auto-select a ``reject_*`` option: real adapters send e.g.
        codex-acp's ``[allow_once 'approved', reject_once 'abort']`` (no
        allow_always), and a worker may offer them reject-first -- the old
        ``options[0]`` fallback would then turn an auto-allow into an auto-DENY.
        Returns None when only reject options exist; the caller then cancels
        cleanly (still a definitive answer, so the worker never wedges).
        """
        opts = list(options or [])
        for pref in ("allow_once", "allow_always"):
            for opt in opts:
                if _tc_get(opt, "kind") == pref:
                    option_id = _tc_get(opt, "option_id", "optionId")
                    if option_id:
                        return option_id
        for opt in opts:
            kind = _tc_get(opt, "kind")
            option_id = _tc_get(opt, "option_id", "optionId")
            # Fail closed: only an explicit allow_* kind may be auto-granted (this
            # catches any future allow_* beyond allow_always/allow_once). A
            # kindless or unknown kind -- cancel / defer / deny_once / ... -- must
            # NOT be treated as allow-like, or an auto-allow could approve a deny
            # variant. Those fall through to a clean DeniedOutcome(cancelled).
            if option_id and kind is not None and str(kind).startswith("allow_"):
                return option_id
        return None

    async def request_permission(self, options: list[Any], session_id: str, tool_call: Any, **kwargs: Any) -> Any:
        tool_id = _tc_get(tool_call, "tool_call_id", "toolCallId", "id")
        # Always clear pending-permission liveness tracking: we ARE answering the
        # request, on every path below (grant OR deny).
        self.activity.resolve_permission(str(tool_id) if tool_id else None)
        if not self.auto_allow_tools:
            # Deny cleanly rather than raising method_not_found. method_not_found
            # advertises "this client has no permission method at all", and some
            # adapters (the 0.3.0-era codex-acp) then HANG on the unanswered gate
            # -- the "every worker hangs on its first tool call" regression. A
            # DeniedOutcome is a definitive answer: the worker cancels the gated
            # call and proceeds/fails instead of wedging. auto_allow_tools=False
            # means "do not auto-grant", which is exactly a deny.
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        # Controller-as-auto-mode router: decide allow / deny / escalate. The
        # decision must ALWAYS resolve the (synchronous) request promptly so the
        # worker never wedges on the permission channel -- escalation answers with
        # a cancel and is surfaced to the user out-of-band (runner -> blocked ->
        # USER-CONFIRM -> re-dispatch), never by holding the request open.
        policy = self.permission_policy or default_permission_policy
        title = _tc_get(tool_call, "title") or "?"
        try:
            decision = policy(tool_call, options, self.cwd)
        except Exception:
            log.exception("permission policy raised for %s; escalating", title)
            decision = PERMISSION_ESCALATE
        if decision == PERMISSION_ALLOW:
            chosen_id = self._select_allow_option(options)
            if chosen_id:
                log.info("auto-allow permission (in scope): %s -> optionId=%s", title, chosen_id)
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", option_id=chosen_id)
                )
            log.warning("permission allow but no allow option offered (%s); cancelling", title)
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if decision == PERMISSION_ESCALATE:
            record = {
                "key": permits.make_key(session_id, str(tool_id) if tool_id else None),
                "tool_call_id": str(tool_id) if tool_id else None,
                "session_id": session_id,
                "title": title,
                "kind": _tc_get(tool_call, "kind"),
                "cwd": self.cwd,
                "locations": _tool_call_locations(tool_call),
                "targets_outside_cwd": _targets_outside_cwd(tool_call, self.cwd),
                "options": [
                    {"kind": _tc_get(o, "kind"), "option_id": _tc_get(o, "option_id", "optionId")}
                    for o in (options or [])
                ],
            }
            if self.permission_mode == PERMISSION_MODE_INLINE:
                # HOLD the request open and authorize it IN PLACE via the controller
                # (file IPC). Returns a definitive outcome on EVERY normal path -- a
                # decision (allow/deny) OR an auto-decline-deny on timeout/IPC-error
                # (the worker then continues; never re-dispatches). Returns None ONLY
                # when inline is unconfigured (no permission_dir); that lone case
                # falls through to the auto escalate path below.
                outcome = await self._await_inline_decision(record, options, tool_id, session_id)
                if outcome is not None:
                    return outcome
                log.info("inline mode has no permission_dir; escalating instead: %s", title)
            self.permission_escalations.append(record)
            # Wake run_prompt immediately so it surfaces the escalation now rather
            # than on its next ~1s poll. The event carries no "message"; the loop
            # treats it as a no-op and re-checks escalations at the top. (Same
            # event loop as this handler, so this is ordered after the append.)
            if self.turn_queue is not None:
                with contextlib.suppress(Exception):
                    self.turn_queue.put_nowait({"source": "permission_escalation"})
            log.info("escalate permission to controller/user: %s", title)
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        # PERMISSION_DENY (a custom policy rejecting a hardcoded-dangerous op).
        log.info("deny permission (policy): %s", title)
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    def _outcome_from_decision(self, decision: dict[str, Any], options: list[Any]) -> Any:
        """Map a controller decision file to an ACP outcome. An ``allow`` honors
        the named option_id ONLY if it is an allow option. A missing id falls back
        to the safe allow selector; a reject/unknown id fails closed."""
        if decision.get("decision") == permits.DECISION_ALLOW:
            offered = set()
            for option in (options or []):
                kind = _tc_get(option, "kind")
                option_id = _tc_get(option, "option_id", "optionId")
                if isinstance(kind, str) and kind.startswith("allow_") and option_id:
                    offered.add(option_id)
            chosen = decision.get("option_id")
            if chosen and chosen in offered:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", option_id=chosen)
                )
            if not chosen:
                chosen = self._select_allow_option(options)
                if chosen:
                    return RequestPermissionResponse(
                        outcome=AllowedOutcome(outcome="selected", option_id=chosen)
                    )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def _await_inline_decision(
        self, record: dict[str, Any], options: list[Any], tool_id: Any, session_id: str
    ) -> Any | None:
        """Hold the ACP permission open across two awake-time phases: a short
        controller ACK window, then a longer post-ACK user-decision window.
        Returns an ACP outcome on a decision, or a deny on timeout / IPC error.
        The held permission is registered with the liveness activity so the
        heartbeat treats the pause as healthy, not as a wedge."""
        directory = self.permission_dir
        if directory is None:
            return None
        key = record["key"]
        hold_id = str(tool_id) if tool_id else key
        ack_deadline = active_monotonic() + self.permission_inline_timeout_s
        self.activity.hold_inline_permission(hold_id, ack_deadline)
        acked = False
        deadline = ack_deadline
        try:
            # Opportunistic cruft removal (one cheap listing): reap orphan files
            # from crashes or the timeout/late-write race. Never touches a live
            # round-trip (only files older than DEFAULT_SWEEP_AGE_S).
            with contextlib.suppress(Exception):
                permits.sweep(directory)
            permits.write_request(directory, record)
            # Nudge any in-process relay that watches the turn queue; harmless to
            # a controller that polls the directory instead.
            if self.turn_queue is not None:
                with contextlib.suppress(Exception):
                    self.turn_queue.put_nowait(
                        {"source": "permission_inline_request", "key": key}
                    )
            while True:
                got = permits.read_decision(directory, key)
                if got is not None:
                    return self._outcome_from_decision(got, options)
                if not acked and permits.read_ack(directory, key):
                    # An ack noticed up to one poll interval after the controller deadline still
                    # extends -- intentional: a controller that acked at the edge IS alive, so honor
                    # it rather than auto-decline a live controller on a sub-second timing race.
                    acked = True
                    deadline = active_monotonic() + self.permission_user_timeout_s
                    self.activity.extend_inline_hold(hold_id, deadline)
                    log.info(
                        "inline permission ACKed by controller; extending to "
                        "user-decision window (%.0fs awake): %s",
                        self.permission_user_timeout_s,
                        record.get("title"),
                    )
                if active_monotonic() >= deadline:
                    got = permits.read_decision(directory, key)
                    if got is not None:
                        return self._outcome_from_decision(got, options)
                    reason = "user_timeout" if acked else "controller_timeout"
                    self.permission_auto_declined.append({
                        "key": record.get("key"),
                        "tool_call_id": record.get("tool_call_id"),
                        "title": record.get("title"),
                        "kind": record.get("kind"),
                        "reason": reason,
                        "timeout_s": (self.permission_user_timeout_s if acked
                                      else self.permission_inline_timeout_s),
                    })
                    log.warning(
                        "inline permission auto-declined (%s) after %.0fs awake-time; "
                        "worker continues without the tool: %s",
                        reason,
                        (self.permission_user_timeout_s if acked else self.permission_inline_timeout_s),
                        record.get("title"),
                    )
                    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
                await asyncio.sleep(self.permission_inline_poll_s)
        except asyncio.CancelledError:
            # The handler was cancelled mid-hold (event-loop / connection teardown).
            # request_permission is a SYNCHRONOUS gate for the worker: if we
            # propagate without answering, a still-alive worker is left waiting on
            # the permission and wedges. Answer with a definitive deny (a returned
            # value here suppresses the cancellation, which is correct for an RPC
            # handler that must always reply) so the worker can never hang. The
            # finally below still releases the hold and clears the IPC files.
            log.info("inline permission cancelled; denying so the worker stays answerable")
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        except Exception:
            log.exception("inline permission IPC failed (%s); auto-declining so the worker continues", record.get("title"))
            self.permission_auto_declined.append({
                "key": record.get("key"),
                "tool_call_id": record.get("tool_call_id"),
                "title": record.get("title"),
                "kind": record.get("kind"),
                "reason": "ipc_error",
                "timeout_s": self.permission_inline_timeout_s,
            })
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        finally:
            self.activity.release_inline_permission(hold_id)
            with contextlib.suppress(Exception):
                permits.clear(directory, key)

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        update_dict = update.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
            exclude_unset=True,
        )
        event = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update_dict},
        }
        self.typed_updates.append(event)

    async def write_text_file(self, *args: Any, **kwargs: Any) -> None:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(self, *args: Any, **kwargs: Any) -> None:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(self, *args: Any, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, *args: Any, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, *args: Any, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/kill")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


@dataclass
class GoalflightAcpConnection:
    agent: str
    session_id: str
    proc: asyncio.subprocess.Process
    conn: Any
    client: GoalflightClient
    guarded_reader: GuardedStreamReader
    verified_pgid: int
    verbose: bool = False
    auto_allow_tools: bool = True
    context_mode: bool = True
    os_sandbox: str = OS_SANDBOX_OFF
    os_sandbox_metadata: dict[str, Any] | None = None
    acp_session_id: str | None = None
    cwd: str | None = None
    reusable: bool = True
    last_active: float = field(default_factory=time.time)
    session_reset: bool = False
    _started_meta: tuple[str, str] | None = None
    _stderr_task: asyncio.Task | None = None
    _registered: bool = False
    # Set when the runner intentionally DETACHES this worker on a non-destructive
    # stall (D2): the pidfile entry is then marked detached so cleanup_ghosts will
    # NOT reap the still-running worker after the controller exits.
    _detached: bool = False

    def __post_init__(self) -> None:
        self._started_meta = _ps_meta(self.proc.pid)
        _register_connection(self)
        self._registered = True
        if self.proc.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def _drain_stderr(self) -> None:
        if self.proc.stderr is None:
            return
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                if self.verbose:
                    log.debug("acp_stderr: %s", line.decode(errors="replace").rstrip()[:300])
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def initialize(self, timeout: float = 60.0) -> Any:
        require_acp_sdk()
        try:
            return await asyncio.wait_for(
                self.conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="goal-flight", version=_VERSION),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            raise AcpError(
                f"initialize: no response within {timeout:.0f}s -- worker likely wedged in handshake"
            ) from e
        except Exception as e:
            raise AcpError(f"initialize failed: {e}") from e

    async def new_session(self, cwd: str, timeout: float = 60.0) -> str:
        require_acp_sdk()
        self.cwd = cwd
        try:
            response = await asyncio.wait_for(
                self.conn.new_session(cwd=cwd, mcp_servers=[]),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            raise AcpError(
                f"session/new: no response within {timeout:.0f}s -- worker likely wedged in handshake"
            ) from e
        except Exception as e:
            raise AcpError(f"session/new failed: {e}") from e
        self.acp_session_id = response.session_id
        return self.acp_session_id

    async def session_new(self, cwd: str, timeout: float = 60.0) -> str:
        return await self.new_session(cwd, timeout=timeout)

    async def prompt(self, text: str) -> Any:
        require_acp_sdk()
        if not self.acp_session_id:
            raise AcpError("prompt called before new_session")
        self.client.activity.begin_turn(active_monotonic())
        try:
            response = await self.conn.prompt(session_id=self.acp_session_id, prompt=[text_block(text)])
        except asyncio.CancelledError:
            self.client.activity.finish_turn("cancelled", active_monotonic())
            raise
        except Exception:
            self.client.activity.finish_turn("error", active_monotonic())
            raise
        self.client.activity.finish_turn(getattr(response, "stop_reason", None), active_monotonic())
        return response

    async def cancel(self, session_id: str | None = None) -> None:
        if not self.acp_session_id and not session_id:
            return
        await self.conn.cancel(session_id=session_id or self.acp_session_id)

    async def close_gracefully(self, soft_timeout: float = 2.0) -> None:
        if self.alive and self.acp_session_id is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.conn.close_session(self.acp_session_id), timeout=soft_timeout)
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
        except Exception:
            pass
        if self.alive:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), timeout=soft_timeout)
        await self.kill()

    async def kill(self) -> None:
        if self.alive:
            live_meta = _ps_meta(self.proc.pid)
            if not _same_process(self._started_meta, live_meta):
                log.warning(
                    "kill skipped: pid=%d identity changed live=%r recorded=%r",
                    self.proc.pid,
                    live_meta,
                    self._started_meta,
                )
            else:
                try:
                    hard_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
                    killed = goalflight_compat.kill_pid(
                        self.proc.pid,
                        hard_signal,
                        pgid=self.verified_pgid,
                        process_group=True,
                    )
                    if not killed:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            self.proc.kill()
                except (ProcessLookupError, PermissionError):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        self.proc.kill()
                with contextlib.suppress(Exception):
                    await self.proc.wait()
        if self._registered:
            with contextlib.suppress(Exception):
                _unregister_connection(self)
            self._registered = False
        current = asyncio.current_task()
        if self._stderr_task and self._stderr_task is not current and not self._stderr_task.done():
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        with contextlib.suppress(Exception):
            await self.conn.close()

    async def __aenter__(self) -> "GoalflightAcpConnection":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close_gracefully()

    async def ping(self, timeout: float = 5.0) -> bool:
        if not self.alive:
            return False
        try:
            await asyncio.wait_for(self.conn.ext_method("ping", {}), timeout=timeout)
            return True
        except Exception:
            return self.alive


# codex-acp routes MCP-server elicitation (request_user_input) into a hang
# UNLESS told to surface it through the ACP permission channel. Without this flag
# an MCP tool that elicits -- e.g. context-mode's ctx_index -- wedges the worker
# on its first tool call: codex-acp neither forwards the elicitation over ACP
# (even when the client advertises ClientCapabilities.elicitation) nor rejects it
# the way `codex exec` does ("request_user_input is not supported in exec mode");
# the tool_call stays in_progress at ~0% CPU forever (reproduced + bisected
# 2026-05-21). With the flag the elicitation arrives as a session/request_permission
# (title "Approve <tool>", standard allow_*/reject_* options) that
# GoalflightClient.request_permission auto-allows, so the tool completes.
_ELICIT_KEY = "features.tool_call_mcp_elicitation"
_CTX_ENABLED_KEY = "mcp_servers.context-mode.enabled"
# Back-compat: the only flag injected before the context-mode toggle existed.
CODEX_ACP_ELICITATION_ARGS = ["-c", f"{_ELICIT_KEY}=true"]


def _strip_c_key(acp_args: list[str], key: str) -> list[str]:
    """Drop any `-c <key>=...` pair (codex's two-token form) from acp_args so our
    injected value is the ONLY one for that key -- order-independent and not
    defeatable by a stray/conflicting caller arg."""
    out: list[str] = []
    i, n = 0, len(acp_args)
    while i < n:
        if (
            acp_args[i] == "-c"
            and i + 1 < n
            and str(acp_args[i + 1]).split("=", 1)[0].strip() == key
        ):
            i += 2
            continue
        out.append(acp_args[i])
        i += 1
    return out


def ensure_codex_acp_args(command: str, acp_args: list[str], *, context_mode: bool = True) -> list[str]:
    """Guarantee codex-acp's MCP posture at the SINGLE spawn boundary, for every
    caller (runner agent_command, AcpProcessPool config, custom launcher). No-op
    for any other adapter.

    context_mode=True (default): route MCP-server elicitation through the ACP
    permission channel (features.tool_call_mcp_elicitation=true) so an eliciting
    tool surfaces as an answerable permission instead of wedging the worker.
    context_mode=False: disable the context-mode MCP server for THIS worker
    (mcp_servers.context-mode.enabled=false) -- no MCP elicitation surface at all.
    The controller chooses per dispatch (goalflight_acp_run --context-mode).

    The chosen flag is appended LAST after stripping any caller value for the same
    key, so a conflicting/stray caller arg can't defeat the guarantee.
    """
    if os.path.basename(str(command)) != "codex-acp":
        return acp_args
    # Strip BOTH related keys first so a caller's OPPOSITE-posture arg can't
    # survive (e.g. a stray enabled=false in context_mode=True), then append the
    # single flag for the chosen posture last (last-wins + conflict-free).
    stripped = _strip_c_key(_strip_c_key(acp_args, _ELICIT_KEY), _CTX_ENABLED_KEY)
    if context_mode:
        return [*stripped, "-c", f"{_ELICIT_KEY}=true"]
    return [*stripped, "-c", f"{_CTX_ENABLED_KEY}=false"]


# Back-compat alias (pre-toggle name); thin wrapper over ensure_codex_acp_args.
def ensure_codex_acp_elicitation(command: str, acp_args: list[str]) -> list[str]:
    return ensure_codex_acp_args(command, acp_args, context_mode=True)


async def spawn_acp_connection(
    command: str,
    acp_args: list[str],
    *,
    agent: str,
    session_id: str,
    cwd: str,
    auto_allow_tools: bool = True,
    verbose: bool = False,
    activity: AcpLivenessActivity | None = None,
    permission_policy: Callable[[Any, list[Any], str | None], str] | None = None,
    permission_mode: str = PERMISSION_MODE_AUTO,
    permission_dir: str | os.PathLike[str] | None = None,
    permission_inline_timeout_s: float | None = None,
    permission_user_timeout_s: float | None = None,
    context_mode: bool = True,
    os_sandbox: str = OS_SANDBOX_OFF,
    env: dict[str, str] | None = None,
) -> GoalflightAcpConnection:
    if goalflight_compat.is_windows():
        raise AcpError(goalflight_compat.windows_dispatch_refusal())
    require_acp_sdk()
    acp_args = ensure_codex_acp_args(command, acp_args, context_mode=context_mode)
    limit = acp_limit_from_env()
    os.makedirs(cwd, exist_ok=True)
    sandboxed = prepare_os_sandbox_command(
        command,
        acp_args,
        cwd=cwd,
        os_sandbox=os_sandbox,
        agent=agent,
    )
    proc = await asyncio.create_subprocess_exec(
        sandboxed.command,
        *sandboxed.args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        start_new_session=True,
        limit=limit,
        env=env,
    )
    if proc.stdin is None or proc.stdout is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise AcpError("failed to create ACP stdio pipes")
    try:
        verified_pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError) as e:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise AcpError(f"could not verify process group for pid={proc.pid}") from e
    if verified_pgid != proc.pid:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise AcpError(
            f"process group isolation failed: pid={proc.pid} pgid={verified_pgid}; "
            "start_new_session=True did not produce a session leader"
        )

    activity = activity or AcpLivenessActivity()
    effective_policy = permission_policy or permission_policy_for_dispatch(sandboxed.profile)
    client = GoalflightClient(
        activity=activity,
        auto_allow_tools=auto_allow_tools,
        cwd=cwd,
        permission_policy=effective_policy,
        permission_mode=permission_mode,
        permission_dir=permission_dir,
        permission_inline_timeout_s=permission_inline_timeout_s,
        permission_user_timeout_s=permission_user_timeout_s,
    )
    stdout_reader: asyncio.StreamReader = proc.stdout
    if os.path.basename(str(sandboxed.command)) == "opencode":
        stdout_reader = JsonRpcLineFilterReader(proc.stdout)
    guarded_reader = GuardedStreamReader(stdout_reader, limit=limit, on_drop=activity.note_dropped_frame)
    conn = connect_to_agent(
        client,
        proc.stdin,
        guarded_reader,
        observers=[client.observe_stream_event],
    )
    return GoalflightAcpConnection(
        agent=agent,
        session_id=session_id,
        proc=proc,
        conn=conn,
        client=client,
        guarded_reader=guarded_reader,
        verified_pgid=verified_pgid,
        verbose=verbose,
        auto_allow_tools=auto_allow_tools,
        context_mode=context_mode,
        os_sandbox=sandboxed.profile,
        os_sandbox_metadata=sandboxed.metadata(),
        cwd=cwd,
    )


class AcpProcessPool:
    def __init__(
        self,
        agents_config: dict[str, Any],
        max_processes: int = 20,
        max_per_agent: int = 10,
        verbose: bool = False,
        auto_allow_tools: bool = False,
        permission_policy: Callable[[Any, list[Any], str | None], str] | None = None,
        permission_mode: str = PERMISSION_MODE_AUTO,
        permission_dir: str | os.PathLike[str] | None = None,
        permission_inline_timeout_s: float | None = None,
        permission_user_timeout_s: float | None = None,
        context_mode: bool = True,
        os_sandbox: str = OS_SANDBOX_OFF,
    ) -> None:
        self._config = agents_config
        self._max = max_processes
        self._max_per_agent = max_per_agent
        self._verbose = verbose
        self._auto_allow_tools = auto_allow_tools
        self._permission_policy = permission_policy
        self._permission_mode = permission_mode
        self._permission_dir = permission_dir
        self._permission_inline_timeout_s = permission_inline_timeout_s
        self._permission_user_timeout_s = permission_user_timeout_s
        self._context_mode = context_mode
        self._os_sandbox = canonical_os_sandbox(os_sandbox)
        self._connections: dict[tuple[str, str], GoalflightAcpConnection] = {}

    def _count_agent(self, agent: str) -> int:
        return sum(1 for (a, _) in self._connections if a == agent)

    async def get_or_create(
        self,
        agent: str,
        session_id: str,
        cwd: str = "",
        context_mode: bool | None = None,
        os_sandbox: str | None = None,
    ) -> GoalflightAcpConnection:
        # Per-dispatch context-mode override (defaults to the pool's). A reused
        # connection carries the launch posture it was spawned with, so it can
        # only be returned when the requested posture matches -- otherwise rebuild
        # (a worker spawned with context-mode enabled can't serve a disabled
        # dispatch, and vice versa).
        effective_context_mode = self._context_mode if context_mode is None else context_mode
        effective_os_sandbox = self._os_sandbox if os_sandbox is None else canonical_os_sandbox(os_sandbox)
        key = (agent, session_id)
        agent_cfg = self._config.get(agent)
        workdir = cwd or (agent_cfg.get("working_dir", "/tmp") if agent_cfg else "/tmp")
        conn = self._connections.get(key)
        if (
            conn
            and conn.alive
            and conn.reusable
            and conn.context_mode == effective_context_mode
            and conn.os_sandbox == effective_os_sandbox
            and _same_dir(conn.cwd, workdir)
        ):
            return conn
        is_rebuild = conn is not None
        if conn:
            self._connections.pop(key, None)
            with contextlib.suppress(Exception):
                await conn.kill()
        if len(self._connections) >= self._max:
            raise PoolExhaustedError(f"global limit reached ({self._max})")
        if self._count_agent(agent) >= self._max_per_agent:
            raise PoolExhaustedError(f"per-agent limit reached for {agent} ({self._max_per_agent})")
        if not agent_cfg:
            raise AcpError(f"agent not found: {agent}")
        os_sandbox_gate = validate_os_sandbox_request(agent, effective_os_sandbox)
        if os_sandbox_gate is not None:
            raise AcpError(f"os sandbox blocked: {json.dumps(os_sandbox_gate, sort_keys=True)}")
        command = agent_cfg["command"]
        acp_args = agent_cfg.get("acp_args", [agent_cfg.get("acp_arg", "acp")])
        new_conn = await spawn_acp_connection(
            command,
            acp_args,
            agent=agent,
            session_id=session_id,
            cwd=workdir,
            auto_allow_tools=self._auto_allow_tools,
            verbose=self._verbose,
            permission_policy=self._permission_policy,
            permission_mode=self._permission_mode,
            permission_dir=self._permission_dir,
            permission_inline_timeout_s=self._permission_inline_timeout_s,
            permission_user_timeout_s=self._permission_user_timeout_s,
            context_mode=effective_context_mode,
            os_sandbox=effective_os_sandbox,
        )
        if is_rebuild:
            new_conn.session_reset = True
        try:
            await new_conn.initialize()
            await new_conn.new_session(workdir)
        except Exception:
            with contextlib.suppress(Exception):
                await new_conn.kill()
            raise
        self._connections[key] = new_conn
        return new_conn

    async def close(self, agent: str, session_id: str) -> None:
        conn = self._connections.pop((agent, session_id), None)
        if conn:
            await conn.kill()

    def remove(self, agent: str, session_id: str) -> None:
        self._connections.pop((agent, session_id), None)

    async def cleanup_idle(self, ttl_seconds: float) -> None:
        cutoff = time.time() - ttl_seconds
        stale = [k for k, c in self._connections.items() if c.last_active < cutoff]
        for key in stale:
            conn = self._connections.pop(key)
            await conn.kill()

    async def health_check(self) -> None:
        dead: list[tuple[str, str]] = []
        for key, conn in list(self._connections.items()):
            if not conn.alive or not await conn.ping():
                dead.append(key)
        for key in dead:
            conn = self._connections.pop(key, None)
            if conn:
                await conn.kill()

    async def shutdown(self) -> None:
        for _, conn in list(self._connections.items()):
            await conn.kill()
        self._connections.clear()

    def cleanup_ghosts(self) -> int:
        return cleanup_ghosts({c.proc.pid for c in self._connections.values()})

    @property
    def stats(self) -> dict[str, Any]:
        agents: dict[str, int] = {}
        for (agent, _), conn in self._connections.items():
            if conn.alive:
                agents[agent] = agents.get(agent, 0) + 1
        return {"total": len(self._connections), "by_agent": agents}


AcpConnection = GoalflightAcpConnection
