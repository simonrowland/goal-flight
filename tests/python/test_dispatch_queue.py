#!/usr/bin/env python3
"""Regression tests for queued dispatch submit/drain mode."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("dispatch queue tests launch POSIX bash workers")

import argparse
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
STATUS = ROOT / "scripts" / "goalflight_status.py"
CAPACITY = ROOT / "scripts" / "goalflight_capacity.py"
FAKE_ACP_AGENT = ROOT / "tests" / "fixtures" / "acp_fake_agent.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    return env


def _run(cmd: list[str], env: dict[str, str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _wait_for(predicate, *, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _status(env: dict[str, str]) -> dict:
    proc = _run([sys.executable, str(STATUS), "--json"], env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _record(payload: dict, dispatch_id: str) -> dict | None:
    for row in payload["dispatch"].get("records", []):
        if row.get("dispatch_id") == dispatch_id:
            return row
    return None


def _write_fake_codex_acp(tmp: Path) -> Path:
    wrapper = tmp / "codex-acp"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_ACP_AGENT))}\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _drain_args(queue: Path, *, limit: int = 0) -> argparse.Namespace:
    return argparse.Namespace(
        queue_dir=str(queue),
        capacity_wait_s=0.0,
        claim_stale_s=D.QUEUE_CLAIM_STALE_S,
        limit=limit,
    )


def _dead_producer_fields(*, at: str | None = None) -> dict:
    stamp = at or D.goalflight_ledger.utc_now()
    pid = 99_999_991
    return {
        "queue_worker_pid": pid,
        "queue_worker_identity": {"pid": pid},
        "queue_worker_pgid": pid,
        "queue_worker_group_leader_identity": {"pid": pid},
        "queue_worker_identity_snapshot_at": stamp,
        "queue_producer_group_contract": True,
        "queue_producer_group_contract_enforced": True,
        "queue_tail_flock_contract": True,
    }


@contextlib.contextmanager
def _process_snapshot(rows: list[dict] | None):
    original = D._process_snapshot
    D._process_snapshot = lambda: rows
    try:
        yield
    finally:
        D._process_snapshot = original


@contextlib.contextmanager
def _sleeping_worker():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(lambda: D.goalflight_ledger.process_identity(proc.pid) is not None, timeout=5.0)
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)


def _write_queue_entry(
    queue: Path,
    dispatch_id: str,
    *,
    filename: str,
    priority: str | None = "normal",
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> Path:
    request = {
        "agent": "test-dispatch",
        "priority": priority,
        "cwd": str(ROOT),
        "tail": str(queue.parent.parent / f"{dispatch_id}.tail"),
        "status_json": str(queue.parent.parent / f"{dispatch_id}.status.json"),
    }
    if priority is None:
        request.pop("priority")
    path = queue / f"{filename}.json"
    D._write_json_atomic(
        path,
        {
            "schema": D.DISPATCH_QUEUE_SCHEMA,
            "state": "queued",
            "dispatch_id": dispatch_id,
            "agent": "test-dispatch",
            "shape": "bash",
            "project_root": str(ROOT),
            "process_cwd": str(ROOT),
            "created_at": created_at,
            "updated_at": created_at,
            "queue_path": str(path),
            "dispatch_argv": [
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                dispatch_id,
                "--tail",
                str(queue.parent.parent / f"{dispatch_id}.tail"),
                "--status-json",
                str(queue.parent.parent / f"{dispatch_id}.status.json"),
                "--cwd",
                str(ROOT),
                "--",
                sys.executable,
                "-c",
                "print('COMPLETE: queued test')",
            ],
            "request": request,
        },
    )
    return path


@contextlib.contextmanager
def _record_drain_launch_order(order: list[str]):
    old_run = D.subprocess.run

    def fake_run(argv, **_kwargs):
        argv = list(argv)
        try:
            dispatch_id = argv[argv.index("--dispatch-id") + 1]
            queue_launch_token = argv[argv.index("--queue-launch-token") + 1]
        except (ValueError, IndexError):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        order.append(dispatch_id)
        D.goalflight_ledger.write_record(
            {
                "schema": D.goalflight_ledger.SCHEMA,
                "dispatch_id": dispatch_id,
                "agent": "test-dispatch",
                "engine": "test-dispatch",
                "shape": "bash",
                "transport": "dispatch",
                "project_root": str(ROOT),
                "worker_pid": os.getpid(),
                "worker_identity": D.goalflight_ledger.process_identity(os.getpid()),
                "stdout_path": str(ROOT / "test.tail"),
                "status_path": str(ROOT / "test.status.json"),
                "state": "running",
                "terminal_state": "unknown",
                "queue_launch_token": queue_launch_token,
                "started_at": D.goalflight_ledger.utc_now(),
            }
        )
        return subprocess.CompletedProcess(argv, 0, stdout=f"DISPATCH-LAUNCHED {dispatch_id}\n", stderr="")

    D.subprocess.run = fake_run
    try:
        yield
    finally:
        D.subprocess.run = old_run


def test_submit_records_replayable_request_without_capacity_acquire() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        old_acquire = D._acquire_capacity
        try:
            os.environ.clear()
            os.environ.update(env)

            def fail_acquire(*_args, **_kwargs):
                raise AssertionError("submit must not acquire capacity")

            D._acquire_capacity = fail_acquire
            started = time.time()
            rc = D.main(
                [
                    "--agent",
                    "test-dispatch",
                    "--submit",
                    "--no-drain-on-submit",
                    "--dispatch-id",
                    "submit-fast",
                    "--tail",
                    str(tmp / "submit.tail"),
                    "--status-json",
                    str(tmp / "submit.status.json"),
                    "--cwd",
                    str(ROOT),
                    "--",
                    sys.executable,
                    "-c",
                    "print('COMPLETE: should launch later')",
                ]
            )
            elapsed = time.time() - started
        finally:
            D._acquire_capacity = old_acquire
            os.environ.clear()
            os.environ.update(old_env)
        assert rc == 0
        assert elapsed < 1.0, elapsed
        queue_path = tmp / "state" / "dispatch-queue" / "submit-fast.json"
        assert queue_path.exists(), "queued request missing"
        entry = json.loads(queue_path.read_text(encoding="utf-8"))
        assert entry["dispatch_id"] == "submit-fast", entry
        assert entry["dispatch_argv"], entry
        status = json.loads((tmp / "submit.status.json").read_text(encoding="utf-8"))
        assert status["state"] == "queued", status
        row = _record(_status(env), "submit-fast")
        assert row and row.get("classification") == "queued_capacity", row


def test_submit_default_runs_one_drain_pass_after_queue_write() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        old_drain = D._drain_queue_once
        calls = []
        try:
            os.environ.clear()
            os.environ.update(env)

            def fake_drain(args):
                calls.append(args)
                return {
                    "schema": "test",
                    "queue_dir": args.queue_dir,
                    "launched": 0,
                    "left_queued": 0,
                    "failed": 0,
                    "remaining": 1,
                    "pending_claims": 0,
                    "recovered_claims": {"restored": 0, "failed": 0},
                    "details": [],
                }

            D._drain_queue_once = fake_drain
            rc = D.main(
                [
                    "--agent",
                    "test-dispatch",
                    "--submit",
                    "--dispatch-id",
                    "submit-drain-default",
                    "--tail",
                    str(tmp / "default.tail"),
                    "--status-json",
                    str(tmp / "default.status.json"),
                    "--cwd",
                    str(ROOT),
                    "--",
                    sys.executable,
                    "-c",
                    "print('COMPLETE: default drain later')",
                ]
            )
        finally:
            D._drain_queue_once = old_drain
            os.environ.clear()
            os.environ.update(old_env)
        assert rc == 0
        assert len(calls) == 1
        assert Path(calls[0].queue_dir) == tmp / "state" / "dispatch-queue"
        assert calls[0].capacity_wait_s == 0.0
        assert calls[0].limit == 1
        assert (tmp / "state" / "dispatch-queue" / "submit-drain-default.json").exists()


def test_submit_drain_on_submit_error_does_not_fail_submit() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        old_drain = D._drain_queue_once
        stderr = io.StringIO()
        try:
            os.environ.clear()
            os.environ.update(env)

            def fail_drain(_args):
                raise RuntimeError("synthetic drain failure")

            D._drain_queue_once = fail_drain
            with contextlib.redirect_stderr(stderr):
                rc = D.main(
                    [
                        "--agent",
                        "test-dispatch",
                        "--submit",
                        "--dispatch-id",
                        "submit-drain-fails",
                        "--tail",
                        str(tmp / "drain-fails.tail"),
                        "--status-json",
                        str(tmp / "drain-fails.status.json"),
                        "--cwd",
                        str(ROOT),
                        "--",
                        sys.executable,
                        "-c",
                        "print('COMPLETE: drain failure stays queued')",
                    ]
                )
        finally:
            D._drain_queue_once = old_drain
            os.environ.clear()
            os.environ.update(old_env)
        assert rc == 0
        assert "drain-on-submit warning" in stderr.getvalue()
        assert (tmp / "state" / "dispatch-queue" / "submit-drain-fails.json").exists()


def test_submit_default_drain_launches_once_and_duplicate_submit_does_not_double_launch() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        marker = tmp / "default-drain-count.txt"
        worker_code = (
            "from pathlib import Path; import time\n"
            f"p=Path({str(marker)!r})\n"
            "p.write_text((p.read_text() if p.exists() else '') + 'x')\n"
            "print('COMPLETE: default drain launched', flush=True)\n"
            "time.sleep(2.0)\n"
        )
        cmd = [
            sys.executable,
            str(DISPATCH),
            "--agent",
            "test-dispatch",
            "--submit",
            "--dispatch-id",
            "submit-default-launch",
            "--tail",
            str(tmp / "default-launch.tail"),
            "--status-json",
            str(tmp / "default-launch.status.json"),
            "--cwd",
            str(ROOT),
            "--",
            sys.executable,
            "-c",
            worker_code,
        ]
        first = _run(cmd, env)
        assert first.returncode == 0, (first.stdout, first.stderr)
        assert _wait_for(lambda: marker.exists() and marker.read_text() == "x"), first.stdout
        assert not (tmp / "state" / "dispatch-queue" / "submit-default-launch.json").exists()

        duplicate = _run(cmd, env)
        assert duplicate.returncode == 64, (duplicate.stdout, duplicate.stderr)
        assert "already has a non-terminal ledger record" in duplicate.stderr
        time.sleep(0.3)
        assert marker.read_text() == "x", "duplicate submit launched the worker again"


def test_submit_is_idempotent_for_matching_args_and_rejects_collisions() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        cmd = [
            sys.executable,
            str(DISPATCH),
            "--agent",
            "test-dispatch",
            "--submit",
            "--no-drain-on-submit",
            "--dispatch-id",
            "submit-idempotent",
            "--tail",
            str(tmp / "idem.tail"),
            "--status-json",
            str(tmp / "idem.status.json"),
            "--cwd",
            str(ROOT),
            "--",
            sys.executable,
            "-c",
            "print('COMPLETE: same')",
        ]
        first = _run(cmd, env)
        assert first.returncode == 0, (first.stdout, first.stderr)
        second = _run(cmd, env)
        assert second.returncode == 0, (second.stdout, second.stderr)
        assert "STATUS: queued already submit-idempotent" in second.stdout
        collision = _run([*cmd[:-1], "print('COMPLETE: different')"], env)
        assert collision.returncode == 64, (collision.stdout, collision.stderr)
        assert "queued request already exists" in collision.stderr


def test_submit_ignores_matching_failed_claim_tombstone_for_requeue() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        cmd = [
            sys.executable,
            str(DISPATCH),
            "--agent",
            "test-dispatch",
            "--submit",
            "--no-drain-on-submit",
            "--dispatch-id",
            "submit-after-failed-tombstone",
            "--tail",
            str(tmp / "failed-tombstone.tail"),
            "--status-json",
            str(tmp / "failed-tombstone.status.json"),
            "--cwd",
            str(ROOT),
            "--",
            sys.executable,
            "-c",
            "print('COMPLETE: same after failed tombstone')",
        ]
        first = _run(cmd, env)
        assert first.returncode == 0, (first.stdout, first.stderr)
        queue_path = tmp / "state" / "dispatch-queue" / "submit-after-failed-tombstone.json"
        entry = json.loads(queue_path.read_text(encoding="utf-8"))
        entry["state"] = "failed"
        failed = queue_path.with_name(f"{queue_path.name}.claimed-123.failed")
        failed.write_text(json.dumps(entry), encoding="utf-8")
        queue_path.unlink()

        second = _run(cmd, env)
        assert second.returncode == 0, (second.stdout, second.stderr)
        assert "DISPATCH-QUEUED" in second.stdout, second.stdout
        assert queue_path.exists(), "failed tombstone blocked fresh requeue"


def test_duplicate_submit_runs_opportunistic_drain() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        old_drain = D._drain_queue_once
        calls = []
        try:
            os.environ.clear()
            os.environ.update(env)

            seed_rc = D.main(
                [
                    "--agent",
                    "test-dispatch",
                    "--submit",
                    "--no-drain-on-submit",
                    "--dispatch-id",
                    "duplicate-drain-nudge",
                    "--tail",
                    str(tmp / "dup-drain.tail"),
                    "--status-json",
                    str(tmp / "dup-drain.status.json"),
                    "--cwd",
                    str(ROOT),
                    "--",
                    sys.executable,
                    "-c",
                    "print('COMPLETE: duplicate drain nudge')",
                ]
            )
            assert seed_rc == 0

            def fake_drain(args):
                calls.append(args)
                return {
                    "schema": "test",
                    "queue_dir": args.queue_dir,
                    "launched": 0,
                    "left_queued": 0,
                    "failed": 0,
                    "remaining": 1,
                    "pending_claims": 0,
                    "recovered_claims": {"restored": 0, "cleared": 0, "pending_launch": 0},
                    "details": [],
                }

            D._drain_queue_once = fake_drain
            duplicate_rc = D.main(
                [
                    "--agent",
                    "test-dispatch",
                    "--submit",
                    "--dispatch-id",
                    "duplicate-drain-nudge",
                    "--tail",
                    str(tmp / "dup-drain.tail"),
                    "--status-json",
                    str(tmp / "dup-drain.status.json"),
                    "--cwd",
                    str(ROOT),
                    "--",
                    sys.executable,
                    "-c",
                    "print('COMPLETE: duplicate drain nudge')",
                ]
            )
        finally:
            D._drain_queue_once = old_drain
            os.environ.clear()
            os.environ.update(old_env)
        assert duplicate_rc == 0
        assert len(calls) == 1, "duplicate submit did not retry drain-on-submit"


def test_concurrent_submit_same_id_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        cmd = [
            sys.executable,
            str(DISPATCH),
            "--agent",
            "test-dispatch",
            "--submit",
            "--no-drain-on-submit",
            "--dispatch-id",
            "submit-race",
            "--tail",
            str(tmp / "race.tail"),
            "--status-json",
            str(tmp / "race.status.json"),
            "--cwd",
            str(ROOT),
            "--",
            sys.executable,
            "-c",
            "print('COMPLETE: same race')",
        ]
        procs = [
            subprocess.Popen(
                cmd,
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(8)
        ]
        results = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=30.0)
            results.append((proc.returncode, stdout, stderr))
        for rc, _stdout, _stderr in results:
            assert rc == 0, (rc, _stdout, _stderr)
            assert "Traceback" not in _stdout + _stderr
        queue_dir = tmp / "state" / "dispatch-queue"
        entries = sorted(queue_dir.glob("submit-race*.json"))
        assert len(entries) == 1, [p.name for p in entries]
        assert not list(queue_dir.glob("submit-race.json.tmp.*"))


def test_submit_write_error_is_clean() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        state = tmp / "state"
        state.mkdir()
        state.chmod(0o500)
        try:
            proc = _run(
                [
                    sys.executable,
                    str(DISPATCH),
                    "--agent",
                    "test-dispatch",
                    "--submit",
                    "--no-drain-on-submit",
                    "--dispatch-id",
                    "submit-readonly",
                    "--tail",
                    str(tmp / "readonly.tail"),
                    "--status-json",
                    str(tmp / "readonly.status.json"),
                    "--cwd",
                    str(ROOT),
                    "--",
                    sys.executable,
                    "-c",
                    "print('COMPLETE: should not queue')",
                ],
                env,
            )
            assert proc.returncode != 0, (proc.stdout, proc.stderr)
            assert "goalflight_dispatch: submit failed for submit-readonly" in proc.stderr
            assert "Traceback" not in proc.stdout + proc.stderr
            assert not (tmp / "readonly.status.json").exists()
            files = [p for p in state.rglob("*") if p.is_file()]
            assert files == [], [str(p) for p in files]
        finally:
            state.chmod(0o700)


def test_submit_status_write_error_removes_queue_entry() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        blocked = tmp / "blocked-status"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            proc = _run(
                [
                    sys.executable,
                    str(DISPATCH),
                    "--agent",
                    "test-dispatch",
                    "--submit",
                    "--no-drain-on-submit",
                    "--dispatch-id",
                    "submit-status-fails",
                    "--tail",
                    str(tmp / "status-fails.tail"),
                    "--status-json",
                    str(blocked / "status.json"),
                    "--cwd",
                    str(ROOT),
                    "--",
                    sys.executable,
                    "-c",
                    "print('COMPLETE: should not leave queue')",
                ],
                env,
            )
            assert proc.returncode != 0, (proc.stdout, proc.stderr)
            assert "goalflight_dispatch: submit failed for submit-status-fails" in proc.stderr
            assert "Traceback" not in proc.stdout + proc.stderr
            assert not (tmp / "state" / "dispatch-queue" / "submit-status-fails.json").exists()
            assert not (blocked / "status.json").exists()
        finally:
            blocked.chmod(0o700)


def test_submit_rejects_active_waiting_capacity_ledger() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": "dup-wait",
                    "agent": "test-dispatch",
                    "engine": "test-dispatch",
                    "shape": "bash",
                    "account": "default",
                    "transport": "dispatch",
                    "project_root": str(ROOT),
                    "worker_pid": None,
                    "stdout_path": str(tmp / "waiting.tail"),
                    "status_path": str(tmp / "waiting.status.json"),
                    "state": "waiting_capacity",
                    "started_at": D.goalflight_ledger.utc_now(),
                }
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        proc = _run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--submit",
                "--no-drain-on-submit",
                "--dispatch-id",
                "dup-wait",
                "--tail",
                str(tmp / "dup.tail"),
                "--status-json",
                str(tmp / "dup.status.json"),
                "--cwd",
                str(ROOT),
                "--",
                sys.executable,
                "-c",
                "print('COMPLETE: should not queue')",
            ],
            env,
        )
        assert proc.returncode == 64, (proc.stdout, proc.stderr)
        assert "already has a non-terminal ledger record" in proc.stderr
        assert not (tmp / "state" / "dispatch-queue" / "dup-wait.json").exists()


def test_drain_launches_queued_request_once_and_exits() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        marker = tmp / "launch-count.txt"
        worker_code = (
            "from pathlib import Path; import time\n"
            f"p=Path({str(marker)!r})\n"
            "p.write_text((p.read_text() if p.exists() else '') + 'x')\n"
            "print('COMPLETE: queued launch done', flush=True)\n"
            "time.sleep(0.2)\n"
        )
        submit = _run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--submit",
                "--no-drain-on-submit",
                "--dispatch-id",
                "drain-launch",
                "--tail",
                str(tmp / "drain.tail"),
                "--status-json",
                str(tmp / "drain.status.json"),
                "--cwd",
                str(ROOT),
                "--",
                sys.executable,
                "-c",
                worker_code,
            ],
            env,
        )
        assert submit.returncode == 0, (submit.stdout, submit.stderr)
        drain = _run([sys.executable, str(DISPATCH), "drain", "--capacity-wait-s", "0", "--json"], env)
        assert drain.returncode == 0, (drain.stdout, drain.stderr)
        payload = json.loads(drain.stdout)
        assert payload["launched"] == 1, payload
        assert not (tmp / "state" / "dispatch-queue" / "drain-launch.json").exists()
        assert _wait_for(lambda: marker.exists() and marker.read_text() == "x"), "worker did not run once"

        second = _run([sys.executable, str(DISPATCH), "drain", "--capacity-wait-s", "0", "--json"], env)
        assert second.returncode == 0, (second.stdout, second.stderr)
        assert json.loads(second.stdout)["launched"] == 0, second.stdout
        time.sleep(0.5)
        assert marker.read_text() == "x", "second drain double-launched the request"


def test_drain_waits_for_submit_status_recording() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        env["GOALFLIGHT_TEST_SUBMIT_STATUS_DELAY_S"] = "2.0"
        marker = tmp / "delayed-drain-ran.txt"
        status_json = tmp / "delayed.status.json"
        queue_path = tmp / "state" / "dispatch-queue" / "submit-drain-race.json"
        worker_code = (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran')\n"
            "print('COMPLETE: delayed drain')\n"
        )
        submit = subprocess.Popen(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--submit",
                "--no-drain-on-submit",
                "--dispatch-id",
                "submit-drain-race",
                "--tail",
                str(tmp / "delayed.tail"),
                "--status-json",
                str(status_json),
                "--cwd",
                str(ROOT),
                "--",
                sys.executable,
                "-c",
                worker_code,
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for(lambda: queue_path.exists(), timeout=5.0), "submit did not expose queue file"
            started = time.time()
            drain = _run([sys.executable, str(DISPATCH), "drain", "--capacity-wait-s", "0", "--json"], env)
            elapsed = time.time() - started
            submit_stdout, submit_stderr = submit.communicate(timeout=30.0)
        finally:
            if submit.poll() is None:
                submit.kill()
                submit.communicate(timeout=5.0)
        assert submit.returncode == 0, (submit.returncode, submit_stdout, submit_stderr)
        assert drain.returncode == 0, (drain.stdout, drain.stderr)
        assert elapsed >= 1.0, f"drain did not wait for submit record lock: {elapsed:.3f}s"
        payload = json.loads(drain.stdout)
        assert payload["launched"] == 1, payload
        final_status = json.loads(status_json.read_text(encoding="utf-8"))
        assert final_status["state"] != "queued", final_status
        assert not queue_path.exists()


def test_drain_write_error_is_json_error_without_traceback() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        state = tmp / "state"
        state.mkdir()
        state.chmod(0o500)
        try:
            proc = _run([sys.executable, str(DISPATCH), "drain", "--json"], env)
            assert proc.returncode != 0, (proc.stdout, proc.stderr)
            assert "Traceback" not in proc.stdout + proc.stderr
            payload = json.loads(proc.stdout)
            assert payload["failed"] == 1, payload
            assert payload["error"].startswith("PermissionError:"), payload
        finally:
            state.chmod(0o700)


def test_acp_submit_then_drain_replays_from_queue() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp(tmp)
        env = _env(tmp)
        env.pop("GOALFLIGHT_STEER_FILE", None)
        env["PATH"] = f"{tmp}{os.pathsep}{env.get('PATH', '')}"
        marker = tmp / "acp-test-complete.txt"
        env["GOALFLIGHT_TEST_MODE"] = "1"
        env["GOALFLIGHT_TEST_ACP_DISPATCH_COMPLETE_FILE"] = str(marker)
        env["GOALFLIGHT_TEST_ACP_DISPATCH_SLEEP_AFTER_RUNNING_S"] = "4"
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "blocked_none"
        env["GOALFLIGHT_FAKE_ACP_INTERVAL"] = "0.01"
        env["GOALFLIGHT_ACP_PYTHON"] = sys.executable
        submit = _run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "codex-acp",
                "--shape",
                "acp",
                "--submit",
                "--no-drain-on-submit",
                "--dispatch-id",
                "acp-drain",
                "--prompt",
                "hello",
                "--tail",
                str(tmp / "acp.tail"),
                "--status-json",
                str(tmp / "acp.status.json"),
                "--cwd",
                str(ROOT),
                "--max-idle-secs",
                "5",
                "--poll-secs",
                "0.1",
            ],
            env,
        )
        assert submit.returncode == 0, (submit.stdout, submit.stderr)
        started = time.time()
        drain = _run(
            [sys.executable, str(DISPATCH), "drain", "--capacity-wait-s", "0", "--json"],
            env,
            timeout=45.0,
        )
        elapsed = time.time() - started
        assert drain.returncode == 0, (drain.stdout, drain.stderr)
        assert elapsed < 3.0, f"ACP drain waited for child completion: {elapsed:.3f}s"
        payload = json.loads(drain.stdout)
        assert payload["launched"] == 1, payload
        assert payload["remaining"] == 0, payload
        assert payload["pending_claims"] == 0, payload
        assert not (tmp / "state" / "dispatch-queue" / "acp-drain.json").exists()
        assert marker.exists(), "test ACP launch hook did not run"
        assert _wait_for(
            lambda: json.loads((tmp / "acp.status.json").read_text(encoding="utf-8")).get("state")
            == "complete",
            timeout=6.0,
        )


def test_drain_leaves_request_queued_when_capacity_full() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "1"
        held = _run(
            [
                sys.executable,
                str(CAPACITY),
                "acquire",
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                "held-capacity",
                "--project-root",
                str(ROOT),
                "--controller-pid",
                str(os.getpid()),
                "--ttl-s",
                "60",
            ],
            env,
        )
        assert held.returncode == 0, held.stderr
        marker = tmp / "should-not-run"
        submit = _run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--submit",
                "--no-drain-on-submit",
                "--dispatch-id",
                "drain-no-capacity",
                "--tail",
                str(tmp / "no-cap.tail"),
                "--status-json",
                str(tmp / "no-cap.status.json"),
                "--cwd",
                str(ROOT),
                "--",
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            ],
            env,
        )
        assert submit.returncode == 0, (submit.stdout, submit.stderr)
        drain = _run([sys.executable, str(DISPATCH), "drain", "--capacity-wait-s", "0", "--json"], env)
        assert drain.returncode == 0, (drain.stdout, drain.stderr)
        payload = json.loads(drain.stdout)
        assert payload["launched"] == 0, payload
        assert payload["remaining"] == 1, payload
        assert (tmp / "state" / "dispatch-queue" / "drain-no-capacity.json").exists()
        assert not marker.exists(), "worker launched despite full capacity"
        status = json.loads((tmp / "no-cap.status.json").read_text(encoding="utf-8"))
        assert status["state"] == "queued", status
        row = _record(_status(env), "drain-no-capacity")
        assert row and row.get("classification") == "queued_capacity", row


def test_drain_orders_by_priority_then_created_at_fifo() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        launched: list[str] = []
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            paths = [
                _write_queue_entry(
                    queue,
                    "bulk-new",
                    filename="000-bulk-new",
                    priority="bulk",
                    created_at="2026-01-01T00:05:00+00:00",
                ),
                _write_queue_entry(
                    queue,
                    "normal-new",
                    filename="001-normal-new",
                    priority="normal",
                    created_at="2026-01-01T00:04:00+00:00",
                ),
                _write_queue_entry(
                    queue,
                    "critical-new",
                    filename="002-critical-new",
                    priority="critical",
                    created_at="2026-01-01T00:03:00+00:00",
                ),
                _write_queue_entry(
                    queue,
                    "bulk-old",
                    filename="003-bulk-old",
                    priority="bulk",
                    created_at="2026-01-01T00:00:00+00:00",
                ),
                _write_queue_entry(
                    queue,
                    "normal-old",
                    filename="004-normal-old",
                    priority="normal",
                    created_at="2026-01-01T00:01:00+00:00",
                ),
                _write_queue_entry(
                    queue,
                    "critical-old",
                    filename="005-critical-old",
                    priority="critical",
                    created_at="2026-01-01T00:02:00+00:00",
                ),
            ]
            for offset, path in enumerate(paths):
                stamp = time.time() + len(paths) - offset
                os.utime(path, (stamp, stamp))
            with _record_drain_launch_order(launched):
                payload = D._drain_queue_once(_drain_args(queue))
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        expected = ["critical-old", "critical-new", "normal-old", "normal-new", "bulk-old", "bulk-new"]
        assert launched == expected
        assert payload["launched"] == len(expected), payload
        assert payload["remaining"] == 0, payload
        assert [row["dispatch_id"] for row in payload["details"]] == expected


def test_drain_degrades_unknown_priority_to_normal_and_survives_bad_json_prescan() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        launched: list[str] = []
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            _write_queue_entry(
                queue,
                "bulk-oldest",
                filename="000-bulk-oldest",
                priority="bulk",
                created_at="2026-01-01T00:00:00+00:00",
            )
            _write_queue_entry(
                queue,
                "missing-priority",
                filename="010-missing-priority",
                priority=None,
                created_at="2026-01-01T00:01:00+00:00",
            )
            _write_queue_entry(
                queue,
                "garbage-priority",
                filename="011-garbage-priority",
                priority="urgent",
                created_at="2026-01-01T00:02:00+00:00",
            )
            bad_json = queue / "012-bad-json.json"
            bad_json.write_text("{not json", encoding="utf-8")
            _write_queue_entry(
                queue,
                "critical-newer",
                filename="999-critical-newer",
                priority="critical",
                created_at="2026-01-01T00:03:00+00:00",
            )
            with _record_drain_launch_order(launched):
                payload = D._drain_queue_once(_drain_args(queue, limit=4))
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert launched == ["critical-newer", "missing-priority", "garbage-priority"], payload
        assert payload["launched"] == 3, payload
        assert payload["failed"] == 1, payload
        assert payload["remaining"] == 1, payload
        assert (tmp / "state" / "dispatch-queue" / "000-bulk-oldest.json").exists()
        assert list((tmp / "state" / "dispatch-queue").glob("012-bad-json.json.claimed-*.failed"))


def test_drain_does_not_tombstone_valid_entry_on_stale_prescan_read_error() -> None:
    """Regression: the pre-scan read is used only for sort ordering; a stale
    pre-scan read_error (e.g. the entry was mid-restore by a concurrent
    stale-claim recovery at scan time) must NOT tombstone a now-valid entry.
    Readability is decided authoritatively from the claimed file."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        old_candidate = D._queue_entry_drain_candidate
        launched: list[str] = []
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            _write_queue_entry(
                queue,
                "stale-scan-survivor",
                filename="000-stale-scan-survivor",
                priority="normal",
                created_at="2026-01-01T00:00:00+00:00",
            )

            def stale_candidate(path):
                # Keep the real (valid) sort key but force a stale pre-scan miss.
                sort_key, p, _entry, _err = old_candidate(path)
                return (sort_key, p, None, "StaleSimulated")

            D._queue_entry_drain_candidate = stale_candidate
            with _record_drain_launch_order(launched):
                payload = D._drain_queue_once(_drain_args(queue))
        finally:
            D._queue_entry_drain_candidate = old_candidate
            os.environ.clear()
            os.environ.update(old_env)
        assert launched == ["stale-scan-survivor"], payload
        assert payload["launched"] == 1, payload
        assert payload["failed"] == 0, payload
        assert not list((tmp / "state" / "dispatch-queue").glob("*.failed")), payload


