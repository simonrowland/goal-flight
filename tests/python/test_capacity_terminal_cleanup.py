"""Terminal capacity cleanup and bounded waits, using isolated machine state."""
import argparse
import datetime as dt
import io
import json
import os
import shlex
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


def test_current_reserved_lease_needs_explicit_no_spawn_proof(tmp_path):
    record = {
        "dispatch_id": "reserved-terminal",
        "project_root": str(tmp_path),
        "state": "waiting_capacity",
    }
    ledger.write_record(record)
    lease = {
        "lease_id": "reserved-terminal-lease",
        "dispatch_id": record["dispatch_id"],
        "state": "active",
        "agent": "codex",
        "machine_id": cap.machine_id(),
        "lease_schema": cap.LEASE_SCHEMA,
        "launch_state": "reserved",
        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
    }
    cap.save_state({"leases": {lease["lease_id"]: lease}, "cooldowns": {}})
    authority = journal.open_or_create_journal(tmp_path)
    attempt = authority.prepare_attempt(record["dispatch_id"]).value
    output = io.StringIO()
    with redirect_stdout(output):
        rc = ledger.cmd_finish(argparse.Namespace(
            dispatch_id=record["dispatch_id"],
            state="failed",
            reason="pre-spawn failure",
            terminal_state=None,
            elapsed_s=None,
            worker_still_alive=False,
            headline=None,
        ))
    assert rc == 0
    assert cap.load_state()["leases"][lease["lease_id"]]["state"] == "error"


def test_current_spawning_lease_stays_protected_without_worker_identity(tmp_path):
    record = {
        "dispatch_id": "spawning-terminal",
        "project_root": str(tmp_path),
        "state": "failed",
        "terminal_state": "failed",
        "worker_still_alive": False,
    }
    ledger.write_record(record)
    lease = {
        "lease_id": "spawning-terminal-lease",
        "dispatch_id": record["dispatch_id"],
        "state": "active",
        "agent": "codex",
        "machine_id": cap.machine_id(),
        "lease_schema": cap.LEASE_SCHEMA,
        "launch_state": "spawning",
        "reservation_deadline_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
    }
    cap.save_state({"leases": {lease["lease_id"]: lease}, "cooldowns": {}})
    cap.cmd_release_stale(argparse.Namespace(state="released", reason="stale", keep=True))
    assert cap.load_state()["leases"][lease["lease_id"]]["state"] == "active"


def test_spawn_failure_transitions_and_releases_spawning_lease(tmp_path):
    record = {
        "dispatch_id": "spawn-failed",
        "project_root": str(tmp_path),
        "state": "failed",
        "terminal_state": "error",
        "worker_still_alive": False,
    }
    ledger.write_record(record)
    lease = {
        "lease_id": "spawn-failed-lease",
        "dispatch_id": record["dispatch_id"],
        "state": "active",
        "agent": "codex",
        "machine_id": cap.machine_id(),
        "lease_schema": cap.LEASE_SCHEMA,
        "launch_state": "spawning",
        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
    }
    cap.save_state({"leases": {lease["lease_id"]: lease}, "cooldowns": {}})
    assert cap.mark_lease_spawn_failed(lease["lease_id"])
    assert cap.load_state()["leases"][lease["lease_id"]]["launch_state"] == "spawn_failed"
    cap.release_terminal_dispatch(record["dispatch_id"], "error")
    assert cap.load_state()["leases"][lease["lease_id"]]["state"] == "error"
    assert not cap.mark_lease_spawn_failed(lease["lease_id"])


def test_expired_spawning_lease_uses_terminal_ledger_proof(tmp_path):
    record = {
        "dispatch_id": "expired-spawning",
        "project_root": str(tmp_path),
        "state": "failed",
        "terminal_state": "error",
        "worker_still_alive": False,
    }
    ledger.write_record(record)
    lease = {
        "lease_id": "expired-spawning-lease",
        "dispatch_id": record["dispatch_id"],
        "state": "active",
        "agent": "codex",
        "machine_id": cap.machine_id(),
        "lease_schema": cap.LEASE_SCHEMA,
        "launch_state": "spawning",
        "reservation_deadline_at": cap.iso(cap.utc_now() - dt.timedelta(seconds=1)),
        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
    }
    cap.save_state({"leases": {lease["lease_id"]: lease}, "cooldowns": {}})
    assert cap.reclaim_stale_leases({"leases": {lease["lease_id"]: lease}, "cooldowns": {}}) == [
        lease["lease_id"]
    ]


