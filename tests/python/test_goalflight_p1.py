#!/usr/bin/env python3
"""P1 journal, carrier transaction, validation, and quarantine contracts."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_steer_mailbox as steer  # noqa: E402
import goalflight_task as task  # noqa: E402


def _set_state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal-state"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "dispatch-state"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    monkeypatch.delenv("GOALFLIGHT_DISABLE_NUDGES", raising=False)


def _subprocess_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
            "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journal-state"),
            "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
            "GOALFLIGHT_STATE_DIR": str(tmp_path / "dispatch-state"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
            "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
            "PYTHONPATH": os.pathsep.join(
                [str(SCRIPTS), str(ROOT), env.get("PYTHONPATH", "")]
            ),
        }
    )
    return env


def _run_python(code: str, *args: Path | str, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", code, *(str(arg) for arg in args)],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "p1-test",
            "GIT_AUTHOR_EMAIL": "p1@example.invalid",
            "GIT_COMMITTER_NAME": "p1-test",
            "GIT_COMMITTER_EMAIL": "p1@example.invalid",
        },
    )


def _carrier_add(path: Path, envelope: dict) -> None:
    messages.update_envelopes(path, lambda existing: (existing + [envelope], None))


def test_journal_two_subprocesses_race_true_fresh_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    expected_path = journal.resolve_journal_path(project)
    assert not expected_path.exists(), "parent must not pre-bootstrap the journal"
    start = tmp_path / "release-bootstrap-race"
    code = """
import json, sys, time
from pathlib import Path
from goalflight_journal import Journal, JournalError, RowOperation, utc_now
start = Path(sys.argv[2])
while not start.exists():
    time.sleep(0.002)
try:
    opened = Journal.create(Path(sys.argv[1]), retry_budget_s=3.0)
    created = True
except JournalError:
    opened = Journal(Path(sys.argv[1]), retry_budget_s=3.0)
    created = False
result = opened.write(RowOperation.update(
    "journal_epochs", {"updated_at": utc_now()}, where={"singleton": 1}, row_cap=1,
    expected_rows=1
))
print(json.dumps({"path": str(opened.path), "mode": opened.read_all("PRAGMA journal_mode")[0][0],
                  "disposition": result.disposition.value, "created": created}))
"""
    processes = [
        _run_python(code, project, start, env=_subprocess_env(tmp_path)) for _ in range(2)
    ]
    assert not expected_path.exists(), "workers must be synchronized before creation"
    start.touch()
    rows: list[dict] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        rows.append(json.loads(stdout))
    assert {row["path"] for row in rows} == {str(expected_path)}
    assert {row["mode"] for row in rows} == {"wal"}
    assert {row["disposition"] for row in rows} == {"committed"}
    assert sorted(row["created"] for row in rows) == [False, True]
    opened = journal.Journal(project)
    assert opened.epochs() == journal.JournalEpochs(
        journal.CURRENT_SCHEMA_EPOCH,
        journal.CURRENT_PROTOCOL_EPOCH,
        journal.CURRENT_REGISTRY_EPOCH,
        journal.CURRENT_READER_EPOCH,
        journal.CURRENT_WRITER_EPOCH,
    )
    assert opened.read_all(
        "SELECT value FROM journal_meta WHERE key = 'journal_identity'"
    )[0][0] == journal.JOURNAL_IDENTITY_VALUE


def test_journal_path_collapses_a_real_linked_git_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    main = tmp_path / "project-main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(main, "add", "tracked.txt")
    _git(main, "commit", "-qm", "initial")
    linked = tmp_path / "project-linked"
    _git(main, "worktree", "add", "-q", "-b", "linked-test", str(linked))

    main_path = journal.resolve_journal_path(main)
    linked_path = journal.resolve_journal_path(linked)
    assert main_path == linked_path
    journal.Journal.create(main)
    assert journal.Journal(main).path == journal.Journal(linked).path == main_path
    assert task.resolve_project_root(str(linked)) == main


def test_journal_declarative_write_bounds_busy_and_cas_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    contender = journal.Journal.create(
        project,
        retry_budget_s=0.06,
        transaction_budget_s=0.5,
        jitter_min_s=0.005,
        jitter_max_s=0.010,
    )
    with pytest.raises(TypeError, match="not callables"):
        contender.write(lambda _connection: None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a RowOperation"):
        contender.write([object()])  # type: ignore[list-item]

    class OverriddenOperation(journal.RowOperation):
        def compiled(self) -> tuple[str, tuple[object, ...]]:
            return "DELETE FROM journal_epochs", ()

    overridden = OverriddenOperation(
        "update",
        "journal_epochs",
        (("updated_at", journal.utc_now()),),
        (("singleton", 1),),
        1,
        1,
    )
    with pytest.raises(TypeError, match="may not override compiled"):
        contender.write(overridden)

    class OverriddenConstructor(journal.RowOperation):
        def __init__(self) -> None:
            object.__setattr__(self, "kind", "delete")
            object.__setattr__(self, "table", "journal_epochs")
            object.__setattr__(self, "expected_rows", None)
            object.__setattr__(self, "_sql", "DELETE FROM journal_epochs")
            object.__setattr__(self, "_parameters", ())
            object.__setattr__(self, "_parameter_bytes", 0)

    with pytest.raises(TypeError, match="may not override __init__"):
        contender.write(OverriddenConstructor())
    with pytest.raises(TypeError, match="row_cap"):
        journal.RowOperation.update(
            "journal_epochs",
            {"updated_at": journal.utc_now()},
            where={"singleton": 1},
        )
    with pytest.raises(TypeError, match="row_cap"):
        journal.RowOperation.delete("journal_epochs", where={"singleton": 1})

    class SQLiteAdapter:
        def __conform__(self, _protocol):
            raise AssertionError("SQLite adapter must never run")

    with pytest.raises(ValueError, match="parameter type SQLiteAdapter refused"):
        journal.RowOperation.update(
            "journal_epochs",
            {"updated_at": SQLiteAdapter()},
            where={"singleton": 1},
            row_cap=1,
        )
    with pytest.raises(ValueError, match="canonical JSON"):
        journal.RowOperation.update(
            "journal_epochs",
            {"updated_at": "x" * (journal.MAX_PARAMETER_VALUE_BYTES + 1)},
            where={"singleton": 1},
            row_cap=1,
        )
    with pytest.raises(ValueError, match="signed 64-bit"):
        journal.RowOperation.update(
            "journal_epochs",
            {"protocol_epoch": 2**63},
            where={"singleton": 1},
            row_cap=1,
        )
    with pytest.raises(ValueError, match=str(journal.MAX_OPERATION_ROWS)):
        journal.RowOperation.delete(
            "journal_epochs",
            where={"singleton": 1},
            row_cap=journal.MAX_OPERATION_ROWS + 1,
        )
    assert "P2" in str(journal.Journal.write.__doc__)

    with sqlite3.connect(contender.path) as connection:
        connection.execute("CREATE TABLE cap_probe (value TEXT, bucket INTEGER)")
        connection.executemany(
            "INSERT INTO cap_probe (value, bucket) VALUES (?, 1)",
            [("before",), ("before",), ("before",)],
        )
    capped = contender.write(
        journal.RowOperation.update(
            "cap_probe",
            {"value": "after"},
            where={"bucket": 1},
            row_cap=2,
            expected_rows=2,
        )
    )
    assert capped.committed
    assert [row[0] for row in contender.read_all(
        "SELECT value FROM cap_probe ORDER BY rowid"
    )].count("after") == 2
    deleted = contender.write(
        journal.RowOperation.delete(
            "cap_probe",
            where={"bucket": 1},
            row_cap=1,
            expected_rows=1,
        )
    )
    assert deleted.committed
    assert contender.read_all("SELECT COUNT(*) FROM cap_probe")[0][0] == 2
    assert journal.MAX_TRANSACTION_OPERATIONS == 128
    with pytest.raises(ValueError, match=str(journal.MAX_TRANSACTION_OPERATIONS)):
        contender.write(
            [
                journal.RowOperation.update(
                    "journal_epochs", {"updated_at": journal.utc_now()}, where={"singleton": 1},
                    row_cap=1,
                )
                for _ in range(129)
            ]
        )

    locked = tmp_path / "holder-has-immediate-transaction"
    holder_code = """
