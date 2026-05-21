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
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Protocol

from goalflight_liveness import active_monotonic


log = logging.getLogger("goal-flight.acp_client")

_VERSION = "0.4.5-sdk"
DEFAULT_ACP_LIMIT = 32 * 1024 * 1024
DEFAULT_PERMISSION_TIMEOUT_S = 30.0
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


_PIDFILE_DIR = Path("/tmp/goal-flight-acp-pids.d")
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


def _write_through_pidfile_locked() -> None:
    try:
        _PIDFILE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("could not create pidfile dir %s: %s", _PIDFILE_DIR, e)
        return
    own_pidfile = _PIDFILE_DIR / f"{os.getpid()}.jsonl"
    entries: list[str] = []
    for conn in _live_connections.values():
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
    for pf in _PIDFILE_DIR.glob("*.jsonl"):
        try:
            controller_pid = int(pf.stem.split(".", 1)[0])
        except ValueError:
            continue
        if controller_pid == own_pid:
            continue
        if _ps_meta(controller_pid) is not None:
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
            try:
                if is_bash_tail and pgid != pid:
                    os.kill(pid, signal.SIGKILL)
                else:
                    os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            killed += 1
        pf.unlink(missing_ok=True)
    if killed or skipped_stale or skipped_live_controller:
        log.info(
            "ghost_cleanup: killed=%d skipped_stale=%d skipped_live_controllers=%d",
            killed,
            skipped_stale,
            skipped_live_controller,
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
    dropped_frames: int = 0

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
        return len(self.outstanding_tools) + len(self.pending_permissions)

    def timed_out(self, now: float, max_tool_s: float) -> tuple[str, float] | None:
        expired_permissions = self._expired_permissions(now)
        if expired_permissions:
            tool_id, started_at = expired_permissions[0]
            for expired_id, _ in expired_permissions:
                self.pending_permissions.pop(expired_id, None)
            return tool_id, now - started_at
        if max_tool_s <= 0:
            return None
        if self.permission_timeout_s <= 0:
            self.pending_permissions.clear()
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
        return {
            "raw_events_seen": self.raw_events_seen,
            "wedge_progress_seen": self.wedge_progress_seen,
            "last_event_kind": self.last_event_kind,
            "quiet_for_s": now - self.last_event_mono,
            "progress_quiet_for_s": now - self.last_progress_mono,
            "outstanding_count": self.outstanding_count(now),
            "dropped_frames": self.dropped_frames,
        }


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


class GoalflightClient(ClientBase):  # type: ignore[misc, valid-type]
    def __init__(
        self,
        *,
        activity: AcpLivenessActivity | None = None,
        auto_allow_tools: bool = True,
        turn_queue: asyncio.Queue[dict[str, Any]] | None = None,
    ) -> None:
        self.activity = activity or AcpLivenessActivity()
        self.auto_allow_tools = auto_allow_tools
        self.turn_queue = turn_queue
        self.typed_updates: list[dict[str, Any]] = []

    def set_turn_queue(self, queue: asyncio.Queue[dict[str, Any]] | None) -> None:
        self.turn_queue = queue

    def observe_stream_event(self, event: Any) -> None:
        if StreamDirection is not None and event.direction != StreamDirection.INCOMING:
            return
        message = dict(event.message)
        kind = self.activity.note_message(message)
        if self.turn_queue is not None:
            self.turn_queue.put_nowait({"source": "observer", "kind": kind, "message": message})

    async def request_permission(self, options: list[Any], session_id: str, tool_call: Any, **kwargs: Any) -> Any:
        tool_id = getattr(tool_call, "tool_call_id", None) or getattr(tool_call, "id", None)
        if not self.auto_allow_tools:
            raise RequestError.method_not_found("session/request_permission")
        chosen_id: str | None = None
        for kind_pref in ("allow_always", "allow_once"):
            for opt in options or []:
                if getattr(opt, "kind", None) == kind_pref:
                    chosen_id = getattr(opt, "option_id", None)
                    break
            if chosen_id:
                break
        if not chosen_id and options:
            chosen_id = getattr(options[0], "option_id", None)
        self.activity.resolve_permission(str(tool_id) if tool_id else None)
        if chosen_id:
            log.info("auto-allow permission: %s -> optionId=%s", getattr(tool_call, "title", "?"), chosen_id)
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=chosen_id)
            )
        log.warning("auto-allow permission request had no selectable options; cancelling")
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

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
    acp_session_id: str | None = None
    cwd: str | None = None
    reusable: bool = True
    last_active: float = field(default_factory=time.time)
    session_reset: bool = False
    _started_meta: tuple[str, str] | None = None
    _stderr_task: asyncio.Task | None = None
    _registered: bool = False

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
        return await self.conn.prompt(session_id=self.acp_session_id, prompt=[text_block(text)])

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
                    os.killpg(self.verified_pgid, signal.SIGKILL)
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
) -> GoalflightAcpConnection:
    require_acp_sdk()
    limit = acp_limit_from_env()
    os.makedirs(cwd, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        command,
        *acp_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        start_new_session=True,
        limit=limit,
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
    client = GoalflightClient(activity=activity, auto_allow_tools=auto_allow_tools)
    guarded_reader = GuardedStreamReader(proc.stdout, limit=limit, on_drop=activity.note_dropped_frame)
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
    )


class AcpProcessPool:
    def __init__(
        self,
        agents_config: dict[str, Any],
        max_processes: int = 20,
        max_per_agent: int = 10,
        verbose: bool = False,
        auto_allow_tools: bool = False,
    ) -> None:
        self._config = agents_config
        self._max = max_processes
        self._max_per_agent = max_per_agent
        self._verbose = verbose
        self._auto_allow_tools = auto_allow_tools
        self._connections: dict[tuple[str, str], GoalflightAcpConnection] = {}

    def _count_agent(self, agent: str) -> int:
        return sum(1 for (a, _) in self._connections if a == agent)

    async def get_or_create(self, agent: str, session_id: str, cwd: str = "") -> GoalflightAcpConnection:
        key = (agent, session_id)
        conn = self._connections.get(key)
        if conn and conn.alive and conn.reusable:
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
        agent_cfg = self._config.get(agent)
        if not agent_cfg:
            raise AcpError(f"agent not found: {agent}")
        command = agent_cfg["command"]
        acp_args = agent_cfg.get("acp_args", [agent_cfg.get("acp_arg", "acp")])
        workdir = cwd or agent_cfg.get("working_dir", "/tmp")
        new_conn = await spawn_acp_connection(
            command,
            acp_args,
            agent=agent,
            session_id=session_id,
            cwd=workdir,
            auto_allow_tools=self._auto_allow_tools,
            verbose=self._verbose,
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