def test_stale_claim_without_worker_record_is_recovered() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            claim = queue / "recover-me.json.claimed-123"
            claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "dispatch_id": "recover-me",
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "task_ids": ["recover-me-task"],
                    }
                ),
                encoding="utf-8",
            )
            os.utime(claim, (time.time() - 60, time.time() - 60))
            result = D._recover_claimed_queue_entries(queue, stale_s=0)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result["restored"] == 1, result
        assert (tmp / "state" / "dispatch-queue" / "recover-me.json").exists()
        assert not claim.exists()


def test_failed_claim_tombstone_is_not_recovered() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            failed = queue / "bad.json.claimed-123.failed"
            failed.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "dispatch_id": "bad",
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "state": "failed",
                    }
                ),
                encoding="utf-8",
            )
            os.utime(failed, (time.time() - 60, time.time() - 60))
            result = D._recover_claimed_queue_entries(queue, stale_s=0)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result["restored"] == 0 and result["cleared"] == 0 and result["pending_launch"] == 0, result
        assert failed.exists()
        assert not (tmp / "state" / "dispatch-queue" / "bad.json").exists()


def test_fresh_token_only_claim_waits_for_stale_window() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            claim = queue / "fresh-token-only.json.claimed-123"
            claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": "fresh-token-only",
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "queue_launch_token": "fresh-token",
                    }
                ),
                encoding="utf-8",
            )
            result = D._recover_claimed_queue_entries(queue, stale_s=3600)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result["restored"] == 0 and result["cleared"] == 0 and result["pending_launch"] == 1, result
        assert claim.exists()
        assert not (queue / "fresh-token-only.json").exists()


