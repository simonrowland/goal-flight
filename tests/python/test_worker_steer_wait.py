#!/usr/bin/env python3
"""Blocking worker steer waits and watcher idle-accounting regressions."""

from __future__ import annotations

import contextlib
import json
import os
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


def _watcher_command(
    *,
    dispatch_id: str,
    worker_pid: int,
    tail: Path,
    status: Path,
    max_idle_secs: float,
) -> list[str]:
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
        "--poll-secs",
        "0.05",
        "--max-idle-secs",
        str(max_idle_secs),
        "--stay-after-terminal",
    ]


def test_wait_reports_arm_time_backlog_without_arming(tmp_path: Path) -> None:
    mailbox = tmp_path / "backlog.steer.jsonl"
    steer.append_steer_entry(mailbox, "reply already waiting", dispatch_id="backlog")
    events: list[dict] = []

    started = time.monotonic()
    result = steer.wait_for_worker_entries(
        mailbox,
        dispatch_id="backlog",
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need a decision",
        timeout_secs=1.0,
        poll_secs=0.05,
        notify=events.append,
    )

    assert time.monotonic() - started < 0.25
    assert result["state"] == "messages"
    assert result["arm_time_backlog"] is True
    assert result["entries"][0]["text"] == "reply already waiting"
    assert [event["state"] for event in events] == ["messages"]
    assert not any(
        entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND
        for entry in steer.read_steer_entries(mailbox)
    )


def test_wait_returns_promptly_at_its_independent_deadline(tmp_path: Path) -> None:
    mailbox = tmp_path / "deadline.steer.jsonl"
    started = time.monotonic()
    result = steer.wait_for_worker_entries(
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
    ]
    assert steer.active_worker_wait(entries, dispatch_id="deadline") is None


