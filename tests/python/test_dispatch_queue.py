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
        assert result == {"restored": 0, "cleared": 0, "pending_launch": 0}, result
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
        assert result == {"restored": 0, "cleared": 0, "pending_launch": 1}, result
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
                        "queue_launch_token": "token-only",
                    }
                ),
                encoding="utf-8",
            )
            os.utime(token_only_claim, (time.time() - 60, time.time() - 60))
            restored = D._recover_claimed_queue_entries(queue, stale_s=0)
            assert restored == {"restored": 1, "cleared": 0, "pending_launch": 0}, restored
            restored_path = queue / "token-only.json"
            assert restored_path.exists()
            restored_entry = json.loads(restored_path.read_text(encoding="utf-8"))
            assert restored_entry["state"] == "queued", restored_entry
            assert "queue_launch_token" not in restored_entry, restored_entry
            assert not token_only_claim.exists()
            restored_path.unlink()

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
                        "request": {
                            "agent": "test-dispatch",
                            "cwd": str(ROOT),
                            "tail": str(crash_tail),
                            "status_json": str(crash_status),
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
            assert recovered == {"restored": 0, "cleared": 1, "pending_launch": 0}, recovered
            assert not crash_claim.exists()
            assert not (queue / "crash-window.json").exists()
            record = json.loads((tmp / "state" / "runs.d" / "crash-window.json").read_text(encoding="utf-8"))
            assert record["state"] == "worker_dead", record
            assert record["terminal_state"] == "worker_dead", record
            assert record["queue_launch_token"] == "intent-token", record
            status = json.loads(crash_status.read_text(encoding="utf-8"))
            assert status["state"] == "worker_dead", status
            second_drain = _run(
                [sys.executable, str(DISPATCH), "drain", "--claim-stale-s", "0", "--json"],
                env,
            )
            assert second_drain.returncode == 0, (second_drain.stdout, second_drain.stderr)
            second_payload = json.loads(second_drain.stdout)
            assert second_payload["launched"] == 0, second_payload
            assert second_payload["remaining"] == 0, second_payload

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
        assert cleared["cleared"] == 1, cleared
        assert not matched_claim.exists()
        assert not (tmp / "state" / "dispatch-queue" / "matched.json").exists()


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
                        "request": {
                            "agent": "codex",
                            "cwd": str(ROOT),
                            "tail": str(tail),
                            "status_json": str(status),
                        },
                        "queue_launch_token": "queued-result-token",
                        "queue_launch_started": True,
                        "queue_worker_spawn_intent": True,
                        "queue_worker_spawn_intent_at": D.goalflight_ledger.utc_now(),
                    }
                ),
                encoding="utf-8",
            )
            os.utime(claim, (time.time() - 60, time.time() - 60))
            recovered = D._recover_claimed_queue_entries(queue, stale_s=0)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert recovered == {"restored": 0, "cleared": 1, "pending_launch": 0}, recovered
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


def test_legacy_claim_dead_worker_record_falls_through_to_recovery() -> None:
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
                    }
                ),
                encoding="utf-8",
            )
            os.utime(claim, (time.time() - 60, time.time() - 60))
            result = D._recover_claimed_queue_entries(queue, stale_s=0)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result == {"restored": 1, "cleared": 0, "pending_launch": 0}, result
        assert (queue / "legacy-dead-worker.json").exists()
        assert not claim.exists()


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
        assert result == {"restored": 0, "cleared": 0, "pending_launch": 1}, result
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
        assert result == {"restored": 0, "cleared": 1, "pending_launch": 0}, result
        assert not claim.exists()
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
        assert record.get("error", {}).get("reason") == "worker_dead_no_terminal_marker", record
        pressure = _run(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_rate_pressure.py"),
                "--state-dir",
                str(tmp / "state"),
                "--threshold",
                "1",
                "--json",
            ],
            env,
        )
        payload = json.loads(pressure.stdout)
        assert any(
            row.get("provider") == "openai" or row.get("budget_key") == "provider:openai"
            for row in payload["providers_under_pressure"]
        ), payload


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
    test_legacy_claim_dead_worker_record_falls_through_to_recovery()
    test_token_mismatch_recovery_preserves_live_worker_record()
    test_token_mismatch_recovery_preserves_terminal_worker_record()
    test_worker_dead_tail_rate_limit_reaches_pressure_sensor()
    test_submit_default_runs_one_drain_pass_after_queue_write()
    test_submit_drain_on_submit_error_does_not_fail_submit()
    test_submit_default_drain_launches_once_and_duplicate_submit_does_not_double_launch()
    print("OK: dispatch queue tests pass")


if __name__ == "__main__":
    main()