def test_stale_claim_launch_token_requires_matching_worker_record() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            token_only_claim = queue / "token-only.json.claimed-123"
            token_only_claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": "token-only",
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "task_ids": ["token-only-task"],
                        "queue_launch_token": "token-only",
                    }
                ),
                encoding="utf-8",
            )
            os.utime(token_only_claim, (time.time() - 60, time.time() - 60))
            restored = D._recover_claimed_queue_entries(queue, stale_s=0)
            assert restored["restored"] == 1 and restored["cleared"] == 0, restored
            restored_path = queue / "token-only.json"
            assert restored_path.exists()
            restored_entry = json.loads(restored_path.read_text(encoding="utf-8"))
            assert restored_entry["state"] == "queued", restored_entry
            assert "queue_launch_token" not in restored_entry, restored_entry
            assert restored_entry.get("claim_recovery_count") == 1, restored_entry
            assert not token_only_claim.exists()
            restored_path.unlink()

            # Spawn intent without a durable PID/producer set is indeterminate;
            # stale time never upgrades it to death (round-5 fail closed).
            crash_claim = queue / "crash-window.json.claimed-123"
            crash_status = tmp / "crash-window.status.json"
            crash_tail = tmp / "crash-window.tail"
            crash_claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": "crash-window",
                        "agent": "test-dispatch",
                        "shape": "bash",
                        "project_root": str(ROOT),
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "task_ids": ["b-065-crash"],
                        "request": {
                            "agent": "test-dispatch",
                            "cwd": str(ROOT),
                            "tail": str(crash_tail),
                            "status_json": str(crash_status),
                            "task_ids": ["b-065-crash"],
                        },
                        "queue_launch_token": "intent-token",
                        "queue_launch_started": True,
                        "queue_worker_spawn_intent": True,
                        "queue_worker_spawn_intent_at": D.goalflight_ledger.utc_now(),
                    }
                ),
                encoding="utf-8",
            )
            os.utime(crash_claim, (time.time() - 60, time.time() - 60))
            recovered = D._recover_claimed_queue_entries(queue, stale_s=0)
            assert recovered["restored"] == 0 and recovered["cleared"] == 0, recovered
            assert recovered["pending_launch"] >= 1, recovered
            assert crash_claim.exists()
            assert not (queue / "crash-window.json").exists()
            assert not (tmp / "state" / "runs.d" / "crash-window.json").exists()
            assert not crash_status.exists()
            crash_claim.unlink()

            matched_claim = queue / "matched.json.claimed-123"
            matched_claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "dispatch_id": "matched",
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "queue_launch_token": "matched-token",
                        "queue_launch_started": True,
                    }
                ),
                encoding="utf-8",
            )
            os.utime(matched_claim, (time.time() - 60, time.time() - 60))
            with _sleeping_worker() as worker:
                D.goalflight_ledger.write_record(
                    {
                        "schema": D.goalflight_ledger.SCHEMA,
                        "dispatch_id": "matched",
                        "state": "running",
                        "terminal_state": "unknown",
                        "worker_pid": worker.pid,
                        "worker_identity": D.goalflight_ledger.process_identity(worker.pid),
                        "queue_launch_token": "matched-token",
                        "started_at": D.goalflight_ledger.utc_now(),
                    }
                )
                cleared = D._recover_claimed_queue_entries(queue, stale_s=0)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        # The carrier is redundant but UNLINKED (no task_ids on either side), so
        # fail-closed parks it in quarantine: never deleted, never restored, live
        # record untouched — the same contract pinned for live unlinked carriers
        # by test_b065_unlinked_early_terminal_and_live_carriers_quarantine. The
        # earlier "preserve as pending" expectation assumed INDETERMINATE
        # admission, but the token-matched live record is merged by
        # _entry_with_record_identity, upgrading admission to LIVE, where the
        # designed unlinked outcome is quarantine, not deferral.
        assert cleared["restored"] == 0, cleared  # never re-launch a live dispatch
        assert cleared["cleared"] == 0, cleared
        assert cleared["quarantined"] == 1, cleared
        # pending_launch == 2: the matched ledger row (LIVE → deferred by the
        # carrier-less ledger scan after its carrier was parked) plus the
        # carrier-less "queued" token-only row left over from the phase-1 restore.
        assert cleared["pending_launch"] == 2, cleared
        assert cleared["ledger_terminalized"] == 0, cleared
        assert not matched_claim.exists(), "carrier is parked, not left in the launch glob"
        parked = queue / "quarantine" / "matched.json.claimed-123.quarantined"
        assert parked.exists(), "quarantine retains the envelope (never deletes)"
        parked_entry = json.loads(parked.read_text(encoding="utf-8"))
        assert parked_entry.get("quarantine_reason") == "live_worker_unlinked_claim_carrier", parked_entry
        assert not (tmp / "state" / "dispatch-queue" / "matched.json").exists()
        record = json.loads((tmp / "state" / "runs.d" / "matched.json").read_text(encoding="utf-8"))
        assert record["state"] == "running", record  # live worker record untouched


