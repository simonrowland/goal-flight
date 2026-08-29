#!/usr/bin/env python3
"""Claim recovery wall time must not scale with stale unresolvable carriers.

Production drain spent 63% of every pass in ``_recover_claimed_queue_entries``
(~10s per dead-claimer carrier). Two costs stacked:

1. ``_find_dispatch_record`` scanned the whole ledger (2500+ JSON files) on
   every lookup, ~16 times per carrier.
2. Fail-closed unlinked corpses (SC-153) were correctly kept, then fully
   re-probed every ~60s, so pass cost grew with the corpse set forever.

Revert-failure: putting ``read_records()`` back inside ``_find_dispatch_record``
makes ``read_records_n`` scale with N_claimed. Dropping the durable
``recovery_probe`` stamp makes the second pass re-enter
``_reconcile_claim_transaction`` for every corpse.

Tests isolate state under pytest's tmp_path. They never touch the live shared
queue at /tmp/goal-flight-501/dispatch-queue.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="recovery-cost tests spawn POSIX helper processes",
)

N_LEDGER_EXTRA = 80
N_SMALL = 4
N_LARGE = 12
# Before the O(1) lookup, 12 carriers * ~16 full scans of 80 rows was seconds.
# After, both sizes should finish well under this ceiling.
RECOVERY_CEILING_S = 6.0


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(state / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER", str(tmp_path / "wake-ledger.json"))
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)
    D._FLOCK_CAPABILITY_CACHE.clear()
    D._FS_IDENTITY_CACHE.clear()
    D._MOUNT_TABLE_CACHE = None
    D._PASS_CACHE = None


def _git_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=project,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return project


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


def _dead_identity() -> tuple[int, dict]:
    proc = _spawn_sleeping_worker()
    try:
        identity = ledger.process_identity(proc.pid)
        pid = proc.pid
    finally:
        _reap(proc)
    assert identity and identity.get("start_token"), identity
    return pid, identity


def _queue_dir(tmp_path: Path) -> Path:
    queue = tmp_path / "state" / "dispatch-queue"
    queue.mkdir(parents=True, exist_ok=True)
    return queue


def _age_iso(age_s: float) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - age_s)
    )


def _write_extra_ledger(project: Path, n: int, created_iso: str) -> None:
    for i in range(n):
        ledger.write_record(
            {
                "schema": ledger.SCHEMA,
                "dispatch_id": f"extra-{i:04d}",
                "agent": "codex",
                "engine": "codex",
                "shape": "bash",
                "transport": "dispatch",
                "project_root": str(project),
                "state": "complete",
                "terminal_state": "complete",
                "started_at": created_iso,
            }
        )


def _write_unlinked_dead_claim(
    queue: Path,
    project: Path,
    dispatch_id: str,
    *,
    pid: int,
    identity: dict,
    age_s: float,
    tmp_path: Path,
) -> Path:
    created_iso = _age_iso(age_s)
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("worker output stopped without a verdict\n", encoding="utf-8")
    entry = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "claimed",
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "shape": "bash",
        "project_root": str(project),
        "dispatch_argv": ["--agent", "test-dispatch", "--dispatch-id", dispatch_id],
        "queue_launch_token": f"{dispatch_id}-token",
        "queue_launch_started": True,
        "queue_worker_spawn_intent": True,
        "queue_worker_spawned_at": created_iso,
        "queue_claimer_pid": pid,
        "queue_claimer_identity": identity,
        "queue_launcher_pid": pid,
        "queue_launcher_identity": identity,
        "queue_worker_pid": pid,
        "queue_worker_identity": identity,
        "created_at": created_iso,
        "orphan_first_seen_at": created_iso,
        "request": {
            "cwd": str(project),
            "shape": "bash",
            "tail": str(tail),
        },
    }
    claim = queue / f"{dispatch_id}.json.claimed-{pid}-1"
    claim.write_text(json.dumps(entry), encoding="utf-8")
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "engine": "codex",
            "shape": "bash",
            "transport": "dispatch",
            "project_root": str(project),
            "worker_pid": pid,
            "worker_identity": identity,
            "stdout_path": str(tail),
            "state": "running",
            "terminal_state": "unknown",
            "queue_launch_token": f"{dispatch_id}-token",
            "started_at": created_iso,
        }
    )
    return claim


def _recover(queue: Path, **kwargs: object) -> dict:
    return D._recover_claimed_queue_entries(queue, stale_s=0.0, **kwargs)


def test_find_dispatch_record_is_direct_lookup_not_a_full_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _git_project(tmp_path)
    created_iso = _age_iso(400)
    _write_extra_ledger(project, N_LEDGER_EXTRA, created_iso)
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": "target-row",
            "agent": "codex",
            "state": "running",
            "project_root": str(project),
        }
    )
    calls = {"n": 0}
    real = ledger.read_records

    def wrapped() -> list[dict]:
        calls["n"] += 1
        return real()

    monkeypatch.setattr(ledger, "read_records", wrapped)
    found = D._find_dispatch_record("target-row")
    missing = D._find_dispatch_record("no-such-dispatch")
    assert found is not None and found.get("dispatch_id") == "target-row"
    assert missing is None
    assert calls["n"] == 0, (
        "revert-failure: _find_dispatch_record scanned read_records(); "
        f"calls={calls['n']}"
    )


def test_read_record_unreadable_is_not_absent(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": "corrupt-row",
            "agent": "codex",
            "state": "running",
            "project_root": str(project),
        }
    )
    path = ledger.record_path("corrupt-row", create=False)
    path.write_text("{not-json", encoding="utf-8")
    record = ledger.read_record("corrupt-row")
    assert ledger.record_is_unreadable(record), record
    assert D._find_dispatch_record("corrupt-row") == record
    assert ledger.read_record("absent-row") is None


def test_recovery_does_not_scan_ledger_once_per_carrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue_dir(tmp_path)
    project = _git_project(tmp_path)
    pid, identity = _dead_identity()
    created_iso = _age_iso(4000)
    _write_extra_ledger(project, N_LEDGER_EXTRA, created_iso)
    for i in range(N_LARGE):
        _write_unlinked_dead_claim(
            queue,
            project,
            f"stale-{i:02d}",
            pid=pid,
            identity=identity,
            age_s=4000,
            tmp_path=tmp_path,
        )
    calls = {"n": 0}
    real = ledger.read_records

    def wrapped() -> list[dict]:
        calls["n"] += 1
        return real()

    monkeypatch.setattr(ledger, "read_records", wrapped)
    result = _recover(queue, restore_ledger_orphans=True)
    assert result["pending_launch"] >= N_LARGE, result
    assert calls["n"] <= 2, (
        "revert-failure: recovery scanned read_records per carrier; "
        f"calls={calls['n']} claimed={N_LARGE} ledger_extra={N_LEDGER_EXTRA}"
    )


def test_recovery_wall_time_does_not_scale_with_stale_carriers(
    tmp_path: Path,
) -> None:
    queue = _queue_dir(tmp_path)
    project = _git_project(tmp_path)
    pid, identity = _dead_identity()
    created_iso = _age_iso(4000)
    _write_extra_ledger(project, N_LEDGER_EXTRA, created_iso)
    times: dict[int, float] = {}
    for n in (N_SMALL, N_LARGE):
        for path in queue.glob("*.claimed-*"):
            path.unlink()
        for i in range(n):
            _write_unlinked_dead_claim(
                queue,
                project,
                f"stale-{n}-{i:02d}",
                pid=pid,
                identity=identity,
                age_s=4000,
                tmp_path=tmp_path,
            )
        t0 = time.monotonic()
        result = _recover(queue, restore_ledger_orphans=False)
        elapsed = time.monotonic() - t0
        times[n] = elapsed
        assert result["pending_launch"] == n, result
        assert result["restored"] == 0, result
        assert result["cleared"] == 0, result
        assert elapsed < RECOVERY_CEILING_S, (
            f"recovery_s={elapsed:.3f} for N={n} exceeded {RECOVERY_CEILING_S}s; "
            f"timing={result.get('timing')}"
        )
    # Second size must not look like O(N * ledger). A 3x carrier count with
    # a fat ledger used to be ~3x wall; skip+O(1) lookup keeps it in a band.
    assert times[N_LARGE] < RECOVERY_CEILING_S
    assert times[N_SMALL] < RECOVERY_CEILING_S


def test_unresolvable_dead_claimer_skips_second_pass_reprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue_dir(tmp_path)
    project = _git_project(tmp_path)
    pid, identity = _dead_identity()
    claim = _write_unlinked_dead_claim(
        queue,
        project,
        "corpse",
        pid=pid,
        identity=identity,
        age_s=4000,
        tmp_path=tmp_path,
    )
    first = _recover(queue, restore_ledger_orphans=False)
    assert first["pending_launch"] == 1, first
    assert first["restored"] == 0 and first["cleared"] == 0
    assert any(
        row.get("reason") == "unlinked_quarantine_deferred"
        for row in first.get("pending_reasons") or []
    ), first
    parked = json.loads(claim.read_text(encoding="utf-8"))
    probe = parked.get("recovery_probe")
    assert isinstance(probe, dict), parked
    assert probe.get("disposition") == "keep_unresolved"
    assert probe.get("reason") == "unlinked_quarantine_deferred"
    assert first.get("timing", {}).get("probe_stamp_n") == 1

    calls = {"n": 0}
    real = D._reconcile_claim_transaction

    def wrapped(*args: object, **kwargs: object):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(D, "_reconcile_claim_transaction", wrapped)
    second = _recover(queue, restore_ledger_orphans=False)
    assert second["pending_launch"] == 1, second
    assert second["restored"] == 0 and second["cleared"] == 0
    assert claim.exists()
    assert calls["n"] == 0, (
        "revert-failure: second pass re-entered _reconcile_claim_transaction "
        f"calls={calls['n']} timing={second.get('timing')}"
    )
    assert second.get("timing", {}).get("skip_n") == 1
    assert any(
        row.get("recovery_probe_skipped")
        for row in second.get("pending_reasons") or []
    ), second


def test_live_worker_carrier_is_never_restored(tmp_path: Path) -> None:
    queue = _queue_dir(tmp_path)
    project = _git_project(tmp_path)
    proc = _spawn_sleeping_worker()
    try:
        identity = ledger.process_identity(proc.pid)
        assert identity and identity.get("start_token"), identity
        created_iso = _age_iso(400)
        dispatch_id = "live-worker"
        tail = tmp_path / f"{dispatch_id}.tail"
        tail.write_text("still running\n", encoding="utf-8")
        entry = {
            "schema": D.DISPATCH_QUEUE_SCHEMA,
            "state": "claimed",
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "shape": "bash",
            "project_root": str(project),
            "dispatch_argv": ["--agent", "test-dispatch"],
            "queue_launch_token": "live-token",
            "queue_claimer_pid": proc.pid,
            "queue_claimer_identity": identity,
            "queue_launcher_pid": proc.pid,
            "queue_launcher_identity": identity,
            "queue_worker_pid": proc.pid,
            "queue_worker_identity": identity,
            "queue_worker_spawn_intent": True,
            "queue_worker_spawned_at": created_iso,
            "created_at": created_iso,
            "request": {"cwd": str(project), "tail": str(tail)},
        }
        claim = queue / f"{dispatch_id}.json.claimed-{proc.pid}-1"
        claim.write_text(json.dumps(entry), encoding="utf-8")
        ledger.write_record(
            {
                "schema": ledger.SCHEMA,
                "dispatch_id": dispatch_id,
                "agent": "codex",
                "engine": "codex",
                "shape": "bash",
                "transport": "dispatch",
                "project_root": str(project),
                "worker_pid": proc.pid,
                "worker_identity": identity,
                "stdout_path": str(tail),
                "state": "running",
                "terminal_state": "unknown",
                "queue_launch_token": "live-token",
                "started_at": created_iso,
            }
        )
        result = _recover(queue, restore_ledger_orphans=False)
        assert result["restored"] == 0, result
        queued = queue / f"{dispatch_id}.json"
        assert not queued.exists(), "live worker must not be requeued for relaunch"
        record = D._find_dispatch_record(dispatch_id)
        assert record is not None
        assert str(record.get("state") or "") == "running"
        assert not D._dispatch_record_is_terminal(record)
        assert result.get("timing", {}).get("skip_n", 0) == 0
    finally:
        _reap(proc)


def test_unreadable_queue_still_refuses_the_pass(tmp_path: Path) -> None:
    queue = _queue_dir(tmp_path)
    project = _git_project(tmp_path)
    pid, identity = _dead_identity()
    claim = _write_unlinked_dead_claim(
        queue,
        project,
        "held-claim",
        pid=pid,
        identity=identity,
        age_s=4000,
        tmp_path=tmp_path,
    )
    original = claim.read_bytes()
    os.chmod(queue, 0o000)
    try:
        recovery = _recover(queue)
        assert str(recovery.get("listing_error") or "").startswith(
            "queue_dir_unreadable"
        ), recovery
        assert recovery["restored"] == 0
        assert recovery["cleared"] == 0
        assert recovery["quarantined"] == 0
        assert recovery["ledger_terminalized"] == 0
    finally:
        os.chmod(queue, 0o700)
    assert claim.read_bytes() == original


def test_identity_change_invalidates_recovery_skip(tmp_path: Path) -> None:
    queue = _queue_dir(tmp_path)
    project = _git_project(tmp_path)
    pid, identity = _dead_identity()
    claim = _write_unlinked_dead_claim(
        queue,
        project,
        "mutated",
        pid=pid,
        identity=identity,
        age_s=4000,
        tmp_path=tmp_path,
    )
    first = _recover(queue, restore_ledger_orphans=False)
    assert first.get("timing", {}).get("probe_stamp_n") == 1
    parked = json.loads(claim.read_text(encoding="utf-8"))
    parked["queue_launch_token"] = "different-token"
    claim.write_text(json.dumps(parked), encoding="utf-8")
    second = _recover(queue, restore_ledger_orphans=False)
    assert second.get("timing", {}).get("skip_n", 0) == 0, second
    assert second["pending_launch"] == 1
    assert claim.exists()
