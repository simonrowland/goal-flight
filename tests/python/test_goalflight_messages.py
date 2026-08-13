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


def test_cursor_version_controls_structural_key_parsing_and_is_idempotent() -> None:
    """A stream name that looks like JSON stays raw text in a cursor key.

    D3 now refuses to CREATE such a stream, so this covers the case D3 cannot
    reach: a stream file already on disk, written by an older build or planted
    by something that is not this API. The cursor still has to key it without
    re-interpreting the name as structure -- otherwise one dispatch's read
    position silently becomes another's.
    """
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        messages_dir.mkdir(parents=True)
        structural_id = '["local","victim"]'

        # The new contract: this name cannot be posted at all.
        refused = None
        try:
            messages.post_message(
                dispatch_id=structural_id,
                msg_type="blocked",
                payload={"text": "unreachable"},
                messages_dir=messages_dir,
                seq=1,
            )
        except Exception as exc:  # MessageError
            refused = exc
        assert_true("D3 refuses a structural-looking stream name", refused is not None)

        # The case D3 cannot reach: the file is already there.
        planted = messages_dir / f"{structural_id}.jsonl"
        planted.write_text("", encoding="utf-8")
        structural_key = messages.inbox_stream_key(planted, messages_dir=messages_dir)
        assert_true(
            "structural-looking name is nested as a string, not as structure",
            json.loads(structural_key) == ["local", structural_id],
        )

        legacy_path = messages.read_cursor_path(messages_dir)
        legacy_path.write_text(
            json.dumps({structural_id: 1}) + "\n", encoding="utf-8"
        )
        first_legacy = messages.load_read_cursor(
            legacy_path,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true(
            "unversioned structural-looking key remains raw text",
            first_legacy == {structural_key: 1},
        )

        legacy_bytes = legacy_path.read_bytes()
        second_legacy = messages.load_read_cursor(
            legacy_path,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true(
            "migrated legacy cursor is idempotent",
            second_legacy == first_legacy
            and legacy_path.read_bytes() == legacy_bytes,
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
        assert_true(
            "both independent sequence maxima retained",
            messages.max_seq_by_inbox(messages_dir=messages_dir, fleet_dir=fleet_dir)
            == {
                messages.inbox_stream_key(local_path, messages_dir=messages_dir): 1,
                messages.inbox_stream_key(fleet_path, messages_dir=messages_dir): 1,
            },
        )
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


def test_dual_source_sequences_keep_independent_cursor_and_wake_identity() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-independent-seq"
        init_git_project(project)

        fleet_path = mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=dispatch_id,
            msg_type="status",
            payload={"text": "remote seven"},
            seq=7,
        )
        fleet_key = messages.inbox_stream_key(fleet_path, messages_dir=messages_dir)
        first_max = messages.max_seq_by_inbox(
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            dispatch_ids={dispatch_id},
        )
        assert_true("fleet-only maximum has its own cursor", first_max == {fleet_key: 7})
        cursor_path = messages.read_cursor_path(messages_dir)
        messages.advance_read_cursor(cursor_path, first_max)

        local_path = Path(messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="status",
            payload={"text": "local three"},
            messages_dir=messages_dir,
            seq=3,
        )["path"])
        local_key = messages.inbox_stream_key(local_path, messages_dir=messages_dir)
        both_max = messages.max_seq_by_inbox(
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            dispatch_ids={dispatch_id},
        )
        assert_true(
            "second stream adds rather than replaces a maximum",
            both_max == {local_key: 3, fleet_key: 7},
        )
        messages.advance_read_cursor(cursor_path, both_max)
        assert_true(
            "second stream does not rewind existing cursor",
            messages.load_read_cursor(cursor_path) == both_max,
        )

        messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="blocked",
            payload={"text": "local four"},
            messages_dir=messages_dir,
        )
        mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=dispatch_id,
            msg_type="blocked",
            payload={"text": "remote eight"},
        )
        shown, counts, advances = messages.unseen_envelopes_for_paths(
            messages.collect_inbox_paths(messages_dir, fleet_dir, dispatch_ids={dispatch_id}),
            messages_dir=messages_dir,
            cursor=messages.load_read_cursor(cursor_path),
        )
        assert_true("one unread message from each stream", [item["seq"] for item in shown] == [4, 8])
        assert_true("unread count aggregates both streams", counts == {dispatch_id: 2})
        assert_true(
            "unread advances remain stream-specific",
            advances == {local_key: 4, fleet_key: 8},
        )
        summary = messages.controller_mail_summary(
            owned_dispatch_ids={dispatch_id},
            task_store_project_root=project,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true("controller unread summary sees both streams", summary["count"] == 2)
        messages.advance_read_cursor(cursor_path, advances)
        assert_true(
            "stream-specific cursor clears both unread messages",
            messages.controller_mail_summary(
                owned_dispatch_ids={dispatch_id},
                task_store_project_root=project,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
            == {},
        )

        wake_id = "d-wake-collision"
        wake_local_path = Path(messages.post_message(
            dispatch_id=wake_id,
            msg_type="blocked",
            payload={"text": "local blocked"},
            messages_dir=messages_dir,
        )["path"])
        wake_fleet_path = mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=wake_id,
            msg_type="blocked",
            payload={"text": "remote blocked"},
        )
        wakes = messages.controller_wake_watermark(
            project_root=project,
            owned_dispatch_ids={wake_id},
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true(
            "equal seq values in different streams have distinct wake identities",
            set(wakes)
            == {
                (messages.inbox_stream_key(wake_local_path, messages_dir=messages_dir), 1),
                (messages.inbox_stream_key(wake_fleet_path, messages_dir=messages_dir), 1),
            },
        )


def test_unseen_last_n_ack_only_advances_shown_event_and_keeps_empty_zero() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        paths = [
            Path(messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": dispatch_id},
                messages_dir=messages_dir,
            )["path"])
            for dispatch_id in ("a-first", "b-shown")
        ]
        empty_path = messages.inbox_path(messages_dir, "c-empty")
        empty_path.touch()
        shown, counts, advances = messages.unseen_envelopes_for_paths(
            [*paths, empty_path],
            messages_dir=messages_dir,
            cursor={},
            last_n=1,
        )
        shown_key = messages.inbox_stream_key(paths[1], messages_dir=messages_dir)
        assert_true("last-n returns only the displayed event", [item["dispatch_id"] for item in shown] == ["b-shown"])
        assert_true("ack advances only the displayed event", advances == {shown_key: 1})
        assert_true("counts describe all unread and retain empty zero", counts == {"a-first": 1, "b-shown": 1, "c-empty": 0})


def test_mark_read_through_clamps_each_stream_and_preserves_later_fleet_mail() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-cursor-skip"
        init_git_project(project)

        local_path = Path(messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="status",
            payload={"text": "local nine"},
            messages_dir=messages_dir,
            seq=9,
        )["path"])
        remote_path = Path(messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="status",
            payload={"text": "fleet one"},
            messages_dir=remote_messages_dir,
            seq=1,
        )["path"])
        merged = messages.merge_remote_register(
            fleet_dir,
            remote_path,
            messages_dir=messages_dir,
        )
        fleet_path = Path(merged["merged_into"])
        assert_true("measured local input path", local_path == messages.inbox_path(messages_dir, dispatch_id))
        assert_true("measured remote input path", remote_path == messages.inbox_path(remote_messages_dir, dispatch_id))
        assert_true("measured fleet input path", fleet_path.exists())

        local_key = messages.inbox_stream_key(local_path, messages_dir=messages_dir)
        fleet_key = messages.inbox_stream_key(fleet_path, messages_dir=messages_dir)
        marked = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["mark-read", "--dispatch-id", dispatch_id, "--through", "9"],
        )
        assert_true("mark-read succeeds", marked.returncode == 0)
        cursor = messages.load_read_cursor(messages.read_cursor_path(messages_dir))
        assert_true("each stream clamps to its measured maximum", cursor == {local_key: 9, fleet_key: 1})

        messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="blocked",
            payload={"text": "fleet two must surface"},
            messages_dir=remote_messages_dir,
        )
        messages.merge_remote_register(fleet_dir, remote_path, messages_dir=messages_dir)
        shown, counts, advances = messages.unseen_envelopes_for_paths(
            messages.collect_inbox_paths(messages_dir, fleet_dir, dispatch_ids={dispatch_id}),
            messages_dir=messages_dir,
            cursor=messages.load_read_cursor(messages.read_cursor_path(messages_dir)),
        )
        assert_true("later lagging-stream blocker remains unread", [(item["seq"], item["payload"]["text"]) for item in shown] == [(2, "fleet two must surface")])
        assert_true("later blocker unread count", counts == {dispatch_id: 1})
        assert_true("later blocker advances only fleet stream", advances == {fleet_key: 2})
        summary = messages.controller_mail_summary(
            owned_dispatch_ids={dispatch_id},
            task_store_project_root=project,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true("later blocker reaches controller summary", summary["count"] == 1)


def test_structural_stream_keys_prevent_dispatch_prefix_collision() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        init_git_project(project)

        local_dispatch_id = "fleet:x"
        fleet_dispatch_id = "x"
        local_path = Path(messages.post_message(
            dispatch_id=local_dispatch_id,
            msg_type="blocked",
            payload={"text": "local prefix-shaped id"},
            messages_dir=messages_dir,
        )["path"])
        fleet_path = mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=fleet_dispatch_id,
            msg_type="blocked",
            payload={"text": "real fleet stream"},
        )
        assert_true("prefix collision local input path", local_path.name == "fleet:x.jsonl")
        assert_true("prefix collision fleet input path", fleet_path.name == "x.jsonl")
        local_key = messages.inbox_stream_key(local_path, messages_dir=messages_dir)
        fleet_key = messages.inbox_stream_key(fleet_path, messages_dir=messages_dir)
        assert_true("source and stem form distinct structural identities", local_key != fleet_key)

        wakes = messages.controller_wake_watermark(
            project_root=project,
            owned_dispatch_ids={local_dispatch_id, fleet_dispatch_id},
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true("both collision-shaped events wake", set(wakes) == {(local_key, 1), (fleet_key, 1)})


def test_merged_envelope_dedupes_event_but_acknowledges_both_streams() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-merged-envelope"
        init_git_project(project)

        local_path = Path(messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="blocked",
            payload={"text": "one logical blocker"},
            messages_dir=messages_dir,
        )["path"])
        merged = messages.merge_remote_register(
            fleet_dir,
            local_path,
            messages_dir=messages_dir,
        )
        fleet_path = Path(merged["merged_into"])
        assert_true("dedupe local input path", local_path == messages.inbox_path(messages_dir, dispatch_id))
        assert_true("dedupe fleet input path", fleet_path.exists())
        local_key = messages.inbox_stream_key(local_path, messages_dir=messages_dir)
        fleet_key = messages.inbox_stream_key(fleet_path, messages_dir=messages_dir)

        aggregate = messages.build_aggregate(
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            include_cursor_keys=True,
        )
        assert_true("aggregate counts copied envelope once", len(aggregate["open_user_needs"]) == 1)
        shown, counts, advances = messages.unseen_envelopes_for_paths(
            messages.collect_inbox_paths(messages_dir, fleet_dir, dispatch_ids={dispatch_id}),
            messages_dir=messages_dir,
            cursor={},
        )
        assert_true("unread view counts copied envelope once", len(shown) == 1 and counts == {dispatch_id: 1})
        assert_true("one logical read advances both physical cursors", advances == {local_key: 1, fleet_key: 1})
        wakes = messages.controller_wake_watermark(
            project_root=project,
            owned_dispatch_ids={dispatch_id},
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true("wake watermark counts copied envelope once", len(wakes) == 1)

        messages.advance_read_cursor(messages.read_cursor_path(messages_dir), advances)
        cursor = messages.load_read_cursor(messages.read_cursor_path(messages_dir))
        assert_true("read acknowledgement persists both cursor domains", cursor == advances)
        assert_true(
            "acknowledged logical event leaves no unread summary",
            messages.controller_mail_summary(
                owned_dispatch_ids={dispatch_id},
                task_store_project_root=project,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            ) == {},
        )
        dispatch_count, item_count = messages._ack_dispatches(
            messages_dir=messages_dir,
            items=aggregate["open_user_needs"],
            dispatch_ids={dispatch_id},
        )
        assert_true("logical acknowledgement reports one event", (dispatch_count, item_count) == (1, 1))
        assert_true(
            "logical acknowledgement persists both source cursors",
            messages.load_read_cursor(messages.ack_cursor_path(messages_dir)) == advances,
        )


def test_legacy_cursor_keys_migrate_atomically_and_idempotently() -> None:
    import tempfile
    from unittest.mock import patch
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        init_git_project(project)

        local_id = "d-legacy-local"
        fleet_id = "d-legacy-fleet"
        addressed_id = "d-legacy-addressed"
        local_path = Path(messages.post_message(
            dispatch_id=local_id,
            msg_type="user_need",
            payload={"text": "already read locally"},
            messages_dir=messages_dir,
        )["path"])
        fleet_path = mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=fleet_id,
            msg_type="user_need",
            payload={"text": "already read remotely"},
        )
        addressed_path = Path(messages.post_message(
            dispatch_id=addressed_id,
            msg_type="status",
            payload={"text": "controller carrier exists"},
            messages_dir=messages_dir,
        )["path"])
        project_root = messages.controller_address_project_root(project)
        old_controller_key = messages.controller_cursor_key(
            "controller-a",
            addressed_id,
            project_root,
        )
        cursor_path = messages.read_cursor_path(messages_dir)
        cursor_path.write_text(json.dumps({
            local_id: 1,
            f"fleet:{fleet_id}": 1,
            old_controller_key: 3,
        }) + "\n", encoding="utf-8")

        with patch.object(messages.os, "replace", wraps=messages.os.replace) as replace:
            summary = messages.controller_mail_summary(
                owned_dispatch_ids={local_id, fleet_id, addressed_id},
                task_store_project_root=project,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
        assert_true("legacy acknowledgements do not reopen", summary == {})
        assert_true(
            "migration replaces cursor file atomically",
            any(Path(call.args[1]) == cursor_path for call in replace.call_args_list),
        )
        local_key = messages.inbox_stream_key(local_path, messages_dir=messages_dir)
        fleet_key = messages.inbox_stream_key(fleet_path, messages_dir=messages_dir)
        addressed_key = messages.controller_cursor_key(
            "controller-a",
            addressed_id,
            project_root,
            inbox_key=messages.inbox_stream_key(addressed_path, messages_dir=messages_dir),
        )
        document = json.loads(cursor_path.read_text(encoding="utf-8"))
        migrated = document["cursor"]
        assert_true(
            "exactly three legacy key forms migrate",
            migrated == {
                local_key: 1,
                fleet_key: 1,
                addressed_key: 3,
            },
        )
        assert_true("cursor migration writes explicit schema", document["schema"] == messages.MESSAGE_CURSOR_SCHEMA and document["schema_version"] == 1)
        assert_true("fully resolved migration reports no unresolved entries", document["migration"]["unresolved"] == {})
        first_bytes = cursor_path.read_bytes()
        with patch.object(messages, "write_read_cursor", wraps=messages.write_read_cursor) as write:
            second = messages.load_read_cursor(
                cursor_path,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
        assert_true("second migration returns identical state", second == migrated)
        assert_true("second migration leaves bytes unchanged", cursor_path.read_bytes() == first_bytes)
        assert_true("second migration performs no rewrite", write.call_count == 0)


def test_legacy_cursor_ambiguous_and_absent_entries_stay_unresolved() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        ambiguous_id = "d-legacy-ambiguous"
        absent_id = "d-legacy-reaped"
        init_git_project(project)

        local_path = Path(messages.post_message(
            dispatch_id=ambiguous_id,
            msg_type="blocked",
            payload={"text": "unread local blocker"},
            messages_dir=messages_dir,
            seq=1,
        )["path"])
        remote_path = Path(messages.post_message(
            dispatch_id=ambiguous_id,
            msg_type="user_need",
            payload={"text": "previously read fleet need"},
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
        assert_true("ambiguous local input path measured", local_path == messages.inbox_path(messages_dir, ambiguous_id))
        assert_true("ambiguous remote input path measured", remote_path == messages.inbox_path(remote_messages_dir, ambiguous_id))
        assert_true("ambiguous fleet input path measured", fleet_path.exists())

        cursor_path = messages.read_cursor_path(messages_dir)
        cursor_path.write_text(
            json.dumps({ambiguous_id: 1, absent_id: 7}) + "\n",
            encoding="utf-8",
        )
        summary = messages.controller_mail_summary(
            owned_dispatch_ids={ambiguous_id},
            task_store_project_root=project,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true(
            "ambiguous legacy cursor acknowledges neither stream",
            messages.load_read_cursor(
                cursor_path,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            ) == {},
        )
        assert_true(
            "both ambiguous carrier events remain visible",
            {item["text"] for item in summary["needs"]}
            == {"unread local blocker", "previously read fleet need"},
        )
        document = json.loads(cursor_path.read_text(encoding="utf-8"))
        unresolved = document["migration"]["unresolved"]
        assert_true(
            "operator summary points to compact unresolved migration report",
            summary["cursor_migration"]
            == {
                "source_format": "unversioned-flat-map",
                "migrated": 0,
                "unresolved_count": 2,
                "report_path": str(cursor_path),
            },
        )
        assert_true("ambiguous original value preserved", unresolved[ambiguous_id]["value"] == 1)
        assert_true("ambiguous provenance reported", unresolved[ambiguous_id]["reason"] == "ambiguous-carrier-provenance")
        assert_true("absent original value preserved", unresolved[absent_id]["value"] == 7)
        assert_true("absent carrier reported", unresolved[absent_id]["reason"] == "carrier-absent")
        migrated_bytes = cursor_path.read_bytes()

        reaped_remote_path = Path(messages.post_message(
            dispatch_id=absent_id,
            msg_type="user_need",
            payload={"text": "returned fleet carrier"},
            messages_dir=remote_messages_dir,
            source={"node": "remote", "adapter": "codex", "transport": "acp"},
            seq=1,
        )["path"])
        reaped_merge = messages.merge_remote_register(
            fleet_dir,
            reaped_remote_path,
            messages_dir=messages_dir,
        )
        reaped_fleet_path = Path(reaped_merge["merged_into"])
        assert_true("reappeared remote input path measured", reaped_remote_path == messages.inbox_path(remote_messages_dir, absent_id))
        assert_true("reappeared fleet input path measured", reaped_fleet_path == messages.steering_register_path(fleet_dir).with_name(f"{absent_id}.jsonl"))
        assert_true(
            "reappeared carrier does not reclassify retained key",
            messages.load_read_cursor(
                cursor_path,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            ) == {},
        )
        assert_true("reappeared carrier leaves migration report unchanged", cursor_path.read_bytes() == migrated_bytes)
        reaped_summary = messages.controller_mail_summary(
            owned_dispatch_ids={absent_id},
            task_store_project_root=project,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true("reappeared fleet need remains visible", [item["text"] for item in reaped_summary["needs"]] == ["returned fleet carrier"])


def test_stream_tokens_refuse_structural_cursor_name_and_versioned_cursor_is_idempotent() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        unusual_id = '["local","victim"]'
        victim_id = "victim"
        init_git_project(project)

        try:
            messages.post_message(
                dispatch_id=unusual_id,
                msg_type="blocked",
                payload={"text": "structural cursor text is not a stream token"},
                messages_dir=messages_dir,
                seq=1,
            )
            assert_true("structural cursor text must be refused as a stream name", False)
        except messages.MessageError as exc:
            assert_true("stream-token refusal gives a reason", "stream token" in str(exc))
        victim_path = Path(messages.post_message(
            dispatch_id=victim_id,
            msg_type="blocked",
            payload={"text": "unread victim"},
            messages_dir=messages_dir,
            seq=1,
        )["path"])
        victim_key = messages.inbox_stream_key(victim_path, messages_dir=messages_dir)
        assert_true("victim input path measured", victim_path == messages.inbox_path(messages_dir, victim_id))

        versioned_path = messages_dir / ".versioned-cursor.json"
        versioned_path.write_text(
            json.dumps({
                "schema": "goalflight.message-cursor.v1",
                "schema_version": 1,
                "cursor": {victim_key: 1},
                "migration": {"unresolved": {}},
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        versioned_bytes = versioned_path.read_bytes()
        first_versioned = messages.load_read_cursor(
            versioned_path,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        second_versioned = messages.load_read_cursor(
            versioned_path,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true("versioned cursor parses structural key", first_versioned == {victim_key: 1})
        assert_true("versioned cursor is idempotent across two loads", second_versioned == first_versioned and versioned_path.read_bytes() == versioned_bytes)


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


def test_corrupt_carrier_quarantines_record_reports_error_and_reads_suffix() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-corrupt-fleet-prefix"
        init_git_project(project)

        local_path = Path(messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="status",
            payload={"text": "healthy local status"},
            messages_dir=messages_dir,
        )["path"])
        remote_need = messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="user_need",
            payload={"text": "fleet escalation before corruption"},
            messages_dir=remote_messages_dir,
            source={"node": "remote", "adapter": "codex", "transport": "acp"},
        )
        merged = messages.merge_remote_register(
            fleet_dir,
            Path(remote_need["path"]),
            messages_dir=messages_dir,
        )
        fleet_path = Path(merged["merged_into"])
        remote_after = messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="status",
            payload={"text": "must remain beyond corrupt boundary"},
            messages_dir=remote_messages_dir,
            source={"node": "remote", "adapter": "codex", "transport": "acp"},
        )["envelope"]
        with fleet_path.open("a", encoding="utf-8") as carrier:
            carrier.write("{corrupt json\n")
            carrier.write(messages.serialize_envelope_line(remote_after))

        summary = messages.controller_mail_summary(
            owned_dispatch_ids={dispatch_id},
            task_store_project_root=project,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true("validated fleet escalation still surfaces", summary["count"] == 1)
        assert_true("healthy stream is not muted", summary["needs"][0]["text"] == "fleet escalation before corruption")
        assert_true("controller summary reports corrupt carrier", len(summary["carrier_errors"]) == 1)
        assert_true("reported corrupt input path is exact", summary["carrier_errors"][0]["path"] == str(fleet_path))
        assert_true("validated prefix boundary is reported", summary["carrier_errors"][0]["validated_through_seq"] == 1)

        max_errors: list[dict[str, object]] = []
        maxes = messages.max_seq_by_inbox(
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            dispatch_ids={dispatch_id},
            carrier_errors=max_errors,
        )
        local_key = messages.inbox_stream_key(local_path, messages_dir=messages_dir)
        fleet_key = messages.inbox_stream_key(fleet_path, messages_dir=messages_dir)
        assert_true("suffix contributes to stream maximum", maxes == {local_key: 1, fleet_key: 2})
        assert_true("max scan reports corruption", len(max_errors) == 1)

        read = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["read", "--dispatch-id", dispatch_id, "--unseen", "--ack"],
        )
        assert_true("corrupt read fails loud", read.returncode == 1)
        assert_true("corrupt read prints warning", "WARNING: carrier corruption:" in read.stderr)
        shown = json.loads(read.stdout.splitlines()[0])
        assert_true(
            "read preserves prefix and post-corruption event",
            [env["type"] for env in shown] == ["status", "user_need", "status"],
        )
        cursor = messages.load_read_cursor(
            messages.read_cursor_path(messages_dir),
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true("ack advances through visible suffix", cursor == {local_key: 1, fleet_key: 2})
        try:
            messages.read_envelopes(fleet_path)
            assert_true("strict audit read must remain fail closed", False)
        except messages.MessageError:
            pass

        utf_dispatch_id = "d-corrupt-fleet-utf8"
        utf_remote = messages.post_message(
            dispatch_id=utf_dispatch_id,
            msg_type="user_need",
            payload={"text": "fleet escalation before invalid UTF-8"},
            messages_dir=remote_messages_dir,
            source={"node": "remote", "adapter": "codex", "transport": "acp"},
        )
        utf_merged = messages.merge_remote_register(
            fleet_dir,
            Path(utf_remote["path"]),
            messages_dir=messages_dir,
        )
        utf_fleet_path = Path(utf_merged["merged_into"])
        with utf_fleet_path.open("ab") as carrier:
            carrier.write(b"\xff invalid UTF-8\n")
        utf_summary = messages.controller_mail_summary(
            owned_dispatch_ids={utf_dispatch_id},
            task_store_project_root=project,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true("invalid UTF-8 preserves validated prefix", utf_summary["count"] == 1)
        assert_true("invalid UTF-8 reports exact line", utf_summary["carrier_errors"][0]["line"] == 2)


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


def test_relay_user_need_e2e() -> None:
    import subprocess
    import tempfile
    from goalflight_messages import inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        (fleet_dir / "register").mkdir()
        dispatch_id = "d-relay-e2e"
        path = inbox_path(messages_dir, dispatch_id)
        _carrier_add(
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
        _carrier_add(
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

        def slow_next_seq(seq_path: Path, *, envelopes: list[dict] | None = None) -> int:
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return original_next_seq(seq_path, envelopes=envelopes)
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


def test_listener_live_session_covers_dispatch_launched_after_start() -> None:
    import tempfile
    import goalflight_session_status as sessions
    from goalflight_messages import post_message

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "late-listener-project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(project)
        session_id = "listener-owner"
        sessions.claim_session(project, pid=os.getpid(), session_id=session_id)
        listener = start_messages_listener(messages_dir, fleet_dir, project)
        time.sleep(0.2)
        write_ledger_record(
            base,
            "late-owned",
            project,
            controller_session_id=session_id,
        )
        post_message(
            dispatch_id="late-owned",
            msg_type="result",
            payload={"complete": True, "text": "finished"},
            messages_dir=messages_dir,
        )
        code, stdout, stderr = listener_result(listener)
        assert_true("late-owned dispatch wakes", code == 0)
        payload = json.loads(stdout)
        assert_true("implicit live session owns wake", payload["items"][0]["dispatch_id"] == "late-owned")
        assert_true("terminal envelope wakes", payload["items"][0]["type"] == "result")
        assert_true("listener stays silent on stderr", stderr == "")


def test_listener_ignores_dispatch_owned_by_different_session() -> None:
    import tempfile
    from goalflight_messages import post_message

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "other-owner-project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(project)
        listener = start_messages_listener(messages_dir, fleet_dir, project, session_id="mine")
        time.sleep(0.2)
        other_dispatch_id = f"{project.name}-theirs"
        write_ledger_record(base, other_dispatch_id, project, controller_session_id="theirs")
        post_message(
            dispatch_id=other_dispatch_id,
            msg_type="blocked",
            payload={"text": "other controller must decide"},
            messages_dir=messages_dir,
        )
        time.sleep(0.7)
        assert_true("different owner does not wake", listener.poll() is None)
        write_ledger_record(base, "mine", project, controller_session_id="mine")
        post_message(
            dispatch_id="mine",
            msg_type="blocked",
            payload={"text": "this controller must decide"},
            messages_dir=messages_dir,
        )
        code, stdout, _stderr = listener_result(listener)
        assert_true("owned escalation wakes", code == 0)
        assert_true("only owned dispatch reported", json.loads(stdout)["items"][0]["dispatch_id"] == "mine")


def test_listener_ignores_unowned_dispatch() -> None:
    import tempfile
    from goalflight_messages import post_message

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "unowned-project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(project)
        listener = start_messages_listener(messages_dir, fleet_dir, project, session_id="mine")
        time.sleep(0.2)
        unowned_dispatch_id = f"{project.name}-unowned"
        write_ledger_record(base, unowned_dispatch_id, project)
        post_message(
            dispatch_id=unowned_dispatch_id,
            msg_type="result",
            payload={"complete": True, "text": "owner unknown"},
            messages_dir=messages_dir,
        )
        time.sleep(0.7)
        assert_true("honestly unowned dispatch does not wake", listener.poll() is None)
        write_ledger_record(base, "mine", project, controller_session_id="mine")
        post_message(
            dispatch_id="mine",
            msg_type="result",
            payload={"complete": True, "text": "owned terminal"},
            messages_dir=messages_dir,
        )
        code, stdout, _stderr = listener_result(listener)
        assert_true("owned terminal wakes after unowned mail", code == 0)
        assert_true("unowned event stays absent", unowned_dispatch_id not in stdout)


def test_listener_task_store_nag_counts_without_waking_then_escalation_wakes() -> None:
    import tempfile
    import goalflight_task as tasks
    from goalflight_messages import controller_mail_summary, post_message

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "nag-project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(project)
        listener = start_messages_listener(messages_dir, fleet_dir, project, session_id="mine")
        time.sleep(0.2)
        task_store_dispatch_id = tasks._next_nudge_dispatch_id(project)
        post_message(
            dispatch_id=task_store_dispatch_id,
            msg_type="user_need",
            payload={
                "nudge_kind": "resume-ready",
                "text": "19 tasks ready (top: t-022) -> continue?",
            },
            messages_dir=messages_dir,
        )
        time.sleep(0.7)
        assert_true("periodic task-store nag does not wake", listener.poll() is None)
        summary = controller_mail_summary(
            owned_dispatch_ids=set(),
            task_store_project_root=project,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        assert_true("task-store nag remains in unread display count", summary["count"] == 1)
        write_ledger_record(base, "real-escalation", project, controller_session_id="mine")
        post_message(
            dispatch_id="real-escalation",
            msg_type="user_need",
            payload={"text": "worker needs a real decision"},
            messages_dir=messages_dir,
        )
        code, stdout, _stderr = listener_result(listener)
        assert_true("real worker escalation wakes", code == 0)
        assert_true("wake output excludes nag", task_store_dispatch_id not in stdout)
        assert_true("wake output includes escalation", "real-escalation" in stdout)


def test_listener_wakes_for_controller_addressed_mail() -> None:
    import tempfile
    from goalflight_messages import post_message

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "addressed-project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(project)
        listener = start_messages_listener(messages_dir, fleet_dir, project, session_id="mine")
        time.sleep(0.2)
        addressed_inbox = f"{project.name}-controller-note"
        post_message(
            dispatch_id=addressed_inbox,
            msg_type="controller-notice",
            payload={"text": "peer controller message"},
            messages_dir=messages_dir,
        )
        code, stdout, _stderr = listener_result(listener)
        assert_true("controller-addressed mail wakes", code == 0)
        assert_true("addressed inbox reported", addressed_inbox in stdout)


def test_wake_filter_uses_sender_direction_and_preserves_unread_mail() -> None:
    import tempfile
    import goalflight_messages as messages
    import goalflight_session_status as sessions

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "direction-project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "mine"
        init_git_project(project)
        sessions.claim_session(
            project,
            pid=os.getpid(),
            session_id="mine-session",
            label="mine-controller",
        )
        write_ledger_record(
            base,
            dispatch_id,
            project,
            controller_session_id="mine-session",
            worker_pid=os.getpid(),
        )

        updates = {
            "GOALFLIGHT_MESSAGES_DIR": str(messages_dir),
            "GOALFLIGHT_FLEET_DIR": str(fleet_dir),
            "GOALFLIGHT_STATE_DIR": str(base / "state"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(base / "pids"),
            "GOALFLIGHT_CONTROLLER_PID": str(os.getpid()),
            "GOALFLIGHT_CONTROLLER_LABEL": "mine-controller",
        }
        previous = {key: os.environ.get(key) for key in updates}
        os.environ.update(updates)
        try:
            messages.post_controller_steer(dispatch_id, "my outbound steer")
            recorded = messages.read_envelopes(messages.inbox_path(messages_dir, dispatch_id))
            assert_true(
                "outbound steer records its proven author session",
                recorded[0]["source"].get("controller_session_id") == "mine-session",
            )
            own_only = messages.controller_wake_watermark(
                project_root=project,
                owned_dispatch_ids={dispatch_id},
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
            assert_true("controller's own outbound steer does not wake", not own_only)

            messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "peer controller steer"},
                messages_dir=messages_dir,
                source={
                    "node": "local",
                    "adapter": "goalflight-dispatch",
                    "transport": "steer",
                    "controller_session_id": "peer-session",
                },
            )
            messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "ambiguous controller steer"},
                messages_dir=messages_dir,
                source={
                    "node": "local",
                    "adapter": "goalflight-dispatch",
                    "transport": "steer",
                },
            )
            messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="blocked",
                payload={"text": "worker escalation with misleading source"},
                messages_dir=messages_dir,
                source={
                    "node": "local",
                    "adapter": "goalflight-dispatch",
                    "transport": "steer",
                    "controller_session_id": "mine-session",
                },
            )
            wakes = messages.controller_wake_watermark(
                project_root=project,
                owned_dispatch_ids={dispatch_id},
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
            wake_key = messages.inbox_stream_key(
                messages.inbox_path(messages_dir, dispatch_id),
                messages_dir=messages_dir,
            )
            assert_true(
                "another controller wakes despite sharing goalflight-dispatch adapter",
                (wake_key, 2) in wakes,
            )
            assert_true("ambiguous controller authorship wakes", (wake_key, 3) in wakes)
            assert_true("typed worker escalation cannot be self-suppressed", (wake_key, 4) in wakes)
            assert_true("self-authored envelope stays out of wake set", (wake_key, 1) not in wakes)

            summary = messages.controller_mail_summary(
                owned_dispatch_ids={dispatch_id},
                task_store_project_root=project,
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
            assert_true("all wake-worthy and quiet mail remains unread", summary["count"] == 4)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


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


def test_named_peer_mail_crosses_projects_when_explicitly_addressed() -> None:
    import tempfile
    import goalflight_messages as messages
    import goalflight_status as status

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        mine = base / "mine"
        peer = base / "peer"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(mine)
        init_git_project(peer)
        fleet_dir.mkdir()
        previous = _claim_test_controller(base, mine)
        try:
            write_ledger_record(
                base,
                "mine-worker",
                mine,
                controller_session_id="mine-session",
                controller_label="mine-controller",
            )
            write_ledger_record(base, "peer-correspondence", peer)
            posted = run_messages_cli(
                messages_dir,
                fleet_dir,
                [
                    "post",
                    "--dispatch-id",
                    "peer-correspondence",
                    "--type",
                    "controller-notice",
                    "--text",
                    "cross-project finding",
                    "--to-controller",
                    "mine-controller",
                    "--controller-project-root",
                    str(mine),
                ],
                cwd=peer,
            )
            assert_true("named controller post records without worker delivery", posted.returncode == 0)
            stored = messages.read_envelopes(messages.inbox_path(messages_dir, "peer-correspondence"))
            assert_true(
                "envelope carries stable controller label",
                stored[0].get("addressee")
                == {
                    "kind": "controller",
                    "label": "mine-controller",
                    "project_root": str(mine.resolve()),
                },
            )
            relayed = run_messages_cli(messages_dir, fleet_dir, ["relay", "--new"], cwd=mine)
            assert_true("default relay surfaces named peer mail", "cross-project finding" in relayed.stdout)
            summary = status._mail_summary({"mine-worker"}, project_root=mine)
            assert_true("default status scope counts named peer mail", summary.get("count") == 1)
            wakes = messages.controller_wake_watermark(
                project_root=mine,
                owned_dispatch_ids={"mine-worker"},
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
            peer_key = messages.inbox_stream_key(
                messages.inbox_path(messages_dir, "peer-correspondence"),
                messages_dir=messages_dir,
            )
            assert_true("shared wake filter includes named peer mail", (peer_key, 1) in wakes)
            wait_wakes = status._mail_watermark(str(mine), ["mine-worker"])
            assert_true(
                "status wait delegates named peer mail to shared filter",
                wait_wakes is not None and (peer_key, 1) in wait_wakes,
            )
        finally:
            _restore_test_controller(previous)


def test_named_mail_for_different_controller_is_quiet_and_readable() -> None:
    import tempfile
    import goalflight_messages as messages

    assert_true(
        "recipient cursor keys cannot collide on colon-bearing labels",
        messages.controller_cursor_key("a:b", "c")
        != messages.controller_cursor_key("a", "b:c"),
    )

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        mine = base / "mine"
        peer = base / "peer"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(mine)
        init_git_project(peer)
        fleet_dir.mkdir()
        previous = _claim_test_controller(base, mine)
        try:
            write_ledger_record(
                base,
                "mine-worker",
                mine,
                controller_session_id="mine-session",
                controller_label="mine-controller",
            )
            messages.post_message(
                dispatch_id="peer-private",
                msg_type="controller-question",
                payload={"text": "for somebody else"},
                messages_dir=messages_dir,
                addressee=messages.controller_addressee(
                    "other-controller", project_root=peer
                ),
            )
            wakes = messages.controller_wake_watermark(
                project_root=mine,
                owned_dispatch_ids={"mine-worker"},
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
            peer_key = messages.inbox_stream_key(
                messages.inbox_path(messages_dir, "peer-private"),
                messages_dir=messages_dir,
            )
            assert_true("different addressee does not wake", (peer_key, 1) not in wakes)
            relayed = run_messages_cli(messages_dir, fleet_dir, ["relay", "--new"], cwd=mine)
            assert_true("different addressee absent from default relay", "for somebody else" not in relayed.stdout)
            readable = run_messages_cli(
                messages_dir,
                fleet_dir,
                ["read", "--dispatch-id", "peer-private", "--json"],
                cwd=mine,
            )
            assert_true("different addressee correspondence remains readable", "for somebody else" in readable.stdout)
        finally:
            _restore_test_controller(previous)


def test_cross_project_worker_traffic_remains_project_scoped() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        mine = base / "mine"
        peer = base / "peer"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(mine)
        init_git_project(peer)
        fleet_dir.mkdir()
        previous = _claim_test_controller(base, mine)
        try:
            write_ledger_record(
                base,
                "mine-worker",
                mine,
                controller_session_id="mine-session",
                controller_label="mine-controller",
            )
            write_ledger_record(base, "peer-worker", peer)
            messages.post_message(
                dispatch_id="peer-worker",
                msg_type="blocked",
                payload={"text": "peer worker escalation"},
                messages_dir=messages_dir,
            )
            wakes = messages.controller_wake_watermark(
                project_root=mine,
                owned_dispatch_ids={"mine-worker"},
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
            peer_key = messages.inbox_stream_key(
                messages.inbox_path(messages_dir, "peer-worker"),
                messages_dir=messages_dir,
            )
            assert_true("foreign worker escalation does not wake", (peer_key, 1) not in wakes)
            relayed = run_messages_cli(messages_dir, fleet_dir, ["relay", "--new"], cwd=mine)
            assert_true("foreign worker traffic absent from default relay", "peer worker escalation" not in relayed.stdout)
        finally:
            _restore_test_controller(previous)


def test_unknown_controller_name_is_preserved_and_reported() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        mine = base / "mine"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        init_git_project(mine)
        fleet_dir.mkdir()
        previous = _claim_test_controller(base, mine)
        try:
            write_ledger_record(
                base,
                "mine-worker",
                mine,
                controller_session_id="mine-session",
                controller_label="mine-controller",
            )
            messages.post_message(
                dispatch_id="ghost-mail",
                msg_type="controller-notice",
                payload={"text": "retain this warning"},
                messages_dir=messages_dir,
                addressee=messages.controller_addressee(
                    "unclaimed-controller", project_root=mine
                ),
            )
            wakes = messages.controller_wake_watermark(
                project_root=mine,
                owned_dispatch_ids={"mine-worker"},
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
            ghost_key = messages.inbox_stream_key(
                messages.inbox_path(messages_dir, "ghost-mail"),
                messages_dir=messages_dir,
            )
            assert_true("unclaimed name does not wake arbitrary controller", (ghost_key, 1) not in wakes)
            report = run_messages_cli(messages_dir, fleet_dir, ["undeliverable"], cwd=mine)
            assert_true("unclaimed name is reported", "1 unresolved controller envelope" in report.stdout)
            assert_true("unclaimed label is named", "to unclaimed-controller" in report.stdout)
            assert_true("unclaimed subject remains reportable", "retain this warning" in report.stdout)
            stored = messages.read_envelopes(messages.inbox_path(messages_dir, "ghost-mail"))
            assert_true("unclaimed envelope remains stored", len(stored) == 1)
            assert_true("unclaimed envelope remains unread", not messages.read_cursor_path(messages_dir).exists())
        finally:
            _restore_test_controller(previous)


def test_backlog_triage_digests_without_deleting_and_new_mail_stays_new() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        for index in range(3):
            messages.post_message(
                dispatch_id=f"legacy-{index}",
                msg_type="status",
                payload={"text": f"legacy body {index}"},
                messages_dir=messages_dir,
            )
        original = {
            path.name: path.read_bytes()
            for path in messages_dir.glob("*.jsonl")
        }
        triaged = run_messages_cli(messages_dir, fleet_dir, ["triage-backlog", "--apply"])
        result = json.loads(triaged.stdout)
        assert_true("exact unread snapshot triaged", result["envelope_count"] == 3)
        digest = json.loads(Path(result["digest"]).read_text(encoding="utf-8"))
        assert_true("digest lists every envelope", len(digest["items"]) == 3)
        assert_true("digest records retention", digest["correspondence_retained"] is True)
        assert_true(
            "original correspondence bytes unchanged",
            all((messages_dir / name).read_bytes() == content for name, content in original.items()),
        )
        messages.post_message(
            dispatch_id="legacy-0",
            msg_type="status",
            payload={"text": "arrived after triage"},
            messages_dir=messages_dir,
        )
        relayed = run_messages_cli(messages_dir, fleet_dir, ["relay", "--new", "--all-projects"])
        assert_true("post-snapshot mail remains new", "arrived after triage" in relayed.stdout)
        assert_true("triaged bodies no longer flood new view", "legacy body" not in relayed.stdout)


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
    import goalflight_messages as messages
    from goalflight_messages import READ_CURSOR_FILE, inbox_path, markers_to_envelopes

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
            _carrier_add(path, env)

        marked = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["mark-read", "--dispatch-id", dispatch_id, "--through", "1"],
        )
        assert_true("mark-read exit 0", marked.returncode == 0)
        cursor_path = messages_dir / READ_CURSOR_FILE
        assert_true("cursor created", cursor_path.exists())
        key = messages.inbox_stream_key(path, messages_dir=messages_dir)
        assert_true("cursor value", messages.load_read_cursor(cursor_path)[key] == 1)

        unseen = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen"])
        assert_true("unseen exit 0", unseen.returncode == 0)
        lines = unseen.stdout.splitlines()
        shown = json.loads(lines[0])
        assert_true("only one unseen", len(shown) == 1)
        assert_true("seq 2 unseen", shown[0]["seq"] == 2)
        assert_true("count line", lines[1] == "unseen counts: d-cursor=1")


def test_mark_read_all_advances_every_inbox_to_current_max() -> None:
    import tempfile
    import goalflight_messages as messages
    from goalflight_messages import READ_CURSOR_FILE, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        paths: dict[str, Path] = {}
        for dispatch_id, count in {"d-one": 1, "d-two": 2}.items():
            path = inbox_path(messages_dir, dispatch_id)
            paths[dispatch_id] = path
            markers = {"STATUS": [f"{dispatch_id}-{idx}" for idx in range(count)]}
            for env in markers_to_envelopes(markers, dispatch_id=dispatch_id):
                _carrier_add(path, env)

        marked = run_messages_cli(messages_dir, fleet_dir, ["mark-read", "--all"])
        assert_true("mark-read all exit 0", marked.returncode == 0)
        cursor = messages.load_read_cursor(messages_dir / READ_CURSOR_FILE)
        assert_true(
            "first inbox max",
            cursor[messages.inbox_stream_key(paths["d-one"], messages_dir=messages_dir)] == 1,
        )
        assert_true(
            "second inbox max",
            cursor[messages.inbox_stream_key(paths["d-two"], messages_dir=messages_dir)] == 2,
        )


def test_mark_read_through_never_rewinds() -> None:
    import tempfile
    import goalflight_messages as messages
    from goalflight_messages import READ_CURSOR_FILE

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        path = Path(messages.post_message(
            dispatch_id="d-sticky",
            msg_type="status",
            payload={"text": "five"},
            messages_dir=messages_dir,
            seq=5,
        )["path"])
        key = messages.inbox_stream_key(path, messages_dir=messages_dir)
        first = run_messages_cli(messages_dir, fleet_dir, ["mark-read", "--dispatch-id", "d-sticky", "--through", "5"])
        second = run_messages_cli(messages_dir, fleet_dir, ["mark-read", "--dispatch-id", "d-sticky", "--through", "3"])
        assert_true("first mark-read ok", first.returncode == 0)
        assert_true("second mark-read ok", second.returncode == 0)
        cursor = messages.load_read_cursor(messages_dir / READ_CURSOR_FILE)
        assert_true("cursor stayed at high-water mark", cursor[key] == 5)
        assert_true("unchanged reported", f"{key} 5->5 (unchanged)" in second.stdout)


def test_concurrent_mark_read_through_merges_per_inbox_max() -> None:
    import tempfile
    import goalflight_messages as messages
    from goalflight_messages import READ_CURSOR_FILE

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        messages_dir.mkdir()
        fleet_dir.mkdir()
        paths = {
            dispatch_id: Path(messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": f"max {maximum}"},
                messages_dir=messages_dir,
                seq=maximum,
            )["path"])
            for dispatch_id, maximum in {"d-a": 9, "d-b": 7}.items()
        }
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
        cursor = messages.load_read_cursor(messages_dir / READ_CURSOR_FILE)
        assert_true(
            "d-a max retained",
            cursor[messages.inbox_stream_key(paths["d-a"], messages_dir=messages_dir)] == 9,
        )
        assert_true(
            "d-b max retained",
            cursor[messages.inbox_stream_key(paths["d-b"], messages_dir=messages_dir)] == 7,
        )


def test_read_unseen_ack_advances_to_shown() -> None:
    import tempfile
    import goalflight_messages as messages
    from goalflight_messages import READ_CURSOR_FILE, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-ack"
        path = inbox_path(messages_dir, dispatch_id)
        for env in markers_to_envelopes({"STATUS": ["one", "two"]}, dispatch_id=dispatch_id):
            _carrier_add(path, env)

        first = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen", "--ack"])
        assert_true("ack read ok", first.returncode == 0)
        first_lines = first.stdout.splitlines()
        assert_true("both shown", [env["seq"] for env in json.loads(first_lines[0])] == [1, 2])
        assert_true("pre-ack count", first_lines[1] == "unseen counts: d-ack=2")
        cursor = messages.load_read_cursor(messages_dir / READ_CURSOR_FILE)
        key = messages.inbox_stream_key(path, messages_dir=messages_dir)
        assert_true("ack cursor", cursor[key] == 2)

        second = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen"])
        second_lines = second.stdout.splitlines()
        assert_true("nothing left", json.loads(second_lines[0]) == [])
        assert_true("zero count", second_lines[1] == "unseen counts: d-ack=0")


def test_ack_cursor_write_failure_warns_without_traceback() -> None:
    import tempfile
    from goalflight_messages import READ_CURSOR_FILE, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-ack-fail"
        path = inbox_path(messages_dir, dispatch_id)
        _carrier_add(path, markers_to_envelopes({"STATUS": ["shown"]}, dispatch_id=dispatch_id)[0])
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
    from goalflight_messages import READ_CURSOR_FILE, inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-corrupt"
        path = inbox_path(messages_dir, dispatch_id)
        for env in markers_to_envelopes({"STATUS": ["one", "two"]}, dispatch_id=dispatch_id):
            _carrier_add(path, env)

        absent = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen"])
        assert_true("absent cursor ok", absent.returncode == 0)
        assert_true("absent shows all", [env["seq"] for env in json.loads(absent.stdout.splitlines()[0])] == [1, 2])

        (messages_dir / READ_CURSOR_FILE).write_text("{not json\n", encoding="utf-8")
        corrupt = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id, "--unseen"])
        assert_true("corrupt cursor ok", corrupt.returncode == 0)
        assert_true("corrupt shows all", [env["seq"] for env in json.loads(corrupt.stdout.splitlines()[0])] == [1, 2])


def test_seen_open_user_need_requires_history_relay() -> None:
    import tempfile
    from goalflight_messages import inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-open-seen"
        path = inbox_path(messages_dir, dispatch_id)
        _carrier_add(
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
    from goalflight_messages import inbox_path, markers_to_envelopes

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
        _carrier_add(path, envelope)

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
    from goalflight_messages import inbox_path, markers_to_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        dispatch_id = "d-relay-ack"
        _carrier_add(
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
    from goalflight_messages import inbox_path, markers_to_envelopes

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
            _carrier_add(inbox_path(messages_dir, dispatch_id), envelope)

        relay = run_messages_cli(messages_dir, fleet_dir, ["relay"], cwd=project)
        assert_true("bounded relay exits with mail", relay.returncode == 2)
        assert_true("hard byte cap", len(relay.stdout.encode("utf-8")) <= 4096)
        lines = relay.stdout.splitlines()
        assert_true("item cap plus elision", len(lines) <= 21)
        assert_true("newest item first", lines[0].startswith("[d-large-079]"))
        assert_true("elision line present", lines[-1].startswith("(+") and lines[-1].endswith("elided)"))


def test_default_relay_excludes_cross_project_mail() -> None:
    import tempfile
    from goalflight_messages import inbox_path, markers_to_envelopes

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
            _carrier_add(
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
    import goalflight_messages as messages
    from goalflight_messages import (
        ACK_CURSOR_FILE,
        READ_CURSOR_FILE,
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
            _carrier_add(
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
        ack_cursor = messages.load_read_cursor(messages_dir / ACK_CURSOR_FILE)
        assert_true(
            "ack cursor exact stale keys",
            set(ack_cursor)
            == {
                messages.inbox_stream_key(
                    inbox_path(messages_dir, dispatch_id),
                    messages_dir=messages_dir,
                )
                for dispatch_id in ("d-closed", "d-old", "d-taskless-old")
            },
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
        test_dual_source_inboxes_aggregate_without_fleet_overwrite,
        test_single_source_inboxes_and_deterministic_order_reject_local_overwrite,
        test_dual_source_sequences_keep_independent_cursor_and_wake_identity,
        test_unseen_last_n_ack_only_advances_shown_event_and_keeps_empty_zero,
        test_mark_read_through_clamps_each_stream_and_preserves_later_fleet_mail,
        test_structural_stream_keys_prevent_dispatch_prefix_collision,
        test_merged_envelope_dedupes_event_but_acknowledges_both_streams,
        test_legacy_cursor_keys_migrate_atomically_and_idempotently,
        test_legacy_cursor_ambiguous_and_absent_entries_stay_unresolved,
        test_cursor_version_controls_structural_key_parsing_and_is_idempotent,
        test_same_id_different_envelopes_both_survive_and_later_need_reopens,
        test_read_returns_fleet_only_message_body,
        test_last_steering_uses_controller_ingestion_order_across_clock_skew,
        test_remerged_identical_steering_reuses_persisted_ingestion_order,
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
        test_listener_live_session_covers_dispatch_launched_after_start,
        test_listener_ignores_dispatch_owned_by_different_session,
        test_listener_ignores_unowned_dispatch,
        test_listener_task_store_nag_counts_without_waking_then_escalation_wakes,
        test_listener_wakes_for_controller_addressed_mail,
        test_wake_filter_uses_sender_direction_and_preserves_unread_mail,
        test_named_peer_mail_crosses_projects_when_explicitly_addressed,
        test_named_mail_for_different_controller_is_quiet_and_readable,
        test_cross_project_worker_traffic_remains_project_scoped,
        test_unknown_controller_name_is_preserved_and_reported,
        test_backlog_triage_digests_without_deleting_and_new_mail_stays_new,
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
