#!/usr/bin/env python3
"""Blocking worker steer waits and watcher idle-accounting regressions."""

from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WATCH = SCRIPTS / "goalflight_watch.py"
sys.path.insert(0, str(SCRIPTS))

import goalflight_steer_mailbox as steer  # noqa: E402
import goalflight_watch as watch  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402


def test_gate_collection_log_uses_private_test_tmpdir() -> None:
    source = (ROOT / "tests" / "run.sh").read_text(encoding="utf-8")
    assert '"$_GF_TEST_ENV_BASE/goal-flight-collect-$$.out"' in source
    assert "/tmp/goal-flight-collect-$$.out" not in source


def test_worker_wait_protocol_assigns_end_row_to_watcher() -> None:
    protocol = (ROOT / "protocols" / "worker-markers.md").read_text(
        encoding="utf-8"
    )
    contract = (ROOT / "protocols" / "worker-contract.md").read_text(
        encoding="utf-8"
    )
    assert "The watcher reads the settlement and appends the mailbox end row." in protocol
    assert "`steer --wait` writes a durable reply settlement" in protocol
    assert "`steer --wait` writes a durable reply settlement to that wait's question\n  outbox and appends its end row." not in protocol
    assert "60-second worker signal cleanup" not in protocol + contract
    assert "The watcher does not signal a worker merely because it remains" in protocol
    assert "A terminal\nmarker does not trigger post-terminal worker signaling." in contract


def test_reply_output_requires_pending_exact_reply_sequence() -> None:
    marker = {
        "kind": "STEER-REPLY",
        "text": json.dumps(
            {
                "kind": steer.WORKER_WAIT_REPLY_KIND,
                "reply_to": "wait-1",
                "seq": 17,
            }
        ),
    }

    assert not watch._worker_wait_reply_output_matches(
        marker,
        {"phase": "awaiting_reply", "wait_id": "wait-1", "reply_seq": None},
    )
    assert not watch._worker_wait_reply_output_matches(
        marker,
        {"phase": "reply_pending", "wait_id": "wait-1", "reply_seq": 18},
    )
    assert watch._worker_wait_reply_output_matches(
        marker,
        {"phase": "reply_pending", "wait_id": "wait-1", "reply_seq": 17},
    )


def test_worker_question_outbox_writes_atomic_create_only_request(
    tmp_path: Path,
) -> None:
    dispatch_id = "outbox-dispatch"
    wait_id = "wait-1"
    outbox = tmp_path / "dispatch" / f"{dispatch_id}.questions"
    outbox.mkdir(parents=True)

    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=600,
    )

    path = outbox / f"{wait_id}.json"
    assert request == json.loads(path.read_text(encoding="utf-8"))
    assert request == {
        "schema": "goalflight.worker-question.v1",
        "dispatch_id": dispatch_id,
        "attempt_id": "attempt-1",
        "wait_id": wait_id,
        "question": "need a decision",
        "question_kind": "USER-NEED",
        "created_at": request["created_at"],
        "timeout_s": 600.0,
        "deadline_at": request["deadline_at"],
    }
    assert steer.validate_worker_question_request(
        request,
        dispatch_id=dispatch_id,
    )["deadline_at"] == request["deadline_at"]
    with pytest.raises(FileExistsError):
        steer.write_worker_question_request(
            outbox,
            dispatch_id=dispatch_id,
            attempt_id="attempt-1",
            wait_id=wait_id,
            question_kind="USER-NEED",
            question="replacement must not overwrite",
            timeout_s=600,
        )
    assert json.loads(path.read_text(encoding="utf-8")) == request


def test_wait_timeout_emits_blocked_marker(capsys: pytest.CaptureFixture[str]) -> None:
    dispatch._wait_event_reporter("dispatch-timeout")(
        {
            "state": "deadline",
            "wait_id": "wait-timeout",
            "question_text": "need a decision",
        }
    )
    output = capsys.readouterr().out
    assert (
        "!BLOCKED: dispatch-timeout - wait timed out; "
        "wait-id=wait-timeout; question=need a decision"
    ) in output
    assert output.splitlines()[-1].startswith("!BLOCKED: dispatch-timeout - wait timed out;")
    assert len(output.splitlines()[-1]) <= 1000


def test_question_marker_budget_includes_the_marker_prefix() -> None:
    text = steer.worker_wait_question_marker_text(
        "dispatch-marker-budget",
        "x" * 2000,
        "wait-marker-budget",
        question_kind="USER-CONFIRM",
    )
    assert len(f"!USER-CONFIRM: {text}") <= 1000


def test_timeout_settlement_requires_valid_deadline_evidence(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatch_id = "settlement-deadline"
    wait_id = "settlement-wait"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-a",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    steer.write_worker_question_settlement(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-a",
        wait_id=wait_id,
        state="timeout",
        deadline_at=request["deadline_at"],
    )
    settlement_path = steer.worker_question_settlement_path(outbox, wait_id)
    settlement = json.loads(settlement_path.read_text(encoding="utf-8"))
    assert settlement["deadline_at"] == request["deadline_at"], settlement
    deadline = datetime.fromisoformat(request["deadline_at"].replace("Z", "+00:00"))
    settled = datetime.fromisoformat(settlement["settled_at"].replace("Z", "+00:00"))
    assert settled >= deadline, settlement
    assert steer.read_worker_question_settlement(outbox, request) == settlement

    invalid_settlements = []
    missing_deadline = dict(settlement)
    missing_deadline.pop("deadline_at")
    invalid_settlements.append(missing_deadline)
    missing_settled_at = dict(settlement)
    missing_settled_at.pop("settled_at")
    invalid_settlements.append(missing_settled_at)
    malformed = dict(settlement, settled_at="not-a-time")
    invalid_settlements.append(malformed)
    mismatched = dict(settlement, deadline_at="2099-01-01T00:00:00Z")
    invalid_settlements.append(mismatched)
    premature = dict(
        settlement,
        settled_at=(deadline - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    )
    invalid_settlements.append(premature)

    for invalid in invalid_settlements:
        settlement_path.write_text(json.dumps(invalid), encoding="utf-8")
        assert steer.read_worker_question_settlement(outbox, request) is None
    assert "ignoring invalid worker question settlement" in caplog.text


@pytest.mark.parametrize("deadline_at", [None, "not-a-time", "2099-01-01T00:00:00Z"])
def test_invalid_question_deadline_is_published_without_idle_exemption(
    tmp_path: Path,
    deadline_at: str | None,
) -> None:
    dispatch_id = "invalid-question-deadline"
    wait_id = f"wait-{deadline_at or 'missing'}"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    request_path = outbox / f"{wait_id}.json"
    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-a",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    if deadline_at is None:
        request.pop("deadline_at")
    else:
        request["deadline_at"] = deadline_at
    request_path.write_text(json.dumps(request), encoding="utf-8")

    normalized = steer.validate_worker_question_request(
        request,
        dispatch_id=dispatch_id,
    )
    assert normalized is not None, normalized
    assert normalized["_deadline_at_valid"] is False

    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}
    published: list[dict] = []
    watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-a",
        publish_func=published.append,
    )

    assert len(published) == 1, published
    tracked, live_any, read_succeeded = watch._read_active_worker_wait(
        mailbox,
        dispatch_id,
        now_mono=steer.active_monotonic(),
        worker_pid=os.getpid(),
        worker_pgid=os.getpgrp(),
    )
    assert read_succeeded
    assert tracked is None
    assert live_any is None

    reply = steer.append_worker_wait_reply(
        mailbox,
        dispatch_id=dispatch_id,
        wait_id=wait_id,
        text="answer remains available",
    )
    assert reply["kind"] == steer.WORKER_WAIT_REPLY_KIND


def test_malformed_reply_settlement_does_not_consume_resumable_late_reply(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mailbox = tmp_path / "malformed-reply-receipt.steer.jsonl"
    dispatch_id = "malformed-reply-receipt"
    first = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need a decision",
        timeout_secs=0.02,
        poll_secs=0.01,
    )
    late = steer.append_worker_wait_reply(
        mailbox,
        dispatch_id=dispatch_id,
        wait_id=first["wait_id"],
        text="late answer",
    )
    outbox = steer.goalflight_dispatch_paths.worker_question_outbox_dir(
        dispatch_id,
        dispatch_dir=mailbox.parent,
    )
    settlement_path = steer.worker_question_settlement_path(
        outbox,
        first["wait_id"],
    )
    request = json.loads(
        (outbox / f"{first['wait_id']}.json").read_text(encoding="utf-8")
    )
    settlement_path.write_text(
        json.dumps(
            {
                "schema": steer.WORKER_QUESTION_SETTLEMENT_SCHEMA,
                "dispatch_id": dispatch_id,
                "attempt_id": request["attempt_id"],
                "wait_id": first["wait_id"],
                "state": "reply",
                "reply_seq": late["seq"],
            }
        ),
        encoding="utf-8",
    )

    assert steer.worker_question_reply_receipts(outbox) == set()
    assert "ignoring invalid worker question settlement" in caplog.text

    resumed = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need a decision",
        attempt_id="resumed-attempt",
        resume_wait_id=first["wait_id"],
        timeout_secs=0.05,
        poll_secs=0.01,
    )
    assert resumed["state"] == "messages", resumed
    assert resumed["recovered_admitted_reply"] is True, resumed
    assert resumed["entries"] == [late]


def test_malformed_settlement_is_ignored_and_timeout_can_replace_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatch_id = "malformed-settlement"
    wait_id = "malformed-wait"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-a",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    steer.append_worker_wait_started(
        mailbox,
        dispatch_id=dispatch_id,
        timeout_secs=30,
        question_kind="USER-NEED",
        question_text=request["question"],
        wait_id=wait_id,
        attempt_id="attempt-a",
        waiter_pid=os.getpid(),
    )
    settlement_path = steer.worker_question_settlement_path(outbox, wait_id)
    settlement_path.write_text(
        json.dumps(
            {
                "schema": steer.WORKER_QUESTION_SETTLEMENT_SCHEMA,
                "dispatch_id": dispatch_id,
                "attempt_id": "attempt-a",
                "wait_id": wait_id,
                "state": [],
            }
        ),
        encoding="utf-8",
    )
    assert steer.read_worker_question_settlement(outbox, request) is None

    identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}
    watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-a",
        publish_func=lambda _event: None,
    )
    timeout = steer.write_worker_question_settlement(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-a",
        wait_id=wait_id,
        state="timeout",
        deadline_at=request["deadline_at"],
    )
    assert timeout["state"] == "timeout"
    assert steer.read_worker_question_settlement(outbox, request) == timeout
    assert list(outbox.glob(f".{wait_id}.settled.invalid.*"))
    assert "ignoring invalid worker question settlement" in caplog.text

    settlement_path.write_bytes(b"\xff")
    assert steer.read_worker_question_settlement(outbox, request) is None
    watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-a",
        publish_func=lambda _event: None,
    )
    timeout = steer.write_worker_question_settlement(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-a",
        wait_id=wait_id,
        state="timeout",
        deadline_at=request["deadline_at"],
    )
    assert timeout["state"] == "timeout"
    assert steer.read_worker_question_settlement(outbox, request) == timeout
    assert len(list(outbox.glob(f".{wait_id}.settled.invalid.*"))) == 2
    assert "ignoring unreadable worker question settlement" in caplog.text


