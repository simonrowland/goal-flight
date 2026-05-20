#!/usr/bin/env python3
"""Focused wedge-detector unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from goalflight_acp_run import ToolActivity, _apply_tool_activity  # noqa: E402


def _update(session_update: str, **fields):
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s1", "update": {"sessionUpdate": session_update, **fields}},
    }


def test_corpus_verified_tool_grace_rule() -> None:
    activity = ToolActivity()

    _apply_tool_activity(_update("tool_call", toolCallId="tool-1"), activity, 100.0)
    assert activity.outstanding_tools == {"tool-1": 100.0}

    _apply_tool_activity(_update("tool_call_update", toolCallId="tool-1", title="progress"), activity, 101.0)
    assert activity.outstanding_tools == {"tool-1": 100.0}

    _apply_tool_activity(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/request_permission",
            "params": {"toolCall": {"toolCallId": "permission-1", "status": "pending"}},
        },
        activity,
        102.0,
    )
    assert activity.outstanding_count == 2

    _apply_tool_activity(_update("tool_call_update", toolCallId="tool-1", status="completed"), activity, 103.0)
    assert activity.outstanding_count == 0

    direct_completed = ToolActivity()
    _apply_tool_activity(_update("tool_call", toolCallId="tool-2", status="completed"), direct_completed, 200.0)
    assert direct_completed.outstanding_count == 0


def main() -> None:
    test_corpus_verified_tool_grace_rule()
    print("OK: wedge detector tests pass")


if __name__ == "__main__":
    main()
