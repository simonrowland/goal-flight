#!/usr/bin/env python3
"""A terminal sidecar never outranks the recorded worker identity.

``reconcile_terminal_outbox`` used to promote a sidecar's ``failed`` /
``idle_timeout`` / ``complete`` into journal AND ledger terminal authority
without checking whether the worker was still alive (sweep-authority finding,
``docs-private/reviews/2026-08-27-sweep-authority/findings.md`` P1). A terminal
sidecar is a statement about the dispatch channel -- watchers write
``idle_timeout`` after the worker already did its work, and a status-write
error mirror says ``failed`` while the worker runs on. Promoting it over an
identity-live worker frees the capacity lease and makes the worktree look
unowned to GC.

The gate: process identity (pid AND start token, never pid alone) outranks the
sidecar. Identity-live holds the terminal write and records the disagreement
as a ``sidecar_terminal_overruled`` journal attention item; an unreadable or
absent identity is UNKNOWN and also holds (terminalizing is the destructive
direction, so doubt resolves against the write); a genuinely dead worker --
identity gone, or the pid now belongs to a different process -- terminalizes
exactly as before.

Every liveness precondition here is REAL: the "worker" is an actual spawned
child process whose pid and start token are recorded from the live process
table. No double answers the liveness question (b-235).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Callable, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402


SpawnWorker = Callable[[], "subprocess.Popen[bytes]"]


@pytest.fixture
def spawn_worker() -> Iterator[SpawnWorker]:
    """Spawn real long-lived children; reap every one in teardown, pass or fail."""
    children: list[subprocess.Popen[bytes]] = []

    def _spawn() -> subprocess.Popen[bytes]:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        children.append(child)
        return child

    yield _spawn
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def _write_sidecar(
    path: Path,
    *,
    dispatch_id: str,
    state: str,
    worker_pid: int | None,
    reason: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "goalflight.status.v1",
        "dispatch_id": dispatch_id,
        "state": state,
        "worker_pid": worker_pid,
        "worker_alive": True,
        "updated_at": 1,
    }
    if reason is not None:
        payload["reason"] = reason
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_ledger_record(
    project: Path,
    *,
    dispatch_id: str,
    status_path: Path,
    worker_pid: int | None = None,
    worker_identity: dict | None = None,
) -> dict:
    record = {
        "schema": ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "prompt_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "project_root": str(project),
        "state": "running",
        "status_path": str(status_path),
        "started_at": ledger.utc_now(),
    }
    if worker_pid is not None:
        record["worker_pid"] = worker_pid
    if worker_identity is not None:
        record["worker_identity"] = worker_identity
    ledger.write_record(record)
    return record


def _mark_attempt_running(project: Path, dispatch_id: str, worker_instance: dict) -> None:
    authority = journal.open_or_create_journal(project)
    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None, prepared
    started = authority.start_attempt(prepared.value.attempt_id, prepared.value.launch_token)
    assert started.committed and started.value is not None, started
    running = authority.mark_attempt_running(
        started.value.attempt_id,
        started.value.launch_token,
        launch_epoch=started.value.launch_epoch,
        worker_instance=worker_instance,
    )
    assert running.committed, running


def _ledger_row(dispatch_id: str) -> dict:
    return json.loads(ledger.record_path(dispatch_id).read_text(encoding="utf-8"))


def _overrule_items(project: Path) -> list[dict]:
    return [
        item
        for item in journal.Journal(project).attention_items()
        if item.get("item_type") == "sidecar_terminal_overruled"
    ]


def _reconcile(project: Path) -> dict:
    return ledger.reconcile_terminal_outbox(
        project,
        messages_dir=Path(os.environ["GOALFLIGHT_MESSAGES_DIR"]),
    )


def test_terminal_sidecars_hold_for_identity_live_worker_then_converge(
    tmp_path: Path, spawn_worker: SpawnWorker
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    worker = spawn_worker()
    identity = ledger.process_identity(worker.pid)
    assert identity, "precondition: live child has a process identity"

    sidecars = {"failed": "error", "idle_timeout": "idle_timeout", "complete": "complete"}
    for sidecar_state in sidecars:
        dispatch_id = f"live-{sidecar_state}"
        _mark_attempt_running(project, dispatch_id, identity)
        status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
        _write_sidecar(
            status_path,
            dispatch_id=dispatch_id,
            state=sidecar_state,
            worker_pid=worker.pid,
            reason="status_write_error:simulated" if sidecar_state == "failed" else None,
        )
        record = _write_ledger_record(
            project,
            dispatch_id=dispatch_id,
            status_path=status_path,
            worker_pid=worker.pid,
            worker_identity=identity,
        )
        assert ledger.worker_identity_liveness(record) == ("live", "live")

    held = _reconcile(project)
    assert held["committed"] == 0, held
    assert held["projected"] == 0, held
    overruled = {entry["dispatch_id"]: entry for entry in held["overruled"]}
    assert set(overruled) == {f"live-{state}" for state in sidecars}, held
    for sidecar_state, terminal_state in sidecars.items():
        dispatch_id = f"live-{sidecar_state}"
        entry = overruled[dispatch_id]
        assert entry["liveness"] == "live", entry
        assert entry["liveness_reason"] == "live", entry
        assert entry["sidecar_state"] == sidecar_state, entry
        assert entry["terminal_state"] == terminal_state, entry
        assert entry["worker_pid"] == worker.pid, entry
        assert entry["attention_item_id"], entry
        row = _ledger_row(dispatch_id)
        assert row["state"] == "running", row
        assert row.get("terminal_state") in (None, "", "unknown"), row
        attempt = journal.Journal(project).attempt_for_dispatch(dispatch_id)
        assert attempt is not None
        assert attempt.lifecycle_state == journal.ATTEMPT_RUNNING, attempt

    items = _overrule_items(project)
    assert len(items) == len(sidecars), items
    for item in items:
        assert item["state"] == "OPEN", item
        assert item["reason"] == "worker_identity_live", item
        payload = json.loads(str(item["payload_json"]))
        assert payload["liveness"] == "live", payload
        assert payload["type"] == "sidecar_terminal_overruled", payload

    # A repeat reconcile re-surfaces the same disagreement but must not stack
    # duplicate attention items: the deterministic item id dedupes.
    repeat = _reconcile(project)
    assert repeat["committed"] == 0, repeat
    assert len(repeat["overruled"]) == len(sidecars), repeat
    assert len(_overrule_items(project)) == len(sidecars)

    # Once the worker is genuinely gone, the same sidecars terminalize exactly
    # as before: the hold is a hold, not a swallow.
    worker.terminate()
    worker.wait(timeout=10)
    assert ledger.process_identity(worker.pid) is None, "precondition: worker reaped"
    converged = _reconcile(project)
    assert converged["committed"] == len(sidecars), converged
    assert converged["projected"] == len(sidecars), converged
    assert converged["overruled"] == [], converged
    for sidecar_state, terminal_state in sidecars.items():
        dispatch_id = f"live-{sidecar_state}"
        row = _ledger_row(dispatch_id)
        assert row["state"] == sidecar_state, row
        assert row["terminal_state"] == terminal_state, row
        assert row["worker_still_alive"] is False, row
        attempt = journal.Journal(project).attempt_for_dispatch(dispatch_id)
        assert attempt is not None
        assert attempt.lifecycle_state == journal.ATTEMPT_TERMINAL, attempt


def test_terminal_sidecar_terminalizes_genuinely_dead_worker(
    tmp_path: Path, spawn_worker: SpawnWorker
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    worker = spawn_worker()
    identity = ledger.process_identity(worker.pid)
    assert identity, "precondition: live child has a process identity"
    dispatch_id = "dead-worker"
    _mark_attempt_running(project, dispatch_id, identity)
    status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
    _write_sidecar(
        status_path,
        dispatch_id=dispatch_id,
        state="failed",
        worker_pid=worker.pid,
        reason="status_write_error:simulated",
    )
    record = _write_ledger_record(
        project,
        dispatch_id=dispatch_id,
        status_path=status_path,
        worker_pid=worker.pid,
        worker_identity=identity,
    )
    worker.terminate()
    worker.wait(timeout=10)
    assert ledger.worker_identity_liveness(record) == ("dead", "dead")

    first = _reconcile(project)
    assert first["committed"] == 1, first
    assert first["projected"] == 1, first
    assert first["overruled"] == [], first
    assert _overrule_items(project) == []
    row = _ledger_row(dispatch_id)
    assert row["state"] == "failed", row
    assert row["terminal_state"] == "error", row
    assert row["worker_still_alive"] is False, row
    attempt = journal.Journal(project).attempt_for_dispatch(dispatch_id)
    assert attempt is not None
    assert attempt.lifecycle_state == journal.ATTEMPT_TERMINAL, attempt

    second = _reconcile(project)
    assert second["committed"] == 0, second
    assert second["already_terminal"] == 1, second
    assert second["overruled"] == [], second


def test_terminal_sidecar_holds_when_liveness_unknown(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    dispatch_id = "unknown-liveness"
    status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
    _write_sidecar(
        status_path,
        dispatch_id=dispatch_id,
        state="failed",
        worker_pid=None,
        reason="status_write_error:simulated",
    )
    # No journal attempt, no recorded pid or identity: nothing to compare
    # against the process table, so liveness is UNKNOWN, never dead.
    record = _write_ledger_record(project, dispatch_id=dispatch_id, status_path=status_path)
    assert ledger.worker_identity_liveness(record) == ("unknown", "no_pid")

    held = _reconcile(project)
    assert held["committed"] == 0, held
    assert len(held["overruled"]) == 1, held
    entry = held["overruled"][0]
    assert entry["dispatch_id"] == dispatch_id, entry
    assert entry["liveness"] == "unknown", entry
    assert entry["liveness_reason"] == "no_pid", entry
    assert entry["worker_pid"] is None, entry
    assert entry["attention_item_id"], entry
    row = _ledger_row(dispatch_id)
    assert row["state"] == "running", row
    assert row.get("terminal_state") in (None, "", "unknown"), row
    # The hold creates nothing: no attempt, no terminal transition.
    assert journal.Journal(project).attempt_for_dispatch(dispatch_id) is None

    items = _overrule_items(project)
    assert len(items) == 1, items
    item = items[0]
    # UNKNOWN is distinguishable from LIVE without parsing payload_json.
    assert item["reason"] == "worker_identity_unknown", item
    payload = json.loads(str(item["payload_json"]))
    assert payload["liveness"] == "unknown", payload
    assert payload["liveness_reason"] == "no_pid", payload


def test_pid_reused_by_different_process_is_dead_not_live(
    tmp_path: Path, spawn_worker: SpawnWorker
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    original = spawn_worker()
    occupant = spawn_worker()
    recorded_identity = ledger.process_identity(original.pid)
    assert recorded_identity, "precondition: original child has a process identity"
    # The pid-reuse shape: the recorded pid is now occupied by a process whose
    # start token is not the recorded one. Both processes are real; only the
    # re-keying stands in for the OS recycling the pid number.
    recorded_identity["pid"] = occupant.pid
    assert ledger.process_identity(occupant.pid) is not None, (
        "precondition: a process exists at the pid -- a bare pid check would say live"
    )

    dispatch_id = "pid-reused"
    _mark_attempt_running(project, dispatch_id, recorded_identity)
    status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
    _write_sidecar(
        status_path,
        dispatch_id=dispatch_id,
        state="complete",
        worker_pid=occupant.pid,
    )
    record = _write_ledger_record(
        project,
        dispatch_id=dispatch_id,
        status_path=status_path,
        worker_pid=occupant.pid,
        worker_identity=recorded_identity,
    )
    liveness, liveness_reason = ledger.worker_identity_liveness(record)
    assert liveness == "dead", (liveness, liveness_reason)
    assert liveness_reason.startswith("pid_reused_"), liveness_reason

    result = _reconcile(project)
    assert result["committed"] == 1, result
    assert result["overruled"] == [], result
    assert _overrule_items(project) == []
    row = _ledger_row(dispatch_id)
    assert row["state"] == "complete", row
    assert row["terminal_state"] == "complete", row
    attempt = journal.Journal(project).attempt_for_dispatch(dispatch_id)
    assert attempt is not None
    assert attempt.lifecycle_state == journal.ATTEMPT_TERMINAL, attempt


def test_running_sidecar_is_neither_promoted_nor_flagged(
    tmp_path: Path, spawn_worker: SpawnWorker
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    worker = spawn_worker()
    identity = ledger.process_identity(worker.pid)
    assert identity, "precondition: live child has a process identity"
    dispatch_id = "running-sidecar"
    _mark_attempt_running(project, dispatch_id, identity)
    status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
    _write_sidecar(status_path, dispatch_id=dispatch_id, state="running", worker_pid=worker.pid)
    _write_ledger_record(
        project,
        dispatch_id=dispatch_id,
        status_path=status_path,
        worker_pid=worker.pid,
        worker_identity=identity,
    )

    result = _reconcile(project)
    assert result["committed"] == 0, result
    assert result["overruled"] == [], result
    assert _overrule_items(project) == []
    row = _ledger_row(dispatch_id)
    assert row["state"] == "running", row
    attempt = journal.Journal(project).attempt_for_dispatch(dispatch_id)
    assert attempt is not None
    assert attempt.lifecycle_state == journal.ATTEMPT_RUNNING, attempt
