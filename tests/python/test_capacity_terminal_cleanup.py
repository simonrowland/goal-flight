"""Terminal capacity cleanup and bounded waits, using isolated machine state."""
import argparse
import datetime as dt
import io
import json
import os
import signal
import sys
from contextlib import redirect_stdout
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


def test_journal_terminal_releases_once(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: False)
    monkeypatch.setattr(cap, "_process_group_liveness", lambda _pgid: False)
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


def test_terminal_live_worker_holds_then_releases(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: True)
    monkeypatch.setattr(cap, "_pid_generation_matches", lambda _pid, _lease: True)
    record = seed(tmp_path)
    data = cap.load_state()
    data["leases"]["held"].update(
        worker_pid=record["worker_pid"], worker_identity=record["worker_identity"]
    )
    cap.save_state(data)
    authority = journal.open_or_create_journal(tmp_path)
    attempt = authority.prepare_attempt(record["dispatch_id"]).value
    authority.commit_terminal(attempt.attempt_id, terminal_state="complete")
    assert cap.load_state()["leases"]["held"]["state"] == "active"
    cap.cmd_release_stale(argparse.Namespace(state="released", reason="stale", keep=True))
    assert cap.load_state()["leases"]["held"]["state"] == "active"
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: False)
    authority.commit_terminal(attempt.attempt_id, terminal_state="complete")
    assert cap.load_state()["leases"]["held"]["state"] == "complete"


def test_release_stale_uses_terminal_ledger_after_failed_attach(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: False)
    monkeypatch.setattr(cap, "_process_group_liveness", lambda _pgid: False)
    seed(tmp_path, attached=False)
    cap.cmd_release_stale(argparse.Namespace(state="released", reason="stale", keep=True))
    assert cap.load_state()["leases"]["held"]["state"] == "released"


def test_terminal_authority_survives_capacity_cleanup_error(tmp_path, monkeypatch):
    record = seed(tmp_path)
    def fail_cleanup(*_args):
        raise RuntimeError("capacity unavailable")

    monkeypatch.setattr(cap, "release_terminal_dispatch", fail_cleanup)
    authority = journal.open_or_create_journal(tmp_path)
    attempt = authority.prepare_attempt(record["dispatch_id"]).value
    result = authority.commit_terminal(attempt.attempt_id, terminal_state="complete")
    assert result.committed
    assert authority.attempt_for_dispatch(record["dispatch_id"]).lifecycle_state == journal.ATTEMPT_TERMINAL


def test_contention_reclaims_dead_worker_before_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: False)
    monkeypatch.setattr(cap, "current_rate_pressure", lambda _args=None: {"providers_under_pressure": []})
    dead = {
        "lease_id": "dead-holder",
        "state": "active",
        "agent": "codex",
        "worker_pid": 4242,
        "worker_identity": {"pid": 4242, "start_token": "gone"},
        "controller_pid": os.getpid(),
        "mem_mb": 386,
        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
    }
    cap.save_state({"leases": {dead["lease_id"]: dead}, "cooldowns": {}})
    args = argparse.Namespace(
        agent="codex", dispatch_id="new-dispatch", prompt_id=None,
        project_root=str(tmp_path), worker_cwd=None, worktree_path=None,
        controller_pid=None, worker_pid=None, lease_id="new-holder",
        mem_mb=386, agent_cap=18, priority="normal", ttl_s=3600,
        max_total=1, ram_mb=65536, reserve_mb=0, worst_worker_mb=1200,
        hard_cap=40, rate_pressure_window_s=None, rate_pressure_threshold=None,
    )
    output = io.StringIO()
    with redirect_stdout(output):
        rc = cap.cmd_acquire(args)
    assert rc == 0
    assert json.loads(output.getvalue())["decision"] == "allow"
    leases = cap.load_state()["leases"]
    assert leases[dead["lease_id"]]["state"] == "expired"
    assert leases["new-holder"]["state"] == "active"


def test_unknown_worker_identity_is_never_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: None)
    monkeypatch.setattr(cap, "_pid_generation_matches", lambda _pid, _lease: None)
    lease = {
        "lease_id": "unknown-holder", "state": "active", "agent": "codex",
        "worker_pid": 4242, "worker_identity": {"pid": 4242, "start_token": "unknown"},
        "controller_pid": os.getpid(), "mem_mb": 386,
        "expires_at": cap.iso(cap.utc_now() - dt.timedelta(hours=1)),
    }
    data = {"leases": {lease["lease_id"]: lease}, "cooldowns": {}}
    assert cap.reclaim_stale_leases(data) == []
    assert data["leases"][lease["lease_id"]]["state"] == "active"


def test_status_reclaims_dead_worker_in_view_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: False)
    monkeypatch.setattr(cap, "current_rate_pressure", lambda _args=None: {"providers_under_pressure": []})
    lease = {
        "lease_id": "status-dead-holder", "state": "active", "agent": "codex",
        "worker_pid": 4242, "worker_identity": {"pid": 4242, "start_token": "gone"},
        "controller_pid": os.getpid(), "mem_mb": 386,
        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
    }
    cap.save_state({"leases": {lease["lease_id"]: lease}, "cooldowns": {}})
    before = cap.state_path().read_text()
    output = io.StringIO()
    with redirect_stdout(output):
        rc = cap.cmd_status(argparse.Namespace(
            json=True, ram_mb=65536, reserve_mb=0, worst_worker_mb=1200,
            hard_cap=40, max_total=None, rate_pressure_window_s=None,
            rate_pressure_threshold=None,
        ))
    assert rc == 0
    assert json.loads(output.getvalue())["active"] == []
    assert cap.state_path().read_text() == before


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
