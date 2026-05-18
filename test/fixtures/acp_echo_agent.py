#!/usr/bin/env python3
# Vendored from aws-samples/sample-acp-bridge @ 2cd3c86af6a09178ea6fa42d398dbe72a572abcf
# License: MIT-0 (https://github.com/aws-samples/sample-acp-bridge/blob/main/LICENSE)
# Source: examples/echo-agent.py
# Local changes: none (vendored verbatim as a smoke-test fixture)
"""
ACP Echo Agent — 最小合规参考实现

实现了 ACP Bridge 要求的全部必选接口：
  - initialize
  - session/new
  - session/prompt  （含 agent_message_chunk 通知）
  - ping            （可选）

用法:
  python examples/echo-agent.py

接入 config.yaml:
  agents:
    echo:
      enabled: true
      mode: "acp"
      command: "python"
      acp_args: ["examples/echo-agent.py"]
      working_dir: "/tmp"
      description: "Echo reference agent"

合规测试:
  bash test/test_agent_compliance.sh python examples/echo-agent.py
"""

import json
import sys
import uuid


def send(msg: dict) -> None:
    print(json.dumps(msg, ensure_ascii=False), flush=True)


def send_response(req_id: int, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def send_error(req_id: int, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def send_notification(method: str, params: dict) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


# session_id → metadata
sessions: dict[str, dict] = {}


def handle(msg: dict) -> None:
    method = msg.get("method", "")
    params = msg.get("params") or {}
    req_id = msg.get("id")

    # ── initialize ──────────────────────────────────────────────────────────
    if method == "initialize":
        send_response(req_id, {
            "protocolVersion": 1,
            "agentInfo": {"name": "echo-agent", "version": "0.7.1"},
            "capabilities": {},
        })

    # ── session/new ─────────────────────────────────────────────────────────
    elif method == "session/new":
        session_id = str(uuid.uuid4())
        sessions[session_id] = {"cwd": params.get("cwd", "/tmp")}
        send_response(req_id, {"sessionId": session_id})

    # ── session/prompt ───────────────────────────────────────────────────────
    elif method == "session/prompt":
        session_id = params.get("sessionId", "")
        if session_id not in sessions:
            send_error(req_id, -32602, f"unknown session: {session_id}")
            return
        prompt_parts = params.get("prompt", [])
        text = "".join(
            p.get("text", "") for p in prompt_parts if p.get("type") == "text"
        )

        # 发送内容通知（agent_message_chunk）
        send_notification("session/update", {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"text": f"echo: {text}"},
            },
        })

        # 发送最终响应
        send_response(req_id, {
            "sessionId": session_id,
            "stopReason": "end_turn",
        })

    # ── ping（可选）─────────────────────────────────────────────────────────
    elif method == "ping":
        if req_id is not None:
            send_response(req_id, {})

    # ── session/cancel（通知，无需响应）─────────────────────────────────────
    elif method == "session/cancel":
        pass

    # ── 未知方法 ─────────────────────────────────────────────────────────────
    elif req_id is not None:
        send_error(req_id, -32601, f"method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(msg)


if __name__ == "__main__":
    main()
