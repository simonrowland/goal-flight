#!/usr/bin/env python3
"""Tiny hermetic ACP peer for SDK transport tests."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid


SCENARIO = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO", "echo")
sessions: dict[str, dict] = {}


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def response(req_id: int, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def notification(method: str, params: dict) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


def text_update(session_id: str, text: str) -> None:
    notification(
        "session/update",
        {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    )


def thought_update(session_id: str, text: str) -> None:
    notification(
        "session/update",
        {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    )


def vendor_update(session_id: str, text: str) -> None:
    notification(
        "session/update",
        {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "_x.ai/vendor_progress",
                "content": {"type": "text", "text": text},
            },
        },
    )


def tool_update(session_id: str, kind: str, tool_id: str, **fields) -> None:
    notification(
        "session/update",
        {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": kind,
                "toolCallId": tool_id,
                "title": fields.pop("title", "tool"),
                **fields,
            },
        },
    )


def read_message() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


DEFAULT_PERMISSION_OPTIONS = [
    {"optionId": "opt_once", "kind": "allow_once", "name": "Allow once"},
    {"optionId": "opt_always", "kind": "allow_always", "name": "Always allow"},
]

# The shape codex-acp 0.14.0 actually sends for a built-in tool gate (captured
# 2026-05-21): allow_once + reject_once, ids approved/abort, NO allow_always.
CODEX_PERMISSION_OPTIONS = [
    {"optionId": "approved", "kind": "allow_once", "name": "Approve"},
    {"optionId": "abort", "kind": "reject_once", "name": "Abort"},
]

# The shape codex-acp surfaces when an MCP tool ELICITS (request_user_input)
# under features.tool_call_mcp_elicitation=true (captured 2026-05-21 from
# context-mode ctx_index, title "Approve Index Content"): multiple allow_always
# options + a reject. Auto-allow must pick an allow_always (approve-for-session).
ELICITATION_PERMISSION_OPTIONS = [
    {"optionId": "approved", "kind": "allow_once", "name": "Approve once"},
    {"optionId": "approved-for-session", "kind": "allow_always", "name": "Approve for session"},
    {"optionId": "approved-always", "kind": "allow_always", "name": "Always approve"},
    {"optionId": "cancel", "kind": "reject_once", "name": "Cancel"},
]


def request_permission(
    session_id: str,
    tool_id: str,
    options: list | None = None,
    title: str = "edit file",
) -> str:
    request_id = 9001
    send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_id,
                    "title": title,
                    "status": "pending",
                },
                "options": DEFAULT_PERMISSION_OPTIONS if options is None else options,
            },
        }
    )
    while True:
        message = read_message()
        if message is None:
            return "eof"
        if message.get("id") == request_id:
            outcome = ((message.get("result") or {}).get("outcome") or {})
            return str(outcome.get("optionId") or outcome.get("option_id") or "")


def handle_prompt(req_id: int, params: dict) -> None:
    session_id = params.get("sessionId", "")
    if SCENARIO == "overlimit":
        big = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "x" * 8192},
                },
            },
        }
        sys.stdout.write(json.dumps(big, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        text_update(session_id, "after-limit")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    if SCENARIO == "overlimit_response":
        big = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "sessionId": session_id,
                "stopReason": "end_turn",
                "payload": "x" * 8192,
            },
        }
        sys.stdout.write(json.dumps(big, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        while True:
            message = read_message()
            if message is None:
                return
            if message.get("method") == "session/cancel":
                return
            time.sleep(0.01)
    if SCENARIO == "permission":
        selected = request_permission(session_id, "perm-1")
        text_update(session_id, f"permission:{selected}")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    if SCENARIO == "permission_codex":
        # Real codex-acp built-in-tool gate: allow_once + reject_once, no
        # allow_always. Auto-allow must pick the allow_once ('approved'),
        # NOT the reject_once ('abort').
        selected = request_permission(
            session_id, "perm-codex",
            options=CODEX_PERMISSION_OPTIONS, title="Edit /tmp/probe.txt",
        )
        text_update(session_id, f"permission:{selected}")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    if SCENARIO == "permission_reject_first":
        # Same options, REJECT offered first. The old options[0] fallback would
        # have selected 'abort'; auto-allow must still pick 'approved'.
        selected = request_permission(
            session_id, "perm-rf",
            options=list(reversed(CODEX_PERMISSION_OPTIONS)), title="Edit x",
        )
        text_update(session_id, f"permission:{selected}")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    if SCENARIO == "permission_elicitation":
        # MCP elicitation surfaced as a permission (codex-acp +
        # features.tool_call_mcp_elicitation=true). The worker unblocks only if
        # the client answers with an allow option (auto-allow picks allow_always).
        selected = request_permission(
            session_id, "perm-elicit",
            options=ELICITATION_PERMISSION_OPTIONS, title="Approve Index Content",
        )
        text_update(session_id, f"permission:{selected}")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    if SCENARIO == "permission_reject_only":
        # Only a reject option exists: auto-allow cannot grant, must cancel
        # cleanly (definitive answer) so the worker still unblocks.
        selected = request_permission(
            session_id, "perm-ro",
            options=[{"optionId": "abort", "kind": "reject_once", "name": "Abort"}],
            title="Edit x",
        )
        text_update(session_id, f"permission:{selected or 'cancelled'}")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    if SCENARIO == "tool_tracking":
        tool_update(session_id, "tool_call", "tool-1", status="pending")
        tool_update(session_id, "tool_call_update", "tool-1", status="completed")
        text_update(session_id, "tool done")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    if SCENARIO == "tool_stuck":
        # Open a tool call and never resolve it (no completed update, no
        # end_turn). The outstanding tool keeps outstanding_count > 0, which
        # gates OFF the silence-class backstops (dead-sample wedge / progress
        # stall / max_quiet all require outstanding_count == 0), so the runner's
        # per-tool absolute wall (--max-tool-s) is the deterministic terminal.
        tool_update(session_id, "tool_call", "tool-stuck", status="pending")
        while True:
            time.sleep(1.0)
    if SCENARIO == "fine_chunks_vendor":
        for chunk in ("COM", "P", "LETE", ": ", "gro", "k ", "sm", "oke", "\n"):
            text_update(session_id, chunk)
            vendor_update(session_id, "noise")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    if SCENARIO == "raw_vendor_flood":
        interval = float(os.environ.get("GOALFLIGHT_FAKE_ACP_INTERVAL", "0.05"))
        while True:
            vendor_update(session_id, "noise")
            time.sleep(interval)
    if SCENARIO == "progress_then_silent":
        text_update(session_id, "started")
        while True:
            time.sleep(1.0)
    if SCENARIO == "long_reasoning_pause":
        pause_s = float(os.environ.get("GOALFLIGHT_FAKE_ACP_LONG_PAUSE_S", "1.0"))
        text_update(session_id, "started")
        time.sleep(pause_s)
        text_update(session_id, "finished")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    if SCENARIO == "thought_stream_pause":
        interval = float(os.environ.get("GOALFLIGHT_FAKE_ACP_INTERVAL", "0.2"))
        chunks = int(os.environ.get("GOALFLIGHT_FAKE_ACP_THOUGHT_CHUNKS", "5"))
        for i in range(chunks):
            thought_update(session_id, f"thinking-{i}")
            if i != chunks - 1:
                time.sleep(interval)
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    if SCENARIO in {"idle_silent", "dead_silent_turn"}:
        # Handshake completes, then the prompt turn emits NOTHING and never
        # responds — models a worker that goes fully event-silent (no progress,
        # no vendor noise). The heartbeat wedge can't fire (it requires >=1 prior
        # progress event), so the runner's idle-timeout / IdleLivenessGate path
        # is the only thing that can reap it. Stays low-CPU so the CPU-aware idle
        # gate classifies it wedged rather than running_quiet.
        while True:
            time.sleep(1.0)
    if SCENARIO == "blocked":
        text_update(session_id, "BLOCKED: need maintainer\n")
        while True:
            message = read_message()
            if message is None:
                return
            if message.get("method") == "session/cancel":
                return
            time.sleep(0.01)
    if SCENARIO == "goal":
        text_update(session_id, "STATUS: working\n")
        text_update(session_id, "COMPLETE: goal done\n")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
    text_update(session_id, "echo")
    response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})


def handle(message: dict) -> None:
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        response(
            req_id,
            {
                "protocolVersion": 1,
                "agentInfo": {"name": "fake-agent", "version": "0.1"},
                "capabilities": {},
            },
        )
    elif method == "session/new":
        session_id = str(uuid.uuid4())
        sessions[session_id] = {"cwd": params.get("cwd")}
        response(req_id, {"sessionId": session_id})
    elif method == "session/prompt":
        handle_prompt(req_id, params)
    elif method == "session/cancel":
        return
    elif req_id is not None:
        send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": method}})


def main() -> None:
    if SCENARIO == "handshake_wedge":
        # Spawn, then never read stdin or answer initialize — the intermittent
        # codex-acp handshake wedge (worker is up but the handshake hangs).
        # spawn_and_handshake_with_retry must hit its handshake_timeout, kill
        # this worker, and respawn (the kill-before-respawn invariant).
        while True:
            time.sleep(1.0)
    while True:
        message = read_message()
        if message is None:
            return
        handle(message)


if __name__ == "__main__":
    main()