def test_reply_writer_waits_while_malformed_settlement_is_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_id = "settlement-race"
    wait_id = "settlement-race-wait"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-a",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    malformed = b'{"state":[]}'
    settlement_path = steer.worker_question_settlement_path(outbox, wait_id)
    settlement_path.write_bytes(malformed)

    malformed_validated = threading.Event()
    release_timeout = threading.Event()
    reply_lock_attempted = threading.Event()
    read_settlement = steer.read_worker_question_settlement
    acquire_flock = steer.goalflight_compat.flock

    def pause_after_timeout_validation(outbox_dir: Path, settlement_request: dict):
        settlement = read_settlement(outbox_dir, settlement_request)
        if (
            threading.current_thread().name == "settlement-timeout"
            and not malformed_validated.is_set()
        ):
            assert settlement is None
            malformed_validated.set()
            assert release_timeout.wait(5)
        return settlement

    def observe_reply_lock(file_handle, operation: int) -> None:
        if threading.current_thread().name == "settlement-reply":
            reply_lock_attempted.set()
        acquire_flock(file_handle, operation)

    monkeypatch.setattr(steer, "read_worker_question_settlement", pause_after_timeout_validation)
    monkeypatch.setattr(steer.goalflight_compat, "flock", observe_reply_lock)

    def settle(state: str) -> dict:
        threading.current_thread().name = f"settlement-{state}"
        return steer.write_worker_question_settlement(
            outbox,
            dispatch_id=dispatch_id,
            attempt_id="attempt-a",
            wait_id=wait_id,
            state=state,
            deadline_at=request["deadline_at"],
            reply_seq=17 if state == "reply" else None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        timeout_writer = pool.submit(settle, "timeout")
        assert malformed_validated.wait(5)
        reply_writer = pool.submit(settle, "reply")
        try:
            assert reply_lock_attempted.wait(5)
            assert not reply_writer.done(), "reply writer bypassed the per-wait lock"
        finally:
            release_timeout.set()
        timeout_writer.result(timeout=5)
        reply_writer.result(timeout=5)

    settlement = read_settlement(outbox, request)
    assert settlement and settlement["state"] == "reply", settlement
    assert settlement["reply_seq"] == 17, settlement
    quarantined = list(outbox.glob(f".{wait_id}.settled.invalid.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == malformed


def test_native_read_only_outbox_failure_has_a_typed_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch_id = "readonly-outbox"
    env = _env(tmp_path)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    outbox = _prepare_question_outbox(env, dispatch_id)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", dispatch_id)
    monkeypatch.setenv("GOALFLIGHT_ATTEMPT_ID", "attempt-readonly")
    monkeypatch.setenv("GOALFLIGHT_STEER_FILE", str(mailbox))
    monkeypatch.setenv("GOALFLIGHT_QUESTION_OUTBOX_DIR", str(outbox))
    monkeypatch.setenv("GOALFLIGHT_WORKER_TAIL_FILE", str(tmp_path / "readonly.tail"))
    real_open = steer.os.open

    def denied_open(path, flags, *args, **kwargs):
        if Path(path).parent == outbox:
            raise PermissionError("read-only sandbox")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(steer.os, "open", denied_open)
    result = dispatch._cmd_steer(
        [dispatch_id, "--wait", "--question-kind", "USER-NEED", "need help"]
    )
    error = capsys.readouterr().err
    assert result == 64, error
    assert "WorkerQuestionOutboxWriteDenied" in error, error
    assert "read-only native Codex workers cannot ask" in error, error


def test_reply_not_late_from_a_watcher_only_monotonic_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = tmp_path / "wall-clock-deadline.steer.jsonl"
    clock = [100.0]
    monkeypatch.setattr(steer, "active_monotonic", lambda: clock[0])
    deadline_at = (
        datetime.now(timezone.utc) + timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")
    arm = steer.append_worker_wait_started(
        mailbox,
        dispatch_id="wall-clock-deadline",
        timeout_secs=60,
        question_kind="USER-NEED",
        question_text="still on time",
        attempt_id="attempt-a",
        deadline_mono=101.0,
        deadline_at=deadline_at,
    )
    clock[0] = 102.0

    reply = steer.append_worker_wait_reply(
        mailbox,
        dispatch_id="wall-clock-deadline",
        wait_id=str(arm["question_id"]),
        text="on time by the question deadline",
    )

    assert reply["context"]["late"] is False, reply


def test_watcher_consumes_question_outbox_once(tmp_path: Path) -> None:
    dispatch_id = "outbox-dedupe"
    wait_id = "wait-dedupe"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    steer_file = tmp_path / f"{dispatch_id}.steer.jsonl"
    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}
    published: list[dict] = []

    first_mtime = watch.consume_worker_question_outbox(
        outbox,
        steer_file,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        publish_func=published.append,
    )
    second_mtime = watch.consume_worker_question_outbox(
        outbox,
        steer_file,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        last_mtime_ns=first_mtime,
        publish_func=published.append,
    )
    third_mtime = watch.consume_worker_question_outbox(
        outbox,
        steer_file,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        last_mtime_ns=second_mtime,
        publish_func=published.append,
    )

    assert first_mtime is not None
    assert second_mtime is not None and second_mtime != first_mtime
    assert third_mtime == second_mtime
    assert len(published) == 1, published
    assert published[0]["wait_id"] == wait_id
    assert published[0]["question_text"] == request["question"]
    assert (outbox / f"{wait_id}.published").is_file()
    arm = steer.active_worker_wait(
        steer.read_steer_entries(steer_file),
        dispatch_id=dispatch_id,
        worker_pid=os.getpid(),
        worker_pgid=os.getpgrp(),
    )
    assert arm is not None
    assert arm["wait_id"] == wait_id
    assert arm["question_marker_text"] == published[0]["question_marker_text"]


def test_watcher_projects_bounded_dispatch_journal_outbox_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import goalflight_journal

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    env = _env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    goalflight_journal.open_or_create_journal(project)
    dispatch_id = "journal-outbox-watch"
    outbox = _prepare_question_outbox(env, dispatch_id)
    monkeypatch.setenv("GOALFLIGHT_QUESTION_OUTBOX_DIR", str(outbox))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", dispatch_id)
    original_projection = messages._worker_journal_projection
    monkeypatch.setattr(
        messages,
        "_worker_journal_projection",
        lambda *_args, **_kwargs: ((), RuntimeError("journal denied")),
    )
    event_ids = []
    for index in range(5):
        posted = messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="user_need",
            payload={"text": f"wake {index}", "project_root": str(project)},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "local", "adapter": "pytest", "transport": "worker"},
        )
        event_ids.append(posted["envelope"]["id"])
    monkeypatch.setattr(messages, "_worker_journal_projection", original_projection)
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", "")

    assert [
        json.loads(path.read_text(encoding="utf-8"))["sequence"]
        for path in sorted((outbox / "journal").glob("journal-*.delivery"))
    ] == [1, 2, 3, 4, 5]
    (outbox / "journal" / "journal-00000000000000000001.delivery").write_bytes(b"\xff{corrupt}\n")
    assert watch.consume_worker_journal_outbox(
        outbox, dispatch_id, max_records=2, budget_s=1.0
    ) == 1
    assert watch.consume_worker_journal_outbox(
        outbox, dispatch_id, max_records=2, budget_s=1.0
    ) == 2
    assert watch.consume_worker_journal_outbox(
        outbox, dispatch_id, max_records=2, budget_s=1.0
    ) == 1
    # A new watcher process resumes from the constant-size sequence cursor.
    assert watch.consume_worker_journal_outbox(
        outbox, dispatch_id, max_records=2, budget_s=1.0
    ) == 0
    rows = goalflight_journal.Journal(project).read_all(
        "SELECT event_uuid, projected_at FROM delivery_events ORDER BY stream_seq"
    )
    assert [row["event_uuid"] for row in rows] == event_ids[1:]
    assert all(row["projected_at"] is not None for row in rows)
    assert (outbox / "journal" / "journal-00000000000000000001.jsonl.quarantine.jsonl").is_file()


def test_question_outbox_scan_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outbox = tmp_path / "bounded-question-scan.questions"
    outbox.mkdir()
    for index in range(140):
        (outbox / f"retained-{index:04d}.json").write_text("{}", encoding="utf-8")
        (outbox / f"retained-{index:04d}.published").write_text("published", encoding="utf-8")
        (outbox / f"retained-{index:04d}.settled.json").write_text("{}", encoding="utf-8")
    dispatch_id = "bounded-question-scan"
    wait_id = "wait-beyond-bounded-scan"
    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    marker = {
        "kind": "USER-NEED",
        "text": steer.worker_wait_question_marker_text(
            dispatch_id,
            request["question"],
            wait_id,
            question_kind="USER-NEED",
        ),
    }

    monkeypatch.setattr(watch, "_promote_resumed_worker_reply_settlements", lambda *_a, **_k: None)
    original_scandir = watch.os.scandir
    scanned = 0

    class CountedEntries:
        def __init__(self, entries):
            self.entries = iter(sorted(entries, key=lambda entry: entry.name))
            entries.close()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            nonlocal scanned
            for entry in self.entries:
                scanned += 1
                assert scanned <= 128, "question scan exceeded its per-pass entry bound"
                yield entry

    monkeypatch.setattr(
        watch.os,
        "scandir",
        lambda path: CountedEntries(original_scandir(path)) if Path(path) == outbox else original_scandir(path),
    )

    published: list[dict] = []
    watch.consume_worker_question_outbox(
        outbox,
        tmp_path / "mailbox.jsonl",
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=None,
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        markers=[marker],
        publish_func=published.append,
    )
    assert scanned <= 128
    assert len(published) == 1 and published[0]["wait_id"] == wait_id
    assert watch._outbox_question_for_marker(
        outbox,
        dispatch_id,
        marker,
        active_attempt_id="attempt-1",
    ) == wait_id


def test_question_outbox_enumeration_resumes_after_bounded_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_id = "question-outbox-resumable-scan"
    wait_id = "wait-after-resumable-scan"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    for index in range(140):
        (outbox / f"retained-{index:04d}.json").write_text("{}", encoding="utf-8")
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    scan_state: dict[str, object] = {}
    published: list[dict] = []
    identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}
    original_scandir = watch.os.scandir
    pass_number = [0]
    scanned = [0, 0]

    class CountedEntries:
        def __init__(self, entries):
            self.entries = iter(sorted(entries, key=lambda entry: entry.name))
            entries.close()

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.entries)
            scanned[pass_number[0]] += 1
            assert scanned[pass_number[0]] <= 128
            return entry

        def close(self):
            return None

    monkeypatch.setattr(
        watch.os,
        "scandir",
        lambda path: CountedEntries(original_scandir(path))
        if Path(path) == outbox
        else original_scandir(path),
    )
    first_mtime = watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        scan_state=scan_state,
        publish_func=published.append,
    )
    assert first_mtime is None
    assert published == []
    pass_number[0] = 1
    second_mtime = watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        last_mtime_ns=first_mtime,
        scan_state=scan_state,
        publish_func=published.append,
    )

    assert scanned[0] == 128
    assert scanned[1] <= 128
    assert second_mtime is not None
    assert len(published) == 1 and published[0]["wait_id"] == wait_id


def test_question_keepalive_survives_journal_projection_error(tmp_path: Path) -> None:
    dispatch_id = "question-keepalive-on-projection-error"
    wait_id = "wait-projection-error"
    env = _env(tmp_path)
    outbox = _prepare_question_outbox(env, dispatch_id)
    question = "controller decision required"
    marker_text = steer.worker_wait_question_marker_text(
        dispatch_id,
        question,
        wait_id,
        question_kind="USER-NEED",
    )
    steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-projection-error",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question=question,
        timeout_s=30,
    )
    tail = tmp_path / "projection-error.tail"
    tail.write_text(f"!USER-NEED: {marker_text}\n", encoding="utf-8")
    status = tmp_path / "projection-error.status.json"
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env,
        start_new_session=True,
    )
    base_command = _watcher_command(
        dispatch_id=dispatch_id,
        worker_pid=worker.pid,
        tail=tail,
        status=status,
        max_idle_secs=30,
    )
    runner = tmp_path / "watcher_projection_failure.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import goalflight_watch\n"
        "goalflight_watch.consume_worker_question_outbox = lambda *args, **kwargs: None\n"
        "def fail_projection(*args, **kwargs):\n"
        "    raise RuntimeError('injected journal projection failure')\n"
        "goalflight_watch.consume_worker_journal_outbox = fail_projection\n"
        f"sys.argv = [{str(WATCH)!r}, *sys.argv[1:]]\n"
        "raise SystemExit(goalflight_watch.main())\n",
        encoding="utf-8",
    )
    watcher_command = [sys.executable, str(runner), *base_command[2:]]
    watcher = subprocess.Popen(
        watcher_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        latest = _wait_for_status(
            status,
            lambda value: value.get("worker_question_wait_id") == wait_id,
            timeout_s=5,
        )
        assert latest.get("terminal_marker") is None, latest
    finally:
        if watcher.poll() is None:
            watcher.terminate()
            try:
                watcher.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                watcher.kill()
                watcher.communicate()
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)


def test_poisoned_journal_outbox_record_retries_then_advances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import goalflight_journal

    project = tmp_path / "poison-project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    env = _env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    goalflight_journal.open_or_create_journal(project)
    dispatch_id = "poison-outbox-projection"
    outbox = _prepare_question_outbox(env, dispatch_id)
    monkeypatch.setenv("GOALFLIGHT_QUESTION_OUTBOX_DIR", str(outbox))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", dispatch_id)
    events = []
    original_projection = messages._worker_journal_projection
    monkeypatch.setattr(
        messages,
        "_worker_journal_projection",
        lambda *_args, **_kwargs: ((), RuntimeError("stage for watcher")),
    )
    for index in range(2):
        posted = messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="user_need",
            payload={"text": f"poison {index}", "project_root": str(project)},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "local", "adapter": "pytest", "transport": "worker"},
        )
        events.append(posted["envelope"]["id"])
    monkeypatch.setattr(messages, "_worker_journal_projection", original_projection)
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", "")
    original_prepare = messages._prepare_journal_delivery
    attempts = 0

    def fail_first_until_quarantined(envelope, *args, **kwargs):
        nonlocal attempts
        if envelope["id"] == events[0]:
            attempts += 1
            raise goalflight_journal.JournalIOError("persistent projection denial")
        return original_prepare(envelope, *args, **kwargs)

    monkeypatch.setattr(messages, "_prepare_journal_delivery", fail_first_until_quarantined)
    clock = [time.time()]
    monkeypatch.setattr(messages.time, "time", lambda: clock[0])
    with contextlib.suppress(Exception):
        watch.consume_worker_journal_outbox(outbox, dispatch_id)
    assert attempts == 1
    with contextlib.suppress(Exception):
        watch.consume_worker_journal_outbox(outbox, dispatch_id)
    assert attempts == 1, "backoff must prevent an immediate retry"
    clock[0] += 0.3
    for _ in range(6):
        with contextlib.suppress(Exception):
            watch.consume_worker_journal_outbox(outbox, dispatch_id)
        clock[0] += 10

    rows = goalflight_journal.Journal(project).read_all(
        "SELECT event_uuid, projected_at FROM delivery_events ORDER BY stream_seq"
    )
    assert attempts >= 3
    assert [row["event_uuid"] for row in rows] == [events[1]]
    assert rows[0]["projected_at"] is not None
    assert (outbox / "journal" / "journal-00000000000000000001.published").is_file()
    quarantine = outbox / "journal" / "journal-00000000000000000001.jsonl.quarantine.jsonl"
    assert "persistent projection denial" in quarantine.read_text(encoding="utf-8")


def test_permanently_refused_outbox_request_does_not_retry_or_pin_watcher(
    tmp_path: Path,
) -> None:
    dispatch_id = "refused-outbox"
    wait_id = "wait-refused"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="second live question",
        timeout_s=30,
    )
    active = steer.append_worker_wait_started(
        mailbox,
        dispatch_id=dispatch_id,
        timeout_secs=30,
        question_kind="USER-NEED",
        question_text="first live question",
        wait_id="wait-live",
        attempt_id="attempt-1",
        waiter_pid=os.getpid(),
    )
    published: list[dict] = []
    identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}

    first_mtime = watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        publish_func=published.append,
    )
    rejection = steer.worker_question_rejection_path(outbox, wait_id)
    assert first_mtime is not None
    assert rejection.is_file()
    assert json.loads(rejection.read_text(encoding="utf-8"))["state"] == "rejected"
    assert not (outbox / f"{wait_id}.published").exists()
    assert not steer.has_unsettled_worker_question(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id=wait_id,
    )
    second_mtime = watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        last_mtime_ns=first_mtime,
        publish_func=published.append,
    )
    assert second_mtime is not None
    assert published == []
    assert active["question_id"] == "wait-live"
    assert request["wait_id"] == wait_id


def test_watcher_publishes_exited_worker_question_and_keeps_late_reply(
    tmp_path: Path,
) -> None:
    dispatch_id = "outbox-exited-worker"
    wait_id = "wait-exited-worker"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    published: list[dict] = []

    watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=99999999,
        worker_start_token="exited-worker-incarnation",
        worker_pgid=99999999,
        active_attempt_id="attempt-1",
        publish_func=published.append,
    )

    assert len(published) == 1, published
    entries = steer.read_steer_entries(mailbox)
    arms = [entry for entry in entries if entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND]
    assert len(arms) == 1, entries
    assert arms[0]["context"]["attempt_id"] == "attempt-1"
    late = steer.append_worker_wait_reply(
        mailbox,
        dispatch_id=dispatch_id,
        wait_id=wait_id,
        text="stored for the resume",
    )
    assert late["context"]["late"] is True, late
    assert late["context"]["attempt_id"] == "attempt-1", late


def test_watcher_ignores_question_from_an_older_attempt(tmp_path: Path) -> None:
    dispatch_id = "outbox-old-attempt"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-old",
        wait_id="wait-old-attempt",
        question_kind="USER-NEED",
        question="old question",
        timeout_s=30,
    )
    published: list[dict] = []

    watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=(
            watch.goalflight_compat.process_start_identity(os.getpid()) or {}
        ).get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-current",
        publish_func=published.append,
    )

    assert published == []
    assert steer.read_steer_entries(mailbox) == []


def test_watcher_keeps_unsettled_wait_past_question_deadline(tmp_path: Path) -> None:
    dispatch_id = "outbox-past-deadline"
    wait_id = "wait-past-deadline"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="still waiting for worker settlement",
        timeout_s=30,
    )
    deadline_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    request["created_at"] = (
        (deadline_at - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    )
    request["deadline_at"] = deadline_at.isoformat().replace("+00:00", "Z")
    (outbox / f"{wait_id}.json").write_text(
        json.dumps(request),
        encoding="utf-8",
    )
    identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}
    published: list[dict] = []

    watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        publish_func=published.append,
    )

    entries = steer.read_steer_entries(mailbox)
    assert [entry.get("kind") for entry in entries] == [
        steer.WORKER_WAIT_STARTED_KIND
    ], entries
    assert published and published[0]["wait_id"] == wait_id
    assert steer.has_unsettled_worker_question(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
    )


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_TEST_PGROUP_CPU_PCT": "0.0",
            "GOALFLIGHT_STATE_DIR": str(tmp / "state"),
            "GOALFLIGHT_DISPATCH_DIR": str(tmp / "state" / "dispatch"),
            "GOALFLIGHT_MESSAGES_DIR": str(tmp / "messages"),
            "GOALFLIGHT_TASK_STORE_DIR": str(tmp / "task-store"),
            "GOALFLIGHT_JOURNAL_DIR": str(tmp / "journal"),
            "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp / "wake-ledger"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(tmp / "pids"),
            "PYTHONPATH": str(SCRIPTS) + os.pathsep + env.get("PYTHONPATH", ""),
        }
    )
    return env