def test_stale_claim_result_marker_with_rate_limit_text_completes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "queued-result-rate-mention"
            tail = tmp / f"{dispatch_id}.tail"
            status = tmp / f"{dispatch_id}.status.json"
            tail.write_text(
                "work finished\nRESULT: completed; notes mention rate limit handling\n",
                encoding="utf-8",
            )
            claim = queue / f"{dispatch_id}.json.claimed-123"
            claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": dispatch_id,
                        "agent": "codex",
                        "shape": "bash",
                        "project_root": str(ROOT),
                        "dispatch_argv": ["--agent", "codex"],
                        "task_ids": ["queued-result-rate-mention-task"],
                        "request": {
                            "agent": "codex",
                            "cwd": str(ROOT),
                            "tail": str(tail),
                            "status_json": str(status),
                            "task_ids": ["queued-result-rate-mention-task"],
                        },
                        "queue_launch_token": "queued-result-token",
                        "queue_launch_started": True,
                        "queue_worker_spawn_intent": True,
                        "queue_worker_spawn_intent_at": D.goalflight_ledger.utc_now(),
                        **_dead_producer_fields(),
                        "queue_worker_pid": 99_999_991,
                        "queue_worker_identity": {"pid": 99_999_991},
                        "queue_worker_pgid": 99_999_991,
                        "queue_worker_group_leader_identity": {"pid": 99_999_991},
                        "queue_worker_identity_snapshot_at": D.goalflight_ledger.utc_now(),
                        "queue_producer_group_contract": True,
                        "queue_producer_group_contract_enforced": True,
                        "queue_tail_flock_contract": True,
                    }
                ),
                encoding="utf-8",
            )
            os.utime(claim, (time.time() - 60, time.time() - 60))
            original_snapshot = D._process_snapshot
            D._process_snapshot = lambda: []
            try:
                recovered = D._recover_claimed_queue_entries(queue, stale_s=0)
            finally:
                D._process_snapshot = original_snapshot
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert recovered["restored"] == 0 and recovered["cleared"] == 1, recovered
        record = json.loads((tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8"))
        assert record["state"] == "complete", record
        assert record["terminal_state"] == "complete", record
        assert record.get("error", {}).get("message") != "dispatch_worker_rate_limited", record
        status_payload = json.loads(status.read_text(encoding="utf-8"))
        assert status_payload["state"] == "complete", status_payload
        assert status_payload.get("terminal_marker", {}).get("kind") == "RESULT", status_payload


def test_drain_replay_argv_injects_queue_control_flags() -> None:
    """Poison-pair: drain replay must inject _drain_launch_argv queue-control flags.

    Regression guard (audit-r24-1): existing drain tests only checked dispatch_id
    recording; dropping --from-queue / --queue-launch-token / --queue-claim-path /
    --launch-detached / --capacity-wait-s from replay argv would not fail CI.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        captured_argv: list[list[str]] = []
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            _write_queue_entry(queue, "argv-guard", filename="000-argv-guard")
            old_run = D.subprocess.run

            def fake_run(argv, **kwargs):
                argv = list(argv)
                if not any("goalflight_dispatch.py" in str(part) for part in argv):
                    return old_run(argv, **kwargs)
                captured_argv.append(argv)
                try:
                    dispatch_id = argv[argv.index("--dispatch-id") + 1]
                    queue_launch_token = argv[argv.index("--queue-launch-token") + 1]
                except (ValueError, IndexError):
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                D.goalflight_ledger.write_record(
                    {
                        "schema": D.goalflight_ledger.SCHEMA,
                        "dispatch_id": dispatch_id,
                        "agent": "test-dispatch",
                        "engine": "test-dispatch",
                        "shape": "bash",
                        "transport": "dispatch",
                        "project_root": str(ROOT),
                        "worker_pid": os.getpid(),
                        "worker_identity": D.goalflight_ledger.process_identity(os.getpid()),
                        "stdout_path": str(ROOT / "test.tail"),
                        "status_path": str(ROOT / "test.status.json"),
                        "state": "running",
                        "terminal_state": "unknown",
                        "queue_launch_token": queue_launch_token,
                        "started_at": D.goalflight_ledger.utc_now(),
                    }
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=f"DISPATCH-LAUNCHED {dispatch_id}\n",
                    stderr="",
                )

            D.subprocess.run = fake_run
            payload = D._drain_queue_once(_drain_args(queue, limit=1))
        finally:
            D.subprocess.run = old_run
            os.environ.clear()
            os.environ.update(old_env)
        assert payload["launched"] == 1, payload
        assert len(captured_argv) == 1, captured_argv
        argv = captured_argv[0]
        required_flags = (
            "--from-queue",
            "--queue-launch-token",
            "--queue-claim-path",
            "--launch-detached",
            "--capacity-wait-s",
            "--dispatch-id",
        )
        for flag in required_flags:
            assert flag in argv, f"missing queue-control flag {flag!r} in replay argv: {argv}"
        assert argv[argv.index("--dispatch-id") + 1] == "argv-guard"
        assert argv[argv.index("--capacity-wait-s") + 1] == "0.0"
        claim_flag_idx = argv.index("--queue-claim-path")
        claim_path = Path(argv[claim_flag_idx + 1])
        assert claim_path.name.startswith("000-argv-guard.json.claimed-"), claim_path
        token = argv[argv.index("--queue-launch-token") + 1]
        assert token, "drain must inject a non-empty queue launch token"


def test_drain_requires_token_matched_ledger_before_clearing_claim() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        old_run = D.subprocess.run
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            _write_queue_entry(queue, "stdout-only-launch", filename="stdout-only-launch")

            def fake_run(argv, **_kwargs):
                return subprocess.CompletedProcess(
                    list(argv),
                    0,
                    stdout='DISPATCH-LAUNCHED {"dispatch_id":"stdout-only-launch"}\n',
                    stderr="",
                )

            D.subprocess.run = fake_run
            payload = D._drain_queue_once(_drain_args(queue))
        finally:
            D.subprocess.run = old_run
            os.environ.clear()
            os.environ.update(old_env)
        assert payload["launched"] == 0, payload
        assert payload["pending_claims"] == 1, payload
        assert payload["failed"] == 1, payload
        assert list(queue.glob("stdout-only-launch.json.claimed-*")), "claim cleared without ledger confirm"


def test_legacy_claim_dead_worker_without_token_defers_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            worker = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                assert _wait_for(lambda: D.goalflight_ledger.process_identity(worker.pid) is not None, timeout=5.0)
                identity = D.goalflight_ledger.process_identity(worker.pid)
                dead_pid = worker.pid
            finally:
                worker.terminate()
                worker.wait(timeout=5.0)
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": "legacy-dead-worker",
                    "state": "running",
                    "terminal_state": "unknown",
                    "worker_pid": dead_pid,
                    "worker_identity": identity,
                    "task_ids": ["legacy-dead-worker-task"],
                    "started_at": D.goalflight_ledger.utc_now(),
                }
            )
            claim = queue / "legacy-dead-worker.json.claimed-123"
            claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": "legacy-dead-worker",
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "task_ids": ["legacy-dead-worker-task"],
                    }
                ),
                encoding="utf-8",
            )
            os.utime(claim, (time.time() - 60, time.time() - 60))
            with _process_snapshot([]):
                result = D._recover_claimed_queue_entries(queue, stale_s=0)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result["restored"] == 0 and result["cleared"] == 0, result
        assert result["pending_launch"] >= 1, result
        assert not (queue / "legacy-dead-worker.json").exists()
        assert claim.exists()


def test_token_mismatch_recovery_preserves_live_worker_record() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            with _sleeping_worker() as worker:
                D.goalflight_ledger.write_record(
                    {
                        "schema": D.goalflight_ledger.SCHEMA,
                        "dispatch_id": "token-mismatch-live-worker",
                        "state": "running",
                        "terminal_state": "unknown",
                        "worker_pid": worker.pid,
                        "worker_identity": D.goalflight_ledger.process_identity(worker.pid),
                        "queue_launch_token": "different-token",
                        "started_at": D.goalflight_ledger.utc_now(),
                    }
                )
                claim = queue / "token-mismatch-live-worker.json.claimed-123"
                claim.write_text(
                    json.dumps(
                        {
                            "schema": D.DISPATCH_QUEUE_SCHEMA,
                            "state": "claimed",
                            "dispatch_id": "token-mismatch-live-worker",
                            "agent": "test-dispatch",
                            "shape": "bash",
                            "project_root": str(ROOT),
                            "dispatch_argv": ["--agent", "test-dispatch"],
                            "request": {
                                "agent": "test-dispatch",
                                "cwd": str(ROOT),
                                "tail": str(tmp / "token-mismatch.tail"),
                                "status_json": str(tmp / "token-mismatch.status.json"),
                            },
                            "queue_launch_token": "claim-token",
                            "queue_launch_started": True,
                            "queue_worker_spawn_intent": True,
                            "queue_worker_spawn_intent_at": D.goalflight_ledger.utc_now(),
                        }
                    ),
                    encoding="utf-8",
                )
                os.utime(claim, (time.time() - 60, time.time() - 60))
                result = D._recover_claimed_queue_entries(queue, stale_s=0)
                record = json.loads(
                    (tmp / "state" / "runs.d" / "token-mismatch-live-worker.json").read_text(encoding="utf-8")
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result["restored"] == 0 and result["cleared"] == 0 and result["pending_launch"] == 1, result
        assert claim.exists()
        assert record["state"] == "running", record
        assert record.get("terminal_state") == "unknown", record


def test_token_mismatch_recovery_preserves_terminal_worker_record() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            status_path = tmp / "token-mismatch-terminal.status.json"
            tail_path = tmp / "token-mismatch-terminal.tail"
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": "token-mismatch-terminal-worker",
                    "state": "complete",
                    "terminal_state": "complete",
                    "worker_pid": os.getpid(),
                    "worker_identity": D.goalflight_ledger.process_identity(os.getpid()),
                    "queue_launch_token": "different-token",
                    "started_at": D.goalflight_ledger.utc_now(),
                    "ended_at": D.goalflight_ledger.utc_now(),
                }
            )
            D.write_status(
                status_path,
                {
                    "schema": "goalflight.status.v1",
                    "dispatch_id": "token-mismatch-terminal-worker",
                    "state": "complete",
                    "terminal_state": "complete",
                    "queue_launch_token": "different-token",
                    "status_path": str(status_path),
                    "tail_path": str(tail_path),
                },
            )
            claim = queue / "token-mismatch-terminal-worker.json.claimed-123"
            claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": "token-mismatch-terminal-worker",
                        "agent": "test-dispatch",
                        "shape": "bash",
                        "project_root": str(ROOT),
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "request": {
                            "agent": "test-dispatch",
                            "cwd": str(ROOT),
                            "tail": str(tail_path),
                            "status_json": str(status_path),
                        },
                        "queue_launch_token": "claim-token",
                        "queue_launch_started": True,
                        "queue_worker_spawn_intent": True,
                        "queue_worker_spawn_intent_at": D.goalflight_ledger.utc_now(),
                    }
                ),
                encoding="utf-8",
            )
            os.utime(claim, (time.time() - 60, time.time() - 60))
            result = D._recover_claimed_queue_entries(queue, stale_s=0)
            record = json.loads(
                (tmp / "state" / "runs.d" / "token-mismatch-terminal-worker.json").read_text(encoding="utf-8")
            )
            status = json.loads(status_path.read_text(encoding="utf-8"))
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result["restored"] == 0 and result["cleared"] == 0, result
        assert result["quarantined"] == 0, result
        assert result["pending_launch"] >= 1, result
        assert claim.exists()
        assert not (queue / "token-mismatch-terminal-worker.json").exists()
        assert record["state"] == "complete", record
        assert record["terminal_state"] == "complete", record
        assert record["queue_launch_token"] == "different-token", record
        assert status["state"] == "complete", status
        assert status["terminal_state"] == "complete", status
        assert status["queue_launch_token"] == "different-token", status

def test_worker_dead_tail_rate_limit_reaches_pressure_sensor() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        worker_code = (
            "import sys\n"
            "print(\"ERROR: You've hit your usage limit. Please try again at Jun 21st\", flush=True)\n"
            "raise SystemExit(1)\n"
        )
        proc = _run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "codex",
                "--dispatch-id",
                "tail-rate-limit",
                "--tail",
                str(tmp / "rate-limit.tail"),
                "--status-json",
                str(tmp / "rate-limit.status.json"),
                "--cwd",
                str(ROOT),
                "--poll-secs",
                "0.1",
                "--max-idle-secs",
                "5",
                "--foreground",
                "--",
                sys.executable,
                "-c",
                worker_code,
            ],
            env,
            timeout=30.0,
        )
        assert proc.returncode == 1, (proc.stdout, proc.stderr)
        record = json.loads((tmp / "state" / "runs.d" / "tail-rate-limit.json").read_text(encoding="utf-8"))
        assert record["state"] == "rate_limited", record
        assert record["terminal_state"] == "rate_limited", record
        error_text = json.dumps(record.get("error"), sort_keys=True)
        assert "usage limit" in error_text.lower(), record
        # Merged contract: the quota refinement wraps the pre-quota reason —
        # the original classification is preserved as previous_reason.
        error_obj = record.get("error", {})
        assert error_obj.get("message") == "dispatch_worker_rate_limited", record
        assert error_obj.get("previous_reason") == "worker_dead_no_terminal_marker", record
        assert error_obj.get("previous_state") == "worker_dead", record
        # Rate-pressure policy is shared-pool scoped and now refuses per-session
        # threshold overrides. Duplicate the same terminal signature enough times
        # to reach the default threshold while keeping this test local.
        for idx in range(2):
            clone = dict(record)
            clone["dispatch_id"] = f"tail-rate-limit-{idx + 2}"
            clone["updated_at"] = record.get("updated_at")
            (tmp / "state" / "runs.d" / f"tail-rate-limit-{idx + 2}.json").write_text(
                json.dumps(clone, sort_keys=True),
                encoding="utf-8",
            )
        pressure = _run(
            [
                sys.executable,
                str(CAPACITY),
                "status",
                "--rate-pressure-threshold",
                "1",
                "--json",
            ],
            env,
        )
        payload = json.loads(pressure.stdout)
        rate_pressure = payload["rate_pressure"]
        assert any(
            "threshold override" in item for item in rate_pressure.get("policy_warnings") or []
        ), rate_pressure
        assert any(
            row.get("provider") == "openai" or row.get("budget_key") == "provider:openai"
            for row in rate_pressure["providers_under_pressure"]
        ), rate_pressure


# ---------------------------------------------------------------------------
# b-065: bounded claim-recovery (must-have hermetic cases)
# ---------------------------------------------------------------------------


def _b065_env(tmp: Path) -> dict[str, str]:
    env = _env(tmp)
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_DISABLE_NUDGES"] = "1"
    return env


def test_b065_state_flips_to_terminal_so_wait_resolves() -> None:
    """1. state (not only terminal_state) becomes terminal; done_code resolves."""
    import goalflight_status as status_mod

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        original_snapshot = D._process_snapshot
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-wait-resolve"
            status_path = tmp / f"{dispatch_id}.status.json"
            tail = tmp / f"{dispatch_id}.tail"
            tail.write_text("still starting\n", encoding="utf-8")
            claim = queue / f"{dispatch_id}.json.claimed-1"
            started_iso = (
                __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .replace(microsecond=0)
                - __import__("datetime").timedelta(seconds=400)
            ).isoformat()
            claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": dispatch_id,
                        "agent": "test-dispatch",
                        "shape": "bash",
                        "project_root": str(ROOT),
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "task_ids": ["b-065"],
                        "request": {
                            "agent": "test-dispatch",
                            "cwd": str(ROOT),
                            "tail": str(tail),
                            "status_json": str(status_path),
                            "task_ids": ["b-065"],
                        },
                        "queue_launch_token": "wait-token",
                        "queue_launch_started": True,
                        "queue_launch_started_at": started_iso,
                        "queue_worker_spawn_intent": True,
                        "queue_worker_spawn_intent_at": started_iso,
                        "started_at": started_iso,
                        **_dead_producer_fields(at=started_iso),
                    }
                ),
                encoding="utf-8",
            )
            # Age via started_at, not mtime alone.
            os.utime(claim, (time.time() - 400, time.time() - 400))
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "state": "starting",
                    "terminal_state": "unknown",
                    "worker_pid": 99_999_991,
                    "worker_identity": {"pid": 99_999_991},
                    "worker_pgid": 99_999_991,
                    "task_ids": ["b-065"],
                    "queue_launch_token": "wait-token",
                    "started_at": started_iso,
                    "status_path": str(status_path),
                    "stdout_path": str(tail),
                    "project_root": str(ROOT),
                    "agent": "test-dispatch",
                }
            )
            with _process_snapshot([]):
                result = D._recover_claimed_queue_entries(queue, stale_s=300)
            record = json.loads(
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result["cleared"] == 1, result
        assert record["state"] == "worker_dead", record
        assert record["terminal_state"] == "worker_dead", record
        # --wait would resolve: done_code 0 for terminal state (not queued+terminal_state only).
        assert status_mod.done_code(record) == 0, record


def test_b065_linked_vs_unlinked_action_matrix() -> None:
    """2. Linked terminalizes; unlinked is quarantined, not deleted/terminalized."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)

            linked_id = "b065-linked"
            linked_status = tmp / f"{linked_id}.status.json"
            linked_tail = tmp / f"{linked_id}.tail"
            linked_claim = queue / f"{linked_id}.json.claimed-1"
            linked_claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": linked_id,
                        "agent": "test-dispatch",
                        "shape": "bash",
                        "project_root": str(ROOT),
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "task_ids": ["b-065-l"],
                        "request": {
                            "agent": "test-dispatch",
                            "cwd": str(ROOT),
                            "tail": str(linked_tail),
                            "status_json": str(linked_status),
                            "task_ids": ["b-065-l"],
                        },
                        "queue_launch_token": "linked-token",
                        "queue_launch_started": True,
                        "queue_worker_spawn_intent": True,
                        "queue_worker_spawn_intent_at": D.goalflight_ledger.utc_now(),
                        **_dead_producer_fields(),
                    }
                ),
                encoding="utf-8",
            )
            os.utime(linked_claim, (time.time() - 400, time.time() - 400))

            unlinked_id = "b065-unlinked"
            unlinked_status = tmp / f"{unlinked_id}.status.json"
            unlinked_tail = tmp / f"{unlinked_id}.tail"
            unlinked_claim = queue / f"{unlinked_id}.json.claimed-1"
            unlinked_claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": unlinked_id,
                        "agent": "test-dispatch",
                        "shape": "bash",
                        "project_root": str(ROOT),
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "request": {
                            "agent": "test-dispatch",
                            "cwd": str(ROOT),
                            "tail": str(unlinked_tail),
                            "status_json": str(unlinked_status),
                        },
                        "queue_launch_token": "unlinked-token",
                        "queue_launch_started": True,
                        "queue_worker_spawn_intent": True,
                        "queue_worker_spawn_intent_at": D.goalflight_ledger.utc_now(),
                        **_dead_producer_fields(),
                    }
                ),
                encoding="utf-8",
            )
            os.utime(unlinked_claim, (time.time() - 400, time.time() - 400))
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": unlinked_id,
                    "state": "starting",
                    "terminal_state": "unknown",
                    "started_at": D.goalflight_ledger.utc_now(),
                    "agent": "test-dispatch",
                }
            )

            with _process_snapshot([]):
                result = D._recover_claimed_queue_entries(queue, stale_s=0)
            linked_record = json.loads(
                (tmp / "state" / "runs.d" / f"{linked_id}.json").read_text(encoding="utf-8")
            )
            unlinked_record = json.loads(
                (tmp / "state" / "runs.d" / f"{unlinked_id}.json").read_text(encoding="utf-8")
            )
            quarantine_files = list((queue / "quarantine").glob("*")) if (queue / "quarantine").exists() else []
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert linked_record["state"] == "worker_dead", linked_record
        assert unlinked_record["state"] == "starting", unlinked_record
        assert unlinked_record.get("terminal_state") in {None, "unknown"}, unlinked_record
        assert not unlinked_claim.exists()
        assert quarantine_files, "unlinked claim must be quarantined, not deleted"
        assert result.get("quarantined", 0) >= 1, result


