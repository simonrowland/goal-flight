"""Ergonomic wrapper around acp_client.AcpConnection.

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

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acp_client import AcpConnection


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

    @property
    def ok(self) -> bool:
        return self.error is None and self.stop_reason == "end_turn"


async def run_prompt(
    conn: AcpConnection,
    text: str,
    *,
    idle_timeout: float | None = 300,
    keep_raw: bool = False,
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
    """
    result = PromptResult()
    async for event in conn.session_prompt(text, idle_timeout=idle_timeout):
        if keep_raw:
            result.raw_events.append(event)
        if "_prompt_result" in event:
            envelope = event["_prompt_result"]
            if "error" in envelope:
                result.error = envelope["error"]
            else:
                inner = envelope.get("result") or {}
                result.stop_reason = inner.get("stopReason")
            continue
        update = event.get("params", {}).get("update", {})
        kind = update.get("sessionUpdate")
        content = update.get("content", {}) or {}
        if kind == "agent_message_chunk":
            result.text += content.get("text", "")
        elif kind == "agent_thought_chunk":
            result.thoughts += content.get("text", "")
        elif kind in ("tool_call", "tool_call_update"):
            result.tool_calls.append(update)
        elif kind == "plan":
            for entry in update.get("entries", []) or []:
                entry_text = entry.get("content")
                if entry_text:
                    result.plan_entries.append(entry_text)
    # Post-hoc scope-leak audit (Design 1): scan tool_call locations for paths
    # outside the connection's cwd. Informational signal; doesn't gate dispatch.
    cwd = getattr(conn, "cwd", None)
    if cwd:
        result.out_of_scope_writes = _scan_out_of_scope_paths(result.tool_calls, cwd)
    return result


# Match the marker vocabulary, tolerating optional markdown emphasis around the
# marker tag. Codex emits unwrapped `STATUS: ...`; grok wraps as `**STATUS:** ...`
# (markdown bold for the tag, value plain). Pattern mirrors SKILL.md §Worker
# message passing: `^\**(MARKER):\**` with the value following on the same line.
_MARKERS_RE = re.compile(
    r"^\**(STATUS|RESULT|USER-NEED|USER-CONFIRM|BLOCKED|COMPLETE):\**\s*(.+?)\s*\**$",
    re.MULTILINE,
)


def extract_markers(text: str) -> dict[str, list[str]]:
    """Pull goal-flight marker-vocab lines from worker output text.

    Returns {marker_type: [values...]} in source order per type. Tolerates
    optional markdown emphasis around the marker tag (grok wraps as
    `**STATUS:** ...`; codex emits unwrapped `STATUS: ...`). See goal-flight
    SKILL.md §Worker message passing for the vocabulary spec.

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
