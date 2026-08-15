#!/usr/bin/env python3
"""Journal-backed controller lease and roster contracts."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key, value in {
        "GOALFLIGHT_TASK_STORE_DIR": tmp_path / "task-store",
        "GOALFLIGHT_JOURNAL_DIR": tmp_path / "journal",
        "GOALFLIGHT_MESSAGES_DIR": tmp_path / "messages",
        "GOALFLIGHT_STATE_DIR": tmp_path / "state",
        "GOALFLIGHT_WAKE_LEDGER_DIR": tmp_path / "wake-ledger",
        "GOAL_FLIGHT_PIDFILE_DIR": tmp_path / "pidfiles",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_register_conflict_and_explicit_join_create_one_active_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    first = sessions.register_controller(root, "engine", session_id="first-nonce")
    assert first["registered"] is True
    holder = wake.register_lease_holder(
        root,
        controller_label="engine",
        lease_nonce=first["session"]["lease_nonce"],
    )
    try:
        conflict = sessions.register_controller(root, "engine", session_id="second-nonce")
        assert conflict["registered"] is False
        assert conflict["reason"] == "label_in_use"
        joined = sessions.join_controller(
            root,
            "engine",
            session_id="second-nonce",
            acknowledge_conflict=True,
        )
    finally:
        holder.close()
    assert joined["joined"] is True and joined["succession"] is True
    authority = journal.Journal(root)
    assert authority.active_lease("engine").nonce == "second-nonce"
    assert sum(row["state"] == "ACTIVE" for row in authority.lease_records(include_ended=True)) == 1


def test_production_register_requires_a_session_beacon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    result = sessions.register_controller(
        root,
        "engine",
        session_id="engine-nonce",
        hold_lock=True,
    )
    assert result == {"registered": False, "reason": "missing_session_beacon"}
    assert not journal.resolve_journal_path(root).exists()


def test_roster_unread_is_journal_cursor_derived(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    registered = sessions.register_controller(root, "engine", session_id="engine-nonce")
    assert registered["registered"] is True
    messages.post_message(
        dispatch_id="roster-mail",
        msg_type="controller-notice",
        payload={"text": "pending"},
        messages_dir=Path(os.environ["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("engine", project_root=root),
    )
    roster = sessions.controller_roster(root, ledger_records=[])
    assert roster["controllers"][0]["unread_addressed_mail"] == 1
    authority = journal.Journal(root)
    lease = authority.active_lease("engine")
    peek = authority.cursor_peek("engine", nonce=lease.nonce, limit=10)
    assert authority.advance_cursor(
        "engine",
        nonce=lease.nonce,
        expected_cursor_version=peek.cursor_version,
        expected_stream_snapshots=peek.stream_snapshots,
        advances={"roster-mail": 1},
        actor="engine",
    ).committed
    assert (
        sessions.controller_roster(root, ledger_records=[])["controllers"][0][
            "unread_addressed_mail"
        ]
        == 0
    )


def test_retirement_releases_lease_without_legacy_mail_cursor_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _isolate(monkeypatch, tmp_path)
    registered = sessions.register_controller(root, "engine", session_id="engine-nonce")
    assert registered["registered"] is True
    result = sessions.retire_controller(
        root,
        "engine",
        session_id="engine-nonce",
        acknowledge=True,
        ledger_records=[],
    )
    assert result["retired"] is True
    assert journal.Journal(root).active_lease("engine") is None
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"] )
    assert not (messages_dir / ".read-cursor.json").exists()
    assert not (messages_dir / ".ack-cursor.json").exists()
