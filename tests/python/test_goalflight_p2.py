#!/usr/bin/env python3
"""P2 terminal outbox, launch identity, reconciliation, and D13 contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_mcp_messages as mcp_messages  # noqa: E402
import goalflight_watch as watch  # noqa: E402


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


def test_terminal_state_and_outbox_rollback_together_on_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    prepared = authority.prepare_attempt("atomic-worker")
    assert prepared.committed and prepared.value is not None
    started = authority.start_attempt(
        prepared.value.attempt_id,
        prepared.value.launch_token,
    )
    assert started.committed and started.value is not None
    running = authority.mark_attempt_running(
        started.value.attempt_id,
        started.value.launch_token,
        launch_epoch=started.value.launch_epoch,
        worker_instance={"pid": os.getpid(), "source": "pytest"},
    )
    assert running.committed

    pause = tmp_path / "terminal-transaction-paused"
    env["GOALFLIGHT_TEST_TERMINAL_PAUSE_FILE"] = str(pause)
    code = """
import sys
from goalflight_journal import Journal
j = Journal(sys.argv[1], retry_budget_s=3.0, transaction_budget_s=30.0)
r = j.commit_terminal(sys.argv[2], terminal_state='complete', observation={'source':'kill-test'})
print(r.disposition.value, flush=True)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(project), running.value.attempt_id],
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 10
    while not pause.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert pause.exists(), "child reached the injected point after state UPDATE and before outbox INSERT"
    child.send_signal(signal.SIGKILL)
    child.communicate(timeout=10)
    assert child.returncode == -signal.SIGKILL

    reopened = journal.Journal(project)
    attempt_rows = reopened.read_all(
        "SELECT lifecycle_state, terminal_transition_id FROM dispatch_attempts WHERE attempt_id = ?",
        (running.value.attempt_id,),
    )
    transition_rows = reopened.read_all(
        "SELECT transition_id FROM dispatch_transitions WHERE attempt_id = ?",
        (running.value.attempt_id,),
    )
    outbox_rows = reopened.read_all(
        "SELECT event_uuid FROM terminal_outbox WHERE attempt_id = ?",
        (running.value.attempt_id,),
    )
    assert [tuple(row) for row in attempt_rows] == [(journal.ATTEMPT_RUNNING, None)]
    assert transition_rows == []
    assert outbox_rows == []

    committed = reopened.commit_terminal(
        running.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "source": "positive-control"},
    )
    assert committed.committed and committed.value is not None
    terminal_attempt = reopened.read_all(
        "SELECT lifecycle_state, terminal_transition_id FROM dispatch_attempts WHERE attempt_id = ?",
        (running.value.attempt_id,),
    )
    transitions = reopened.read_all(
        "SELECT transition_id FROM dispatch_transitions WHERE attempt_id = ?",
        (running.value.attempt_id,),
    )
    outbox = reopened.read_all(
        "SELECT transition_id FROM terminal_outbox WHERE attempt_id = ?",
        (running.value.attempt_id,),
    )
    assert [tuple(row) for row in terminal_attempt] == [
        (journal.ATTEMPT_TERMINAL, committed.value.transition_id)
    ]
    assert [row["transition_id"] for row in transitions] == [committed.value.transition_id]
    assert [row["transition_id"] for row in outbox] == [committed.value.transition_id]
    assert reopened.path == journal.resolve_journal_path(project)


