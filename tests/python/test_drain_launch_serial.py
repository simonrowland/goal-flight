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


def _write_queued_ledger(path: Path) -> dict:
    entry = json.loads(path.read_text(encoding="utf-8"))
    request = entry["request"]
    agent = str(entry["agent"])
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": entry["dispatch_id"],
            "agent": agent,
            "engine": agent,
            "shape": entry["shape"],
            "transport": "dispatch",
            "project_root": entry["project_root"],
            "worker_pid": None,
            "stdout_path": request["tail"],
            "status_path": request["status_json"],
            "state": "queued",
            "terminal_state": "unknown",
            "dispatch_argv": entry["dispatch_argv"],
            "started_at": ledger.utc_now(),
        }
    )
    return entry


def _write_missing_prompt_entry(
    queue: Path,
    dispatch_id: str,
    *,
    project_root: Path,
) -> Path:
    path = queue / f"{dispatch_id}.json"
    missing_prompt = project_root / "deleted-before-drain.md"
    tail = project_root / f"{dispatch_id}.tail"
    status = project_root / f"{dispatch_id}.status.json"
    argv = [
        "--agent",
        "codex",
        "--prompt-file",
        str(missing_prompt),
        "--dispatch-id",
        dispatch_id,
        "--cwd",
        str(project_root),
        "--tail",
        str(tail),
        "--status-json",
        str(status),
        "--unregistered-forced",
        "--occupied-worktree-forced",
        "--ignore-git-warn",
    ]
    payload = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "queued",
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "shape": "bash",
        "project_root": str(project_root),
        "process_cwd": str(project_root),
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "queue_path": str(path),
        "dispatch_argv": argv,
        "request": {
            "agent": "codex",
            "cwd": str(project_root),
            "prompt_file": str(missing_prompt),
            "tail": str(tail),
            "status_json": str(status),
        },
    }
    D._write_json_atomic(path, payload)
    _write_queued_ledger(path)
    return path


def _write_prompt_file_entry(
    queue: Path,
    dispatch_id: str,
    *,
    project_root: Path,
    agent: str,
    prompt: str,
    extra_argv: list[str] | None = None,
    request_extra: dict | None = None,
) -> Path:
    path = queue / f"{dispatch_id}.json"
    prompt_file = project_root / f"{dispatch_id}.prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    tail = project_root / f"{dispatch_id}.tail"
    status = project_root / f"{dispatch_id}.status.json"
    argv = [
        "--agent",
        agent,
        "--prompt-file",
        str(prompt_file),
        "--dispatch-id",
        dispatch_id,
        "--cwd",
        str(project_root),
        "--tail",
        str(tail),
        "--status-json",
        str(status),
        "--unregistered-forced",
        "--occupied-worktree-forced",
        "--ignore-git-warn",
    ]
    if extra_argv:
        argv[2:2] = list(extra_argv)
    payload = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "queued",
        "dispatch_id": dispatch_id,
        "agent": agent,
        "shape": "bash",
        "project_root": str(project_root),
        "process_cwd": str(project_root),
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "queue_path": str(path),
        "dispatch_argv": argv,
        "request": {
            "agent": agent,
            "cwd": str(project_root),
            "prompt_file": str(prompt_file),
            "tail": str(tail),
            "status_json": str(status),
            **(request_extra or {}),
        },
    }
    D._write_json_atomic(path, payload)
    _write_queued_ledger(path)
    return path


def _clear_launch_backoff(path: Path) -> dict:
    queued = json.loads(path.read_text(encoding="utf-8"))
    queued.pop("launch_backoff_until", None)
    D._write_json_atomic(path, queued)
    return queued


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


