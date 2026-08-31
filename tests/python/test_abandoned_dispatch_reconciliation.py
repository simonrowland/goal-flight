#!/usr/bin/env python3
"""Conservative automatic reconciliation for abandoned dispatch records."""

from __future__ import annotations

import datetime as dt
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_capacity as C  # noqa: E402
import goalflight_compat  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402


SESSION_ID = "12345678-1234-4abc-8def-1234567890ab"


def _gone_controller() -> dict:
    """Proven-dead controller fields so abandonment can still close.

    Empty label is no longer permission to close (SC-153 C5). Close tests
    must name a controller that ``probe_live_session`` will report dead
    against an isolated journal-less project root.
    """
    return {
        "controller_pid": 99999,
        "controller_session_id": "dead-controller-session",
        "controller_label": "dead-controller",
    }


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER", str(tmp_path / "wake-ledger.json"))
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)


def _spawn_sleeping_worker() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


def _future_now(seconds: float = 900.0) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)


def _record(
    tmp_path: Path,
    dispatch_id: str,
    *,
    tail_text: str = "worker output stopped without a verdict\n",
    worker_pid: object = None,
    worker_identity: dict | None = None,
    lease_id: str | None = None,
    parent_dispatch_id: str | None = None,
    codex_home: Path | None = None,
    controller_pid: int | None = None,
    controller_session_id: str | None = None,
    controller_label: str | None = None,
    state: str = "running",
    **extra: object,
) -> dict:
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text(tail_text, encoding="utf-8")
    status = tmp_path / f"{dispatch_id}.status.json"
    payload = {
        "schema": L.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "shape": "bash",
        "transport": "dispatch",
        "project_root": str(tmp_path),
        "worker_cwd": str(tmp_path),
        "hostname": socket.gethostname(),
        "state": state,
        "terminal_state": "unknown",
        "started_at": (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        ).isoformat(timespec="seconds"),
        "worker_pid": worker_pid,
        "worker_identity": worker_identity,
        "lease_id": lease_id,
        "stdout_path": str(tail),
        "status_path": str(status),
        "controller_pid": controller_pid,
        "controller_session_id": controller_session_id,
        "controller_label": controller_label,
    }
    if parent_dispatch_id:
        payload["parent_dispatch_id"] = parent_dispatch_id
    if codex_home is not None:
        payload.update(
            {
                "codex_session_id": SESSION_ID,
                "codex_home": str(codex_home),
                "codex_home_owner_dispatch_id": codex_home.name,
            }
        )
    if "worker_pgid" not in extra and isinstance(worker_identity, dict):
        pgid = worker_identity.get("pgid")
        if pgid is not None:
            extra = {**extra, "worker_pgid": pgid}
    payload.update(extra)
    L.write_record(payload)
    status_file = Path(str(payload.get("status_path") or status))
    if not status_file.exists():
        _write_status(tmp_path, dispatch_id)
    return payload


def _write_status(tmp_path: Path, record_dispatch_id: str, **updates: object) -> dict:
    payload = {
        "schema": "goalflight.status.v1",
        "dispatch_id": record_dispatch_id,
        **updates,
    }
    (tmp_path / f"{record_dispatch_id}.status.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return payload


def _read(dispatch_id: str) -> dict:
    return json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))


def _run(tmp_path: Path, *, now: dt.datetime | None = None, dry_run: bool = False) -> dict:
    queue_dir = tmp_path / "state" / "dispatch-queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    return D.reconcile_abandoned_dispatches(
        queue_dir=queue_dir,
        dry_run=dry_run,
        now=now or _future_now(),
    )


def _write_active_lease(dispatch_id: str, lease_id: str) -> None:
    state = {
        "schema": C.SCHEMA,
        "machine_id": "fixture-machine",
        "leases": {
            lease_id: {
                "lease_id": lease_id,
                "dispatch_id": dispatch_id,
                "state": "active",
            }
        },
        "cooldowns": {},
    }
    with C.StateLock():
        C.save_state(state)


