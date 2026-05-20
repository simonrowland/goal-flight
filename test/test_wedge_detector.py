#!/usr/bin/env python3
"""Focused wedge-detector unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from goalflight_acp_client import AcpLivenessActivity  # noqa: E402


def _update(session_update: str, **fields):
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s1", "update": {"sessionUpdate": session_update, **fields}},
    }


def test_corpus_verified_tool_grace_rule() -> None:
    activity = AcpLivenessActivity(permission_timeout_s=10.0)

    activity.note_message(_update("tool_call", toolCallId="tool-1"), 100.0)
    assert activity.outstanding_tools == {"tool-1": 100.0}

    activity.note_message(_update("tool_call_update", toolCallId="tool-1", title="progress"), 101.0)
    assert activity.outstanding_tools == {"tool-1": 100.0}

    activity.note_message(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/request_permission",
            "params": {"toolCall": {"toolCallId": "permission-1", "status": "pending"}},
        },
        102.0,
    )
    assert activity.outstanding_count(103.0) == 2

    activity.note_message(_update("tool_call_update", toolCallId="tool-1", status="completed"), 103.0)
    assert activity.outstanding_count(103.0) == 1
    assert activity.snapshot(113.0)["outstanding_count"] == 1
    assert activity.timed_out(113.0, max_tool_s=60.0) == ("permission-1", 11.0)
    assert activity.outstanding_count(113.0) == 0

    direct_completed = AcpLivenessActivity()
    direct_completed.note_message(_update("tool_call", toolCallId="tool-2", status="completed"), 200.0)
    assert direct_completed.outstanding_count(200.0) == 0


def main() -> None:
    test_corpus_verified_tool_grace_rule()
    print("OK: wedge detector tests pass")


if __name__ == "__main__":
    main()