def test_b065_superseded_does_not_demote_task_success() -> None:
    """3. Neutral superseded on an old dispatch must not demote real task success."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            # Real successful dispatch for the task.
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": "b065-real-success",
                    "state": "complete",
                    "terminal_state": "complete",
                    "task_ids": ["b-065-task"],
                    "started_at": D.goalflight_ledger.utc_now(),
                    "ended_at": D.goalflight_ledger.utc_now(),
                    "agent": "test-dispatch",
                    "project_root": str(tmp / "proj"),
                }
            )
            # Orphan claim for same task: ladder leg 3 → system-owned superseded
            # (not a forged COMPLETE). Real success must remain complete.
            project = tmp / "proj"
            project.mkdir(parents=True)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            orphan_id = "b065-orphan-same-task"
            tail = tmp / f"{orphan_id}.tail"
            status_path = tmp / f"{orphan_id}.status.json"
            tail.write_text("partial only\n", encoding="utf-8")
            claim = queue / f"{orphan_id}.json.claimed-1"
            claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": orphan_id,
                        "agent": "test-dispatch",
                        "shape": "bash",
                        "project_root": str(project),
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "task_ids": ["b-065-task"],
                        "request": {
                            "agent": "test-dispatch",
                            "cwd": str(project),
                            "tail": str(tail),
                            "status_json": str(status_path),
                            "task_ids": ["b-065-task"],
                        },
                        "queue_launch_token": "orphan-token",
                        "queue_launch_started": True,
                        "queue_worker_spawn_intent": True,
                        "queue_worker_spawn_intent_at": D.goalflight_ledger.utc_now(),
                        **_dead_producer_fields(),
                    }
                ),
                encoding="utf-8",
            )
            os.utime(claim, (time.time() - 400, time.time() - 400))
            with _process_snapshot([]):
                D._recover_claimed_queue_entries(queue, stale_s=0)
            success = json.loads(
                (tmp / "state" / "runs.d" / "b065-real-success.json").read_text(encoding="utf-8")
            )
            orphan = json.loads(
                (tmp / "state" / "runs.d" / f"{orphan_id}.json").read_text(encoding="utf-8")
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert success["state"] == "complete", success
        assert success["terminal_state"] == "complete", success
        assert orphan["state"] == "superseded", orphan
        assert orphan["terminal_state"] == "superseded", orphan
        recon = (orphan.get("outcome") or {}).get("reconciliation") or {}
        assert recon.get("resolution_source") == "task_store", orphan
        # No forged COMPLETE in the orphan tail.
        assert "COMPLETE:" not in tail.read_text(encoding="utf-8")


def test_b065_launch_age_ignores_updated_at_heartbeats() -> None:
    """4. Poison: rewrite updated_at every 60s; still orphans at 300s from started_at."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-heartbeat-poison"
            status_path = tmp / f"{dispatch_id}.status.json"
            tail = tmp / f"{dispatch_id}.tail"
            started = (
                __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .replace(microsecond=0)
                - __import__("datetime").timedelta(seconds=310)
            ).isoformat()
            # No claim carrier — pure ledger-only zombie with fresh updated_at.
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "state": "starting",
                    "terminal_state": "unknown",
                    "worker_pid": 99_999_991,
                    "worker_identity": {"pid": 99_999_991},
                    "worker_pgid": 99_999_991,
                    "task_ids": ["b-065-hb"],
                    "started_at": started,
                    "updated_at": D.goalflight_ledger.utc_now(),  # heartbeat "just now"
                    "status_path": str(status_path),
                    "stdout_path": str(tail),
                    "project_root": str(ROOT),
                    "agent": "test-dispatch",
                    "queue_launch_token": "hb-token",
                }
            )
            # Simulate 5 heartbeats rewriting updated_at (would starve a 300s clock).
            for _ in range(5):
                rec = json.loads(
                    (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
                )
                rec["updated_at"] = D.goalflight_ledger.utc_now()
                # Direct write without going through write_record age stamp.
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").write_text(
                    json.dumps(rec, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            with _process_snapshot([]):
                result = D._recover_claimed_queue_entries(queue, stale_s=300)
            record = json.loads(
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
            )
            # Age helper must ignore updated_at.
            age = D._launch_age_timestamp_s(record)
            started_ts = D._parse_timestamp_s(started)
            assert age is not None and started_ts is not None
            # Newest launch-progress stamp should be started_at (or orphan_first_seen), not updated_at.
            assert abs(age - started_ts) < 2.0 or age == D._parse_timestamp_s(
                record.get("orphan_first_seen_at")
            ), (age, started_ts, record.get("updated_at"), record.get("started_at"))
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert record["state"] == "worker_dead", record
        assert result.get("ledger_terminalized", 0) >= 1, result


def test_b065_weak_worker_pid_claim_unlink_then_ledger_terminalizes() -> None:
    """5. Weak worker_pid claim clear → next reconcile terminalizes, no re-enqueue."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-weak-unlink"
            status_path = tmp / f"{dispatch_id}.status.json"
            tail = tmp / f"{dispatch_id}.tail"
            # Dead pid with identity so status is confirmed-dead.
            worker = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                assert _wait_for(lambda: D.goalflight_ledger.process_identity(worker.pid) is not None)
                identity = D.goalflight_ledger.process_identity(worker.pid)
                dead_pid = worker.pid
                dead_pgid = os.getpgid(worker.pid)
            finally:
                worker.terminate()
                worker.wait(timeout=5)
            started = (
                __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .replace(microsecond=0)
                - __import__("datetime").timedelta(seconds=400)
            ).isoformat()
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "state": "starting",
                    "terminal_state": "unknown",
                    "worker_pid": dead_pid,
                    "worker_identity": identity,
                    "worker_pgid": dead_pgid,
                    "task_ids": ["b-065-wu"],
                    "queue_launch_token": "weak-token",
                    "started_at": started,
                    "status_path": str(status_path),
                    "stdout_path": str(tail),
                    "project_root": str(ROOT),
                    "agent": "test-dispatch",
                }
            )
            # Claim already gone (simulating weak drain clear) — ledger only.
            with _process_snapshot([]):
                result = D._recover_claimed_queue_entries(queue, stale_s=300)
            record = json.loads(
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert record["state"] == "worker_dead", record
        assert result["restored"] == 0, result
        assert not (queue / f"{dispatch_id}.json").exists()


def test_b065_late_complete_wins_over_worker_dead() -> None:
    """6. Late COMPLETE between scan and finish → complete, not worker_dead."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-late-complete"
            status_path = tmp / f"{dispatch_id}.status.json"
            tail = tmp / f"{dispatch_id}.tail"
            # Empty tail initially — will write COMPLETE just before mark via monkeypatch.
            tail.write_text("working...\n", encoding="utf-8")
            claim = queue / f"{dispatch_id}.json.claimed-1"
            entry = {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "state": "claimed",
                "dispatch_id": dispatch_id,
                "agent": "test-dispatch",
                "shape": "bash",
                "project_root": str(ROOT),
                "dispatch_argv": ["--agent", "test-dispatch"],
                "task_ids": ["b-065-lc"],
                "request": {
                    "agent": "test-dispatch",
                    "cwd": str(ROOT),
                    "tail": str(tail),
                    "status_json": str(status_path),
                    "task_ids": ["b-065-lc"],
                },
                "queue_launch_token": "late-token",
                "queue_launch_started": True,
                "queue_worker_spawn_intent": True,
                "queue_worker_spawn_intent_at": D.goalflight_ledger.utc_now(),
                **_dead_producer_fields(),
            }
            claim.write_text(json.dumps(entry), encoding="utf-8")
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "state": "starting",
                    "terminal_state": "unknown",
                    "worker_pid": 99_999_991,
                    "worker_identity": {"pid": 99_999_991},
                    "worker_pgid": 99_999_991,
                    "task_ids": ["b-065-lc"],
                    "queue_launch_token": "late-token",
                    "started_at": D.goalflight_ledger.utc_now(),
                    "status_path": str(status_path),
                    "stdout_path": str(tail),
                    "project_root": str(ROOT),
                    "agent": "test-dispatch",
                }
            )
            # Producer appends only after every outer/final pre-lock scan, at
            # the boundary where finish acquires the shared lifecycle lock.
            original_lock = D._tail_reconciliation_lock
            injected = {"n": 0}

            @contextlib.contextmanager
            def late_complete_before_finish(path):
                if Path(path) == tail and injected["n"] == 0:
                    with D._tail_mutation_lock(Path(path)):
                        with Path(path).open("a", encoding="utf-8") as tail_f:
                            tail_f.write("COMPLETE: late finish\n")
                    injected["n"] += 1
                with original_lock(Path(path)):
                    yield

            D._tail_reconciliation_lock = late_complete_before_finish
            try:
                with _process_snapshot([]):
                    D._mark_claim_worker_dead(entry, reason="orphaned_prelaunch")
            finally:
                D._tail_reconciliation_lock = original_lock
            record = json.loads(
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert record["state"] == "complete", record
        assert record["terminal_state"] == "complete", record
        assert injected["n"] == 1, injected


def test_b065_live_stdout_lock_skips_reconciliation_promptly() -> None:
    """A live tail producer skips this drain tick instead of blocking it."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        original_snapshot = D._process_snapshot
        try:
            os.environ.clear()
            os.environ.update(env)
            dispatch_id = "b065-inherited-tail-lock"
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            tail = tmp / f"{dispatch_id}.tail"
            status_path = tmp / f"{dispatch_id}.status.json"
            ready = tmp / "producer.ready"
            release = tmp / "producer.release"
            claim = queue / f"{dispatch_id}.json.claimed-1"
            started = (
                __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .replace(microsecond=0)
                - __import__("datetime").timedelta(seconds=400)
            ).isoformat()
            entry = {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "state": "claimed",
                "dispatch_id": dispatch_id,
                "agent": "test-dispatch",
                "shape": "bash",
                "project_root": str(tmp),
                "dispatch_argv": ["--agent", "test-dispatch"],
                "task_ids": ["b-065-lock"],
                "queue_launch_token": "lock-token",
                "queue_launch_started": True,
                "queue_worker_spawn_intent": True,
                "started_at": started,
                **_dead_producer_fields(at=started),
                "request": {
                    "agent": "test-dispatch",
                    "cwd": str(tmp),
                    "tail": str(tail),
                    "status_json": str(status_path),
                    "task_ids": ["b-065-lock"],
                },
            }
            claim.write_text(json.dumps(entry), encoding="utf-8")
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "state": "starting",
                    "terminal_state": "unknown",
                    "worker_pid": 99_999_991,
                    "worker_identity": {"pid": 99_999_991},
                    "worker_pgid": 99_999_991,
                    "task_ids": ["b-065-lock"],
                    "queue_launch_token": "lock-token",
                    "stdout_path": str(tail),
                    "status_path": str(status_path),
                    "project_root": str(tmp),
                    "started_at": started,
                }
            )
            script = (
                "from pathlib import Path; import time; "
                f"ready=Path({str(ready)!r}); release=Path({str(release)!r}); "
                "print('working...', flush=True); ready.write_text('ready'); "
                "\nwhile not release.exists(): time.sleep(0.01)\n"
                "print('COMPLETE: inherited lock', flush=True)"
            )
            D._spawn_daemonized_process(
                [sys.executable, "-c", script],
                env=os.environ.copy(),
                stdout_path=tail,
                stdout_mode="wb",
                stderr="stdout",
                serialize_stdout=True,
                label="test-worker",
            )
            assert _wait_for(
                lambda: ready.exists() and tail.exists() and "working" in tail.read_text(encoding="utf-8"),
                timeout=5.0,
            )
            result: dict[str, object] = {}
            D._process_snapshot = lambda: []

            def recover() -> None:
                result.update(D._recover_claimed_queue_entries(queue, stale_s=300))

            tick = threading.Thread(target=recover, daemon=True)
            before = time.monotonic()
            tick.start()
            tick.join(timeout=0.5)
            elapsed = time.monotonic() - before
            returned_promptly = not tick.is_alive()
            record_while_live = json.loads(
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
            )
            claim_while_live = claim.exists()
            release.write_text("release", encoding="utf-8")
            tick.join(timeout=5.0)
            assert not tick.is_alive(), "test cleanup could not release the reconciler"
            if returned_promptly:
                def tail_released() -> bool:
                    try:
                        with D._tail_reconciliation_lock(tail):
                            return True
                    except D._TailLockBusy:
                        return False

                assert _wait_for(tail_released, timeout=5.0), "producer did not release tail flock"
                second = D._recover_claimed_queue_entries(queue, stale_s=300)
                record_after_eof = json.loads(
                    (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
                )
        finally:
            D._process_snapshot = original_snapshot
            os.environ.clear()
            os.environ.update(old_env)
        assert returned_promptly, f"drain tick blocked {elapsed:.3f}s on live tail producer"
        assert elapsed < 0.5, elapsed
        assert claim_while_live, "live dispatch carrier must remain intact"
        assert record_while_live["state"] == "starting", record_while_live
        assert result.get("pending_launch") == 1, result
        assert second.get("cleared") == 1, second
        assert record_after_eof["state"] == "complete", record_after_eof
        assert record_after_eof["terminal_state"] == "complete", record_after_eof
        assert "COMPLETE: inherited lock" in tail.read_text(encoding="utf-8")


def test_b065_unlinked_quarantine_not_deleted_or_terminalized() -> None:
    """7. Unlinked post-spawn orphan at quarantine → not deleted, not auto-terminalized."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-unlinked-q"
            status_path = tmp / f"{dispatch_id}.status.json"
            tail = tmp / f"{dispatch_id}.tail"
            claim = queue / f"{dispatch_id}.json.claimed-1"
            payload = {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "state": "claimed",
                "dispatch_id": dispatch_id,
                "agent": "test-dispatch",
                "shape": "bash",
                "project_root": str(ROOT),
                "dispatch_argv": ["--agent", "test-dispatch", "--prompt", "keep-me"],
                "request": {
                    "agent": "test-dispatch",
                    "cwd": str(ROOT),
                    "tail": str(tail),
                    "status_json": str(status_path),
                },
                "queue_launch_token": "uq-token",
                "queue_launch_started": True,
                "queue_worker_spawn_intent": True,
                "queue_worker_spawn_intent_at": D.goalflight_ledger.utc_now(),
                **_dead_producer_fields(),
            }
            claim.write_text(json.dumps(payload), encoding="utf-8")
            os.utime(claim, (time.time() - 400, time.time() - 400))
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "state": "starting",
                    "terminal_state": "unknown",
                    "worker_pid": 99_999_991,
                    "worker_identity": {"pid": 99_999_991},
                    "worker_pgid": 99_999_991,
                    "started_at": D.goalflight_ledger.utc_now(),
                    "agent": "test-dispatch",
                    "queue_launch_token": "uq-token",
                }
            )
            with _process_snapshot([]):
                result = D._recover_claimed_queue_entries(queue, stale_s=0)
            record = json.loads(
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
            )
            qfiles = list((queue / "quarantine").glob("*.quarantined*"))
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert record["state"] == "starting", record
        assert not claim.exists()
        assert qfiles, "quarantine artifact must retain the envelope"
        parked = json.loads(qfiles[0].read_text(encoding="utf-8"))
        assert parked.get("dispatch_argv"), parked
        assert result.get("quarantined", 0) >= 1, result