def _stub_resume_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict], list[int]]:
    spawn_calls: list[dict] = []
    worker_pids: list[int] = []
    monkeypatch.setattr(D, "_reap_quota_stuck_before_bash_launch", lambda: None)
    monkeypatch.setattr(D, "_resolve_account_env", lambda _args: {})
    monkeypatch.setattr(D, "_acquire_capacity", lambda *_args, **_kwargs: "lease-resume")
    monkeypatch.setattr(
        D,
        "_rebuild_codex_resume_home",
        lambda _root, _parent, expected_home, _session, **_kwargs: (
            str(expected_home),
            "fixture-seat",
        ),
    )
    monkeypatch.setattr(D, "_mark_queue_claim_launch_started", lambda _args: None)
    monkeypatch.setattr(D, "_mark_queue_claim_worker_spawn_intent", lambda _args: None)
    monkeypatch.setattr(D, "_mark_queue_claim_worker_spawned", lambda _args, _pid: None)
    monkeypatch.setattr(
        D,
        "_process_identity_after_spawn",
        lambda pid: {"pid": pid, "lstart": "fixture", "comm": "codex"},
    )
    monkeypatch.setattr(D, "process_group_id", lambda pid: pid)
    monkeypatch.setattr(D, "_start_caffeinate", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(D, "_attach_worker_to_lease", lambda *_args: None)
    monkeypatch.setattr(D, "_detach_lease_to_worker", lambda *_args: None)
    monkeypatch.setattr(D, "_write_pidfile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(D, "_upsert_project_registry_for_dispatch", lambda *_args: None)

    def spawn(argv: list[str], **kwargs: object) -> int:
        pid = 42000 + len(spawn_calls) + 1
        spawn_calls.append({"argv": list(argv), "label": kwargs.get("label"), "pid": pid})
        if kwargs.get("label") == "worker":
            worker_pids.append(pid)
        return pid

    monkeypatch.setattr(D, "_spawn_daemonized_process", spawn)
    return spawn_calls, worker_pids


def _write_rollout(home: Path) -> None:
    rollout = (
        home
        / "sessions"
        / "2026"
        / "08"
        / "03"
        / f"rollout-2026-08-03T12-00-00-{SESSION_ID}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")


def test_no_pid_no_lease_no_marker_closes_without_asserting_outcome(tmp_path: Path) -> None:
    dispatch_id = "abandoned-no-verdict"
    _record(tmp_path, dispatch_id, **_gone_controller())

    result = _run(tmp_path)
    closed = _read(dispatch_id)

    assert result["closed"] == 1
    assert closed["state"] == "inconclusive_no_final"
    assert closed["terminal_state"] == "inconclusive_no_final"
    assert closed["reason"] == "abandoned_without_verdict"
    reconciliation = closed["outcome"]["reconciliation"]
    assert reconciliation["source"] == "goalflight_dispatch.drain"
    assert reconciliation["basis"] == "inferred_abandonment"
    assert reconciliation["observed_outcome"] is False
    assert reconciliation["process_evidence"] == "no_recorded_pid"
    assert reconciliation["lease_evidence"] == "lease_absent"


def test_live_worker_pid_is_never_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "live-worker"
    _record(
        tmp_path,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_identity=L.process_identity(os.getpid()),
    )
    real_pid = os.getpid()

    def identity_matches(record: dict) -> tuple[bool, str]:
        assert record["worker_pid"] == real_pid
        return True, "live"

    monkeypatch.setattr(D.goalflight_ledger, "identity_matches", identity_matches)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}


def test_status_worker_alive_without_pid_is_never_closed(tmp_path: Path) -> None:
    dispatch_id = "status-says-live"
    _record(tmp_path, dispatch_id)
    _write_status(tmp_path, dispatch_id, worker_alive=True)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}


def test_stale_worker_alive_true_with_dead_pid_is_eligible(tmp_path: Path) -> None:
    proc = _spawn_sleeping_worker()
    try:
        identity = L.process_identity(proc.pid)
        assert identity is not None
        pid = proc.pid
    finally:
        _reap(proc)
    assert L.identity_matches({"worker_pid": pid, "worker_identity": identity}) == (
        False,
        "dead",
    )

    dispatch_id = "stale-alive-dead-pid"
    _record(
        tmp_path,
        dispatch_id,
        worker_pid=pid,
        worker_identity=identity,
        **_gone_controller(),
    )
    _write_status(
        tmp_path,
        dispatch_id,
        worker_alive=True,
        worker_pid=pid,
        expected_worker_identity=identity,
    )

    result = _run(tmp_path)
    closed = _read(dispatch_id)

    assert result["closed"] == 1
    assert closed["state"] == "inconclusive_no_final"
    process_evidence = closed["outcome"]["reconciliation"]["process_evidence"]
    assert "dead" in process_evidence
    assert "status_worker_alive:true" not in process_evidence


def test_worker_alive_true_with_live_pid_is_not_eligible(tmp_path: Path) -> None:
    proc = _spawn_sleeping_worker()
    try:
        identity = L.process_identity(proc.pid)
        assert identity is not None
        dispatch_id = "alive-flag-live-pid"
        _record(
            tmp_path,
            dispatch_id,
            worker_pid=proc.pid,
            worker_identity=identity,
        )
        _write_status(
            tmp_path,
            dispatch_id,
            worker_alive=True,
            worker_pid=proc.pid,
            expected_worker_identity=identity,
        )

        result = _run(tmp_path)

        assert result["closed"] == 0
        assert _read(dispatch_id)["state"] == "running"
        assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}
        assert "live" in str(result["entries"][0].get("process_evidence", ""))
    finally:
        _reap(proc)