def _prepare_question_outbox(env: dict[str, str], dispatch_id: str) -> Path:
    outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def _wait_with_watcher(path: Path, **kwargs) -> dict:
    """Drive the outbox consumer as the unsandboxed watcher would."""
    dispatch_id = str(kwargs["dispatch_id"])
    outbox = kwargs.pop("outbox_dir", None)
    if outbox is None:
        outbox = steer.goalflight_dispatch_paths.worker_question_outbox_dir(
            dispatch_id,
            dispatch_dir=Path(path).parent,
        )
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    publish_question = kwargs.pop("publish_question", None)
    if publish_question is None:
        publish_question = lambda _event: None
    caller_notify = kwargs.pop("notify", None)

    def report(event: dict) -> None:
        state = event.get("state")
        if state == "armed":
            identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}
            watch.consume_worker_question_outbox(
                outbox,
                Path(path),
                dispatch_id,
                worker_pid=os.getpid(),
                worker_start_token=identity.get("start_token"),
                worker_pgid=os.getpgrp(),
                publish_func=publish_question,
            )
            entries = steer.read_steer_entries(path)
            arm = steer.active_worker_wait(entries, dispatch_id=dispatch_id)
            if arm is not None:
                arm["dispatch_id"] = dispatch_id
                arm["question_id"] = arm["wait_id"]
                event["arm"] = arm
        if caller_notify is not None:
            caller_notify(event)
        if state in {"messages", "deadline"}:
            entries = steer.read_steer_entries(path)
            event_wait_id = str(event.get("wait_id") or "")
            arm = next(
                (
                    entry
                    for entry in reversed(entries)
                    if entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND
                    and entry.get("dispatch_id") == dispatch_id
                    and entry.get("question_id") == event_wait_id
                ),
                None,
            )
            if arm is not None:
                arm["dispatch_id"] = dispatch_id
                arm.setdefault("question_id", arm.get("wait_id"))
                arm_seq = int(arm.get("seq") or 0)
                existing_settlement = steer._worker_wait_settlement(
                    entries,
                    wait_id=event_wait_id,
                    after_seq=arm_seq,
                )
                if not (state == "messages" and existing_settlement is not None):
                    try:
                        context = {}
                        if state == "messages":
                            decision = "reply"
                            context["reply_seq"] = (event.get("entries") or [{}])[0].get("seq")
                        else:
                            decision = "timeout"
                            context["reason"] = "test_watcher_deadline"
                        steer.append_worker_wait_ended(
                            path,
                            arm,
                            decision=decision,
                            reply_seq=(context.get("reply_seq") if decision == "reply" else None),
                            context=context,
                        )
                    except steer.WorkerWaitReplyPending:
                        pass
                    except ValueError as exc:
                        if "already settled" not in str(exc):
                            raise

    return steer.wait_for_worker_entries(
        path,
        **kwargs,
        outbox_dir=outbox,
        notify=report,
    )


def _start_worker_wait_request(
    mailbox: Path,
    outbox: Path,
    dispatch_id: str,
    *,
    question: str,
    published: list[dict],
) -> dict:
    class WaitArmed(Exception):
        pass

    armed: dict = {}

    def stop_after_arm(event: dict) -> None:
        if event.get("state") == "armed":
            armed.update(event)
            raise WaitArmed

    try:
        _wait_with_watcher(
            mailbox,
            outbox_dir=outbox,
            dispatch_id=dispatch_id,
            acked_seqs=set(),
            question_kind="USER-CONFIRM",
            question_text=question,
            attempt_id="attempt-1",
            timeout_secs=30,
            poll_secs=0.01,
            publish_question=published.append,
            notify=stop_after_arm,
        )
    except WaitArmed:
        pass
    else:
        raise AssertionError("worker wait did not arm")
    wait_id = str(armed.get("wait_id") or "")
    assert wait_id
    return json.loads((outbox / f"{wait_id}.json").read_text(encoding="utf-8"))


def test_next_wait_recovers_prior_reply_before_publishing_question(
    tmp_path: Path,
) -> None:
    dispatch_id = "recover-before-question"
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    published: list[dict] = []
    prior_request = _start_worker_wait_request(
        mailbox,
        outbox,
        dispatch_id,
        question="authorize the first action?",
        published=published,
    )
    prior_wait_id = prior_request["wait_id"]
    steer.append_worker_wait_reply(
        mailbox,
        dispatch_id=dispatch_id,
        wait_id=prior_wait_id,
        text="approved",
        decision="yes",
    )
    published.clear()

    result = _wait_with_watcher(
        mailbox,
        outbox_dir=outbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-CONFIRM",
        question_text="authorize the next action?",
        attempt_id="attempt-1",
        timeout_secs=1,
        poll_secs=0.01,
        publish_question=published.append,
    )

    requests = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in outbox.glob("*.json")
        if not path.name.endswith(".settled.json")
    ]
    assert result["state"] == "messages", result
    assert result["wait_id"] == prior_wait_id, result
    assert len(requests) == 1, requests
    assert requests[0]["wait_id"] == prior_wait_id, requests
    assert published == [], published


def test_recovered_reply_supersedes_request_before_watcher_can_arm_it(
    tmp_path: Path,
) -> None:
    dispatch_id = "recover-racing-watcher"
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    published: list[dict] = []
    prior_request = _start_worker_wait_request(
        mailbox,
        outbox,
        dispatch_id,
        question="authorize the first action?",
        published=published,
    )
    prior_wait_id = prior_request["wait_id"]
    published.clear()
    replied = False

    def reply_before_watcher(event: dict) -> None:
        nonlocal replied
        if event.get("state") == "armed" and not replied:
            steer.append_worker_wait_reply(
                mailbox,
                dispatch_id=dispatch_id,
                wait_id=prior_wait_id,
                text="approved",
                decision="yes",
            )
            replied = True

    result = steer.wait_for_worker_entries(
        mailbox,
        outbox_dir=outbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-CONFIRM",
        question_text="authorize the next action?",
        attempt_id="attempt-1",
        timeout_secs=1,
        poll_secs=0.01,
        notify=reply_before_watcher,
    )
    renewal_wait_id = str(
        next(
            path.stem
            for path in outbox.glob("*.json")
            if not path.name.endswith(".settled.json")
            and path.stem != prior_wait_id
        )
    )

    watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=(watch.goalflight_compat.process_start_identity(os.getpid()) or {}).get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        publish_func=published.append,
    )

    entries = steer.read_steer_entries(mailbox)
    rejection = steer.worker_question_rejection_path(outbox, renewal_wait_id)
    assert replied
    assert result["state"] == "messages", result
    assert result["wait_id"] == prior_wait_id, result
    assert rejection.is_file(), rejection
    assert "superseded by recovered reply" in rejection.read_text(encoding="utf-8")
    assert not any(
        entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND
        and entry.get("question_id") == renewal_wait_id
        for entry in entries
    ), entries
    assert all(event.get("wait_id") != renewal_wait_id for event in published), published


def _wait_for_status(status: Path, predicate, *, timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    latest: dict = {}
    while time.monotonic() < deadline:
        try:
            latest = json.loads(status.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        else:
            if predicate(latest):
                return latest
        time.sleep(0.02)
    raise AssertionError(f"status predicate not reached: {latest}")


def _wait_for_wait_end(
    mailbox: Path,
    *,
    wait_id: str,
    reply_seq: int,
    timeout_s: float = 5.0,
) -> list[dict]:
    """Wait for the worker's exact reply-end row in tests."""
    deadline = time.monotonic() + timeout_s
    entries: list[dict] = []
    while time.monotonic() < deadline:
        entries = steer.read_steer_entries(mailbox)
        if any(
            entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
            and entry.get("reply_to") == wait_id
            and (entry.get("context") or {}).get("reply_seq") == reply_seq
            for entry in entries
        ):
            return entries
        threading.Event().wait(0.01)
    raise AssertionError(f"worker wait end row did not appear: {entries}")


def _watcher_command(
    *,
    dispatch_id: str,
    worker_pid: int,
    tail: Path,
    status: Path,
    max_idle_secs: float,
) -> list[str]:
    # Idle-timeout classification requires a measured tree probe, which needs a
    # worker cwd distinct from the canonical root. Status/tail stay beside the
    # fixture; the scanned cwd is an empty sibling so watcher writes cannot
    # keep the tree "alive".
    harness_root = status.parent
    project_root = harness_root / "canonical-root"
    worker_cwd = harness_root / "worker-cwd"
    project_root.mkdir(exist_ok=True)
    worker_cwd.mkdir(exist_ok=True)
    identity = watch.goalflight_compat.process_start_identity(worker_pid) or {}
    identity_args: list[str] = []
    if identity.get("start_token"):
        identity_path = status.with_suffix(status.suffix + ".worker-identity.json")
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        identity_args = ["--worker-identity-json", str(identity_path)]
    return [
        sys.executable,
        str(WATCH),
        "--pid",
        str(worker_pid),
        "--tail",
        str(tail),
        "--status-json",
        str(status),
        "--dispatch-id",
        dispatch_id,
        "--project-root",
        str(project_root),
        "--worker-cwd",
        str(worker_cwd),
        "--poll-secs",
        "0.05",
        "--max-idle-secs",
        str(max_idle_secs),
        "--stay-after-terminal",
        *identity_args,
    ]


def test_user_need_backlog_unblocks_without_arming_a_question(tmp_path: Path) -> None:
    mailbox = tmp_path / "backlog.steer.jsonl"
    steer.append_steer_entry(mailbox, "reply already waiting", dispatch_id="backlog")
    events: list[dict] = []

    started = time.monotonic()
    result = _wait_with_watcher(
        mailbox,
        dispatch_id="backlog",
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need a decision",
        timeout_secs=0.05,
        poll_secs=0.05,
        notify=events.append,
    )

    assert time.monotonic() - started < 0.5
    assert result["state"] == "messages"
    assert result["arm_time_backlog"] is True
    assert result["entries"][0]["text"] == "reply already waiting"
    assert events == [result]
    assert not any(entry.get("kind") == steer.WORKER_WAIT_REPLY_KIND for entry in steer.read_steer_entries(mailbox))
    assert not list(mailbox.parent.glob("backlog.questions/*.json"))


def test_worker_mailbox_poll_does_not_open_writer_lock(tmp_path: Path) -> None:
    mailbox = tmp_path / "read-only.steer.jsonl"
    mailbox.write_text("", encoding="utf-8")

    assert steer.read_steer_entries_read_only(mailbox) == []
    assert not messages.mail_lock_path(mailbox).exists()


def test_wait_reply_does_not_spawn_a_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = tmp_path / "no-wait-process.steer.jsonl"

    def reject_process(*_args, **_kwargs):
        raise AssertionError("worker wait spawned a process")

    monkeypatch.setattr(subprocess, "Popen", reject_process)

    def reply_on_arm(event: dict) -> None:
        if event["state"] == "armed":
            steer.append_worker_wait_reply(
                mailbox,
                dispatch_id="no-wait-process",
                wait_id=event["wait_id"],
                text="continue",
            )

    result = _wait_with_watcher(
        mailbox,
        dispatch_id="no-wait-process",
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need a decision",
        timeout_secs=1.0,
        poll_secs=0.01,
        notify=reply_on_arm,
    )
    assert result["state"] == "messages", result


def test_wait_returns_promptly_at_its_independent_deadline(tmp_path: Path) -> None:
    mailbox = tmp_path / "deadline.steer.jsonl"
    started = time.monotonic()
    result = _wait_with_watcher(
        mailbox,
        dispatch_id="deadline",
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need a decision",
        timeout_secs=0.2,
        poll_secs=1.0,
    )
    elapsed = time.monotonic() - started

    assert result["state"] == "deadline"
    assert 0.15 <= elapsed < 0.6
    entries = steer.read_steer_entries(mailbox)
    assert [entry["kind"] for entry in entries] == [
        steer.WORKER_WAIT_STARTED_KIND,
        steer.WORKER_WAIT_ENDED_KIND,
    ]
    assert entries[-1]["decision"] == "timeout"
    assert steer.active_worker_wait(entries, dispatch_id="deadline") is None
    outbox = steer.goalflight_dispatch_paths.worker_question_outbox_dir(
        "deadline",
        dispatch_dir=mailbox.parent,
    )
    request = json.loads((outbox / f"{entries[0]['question_id']}.json").read_text())
    settlement = steer.read_worker_question_settlement(outbox, request)
    assert settlement and settlement["state"] == "timeout", settlement


def test_valid_question_kinds_round_trip_through_watcher(tmp_path: Path) -> None:
    for index, question_kind in enumerate(sorted(steer.WORKER_WAIT_QUESTION_KINDS)):
        mailbox = tmp_path / f"question-{index}.steer.jsonl"
        published: list[dict] = []
        result = _wait_with_watcher(
            mailbox,
            dispatch_id=f"question-{index}",
            acked_seqs=set(),
            question_kind=question_kind,
            question_text="supply the missing value",
            timeout_secs=0.05,
            poll_secs=0.2,
            publish_question=published.append,
        )

        assert result["state"] == "deadline", result
        assert len(published) == 1, published
        assert published[0]["question_kind"] == question_kind, published
        assert published[0]["question_text"] == "supply the missing value", published
        reply_tokens = shlex.split(published[0]["reply_command"])
        assert reply_tokens.index("<answer>") < reply_tokens.index("--reply-to"), published
        tail = tmp_path / f"question-{index}.tail"
        tail.write_text(
            f"!{question_kind}: {published[0]['question_marker_text']}\n",
            encoding="utf-8",
        )
        markers, _size = watch.extract_markers(tail)
        assert markers[-1]["kind"] == question_kind, markers
        entries = steer.read_steer_entries(mailbox)
        assert [entry["kind"] for entry in entries] == [
            steer.WORKER_WAIT_STARTED_KIND,
            steer.WORKER_WAIT_ENDED_KIND,
        ], entries
        assert entries[-1]["decision"] == "timeout", entries


def test_timeout_settlement_accepts_and_records_late_reply_then_next_wait(
    tmp_path: Path,
) -> None:
    mailbox = tmp_path / "late-reply.steer.jsonl"
    dispatch_id = "late-reply"
    first = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-CONFIRM",
        question_text="first question",
        timeout_secs=0.05,
        poll_secs=0.01,
    )
    assert first["state"] == "deadline", first
    entries = steer.read_steer_entries(mailbox)
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
        and entry.get("decision") == "timeout"
        for entry in entries
    ) == 1, entries
    late = steer.append_worker_wait_reply(
        mailbox,
        dispatch_id=dispatch_id,
        wait_id=first["wait_id"],
        text="arrived after timeout",
        decision="yes",
    )
    assert late["context"]["late"] is True, late

    second = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-CONFIRM",
        question_text="first question",
        timeout_secs=0.05,
        poll_secs=0.2,
    )
    assert second["state"] == "deadline", second
    assert second["wait_id"] != first["wait_id"], second

    resumed = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-CONFIRM",
        question_text="first question",
        attempt_id="resumed-attempt",
        resume_wait_id=first["wait_id"],
        timeout_secs=0.05,
        poll_secs=0.01,
    )
    assert resumed["state"] == "messages", resumed
    assert resumed["recovered_admitted_reply"] is True, resumed
    assert resumed["wait_id"] == first["wait_id"]
    assert resumed["entries"] == [late]

    entries = steer.read_steer_entries(mailbox)
    assert sum(entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries) == 2, entries
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
        and entry.get("decision") == "timeout"
        for entry in entries
    ) == 2, entries
    outbox = steer.goalflight_dispatch_paths.worker_question_outbox_dir(
        dispatch_id,
        dispatch_dir=mailbox.parent,
    )
    requests = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in outbox.glob("*.json")
        if not path.name.endswith(".settled.json")
    ]
    assert len(requests) == 2, requests
    assert len({request["wait_id"] for request in requests}) == 2, requests
    assert {request["question"] for request in requests} == {
        "first question",
        "first question",
    }
    assert late["reply_to"] == first["wait_id"]