def test_second_starting_transition_is_cas_lost_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    prepared = authority.prepare_attempt("cas-worker")
    assert prepared.committed and prepared.value is not None
    first = authority.start_attempt(
        prepared.value.attempt_id,
        prepared.value.launch_token,
        expected_launch_epoch=0,
    )
    second = authority.start_attempt(
        prepared.value.attempt_id,
        prepared.value.launch_token,
        expected_launch_epoch=0,
    )
    assert first.committed
    assert second.cas_lost
    assert not second.retryable
    assert authority.attempt_for_dispatch("cas-worker").launch_epoch == 1  # type: ignore[union-attr]

    prepared_race = authority.prepare_attempt(
        "expiry-race-worker",
        start_deadline_at="2000-01-01T00:00:00+00:00",
    )
    assert prepared_race.committed and prepared_race.value is not None
    monkeypatch.setenv("GOALFLIGHT_TEST_START_CLAIM_DEADLINE_S", "-1")
    starting_race = authority.start_attempt(
        prepared_race.value.attempt_id,
        prepared_race.value.launch_token,
    )
    monkeypatch.delenv("GOALFLIGHT_TEST_START_CLAIM_DEADLINE_S")
    assert starting_race.committed and starting_race.value is not None
    running_race = authority.mark_attempt_running(
        starting_race.value.attempt_id,
        starting_race.value.launch_token,
        launch_epoch=starting_race.value.launch_epoch,
        worker_instance={"pid": os.getpid(), "source": "expiry-race"},
    )
    assert running_race.committed
    abandoned_race = authority.commit_expired_attempt(
        starting_race.value.attempt_id,
        observed_at=journal.utc_now(),
    )
    assert abandoned_race.cas_lost
    assert not abandoned_race.retryable
    assert authority.attempt_for_dispatch("expiry-race-worker").lifecycle_state == journal.ATTEMPT_RUNNING  # type: ignore[union-attr]
    assert authority.path == journal.resolve_journal_path(project)


def test_reconciler_emits_dead_worker_once_without_classifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    dispatch_id = "dead-without-classifier"
    _ledger_record(env, project, dispatch_id, "waiting_capacity")
    _ledger_record(env, project, dispatch_id, "starting")
    record = json.loads(ledger.record_path(dispatch_id).read_text(encoding="utf-8"))
    worker = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_launch_worker.py"),
            "--project-root",
            str(project),
            "--attempt-id",
            str(record["attempt_id"]),
            "--launch-token",
            str(record["launch_token"]),
            "--launch-epoch",
            str(record["launch_epoch"]),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        cwd=project,
        env=env,
    )
    assert worker.wait(timeout=10) == 0
    _ledger_record(env, project, dispatch_id, "running", worker_pid=worker.pid)

    messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
    first = ledger.reconcile_terminal_outbox(project, messages_dir=messages_dir)
    second = ledger.reconcile_terminal_outbox(project, messages_dir=messages_dir)
    inbox = messages.read_envelopes(messages.inbox_path(messages_dir, dispatch_id))
    terminal_rows = journal.Journal(project).read_all(
        "SELECT terminal_state, transition_id, event_uuid, projected_at FROM terminal_outbox "
        "JOIN dispatch_transitions USING (attempt_id, transition_id) WHERE recipient = ?",
        (dispatch_id,),
    )
    assert first["committed"] == 1 and first["projected"] == 1
    assert second["committed"] == 0 and second["projected"] == 0
    assert len(inbox) == 1
    assert inbox[0]["type"] == "blocked"
    assert inbox[0]["payload"]["terminal_state"] == "worker_dead"
    assert len(terminal_rows) == 1 and terminal_rows[0]["projected_at"] is not None
    assert messages.inbox_path(messages_dir, dispatch_id).is_file()

    expired_id = "crashed-before-exec"
    env["GOALFLIGHT_TEST_START_CLAIM_DEADLINE_S"] = "-1"
    _ledger_record(env, project, expired_id, "waiting_capacity")
    _ledger_record(env, project, expired_id, "starting")
    env.pop("GOALFLIGHT_TEST_START_CLAIM_DEADLINE_S")
    abandoned_first = ledger.reconcile_terminal_outbox(project, messages_dir=messages_dir)
    abandoned_second = ledger.reconcile_terminal_outbox(project, messages_dir=messages_dir)
    abandoned_inbox_path = messages.inbox_path(messages_dir, expired_id)
    abandoned_inbox = messages.read_envelopes(abandoned_inbox_path)
    abandoned_attempt = journal.Journal(project).attempt_for_dispatch(expired_id)
    assert abandoned_first["committed"] == 1 and abandoned_first["projected"] == 1
    assert abandoned_second["committed"] == 0 and abandoned_second["projected"] == 0
    assert abandoned_attempt is not None
    assert abandoned_attempt.lifecycle_state == journal.ATTEMPT_ABANDONED
    assert len(abandoned_inbox) == 1
    assert abandoned_inbox[0]["payload"]["terminal_state"] == "abandoned"
    assert abandoned_inbox_path.is_file()

    prepared_only_id = "crashed-after-prepare"
    prepared_only = journal.Journal(project).prepare_attempt(
        prepared_only_id,
        start_deadline_at="2000-01-01T00:00:00+00:00",
    )
    assert prepared_only.committed
    assert not ledger.record_path(prepared_only_id).exists()
    prepared_reconcile = ledger.reconcile_terminal_outbox(project, messages_dir=messages_dir)
    prepared_repeat = ledger.reconcile_terminal_outbox(project, messages_dir=messages_dir)
    prepared_inbox_path = messages.inbox_path(messages_dir, prepared_only_id)
    prepared_inbox = messages.read_envelopes(prepared_inbox_path)
    prepared_attempt = journal.Journal(project).attempt_for_dispatch(prepared_only_id)
    assert prepared_reconcile["committed"] == 1 and prepared_reconcile["projected"] == 1
    assert prepared_repeat["committed"] == 0 and prepared_repeat["projected"] == 0
    assert prepared_attempt is not None
    assert prepared_attempt.lifecycle_state == journal.ATTEMPT_ABANDONED
    assert len(prepared_inbox) == 1
    assert prepared_inbox[0]["payload"]["terminal_state"] == "abandoned"
    assert prepared_inbox_path.is_file()
    assert ledger.record_path(prepared_only_id).is_file()