def test_eperm_probe_with_stale_worker_alive_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    proc: subprocess.Popen | None = None
    try:
        if goalflight_compat.pid_liveness(1) is None:
            pid = 1
            identity = L.process_identity(pid) or {
                "pid": pid,
                "identity_available": False,
                "identity_probe_error": True,
                "identity_source": "pid_probe_error",
            }
        else:
            proc = _spawn_sleeping_worker()
            identity = L.process_identity(proc.pid)
            assert identity is not None
            pid = proc.pid
            real_kill = goalflight_compat.os.kill

            def _kill(probe_pid: int, sig: int) -> None:
                if int(probe_pid) == int(pid):
                    raise PermissionError(errno.EPERM, "Operation not permitted")
                return real_kill(probe_pid, sig)

            monkeypatch.setattr(goalflight_compat.os, "kill", _kill)

        dispatch_id = "eperm-stale-alive"
        _record(tmp_path, dispatch_id, worker_pid=pid, worker_identity=identity)
        _write_status(
            tmp_path,
            dispatch_id,
            worker_alive=True,
            worker_pid=pid,
            expected_worker_identity=identity,
        )

        result = _run(tmp_path)

        assert result["closed"] == 0
        assert _read(dispatch_id)["state"] == "running"
        assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}
        evidence = str(result["entries"][0].get("process_evidence", ""))
        assert "indeterminate" in evidence
    finally:
        if proc is not None:
            _reap(proc)


def _dead_worker_identity() -> tuple[int, dict]:
    proc = _spawn_sleeping_worker()
    try:
        identity = L.process_identity(proc.pid)
        assert identity is not None
        pid = proc.pid
    finally:
        _reap(proc)
    assert L.identity_matches({"worker_pid": pid, "worker_identity": identity}) == (
        False,
        "dead",
    )
    return pid, identity


def _indeterminate_group_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, dict, subprocess.Popen | None]:
    """Return a pid whose liveness probe is genuinely unreadable (EPERM)."""
    if goalflight_compat.pid_liveness(1) is None:
        identity = L.process_identity(1) or {
            "pid": 1,
            "identity_available": False,
            "identity_probe_error": True,
            "identity_source": "pid_probe_error",
        }
        return 1, identity, None
    proc = _spawn_sleeping_worker()
    identity = L.process_identity(proc.pid)
    assert identity is not None
    pid = proc.pid
    real_kill = goalflight_compat.os.kill

    def _kill(probe_pid: int, sig: int) -> None:
        if int(probe_pid) == int(pid):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_kill(probe_pid, sig)

    monkeypatch.setattr(goalflight_compat.os, "kill", _kill)
    return pid, identity, proc


def test_dead_pids_without_group_contract_are_eligible(tmp_path: Path) -> None:
    pid, identity = _dead_worker_identity()
    dispatch_id = "dead-no-group-contract"
    _record(
        tmp_path,
        dispatch_id,
        worker_pid=pid,
        worker_identity=identity,
        worker_pgid=pid,
        queue_launch_token="launch-token-absent-contract",
        **_gone_controller(),
    )
    _write_status(
        tmp_path,
        dispatch_id,
        worker_alive=True,
        worker_pid=pid,
        expected_worker_identity=identity,
    )

    result = _run(tmp_path)
    closed = _read(dispatch_id)
    entry = result["entries"][0]

    assert result["closed"] == 1
    assert entry["eligible"] is True
    assert entry["reason"] == D._ELIGIBLE_NO_GROUP_CONTRACT
    assert D._PRODUCER_SET_STRUCTURALLY_ABSENT in str(entry.get("process_evidence", ""))
    assert "producer_set:indeterminate:" not in str(entry.get("process_evidence", ""))
    assert closed["state"] == "inconclusive_no_final"
    assert closed["outcome"]["reconciliation"]["reason"] == D._ELIGIBLE_NO_GROUP_CONTRACT


def test_dead_pids_with_unreadable_group_contract_are_not_eligible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pid, identity = _dead_worker_identity()
    group_pid, group_identity, extra = _indeterminate_group_leader(monkeypatch)
    try:
        dispatch_id = "dead-unreadable-contract"
        _record(
            tmp_path,
            dispatch_id,
            worker_pid=pid,
            worker_identity=identity,
            worker_pgid=group_pid,
            worker_group_leader_identity=group_identity,
            producer_group_contract=True,
            producer_group_contract_enforced=True,
            queue_launch_token="launch-token-present-unread",
        )
        _write_status(
            tmp_path,
            dispatch_id,
            worker_alive=True,
            worker_pid=pid,
            expected_worker_identity=identity,
        )

        result = _run(tmp_path)
        evidence = str(result["entries"][0].get("process_evidence", ""))

        assert result["closed"] == 0
        assert _read(dispatch_id)["state"] == "running"
        assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}
        assert "producer_set:indeterminate:" in evidence
        assert D._PRODUCER_SET_STRUCTURALLY_ABSENT not in evidence
    finally:
        if extra is not None:
            _reap(extra)


