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
import goalflight_messages as _carrier_messages
from goalflight_messages import MARKER_TO_TYPE, markers_to_envelopes


def _carrier_add(path: Path, envelope: dict) -> None:
    _carrier_messages.update_envelopes(
        path, lambda existing: (existing + [envelope], None)
    )


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


def start_messages_listener(
    messages_dir: Path,
    fleet_dir: Path,
    project_root: Path,
    *,
    session_id: str | None = None,
) -> subprocess.Popen[str]:
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
    command = [
        sys.executable,
        str(ROOT / "scripts" / "goalflight_messages.py"),
        "--messages-dir",
        str(messages_dir),
        "--fleet-dir",
        str(fleet_dir),
        "listen",
        "--project-root",
        str(project_root),
        "--poll-secs",
        "0.05",
        "--timeout-s",
        "5",
        "--json",
    ]
    if session_id is not None:
        command.extend(["--session-id", session_id])
    return subprocess.Popen(
        command,
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def listener_result(process: subprocess.Popen[str]) -> tuple[int, str, str]:
    try:
        stdout, stderr = process.communicate(timeout=7)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
    return int(process.returncode or 0), stdout, stderr


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
    controller_session_id: str | None = None,
    controller_pid: int | None = None,
    controller_label: str | None = None,
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
    if controller_session_id is not None:
        record["controller_session_id"] = controller_session_id
        record["controller_pid"] = controller_pid if controller_pid is not None else os.getpid()
        if controller_label is not None:
            record["controller_label"] = controller_label
    (runs_dir / f"{dispatch_id}.json").write_text(json.dumps(record) + "\n", encoding="utf-8")


def init_git_project(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def mirror_remote_message(
    *,
    remote_messages_dir: Path,
    fleet_dir: Path,
    messages_dir: Path,
    dispatch_id: str,
    msg_type: str,
    payload: dict,
    seq: int | None = None,
) -> Path:
    import goalflight_messages as messages

    posted = messages.post_message(
        dispatch_id=dispatch_id,
        msg_type=msg_type,
        payload=payload,
        messages_dir=remote_messages_dir,
        source={"node": "remote", "adapter": "codex", "transport": "acp"},
        seq=seq,
    )
    merged = messages.merge_remote_register(
        fleet_dir,
        Path(posted["path"]),
        messages_dir=messages_dir,
    )
    return Path(merged["merged_into"])


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
    from goalflight_messages import inbox_path, read_envelopes

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        path = inbox_path(messages_dir, "d-inbox")
        env1 = markers_to_envelopes({"STATUS": ["a"]}, dispatch_id="d-inbox")[0]
        env2 = markers_to_envelopes({"USER-NEED": ["help"]}, dispatch_id="d-inbox", seq_start=2)[0]
        _carrier_add(path, env1)
        _carrier_add(path, env2)
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
    from goalflight_messages import build_aggregate, inbox_path, refresh_aggregate

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        (fleet_dir / "register").mkdir()
        path = inbox_path(messages_dir, "d-agg")
        _carrier_add(
            path,
            markers_to_envelopes({"USER-NEED": ["pick account"]}, dispatch_id="d-agg")[0],
        )
        aggregate = build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("active dispatch", "d-agg" in aggregate["active_dispatches"])
        assert_true("open need", len(aggregate["open_user_needs"]) == 1)
        written = refresh_aggregate(fleet_dir, messages_dir=messages_dir)
        assert_true("written aggregate", (fleet_dir / "register" / "aggregate.json").exists())
        assert_true("same open need", len(written["open_user_needs"]) == 1)


def test_dual_source_inboxes_aggregate_without_fleet_overwrite() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-dual-local-need"
        local_path = Path(
            messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_need",
                payload={"text": "choose the account"},
                messages_dir=messages_dir,
            )["path"]
        )
        fleet_path = mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=dispatch_id,
            msg_type="status",
            payload={"text": "remote worker is waiting"},
        )

        paths = messages.collect_inbox_paths(messages_dir, fleet_dir)
        assert_true("local then fleet streams retained", paths == [local_path, fleet_path])
        aggregate = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("local user need survives fleet merge", len(aggregate["open_user_needs"]) == 1)
        assert_true(
            "private cursor identity stays out of aggregate contract",
            "_goalflight_inbox_cursor_key" not in aggregate["open_user_needs"][0],
        )
        assert_true(
            "local user need text survives fleet merge",
            aggregate["open_user_needs"][0]["text"] == "choose the account",
        )


