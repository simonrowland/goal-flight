# Vendored from aws-samples/sample-acp-bridge @ 2cd3c86af6a09178ea6fa42d398dbe72a572abcf
# License: MIT-0 (https://github.com/aws-samples/sample-acp-bridge/blob/main/LICENSE)
# Source: src/acp_client.py
# Local changes:
#   - _VERSION literal (upstream read from ../VERSION; goal-flight has no such file)
#   - log namespace "acp-bridge.acp_client" -> "goal-flight.acp_client"
#   - clientInfo.name "acp-bridge" -> "goal-flight"
#   - pidfile path /tmp/acp-bridge-pids -> /tmp/goal-flight-acp-pids.d/ (namespace separation
#     so both bridges can coexist on the same host without trampling ghost-cleanup state;
#     the .d/ suffix reflects the per-controller-PID directory scheme below)
#   - auto_allow_tools: bool = False field on AcpConnection; gates the
#     session/request_permission auto-reply. Upstream auto-allows every tool call
#     unconditionally — fine for chat-bridge use, bad for a controller that wants
#     the user-confirmation surface. When False, the permission request is
#     dropped (logged as warning) — the agent will hang on that request, which
#     is the correct failure mode for "the controller should have asked the
#     user first." Set True only for trusted automation where the controller
#     has already decided every tool call is acceptable.
#   - Permission response schema corrected to match the ACP spec
#     (zed-industries/agent-client-protocol schema.json). Upstream sent
#     {"optionId": "allow_always"} which works against claude-agent-acp (lenient)
#     but is rejected by codex-acp with -32700 "missing field `outcome`". Correct
#     shape is the discriminated-union RequestPermissionResponse:
#     {"outcome": {"outcome": "selected", "optionId": "<id-from-request.options>"}}.
#     Also: optionId is per-request (the agent supplies the option list in the
#     request); we now introspect params.options, prefer kind="allow_always",
#     fall back to "allow_once", then to options[0]. Hardcoding "allow_always"
#     as the optionId only worked when the agent happened to use that literal
#     string as its optionId — codex's options use distinct ids.
#   - asyncio reader limit bumped to 8 MB on _spawn() (default 65 KB chokes on
#     goal-mode / implement-mode workers that stream a long reasoning trace as a
#     single line — surfaces as "Separator is not found, and chunk exceed the
#     limit" mid-prompt and crashes the reader loop).
#   - close_gracefully() added — attempts ACP session/close (capability-gated;
#     none of the workers tested 2026-05-17 advertise sessionCapabilities.close,
#     so this is a future-friendly hook), then closes stdin, waits soft_timeout
#     for natural exit, escalates to kill() (SIGKILL via process group). Use
#     this instead of bare kill() when a clean teardown is preferable.
#   - AcpConnection is now an async context manager (__aenter__/__aexit__) so
#     `async with conn:` guarantees close_gracefully() on exit — bulletproof
#     replacement for try/finally + .kill() in callers.
#   - Ghost-cleanup pidfile schema upgraded from bare-integer-per-line to
#     JSON-Lines with full identity disambiguation: pid, pgid, started_at,
#     cmd, agent, session_id. Upstream cleanup_ghosts() killed by PID alone
#     — on Mac (kern.maxproc=16000, fast PID reuse) that would SIGKILL whatever
#     unrelated process happened to inherit the PID after a controller restart.
#     New cleanup_ghosts() verifies live ps lstart+comm against the recorded
#     values before killing; entries with mismatched identity are logged as
#     stale and skipped (no kill). Legacy bare-integer pidfile lines are
#     silently ignored to avoid the same risk on read-back of pre-upgrade
#     state. _ps_meta() is the lookup helper; uses `ps -o lstart=,comm= -p <pid>`
#     (POSIX-portable; on Linux this returns the same shape).
#   - Pidfile concurrent-safety: pidfile is now a DIRECTORY
#     (/tmp/goal-flight-acp-pids.d/) containing one JSONL file per
#     controller process (named <controller-pid>.jsonl). cleanup_ghosts()
#     walks the directory; skips files whose controller-PID is still alive
#     (another goal-flight run is using those workers); processes only
#     orphaned files from dead controllers. Solves the "two goal-flight
#     worktrees on the same Mac clobber each other" failure mode. Upgrade
#     path: the legacy /tmp/goal-flight-acp-pids singleton file is no
#     longer written or read; users can `rm` it manually (it'll be a
#     no-op after first new-version run).
#   - Auto-allow empty-options defense: when session/request_permission
#     arrives with params.options=[] (no choices to pick from), the auto-allow
#     path no longer sends optionId:null (which violates schema and codex
#     rejects). Logs warning + lets the request hang — correct fallback for
#     "I have no valid choice to make."
#   - cleanup_ghosts TOCTOU defense: re-verifies identity via _ps_meta()
#     immediately before killpg. Narrow microsecond window between initial
#     check and kill still allows PID reuse; the double-check shrinks it
#     to a much smaller window where the recheck-and-kill must both happen
#     after a reuse — extremely unlikely under normal scheduling.
#   - cleanup_ghosts pidfile-stem split: extracts controller-pid from the
#     LEADING int prefix of the filename rather than requiring the full
#     stem to parse as int. Allows goal-flight's bash-tail watcher
#     (`scripts/watch-dispatch-tail.sh`) to register orphan-eligible
#     workers under the same pidfile dir with the naming pattern
#     `<controller-pid>.bashtail.<worker-pid>.jsonl` — single cleanup_ghosts
#     pass reaps orphans across both ACP and Bash-tail dispatch paths.
#   - AcpProcessPool._save_pids / _own_pidfile / _pidfile_dir removed (0.3.4):
#     Design 2's module-level registry (_live_connections + write-through
#     to _PIDFILE_DIR via _write_through_pidfile_locked()) is now the SINGLE
#     writer to the per-controller pidfile. AcpConnection.__post_init__ +
#     .kill() handle register/unregister automatically. The pool's prior
#     _save_pids() snapshots have been removed from get_or_create() + close();
#     cleanup_ghosts reads from the module-level _PIDFILE_DIR. Eliminates
#     the latent mixed-mode race where pool's narrower view would clobber
#     the registry's superset.
#   - _send_request() gained an optional `timeout` (0.4.2); initialize() and
#     session_new() pass a 60s handshake timeout. Upstream `await fut` was
#     unbounded, so a worker that spawned but never answered the handshake
#     (the codex-acp wedge: idle CPU, empty acp-run.log, no status JSON) hung
#     the runner forever — and since the handshake precedes session_prompt,
#     the execution idle-timeout never applied. Now the wedge raises a clean
#     AcpError in ~60s; the runner writes state=failed and the controller
#     falls back. This matters more after 0.4.1 raised the goal-mode execution
#     idle-timeout to 36000s: without a separate handshake bound, a wedged
#     goal-mode handshake would have hung ~10h.
#   - session_prompt() gained an optional `on_idle` callback (0.4.2 liveness):
#     when the idle window elapses with no ACP events, the hook is consulted
#     BEFORE cancelling — it returns True ("worker alive, keep waiting") or
#     False ("wedged, give up"). goal-flight passes a process-group-CPU probe so
#     a healthy-but-silent worker (running_quiet: CPU>0, no events) is NOT
#     cancelled — the false-positive idle-timeout retry that the Phase-1
#     liveness work exists to kill. The CPU policy lives in goalflight_acp_run.py,
#     NOT here, so the vendored client stays generic (the hook is just "ask
#     before giving up on idle"). No callback → original event-gap-only cancel.
"""ACP stdio JSON-RPC client — manages CLI agent subprocesses."""