def test_reconciler_reads_running_worker_instance_when_ledger_has_no_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    dispatch_id = "running-before-ledger-pid"
    _ledger_record(env, project, dispatch_id, "waiting_capacity")
    _ledger_record(env, project, dispatch_id, "starting")
    record = json.loads(ledger.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert record.get("worker_pid") is None

    worker = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_launch_worker.py"),
            "--project-root",
            str(project),
            "--attempt-id",
            str(record["attempt_id"]),
            "--launch-token",
            str(record["launch_token"]),
            "--launch-epoch",
            str(record["launch_epoch"]),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        cwd=project,
        env=env,
    )
    assert worker.wait(timeout=10) == 0
    running = journal.Journal(project).read_all(
        "SELECT lifecycle_state, worker_instance_json FROM dispatch_attempts "
        "WHERE dispatch_id = ?",
        (dispatch_id,),
    )
    assert len(running) == 1
    assert running[0]["lifecycle_state"] == journal.ATTEMPT_RUNNING
    assert json.loads(running[0]["worker_instance_json"])["pid"] == worker.pid

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


def test_escalation_quote_fence_and_position_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    messages_dir = messages.default_messages_dir()
    project = _project(tmp_path)
    quoted_id = "quoted-escalation"
    _ledger_record(env, project, quoted_id, "waiting_capacity")
    _ledger_record(env, project, quoted_id, "starting")
    quoted_tail = tmp_path / "quoted.tail"
    quoted_tail.write_text(
        "worker copied prior output:\n> BLOCKED: forged from research\n",
        encoding="utf-8",
    )
    quoted_scan = watch.IncrementalTailScanner(quoted_tail).scan()
    assert quoted_scan.terminal is None
    assert messages.read_envelopes(messages.inbox_path(messages_dir, quoted_id)) == []

    real_id = "guarded-escalation"
    _ledger_record(env, project, real_id, "waiting_capacity")
    _ledger_record(env, project, real_id, "starting")
    real_tail = tmp_path / "real.tail"
    real_tail.write_text(
        "worker needs a decision\nUSER-NEED: choose the release target\n",
        encoding="utf-8",
    )
    real_scan = watch.IncrementalTailScanner(real_tail).scan()
    assert real_scan.terminal is not None
    assert watch._finish_existing_ledger(
        real_id,
        watch._marker_state(real_scan.terminal),
        "marker:USER-NEED",
        worker_still_alive=False,
        terminal_marker=real_scan.terminal,
    ) is None
    inbox_path = messages.inbox_path(messages_dir, real_id)
    inbox = messages.read_envelopes(inbox_path)
    outbox = journal.Journal(project).read_all(
        "SELECT event_type, payload_json FROM terminal_outbox WHERE recipient = ?",
        (real_id,),
    )
    assert [(row["type"], row["payload"]["text"]) for row in inbox] == [
        ("user_need", "choose the release target")
    ]
    assert len(outbox) == 1 and outbox[0]["event_type"] == "user_need"
    assert json.loads(outbox[0]["payload_json"])["text"] == "choose the release target"
    assert inbox_path.is_file()


