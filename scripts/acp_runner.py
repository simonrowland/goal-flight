"""Ergonomic wrapper around goal-flight ACP SDK connections.

Surfaces:
  - PromptResult dataclass — text / thoughts / tool_calls / plan / stop_reason
    / error / out_of_scope_writes
  - run_prompt(conn, text) — accumulate session/update notifications into a PromptResult
  - extract_markers(text) — pull goal-flight marker-vocab lines out of accumulated output
  - _scan_out_of_scope_paths(tool_calls, cwd) — audit helper for scope-leak detection

Why this exists separate from acp_client.py: keep the vendored client close to
upstream (aws-samples/sample-acp-bridge) so future re-vendoring is a small diff.
All goal-flight-specific ergonomics live here.
"""

import asyncio
import contextlib
from dataclasses import dataclass, field
import inspect
import json
from pathlib import Path
import re
from typing import Any, Callable

from goalflight_acp_client import AcpConnection
from goalflight_liveness import active_monotonic


@dataclass
class PromptResult:
    text: str = ""
    thoughts: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    plan_entries: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    error: dict[str, Any] | None = None
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    out_of_scope_writes: list[str] = field(default_factory=list)
    """Paths from tool_call locations that fall outside the connection's cwd.
    Populated by run_prompt post-hoc — a scope-leak audit signal for the
    controller, not a runtime gate. Empty when no leaks OR no recorded cwd."""
    cancelled_for_marker: bool = False
    early_marker: str | None = None
    permission_escalations: list[dict[str, Any]] = field(default_factory=list)
    """Permission requests the controller's router escalated to the user (the
    worker asked to cross a boundary it couldn't auto-decide). When non-empty the
    turn was cancelled (early_marker='USER-CONFIRM'); the controller surfaces
    these to the user, records a decision, and re-dispatches."""
    permission_auto_declined: list[dict[str, Any]] = field(default_factory=list)
    """Inline permissions the controller auto-declined (it did not answer within
    the inline timeout / IPC failed); the worker was denied and CONTINUED --
    informational, not a re-dispatch signal (unlike permission_escalations)."""

    @property
    def ok(self) -> bool:
        return self.error is None and self.stop_reason == "end_turn"


async def run_prompt(
    conn: AcpConnection,
    text: str,
    *,
    idle_timeout: float | None = 300,
    keep_raw: bool = False,
    on_event: Callable[[dict], Any] | None = None,
    on_idle: Callable[[], Any] | None = None,
) -> PromptResult:
    """Run one prompt on a connection; concurrent reuse of one connection is unsupported."""
    if getattr(conn.client, "_prompt_in_use", False):
        raise RuntimeError(
            "connection already running a prompt "
            "(concurrent reuse of one ACP connection is unsupported)"
        )
    conn.client._prompt_in_use = True
    try:
        return await _run_prompt_locked(
            conn,
            text,
            idle_timeout=idle_timeout,
            keep_raw=keep_raw,
            on_event=on_event,
            on_idle=on_idle,
        )
    finally:
        conn.client._prompt_in_use = False


