#!/usr/bin/env python3
"""P3 lease, cursor-listener, coverage, attention, and auto-claim contracts."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import inspect
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
import threading
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_fleet_dispatch as fleet_dispatch  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
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
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp_path / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
        "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
        "GOALFLIGHT_TEST_MODE": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
    monkeypatch.delenv("GOALFLIGHT_PROCESS_ROLE", raising=False)
    env = os.environ.copy()
    env.update(values)
    env.pop("GOALFLIGHT_DISPATCH_ID", None)
    env.pop("GOALFLIGHT_PROCESS_ROLE", None)
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


def test_epoch_four_migration_is_race_safe_idempotent_and_corruption_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    env[journal.ALLOW_MIGRATION_ENV] = "1"
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    historical = authority.prepare_attempt("historical-unattributed-seat")
    assert historical.committed
    with sqlite3.connect(authority.path) as connection:
        epochs = tuple(
            connection.execute(
                """SELECT schema_epoch, protocol_epoch, registry_epoch,
                          minimum_reader_epoch, minimum_writer_epoch
                   FROM journal_epochs WHERE singleton = 1"""
            ).fetchone()
        )
        assert epochs == (6, 6, 6, 6, 6)
        connection.execute(
            """UPDATE journal_epochs
               SET schema_epoch = 4, protocol_epoch = 4, registry_epoch = 4,
                   minimum_reader_epoch = 4, minimum_writer_epoch = 4
               WHERE singleton = 1"""
        )
        connection.execute("DROP TABLE listener_coverage")
        connection.execute("DROP TABLE system_attention_items")
        connection.execute(
            "ALTER TABLE dispatch_attempts DROP COLUMN engine"
        )
        connection.execute(
            "ALTER TABLE dispatch_attempts DROP COLUMN effective_account"
        )
        connection.execute(
            "ALTER TABLE dispatch_attempts DROP COLUMN owner_session_digest"
        )
        connection.execute(
            "ALTER TABLE dispatch_attempts DROP COLUMN owner_controller_label"
        )
        connection.execute(
            "ALTER TABLE terminal_outbox DROP COLUMN projection_quarantined_at"
        )
        connection.execute("ALTER TABLE terminal_outbox DROP COLUMN projection_retry_at")

    open_code = (
        "from pathlib import Path; "
        "import goalflight_journal as journal; "
        f"journal.Journal(Path({str(project)!r})); "
        "print('opened')"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", open_code],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        results = [process.communicate(timeout=10) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
    for process, (stdout, stderr) in zip(processes, results, strict=True):
        assert process.returncode == 0, stderr
        assert stdout.strip() == "opened"

    reopened = journal.Journal(project)
    journal.Journal(project)
    assert reopened.read_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'listener_coverage'"
    )
    assert reopened.read_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'system_attention_items'"
    )
    assert tuple(
        str(row["name"]) for row in reopened.read_all("PRAGMA table_info(terminal_outbox)")
    ) == journal.CURRENT_SCHEMA_COLUMNS["terminal_outbox"]
    assert tuple(
        str(row["name"])
        for row in reopened.read_all("PRAGMA table_info(dispatch_attempts)")
    ) == journal.CURRENT_SCHEMA_COLUMNS["dispatch_attempts"]
    historical_row = reopened.read_all(
        """SELECT effective_account, engine FROM dispatch_attempts
           WHERE dispatch_id = 'historical-unattributed-seat'"""
    )[0]
    assert historical_row["effective_account"] is None
    assert historical_row["engine"] is None
    assert reopened.read_all(
        """SELECT COUNT(*) AS marker_count FROM journal_migrations
           WHERE migration_id = 'dispatch-attempt-owner-v1'"""
    )[0]["marker_count"] == 1
    assert reopened.read_all(
        """SELECT COUNT(*) AS marker_count FROM journal_migrations
           WHERE migration_id = 'dispatch-attempt-seat-attribution-v1'"""
    )[0]["marker_count"] == 1
    assert tuple(
        reopened.read_all(
            """SELECT schema_epoch, protocol_epoch, registry_epoch,
                      minimum_reader_epoch, minimum_writer_epoch
               FROM journal_epochs WHERE singleton = 1"""
        )[0]
    ) == (6, 6, 6, 6, 6)

    corrupt_project = tmp_path / "corrupt-project"
    corrupt_project.mkdir()
    corrupt_path = journal.resolve_journal_path(corrupt_project)
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"not a sqlite database")
    with pytest.raises(journal.JournalIntegrityError, match="integrity check failed"):
        journal.Journal(corrupt_project)


def test_older_journal_requires_explicit_migration_without_mutating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    monkeypatch.delenv(journal.ALLOW_MIGRATION_ENV, raising=False)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            """UPDATE journal_epochs
               SET schema_epoch = 4, protocol_epoch = 4, registry_epoch = 4,
                   minimum_reader_epoch = 4, minimum_writer_epoch = 4
               WHERE singleton = 1"""
        )
        expected_epochs = tuple(
            connection.execute(
                """SELECT schema_epoch, protocol_epoch, registry_epoch,
                          minimum_reader_epoch, minimum_writer_epoch
                   FROM journal_epochs WHERE singleton = 1"""
            ).fetchone()
        )
    before_mtime = authority.path.stat().st_mtime_ns
    before_bytes = authority.path.read_bytes()

    with pytest.raises(
        journal.JournalUpgradeRequired,
        match=r"UPGRADE_REQUIRED:.*goalflight_journal\.py.*migrate",
    ):
        journal.Journal(project)

    with sqlite3.connect(authority.path) as connection:
        actual_epochs = tuple(
            connection.execute(
                """SELECT schema_epoch, protocol_epoch, registry_epoch,
                          minimum_reader_epoch, minimum_writer_epoch
                   FROM journal_epochs WHERE singleton = 1"""
            ).fetchone()
        )
    assert actual_epochs == expected_epochs == (4, 4, 4, 4, 4)
    assert authority.path.stat().st_mtime_ns == before_mtime
    assert authority.path.read_bytes() == before_bytes
    assert journal.main(["--project-root", str(project), "migrate"]) == 0
    assert journal.Journal(project).epochs() == journal.JournalEpochs(6, 6, 6, 6, 6)


def test_explicit_migration_runs_once_and_status_mixed_epoch_walk_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    monkeypatch.delenv(journal.ALLOW_MIGRATION_ENV, raising=False)
    old_project = _project(tmp_path)
    current_project = tmp_path / "current-project"
    current_project.mkdir()
    old_authority = journal.open_or_create_journal(old_project)
    current_authority = journal.open_or_create_journal(current_project)
    for authority, dispatch_id in (
        (old_authority, "old-epoch-dispatch"),
        (current_authority, "current-epoch-dispatch"),
    ):
        prepared = authority.prepare_attempt(dispatch_id)
        assert prepared.committed
    with sqlite3.connect(old_authority.path) as connection:
        connection.execute(
            """UPDATE journal_epochs
               SET schema_epoch = 4, protocol_epoch = 4, registry_epoch = 4,
                   minimum_reader_epoch = 4, minimum_writer_epoch = 4
               WHERE singleton = 1"""
        )

    migrated = journal.Journal(old_project, allow_migration=True)
    assert migrated.epochs() == journal.JournalEpochs(6, 6, 6, 6, 6)
    first_markers = migrated.read_all(
        """SELECT migration_id, COUNT(*) AS count
           FROM journal_migrations GROUP BY migration_id ORDER BY migration_id"""
    )
    journal.Journal(old_project, allow_migration=True)
    assert migrated.read_all(
        """SELECT migration_id, COUNT(*) AS count
           FROM journal_migrations GROUP BY migration_id ORDER BY migration_id"""
    ) == first_markers

    # Return one member of the aggregate set to an old epoch. The status walk
    # must use Journal.open_reader: replacing it with Journal(...) would trip
    # the constructor mutation below before it could inspect either project.
    with sqlite3.connect(old_authority.path) as connection:
        connection.execute(
            """UPDATE journal_epochs
               SET schema_epoch = 4, protocol_epoch = 4, registry_epoch = 4,
                   minimum_reader_epoch = 4, minimum_writer_epoch = 4
               WHERE singleton = 1"""
        )
    def fingerprint(authority: journal.Journal) -> tuple[int, bytes, tuple[int, ...]]:
        with sqlite3.connect(authority.path) as connection:
            epochs = tuple(
                connection.execute(
                    """SELECT schema_epoch, protocol_epoch, registry_epoch,
                              minimum_reader_epoch, minimum_writer_epoch
                       FROM journal_epochs WHERE singleton = 1"""
                ).fetchone()
            )
        return authority.path.stat().st_mtime_ns, authority.path.read_bytes(), epochs

    before = {
        authority.path: fingerprint(authority)
        for authority in (old_authority, current_authority)
    }

    def ordinary_open_forbidden(*_args, **_kwargs):
        raise AssertionError("aggregate walk used a migration-capable Journal open")

    monkeypatch.setattr(journal.Journal, "__init__", ordinary_open_forbidden)
    rows = status._wait_authority_rows(
        ["old-epoch-dispatch", "current-epoch-dispatch"],
        {
            "old-epoch-dispatch": {"project_root": str(old_project)},
            "current-epoch-dispatch": {"project_root": str(current_project)},
        },
        project_root=None,
        journal_cache={},
    )
    assert rows["old-epoch-dispatch"] == {"_wait_journal_error": True}
    assert rows["current-epoch-dispatch"]["lifecycle_state"] == "PREPARED"
    assert messages.controller_mail_summary(
        task_store_project_root=old_project
    ) == {}
    for path, fingerprint in before.items():
        with sqlite3.connect(path) as connection:
            epochs = tuple(
                connection.execute(
                    """SELECT schema_epoch, protocol_epoch, registry_epoch,
                              minimum_reader_epoch, minimum_writer_epoch
                       FROM journal_epochs WHERE singleton = 1"""
                ).fetchone()
            )
        assert (path.stat().st_mtime_ns, path.read_bytes(), epochs) == fingerprint


def test_epoch_six_fences_epoch_five_reader_writer_before_schema_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            "ALTER TABLE dispatch_attempts DROP COLUMN engine"
        )
        connection.execute(
            "ALTER TABLE dispatch_attempts DROP COLUMN effective_account"
        )

    epoch_five_client = journal.ClientEpochs(
        schema=5,
        protocol=5,
        registry=5,
        reader=5,
        writer=5,
    )
    fence_match = r"UPGRADE_REQUIRED:.*schema client=5 journal=6"
    with pytest.raises(
        journal.JournalUpgradeRequired,
        match=fence_match,
    ) as direct_refusal:
        journal.Journal(project, client_epochs=epoch_five_client)
    assert not isinstance(direct_refusal.value, journal.JournalIntegrityError)

    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            """UPDATE journal_epochs
               SET schema_epoch = 5, protocol_epoch = 5, registry_epoch = 5,
                   minimum_reader_epoch = 5, minimum_writer_epoch = 5
               WHERE singleton = 1"""
        )

    old_client = journal.Journal(
        project,
        client_epochs=epoch_five_client,
        allow_migration=True,
    )
    migrated = journal.Journal(project, allow_migration=True)
    assert migrated.epochs() == journal.JournalEpochs(6, 6, 6, 6, 6)

    with pytest.raises(journal.JournalUpgradeRequired, match=fence_match):
        journal.Journal(project, client_epochs=epoch_five_client)
    with pytest.raises(journal.JournalUpgradeRequired, match=fence_match):
        old_client.read_all("SELECT attempt_id FROM dispatch_attempts")
    with pytest.raises(journal.JournalUpgradeRequired, match=fence_match):
        old_client.write(
            journal.RowOperation.update(
                "journal_epochs",
                {"updated_at": journal.utc_now()},
                where={"singleton": 1},
                row_cap=1,
            )
        )


def test_prepare_attempt_records_immutable_digested_owner_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(_project(tmp_path))
    owner_label = "controller-label-verbatim-" + ("x" * 70)
    raw_nonce = "raw-controller-capability-" + ("n" * 96)
    prepared = authority.prepare_attempt(
        "owned-at-prepare",
        owner_controller_label=owner_label,
        owner_session_nonce=raw_nonce,
    )
    assert prepared.committed and prepared.value is not None
    row = authority.read_all(
        """SELECT owner_controller_label, owner_session_digest
           FROM dispatch_attempts WHERE dispatch_id = ?""",
        ("owned-at-prepare",),
    )[0]
    assert row["owner_controller_label"] == owner_label
    assert row["owner_session_digest"] == wake.controller_session_digest(raw_nonce)
    assert raw_nonce not in tuple(row)

    replay = authority.prepare_attempt(
        "owned-at-prepare",
        owner_controller_label=owner_label,
        owner_session_nonce=raw_nonce,
    )
    assert replay.committed and replay.value == prepared.value
    conflicting = authority.prepare_attempt(
        "owned-at-prepare",
        owner_controller_label="different-owner",
        owner_session_nonce="different-capability",
    )
    assert conflicting.cas_lost


def test_prepare_attempt_records_effective_seat_without_defaulting_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(_project(tmp_path))
    waiting = authority.prepare_attempt(
        "seat-at-prepare",
        defer_start_deadline=True,
        engine="codex",
    )
    assert waiting.committed and waiting.value is not None
    waiting_row = authority.read_all(
        """SELECT effective_account, engine FROM dispatch_attempts
           WHERE dispatch_id = ?""",
        ("seat-at-prepare",),
    )[0]
    assert waiting_row["effective_account"] is None
    assert waiting_row["engine"] == "codex"

    resolved = authority.prepare_attempt(
        "seat-at-prepare",
        effective_account="served-seat",
        engine="codex",
    )
    assert resolved.committed and resolved.value == waiting.value
    resolved_row = authority.read_all(
        """SELECT effective_account, engine FROM dispatch_attempts
           WHERE dispatch_id = ?""",
        ("seat-at-prepare",),
    )[0]
    assert resolved_row["effective_account"] == "served-seat"
    assert resolved_row["engine"] == "codex"

    conflict = authority.prepare_attempt(
        "seat-at-prepare",
        effective_account="different-seat",
        engine="codex",
    )
    assert conflict.cas_lost

    unattributed = authority.prepare_attempt(
        "seat-not-determined",
        effective_account="",
        engine="worker",
    )
    assert unattributed.committed
    unattributed_row = authority.read_all(
        """SELECT effective_account, engine FROM dispatch_attempts
           WHERE dispatch_id = ?""",
        ("seat-not-determined",),
    )[0]
    assert unattributed_row["effective_account"] is None
    assert unattributed_row["engine"] == "worker"

    missing_engine = authority.prepare_attempt(
        "seat-without-engine",
        effective_account="ambiguous-seat",
    )
    assert missing_engine.committed
    missing_engine_row = authority.read_all(
        """SELECT effective_account, engine FROM dispatch_attempts
           WHERE dispatch_id = ?""",
        ("seat-without-engine",),
    )[0]
    assert missing_engine_row["effective_account"] is None
    assert missing_engine_row["engine"] is None


def test_current_epoch_structurally_wrong_table_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    with sqlite3.connect(authority.path) as connection:
        connection.execute("DROP TABLE listener_coverage")
        connection.execute(
            "CREATE TABLE listener_coverage (coverage_id TEXT PRIMARY KEY)"
        )
    with pytest.raises(
        journal.JournalIntegrityError,
        match="structurally invalid tables: listener_coverage",
    ):
        journal.Journal(project)

    mixed_project = tmp_path / "mixed-incomplete-project"
    mixed_project.mkdir()
    mixed = journal.open_or_create_journal(mixed_project)
    with sqlite3.connect(mixed.path) as connection:
        connection.execute("DROP TABLE listener_coverage")
        connection.execute(
            "CREATE TABLE listener_coverage (coverage_id TEXT PRIMARY KEY)"
        )
    with pytest.raises(
        journal.JournalIntegrityError,
        match="structurally invalid tables: listener_coverage",
    ):
        journal.Journal(mixed_project)


def test_epoch_three_migration_deletes_cursor_token_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            """CREATE TABLE journal_secrets (
                   singleton INTEGER PRIMARY KEY,
                   cursor_token_secret TEXT NOT NULL,
                   created_at TEXT NOT NULL
               )"""
        )
        connection.execute(
            "INSERT INTO journal_secrets VALUES (1, ?, ?)",
            ("a" * 64, journal.utc_now()),
        )
        connection.execute(
            """UPDATE journal_epochs SET schema_epoch = 3, protocol_epoch = 3,
                   registry_epoch = 3, minimum_reader_epoch = 3,
                   minimum_writer_epoch = 3 WHERE singleton = 1"""
        )
    migrated = journal.Journal(project, allow_migration=True)
    assert migrated.read_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'journal_secrets'"
    ) == []


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


def test_cursor_peek_and_server_validated_cas_deliver_remainder(
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

    first = authority.cursor_peek("controller", nonce=lease.nonce, limit=2)
    assert len(first.items) == 2
    fabricated = authority.advance_cursor(
        "controller",
        nonce=lease.nonce,
        expected_cursor_version=first.cursor_version,
        expected_stream_snapshots=first.stream_snapshots,
        advances={"controller-mail": 999},
        actor="test-controller",
    )
    assert fabricated.cas_lost
    advanced = authority.advance_cursor(
        "controller",
        nonce=lease.nonce,
        expected_cursor_version=first.cursor_version,
        expected_stream_snapshots=first.stream_snapshots,
        advances={"controller-mail": 2},
        actor="test-controller",
    )
    assert advanced.committed

    stale = authority.advance_cursor(
        "controller",
        nonce=lease.nonce,
        expected_cursor_version=first.cursor_version,
        expected_stream_snapshots=first.stream_snapshots,
        advances={"controller-mail": 2},
        actor="test-controller",
    )
    assert stale.cas_lost
    cursor = authority.cursor_status("controller")
    assert cursor is not None and cursor["positions"] == {"controller-mail": 2}

    second = authority.cursor_peek("controller", nonce=lease.nonce, limit=2)
    assert [row["stream_seq"] for row in second.items] == [3]
    assert authority.advance_cursor(
        "controller",
        nonce=lease.nonce,
        expected_cursor_version=second.cursor_version,
        expected_stream_snapshots=second.stream_snapshots,
        advances={"controller-mail": 3},
        actor="test-controller",
    ).committed
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
    assert replacement["envelope"]["seq"] == 3
    replacement_peek = authority.cursor_peek("controller", nonce=lease.nonce, limit=1)
    assert [row["stream_seq"] for row in replacement_peek.items] == [2]
    assert authority.advance_cursor(
        "controller",
        nonce=lease.nonce,
        expected_cursor_version=replacement_peek.cursor_version,
        expected_stream_snapshots=replacement_peek.stream_snapshots,
        advances={"replace-stream": 2},
        actor="test-controller",
    ).committed
    later_peek = authority.cursor_peek("controller", nonce=lease.nonce, limit=1)
    assert [row["stream_seq"] for row in later_peek.items] == [3]
    assert authority.advance_cursor(
        "controller",
        nonce=lease.nonce,
        expected_cursor_version=later_peek.cursor_version,
        expected_stream_snapshots=later_peek.stream_snapshots,
        advances={"replace-stream": 3},
        actor="test-controller",
    ).committed

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
    backlog_first = authority.cursor_peek("controller", nonce=lease.nonce, limit=1)
    assert [row["stream_id"] for row in backlog_first.items] == ["a-wake"]
    backlog_advanced = authority.advance_cursor(
        "controller",
        nonce=lease.nonce,
        expected_cursor_version=backlog_first.cursor_version,
        expected_stream_snapshots=backlog_first.stream_snapshots,
        advances={"a-wake": 1},
        actor="test-controller",
    )
    assert backlog_advanced.committed
    backlog_second = authority.cursor_peek("controller", nonce=lease.nonce, limit=1)
    assert [row["stream_id"] for row in backlog_second.items] == ["z-quiet"]
    assert [row["wake_class"] for row in backlog_second.items] == ["quiet"]
    assert backlog_second.items


def test_explicit_sequence_admission_is_monotonic_per_stream_mutation_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def run_case(case: str, *, mutate_admission: bool) -> None:
        case_root = tmp_path / case
        case_root.mkdir()
        with monkeypatch.context() as case_patch:
            _set_state_env(case_patch, case_root)
            if mutate_admission:
                case_patch.setattr(
                    messages,
                    "_admit_stream_seq",
                    lambda *, provided_seq, envelopes: (
                        provided_seq
                        if provided_seq is not None
                        else max((int(row["seq"]) for row in envelopes), default=0) + 1
                    ),
                )
            project = _project(case_root)
            authority = journal.open_or_create_journal(project)
            lease = _claim(authority)
            messages_dir = case_root / "messages"
            addressee = messages.controller_addressee("controller", project_root=project)

            def mcp_post(dispatch_id: str, seq: int, text: str) -> dict:
                return messages.goalflight_post_message_tool(
                    {
                        "dispatch_id": dispatch_id,
                        "type": "controller-notice",
                        "payload": {"text": text},
                        "source": {
                            "node": "test-node",
                            "adapter": "mcp",
                            "transport": "controller",
                        },
                        "addressee": addressee,
                        "seq": seq,
                    },
                    messages_dir=messages_dir,
                )

            first = mcp_post("monotonic-stream", 2, "sequence two")
            assert first["envelope"]["seq"] == 2
            emitted = authority.cursor_peek("controller", nonce=lease.nonce)
            assert [int(row["stream_seq"]) for row in emitted.items] == [2]
            assert authority.advance_cursor(
                "controller",
                nonce=lease.nonce,
                expected_cursor_version=emitted.cursor_version,
                expected_stream_snapshots=emitted.stream_snapshots,
                advances={"monotonic-stream": 2},
                actor="admission-controller",
            ).committed

            admitted = mcp_post("monotonic-stream", 1, "late explicit sequence one")
            if mutate_admission:
                assert admitted["envelope"]["seq"] == 1
                assert authority.cursor_peek("controller", nonce=lease.nonce).items == ()
            else:
                assert admitted["envelope"]["seq"] == 3
                pending = authority.cursor_peek("controller", nonce=lease.nonce)
                assert [int(row["stream_seq"]) for row in pending.items] == [3]
                independent = mcp_post("independent-stream", 1, "independent sequence one")
                assert independent["envelope"]["seq"] == 1

    run_case("fixed-admission", mutate_admission=False)
    # Mutation control: accepting the caller's old explicit value recreates the
    # review finding and proves the fixed half would catch that regression.
    run_case("mutated-admission", mutate_admission=True)


def test_stream_safe_advance_rejects_unseen_lower_row_mutation_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def run_case(case: str, *, mutate_safety: bool) -> None:
        case_root = tmp_path / case
        case_root.mkdir()
        with monkeypatch.context() as case_patch:
            _set_state_env(case_patch, case_root)
            if mutate_safety:
                case_patch.setattr(
                    journal.Journal,
                    "_cursor_advance_has_unseen_at_or_below",
                    staticmethod(lambda *args, **kwargs: False),
                )
            project = _project(case_root)
            authority = journal.open_or_create_journal(project)
            lease = _claim(authority)
            messages.post_message(
                dispatch_id="late-lower-stream",
                msg_type="controller-notice",
                payload={"text": "sequence two"},
                messages_dir=case_root / "messages",
                source={"node": "test-node", "adapter": "test", "transport": "controller"},
                addressee=messages.controller_addressee("controller", project_root=project),
                seq=2,
            )
            emitted = authority.cursor_peek("controller", nonce=lease.nonce)
            assert [int(row["stream_seq"]) for row in emitted.items] == [2]

            lower_id = str(uuid.uuid4())
            inserted = authority.record_delivery_event(
                recipient_label="controller",
                origin_node="mutation-probe",
                event_uuid=lower_id,
                stream_id="late-lower-stream",
                stream_seq=1,
                carrier_path=case_root / "late-lower.jsonl",
                event_type="controller-notice",
                wake_class="waking",
                created_at=journal.utc_now(),
            )
            assert inserted.committed
            outcome = authority.advance_cursor(
                "controller",
                nonce=lease.nonce,
                expected_cursor_version=emitted.cursor_version,
                expected_stream_snapshots=emitted.stream_snapshots,
                advances={"late-lower-stream": 2},
                actor="late-lower-controller",
            )
            if mutate_safety:
                assert outcome.committed
            else:
                assert outcome.cas_lost
                assert authority.cursor_status("controller")["positions"] == {}
            assert authority.mark_delivery_projected(
                recipient_label="controller",
                origin_node="mutation-probe",
                event_uuid=lower_id,
            ).committed
            if mutate_safety:
                assert authority.cursor_peek("controller", nonce=lease.nonce).items == ()
            else:
                refreshed = authority.cursor_peek("controller", nonce=lease.nonce)
                assert [int(row["stream_seq"]) for row in refreshed.items] == [1, 2]
                assert authority.advance_cursor(
                    "controller",
                    nonce=lease.nonce,
                    expected_cursor_version=refreshed.cursor_version,
                    expected_stream_snapshots=refreshed.stream_snapshots,
                    advances={"late-lower-stream": 2},
                    actor="fresh-lower-controller",
                ).committed

    run_case("fixed-stream-safety", mutate_safety=False)
    # Mutation control: removing the unseen-row predicate commits position 2
    # and makes the later-projected position 1 unreachable.
    run_case("mutated-stream-safety", mutate_safety=True)


def test_stream_safe_advance_accepts_other_and_above_churn_mutation_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def run_case(case: str, *, mutate_global_cas: bool) -> None:
        case_root = tmp_path / case
        case_root.mkdir()
        with monkeypatch.context() as case_patch:
            _set_state_env(case_patch, case_root)
            if mutate_global_cas:
                case_patch.setattr(
                    journal.Journal,
                    "_cursor_snapshot_version_is_admissible",
                    staticmethod(
                        lambda *, expected_cursor_version, current_cursor_version: (
                            expected_cursor_version == current_cursor_version
                        )
                    ),
                )
            project = _project(case_root)
            authority = journal.open_or_create_journal(project)
            lease = _claim(authority)
            common = {
                "msg_type": "controller-notice",
                "messages_dir": case_root / "messages",
                "source": {"node": "test-node", "adapter": "test", "transport": "controller"},
                "addressee": messages.controller_addressee("controller", project_root=project),
            }
            messages.post_message(
                dispatch_id="target-stream", payload={"text": "target one"}, **common
            )
            emitted = authority.cursor_peek("controller", nonce=lease.nonce)
            assert [
                (str(row["stream_id"]), int(row["stream_seq"])) for row in emitted.items
            ] == [("target-stream", 1)]
            messages.post_message(
                dispatch_id="target-stream", payload={"text": "target two"}, **common
            )
            messages.post_message(
                dispatch_id="other-stream", payload={"text": "other one"}, **common
            )
            assert authority.cursor_peek(
                "controller", nonce=lease.nonce
            ).cursor_version > emitted.cursor_version
            outcome = authority.advance_cursor(
                "controller",
                nonce=lease.nonce,
                expected_cursor_version=emitted.cursor_version,
                expected_stream_snapshots=emitted.stream_snapshots,
                advances={"target-stream": 1},
                actor="churn-controller",
            )
            assert outcome.cas_lost if mutate_global_cas else outcome.committed

    run_case("fixed-version-admission", mutate_global_cas=False)
    # Mutation control: restoring equality on the aggregate version rejects a
    # command changed only by an above-P and an other-stream arrival.
    run_case("mutated-global-cas", mutate_global_cas=True)


def test_stream_snapshot_rejects_projected_rehome_below_position_mutation_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def run_case(case: str, *, mutate_safety: bool) -> None:
        case_root = tmp_path / case
        case_root.mkdir()
        with monkeypatch.context() as case_patch:
            _set_state_env(case_patch, case_root)
            if mutate_safety:
                case_patch.setattr(
                    journal.Journal,
                    "_cursor_advance_has_unseen_at_or_below",
                    staticmethod(lambda *args, **kwargs: False),
                )
            project = _project(case_root)
            authority = journal.open_or_create_journal(project)
            retiring = _claim(authority, "retiring-controller")
            successor = _claim(authority, "successor-controller")
            common = {
                "dispatch_id": "retirement-stream",
                "msg_type": "controller-notice",
                "messages_dir": case_root / "messages",
                "source": {
                    "node": "test-node",
                    "adapter": "test",
                    "transport": "controller",
                },
            }
            messages.post_message(
                payload={"text": "retiring position one"},
                addressee=messages.controller_addressee(
                    "retiring-controller", project_root=project
                ),
                **common,
            )
            messages.post_message(
                payload={"text": "successor position two"},
                addressee=messages.controller_addressee(
                    "successor-controller", project_root=project
                ),
                **common,
            )
            emitted = authority.cursor_peek(
                "successor-controller", nonce=successor.nonce
            )
            assert [int(row["stream_seq"]) for row in emitted.items] == [2]

            assert authority.release_lease(
                "retiring-controller",
                nonce=retiring.nonce,
                reason="stream-snapshot-rehome",
            ).committed
            outcome = authority.advance_cursor(
                "successor-controller",
                nonce=successor.nonce,
                expected_cursor_version=emitted.cursor_version,
                expected_stream_snapshots=emitted.stream_snapshots,
                advances={"retirement-stream": 2},
                actor="successor-controller",
            )
            if mutate_safety:
                assert outcome.committed
                assert authority.cursor_peek(
                    "successor-controller", nonce=successor.nonce
                ).items == ()
            else:
                assert outcome.cas_lost
                refreshed = authority.cursor_peek(
                    "successor-controller", nonce=successor.nonce
                )
                assert [int(row["stream_seq"]) for row in refreshed.items] == [1, 2]
                assert authority.advance_cursor(
                    "successor-controller",
                    nonce=successor.nonce,
                    expected_cursor_version=refreshed.cursor_version,
                    expected_stream_snapshots=refreshed.stream_snapshots,
                    advances={"retirement-stream": 2},
                    actor="successor-controller-refreshed",
                ).committed

    run_case("fixed-projected-rehome", mutate_safety=False)
    # Mutation control: dropping the stream fingerprint lets a projected row
    # move into the recipient's lower range after peek and be silently eaten.
    run_case("mutated-projected-rehome", mutate_safety=True)


def test_cursor_items_and_stream_token_share_one_read_snapshot_mutation_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def run_case(case: str, *, mutate_read_snapshot: bool) -> None:
        case_root = tmp_path / case
        case_root.mkdir()
        with monkeypatch.context() as case_patch:
            _set_state_env(case_patch, case_root)
            if mutate_read_snapshot:
                case_patch.setattr(
                    journal.Journal,
                    "_begin_cursor_read_snapshot",
                    staticmethod(lambda _connection: None),
                )
            project = _project(case_root)
            authority = journal.open_or_create_journal(project)
            retiring = _claim(authority, "retiring-controller")
            successor = _claim(authority, "successor-controller")
            common = {
                "dispatch_id": "atomic-snapshot-stream",
                "msg_type": "controller-notice",
                "messages_dir": case_root / "messages",
                "source": {
                    "node": "test-node",
                    "adapter": "test",
                    "transport": "controller",
                },
            }
            messages.post_message(
                payload={"text": "retiring position one"},
                addressee=messages.controller_addressee(
                    "retiring-controller", project_root=project
                ),
                **common,
            )
            messages.post_message(
                payload={"text": "successor position two"},
                addressee=messages.controller_addressee(
                    "successor-controller", project_root=project
                ),
                **common,
            )

            rows_read = threading.Event()
            allow_token = threading.Event()
            original_snapshot = journal.Journal._cursor_stream_snapshot

            def pause_between_rows_and_token(connection, **kwargs):
                if threading.current_thread().name == f"atomic-peek-{case}":
                    rows_read.set()
                    assert allow_token.wait(5), "cursor token read was not released"
                return original_snapshot(connection, **kwargs)

            case_patch.setattr(
                journal.Journal,
                "_cursor_stream_snapshot",
                staticmethod(pause_between_rows_and_token),
            )
            peek_outcome: dict[str, object] = {}

            def peek_cursor() -> None:
                try:
                    peek_outcome["peek"] = authority.cursor_peek(
                        "successor-controller", nonce=successor.nonce
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    peek_outcome["error"] = exc

            peeker = threading.Thread(target=peek_cursor, name=f"atomic-peek-{case}")
            peeker.start()
            assert rows_read.wait(5), "cursor rows were not read"
            assert authority.release_lease(
                "retiring-controller",
                nonce=retiring.nonce,
                reason="atomic-snapshot-rehome",
            ).committed
            allow_token.set()
            peeker.join(timeout=5)
            assert not peeker.is_alive()
            assert "error" not in peek_outcome, peek_outcome.get("error")
            emitted = peek_outcome["peek"]
            assert isinstance(emitted, journal.CursorPeek)
            assert [int(row["stream_seq"]) for row in emitted.items] == [2]
            outcome = authority.advance_cursor(
                "successor-controller",
                nonce=successor.nonce,
                expected_cursor_version=emitted.cursor_version,
                expected_stream_snapshots=emitted.stream_snapshots,
                advances={"atomic-snapshot-stream": 2},
                actor="atomic-snapshot-controller",
            )
            assert outcome.committed if mutate_read_snapshot else outcome.cas_lost

    run_case("fixed-atomic-snapshot", mutate_read_snapshot=False)
    # Mutation control: autocommit SELECTs let the token include a re-homed row
    # that was absent from the returned items, so the stale advance eats it.
    run_case("mutated-autocommit-snapshot", mutate_read_snapshot=True)


def test_waking_only_snapshot_cannot_ack_quiet_lower_row_mutation_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def run_case(case: str, *, mutate_filter: bool) -> None:
        case_root = tmp_path / case
        case_root.mkdir()
        with monkeypatch.context() as case_patch:
            _set_state_env(case_patch, case_root)
            original_snapshot = journal.Journal._cursor_stream_snapshot
            if mutate_filter:
                def ignore_waking_filter(connection, **kwargs):
                    kwargs["waking_only"] = False
                    return original_snapshot(connection, **kwargs)

                case_patch.setattr(
                    journal.Journal,
                    "_cursor_stream_snapshot",
                    staticmethod(ignore_waking_filter),
                )
            project = _project(case_root)
            authority = journal.open_or_create_journal(project)
            lease = _claim(authority)
            common = {
                "dispatch_id": "filtered-stream",
                "messages_dir": case_root / "messages",
                "source": {
                    "node": "test-node",
                    "adapter": "test",
                    "transport": "controller",
                },
                "addressee": messages.controller_addressee(
                    "controller", project_root=project
                ),
            }
            quiet_id = str(uuid.uuid4())
            assert authority.record_delivery_event(
                recipient_label="controller",
                origin_node="test-node",
                event_uuid=quiet_id,
                stream_id="filtered-stream",
                stream_seq=1,
                carrier_path=case_root / "quiet.jsonl",
                event_type="status",
                wake_class="quiet",
                created_at=journal.utc_now(),
            ).committed
            assert authority.mark_delivery_projected(
                recipient_label="controller",
                origin_node="test-node",
                event_uuid=quiet_id,
            ).committed
            messages.post_message(
                msg_type="controller-notice",
                payload={"text": "waking two"},
                seq=2,
                **common,
            )
            emitted = authority.cursor_peek(
                "controller", nonce=lease.nonce, waking_only=True
            )
            assert [int(row["stream_seq"]) for row in emitted.items] == [2]
            outcome = authority.advance_cursor(
                "controller",
                nonce=lease.nonce,
                expected_cursor_version=emitted.cursor_version,
                expected_stream_snapshots=emitted.stream_snapshots,
                advances={"filtered-stream": 2},
                actor="filtered-controller",
            )
            if mutate_filter:
                assert outcome.committed
            else:
                assert outcome.cas_lost
                refreshed = authority.cursor_peek("controller", nonce=lease.nonce)
                assert [int(row["stream_seq"]) for row in refreshed.items] == [1, 2]
                assert authority.advance_cursor(
                    "controller",
                    nonce=lease.nonce,
                    expected_cursor_version=refreshed.cursor_version,
                    expected_stream_snapshots=refreshed.stream_snapshots,
                    advances={"filtered-stream": 2},
                    actor="full-controller",
                ).committed

    run_case("fixed-filtered-snapshot", mutate_filter=False)
    # Mutation control: hashing quiet rows that were filtered from the returned
    # batch lets the waking-only command silently acknowledge unseen mail.
    run_case("mutated-filtered-snapshot", mutate_filter=True)


def test_continuous_delivery_cannot_starve_stream_advance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority)
    common = {
        "dispatch_id": "continuous-stream",
        "msg_type": "controller-notice",
        "messages_dir": tmp_path / "messages",
        "source": {"node": "test-node", "adapter": "test", "transport": "controller"},
        "addressee": messages.controller_addressee("controller", project_root=project),
    }
    messages.post_message(payload={"text": "position one"}, **common)
    emitted = authority.cursor_peek("controller", nonce=lease.nonce, limit=1)
    assert [int(row["stream_seq"]) for row in emitted.items] == [1]

    stop_delivery = threading.Event()
    delivery_started = threading.Event()
    deliveries: list[int] = []
    delivery_errors: list[BaseException] = []

    def deliver_continuously() -> None:
        index = 2
        try:
            while not stop_delivery.is_set():
                posted = messages.post_message(payload={"text": f"position {index}"}, **common)
                deliveries.append(int(posted["envelope"]["seq"]))
                if len(deliveries) >= 3:
                    delivery_started.set()
                index += 1
                time.sleep(0.001)
        except BaseException as exc:  # pragma: no cover - asserted below
            delivery_errors.append(exc)
            delivery_started.set()

    producer = threading.Thread(target=deliver_continuously, name="continuous-delivery")
    producer.start()
    outcome = None
    attempts = 0
    try:
        assert delivery_started.wait(5), "continuous delivery did not start"
        for attempts in range(1, 9):
            outcome = authority.advance_cursor(
                "controller",
                nonce=lease.nonce,
                expected_cursor_version=emitted.cursor_version,
                expected_stream_snapshots=emitted.stream_snapshots,
                advances={"continuous-stream": 1},
                actor="starvation-free-controller",
            )
            if outcome.committed:
                break
            time.sleep(0.005)
    finally:
        stop_delivery.set()
        producer.join(timeout=5)
    assert not producer.is_alive()
    assert delivery_errors == []
    assert len(deliveries) >= 3
    assert attempts <= 8
    assert outcome is not None and outcome.committed, outcome.reason if outcome else None
    assert authority.cursor_status("controller")["positions"] == {"continuous-stream": 1}


def test_delivery_planner_keeps_b7d1b03_recipient_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    first_primary = _claim(authority, "primary")
    replacement = authority.claim_or_renew_lease(
        "primary",
        principal=_principal(41002, "start-b"),
        takeover=True,
    )
    assert replacement.committed and replacement.value is not None
    assert replacement.value.generation == first_primary.generation + 1
    _claim(authority, "secondary")

    def write_dispatch(dispatch_id: str, owner: str | None) -> None:
        record = {
            "dispatch_id": dispatch_id,
            "project_root": str(project),
            "state": "running",
        }
        if owner is not None:
            record["controller_label"] = owner
        path = ledger.record_path(dispatch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")

    def post(
        dispatch_id: str,
        *,
        msg_type: str = "result",
        payload: dict[str, object] | None = None,
        addressee: dict[str, object] | None = None,
    ) -> None:
        messages.post_message(
            dispatch_id=dispatch_id,
            msg_type=msg_type,
            payload=payload or {"summary": dispatch_id},
            messages_dir=tmp_path / "messages",
            source={"node": "test", "adapter": "test", "transport": "controller"},
            addressee=addressee,
        )

    write_dispatch("planner-owned", "primary")
    post("planner-owned")
    write_dispatch("planner-unowned", None)
    post("planner-unowned")
    post(
        "planner-addressed",
        msg_type="controller-notice",
        addressee=messages.controller_addressee("secondary", project_root=project),
    )
    post(
        "planner-star",
        msg_type="controller-notice",
        addressee=messages.controller_addressee("*", project_root=project),
    )
    post(
        "planner-project-broadcast",
        msg_type="controller-notice",
        payload={"project_root": str(project), "text": "broadcast"},
    )

    recipients = {
        str(stream_id): [str(row["recipient_label"]) for row in authority.read_all(
            """SELECT recipient_label FROM delivery_events
               WHERE stream_id = ? ORDER BY recipient_label""",
            (stream_id,),
        )]
        for stream_id in (
            "planner-owned",
            "planner-unowned",
            "planner-addressed",
            "planner-star",
            "planner-project-broadcast",
        )
    }
    assert recipients == {
        "planner-owned": ["primary"],
        "planner-unowned": ["primary", "secondary"],
        "planner-addressed": ["secondary"],
        "planner-star": ["*"],
        "planner-project-broadcast": ["*"],
    }
    primary_states = [
        str(row["state"])
        for row in authority.lease_records(include_ended=True)
        if row["label"] == "primary"
    ]
    assert primary_states == ["SUPERSEDED", "ACTIVE"]


def test_terminal_projection_targets_owned_exactly_and_unowned_by_fanout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    leases = {
        label: _claim(authority, label)
        for label in ("primary", "secondary")
    }

    for dispatch_id, controller_label in (
        ("owned-terminal", "primary"),
        ("unowned-terminal", None),
    ):
        record = {
            "dispatch_id": dispatch_id,
            "project_root": str(project),
            "state": "running",
        }
        if controller_label is not None:
            record["controller_label"] = controller_label
        path = ledger.record_path(dispatch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        prepared = authority.prepare_attempt(
            dispatch_id,
            owner_controller_label=controller_label,
            owner_session_nonce=(
                leases[controller_label].nonce if controller_label is not None else None
            ),
        )
        assert prepared.committed and prepared.value is not None
        committed = authority.commit_terminal(
            prepared.value.attempt_id,
            terminal_state="complete",
            observation={"state": "complete", "outcome": {}},
        )
        assert committed.committed
        assert len(
            authority.project_terminal_outbox(messages_dir=tmp_path / "messages")
        ) == 1

    recipients = {
        (str(row["stream_id"]), str(row["recipient_label"]))
        for row in authority.read_all(
            "SELECT stream_id, recipient_label FROM delivery_events"
        )
    }
    assert recipients == {
        ("owned-terminal", "primary"),
        ("unowned-terminal", "primary"),
        ("unowned-terminal", "secondary"),
    }
    assert {
        str(row["stream_id"])
        for row in authority.pending_delivery_events("primary")
    } == {"owned-terminal", "unowned-terminal"}
    assert {
        str(row["stream_id"])
        for row in authority.pending_delivery_events("secondary")
    } == {"unowned-terminal"}

    messages.post_message(
        dispatch_id="unowned-terminal",
        msg_type="advisory",
        payload={"text": "retain non-terminal unowned behavior"},
        messages_dir=tmp_path / "messages",
        source={"node": "test", "adapter": "test", "transport": "controller"},
    )
    assert len(authority.read_all("SELECT 1 FROM delivery_events")) == 3


def test_terminal_projection_rehomes_retired_owner_to_current_roster(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    retired = _claim(authority, "retired-owner")
    successor = _claim(authority, "successor-owner")
    dispatch_id = "terminal-after-owner-retirement"
    record_path = ledger.record_path(dispatch_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "project_root": str(project),
                "state": "running",
                "controller_label": "retired-owner",
            }
        ),
        encoding="utf-8",
    )
    prepared = authority.prepare_attempt(
        dispatch_id,
        owner_controller_label="retired-owner",
        owner_session_nonce=retired.nonce,
    )
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "outcome": {}},
    )
    assert committed.committed
    released = authority.release_lease(
        "retired-owner",
        nonce=retired.nonce,
        reason="retired-before-terminal-projection",
    )
    assert released.committed

    assert len(authority.project_terminal_outbox(messages_dir=tmp_path / "messages")) == 1
    live = authority.read_all(
        """SELECT recipient_label FROM delivery_events
           WHERE stream_id = ? AND withdrawn_at IS NULL""",
        (dispatch_id,),
    )
    assert [str(row["recipient_label"]) for row in live] == ["successor-owner"]
    pending = authority.cursor_peek("successor-owner", nonce=successor.nonce)
    assert [str(row["stream_id"]) for row in pending.items] == [dispatch_id]


def test_terminal_projection_uses_full_immutable_owner_not_truncated_ledger_label(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    owner_label = "controller-label-verbatim-" + ("x" * 70)
    owner = _claim(authority, owner_label)
    dispatch_id = "terminal-with-long-owner-label"
    record_path = ledger.record_path(dispatch_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "project_root": str(project),
                "state": "running",
                "controller_label": owner_label[:64],
            }
        ),
        encoding="utf-8",
    )
    prepared = authority.prepare_attempt(
        dispatch_id,
        owner_controller_label=owner_label,
        owner_session_nonce=owner.nonce,
    )
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "outcome": {}},
    )
    assert committed.committed

    assert len(authority.project_terminal_outbox(messages_dir=tmp_path / "messages")) == 1
    live = authority.read_all(
        """SELECT recipient_label FROM delivery_events
           WHERE stream_id = ? AND withdrawn_at IS NULL""",
        (dispatch_id,),
    )
    assert [str(row["recipient_label"]) for row in live] == [owner_label]
    assert owner_label[:64] != owner_label
    pending = authority.cursor_peek(owner_label, nonce=owner.nonce)
    assert [str(row["stream_id"]) for row in pending.items] == [dispatch_id]


def test_terminal_outbox_never_exposes_truncated_carrier_recipient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    owner_label = "controller-label-verbatim-" + ("x" * 70)
    truncated_label = owner_label[:64]
    owner = _claim(authority, owner_label)
    truncated = _claim(authority, truncated_label)
    dispatch_id = "terminal-with-transient-ledger-recipient"
    record_path = ledger.record_path(dispatch_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "project_root": str(project),
                "state": "running",
                "controller_label": truncated_label,
            }
        ),
        encoding="utf-8",
    )
    prepared = authority.prepare_attempt(
        dispatch_id,
        owner_controller_label=owner_label,
        owner_session_nonce=owner.nonce,
    )
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "outcome": {}},
    )
    assert committed.committed
    carrier_appended = threading.Event()
    allow_completion = threading.Event()
    original_complete = journal.Journal._complete_terminal_delivery

    def pause_before_authoritative_completion(self, *args, **kwargs):
        carrier_appended.set()
        assert allow_completion.wait(5), "terminal completion pause was not released"
        return original_complete(self, *args, **kwargs)

    monkeypatch.setattr(
        journal.Journal,
        "_complete_terminal_delivery",
        pause_before_authoritative_completion,
    )
    outcome: dict[str, object] = {}

    def project() -> None:
        outcome["result"] = authority.project_terminal_outbox(
            messages_dir=tmp_path / "messages"
        )

    projector = threading.Thread(target=project, name="terminal-outbox-projector")
    projector.start()
    assert carrier_appended.wait(5), "carrier append did not reach completion boundary"
    transient = authority.cursor_peek(truncated_label, nonce=truncated.nonce)
    assert [str(row["stream_id"]) for row in transient.items] == []
    allow_completion.set()
    projector.join(timeout=5)
    assert not projector.is_alive()
    assert len(outcome["result"]) == 1
    live = authority.read_all(
        """SELECT recipient_label FROM delivery_events
           WHERE stream_id = ? AND withdrawn_at IS NULL""",
        (dispatch_id,),
    )
    assert [str(row["recipient_label"]) for row in live] == [owner_label]


def test_terminal_projection_keeps_wildcard_rehomed_during_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    owner = _claim(authority, "retiring-owner")
    successor = _claim(authority, "successor-owner")
    dispatch_id = "terminal-owner-retires-during-projection"
    prepared = authority.prepare_attempt(
        dispatch_id,
        owner_controller_label="retiring-owner",
        owner_session_nonce=owner.nonce,
    )
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "outcome": {}},
    )
    assert committed.committed
    original_reconcile = journal.Journal._reconcile_terminal_delivery_recipients
    retired = False

    def retire_after_projection(self, *args, **kwargs):
        nonlocal retired
        if not retired:
            retired = True
            released = self.release_lease(
                "retiring-owner",
                nonce=owner.nonce,
                reason="retired-between-projection-and-reconciliation",
            )
            assert released.committed
        return original_reconcile(self, *args, **kwargs)

    monkeypatch.setattr(
        journal.Journal,
        "_reconcile_terminal_delivery_recipients",
        retire_after_projection,
    )
    assert len(authority.project_terminal_outbox(messages_dir=tmp_path / "messages")) == 1
    assert retired
    live = authority.read_all(
        """SELECT recipient_label FROM delivery_events
           WHERE stream_id = ? AND withdrawn_at IS NULL""",
        (dispatch_id,),
    )
    assert [str(row["recipient_label"]) for row in live] == ["*"]
    pending = authority.cursor_peek("successor-owner", nonce=successor.nonce)
    assert [str(row["stream_id"]) for row in pending.items] == [dispatch_id]


def test_unowned_terminal_revalidates_retiring_roster_inside_assignment_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    retiring = _claim(authority, "retiring-controller")
    dispatch_id = "retirement-race-terminal"
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "project_root": str(project),
            "state": "running",
        }
    )

    roster_snapshotted = threading.Event()
    allow_assignment = threading.Event()
    original_record_delivery = journal.Journal.record_delivery_event

    def pause_after_roster_snapshot(self, *args, **kwargs):
        if kwargs.get("recipient_label") == "retiring-controller":
            roster_snapshotted.set()
            assert allow_assignment.wait(5), "retirement race was not released"
        return original_record_delivery(self, *args, **kwargs)

    monkeypatch.setattr(journal.Journal, "record_delivery_event", pause_after_roster_snapshot)
    outcome: dict[str, object] = {}

    def project_terminal() -> None:
        try:
            outcome["result"] = messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="result",
                payload={"complete": True, "text": "retirement race"},
                messages_dir=tmp_path / "messages",
                source={"node": "test", "adapter": "pytest", "transport": "journal"},
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcome["error"] = exc

    projector = threading.Thread(target=project_terminal, name="retirement-race-projector")
    projector.start()
    assert roster_snapshotted.wait(5), "projection never reached the assignment boundary"
    released = authority.release_lease(
        "retiring-controller",
        nonce=retiring.nonce,
        reason="retired-during-projection",
    )
    assert released.committed
    allow_assignment.set()
    projector.join(timeout=5)
    assert not projector.is_alive()
    assert "error" not in outcome, outcome.get("error")

    rows = authority.read_all(
        """SELECT recipient_label, projected_at, withdrawn_at
           FROM delivery_events WHERE stream_id = ?""",
        (dispatch_id,),
    )
    assert len(rows) == 1
    assert rows[0]["recipient_label"] == "*"
    assert rows[0]["projected_at"] is not None
    assert rows[0]["withdrawn_at"] is None

    successor = _claim(authority, "successor-controller")
    pending = authority.cursor_peek(
        "successor-controller",
        nonce=successor.nonce,
    )
    assert [str(row["stream_id"]) for row in pending.items] == [dispatch_id]


def test_retirement_after_exact_assignment_rehomes_mail_mutation_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def run_case(case: str, *, mutate_rehome: bool) -> None:
        case_root = tmp_path / case
        case_root.mkdir()
        with monkeypatch.context() as case_patch:
            _set_state_env(case_patch, case_root)
            if mutate_rehome:
                case_patch.setattr(
                    journal.Journal,
                    "_rehome_retired_delivery_events",
                    staticmethod(lambda *args, **kwargs: 0),
                )
            project = _project(case_root)
            authority = journal.open_or_create_journal(project)
            retiring = _claim(authority, "retiring-controller")
            dispatch_id = f"retirement-after-assignment-{case}"
            ledger.write_record(
                {
                    "schema": ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "project_root": str(project),
                    "state": "running",
                }
            )

            assignment_committed = threading.Event()
            allow_projection = threading.Event()
            original_record_delivery = journal.Journal.record_delivery_event

            def pause_after_exact_assignment(self, *args, **kwargs):
                result = original_record_delivery(self, *args, **kwargs)
                if kwargs.get("recipient_label") == "retiring-controller":
                    assert result.committed
                    assignment_committed.set()
                    assert allow_projection.wait(5), "post-assignment race was not released"
                return result

            case_patch.setattr(
                journal.Journal,
                "record_delivery_event",
                pause_after_exact_assignment,
            )
            outcome: dict[str, object] = {}

            def project_terminal() -> None:
                try:
                    outcome["result"] = messages.post_message(
                        dispatch_id=dispatch_id,
                        msg_type="result",
                        payload={"complete": True, "text": "retire after assignment"},
                        messages_dir=case_root / "messages",
                        source={"node": "test", "adapter": "pytest", "transport": "journal"},
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    outcome["error"] = exc

            projector = threading.Thread(
                target=project_terminal,
                name=f"post-assignment-retirement-{case}",
            )
            projector.start()
            assert assignment_committed.wait(5), "exact assignment never committed"
            released = authority.release_lease(
                "retiring-controller",
                nonce=retiring.nonce,
                reason="retired-after-assignment",
            )
            assert released.committed, released.reason
            allow_projection.set()
            projector.join(timeout=5)
            assert not projector.is_alive()
            assert "error" not in outcome, outcome.get("error")

            rows = authority.read_all(
                """SELECT recipient_label, projected_at, withdrawn_at
                   FROM delivery_events WHERE stream_id = ?""",
                (dispatch_id,),
            )
            assert len(rows) == 1
            successor = _claim(authority, "successor-controller")
            pending = authority.cursor_peek(
                "successor-controller",
                nonce=successor.nonce,
            )
            if mutate_rehome:
                assert rows[0]["recipient_label"] == "retiring-controller"
                assert pending.items == ()
            else:
                assert rows[0]["recipient_label"] == "*"
                assert rows[0]["projected_at"] is not None
                assert rows[0]["withdrawn_at"] is None
                assert [str(row["stream_id"]) for row in pending.items] == [dispatch_id]

    run_case("fixed-retirement", mutate_rehome=False)
    # Mutation control: omitting the in-transaction re-home leaves the exact
    # projected row bound to the retired label and invisible to its successor.
    run_case("mutated-retirement", mutate_rehome=True)


def test_retirement_rehomes_pending_but_does_not_replay_processed_mail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    retiring = _claim(authority, "retiring-controller")
    common = {
        "msg_type": "controller-notice",
        "messages_dir": tmp_path / "messages",
        "source": {"node": "test", "adapter": "pytest", "transport": "controller"},
        "addressee": messages.controller_addressee(
            "retiring-controller",
            project_root=project,
        ),
    }
    messages.post_message(
        dispatch_id="processed-before-retirement",
        payload={"text": "processed"},
        **common,
    )
    processed = authority.cursor_peek("retiring-controller", nonce=retiring.nonce)
    assert authority.advance_cursor(
        "retiring-controller",
        nonce=retiring.nonce,
        expected_cursor_version=processed.cursor_version,
        expected_stream_snapshots=processed.stream_snapshots,
        advances={"processed-before-retirement": 1},
        actor="retiring-controller",
    ).committed
    messages.post_message(
        dispatch_id="pending-at-retirement",
        payload={"text": "pending"},
        **common,
    )
    assert authority.release_lease(
        "retiring-controller",
        nonce=retiring.nonce,
        reason="retirement-scope-probe",
    ).committed
    rows = authority.read_all(
        """SELECT stream_id, recipient_label FROM delivery_events
           WHERE stream_id IN ('processed-before-retirement', 'pending-at-retirement')
           ORDER BY stream_id"""
    )
    assert [(str(row["stream_id"]), str(row["recipient_label"])) for row in rows] == [
        ("pending-at-retirement", "*"),
        ("processed-before-retirement", "retiring-controller"),
    ]
    successor = _claim(authority, "successor-controller")
    pending = authority.cursor_peek("successor-controller", nonce=successor.nonce)
    assert [str(row["stream_id"]) for row in pending.items] == ["pending-at-retirement"]


def test_unowned_terminal_projection_with_no_controller_is_held_for_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    dispatch_id = "unowned-terminal-held"
    path = ledger.record_path(dispatch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "project_root": str(project),
                "state": "running",
            }
        ),
        encoding="utf-8",
    )

    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "outcome": {}},
    )
    assert committed.committed
    assert len(authority.project_terminal_outbox(messages_dir=tmp_path / "messages")) == 1

    rows = authority.read_all(
        """SELECT recipient_label, projected_at, withdrawn_at
           FROM delivery_events WHERE stream_id = ?""",
        (dispatch_id,),
    )
    assert len(rows) == 1
    assert rows[0]["recipient_label"] == "*"
    assert rows[0]["projected_at"] is not None
    assert rows[0]["withdrawn_at"] is None

    lease = _claim(authority, "late-controller")
    pending = authority.cursor_peek(
        "late-controller",
        nonce=lease.nonce,
    )
    assert [str(row["stream_id"]) for row in pending.items] == [dispatch_id]


def test_terminal_outbox_without_ledger_completes_journal_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mutation guard: deleting direct delivery makes this producer-free wake vanish."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    leases = {
        label: _claim(authority, label)
        for label in ("primary", "secondary")
    }
    dispatch_id = "terminal-without-ledger"
    assert not ledger.record_path(dispatch_id).exists()
    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "outcome": {}},
    )
    assert committed.committed

    projected = authority.project_terminal_outbox(messages_dir=tmp_path / "messages")
    assert len(projected) == 1
    assert authority.project_terminal_outbox(messages_dir=tmp_path / "messages") == []
    rows = authority.read_all(
        """SELECT recipient_label, origin_node, event_uuid, projected_at
           FROM delivery_events WHERE stream_id = ? ORDER BY recipient_label""",
        (dispatch_id,),
    )
    assert [str(row["recipient_label"]) for row in rows] == ["primary", "secondary"]
    assert {str(row["event_uuid"]) for row in rows} == {committed.value.event_uuid}
    assert all(row["projected_at"] is not None for row in rows)
    for label, lease in leases.items():
        pending = authority.cursor_peek(label, nonce=lease.nonce)
        assert [str(row["stream_id"]) for row in pending.items] == [dispatch_id]


def test_produce_path_invalidate_cannot_consume_cursor() -> None:
    """Mutation guard: produce-path snapshot invalidation is not a drain."""
    src = inspect.getsource(journal.Journal._invalidate_delivery_cursor_snapshots)
    assert "INSERT INTO controller_stream_cursors" not in src, (
        "produce path advanced a stream cursor without a consumer"
    )
    assert "advanced_by =" not in src, (
        "produce path stamped advanced_by; only a drain may record a consumer"
    )
    assert "backlog_pending = 0" not in src, (
        "produce path forced backlog_pending=0 and retired unread mail"
    )


def test_terminal_event_leaves_backlog_pending_until_drain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live recipient's terminal event stays unconsumed until a named drain."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "inbox")
    before = authority.cursor_status("inbox")
    assert before is not None
    assert before["backlog_pending"] == 0
    assert before["positions"] == {}
    assert before["advanced_by"] is None

    dispatch_id = "silent-consumption-terminal"
    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "outcome": {}},
    )
    assert committed.committed
    assert len(authority.project_terminal_outbox(messages_dir=tmp_path / "messages")) == 1

    after_produce = authority.cursor_status("inbox")
    assert after_produce is not None
    assert after_produce["backlog_pending"] == 1, (
        "produce path retired the terminal event before a consumer acted"
    )
    assert after_produce["positions"] == {}, (
        "produce path advanced a stream cursor without a consumer"
    )
    assert after_produce["advanced_by"] is None, (
        "produce path stamped a consumer that did not act"
    )
    assert after_produce["advanced_at"] is None
    peek = authority.cursor_peek("inbox", nonce=lease.nonce)
    assert [str(row["stream_id"]) for row in peek.items] == [dispatch_id]

    actor = "controller:168:relay-drain"
    advanced = authority.advance_cursor(
        "inbox",
        nonce=lease.nonce,
        expected_cursor_version=peek.cursor_version,
        expected_stream_snapshots=peek.stream_snapshots,
        advances={dispatch_id: 1},
        actor=actor,
    )
    assert advanced.committed
    after_drain = authority.cursor_status("inbox")
    assert after_drain is not None
    assert after_drain["backlog_pending"] == 0
    assert after_drain["advanced_by"] == actor
    assert after_drain["advanced_at"]
    assert after_drain["positions"] == {dispatch_id: 1}
    drained = authority.cursor_peek("inbox", nonce=lease.nonce)
    assert drained.items == ()


def test_terminal_outbox_retry_heals_partial_recipient_fanout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A crash after recipient A commits must not strand recipient B."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    for label in ("recipient-a", "recipient-b"):
        _claim(authority, label)
    dispatch_id = "terminal-partial-fanout"
    assert not ledger.record_path(dispatch_id).exists()
    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "outcome": {}},
    )
    assert committed.committed

    original_record = journal.Journal.record_delivery_event
    failed_after_first_commit = False

    def crash_after_first_recipient(self, *args, **kwargs):
        nonlocal failed_after_first_commit
        result = original_record(self, *args, **kwargs)
        if not failed_after_first_commit:
            failed_after_first_commit = True
            raise RuntimeError("synthetic crash after recipient A commit")
        return result

    monkeypatch.setattr(
        journal.Journal,
        "record_delivery_event",
        crash_after_first_recipient,
    )
    assert authority.project_terminal_outbox(messages_dir=tmp_path / "messages") == []
    partial = authority.read_all(
        """SELECT recipient_label, COUNT(*) AS count FROM delivery_events
           WHERE stream_id = ? GROUP BY recipient_label ORDER BY recipient_label""",
        (dispatch_id,),
    )
    assert [(str(row["recipient_label"]), int(row["count"])) for row in partial] == [
        ("recipient-a", 1),
    ]
    assert authority.read_all(
        """SELECT projected_at FROM terminal_outbox WHERE attempt_id = ?""",
        (prepared.value.attempt_id,),
    )[0]["projected_at"] is None

    monkeypatch.setattr(journal.Journal, "record_delivery_event", original_record)
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            """UPDATE terminal_outbox SET projection_retry_at = ?
               WHERE attempt_id = ?""",
            ("1970-01-01T00:00:00+00:00", prepared.value.attempt_id),
        )

    assert len(authority.project_terminal_outbox(messages_dir=tmp_path / "messages")) == 1
    healed = authority.read_all(
        """SELECT recipient_label, COUNT(*) AS count, projected_at
           FROM delivery_events WHERE stream_id = ?
           GROUP BY recipient_label ORDER BY recipient_label""",
        (dispatch_id,),
    )
    assert [
        (str(row["recipient_label"]), int(row["count"])) for row in healed
    ] == [("recipient-a", 1), ("recipient-b", 1)]
    assert all(row["projected_at"] is not None for row in healed)
    assert authority.read_all(
        """SELECT projected_at FROM terminal_outbox WHERE attempt_id = ?""",
        (prepared.value.attempt_id,),
    )[0]["projected_at"] is not None


def test_first_wildcard_processor_adopts_once_while_unhandled_rows_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)

    def insert_delivery(
        stream_id: str,
        *,
        recipient_label: str = "*",
        event_id: str | None = None,
    ) -> str:
        event_id = event_id or str(uuid.uuid4())
        recorded = authority.record_delivery_event(
            recipient_label=recipient_label,
            origin_node="test-node",
            event_uuid=event_id,
            stream_id=stream_id,
            stream_seq=1,
            carrier_path=tmp_path / f"{stream_id}.jsonl",
            event_type="result",
            wake_class="waking",
            created_at=journal.utc_now(),
        )
        assert recorded.committed
        projected = authority.mark_delivery_projected(
            recipient_label=recipient_label,
            origin_node="test-node",
            event_uuid=event_id,
        )
        assert projected.committed
        return event_id

    insert_delivery("wildcard-handled-once")
    leases = {
        label: _claim(authority, label)
        for label in ("first-processor", "second-processor")
    }
    peeks = {
        label: authority.cursor_peek(label, nonce=lease.nonce)
        for label, lease in leases.items()
    }
    assert all(
        [str(row["stream_id"]) for row in peek.items] == ["wildcard-handled-once"]
        for peek in peeks.values()
    )
    barrier = threading.Barrier(2)

    def process(label: str):
        barrier.wait()
        return journal.Journal(project).advance_cursor(
            label,
            nonce=leases[label].nonce,
            expected_cursor_version=peeks[label].cursor_version,
            expected_stream_snapshots=peeks[label].stream_snapshots,
            advances={"wildcard-handled-once": 1},
            actor=label,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = dict(zip(leases, pool.map(process, leases)))
    assert sum(result.committed for result in outcomes.values()) == 1
    assert sum(result.cas_lost for result in outcomes.values()) == 1
    winner = next(label for label, result in outcomes.items() if result.committed)
    handled = authority.read_all(
        """SELECT recipient_label, withdrawn_at FROM delivery_events
           WHERE stream_id = 'wildcard-handled-once'"""
    )
    assert len(handled) == 1
    assert handled[0]["recipient_label"] == winner
    assert handled[0]["withdrawn_at"] is None

    late = _claim(authority, "late-controller")
    assert authority.cursor_peek("late-controller", nonce=late.nonce).items == ()

    conflict_event = str(uuid.uuid4())
    insert_delivery(
        "wildcard-conflicts-with-exact",
        recipient_label="late-controller",
        event_id=conflict_event,
    )
    insert_delivery("wildcard-conflicts-with-exact", event_id=conflict_event)
    conflict = authority.cursor_peek("late-controller", nonce=late.nonce)
    assert len(conflict.items) == 2
    assert authority.advance_cursor(
        "late-controller",
        nonce=late.nonce,
        expected_cursor_version=conflict.cursor_version,
        expected_stream_snapshots=conflict.stream_snapshots,
        advances={"wildcard-conflicts-with-exact": 1},
        actor="exact-conflict-processor",
    ).committed
    conflict_rows = authority.read_all(
        """SELECT recipient_label, withdrawn_at FROM delivery_events
           WHERE stream_id = 'wildcard-conflicts-with-exact'
           ORDER BY recipient_label"""
    )
    assert [str(row["recipient_label"]) for row in conflict_rows] == [
        "*",
        "late-controller",
    ]
    assert conflict_rows[0]["withdrawn_at"] is not None
    assert conflict_rows[1]["withdrawn_at"] is None

    later = _claim(authority, "later-controller")
    assert authority.cursor_peek("later-controller", nonce=later.nonce).items == ()
    insert_delivery("wildcard-still-unhandled")
    waiting = authority.cursor_peek("later-controller", nonce=later.nonce)
    assert [str(row["stream_id"]) for row in waiting.items] == [
        "wildcard-still-unhandled"
    ]


def test_unowned_terminal_projection_wakes_armed_doorbell_three_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    env["GOALFLIGHT_TEST_LISTENER_START_TOKEN"] = "unowned-terminal-listener-token"
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "controller")
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
        "8",
        "--json",
    ]

    measurements: list[float] = []
    for run in range(1, 4):
        dispatch_id = f"unowned-terminal-doorbell-{run}"
        path = ledger.record_path(dispatch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "dispatch_id": dispatch_id,
                    "project_root": str(project),
                    "state": "running",
                }
            ),
            encoding="utf-8",
        )
        prepared = authority.prepare_attempt(dispatch_id)
        assert prepared.committed and prepared.value is not None
        listener = subprocess.Popen(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                coverage = authority.active_coverage("controller")
                if coverage is not None and coverage["pid"] == listener.pid:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("unowned terminal listener never armed its doorbell")

            started = time.monotonic()
            committed = authority.commit_terminal(
                prepared.value.attempt_id,
                terminal_state="complete",
                observation={"state": "complete", "outcome": {}},
            )
            assert committed.committed
            assert len(
                authority.project_terminal_outbox(messages_dir=tmp_path / "messages")
            ) == 1
            stdout, stderr = listener.communicate(timeout=6)
            elapsed = time.monotonic() - started
            assert listener.returncode == 0, stderr
            assert json.loads(stdout)["reason"] == "event"
            assert elapsed < 5.0
            measurements.append(elapsed)
            recipients = authority.read_all(
                """SELECT recipient_label FROM delivery_events
                   WHERE stream_id = ? ORDER BY recipient_label""",
                (dispatch_id,),
            )
            assert [str(row["recipient_label"]) for row in recipients] == ["controller"]
            print(f"UNOWNED_TERMINAL_DOORBELL run={run} seconds={elapsed:.3f}")

            peek = authority.cursor_peek(
                "controller",
                nonce=lease.nonce,
            )
            assert peek.items
            advances = {
                str(row["stream_id"]): int(row["stream_seq"])
                for row in peek.items
            }
            assert authority.advance_cursor(
                "controller",
                nonce=lease.nonce,
                expected_cursor_version=peek.cursor_version,
                expected_stream_snapshots=peek.stream_snapshots,
                advances=advances,
                actor="unowned-terminal-doorbell-test",
            ).committed
        finally:
            if listener.poll() is None:
                listener.terminate()
                listener.wait(timeout=2)

    assert len(measurements) == 3


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
    peek = authority.cursor_peek("controller", nonce=lease.nonce, limit=1)
    barrier = threading.Barrier(32)

    def race(index: int):
        barrier.wait()
        return journal.Journal(project).advance_cursor(
            "controller",
            nonce=lease.nonce,
            expected_cursor_version=peek.cursor_version,
            expected_stream_snapshots=peek.stream_snapshots,
            advances={"contended-stream": 1},
            actor=f"contender-{index}",
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(race, range(32)))
    assert sum(result.committed for result in outcomes) == 1
    assert all(result.committed or result.cas_lost or result.retryable for result in outcomes)
    cursor = authority.cursor_status("controller")
    assert cursor is not None
    assert cursor["cursor_version"] == peek.cursor_version + 1
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


def test_second_real_doorbell_loses_generation_lock_and_first_wakes_body_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    env["GOALFLIGHT_TEST_LISTENER_START_TOKEN"] = "constructed-listener-token"
    # Slot pools made a second doorbell legitimate; this test is about the
    # contention refusal itself, so it pins a single-slot pool and asserts the
    # FIRST surplus listener is refused. Pool-depth behavior is covered by the
    # listener-pool tests.
    env["GOALFLIGHT_LISTENER_SLOTS"] = "1"
    monkeypatch.setenv("GOALFLIGHT_LISTENER_SLOTS", "1")
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
        second_stdout, second_stderr = second.communicate(timeout=3)
        assert second.returncode == 3, (second_stdout, second_stderr)
        # The refusal is actionable now: it names the slot budget and the
        # holder pids, and warns against pattern-kills (a broad pkill once
        # took out a sibling session's doorbell).
        assert "listener slots hold live doorbells" in second_stderr
        assert "do NOT kill by pattern" in second_stderr

        messages.post_message(
            dispatch_id="listener-real",
            msg_type="controller-notice",
            payload={"text": "wake first listener"},
            messages_dir=tmp_path / "messages",
            source={"node": "test-node", "adapter": "test", "transport": "controller"},
            addressee=messages.controller_addressee("controller", project_root=project),
        )
        first_stdout, first_stderr = first.communicate(timeout=3)
        assert first.returncode == 0, first_stderr
        doorbell = json.loads(first_stdout)
        assert doorbell["reason"] == "event"
        # "kind" is the jsonl discriminator added with --report-pending's
        # multi-object output. "rearm" is the remaining-depth floor plan
        # after the slot is consumed; it is not a mail body.
        assert doorbell["kind"] == "ring"
        assert set(doorbell) == {
            "advance_command",
            "coverage_id",
            "cursor_version",
            "kind",
            "rearm",
            "reason",
            "registry_generation",
        }
        assert doorbell["rearm"]["work_in_flight"] is True
        assert doorbell["rearm"]["missing"] >= 1
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=2)

    rows = authority.read_all(
        "SELECT state, exit_reason FROM listener_coverage ORDER BY armed_at, coverage_id"
    )
    assert [(row["state"], row["exit_reason"]) for row in rows] == [
        ("EXITED", "event"),
    ]
    after = authority.active_lease("controller")
    assert after is not None
    assert after.renewed_at == before.renewed_at
    assert after.renew_deadline_at == before.renew_deadline_at


def test_wait_path_terminal_commit_wakes_under_seeded_history_three_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    env["GOALFLIGHT_CONTROLLER_LABEL"] = "controller"
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    dispatch_ids = [f"wait-latency-{index}" for index in range(3)]
    seeded_ids = [f"wait-seeded-history-{index:04d}" for index in range(1397)] + dispatch_ids
    for dispatch_id in seeded_ids:
        path = ledger.record_path(dispatch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "dispatch_id": dispatch_id,
                    "project_root": str(project),
                    "controller_label": "controller",
                    "state": "running",
                }
            ),
            encoding="utf-8",
        )

    measurements: list[float] = []
    for index, dispatch_id in enumerate(dispatch_ids, start=1):
        prepared = authority.prepare_attempt(dispatch_id)
        assert prepared.committed and prepared.value is not None
        waiter = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_status.py"),
                "--project",
                str(project),
                "--wait",
                dispatch_id,
                "--timeout-s",
                "8",
                "--poll-s",
                "0.01",
            ],
            cwd=project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                live = wake.live_waiters(
                    project,
                    controller_label="controller",
                )
                if any(
                    row.kind == "wait"
                    for row in (live or [])
                ):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("wait latency probe never armed")

            terminal_started = time.monotonic()
            committed = authority.commit_terminal(
                prepared.value.attempt_id,
                terminal_state="complete",
                observation={"state": "complete", "outcome": {}},
            )
            assert committed.committed
            stdout, stderr = waiter.communicate(timeout=6)
            elapsed = time.monotonic() - terminal_started
            assert waiter.returncode == 0, stderr
            assert "wait complete: 1/1 terminal" in stdout
            assert elapsed < 5.0
            measurements.append(elapsed)
            print(
                f"WAIT_PATH_PROBE run={index} seeded={len(seeded_ids)} "
                f"seconds={elapsed:.3f}"
            )
        finally:
            if waiter.poll() is None:
                waiter.terminate()
                waiter.wait(timeout=2)

    assert len(measurements) == 3
    assert all(value < 5.0 for value in measurements)


def test_reconcile_projects_wake_before_slow_failing_history_mutation_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reverting wake-before-history makes the first event wait time out."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    _claim(authority)
    dispatch_id = "reconcile-wake-before-history"
    status_path = tmp_path / "terminal-status.json"
    status_path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "state": "complete",
                "terminal_state": "complete",
            }
        ),
        encoding="utf-8",
    )
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "project_root": str(project),
            "state": "running",
            "status_path": str(status_path),
        }
    )

    projected = threading.Event()
    history_entered = threading.Event()
    release_history = threading.Event()
    original_project_outbox = journal.Journal.project_terminal_outbox

    def observe_outbox(self, *args, **kwargs):
        result = original_project_outbox(self, *args, **kwargs)
        projected.set()
        return result

    def slow_failing_history(_record: dict) -> None:
        history_entered.set()
        assert release_history.wait(5), "slow history hook was not released"
        raise RuntimeError("constructed history failure")

    monkeypatch.setattr(journal.Journal, "project_terminal_outbox", observe_outbox)
    monkeypatch.setattr(
        ledger.goalflight_fleet_console_history,
        "project_terminal",
        slow_failing_history,
    )
    outcome: dict[str, object] = {}

    def reconcile() -> None:
        try:
            outcome["result"] = ledger.reconcile_terminal_outbox(
                project,
                messages_dir=tmp_path / "messages",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcome["error"] = exc

    worker = threading.Thread(target=reconcile, name="reconcile-history-order")
    worker.start()
    try:
        assert projected.wait(2), "terminal wake projection was delayed by history"
        assert history_entered.wait(2), "history hook was not attempted after projection"
        assert worker.is_alive(), "slow history hook did not block the reconciliation tail"
    finally:
        release_history.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert "error" not in outcome, outcome.get("error")
    result = outcome["result"]
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["projected"] == 1


def test_listener_path_terminal_commit_wakes_under_seeded_history_three_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    env["GOALFLIGHT_TEST_LISTENER_START_TOKEN"] = "listener-latency-token"
    project = _project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority)
    dispatch_ids = [f"listener-latency-{index}" for index in range(3)]

    seeded_ids = [f"seeded-history-{index:04d}" for index in range(1397)] + dispatch_ids
    for dispatch_id in seeded_ids:
        path = ledger.record_path(dispatch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "dispatch_id": dispatch_id,
                    "project_root": str(project),
                    "controller_label": "controller",
                    "state": "running",
                }
            ),
            encoding="utf-8",
        )

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
        "8",
        "--json",
    ]
    measurements: list[float] = []
    for index, dispatch_id in enumerate(dispatch_ids, start=1):
        prepared = authority.prepare_attempt(dispatch_id)
        assert prepared.committed and prepared.value is not None
        listener = subprocess.Popen(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                coverage = authority.active_coverage("controller")
                if coverage is not None and coverage["pid"] == listener.pid:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("listener latency probe never armed its real doorbell")

            terminal_started = time.monotonic()
            committed = authority.commit_terminal(
                prepared.value.attempt_id,
                terminal_state="complete",
                observation={"state": "complete", "outcome": {}},
            )
            assert committed.committed
            projected = authority.project_terminal_outbox(
                messages_dir=tmp_path / "messages"
            )
            assert len(projected) == 1
            stdout, stderr = listener.communicate(timeout=6)
            elapsed = time.monotonic() - terminal_started
            assert listener.returncode == 0, stderr
            assert json.loads(stdout)["reason"] == "event"
            assert elapsed < 5.0
            measurements.append(elapsed)
            print(
                f"LISTENER_PATH_PROBE run={index} seeded={len(seeded_ids)} "
                f"seconds={elapsed:.3f}"
            )
            peek = authority.cursor_peek(
                "controller",
                nonce=lease.nonce,
                waking_only=False,
            )
            advances: dict[str, int] = {}
            for item in peek.items:
                stream_id = str(item["stream_id"])
                advances[stream_id] = max(
                    advances.get(stream_id, 0),
                    int(item["stream_seq"]),
                )
            assert advances
            assert authority.advance_cursor(
                "controller",
                nonce=lease.nonce,
                expected_cursor_version=peek.cursor_version,
                expected_stream_snapshots=peek.stream_snapshots,
                advances=advances,
                actor="listener-latency-probe",
            ).committed
        finally:
            if listener.poll() is None:
                listener.terminate()
                listener.wait(timeout=2)

    assert len(measurements) == 3
    assert all(value < 5.0 for value in measurements)


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


def test_every_dispatch_writer_stores_main_root_before_worktree_disappears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    main = tmp_path / "canonical-main"
    worktree = tmp_path / "disposable-worktree"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=main, check=True
    )
    subprocess.run(["git", "config", "user.name", "P3 Test"], cwd=main, check=True)
    (main / "seed").write_text("seed\n")
    subprocess.run(["git", "add", "seed"], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=main, check=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(worktree)],
        cwd=main,
        check=True,
    )
    canonical_main = str(main.resolve())

    monkeypatch.chdir(worktree)
    assert ledger.main(
        [
            "record",
            "--dispatch-id",
            "local-worktree-writer",
            "--agent",
            "test-worker",
            "--project-root",
            ".",
            "--state",
            "complete",
            "--json",
        ]
    ) == 0
    preview = fleet_dispatch.DispatchPreview(
        dispatch_id="fleet-worktree-writer",
        node_id="test-node",
        agent="codex-acp",
        billing_account="openai/test",
        prompt="race.md",
        worktree_path="/remote/disposable",
        base_sha="a" * 40,
    )
    fleet_dispatch.record_dispatch_ledger(
        preview,
        fleet_dispatch.LockChainResult(remote_lease_id="remote-lease"),
    )

    for dispatch_id in ("local-worktree-writer", "fleet-worktree-writer"):
        stored = json.loads(ledger.record_path(dispatch_id, create=False).read_text())
        assert stored["project_root"] == canonical_main

    authority = journal.open_or_create_journal(main)
    lease = _claim(authority, "surviving-controller")
    monkeypatch.chdir(main)
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=main,
        check=True,
    )
    assert not worktree.exists()

    for dispatch_id in ("local-worktree-writer", "fleet-worktree-writer"):
        messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="result",
            payload={"complete": True, "text": "after worktree removal"},
            messages_dir=tmp_path / "messages",
            source={"node": "test", "adapter": "pytest", "transport": "journal"},
        )
    pending = authority.cursor_peek("surviving-controller", nonce=lease.nonce)
    assert {str(row["stream_id"]) for row in pending.items} == {
        "local-worktree-writer",
        "fleet-worktree-writer",
    }


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
    env = _set_state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GOALFLIGHT_ROOT", str(ROOT))
    env["GOALFLIGHT_ROOT"] = str(ROOT)
    env["GOALFLIGHT_TEST_LISTENER_START_TOKEN"] = "drain-loop-listener-token"
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
    messages.post_message(
        dispatch_id="task-store:goal-flight-hidden",
        msg_type="controller-notice",
        payload={"text": "second stream with delimiter-shaped name"},
        messages_dir=tmp_path / "messages",
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("hidden", project_root=project),
    )

    watermark = status._mail_watermark(str(project), ["hidden-stream"])
    assert watermark is not None and len(watermark) == 1
    unread, unread_error = sessions._addressed_unread_counts(project)
    assert unread_error is None and unread == {"hidden": 2}
    summary = messages.controller_mail_summary(task_store_project_root=project)
    assert summary["count"] == 2

    before = authority.cursor_status("hidden")
    monkeypatch.setattr(
        sessions,
        "claim_controller_startup",
        lambda *args, **kwargs: pytest.fail("peek-only relay attempted to claim a lease"),
    )
    assert messages.main(
        [
            "--messages-dir",
            str(tmp_path / "messages"),
            "relay",
            "--new",
            "--json",
        ]
    ) == 0
    relay_payload = json.loads(capsys.readouterr().out)
    assert relay_payload["positions"] == {
        "hidden-stream": 1,
        "task-store:goal-flight-hidden": 1,
    }
    assert relay_payload["cursor_version"] == before["cursor_version"]
    advance_argv = shlex.split(relay_payload["advance_command"])
    assert advance_argv[-3:] == [
        "--position",
        "hidden-stream=1",
        "task-store:goal-flight-hidden=1",
    ]
    assert "cursor_token" not in relay_payload
    assert "more_pending" not in relay_payload
    assert authority.cursor_status("hidden") == before
    assert not (tmp_path / "messages" / ".read-cursor.json").exists()
    assert not (tmp_path / "messages" / ".ack-cursor.json").exists()
    assert not hasattr(messages, "load_read_cursor")

    advanced = subprocess.run(
        advance_argv,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert advanced.returncode == 0, (advanced.stdout, advanced.stderr)
    assert status._mail_watermark(str(project), ["hidden-stream"]) == watermark
    unread_after, unread_after_error = sessions._addressed_unread_counts(project)
    assert unread_after_error is None and unread_after == {"hidden": 0}

    listener_command = [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen",
        "--project-root",
        str(project),
        "--controller-label",
        "hidden",
        "--lease-nonce",
        lease.nonce,
        "--poll-secs",
        "0.01",
        "--timeout-s",
        "5",
        "--json",
    ]
    listener = subprocess.Popen(
        listener_command,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            coverage = authority.active_coverage("hidden")
            if coverage is not None and coverage["pid"] == listener.pid:
                break
            time.sleep(0.01)
        else:
            pytest.fail("drain-loop listener never armed")
        time.sleep(0.15)
        assert listener.poll() is None, "drained cursor immediately re-fired the doorbell"

        messages.post_message(
            dispatch_id="hidden-stream",
            msg_type="controller-notice",
            payload={"text": "fresh mail after re-arm"},
            messages_dir=tmp_path / "messages",
            source={"node": "test-node", "adapter": "test", "transport": "controller"},
            addressee=messages.controller_addressee("hidden", project_root=project),
        )
        listener_stdout, listener_stderr = listener.communicate(timeout=3)
        assert listener.returncode == 0, listener_stderr
        doorbell = json.loads(listener_stdout)
        assert "advance --project-root" in doorbell["advance_command"]
        assert "hidden-stream=2" in doorbell["advance_command"]
    finally:
        if listener.poll() is None:
            listener.kill()
            listener.communicate(timeout=3)

    retired = sessions.retire_controller(
        project,
        "hidden",
        session_id=lease.nonce,
        acknowledge=True,
        ledger_records=[],
    )
    assert retired["retired"] is True
    assert authority.active_lease("hidden") is None