import sqlite3, sys, time
from pathlib import Path
connection = sqlite3.connect(sys.argv[1], timeout=0)
connection.execute("BEGIN IMMEDIATE")
Path(sys.argv[2]).touch()
time.sleep(0.35)
connection.rollback()
connection.close()
"""
    holder = _run_python(holder_code, contender.path, locked, env=_subprocess_env(tmp_path))
    deadline = time.monotonic() + 5
    while not locked.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert locked.exists()
    busy = contender.write(
        journal.RowOperation.update(
            "journal_epochs",
            {"updated_at": journal.utc_now()},
            where={"singleton": 1},
            row_cap=1,
            expected_rows=1,
        )
    )
    assert busy.retryable and not busy.cas_lost
    stdout, stderr = holder.communicate(timeout=5)
    assert holder.returncode == 0, f"stdout={stdout}; stderr={stderr}"

    lost = contender.write(
        journal.RowOperation.update(
            "journal_epochs",
            {"updated_at": journal.utc_now()},
            where={"singleton": 999},
            row_cap=1,
            expected_rows=1,
        )
    )
    assert lost.cas_lost and not lost.retryable
    deadline_result = journal.Journal(project, transaction_budget_s=1e-12).write(
        journal.RowOperation.update(
            "journal_epochs", {"updated_at": journal.utc_now()}, where={"singleton": 1},
            row_cap=1,
        )
    )
    assert deadline_result.retryable and "transaction exceeded" in str(deadline_result.reason)


def test_existing_journal_missing_required_tables_and_bad_epoch_types_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    for index, missing_table in enumerate(("journal_epochs", "journal_meta")):
        project = tmp_path / f"missing-{index}"
        project.mkdir()
        path = journal.Journal.create(project).path
        with sqlite3.connect(path) as connection:
            schema_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'journal_epochs'"
                ).fetchone()[0]
            )
            assert schema_sql.count("typeof(") == 6
            connection.execute(f"DROP TABLE {missing_table}")
        with pytest.raises(journal.JournalIntegrityError, match="Restore a validated WAL-safe backup"):
            journal.Journal(
                project,
                retry_budget_s=0.01,
                jitter_min_s=0.001,
                jitter_max_s=0.002,
            )

    typed_project = tmp_path / "bad-epoch-type"
    typed_project.mkdir()
    opened = journal.Journal.create(typed_project)
    with sqlite3.connect(opened.path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE journal_epochs SET schema_epoch = 'not-an-int'")
    with pytest.raises(journal.JournalIntegrityError, match="non-integer storage class"):
        opened.epochs()


def test_present_journal_open_failure_retries_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "present-open-retry"
    project.mkdir()
    authority = journal.Journal.create(project)
    real_connect = journal.sqlite3.connect
    failed_opens = 0

    def fail_first_rw_open(database: object, *args: object, **kwargs: object):
        nonlocal failed_opens
        if "?mode=rw" in str(database) and failed_opens == 0:
            failed_opens += 1
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(journal.sqlite3, "connect", fail_first_rw_open)
    reopened = journal.Journal(
        project,
        open_retry_budget_s=0.1,
        jitter_min_s=0.001,
        jitter_max_s=0.002,
    )

    assert failed_opens == 1
    assert reopened.path == authority.path
    assert reopened.epochs().schema == journal.CURRENT_SCHEMA_EPOCH


def test_present_journal_permanent_open_failure_is_bounded_io_not_disappearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "present-open-exhausted"
    project.mkdir()
    authority = journal.Journal.create(project)
    attempts = 0

    def reject_every_open(*_args: object, **_kwargs: object):
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(journal.sqlite3, "connect", reject_every_open)
    started = time.monotonic()
    with pytest.raises(journal.JournalIOError) as captured:
        journal.Journal(
            project,
            open_retry_budget_s=0.02,
            jitter_min_s=0.001,
            jitter_max_s=0.002,
        )
    elapsed = time.monotonic() - started

    assert authority.path.exists(), "the injected opener must not remove the journal"
    assert attempts > 1, "a first-open exit silently defeats transient survival"
    assert elapsed < 0.5, "the retry budget must remain a bound"
    assert "still present" in str(captured.value)
    assert "after" in str(captured.value)
    assert not isinstance(captured.value, journal.JournalDisappeared)


def test_genuinely_absent_journal_keeps_disappearance_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "genuinely-absent"
    project.mkdir()

    with pytest.raises(journal.JournalDisappeared, match="journal database is absent"):
        journal.Journal(project, open_retry_budget_s=0.02)


def test_unreadable_journal_parent_is_io_not_disappearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "unreadable-parent"
    project.mkdir()
    authority = journal.Journal.create(project)
    journal_dir = authority.path.parent
    os.chmod(journal_dir, 0o000)
    try:
        with pytest.raises(journal.JournalIOError, match="disappearance is unverified"):
            journal.Journal.open_reader(project)
        with pytest.raises(journal.JournalIOError):
            journal.Journal(project, open_retry_budget_s=0.02)
    finally:
        os.chmod(journal_dir, 0o700)


def test_open_retry_still_detects_replacement_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "replaced-during-open"
    project.mkdir()
    authority = journal.Journal.create(project)
    with sqlite3.connect(authority.path) as connection:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
            0,
            0,
            0,
        )
    for suffix in ("-shm", "-wal"):
        Path(f"{authority.path}{suffix}").unlink(missing_ok=True)
    different_project = tmp_path / "different-project"
    different_project.mkdir()
    replacement_authority = journal.Journal.create(different_project)
    with sqlite3.connect(replacement_authority.path) as connection:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
            0,
            0,
            0,
        )
    for suffix in ("-shm", "-wal"):
        Path(f"{replacement_authority.path}{suffix}").unlink(missing_ok=True)
    replacement = replacement_authority.path

    real_connect = journal.sqlite3.connect
    replaced = False

    def replace_then_fail(database: object, *args: object, **kwargs: object):
        nonlocal replaced
        if "?mode=rw" in str(database) and not replaced:
            os.replace(replacement, authority.path)
            replaced = True
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(journal.sqlite3, "connect", replace_then_fail)
    with pytest.raises(
        journal.JournalIntegrityError,
        match="journal database was replaced",
    ):
        journal.Journal(
            project,
            retry_budget_s=0.01,
            open_retry_budget_s=0.1,
            jitter_min_s=0.001,
            jitter_max_s=0.002,
        )

    assert replaced


def test_journal_epoch_fence_rechecks_declarative_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    opened = journal.Journal.create(project)
    advanced_protocol = journal.CURRENT_PROTOCOL_EPOCH + 1
    advanced = opened.write(
        journal.RowOperation.update(
            "journal_epochs",
            {"protocol_epoch": advanced_protocol, "updated_at": journal.utc_now()},
            where={"singleton": 1},
            row_cap=1,
            expected_rows=1,
        )
    )
    assert advanced.committed
    with pytest.raises(journal.JournalUpgradeRequired, match="UPGRADE_REQUIRED"):
        opened.epochs()
    with pytest.raises(
        journal.JournalUpgradeRequired,
        match=rf"protocol client={journal.CURRENT_PROTOCOL_EPOCH} journal={advanced_protocol}",
    ):
        journal.Journal(project)
    with pytest.raises(
        journal.JournalUpgradeRequired,
        match=rf"protocol client={journal.CURRENT_PROTOCOL_EPOCH} journal={advanced_protocol}",
    ):
        opened.write(
            journal.RowOperation.update(
                "journal_epochs", {"updated_at": journal.utc_now()}, where={"singleton": 1},
                row_cap=1,
            )
        )


def test_journal_operator_cli_inspect_dump_snapshot_and_guarded_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    snapshot = tmp_path / "backups" / "validated.sqlite3"
    env = _subprocess_env(tmp_path)
    base = [sys.executable, str(SCRIPTS / "goalflight_journal.py"), "--project-root", str(project)]

    absent_commands = (
        [*base, "inspect"],
        [*base, "dump"],
        [*base, "snapshot", "--output", str(snapshot)],
        [*base, "restore", "--snapshot", str(snapshot)],
    )
    for command in absent_commands:
        absent = subprocess.run(
            command, cwd=ROOT, env=env, text=True, capture_output=True, check=False
        )
        assert absent.returncode == 2 and "absent" in absent.stderr
    initialized = subprocess.run(
        [*base, "init"], cwd=ROOT, env=env, text=True, capture_output=True, check=False
    )
    assert initialized.returncode == 0, initialized.stderr
    duplicate_init = subprocess.run(
        [*base, "init"], cwd=ROOT, env=env, text=True, capture_output=True, check=False
    )
    assert duplicate_init.returncode == 2 and "already exists" in duplicate_init.stderr
    opened = journal.Journal(project)

    inspected = subprocess.run(
        [*base, "inspect"], cwd=ROOT, env=env, text=True, capture_output=True, check=False
    )
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout)["integrity"] == "ok"
    dumped = subprocess.run(
        [*base, "dump"], cwd=ROOT, env=env, text=True, capture_output=True, check=False
    )
    assert dumped.returncode == 0 and "CREATE TABLE journal_meta" in dumped.stdout
    snapped = subprocess.run(
        [*base, "snapshot", "--output", str(snapshot)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert snapped.returncode == 0, snapped.stderr
    expected_epochs = journal.JournalEpochs(
        journal.CURRENT_SCHEMA_EPOCH,
        journal.CURRENT_PROTOCOL_EPOCH,
        journal.CURRENT_REGISTRY_EPOCH,
        journal.CURRENT_READER_EPOCH,
        journal.CURRENT_WRITER_EPOCH,
    )
    assert journal._validate_snapshot_file(snapshot) == expected_epochs

    refused = subprocess.run(
        [*base, "restore", "--snapshot", str(snapshot)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert refused.returncode == 2 and "--i-understand" in refused.stderr
    opened.write(
        journal.RowOperation.update(
            "journal_epochs", {"updated_at": "changed-after-snapshot"},
            where={"singleton": 1}, row_cap=1,
        )
    )
    restored = subprocess.run(
        [*base, "restore", "--snapshot", str(snapshot), "--i-understand"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert restored.returncode == 0, restored.stderr
    assert journal.Journal(project).inspect()["integrity"] == "ok"

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not sqlite")
    bad_restore = subprocess.run(
        [*base, "restore", "--snapshot", str(corrupt), "--i-understand"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert bad_restore.returncode == 2 and "validation failed" in bad_restore.stderr

    incompatible = tmp_path / "incompatible.sqlite3"
    with sqlite3.connect(snapshot) as source, sqlite3.connect(incompatible) as target:
        source.backup(target)
        target.execute(
            "UPDATE journal_epochs SET protocol_epoch = ? WHERE singleton = 1",
            (journal.CURRENT_PROTOCOL_EPOCH + 1,),
        )
    incompatible_restore = subprocess.run(
        [*base, "restore", "--snapshot", str(incompatible), "--i-understand"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert incompatible_restore.returncode == 2
    assert "refused restore before replacement" in incompatible_restore.stderr
    assert journal.Journal(project).epochs() == expected_epochs


def test_restore_holds_write_domain_validates_copy_and_rolls_back_post_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    opened = journal.Journal.create(project)
    snapshot = opened.snapshot(tmp_path / "snapshot.sqlite3")
    opened.write(
        journal.RowOperation.update(
            "journal_epochs",
            {"updated_at": "live-preimage"},
            where={"singleton": 1},
            row_cap=1,
            expected_rows=1,
        )
    )

    result: dict[str, object] = {}

    def restore_in_thread() -> None:
        try:
            result["path"] = journal.restore_snapshot(project, snapshot, i_understand=True)
        except BaseException as exc:  # pragma: no cover - asserted below
            result["error"] = exc

    with task.FileLock(journal.journal_write_lock_path(opened.path)):
        thread = threading.Thread(target=restore_in_thread)
        thread.start()
        time.sleep(0.05)
        assert thread.is_alive() and result == {}
    thread.join(timeout=5)
    assert not thread.is_alive() and result == {"path": opened.path}

    real_validate = journal._validate_snapshot_file
    copied_paths: list[Path] = []

    def mutate_source_after_copy(path: Path) -> journal.JournalEpochs:
        epochs = real_validate(path)
        if ".restore-copy-" in path.name:
            copied_paths.append(path)
            with sqlite3.connect(snapshot) as connection:
                connection.execute(
                    "UPDATE journal_epochs SET updated_at = 'source-swapped' WHERE singleton = 1"
                )
        return epochs

    monkeypatch.setattr(journal, "_validate_snapshot_file", mutate_source_after_copy)
    journal.restore_snapshot(project, snapshot, i_understand=True)
    assert copied_paths
    assert journal.Journal(project).read_all(
        "SELECT updated_at FROM journal_epochs WHERE singleton = 1"
    )[0][0] != "source-swapped"

    monkeypatch.setattr(journal, "_validate_snapshot_file", real_validate)
    journal.Journal(project).write(
        journal.RowOperation.update(
            "journal_epochs",
            {"updated_at": "rollback-preimage"},
            where={"singleton": 1},
            row_cap=1,
            expected_rows=1,
        )
    )

    def fail_post_install(path: Path) -> journal.JournalEpochs:
        if path == opened.path:
            raise journal.JournalIntegrityError("forced post-install validation failure")
        return real_validate(path)

    monkeypatch.setattr(journal, "_validate_snapshot_file", fail_post_install)
    with pytest.raises(journal.JournalIntegrityError, match="forced post-install"):
        journal.restore_snapshot(project, snapshot, i_understand=True)
    monkeypatch.setattr(journal, "_validate_snapshot_file", real_validate)
    assert journal.Journal(project).read_all(
        "SELECT updated_at FROM journal_epochs WHERE singleton = 1"
    )[0][0] == "rollback-preimage"
    assert not list(opened.path.parent.glob(f".{opened.path.name}.restore-*"))

    for suffix in ("-wal", "-shm"):
        Path(f"{opened.path}{suffix}").unlink(missing_ok=True)
    opened.path.write_bytes(b"unreadable live preimage")
    with pytest.raises(journal.JournalIntegrityError, match="validation failed"):
        journal.restore_snapshot(project, snapshot, i_understand=True)
    assert opened.path.read_bytes() == b"unreadable live preimage"


def test_carrier_rewrite_and_append_are_serialized_across_real_processes(tmp_path: Path) -> None:
    messages_dir = tmp_path / "messages"
    messages.post_message(
        dispatch_id="carrier-race",
        msg_type="status",
        payload={"text": "replace me"},
        messages_dir=messages_dir,
    )
    path = messages.inbox_path(messages_dir, "carrier-race")
    entered = tmp_path / "rewrite-entered"
    release = tmp_path / "release-rewrite"
    rewrite_code = """