def test_new_same_text_question_does_not_recover_a_late_reply(
    tmp_path: Path,
) -> None:
    mailbox = tmp_path / "same-text-late-reply.steer.jsonl"
    dispatch_id = "same-text-late-reply"
    first = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="same question",
        timeout_secs=0.02,
        poll_secs=0.01,
    )
    late = steer.append_worker_wait_reply(
        mailbox,
        dispatch_id=dispatch_id,
        wait_id=first["wait_id"],
        text="old approval",
    )

    new_question = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="same question",
        timeout_secs=0.02,
        poll_secs=0.01,
    )

    assert new_question["state"] == "deadline", new_question
    assert new_question["wait_id"] != first["wait_id"], new_question
    assert new_question.get("entries") == [], new_question
    assert late["reply_to"] == first["wait_id"]


def test_next_wait_recovers_reply_written_during_final_sleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = tmp_path / "final-sleep.steer.jsonl"
    clock = [100.0]
    arm: dict | None = None
    reply_written = False

    monkeypatch.setattr(steer, "active_monotonic", lambda: clock[0])

    def report(event: dict) -> None:
        nonlocal arm
        if event["state"] == "armed":
            arm = event["arm"]

    def sleep_through_deadline(seconds: float) -> None:
        nonlocal reply_written
        assert arm is not None
        assert not reply_written
        clock[0] += seconds / 2
        steer.append_worker_wait_reply(
            mailbox,
            dispatch_id="final-sleep",
            wait_id=str(arm["question_id"]),
            text="accepted before the deadline",
        )
        reply_written = True
        clock[0] += seconds / 2

    monkeypatch.setattr(steer.time, "sleep", sleep_through_deadline)
    first = _wait_with_watcher(
        mailbox,
        dispatch_id="final-sleep",
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need a boundary answer",
        timeout_secs=1.0,
        poll_secs=2.0,
        notify=report,
    )

    assert reply_written
    assert first["state"] == "messages"
    assert first["entries"][0]["text"] == "accepted before the deadline"
    entries = _wait_for_wait_end(
        mailbox,
        wait_id=str(first["wait_id"]),
        reply_seq=int(first["entries"][0]["seq"]),
    )
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries
    ) == 1
    assert any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
        for entry in entries
    )


def test_wait_deadline_is_independent_of_mailbox_writer_lock(tmp_path: Path) -> None:
    mailbox = tmp_path / "locked.steer.jsonl"
    outbox = tmp_path / "locked.questions"
    outbox.mkdir()
    ready = threading.Event()

    def hold_lock() -> None:
        with messages.mail_lock(mailbox):
            ready.set()
            time.sleep(0.5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert ready.wait(timeout=2), "lock holder did not become ready"

        started = time.monotonic()
        result = steer.wait_for_worker_entries(
            mailbox,
            dispatch_id="locked",
            acked_seqs=set(),
            question_kind="USER-NEED",
            question_text="need a decision",
            attempt_id="attempt-1",
            outbox_dir=outbox,
            timeout_secs=0.2,
            poll_secs=0.05,
        )
        elapsed = time.monotonic() - started

        assert result["state"] == "deadline"
        assert 0.15 <= elapsed < 0.6
    finally:
        holder.join(timeout=2)
    assert not holder.is_alive(), "lock holder did not release"


def test_bounded_mailbox_read_retries_and_times_out_typed(
    tmp_path: Path,
) -> None:
    mailbox = tmp_path / "read-lock.steer.jsonl"
    ready = threading.Event()

    def hold_lock(seconds: float) -> None:
        with messages.mail_lock(mailbox):
            ready.set()
            time.sleep(seconds)

    holder = threading.Thread(target=hold_lock, args=(0.3,))
    holder.start()
    assert ready.wait(timeout=2), "mailbox lock holder did not become ready"
    try:
        assert steer.read_steer_entries(mailbox, lock_timeout_secs=5.0) == []
    finally:
        holder.join(timeout=5)
    assert not holder.is_alive(), "mailbox lock holder did not release"

    ready.clear()
    holder = threading.Thread(target=hold_lock, args=(0.3,))
    holder.start()
    assert ready.wait(timeout=2), "mailbox lock holder did not become ready"
    try:
        with pytest.raises(TimeoutError, match=r"mailbox lock busy for 0.05s"):
            steer.read_steer_entries(mailbox, lock_timeout_secs=0.05)
    finally:
        holder.join(timeout=5)
    assert not holder.is_alive(), "mailbox lock holder did not release"


def test_watcher_arm_lock_budget_is_independent_of_question_deadline(
    tmp_path: Path,
) -> None:
    dispatch_id = "watcher-lock-budget"
    wait_id = "wait-lock-budget"
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="a long question deadline must not grow the lock wait",
        timeout_s=600,
    )
    ready = threading.Event()

    def hold_lock() -> None:
        with messages.mail_lock(mailbox):
            ready.set()
            time.sleep(0.3)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    published: list[dict] = []
    identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}
    try:
        assert ready.wait(timeout=2), "lock holder did not become ready"
        started = time.monotonic()
        retry = watch.consume_worker_question_outbox(
            outbox,
            mailbox,
            dispatch_id,
            worker_pid=os.getpid(),
            worker_start_token=identity.get("start_token"),
            worker_pgid=os.getpgrp(),
            active_attempt_id="attempt-1",
            publish_func=published.append,
        )
        elapsed = time.monotonic() - started
        assert retry is None
        assert elapsed < 0.2, elapsed
        assert published == []
    finally:
        holder.join(timeout=2)
    assert not holder.is_alive(), "mailbox lock holder did not release"

    watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="attempt-1",
        publish_func=published.append,
    )
    assert len(published) == 1, published


def test_watcher_retries_resumed_reply_promotion_after_settlement_lock_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_dir = tmp_path / "dispatch"
    dispatch_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    parent_id = "settlement-lock-parent"
    child_id = "settlement-lock-child"
    wait_id = "settlement-lock-wait"
    parent_outbox = steer.goalflight_dispatch_paths.worker_question_outbox_dir(
        parent_id,
        dispatch_dir=dispatch_dir,
    )
    child_outbox = steer.goalflight_dispatch_paths.worker_question_outbox_dir(
        child_id,
        dispatch_dir=dispatch_dir,
    )
    parent_outbox.mkdir()
    child_outbox.mkdir()
    parent_mailbox = dispatch_dir / f"{parent_id}.steer.jsonl"
    child_mailbox = dispatch_dir / f"{child_id}.steer.jsonl"
    request = steer.write_worker_question_request(
        parent_outbox,
        dispatch_id=parent_id,
        attempt_id="parent-attempt",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    steer.append_worker_wait_started(
        parent_mailbox,
        dispatch_id=parent_id,
        timeout_secs=request["timeout_s"],
        question_kind=request["question_kind"],
        question_text=request["question"],
        wait_id=wait_id,
        attempt_id="parent-attempt",
        deadline_at=request["deadline_at"],
    )
    reply = steer.append_worker_wait_reply(
        parent_mailbox,
        dispatch_id=parent_id,
        wait_id=wait_id,
        text="approved",
    )
    steer.write_worker_question_settlement(
        child_outbox,
        dispatch_id=parent_id,
        attempt_id="parent-attempt",
        wait_id=wait_id,
        state="reply",
        deadline_at=request["deadline_at"],
        reply_seq=reply["seq"],
    )
    records = {
        parent_id: {
            "dispatch_id": parent_id,
            "project_root": str(project_root),
            "state": "running",
            "attempt_id": "parent-attempt",
        },
        child_id: {
            "dispatch_id": child_id,
            "parent_dispatch_id": parent_id,
            "project_root": str(project_root),
            "state": "running",
            "attempt_id": "child-attempt",
        },
    }
    monkeypatch.setattr(watch.goalflight_ledger, "read_record", records.get)
    monkeypatch.setattr(
        watch.goalflight_dispatch_paths,
        "steer_file",
        lambda _dispatch_id: parent_mailbox,
    )

    ready = threading.Event()
    release = threading.Event()

    def hold_settlement_lock() -> None:
        with steer._worker_question_settlement_lock(parent_outbox, wait_id):
            ready.set()
            release.wait(5)

    holder = threading.Thread(target=hold_settlement_lock)
    holder.start()
    identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}
    try:
        assert ready.wait(2), "settlement lock holder did not become ready"
        started = time.monotonic()
        retry = watch.consume_worker_question_outbox(
            child_outbox,
            child_mailbox,
            child_id,
            worker_pid=os.getpid(),
            worker_start_token=identity.get("start_token"),
            worker_pgid=os.getpgrp(),
            active_attempt_id="child-attempt",
            publish_func=lambda _event: None,
        )
        elapsed = time.monotonic() - started
        assert retry is None
        assert elapsed < 2.5, elapsed
    finally:
        release.set()
        holder.join(timeout=2)
    assert not holder.is_alive(), "settlement lock holder did not release"

    retried = watch.consume_worker_question_outbox(
        child_outbox,
        child_mailbox,
        child_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        active_attempt_id="child-attempt",
        publish_func=lambda _event: None,
    )
    assert retried is not None
    parent_settlement = steer.read_worker_question_settlement(parent_outbox, request)
    assert parent_settlement and parent_settlement["state"] == "reply"
    assert parent_settlement["reply_seq"] == reply["seq"]


def test_watcher_retries_unpublished_question_without_duplicate_events(
    tmp_path: Path,
) -> None:
    dispatch_id = "publish-retry"
    wait_id = "wait-publish-retry"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    marker = outbox / f"{wait_id}.published"
    steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}

    def fail_once(_event: dict) -> None:
        raise RuntimeError("controller event store unavailable")

    retry_mtime = watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        publish_func=fail_once,
    )
    assert retry_mtime is None
    assert not marker.exists()

    published: list[dict] = []
    retry_mtime = watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        last_mtime_ns=retry_mtime,
        publish_func=published.append,
    )
    watch.consume_worker_question_outbox(
        outbox,
        mailbox,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_start_token=identity.get("start_token"),
        worker_pgid=os.getpgrp(),
        last_mtime_ns=retry_mtime,
        publish_func=published.append,
    )

    assert marker.exists()
    assert len(published) == 1, published
    entries = steer.read_steer_entries(mailbox)
    assert sum(entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries) == 1


def test_watcher_retries_question_publication_after_worker_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_id = "publish-after-exit"
    env = _env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    outbox = _prepare_question_outbox(env, dispatch_id)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-1",
        wait_id="wait-after-exit",
        question_kind="USER-NEED",
        question="need a decision",
        timeout_s=30,
    )
    tail = tmp_path / "publish-after-exit.tail"
    tail.write_text(f"!COMPLETE: {dispatch_id} — done\n", encoding="utf-8")
    worker = subprocess.Popen([sys.executable, "-c", "pass"], env=env)
    worker.wait(timeout=5)
    status = tmp_path / "publish-after-exit.status.json"
    attempts = 0

    def publish_once_fails(_event: dict) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary publication failure")

    monkeypatch.setattr(steer, "publish_worker_wait_question", publish_once_fails)
    command = _watcher_command(
        dispatch_id=dispatch_id,
        worker_pid=worker.pid,
        tail=tail,
        status=status,
        max_idle_secs=0.2,
    )
    monkeypatch.setattr(sys, "argv", command[1:])

    result = watch.main()

    assert result == 0
    assert attempts == 2, attempts
    assert (outbox / "wait-after-exit.published").is_file()
    entries = steer.read_steer_entries(mailbox)
    assert sum(entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries) == 1
    assert json.loads(status.read_text(encoding="utf-8"))["state"] == "complete"


def test_watcher_main_loop_ignores_non_utf8_settlement_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatch_id = "non-utf8-settlement-main-loop"
    env = _env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    outbox = _prepare_question_outbox(env, dispatch_id)
    (outbox / "corrupt-wait.settled.json").write_bytes(b"\xff")
    tail = tmp_path / "non-utf8-settlement.tail"
    tail.write_text(f"!COMPLETE: {dispatch_id} — done\n", encoding="utf-8")
    worker = subprocess.Popen([sys.executable, "-c", "pass"], env=env)
    worker.wait(timeout=5)
    status = tmp_path / "non-utf8-settlement.status.json"
    command = _watcher_command(
        dispatch_id=dispatch_id,
        worker_pid=worker.pid,
        tail=tail,
        status=status,
        max_idle_secs=0.2,
    )
    monkeypatch.setattr(sys, "argv", command[1:])

    result = watch.main()

    assert result == 0
    assert json.loads(status.read_text(encoding="utf-8"))["state"] == "complete"
    assert "ignoring unreadable worker question settlement receipt" in caplog.text


