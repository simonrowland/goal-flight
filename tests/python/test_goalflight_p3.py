#!/usr/bin/env python3
"""P3 lease, cursor-listener, coverage, attention, and auto-claim contracts."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import threading

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_status as status  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _set_state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    values = {
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
        "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journal-state"),
        "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
        "GOALFLIGHT_STATE_DIR": str(tmp_path / "dispatch-state"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
        "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
        "GOALFLIGHT_TEST_MODE": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    env = os.environ.copy()
    env.update(values)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SCRIPTS), str(ROOT), env.get("PYTHONPATH", "")]
    )
    return env


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "sandbox-project"
    project.mkdir()
    return project


def _principal(pid: int, token: str) -> dict[str, object]:
    return {"pid": pid, "start_token": token, "hostname": "test-host"}


def _claim(authority: journal.Journal, label: str = "controller") -> journal.LeaseIdentity:
    result = authority.claim_or_renew_lease(
        label,
        principal=_principal(41001, "start-a"),
    )
    assert result.committed and result.value is not None, result.reason
    return result.value


def test_current_epoch_missing_p3_table_self_heals_but_corruption_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    with sqlite3.connect(authority.path) as connection:
        epochs = tuple(
            connection.execute(
                """SELECT schema_epoch, protocol_epoch, registry_epoch,
                          minimum_reader_epoch, minimum_writer_epoch
                   FROM journal_epochs WHERE singleton = 1"""
            ).fetchone()
        )
        assert epochs == (3, 3, 3, 3, 3)
        connection.execute("DROP TABLE journal_secrets")

    reopened = journal.Journal(project)
    secret_rows = reopened.read_all(
        "SELECT length(cursor_token_secret) AS secret_length FROM journal_secrets"
    )
    assert len(secret_rows) == 1 and secret_rows[0]["secret_length"] == 64

    corrupt_project = tmp_path / "corrupt-project"
    corrupt_project.mkdir()
    corrupt_path = journal.resolve_journal_path(corrupt_project)
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"not a sqlite database")
    with pytest.raises(journal.JournalIntegrityError, match="integrity check failed"):
        journal.Journal(corrupt_project)


def test_current_epoch_structurally_wrong_p3_table_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    with sqlite3.connect(authority.path) as connection:
        connection.execute("DROP TABLE journal_secrets")
        connection.execute(
            "CREATE TABLE journal_secrets (singleton INTEGER PRIMARY KEY)"
        )
    with pytest.raises(
        journal.JournalIntegrityError,
        match="structurally invalid tables: journal_secrets",
    ):
        journal.Journal(project)

    mixed_project = tmp_path / "mixed-incomplete-project"
    mixed_project.mkdir()
    mixed = journal.open_or_create_journal(mixed_project)
    with sqlite3.connect(mixed.path) as connection:
        connection.execute("DROP TABLE journal_secrets")
        connection.execute("DROP TABLE listener_coverage")
        connection.execute(
            "CREATE TABLE journal_secrets (singleton INTEGER PRIMARY KEY)"
        )
    with pytest.raises(
        journal.JournalIntegrityError,
        match="structurally invalid tables: journal_secrets",
    ):
        journal.Journal(mixed_project)


def test_one_active_lease_generation_and_live_different_claim_never_steals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(_project(tmp_path))
    first = _claim(authority)
    renewed = authority.claim_or_renew_lease(
        "controller",
        principal=_principal(41001, "start-a"),
        nonce=first.nonce,
    )
    assert renewed.committed and renewed.value is not None
    assert renewed.value.generation == first.generation
    assert renewed.value.nonce == first.nonce
    assert renewed.value.principal == first.principal
    assert renewed.value.renew_deadline_at >= first.renew_deadline_at

    renewed_without_capability = authority.claim_or_renew_lease(
        "controller",
        principal=_principal(41001, "start-a"),
    )
    assert renewed_without_capability.committed
    assert renewed_without_capability.value is not None
    assert renewed_without_capability.value.generation == first.generation
    assert renewed_without_capability.value.nonce == first.nonce

    refused = authority.claim_or_renew_lease(
        "controller",
        principal=_principal(41002, "start-b"),
        nonce=first.nonce,
    )
    assert refused.cas_lost
    assert "label in use" in str(refused.reason)
    assert authority.active_lease("controller") == renewed_without_capability.value

    takeover = authority.claim_or_renew_lease(
        "controller",
        principal=_principal(41002, "start-b"),
        takeover=True,
    )
    assert takeover.committed and takeover.value is not None
    assert takeover.value.generation == first.generation + 1
    active_rows = [
        row for row in authority.lease_records(include_ended=True) if row["state"] == "ACTIVE"
    ]
    assert len(active_rows) == 1
    ended = next(
        row
        for row in authority.lease_records(include_ended=True)
        if row["generation"] == first.generation
    )
    assert ended["state"] == "SUPERSEDED"
    assert ended["ended_reason"] == "explicit-takeover"
    assert ended["ended_at"] is not None


def test_cursor_cas_bounded_batch_and_rearm_delivers_remainder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority)
    messages_dir = tmp_path / "messages"
    for index in range(3):
        messages.post_message(
            dispatch_id="controller-mail",
            msg_type="controller-notice",
            payload={"text": f"message {index}"},
            messages_dir=messages_dir,
            source={"node": "test-node", "adapter": "test", "transport": "controller"},
            addressee=messages.controller_addressee("controller", project_root=project),
        )

    first = authority.cursor_batch("controller", nonce=lease.nonce, limit=2)
    assert len(first.items) == 2
    assert first.more_pending is True
    assert first.wake_pending is True
    tampered = first.token[:-1] + ("A" if first.token[-1] != "A" else "B")
    with pytest.raises(ValueError, match="cursor token is corrupt"):
        authority.advance_cursor(tampered, actor="test-controller")
    advanced = authority.advance_cursor(first.token, actor="test-controller")
    assert advanced.committed

    stale = authority.advance_cursor(first.token, actor="test-controller")
    assert stale.cas_lost
    cursor = authority.cursor_status("controller")
    assert cursor is not None and cursor["positions"] == {"controller-mail": 2}

    second = authority.cursor_batch("controller", nonce=lease.nonce, limit=2)
    assert [row["stream_seq"] for row in second.items] == [3]
    assert second.more_pending is False
    assert authority.advance_cursor(second.token, actor="test-controller").committed
    assert authority.pending_delivery_events("controller", waking_only=False) == []

    original = messages.post_message(
        dispatch_id="replace-stream",
        msg_type="controller-notice",
        payload={"text": "old"},
        messages_dir=messages_dir,
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("controller", project_root=project),
        seq=1,
    )
    later = messages.post_message(
        dispatch_id="replace-stream",
        msg_type="controller-notice",
        payload={"text": "later"},
        messages_dir=messages_dir,
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("controller", project_root=project),
        seq=2,
    )
    replacement = messages.post_message(
        dispatch_id="replace-stream",
        msg_type="controller-notice",
        payload={"text": "new"},
        messages_dir=messages_dir,
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("controller", project_root=project),
        seq=1,
        replace_if=lambda envelope: envelope["id"] == original["envelope"]["id"],
    )
    replacement_rows = [
        row
        for row in authority.pending_delivery_events("controller", waking_only=False)
        if row["stream_id"] == "replace-stream"
    ]
    assert {row["event_uuid"] for row in replacement_rows} == {
        replacement["envelope"]["id"],
        later["envelope"]["id"],
    }
    replacement_batch = authority.cursor_batch("controller", nonce=lease.nonce, limit=1)
    assert [row["stream_seq"] for row in replacement_batch.items] == [1]
    assert replacement_batch.more_pending is True
    assert authority.advance_cursor(replacement_batch.token, actor="test-controller").committed
    later_batch = authority.cursor_batch("controller", nonce=lease.nonce, limit=1)
    assert [row["stream_seq"] for row in later_batch.items] == [2]
    assert authority.advance_cursor(later_batch.token, actor="test-controller").committed

    messages.post_message(
        dispatch_id="a-wake",
        msg_type="controller-notice",
        payload={"text": "wake before quiet remainder"},
        messages_dir=messages_dir,
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("controller", project_root=project),
    )
    messages.post_message(
        dispatch_id="z-quiet",
        msg_type="status",
        payload={"text": "quiet remainder", "project_root": str(project)},
        messages_dir=messages_dir,
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
    )
    backlog_first = authority.cursor_batch("controller", nonce=lease.nonce, limit=1)
    assert [row["stream_id"] for row in backlog_first.items] == ["a-wake"]
    assert backlog_first.more_pending is True
    backlog_advanced = authority.advance_cursor(
        backlog_first.token,
        actor="test-controller",
    )
    assert backlog_advanced.committed and backlog_advanced.value["more_pending"] is True
    backlog_second = authority.cursor_batch("controller", nonce=lease.nonce, limit=1)
    assert [row["stream_id"] for row in backlog_second.items] == ["z-quiet"]
    assert [row["wake_class"] for row in backlog_second.items] == ["quiet"]
    assert backlog_second.wake_pending is True


def test_cursor_cas_has_one_winner_under_32_way_contention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority)
    messages.post_message(
        dispatch_id="contended-stream",
        msg_type="controller-notice",
        payload={"text": "contended"},
        messages_dir=tmp_path / "messages",
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("controller", project_root=project),
    )
    token = authority.cursor_batch("controller", nonce=lease.nonce, limit=1).token
    barrier = threading.Barrier(32)

    def race(index: int):
        barrier.wait()
        return journal.Journal(project).advance_cursor(token, actor=f"contender-{index}")

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(race, range(32)))
    assert sum(result.committed for result in outcomes) == 1
    assert all(result.committed or result.cas_lost or result.retryable for result in outcomes)
    cursor = authority.cursor_status("controller")
    assert cursor is not None
    assert cursor["cursor_version"] == 1
    assert cursor["positions"] == {"contended-stream": 1}


def test_second_listener_supersedes_first_and_listener_never_renews_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(_project(tmp_path))
    lease = _claim(authority)
    before = authority.active_lease("controller")
    assert before is not None
    first = authority.arm_listener(
        "controller",
        nonce=lease.nonce,
        pid=42001,
        start_token="listener-a",
        parent_pid=41001,
    )
    second = authority.arm_listener(
        "controller",
        nonce=lease.nonce,
        pid=42002,
        start_token="listener-b",
        parent_pid=41001,
    )
    assert first.committed and first.value and second.committed and second.value
    first_row = authority.coverage(str(first.value["coverage_id"]))
    second_row = authority.coverage(str(second.value["coverage_id"]))
    assert first_row is not None
    assert first_row["state"] == "EXITED"
    assert first_row["exit_reason"] == "superseded"
    assert second_row is not None and second_row["state"] == "ARMED"
    after = authority.active_lease("controller")
    assert after is not None
    assert after.renewed_at == before.renewed_at
    assert after.renew_deadline_at == before.renew_deadline_at


def test_two_real_one_shot_listeners_supersede_and_record_both_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    env["GOALFLIGHT_TEST_LISTENER_START_TOKEN"] = "constructed-listener-token"
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority)
    before = authority.active_lease("controller")
    assert before is not None
    command = [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "--messages-dir",
        str(tmp_path / "messages"),
        "listen",
        "--project-root",
        str(project),
        "--controller-label",
        "controller",
        "--lease-nonce",
        lease.nonce,
        "--poll-secs",
        "0.01",
        "--timeout-s",
        "5",
        "--json",
    ]
    first = subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            row = authority.active_coverage("controller")
            if row is not None and row["pid"] == first.pid:
                break
            time.sleep(0.01)
        else:
            pytest.fail("first listener never armed coverage")

        second = subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first_stdout, first_stderr = first.communicate(timeout=3)
        assert first.returncode == 3, first_stderr
        assert json.loads(first_stdout)["reason"] == "superseded"

        messages.post_message(
            dispatch_id="listener-real",
            msg_type="controller-notice",
            payload={"text": "wake second listener"},
            messages_dir=tmp_path / "messages",
            source={"node": "test-node", "adapter": "test", "transport": "controller"},
            addressee=messages.controller_addressee("controller", project_root=project),
        )
        second_stdout, second_stderr = second.communicate(timeout=3)
        assert second.returncode == 0, second_stderr
        second_payload = json.loads(second_stdout)
        assert second_payload["reason"] == "batch"
        assert second_payload["count"] == 1
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=2)

    rows = authority.read_all(
        "SELECT state, exit_reason FROM listener_coverage ORDER BY armed_at, coverage_id"
    )
    assert sorted((row["state"], row["exit_reason"]) for row in rows) == [
        ("EXITED", "batch"),
        ("EXITED", "superseded"),
    ]
    after = authority.active_lease("controller")
    assert after is not None
    assert after.renewed_at == before.renewed_at
    assert after.renew_deadline_at == before.renew_deadline_at


def test_lease_death_attention_materializes_on_listener_and_holder_lock_sides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(_project(tmp_path))
    first = _claim(authority, "listener-side")
    assert authority.prepare_attempt("live-work").committed
    coverage = authority.arm_listener(
        "listener-side",
        nonce=first.nonce,
        pid=43001,
        start_token="listener-side-token",
        parent_pid=41001,
    )
    assert coverage.committed and coverage.value
    exited = authority.exit_listener(
        str(coverage.value["coverage_id"]),
        reason="orphaned",
    )
    assert exited.committed
    listener_items = authority.attention_items()
    assert len(listener_items) == 1
    assert listener_items[0]["trigger_side"] == "listener"

    second_result = authority.claim_or_renew_lease(
        "horizon-side",
        principal=_principal(41002, "start-b"),
        horizon_s=60,
    )
    assert second_result.committed and second_result.value is not None
    replacement = authority.claim_or_renew_lease(
        "horizon-side",
        principal=_principal(41003, "start-c"),
        incumbent_liveness=journal.LeaseLivenessEvidence(
            generation=second_result.value.generation,
            nonce=second_result.value.nonce,
            alive=False,
        ),
    )
    assert replacement.committed
    holder_lock_items = [
        row for row in authority.attention_items() if row["source_label"] == "horizon-side"
    ]
    assert len(holder_lock_items) == 1
    assert holder_lock_items[0]["trigger_side"] == "horizon"
    assert holder_lock_items[0]["reason"] == "holder-dead"


def test_orphaned_and_corrupt_self_checks_write_exit_rows_from_constructed_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(_project(tmp_path))
    lease = _claim(authority)

    orphan = authority.arm_listener(
        "controller",
        nonce=lease.nonce,
        pid=43101,
        start_token="orphan-token",
        parent_pid=41001,
    )
    assert orphan.committed and orphan.value
    orphan_row = authority.coverage(str(orphan.value["coverage_id"]))
    orphan_reason = journal.listener_exit_reason(
        orphan_row,
        lease.__dict__,
        current_parent_pid=41002,
        identity_matches=True,
    )
    assert orphan_reason == "orphaned"
    assert authority.exit_listener(
        str(orphan.value["coverage_id"]), reason=orphan_reason
    ).committed

    corrupt = authority.arm_listener(
        "controller",
        nonce=lease.nonce,
        pid=43102,
        start_token="corrupt-token",
        parent_pid=41001,
    )
    assert corrupt.committed and corrupt.value
    corrupt_row = authority.coverage(str(corrupt.value["coverage_id"]))
    corrupt_reason = journal.listener_exit_reason(
        corrupt_row,
        lease.__dict__,
        current_parent_pid=41001,
        identity_matches=False,
    )
    assert corrupt_reason == "corrupt"
    assert authority.exit_listener(
        str(corrupt.value["coverage_id"]), reason=corrupt_reason
    ).committed

    exits = {
        str(row["exit_reason"])
        for row in authority.read_all("SELECT exit_reason FROM listener_coverage")
    }
    assert exits == {"orphaned", "corrupt"}


def test_worktree_and_parent_share_one_lease_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    main = tmp_path / "main"
    worktree = tmp_path / "linked"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "P3 Test"], cwd=main, check=True)
    (main / "seed").write_text("seed\n")
    subprocess.run(["git", "add", "seed"], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=main, check=True)
    subprocess.run(["git", "worktree", "add", "-q", str(worktree)], cwd=main, check=True)

    parent_authority = journal.open_or_create_journal(main)
    lease = _claim(parent_authority)
    worktree_authority = journal.Journal(worktree)
    observed = worktree_authority.active_lease("controller")
    assert observed == lease
    assert worktree_authority.path == parent_authority.path


def test_auto_claim_is_controller_only_and_never_steals_live_label(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    identities = {
        44001: {"pid": 44001, "start_token": "controller-entry"},
        44002: {"pid": 44002, "start_token": "different-controller"},
    }
    monkeypatch.setattr(sessions, "_controller_process_identity", identities.get)

    claimed = sessions.claim_controller_startup(
        project,
        pid=44001,
        label="entry",
        role="controller",
    )
    assert claimed["claimed"] is True
    authority = journal.Journal(project)
    incumbent = authority.active_lease("entry")
    assert incumbent is not None
    holder = wake.register_lease_holder(
        project,
        controller_label="entry",
        lease_nonce=incumbent.nonce,
    )
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", incumbent.nonce)

    watchdog = sessions.claim_controller_startup(
        project,
        pid=44001,
        label="entry",
        role="watchdog",
    )
    assert watchdog["claimed"] is True
    assert watchdog["session"]["generation"] == incumbent.generation

    for role in ("listener", "drainer", "mirror", "dashboard"):
        skipped = sessions.claim_controller_startup(
            project,
            pid=44002,
            label=f"{role}-must-not-exist",
            role=role,
        )
        assert skipped == {"claimed": False, "reason": "role_does_not_claim", "role": role}
        assert authority.active_lease(f"{role}-must-not-exist") is None

    refused = sessions.claim_controller_startup(
        project,
        pid=44002,
        label="entry",
        role="controller",
    )
    assert refused["reason"] == "label_in_use"
    after_refusal = authority.active_lease("entry")
    assert after_refusal is not None
    assert after_refusal.generation == incumbent.generation
    assert after_refusal.nonce == incumbent.nonce
    assert after_refusal.principal == incumbent.principal
    holder.close()


def test_hidden_consumers_use_journal_and_relay_is_peek_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "hidden")
    authority = journal.open_or_create_journal(project)
    lease_result = authority.claim_or_renew_lease(
        "hidden",
        principal={"principal_id": "hidden-controller"},
    )
    assert lease_result.committed and lease_result.value is not None
    lease = lease_result.value
    messages.post_message(
        dispatch_id="hidden-stream",
        msg_type="controller-notice",
        payload={"text": "hidden consumer mail"},
        messages_dir=tmp_path / "messages",
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("hidden", project_root=project),
    )

    watermark = status._mail_watermark(str(project), ["hidden-stream"])
    assert watermark is not None and len(watermark) == 1
    unread, unread_error = sessions._addressed_unread_counts(project)
    assert unread_error is None and unread == {"hidden": 1}
    summary = messages.controller_mail_summary(task_store_project_root=project)
    assert summary["count"] == 1

    before = authority.cursor_status("hidden")
    monkeypatch.setattr(
        sessions,
        "claim_controller_startup",
        lambda *args, **kwargs: pytest.fail("peek-only relay attempted to claim a lease"),
    )
    assert messages.main(
        ["--messages-dir", str(tmp_path / "messages"), "relay", "--new"]
    ) == 0
    capsys.readouterr()
    assert authority.cursor_status("hidden") == before
    assert not (tmp_path / "messages" / ".read-cursor.json").exists()
    assert not (tmp_path / "messages" / ".ack-cursor.json").exists()
    assert not hasattr(messages, "load_read_cursor")

    batch = authority.cursor_batch("hidden", nonce=lease.nonce, limit=10)
    assert authority.advance_cursor(batch.token, actor="hidden-controller").committed
    assert status._mail_watermark(str(project), ["hidden-stream"]) == watermark
    unread_after, unread_after_error = sessions._addressed_unread_counts(project)
    assert unread_after_error is None and unread_after == {"hidden": 0}

    retired = sessions.retire_controller(
        project,
        "hidden",
        session_id=lease.nonce,
        acknowledge=True,
        ledger_records=[],
    )
    assert retired["retired"] is True
    assert authority.active_lease("hidden") is None