def test_b065_concurrent_double_restore_second_exhausts() -> None:
    """Two ticks race one pre-spawn claim → exactly one restore; second exhausts."""
    import threading

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-double-restore"
            claim = queue / f"{dispatch_id}.json.claimed-1"
            payload = {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "state": "claimed",
                "dispatch_id": dispatch_id,
                "agent": "test-dispatch",
                "shape": "bash",
                "project_root": str(tmp),
                "dispatch_argv": ["--agent", "test-dispatch"],
                "task_ids": ["b-065-dr"],
                "request": {
                    "agent": "test-dispatch",
                    "cwd": str(tmp),
                    "tail": str(tmp / f"{dispatch_id}.tail"),
                    "status_json": str(tmp / f"{dispatch_id}.status.json"),
                    "task_ids": ["b-065-dr"],
                },
                "queue_launch_token": "dr-token",
                "claim_recovery_count": 0,
                "started_at": (
                    __import__("datetime")
                    .datetime.now(__import__("datetime").timezone.utc)
                    .replace(microsecond=0)
                    - __import__("datetime").timedelta(seconds=400)
                ).isoformat(),
            }
            claim.write_text(json.dumps(payload), encoding="utf-8")
            os.utime(claim, (time.time() - 400, time.time() - 400))
            results: list[tuple[bool, object]] = []
            barrier = threading.Barrier(2)

            def race() -> None:
                barrier.wait(timeout=5)
                results.append(D._bounded_restore_claim(claim, dict(payload), queue))

            t1 = threading.Thread(target=race)
            t2 = threading.Thread(target=race)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
            restored_path = queue / f"{dispatch_id}.json"
            restored_ok = [r for r in results if r[0] is True]
            restored_fail = [r for r in results if r[0] is False]
            assert len(results) == 2, results
            assert len(restored_ok) == 1, results
            assert len(restored_fail) == 1, results
            assert restored_path.exists(), "exactly one restore must write the carrier"
            entry = json.loads(restored_path.read_text(encoding="utf-8"))
            assert entry.get("claim_recovery_count") == 1, entry
            assert not claim.exists()
            # Second attempt on an already-restored / count=1 claim exhausts.
            # Re-claim the restored envelope and try again → must not restore.
            claim2 = queue / f"{dispatch_id}.json.claimed-2"
            entry["queue_launch_token"] = "dr-token-2"
            claim2.write_text(json.dumps(entry), encoding="utf-8")
            restored_path.unlink()
            ok2, _dec2 = D._bounded_restore_claim(claim2, entry, queue)
            assert ok2 is False, "recovery_count>=1 must refuse second restore"
            assert not (queue / f"{dispatch_id}.json").exists()
            # Exhaustion path for linked pre-spawn with count>=1 → worker_dead.
            action = D._act_on_orphan_claim(
                claim2,
                entry,
                queue_dir=queue,
                reason="stale_claim_pre_spawn",
            )
            assert action == "cleared", action
            record = json.loads(
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
            )
            assert record["state"] == "worker_dead", record
            assert record.get("reason") == "claim_recovery_exhausted" or "claim_recovery_exhausted" in str(
                record.get("reason") or ""
            ), record
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def test_b065_late_complete_racing_restore_wins() -> None:
    """Completion between outer scan and locked restore write → complete, no relaunch."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-late-restore"
            tail = tmp / f"{dispatch_id}.tail"
            status_path = tmp / f"{dispatch_id}.status.json"
            tail.write_text("working...\n", encoding="utf-8")
            claim = queue / f"{dispatch_id}.json.claimed-1"
            started = (
                __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .replace(microsecond=0)
                - __import__("datetime").timedelta(seconds=400)
            ).isoformat()
            entry = {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "state": "claimed",
                "dispatch_id": dispatch_id,
                "agent": "test-dispatch",
                "shape": "bash",
                "project_root": str(tmp),
                "dispatch_argv": ["--agent", "test-dispatch"],
                "task_ids": ["b-065-lr"],
                "request": {
                    "agent": "test-dispatch",
                    "cwd": str(tmp),
                    "tail": str(tail),
                    "status_json": str(status_path),
                    "task_ids": ["b-065-lr"],
                },
                "queue_launch_token": "lr-token",
                "claim_recovery_count": 0,
                "started_at": started,
            }
            claim.write_text(json.dumps(entry), encoding="utf-8")
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "state": "queued",
                    "terminal_state": "unknown",
                    "task_ids": ["b-065-lr"],
                    "queue_launch_token": "lr-token",
                    "started_at": started,
                    "status_path": str(status_path),
                    "stdout_path": str(tail),
                    "project_root": str(tmp),
                    "agent": "test-dispatch",
                }
            )
            # Append after _act_on_orphan_claim's outer ladder scan, exactly
            # before the locked final ladder + conditional restore write.
            original_lock = D._tail_reconciliation_lock
            injected = {"n": 0}

            @contextlib.contextmanager
            def late_complete_before_restore(path):
                if Path(path) == tail and injected["n"] == 0:
                    with D._tail_mutation_lock(Path(path)):
                        with Path(path).open("a", encoding="utf-8") as tail_f:
                            tail_f.write("COMPLETE: raced the restore\n")
                    injected["n"] += 1
                with original_lock(Path(path)):
                    yield

            D._tail_reconciliation_lock = late_complete_before_restore
            try:
                action = D._act_on_orphan_claim(
                    claim,
                    entry,
                    queue_dir=queue,
                    reason="stale_claim_pre_spawn",
                )
            finally:
                D._tail_reconciliation_lock = original_lock
            assert action == "cleared", action
            assert injected["n"] == 1, injected
            record = json.loads(
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
            )
            assert record["state"] == "complete", record
            assert record["terminal_state"] == "complete", record
            assert not (queue / f"{dispatch_id}.json").exists(), "must not relaunch"
            assert record["state"] != "worker_dead"
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def test_b065_normal_drain_restore_honors_completed_linked_task() -> None:
    """Remote/capacity restore branches terminalize completed work, never replay it."""
    for branch in ("capacity", "remote"):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = _b065_env(tmp)
            old_env = os.environ.copy()
            originals = {
                "run": D.subprocess.run,
                "release": D._release_stale_capacity_for_drain,
                "hook": D._run_drain_prelaunch_hook,
                "remote_node": D._remote_drain_node,
                "validate_remote": D._validate_remote_drain_node,
                "launch_remote": D._drain_launch_remote_claim,
            }
            try:
                os.environ.clear()
                os.environ.update(env)
                queue = tmp / "state" / "dispatch-queue"
                queue.mkdir(parents=True)
                dispatch_id = f"b065-normal-{branch}"
                task_id = "b-065-normal-drain"
                path = _write_queue_entry(queue, dispatch_id, filename=dispatch_id)
                entry = json.loads(path.read_text(encoding="utf-8"))
                entry["project_root"] = str(tmp)
                entry["task_ids"] = [task_id]
                entry["request"]["cwd"] = str(tmp)
                entry["request"]["task_ids"] = [task_id]
                D._write_json_atomic(path, entry)
                D.goalflight_ledger.write_record(
                    {
                        "schema": D.goalflight_ledger.SCHEMA,
                        "dispatch_id": f"b065-prior-complete-{branch}",
                        "state": "complete",
                        "terminal_state": "complete",
                        "task_ids": [task_id],
                        "project_root": str(tmp),
                        "started_at": D.goalflight_ledger.utc_now(),
                        "ended_at": D.goalflight_ledger.utc_now(),
                    }
                )
                D._release_stale_capacity_for_drain = lambda: None
                D._run_drain_prelaunch_hook = lambda _agents: None
                args = _drain_args(queue)
                if branch == "capacity":
                    D.subprocess.run = lambda argv, **kwargs: subprocess.CompletedProcess(
                        argv,
                        2,
                        stdout="blocked_capacity\n",
                        stderr="",
                    )
                else:
                    args.remote_node = "test-node"
                    args.remote_runner = object()
                    D._remote_drain_node = lambda _args: "test-node"
                    D._validate_remote_drain_node = lambda _args: None

                    def blocked(*_args, **_kwargs):
                        raise D._RemoteDrainBlocked("test", code="test_block")

                    D._drain_launch_remote_claim = blocked
                payload = D._drain_queue_once(args)
                record = json.loads(
                    (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
                )
            finally:
                D.subprocess.run = originals["run"]
                D._release_stale_capacity_for_drain = originals["release"]
                D._run_drain_prelaunch_hook = originals["hook"]
                D._remote_drain_node = originals["remote_node"]
                D._validate_remote_drain_node = originals["validate_remote"]
                D._drain_launch_remote_claim = originals["launch_remote"]
                os.environ.clear()
                os.environ.update(old_env)
            assert record["state"] == "superseded", (branch, record)
            assert record["terminal_state"] == "superseded", (branch, record)
            assert record["outcome"]["reconciliation"]["resolution_source"] == "task_store", record
            assert payload["remaining"] == 0, (branch, payload)
            assert payload["left_queued"] == 0, (branch, payload)
            assert not path.exists(), (branch, path)
            assert payload["details"][0]["state"] == "superseded", (branch, payload)


def test_b065_carrier_mtime_poison_still_orphans() -> None:
    """Claim mtime rewritten to now with started_at=now-400s still orphans at 300s."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-mtime-poison"
            status_path = tmp / f"{dispatch_id}.status.json"
            tail = tmp / f"{dispatch_id}.tail"
            started = (
                __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .replace(microsecond=0)
                - __import__("datetime").timedelta(seconds=400)
            ).isoformat()
            claim = queue / f"{dispatch_id}.json.claimed-1"
            claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": dispatch_id,
                        "agent": "test-dispatch",
                        "shape": "bash",
                        "project_root": str(tmp),
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "task_ids": ["b-065-mt"],
                        "request": {
                            "agent": "test-dispatch",
                            "cwd": str(tmp),
                            "tail": str(tail),
                            "status_json": str(status_path),
                            "task_ids": ["b-065-mt"],
                        },
                        "queue_launch_token": "mt-token",
                        "queue_launch_started": True,
                        "queue_worker_spawn_intent": True,
                        "queue_worker_spawn_intent_at": started,
                        "started_at": started,
                        **_dead_producer_fields(at=started),
                    }
                ),
                encoding="utf-8",
            )
            # Heartbeat rewrites carrier mtime to "now" — must not starve age.
            now = time.time()
            os.utime(claim, (now, now))
            age = D._claim_launch_age_s(
                json.loads(claim.read_text(encoding="utf-8")),
                claim,
                now=now,
            )
            assert age >= 300, f"mtime poison must not reset age; got {age}"
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "state": "starting",
                    "terminal_state": "unknown",
                    "worker_pid": 99_999_991,
                    "worker_identity": {"pid": 99_999_991},
                    "worker_pgid": 99_999_991,
                    "task_ids": ["b-065-mt"],
                    "queue_launch_token": "mt-token",
                    "started_at": started,
                    "status_path": str(status_path),
                    "stdout_path": str(tail),
                    "project_root": str(tmp),
                    "agent": "test-dispatch",
                }
            )
            with _process_snapshot([]):
                result = D._recover_claimed_queue_entries(queue, stale_s=300)
            record = json.loads(
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert age >= 300
        assert record["state"] == "worker_dead", record
        assert result.get("cleared", 0) >= 1 or result.get("ledger_terminalized", 0) >= 1, result


def test_b065_unlinked_early_terminal_and_live_carriers_quarantine() -> None:
    """Early terminal/live-token branches never delete unlinked carriers."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            with _sleeping_worker() as worker:
                worker_identity = D.goalflight_ledger.process_identity(worker.pid)
                cases = (
                    ("terminal", "complete", None, None),
                    ("live", "running", worker.pid, worker_identity),
                )
                for label, state, worker_pid, identity in cases:
                    dispatch_id = f"b065-unlinked-early-{label}"
                    tail = tmp / f"{dispatch_id}.tail"
                    tail.write_text("working...\n", encoding="utf-8")
                    claim = queue / f"{dispatch_id}.json.claimed-1"
                    claim.write_text(
                        json.dumps(
                            {
                                "schema": D.DISPATCH_QUEUE_SCHEMA,
                                "state": "claimed",
                                "dispatch_id": dispatch_id,
                                "agent": "test-dispatch",
                                "shape": "bash",
                                "project_root": str(tmp),
                                "dispatch_argv": ["--agent", "test-dispatch"],
                                "created_at": "2000-01-01T00:00:00+00:00",
                                "queue_launch_token": f"{label}-token",
                                "queue_worker_pid": worker_pid,
                                "queue_worker_identity": identity,
                                "request": {"tail": str(tail), "cwd": str(tmp)},
                            }
                        ),
                        encoding="utf-8",
                    )
                    D.goalflight_ledger.write_record(
                        {
                            "schema": D.goalflight_ledger.SCHEMA,
                            "dispatch_id": dispatch_id,
                            "state": state,
                            "terminal_state": "complete" if state == "complete" else "unknown",
                            "worker_pid": worker_pid,
                            "worker_identity": identity,
                            "queue_launch_token": f"{label}-token",
                            "stdout_path": str(tail),
                            "project_root": str(tmp),
                            "started_at": D.goalflight_ledger.utc_now(),
                        }
                    )
                captured = io.StringIO()
                original_identity_status = D._queue_claim_identity_status

                def identity_status(pid, identity):
                    if pid == worker.pid:
                        return "live", "identity_matches"
                    return original_identity_status(pid, identity)

                D._queue_claim_identity_status = identity_status
                try:
                    with contextlib.redirect_stderr(captured):
                        result = D._recover_claimed_queue_entries(queue, stale_s=300)
                finally:
                    D._queue_claim_identity_status = original_identity_status
                live_record = json.loads(
                    (tmp / "state" / "runs.d" / "b065-unlinked-early-live.json").read_text(
                        encoding="utf-8"
                    )
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        quarantined = list((queue / "quarantine").glob("*.quarantined*"))
        assert result["quarantined"] == 2, result
        assert len(quarantined) == 2, quarantined
        assert live_record["state"] == "running", live_record
        assert captured.getvalue().count('"action": "quarantine"') == 2, captured.getvalue()


def test_b065_unlinked_complete_carrier_quarantines_before_authority() -> None:
    """Valid COMPLETE cannot authorize silent deletion of an unlinked carrier."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-unlinked-complete"
            tail = tmp / f"{dispatch_id}.tail"
            tail.write_text("COMPLETE: valid but unlinked\n", encoding="utf-8")
            started = (
                __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .replace(microsecond=0)
                - __import__("datetime").timedelta(seconds=400)
            ).isoformat()
            claim = queue / f"{dispatch_id}.json.claimed-1"
            entry = {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "state": "claimed",
                "dispatch_id": dispatch_id,
                "agent": "test-dispatch",
                "shape": "bash",
                "project_root": str(tmp),
                "dispatch_argv": ["--agent", "test-dispatch"],
                "queue_launch_token": "unlinked-complete-token",
                "queue_launch_started": True,
                "queue_worker_spawn_intent": True,
                "started_at": started,
                **_dead_producer_fields(at=started),
                "request": {"tail": str(tail), "cwd": str(tmp)},
            }
            claim.write_text(json.dumps(entry), encoding="utf-8")
            D.goalflight_ledger.write_record(
                {
                    "schema": D.goalflight_ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "state": "starting",
                    "terminal_state": "unknown",
                    "worker_pid": 99_999_991,
                    "worker_identity": {"pid": 99_999_991},
                    "worker_pgid": 99_999_991,
                    "queue_launch_token": "unlinked-complete-token",
                    "stdout_path": str(tail),
                    "project_root": str(tmp),
                    "started_at": started,
                    "agent": "test-dispatch",
                }
            )
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured), _process_snapshot([]):
                result = D._recover_claimed_queue_entries(queue, stale_s=300)
            record = json.loads(
                (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
            )
            quarantined = list((queue / "quarantine").glob("*.quarantined*"))
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result["quarantined"] == 1, result
        assert not claim.exists(), "active carrier must move to quarantine"
        assert len(quarantined) == 1, quarantined
        assert record["state"] == "complete", record
        assert record["terminal_state"] == "complete", record
        assert "CLAIM-RECOVERY-ALERT" in captured.getvalue(), captured.getvalue()


def test_b065_unlinked_pre_spawn_carrier_quarantines_instead_of_restore() -> None:
    """An unlinked pre-spawn carrier is controller evidence, not replay authority."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-unlinked-pre-spawn"
            claim = queue / f"{dispatch_id}.json.claimed-1"
            claim.write_text(
                json.dumps(
                    {
                        "schema": D.DISPATCH_QUEUE_SCHEMA,
                        "state": "claimed",
                        "dispatch_id": dispatch_id,
                        "agent": "test-dispatch",
                        "shape": "bash",
                        "project_root": str(tmp),
                        "dispatch_argv": ["--agent", "test-dispatch"],
                        "queue_launch_token": "unlinked-pre-spawn-token",
                        "request": {"cwd": str(tmp)},
                    }
                ),
                encoding="utf-8",
            )
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                result = D._recover_claimed_queue_entries(queue, stale_s=0)
            quarantined = list((queue / "quarantine").glob("*.quarantined*"))
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result["restored"] == 0, result
        assert result["quarantined"] == 1, result
        assert not (queue / f"{dispatch_id}.json").exists()
        assert len(quarantined) == 1, quarantined
        assert "CLAIM-RECOVERY-ALERT" in captured.getvalue(), captured.getvalue()


def test_b065_identity_exception_is_indeterminate() -> None:
    """Identity-provider exceptions preserve+alert end to end; no terminalization."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "b065-identity-indeterminate"
            status_path = tmp / f"{dispatch_id}.status.json"
            tail = tmp / f"{dispatch_id}.tail"
            tail.write_text("working...\n", encoding="utf-8")
            worker = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                identity = D.goalflight_ledger.process_identity(worker.pid) or {"pid": worker.pid}
                started = (
                    __import__("datetime")
                    .datetime.now(__import__("datetime").timezone.utc)
                    .replace(microsecond=0)
                    - __import__("datetime").timedelta(seconds=400)
                ).isoformat()
                claim = queue / f"{dispatch_id}.json.claimed-1"
                entry = {
                    "schema": D.DISPATCH_QUEUE_SCHEMA,
                    "state": "claimed",
                    "dispatch_id": dispatch_id,
                    "agent": "test-dispatch",
                    "shape": "bash",
                    "project_root": str(tmp),
                    "dispatch_argv": ["--agent", "test-dispatch"],
                    "task_ids": ["b-065-identity"],
                    "request": {
                        "agent": "test-dispatch",
                        "cwd": str(tmp),
                        "tail": str(tail),
                        "status_json": str(status_path),
                        "task_ids": ["b-065-identity"],
                    },
                    "queue_launch_token": "identity-token",
                    "queue_launch_started": True,
                    "queue_worker_spawn_intent": True,
                    "queue_worker_pid": worker.pid,
                    "queue_worker_identity": identity,
                    "started_at": started,
                }
                claim.write_text(json.dumps(entry), encoding="utf-8")
                D.goalflight_ledger.write_record(
                    {
                        "schema": D.goalflight_ledger.SCHEMA,
                        "dispatch_id": dispatch_id,
                        "state": "starting",
                        "terminal_state": "unknown",
                        "worker_pid": worker.pid,
                        "worker_identity": identity,
                        "task_ids": ["b-065-identity"],
                        "queue_launch_token": "identity-token",
                        "started_at": started,
                        "status_path": str(status_path),
                        "stdout_path": str(tail),
                        "project_root": str(tmp),
                        "agent": "test-dispatch",
                    }
                )
                original = D.goalflight_ledger.identity_matches

                def boom(_record):
                    raise RuntimeError("provider down")

                D.goalflight_ledger.identity_matches = boom  # type: ignore[assignment]
                try:
                    captured = io.StringIO()
                    with contextlib.redirect_stderr(captured):
                        result = D._recover_claimed_queue_entries(queue, stale_s=300)
                finally:
                    D.goalflight_ledger.identity_matches = original  # type: ignore[assignment]
                record = json.loads(
                    (tmp / "state" / "runs.d" / f"{dispatch_id}.json").read_text(encoding="utf-8")
                )
            finally:
                worker.terminate()
                worker.wait(timeout=5)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert claim.exists(), "identity-indeterminate carrier must be preserved"
        assert result["pending_launch"] == 1, result
        assert record["state"] == "starting", record
        assert record.get("terminal_state") == "unknown", record
        assert "CLAIM-RECOVERY-ALERT" in captured.getvalue(), captured.getvalue()
        assert "identity_indeterminate" in captured.getvalue(), captured.getvalue()


def test_round5_admission_and_mode_decision_tables() -> None:
    old = "2000-01-01T00:00:00+00:00"
    assert D.classify_reconciliation_admission(
        {"created_at": old}, time.time(), stale_s=300
    ) is D.PreAdmitClass.STALE_NO_SPAWN
    assert D.classify_reconciliation_admission(
        {"created_at": D.goalflight_ledger.utc_now()}, time.time(), stale_s=300
    ) is D.PreAdmitClass.NOT_STALE
    assert D.classify_reconciliation_admission(
        {
            "queue_launch_token": "spawn-no-pid",
            "queue_worker_spawn_intent": True,
            "created_at": old,
        },
        time.time(),
        stale_s=0,
    ) is D.PreAdmitClass.INDETERMINATE
    assert D.classify_reconciliation_admission(
        {
            "queue_launch_token": "spawn-dead-launcher-no-worker",
            "queue_worker_spawn_intent": True,
            "queue_launcher_pid": 99_999_991,
            "queue_launcher_identity": {"pid": 99_999_991},
            "created_at": old,
        },
        time.time(),
        stale_s=0,
    ) is D.PreAdmitClass.INDETERMINATE
    assert D.ADMISSION_DECISION[D.PreAdmitClass.LIVE] is D.AdmissionAction.DEFER_UNCHANGED
    assert D.ADMISSION_DECISION[D.PreAdmitClass.INDETERMINATE] is D.AdmissionAction.DEFER_UNCHANGED
    assert D.ADMISSION_DECISION[D.PreAdmitClass.CONFIRMED_DEAD] is D.AdmissionAction.ADMIT_TO_GATE

    fs = D.FilesystemIdentity(1, "/", "apfs", "local")
    tail = Path("/tmp/round5-mode-tail")
    assert D.resolve_reconciliation_mode(
        transport="bash",
        node=None,
        locality="local",
        tail_path=tail,
        tail_filesystem=fs,
        flock_probe=D.FlockCapability.COHERENT_LOCAL,
        worker_tail_lock_contract=True,
    ) is D.ReconciliationMode.LOCAL_FLOCK
    assert D.resolve_reconciliation_mode(
        transport="bash",
        node=None,
        locality="local",
        tail_path=tail,
        tail_filesystem=fs,
        flock_probe=D.FlockCapability.UNAVAILABLE,
        producer_set_authoritative=True,
    ) is D.ReconciliationMode.FALLBACK_PID_IDENTITY
    assert D.resolve_reconciliation_mode(
        transport="fleet",
        node="n1",
        locality="remote",
        tail_path=tail,
        tail_filesystem=D.FilesystemIdentity(1, "/", "nfs", "shared"),
        flock_probe=D.FlockCapability.UNPROVEN,
        node_authority_available=True,
    ) is D.ReconciliationMode.DEFER_TO_NODE
    assert D.resolve_reconciliation_mode(
        transport="fleet",
        node="n1",
        locality="remote",
        tail_path=tail,
        tail_filesystem=D.FilesystemIdentity(1, "/", "nfs", "shared"),
        flock_probe=D.FlockCapability.UNPROVEN,
    ) is D.ReconciliationMode.FAIL_CLOSED_DEFER
    with tempfile.TemporaryDirectory() as td:
        D._FLOCK_CAPABILITY_CACHE.clear()
        local_only = D._probe_flock_capability(
            transport="fleet",
            node="n1",
            tail_path=Path(td) / "remote.tail",
            filesystem=D.FilesystemIdentity(1, td, "nfs", "shared"),
        )
        assert local_only is not D.FlockCapability.COHERENT_CROSS_NODE


def test_round5_fallback_whole_producer_set_and_no_fcntl_lockfile() -> None:
    entry = {
        "queue_launch_token": "producer-token",
        "queue_worker_pid": 99_999_991,
        "queue_worker_identity": {"pid": 99_999_991},
        "queue_worker_pgid": 42_424,
        "queue_worker_group_leader_identity": {"pid": 42_424},
        "queue_producer_group_contract": True,
        "queue_producer_group_contract_enforced": True,
    }
    original_identity = D.goalflight_ledger.process_identity
    original_status = D._queue_claim_identity_status
    original_fcntl = D.fcntl
    try:
        D._queue_claim_identity_status = lambda pid, identity: ("dead", "dead")
        D.goalflight_ledger.process_identity = lambda pid: {
            "pid": pid,
            "identity_available": True,
            "start": "bound",
        }
        live = D.enumerate_token_producers(
            entry,
            [{"pid": 42_425, "ppid": 99_999_991, "pgid": 42_424, "command": "writer"}],
        )
        assert live.state is D.ProducerSetState.LIVE, live
        dead = D.enumerate_token_producers(entry, [])
        assert dead.state is D.ProducerSetState.DEAD, dead
        unenforced = D.enumerate_token_producers(
            {**entry, "queue_producer_group_contract_enforced": False},
            [],
        )
        assert unenforced.state is D.ProducerSetState.INDETERMINATE, unenforced
        assert D.enumerate_token_producers({**entry, "queue_worker_pgid": None}, []).state is D.ProducerSetState.INDETERMINATE

        D.fcntl = None
        with tempfile.TemporaryDirectory() as td:
            tail = Path(td) / "tail"
            # A free reconciler lockfile is serialization only; it is never EOF/death.
            try:
                with D._tail_reconciliation_lock(tail):
                    raise AssertionError("no-fcntl tail lock must fail closed")
            except D._TailLockBusy:
                pass
            assert not D._tail_lock_path(tail).exists()
    finally:
        D.goalflight_ledger.process_identity = original_identity
        D._queue_claim_identity_status = original_status
        D.fcntl = original_fcntl


def test_round5_deadline_locks_zero_mutation_and_progress() -> None:
    import goalflight_task

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            project = tmp / "project"
            project.mkdir()
            entry = {
                "dispatch_id": "round5-locks",
                "project_root": str(project),
                "queue_launch_token": "locks-token",
                "created_at": "2000-01-01T00:00:00+00:00",
                "task_ids": ["round5-task"],
                "request": {"cwd": str(project), "tail": str(tmp / "tail")},
            }
            admission = D.PreAdmitClass.STALE_NO_SPAWN
            store = goalflight_task.TaskStore(project)
            store._ensure_docs_dir_for_write()
            cases = []
            q_hold = D.try_acquire_queue_lock(queue, deadline_s=time.monotonic() + 1)
            assert q_hold is not None
            cases.append((q_hold, True, True, True))
            s_hold = store.try_store_lock(deadline_s=time.monotonic() + 1)
            assert s_hold is not None
            cases.append((s_hold, False, True, True))
            l_hold = D.goalflight_ledger.StateLock.try_acquire(time.monotonic() + 1)
            assert l_hold is not None
            cases.append((l_hold, False, False, True))

            def durable_snapshot() -> dict[str, bytes | None]:
                snapshot: dict[str, bytes | None] = {}
                for path in sorted(tmp.rglob("*")):
                    key = str(path.relative_to(tmp))
                    if key == "tail":
                        continue  # flock gate may create the output carrier; it is not reconciler-owned state
                    snapshot[key] = path.read_bytes() if path.is_file() else None
                return snapshot

            # Exercise each independently, releasing unrelated holds first.
            for index, (hold, need_q, need_s, need_l) in enumerate(cases):
                for other, *_ in cases:
                    if other is not hold:
                        other.release()
                before = durable_snapshot()
                started = time.monotonic()
                txn = D._begin_reconcile_transaction(
                    entry,
                    queue_dir=queue,
                    stale_s=0,
                    need_queue=need_q,
                    need_task_store=need_s,
                    need_ledger=need_l,
                    admission=admission,
                )
                elapsed = time.monotonic() - started
                assert txn is None, (index, txn)
                assert elapsed < 0.35, (index, elapsed)
                after = durable_snapshot()
                assert after == before, (index, before, after)
                hold.release()
                # Later uncontended dispatch progresses after the timed-out one.
                progressed = D._begin_reconcile_transaction(
                    {**entry, "dispatch_id": f"round5-progress-{index}"},
                    queue_dir=queue,
                    stale_s=0,
                    need_queue=need_q,
                    need_task_store=need_s,
                    need_ledger=need_l,
                    admission=admission,
                )
                assert progressed is not None, index
                progressed.release()
                if index + 1 < len(cases):
                    # Reacquire the next case after prior handles were released.
                    if index + 1 == 1:
                        cases[index + 1] = (store.try_store_lock(deadline_s=time.monotonic() + 1), False, True, True)
                    elif index + 1 == 2:
                        cases[index + 1] = (D.goalflight_ledger.StateLock.try_acquire(time.monotonic() + 1), False, False, True)
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def test_round5_missing_row_terminal_result_and_deferred_write() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        original_write = D.goalflight_ledger.write_record
        original_authority = D._entry_completion_authority
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            entry = {
                "dispatch_id": "round5-created-terminal",
                "queue_launch_token": "created-token",
                "created_at": "2000-01-01T00:00:00+00:00",
                "request": {"tail": str(tmp / "created.tail"), "cwd": str(tmp)},
            }
            txn = D._begin_reconcile_transaction(
                entry,
                queue_dir=queue,
                stale_s=0,
                need_queue=False,
                need_task_store=False,
                need_ledger=True,
                admission=D.PreAdmitClass.STALE_NO_SPAWN,
            )
            assert txn is not None
            try:
                result = D.commit_reconciled_terminal(
                    txn, entry, {"state": "worker_dead", "reason": "confirmed_dead"}
                )
            finally:
                txn.release()
            assert result.kind is D.TerminalCommitKind.CREATED_TERMINAL and result.committed
            row = json.loads(D.goalflight_ledger.record_path(entry["dispatch_id"]).read_text())
            assert row["state"] == "worker_dead" and row["terminal_state"] == "worker_dead"

            failed = {**entry, "dispatch_id": "round5-deferred-terminal"}
            txn = D._begin_reconcile_transaction(
                failed,
                queue_dir=queue,
                stale_s=0,
                need_queue=False,
                need_task_store=False,
                need_ledger=True,
                admission=D.PreAdmitClass.STALE_NO_SPAWN,
            )
            assert txn is not None
            D.goalflight_ledger.write_record = lambda _record: (_ for _ in ()).throw(OSError("injected"))
            try:
                deferred = D.commit_reconciled_terminal(
                    txn, failed, {"state": "worker_dead", "reason": "confirmed_dead"}
                )
            finally:
                txn.release()
            assert deferred.kind is D.TerminalCommitKind.DEFERRED and not deferred.committed
            assert not D.goalflight_ledger.record_path(failed["dispatch_id"], create=False).exists()

            authority_failed = {**entry, "dispatch_id": "round5-authority-deferred-terminal"}
            txn = D._begin_reconcile_transaction(
                authority_failed,
                queue_dir=queue,
                stale_s=0,
                need_queue=False,
                need_task_store=False,
                need_ledger=True,
                admission=D.PreAdmitClass.STALE_NO_SPAWN,
            )
            assert txn is not None
            D.goalflight_ledger.write_record = original_write
            D._entry_completion_authority = lambda *_args, **_kwargs: {
                "state": "deferred",
                "reason": "injected_authority_error",
            }
            try:
                authority_deferred = D.commit_reconciled_terminal(
                    txn,
                    authority_failed,
                    {"state": "worker_dead", "reason": "confirmed_dead"},
                )
            finally:
                txn.release()
            assert authority_deferred.kind is D.TerminalCommitKind.DEFERRED
            assert not authority_deferred.committed
            assert not D.goalflight_ledger.record_path(
                authority_failed["dispatch_id"], create=False
            ).exists()
        finally:
            D.goalflight_ledger.write_record = original_write
            D._entry_completion_authority = original_authority
            os.environ.clear()
            os.environ.update(old_env)


def test_round5_restore_commit_point_and_crash_retry() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "round5-restore"
            claim = queue / f"{dispatch_id}.json.claimed-1"
            entry = {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "dispatch_id": dispatch_id,
                "dispatch_argv": ["--agent", "test-dispatch"],
                "task_ids": ["round5-restore-task"],
                "queue_launch_token": "restore-token",
                "created_at": "2000-01-01T00:00:00+00:00",
                "project_root": str(tmp),
                "request": {"cwd": str(tmp), "tail": str(tmp / "restore.tail")},
            }
            claim.write_text(json.dumps(entry), encoding="utf-8")
            restored, decision = D._bounded_restore_claim(claim, entry, queue)
            assert restored and decision is None
            target = queue / f"{dispatch_id}.json"
            queued = json.loads(target.read_text())
            row = json.loads(D.goalflight_ledger.record_path(dispatch_id).read_text())
            assert queued["state"] == "queued"
            assert queued["restore_txn_id"] == row["restore_txn_id"]
            assert row["state"] == "queued" and not claim.exists()

            retry_id = "round5-restore-retry"
            retry_claim = queue / f"{retry_id}.json.claimed-1"
            retry_entry = {**entry, "dispatch_id": retry_id, "queue_launch_token": "retry-token"}
            retry_claim.write_text(json.dumps(retry_entry), encoding="utf-8")
            retry_target = queue / f"{retry_id}.json"
            txn_id = "injected-restore-txn"
            prepared = D._sanitize_restore_envelope(retry_entry, increment_recovery_count=True)
            prepared.update({"state": "restore_prepared", "restore_txn_id": txn_id})
            D._write_json_atomic(retry_target, prepared)
            ledger = D._new_reconciliation_record(retry_entry)
            ledger.update({"state": "queued", "terminal_state": "unknown", "restore_txn_id": txn_id, "queue_path": str(retry_target)})
            D.goalflight_ledger.write_record(ledger)
            retried, decision = D._bounded_restore_claim(retry_claim, retry_entry, queue)
            assert retried and decision is None
            assert json.loads(retry_target.read_text())["state"] == "queued"
            assert not retry_claim.exists()

            precommit_id = "round5-restore-precommit-retry"
            precommit_claim = queue / f"{precommit_id}.json.claimed-1"
            precommit_entry = {
                **entry,
                "dispatch_id": precommit_id,
                "queue_launch_token": "precommit-retry-token",
            }
            precommit_claim.write_text(json.dumps(precommit_entry), encoding="utf-8")
            precommit_target = queue / f"{precommit_id}.json"
            precommit_txn_id = "injected-precommit-restore-txn"
            precommit_prepared = D._sanitize_restore_envelope(
                precommit_entry,
                increment_recovery_count=True,
            )
            precommit_prepared.update(
                {"state": "restore_prepared", "restore_txn_id": precommit_txn_id}
            )
            D._write_json_atomic(precommit_target, precommit_prepared)
            assert not D.goalflight_ledger.record_path(precommit_id, create=False).exists()
            resumed, decision = D._bounded_restore_claim(
                precommit_claim,
                precommit_entry,
                queue,
            )
            assert resumed and decision is None
            resumed_queue = json.loads(precommit_target.read_text())
            resumed_ledger = json.loads(D.goalflight_ledger.record_path(precommit_id).read_text())
            assert resumed_queue["state"] == "queued"
            assert resumed_queue["restore_txn_id"] == precommit_txn_id
            assert resumed_ledger["restore_txn_id"] == precommit_txn_id
            assert not precommit_claim.exists()

            terminal_id = "round5-terminal-after-publish"
            terminal_claim = queue / f"{terminal_id}.json.claimed-1"
            terminal_entry = {
                **entry,
                "dispatch_id": terminal_id,
                "queue_launch_token": "terminal-after-publish-token",
            }
            terminal_claim.write_text(json.dumps(terminal_entry), encoding="utf-8")
            terminal_target = queue / f"{terminal_id}.json"
            terminal_txn_id = "terminal-after-publish-txn"
            terminal_queue = D._sanitize_restore_envelope(
                terminal_entry,
                increment_recovery_count=True,
            )
            terminal_queue.update(
                {"state": "queued", "restore_txn_id": terminal_txn_id}
            )
            D._write_json_atomic(terminal_target, terminal_queue)
            terminal_row = D._new_reconciliation_record(terminal_entry)
            terminal_row.update(
                {
                    "state": "complete",
                    "terminal_state": "complete",
                    "restore_txn_id": terminal_txn_id,
                    "queue_path": str(terminal_target),
                }
            )
            D.goalflight_ledger.write_record(terminal_row)
            restored, decision = D._bounded_restore_claim(
                terminal_claim,
                terminal_entry,
                queue,
            )
            assert not restored and decision and decision["state"] == "complete"
            assert not terminal_target.exists(), "terminal ledger must make published restore unlaunchable"
            assert terminal_claim.exists(), "claim cleanup follows durable terminal handling"

            deferred_id = "round5-deferred-after-publish"
            deferred_claim = queue / f"{deferred_id}.json.claimed-1"
            deferred_entry = {
                **entry,
                "dispatch_id": deferred_id,
                "queue_launch_token": "deferred-after-publish-token",
            }
            deferred_claim.write_text(json.dumps(deferred_entry), encoding="utf-8")
            deferred_target = queue / f"{deferred_id}.json"
            deferred_txn_id = "deferred-after-publish-txn"
            deferred_queue = D._sanitize_restore_envelope(
                deferred_entry,
                increment_recovery_count=True,
            )
            deferred_queue.update({"state": "queued", "restore_txn_id": deferred_txn_id})
            D._write_json_atomic(deferred_target, deferred_queue)
            deferred_row = D._new_reconciliation_record(deferred_entry)
            deferred_row.update(
                {
                    "state": "queued",
                    "terminal_state": "unknown",
                    "restore_txn_id": deferred_txn_id,
                    "queue_path": str(deferred_target),
                }
            )
            D.goalflight_ledger.write_record(deferred_row)
            original_authority = D._entry_completion_authority
            D._entry_completion_authority = lambda *_args, **_kwargs: {
                "state": "deferred",
                "reason": "injected_authority_error",
            }
            try:
                restored, decision = D._bounded_restore_claim(
                    deferred_claim,
                    deferred_entry,
                    queue,
                )
            finally:
                D._entry_completion_authority = original_authority
            assert not restored and decision and decision["state"] == "deferred"
            assert deferred_target.exists()
            assert json.loads(deferred_target.read_text())["state"] == "queued"
            assert deferred_claim.exists()
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def test_round5_authority_errors_defer_before_restore() -> None:
    import goalflight_task

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        old_env = os.environ.copy()
        original_load = goalflight_task.TaskStore.load_items
        original_read_records = D.goalflight_ledger.read_records
        try:
            os.environ.clear()
            os.environ.update(_b065_env(tmp))
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            project = tmp / "project"
            project.mkdir()
            dispatch_id = "round5-authority-error"
            claim = queue / f"{dispatch_id}.json.claimed-1"
            entry = {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "dispatch_id": dispatch_id,
                "dispatch_argv": ["--agent", "test-dispatch"],
                "task_ids": ["already-complete-but-unreadable"],
                "queue_launch_token": "authority-error-token",
                "created_at": "2000-01-01T00:00:00+00:00",
                "project_root": str(project),
                "request": {"cwd": str(project), "tail": str(tmp / "authority.tail")},
            }
            claim.write_text(json.dumps(entry), encoding="utf-8")
            goalflight_task.TaskStore.load_items = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("injected task truth failure")
            )
            restored, decision = D._bounded_restore_claim(claim, entry, queue)
            assert not restored
            assert decision and decision["state"] == "deferred"
            assert claim.exists()
            assert not (queue / f"{dispatch_id}.json").exists()
            assert not D.goalflight_ledger.record_path(dispatch_id, create=False).exists()

            goalflight_task.TaskStore.load_items = original_load
            D.goalflight_ledger.read_records = lambda: (_ for _ in ()).throw(
                OSError("injected ledger truth failure")
            )
            ledger_error = D._entry_completion_authority(entry, record={})
            assert ledger_error and ledger_error["state"] == "deferred", ledger_error
        finally:
            goalflight_task.TaskStore.load_items = original_load
            D.goalflight_ledger.read_records = original_read_records
            os.environ.clear()
            os.environ.update(old_env)