def test_real_pre_spawn_refusal_terminalizes_after_bounded_attempts(
    tmp_path: Path,
) -> None:
    """A real child computes its missing-prompt refusal; drain never supplies it.

    Pins bounded abandon, not the exact strike count. A policy of 4 strikes,
    or two permanents after one transient, still meets the contract as long as
    the envelope is failed within MAX_DRAIN_PRE_WORKER_FAILURES counted
    attempts and does not retry unboundedly.
    """
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "missing-prompt-bounded"
    path = _write_missing_prompt_entry(
        queue,
        dispatch_id,
        project_root=project,
    )

    payloads = []
    bound = D.MAX_DRAIN_PRE_WORKER_FAILURES + 2
    last_count = 0
    for _attempt in range(1, bound + 1):
        payload = D._drain_queue_once(_drain_args(queue))
        payloads.append(payload)
        if not path.exists():
            break
        queued = json.loads(path.read_text(encoding="utf-8"))
        assert queued["state"] == "queued", (queued, payload)
        assert queued.get("launch_backoff_until"), queued
        last_count = int(queued.get("launch_pre_worker_failure_count") or 0)
        assert 1 <= last_count <= D.MAX_DRAIN_PRE_WORKER_FAILURES, queued
        assert "prompt file not found" in queued["launch_fail_reason"], queued
        _clear_launch_backoff(path)
    else:
        raise AssertionError(
            f"carrier still queued after {bound} passes; payloads={payloads}"
        )

    assert path.exists() is False, payloads
    failed_paths = list(queue.glob(f"{dispatch_id}.json.claimed-*.failed"))
    assert len(failed_paths) == 1, (failed_paths, payloads)
    failed = json.loads(failed_paths[0].read_text(encoding="utf-8"))
    assert failed["state"] == "failed", failed
    assert 1 <= int(failed["launch_pre_worker_failure_count"]) <= (
        D.MAX_DRAIN_PRE_WORKER_FAILURES
    ), failed
    reason = str(failed.get("reason") or "")
    assert reason.startswith("launch_attempt_limit_exceeded:"), failed
    assert "prompt file not found" in reason, failed
    assert "launch_budget_burn" not in reason, failed

    record = json.loads(ledger.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert record.get("state") == "failed", record
    assert str(record.get("terminal_state") or "") not in {"", "unknown"}, record
    assert str(record.get("reason") or "").startswith("launch_attempt_limit_exceeded:"), record
    assert 1 <= int(record["launch_pre_worker_failure_count"]) <= (
        D.MAX_DRAIN_PRE_WORKER_FAILURES
    )
    status = json.loads((project / f"{dispatch_id}.status.json").read_text(encoding="utf-8"))
    assert status.get("state") == "failed", status
    assert str(status.get("reason") or "").startswith("launch_attempt_limit_exceeded:"), status


def test_legacy_launch_timeout_count_does_not_spend_new_failure_budget(
    tmp_path: Path,
) -> None:
    """An old confirm-timeout count is audit history, not new refusal proof."""
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    path = _write_missing_prompt_entry(
        queue,
        "legacy-count-first-new-refusal",
        project_root=project,
    )
    queued = json.loads(path.read_text(encoding="utf-8"))
    queued["launch_timeout_count"] = 18
    D._write_json_atomic(path, queued)

    payload = D._drain_queue_once(_drain_args(queue))

    assert path.exists(), payload
    queued = json.loads(path.read_text(encoding="utf-8"))
    assert queued["launch_timeout_count"] == 18, queued
    assert queued["launch_pre_worker_failure_count"] == 1, queued
    assert not list(queue.glob("legacy-count-first-new-refusal*.failed")), payload


@pytest.mark.parametrize(
    ("returncode", "diagnostic"),
    [
        (2, "refusing to git worktree add; wait for a seat"),
        (73, "controller label in use"),
        (64, "queue claim launch marker failed: OSError"),
    ],
    ids=("worktree-seat", "controller", "filesystem"),
)
def test_transient_local_pre_spawn_gate_does_not_spend_failure_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    diagnostic: str,
) -> None:
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = f"transient-{returncode}"
    path = _write_entry(
        queue,
        dispatch_id,
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )

    def refuse(argv, *args, **kwargs):
        argv_list = list(argv)
        if not _is_drain_child(argv_list):
            return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
        return subprocess.CompletedProcess(
            argv_list,
            returncode,
            stdout="",
            stderr=f"goalflight_dispatch: {diagnostic}\n",
        )

    monkeypatch.setattr(D.subprocess, "run", refuse)
    payload = D._drain_queue_once(_drain_args(queue))

    assert path.exists(), payload
    queued = json.loads(path.read_text(encoding="utf-8"))
    assert int(queued.get("launch_pre_worker_failure_count") or 0) == 0, queued
    assert int(queued.get("launch_timeout_count") or 0) == 0, queued
    assert queued.get("launch_attempt_class") == (
        D.LAUNCH_ATTEMPT_CLASS_PROVEN_TRANSIENT
    ), queued
    assert not queued.get("launch_backoff_until"), queued
    assert queued.get("launch_fail_reason") == (
        f"launch_refused_pre_spawn:{returncode}:{diagnostic}"
    ), queued