def test_wait_consumes_reply_written_during_final_sleep(
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
    result = steer.wait_for_worker_entries(
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
    assert result["state"] == "messages"
    assert result["entries"][0]["text"] == "accepted before the deadline"
    assert any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
        for entry in steer.read_steer_entries(mailbox)
    )


def test_wait_deadline_includes_mailbox_lock_acquisition(tmp_path: Path) -> None:
    mailbox = tmp_path / "locked.steer.jsonl"
    ready = tmp_path / "lock-ready"
    holder_code = r'''
import os
import time
from pathlib import Path
import goalflight_messages as messages

with messages.mail_lock(Path(os.environ["TEST_STEER_FILE"])):
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
    holder = subprocess.Popen([sys.executable, "-c", holder_code], env=env)
    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "lock holder did not become ready"

        started = time.monotonic()
        result = steer.wait_for_worker_entries(
            mailbox,
            dispatch_id="locked",
            acked_seqs=set(),
            question_kind="USER-NEED",
            question_text="need a decision",
            timeout_secs=0.2,
            poll_secs=0.05,
        )
        elapsed = time.monotonic() - started

        assert result["state"] == "deadline"
        assert 0.15 <= elapsed < 0.6
    finally:
        holder.terminate()
        holder.wait(timeout=5)


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

    entries = steer.read_steer_entries(mailbox)
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


def test_expired_wait_cannot_renew_suspension_indefinitely(tmp_path: Path) -> None:
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
    ) is None

    with pytest.raises(ValueError, match="renewal refused"):
        steer.append_worker_wait_started(
            mailbox,
            dispatch_id="nonrenewable",
            timeout_secs=1,
            question_kind="USER-NEED",
            question_text="renewed question",
        )


def test_two_concurrent_waiters_create_exactly_one_arm(tmp_path: Path) -> None:
    mailbox = tmp_path / "one-active-wait.steer.jsonl"
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    go = tmp_path / "go"
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
    print("REFUSED", flush=True)
else:
    print("ARMED", flush=True)
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
    outputs = [child.communicate(timeout=5) for child in children]
    assert sorted(stdout.strip() for stdout, _stderr in outputs) == ["ARMED", "REFUSED"], outputs
    starts = [
        entry
        for entry in steer.read_steer_entries(mailbox)
        if entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND
    ]
    assert len(starts) == 1, starts


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
    mailbox.parent.mkdir(parents=True)
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
    timeout_secs=3,
    question_kind="USER-NEED",
    question_text="marker was never emitted",
)
time.sleep(5)
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
    started = time.monotonic()
    try:
        watcher_out, watcher_err = watcher.communicate(timeout=5)
        elapsed = time.monotonic() - started
        assert watcher.returncode == 2, watcher_out + watcher_err
        assert elapsed < 1.2, f"markerless arm suspended idle for {elapsed:.3f}s"
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
    mailbox.parent.mkdir(parents=True)
    tail = tmp_path / "foreign-helper.tail"
    status = tmp_path / "foreign-helper.status.json"
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
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
    timeout_secs=3,
    question_kind="USER-NEED",
    question_text="foreign helper question",
)
Path(os.environ["TEST_TAIL_FILE"]).write_text(
    "!USER-NEED: " + steer.worker_wait_question_marker_text(
        os.environ["TEST_DISPATCH_ID"],
        "foreign helper question",
        str(arm["question_id"]),
    ) + "\n",
    encoding="utf-8",
)
time.sleep(5)
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
            max_idle_secs=0.2,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    started = time.monotonic()
    try:
        watcher_out, watcher_err = watcher.communicate(timeout=5)
        elapsed = time.monotonic() - started
        assert watcher.returncode == 2, watcher_out + watcher_err
        assert elapsed < 1.2, f"foreign helper suspended idle for {elapsed:.3f}s"
        final = json.loads(status.read_text(encoding="utf-8"))
        assert final["state"] == "idle_timeout", final
    finally:
        for proc in (helper, worker):
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)


def test_worker_wait_longer_than_idle_then_reply_completes(tmp_path: Path) -> None:
    """Acceptance: the reply wait outlives max_idle_secs without idle_timeout."""
    dispatch_id = "worker-wait-acceptance"
    env = _env(tmp_path)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    mailbox.parent.mkdir(parents=True)
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
    mailbox.parent.mkdir(parents=True)
    tail = tmp_path / "reply-pending.tail"
    status = tmp_path / "reply-pending.status.json"
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
        # Model a descheduled waiter after the durable reply exists but before
        # it records worker_wait_ended or emits any disproving output.
        time.sleep(2.0)

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

        ready = tmp_path / "reply-lock-ready"
        holder_code = r'''
import os
import time
from pathlib import Path
import goalflight_messages as messages

with messages.mail_lock(Path(os.environ["TEST_STEER_FILE"])):
    Path(os.environ["TEST_READY_FILE"]).write_text("ready", encoding="utf-8")
    time.sleep(1.3)
'''
        holder_env = dict(env)
        holder_env["TEST_READY_FILE"] = str(ready)
        lock_holder = subprocess.Popen([sys.executable, "-c", holder_code], env=holder_env)
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "mailbox lock holder did not become ready"

        # Exceed both ordinary idle and the old fixed terminal grace while the
        # watcher cannot reread the arm. Cached reply_pending must survive.
        time.sleep(1.1)
        during_contention = json.loads(status.read_text(encoding="utf-8"))
        assert watcher.poll() is None, during_contention
        assert worker.poll() is None, during_contention
        assert during_contention["state"] == "awaiting_steer_reply", during_contention
        assert during_contention["worker_wait"]["phase"] == "reply_pending"
        assert "unrelated sibling output" in tail.read_text(encoding="utf-8")

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


def test_reply_before_watcher_first_scan_remains_pending_until_consumed(
    tmp_path: Path,
) -> None:
    dispatch_id = "reply-before-first-scan"
    env = _env(tmp_path)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    mailbox.parent.mkdir(parents=True)
    tail = tmp_path / "reply-before-scan.tail"
    status = tmp_path / "reply-before-scan.status.json"
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
        time.sleep(1.5)
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
    deadline = time.monotonic() + 2
    arm: dict | None = None
    while time.monotonic() < deadline:
        entries = steer.read_steer_entries(mailbox)
        arm = next(
            (
                entry
                for entry in entries
                if entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND
            ),
            None,
        )
        if arm is not None and tail.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.01)
    assert arm is not None, "worker did not arm before watcher startup"
    steer.append_worker_wait_reply(
        mailbox,
        dispatch_id=dispatch_id,
        wait_id=str(arm["question_id"]),
        text="continue",
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
        pending = _wait_for_status(
            status,
            lambda payload: (payload.get("worker_wait") or {}).get("phase")
            == "reply_pending",
        )
        assert pending["state"] == "awaiting_steer_reply", pending
        time.sleep(0.55)
        assert watcher.poll() is None, json.loads(status.read_text(encoding="utf-8"))
        assert worker.poll() is None

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


def test_post_reply_output_tombstones_wait_when_end_row_write_fails(
    tmp_path: Path,
) -> None:
    dispatch_id = "reply-output-tombstone"
    env = _env(tmp_path)
    mailbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.steer.jsonl"
    mailbox.parent.mkdir(parents=True)
    tail = tmp_path / "reply-output.tail"
    status = tmp_path / "reply-output.status.json"
    worker_code = r'''
import json
import os
import time
from pathlib import Path
import goalflight_steer_mailbox as steer

dispatch_id = os.environ["TEST_DISPATCH_ID"]

def missed_end_write(*_args, **_kwargs):
    raise TimeoutError("forced cleanup lock miss")

steer.append_worker_wait_ended = missed_end_write

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
        assert elapsed < 1.5, f"disproved wait reacquired suspension for {elapsed:.3f}s"
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


def test_consumed_receipt_allows_second_wait_when_end_row_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_id = "cleanup-retry"
    mailbox = tmp_path / "cleanup-retry.steer.jsonl"
    tail = tmp_path / "cleanup-retry.tail"

    def missed_end_write(*_args, **_kwargs):
        raise TimeoutError("forced cleanup lock miss")

    monkeypatch.setattr(steer, "append_worker_wait_ended", missed_end_write)

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

    first = steer.wait_for_worker_entries(
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
    second = steer.wait_for_worker_entries(
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
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_REPLY_KIND for entry in entries
    ) == 2
    assert not any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND for entry in entries
    )


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
    assert reply_lines[0].startswith("STEER-REPLY: "), reply_lines
    receipt = json.loads(reply_lines[0][len("STEER-REPLY: "):])
    assert receipt == {
        "kind": steer.WORKER_WAIT_REPLY_KIND,
        "reply_to": "wait-9",
        "seq": 7,
    }
    assert steer._worker_wait_receipt_identity(reply_lines[0][len("STEER-REPLY: "):]) == (
        "wait-9",
        7,
    )
    assert reply_lines[1].startswith("STEER-MESSAGE: "), reply_lines


def test_wait_final_read_outlasts_admitted_writers_fsync_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply admitted before the deadline must survive its own slow fsync.

    The writer validates inside the mailbox lock before the deadline, then
    stalls in append/fsync well past one poll interval. The waiter's final
    read must wait out that critical section instead of reporting deadline
    for a reply that was admitted.
    """
    mailbox = tmp_path / "final-read-race.steer.jsonl"
    dispatch_id = "final-read-race"
    timeout_secs = 1.0
    stall_secs = 0.8  # >3x the pre-fix final-read budget of one poll interval
    clock = [100.0]
    stall_started = threading.Event()
    stall_state = {"used": False}
    writers: list[threading.Thread] = []

    monkeypatch.setattr(steer, "active_monotonic", lambda: clock[0])

    real_append_fsync = messages._append_fsync

    def stalled_append_fsync(path, data):
        if b"worker_wait_reply" in data and not stall_state["used"]:
            stall_state["used"] = True
            # The writer is admitted: it passed the in-lock deadline check
            # above. The deadline passes while its fsync stalls.
            clock[0] += timeout_secs + 1.0
            stall_started.set()
            time.sleep(stall_secs)
        real_append_fsync(path, data)

    monkeypatch.setattr(messages, "_append_fsync", stalled_append_fsync)

    def report(event: dict) -> None:
        if event["state"] != "armed":
            return
        writer = threading.Thread(
            target=lambda: steer.append_worker_wait_reply(
                mailbox,
                dispatch_id=dispatch_id,
                wait_id=str(event["arm"]["question_id"]),
                text="admitted before the deadline",
            ),
        )
        writer.start()
        writers.append(writer)
        # Hold the waiter inside notify until the admitted writer is stalled
        # in its fsync, so the waiter's next loop iteration is the final read.
        assert stall_started.wait(timeout=10)

    result = steer.wait_for_worker_entries(
        mailbox,
        dispatch_id=dispatch_id,
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need a boundary answer",
        timeout_secs=timeout_secs,
        poll_secs=0.05,
        notify=report,
    )
    for writer in writers:
        writer.join(timeout=10)

    assert stall_state["used"], "the writer never reached its fsync stall"
    assert result["state"] == "messages", result
    assert result["entries"][0]["text"] == "admitted before the deadline"
    # The reply landed after the deadline, so the best-effort end row has no
    # lock budget left; the durable receipt sidecar carries the consumption
    # evidence instead.
    reply_seq = result["entries"][0]["seq"]
    receipts = steer.consumed_worker_wait_receipts({}, mailbox_path=mailbox)
    assert receipts == {(result["wait_id"], reply_seq)}


@pytest.mark.parametrize("failure_kind", ["oserror", "messageerror"])
def test_wait_tolerates_transient_carrier_read_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A transient carrier read failure after the reply is durable must not
    fail the wait: escaping emits no receipt and no end row, which refuses
    every later wait."""
    mailbox = tmp_path / f"transient-{failure_kind}.steer.jsonl"
    dispatch_id = f"transient-{failure_kind}"
    armed = threading.Event()
    failures_left = {"count": 2}

    real_read = steer.read_steer_entries

    def flaky_read(*args, **kwargs):
        if armed.is_set() and failures_left["count"] > 0:
            failures_left["count"] -= 1
            if failure_kind == "oserror":
                raise OSError("simulated transient carrier read failure")
            raise messages.MessageError("simulated transient carrier read failure")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(steer, "read_steer_entries", flaky_read)

    def report(event: dict) -> None:
        if event["state"] == "armed":
            steer.append_worker_wait_reply(
                mailbox,
                dispatch_id=dispatch_id,
                wait_id=str(event["arm"]["question_id"]),
                text="durable before the read hiccup",
            )
            armed.set()

    result = steer.wait_for_worker_entries(
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
    entries = steer.read_steer_entries(mailbox)
    assert any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND for entry in entries
    ), entries
    err = capsys.readouterr().err
    assert err.count("WARNING: steer mailbox read failed") == 1, err


def test_wait_reports_deadline_when_carrier_stays_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A persistent carrier read failure ends at the bounded deadline with an
    observable warning, not an escaping exception that fails the CLI."""
    mailbox = tmp_path / "unreadable.steer.jsonl"
    armed = threading.Event()

    real_read = steer.read_steer_entries

    def unreadable(*args, **kwargs):
        if armed.is_set():
            raise messages.MessageError("carrier identity changed")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(steer, "read_steer_entries", unreadable)

    def report(event: dict) -> None:
        if event["state"] == "armed":
            armed.set()

    started = time.monotonic()
    result = steer.wait_for_worker_entries(
        mailbox,
        dispatch_id="unreadable",
        acked_seqs=set(),
        question_kind="USER-NEED",
        question_text="need an answer",
        timeout_secs=0.3,
        poll_secs=0.05,
        notify=report,
    )
    elapsed = time.monotonic() - started

    assert result["state"] == "deadline", result
    assert elapsed < 5.0, f"unbounded unreadable wait: {elapsed:.3f}s"
    err = capsys.readouterr().err
    assert err.count("WARNING: steer mailbox read failed") == 1, err


def test_consumed_receipt_survives_tail_aging_via_mailbox_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receipt sidecar re-discovers a consumed reply after the watched
    tail and the status marker window have aged the STEER-REPLY line out."""
    dispatch_id = "receipt-aging"
    mailbox = tmp_path / "receipt-aging.steer.jsonl"

    def missed_end_write(*_args, **_kwargs):
        raise TimeoutError("forced cleanup lock miss")

    monkeypatch.setattr(steer, "append_worker_wait_ended", missed_end_write)

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

    first = steer.wait_for_worker_entries(
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

    # Neither discovery view carries the receipt: the status marker window is
    # empty and the stdout rescan sees only unrelated output. Only the
    # append-only sidecar still proves consumption.
    stale_tail = tmp_path / "stale.tail"
    stale_tail.write_text("STATUS: unrelated output\n", encoding="utf-8")
    markers, _size = watch.extract_markers(stale_tail)
    receipts = steer.consumed_worker_wait_receipts(
        {"stdout_path": str(stale_tail)},
        marker_entries=markers,
        mailbox_path=mailbox,
    )
    assert receipts == expected, receipts

    second = steer.wait_for_worker_entries(
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
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND for entry in entries
    ) == 2, entries
    assert sum(
        entry.get("kind") == steer.WORKER_WAIT_REPLY_KIND for entry in entries
    ) == 2, entries
    assert not any(
        entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND for entry in entries
    ), entries

    sidecar_lines = [
        line
        for line in steer.worker_wait_receipts_path(mailbox)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    identities = {
        steer._worker_wait_receipt_identity(line) for line in sidecar_lines
    }
    assert identities == {
        next(iter(expected)),
        (second["wait_id"], second["entries"][0]["seq"]),
    }, sidecar_lines