def test_single_source_inboxes_and_deterministic_order_reject_local_overwrite() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        fleet_only_id = "a-fleet-only"
        dual_id = "m-dual-fleet-need"
        local_only_id = "z-local-only"

        local_only_path = Path(
            messages.post_message(
                dispatch_id=local_only_id,
                msg_type="user_need",
                payload={"text": "local only"},
                messages_dir=messages_dir,
            )["path"]
        )
        fleet_only_path = mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=fleet_only_id,
            msg_type="user_need",
            payload={"text": "fleet only"},
        )
        dual_local_path = Path(
            messages.post_message(
                dispatch_id=dual_id,
                msg_type="status",
                payload={"text": "local controller status"},
                messages_dir=messages_dir,
            )["path"]
        )
        dual_fleet_path = mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=dual_id,
            msg_type="user_need",
            payload={"text": "fleet escalation"},
        )

        expected = [fleet_only_path, dual_local_path, dual_fleet_path, local_only_path]
        first = messages.collect_inbox_paths(messages_dir, fleet_dir)
        second = messages.collect_inbox_paths(messages_dir, fleet_dir)
        assert_true("local-only and fleet-only streams retained", first == expected)
        assert_true("collection order stable across runs", second == first)
        aggregate = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        needs = {
            (item["dispatch_id"], item["text"])
            for item in aggregate["open_user_needs"]
        }
        assert_true(
            "single-source directions and dual fleet need all surface",
            needs
            == {
                (local_only_id, "local only"),
                (fleet_only_id, "fleet only"),
                (dual_id, "fleet escalation"),
            },
        )


def test_same_id_different_envelopes_both_survive_and_later_need_reopens() -> None:
    import tempfile
    from unittest.mock import patch
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-shared-envelope-id"

        shared_id = messages.uuid.UUID("00000000-0000-0000-0000-000000000122")
        with patch.object(messages.uuid, "uuid4", return_value=shared_id):
            messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="result",
                payload={"complete": True, "text": "local terminal result"},
                messages_dir=messages_dir,
            )
            remote = messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_need",
                payload={"text": "later remote blocker"},
                messages_dir=remote_messages_dir,
                source={"node": "remote", "adapter": "codex", "transport": "acp"},
            )
        messages.merge_remote_register(
            fleet_dir,
            Path(remote["path"]),
            messages_dir=messages_dir,
        )

        logical = messages.logical_envelopes_for_paths(
            messages.collect_inbox_paths(messages_dir, fleet_dir, dispatch_ids={dispatch_id}),
            messages_dir=messages_dir,
        )
        assert_true("same UUID with different content remains two events", len(logical) == 2)
        assert_true("both logical types survive", [env["type"] for env in logical] == ["result", "user_need"])
        aggregate = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true(
            "later cross-carrier need survives earlier terminal result",
            [item["text"] for item in aggregate["open_user_needs"]] == ["later remote blocker"],
        )


def test_read_returns_fleet_only_message_body() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-read-fleet-only"
        mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=dispatch_id,
            msg_type="user_need",
            payload={"text": "fleet-only body"},
        )

        read = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id])
        assert_true("fleet-only read succeeds", read.returncode == 0)
        envelopes = json.loads(read.stdout)
        assert_true("fleet-only read returns one envelope", len(envelopes) == 1)
        assert_true("fleet-only read returns body", envelopes[0]["payload"]["text"] == "fleet-only body")


def test_last_steering_uses_controller_ingestion_order_across_clock_skew() -> None:
    import tempfile
    from unittest.mock import patch
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = messages.STEERING_DISPATCH_ID

        with patch.object(messages, "utc_now", return_value="2026-08-08T10:00:10+00:00"):
            local_path = Path(messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="steering",
                payload={"text": "first local steer"},
                messages_dir=messages_dir,
                seq=1,
            )["path"])
        with patch.object(messages, "utc_now", return_value="2026-08-08T10:00:00+00:00"):
            remote_path = Path(messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="steering",
                payload={"text": "second remote steer"},
                messages_dir=remote_messages_dir,
                source={"node": "remote", "adapter": "codex", "transport": "acp"},
                seq=1,
            )["path"])
        merged = messages.merge_remote_register(
            fleet_dir,
            remote_path,
            messages_dir=messages_dir,
        )
        fleet_path = Path(merged["merged_into"])
        assert_true("steering local input path", local_path == messages.inbox_path(messages_dir, dispatch_id))
        assert_true("steering fleet input path", fleet_path.exists())

        aggregate = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("second ingestion wins despite backwards source clock", aggregate["last_steering"]["payload"]["text"] == "second remote steer")
        assert_true("source timestamp remains display value", aggregate["last_steering"]["ts"] == "2026-08-08T10:00:00+00:00")