def test_round5_normal_restore_revalidates_fresh_admission() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        old_env = os.environ.copy()
        original_classify = D.classify_reconciliation_admission
        original_probe = D._probe_flock_capability
        try:
            os.environ.clear()
            os.environ.update(_b065_env(tmp))
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            dispatch_id = "round5-fresh-admission"
            claim = queue / f"{dispatch_id}.json.claimed-1"
            entry = {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "dispatch_id": dispatch_id,
                "dispatch_argv": ["--agent", "test-dispatch"],
                "queue_launch_token": "fresh-admission-token",
                "created_at": "2000-01-01T00:00:00+00:00",
                "project_root": str(tmp),
                "request": {"cwd": str(tmp), "tail": str(tmp / "fresh.tail")},
            }
            claim.write_text(json.dumps(entry), encoding="utf-8")
            states = iter((D.PreAdmitClass.STALE_NO_SPAWN, D.PreAdmitClass.LIVE))
            D.classify_reconciliation_admission = lambda *_args, **_kwargs: next(states)
            D._probe_flock_capability = lambda **_kwargs: D.FlockCapability.COHERENT_LOCAL
            restored, decision = D._restore_claim_if_incomplete(claim, entry, queue)
            assert restored is None and decision is None
            assert claim.exists()
            assert not (queue / f"{dispatch_id}.json").exists()
            assert not D.goalflight_ledger.record_path(dispatch_id, create=False).exists()
        finally:
            D.classify_reconciliation_admission = original_classify
            D._probe_flock_capability = original_probe
            os.environ.clear()
            os.environ.update(old_env)


