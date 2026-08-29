#!/usr/bin/env python3
"""Drain pass wall time must not scale with un-launchable queue depth.

A pass used to call blocking ``subprocess.run`` per entry with a 45s
launch-confirmation timeout. N hung entries made the pass take ~N*45s even
when vendor capacity was empty. Queuing is justified by capacity pressure,
not by serial launch-confirmation waits.

Launches stay serial: completion authority on the launch path is a ledger
read, not a lock, and same-task double-spawn is a confirmed TOCTOU if two
children are in flight before either records running. The pass instead bounds
total launch wait and backs off entries that just timed out.

A slow non-timeout failure at a stable FIFO head used to burn the pass
budget without a backoff stamp, so the same dead letter was retried every
pass and the healthy tail never started. Tests below lock that hole.

Tests isolate state under pytest's tmp_path. They never touch the live shared
queue at /tmp/goal-flight-501/dispatch-queue.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import goalflight_capacity as cap  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="drain launch-budget tests launch POSIX queue helpers",
)

_REAL_SUBPROCESS_RUN = subprocess.run
HANG_S = 0.45
N_HUNG = 4
# One confirmation wait plus restore, not N waits. Restore of a timed-out
# claim is ~1s; N serial hangs on 2d88c4f measured 3.49s.
SERIAL_CEILING_S = HANG_S * N_HUNG + 0.8
# Probe D: front sleeps past the pass budget then returns rc=2. Sleep must
# meet or exceed the budget so leftover remaining_s cannot start the tail
# even if restore is cheap. Revert-failure on 2c160d8: healthy is attempted
# on neither pass.
SLOW_FAIL_S = 0.50
STARVE_BUDGET_S = 0.40
BOUNDED_PASSES = 3


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
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_release_stale_capacity_for_drain", lambda: None)
    monkeypatch.setattr(D, "_run_drain_prelaunch_hook", lambda _agents: None)


def _drain_args(queue: Path, **extra: object) -> argparse.Namespace:
    ns = argparse.Namespace(
        queue_dir=str(queue),
        capacity_wait_s=0.0,
        claim_stale_s=D.QUEUE_CLAIM_STALE_S,
        limit=0,
    )
    for key, value in extra.items():
        setattr(ns, key, value)
    return ns


def _queue_dir(tmp_path: Path) -> Path:
    queue = tmp_path / "state" / "dispatch-queue"
    queue.mkdir(parents=True, exist_ok=True)
    return queue


def _write_entry(
    queue: Path,
    dispatch_id: str,
    *,
    project_root: Path,
    created_at: str,
    task_ids: list[str] | None = None,
    worker_code: str | None = None,
) -> Path:
    path = queue / f"{dispatch_id}.json"
    code = worker_code or (
        f"print('COMPLETE: {dispatch_id} — drain launch serial test')"
    )
    argv = [
        "--agent",
        "test-dispatch",
        "--dispatch-id",
        dispatch_id,
        "--cwd",
        str(project_root),
        "--tail",
        str(project_root / f"{dispatch_id}.tail"),
        "--status-json",
        str(project_root / f"{dispatch_id}.status.json"),
        "--unregistered-forced",
        "--occupied-worktree-forced",
    ]
    if task_ids:
        argv.extend(["--task", ",".join(task_ids)])
    argv.extend(["--", sys.executable, "-c", code])
    payload = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "queued",
        "dispatch_id": dispatch_id,
        "agent": "test-dispatch",
        "shape": "bash",
        "project_root": str(project_root),
        "process_cwd": str(project_root),
        "created_at": created_at,
        "updated_at": created_at,
        "queue_path": str(path),
        "dispatch_argv": argv,
        "request": {
            "agent": "test-dispatch",
            "cwd": str(project_root),
            "tail": str(project_root / f"{dispatch_id}.tail"),
            "status_json": str(project_root / f"{dispatch_id}.status.json"),
        },
    }
    if task_ids:
        payload["task_ids"] = list(task_ids)
        payload["request"]["task_ids"] = list(task_ids)
    D._write_json_atomic(path, payload)
    return path


def _is_drain_child(argv: list[object]) -> bool:
    return any(str(part).endswith("goalflight_dispatch.py") for part in argv[:3])


def _hanging_run(argv, *args, **kwargs):
    argv_list = list(argv)
    if _is_drain_child(argv_list):
        time.sleep(HANG_S)
        raise subprocess.TimeoutExpired(argv_list, kwargs.get("timeout") or HANG_S)
    return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)


def _launched_run_factory(tmp_path: Path):
    def fake_run(argv, *args, **kwargs):
        argv_list = list(argv)
        if not _is_drain_child(argv_list):
            return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
        try:
            dispatch_id = argv_list[argv_list.index("--dispatch-id") + 1]
            token = argv_list[argv_list.index("--queue-launch-token") + 1]
            cwd = argv_list[argv_list.index("--cwd") + 1]
        except (ValueError, IndexError):
            return subprocess.CompletedProcess(argv_list, 0, stdout="", stderr="")
        ledger.write_record(
            {
                "schema": ledger.SCHEMA,
                "dispatch_id": dispatch_id,
                "agent": "test-dispatch",
                "engine": "test-dispatch",
                "shape": "bash",
                "transport": "dispatch",
                "project_root": cwd,
                "worker_pid": os.getpid(),
                "worker_identity": ledger.process_identity(os.getpid()),
                "stdout_path": str(tmp_path / f"{dispatch_id}.tail"),
                "status_path": str(tmp_path / f"{dispatch_id}.status.json"),
                "state": "running",
                "terminal_state": "unknown",
                "queue_launch_token": token,
                "started_at": ledger.utc_now(),
            }
        )
        return subprocess.CompletedProcess(
            argv_list,
            0,
            stdout=f"DISPATCH-LAUNCHED {dispatch_id}\n",
            stderr="",
        )

    return fake_run


def _reason_for(payload: dict, dispatch_id: str) -> str:
    for row in payload.get("details") or []:
        if row.get("dispatch_id") == dispatch_id:
            return str(row.get("reason") or "")
    return ""


def _launched_ids(payload: dict) -> list[str]:
    return [
        str(row.get("dispatch_id") or "")
        for row in payload.get("details") or []
        if row.get("state") == "launched"
    ]


def _selective_child_run(
    tmp_path: Path,
    *,
    slow_fail_ids: frozenset[str] = frozenset(),
    hang_ids: frozenset[str] = frozenset(),
    attempts: list[str] | None = None,
):
    """Drain-child mock: hang, slow rc=2, or ledger-confirmed launch by id."""
    launch = _launched_run_factory(tmp_path)

    def fake_run(argv, *args, **kwargs):
        argv_list = list(argv)
        if not _is_drain_child(argv_list):
            return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
        try:
            dispatch_id = argv_list[argv_list.index("--dispatch-id") + 1]
        except (ValueError, IndexError):
            dispatch_id = ""
        if attempts is not None:
            attempts.append(dispatch_id)
        if dispatch_id in hang_ids:
            time.sleep(HANG_S)
            raise subprocess.TimeoutExpired(
                argv_list, kwargs.get("timeout") or HANG_S
            )
        if dispatch_id in slow_fail_ids:
            time.sleep(SLOW_FAIL_S)
            return subprocess.CompletedProcess(
                argv_list, 2, stdout="", stderr="slow-fail"
            )
        return launch(argv, *args, **kwargs)

    return fake_run


def _write_front_and_healthy(queue: Path, project: Path) -> tuple[Path, Path]:
    front = _write_entry(
        queue,
        "slow-fail",
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    back = _write_entry(
        queue,
        "healthy-back",
        project_root=project,
        created_at="2026-01-01T00:00:01+00:00",
    )
    return front, back


def test_unlaunchable_entries_do_not_multiply_pass_wall_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N hung launches must not take ~N times one confirmation wait.

    Revert-failure on 2d88c4f: elapsed >= N*hang because each TimeoutExpired
    is waited out serially with no pass budget.
    """
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    for index in range(N_HUNG):
        _write_entry(
            queue,
            f"hung-{index}",
            project_root=project,
            created_at=f"2026-01-01T00:00:0{index}+00:00",
        )
    monkeypatch.setattr(D.subprocess, "run", _hanging_run)
    args = _drain_args(queue, launch_budget_s=HANG_S + 0.25)
    started = time.monotonic()
    payload = D._drain_queue_once(args)
    elapsed = time.monotonic() - started
    assert elapsed < SERIAL_CEILING_S, (
        f"pass wall {elapsed:.3f}s scaled with {N_HUNG} hung entries "
        f"(ceiling {SERIAL_CEILING_S:.3f}s); payload={payload}"
    )
    assert payload["launched"] == 0, payload
    leftover = payload["left_queued"] + payload.get("pending_claims", 0)
    assert leftover >= N_HUNG, payload
    timeouts = int((payload.get("timing") or {}).get("launch_timeouts") or 0)
    # Slack after the first hang used to be eaten by ~1s claim restore (full
    # ledger scan). Direct record lookup made restore cheap, so leftover
    # budget may start one more short hang. N hangs still cannot run.
    assert 1 <= timeouts < N_HUNG, payload
    reasons = [str(row.get("reason") or "") for row in payload.get("details") or []]
    assert reasons.count("launch_timeout_pending_ledger") == timeouts, payload
    assert reasons.count("pass_launch_budget") == N_HUNG - timeouts, payload


