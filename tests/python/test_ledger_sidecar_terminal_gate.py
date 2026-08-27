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
direction, so doubt resolves against the write). Unknown is not a leak: each
reconcile re-probes (the record may gain a pid from a RUNNING journal
instance, or the process table may become readable). Age is never the
resolver. Still-unknown stays held and is named ``held: unknown`` on status
and fleet, distinct from ``held: live``. Once identity is dead the sidecar
terminalizes exactly as before, and the OPEN overrule item is resolved.

Pre-reconcile identity checks use ``identity_matches`` (the helper that
existed before this gate) so a revert of the gate fails on ``committed`` /
ledger state, not ``AttributeError: worker_identity_liveness``.

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

import goalflight_fleet_console as fleet  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_status as status  # noqa: E402


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


def _overrule_items(project: Path, *, state: str = "OPEN") -> list[dict]:
    return [
        item
        for item in journal.Journal(project).attention_items(state=state)
        if item.get("item_type") == "sidecar_terminal_overruled"
    ]


def _identity_precondition(record: dict) -> tuple[bool, str]:
    """Identity check that existed before the sidecar-terminal gate helper.

    Tests must not call ``worker_identity_liveness`` before the behavioral
    pin: on revert of the gate that helper is missing and the failure becomes
    AttributeError instead of committed-over-live-worker.
    """
    return ledger.identity_matches(record)


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
        matched, reason = _identity_precondition(record)
        assert matched and reason == "live", (matched, reason)

    held = _reconcile(project)
    assert held["committed"] == 0, held
    assert held["projected"] == 0, held
    overruled = {entry["dispatch_id"]: entry for entry in held.get("overruled", [])}
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
    assert len(repeat.get("overruled", [])) == len(sidecars), repeat
    assert len(_overrule_items(project)) == len(sidecars)

    # Once the worker is genuinely gone, the same sidecars terminalize exactly
    # as before: the hold is a hold, not a swallow.
    worker.terminate()
    worker.wait(timeout=10)
    assert ledger.process_identity(worker.pid) is None, "precondition: worker reaped"
    converged = _reconcile(project)
    assert converged["committed"] == len(sidecars), converged
    assert converged["projected"] == len(sidecars), converged
    assert converged.get("overruled", []) == [], converged
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
    matched, reason = _identity_precondition(record)
    assert not matched and reason == "dead", (matched, reason)

    first = _reconcile(project)
    assert first["committed"] == 1, first
    assert first["projected"] == 1, first
    assert first.get("overruled", []) == [], first
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
    assert second.get("overruled", []) == [], second


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
    matched, reason = _identity_precondition(record)
    assert not matched and reason == "no_pid", (matched, reason)

    held = _reconcile(project)
    assert held["committed"] == 0, held
    assert len(held.get("overruled", [])) == 1, held
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
    assert "not resolved by age" in str(payload.get("text") or ""), payload
    stamped = _ledger_row(dispatch_id)
    assert stamped.get("sidecar_hold") == "unknown", stamped
    assert stamped.get("sidecar_hold_reason") == "no_pid", stamped


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
    matched, liveness_reason = _identity_precondition(record)
    assert not matched, (matched, liveness_reason)
    assert liveness_reason.startswith("pid_reused_"), liveness_reason

    result = _reconcile(project)
    assert result["committed"] == 1, result
    assert result.get("overruled", []) == [], result
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
    assert result.get("overruled", []) == [], result
    assert _overrule_items(project) == []
    row = _ledger_row(dispatch_id)
    assert row["state"] == "running", row
    attempt = journal.Journal(project).attempt_for_dispatch(dispatch_id)
    assert attempt is not None
    assert attempt.lifecycle_state == journal.ATTEMPT_RUNNING, attempt