def test_indeterminate_pid_without_group_contract_is_not_eligible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    group_pid, group_identity, extra = _indeterminate_group_leader(monkeypatch)
    try:
        dispatch_id = "indeterminate-no-group-contract"
        _record(
            tmp_path,
            dispatch_id,
            worker_pid=group_pid,
            worker_identity=group_identity,
            worker_pgid=group_pid,
            queue_launch_token="launch-token-absent-contract",
        )
        _write_status(
            tmp_path,
            dispatch_id,
            worker_alive=True,
            worker_pid=group_pid,
            expected_worker_identity=group_identity,
        )

        result = _run(tmp_path)
        evidence = str(result["entries"][0].get("process_evidence", ""))

        assert result["closed"] == 0
        assert _read(dispatch_id)["state"] == "running"
        assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}
        assert "indeterminate" in evidence
        assert D._PRODUCER_SET_STRUCTURALLY_ABSENT not in evidence
        assert result["entries"][0]["reason"] != D._ELIGIBLE_NO_GROUP_CONTRACT
    finally:
        if extra is not None:
            _reap(extra)


def test_live_worker_without_group_contract_is_not_eligible(tmp_path: Path) -> None:
    proc = _spawn_sleeping_worker()
    try:
        identity = L.process_identity(proc.pid)
        assert identity is not None
        dispatch_id = "live-no-group-contract"
        _record(
            tmp_path,
            dispatch_id,
            worker_pid=proc.pid,
            worker_identity=identity,
            worker_pgid=proc.pid,
            queue_launch_token="launch-token-absent-contract",
        )
        _write_status(
            tmp_path,
            dispatch_id,
            worker_alive=True,
            worker_pid=proc.pid,
            expected_worker_identity=identity,
        )

        result = _run(tmp_path)

        assert result["closed"] == 0
        assert _read(dispatch_id)["state"] == "running"
        assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}
        assert "live" in str(result["entries"][0].get("process_evidence", ""))
        assert result["entries"][0]["reason"] != D._ELIGIBLE_NO_GROUP_CONTRACT
    finally:
        _reap(proc)


def test_live_persisted_descendant_is_never_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "live-descendant"
    record = _record(tmp_path, dispatch_id)
    record["producer_descendants"] = [
        {"pid": os.getpid(), "identity": {"generation": "descendant"}}
    ]
    L.write_record(record)

    def identity_matches(probe: dict) -> tuple[bool, str]:
        assert probe["worker_pid"] == os.getpid()
        assert probe["worker_identity"]["generation"] == "descendant"
        return True, "live"

    monkeypatch.setattr(D.goalflight_ledger, "identity_matches", identity_matches)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}


def test_malformed_nonempty_pid_is_ambiguous(tmp_path: Path) -> None:
    dispatch_id = "malformed-pid"
    _record(tmp_path, dispatch_id, worker_pid="not-a-pid")

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}


def test_conflicting_identities_for_same_pid_probe_both(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "same-pid-new-generation"
    pid = 44551
    _record(
        tmp_path,
        dispatch_id,
        worker_pid=pid,
        worker_identity={"generation": "stale"},
    )
    _write_status(
        tmp_path,
        dispatch_id,
        worker_pid=pid,
        expected_worker_identity={"generation": "live"},
    )
    probes: list[str] = []

    def identity_matches(probe: dict) -> tuple[bool, str]:
        generation = probe["worker_identity"]["generation"]
        probes.append(generation)
        return (generation == "live", "live" if generation == "live" else "dead")

    monkeypatch.setattr(D.goalflight_ledger, "identity_matches", identity_matches)

    result = _run(tmp_path)

    assert probes == ["stale", "live"]
    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"


def test_terminal_marker_reconciles_observed_real_outcome(tmp_path: Path) -> None:
    dispatch_id = "observed-complete"
    _record(
        tmp_path,
        dispatch_id,
        tail_text=f"work log\nCOMPLETE: {dispatch_id} — verified result\n",
        **_gone_controller(),
    )

    result = _run(tmp_path)
    closed = _read(dispatch_id)

    assert result["closed"] == 1
    assert closed["state"] == "complete"
    assert closed["terminal_state"] == "complete"
    assert closed["terminal_marker"]["kind"] == "COMPLETE"
    reconciliation = closed["outcome"]["reconciliation"]
    assert reconciliation["basis"] == "observed_terminal_marker"
    assert reconciliation["observed_outcome"] is True
    assert reconciliation["terminal_marker_kind"] == "COMPLETE"


def test_terminal_marker_arriving_after_final_evaluation_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "marker-during-close"
    record = _record(tmp_path, dispatch_id, **_gone_controller())
    tail = Path(record["stdout_path"])
    original = D._abandoned_terminal_outcome
    injected = False

    def inject_marker(fresh: dict) -> tuple[str, object, dict | None]:
        nonlocal injected
        if not injected:
            injected = True
            with tail.open("a", encoding="utf-8") as stream:
                stream.write(f"COMPLETE: {dispatch_id} — terminal marker reached disk\n")
        return original(fresh)

    monkeypatch.setattr(D, "_abandoned_terminal_outcome", inject_marker)

    result = _run(tmp_path)
    closed = _read(dispatch_id)

    assert injected is True
    assert result["closed"] == 1
    assert closed["state"] == "complete"
    assert closed["outcome"]["reconciliation"]["basis"] == "observed_terminal_marker"


def test_recent_progress_is_ambiguous_and_left_open(tmp_path: Path) -> None:
    dispatch_id = "recent-progress"
    _record(tmp_path, dispatch_id, **_gone_controller())

    result = _run(tmp_path, now=dt.datetime.now(dt.timezone.utc))

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"recent_progress": 1}