def test_remerged_identical_steering_reuses_persisted_ingestion_order() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = messages.STEERING_DISPATCH_ID

        remote_path = Path(messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="steering",
            payload={"text": "old remote steer"},
            messages_dir=remote_messages_dir,
            source={"node": "remote", "adapter": "codex", "transport": "acp"},
            seq=1,
        )["path"])
        first_merge = messages.merge_remote_register(
            fleet_dir,
            remote_path,
            messages_dir=messages_dir,
        )
        fleet_path = Path(first_merge["merged_into"])
        first_order = messages.read_envelopes(fleet_path)[0][messages._INGESTION_ORDER_FIELD]
        local_path = Path(messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="steering",
            payload={"text": "newer local steer"},
            messages_dir=messages_dir,
            seq=1,
        )["path"])
        assert_true("remerge remote input path measured", remote_path == messages.inbox_path(remote_messages_dir, dispatch_id))
        assert_true("remerge local input path measured", local_path == messages.inbox_path(messages_dir, dispatch_id))
        before_rotation = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("genuinely newer local steer wins before rotation", before_rotation["last_steering"]["payload"]["text"] == "newer local steer")

        rotated_path = fleet_path.with_name(f"{fleet_path.stem}.rotated.jsonl")
        fleet_path.rename(rotated_path)
        second_merge = messages.merge_remote_register(
            fleet_dir,
            remote_path,
            messages_dir=messages_dir,
        )
        remerged_path = Path(second_merge["merged_into"])
        second_order = messages.read_envelopes(remerged_path)[0][messages._INGESTION_ORDER_FIELD]
        after_remerge = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("remerged carrier input path measured", remerged_path == fleet_path)
        assert_true("identical envelope reuses first ingestion order", second_order == first_order)
        assert_true("remerge cannot displace newer local steer", after_remerge["last_steering"]["payload"]["text"] == "newer local steer")
        identity_store = messages_dir / ".ingestion-identities.json"
        assert_true("canonical ingestion identity store is outside carrier", identity_store.is_file() and identity_store.parent == messages_dir)
        identity_document = json.loads(identity_store.read_text(encoding="utf-8"))
        remote_identity = messages._canonical_envelope_identity(messages.read_envelopes(remote_path)[0])
        assert_true("identity store keys the remote event to its first order", identity_document["orders"][remote_identity] == first_order)


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
        original_admit_stream_seq = messages._admit_stream_seq
        guard = threading.Lock()
        active = 0
        max_active = 0

        def slow_admit_stream_seq(*, provided_seq: int | None, envelopes: list[dict]) -> int:
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return original_admit_stream_seq(
                    provided_seq=provided_seq,
                    envelopes=envelopes,
                )
            finally:
                with guard:
                    active -= 1

        messages._admit_stream_seq = slow_admit_stream_seq  # type: ignore[assignment]
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
            messages._admit_stream_seq = original_admit_stream_seq  # type: ignore[assignment]

        loaded = read_envelopes(path)
        assert_true("serialized sequence admission critical section", max_active == 1)
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


def _claim_test_controller(
    base: Path,
    project: Path,
    *,
    label: str = "mine-controller",
    session_id: str = "mine-session",
) -> dict[str, str | None]:
    import goalflight_session_status as sessions

    sessions.claim_session(
        project,
        pid=os.getpid(),
        session_id=session_id,
        label=label,
    )
    updates = {
        "GOALFLIGHT_STATE_DIR": str(base / "state"),
        "GOALFLIGHT_MESSAGES_DIR": str(base / "messages"),
        "GOALFLIGHT_FLEET_DIR": str(base / "fleet"),
        "GOALFLIGHT_CONTROLLER_PID": str(os.getpid()),
        "GOALFLIGHT_CONTROLLER_LABEL": label,
    }
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    return previous


def _restore_test_controller(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


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


def main() -> None:
    tests = sorted(
        [
            value
            for name, value in globals().items()
            if name.startswith("test_") and callable(value)
        ],
        key=lambda test: test.__name__,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