import asyncio
import inspect
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

log = logging.getLogger("goal-flight.acp_client")


def _ps_meta(pid: int) -> tuple[str, str] | None:
    """Return (lstart, comm) for a live PID, or None if the PID is gone.

    lstart format on Mac/BSD: "Mon May 18 00:01:09 2026" (5 whitespace-separated
    tokens). On Linux ps with the same flags it's equivalent. Literal string
    match is the safe disambiguator — no parsing, no TZ math, just compare
    what we recorded against what's live right now.

    comm is `argv[0]` basename (or process name) — secondary check.

    A PID reused by an unrelated process WILL have a different lstart (the
    new process started at a different wall-clock instant). This is the
    load-bearing safety check for ghost cleanup across controller restarts.
    """
    try:
        r = subprocess.run(
            ["ps", "-o", "lstart=,comm=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
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

_VERSION = "0.1.0-vendored"


class AcpError(Exception):
    pass


class PoolExhaustedError(AcpError):
    pass


# ---------------------------------------------------------------------------
# Module-level connection registry (Design 2 — bare-AcpConnection orphan defense)
# ---------------------------------------------------------------------------
# Both bare AcpConnection and AcpProcessPool register here on spawn. Write-
# through pidfile so even a SIGKILL of the controller leaves the latest
# snapshot on disk — cleanup_ghosts() on the next controller start reads
# from the same dir and reaps orphans with identity verification. Works
# alongside theirs's [bash-tail] per-watcher pidfile naming (cleanup_ghosts
# already parses both via stem.split(".", 1)[0]).

import threading  # noqa: E402

_PIDFILE_DIR = Path("/tmp/goal-flight-acp-pids.d")
_live_connections: dict[int, "AcpConnection"] = {}
_registry_lock = threading.Lock()


def _register_connection(conn: "AcpConnection") -> None:
    """Add `conn` to the live-connections registry; write-through to pidfile.
    Called by AcpConnection.__post_init__ — automatic for both bare and
    pool-managed connections."""
    with _registry_lock:
        _live_connections[conn.proc.pid] = conn
        _write_through_pidfile_locked()


def _unregister_connection(conn: "AcpConnection") -> None:
    """Remove `conn` from registry; write-through to pidfile.
    Called by AcpConnection.kill()."""
    with _registry_lock:
        _live_connections.pop(conn.proc.pid, None)
        _write_through_pidfile_locked()


def _write_through_pidfile_locked() -> None:
    """Persist the live-connections snapshot. Caller MUST hold _registry_lock.

    Per-controller pidfile path (named <controller-pid>.jsonl) so concurrent
    goal-flight runs don't share/clobber state. Coexists with [bash-tail]
    watcher's per-worker subfiles (<pid>.bashtail.<wpid>.jsonl) — both share
    the same parent dir; cleanup_ghosts handles both via stem parsing.
    """
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
        try:
            pgid = os.getpgid(conn.proc.pid)
        except (ProcessLookupError, PermissionError):
            pgid = conn.proc.pid
        entries.append(json.dumps({
            "pid": conn.proc.pid,
            "pgid": pgid,
            "started_at": lstart,
            "cmd": comm,
            "agent": conn.agent,
            "session_id": conn.session_id,
        }))
    try:
        if entries:
            own_pidfile.write_text("\n".join(entries) + "\n")
        else:
            own_pidfile.unlink(missing_ok=True)
    except OSError as e:
        log.warning("pidfile write failed (%s): %s", own_pidfile, e)


@dataclass
class AcpConnection:
    agent: str
    session_id: str
    proc: asyncio.subprocess.Process
    verbose: bool = False
    auto_allow_tools: bool = False
    _req_id: int = field(default=0, init=False)
    _pending: dict[int, asyncio.Future] = field(default_factory=dict, init=False)
    _reader_task: asyncio.Task | None = field(default=None, init=False)
    _stderr_task: asyncio.Task | None = field(default=None, init=False)
    _notification_queues: dict[int, asyncio.Queue] = field(default_factory=dict, init=False)
    acp_session_id: str | None = field(default=None, init=False)
    cwd: str | None = field(default=None, init=False)  # set by session_new; used by run_prompt scope check

    def __post_init__(self) -> None:
        # Register for orphan-cleanup defense. Isolate I/O failures —
        # pidfile write errors should NOT block AcpConnection construction.
        try:
            _register_connection(self)
        except Exception as e:
            log.warning(
                "AcpConnection registration failed (%s); orphan cleanup defense "
                "disabled for pid=%d. Connection itself unaffected.",
                e, self.proc.pid,
            )
    last_active: float = field(default_factory=time.time, init=False)
    session_reset: bool = field(default=False, init=False)

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _send(self, msg: dict) -> None:
        data = json.dumps(msg) + "\n"
        if self.verbose:
            log.debug("acp_send: %s", data.rstrip()[:500])
        self.proc.stdin.write(data.encode())
        await self.proc.stdin.drain()

    async def _send_request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        req_id = self._next_id()
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send(msg)
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            # Worker stdin closed (process likely dead). Don't leak the pending
            # future; raise a clean AcpError so callers can classify cleanly.
            self._discard_pending(req_id)
            raise AcpError(f"{method}: send failed (worker likely dead): {e}") from e
        # `timeout` bounds the wait for the response future. Critically used by
        # initialize()/session_new() so a worker that spawns but never answers
        # the handshake (the codex-acp wedge: idle CPU, empty log, no status
        # JSON) fails fast with a clean AcpError instead of hanging forever.
        # Without it, `await fut` blocks indefinitely — and the session_prompt
        # idle-timeout never applies because the handshake happens before the
        # prompt. None preserves the unbounded behavior for callers that don't
        # want a request timeout.
        try:
            if timeout is not None:
                result = await asyncio.wait_for(fut, timeout=timeout)
            else:
                result = await fut
        except asyncio.TimeoutError:
            self._discard_pending(req_id)
            raise AcpError(
                f"{method}: no response within {timeout:.0f}s — worker likely "
                f"wedged in handshake (spawned but idle, nothing written). "
                f"Fall back to another transport/worker."
            )
        except asyncio.CancelledError:
            self._discard_pending(req_id)
            raise
        if "error" in result:
            raise AcpError(f"ACP error on {method}: {result['error']}")
        return result.get("result")

    async def _send_notification(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    def _discard_pending(self, req_id: int) -> None:
        """Drop a request future when the caller has stopped waiting for it."""
        fut = self._pending.pop(req_id, None)
        if fut is not None and not fut.done():
            fut.cancel()

    def _start_reader(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                if self.verbose:
                    log.debug("acp_stderr: %s", line.decode().rstrip()[:300])
        except Exception:
            pass

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode())
                except json.JSONDecodeError:
                    continue
                if self.verbose:
                    log.debug("acp_recv: %s", line.decode().rstrip()[:500])
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    self._pending.pop(msg_id).set_result(msg)
                else:
                    if msg.get("method") == "session/request_permission" and msg_id is not None:
                        params = msg.get("params", {}) or {}
                        title = params.get("toolCall", {}).get("title", "?")
                        options = params.get("options", []) or []
                        if self.auto_allow_tools:
                            chosen_id: str | None = None
                            for kind_pref in ("allow_always", "allow_once"):
                                for opt in options:
                                    if opt.get("kind") == kind_pref:
                                        chosen_id = opt.get("optionId")
                                        break
                                if chosen_id:
                                    break
                            if not chosen_id and options:
                                chosen_id = options[0].get("optionId")
                            if chosen_id is None:
                                # No options offered — can't auto-pick. Log + let the
                                # request hang; if the agent wanted us to decide, it
                                # would have given us choices. ACP schema requires
                                # optionId to be a string; sending null is rejected.
                                log.warning(
                                    "auto-allow: request has no options to pick from; "
                                    "letting it hang. title=%s. Agent may need a manual "
                                    "permission policy in the controller.", title,
                                )
                            else:
                                log.info("auto-allow permission: %s -> optionId=%s", title, chosen_id)
                                reply = {
                                    "jsonrpc": "2.0",
                                    "id": msg_id,
                                    "result": {"outcome": {"outcome": "selected", "optionId": chosen_id}},
                                }
                                data = json.dumps(reply) + "\n"
                                self.proc.stdin.write(data.encode())
                                asyncio.ensure_future(self.proc.stdin.drain())
                        else:
                            log.warning(
                                "session/request_permission received but auto_allow_tools=False; "
                                "request will hang. title=%s. Set auto_allow_tools=True on the "
                                "AcpConnection for trusted automation, or handle the request via "
                                "your own subscriber.", title,
                            )
                    for q in self._notification_queues.values():
                        q.put_nowait(msg)
        except Exception as e:
            log.error("reader loop crashed: %s", e)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result({"error": {"code": -1, "message": "connection closed"}})
            self._pending.clear()
            for q in self._notification_queues.values():
                q.put_nowait(None)

    def _subscribe(self) -> tuple[int, asyncio.Queue]:
        sub_id = id(asyncio.current_task())
        q: asyncio.Queue = asyncio.Queue()
        self._notification_queues[sub_id] = q
        return sub_id, q

    def _unsubscribe(self, sub_id: int) -> None:
        self._notification_queues.pop(sub_id, None)

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def initialize(self, timeout: float = 60.0) -> dict:
        # `timeout` bounds the handshake. A healthy adapter answers initialize
        # in well under a second; 60s tolerates a slow cold-start while still
        # catching the wedge (codex-acp spawning but never handshaking) two
        # orders of magnitude faster than the execution idle-timeout, which
        # is now up to 36000s for goal-mode dispatches.
        self._start_reader()
        result = await self._send_request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "goal-flight", "version": _VERSION},
        }, timeout=timeout)
        log.info("initialized: agent=%s version=%s",
                 result.get("agentInfo", {}).get("name"),
                 result.get("agentInfo", {}).get("version"))
        return result

    async def session_new(self, cwd: str, timeout: float = 60.0) -> str:
        self.cwd = cwd  # remember for run_prompt's scope-leak audit (Design 1)
        sub_id, q = self._subscribe()
        try:
            result = await self._send_request("session/new", {
                "cwd": cwd,
                "mcpServers": [],
            }, timeout=timeout)
            self.acp_session_id = result["sessionId"]
            log.info("session created: acp_session=%s", self.acp_session_id)
            return self.acp_session_id
        finally:
            self._unsubscribe(sub_id)

    async def session_prompt(
        self,
        prompt: str,
        idle_timeout: float | None = 300,
        on_idle: Callable[[], Any] | None = None,
    ) -> AsyncIterator[dict]:
        self.last_active = time.time()
        last_event_time = time.time()
        sub_id, q = self._subscribe()
        req_id = self._next_id()
        timeout_enabled = idle_timeout is not None and idle_timeout > 0
        cancel_sent = False
        msg = {
            "jsonrpc": "2.0", "id": req_id, "method": "session/prompt",
            "params": {
                "sessionId": self.acp_session_id,
                "prompt": [{"type": "text", "text": prompt}],
            },
        }
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send(msg)
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            # Worker stdin closed before we could send the prompt. Yield a
            # clean error sentinel + return so callers don't see raw asyncio
            # exceptions.
            self._discard_pending(req_id)
            self._unsubscribe(sub_id)
            yield {"_prompt_result": {"error": {"code": -1, "message": f"send failed: {e}"}}}
            return

        try:
            while True:
                if fut.done():
                    break
                if timeout_enabled and time.time() - last_event_time > idle_timeout:
                    # Idle window elapsed with no ACP events. Before treating
                    # this as a wedge, consult the optional liveness hook: a
                    # healthy worker doing CPU-heavy silent work (running_quiet)
                    # must NOT be cancelled — that false positive is the retry
                    # storm Phase 1 exists to kill. The hook (supplied by
                    # goalflight_acp_run.py) samples process-group CPU and
                    # returns True ("alive, keep waiting") / False ("wedged,
                    # give up"). No hook → original event-gap-only cancel.
                    # NOTE: the hook runs inline in this consumer loop, so a slow
                    # hook briefly pauses event drain — harmless here because we
                    # only reach this branch when the worker has BEEN silent for a
                    # full idle window (no events to drain), and the hook is
                    # bounded (a couple of short CPU samples).
                    keep_waiting = False
                    if on_idle is not None:
                        try:
                            verdict = on_idle()
                            if inspect.isawaitable(verdict):
                                verdict = await verdict
                            keep_waiting = bool(verdict)
                        except Exception as e:
                            log.warning("on_idle liveness hook raised (%s); treating as wedged", e)
                            keep_waiting = False
                    # The hook may have awaited (e.g. a CPU sample); during that
                    # await the worker could have shown signs of life that make the
                    # idle verdict stale. Only cancel if it truly showed none. Any
                    # of these means alive → never cancel on a stale verdict:
                    #   - fut.done()      : the final response arrived → go yield it
                    #   - q not empty     : a progress event was queued during the
                    #                       await → the worker is actively emitting
                    #   - keep_waiting    : the hook says CPU-busy (running_quiet)
                    if fut.done():
                        break
                    if keep_waiting or not q.empty():
                        # Reset the idle clock and keep streaming. The next idle
                        # window re-checks liveness, so a worker that later goes
                        # truly quiet is still caught within one idle_timeout.
                        last_event_time = time.time()
                        continue
                    self._discard_pending(req_id)
                    if self.alive and self.acp_session_id is not None:
                        try:
                            await self.session_cancel()
                            cancel_sent = True
                        except (ConnectionResetError, BrokenPipeError, OSError, AcpError) as e:
                            log.warning("session_cancel after idle timeout failed: %s", e)
                    yield {"_prompt_result": {"error": {"code": -1, "message": "agent_timeout (idle)"}}}
                    return
                try:
                    notification = await asyncio.wait_for(q.get(), timeout=1.0)
                    if notification is None:
                        break
                    last_event_time = time.time()
                    yield notification
                except asyncio.TimeoutError:
                    continue

            while not q.empty():
                n = q.get_nowait()
                if n is not None:
                    yield n

            # Give reader task a moment to flush remaining notifications
            await asyncio.sleep(0.05)
            while not q.empty():
                n = q.get_nowait()
                if n is not None:
                    yield n

            result = fut.result() if fut.done() else {"error": {"code": -1, "message": "no response"}}
            yield {"_prompt_result": result}
        finally:
            if req_id in self._pending and not fut.done() and not cancel_sent:
                if self.alive and self.acp_session_id is not None:
                    try:
                        await self.session_cancel()
                    except (ConnectionResetError, BrokenPipeError, OSError, AcpError) as e:
                        log.warning("session_cancel during prompt cleanup failed: %s", e)
            self._discard_pending(req_id)
            self._unsubscribe(sub_id)
            self.last_active = time.time()

    async def session_cancel(self) -> None:
        await self._send_notification("session/cancel", {
            "sessionId": self.acp_session_id,
        })

    async def kill(self) -> None:
        if self.alive:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.proc.kill()
            await self.proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # Unregister from live-connections; write-through removes our entry
        # from the pidfile. Isolate I/O failures — a pidfile error here
        # shouldn't propagate past kill() (caller wants to know the worker
        # is dead, not whether bookkeeping succeeded).
        try:
            _unregister_connection(self)
        except Exception as e:
            log.warning("AcpConnection unregister failed (%s); pidfile may be stale", e)

    async def close_gracefully(self, soft_timeout: float = 2.0) -> None:
        """Try a clean shutdown, fall back to SIGKILL.

        Sequence:
          1. ACP session/close request (capability-gated; harmless if unsupported)
          2. Close stdin so the worker sees EOF
          3. Wait soft_timeout for the process to exit naturally
          4. Escalate to kill() (process-group SIGKILL)

        Use this in preference to bare kill() when a clean teardown is preferable
        (e.g., letting the worker flush transcripts / commit local state). Bare
        kill() is fine when speed matters more than cleanliness.
        """
        if self.alive and self.acp_session_id is not None:
            try:
                await asyncio.wait_for(
                    self._send_request("session/close", {"sessionId": self.acp_session_id}),
                    timeout=soft_timeout,
                )
            except (AcpError, asyncio.TimeoutError, Exception):
                pass  # capability not advertised, agent already gone, or close not supported
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
        except Exception:
            pass
        if self.alive:
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=soft_timeout)
            except asyncio.TimeoutError:
                pass
        await self.kill()  # idempotent — no-op if already exited

    async def __aenter__(self) -> "AcpConnection":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close_gracefully()

    async def ping(self, timeout: float = 5) -> bool:
        """Lightweight health probe — send a no-op JSON-RPC request."""
        if not self.alive:
            return False
        try:
            await asyncio.wait_for(
                self._send_request("ping", {}), timeout=timeout
            )
            return True
        except AcpError:
            # Agent replied with an error (method not found) — still alive
            return True
        except Exception:
            return False