def test_unknown_hold_does_not_terminalize_while_still_indeterminate(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    dispatch_id = "unknown-still-indeterminate"
    status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
    _write_sidecar(
        status_path,
        dispatch_id=dispatch_id,
        state="failed",
        worker_pid=None,
        reason="status_write_error:simulated",
    )
    _write_ledger_record(project, dispatch_id=dispatch_id, status_path=status_path)

    first = _reconcile(project)
    assert first["committed"] == 0, first
    row = _ledger_row(dispatch_id)
    assert row["state"] == "running", row
    assert row.get("sidecar_hold") == "unknown", row
    assert row.get("sidecar_hold_reason") == "no_pid", row

    second = _reconcile(project)
    assert second["committed"] == 0, second
    row = _ledger_row(dispatch_id)
    assert row["state"] == "running", row
    assert row.get("terminal_state") in (None, "", "unknown"), row
    assert row.get("sidecar_hold") == "unknown", row
    assert journal.Journal(project).attempt_for_dispatch(dispatch_id) is None


def test_unknown_hold_resolves_when_running_journal_identity_is_dead(
    tmp_path: Path, spawn_worker: SpawnWorker
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    dispatch_id = "unknown-then-dead"
    status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
    _write_sidecar(
        status_path,
        dispatch_id=dispatch_id,
        state="failed",
        worker_pid=None,
        reason="status_write_error:simulated",
    )
    record = _write_ledger_record(project, dispatch_id=dispatch_id, status_path=status_path)
    matched, reason = _identity_precondition(record)
    assert not matched and reason == "no_pid", (matched, reason)

    held = _reconcile(project)
    assert held["committed"] == 0, held
    assert _ledger_row(dispatch_id).get("sidecar_hold") == "unknown"

    worker = spawn_worker()
    identity = ledger.process_identity(worker.pid)
    assert identity, "precondition: live child has a process identity"
    worker.terminate()
    worker.wait(timeout=10)
    assert ledger.process_identity(worker.pid) is None, "precondition: worker reaped"
    _mark_attempt_running(project, dispatch_id, identity)

    converged = _reconcile(project)
    assert converged["committed"] == 1, converged
    assert converged.get("overruled", []) == [], converged
    row = _ledger_row(dispatch_id)
    assert row["state"] == "failed", row
    assert row["terminal_state"] == "error", row
    assert "sidecar_hold" not in row, row
    attempt = journal.Journal(project).attempt_for_dispatch(dispatch_id)
    assert attempt is not None
    assert attempt.lifecycle_state == journal.ATTEMPT_TERMINAL, attempt
    assert _overrule_items(project) == []
    resolved = _overrule_items(project, state="RESOLVED")
    assert resolved, resolved
    assert all(item["state"] == "RESOLVED" for item in resolved), resolved


def test_held_unknown_is_visible_in_status_and_fleet_distinct_from_held_live(
    tmp_path: Path, spawn_worker: SpawnWorker
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    worker = spawn_worker()
    identity = ledger.process_identity(worker.pid)
    assert identity, "precondition: live child has a process identity"

    live_id = "held-live-visible"
    unknown_id = "held-unknown-visible"
    live_status = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{live_id}.status.json"
    unknown_status = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{unknown_id}.status.json"
    _mark_attempt_running(project, live_id, identity)
    _write_sidecar(live_status, dispatch_id=live_id, state="failed", worker_pid=worker.pid)
    _write_ledger_record(
        project,
        dispatch_id=live_id,
        status_path=live_status,
        worker_pid=worker.pid,
        worker_identity=identity,
    )
    _write_sidecar(
        unknown_status,
        dispatch_id=unknown_id,
        state="failed",
        worker_pid=None,
        reason="status_write_error:simulated",
    )
    _write_ledger_record(project, dispatch_id=unknown_id, status_path=unknown_status)

    held = _reconcile(project)
    assert held["committed"] == 0, held

    live_cells = status._dispatch_cells(  # noqa: SLF001
        status.reconcile_fast_plane_record(_ledger_row(live_id))
    )
    unknown_cells = status._dispatch_cells(  # noqa: SLF001
        status.reconcile_fast_plane_record(_ledger_row(unknown_id))
    )
    assert "held: live" in live_cells, live_cells
    assert "held: unknown" not in live_cells, live_cells
    assert "held: unknown" in unknown_cells, unknown_cells
    assert "held: live" not in unknown_cells, unknown_cells
    assert "no_pid" in unknown_cells, unknown_cells

    live_fleet = fleet._worker_row(_ledger_row(live_id))  # noqa: SLF001
    unknown_fleet = fleet._worker_row(_ledger_row(unknown_id))  # noqa: SLF001
    assert live_fleet["authority_resolution"] == "held: live", live_fleet
    assert unknown_fleet["authority_resolution"] == "held: unknown", unknown_fleet
    assert live_fleet["is_terminal"] is False, live_fleet
    assert unknown_fleet["is_terminal"] is False, unknown_fleet
    assert "held: live" in str(live_fleet.get("authority_detail") or ""), live_fleet
    assert "held: unknown" in str(unknown_fleet.get("authority_detail") or ""), unknown_fleet
    payload = ledger.status_payload()
    by_id = {row["dispatch_id"]: row for row in payload["records"]}
    assert by_id[live_id].get("sidecar_hold") == "live", by_id[live_id]
    assert by_id[unknown_id].get("sidecar_hold") == "unknown", by_id[unknown_id]


def test_sidecar_overrule_attention_resolves_after_hold_converges(
    tmp_path: Path, spawn_worker: SpawnWorker
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    worker = spawn_worker()
    identity = ledger.process_identity(worker.pid)
    assert identity, "precondition: live child has a process identity"
    dispatch_id = "overrule-then-resolve"
    _mark_attempt_running(project, dispatch_id, identity)
    status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
    _write_sidecar(
        status_path, dispatch_id=dispatch_id, state="idle_timeout", worker_pid=worker.pid
    )
    _write_ledger_record(
        project,
        dispatch_id=dispatch_id,
        status_path=status_path,
        worker_pid=worker.pid,
        worker_identity=identity,
    )

    held = _reconcile(project)
    assert held["committed"] == 0, held
    open_items = _overrule_items(project)
    assert len(open_items) == 1, open_items
    assert open_items[0]["state"] == "OPEN", open_items[0]
    item_id = open_items[0]["item_id"]

    worker.terminate()
    worker.wait(timeout=10)
    converged = _reconcile(project)
    assert converged["committed"] == 1, converged
    assert _overrule_items(project) == []
    resolved = _overrule_items(project, state="RESOLVED")
    assert len(resolved) == 1, resolved
    assert resolved[0]["item_id"] == item_id, resolved
    assert resolved[0]["resolved_at"], resolved[0]