def test_young_partially_written_record_is_left_open(tmp_path: Path) -> None:
    dispatch_id = "young-partial-record"
    record = _record(tmp_path, dispatch_id, **_gone_controller())
    Path(record["stdout_path"]).unlink()

    result = _run(tmp_path, now=dt.datetime.now(dt.timezone.utc))

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"recent_progress": 1}


def test_corrupt_status_is_ambiguous_and_left_open(tmp_path: Path) -> None:
    dispatch_id = "corrupt-status"
    _record(tmp_path, dispatch_id)
    (tmp_path / f"{dispatch_id}.status.json").write_text("{", encoding="utf-8")

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"status_indeterminate": 1}


def test_mismatched_status_is_ambiguous_and_left_open(tmp_path: Path) -> None:
    dispatch_id = "mismatched-status"
    _record(tmp_path, dispatch_id)
    _write_status(tmp_path, dispatch_id, dispatch_id="different-dispatch")

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"status_indeterminate": 1}


def test_missing_status_pointer_is_ambiguous_and_left_open(tmp_path: Path) -> None:
    dispatch_id = "missing-status-pointer"
    record = _record(tmp_path, dispatch_id)
    record.pop("status_path")
    L.write_record(record)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"status_indeterminate": 1}


def test_recorded_status_path_file_absent_is_ambiguous_and_left_open(
    tmp_path: Path,
) -> None:
    """C3: a recorded status_path whose file is missing is not empty evidence."""
    dispatch_id = "missing-status-file"
    record = _record(tmp_path, dispatch_id, **_gone_controller())
    Path(str(record["status_path"])).unlink()

    result = _run(tmp_path)
    entry = result["entries"][0]

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"status_indeterminate": 1}
    assert entry["status_evidence"] == "status_file_absent"


def test_empty_controller_label_is_not_permission_to_close(tmp_path: Path) -> None:
    """C5: missing controller identity retains, it does not read as unowned."""
    dispatch_id = "empty-controller-label"
    _record(tmp_path, dispatch_id)

    result = _run(tmp_path)
    entry = result["entries"][0]

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"controller_indeterminate": 1}
    assert entry["controller_evidence"] == "controller_identity_absent"


def test_output_child_of_unsearchable_parent_is_not_gone(tmp_path: Path) -> None:
    """C4: Path.exists() False on an unsearchable parent is not output_file_absent."""
    dispatch_id = "output-unsearchable-parent"
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    tail = hidden / f"{dispatch_id}.tail"
    tail.write_text("worker output stopped without a verdict\n", encoding="utf-8")
    _record(
        tmp_path,
        dispatch_id,
        **_gone_controller(),
        stdout_path=str(tail),
    )

    os.chmod(hidden, 0o000)
    try:
        result = _run(tmp_path)
    finally:
        os.chmod(hidden, 0o700)

    entry = result["entries"][0]
    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"output_indeterminate": 1}
    assert "unsearchable" in str(entry.get("output_evidence") or "")


def test_missing_output_pointer_is_ambiguous_and_left_open(tmp_path: Path) -> None:
    dispatch_id = "missing-output-pointer"
    record = _record(tmp_path, dispatch_id)
    record.pop("stdout_path")
    L.write_record(record)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"output_indeterminate": 1}


def test_active_lease_vetoes_reconciliation(tmp_path: Path) -> None:
    dispatch_id = "leased-worker"
    lease_id = "lease-live"
    _record(tmp_path, dispatch_id, lease_id=lease_id)
    _write_active_lease(dispatch_id, lease_id)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"lease_live_or_indeterminate": 1}


def test_malformed_matching_lease_is_ambiguous(tmp_path: Path) -> None:
    dispatch_id = "malformed-lease"
    lease_id = "lease-malformed"
    _record(tmp_path, dispatch_id, lease_id=lease_id)
    with C.StateLock():
        C.save_state(
            {
                "schema": C.SCHEMA,
                "machine_id": "fixture-machine",
                "leases": {lease_id: "partially-written"},
                "cooldowns": {},
            }
        )

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"lease_live_or_indeterminate": 1}