class AcpProcessPool:
    def __init__(
        self,
        agents_config: dict,
        max_processes: int = 20,
        max_per_agent: int = 10,
        verbose: bool = False,
        auto_allow_tools: bool = False,
    ):
        """
        auto_allow_tools: when True, every spawned AcpConnection gets
        auto_allow_tools=True so session/request_permission requests are
        auto-replied with the agent's preferred 'allow_always' option (or
        the first available option if 'allow_always' isn't offered). Goal-flight's
        autonomous chunk dispatch needs this — without it, codex-acp / cursor-agent /
        claude-code-cli-acp will hang on the first tool call (per the documented
        intentional failure mode at AcpConnection's permission handler). Default
        False matches the upstream chat-bridge use-case where each tool call is
        an interactive prompt.
        """
        self._config = agents_config
        self._max = max_processes
        self._max_per_agent = max_per_agent
        self._verbose = verbose
        self._auto_allow_tools = auto_allow_tools
        self._connections: dict[tuple[str, str], AcpConnection] = {}

    def _count_agent(self, agent: str) -> int:
        return sum(1 for (a, _) in self._connections if a == agent)

    async def get_or_create(self, agent: str, session_id: str, cwd: str = "") -> AcpConnection:
        key = (agent, session_id)
        conn = self._connections.get(key)

        if conn and conn.alive:
            return conn

        is_rebuild = conn is not None
        if conn:
            log.warning("stale connection: agent=%s session=%s, rebuilding", agent, session_id)
            self._connections.pop(key, None)

        if len(self._connections) >= self._max:
            raise PoolExhaustedError(f"global limit reached ({self._max})")
        if self._count_agent(agent) >= self._max_per_agent:
            raise PoolExhaustedError(f"per-agent limit reached for {agent} ({self._max_per_agent})")

        agent_cfg = self._config.get(agent)
        if not agent_cfg:
            raise AcpError(f"agent not found: {agent}")

        conn = await self._spawn(agent, session_id, agent_cfg, is_rebuild=is_rebuild, cwd_override=cwd)
        self._connections[key] = conn
        # Pidfile write-through happens automatically via Design 2's module-level
        # registry (AcpConnection.__post_init__ → _register_connection →
        # _write_through_pidfile_locked). Single writer keeps pidfile content
        # consistent — see _save_pids removal note below.
        return conn

    async def _spawn(self, agent: str, session_id: str, cfg: dict, is_rebuild: bool = False, cwd_override: str = "") -> AcpConnection:
        command = cfg["command"]
        acp_args = cfg.get("acp_args", ["acp"])
        cwd = cwd_override or cfg.get("working_dir", "/tmp")
        os.makedirs(cwd, exist_ok=True)

        log.info("spawning: agent=%s session=%s cmd=%s %s rebuild=%s", agent, session_id, command, acp_args, is_rebuild)
        proc = await asyncio.create_subprocess_exec(
            command, *acp_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,  # create process group so we can kill the whole tree
            limit=8 * 1024 * 1024,   # 8MB read buffer; goal-mode traces blow past 64KB default
        )

        conn = AcpConnection(
            agent=agent,
            session_id=session_id,
            proc=proc,
            verbose=self._verbose,
            auto_allow_tools=self._auto_allow_tools,
        )
        if is_rebuild:
            conn.session_reset = True
        await conn.initialize()
        await conn.session_new(cwd)
        return conn

    async def close(self, agent: str, session_id: str) -> None:
        key = (agent, session_id)
        conn = self._connections.pop(key, None)
        if conn:
            log.info("closing: agent=%s session=%s", agent, session_id)
            await conn.kill()
            # conn.kill() calls _unregister_connection which write-throughs
            # the pidfile via Design 2; no need for an explicit _save_pids here.

    def remove(self, agent: str, session_id: str) -> None:
        self._connections.pop((agent, session_id), None)

    async def cleanup_idle(self, ttl_seconds: float) -> None:
        cutoff = time.time() - ttl_seconds
        stale = [k for k, c in self._connections.items() if c.last_active < cutoff]
        for key in stale:
            conn = self._connections.pop(key)
            log.info("cleanup idle: agent=%s session=%s", key[0], key[1])
            await conn.kill()

    async def health_check(self) -> None:
        """Ping all idle connections; kill and remove unresponsive ones."""
        dead: list[tuple[str, str]] = []
        for key, conn in list(self._connections.items()):
            if not conn.alive:
                dead.append(key)
                continue
            ok = await conn.ping()
            if not ok:
                dead.append(key)
        for key in dead:
            conn = self._connections.pop(key, None)
            if conn:
                log.warning("health_check: agent=%s session=%s unresponsive, killing", key[0], key[1])
                await conn.kill()

    async def shutdown(self) -> None:
        for key, conn in list(self._connections.items()):
            log.info("shutdown: killing agent=%s session=%s", key[0], key[1])
            await conn.kill()
        self._connections.clear()

    # 0.3.4 dedupe: `_pidfile_dir`, `_own_pidfile()`, and `_save_pids()` removed —
    # Design 2's module-level registry (_live_connections + _write_through_pidfile_locked
    # near the top of this file) is now the single writer to the pidfile dir.
    # AcpConnection.__post_init__ + .kill() handle register/unregister automatically
    # for both bare and pool-managed connections, so the pool no longer needs its own
    # snapshot path. cleanup_ghosts below reads from the module-level _PIDFILE_DIR.

    def cleanup_ghosts(self) -> int:
        """Kill orphaned agent processes recorded by dead controller(s).

        Walks the pidfile directory. For each per-controller pidfile:
          - Skip our own file (we manage it via Design 2's write-through registry).
          - Skip files whose controller PID is still alive (a concurrent
            goal-flight run owns those workers; don't touch them).
          - For files from dead controllers: identity-verify each worker
            entry via _ps_meta before killing. Mismatched identity ->
            skip with warning (PID reuse defense). Matched identity ->
            re-verify immediately before killpg (TOCTOU defense) -> kill.

        Returns the number of workers killed.
        """
        if not _PIDFILE_DIR.exists():
            return 0
        own_pid = os.getpid()
        own_worker_pids = {c.proc.pid for c in self._connections.values()}
        killed = 0
        skipped_stale = 0
        skipped_live_controller = 0
        for pf in _PIDFILE_DIR.glob("*.jsonl"):
            # Stem patterns recognized:
            #   "<int>"                            → ACP pool from controller <int>
            #   "<int>.bashtail.<int>"             → bash-tail watcher subfile
            #                                       (scripts/watch-dispatch-tail.sh)
            # Controller PID is always the leading int prefix; the suffix (if any)
            # carries goal-flight-specific subtyping that cleanup_ghosts ignores.
            try:
                controller_pid = int(pf.stem.split(".", 1)[0])
            except ValueError:
                continue
            if controller_pid == own_pid:
                continue  # our file; managed by Design 2's _write_through_pidfile_locked
            if _ps_meta(controller_pid) is not None:
                # Another goal-flight controller is alive — don't touch its workers.
                skipped_live_controller += 1
                continue
            # Controller is dead; its workers may be orphans worth reaping.
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
                    continue  # already dead
                live_lstart, live_comm = meta
                if live_lstart != entry.get("started_at") or live_comm != entry.get("cmd"):
                    log.warning(
                        "ghost_cleanup: pid=%d looks stale "
                        "(live={start=%r cmd=%r} != recorded={start=%r cmd=%r}); "
                        "SKIPPING kill to avoid hitting an unrelated process",
                        pid, live_lstart, live_comm,
                        entry.get("started_at"), entry.get("cmd"),
                    )
                    skipped_stale += 1
                    continue
                # TOCTOU defense: re-verify identity immediately before killpg.
                meta2 = _ps_meta(pid)
                if meta2 is None or meta2 != (live_lstart, live_comm):
                    log.warning(
                        "ghost_cleanup: pid=%d identity changed between check and kill "
                        "(was %r, now %r); SKIPPING kill",
                        pid, (live_lstart, live_comm), meta2,
                    )
                    skipped_stale += 1
                    continue
                pgid = entry.get("pgid", pid)
                agent = entry.get("agent", "")
                # Process-group safety for bash-tail entries: a worker launched
                # via `codex exec ... &` in non-interactive bash inherits the
                # PARENT SHELL'S pgroup (the controller's). killpg(controller_pgid,
                # SIGKILL) would then kill the controller and every sibling worker
                # in its group. The watcher records pgid best-effort but cannot
                # promise the worker is a session leader (macOS lacks /usr/bin/setsid,
                # so the bash-tail recipe in commands/execute.md doesn't enforce
                # isolation by default).
                # Defense: for bash-tail entries (agent suffix `-bash-tail`),
                # only killpg when pgid == pid (worker IS its own session leader).
                # Otherwise fall back to single-pid kill — slower if the worker
                # has child processes but safe.
                is_bash_tail = agent.endswith("-bash-tail")
                if is_bash_tail and pgid != pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        continue
                else:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            continue
                log.warning(
                    "ghost_cleanup: killed pid=%d pgid=%d agent=%s session=%s (from dead controller pid=%d)",
                    pid, pgid, entry.get("agent", "?"), entry.get("session_id", "?"), controller_pid,
                )
                killed += 1
            # Consumed this dead controller's pidfile.
            pf.unlink(missing_ok=True)
        if killed or skipped_stale or skipped_live_controller:
            log.info(
                "ghost_cleanup: killed=%d skipped_stale=%d skipped_live_controllers=%d",
                killed, skipped_stale, skipped_live_controller,
            )
        return killed

    @property
    def stats(self) -> dict:
        agents: dict[str, int] = {}
        for (a, _), c in self._connections.items():
            if c.alive:
                agents[a] = agents.get(a, 0) + 1
        return {"total": len(self._connections), "by_agent": agents}