def test_remote_fleet_gate_does_not_spend_failure_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "transient-fleet"
    path = _write_entry(
        queue,
        dispatch_id,
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(D, "_validate_remote_drain_node", lambda _args: None)

    def blocked(*_args, **_kwargs):
        raise D._RemoteDrainBlocked("fleet unavailable", code="fleet_unavailable")

    monkeypatch.setattr(D, "_drain_launch_remote_claim", blocked)
    payload = D._drain_queue_once(
        _drain_args(
            queue,
            remote_node="test-node",
            remote_runner=object(),
        )
    )

    assert path.exists(), payload
    queued = json.loads(path.read_text(encoding="utf-8"))
    assert int(queued.get("launch_pre_worker_failure_count") or 0) == 0, queued
    assert int(queued.get("launch_timeout_count") or 0) == 0, queued
    assert queued.get("launch_attempt_class") == (
        D.LAUNCH_ATTEMPT_CLASS_PROVEN_TRANSIENT
    ), queued
    assert "remote_blocked:fleet_unavailable" in _reason_for(payload, dispatch_id)


def test_real_confirmed_launch_does_not_spend_failure_budget(tmp_path: Path) -> None:
    """The production child must launch and ledger-confirm without a supplied proof."""
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "real-confirmed-launch"
    path = _write_entry(
        queue,
        dispatch_id,
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    _write_queued_ledger(path)

    payload = D._drain_queue_once(_drain_args(queue))
    assert payload["launched"] == 1, payload
    assert path.exists() is False, payload
    assert not list(queue.glob(f"{dispatch_id}*.failed")), payload
    assert int((payload.get("timing") or {}).get("launch_timeouts") or 0) == 0, payload


def test_real_ledger_confirmation_survives_lost_launcher_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lose the real child's response after it launches; ledger proof must win."""
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "real-launch-response-lost"
    path = _write_entry(
        queue,
        dispatch_id,
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    _write_queued_ledger(path)

    def lose_response_after_real_launch(argv, *args, **kwargs):
        proc = _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
        if not _is_drain_child(list(argv)):
            return proc
        assert "DISPATCH-LAUNCHED " in proc.stdout, proc
        raise subprocess.TimeoutExpired(
            list(argv),
            kwargs.get("timeout") or 1.0,
            output=proc.stdout,
            stderr=proc.stderr,
        )

    monkeypatch.setattr(D.subprocess, "run", lose_response_after_real_launch)
    payload = D._drain_queue_once(_drain_args(queue))
    assert payload["launched"] == 1, payload
    assert int((payload.get("timing") or {}).get("launch_timeouts") or 0) == 1, payload
    assert _reason_for(payload, dispatch_id) == (
        "worker_record_present_after_launch_timeout"
    ), payload
    assert path.exists() is False, payload
    assert not list(queue.glob(f"{dispatch_id}*.failed")), payload


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


def test_timeout_confirmed_cleanup_pending_still_blocks_same_task_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ledger proof must reserve the task even when carrier cleanup is pending."""
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    task_id = "t-timeout-cleanup-pending"
    for index, dispatch_id in enumerate(("task-a", "task-b")):
        _write_entry(
            queue,
            dispatch_id,
            project_root=project,
            created_at=f"2026-01-01T00:00:0{index}+00:00",
            task_ids=[task_id],
        )

    launch = _launched_run_factory(tmp_path)
    attempts: list[str] = []

    def lose_first_response(argv, *args, **kwargs):
        argv_list = list(argv)
        if not _is_drain_child(argv_list):
            return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
        dispatch_id = argv_list[argv_list.index("--dispatch-id") + 1]
        attempts.append(dispatch_id)
        proc = launch(argv, *args, **kwargs)
        if dispatch_id == "task-a":
            raise subprocess.TimeoutExpired(
                argv_list,
                kwargs.get("timeout") or 1.0,
                output=proc.stdout,
                stderr=proc.stderr,
            )
        return proc

    monkeypatch.setattr(D.subprocess, "run", lose_first_response)
    monkeypatch.setattr(
        D,
        "_positive_live_carrier_cleanup",
        lambda *_args, **_kwargs: "pending",
    )
    payload = D._drain_queue_once(_drain_args(queue))

    assert attempts == ["task-a"], (attempts, payload)
    assert _reason_for(payload, "task-a") == (
        "worker_record_present_after_launch_timeout_carrier_cleanup_pending"
    ), payload


def test_failure_count_survives_ledger_carrier_republication(tmp_path: Path) -> None:
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "republished-failure-count"
    path = _write_missing_prompt_entry(queue, dispatch_id, project_root=project)

    first = D._drain_queue_once(_drain_args(queue))
    assert path.exists(), first
    stamped = json.loads(path.read_text(encoding="utf-8"))
    assert stamped["launch_pre_worker_failure_count"] == 1
    assert stamped.get("launch_backoff_until"), stamped
    backoff_count = int(stamped.get("launch_backoff_count") or 0)
    assert backoff_count >= 1, stamped
    path.unlink()

    recovered = D._recover_claimed_queue_entries(queue, stale_s=0.0)

    assert recovered.get("restored", 0) >= 1, recovered
    assert path.exists(), recovered
    republished = json.loads(path.read_text(encoding="utf-8"))
    assert republished["launch_pre_worker_failure_count"] == 1, republished
    assert republished["launch_backoff_count"] == backoff_count, republished
    assert republished.get("launch_backoff_until"), republished


def test_launch_attempt_stamp_write_error_is_reported_and_ledger_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "stamp-write-visible"
    path = _write_missing_prompt_entry(queue, dispatch_id, project_root=project)
    real_write = D._write_json_atomic

    def fail_carrier_stamp(target: Path, payload: dict) -> None:
        if (
            target == path
            and "launch_pre_worker_failure_count" in payload
            and path.exists()
        ):
            raise OSError("simulated carrier stamp failure")
        real_write(target, payload)

    monkeypatch.setattr(D, "_write_json_atomic", fail_carrier_stamp)
    result = D._drain_queue_once(_drain_args(queue))

    detail = next(
        row
        for row in result.get("details") or []
        if row.get("dispatch_id") == dispatch_id
    )
    assert detail["launch_attempt_stamp_error"] == (
        "launch_attempt_stamp_failed:carrier:OSError"
    ), detail
    record = ledger.read_record(dispatch_id)
    assert record is not None
    assert record["launch_pre_worker_failure_count"] == 1, record


def test_launch_backoff_counter_saturates_at_capped_delay(tmp_path: Path) -> None:
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    path = _write_entry(
        queue,
        "saturated-backoff",
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    entry = _write_queued_ledger(path)
    entry["launch_backoff_count"] = D.MAX_LAUNCH_BACKOFF_COUNT
    D._write_json_atomic(path, entry)

    error = D._stamp_launch_attempt(
        path,
        entry,
        backoff=True,
        fail_reason="capacity_unavailable",
    )

    assert error is None
    stamped = json.loads(path.read_text(encoding="utf-8"))
    assert stamped["launch_backoff_count"] == D.MAX_LAUNCH_BACKOFF_COUNT, stamped
    record = ledger.read_record("saturated-backoff")
    assert record is not None
    assert record["launch_backoff_count"] == D.MAX_LAUNCH_BACKOFF_COUNT, record


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
    assert queued.get("launch_fail_reason") == (
        "launch_refused_pre_spawn:2:slow-fail"
    ), queued

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


def _assert_undetermined_and_backed_off(
    queue: Path,
    path: Path,
    dispatch_id: str,
    payload: dict,
) -> dict:
    assert path.exists(), payload
    queued = json.loads(path.read_text(encoding="utf-8"))
    assert queued.get("state") == "queued", queued
    assert queued.get("launch_attempt_class") == (
        D.LAUNCH_ATTEMPT_CLASS_UNDETERMINED
    ), queued
    assert queued.get("launch_backoff_until"), queued
    assert int(queued.get("launch_undetermined_count") or 0) >= 1, queued
    assert int(queued.get("launch_pre_worker_failure_count") or 0) == 0, queued
    second = D._drain_queue_once(_drain_args(queue))
    assert _reason_for(second, dispatch_id) == "launch_backoff", second
    assert int(second.get("launched") or 0) == 0, second
    return queued


def test_grok_code_web_research_prompt_is_undetermined_and_backs_off(
    tmp_path: Path,
) -> None:
    """Queued grok-code 'search the web' must not sit with no accounting."""
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "grok-web-research"
    path = _write_prompt_file_entry(
        queue,
        dispatch_id,
        project_root=project,
        agent="grok-code",
        prompt="Please search the web for goal-flight drain launch accounting.",
    )
    payload = D._drain_queue_once(_drain_args(queue))
    queued = _assert_undetermined_and_backed_off(queue, path, dispatch_id, payload)
    assert "WEB RESEARCH" in str(queued.get("launch_fail_reason") or ""), queued


def test_read_only_write_artifact_prompt_is_undetermined_and_backs_off(
    tmp_path: Path,
) -> None:
    """Queued --read-only write-artifact prompt must not sit with no accounting."""
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "readonly-write-artifact"
    path = _write_prompt_file_entry(
        queue,
        dispatch_id,
        project_root=project,
        agent="codex",
        prompt="Please write the review artifact to docs-private/findings.md.",
        extra_argv=["--read-only"],
        request_extra={"read_only": True},
    )
    payload = D._drain_queue_once(_drain_args(queue))
    queued = _assert_undetermined_and_backed_off(queue, path, dispatch_id, payload)
    assert "read-only" in str(queued.get("launch_fail_reason") or "").lower(), queued


def test_remote_missing_prompt_is_undetermined_and_backs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remote drain with no prompt must not sit queued with no accounting."""
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "remote-missing-prompt"
    path = _write_entry(
        queue,
        dispatch_id,
        project_root=project,
        created_at="2026-01-01T00:00:00+00:00",
    )
    queued = json.loads(path.read_text(encoding="utf-8"))
    queued["agent"] = "codex"
    queued["request"]["agent"] = "codex"
    D._write_json_atomic(path, queued)
    _write_queued_ledger(path)
    monkeypatch.setattr(D, "_validate_remote_drain_node", lambda _args: None)
    payload = D._drain_queue_once(
        _drain_args(
            queue,
            remote_node="test-node",
            remote_runner=object(),
        )
    )
    queued = _assert_undetermined_and_backed_off(queue, path, dispatch_id, payload)
    assert "remote_blocked:missing_prompt" in str(
        queued.get("launch_fail_reason") or ""
    ), queued


def test_unprefixed_bad_task_id_is_undetermined_not_a_named_case(
    tmp_path: Path,
) -> None:
    """A carrier defect outside the three measured cases is still classified.

    Queued --task with a syntactically invalid id raises DispatchUsageError
    before the proven-prefix handler. That is the class: unrecognised prefix,
    not a widened match of the three symptoms.
    """
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "bad-task-id"
    path = _write_prompt_file_entry(
        queue,
        dispatch_id,
        project_root=project,
        agent="codex",
        prompt="ordinary coding prompt",
        extra_argv=["--task", "not-a-task-id"],
    )
    payload = D._drain_queue_once(_drain_args(queue))
    queued = _assert_undetermined_and_backed_off(queue, path, dispatch_id, payload)
    assert "t-/b- ids" in str(queued.get("launch_fail_reason") or ""), queued


def test_dual_stamp_write_failure_still_persists_cursor_via_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore publishes the in-memory cursor even if stamp's two writes throw."""
    queue = _queue_dir(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dispatch_id = "dual-stamp-fail"
    path = _write_missing_prompt_entry(queue, dispatch_id, project_root=project)
    real_write = D._write_json_atomic

    def fail_ledger(_record: dict) -> Path:
        raise OSError("simulated ledger stamp failure")

    def fail_second_carrier(target: Path, payload: dict) -> None:
        if (
            target == path
            and "launch_pre_worker_failure_count" in payload
            and path.exists()
        ):
            raise OSError("simulated carrier stamp failure")
        real_write(target, payload)

    monkeypatch.setattr(D.goalflight_ledger, "write_record", fail_ledger)
    monkeypatch.setattr(D, "_write_json_atomic", fail_second_carrier)
    payload = D._drain_queue_once(_drain_args(queue))

    assert path.exists(), payload
    queued = json.loads(path.read_text(encoding="utf-8"))
    assert queued["launch_pre_worker_failure_count"] == 1, queued
    detail = next(
        row
        for row in payload.get("details") or []
        if row.get("dispatch_id") == dispatch_id
    )
    error = str(detail.get("launch_attempt_stamp_error") or "")
    assert "ledger:OSError" in error, detail
    assert "carrier:OSError" in error, detail


def test_terminalize_without_ledger_still_fails_the_carrier(tmp_path: Path) -> None:
    """A queue-only proven refusal must not retry forever because ledger is missing."""
    queue = _queue_dir(tmp_path)
    dispatch_id = "orphan-no-ledger"
    claim = queue / f"{dispatch_id}.json.claimed-test"
    entry = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "claimed",
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "queue_launch_token": "tok",
        "project_root": str(tmp_path),
    }
    D._write_json_atomic(claim, entry)
    committed, detail = D._terminalize_pre_worker_launch_failure(
        claim,
        entry,
        reason="launch_attempt_limit_exceeded:test",
        failure_count=D.MAX_DRAIN_PRE_WORKER_FAILURES,
    )
    assert committed is True, detail
    assert claim.exists() is False, detail
    failed_paths = list(queue.glob(f"{dispatch_id}.json.claimed-test.failed"))
    assert len(failed_paths) == 1, (failed_paths, detail)
    failed = json.loads(failed_paths[0].read_text(encoding="utf-8"))
    assert failed["state"] == "failed", failed
    assert "ledger_missing" in detail, detail


def test_real_claim_marker_oserror_is_not_a_proven_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Production claim-marker OSError must not emit the authenticated prefix."""
    claim = tmp_path / "marker.json"
    claim.write_text("{}", encoding="utf-8")
    claim.chmod(0o000)
    args = argparse.Namespace(
        from_queue=True,
        queue_claim_path=str(claim),
        queue_launch_token="tok",
    )
    try:
        with pytest.raises(D.DispatchUsageError) as caught:
            D._mark_queue_claim_launch_started(args)
    finally:
        claim.chmod(0o600)
    assert "queue claim launch marker failed:" in str(caught.value)
    captured = capsys.readouterr()
    assert D.PROVEN_PRE_WORKER_REFUSAL_PREFIX not in captured.out
    assert D.PROVEN_PRE_WORKER_REFUSAL_PREFIX not in captured.err
    assert not isinstance(caught.value, D.ProvenPreWorkerRefusal)


def test_controller_label_in_use_handler_does_not_emit_proven_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The production rc-73 handler must not raise or print a proven refusal."""
    monkeypatch.setattr(
        D,
        "_stamp_controller_session",
        lambda *_args, **_kwargs: {
            "reason": "label_in_use",
            "message": "controller label in use",
        },
    )
    code = D.main(
        [
            "--agent",
            "test-dispatch",
            "--cwd",
            str(tmp_path),
            "--unregistered-forced",
            "--occupied-worktree-forced",
            "--",
            sys.executable,
            "-c",
            "print('must-not-launch')",
        ]
    )
    captured = capsys.readouterr()
    assert code == 73, (code, captured)
    assert "label in use" in captured.err
    assert D.PROVEN_PRE_WORKER_REFUSAL_PREFIX not in captured.out
    assert D.PROVEN_PRE_WORKER_REFUSAL_PREFIX not in captured.err


def test_real_worktree_seat_child_does_not_emit_proven_prefix_or_spend_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Production WorktreeSeatUnavailable must not emit the authenticated prefix.

    Drain classification of that diagnostic is pinned by the parametrized
    transient-gate test. This drives the real bind path, not a mocked child.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "goalflight-test@example.invalid"],
        ["git", "config", "user.name", "Goal Flight Test"],
    ):
        result = _REAL_SUBPROCESS_RUN(
            args,
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert result.returncode == 0, (args, result.stderr)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    for args in (["git", "add", "tracked.txt"], ["git", "commit", "-m", "base"]):
        result = _REAL_SUBPROCESS_RUN(
            args,
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert result.returncode == 0, (args, result.stderr)

    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    import goalflight_worktree_pool as pool

    holder = pool.acquire_worktree_seat(repo, "seat-holder")
    try:
        code = D.main(
            [
                "--agent",
                "test-dispatch",
                "--cwd",
                str(repo),
                "--worktree",
                "HEAD",
                "--unregistered-forced",
                "--occupied-worktree-forced",
                "--ignore-git-warn",
                "--",
                sys.executable,
                "-c",
                "print('must-not-launch')",
            ]
        )
    finally:
        holder.release()
    captured = capsys.readouterr()
    assert code == 2, (code, captured)
    assert "wait for a seat" in captured.err
    assert D.PROVEN_PRE_WORKER_REFUSAL_PREFIX not in captured.out
    assert D.PROVEN_PRE_WORKER_REFUSAL_PREFIX not in captured.err


def main() -> None:
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    main()