def test_reconcile_terminal_projection_retries_capacity_cleanup(tmp_path, monkeypatch):
    dispatch_id = "reconcile-terminal-lease"
    project_root = Path.cwd()
    status_path = tmp_path / "reconcile.status.json"
    status_path.write_text(
        json.dumps({
            "dispatch_id": dispatch_id,
            "state": "complete",
            "worker_alive": False,
        })
    )
    record = {
        "dispatch_id": dispatch_id,
        "project_root": str(project_root),
        "state": "running",
        "terminal_state": "unknown",
        "status_path": str(status_path),
        "worker_still_alive": False,
    }
    ledger.write_record(record)
    lease = {
        "lease_id": "reconcile-lease",
        "dispatch_id": dispatch_id,
        "state": "active",
        "agent": "codex",
        "machine_id": cap.machine_id(),
        "lease_schema": cap.LEASE_SCHEMA,
        "launch_state": "reserved",
        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
    }
    cap.save_state({"leases": {lease["lease_id"]: lease}, "cooldowns": {}})
    authority = journal.open_or_create_journal(project_root)
    attempt = authority.prepare_attempt(dispatch_id).value
    monkeypatch.setattr(ledger, "worker_identity_liveness", lambda _record: ("dead", "pid_absent"))
    assert authority.commit_terminal(
        attempt.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "worker_still_alive": False},
    ).committed
    assert cap.load_state()["leases"][lease["lease_id"]]["state"] == "active"
    summary = ledger.reconcile_terminal_outbox(project_root)
    assert cap.load_state()["leases"][lease["lease_id"]]["state"] == "complete", summary


def test_unattached_lease_is_not_reclaimed_from_claimant_death(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: False)
    lease = {
        "lease_id": "spawn-handoff",
        "dispatch_id": "spawn-handoff-dispatch",
        "state": "active",
        "agent": "codex",
        "claimant_pid": 4242,
        "controller_pid": 4343,
        "machine_id": cap.machine_id(),
        "lease_schema": cap.LEASE_SCHEMA,
        "started_at": cap.iso(cap.utc_now() - dt.timedelta(hours=2)),
        "expires_at": cap.iso(cap.utc_now() - dt.timedelta(hours=1)),
    }
    data = {"machine_id": cap.machine_id(), "leases": {lease["lease_id"]: lease}, "cooldowns": {}}
    assert cap.reclaim_stale_leases(data) == []
    cap.prune_state(data)
    assert data["leases"][lease["lease_id"]]["state"] == "active"


def test_legacy_unattached_lease_expiry_requires_manual_identity_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: False)
    lease = {
        # v1.7.0 wrote no per-lease machine_id or lease_schema.
        "lease_id": "legacy-spawn-handoff",
        "state": "active",
        "agent": "codex",
        "dispatch_id": "legacy-unattached",
        "claimant_pid": 4242,
        "controller_pid": 4343,
        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
    }
    data = {"machine_id": cap.machine_id(), "leases": {lease["lease_id"]: lease}, "cooldowns": {}}
    assert cap.reclaim_stale_leases(data) == []
    lease["expires_at"] = cap.iso(cap.utc_now() - dt.timedelta(seconds=1))
    assert cap.reclaim_stale_leases(data) == []
    cap.prune_state(data)
    assert data["leases"][lease["lease_id"]]["state"] == "active"
    cap.save_state(data)
    output = io.StringIO()
    with redirect_stdout(output):
        assert cap.cmd_status(argparse.Namespace(
            json=True, ram_mb=65536, reserve_mb=0, worst_worker_mb=1200,
            hard_cap=40, max_total=None, rate_pressure_window_s=None,
            rate_pressure_threshold=None,
        )) == 0
    status = json.loads(output.getvalue())
    manual = status["legacy_manual_release"]
    assert manual and lease["lease_id"] == manual[0]["lease_id"]
    assert lease["lease_id"] in manual[0]["manual_release_command"]
    assert cap.main(shlex.split(manual[0]["manual_release_command"])[2:]) == 0
    released = cap.load_state()["leases"][lease["lease_id"]]
    assert released["state"] == "released"
    assert released["operator_confirmed"] is True
    assert released["released_by"]
    assert released["reason"] == "manual_legacy_release"
    assert released["released_at"]