def test_round5_launch_owned_claim_writers_hold_queue_lock() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        queue = tmp / "dispatch-queue"
        queue.mkdir()
        claim = queue / "round5-launch-writer.json.claimed-1"
        token = "round5-launch-writer-token"
        claim.write_text(
            json.dumps({"dispatch_id": "round5-launch-writer", "queue_launch_token": token}),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            from_queue=True,
            queue_claim_path=str(claim),
            queue_launch_token=token,
        )
        original_lock = D._queue_mutation_lock
        original_snapshot = D._process_snapshot
        events: list[str] = []

        @contextlib.contextmanager
        def observed_lock(path: Path):
            assert path == queue
            events.append("Q+")
            try:
                yield
            finally:
                events.append("Q-")

        try:
            D._queue_mutation_lock = observed_lock
            D._process_snapshot = lambda: []
            D._mark_queue_claim_launch_started(args)
            D._mark_queue_claim_worker_spawn_intent(args)
            D._mark_queue_claim_worker_spawned(args, os.getpid())
            fresh = json.loads(claim.read_text())
            D._mark_claim_failed(claim, fresh, reason="injected-pre-spawn-failure")
        finally:
            D._queue_mutation_lock = original_lock
            D._process_snapshot = original_snapshot
        assert events == ["Q+", "Q-", "Q+", "Q-", "Q+", "Q-", "Q+", "Q-"]
        assert not claim.exists()
        assert claim.with_name(f"{claim.name}.failed").exists()


def test_round5_tail_substitution_and_frozen_lock_order() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _b065_env(tmp)
        old_env = os.environ.copy()
        original_gate = D._tail_reconciliation_lock
        original_probe = D._probe_flock_capability
        original_q = D.try_acquire_queue_lock
        original_s = D.try_acquire_task_store_lock
        original_l = D.try_acquire_ledger_lock
        events: list[str] = []

        class Handle:
            def __init__(self, name): self.name = name
            def release(self): events.append(f"-{self.name}")

        @contextlib.contextmanager
        def gate(_tail):
            events.append("T")
            try:
                yield
            finally:
                events.append("-T")

        try:
            os.environ.clear()
            os.environ.update(env)
            queue = tmp / "state" / "dispatch-queue"
            queue.mkdir(parents=True)
            entry = {
                "dispatch_id": "round5-order",
                "queue_launch_token": "order-token",
                "created_at": "2000-01-01T00:00:00+00:00",
                "task_ids": ["order-task"],
                "project_root": str(tmp),
                "request": {"cwd": str(tmp), "tail": str(tmp / "tail-a")},
            }
            D._tail_reconciliation_lock = gate
            D._probe_flock_capability = lambda **_kwargs: D.FlockCapability.COHERENT_LOCAL
            D.try_acquire_queue_lock = lambda *_args, **_kwargs: (events.append("Q") or Handle("Q"))
            D.try_acquire_task_store_lock = lambda *_args, **_kwargs: (events.append("S") or Handle("S"))
            D.try_acquire_ledger_lock = lambda **_kwargs: (events.append("L") or Handle("L"))
            txn = D._begin_reconcile_transaction(
                entry,
                queue_dir=queue,
                stale_s=0,
                need_queue=True,
                need_task_store=True,
                need_ledger=True,
                admission=D.PreAdmitClass.STALE_NO_SPAWN,
            )
            assert txn is not None
            substituted = {**entry, "request": {**entry["request"], "tail": str(tmp / "tail-b")}}
            assert not D._reconcile_transaction_still_valid(txn, substituted)
            txn.release()
            assert events == ["T", "Q", "S", "L", "-L", "-S", "-Q", "-T"], events
        finally:
            D._tail_reconciliation_lock = original_gate
            D._probe_flock_capability = original_probe
            D.try_acquire_queue_lock = original_q
            D.try_acquire_task_store_lock = original_s
            D.try_acquire_ledger_lock = original_l
            os.environ.clear()
            os.environ.update(old_env)


def main() -> None:
    test_submit_records_replayable_request_without_capacity_acquire()
    test_submit_is_idempotent_for_matching_args_and_rejects_collisions()
    test_submit_ignores_matching_failed_claim_tombstone_for_requeue()
    test_duplicate_submit_runs_opportunistic_drain()
    test_concurrent_submit_same_id_is_idempotent()
    test_submit_write_error_is_clean()
    test_submit_status_write_error_removes_queue_entry()
    test_submit_rejects_active_waiting_capacity_ledger()
    test_drain_launches_queued_request_once_and_exits()
    test_drain_waits_for_submit_status_recording()
    test_drain_write_error_is_json_error_without_traceback()
    test_acp_submit_then_drain_replays_from_queue()
    test_drain_leaves_request_queued_when_capacity_full()
    test_drain_orders_by_priority_then_created_at_fifo()
    test_drain_degrades_unknown_priority_to_normal_and_survives_bad_json_prescan()
    test_drain_does_not_tombstone_valid_entry_on_stale_prescan_read_error()
    test_stale_claim_without_worker_record_is_recovered()
    test_failed_claim_tombstone_is_not_recovered()
    test_fresh_token_only_claim_waits_for_stale_window()
    test_stale_claim_launch_token_requires_matching_worker_record()
    test_stale_claim_result_marker_with_rate_limit_text_completes()
    test_drain_replay_argv_injects_queue_control_flags()
    test_drain_requires_token_matched_ledger_before_clearing_claim()
    test_legacy_claim_dead_worker_without_token_defers_fail_closed()
    test_token_mismatch_recovery_preserves_live_worker_record()
    test_token_mismatch_recovery_preserves_terminal_worker_record()
    test_worker_dead_tail_rate_limit_reaches_pressure_sensor()
    test_submit_default_runs_one_drain_pass_after_queue_write()
    test_submit_drain_on_submit_error_does_not_fail_submit()
    test_submit_default_drain_launches_once_and_duplicate_submit_does_not_double_launch()
    test_b065_state_flips_to_terminal_so_wait_resolves()
    test_b065_linked_vs_unlinked_action_matrix()
    test_b065_superseded_does_not_demote_task_success()
    test_b065_launch_age_ignores_updated_at_heartbeats()
    test_b065_weak_worker_pid_claim_unlink_then_ledger_terminalizes()
    test_b065_late_complete_wins_over_worker_dead()
    test_b065_live_stdout_lock_skips_reconciliation_promptly()
    test_b065_unlinked_quarantine_not_deleted_or_terminalized()
    test_b065_concurrent_double_restore_second_exhausts()
    test_b065_late_complete_racing_restore_wins()
    test_b065_normal_drain_restore_honors_completed_linked_task()
    test_b065_carrier_mtime_poison_still_orphans()
    test_b065_unlinked_early_terminal_and_live_carriers_quarantine()
    test_b065_unlinked_complete_carrier_quarantines_before_authority()
    test_b065_unlinked_pre_spawn_carrier_quarantines_instead_of_restore()
    test_b065_identity_exception_is_indeterminate()
    test_round5_admission_and_mode_decision_tables()
    test_round5_fallback_whole_producer_set_and_no_fcntl_lockfile()
    test_round5_deadline_locks_zero_mutation_and_progress()
    test_round5_missing_row_terminal_result_and_deferred_write()
    test_round5_restore_commit_point_and_crash_retry()
    test_round5_authority_errors_defer_before_restore()
    test_round5_normal_restore_revalidates_fresh_admission()
    test_round5_launch_owned_claim_writers_hold_queue_lock()
    test_round5_tail_substitution_and_frozen_lock_order()
    print("OK: dispatch queue tests pass")


if __name__ == "__main__":
    main()
