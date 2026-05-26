#!/usr/bin/env python3
"""Phase 0 MCP spike: goalflight_post_message → same bytes as file append."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from goalflight_messages import (  # noqa: E402
    MCP_TOOL_POST_MESSAGE,
    MessageError,
    default_fleet_dir,
    default_messages_dir,
    goalflight_post_message_tool,
)

TOOL_DESCRIPTOR = {
    "name": MCP_TOOL_POST_MESSAGE,
    "description": "Append a goalflight.message.v1 envelope to the dispatch inbox register.",
    "inputSchema": {
        "type": "object",
        "required": ["dispatch_id", "type"],
        "properties": {
            "dispatch_id": {"type": "string"},
            "type": {"type": "string"},
            "payload": {"type": "object"},
            "source": {"type": "object"},
            "seq": {"type": "integer"},
            "priority": {"type": "string"},
        },
    },
}


def handle_request(
    request: dict,
    *,
    messages_dir: Path,
    fleet_dir: Path | None = None,
    refresh_aggregate: bool = False,
) -> dict:
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    def respond(result: dict | None = None, *, error: dict | None = None) -> dict:
        out: dict = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result
        return out

    if method == "initialize":
        return respond(
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "goalflight-messages", "version": "0.1.0"},
            }
        )
    if method == "notifications/initialized":
        return {}
    if method == "tools/list":
        return respond({"tools": [TOOL_DESCRIPTOR]})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != MCP_TOOL_POST_MESSAGE:
            return respond(error={"code": -32601, "message": f"unknown tool: {name}"})
        try:
            posted = goalflight_post_message_tool(
                arguments,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
                refresh_aggregate=refresh_aggregate,
            )
        except MessageError as exc:
            return respond(error={"code": -32000, "message": str(exc)})
        return respond(
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(posted, indent=2),
                    }
                ],
                "isError": False,
            }
        )
    if method == "ping":
        return respond({})
    return respond(error={"code": -32601, "message": f"method not found: {method}"})


def cmd_call(args: argparse.Namespace) -> int:
    arguments = json.loads(args.arguments)
    result = goalflight_post_message_tool(
        arguments,
        messages_dir=args.messages_dir,
        fleet_dir=args.fleet_dir,
        refresh_aggregate=args.refresh_aggregate,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_stdio(args: argparse.Namespace) -> int:
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            request = json.loads(stripped)
        except json.JSONDecodeError as exc:
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            print(json.dumps(err), flush=True)
            continue
        response = handle_request(
            request,
            messages_dir=args.messages_dir,
            fleet_dir=args.fleet_dir,
            refresh_aggregate=args.refresh_aggregate,
        )
        if response:
            print(json.dumps(response), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Flight MCP message spike")
    parser.add_argument("--messages-dir", type=Path, default=default_messages_dir())
    parser.add_argument("--fleet-dir", type=Path, default=default_fleet_dir())
    parser.add_argument("--refresh-aggregate", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    call = sub.add_parser("call", help="Invoke goalflight_post_message once (no MCP transport)")
    call.add_argument("--arguments", required=True, help="JSON object tool arguments")
    call.set_defaults(func=cmd_call)

    stdio = sub.add_parser("stdio", help="Minimal MCP JSON-RPC over stdin/stdout")
    stdio.set_defaults(func=cmd_stdio)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
