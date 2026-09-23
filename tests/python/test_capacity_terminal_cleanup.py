"""Terminal capacity cleanup and bounded waits, using isolated machine state."""
import argparse
import datetime as dt
import json
import os
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import goalflight_capacity as cap
import goalflight_journal as journal
import goalflight_ledger as ledger


def seed(tmp_path, *, attached=True):
    record = {
        "dispatch_id": "terminal-worker", "project_root": str(tmp_path),
        "state": "complete", "terminal_state": "complete",
        "worker_pid": 99999999,
        "worker_identity": {"pid": 99999999, "start_token": "old"},
        "detached_reason": "bash_background_default",
    }
    ledger.write_record(record)
    lease = {
        "lease_id": "held", "dispatch_id": record["dispatch_id"],
        "state": "active", "agent": "codex", "controller_pid": os.getpid(),
        "claimant_pid": os.getpid(), "project_root": str(tmp_path),
        "started_at": cap.iso(),
        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
    }
    if attached:
        lease.update(worker_pid=record["worker_pid"], worker_identity=record["worker_identity"])
    cap.save_state({"leases": {"held": lease}, "cooldowns": {}})
    return record


def test_journal_terminal_releases_once(tmp_path):
    record = seed(tmp_path)
    authority = journal.open_or_create_journal(tmp_path)
    attempt = authority.prepare_attempt(record["dispatch_id"]).value
    first = authority.commit_terminal(attempt.attempt_id, terminal_state="complete")
    assert first.committed
    released = cap.load_state()["leases"]["held"]
    assert released["state"] == "complete"
    second = authority.commit_terminal(attempt.attempt_id, terminal_state="complete")
    assert second.value.idempotent
    assert cap.load_state()["leases"]["held"] == released


def test_terminal_live_worker_holds_then_releases(tmp_path):
    record = seed(tmp_path)
    record.update(worker_pid=os.getpid(), worker_identity=cap._process_start_identity(os.getpid()))
    ledger.write_record(record)
    data = cap.load_state()
    data["leases"]["held"].update(worker_pid=os.getpid(), worker_identity=record["worker_identity"])
    cap.save_state(data)
    authority = journal.open_or_create_journal(tmp_path)
    attempt = authority.prepare_attempt(record["dispatch_id"]).value
    authority.commit_terminal(attempt.attempt_id, terminal_state="complete")
    assert cap.load_state()["leases"]["held"]["state"] == "active"
    cap.cmd_release_stale(argparse.Namespace(state="released", reason="stale", keep=True))
    assert cap.load_state()["leases"]["held"]["state"] == "active"
    seed(tmp_path)
    authority.commit_terminal(attempt.attempt_id, terminal_state="complete")
    assert cap.load_state()["leases"]["held"]["state"] == "complete"


def test_release_stale_uses_terminal_ledger_after_failed_attach(tmp_path):
    seed(tmp_path, attached=False)
    cap.cmd_release_stale(argparse.Namespace(state="released", reason="stale", keep=True))
    assert cap.load_state()["leases"]["held"]["state"] == "released"


def test_wait_exhaustion_is_truthful():
    clock = [0.0]
    def acquire(_args):
        print(json.dumps({"decision": "wait", "reason": "agent_worker_cap"}))
        return 2
    def sleep(seconds):
        clock[0] += seconds
    result = cap.acquire_with_wait(
        argparse.Namespace(), lane="normal", wait_s=2, poll_s=1, jitter=0,
        monotonic_fn=lambda: clock[0], sleep_fn=sleep, acquire_func=acquire,
    )
    assert result["reason"] == "budget_exhausted"
    assert clock[0] == 2


def test_real_sigint_interrupts():
    def acquire(_args):
        print(json.dumps({"decision": "wait", "reason": "agent_worker_cap"}))
        return 2
    with pytest.raises(cap.CapacityWaitInterrupted) as caught:
        cap.acquire_with_wait(
            argparse.Namespace(), lane="normal", wait_s=30, acquire_func=acquire,
            install_signal_handlers=True, sleep_fn=lambda _: signal.raise_signal(signal.SIGINT),
        )
    assert caught.value.signum == signal.SIGINT
    assert caught.value.exit_code == 130