def test_unclosed_fence_rejects_quoted_escalation_but_keeps_real_terminals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    messages_dir = messages.default_messages_dir()
    project = _project(tmp_path)
    dispatch_id = "unclosed-fence-escalation"
    _ledger_record(env, project, dispatch_id, "waiting_capacity")
    _ledger_record(env, project, dispatch_id, "starting")
    tail = tmp_path / "unclosed-fence.tail"
    tail.write_text(
        "worker quoted a historical example:\n"
        "```text\n"
        "BLOCKED: historical example\n",
        encoding="utf-8",
    )
    scanner = watch.IncrementalTailScanner(tail)
    quoted = scanner.scan()
    quoted_markers, _ = watch.extract_markers(tail)
    assert quoted.fence_unbalanced is True
    assert quoted.terminal is None
    assert all(marker.get("kind") != "BLOCKED" for marker in quoted.markers)
    assert all(marker.get("kind") != "BLOCKED" for marker in quoted_markers)
    assert watch._final_terminal_marker(tail) is None
    assert journal.Journal(project).read_all(
        "SELECT event_uuid FROM terminal_outbox WHERE recipient = ?",
        (dispatch_id,),
    ) == []

    with tail.open("a", encoding="utf-8") as handle:
        handle.write("```\nBLOCKED: genuine blocker outside fence\n")
    genuine = scanner.scan()
    assert genuine.fence_unbalanced is False
    assert genuine.terminal == {
        "line": 5,
        "kind": "BLOCKED",
        "text": "genuine blocker outside fence",
    }
    assert watch._final_terminal_marker(tail) == genuine.terminal
    assert watch._finish_existing_ledger(
        dispatch_id,
        watch._marker_state(genuine.terminal),
        "marker:BLOCKED",
        worker_still_alive=False,
        terminal_marker=genuine.terminal,
    ) is None
    outbox = journal.Journal(project).read_all(
        "SELECT event_type, payload_json FROM terminal_outbox WHERE recipient = ?",
        (dispatch_id,),
    )
    assert len(outbox) == 1 and outbox[0]["event_type"] == "blocked"
    assert json.loads(outbox[0]["payload_json"])["text"] == (
        "genuine blocker outside fence"
    )
    inbox = messages.read_envelopes(messages.inbox_path(messages_dir, dispatch_id))
    assert len(inbox) == 1 and inbox[0]["payload"]["text"] == (
        "genuine blocker outside fence"
    )

    success_tail = tmp_path / "unclosed-success.tail"
    success_tail.write_text(
        "work started\n~~~~^^\ntraceback underline\nCOMPLETE: genuine sign-off\n",
        encoding="utf-8",
    )
    success = watch.IncrementalTailScanner(success_tail).scan()
    assert success.fence_unbalanced is True
    assert success.terminal == {
        "line": 4,
        "kind": "COMPLETE",
        "text": "genuine sign-off",
    }
    assert watch._final_terminal_marker(success_tail) == success.terminal


