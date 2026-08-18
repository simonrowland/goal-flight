#!/usr/bin/env python3
"""Watcher-owned STARTING -> RUNNING claim (t-281)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_messages as messages  # noqa: E402


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


def _prepare_starting(project: Path, dispatch_id: str) -> journal.AttemptIdentity:
    authority = journal.Journal.create(project)
    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None
    started = authority.start_attempt(
        prepared.value.attempt_id,
        prepared.value.launch_token,
    )
    assert started.committed and started.value is not None
    return started.value


def _ledger_record(
    env: dict[str, str],
    project: Path,
    dispatch_id: str,
    state: str,
    *,
    worker_pid: int | None = None,
) -> dict:
    command = [
        sys.executable,
        str(SCRIPTS / "goalflight_ledger.py"),
        "record",
        "--dispatch-id",
        dispatch_id,
        "--agent",
        "codex",
        "--project-root",
        str(project),
        "--state",
        state,
        "--json",
    ]
    if worker_pid is not None:
        command.extend(["--worker-pid", str(worker_pid)])
    completed = subprocess.run(
        command,
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def test_watcher_claim_stamps_live_worker_pid_and_start_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    dispatch_id = "watcher-live-claim"
    _prepare_starting(project, dispatch_id)
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=project,
    )
    try:
        claimed = ledger.claim_attempt_running(project, dispatch_id, worker.pid)
        assert claimed.lifecycle_state == journal.ATTEMPT_RUNNING
        row = journal.Journal(project).read_all(
            "SELECT worker_instance_json FROM dispatch_attempts WHERE dispatch_id = ?",
            (dispatch_id,),
        )[0]
        instance = json.loads(row["worker_instance_json"])
        assert instance["pid"] == worker.pid
        measured = ledger.process_identity(worker.pid)
        assert measured is not None
        if measured.get("start_token"):
            assert instance.get("start_token") == measured["start_token"]
    finally:
        worker.kill()
        worker.wait(timeout=5)


def test_dead_before_claim_is_running_then_worker_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    dispatch_id = "dead-before-watcher-claim"
    _ledger_record(env, project, dispatch_id, "waiting_capacity")
    _ledger_record(env, project, dispatch_id, "starting")
    worker = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        cwd=project,
        env=env,
    )
    assert worker.wait(timeout=10) == 0
    assert ledger.process_identity(worker.pid) is None
    claimed = ledger.claim_attempt_running(project, dispatch_id, worker.pid)
    assert claimed.lifecycle_state == journal.ATTEMPT_RUNNING
    row = journal.Journal(project).read_all(
        "SELECT worker_instance_json FROM dispatch_attempts WHERE dispatch_id = ?",
        (dispatch_id,),
    )[0]
    assert json.loads(row["worker_instance_json"])["pid"] == worker.pid

    messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
    first = ledger.reconcile_terminal_outbox(project, messages_dir=messages_dir)
    second = ledger.reconcile_terminal_outbox(project, messages_dir=messages_dir)
    attempt = journal.Journal(project).attempt_for_dispatch(dispatch_id)
    inbox = messages.read_envelopes(messages.inbox_path(messages_dir, dispatch_id))
    assert first["committed"] == 1 and first["projected"] == 1
    assert second["committed"] == 0 and second["projected"] == 0
    assert attempt is not None and attempt.lifecycle_state == journal.ATTEMPT_TERMINAL
    assert len(inbox) == 1
    assert inbox[0]["payload"]["terminal_state"] == "worker_dead"


def test_claim_is_idempotent_when_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    dispatch_id = "watcher-claim-twice"
    started = _prepare_starting(project, dispatch_id)
    first = ledger.claim_attempt_running(project, dispatch_id, os.getpid())
    second = ledger.claim_attempt_running(project, dispatch_id, os.getpid() + 1)
    assert first.lifecycle_state == journal.ATTEMPT_RUNNING
    assert second.attempt_id == started.attempt_id
    row = journal.Journal(project).read_all(
        "SELECT worker_instance_json FROM dispatch_attempts WHERE dispatch_id = ?",
        (dispatch_id,),
    )[0]
    assert json.loads(row["worker_instance_json"])["pid"] == os.getpid()


def test_claim_refuses_non_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    dispatch_id = "watcher-claim-prepared-only"
    authority = journal.Journal.create(project)
    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed
    with pytest.raises(RuntimeError, match="before RUNNING"):
        ledger.claim_attempt_running(project, dispatch_id, os.getpid())
