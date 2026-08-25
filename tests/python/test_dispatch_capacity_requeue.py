#!/usr/bin/env python3
"""Capacity refusal must preserve detached dispatches in the durable queue."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("capacity requeue tests launch POSIX bash workers")

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
CAPACITY = ROOT / "scripts" / "goalflight_capacity.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GOALFLIGHT_STATE_DIR": str(tmp / "state"),
            "GOALFLIGHT_TASK_STORE_DIR": str(tmp / "task-store"),
            "GOALFLIGHT_JOURNAL_DIR": str(tmp / "journal"),
            "GOALFLIGHT_MESSAGES_DIR": str(tmp / "messages"),
            "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp / "wake-ledger"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(tmp / "pids"),
            "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
            "GOALFLIGHT_CAPACITY_MAX_TOTAL": "1",
            "GOALFLIGHT_TEST_PROJECT_ROOT": str(tmp),
        }
    )
    env.pop("GOALFLIGHT_CAPACITY_WAIT_S", None)
    return env


def _run(
    argv: list[str],
    env: dict[str, str],
    *,
    cwd: Path = ROOT,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _hold_capacity(tmp: Path, env: dict[str, str], dispatch_id: str) -> str:
    held = _run(
        [
            sys.executable,
            str(CAPACITY),
            "acquire",
            "--agent",
            "test-dispatch",
            "--dispatch-id",
            dispatch_id,
            "--project-root",
            str(tmp),
            "--controller-pid",
            str(os.getpid()),
            "--ttl-s",
            "60",
        ],
        env,
    )
    assert held.returncode == 0, (held.stdout, held.stderr)
    payload = json.loads(held.stdout)
    assert payload["decision"] == "allow", payload
    return str(payload["lease"]["lease_id"])


def _release_capacity(env: dict[str, str], lease_id: str) -> None:
    released = _run(
        [
            sys.executable,
            str(CAPACITY),
            "release",
            "--lease-id",
            lease_id,
        ],
        env,
    )
    assert released.returncode == 0, (released.stdout, released.stderr)


def _dispatch_command(
    tmp: Path,
    dispatch_id: str,
    worker_code: str,
    *,
    extra: list[str] | None = None,
) -> list[str]:
    return [
        sys.executable,
        str(DISPATCH),
        "--unregistered-forced",
        "--agent",
        "test-dispatch",
        "--dispatch-id",
        dispatch_id,
        "--tail",
        str(tmp / f"{dispatch_id}.tail"),
        "--status-json",
        str(tmp / f"{dispatch_id}.status.json"),
        "--poll-secs",
        "0.1",
        "--max-idle-secs",
        "10",
        "--cwd",
        str(tmp),
        *(extra or []),
        "--",
        sys.executable,
        "-c",
        worker_code,
    ]


def _wait_for(predicate, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "condition not met before timeout"


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def test_detached_capacity_refusal_is_durable_and_replayable() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        held_lease = _hold_capacity(tmp, env, "held-for-detached-fallback")
        dispatch_id = "detached-capacity-fallback"
        marker = tmp / "worker-ran"
        status_path = tmp / f"{dispatch_id}.status.json"
        queue_path = tmp / "state" / "dispatch-queue" / f"{dispatch_id}.json"
        worker_code = (
            "from pathlib import Path; "
            f"Path({str(marker)!r}).write_text('once', encoding='utf-8'); "
            f"print('COMPLETE: {dispatch_id} — replay accepted', flush=True)"
        )

        refused = _run(
            _dispatch_command(
                tmp,
                dispatch_id,
                worker_code,
                extra=["--capacity-wait-s", "0", "--no-drain-on-submit"],
            ),
            env,
        )
        assert refused.returncode == 0, (refused.stdout, refused.stderr)
        assert queue_path.exists(), "capacity-refused detached work was not queued"
        assert not marker.exists(), "capacity-refused worker launched despite the held slot"
        assert _read_json(status_path).get("state") == "queued", _read_json(status_path)
        ledger_path = tmp / "state" / "runs.d" / f"{dispatch_id}.json"
        assert _read_json(ledger_path).get("state") == "queued", _read_json(ledger_path)

        _release_capacity(env, held_lease)
        drained = _run(
            [sys.executable, str(DISPATCH), "drain", "--capacity-wait-s", "0", "--json"],
            env,
            cwd=tmp,
        )
        assert drained.returncode == 0, (drained.stdout, drained.stderr)
        drain_payload = json.loads(drained.stdout)
        assert drain_payload["launched"] == 1, drain_payload
        assert not queue_path.exists(), "accepted replay left a duplicate queue carrier"
        _wait_for(lambda: marker.exists() and marker.read_text(encoding="utf-8") == "once")
        _wait_for(
            lambda: _read_json(status_path).get("state") == "complete"
            and _read_json(status_path).get("worker_alive") is not True
        )


def test_capacity_refusal_guard_mirrors() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        _hold_capacity(tmp, env, "held-for-guard-mirrors")

        for dispatch_id, guard in (
            ("from-queue-refusal", ["--from-queue"]),
            ("foreground-refusal", ["--foreground"]),
        ):
            result = _run(
                _dispatch_command(
                    tmp,
                    dispatch_id,
                    "raise SystemExit('must not run')",
                    extra=["--capacity-wait-s", "0", *guard],
                ),
                env,
            )
            assert result.returncode == 2, (dispatch_id, result.stdout, result.stderr)
            status = _read_json(tmp / f"{dispatch_id}.status.json")
            assert status.get("state") == "blocked_capacity", (dispatch_id, status)
            ledger = _read_json(tmp / "state" / "runs.d" / f"{dispatch_id}.json")
            assert ledger.get("state") == "blocked_capacity", (dispatch_id, ledger)
            queue_path = tmp / "state" / "dispatch-queue" / f"{dispatch_id}.json"
            assert not queue_path.exists(), f"{dispatch_id} incorrectly created a queue entry"


def test_detached_capacity_wait_interrupt_does_not_enqueue() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        _hold_capacity(tmp, env, "held-for-interrupt")
        dispatch_id = "detached-wait-interrupted"
        status_path = tmp / f"{dispatch_id}.status.json"
        proc = subprocess.Popen(
            _dispatch_command(
                tmp,
                dispatch_id,
                "raise SystemExit('must not run')",
                extra=["--capacity-wait-s", "30"],
            ),
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for(lambda: _read_json(status_path).get("state") == "waiting_capacity")
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        assert proc.returncode == 143, (stdout, stderr)
        status = _read_json(status_path)
        assert status.get("state") == "blocked_capacity", status
        assert (status.get("reason") or {}).get("reason") == "wait_interrupted", status
        queue_path = tmp / "state" / "dispatch-queue" / f"{dispatch_id}.json"
        assert not queue_path.exists(), "operator interrupt was mistaken for capacity refusal"


def test_submit_drain_capacity_refusal_still_restores_one_entry() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        held_lease = _hold_capacity(tmp, env, "held-for-submit-regression")
        dispatch_id = "submit-capacity-regression"
        queue_dir = tmp / "state" / "dispatch-queue"
        queue_path = queue_dir / f"{dispatch_id}.json"
        marker = tmp / "submit-worker-ran"
        worker_code = (
            "from pathlib import Path; "
            f"Path({str(marker)!r}).write_text('once', encoding='utf-8'); "
            f"print('COMPLETE: {dispatch_id} — restored submit ran', flush=True)"
        )

        submitted = _run(
            _dispatch_command(
                tmp,
                dispatch_id,
                worker_code,
                extra=["--submit", "--no-drain-on-submit"],
            ),
            env,
        )
        assert submitted.returncode == 0, (submitted.stdout, submitted.stderr)
        assert queue_path.exists(), "submit did not create its durable entry"

        drained = _run(
            [sys.executable, str(DISPATCH), "drain", "--capacity-wait-s", "0", "--json"],
            env,
            cwd=tmp,
        )
        assert drained.returncode == 0, (drained.stdout, drained.stderr)
        payload = json.loads(drained.stdout)
        assert payload["launched"] == 0, payload
        assert payload["left_queued"] == 1, payload
        assert payload["pending_claims"] == 0, payload
        assert queue_path.exists(), "capacity refusal lost the submit queue entry"
        assert list(queue_dir.glob("*.json")) == [queue_path], "drain duplicated the queue entry"
        assert not list(queue_dir.glob("*.claimed.*")), "drain stranded a claimed carrier"

        held_record = _read_json(tmp / "state" / "runs.d" / f"{dispatch_id}.json")
        assert held_record.get("state") == "queued", held_record
        _release_capacity(env, held_lease)
        launched = _run(
            [sys.executable, str(DISPATCH), "drain", "--capacity-wait-s", "0", "--json"],
            env,
            cwd=tmp,
        )
        assert launched.returncode == 0, (launched.stdout, launched.stderr)
        launched_payload = json.loads(launched.stdout)
        assert launched_payload["launched"] == 1, launched_payload
        assert not queue_path.exists(), "restored submit entry was not consumed"
        _wait_for(lambda: marker.exists() and marker.read_text(encoding="utf-8") == "once")
        _wait_for(
            lambda: _read_json(tmp / f"{dispatch_id}.status.json").get("state") == "complete"
            and _read_json(tmp / f"{dispatch_id}.status.json").get("worker_alive") is not True
        )


def test_detached_default_wait_is_zero_without_overriding_explicit_budgets() -> None:
    old = os.environ.pop("GOALFLIGHT_CAPACITY_WAIT_S", None)
    try:
        detached = SimpleNamespace(
            priority="normal", capacity_wait_s=None, foreground=False, from_queue=False
        )
        foreground = SimpleNamespace(
            priority="normal", capacity_wait_s=None, foreground=True, from_queue=False
        )
        explicit = SimpleNamespace(
            priority="normal", capacity_wait_s=7.0, foreground=False, from_queue=False
        )
        assert D._capacity_wait_seconds(detached) == 0.0
        assert D._capacity_wait_seconds(foreground) == 600.0
        assert D._capacity_wait_seconds(explicit) == 7.0
        os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = "4.5"
        assert D._capacity_wait_seconds(detached) == 4.5
    finally:
        if old is None:
            os.environ.pop("GOALFLIGHT_CAPACITY_WAIT_S", None)
        else:
            os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = old


if __name__ == "__main__":
    test_detached_capacity_refusal_is_durable_and_replayable()
    test_capacity_refusal_guard_mirrors()
    test_detached_capacity_wait_interrupt_does_not_enqueue()
    test_submit_drain_capacity_refusal_still_restores_one_entry()
    test_detached_default_wait_is_zero_without_overriding_explicit_budgets()
    print("ok: dispatch capacity refusal requeue")