import sys, time
from pathlib import Path
import goalflight_messages as messages
path, entered, release = map(Path, sys.argv[1:4])
rewritten = messages.markers_to_envelopes(
    {"STATUS": ["rewritten"]}, dispatch_id="carrier-race", seq_start=2
)[0]
def update(_existing):
    entered.touch()
    while not release.exists():
        time.sleep(0.002)
    return [rewritten], None
messages.update_envelopes(path, update)
"""
    append_code = """
import sys
from pathlib import Path
import goalflight_messages as messages
messages.post_message(dispatch_id="carrier-race", msg_type="user_need",
                      payload={"text": "concurrent append"}, messages_dir=Path(sys.argv[1]))
"""
    rewriter = _run_python(
        rewrite_code, path, entered, release, env=_subprocess_env(tmp_path)
    )
    deadline = time.monotonic() + 5
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert entered.exists()
    appender = _run_python(append_code, messages_dir, env=_subprocess_env(tmp_path))
    time.sleep(0.03)
    release.touch()
    for process in (rewriter, appender):
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, f"stdout={stdout}; stderr={stderr}"
    loaded = messages.read_envelopes(path)
    assert [item["payload"]["text"] for item in loaded] == ["rewritten", "concurrent append"]
    assert [item["seq"] for item in loaded] == [2, 3]
    assert messages.mail_lock_path(path).exists()
    assert not list(path.parent.glob(f".{path.name}.tmp-*"))


def test_carrier_read_state_refuses_writes_when_the_carrier_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages_dir = tmp_path / "messages"
    posted = messages.post_message(
        dispatch_id="carrier-state",
        msg_type="status",
        payload={"text": "preserve"},
        messages_dir=messages_dir,
    )
    path = Path(posted["path"])
    original = path.read_bytes()
    assert messages.read_envelopes_result(path).status is messages.CarrierReadStatus.OK
    with path.open("ab") as handle:
        handle.write(b"{broken record\n")
    quarantined = messages.read_envelopes_result(path)
    assert quarantined.status is messages.CarrierReadStatus.CORRUPT_RECORDS_QUARANTINED
    path.write_bytes(original)

    real_read = messages._read_nofollow_bytes

    def refuse_target(candidate: Path) -> bytes:
        if candidate == path:
            raise messages.MessageError(f"{candidate}: forced unreadable carrier")
        return real_read(candidate)

    monkeypatch.setattr(messages, "_read_nofollow_bytes", refuse_target)
    unreadable = messages.read_envelopes_result(path)
    assert unreadable.status is messages.CarrierReadStatus.CARRIER_UNREADABLE
    with pytest.raises(messages.MessageError, match="CARRIER-UNREADABLE: retryable"):
        messages.read_envelopes_tolerant(path)
    update_called = False

    def replacement(_existing: list[dict]) -> tuple[list[dict], None]:
        nonlocal update_called
        update_called = True
        return [], None

    with pytest.raises(messages.MessageError, match="CARRIER-UNREADABLE: retryable"):
        messages.update_envelopes(path, replacement)
    with pytest.raises(messages.MessageError, match="CARRIER-UNREADABLE: retryable"):
        messages.post_message(
            dispatch_id="carrier-state",
            msg_type="status",
            payload={"text": "must not allocate"},
            messages_dir=messages_dir,
        )
    with messages.carrier_transaction(path) as transaction:
        with pytest.raises(messages.MessageError, match="forced unreadable"):
            transaction.read_bytes()
        with pytest.raises(messages.MessageError, match="CARRIER-UNREADABLE"):
            transaction.replace_bytes(b"")
    assert not update_called
    assert path.read_bytes() == original


def test_task_nudge_coalesces_through_tolerant_carrier_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    for suffix in ("first", "second"):
        task._post_task_store_nudge(
            project_root=project,
            nudge_kind=task.RESUME_NUDGE_KIND,
            dedup_suffix=suffix,
            text=f"nudge {suffix}",
            payload={"frontier_ids": [suffix]},
            transport="pytest",
        )
        if suffix == "first":
            messages.post_message(
                dispatch_id=task._next_nudge_dispatch_id(project),
                msg_type="status",
                payload={"text": "unrelated carrier state"},
                messages_dir=tmp_path / "messages",
            )
    dispatch_id = task._next_nudge_dispatch_id(project)
    inbox = messages.inbox_path(tmp_path / "messages", dispatch_id)
    loaded = messages.read_envelopes(inbox)
    assert [item["type"] for item in loaded] == ["status", "user_need"]
    assert loaded[-1]["payload"]["frontier_ids"] == ["second"]
    assert not (inbox.parent / f".{dispatch_id}.lock").exists()
    assert messages.mail_lock_path(inbox).exists()


def test_stream_traversal_final_and_parent_symlinks_and_mismatch_are_refused(
    tmp_path: Path,
) -> None:
    messages_dir = tmp_path / "messages"
    for malformed in ("../escape", str(tmp_path / "absolute")):
        with pytest.raises(messages.MessageError, match="stream token"):
            messages.inbox_path(messages_dir, malformed)
    messages_dir.mkdir(exist_ok=True)
    final_link = messages_dir / "linked.jsonl"
    final_link.symlink_to(tmp_path / "target.jsonl")
    with pytest.raises(messages.MessageError, match="symlinked inbox refused"):
        messages.post_message(
            dispatch_id="linked",
            msg_type="status",
            payload={"text": "must not follow link"},
            messages_dir=messages_dir,
        )
    steer_target = tmp_path / "steer-target.jsonl"
    steer_link = tmp_path / "linked-steer.jsonl"
    steer_link.symlink_to(steer_target)
    with pytest.raises(messages.MessageError, match="symlinked inbox refused"):
        steer.read_steer_entries(steer_link)
    with pytest.raises(messages.MessageError, match="symlinked inbox refused"):
        steer.append_steer_entry(steer_link, "must not follow")
    assert not steer_target.exists()
    assert "zero-live-dispatch cutover gate" in str(steer.__doc__)
    actual_parent = tmp_path / "actual-parent"
    actual_parent.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(actual_parent, target_is_directory=True)
    resolved_parent_carrier = messages.inbox_path(parent_link, "parent-symlink")
    assert resolved_parent_carrier == actual_parent / "parent-symlink.jsonl"

    mismatch_path = messages.inbox_path(messages_dir, "expected-stream")
    mismatch = messages.markers_to_envelopes(
        {"STATUS": ["wrong carrier"]}, dispatch_id="different-stream"
    )[0]
    with pytest.raises(messages.MessageError, match="does not match stream"):
        _carrier_add(mismatch_path, mismatch)
    assert not mismatch_path.exists()
    assert not (tmp_path / "escape.jsonl").exists()


def test_envelope_validation_serializes_total_bounded_canonical_json(tmp_path: Path) -> None:
    messages_dir = tmp_path / "messages"
    path = messages.inbox_path(messages_dir, "value-check")
    invalid_payloads: list[object] = [
        [],
        {"value": object()},
        {"value": float("nan")},
        {"value": {1: "non-string-key"}},
        {"value": "x" * messages.MAX_PAYLOAD_JSON_BYTES},
    ]
    deep: object = "leaf"
    for _ in range(messages.MAX_JSON_DEPTH + 2):
        deep = {"nested": deep}
    invalid_payloads.append({"value": deep})
    for payload in invalid_payloads:
        with pytest.raises(messages.MessageError):
            messages.post_message(
                dispatch_id="value-check",
                msg_type="status",
                payload=payload,  # type: ignore[arg-type]
                messages_dir=messages_dir,
            )
    assert not path.exists()

    result = messages.post_message(
        dispatch_id="value-check",
        msg_type="status",
        payload={"z": "é", "a": [1, True, None]},
        messages_dir=messages_dir,
    )
    raw = path.read_text(encoding="utf-8")
    assert raw == result["line"] == messages.serialize_envelope_line(result["envelope"])
    assert '"payload":{"z":"é","a":[1,true,null]}' in raw

    project = tmp_path / "project"
    project.mkdir()
    invalid_root = {
        **result["envelope"],
        "type": "controller-notice",
        "addressee": {
            "kind": "controller",
            "label": "main",
            "project_root": str(project / ".." / project.name),
        },
    }
    with pytest.raises(messages.MessageError, match="expected canonical root"):
        messages.validate_envelope(invalid_root)


def test_validate_envelope_unresolvable_addressee_root_is_message_error_not_task_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validating stored mail whose addressee checkout vanished is a MessageError.

    Reverting the conversion leaves ``validate_envelope`` raising TaskError,
    which ``_read_envelope_records`` does not catch, so one stale addressee
    poisons the carrier. Capture of the same path must still refuse.
    """
    _set_state_env(monkeypatch, tmp_path)
    first = messages.post_message(
        dispatch_id="vanished-addressee",
        msg_type="status",
        payload={"text": "before"},
        messages_dir=tmp_path / "messages",
        project_journal_delivery=False,
    )
    missing = tmp_path / "deleted-checkout"
    assert not missing.exists()
    stored = {
        **first["envelope"],
        "type": "controller-notice",
        "addressee": {
            "kind": "controller",
            "label": "main",
            "project_root": str(missing),
        },
    }
    with pytest.raises(messages.MessageError, match="unresolvable project root") as caught:
        messages.validate_envelope(stored)
    assert not isinstance(caught.value, task.TaskError)
    assert "Refusing to write to another store" not in str(caught.value)
    with pytest.raises(task.TaskError, match="Refusing to write to another store"):
        task.resolve_project_root(str(missing))

    path = Path(first["path"])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(stored) + "\n")
    errors: list[dict[str, object]] = []
    loaded = messages.read_envelopes_tolerant(path, carrier_errors=errors)
    assert [item["payload"]["text"] for item in loaded] == ["before"]
    assert errors
    assert any("unresolvable project root" in str(error.get("error", "")) for error in errors)