async def _run_prompt_locked(
    conn: AcpConnection,
    text: str,
    *,
    idle_timeout: float | None = 300,
    keep_raw: bool = False,
    on_event: Callable[[dict], Any] | None = None,
    on_idle: Callable[[], Any] | None = None,
) -> PromptResult:
    """Send a prompt through an already-initialized ACP connection.

    The connection must already have had `initialize()` and `session_new(cwd)`
    called. session_prompt yields a stream of session/update notifications
    plus a terminal `{"_prompt_result": <full JSON-RPC envelope>}` event; we
    unwrap and classify those into the structured PromptResult.

    idle_timeout: seconds without ANY event from the agent before the runner
    gives up. Default 300s suits short prompts; set to 0/None (no timeout) or
    something like 7200 for goal-mode / implement-mode dispatches that run
    multi-minute autonomously between agent_message_chunks.

    on_idle: optional liveness hook consulted when the idle window elapses with
    no events. Return True to keep waiting (worker alive-but-quiet), False to
    let the runner cancel (wedged). goalflight_acp_run.py passes a process-group
    CPU probe so a healthy-but-silent worker isn't false-positive cancelled.
    Passed straight through to AcpConnection.session_prompt.
    """
    result = PromptResult()
    turn_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    conn.client.set_turn_queue(turn_queue)
    # Permission escalations are per-turn: clear any from a prior turn on a reused
    # (pool) connection so a stale deferral can't re-cancel this one.
    _escalations = getattr(conn.client, "permission_escalations", None)
    if _escalations is not None:
        _escalations.clear()
    _auto = getattr(conn.client, "permission_auto_declined", None)
    if _auto is not None:
        _auto.clear()
    prompt_task = asyncio.create_task(conn.prompt(text))
    last_event_time = active_monotonic()
    timeout_enabled = idle_timeout is not None and idle_timeout > 0

    def _pending_escalations() -> list[dict[str, Any]]:
        return list(getattr(conn.client, "permission_escalations", None) or [])

    def _copy_permission_auto_declined() -> None:
        result.permission_auto_declined = list(
            getattr(conn.client, "permission_auto_declined", None) or []
        )

    def _inline_holding() -> bool:
        # True while the inline permission router is holding a request open
        # awaiting a controller/user decision (permission_mode="inline"). The
        # worker is intentionally quiet during the hold, not wedged.
        activity = getattr(getattr(conn, "client", None), "activity", None)
        return bool(activity is not None and activity.has_inline_holds())

    async def _call_on_event(message: dict[str, Any]) -> None:
        if on_event is None:
            return
        maybe_awaitable = on_event(message)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable

    def _content_text(content: Any) -> str:
        if isinstance(content, dict):
            return str(content.get("text") or "")
        return ""

    def _accumulate(message: dict[str, Any]) -> None:
        if keep_raw:
            result.raw_events.append(message)
        if message.get("method") != "session/update":
            return
        update = (message.get("params", {}) or {}).get("update", {}) or {}
        kind = update.get("sessionUpdate") or update.get("session_update")
        content = update.get("content", {}) or {}
        if kind == "agent_message_chunk":
            result.text += _content_text(content)
        elif kind == "agent_thought_chunk":
            result.thoughts += _content_text(content)
        elif kind in ("tool_call", "tool_call_update"):
            result.tool_calls.append(update)
        elif kind == "plan":
            for entry in update.get("entries", []) or []:
                entry_text = entry.get("content") if isinstance(entry, dict) else None
                if entry_text:
                    result.plan_entries.append(entry_text)

    def _early_marker() -> str | None:
        markers = extract_markers(result.text)
        for marker in ("BLOCKED", "USER-CONFIRM", "USER-NEED"):
            if markers.get(marker):
                return marker
        return None

    async def _cancel_prompt(reason: str) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(conn.cancel(), timeout=1.0)
        try:
            await asyncio.wait_for(asyncio.shield(prompt_task), timeout=2.0)
        except asyncio.TimeoutError:
            conn.reusable = False
            with contextlib.suppress(Exception):
                await conn.kill()
            prompt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await prompt_task
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        while not turn_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                turn_queue.get_nowait()
        result.cancelled_for_marker = True
        result.early_marker = reason

    try:
        while True:
            esc = _pending_escalations()
            if esc and not result.cancelled_for_marker:
                # The controller's permission router escalated a request to the
                # user. The ACP request was ALREADY answered (cancel), so the
                # worker is not held open; stop the turn and surface the deferral
                # as USER-CONFIRM so the controller can ask the user and
                # re-dispatch. Liveness-safe: we never hold the permission open.
                result.permission_escalations = esc
                await _cancel_prompt("USER-CONFIRM")
                _copy_permission_auto_declined()
                return result
            if prompt_task.done():
                await asyncio.sleep(0)
                while not turn_queue.empty():
                    event = turn_queue.get_nowait()
                    message = event.get("message") or {}
                    await _call_on_event(message)
                    _accumulate(message)
                    marker = _early_marker()
                    if marker:
                        await _cancel_prompt(marker)
                        _copy_permission_auto_declined()
                        return result
                break
            if timeout_enabled and active_monotonic() - last_event_time > float(idle_timeout):
                if _inline_holding():
                    # An inline permission is held open awaiting a controller/user
                    # decision (file IPC). The worker is paused by design, not
                    # wedged; the handler's own inline timeout bounds the hold.
                    # Keep waiting WITHOUT consulting the CPU gate (the worker is
                    # legitimately at ~0% CPU while blocked on the permission).
                    last_event_time = active_monotonic()
                    continue
                keep_waiting = False
                if on_idle is not None:
                    try:
                        verdict = on_idle()
                        if inspect.isawaitable(verdict):
                            verdict = await verdict
                        keep_waiting = bool(verdict)
                    except Exception:
                        keep_waiting = False
                if prompt_task.done() or keep_waiting or not turn_queue.empty():
                    last_event_time = active_monotonic()
                    continue
                with contextlib.suppress(Exception):
                    await conn.cancel()
                prompt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await prompt_task
                result.error = {"code": -1, "message": "agent_timeout (idle)"}
                _copy_permission_auto_declined()
                return result
            try:
                event = await asyncio.wait_for(turn_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            message = event.get("message") or {}
            last_event_time = active_monotonic()
            await _call_on_event(message)
            _accumulate(message)
            marker = _early_marker()
            if marker:
                await _cancel_prompt(marker)
                _copy_permission_auto_declined()
                return result

        try:
            response = await prompt_task
            result.stop_reason = response.stop_reason
        except asyncio.CancelledError:
            raise
        except Exception as e:
            result.error = {
                "code": getattr(e, "code", -1),
                "message": str(e),
            }
    except asyncio.CancelledError:
        conn.reusable = False
        with contextlib.suppress(Exception):
            await asyncio.wait_for(conn.cancel(), timeout=1.0)
        prompt_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await prompt_task
        raise
    finally:
        conn.client.set_turn_queue(None)
    # Post-hoc scope-leak audit (Design 1): scan tool_call locations for paths
    # outside the connection's cwd. Informational signal; doesn't gate dispatch.
    cwd = getattr(conn, "cwd", None)
    if cwd:
        result.out_of_scope_writes = _scan_out_of_scope_paths(result.tool_calls, cwd)
    _copy_permission_auto_declined()
    return result


# Match the marker vocabulary, tolerating optional markdown emphasis around the
# marker tag. Codex emits unwrapped `STATUS: ...`; grok wraps as `**STATUS:** ...`
# (markdown bold for the tag, value plain). Pattern mirrors
# protocols/worker-markers.md: `^\**(MARKER):\**` with the value following on
# the same line.
_MARKERS_RE = re.compile(
    r"^\**(STATUS|RESULT|USER-NEED|USER-CONFIRM|BLOCKED|COMPLETE):\**\s*(.+?)\s*\**$",
    re.MULTILINE,
)


def extract_markers(text: str) -> dict[str, list[str]]:
    """Pull goal-flight marker-vocab lines from worker output text.

    Returns {marker_type: [values...]} in source order per type. Tolerates
    optional markdown emphasis around the marker tag (grok wraps as
    `**STATUS:** ...`; codex emits unwrapped `STATUS: ...`). See
    protocols/worker-markers.md for the vocabulary spec.

    Empty-content matches (e.g. a bare `**STATUS:**` line) are skipped so
    they don't appear as spurious empty entries.
    """
    out: dict[str, list[str]] = {}
    for m in _MARKERS_RE.finditer(text):
        # Strip any trailing markdown emphasis or whitespace from the value.
        value = m.group(2).rstrip("* \t")
        if value:  # skip empty captures
            out.setdefault(m.group(1), []).append(value)
    return out


def extract_message_envelopes(
    text: str,
    dispatch_id: str,
    *,
    seq_start: int = 1,
    source: dict | None = None,
) -> list[dict]:
    """Markers from worker text as goalflight.message.v1 envelopes."""
    from goalflight_messages import markers_to_envelopes

    return markers_to_envelopes(
        extract_markers(text),
        dispatch_id=dispatch_id,
        seq_start=seq_start,
        source=source,
    )


def _scan_out_of_scope_paths(tool_calls: list[dict[str, Any]], cwd: str | Path) -> list[str]:
    """Walk tool_call updates; return path strings from `locations` arrays
    that resolve outside cwd. De-duplicated, source-order preserved.

    ACP `ToolCall` / `ToolCallUpdate` may include a `locations: [{path, line?}]`
    array enumerating the files/dirs the call touches. Any path resolving
    outside the connection's working directory is a scope-leak candidate —
    the worker is reading/writing/executing something the controller's
    chunk wrapper didn't scope. Worth surfacing.

    Relative paths emitted by workers are resolved against the CONNECTION's
    cwd, not the caller process's cwd. Prevents false positives when the
    controller script runs from a different directory than the worker.

    Empty/None cwd disables scope checking entirely — returns [] regardless
    of tool_calls. Avoids Path("").resolve() spuriously matching the test
    process's cwd.
    """
    if not cwd:
        return []
    try:
        cwd_resolved = Path(cwd).resolve()
    except (OSError, ValueError, TypeError):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tc in tool_calls:
        for loc in tc.get("locations", []) or []:
            p = loc.get("path") if isinstance(loc, dict) else None
            if not p or p in seen:
                continue
            try:
                p_path = Path(p)
                if not p_path.is_absolute():
                    # Resolve relative paths against the connection's cwd,
                    # NOT the caller process's cwd. Workers often emit
                    # paths like "src/foo.py" relative to their working dir.
                    p_path = cwd_resolved / p_path
                p_resolved = p_path.resolve()
            except (OSError, ValueError):
                continue
            if not p_resolved.is_relative_to(cwd_resolved):
                seen.add(p)
                out.append(p)
    return out
