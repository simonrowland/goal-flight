#!/usr/bin/env python3
"""Tests for marker → envelope conversion."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from acp_runner import extract_markers, extract_message_envelopes
from goalflight_messages import MARKER_TO_TYPE, markers_to_envelopes


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def run_messages_cli(messages_dir: Path, fleet_dir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "goalflight_messages.py"),
            "--messages-dir",
            str(messages_dir),
            "--fleet-dir",
            str(fleet_dir),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


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


def test_post_message_rejects_invalid_seq_and_accepts_one() -> None:
    import tempfile
    from goalflight_messages import MessageError, inbox_path, post_message, read_envelopes

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        for bad_seq in (0, "abc"):
            try:
                post_message(
                    dispatch_id="d-seq",
                    msg_type="status",
                    payload={"text": "bad"},
                    messages_dir=messages_dir,
                    seq=bad_seq,  # type: ignore[arg-type]
                )
                assert_true(f"seq {bad_seq!r} rejected", False)
            except MessageError as exc:
                assert_true("seq error is closed", "seq must be an integer >= 1" in str(exc))

        result = post_message(
            dispatch_id="d-seq",
            msg_type="status",
            payload={"text": "ok"},
            messages_dir=messages_dir,
            seq=1,
        )
        path = inbox_path(messages_dir, "d-seq")
        loaded = read_envelopes(path)
        assert_true("valid seq writes", len(loaded) == 1)
        assert_true("valid seq remains one", loaded[0]["seq"] == 1)
        assert_true("returned line matches", path.read_text() == result["line"])


def test_post_message_allocates_seq_under_mail_lock() -> None:
    import tempfile
    import goalflight_messages as messages
    from goalflight_messages import post_message, read_envelopes

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        path = messages.inbox_path(messages_dir, "d-race")
        original_next_seq = messages.next_seq
        guard = threading.Lock()
        active = 0
        max_active = 0

        def slow_next_seq(seq_path: Path) -> int:
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return original_next_seq(seq_path)
            finally:
                with guard:
                    active -= 1

        messages.next_seq = slow_next_seq  # type: ignore[assignment]
        try:
            threads = [
                threading.Thread(
                    target=post_message,
                    kwargs={
                        "dispatch_id": "d-race",
                        "msg_type": "status",
                        "payload": {"text": f"msg-{idx}"},
                        "messages_dir": messages_dir,
                    },
                )
                for idx in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            messages.next_seq = original_next_seq  # type: ignore[assignment]

        loaded = read_envelopes(path)
        assert_true("serialized next_seq critical section", max_active == 1)
        assert_true("two messages", len(loaded) == 2)
        assert_true("unique monotonic seqs", [env["seq"] for env in loaded] == [1, 2])


def test_controller_summary_includes_quota_advisory() -> None:
    import tempfile
    from goalflight_messages import controller_mail_summary, post_message

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        post_message(
            dispatch_id="controller-quota-advisory",
            msg_type="advisory",
            payload={"text": "openai quota exhausted"},
            messages_dir=messages_dir,
        )
        summary = controller_mail_summary(owned_dispatch_ids={"mine-1"}, messages_dir=messages_dir)
        assert_true("advisory surfaced", summary["count"] == 1)
        assert_true("advisory dispatch id", summary["needs"][0]["dispatch_id"] == "controller-quota-advisory")
        assert_true("advisory kind", summary["needs"][0]["type"] == "advisory")
        assert_true("advisory hint", "openai quota exhausted" in summary["hint"])


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


def test_mark_read_creates_cursor_and_unseen_filters() -> None:
    import tempfile
    from goalflight_messages import READ_CURSOR_FILE, append_envelope, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-cursor"
        path = inbox_path(messages_dir, dispatch_id)
        for env in markers_to_envelopes(
            {"STATUS": ["started"], "USER-NEED": ["need decision"]},
            dispatch_id=dispatch_id,
        ):
            append_envelope(path, env)

        marked = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["mark-read", "--dispatch-id", dispatch_id, "--through", "1"],
        )
        assert_true("mark-read exit 0", marked.returncode == 0)
        cursor_path = messages_dir / READ_CURSOR_FILE
        assert_true("cursor created", cursor_path.exists())
        assert_true("cursor value", json.loads(cursor_path.read_text())[dispatch_id] == 1)

        unseen = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen"])
        assert_true("unseen exit 0", unseen.returncode == 0)
        lines = unseen.stdout.splitlines()
        shown = json.loads(lines[0])
        assert_true("only one unseen", len(shown) == 1)
        assert_true("seq 2 unseen", shown[0]["seq"] == 2)
        assert_true("count line", lines[1] == "unseen counts: d-cursor=1")


def test_mark_read_all_advances_every_inbox_to_current_max() -> None:
    import tempfile
    from goalflight_messages import READ_CURSOR_FILE, append_envelope, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        for dispatch_id, count in {"d-one": 1, "d-two": 2}.items():
            path = inbox_path(messages_dir, dispatch_id)
            markers = {"STATUS": [f"{dispatch_id}-{idx}" for idx in range(count)]}
            for env in markers_to_envelopes(markers, dispatch_id=dispatch_id):
                append_envelope(path, env)

        marked = run_messages_cli(messages_dir, fleet_dir, ["mark-read", "--all"])
        assert_true("mark-read all exit 0", marked.returncode == 0)
        cursor = json.loads((messages_dir / READ_CURSOR_FILE).read_text())
        assert_true("first inbox max", cursor["d-one"] == 1)
        assert_true("second inbox max", cursor["d-two"] == 2)


def test_mark_read_through_never_rewinds() -> None:
    import tempfile
    from goalflight_messages import READ_CURSOR_FILE

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        first = run_messages_cli(messages_dir, fleet_dir, ["mark-read", "--dispatch-id", "d-sticky", "--through", "5"])
        second = run_messages_cli(messages_dir, fleet_dir, ["mark-read", "--dispatch-id", "d-sticky", "--through", "3"])
        assert_true("first mark-read ok", first.returncode == 0)
        assert_true("second mark-read ok", second.returncode == 0)
        cursor = json.loads((messages_dir / READ_CURSOR_FILE).read_text())
        assert_true("cursor stayed at high-water mark", cursor["d-sticky"] == 5)
        assert_true("unchanged reported", "d-sticky 5->5 (unchanged)" in second.stdout)


def test_concurrent_mark_read_through_merges_per_inbox_max() -> None:
    import tempfile
    from goalflight_messages import READ_CURSOR_FILE

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        messages_dir.mkdir()
        fleet_dir.mkdir()
        targets = [
            ("d-a", 1),
            ("d-a", 5),
            ("d-a", 3),
            ("d-a", 9),
            ("d-b", 2),
            ("d-b", 7),
            ("d-b", 4),
            ("d-b", 6),
        ]
        results: list[tuple[str, int, subprocess.CompletedProcess[str]]] = []
        guard = threading.Lock()

        def worker(dispatch_id: str, through: int) -> None:
            result = run_messages_cli(
                messages_dir,
                fleet_dir,
                ["mark-read", "--dispatch-id", dispatch_id, "--through", str(through)],
            )
            with guard:
                results.append((dispatch_id, through, result))

        threads = [threading.Thread(target=worker, args=target) for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert_true("all workers returned", len(results) == len(targets))
        for dispatch_id, through, result in results:
            assert_true(f"{dispatch_id} through {through} exit 0: {result.stderr}", result.returncode == 0)
        cursor = json.loads((messages_dir / READ_CURSOR_FILE).read_text())
        assert_true("d-a max retained", cursor["d-a"] == 9)
        assert_true("d-b max retained", cursor["d-b"] == 7)


def test_read_unseen_ack_advances_to_shown() -> None:
    import tempfile
    from goalflight_messages import READ_CURSOR_FILE, append_envelope, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-ack"
        path = inbox_path(messages_dir, dispatch_id)
        for env in markers_to_envelopes({"STATUS": ["one", "two"]}, dispatch_id=dispatch_id):
            append_envelope(path, env)

        first = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen", "--ack"])
        assert_true("ack read ok", first.returncode == 0)
        first_lines = first.stdout.splitlines()
        assert_true("both shown", [env["seq"] for env in json.loads(first_lines[0])] == [1, 2])
        assert_true("pre-ack count", first_lines[1] == "unseen counts: d-ack=2")
        cursor = json.loads((messages_dir / READ_CURSOR_FILE).read_text())
        assert_true("ack cursor", cursor[dispatch_id] == 2)

        second = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen"])
        second_lines = second.stdout.splitlines()
        assert_true("nothing left", json.loads(second_lines[0]) == [])
        assert_true("zero count", second_lines[1] == "unseen counts: d-ack=0")


def test_ack_cursor_write_failure_warns_without_traceback() -> None:
    import tempfile
    from goalflight_messages import READ_CURSOR_FILE, append_envelope, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-ack-fail"
        path = inbox_path(messages_dir, dispatch_id)
        append_envelope(path, markers_to_envelopes({"STATUS": ["shown"]}, dispatch_id=dispatch_id)[0])
        (messages_dir / READ_CURSOR_FILE).mkdir()

        read = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen", "--ack"])
        assert_true("read ack exits nonzero", read.returncode == 1)
        assert_true("read still shows envelope", json.loads(read.stdout.splitlines()[0])[0]["seq"] == 1)
        assert_true("read count printed", read.stdout.splitlines()[1] == "unseen counts: d-ack-fail=1")
        assert_true("read warning", "WARNING: cursor not advanced:" in read.stderr)
        assert_true("read no traceback", "Traceback" not in read.stderr)

        relay = run_messages_cli(messages_dir, fleet_dir, ["relay", "--new", "--ack"])
        assert_true("relay ack exits nonzero", relay.returncode == 1)
        assert_true("relay still shows envelope", json.loads(relay.stdout.splitlines()[0])[0]["seq"] == 1)
        assert_true("relay count printed", relay.stdout.splitlines()[1] == "unseen counts: d-ack-fail=1")
        assert_true("relay warning", "WARNING: cursor not advanced:" in relay.stderr)
        assert_true("relay no traceback", "Traceback" not in relay.stderr)


def test_mark_read_cursor_write_failure_warns_without_traceback() -> None:
    import tempfile
    from goalflight_messages import READ_CURSOR_FILE

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        messages_dir.mkdir()
        fleet_dir.mkdir()
        (messages_dir / READ_CURSOR_FILE).mkdir()

        marked = run_messages_cli(messages_dir, fleet_dir, ["mark-read", "--dispatch-id", "d-fail", "--through", "1"])
        assert_true("mark-read exits nonzero", marked.returncode == 1)
        assert_true("mark-read warning", "WARNING: cursor not advanced:" in marked.stderr)
        assert_true("mark-read no traceback stderr", "Traceback" not in marked.stderr)
        assert_true("mark-read no traceback stdout", "Traceback" not in marked.stdout)


def test_corrupt_or_absent_cursor_means_all_unseen() -> None:
    import tempfile
    from goalflight_messages import READ_CURSOR_FILE, append_envelope, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-corrupt"
        path = inbox_path(messages_dir, dispatch_id)
        for env in markers_to_envelopes({"STATUS": ["one", "two"]}, dispatch_id=dispatch_id):
            append_envelope(path, env)

        absent = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen"])
        assert_true("absent cursor ok", absent.returncode == 0)
        assert_true("absent shows all", [env["seq"] for env in json.loads(absent.stdout.splitlines()[0])] == [1, 2])

        (messages_dir / READ_CURSOR_FILE).write_text("{not json\n", encoding="utf-8")
        corrupt = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen"])
        assert_true("corrupt cursor ok", corrupt.returncode == 0)
        assert_true("corrupt shows all", [env["seq"] for env in json.loads(corrupt.stdout.splitlines()[0])] == [1, 2])


def test_seen_but_open_user_need_still_surfaces_in_normal_relay() -> None:
    import tempfile
    from goalflight_messages import append_envelope, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-open-seen"
        path = inbox_path(messages_dir, dispatch_id)
        append_envelope(
            path,
            markers_to_envelopes({"USER-NEED": ["answer required"]}, dispatch_id=dispatch_id)[0],
        )
        mark = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["mark-read", "--dispatch-id", dispatch_id, "--through", "1"],
        )
        assert_true("mark seen ok", mark.returncode == 0)

        unseen = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen"])
        assert_true("seen hidden from unseen", json.loads(unseen.stdout.splitlines()[0]) == [])
        relay = run_messages_cli(messages_dir, fleet_dir, ["relay"])
        assert_true("normal relay still open", relay.returncode == 2)
        assert_true("open user_need remains", "USER-NEED relay: [d-open-seen] user_need: answer required" in relay.stdout)


def test_default_read_and_relay_output_unchanged_without_cursor_ops() -> None:
    import tempfile
    from goalflight_messages import append_envelope, inbox_path, markers_to_envelopes

    def stable_status(stdout: str) -> dict:
        data = json.loads(stdout)
        data.pop("updated_at", None)
        return data

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-default"
        path = inbox_path(messages_dir, dispatch_id)
        envelope = markers_to_envelopes({"USER-NEED": ["byte stable"]}, dispatch_id=dispatch_id)[0]
        append_envelope(path, envelope)

        read = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id])
        assert_true("read default exit 0", read.returncode == 0)
        assert_true("read bytes stable", read.stdout == json.dumps([envelope]) + "\n")

        relay = run_messages_cli(messages_dir, fleet_dir, ["relay"])
        assert_true("relay default exit 2", relay.returncode == 2)
        assert_true("relay bytes stable", relay.stdout == "USER-NEED relay: [d-default] user_need: byte stable\n")

        status = run_messages_cli(messages_dir, fleet_dir, ["status"])
        assert_true("status default exit 0", status.returncode == 0)
        stable_before = stable_status(status.stdout)

        marked = run_messages_cli(messages_dir, fleet_dir, ["mark-read", "--dispatch-id", dispatch_id, "--through", "1"])
        assert_true("cursor op exit 0", marked.returncode == 0)

        read_after = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id])
        assert_true("read after cursor exit 0", read_after.returncode == 0)
        assert_true("read after cursor bytes stable", read_after.stdout == read.stdout)

        relay_after = run_messages_cli(messages_dir, fleet_dir, ["relay"])
        assert_true("relay after cursor exit 2", relay_after.returncode == 2)
        assert_true("relay after cursor bytes stable", relay_after.stdout == relay.stdout)

        status_after = run_messages_cli(messages_dir, fleet_dir, ["status"])
        assert_true("status after cursor exit 0", status_after.returncode == 0)
        assert_true("status stable fields unchanged", stable_status(status_after.stdout) == stable_before)


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
        test_post_message_rejects_invalid_seq_and_accepts_one,
        test_post_message_allocates_seq_under_mail_lock,
        test_controller_summary_includes_quota_advisory,
        test_mcp_stdio_tools_call,
        test_mark_read_creates_cursor_and_unseen_filters,
        test_mark_read_all_advances_every_inbox_to_current_max,
        test_mark_read_through_never_rewinds,
        test_concurrent_mark_read_through_merges_per_inbox_max,
        test_read_unseen_ack_advances_to_shown,
        test_ack_cursor_write_failure_warns_without_traceback,
        test_mark_read_cursor_write_failure_warns_without_traceback,
        test_corrupt_or_absent_cursor_means_all_unseen,
        test_seen_but_open_user_need_still_surfaces_in_normal_relay,
        test_default_read_and_relay_output_unchanged_without_cursor_ops,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