def test_acp_user_need_turn_end_never_signals_live_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_id = "acp-user-need-turn-end"
    env = _env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    tail = tmp_path / "acp-user-need.tail"
    status = tmp_path / "acp-user-need.status.json"
    release = tmp_path / "release-acp-worker"
    worker_code = r'''
import sys
import time
from pathlib import Path

dispatch_id = sys.argv[1]
release = Path(sys.argv[2])
print(f"!USER-NEED: {dispatch_id} — wait for controller input", flush=True)
while not release.exists():
    time.sleep(0.01)
'''
    with tail.open("w", encoding="utf-8") as worker_stdout:
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code, dispatch_id, str(release)],
            stdout=worker_stdout,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    monkeypatch.setattr(watch, "_load_dispatch_record", lambda _dispatch_id: {"shape": "acp"})
    monkeypatch.setattr(watch, "WORKER_WAIT_ARM_GRACE_SECS", 0.0)
    live_identity = watch.goalflight_compat.process_start_identity(worker.pid) or {
        "pid": worker.pid,
        "start_token": "test-acp-worker",
    }
    monkeypatch.setattr(
        watch._WorkerProcessProbe,
        "sample",
        lambda _probe: (True, "live", live_identity),
    )
    termination_calls: list[tuple] = []

    def record_termination(*args, **kwargs):
        termination_calls.append((args, kwargs))
        return {
            "worker_alive": False,
            "worker_termination_confirmed": True,
            "worker_termination_signals": ["SIGTERM"],
        }

    monkeypatch.setattr(watch, "terminate_indeterminate_worker", record_termination)
    command = _watcher_command(
        dispatch_id=dispatch_id,
        worker_pid=worker.pid,
        tail=tail,
        status=status,
        max_idle_secs=0.2,
    )
    command.extend(["--agent", "codex-acp"])
    monkeypatch.setattr(sys, "argv", command[1:])

    try:
        result = watch.main()
        final = json.loads(status.read_text(encoding="utf-8"))
        assert result != 0
        assert final.get("worker_alive") is True, final
        assert (final.get("terminal_marker") or {}).get("kind") == "USER-NEED", final
        assert termination_calls == [], termination_calls
        assert worker.poll() is None, "ACP USER-NEED must end the turn without signaling its worker"
    finally:
        release.touch()
        worker.wait(timeout=5)


def test_carrier_validation_error_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    @contextlib.contextmanager
    def broken_carrier(*_args, **_kwargs):
        raise messages.MessageError("carrier identity changed")
        yield  # pragma: no cover

    monkeypatch.setattr(messages, "carrier_transaction", broken_carrier)
    with pytest.raises(messages.MessageError):
        steer.read_steer_entries(tmp_path / "broken.steer.jsonl")
    assert watch._active_worker_wait(
        tmp_path / "broken.steer.jsonl",
        "broken",
        now_mono=time.monotonic(),
    ) is None


def test_dead_waiter_cannot_leave_idle_exemption(tmp_path: Path) -> None:
    mailbox = tmp_path / "dead-waiter.steer.jsonl"
    ready = tmp_path / "waiter-ready"
    waiter_code = r'''
import os
import time
from pathlib import Path
import goalflight_steer_mailbox as steer

steer.append_worker_wait_started(
    Path(os.environ["TEST_STEER_FILE"]),
    dispatch_id="dead-waiter",
    timeout_secs=5,
    question_kind="USER-NEED",
    question_text="need a decision",
)
Path(os.environ["TEST_READY_FILE"]).write_text("ready", encoding="utf-8")
time.sleep(5)
'''
    env = _env(tmp_path)
    env.update(
        {
            "TEST_STEER_FILE": str(mailbox),
            "TEST_READY_FILE": str(ready),
        }
    )
    waiter = subprocess.Popen([sys.executable, "-c", waiter_code], env=env)
    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "waiter did not become ready"
        entries = steer.read_steer_entries(mailbox)
        assert steer.active_worker_wait(entries, dispatch_id="dead-waiter") is not None
    finally:
        waiter.terminate()
        waiter.wait(timeout=5)

    renewed = steer.append_worker_wait_started(
        mailbox,
        dispatch_id="dead-waiter",
        timeout_secs=1,
        question_kind="USER-NEED",
        question_text="the replacement question",
    )
    steer.append_worker_wait_ended(mailbox, renewed, decision="timeout")
    entries = steer.read_steer_entries(mailbox)
    assert any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
        and entry.get("reply_to") == entries[0].get("question_id")
        and entry.get("decision") == "failed"
        for entry in entries
    ), entries
    assert steer.active_worker_wait(entries, dispatch_id="dead-waiter") is None


def test_wait_arm_matches_only_its_exact_question_marker(tmp_path: Path) -> None:
    mailbox = tmp_path / "correlation.steer.jsonl"
    arm = steer.append_worker_wait_started(
        mailbox,
        dispatch_id="correlation",
        timeout_secs=1,
        question_kind="USER-CONFIRM",
        question_text="approve?",
    )
    wait_state = steer.active_worker_wait(
        steer.read_steer_entries(mailbox),
        dispatch_id="correlation",
    )
    assert wait_state is not None
    stale = {
        "kind": "USER-CONFIRM",
        "text": "correlation — approve? [wait-id:historical]",
        "line": 1,
    }
    current = {
        "kind": wait_state["question_kind"],
        "text": wait_state["question_marker_text"],
        "line": 2,
    }
    assert not watch._worker_wait_marker_matches(stale, wait_state, "correlation")
    assert watch._worker_wait_marker_matches(current, wait_state, "correlation")


def test_unknown_waiter_identity_keeps_prior_arm_unsettled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = tmp_path / "unknown-waiter.steer.jsonl"
    arm = steer.append_worker_wait_started(
        mailbox,
        dispatch_id="unknown-waiter",
        timeout_secs=5,
        question_kind="USER-NEED",
        question_text="keep the live question",
    )
    monkeypatch.setattr(
        steer.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: None,
    )

    with pytest.raises(ValueError, match="renewal refused"):
        steer.append_worker_wait_started(
            mailbox,
            dispatch_id="unknown-waiter",
            timeout_secs=1,
            question_kind="USER-NEED",
            question_text="replacement question",
        )

    entries = steer.read_steer_entries(mailbox)
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries
    ) == 1, entries
    assert not any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
        and entry.get("reply_to") == arm["question_id"]
        for entry in entries
    ), entries


def test_dead_waiter_failed_settlement_preserves_reply_admitted_during_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = tmp_path / "dead-waiter-reply-race.steer.jsonl"
    dispatch_id = "dead-waiter-reply-race"
    arm = steer.append_worker_wait_started(
        mailbox,
        dispatch_id=dispatch_id,
        timeout_secs=5,
        question_kind="USER-NEED",
        question_text="preserve the answer",
    )
    monkeypatch.setattr(
        steer.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: False,
    )
    real_end = steer.append_worker_wait_ended
    injected = False

    def append_reply_before_failed_end(path, prior, **kwargs):
        nonlocal injected
        if kwargs.get("decision") == "failed" and not injected:
            injected = True
            steer.append_worker_wait_reply(
                path,
                dispatch_id=dispatch_id,
                wait_id=str(arm["question_id"]),
                text="durable answer",
            )
        return real_end(path, prior, **kwargs)

    monkeypatch.setattr(steer, "append_worker_wait_ended", append_reply_before_failed_end)
    with pytest.raises(steer.WorkerWaitReplyPending) as pending:
        steer.append_worker_wait_started(
            mailbox,
            dispatch_id=dispatch_id,
            timeout_secs=1,
            question_kind="USER-NEED",
            question_text="replacement question",
        )

    assert pending.value.reply["text"] == "durable answer", pending.value.reply
    entries = steer.read_steer_entries(mailbox)
    assert any(
        entry.get("kind") == steer.WORKER_WAIT_REPLY_KIND
        and entry.get("text") == "durable answer"
        for entry in entries
    ), entries
    assert not any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
        and entry.get("reply_to") == arm["question_id"]
        and entry.get("decision") == "failed"
        for entry in entries
    ), entries


def test_unsettled_wait_cannot_be_renewed_and_settlement_does_not_resurrect_it(
    tmp_path: Path,
) -> None:
    mailbox = tmp_path / "superseded.steer.jsonl"
    older = steer.append_worker_wait_started(
        mailbox,
        dispatch_id="superseded",
        timeout_secs=2,
        question_kind="USER-NEED",
        question_text="older question",
    )
    with pytest.raises(ValueError, match="renewal refused"):
        steer.append_worker_wait_started(
            mailbox,
            dispatch_id="superseded",
            timeout_secs=2,
            question_kind="USER-NEED",
            question_text="overlapping question",
        )
    older_reply = steer.append_worker_wait_reply(
        mailbox,
        dispatch_id="superseded",
        wait_id=str(older["question_id"]),
        text="first answer",
    )
    steer.append_worker_wait_ended(
        mailbox,
        older,
        decision="reply",
        reply_seq=int(older_reply["seq"]),
    )
    newest = steer.append_worker_wait_started(
        mailbox,
        dispatch_id="superseded",
        timeout_secs=2,
        question_kind="USER-NEED",
        question_text="newer question",
    )
    newest_reply = steer.append_worker_wait_reply(
        mailbox,
        dispatch_id="superseded",
        wait_id=str(newest["question_id"]),
        text="second answer",
    )
    steer.append_worker_wait_ended(
        mailbox,
        newest,
        decision="reply",
        reply_seq=int(newest_reply["seq"]),
    )

    assert steer.active_worker_wait(
        steer.read_steer_entries(mailbox),
        dispatch_id="superseded",
    ) is None


def test_waiter_deadline_does_not_expire_arm_without_settlement(tmp_path: Path) -> None:
    mailbox = tmp_path / "nonrenewable.steer.jsonl"
    steer.append_worker_wait_started(
        mailbox,
        dispatch_id="nonrenewable",
        timeout_secs=0.05,
        question_kind="USER-NEED",
        question_text="first question",
    )
    time.sleep(0.08)
    assert steer.active_worker_wait(
        steer.read_steer_entries(mailbox),
        dispatch_id="nonrenewable",
    ) is not None

    first = steer.read_steer_entries(mailbox)[0]
    steer.append_worker_wait_ended(mailbox, first, decision="timeout")
    assert steer.active_worker_wait(
        steer.read_steer_entries(mailbox),
        dispatch_id="nonrenewable",
    ) is None

    renewed = steer.append_worker_wait_started(
        mailbox,
        dispatch_id="nonrenewable",
        timeout_secs=1,
        question_kind="USER-NEED",
        question_text="renewed question",
    )
    entries = steer.read_steer_entries(mailbox)
    assert any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
        and entry.get("reply_to") == entries[0].get("question_id")
        and entry.get("decision") == "timeout"
        for entry in entries
    ), entries


def test_watcher_wait_exemption_ends_at_question_deadline(
    tmp_path: Path,
) -> None:
    dispatch_id = "watcher-deadline-settlement"
    wait_id = "watcher-deadline-wait"
    mailbox = tmp_path / f"{dispatch_id}.steer.jsonl"
    outbox = tmp_path / f"{dispatch_id}.questions"
    outbox.mkdir()
    request = steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-a",
        wait_id=wait_id,
        question_kind="USER-NEED",
        question="wait until the worker settles",
        timeout_s=30,
    )
    identity = watch.goalflight_compat.process_start_identity(os.getpid()) or {}
    deadline_at = datetime.fromisoformat(
        request["deadline_at"].replace("Z", "+00:00")
    )
    deadline_mono = steer.active_monotonic() + max(
        0.0,
        (deadline_at - datetime.now(timezone.utc)).total_seconds(),
    )
    arm = steer.append_worker_wait_started(
        mailbox,
        dispatch_id=dispatch_id,
        timeout_secs=30,
        question_kind="USER-NEED",
        question_text=request["question"],
        wait_id=wait_id,
        attempt_id="attempt-a",
        waiter_pid=os.getpid(),
        waiter_start_token=str(identity.get("start_token") or ""),
        waiter_pgid=os.getpgrp(),
        deadline_mono=deadline_mono,
        deadline_at=request["deadline_at"],
    )

    tracked = steer.active_worker_wait(
        steer.read_steer_entries(mailbox),
        dispatch_id=dispatch_id,
        worker_pid=os.getpid(),
        worker_pgid=os.getpgrp(),
    )
    assert tracked and tracked["wait_id"] == wait_id, tracked
    deadline_mono = tracked["deadline_awake_mono_ns"] / 1_000_000_000
    live_mono = deadline_mono - 1.0
    assert watch._cached_worker_wait_is_valid(
        tracked,
        now_mono=live_mono,
        worker_pid=os.getpid(),
        worker_pgid=os.getpgrp(),
    )
    expired_mono = deadline_mono + 1.0
    assert watch._cached_worker_wait_is_valid(
        tracked,
        now_mono=expired_mono,
        worker_pid=os.getpid(),
        worker_pgid=os.getpgrp(),
    ) is False

    steer.write_worker_question_settlement(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="attempt-a",
        wait_id=wait_id,
        state="timeout",
        deadline_at=request["deadline_at"],
    )
    assert watch._cached_worker_wait_is_valid(
        tracked,
        now_mono=expired_mono,
        worker_pid=os.getpid(),
        worker_pgid=os.getpgrp(),
    ) is False
    assert arm["question_id"] == wait_id