def test_validate_envelope_resolvable_noncanonical_addressee_root_is_still_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two spellings of a live root must still fail validation.

    A linked worktree collapses to the main checkout. Storing the worktree
    path would hash a slug no reader watches. Reverting the unresolvable
    conversion must not be the only way this stays rejected: a live
    non-canonical spelling is MessageError, not a pass.
    """
    _set_state_env(monkeypatch, tmp_path)
    main = tmp_path / "project-main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(main, "add", "tracked.txt")
    _git(main, "commit", "-qm", "initial")
    linked = tmp_path / "project-linked"
    _git(main, "worktree", "add", "-q", "-b", "linked-test", str(linked))
    canonical = str(task.resolve_project_root(str(linked)))
    assert canonical == str(task.resolve_project_root(str(main)))
    assert str(linked) != canonical

    posted = messages.post_message(
        dispatch_id="noncanonical-addressee",
        msg_type="status",
        payload={"text": "valid"},
        messages_dir=tmp_path / "messages",
        project_journal_delivery=False,
    )
    stored = {
        **posted["envelope"],
        "type": "controller-notice",
        "addressee": {
            "kind": "controller",
            "label": "main",
            "project_root": str(linked),
        },
    }
    with pytest.raises(messages.MessageError, match="expected canonical root") as caught:
        messages.validate_envelope(stored)
    assert canonical in str(caught.value)
    assert "unresolvable project root" not in str(caught.value)
    assert not isinstance(caught.value, task.TaskError)


def test_record_local_value_and_recursion_failures_quarantine_and_writers_continue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages_dir = tmp_path / "messages"
    first = messages.post_message(
        dispatch_id="parse-failures",
        msg_type="status",
        payload={"text": "before"},
        messages_dir=messages_dir,
    )
    path = Path(first["path"])
    oversized_integer = b"9" * 5000
    recursive_json = b'{"force_recursion":true}'
    with path.open("ab") as handle:
        handle.write(oversized_integer + b"\n" + recursive_json + b"\n")
    real_loads = messages.json.loads

    def loads_with_record_local_recursion(value):
        if value == recursive_json.decode("utf-8"):
            raise RecursionError("forced record-local recursion")
        return real_loads(value)

    monkeypatch.setattr(messages.json, "loads", loads_with_record_local_recursion)
    second = messages.post_message(
        dispatch_id="parse-failures",
        msg_type="status",
        payload={"text": "after"},
        messages_dir=messages_dir,
    )
    assert second["envelope"]["seq"] == 2
    assert "WARNING: carrier corruption:" in capsys.readouterr().err
    errors: list[dict[str, object]] = []
    loaded = messages.read_envelopes_tolerant(path, carrier_errors=errors)
    assert [item["payload"]["text"] for item in loaded] == ["before", "after"]
    assert len(errors) == 2
    rows = [
        json.loads(line)
        for line in messages.quarantine_path(path).read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 2
    assert all(row["reason"].startswith("invalid JSON:") for row in rows)
    assert any("forced record-local recursion" in row["reason"] for row in rows)
    with pytest.raises(messages.MessageError, match="invalid JSON"):
        messages.read_envelopes(path)


def test_quarantine_dedup_uses_resolved_identity_for_relative_and_absolute_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages_dir = tmp_path / "messages"
    result = messages.post_message(
        dispatch_id="alias-dedup",
        msg_type="status",
        payload={"text": "valid"},
        messages_dir=messages_dir,
    )
    path = Path(result["path"])
    with path.open("ab") as handle:
        handle.write(b"{bad alias row\n")
    monkeypatch.chdir(path.parent)
    messages.read_envelopes_tolerant(Path(path.name), carrier_errors=[])
    messages.read_envelopes_tolerant(path, carrier_errors=[])
    sidecar = messages.quarantine_path(path)
    rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["path"] == str(path.resolve())


def test_deleted_legacy_message_surfaces_and_steer_use_carrier_transaction(
    tmp_path: Path,
) -> None:
    assert not hasattr(messages, "append" + "_envelope")
    assert not hasattr(messages, "rewrite" + "_envelopes")
    assert not hasattr(messages, "cmd_" + "append")
    help_result = subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert help_result.returncode == 0
    assert "append" not in help_result.stdout.split("positional arguments:", 1)[-1]

    path = tmp_path / "dispatch" / "worker.steer.jsonl"
    first = steer.append_steer_entry(path, "before", dispatch_id="worker")
    with path.open("ab") as handle:
        handle.write(b"{broken steer\n")
    second = steer.append_steer_entry(path, "after", dispatch_id="worker")
    assert [first["seq"], second["seq"]] == [1, 2]
    assert [entry["text"] for entry in steer.read_steer_entries(path)] == ["before", "after"]
    assert messages.quarantine_path(path).is_file()
    assert messages.mail_lock_path(path).is_file()


def test_restore_refuses_incompatible_live_epoch_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    opened = journal.Journal.create(project)
    snapshot = opened.snapshot(tmp_path / "snapshot.sqlite3")
    newer_protocol = journal.CURRENT_PROTOCOL_EPOCH + 1
    with sqlite3.connect(opened.path) as connection:
        connection.execute(
            "UPDATE journal_epochs SET protocol_epoch = ? WHERE singleton = 1",
            (newer_protocol,),
        )

    replacement_attempted = False
    real_replace = journal.os.replace

    def reject_replacement_instant(source: Path | str, destination: Path | str) -> None:
        nonlocal replacement_attempted
        if Path(destination) == opened.path:
            replacement_attempted = True
            raise AssertionError("stale client reached the destructive replacement instant")
        real_replace(source, destination)

    monkeypatch.setattr(journal.os, "replace", reject_replacement_instant)
    with pytest.raises(journal.JournalUpgradeRequired, match="live journal epoch fence"):
        journal.restore_snapshot(project, snapshot, i_understand=True)
    assert not replacement_attempted
    with sqlite3.connect(opened.path) as connection:
        assert connection.execute(
            "SELECT protocol_epoch FROM journal_epochs WHERE singleton = 1"
        ).fetchone() == (newer_protocol,)


def test_envelope_rejects_non_integer_schema_versions_and_raw_overlong_label(
    tmp_path: Path,
) -> None:
    result = messages.post_message(
        dispatch_id="strict-envelope",
        msg_type="status",
        payload={"text": "valid"},
        messages_dir=tmp_path / "messages",
    )
    for invalid_version in (True, 1.0):
        invalid = {**result["envelope"], "schema_version": invalid_version}
        with pytest.raises(messages.MessageError, match="schema_version"):
            messages.validate_envelope(invalid)

    project = tmp_path / "project"
    project.mkdir()
    invalid_label = {
        **result["envelope"],
        "type": "controller-notice",
        "addressee": {
            "kind": "controller",
            "label": " " * 1000 + "main",
            "project_root": str(project),
        },
    }
    with pytest.raises(messages.MessageError, match="addressee.label"):
        messages.validate_envelope(invalid_label)


def test_first_carrier_append_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "messages"
    parent.mkdir(exist_ok=True)
    path = messages.inbox_path(parent, "first-append")
    real_fsync = messages.os.fsync
    fsync_targets: list[str] = []

    def observe_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        fsync_targets.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(messages.os, "fsync", observe_fsync)
    with messages.carrier_transaction(path) as transaction:
        assert transaction.read_bytes() == b""
        transaction.append_bytes(b"first\n")
    first_append_targets = list(fsync_targets)
    fsync_targets.clear()
    with messages.carrier_transaction(path) as transaction:
        assert transaction.read_bytes() == b"first\n"
        transaction.append_bytes(b"second\n")

    assert first_append_targets == ["file", "directory"]
    assert fsync_targets == ["file"]

    real_open = messages.os.open

    def refuse_directory_open(candidate: Path | str, flags: int, *args: object) -> int:
        if Path(candidate) == parent and flags & getattr(os, "O_DIRECTORY", 0):
            raise PermissionError("forced carrier directory open failure")
        return real_open(candidate, flags, *args)

    monkeypatch.setattr(messages.os, "open", refuse_directory_open)
    failure_path = messages.inbox_path(parent, "failed-first-append")
    with messages.carrier_transaction(failure_path) as transaction:
        assert transaction.read_bytes() == b""
        with pytest.raises(messages.MessageError, match="fsync carrier directory"):
            transaction.append_bytes(b"not reported durable\n")


def test_restore_cleanup_has_a_durable_crash_boundary_and_fsync_failure_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    opened = journal.Journal.create(project)
    snapshot = opened.snapshot(tmp_path / "snapshot.sqlite3")
    opened.write(
        journal.RowOperation.update(
            "journal_epochs",
            {"updated_at": "live-before-cleanup"},
            where={"singleton": 1},
            row_cap=1,
            expected_rows=1,
        )
    )

    class SimulatedCrash(BaseException):
        pass

    real_fsync_directory = journal._fsync_directory
    durability_instants: list[list[Path]] = []

    def crash_before_cleanup_fsync(path: Path) -> None:
        preimages = list(path.glob(f".{opened.path.name}.restore-preimage-*"))
        durability_instants.append(preimages)
        if len(durability_instants) == 2:
            raise SimulatedCrash("crash after pre-image unlink, before directory fsync")
        real_fsync_directory(path)

    monkeypatch.setattr(journal, "_fsync_directory", crash_before_cleanup_fsync)
    with pytest.raises(SimulatedCrash, match="pre-image unlink"):
        journal.restore_snapshot(project, snapshot, i_understand=True)
    assert len(durability_instants) == 2
    assert durability_instants[0]
    assert durability_instants[1] == []
    assert not list(opened.path.parent.glob(f".{opened.path.name}.restore-preimage-*"))
    assert journal._validate_snapshot_file(opened.path) == journal._validate_snapshot_file(snapshot)

    monkeypatch.setattr(journal, "_fsync_directory", real_fsync_directory)
    real_open = journal.os.open

    def refuse_directory_open(path: Path | str, flags: int, *args: object) -> int:
        if Path(path) == opened.path.parent and flags & getattr(os, "O_DIRECTORY", 0):
            raise PermissionError("forced directory fsync open failure")
        return real_open(path, flags, *args)

    monkeypatch.setattr(journal.os, "open", refuse_directory_open)
    with pytest.raises(journal.JournalError, match="fsync journal directory"):
        journal.restore_snapshot(project, snapshot, i_understand=True)


def test_concurrent_rotation_and_append_both_succeed_under_the_carrier_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages_dir = tmp_path / "messages"
    result = messages.post_message(
        dispatch_id="rotation-race",
        msg_type="status",
        payload={"text": "old inode"},
        messages_dir=messages_dir,
    )
    path = Path(result["path"])
    old_stat = os.lstat(path)
    writer_a_observed_old_inode = threading.Event()
    writer_b_rotated = threading.Event()
    lock_state = threading.local()
    real_lstat = messages.os.lstat
    real_mail_lock = messages.mail_lock

    def interleaved_lstat(candidate: Path | str, *args: object, **kwargs: object):
        if (
            threading.current_thread().name == "writer-a"
            and not getattr(lock_state, "held", False)
            and Path(candidate) == path
        ):
            writer_a_observed_old_inode.set()
            assert writer_b_rotated.wait(5)
            return old_stat
        return real_lstat(candidate, *args, **kwargs)

    @contextlib.contextmanager
    def observed_mail_lock(candidate: Path):
        with real_mail_lock(candidate):
            lock_state.held = True
            try:
                yield
            finally:
                lock_state.held = False

    monkeypatch.setattr(messages.os, "lstat", interleaved_lstat)
    monkeypatch.setattr(messages, "mail_lock", observed_mail_lock)
    failures: list[BaseException] = []

    def writer_a() -> None:
        try:
            messages.post_message(
                dispatch_id="rotation-race",
                msg_type="user_need",
                payload={"text": "append after rotation"},
                messages_dir=messages_dir,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def writer_b() -> None:
        try:
            assert writer_a_observed_old_inode.wait(5)
            replacement = messages.markers_to_envelopes(
                {"STATUS": ["rotated inode"]}, dispatch_id="rotation-race", seq_start=2
            )[0]
            messages.update_envelopes(path, lambda _existing: ([replacement], None))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            writer_b_rotated.set()

    threads = [
        threading.Thread(target=writer_a, name="writer-a"),
        threading.Thread(target=writer_b, name="writer-b"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert [item["payload"]["text"] for item in messages.read_envelopes(path)] == [
        "rotated inode",
        "append after rotation",
    ]


def test_steer_writers_and_bounded_legacy_types_resolve_to_registered_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    _path, steering = steer.append_steer_message("canonical-steer", "continue")
    _path, confirmation = steer.append_steer_message(
        "canonical-steer",
        "yes",
        reply_to="question-1",
        decision="yes",
    )
    assert steering["kind"] == "steering"
    assert confirmation["kind"] == "user_confirm"
    assert steering["kind"] in messages.EVENT_TYPE_REGISTRY
    assert confirmation["kind"] in messages.EVENT_TYPE_REGISTRY

    legacy = messages.post_message(
        dispatch_id="legacy-cross-repo",
        msg_type="qa-round",
        payload={"text": "known legacy producer"},
        messages_dir=tmp_path / "messages",
    )
    assert legacy["envelope"]["type"] == "qa-round"
    assert messages.canonical_event_type("qa-round") == "advisory"
    assert set(messages.EVENT_TYPE_COMPATIBILITY_ALIASES).issubset(messages.EVENT_TYPE_REGISTRY)
    with pytest.raises(messages.MessageError, match="unregistered message type"):
        messages.post_message(
            dispatch_id="unknown-cross-repo",
            msg_type="unseen-future-type",
            payload={},
            messages_dir=tmp_path / "messages",
        )
