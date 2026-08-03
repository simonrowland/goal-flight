#!/usr/bin/env python3
"""Tests for marker → envelope conversion."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from acp_runner import extract_markers, extract_message_envelopes
from goalflight_messages import MARKER_TO_TYPE, markers_to_envelopes


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def run_messages_cli(
    messages_dir: Path,
    fleet_dir: Path,
    args: list[str],
    *,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        {
            "GOALFLIGHT_MESSAGES_DIR": str(messages_dir),
            "GOALFLIGHT_FLEET_DIR": str(fleet_dir),
            "GOALFLIGHT_STATE_DIR": str(messages_dir.parent / "state"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(messages_dir.parent / "pids"),
            "GOALFLIGHT_TASK_STORE_DIR": str(messages_dir.parent / "task-store"),
        }
    )
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
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def write_ledger_record(
    base: Path,
    dispatch_id: str,
    project_root: Path,
    *,
    state: str = "running",
    task_ids: list[str] | None = None,
    worker_pid: int | None = None,
    detached: bool = False,
    reason: str | None = None,
    started_at: str = "2026-07-23T00:00:00+00:00",
) -> None:
    runs_dir = base / "state" / "runs.d"
    runs_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "goalflight.dispatch.v1",
        "dispatch_id": dispatch_id,
        "project_root": str(project_root.resolve()),
        "state": state,
        "started_at": started_at,
    }
    if task_ids:
        record["task_ids"] = task_ids
    if worker_pid is not None:
        import goalflight_ledger

        record["worker_pid"] = worker_pid
        record["worker_identity"] = goalflight_ledger.process_identity(worker_pid)
    if detached:
        record["detached"] = True
    if reason is not None:
        record["reason"] = reason
    (runs_dir / f"{dispatch_id}.json").write_text(json.dumps(record) + "\n", encoding="utf-8")


def init_git_project(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


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
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_messages.py"),
                "relay",
                "--all-projects",
                "--history",
            ],
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
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_messages.py"),
                "relay",
                "--all-projects",
                "--history",
            ],
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


def test_controller_post_reaches_worker_steer_read_path() -> None:
    import tempfile
    import goalflight_steer_mailbox as steer

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-live"
        write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())
        steer_path = steer.steer_file(dispatch_id, state_dir=base / "state")
        steer.append_steer_entry(steer_path, "earlier steer", dispatch_id=dispatch_id)

        posted = run_messages_cli(
            messages_dir,
            fleet_dir,
            [
                "post",
                "--dispatch-id",
                dispatch_id,
                "--type",
                "controller-notice",
                "--text",
                "worker-visible notice",
            ],
        )

        assert_true(f"controller post succeeds: {posted.stderr}", posted.returncode == 0)
        result = json.loads(posted.stdout)
        assert_true("record is reported", result["recorded"] is True)
        assert_true("worker delivery is reported", result["delivery"]["delivered"] is True)
        entries = steer.worker_entries(steer.read_steer_entries(steer_path))
        delivered = entries[-1]
        assert_true("worker read path sees posted text", delivered["text"] == "worker-visible notice")
        assert_true("steer sequence remains independent", delivered["seq"] == 2)
        envelope = delivered["context"]["message_envelope"]
        assert_true("typed envelope survives projection", envelope["type"] == "controller-notice")
        assert_true("message sequence remains canonical", envelope["seq"] == 1)


def test_worker_sideband_type_does_not_echo_to_worker_from_controller_context() -> None:
    import tempfile
    from goalflight_messages import read_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-sideband"
        write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())

        posted = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["post", "--dispatch-id", dispatch_id, "--type", "status", "--text", "worker progress"],
        )

        assert_true(f"sideband record succeeds: {posted.stderr}", posted.returncode == 0)
        result = json.loads(posted.stdout)
        assert_true("worker delivery is not requested for sideband type", result["delivery"]["requested"] is False)
        assert_true("sideband is still recorded", read_envelopes(messages_dir / f"{dispatch_id}.jsonl")[0]["type"] == "status")
        steer_path = base / "state" / "dispatch" / f"{dispatch_id}.steer.jsonl"
        assert_true("sideband does not echo into worker steer", not steer_path.exists())


def test_controller_channel_types_project_and_remain_in_aggregate() -> None:
    import tempfile
    import goalflight_steer_mailbox as steer
    from goalflight_messages import CONTROLLER_CHANNEL_TYPES, build_aggregate

    expected_types = {
        "controller-question",
        "controller-answer",
        "controller-notice",
        "controller-coordination",
        "coordination",
        "notice",
    }
    assert_true("controller channel type contract is complete", CONTROLLER_CHANNEL_TYPES == expected_types)

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-types"
        fleet_dir.mkdir()
        write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())

        for msg_type in sorted(expected_types):
            posted = run_messages_cli(
                messages_dir,
                fleet_dir,
                ["post", "--dispatch-id", dispatch_id, "--type", msg_type, "--text", msg_type],
            )
            assert_true(f"{msg_type} reaches worker: {posted.stderr}", posted.returncode == 0)

        entries = steer.worker_entries(
            steer.read_steer_entries(steer.steer_file(dispatch_id, state_dir=base / "state"))
        )
        projected_types = {
            entry["context"]["message_envelope"]["type"]
            for entry in entries
        }
        assert_true("every controller channel type projects", projected_types == expected_types)
        aggregate = build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        aggregate_types = {item["type"] for item in aggregate["open_controller_channel"]}
        assert_true("every projected type remains relay-visible", aggregate_types == expected_types)


def test_concurrent_controller_posts_preserve_worker_view_order() -> None:
    import tempfile
    import goalflight_messages as messages
    import goalflight_steer_mailbox as steer

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        dispatch_id = "d-controller-concurrent"
        first_delivery_started = threading.Event()
        release_first_delivery = threading.Event()
        second_delivery_started = threading.Event()
        errors: list[BaseException] = []
        original_record = messages._dispatch_record
        original_append = messages.goalflight_steer_mailbox.append_message_view

        def running_record(_dispatch_id: str) -> tuple[dict, None]:
            import goalflight_ledger

            return (
                {
                    "dispatch_id": dispatch_id,
                    "state": "running",
                    "worker_pid": os.getpid(),
                    "worker_identity": goalflight_ledger.process_identity(os.getpid()),
                },
                None,
            )

        def delayed_append(target_dispatch_id: str, envelope: dict):
            if envelope["seq"] == 1:
                first_delivery_started.set()
                if not release_first_delivery.wait(timeout=2):
                    raise AssertionError("timed out waiting to release first delivery")
            else:
                second_delivery_started.set()
            return original_append(target_dispatch_id, envelope, state_dir=base / "state")

        def post(text: str) -> None:
            try:
                messages.post_message(
                    dispatch_id=dispatch_id,
                    msg_type="controller-notice",
                    payload={"text": text},
                    messages_dir=messages_dir,
                    deliver_to_worker=True,
                )
            except BaseException as exc:  # noqa: BLE001 - relayed to the test thread
                errors.append(exc)

        messages._dispatch_record = running_record  # type: ignore[assignment]
        messages.goalflight_steer_mailbox.append_message_view = delayed_append  # type: ignore[assignment]
        first = threading.Thread(target=post, args=("first",))
        second = threading.Thread(target=post, args=("second",))
        try:
            first.start()
            assert_true("first delivery reaches projection", first_delivery_started.wait(timeout=1))
            second.start()
            # Broken code reaches the second projection while seq 1 is paused.
            # Correct code holds the message lock until seq 1's projection lands.
            second_delivery_started.wait(timeout=0.5)
            release_first_delivery.set()
            first.join(timeout=2)
            second.join(timeout=2)
        finally:
            release_first_delivery.set()
            first.join(timeout=2)
            second.join(timeout=2)
            messages._dispatch_record = original_record  # type: ignore[assignment]
            messages.goalflight_steer_mailbox.append_message_view = original_append  # type: ignore[assignment]

        assert_true("concurrent posts do not raise", not errors)
        assert_true("concurrent post threads finish", not first.is_alive() and not second.is_alive())
        entries = steer.worker_entries(
            steer.read_steer_entries(steer.steer_file(dispatch_id, state_dir=base / "state"))
        )
        envelope_seqs = [entry["context"]["message_envelope"]["seq"] for entry in entries]
        assert_true("worker view follows canonical message order", envelope_seqs == [1, 2])


def test_live_controller_post_delivery_failure_is_nonzero_and_recorded() -> None:
    import tempfile
    from goalflight_messages import read_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-undeliverable"
        write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())
        blocked_steer_path = base / "state" / "dispatch" / f"{dispatch_id}.steer.jsonl"
        blocked_steer_path.mkdir(parents=True)

        posted = run_messages_cli(
            messages_dir,
            fleet_dir,
            [
                "post",
                "--dispatch-id",
                dispatch_id,
                "--type",
                "controller-notice",
                "--text",
                "record even when delivery fails",
            ],
        )

        assert_true("undeliverable live post is nonzero", posted.returncode != 0)
        result = json.loads(posted.stdout)
        assert_true("failed delivery still reports record", result["recorded"] is True)
        assert_true("failed delivery is explicit", result["delivery"]["status"] == "worker_delivery_failed")
        assert_true("failed delivery is not claimed", result["delivery"]["delivered"] is False)
        assert_true("call site says record versus delivery", "recorded but worker delivery failed" in posted.stderr)
        envelopes = read_envelopes(messages_dir / f"{dispatch_id}.jsonl")
        assert_true(
            "record survives delivery failure",
            envelopes[0]["payload"]["text"] == "record even when delivery fails",
        )


def test_running_label_without_live_identity_is_not_delivery() -> None:
    import tempfile
    from goalflight_messages import read_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-no-worker"
        write_ledger_record(base, dispatch_id, base / "project", state="running")

        posted = run_messages_cli(
            messages_dir,
            fleet_dir,
            [
                "post",
                "--dispatch-id",
                dispatch_id,
                "--type",
                "controller-notice",
                "--text",
                "do not call a state label delivery",
            ],
        )

        assert_true("running label without worker is nonzero", posted.returncode != 0)
        result = json.loads(posted.stdout)
        assert_true("message remains recorded", result["recorded"] is True)
        assert_true("no worker delivery is claimed", result["delivery"]["delivered"] is False)
        assert_true("missing worker identity is explicit", result["delivery"]["dispatch_classification"] == "unknown_no_pid")
        assert_true("worker view is not written", result["delivery"]["worker_view_written"] is False)
        assert_true("record survives unavailable worker", len(read_envelopes(messages_dir / f"{dispatch_id}.jsonl")) == 1)


def test_detached_controller_dead_record_with_live_worker_still_delivers() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-detached-live-worker"
        write_ledger_record(
            base,
            dispatch_id,
            base / "project",
            state="controller_dead",
            worker_pid=os.getpid(),
            detached=True,
            reason="controller_dead",
        )

        posted = run_messages_cli(
            messages_dir,
            fleet_dir,
            [
                "post",
                "--dispatch-id",
                dispatch_id,
                "--type",
                "controller-notice",
                "--text",
                "detached worker still owns this mailbox",
            ],
        )

        assert_true(f"detached live worker post succeeds: {posted.stderr}", posted.returncode == 0)
        result = json.loads(posted.stdout)
        assert_true("detached worker is classified live", result["delivery"]["dispatch_classification"] == "expected_live")
        assert_true("detached live worker receives view", result["delivery"]["delivered"] is True)


def test_terminal_controller_post_is_recorded_and_labelled_record_only() -> None:
    import tempfile
    from goalflight_messages import read_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-terminal"
        write_ledger_record(base, dispatch_id, base / "project", state="complete")

        posted = run_messages_cli(
            messages_dir,
            fleet_dir,
            [
                "post",
                "--dispatch-id",
                dispatch_id,
                "--type",
                "controller-notice",
                "--text",
                "terminal history",
            ],
        )

        assert_true(f"terminal post is a normal case: {posted.stderr}", posted.returncode == 0)
        result = json.loads(posted.stdout)
        assert_true("terminal post is recorded", result["recorded"] is True)
        assert_true("terminal delivery is false", result["delivery"]["delivered"] is False)
        assert_true(
            "terminal result is labelled record-only",
            result["delivery"]["status"] == "terminal_recorded_only",
        )
        assert_true("terminal result says no reader", "no worker will read it" in result["delivery"]["detail"])
        envelopes = read_envelopes(messages_dir / f"{dispatch_id}.jsonl")
        assert_true("terminal message stays in record", envelopes[0]["payload"]["text"] == "terminal history")
        steer_path = base / "state" / "dispatch" / f"{dispatch_id}.steer.jsonl"
        assert_true("terminal post does not create worker view", not steer_path.exists())


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
        assert_true("advisory hint is body-free", "openai quota exhausted" not in summary["hint"])
        assert_true("advisory hint has relay command", "goalflight_messages.py relay --new" in summary["hint"])


def test_controller_summary_resolves_canonical_root_once() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        git_common_dir_calls: list[list[str]] = []
        original_run = messages.subprocess.run
        original_check_output = messages.subprocess.check_output

        def track_run(command, *args, **kwargs):
            if list(command) == ["git", "rev-parse", "--git-common-dir"]:
                git_common_dir_calls.append(list(command))
            return original_run(command, *args, **kwargs)

        def track_check_output(command, *args, **kwargs):
            if list(command) == ["git", "rev-parse", "--git-common-dir"]:
                git_common_dir_calls.append(list(command))
            return original_check_output(command, *args, **kwargs)

        messages.subprocess.run = track_run  # type: ignore[assignment]
        messages.subprocess.check_output = track_check_output  # type: ignore[assignment]
        try:
            messages.controller_mail_summary(
                owned_dispatch_ids=set(),
                task_store_project_root=project,
                messages_dir=base / "messages",
                fleet_dir=base / "fleet",
            )
        finally:
            messages.subprocess.run = original_run  # type: ignore[assignment]
            messages.subprocess.check_output = original_check_output  # type: ignore[assignment]

        assert_true("one canonical root resolution", len(git_common_dir_calls) == 1)


def test_controller_summary_git_failure_uses_task_store_root_fallback() -> None:
    import tempfile
    import goalflight_messages as messages
    import goalflight_task as tasks

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        managed_worktree = project / ".claude" / "worktrees" / "worker"
        messages_dir = base / "messages"
        dispatch_id = tasks._next_nudge_dispatch_id(project)
        messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="user_need",
            payload={"text": "controller decision"},
            messages_dir=messages_dir,
        )
        git_common_dir_calls = 0
        original_run = messages.subprocess.run

        def fail_git_common_dir(command, *args, **kwargs):
            nonlocal git_common_dir_calls
            if list(command) == ["git", "rev-parse", "--git-common-dir"]:
                git_common_dir_calls += 1
                return messages.subprocess.CompletedProcess(command, 1, "", "not a git repository")
            return original_run(command, *args, **kwargs)

        messages.subprocess.run = fail_git_common_dir  # type: ignore[assignment]
        try:
            summary = messages.controller_mail_summary(
                owned_dispatch_ids=set(),
                task_store_project_root=managed_worktree,
                messages_dir=messages_dir,
                fleet_dir=base / "fleet",
            )
        finally:
            messages.subprocess.run = original_run  # type: ignore[assignment]

        assert_true("one failed canonical root resolution", git_common_dir_calls == 1)
        assert_true("task store fallback inbox surfaced", summary["needs"][0]["dispatch_id"] == dispatch_id)


def test_mcp_stdio_tools_call() -> None:
    import json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        env = {
            **dict(os.environ),
            "GOALFLIGHT_MESSAGES_DIR": str(messages_dir),
            "GOALFLIGHT_STATE_DIR": str(Path(td) / "state"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(Path(td) / "pids"),
            "GOALFLIGHT_TASK_STORE_DIR": str(Path(td) / "task-store"),
        }
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


def test_mcp_delivery_failure_sets_tool_error_and_call_exit() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-mcp-undeliverable"
        write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())
        blocked_steer_path = base / "state" / "dispatch" / f"{dispatch_id}.steer.jsonl"
        blocked_steer_path.mkdir(parents=True)
        arguments = {
            "dispatch_id": dispatch_id,
            "type": "controller-notice",
            "payload": {"text": "recorded MCP failure"},
        }
        env = {
            **dict(os.environ),
            "GOALFLIGHT_MESSAGES_DIR": str(messages_dir),
            "GOALFLIGHT_FLEET_DIR": str(fleet_dir),
            "GOALFLIGHT_STATE_DIR": str(base / "state"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(base / "pids"),
            "GOALFLIGHT_TASK_STORE_DIR": str(base / "task-store"),
        }
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "goalflight_post_message", "arguments": arguments},
        }

        stdio = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "goalflight_mcp_messages.py"), "stdio"],
            input=json.dumps(request) + "\n",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert_true(f"MCP stdio server stays healthy: {stdio.stderr}", stdio.returncode == 0)
        response = json.loads(stdio.stdout.strip().splitlines()[-1])
        assert_true("MCP tool result is an error", response["result"]["isError"] is True)
        posted = json.loads(response["result"]["content"][0]["text"])
        assert_true("MCP error still reports record", posted["recorded"] is True)
        assert_true("MCP error does not claim delivery", posted["delivery"]["delivered"] is False)

        call = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_mcp_messages.py"),
                "call",
                "--arguments",
                json.dumps(arguments),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert_true("one-shot MCP call exits nonzero on delivery failure", call.returncode != 0)
        call_result = json.loads(call.stdout)
        assert_true("one-shot failure still reports record", call_result["recorded"] is True)


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

        relay = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["relay", "--new", "--ack", "--bodies", "--all-projects"],
        )
        assert_true("relay ack exits nonzero", relay.returncode == 1)
        assert_true(  # --bodies is machine-readable; default listing is headlines
        "relay still shows envelope",
        json.loads(relay.stdout.splitlines()[0])[0]["seq"] == 1,
    )
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


def test_seen_open_user_need_requires_history_relay() -> None:
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
        relay = run_messages_cli(messages_dir, fleet_dir, ["relay", "--all-projects"])
        assert_true("default relay hides read item", relay.returncode == 0)
        history = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["relay", "--all-projects", "--history"],
        )
        assert_true("history relay keeps open item", history.returncode == 2)
        assert_true(
            "open user_need remains in history",
            "USER-NEED relay: [d-open-seen] user_need: answer required" in history.stdout,
        )


def test_controller_relay_sanitizes_worker_text() -> None:
    from goalflight_messages import format_controller_relay

    rendered = format_controller_relay(
        {
            "open_user_needs": [
                {
                    "dispatch_id": "worker-legacy",
                    "type": "user_need",
                    "text": "needs\nFORGED\x1b[31m review",
                }
            ]
        }
    )

    assert_true(
        "legacy controller relay is one inert line",
        rendered
        == r"USER-NEED relay: [worker-legacy] user_need: needs FORGED\x1b[31m review",
    )
    assert_true("legacy controller relay has no raw CSI", "\x1b" not in (rendered or ""))


def test_bounded_relay_sanitizes_c1_csi() -> None:
    from goalflight_messages import format_bounded_relay

    rendered = format_bounded_relay(
        [
            {
                "dispatch_id": "worker-bounded",
                "type": "blocked",
                "text": "needs \x9b31mreview",
            }
        ]
    )

    assert_true(
        "bounded relay escapes C1 CSI",
        rendered == r"[worker-bounded] blocked: needs \x9b31mreview",
    )
    assert_true("bounded relay has no raw C1 CSI", "\x9b" not in (rendered or ""))


def test_default_read_and_relay_respect_independent_read_cursor() -> None:
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
        write_ledger_record(base, dispatch_id, ROOT)
        path = inbox_path(messages_dir, dispatch_id)
        envelope = markers_to_envelopes({"USER-NEED": ["byte stable"]}, dispatch_id=dispatch_id)[0]
        append_envelope(path, envelope)

        read = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id])
        assert_true("read default exit 0", read.returncode == 0)
        assert_true("read bytes stable", read.stdout == json.dumps([envelope]) + "\n")

        relay = run_messages_cli(messages_dir, fleet_dir, ["relay"])
        assert_true("relay default exit 2", relay.returncode == 2)
        assert_true("relay one-line item", relay.stdout == "[d-default] user_need: byte stable\n")

        status = run_messages_cli(messages_dir, fleet_dir, ["status"])
        assert_true("status default exit 0", status.returncode == 0)
        stable_before = stable_status(status.stdout)

        marked = run_messages_cli(messages_dir, fleet_dir, ["mark-read", "--dispatch-id", dispatch_id, "--through", "1"])
        assert_true("cursor op exit 0", marked.returncode == 0)

        read_after = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id])
        assert_true("read after cursor exit 0", read_after.returncode == 0)
        assert_true("read after cursor bytes stable", read_after.stdout == read.stdout)

        relay_after = run_messages_cli(messages_dir, fleet_dir, ["relay"])
        assert_true("relay after cursor exit 0", relay_after.returncode == 0)
        assert_true("relay after cursor hides item", relay_after.stdout == "no open unread items\n")

        history_after = run_messages_cli(messages_dir, fleet_dir, ["relay", "--history"])
        assert_true("history after cursor exit 2", history_after.returncode == 2)
        assert_true("history after cursor keeps item", dispatch_id in history_after.stdout)

        status_after = run_messages_cli(messages_dir, fleet_dir, ["status"])
        assert_true("status after cursor exit 0", status_after.returncode == 0)
        assert_true("status stable fields unchanged", stable_status(status_after.stdout) == stable_before)


def test_relay_new_ack_reports_post_ack_unseen_counts() -> None:
    import tempfile
    from goalflight_messages import append_envelope, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-relay-ack"
        append_envelope(
            inbox_path(messages_dir, dispatch_id),
            markers_to_envelopes({"STATUS": ["ready"]}, dispatch_id=dispatch_id)[0],
        )

        relayed = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["relay", "--new", "--all-projects", "--ack"],
        )

        assert_true("relay ack exit 0", relayed.returncode == 0)
        assert_true(
            "relay ack count reflects advanced cursor",
            relayed.stdout.splitlines()[-1] == f"unseen counts: {dispatch_id}=0",
        )


def test_default_relay_is_bounded_newest_first_with_elision() -> None:
    import tempfile
    from goalflight_messages import append_envelope, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(project)
        fleet_dir.mkdir()
        for index in range(80):
            dispatch_id = f"d-large-{index:03d}"
            stamp = f"2026-07-23T00:{index // 60:02d}:{index % 60:02d}+00:00"
            write_ledger_record(
                base,
                dispatch_id,
                project,
                started_at=stamp,
            )
            envelope = markers_to_envelopes(
                {"USER-NEED": [f"fixture {index} " + ("x" * 180)]},
                dispatch_id=dispatch_id,
                ts=stamp,
            )[0]
            append_envelope(inbox_path(messages_dir, dispatch_id), envelope)

        relay = run_messages_cli(messages_dir, fleet_dir, ["relay"], cwd=project)
        assert_true("bounded relay exits with mail", relay.returncode == 2)
        assert_true("hard byte cap", len(relay.stdout.encode("utf-8")) <= 4096)
        lines = relay.stdout.splitlines()
        assert_true("item cap plus elision", len(lines) <= 21)
        assert_true("newest item first", lines[0].startswith("[d-large-079]"))
        assert_true("elision line present", lines[-1].startswith("(+") and lines[-1].endswith("elided)"))


def test_default_relay_excludes_cross_project_mail() -> None:
    import tempfile
    from goalflight_messages import append_envelope, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "mine"
        other = base / "other"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(project)
        init_git_project(other)
        fleet_dir.mkdir()
        write_ledger_record(base, "mine-dispatch", project)
        write_ledger_record(base, "other-dispatch", other)
        for dispatch_id in ("mine-dispatch", "other-dispatch"):
            append_envelope(
                inbox_path(messages_dir, dispatch_id),
                markers_to_envelopes(
                    {"USER-NEED": [f"{dispatch_id} need"]},
                    dispatch_id=dispatch_id,
                )[0],
            )

        scoped = run_messages_cli(messages_dir, fleet_dir, ["relay"], cwd=project)
        assert_true("scoped relay has own mail", "mine-dispatch" in scoped.stdout)
        assert_true("scoped relay excludes other mail", "other-dispatch" not in scoped.stdout)
        all_projects = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["relay", "--all-projects"],
            cwd=project,
        )
        assert_true("explicit all-projects includes own mail", "mine-dispatch" in all_projects.stdout)
        assert_true("explicit all-projects includes other mail", "other-dispatch" in all_projects.stdout)


def test_ack_stale_expires_exact_stale_set_and_preserves_read_cursor() -> None:
    import tempfile
    import goalflight_task as T
    from goalflight_messages import (
        ACK_CURSOR_FILE,
        READ_CURSOR_FILE,
        append_envelope,
        inbox_path,
        markers_to_envelopes,
    )

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(project)
        fleet_dir.mkdir()

        def task(task_id: str, *, done: bool = False) -> dict:
            return {
                "schema_version": 1,
                "id": task_id,
                "kind": "task",
                "title": task_id,
                "blocked_by": [],
                "links": [],
                "done": done,
                "created_at": "2026-07-23T00:00:00+00:00",
                "created_by": "test",
            }

        prior_task_store_dir = os.environ.get("GOALFLIGHT_TASK_STORE_DIR")
        os.environ["GOALFLIGHT_TASK_STORE_DIR"] = str(base / "task-store")
        try:
            task_store = T.TaskStore(project)
            assert_true(
                "task store is isolated from operator state",
                task_store.store_dir.is_relative_to((base / "task-store").resolve()),
            )
            task_store.save_items_atomic(
                [
                    task("t-closed", done=True),
                    task("t-superseded"),
                    task("t-live", done=True),
                    task("t-current"),
                ]
            )
        finally:
            if prior_task_store_dir is None:
                os.environ.pop("GOALFLIGHT_TASK_STORE_DIR", None)
            else:
                os.environ["GOALFLIGHT_TASK_STORE_DIR"] = prior_task_store_dir
        now = dt.datetime.now(dt.timezone.utc)
        taskless_old_at = (now - dt.timedelta(hours=25)).isoformat()
        taskless_recent_at = (now - dt.timedelta(hours=1)).isoformat()
        write_ledger_record(
            base,
            "d-closed",
            project,
            state="complete",
            task_ids=["t-closed"],
            started_at="2026-07-23T00:00:00+00:00",
        )
        write_ledger_record(
            base,
            "d-old",
            project,
            state="failed",
            task_ids=["t-superseded"],
            started_at="2026-07-23T00:01:00+00:00",
        )
        write_ledger_record(
            base,
            "d-new",
            project,
            state="running",
            task_ids=["t-superseded"],
            started_at="2026-07-23T00:02:00+00:00",
        )
        write_ledger_record(
            base,
            "d-live",
            project,
            state="running",
            task_ids=["t-live"],
            started_at="2026-07-23T00:03:00+00:00",
        )
        write_ledger_record(
            base,
            "d-current",
            project,
            state="failed",
            task_ids=["t-current"],
            started_at="2026-07-23T00:04:00+00:00",
        )
        write_ledger_record(
            base,
            "d-taskless-old",
            project,
            state="failed",
            started_at=taskless_old_at,
        )
        write_ledger_record(
            base,
            "d-taskless-recent",
            project,
            state="failed",
            started_at=taskless_recent_at,
        )
        write_ledger_record(
            base,
            "d-taskless-live",
            project,
            state="running",
            started_at=taskless_old_at,
        )
        for dispatch_id in (
            "d-closed",
            "d-old",
            "d-live",
            "d-current",
            "d-taskless-old",
            "d-taskless-recent",
            "d-taskless-live",
        ):
            append_envelope(
                inbox_path(messages_dir, dispatch_id),
                markers_to_envelopes(
                    {"BLOCKED": [f"{dispatch_id} escalation"]},
                    dispatch_id=dispatch_id,
                )[0],
            )

        stale = run_messages_cli(messages_dir, fleet_dir, ["ack", "--stale"], cwd=project)
        assert_true("ack stale succeeds", stale.returncode == 0)
        assert_true("exact stale dispatch count", "3 dispatch(es), 3 open item(s)" in stale.stdout)
        relay = run_messages_cli(messages_dir, fleet_dir, ["relay"], cwd=project)
        assert_true("live escalation survives", "d-live" in relay.stdout)
        assert_true("current terminal escalation survives", "d-current" in relay.stdout)
        assert_true("recent task-less terminal escalation survives", "d-taskless-recent" in relay.stdout)
        assert_true("task-less live escalation survives", "d-taskless-live" in relay.stdout)
        assert_true("closed task escalation expired", "d-closed" not in relay.stdout)
        assert_true("superseded escalation expired", "d-old" not in relay.stdout)
        assert_true("old task-less terminal escalation expired", "d-taskless-old" not in relay.stdout)
        ack_cursor = json.loads((messages_dir / ACK_CURSOR_FILE).read_text())
        assert_true(
            "ack cursor exact stale keys",
            set(ack_cursor) == {"d-closed", "d-old", "d-taskless-old"},
        )
        assert_true("read cursor remains untouched", not (messages_dir / READ_CURSOR_FILE).exists())

        explicit = run_messages_cli(messages_dir, fleet_dir, ["ack", "d-live"], cwd=project)
        assert_true("explicit ack succeeds", explicit.returncode == 0)
        relay_after = run_messages_cli(messages_dir, fleet_dir, ["relay"], cwd=project)
        assert_true("explicit ack removes live escalation", "d-live" not in relay_after.stdout)
        assert_true("unacked current escalation remains", "d-current" in relay_after.stdout)
        assert_true("explicit ack still leaves read cursor untouched", not (messages_dir / READ_CURSOR_FILE).exists())


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
        test_controller_post_reaches_worker_steer_read_path,
        test_worker_sideband_type_does_not_echo_to_worker_from_controller_context,
        test_controller_channel_types_project_and_remain_in_aggregate,
        test_concurrent_controller_posts_preserve_worker_view_order,
        test_live_controller_post_delivery_failure_is_nonzero_and_recorded,
        test_running_label_without_live_identity_is_not_delivery,
        test_detached_controller_dead_record_with_live_worker_still_delivers,
        test_terminal_controller_post_is_recorded_and_labelled_record_only,
        test_controller_summary_includes_quota_advisory,
        test_controller_summary_resolves_canonical_root_once,
        test_controller_summary_git_failure_uses_task_store_root_fallback,
        test_mcp_stdio_tools_call,
        test_mcp_delivery_failure_sets_tool_error_and_call_exit,
        test_mark_read_creates_cursor_and_unseen_filters,
        test_mark_read_all_advances_every_inbox_to_current_max,
        test_mark_read_through_never_rewinds,
        test_concurrent_mark_read_through_merges_per_inbox_max,
        test_read_unseen_ack_advances_to_shown,
        test_ack_cursor_write_failure_warns_without_traceback,
        test_mark_read_cursor_write_failure_warns_without_traceback,
        test_corrupt_or_absent_cursor_means_all_unseen,
        test_seen_open_user_need_requires_history_relay,
        test_controller_relay_sanitizes_worker_text,
        test_bounded_relay_sanitizes_c1_csi,
        test_default_read_and_relay_respect_independent_read_cursor,
        test_relay_new_ack_reports_post_ack_unseen_counts,
        test_default_relay_is_bounded_newest_first_with_elision,
        test_default_relay_excludes_cross_project_mail,
        test_ack_stale_expires_exact_stale_set_and_preserves_read_cursor,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