def test_healthy_entry_launches_promptly_when_capacity_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    _write_entry(
        queue,
        "healthy-one",
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(D.subprocess, "run", _launched_run_factory(tmp_path))
    started = time.monotonic()
    payload = D._drain_queue_once(_drain_args(queue))
    elapsed = time.monotonic() - started
    assert payload["launched"] == 1, payload
    assert elapsed < 2.0, elapsed


def test_launch_timeout_backs_off_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    path = _write_entry(
        queue,
        "timeout-once",
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(D.subprocess, "run", _hanging_run)
    first = D._drain_queue_once(_drain_args(queue, launch_budget_s=HANG_S + 0.25))
    assert first["launched"] == 0, first
    queued = json.loads(path.read_text(encoding="utf-8"))
    assert queued.get("state") == "queued", queued
    assert queued.get("launch_backoff_until"), queued

    calls = {"n": 0}

    def count_run(argv, *args, **kwargs):
        argv_list = list(argv)
        if _is_drain_child(argv_list):
            calls["n"] += 1
            time.sleep(HANG_S)
            raise subprocess.TimeoutExpired(argv_list, kwargs.get("timeout") or HANG_S)
        return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)

    monkeypatch.setattr(D.subprocess, "run", count_run)
    started = time.monotonic()
    second = D._drain_queue_once(_drain_args(queue, launch_budget_s=HANG_S + 0.25))
    elapsed = time.monotonic() - started
    assert calls["n"] == 0, "backed-off entry was launched again immediately"
    assert elapsed < HANG_S, elapsed
    reasons = [str(row.get("reason") or "") for row in second.get("details") or []]
    assert "launch_backoff" in reasons, second


def test_pass_reports_launch_and_reconcile_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    _write_entry(
        queue,
        "timed",
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(D.subprocess, "run", _launched_run_factory(tmp_path))
    payload = D._drain_queue_once(_drain_args(queue))
    timing = payload.get("timing")
    assert isinstance(timing, dict), payload
    for key in (
        "pass_s",
        "launch_s",
        "reconcile_s",
        "recovery_s",
        "capacity_slots",
        "launch_timeouts",
        "launch_budget_burns",
    ):
        assert key in timing, timing
        assert isinstance(timing[key], (int, float)), timing
    recovery = timing.get("recovery")
    assert isinstance(recovery, dict), timing
    for key in (
        "listing_s",
        "claim_loop_s",
        "ledger_lookup_s",
        "fs_identity_s",
        "flock_probe_s",
        "skip_n",
        "claimed_carriers",
    ):
        assert key in recovery, recovery


def test_launch_slot_budget_is_read_only_remaining_slots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "cap-state"))
    budget = cap.launch_slot_budget()
    assert budget["unreadable"] is False
    assert budget["global_remaining"] >= 0
    assert budget["operating_cap"] >= budget["active"]
    assert "by_pool" in budget


