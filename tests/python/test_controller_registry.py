#!/usr/bin/env python3
"""Durable controller-name registry, heartbeat, mail, and retirement tests."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402


def _beacon() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _set_heartbeat(root: Path, label: str, stamp: str) -> None:
    path = sessions._session_file(root)
    data = sessions._read_session_map(path)
    for key, record in data.items():
        if record.get("label") == label:
            record = dict(record)
            record["heartbeat_at"] = stamp
            data[key] = record
    sessions._write_session_map(path, data)


def _registry(root: Path, label: str) -> dict:
    data = sessions._read_session_map(sessions._session_file(root))
    return data[sessions._controller_registry_key(label)]


def _ledger_record(root: Path, label: str, dispatch_id: str, state: str = "running") -> dict:
    return {
        "dispatch_id": dispatch_id,
        "project_root": str(root.resolve()),
        "controller_label": label,
        "state": state,
    }


def _run_status_cli(base: Path, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop(sessions.CONTROLLER_LABEL_ENV, None)
    env.pop(sessions.CONTROLLER_PID_ENV, None)
    env.pop(sessions.CONTROLLER_SESSION_ID_ENV, None)
    env.update(
        {
            "GOALFLIGHT_MESSAGES_DIR": str(base / "messages"),
            "GOALFLIGHT_FLEET_DIR": str(base / "fleet"),
            "GOALFLIGHT_STATE_DIR": str(base / "state"),
            "GOALFLIGHT_TASK_STORE_DIR": str(base / "task-store"),
        }
    )
    (base / "fleet").mkdir(exist_ok=True)
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "goalflight_session_status.py"),
            "--project-root",
            str(root),
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_roster_measures_idle_incarnations_mail_and_owned_dispatches() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        root.mkdir()
        fleet_dir.mkdir()
        live = _beacon()
        dead = _beacon()
        try:
            assert sessions.register_controller(root, "battery-bugs")["registered"]
            assert sessions.register_controller(root, "battery-main", pid=live.pid)["registered"]
            assert sessions.register_controller(root, "battery-dead", pid=dead.pid)["registered"]
            dead.terminate()
            dead.wait(timeout=10)
            _set_heartbeat(root, "battery-bugs", "2026-08-02T12:00:00+00:00")
            messages.post_message(
                dispatch_id="bugs-mail",
                msg_type="controller-notice",
                payload={"text": "one unread"},
                messages_dir=messages_dir,
                addressee=messages.controller_addressee("battery-bugs", project_root=root),
            )
            roster = sessions.controller_roster(
                root,
                now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
                ledger_records=[_ledger_record(root, "battery-bugs", "bugs-owned")],
            )
            by_label = {row["label"]: row for row in roster["controllers"]}
            assert by_label["battery-bugs"]["idle"] == "idle 3 days"
            assert by_label["battery-bugs"]["incarnation_state"] == "heartbeat-only"
            assert by_label["battery-main"]["incarnation_state"] == "live-pid"
            assert by_label["battery-dead"]["incarnation_state"] == "dead-pid"
            assert by_label["battery-bugs"]["unread_addressed_mail"] == 1
            assert by_label["battery-bugs"]["nonterminal_owned_dispatches"] == 1
            assert (
                "battery-bugs | idle 3 days | heartbeat-only | unread 1 | owned 1"
                in sessions.controller_roster_lines(roster)
            )
        finally:
            if live.poll() is None:
                live.terminate()
                live.wait(timeout=10)
            if dead.poll() is None:
                dead.terminate()
                dead.wait(timeout=10)


def test_idle_succession_without_pid_receives_registered_addressed_mail() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        root.mkdir()
        fleet_dir.mkdir()
        first = sessions.register_controller(root, "battery-engine")
        first_id = first["session"]["id"]
        _set_heartbeat(root, "battery-engine", "2000-01-01T00:00:00+00:00")
        joined = sessions.join_controller(root, "battery-engine")
        assert joined["joined"] is True
        assert joined["succession"] is True
        assert joined["session"]["id"] != first_id
        messages.post_message(
            dispatch_id="engine-mail",
            msg_type="controller-notice",
            payload={"text": "verify the fork"},
            messages_dir=messages_dir,
            addressee=messages.controller_addressee("battery-engine", project_root=root),
        )
        with patch.dict(
            os.environ,
            {sessions.CONTROLLER_LABEL_ENV: "", sessions.CONTROLLER_PID_ENV: ""},
        ):
            wake = messages.controller_wake_watermark(
                project_root=root,
                owned_dispatch_ids=set(),
                controller_session_id=joined["session"]["id"],
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
        assert ("engine-mail", 1) in wake
        with patch.object(messages, "_current_project_root", return_value=root):
            assert messages.unresolved_controller_envelopes(
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            ) == []
        roster = sessions.controller_roster(
            root,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            ledger_records=[],
        )
        assert roster["controllers"][0]["session_id"] == joined["session"]["id"]


def test_recent_join_reports_conflict_without_silent_displacement() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        registered = sessions.register_controller(root, "battery-main")
        original_id = registered["session"]["id"]
        conflict = sessions.join_controller(root, "battery-main")
        assert conflict["joined"] is False
        assert conflict["reason"] == "controller_label_conflict"
        assert conflict["conflicting_beacons"] == 1
        assert conflict["acknowledgement_available"] is True
        assert _registry(root, "battery-main")["id"] == original_id
        acknowledged = sessions.join_controller(
            root,
            "battery-main",
            acknowledge_conflict=True,
        )
        assert acknowledged["joined"] is True
        assert acknowledged["conflict_acknowledged"] is True
        assert acknowledged["session"]["id"] != original_id
        current = sessions.live_session(root, label="battery-main")
        assert current["id"] == acknowledged["session"]["id"]
        assert "conflicting_beacons" not in current


def test_retirement_digests_without_deleting_and_requires_dispatch_ack() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        root.mkdir()
        fleet_dir.mkdir()
        sessions.register_controller(root, "battery-engine")
        _set_heartbeat(root, "battery-engine", "2000-01-01T00:00:00+00:00")
        joined = sessions.join_controller(root, "battery-engine")
        assert joined["joined"] is True
        messages.post_message(
            dispatch_id="retire-mail",
            msg_type="controller-notice",
            payload={"text": "fork is complete"},
            messages_dir=messages_dir,
            addressee=messages.controller_addressee("battery-engine", project_root=root),
        )
        inbox = messages.inbox_path(messages_dir, "retire-mail")
        assert messages.read_envelopes(inbox)[0]["payload"]["text"] == "fork is complete"
        original = inbox.read_bytes()
        records = [_ledger_record(root, "battery-engine", "engine-owned")]
        other_incarnation = _beacon()
        try:
            sessions.claim_session(root, pid=other_incarnation.pid, label="battery-engine")
            warned = sessions.retire_controller(
                root,
                "battery-engine",
                session_id=joined["session"]["id"],
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
                ledger_records=records,
            )
        finally:
            other_incarnation.terminate()
            other_incarnation.wait(timeout=10)
        assert warned["retired"] is False
        assert warned["reason"] == "retirement_requires_acknowledgement"
        assert warned["live_incarnations"][0]["pid"] == other_incarnation.pid
        assert warned["nonterminal_owned_dispatches"] == [
            {"dispatch_id": "engine-owned", "state": "running"}
        ]
        retired = sessions.retire_controller(
            root,
            "battery-engine",
            session_id=joined["session"]["id"],
            acknowledge=True,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            ledger_records=records,
        )
        assert retired["retired"] is True
        assert retired["correspondence_retained"] is True
        assert inbox.read_bytes() == original
        digest = json.loads(Path(retired["digest"]).read_text(encoding="utf-8"))
        assert digest["envelope_count"] == 1
        assert digest["retirement"]["controller_label"] == "battery-engine"
        assert digest["retirement"]["retired_by"]["session_id"] == joined["session"]["id"]
        assert sessions.controller_roster(
            root,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            ledger_records=[],
        )["controllers"] == []
        retired_roster = sessions.controller_roster(
            root,
            include_retired=True,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            ledger_records=[],
        )
        assert retired_roster["controllers"][0]["retired"] is True
        messages.post_message(
            dispatch_id="after-retirement",
            msg_type="controller-notice",
            payload={"text": "late follow-up"},
            messages_dir=messages_dir,
            addressee=messages.controller_addressee(
                "battery-engine", project_root=root
            ),
        )
        with patch.object(messages, "_current_project_root", return_value=root):
            unresolved = messages.unresolved_controller_envelopes(
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
        assert [item["dispatch_id"] for item in unresolved] == ["after-retirement"]
        assert inbox.read_bytes() == original


def test_succession_inherits_previous_incarnation_worker_wakes_by_name() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        root.mkdir()
        fleet_dir.mkdir()
        first = sessions.register_controller(root, "engine")
        _set_heartbeat(root, "engine", "2000-01-01T00:00:00+00:00")
        successor = sessions.join_controller(root, "engine")
        records = [
            {
                **_ledger_record(root, "engine", "old-worker"),
                "controller_session_id": first["session"]["id"],
            }
        ]
        messages.post_message(
            dispatch_id="old-worker",
            msg_type="result",
            payload={"text": "completed under the old incarnation"},
            messages_dir=messages_dir,
        )
        with (
            patch.object(messages, "_project_ledger_records", return_value=records),
            patch.dict(
                os.environ,
                {
                    sessions.CONTROLLER_LABEL_ENV: "",
                    sessions.CONTROLLER_PID_ENV: "",
                },
            ),
        ):
            wakes = messages.controller_wake_watermark(
                project_root=root,
                controller_session_id=successor["session"]["id"],
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
        assert ("old-worker", 1) in wakes


def test_retire_requires_current_joined_incarnation_and_audits_actual_retirer() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        first = sessions.register_controller(root, "side")
        refused = sessions.retire_controller(
            root,
            "side",
            messages_dir=base / "messages",
            fleet_dir=base / "fleet",
            ledger_records=[],
        )
        assert refused["reason"] == "retirer_not_incumbent"
        _set_heartbeat(root, "side", "2000-01-01T00:00:00+00:00")
        successor = sessions.join_controller(root, "side")
        stale = sessions.retire_controller(
            root,
            "side",
            session_id=first["session"]["id"],
            messages_dir=base / "messages",
            fleet_dir=base / "fleet",
            ledger_records=[],
        )
        assert stale["reason"] == "retirer_not_incumbent"
        retired = sessions.retire_controller(
            root,
            "side",
            session_id=successor["session"]["id"],
            messages_dir=base / "messages",
            fleet_dir=base / "fleet",
            ledger_records=[],
        )
        assert retired["retired"] is True
        assert retired["retired_by"]["session_id"] == successor["session"]["id"]


def test_heartbeat_only_retirement_audit_never_carries_an_unmeasured_pid() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        session = sessions.register_controller(root, "side")["session"]
        foreign = _beacon()
        try:
            retired = sessions.retire_controller(
                root,
                "side",
                pid=foreign.pid,
                session_id=session["id"],
                messages_dir=base / "messages",
                fleet_dir=base / "fleet",
                ledger_records=[],
            )
        finally:
            foreign.terminate()
            foreign.wait(timeout=10)
        assert retired["retired"] is True
        assert retired["retired_by"]["session_id"] == session["id"]
        assert retired["retired_by"]["pid"] is None
        assert retired["retired_by"]["process_identity"] is None


def test_same_label_in_other_project_never_receives_addressed_mail() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root_a = base / "project-a"
        root_b = base / "project-b"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        root_a.mkdir()
        root_b.mkdir()
        fleet_dir.mkdir()
        session_a = sessions.register_controller(root_a, "shared")["session"]
        session_b = sessions.register_controller(root_b, "shared")["session"]
        retired = sessions.retire_controller(
            root_a,
            "shared",
            session_id=session_a["id"],
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            ledger_records=[],
        )
        assert retired["retired"] is True
        messages.post_message(
            dispatch_id="late-a",
            msg_type="controller-notice",
            payload={"text": "only project A"},
            messages_dir=messages_dir,
            addressee=messages.controller_addressee("shared", project_root=root_a),
        )
        with patch.dict(
            os.environ,
            {
                sessions.CONTROLLER_LABEL_ENV: "",
                sessions.CONTROLLER_PID_ENV: "",
            },
        ):
            wakes = messages.controller_wake_watermark(
                project_root=root_b,
                owned_dispatch_ids=set(),
                controller_session_id=session_b["id"],
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
        assert ("late-a", 1) not in wakes
        with patch.object(messages, "_current_project_root", return_value=root_b):
            unresolved = messages.unresolved_controller_envelopes(
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
            )
        assert [item["dispatch_id"] for item in unresolved] == ["late-a"]


def test_joined_label_declaration_carries_bare_relay_identity() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        root.mkdir()
        fleet_dir.mkdir()
        sessions.register_controller(root, "engine")
        _set_heartbeat(root, "engine", "2000-01-01T00:00:00+00:00")
        sessions.join_controller(root, "engine")
        messages.post_message(
            dispatch_id="engine-note",
            msg_type="controller-notice",
            payload={"text": "joined role mail"},
            messages_dir=messages_dir,
            addressee=messages.controller_addressee("engine", project_root=root),
        )
        args = argparse.Namespace(
            ack=False,
            new=True,
            history=False,
            all_projects=False,
            bodies=True,
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
        )
        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    sessions.CONTROLLER_LABEL_ENV: "engine",
                    sessions.CONTROLLER_PID_ENV: "",
                },
            ),
            patch.object(messages, "_mail_scope", return_value=(root, set())),
            contextlib.redirect_stdout(output),
        ):
            assert messages.cmd_relay(args) == 0
        assert "joined role mail" in output.getvalue()


def test_retirement_sees_preledger_dispatch_resolution_under_registry_lock() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        session = sessions.register_controller(root, "engine")["session"]
        args = argparse.Namespace(
            controller_label="engine",
            controller_beacon_pid=None,
            controller_session_id=None,
            _controller_beacon_pid=None,
        )
        dispatch._stamp_controller_session(args, root)
        warned = sessions.retire_controller(
            root,
            "engine",
            session_id=session["id"],
            messages_dir=base / "messages",
            fleet_dir=base / "fleet",
            ledger_records=[],
        )
        assert warned["reason"] == "retirement_requires_acknowledgement"
        assert warned["active_dispatch_resolution"]["dispatcher_pid"] == os.getpid()


def test_retirement_crash_prefixes_are_honest_and_retry_reuses_digest() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        root.mkdir()
        fleet_dir.mkdir()
        session = sessions.register_controller(root, "engine")["session"]
        messages.post_message(
            dispatch_id="retire-note",
            msg_type="controller-notice",
            payload={"text": "one snapshot"},
            messages_dir=messages_dir,
            addressee=messages.controller_addressee("engine", project_root=root),
        )
        with patch.object(sessions, "_write_session_map", side_effect=OSError("crash")):
            prefix = sessions.retire_controller(
                root,
                "engine",
                session_id=session["id"],
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
                ledger_records=[],
            )
        assert prefix["reason"] == "retirement_registry_write_failed"
        assert not messages.read_cursor_path(messages_dir).exists()
        retried = sessions.retire_controller(
            root,
            "engine",
            session_id=session["id"],
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            ledger_records=[],
        )
        assert retried["retired"] is True
        assert retried["digest"] == prefix["digest"]
        assert retried["digested_envelopes"] == 1

        other_root = base / "other"
        other_root.mkdir()
        other = sessions.register_controller(other_root, "other")["session"]
        messages.post_message(
            dispatch_id="other-note",
            msg_type="controller-notice",
            payload={"text": "cursor retry"},
            messages_dir=messages_dir,
            addressee=messages.controller_addressee("other", project_root=other_root),
        )
        with patch.object(
            messages,
            "finalize_controller_retirement_mailbox",
            side_effect=OSError("crash"),
        ):
            cursor_prefix = sessions.retire_controller(
                other_root,
                "other",
                session_id=other["id"],
                messages_dir=messages_dir,
                fleet_dir=fleet_dir,
                ledger_records=[],
            )
        assert cursor_prefix["retired"] is True
        assert cursor_prefix["cursor_finalized"] is False
        completed = sessions.retire_controller(
            other_root,
            "other",
            session_id=other["id"],
            messages_dir=messages_dir,
            fleet_dir=fleet_dir,
            ledger_records=[],
        )
        assert completed["already_retired"] is True
        assert completed["cursor_finalized"] is True
        assert completed["digest"] == cursor_prefix["digest"]


def test_clock_skew_is_bounded_and_live_pid_outweighs_old_wall_clock() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        future = sessions.register_controller(root, "future")["session"]
        _set_heartbeat(root, "future", "2099-01-01T00:00:00+00:00")
        roster = sessions.controller_roster(root, ledger_records=[])
        row = next(item for item in roster["controllers"] if item["label"] == "future")
        assert row["heartbeat_clock_state"] == "future-skew"
        assert row["idle"] == "clock skew: future heartbeat"
        joined = sessions.join_controller(root, "future")
        assert joined["joined"] is True
        assert joined["session"]["id"] != future["id"]

        beacon = _beacon()
        try:
            sessions.register_controller(root, "live-old", pid=beacon.pid)
            _set_heartbeat(root, "live-old", "2000-01-01T00:00:00+00:00")
            conflict = sessions.join_controller(root, "live-old")
            assert conflict["reason"] == "controller_label_conflict"
            assert conflict["incarnation_state"] == "live-pid"
        finally:
            beacon.terminate()
            beacon.wait(timeout=10)


def test_roster_and_join_report_measured_conflict_counts() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        first = _beacon()
        second = _beacon()
        try:
            sessions.claim_session(root, pid=first.pid, label="shared")
            sessions.claim_session(root, pid=second.pid, label="shared")
            roster = sessions.controller_roster(root, ledger_records=[])
            row = next(item for item in roster["controllers"] if item["label"] == "shared")
            assert row["conflicting_beacons"] == 2
            assert "conflict 2" in sessions.controller_roster_lines(roster)[0]
            conflict = sessions.join_controller(root, "shared")
            assert conflict["conflicting_beacons"] == 2
        finally:
            first.terminate()
            second.terminate()
            first.wait(timeout=10)
            second.wait(timeout=10)


def test_repo_name_startup_registers_then_joins_idempotently() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo-name"
        root.mkdir()
        beacon = _beacon()
        try:
            first = sessions.claim_controller_startup(root, pid=beacon.pid, environ={})
            assert first["claimed"] is True
            # A single startup must register the repo name on its own; the
            # second invocation joins it rather than healing a missing record.
            assert _registry(root, root.name)["id"] == first["session"]["id"]
            assert sessions.registered_controller_labels(root) == {root.name}
            relabel = sessions.register_controller(root, "different-name", pid=beacon.pid)
            second = sessions.claim_controller_startup(root, pid=beacon.pid, environ={})
            assert relabel["reason"] == "controller_label_mismatch"
            assert second["claimed"] is True
            assert first["session"]["id"] == second["session"]["id"]
            registry = _registry(root, root.name)
            assert registry["id"] == first["session"]["id"]
            assert sessions.registered_controller_labels(root) == {root.name}
        finally:
            beacon.terminate()
            beacon.wait(timeout=10)


def test_register_join_claim_dispatch_and_listener_touch_heartbeat() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        registered = sessions.register_controller(root, "heartbeat-only")
        assert registered["session"]["heartbeat_at"]
        _set_heartbeat(root, "heartbeat-only", "2000-01-01T00:00:00+00:00")
        stale_args = argparse.Namespace(
            controller_label="heartbeat-only",
            controller_beacon_pid=None,
            controller_session_id=None,
            _controller_beacon_pid=None,
        )
        dispatch._stamp_controller_session(stale_args, root)
        assert dispatch._controller_session_id(stale_args) is None
        joined = sessions.join_controller(root, "heartbeat-only")
        join_heartbeat = joined["session"]["heartbeat_at"]
        assert join_heartbeat != "2000-01-01T00:00:00+00:00"

        args = argparse.Namespace(
            controller_label="heartbeat-only",
            controller_beacon_pid=None,
            controller_session_id=None,
            _controller_beacon_pid=None,
        )
        dispatch._stamp_controller_session(args, root)
        dispatch_heartbeat = _registry(root, "heartbeat-only")["heartbeat_at"]
        assert dispatch._controller_session_id(args) == joined["session"]["id"]
        assert dispatch._controller_pid(args) is None
        assert dispatch._controller_label(args) == "heartbeat-only"
        assert dispatch_heartbeat >= join_heartbeat

        resolved = messages._resolve_listener_session_id(root, joined["session"]["id"])
        listener_heartbeat = _registry(root, "heartbeat-only")["heartbeat_at"]
        assert resolved == joined["session"]["id"]
        assert listener_heartbeat >= dispatch_heartbeat

        beacon = _beacon()
        try:
            claim = sessions.claim_session(root, pid=beacon.pid, label="pid-controller")
            _set_heartbeat(root, "pid-controller", "2000-01-01T00:00:00+00:00")
            refreshed = sessions.claim_session(root, pid=beacon.pid, label="pid-controller")
            assert refreshed["id"] == claim["id"]
            assert refreshed["heartbeat_at"] != "2000-01-01T00:00:00+00:00"
            assert _registry(root, "pid-controller")["heartbeat_at"] == refreshed["heartbeat_at"]
        finally:
            beacon.terminate()
            beacon.wait(timeout=10)


def test_registry_cli_exposes_all_four_verbs_and_json_roster() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        registered = _run_status_cli(base, root, "--register", "battery-side")
        assert registered.returncode == 0
        assert json.loads(registered.stdout)["registered"] is True
        listed = _run_status_cli(base, root, "--list-controllers", "--json")
        assert listed.returncode == 0
        assert json.loads(listed.stdout)["controllers"][0]["label"] == "battery-side"
        conflict = _run_status_cli(base, root, "--join", "battery-side")
        assert conflict.returncode == 2
        assert json.loads(conflict.stdout)["reason"] == "controller_label_conflict"
        joined = _run_status_cli(
            base,
            root,
            "--join",
            "battery-side",
            "--acknowledge-controller-conflict",
        )
        assert joined.returncode == 0
        joined_payload = json.loads(joined.stdout)
        assert joined_payload["joined"] is True
        retired = _run_status_cli(
            base,
            root,
            "--retire",
            "battery-side",
            "--controller-session-id",
            joined_payload["session"]["id"],
        )
        assert retired.returncode == 0
        assert json.loads(retired.stdout)["retired"] is True
        default_roster = json.loads(
            _run_status_cli(base, root, "--list-controllers", "--json").stdout
        )
        assert default_roster["controllers"] == []
        retired_roster = json.loads(
            _run_status_cli(
                base,
                root,
                "--list-controllers",
                "--include-retired",
                "--json",
            ).stdout
        )
        assert retired_roster["controllers"][0]["retired"] is True