def test_two_concurrent_waiters_create_exactly_one_arm(tmp_path: Path) -> None:
    mailbox = tmp_path / "one-active-wait.steer.jsonl"
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    outcome_dir = tmp_path / "outcomes"
    outcome_dir.mkdir()
    go = tmp_path / "go"
    release_winner = tmp_path / "release-winner"
    child_code = r'''
import os
import time
from pathlib import Path
import goalflight_steer_mailbox as steer

ready = Path(os.environ["TEST_READY_DIR"]) / os.environ["TEST_SLOT"]
ready.write_text("ready", encoding="utf-8")
go = Path(os.environ["TEST_GO_FILE"])
while not go.exists():
    time.sleep(0.001)
try:
    steer.append_worker_wait_started(
        Path(os.environ["TEST_STEER_FILE"]),
        dispatch_id="one-active-wait",
        timeout_secs=2,
        question_kind="USER-NEED",
        question_text="one consumer only",
    )
except ValueError:
    (Path(os.environ["TEST_OUTCOME_DIR"]) / os.environ["TEST_SLOT"]).write_text(
        "REFUSED", encoding="utf-8"
    )
    print("REFUSED", flush=True)
else:
    (Path(os.environ["TEST_OUTCOME_DIR"]) / os.environ["TEST_SLOT"]).write_text(
        "ARMED", encoding="utf-8"
    )
    print("ARMED", flush=True)
    release = Path(os.environ["TEST_RELEASE_WINNER"])
    while not release.exists():
        time.sleep(0.001)
'''
    children: list[subprocess.Popen] = []
    for slot in ("a", "b"):
        env = _env(tmp_path)
        env.update(
            {
                "TEST_READY_DIR": str(ready_dir),
                "TEST_SLOT": slot,
                "TEST_GO_FILE": str(go),
                "TEST_STEER_FILE": str(mailbox),
                "TEST_OUTCOME_DIR": str(outcome_dir),
                "TEST_RELEASE_WINNER": str(release_winner),
            }
        )
        children.append(
            subprocess.Popen(
                [sys.executable, "-c", child_code],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    deadline = time.monotonic() + 2
    while len(list(ready_dir.iterdir())) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(list(ready_dir.iterdir())) == 2, "waiters did not reach the barrier"
    go.write_text("go", encoding="utf-8")
    try:
        deadline = time.monotonic() + 5
        while len(list(outcome_dir.iterdir())) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(list(outcome_dir.iterdir())) == 2, "waiters did not report outcomes"
        refused_slots = [
            path.name for path in outcome_dir.iterdir() if path.read_text() == "REFUSED"
        ]
        assert len(refused_slots) == 1, list(outcome_dir.iterdir())
        refused_child = children[("a", "b").index(refused_slots[0])]
        refused_child.wait(timeout=5)
        release_winner.touch()
        outputs = [child.communicate(timeout=5) for child in children]
        assert sorted(stdout.strip() for stdout, _stderr in outputs) == ["ARMED", "REFUSED"], outputs
        starts = [
            entry
            for entry in steer.read_steer_entries(mailbox)
            if entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND
        ]
        assert len(starts) == 1, starts
    finally:
        release_winner.touch()
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


def test_two_concurrent_repliers_create_exactly_one_reply(tmp_path: Path) -> None:
    mailbox = tmp_path / "one-reply.steer.jsonl"
    arm = steer.append_worker_wait_started(
        mailbox,
        dispatch_id="one-reply",
        timeout_secs=2,
        question_kind="USER-NEED",
        question_text="answer once",
    )
    ready_dir = tmp_path / "reply-ready"
    ready_dir.mkdir()
    go = tmp_path / "reply-go"
    child_code = r'''
import os
import time
from pathlib import Path
import goalflight_steer_mailbox as steer

(Path(os.environ["TEST_READY_DIR"]) / os.environ["TEST_SLOT"]).write_text(
    "ready", encoding="utf-8"
)
go = Path(os.environ["TEST_GO_FILE"])
while not go.exists():
    time.sleep(0.001)
try:
    steer.append_worker_wait_reply(
        Path(os.environ["TEST_STEER_FILE"]),
        dispatch_id="one-reply",
        wait_id=os.environ["TEST_WAIT_ID"],
        text="controller answer " + os.environ["TEST_SLOT"],
    )
except ValueError:
    print("REFUSED", flush=True)
else:
    print("REPLIED", flush=True)
'''
    children: list[subprocess.Popen] = []
    for slot in ("a", "b"):
        env = _env(tmp_path)
        env.update(
            {
                "TEST_READY_DIR": str(ready_dir),
                "TEST_SLOT": slot,
                "TEST_GO_FILE": str(go),
                "TEST_STEER_FILE": str(mailbox),
                "TEST_WAIT_ID": str(arm["question_id"]),
            }
        )
        children.append(
            subprocess.Popen(
                [sys.executable, "-c", child_code],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    deadline = time.monotonic() + 2
    while len(list(ready_dir.iterdir())) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(list(ready_dir.iterdir())) == 2, "repliers did not reach the barrier"
    go.write_text("go", encoding="utf-8")
    outputs = [child.communicate(timeout=5) for child in children]
    assert sorted(stdout.strip() for stdout, _stderr in outputs) == ["REFUSED", "REPLIED"], outputs
    replies = [
        entry
        for entry in steer.read_steer_entries(mailbox)
        if entry.get("kind") == steer.WORKER_WAIT_REPLY_KIND
    ]
    assert len(replies) == 1, replies
    assert replies[0]["reply_to"] == arm["question_id"]


def test_reply_is_typed_correlated_explicit_and_single_consumer(tmp_path: Path) -> None:
    mailbox = tmp_path / "typed-reply.steer.jsonl"
    arm = steer.append_worker_wait_started(
        mailbox,
        dispatch_id="typed-reply",
        timeout_secs=2,
        question_kind="USER-CONFIRM",
        question_text="approve?",
    )
    wait_id = str(arm["question_id"])
    steer.append_steer_entry(
        mailbox,
        "unrelated controller note",
        dispatch_id="typed-reply",
    )
    state = steer.active_worker_wait(
        steer.read_steer_entries(mailbox),
        dispatch_id="typed-reply",
    )
    assert state is not None and state["phase"] == "awaiting_reply"

    with pytest.raises(ValueError, match="no worker wait"):
        steer.append_worker_wait_reply(
            mailbox,
            dispatch_id="typed-reply",
            wait_id="foreign-wait-id",
            text="yes",
            decision="yes",
        )
    with pytest.raises(ValueError, match="requires decision=yes or decision=no"):
        steer.append_worker_wait_reply(
            mailbox,
            dispatch_id="typed-reply",
            wait_id=wait_id,
            text="ambiguous approval",
        )

    reply = steer.append_worker_wait_reply(
        mailbox,
        dispatch_id="typed-reply",
        wait_id=wait_id,
        text="approved",
        decision="yes",
    )
    state = steer.active_worker_wait(
        steer.read_steer_entries(mailbox),
        dispatch_id="typed-reply",
    )
    assert state is not None and state["phase"] == "reply_pending"
    assert state["reply_seq"] == reply["seq"]
    assert state["reply_decision"] == "yes"
    assert reply not in steer.pending_worker_entries(
        steer.read_steer_entries(mailbox),
        set(),
    )
    with pytest.raises(ValueError, match="already has a reply"):
        steer.append_worker_wait_reply(
            mailbox,
            dispatch_id="typed-reply",
            wait_id=wait_id,
            text="second consumer",
            decision="no",
        )


def test_tracked_worker_arm_without_exact_marker_does_not_suspend_idle(
    tmp_path: Path,
) -> None:
    """An arm from the tracked process is inert until its exact marker is observed."""
    dispatch_id = "markerless-worker-arm"
    env = _env(tmp_path)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    mailbox.parent.mkdir(parents=True, exist_ok=True)
    _prepare_question_outbox(env, dispatch_id)
    tail = tmp_path / "markerless.tail"
    tail.write_text("!STATUS: about to ask\n", encoding="utf-8")
    status = tmp_path / "markerless.status.json"
    worker_code = r'''
import os
import time
from pathlib import Path
import goalflight_steer_mailbox as steer

steer.append_worker_wait_started(
    Path(os.environ["TEST_STEER_FILE"]),
    dispatch_id=os.environ["TEST_DISPATCH_ID"],
        timeout_secs=30,
    question_kind="USER-NEED",
    question_text="marker was never emitted",
)
time.sleep(60)
'''
    env.update({"TEST_DISPATCH_ID": dispatch_id, "TEST_STEER_FILE": str(mailbox)})
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_code],
        env=env,
        start_new_session=True,
    )
    watcher = subprocess.Popen(
        _watcher_command(
            dispatch_id=dispatch_id,
            worker_pid=worker.pid,
            tail=tail,
            status=status,
            max_idle_secs=0.2,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        watcher_out, watcher_err = watcher.communicate(timeout=10)
        assert watcher.returncode == 2, watcher_out + watcher_err
        assert worker.poll() is None, "markerless worker exited before idle timeout"
        final = json.loads(status.read_text(encoding="utf-8"))
        assert final["state"] == "idle_timeout", final
        assert final.get("worker_wait") is None, final
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)


def test_live_foreign_process_group_cannot_arm_tracked_worker(tmp_path: Path) -> None:
    """Even an exact tail marker cannot bind a foreign helper's live arm."""
    dispatch_id = "foreign-helper-arm"
    env = _env(tmp_path)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    mailbox.parent.mkdir(parents=True, exist_ok=True)
    _prepare_question_outbox(env, dispatch_id)
    tail = tmp_path / "foreign-helper.tail"
    status = tmp_path / "foreign-helper.status.json"
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(8)"],
        env=env,
        start_new_session=True,
    )
    helper_code = r'''
import os
import time
from pathlib import Path
import goalflight_steer_mailbox as steer

arm = steer.append_worker_wait_started(
    Path(os.environ["TEST_STEER_FILE"]),
    dispatch_id=os.environ["TEST_DISPATCH_ID"],
    timeout_secs=6,
    question_kind="USER-NEED",
    question_text="foreign helper question",
)
Path(os.environ["TEST_TAIL_FILE"]).write_text(
    "!USER-NEED: " + steer.worker_wait_question_marker_text(
        os.environ["TEST_DISPATCH_ID"],
        "foreign helper question",
        str(arm["question_id"]),
        question_kind="USER-NEED",
    ) + "\n",
    encoding="utf-8",
)
time.sleep(8)
'''
    helper_env = dict(env)
    helper_env.update(
        {
            "TEST_DISPATCH_ID": dispatch_id,
            "TEST_STEER_FILE": str(mailbox),
            "TEST_TAIL_FILE": str(tail),
        }
    )
    helper = subprocess.Popen(
        [sys.executable, "-c", helper_code],
        env=helper_env,
        start_new_session=True,
    )
    deadline = time.monotonic() + 2
    while not tail.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert tail.exists(), "foreign helper did not arm and emit its marker"
    watcher = subprocess.Popen(
        _watcher_command(
            dispatch_id=dispatch_id,
            worker_pid=worker.pid,
            tail=tail,
            status=status,
            # Above WORKER_WAIT_ARM_GRACE_SECS so a foreign USER-NEED would
            # terminalize as blocked unless process-identity filtering holds.
            max_idle_secs=1.2,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    started = time.monotonic()
    try:
        watcher_out, watcher_err = watcher.communicate(timeout=8)
        elapsed = time.monotonic() - started
        assert watcher.returncode == 2, watcher_out + watcher_err
        assert elapsed < 8, f"foreign helper suspended idle for {elapsed:.3f}s"
        final = json.loads(status.read_text(encoding="utf-8"))
        assert final["state"] == "idle_timeout", final
        assert final.get("reason") != "marker:USER-NEED", final
        assert final.get("worker_wait") is None, final
    finally:
        for proc in (helper, worker):
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)


def test_foreign_wait_owns_question_marker_uses_waiter_not_dispatch_id() -> None:
    """A USER-NEED is bound to a dispatch by its waiter's process, not the id text."""
    dispatch_id = "attr-dispatch"
    wait_id = "wait-foreign"
    marker_text = steer.worker_wait_question_marker_text(
        dispatch_id,
        "foreign helper question",
        wait_id,
        question_kind="USER-NEED",
    )
    marker = {"kind": "USER-NEED", "text": marker_text, "line": 1}
    foreign_wait = {
        "wait_id": wait_id,
        "question_kind": "USER-NEED",
        "question_marker_text": marker_text,
    }
    assert watch._foreign_wait_owns_question_marker(
        marker, foreign_wait, None, dispatch_id
    )
    assert not watch._foreign_wait_owns_question_marker(
        marker, foreign_wait, foreign_wait, dispatch_id
    )
    ceremony_free = {
        "kind": "USER-NEED",
        "text": f"{dispatch_id} — controller decision required",
        "line": 2,
    }
    assert not watch._foreign_wait_owns_question_marker(
        ceremony_free, None, None, dispatch_id
    )
    assert not watch._foreign_wait_owns_question_marker(
        ceremony_free, foreign_wait, None, dispatch_id
    )


def test_tracked_worker_user_need_still_blocks_dispatch(tmp_path: Path) -> None:
    """A ceremony-free USER-NEED from the tracked worker still gates the dispatch.

    The worker exits on its own; the watcher must preserve the terminal need
    without reaping a live worker before the 60-second cleanup grace.
    """
    dispatch_id = "tracked-user-need-blocks"
    env = _env(tmp_path)
    tail = tmp_path / "tracked-need.tail"
    status = tmp_path / "tracked-need.status.json"
    worker_code = r'''
import os
import time

dispatch_id = os.environ["TEST_DISPATCH_ID"]
print(f"!USER-NEED: {dispatch_id} — controller decision required", flush=True)
time.sleep(0.5)
'''
    env["TEST_DISPATCH_ID"] = dispatch_id
    with tail.open("w", encoding="utf-8") as worker_stdout:
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            stdout=worker_stdout,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            text=True,
        )
    watcher = subprocess.Popen(
        _watcher_command(
            dispatch_id=dispatch_id,
            worker_pid=worker.pid,
            tail=tail,
            status=status,
            max_idle_secs=5,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        reaper = threading.Thread(target=worker.wait, kwargs={"timeout": 5}, daemon=True)
        reaper.start()
        watcher_out, watcher_err = watcher.communicate(timeout=5)
        assert watcher.returncode == 4, watcher_out + watcher_err
        reaper.join(timeout=5)
        assert not reaper.is_alive(), "watcher did not terminate the lingering worker"
        final = json.loads(status.read_text(encoding="utf-8"))
        assert final["state"] == "blocked", final
        assert final.get("reason") == "marker:USER-NEED", final
        assert final.get("state") != "idle_timeout", final
        assert not final.get("worker_termination_signals"), final
        assert final.get("worker_termination_confirmed") is not True, final
        assert worker.poll() is not None
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)


def test_worker_wait_longer_than_idle_then_reply_completes(tmp_path: Path) -> None:
    """Acceptance: the reply wait outlives max_idle_secs without idle_timeout."""
    dispatch_id = "worker-wait-acceptance"
    env = _env(tmp_path)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    mailbox.parent.mkdir(parents=True, exist_ok=True)
    _prepare_question_outbox(env, dispatch_id)
    tail = tmp_path / "worker.tail"
    status = tmp_path / "worker.status.json"
    worker_code = r'''
import os
import subprocess
import sys

helper_code = """
import json
import os
from pathlib import Path
import goalflight_steer_mailbox as steer

dispatch_id = os.environ["TEST_DISPATCH_ID"]

def report(event):
    if event["state"] == "armed":
        print(f"!{event['question_kind']}: {event['question_marker_text']}", flush=True)
        print(f"STEER-WAIT: dispatch_id={dispatch_id} armed", flush=True)
    elif event["state"] == "messages":
        for entry in event["entries"]:
            for line in steer.worker_wait_reply_output_lines(entry):
                print(line, flush=True)

result = steer.wait_for_worker_entries(
    Path(os.environ["TEST_STEER_FILE"]),
    dispatch_id=dispatch_id,
    acked_seqs=set(),
    question_kind="USER-NEED",
    question_text="controller decision required",
    timeout_secs=3.0,
    poll_secs=0.02,
    notify=report,
)
if result["state"] != "messages":
    raise SystemExit(1)
print(f"!STEER-ACK: {result['entries'][0]['seq']}", flush=True)
"""
helper = subprocess.run([sys.executable, "-c", helper_code], env=os.environ.copy())
if helper.returncode != 0:
    raise SystemExit(helper.returncode)
dispatch_id = os.environ["TEST_DISPATCH_ID"]
print(f"!COMPLETE: {dispatch_id} — resumed after controller reply", flush=True)
'''
    env.update(
        {
            "TEST_DISPATCH_ID": dispatch_id,
            "TEST_STEER_FILE": str(mailbox),
        }
    )
    with tail.open("w", encoding="utf-8") as worker_stdout:
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            stdout=worker_stdout,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            text=True,
        )
    watcher = subprocess.Popen(
        _watcher_command(
            dispatch_id=dispatch_id,
            worker_pid=worker.pid,
            tail=tail,
            status=status,
            max_idle_secs=0.2,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        armed = _wait_for_status(
            status,
            lambda payload: payload.get("state") == "awaiting_steer_reply",
        )
        assert armed["liveness_state"] == "intentionally_blocked"

        # Deliberately exceed the ordinary idle budget before answering.
        time.sleep(0.55)
        during_wait = json.loads(status.read_text(encoding="utf-8"))
        assert watcher.poll() is None
        assert worker.poll() is None
        assert during_wait["state"] == "awaiting_steer_reply"
        assert during_wait.get("reason") != "idle_timeout"

        steer.append_steer_entry(
            mailbox,
            "unrelated controller note",
            dispatch_id=dispatch_id,
        )
        time.sleep(0.15)
        still_waiting = json.loads(status.read_text(encoding="utf-8"))
        assert worker.poll() is None
        assert still_waiting["state"] == "awaiting_steer_reply", still_waiting
        steer.append_worker_wait_reply(
            mailbox,
            dispatch_id=dispatch_id,
            wait_id=armed["worker_wait"]["wait_id"],
            text="approved; continue",
        )
        worker_rc = worker.wait(timeout=5)
        watcher_out, watcher_err = watcher.communicate(timeout=5)
        assert worker_rc == 0, tail.read_text(encoding="utf-8")
        assert watcher.returncode == 0, watcher_out + watcher_err
        final = json.loads(status.read_text(encoding="utf-8"))
        assert final["state"] == "complete", final
        assert final.get("reason") != "idle_timeout", final
        assert "STEER-REPLY:" in tail.read_text(encoding="utf-8")

        mailbox_entries = steer.read_steer_entries(mailbox)
        typed_reply = next(
            entry
            for entry in mailbox_entries
            if entry.get("kind") == steer.WORKER_WAIT_REPLY_KIND
        )
        mailbox_entries = _wait_for_wait_end(
            mailbox,
            wait_id=str(armed["worker_wait"]["wait_id"]),
            reply_seq=int(typed_reply["seq"]),
        )
        ended = next(
            entry
            for entry in mailbox_entries
            if entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
        )
        assert ended["context"]["reply_seq"] == typed_reply["seq"]

        envelopes = [
            json.loads(line)
            for line in (tmp_path / "messages" / f"{dispatch_id}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        question = next(item for item in envelopes if item["type"] == "user_need")
        assert question["payload"]["awaiting_reply"] is True
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)
        if watcher.poll() is None:
            watcher.terminate()
            watcher.wait(timeout=5)


def test_reply_pending_survives_consumption_delay_and_transient_lock_failure(
    tmp_path: Path,
) -> None:
    dispatch_id = "reply-pending-delay"
    env = _env(tmp_path)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    mailbox.parent.mkdir(parents=True, exist_ok=True)
    _prepare_question_outbox(env, dispatch_id)
    tail = tmp_path / "reply-pending.tail"
    status = tmp_path / "reply-pending.status.json"
    reply_consumed = tmp_path / "reply-consumed"
    release_worker = tmp_path / "release-reply-worker"
    release_lock = tmp_path / "release-reply-lock"
    ready = tmp_path / "reply-lock-ready"
    contention_read = tmp_path / "watcher-read-under-lock"
    worker_code = r'''
import os
import subprocess
import sys
import time
from pathlib import Path
import goalflight_steer_mailbox as steer

dispatch_id = os.environ["TEST_DISPATCH_ID"]

def report(event):
    if event["state"] == "armed":
        print(f"!{event['question_kind']}: {event['question_marker_text']}", flush=True)
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(0.5); print('unrelated sibling output', flush=True)",
            ]
        )
    elif event["state"] == "messages":
        # Hold the waiter until the test has observed a watcher poll while the
        # mailbox lock is owned by another process.
        Path(os.environ["TEST_REPLY_CONSUMED_FILE"]).write_text("ready", encoding="utf-8")
        release = Path(os.environ["TEST_RELEASE_WORKER_FILE"])
        while not release.exists():
            time.sleep(0.01)

result = steer.wait_for_worker_entries(
    Path(os.environ["TEST_STEER_FILE"]),
    dispatch_id=dispatch_id,
    acked_seqs=set(),
    question_kind="USER-NEED",
    question_text="reply may be pending",
    timeout_secs=5,
    poll_secs=0.02,
    notify=report,
)
if result["state"] != "messages":
    print(f"!FAILED: {dispatch_id} — reply wait expired", flush=True)
    raise SystemExit(1)
print("reply consumed", flush=True)
print(f"!COMPLETE: {dispatch_id} — survived pending reply", flush=True)
'''
    env.update(
        {
            "TEST_DISPATCH_ID": dispatch_id,
            "TEST_STEER_FILE": str(mailbox),
            "TEST_REPLY_CONSUMED_FILE": str(reply_consumed),
            "TEST_RELEASE_WORKER_FILE": str(release_worker),
            "TEST_READY_FILE": str(ready),
            "TEST_CONTENTION_READ_FILE": str(contention_read),
        }
    )
    with tail.open("w", encoding="utf-8") as worker_stdout:
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            stdout=worker_stdout,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            text=True,
        )
    watcher_command = _watcher_command(
        dispatch_id=dispatch_id,
        worker_pid=worker.pid,
        tail=tail,
        status=status,
        max_idle_secs=0.2,
    )
    watcher_probe = r'''
import os
import runpy
import sys
from pathlib import Path
import goalflight_steer_mailbox as steer

script, *args = sys.argv[1:]
mailbox = Path(os.environ["TEST_STEER_FILE"])
ready = Path(os.environ["TEST_READY_FILE"])
observed = Path(os.environ["TEST_CONTENTION_READ_FILE"])
read_entries = steer.read_steer_entries

def observed_read(path, *args, **kwargs):
    if Path(path) == mailbox and ready.exists():
        observed.touch()
    return read_entries(path, *args, **kwargs)

steer.read_steer_entries = observed_read
sys.argv = [script, *args]
runpy.run_path(script, run_name="__main__")
'''
    watcher = subprocess.Popen(
        [sys.executable, "-c", watcher_probe, *watcher_command[1:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    lock_holder: subprocess.Popen | None = None
    try:
        armed = _wait_for_status(
            status,
            lambda payload: payload.get("state") == "awaiting_steer_reply",
        )
        wait_id = armed["worker_wait"]["wait_id"]
        steer.append_worker_wait_reply(
            mailbox,
            dispatch_id=dispatch_id,
            wait_id=wait_id,
            text="continue",
        )
        _wait_for_status(
            status,
            lambda payload: (payload.get("worker_wait") or {}).get("phase")
            == "reply_pending",
        )

        holder_code = r'''
import os
import time
from pathlib import Path
import goalflight_messages as messages

with messages.mail_lock(Path(os.environ["TEST_STEER_FILE"])):
    Path(os.environ["TEST_READY_FILE"]).write_text("ready", encoding="utf-8")
    release = Path(os.environ["TEST_RELEASE_LOCK_FILE"])
    while not release.exists():
        time.sleep(0.01)
'''
        holder_env = dict(env)
        holder_env["TEST_READY_FILE"] = str(ready)
        holder_env["TEST_RELEASE_LOCK_FILE"] = str(release_lock)
        lock_holder = subprocess.Popen([sys.executable, "-c", holder_code], env=holder_env)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "mailbox lock holder did not become ready"
        deadline = time.monotonic() + 5
        while (
            (not contention_read.exists() or not reply_consumed.exists())
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert contention_read.exists(), "watcher did not read while the mailbox lock was held"
        assert reply_consumed.exists(), "worker did not reach the reply-consumed pause"
        deadline = time.monotonic() + 5
        while (
            "unrelated sibling output" not in tail.read_text(encoding="utf-8")
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        during_contention = json.loads(status.read_text(encoding="utf-8"))
        assert watcher.poll() is None, during_contention
        assert worker.poll() is None, during_contention
        assert during_contention["state"] == "awaiting_steer_reply", during_contention
        assert during_contention["worker_wait"]["phase"] == "reply_pending"
        assert "unrelated sibling output" in tail.read_text(encoding="utf-8")

        release_lock.touch()
        lock_holder.wait(timeout=5)
        release_worker.touch()

        worker_rc = worker.wait(timeout=5)
        watcher_out, watcher_err = watcher.communicate(timeout=5)
        assert worker_rc == 0, tail.read_text(encoding="utf-8")
        assert watcher.returncode == 0, watcher_out + watcher_err
        final = json.loads(status.read_text(encoding="utf-8"))
        assert final["state"] == "complete", final
    finally:
        if lock_holder is not None and lock_holder.poll() is None:
            lock_holder.terminate()
            lock_holder.wait(timeout=5)
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)
        if watcher.poll() is None:
            watcher.terminate()
            watcher.wait(timeout=5)


def test_watcher_arms_outbox_before_controller_reply_reaches_worker(
    tmp_path: Path,
) -> None:
    dispatch_id = "reply-before-first-scan"
    env = _env(tmp_path)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    mailbox.parent.mkdir(parents=True, exist_ok=True)
    _prepare_question_outbox(env, dispatch_id)
    tail = tmp_path / "reply-before-scan.tail"
    status = tmp_path / "reply-before-scan.status.json"
    reply_seen = tmp_path / "reply-before-scan.seen"
    release_reply = tmp_path / "reply-before-scan.release"
    worker_code = r'''
import json
import os
import time
from pathlib import Path
import goalflight_steer_mailbox as steer

dispatch_id = os.environ["TEST_DISPATCH_ID"]

def report(event):
    if event["state"] == "armed":
        print(f"!{event['question_kind']}: {event['question_marker_text']}", flush=True)
    elif event["state"] == "messages":
        Path(os.environ["TEST_REPLY_SEEN"]).write_text("seen", encoding="utf-8")
        while not Path(os.environ["TEST_RELEASE_REPLY"]).exists():
            time.sleep(0.01)
        for entry in event["entries"]:
            for line in steer.worker_wait_reply_output_lines(entry):
                print(line, flush=True)

result = steer.wait_for_worker_entries(
    Path(os.environ["TEST_STEER_FILE"]),
    dispatch_id=dispatch_id,
    acked_seqs=set(),
    question_kind="USER-NEED",
    question_text="reply predates watcher",
    timeout_secs=4,
    poll_secs=0.02,
    notify=report,
)
if result["state"] != "messages":
    raise SystemExit(1)
print(f"!COMPLETE: {dispatch_id} — pending reply consumed", flush=True)
'''
    env.update(
        {
            "TEST_DISPATCH_ID": dispatch_id,
            "TEST_STEER_FILE": str(mailbox),
            "TEST_REPLY_SEEN": str(reply_seen),
            "TEST_RELEASE_REPLY": str(release_reply),
        }
    )
    with tail.open("w", encoding="utf-8") as worker_stdout:
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            stdout=worker_stdout,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            text=True,
        )
    watcher = subprocess.Popen(
        _watcher_command(
            dispatch_id=dispatch_id,
            worker_pid=worker.pid,
            tail=tail,
            status=status,
            max_idle_secs=0.2,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        armed = _wait_for_status(
            status,
            lambda payload: payload.get("state") == "awaiting_steer_reply",
        )
        steer.append_worker_wait_reply(
            mailbox,
            dispatch_id=dispatch_id,
            wait_id=armed["worker_wait"]["wait_id"],
            text="continue",
        )
        deadline = time.monotonic() + 2
        while not reply_seen.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reply_seen.exists(), tail.read_text(encoding="utf-8")
        pending = _wait_for_status(
            status,
            lambda payload: (payload.get("worker_wait") or {}).get("phase")
            == "reply_pending",
        )
        assert pending["state"] == "awaiting_steer_reply", pending
        time.sleep(0.55)
        assert watcher.poll() is None, json.loads(status.read_text(encoding="utf-8"))
        assert worker.poll() is None
        release_reply.write_text("release", encoding="utf-8")

        worker_rc = worker.wait(timeout=5)
        watcher_out, watcher_err = watcher.communicate(timeout=5)
        assert worker_rc == 0, tail.read_text(encoding="utf-8")
        assert watcher.returncode == 0, watcher_out + watcher_err
        final = json.loads(status.read_text(encoding="utf-8"))
        assert final["state"] == "complete", final
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)
        if watcher.poll() is None:
            watcher.terminate()
            watcher.wait(timeout=5)


def test_long_reply_output_does_not_reacquire_wait_after_consumption(
    tmp_path: Path,
) -> None:
    dispatch_id = "reply-output-tombstone"
    env = _env(tmp_path)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    mailbox.parent.mkdir(parents=True, exist_ok=True)
    _prepare_question_outbox(env, dispatch_id)
    tail = tmp_path / "reply-output.tail"
    status = tmp_path / "reply-output.status.json"
    worker_code = r'''
import os
import time
from pathlib import Path
import goalflight_steer_mailbox as steer

dispatch_id = os.environ["TEST_DISPATCH_ID"]

def report(event):
    if event["state"] == "armed":
        print(f"!{event['question_kind']}: {event['question_marker_text']}", flush=True)
    elif event["state"] == "messages":
        for entry in event["entries"]:
            for line in steer.worker_wait_reply_output_lines(entry):
                print(line, flush=True)

result = steer.wait_for_worker_entries(
    Path(os.environ["TEST_STEER_FILE"]),
    dispatch_id=dispatch_id,
    acked_seqs=set(),
    question_kind="USER-NEED",
    question_text="end write may fail",
    timeout_secs=4,
    poll_secs=0.02,
    notify=report,
)
if result["state"] != "messages":
    raise SystemExit(1)
print("reply consumed; continuing nonterminal work", flush=True)
time.sleep(5)
'''
    env.update({"TEST_DISPATCH_ID": dispatch_id, "TEST_STEER_FILE": str(mailbox)})
    with tail.open("w", encoding="utf-8") as worker_stdout:
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            stdout=worker_stdout,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            text=True,
        )
    watcher = subprocess.Popen(
        _watcher_command(
            dispatch_id=dispatch_id,
            worker_pid=worker.pid,
            tail=tail,
            status=status,
            max_idle_secs=0.2,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        armed = _wait_for_status(
            status,
            lambda payload: payload.get("state") == "awaiting_steer_reply",
        )
        steer.append_worker_wait_reply(
            mailbox,
            dispatch_id=dispatch_id,
            wait_id=armed["worker_wait"]["wait_id"],
            text="x" * 2000,
        )
        started = time.monotonic()
        watcher_out, watcher_err = watcher.communicate(timeout=3)
        elapsed = time.monotonic() - started
        assert watcher.returncode == 2, watcher_out + watcher_err
        assert elapsed < 2.5, f"disproved wait reacquired suspension for {elapsed:.3f}s"
        final = json.loads(status.read_text(encoding="utf-8"))
        assert final["state"] == "idle_timeout", final
        assert final["reason"] == "idle_timeout", final
        assert final.get("worker_wait") is None, final
        tail_text = tail.read_text(encoding="utf-8")
        assert "reply consumed" in tail_text
        receipt_line = next(
            line for line in tail_text.splitlines() if line.startswith("STEER-REPLY: ")
        )
        assert len(receipt_line) < 1000
        assert "x" * 2000 not in receipt_line
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)
        if watcher.poll() is None:
            watcher.terminate()
            watcher.wait(timeout=5)


def test_settled_reply_allows_second_wait(
    tmp_path: Path,
) -> None:
    dispatch_id = "settled-retry"
    mailbox = tmp_path / "settled-retry.steer.jsonl"
    tail = tmp_path / "settled-retry.tail"

    def report(event: dict) -> None:
        if event["state"] == "armed":
            arm = event["arm"]
            steer.append_worker_wait_reply(
                mailbox,
                dispatch_id=dispatch_id,
                wait_id=str(arm["question_id"]),
                text=f"answer for {arm['question_id']}",
            )
        elif event["state"] == "messages":
            with tail.open("a", encoding="utf-8") as stream:
                for entry in event["entries"]:
                    for line in steer.worker_wait_reply_output_lines(entry):
                        print(line, file=stream, flush=True)

    first = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="first question",
        timeout_secs=1.0,
        poll_secs=0.05,
        notify=report,
    )
    markers, _tail_size = watch.extract_markers(tail)
    receipts = steer.consumed_worker_wait_receipts(
        {"stdout_path": str(tail)},
        marker_entries=markers,
    )
    second = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        consumed_reply_receipts=receipts,
        question_kind="USER-NEED",
        question_text="second question",
        timeout_secs=1.0,
        poll_secs=0.05,
        notify=report,
    )

    assert first["state"] == second["state"] == "messages"
    entries = steer.read_steer_entries(mailbox)
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries
    ) == 2
    assert sum(entry.get("kind") == steer.WORKER_WAIT_REPLY_KIND for entry in entries) == 2
    assert sum(entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND for entry in entries) == 2


def test_report_failure_does_not_record_reply_as_consumed(
    tmp_path: Path,
) -> None:
    """Delivery evidence must follow successful reply output, never precede it."""
    dispatch_id = "report-failure"
    mailbox = tmp_path / "report-failure.steer.jsonl"
    posted_reply: list[dict] = []

    def broken_report(event: dict) -> None:
        if event["state"] == "armed":
            posted_reply.append(
                steer.append_worker_wait_reply(
                    mailbox,
                    dispatch_id=dispatch_id,
                    wait_id=str(event["arm"]["question_id"]),
                    text="must be redelivered",
                )
            )
        elif event["state"] == "messages":
            raise BrokenPipeError("simulated worker output failure")

    with pytest.raises(BrokenPipeError, match="worker output failure"):
        _wait_with_watcher(
            mailbox,
            dispatch_id=dispatch_id,
            acked_seqs=set(),
            question_kind="USER-NEED",
            question_text="first delivery attempt",
            timeout_secs=1.0,
            poll_secs=0.05,
            notify=broken_report,
        )

    entries = steer.read_steer_entries(mailbox)
    assert not any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND for entry in entries
    ), entries
    outbox = steer.goalflight_dispatch_paths.worker_question_outbox_dir(
        dispatch_id,
        dispatch_dir=mailbox.parent,
    )
    request = json.loads(
        (outbox / f"{posted_reply[0]['reply_to']}.json").read_text(encoding="utf-8")
    )
    assert steer.read_worker_question_settlement(outbox, request) is None

    recovered = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="first delivery attempt",
        resume_wait_id=posted_reply[0]["reply_to"],
        timeout_secs=1.0,
        poll_secs=0.05,
    )

    assert recovered["entries"] == posted_reply
    assert recovered["recovered_admitted_reply"] is True


def test_consumed_reply_allows_new_question(
    tmp_path: Path,
) -> None:
    """The watcher end row allows a later wait without redelivering."""
    dispatch_id = "cleanup-miss-redelivery"
    mailbox = tmp_path / "cleanup-miss-redelivery.steer.jsonl"

    def report(event: dict) -> None:
        if event["state"] == "armed":
            steer.append_worker_wait_reply(
                mailbox,
                dispatch_id=dispatch_id,
                wait_id=str(event["arm"]["question_id"]),
                text="deliver at least once",
            )

    first = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="first attempt",
        timeout_secs=1.0,
        poll_secs=0.05,
        notify=report,
    )
    second = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="new question after settled reply",
        timeout_secs=1.0,
        poll_secs=0.05,
        notify=report,
    )

    assert first["state"] == second["state"] == "messages"
    assert second["entries"] != first["entries"]
    assert second["wait_id"] != first["wait_id"]
    assert second["recovered_admitted_reply"] is False
    entries = steer.read_steer_entries(mailbox)
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries
    ) == 2, entries
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND for entry in entries
    ) == 2, entries


