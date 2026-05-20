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


def request_permission(session_id: str, tool_id: str) -> str:
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
                    "title": "edit file",
                    "status": "pending",
                },
                "options": [
                    {"optionId": "opt_once", "kind": "allow_once", "name": "Allow once"},
                    {"optionId": "opt_always", "kind": "allow_always", "name": "Always allow"},
                ],
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
    if SCENARIO == "tool_tracking":
        tool_update(session_id, "tool_call", "tool-1", status="pending")
        tool_update(session_id, "tool_call_update", "tool-1", status="completed")
        text_update(session_id, "tool done")
        response(req_id, {"sessionId": session_id, "stopReason": "end_turn"})
        return
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
    while True:
        message = read_message()
        if message is None:
            return
        handle(message)


if __name__ == "__main__":
    main()