def test_two_same_task_queue_entries_yield_one_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two queued ids, one --task: drain must spawn one child.

    The launch-path gate is a ledger read plus an in-pass spawned-task set.
    This test drives actual drain-child ``subprocess.run`` calls (not the
    authority predicate). Revert-failure: both ids appear in ``launches``.
    """
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    _REAL_SUBPROCESS_RUN(
        ["git", "init"],
        cwd=str(project),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    task_id = "t-drain-serial-one"
    launches: list[str] = []
    lock = threading.Lock()

    def fake_run(argv, *args, **kwargs):
        argv_list = list(argv)
        if not _is_drain_child(argv_list):
            return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
        try:
            dispatch_id = argv_list[argv_list.index("--dispatch-id") + 1]
            token = argv_list[argv_list.index("--queue-launch-token") + 1]
            cwd = argv_list[argv_list.index("--cwd") + 1]
        except (ValueError, IndexError):
            return subprocess.CompletedProcess(argv_list, 0, stdout="", stderr="")
        with lock:
            launches.append(dispatch_id)
        record = {
            "schema": ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "agent": "test-dispatch",
            "engine": "test-dispatch",
            "shape": "bash",
            "transport": "dispatch",
            "project_root": cwd,
            "worker_pid": os.getpid(),
            "worker_identity": ledger.process_identity(os.getpid()),
            "stdout_path": str(tmp_path / f"{dispatch_id}.tail"),
            "status_path": str(tmp_path / f"{dispatch_id}.status.json"),
            "state": "running",
            "terminal_state": "unknown",
            "queue_launch_token": token,
            "task_ids": [task_id],
            "started_at": ledger.utc_now(),
        }
        ledger.write_record(record)
        return subprocess.CompletedProcess(
            argv_list,
            0,
            stdout=f"DISPATCH-LAUNCHED {dispatch_id}\n",
            stderr="",
        )

    _write_entry(
        queue,
        "task-a",
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
        task_ids=[task_id],
    )
    _write_entry(
        queue,
        "task-b",
        project_root=project,
        created_at="2026-01-01T00:00:01+00:00",
        task_ids=[task_id],
    )
    monkeypatch.setattr(D.subprocess, "run", fake_run)
    payload = D._drain_queue_once(_drain_args(queue))
    assert launches == ["task-a"], (launches, payload)
    assert payload["launched"] == 1, payload
    loser_reasons = [
        str(row.get("reason") or "")
        for row in payload.get("details") or []
        if row.get("dispatch_id") == "task-b"
    ]
    assert loser_reasons, payload
    assert not any("task-b" == item for item in launches)


def test_two_drain_threads_on_one_dispatch_id_yield_one_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two drain passes racing one envelope still produce one child launch."""
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    _write_entry(
        queue,
        "solo-id",
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    launches: list[str] = []
    lock = threading.Lock()

    def fake_run(argv, *args, **kwargs):
        argv_list = list(argv)
        if not _is_drain_child(argv_list):
            return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
        try:
            dispatch_id = argv_list[argv_list.index("--dispatch-id") + 1]
            token = argv_list[argv_list.index("--queue-launch-token") + 1]
            cwd = argv_list[argv_list.index("--cwd") + 1]
        except (ValueError, IndexError):
            return subprocess.CompletedProcess(argv_list, 0, stdout="", stderr="")
        with lock:
            launches.append(dispatch_id)
        ledger.write_record(
            {
                "schema": ledger.SCHEMA,
                "dispatch_id": dispatch_id,
                "agent": "test-dispatch",
                "engine": "test-dispatch",
                "shape": "bash",
                "transport": "dispatch",
                "project_root": cwd,
                "worker_pid": os.getpid(),
                "worker_identity": ledger.process_identity(os.getpid()),
                "stdout_path": str(tmp_path / f"{dispatch_id}.tail"),
                "status_path": str(tmp_path / f"{dispatch_id}.status.json"),
                "state": "running",
                "terminal_state": "unknown",
                "queue_launch_token": token,
                "started_at": ledger.utc_now(),
            }
        )
        return subprocess.CompletedProcess(
            argv_list,
            0,
            stdout=f"DISPATCH-LAUNCHED {dispatch_id}\n",
            stderr="",
        )

    monkeypatch.setattr(D.subprocess, "run", fake_run)
    results: list[dict] = []

    def _run() -> None:
        results.append(D._drain_queue_once(_drain_args(queue)))

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert launches == ["solo-id"], launches
    assert sum(row.get("launched") or 0 for row in results) == 1, results


def test_slow_non_timeout_failure_at_head_does_not_starve_healthy_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe D: front returns rc=2 after most of the budget; tail must launch.

    Revert-failure on 2c160d8: both passes attempt only slow-fail
    (launch_refused_pre_spawn:2 + pass_launch_budget). Healthy-back is
    never subprocess-attempted because the restored head keeps the same
    FIFO position and is immediately eligible next pass.
    """
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    _write_front_and_healthy(queue, project)
    attempts: list[str] = []
    monkeypatch.setattr(
        D.subprocess,
        "run",
        _selective_child_run(
            tmp_path,
            slow_fail_ids=frozenset({"slow-fail"}),
            attempts=attempts,
        ),
    )
    args = _drain_args(queue, launch_budget_s=STARVE_BUDGET_S)
    payloads: list[dict] = []
    for _ in range(BOUNDED_PASSES):
        payloads.append(D._drain_queue_once(args))
        if "healthy-back" in _launched_ids(payloads[-1]):
            break
    launched_across = [did for payload in payloads for did in _launched_ids(payload)]
    assert "healthy-back" in launched_across, (
        f"healthy-back never launched within {BOUNDED_PASSES} passes; "
        f"attempts={attempts} payloads={payloads}"
    )
    assert attempts.count("healthy-back") >= 1, attempts
    assert len(payloads) <= BOUNDED_PASSES, payloads


def test_launch_refused_pre_spawn_at_head_stamps_backoff_and_unblocks_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refused-pre-spawn restore path must stamp backoff, not just TimeoutExpired.

    Revert-failure on 2c160d8: pass 1 reason is launch_refused_pre_spawn:2,
    the queued envelope has no launch_backoff_until, timing.launch_timeouts
    is 0, and there is no non-timeout burn field. Pass 2 retries the same
    head.
    """
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    front, _back = _write_front_and_healthy(queue, project)
    attempts: list[str] = []
    monkeypatch.setattr(
        D.subprocess,
        "run",
        _selective_child_run(
            tmp_path,
            slow_fail_ids=frozenset({"slow-fail"}),
            attempts=attempts,
        ),
    )
    args = _drain_args(queue, launch_budget_s=STARVE_BUDGET_S)
    first = D._drain_queue_once(args)
    assert first["launched"] == 0, first
    assert _reason_for(first, "slow-fail").startswith("launch_refused_pre_spawn"), first
    assert _reason_for(first, "healthy-back") == "pass_launch_budget", first
    timing = first.get("timing") or {}
    assert int(timing.get("launch_timeouts") or 0) == 0, first
    burns = int(timing.get("launch_budget_burns") or 0)
    assert burns >= 1, first
    queued = json.loads(front.read_text(encoding="utf-8"))
    assert queued.get("state") == "queued", queued
    assert queued.get("launch_backoff_until"), queued
    assert queued.get("launch_last_attempted_at"), queued
    assert queued.get("launch_fail_reason") == "launch_budget_burn", queued

    second = D._drain_queue_once(args)
    assert "healthy-back" in _launched_ids(second), (attempts, second)
    assert "slow-fail" not in attempts[1:], attempts


def test_timeout_at_head_still_launches_healthy_on_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe B: TimeoutExpired at the front still unblocks the tail on pass 2.

    Revert-failure: hung-front retried immediately, healthy never launched,
    or launch_budget_burns is charged for a timeout (it must stay a timeout).
    """
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    _write_entry(
        queue,
        "hung-front",
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    _write_entry(
        queue,
        "healthy-back",
        project_root=project,
        created_at="2026-01-01T00:00:01+00:00",
    )
    attempts: list[str] = []
    monkeypatch.setattr(
        D.subprocess,
        "run",
        _selective_child_run(
            tmp_path,
            hang_ids=frozenset({"hung-front"}),
            attempts=attempts,
        ),
    )
    args = _drain_args(queue, launch_budget_s=STARVE_BUDGET_S)
    first = D._drain_queue_once(args)
    assert first["launched"] == 0, first
    assert int((first.get("timing") or {}).get("launch_timeouts") or 0) == 1, first
    assert int((first.get("timing") or {}).get("launch_budget_burns") or 0) == 0, first
    assert attempts == ["hung-front"], attempts

    second = D._drain_queue_once(args)
    assert "healthy-back" in _launched_ids(second), (attempts, second)
    assert "hung-front" not in attempts[1:], attempts


def test_attempt_cursor_unblocks_tail_after_backoff_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIFO head must not shadow the tail once backoff has expired.

    Backoff skip is enough for the next daemon tick, but a stable
    (priority, created_at, name) order would retry the dead letter the
    moment launch_backoff_until lapses. The attempt cursor must put
    never-attempted work first. Revert-failure on 2c160d8: after clearing
    backoff, pass 2 attempts only slow-fail again.
    """
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    front, _back = _write_front_and_healthy(queue, project)
    attempts: list[str] = []
    monkeypatch.setattr(
        D.subprocess,
        "run",
        _selective_child_run(
            tmp_path,
            slow_fail_ids=frozenset({"slow-fail"}),
            attempts=attempts,
        ),
    )
    args = _drain_args(queue, launch_budget_s=STARVE_BUDGET_S)
    first = D._drain_queue_once(args)
    assert first["launched"] == 0, first
    queued = json.loads(front.read_text(encoding="utf-8"))
    queued.pop("launch_backoff_until", None)
    D._write_json_atomic(front, queued)

    second = D._drain_queue_once(args)
    assert "healthy-back" in _launched_ids(second), (attempts, second)
    assert attempts[-1] == "healthy-back" or "healthy-back" in attempts[1:], attempts


def test_pass_wall_time_still_does_not_scale_with_n_unlaunchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N slow rc=2 entries must not take ~N times one confirmation wait.

    Same N-independence claim as the TimeoutExpired case, for the
    non-timeout restore path. Revert-failure: elapsed scales with N.
    """
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    n = N_HUNG
    fail_ids = frozenset(f"slow-{index}" for index in range(n))
    for index in range(n):
        _write_entry(
            queue,
            f"slow-{index}",
            project_root=project,
            created_at=f"2026-01-01T00:00:0{index}+00:00",
        )
    monkeypatch.setattr(
        D.subprocess,
        "run",
        _selective_child_run(tmp_path, slow_fail_ids=fail_ids),
    )
    args = _drain_args(queue, launch_budget_s=STARVE_BUDGET_S)
    started = time.monotonic()
    payload = D._drain_queue_once(args)
    elapsed = time.monotonic() - started
    # One slow wait plus restore, not N waits. N*SLOW_FAIL_S is 2.0s for
    # n=4, which sits inside a linear ceiling; the ratchet is one burn.
    ceiling = SLOW_FAIL_S + 1.2
    assert elapsed < ceiling, (
        f"pass wall {elapsed:.3f}s scaled with {n} slow-fail entries "
        f"(ceiling {ceiling:.3f}s); payload={payload}"
    )
    assert payload["launched"] == 0, payload
    burns = int((payload.get("timing") or {}).get("launch_budget_burns") or 0)
    assert burns == 1, payload
    reasons = [str(row.get("reason") or "") for row in payload.get("details") or []]
    assert reasons.count("pass_launch_budget") == n - 1, payload