def _stub_controller_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str,
    session: dict | None = None,
    expected_label: str | None | object = ...,
) -> None:
    def probe(
        _project: Path,
        *,
        label: str | None = None,
        pid: int | None = None,
    ) -> tuple[str, dict | None]:
        del pid
        if expected_label is not ... and label != expected_label:
            pytest.fail(f"unexpected label: {label}")
        return state, session

    monkeypatch.setattr(D.goalflight_session_status, "probe_live_session", probe)
    monkeypatch.setattr(
        D.goalflight_session_status,
        "live_session",
        lambda *_args, **_kwargs: pytest.fail("legacy collapsing wrapper was used"),
    )


def test_live_controller_beacon_vetoes_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "controller-owned"
    controller_pid = os.getpid()
    _record(
        tmp_path,
        dispatch_id,
        controller_pid=controller_pid,
        controller_session_id="controller-session",
    )
    _stub_controller_probe(
        monkeypatch,
        state="live",
        session={"id": "controller-session", "pid": controller_pid},
        expected_label=None,
    )

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"controller_live_or_indeterminate": 1}


def test_live_stable_controller_label_vetoes_after_session_rollover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "stable-controller-owner"
    _record(
        tmp_path,
        dispatch_id,
        controller_pid=11111,
        controller_session_id="old-controller-session",
        controller_label="controller-a",
    )
    _stub_controller_probe(
        monkeypatch,
        state="live",
        session={
            "id": "new-controller-session",
            "pid": os.getpid(),
            "label": "controller-a",
        },
        expected_label="controller-a",
    )

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"controller_live_or_indeterminate": 1}


def test_unreadable_controller_probe_does_not_reclaim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "controller-unreadable"
    _record(
        tmp_path,
        dispatch_id,
        controller_pid=os.getpid(),
        controller_session_id="controller-session",
    )
    _stub_controller_probe(monkeypatch, state="unreadable", session=None)

    result = _run(tmp_path)
    entry = result["entries"][0]

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert entry["eligible"] is False
    assert entry["reason"] == "controller_indeterminate"
    assert entry["controller_evidence"] == "controller_indeterminate"
    assert result["kept_reasons"] == {"controller_indeterminate": 1}
    detail = entry["detail"]
    assert "--retire" in detail
    assert "--acknowledge-retirement" in detail
    assert "t-238" in detail


def test_unexpected_controller_probe_state_does_not_reclaim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "controller-busy"
    _record(
        tmp_path,
        dispatch_id,
        controller_pid=os.getpid(),
        controller_session_id="controller-session",
    )
    _stub_controller_probe(monkeypatch, state="busy", session=None)

    result = _run(tmp_path)
    entry = result["entries"][0]

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert entry["eligible"] is False
    assert entry["reason"] == "controller_live_or_indeterminate"
    assert entry["controller_evidence"] == "controller_beacon_error:unexpected_probe_state:busy"
    assert result["kept_reasons"] == {"controller_live_or_indeterminate": 1}