def test_pending_prior_reply_is_recovered_without_rearming(
    tmp_path: Path,
) -> None:
    """A pending prior reply is recovered before the worker asks again."""
    dispatch_id = "reply-before-rearm"
    mailbox = tmp_path / "reply-before-rearm.steer.jsonl"
    prior = steer.append_worker_wait_started(
        mailbox,
        dispatch_id=dispatch_id,
        timeout_secs=2.0,
        question_kind="USER-CONFIRM",
        question_text="approve the original action?",
    )
    posted_reply = steer.append_worker_wait_reply(
        mailbox,
        dispatch_id=dispatch_id,
        wait_id=str(prior["question_id"]),
        text="approved once",
        decision="yes",
    )
    outbox = steer.goalflight_dispatch_paths.worker_question_outbox_dir(
        dispatch_id,
        dispatch_dir=mailbox.parent,
    )
    outbox.mkdir(parents=True, exist_ok=True)
    steer.write_worker_question_request(
        outbox,
        dispatch_id=dispatch_id,
        attempt_id="unknown",
        wait_id=str(prior["question_id"]),
        question_kind="USER-CONFIRM",
        question="approve the original action?",
        timeout_s=2.0,
    )
    result = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-CONFIRM",
        question_text="approve the original action?",
        resume_wait_id=str(prior["question_id"]),
        timeout_secs=1.0,
        poll_secs=0.05,
    )

    assert result["entries"] == [posted_reply]
    assert result["wait_id"] == prior["question_id"]
    assert result["recovered_admitted_reply"] is True
    entries = steer.read_steer_entries(mailbox)
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries
    ) == 1, entries


