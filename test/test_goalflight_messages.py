#!/usr/bin/env python3
"""Tests for marker → envelope conversion."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from acp_runner import extract_markers, extract_message_envelopes
from goalflight_messages import MARKER_TO_TYPE, markers_to_envelopes


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_marker_mapping() -> None:
    sample = "**STATUS:** working\nUSER-NEED: need maintainer\nCOMPLETE: goal done\n"
    markers = extract_markers(sample)
    envelopes = markers_to_envelopes(
        markers,
        dispatch_id="d-test",
        source={"node": "local", "adapter": "codex-acp", "transport": "acp"},
    )
    assert_true("three envelopes", len(envelopes) == 3)
    assert_true("monotonic seq", [e["seq"] for e in envelopes] == [1, 2, 3])
    assert_true("status type", envelopes[0]["type"] == "status")
    assert_true("user_need type", envelopes[1]["type"] == "user_need")
    assert_true("complete maps to result", envelopes[2]["type"] == "result")
    assert_true("complete payload flag", envelopes[2]["payload"].get("complete") is True)


def test_unknown_marker_monitor() -> None:
    envelopes = markers_to_envelopes({"CUSTOM": ["something"]}, dispatch_id="d2")
    assert_true("unknown -> monitor", envelopes[0]["type"] == "monitor")
    assert_true("unknown payload", envelopes[0]["payload"]["unknown_marker"] == "CUSTOM")


def test_acp_runner_wrapper() -> None:
    text = "USER-CONFIRM: approve risky change\n"
    envelopes = extract_message_envelopes(text, "d3", source={"transport": "bash-tail"})
    assert_true("wrapper count", len(envelopes) == 1)
    assert_true("wrapper type", envelopes[0]["type"] == "user_confirm")
    assert_true("all mapped kinds covered", set(MARKER_TO_TYPE) >= {
        "STATUS", "RESULT", "USER-NEED", "USER-CONFIRM", "BLOCKED", "COMPLETE"
    })


def test_inbox_append_read_order() -> None:
    import tempfile
    from goalflight_messages import append_envelope, inbox_path, read_envelopes

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        path = inbox_path(messages_dir, "d-inbox")
        env1 = markers_to_envelopes({"STATUS": ["a"]}, dispatch_id="d-inbox")[0]
        env2 = markers_to_envelopes({"USER-NEED": ["help"]}, dispatch_id="d-inbox", seq_start=2)[0]
        append_envelope(path, env1)
        append_envelope(path, env2)
        loaded = read_envelopes(path)
        assert_true("two lines", len(loaded) == 2)
        assert_true("order preserved", loaded[0]["seq"] == 1 and loaded[1]["seq"] == 2)
        assert_true("last one", read_envelopes(path, last_n=1)[0]["type"] == "user_need")


def test_inbox_corrupt_line_fails_closed() -> None:
    import tempfile
    from goalflight_messages import MessageError, inbox_path, read_envelopes

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        path = inbox_path(messages_dir, "bad")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema":"goalflight.message.v1"}\n')
        try:
            read_envelopes(path)
            assert_true("should fail", False)
        except MessageError:
            pass


def test_aggregate_open_user_need() -> None:
    import tempfile
    from goalflight_messages import append_envelope, build_aggregate, inbox_path, refresh_aggregate

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        (fleet_dir / "register").mkdir()
        path = inbox_path(messages_dir, "d-agg")
        append_envelope(
            path,
            markers_to_envelopes({"USER-NEED": ["pick account"]}, dispatch_id="d-agg")[0],
        )
        aggregate = build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("active dispatch", "d-agg" in aggregate["active_dispatches"])
        assert_true("open need", len(aggregate["open_user_needs"]) == 1)
        written = refresh_aggregate(fleet_dir, messages_dir=messages_dir)
        assert_true("written aggregate", (fleet_dir / "register" / "aggregate.json").exists())
        assert_true("same open need", len(written["open_user_needs"]) == 1)


def test_relay_user_need_e2e() -> None:
    import subprocess
    import tempfile
    from goalflight_messages import append_envelope, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        (fleet_dir / "register").mkdir()
        dispatch_id = "d-relay-e2e"
        path = inbox_path(messages_dir, dispatch_id)
        append_envelope(
            path,
            markers_to_envelopes({"USER-NEED": ["pick billing account"]}, dispatch_id=dispatch_id)[0],
        )
        env = {**dict(os.environ), "GOALFLIGHT_MESSAGES_DIR": str(messages_dir), "GOALFLIGHT_FLEET_DIR": str(fleet_dir)}
        relay = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "goalflight_messages.py"), "relay"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert_true("relay exit 2", relay.returncode == 2)
        assert_true("relay summary", "USER-NEED relay:" in relay.stdout)
        assert_true("dispatch in summary", dispatch_id in relay.stdout)
        append_envelope(
            path,
            markers_to_envelopes({"COMPLETE": ["answered"]}, dispatch_id=dispatch_id, seq_start=2)[0],
        )
        relay2 = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "goalflight_messages.py"), "relay"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert_true("relay clear exit 0", relay2.returncode == 0)


def test_mcp_post_matches_file_append() -> None:
    import tempfile
    from goalflight_messages import (
        goalflight_post_message_tool,
        inbox_path,
        post_message,
        read_envelopes,
        serialize_envelope_line,
    )

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        args = {
            "dispatch_id": "d-mcp",
            "type": "user_need",
            "payload": {"text": "via mcp"},
            "source": {"node": "local", "adapter": "mcp-spike", "transport": "mcp"},
            "seq": 1,
        }
        mcp = goalflight_post_message_tool(args, messages_dir=messages_dir)
        cli = post_message(
            dispatch_id="d-mcp",
            msg_type="user_need",
            payload={"text": "via cli"},
            messages_dir=messages_dir,
            source={"node": "local", "adapter": "cli", "transport": "controller"},
            seq=2,
        )
        path = inbox_path(messages_dir, "d-mcp")
        raw = path.read_text()
        lines = raw.splitlines(keepends=True)
        assert_true("two lines", len(lines) == 2)
        assert_true("mcp bytes canonical", lines[0] == mcp["line"])
        assert_true("cli bytes canonical", lines[1] == cli["line"])
        loaded = read_envelopes(path)
        assert_true("seq order", [e["seq"] for e in loaded] == [1, 2])
        assert_true("serialize helper", serialize_envelope_line(loaded[0]) == lines[0])


def test_mcp_stdio_tools_call() -> None:
    import json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        env = {**dict(os.environ), "GOALFLIGHT_MESSAGES_DIR": str(messages_dir)}
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "goalflight_post_message",
                "arguments": {
                    "dispatch_id": "d-stdio",
                    "type": "status",
                    "payload": {"text": "stdio spike"},
                    "seq": 1,
                },
            },
        }
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "goalflight_mcp_messages.py"), "stdio"],
            input=json.dumps(req) + "\n",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert_true("stdio exit 0", proc.returncode == 0)
        response = json.loads(proc.stdout.strip().splitlines()[-1])
        assert_true("no rpc error", "error" not in response)
        content = response["result"]["content"][0]["text"]
        posted = json.loads(content)
        path = messages_dir / "d-stdio.jsonl"
        assert_true("file exists", path.exists())
        assert_true("stdio line match", path.read_text() == posted["line"])


def main() -> None:
    for test in (
        test_marker_mapping,
        test_unknown_marker_monitor,
        test_acp_runner_wrapper,
        test_inbox_append_read_order,
        test_inbox_corrupt_line_fails_closed,
        test_aggregate_open_user_need,
        test_relay_user_need_e2e,
        test_mcp_post_matches_file_append,
        test_mcp_stdio_tools_call,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