def test_reconcile_abandoned_text_print_includes_kept_reasons(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch_id = "controller-unreadable-print"
    _record(
        tmp_path,
        dispatch_id,
        controller_pid=os.getpid(),
        controller_session_id="controller-session",
        controller_label="engine",
    )
    _stub_controller_probe(
        monkeypatch,
        state="unreadable",
        session=None,
        expected_label="engine",
    )
    queue_dir = tmp_path / "state" / "dispatch-queue"
    queue_dir.mkdir(parents=True, exist_ok=True)

    rc = D._cmd_reconcile_abandoned(
        ["--queue-dir", str(queue_dir), "--stale-s", "0"]
    )
    captured = capsys.readouterr().out.strip()

    assert rc == 0
    assert captured.startswith("RECONCILE-ABANDONED ")
    payload = json.loads(captured[captured.index("{") :])
    assert payload["kept_reasons"] == {"controller_indeterminate": 1}
    assert payload["would_close"] == 0
    assert payload["kept"] == 1
    assert payload["mode"] == "dry-run"
    assert "controller_indeterminate" in captured
    assert _read(dispatch_id)["state"] == "running"


def test_reconcile_abandoned_dry_run_text_states_no_record_changed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch_id = "dry-run-would-close"
    _record(tmp_path, dispatch_id, **_gone_controller())
    queue_dir = tmp_path / "state" / "dispatch-queue"
    queue_dir.mkdir(parents=True, exist_ok=True)

    rc = D._cmd_reconcile_abandoned(
        ["--queue-dir", str(queue_dir), "--stale-s", "0"]
    )
    captured = capsys.readouterr().out.strip()
    payload = json.loads(captured[captured.index("{") :])

    assert rc == 0
    assert "dry-run" in captured
    assert "no ledger record was changed" in captured
    assert "no apply flag" in captured
    assert "only drain writes" in captured
    assert payload["mode"] == "dry-run"
    assert payload["would_close"] == 1
    assert payload["kept"] == 0
    assert _read(dispatch_id)["state"] == "running"


def test_dead_controller_probe_still_reclaims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "controller-dead"
    _record(
        tmp_path,
        dispatch_id,
        controller_pid=99999,
        controller_session_id="controller-session",
    )
    _stub_controller_probe(monkeypatch, state="dead", session=None)

    result = _run(tmp_path)
    closed = _read(dispatch_id)

    assert result["closed"] == 1
    assert closed["state"] == "inconclusive_no_final"
    assert closed["reason"] == "abandoned_without_verdict"
    assert closed["outcome"]["reconciliation"]["controller_evidence"] == (
        "controller_beacon_absent"
    )


def test_reconciliation_is_idempotent(tmp_path: Path) -> None:
    dispatch_id = "idempotent-close"
    _record(tmp_path, dispatch_id, **_gone_controller())

    first = _run(tmp_path)
    record_path = L.record_path(dispatch_id)
    after_first = record_path.read_bytes()
    second = _run(tmp_path, now=_future_now(1200.0))

    assert first["closed"] == 1
    assert second["closed"] == 0
    assert record_path.read_bytes() == after_first


def test_reconcile_parses_far_fewer_rows_than_terminal_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pass over mostly-terminal history must not json.loads every row.

    Work unit is parse/evaluate count, not wall time. Age is not a skip
    predicate: the live rows below are still all evaluated.
    """
    terminal_n = 40
    live_ids = ("live-a", "live-b", "live-c")
    for i in range(terminal_n):
        _record(tmp_path, f"done-{i:02d}", state="complete")
    for dispatch_id in live_ids:
        _record(tmp_path, dispatch_id, **_gone_controller())

    evaluated: list[str] = []
    real = D._evaluate_abandoned_dispatch

    def wrapped(record: dict, **kwargs: object) -> dict:
        evaluated.append(str(record.get("dispatch_id") or ""))
        return real(record, **kwargs)

    monkeypatch.setattr(D, "_evaluate_abandoned_dispatch", wrapped)
    result = _run(tmp_path)
    work = L.last_read_work()

    assert work["listed"] == terminal_n + len(live_ids)
    assert work["parsed"] == len(live_ids)
    assert work["skipped_terminal"] == terminal_n
    assert result["parsed"] == len(live_ids)
    assert result["skipped_terminal"] == terminal_n
    assert result["listed"] == work["listed"]
    assert set(evaluated) == set(live_ids)
    assert work["parsed"] < work["listed"] / 4


def test_skipped_terminal_row_reenters_when_rewritten_nonterminal(
    tmp_path: Path,
) -> None:
    _record(tmp_path, "flip", state="complete")
    first = L.read_records(skip_terminal=True)
    assert [row.get("dispatch_id") for row in first] == []
    assert L.last_read_work()["skipped_terminal"] == 1
    assert L.last_read_work()["parsed"] == 0

    current = _read("flip")
    current["state"] = "running"
    current["terminal_state"] = "unknown"
    L.write_record(current)
    second = L.read_records(skip_terminal=True)
    assert [row.get("dispatch_id") for row in second] == ["flip"]
    assert L.last_read_work()["parsed"] == 1
    assert L.last_read_work()["skipped_terminal"] == 0


def test_old_nonterminal_row_is_not_skipped_by_age(tmp_path: Path) -> None:
    _record(tmp_path, "old-queued", state="queued")
    path = L.record_path("old-queued")
    os.utime(path, (0, 0))
    rows = L.read_records(skip_terminal=True)
    assert [row.get("dispatch_id") for row in rows] == ["old-queued"]
    assert L.last_read_work()["parsed"] == 1
    assert L.last_read_work()["skipped_terminal"] == 0


def test_read_records_default_still_returns_terminal_history(tmp_path: Path) -> None:
    _record(tmp_path, "done", state="complete")
    _record(tmp_path, "live", state="running")
    rows = L.read_records()
    assert {row.get("dispatch_id") for row in rows} == {"done", "live"}
    assert L.last_read_work()["parsed"] == 2
    assert L.last_read_work()["skipped_terminal"] == 0


def test_compact_terminal_json_is_parsed_not_silently_skipped(tmp_path: Path) -> None:
    """Peek only matches pretty-printed top-level state. Compact JSON fail-opens."""
    path = L.record_path("compact-done")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"dispatch_id": "compact-done", "state": "complete"}),
        encoding="utf-8",
    )
    rows = L.read_records(skip_terminal=True)
    assert [row.get("dispatch_id") for row in rows] == ["compact-done"]
    assert L.last_read_work()["parsed"] == 1
    assert L.last_read_work()["skipped_terminal"] == 0


def test_overlapping_skip_reads_both_see_live_rows(tmp_path: Path) -> None:
    for i in range(20):
        _record(tmp_path, f"done-{i:02d}", state="complete")
    _record(tmp_path, "live", state="running")
    seen: list[list[object]] = []

    def worker() -> None:
        rows = L.read_records(skip_terminal=True)
        seen.append([row.get("dispatch_id") for row in rows])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert seen == [["live"], ["live"]]


def test_local_drain_tick_runs_reconciliation_automatically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_dir = Path(os.environ["GOALFLIGHT_STATE_DIR"]) / "dispatch-queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    calls: list[Path] = []
    expected = {
        "schema": D.ABANDONED_RECONCILIATION_SCHEMA,
        "mode": "automatic",
        "closed": 2,
    }
    monkeypatch.setattr(D, "_release_stale_capacity_for_drain", lambda: None)
    monkeypatch.setattr(
        D,
        "_reconcile_abandoned_for_drain",
        lambda path: calls.append(path) or expected,
    )
    monkeypatch.setattr(
        D,
        "_recover_claimed_queue_entries",
        lambda *_args, **_kwargs: {"restored": 0, "failed": 0},
    )
    args = SimpleNamespace(
        queue_dir=str(queue_dir),
        remote_node=None,
        claim_stale_s=300.0,
        limit=0,
    )

    payload = D._drain_queue_once(args)

    assert calls == [queue_dir]
    assert payload["abandoned_reconciliation"] == expected


def test_inferred_abandonment_is_resumable_and_fresh_child_stays_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "resume-parent"
    stale_child_id = "resume-child-stale"
    fresh_child_id = "resume-child-fresh"
    home = tmp_path / "state" / "dispatch-homes" / parent_id
    _write_rollout(home)
    parent = _record(tmp_path, parent_id, codex_home=home, **_gone_controller())
    stale_child = _record(
        tmp_path,
        stale_child_id,
        parent_dispatch_id=parent_id,
        codex_home=home,
        state="waiting_capacity",
        **_gone_controller(),
    )
    stale_child["codex_home_owner_dispatch_id"] = parent_id
    L.write_record(stale_child)

    first = _run(tmp_path)
    assert first["closed"] == 2
    assert _read(parent_id)["state"] == "inconclusive_no_final"
    assert _read(stale_child_id)["state"] == "inconclusive_no_final"

    prompt = tmp_path / "resume.md"
    prompt.write_text("Add one more feature.", encoding="utf-8")
    monkeypatch.setattr(D, "_reserve_auto_dispatch_id", lambda *_args: fresh_child_id)
    spawn_calls, worker_pids = _stub_resume_runtime(monkeypatch)

    def identity_matches(probe: dict) -> tuple[bool, str]:
        try:
            pid = int(probe.get("worker_pid") or 0)
        except (TypeError, ValueError):
            return False, "identity_indeterminate"
        return (pid in worker_pids, "live" if pid in worker_pids else "dead")

    monkeypatch.setattr(D.goalflight_ledger, "identity_matches", identity_matches)

    assert D._cmd_resume(
        [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    ) == 0
    resumed = _read(fresh_child_id)
    reopened_parent = _read(parent_id)
    assert [call["label"] for call in spawn_calls] == ["worker", "watcher"]
    assert resumed["state"] == "running"
    assert resumed["parent_dispatch_id"] == parent_id
    assert "outcome" not in resumed
    assert L.parse_utc(resumed["started_at"]) > L.parse_utc(parent["started_at"])
    assert reopened_parent["state"] == "superseded"
    assert reopened_parent["resumed_by_dispatch_id"] == fresh_child_id
    assert reopened_parent["resumed_at"] == resumed["started_at"]
    # `resumed_at` is COPIED from the child's started_at, so equality above is a
    # real contract. `updated_at` is stamped by write_record at write time, so it
    # equals started_at only when the write happens to land in the same second --
    # asserting equality here made the suite fail whenever the resume straddled a
    # tick (observed 2026-08-10: 18:02:36 vs 18:02:37). The actual guarantee is
    # ordering: the parent is updated at or after the child starts.
    assert L.parse_utc(reopened_parent["updated_at"]) >= L.parse_utc(resumed["started_at"])
    assert reopened_parent["outcome"]["reconciliation"]["basis"] == "inferred_abandonment"
    assert reopened_parent["outcome"]["resume"]["dispatch_id"] == fresh_child_id

    second = _run(tmp_path, now=_future_now(1800.0))
    assert second["closed"] == 0
    assert _read(fresh_child_id)["state"] == "running"


def test_peek_refuses_truncated_document_and_parses_it_instead() -> None:
    """A truncated prefix must never be skipped as terminal.

    The top-level state line can appear in an incomplete document. Skipping
    on that strands the row forever: the same prefix peeks the same terminal
    state on every later pass, so it is never parsed. Fail toward parsing.
    """
    import goalflight_ledger as ledger

    truncated = '{\n  "dispatch_id": "x",\n  "state": "complete",\n  "tail":'
    complete = '{\n  "dispatch_id": "x",\n  "state": "complete"\n}\n'

    assert ledger._peek_top_level_state(truncated) is None
    assert ledger._peek_top_level_state(complete) == "complete"
    assert ledger._peek_top_level_state(complete + "\n\n") == "complete"