def test_genuinely_wedged_worker_without_question_still_idle_times_out(
    tmp_path: Path,
) -> None:
    dispatch_id = "genuine-wedge"
    env = _env(tmp_path)
    tail = tmp_path / "wedged.tail"
    tail.write_text("!STATUS: working, then wedged\n", encoding="utf-8")
    status = tmp_path / "wedged.status.json"
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        env=env,
        start_new_session=True,
    )
    watcher = subprocess.Popen(
        _watcher_command(
            dispatch_id=dispatch_id,
            worker_pid=worker.pid,
            tail=tail,
            status=status,
            max_idle_secs=0.2,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        watcher_out, watcher_err = watcher.communicate(timeout=5)
        assert watcher.returncode == 2, watcher_out + watcher_err
        final = json.loads(status.read_text(encoding="utf-8"))
        assert final["state"] == "idle_timeout", final
        assert final["reason"] == "idle_timeout", final
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)


def test_generic_backlog_rows_do_not_wear_the_reply_receipt_label() -> None:
    """Only a typed correlated reply may print STEER-REPLY.

    A streaming worker reads these lines while its wait is still live; a
    generic backlog row prefixed STEER-REPLY reads as a confirmation.
    """
    generic = {
        "kind": steer.STEERING_KIND,
        "seq": 3,
        "text": "unrelated controller note",
    }
    generic_lines = steer.worker_wait_reply_output_lines(generic)
    assert not any(line.startswith("STEER-REPLY:") for line in generic_lines), generic_lines
    assert generic_lines[0].startswith("STEER-BACKLOG: "), generic_lines
    backlog_identity = json.loads(generic_lines[0][len("STEER-BACKLOG: "):])
    assert backlog_identity == {"kind": steer.STEERING_KIND, "seq": 3}
    assert generic_lines[1].startswith("STEER-MESSAGE: "), generic_lines
    assert "unrelated controller note" in generic_lines[1]
    # A backlog receipt line must never validate as a consumption receipt.
    assert steer._worker_wait_receipt_identity(generic_lines[0][len("STEER-BACKLOG: "):]) is None

    reply = {
        "kind": steer.WORKER_WAIT_REPLY_KIND,
        "reply_to": "wait-9",
        "seq": 7,
        "decision": "yes",
        "text": "approved",
    }
    reply_lines = steer.worker_wait_reply_output_lines(reply)
    assert reply_lines[0].startswith("STEER-MESSAGE: "), reply_lines
    assert reply_lines[1].startswith("STEER-REPLY: "), reply_lines
    receipt = json.loads(reply_lines[1][len("STEER-REPLY: "):])
    assert receipt == {
        "kind": steer.WORKER_WAIT_REPLY_KIND,
        "reply_to": "wait-9",
        "seq": 7,
    }
    assert steer._worker_wait_receipt_identity(reply_lines[1][len("STEER-REPLY: "):]) == (
        "wait-9",
        7,
    )






def test_next_wait_recovers_reply_from_writer_admitted_before_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply admitted before the deadline survives a slow writer fsync."""
    mailbox = tmp_path / "admitted-writer-race.steer.jsonl"
    dispatch_id = "admitted-writer-race"
    timeout_secs = 1.0
    stall_secs = 2.0
    stall_started = threading.Event()
    stall_state = {"used": False}
    writer_future = None
    wait_started_at = time.monotonic()
    stall_started_at = [0.0]

    real_append_fsync = messages._append_fsync

    def stalled_append_fsync(path, data):
        if b"worker_wait_reply" in data and not stall_state["used"]:
            stall_state["used"] = True
            stall_started_at[0] = time.monotonic()
            stall_started.set()
            time.sleep(stall_secs)
        real_append_fsync(path, data)

    monkeypatch.setattr(messages, "_append_fsync", stalled_append_fsync)

    def report(event: dict) -> None:
        nonlocal writer_future
        if event["state"] != "armed":
            return
        writer_future = pool.submit(
            steer.append_worker_wait_reply,
            mailbox,
            dispatch_id=dispatch_id,
            wait_id=str(event["arm"]["question_id"]),
            text="admitted before the deadline",
        )
        assert stall_started.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = _wait_with_watcher(
            mailbox,
            dispatch_id=dispatch_id,
            acked_seqs=set(),
            question_kind="USER-NEED",
            question_text="need a boundary answer",
            timeout_secs=timeout_secs,
            poll_secs=0.05,
            notify=report,
        )
        assert stall_started_at[0] - wait_started_at < timeout_secs
        assert writer_future is not None
        writer_future.result(timeout=10)

    assert stall_state["used"], "the writer never reached its fsync stall"
    assert first["state"] == "deadline", first

    second = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need a boundary answer",
        resume_wait_id=str(first["wait_id"]),
        timeout_secs=1.0,
        poll_secs=0.05,
    )
    durable_reply = next(
        entry
        for entry in steer.read_steer_entries(mailbox)
        if entry.get("kind") == steer.WORKER_WAIT_REPLY_KIND
    )

    assert second["state"] == "messages", second
    assert second["entries"] == [durable_reply]
    entries = steer.read_steer_entries(mailbox)
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries
    ) == 1, entries


@pytest.mark.parametrize("failure_kind", ["oserror", "messageerror"])
def test_wait_tolerates_transient_carrier_read_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A transient carrier read failure after the reply is durable is retried
    within the same wait; a deadline would leave the row recoverable later."""
    mailbox = tmp_path / f"transient-{failure_kind}.steer.jsonl"
    dispatch_id = f"transient-{failure_kind}"
    armed = threading.Event()
    failures_left = {"count": 2}

    real_read = steer.read_steer_entries_read_only

    def flaky_read(*args, **kwargs):
        if armed.is_set() and failures_left["count"] > 0:
            failures_left["count"] -= 1
            if failure_kind == "oserror":
                raise OSError("simulated transient carrier read failure")
            raise messages.MessageError("simulated transient carrier read failure")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(steer, "read_steer_entries_read_only", flaky_read)

    def report(event: dict) -> None:
        if event["state"] == "armed":
            steer.append_worker_wait_reply(
                mailbox,
                dispatch_id=dispatch_id,
                wait_id=str(event["arm"]["question_id"]),
                text="durable before the read hiccup",
            )
            armed.set()

    result = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need an answer across a read hiccup",
        timeout_secs=1.0,
        poll_secs=0.05,
        notify=report,
    )

    assert failures_left["count"] == 0, "the injected failures never fired"
    assert result["state"] == "messages", result
    assert result["entries"][0]["text"] == "durable before the read hiccup"
    reply = result["entries"][0]
    entries = _wait_for_wait_end(
        mailbox,
        wait_id=str(result["wait_id"]),
        reply_seq=int(reply["seq"]),
    )
    assert any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND for entry in entries
    ), entries
    capsys.readouterr()


@pytest.mark.parametrize("failure_kind", ["oserror", "messageerror"])
def test_next_wait_recovers_durable_reply_after_errors_through_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Carrier errors through the deadline must not strand a durable reply."""
    mailbox = tmp_path / f"durable-unreadable-{failure_kind}.steer.jsonl"
    dispatch_id = f"durable-unreadable-{failure_kind}"
    unreadable = threading.Event()
    posted_reply: list[dict] = []

    real_read = steer.read_steer_entries_read_only

    def failing_read(*args, **kwargs):
        if unreadable.is_set():
            if failure_kind == "oserror":
                raise OSError("carrier temporarily unreadable")
            raise messages.MessageError("carrier identity changed")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(steer, "read_steer_entries_read_only", failing_read)

    def report(event: dict) -> None:
        if event["state"] == "armed":
            posted_reply.append(
                steer.append_worker_wait_reply(
                    mailbox,
                    dispatch_id=dispatch_id,
                    wait_id=str(event["arm"]["question_id"]),
                    text="durable before carrier errors",
                )
            )
            unreadable.set()

    started = time.monotonic()
    first = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need an answer",
        timeout_secs=0.3,
        poll_secs=0.05,
        notify=report,
    )
    elapsed = time.monotonic() - started

    assert posted_reply, "reply was not durable before carrier errors began"
    assert first["state"] == "deadline", first
    assert elapsed < 5.0, f"unbounded unreadable wait: {elapsed:.3f}s"
    unreadable.clear()
    recovered = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need an answer",
        resume_wait_id=str(first["wait_id"]),
        timeout_secs=0.3,
        poll_secs=0.05,
    )
    assert recovered["state"] == "messages", recovered
    assert recovered["entries"] == posted_reply
    assert recovered["recovered_admitted_reply"] is True
    capsys.readouterr()


def test_watcher_settlement_survives_tail_aging(
    tmp_path: Path,
) -> None:
    """The watcher end row survives when a STEER-REPLY marker has aged out."""
    dispatch_id = "receipt-aging"
    mailbox = tmp_path / "receipt-aging.steer.jsonl"

    def report(event: dict) -> None:
        if event["state"] == "armed":
            steer.append_worker_wait_reply(
                mailbox,
                dispatch_id=dispatch_id,
                wait_id=str(event["arm"]["question_id"]),
                text=f"answer for {event['arm']['question_id']}",
            )
        # The messages event deliberately writes nothing to any tail: the
        # watched-tail receipt is treated as aged out of both discovery views.

    first = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="first question",
        timeout_secs=1.0,
        poll_secs=0.05,
        notify=report,
    )

    assert first["state"] == "messages", first
    expected = {(first["wait_id"], first["entries"][0]["seq"])}

    # The tail no longer carries reply output; the outbox settlement remains
    # the durable evidence.
    stale_tail = tmp_path / "stale.tail"
    stale_tail.write_text("STATUS: unrelated output\n", encoding="utf-8")
    markers, _size = watch.extract_markers(stale_tail)
    receipts = steer.consumed_worker_wait_receipts(
        {"stdout_path": str(stale_tail)},
        marker_entries=markers,
    )
    assert receipts == set(), receipts

    second = _wait_with_watcher(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        consumed_reply_receipts=receipts,
        question_kind="USER-NEED",
        question_text="second question",
        timeout_secs=1.0,
        poll_secs=0.05,
        notify=report,
    )

    assert second["state"] == "messages", second
    entries = steer.read_steer_entries(mailbox)
    assert sum(entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries) == 2, entries
    assert sum(entry.get("kind") == steer.WORKER_WAIT_REPLY_KIND for entry in entries) == 2, entries
    assert sum(entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND for entry in entries) == 2, entries