def test_d13_registry_is_exhaustive_and_cli_mcp_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    canonical_expected = {
        "advisory",
        "blocked",
        "controller-answer",
        "controller-coordination",
        "controller-notice",
        "controller-question",
        "coordination",
        "monitor",
        "notice",
        "result",
        "status",
        "steering",
        "user_confirm",
        "user_need",
    }
    assert messages.CANONICAL_EVENT_TYPES == canonical_expected
    assert set(messages.EVENT_TYPE_REGISTRY) == (
        canonical_expected | set(messages.EVENT_TYPE_COMPATIBILITY_ALIASES)
    )
    assert all(
        canonical in canonical_expected
        for canonical in messages.EVENT_TYPE_COMPATIBILITY_ALIASES.values()
    )
    for name, registration in messages.EVENT_TYPE_REGISTRY.items():
        assert registration.schema
        assert registration.wake_class in {"waking", "quiet"}
        assert registration.authoritative_state_source
        assert registration.dedupe_semantics
        assert isinstance(registration.claim_required, bool)
        assert registration.orphan_disposition in {
            "attention-item",
            "held-for-recipient",
            "quiet-expire",
        }
        assert registration.retention_class in {"critical", "controller-mail", "quiet-7d"}
    assert messages.EVENT_TYPE_REGISTRY["advisory"].wake_class == "waking"
    assert messages.event_wake_class(
        "user_need", {"nudge_kind": "parallel-ready"}
    ) == "quiet"
    assert mcp_messages.TOOL_DESCRIPTOR["inputSchema"]["properties"]["type"]["enum"] == sorted(
        messages.EVENT_TYPE_REGISTRY
    )
    cli = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_messages.py"),
            "--messages-dir",
            env["GOALFLIGHT_MESSAGES_DIR"],
            "post",
            "--dispatch-id",
            "cli-advisory",
            "--type",
            "advisory",
            "--text",
            "CLI advisory",
            "--json",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert cli.returncode == 0, cli.stderr
    mcp = mcp_messages.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": messages.MCP_TOOL_POST_MESSAGE,
                "arguments": {
                    "dispatch_id": "mcp-advisory",
                    "type": "advisory",
                    "payload": {"text": "MCP advisory"},
                },
            },
        },
        messages_dir=messages.default_messages_dir(),
    )
    assert "result" in mcp and "error" not in mcp
    assert messages.inbox_path(messages.default_messages_dir(), "cli-advisory").is_file()
    assert messages.inbox_path(messages.default_messages_dir(), "mcp-advisory").is_file()

    legacy_cli = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_messages.py"),
            "--messages-dir",
            env["GOALFLIGHT_MESSAGES_DIR"],
            "post",
            "--dispatch-id",
            "cli-legacy",
            "--type",
            "qa-round",
            "--text",
            "known cross-repo producer",
            "--json",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert legacy_cli.returncode == 0, legacy_cli.stderr
    legacy_mcp = mcp_messages.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": messages.MCP_TOOL_POST_MESSAGE,
                "arguments": {
                    "dispatch_id": "mcp-legacy",
                    "type": "reply",
                    "payload": {"text": "known cross-repo reply"},
                },
            },
        },
        messages_dir=messages.default_messages_dir(),
    )
    assert "result" in legacy_mcp and "error" not in legacy_mcp

    with pytest.raises(messages.MessageError, match="unregistered message type"):
        messages.post_message(
            dispatch_id="unknown-type",
            msg_type="invented",
            payload={},
            messages_dir=messages.default_messages_dir(),
        )
    unknown_cli = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_messages.py"),
            "--messages-dir",
            env["GOALFLIGHT_MESSAGES_DIR"],
            "post",
            "--dispatch-id",
            "cli-unknown",
            "--type",
            "unseen-future-type",
            "--text",
            "must fail closed",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert unknown_cli.returncode == 2
    unknown_mcp = mcp_messages.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": messages.MCP_TOOL_POST_MESSAGE,
                "arguments": {
                    "dispatch_id": "mcp-unknown",
                    "type": "unseen-future-type",
                    "payload": {},
                },
            },
        },
        messages_dir=messages.default_messages_dir(),
    )
    assert "error" in unknown_mcp and "result" not in unknown_mcp