@pytest.mark.parametrize("identity_source", ["lease", "ledger", "status"])
def test_operator_release_refuses_proven_live_worker(tmp_path, monkeypatch, identity_source):
    record = seed(tmp_path, attached=identity_source == "lease")
    if identity_source == "status":
        status_path = tmp_path / "worker-status.json"
        status_path.write_text(json.dumps(record))
        record.pop("worker_pid")
        record.pop("worker_identity")
        record["status_path"] = str(status_path)
        ledger.write_record(record)
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: True)
    monkeypatch.setattr(cap, "_pid_generation_matches", lambda _pid, _lease: True)
    assert cap.main(["release", "--lease-id", "held", "--operator-confirmed",
                     "--reason", "operator checked"]) == 1
    assert cap.load_state()["leases"]["held"]["state"] == "active"


@pytest.mark.parametrize("reason", [None, "   "])
def test_operator_release_requires_reason(tmp_path, reason):
    cap.save_state({"leases": {"held": {"lease_id": "held", "state": "active"}}, "cooldowns": {}})
    argv = ["release", "--lease-id", "held", "--operator-confirmed"]
    if reason is not None:
        argv.extend(["--reason", reason])
    assert cap.main(argv) == 1
    assert cap.load_state()["leases"]["held"]["state"] == "active"


@pytest.mark.parametrize("live", [True, False, None])
def test_operator_release_only_overrides_unknown_identity(tmp_path, monkeypatch, live):
    seed(tmp_path)
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: live)
    monkeypatch.setattr(cap, "_pid_generation_matches", lambda _pid, _lease: None)
    argv = ["release", "--lease-id", "held", "--operator-confirmed", "--reason", "checked session"]
    assert cap.main(argv) == (1 if live is False else 0)
    assert cap.load_state()["leases"]["held"]["state"] == ("active" if live is False else "released")


@pytest.mark.parametrize("keep", [False, True])
def test_normal_release_preserves_unknown_and_releases_dead(tmp_path, monkeypatch, keep):
    seed(tmp_path)
    argv = ["release", "--lease-id", "held", "--reason", "normal"]
    if keep:
        argv.append("--keep")
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: None)
    assert cap.main(argv) == 1
    assert cap.load_state()["leases"]["held"]["state"] == "active"
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: False)
    assert cap.main(argv) == 0
    leases = cap.load_state()["leases"]
    if keep:
        assert leases["held"]["state"] == "released"
        assert "operator_confirmed" not in leases["held"]
    else:
        assert "held" not in leases


def test_remote_lease_is_never_probed_or_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "machine_id", lambda: "local-machine")

    def fail_probe(_pid):
        raise AssertionError("remote worker identity was probed locally")

    monkeypatch.setattr(cap, "_probe_pid_liveness", fail_probe)
    lease = {
        "lease_id": "remote-holder",
        "state": "active",
        "agent": "codex",
        "machine_id": "remote-machine",
        "worker_pid": 4242,
        "worker_identity": {"pid": 4242, "start_token": "remote"},
        "expires_at": cap.iso(cap.utc_now() - dt.timedelta(hours=1)),
    }
    data = {"machine_id": "local-machine", "leases": {lease["lease_id"]: lease}, "cooldowns": {}}
    assert cap.reclaim_stale_leases(data) == []
    cap.prune_state(data)
    assert data["leases"][lease["lease_id"]]["state"] == "active"


def test_release_does_not_free_unattached_live_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "_probe_pid_liveness", lambda _pid: True)
    monkeypatch.setattr(cap, "_pid_generation_matches", lambda _pid, _lease: True)
    record = {
        "dispatch_id": "running-after-attach-failure",
        "project_root": str(tmp_path),
        "state": "running",
        "worker_pid": 4242,
        "worker_identity": {"pid": 4242, "start_token": "live"},
    }
    ledger.write_record(record)
    lease = {
        "lease_id": "unattached-live",
        "dispatch_id": record["dispatch_id"],
        "state": "active",
        "agent": "codex",
        "project_root": str(tmp_path),
    }
    cap.save_state({"leases": {lease["lease_id"]: lease}, "cooldowns": {}})
    output = io.StringIO()
    with redirect_stdout(output):
        rc = cap.cmd_release(argparse.Namespace(
            lease_id=lease["lease_id"], state="failed", reason="finalize", keep=True,
        ))
    assert rc == 1
    assert cap.load_state()["leases"][lease["lease_id"]]["state"] == "active"


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
