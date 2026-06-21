#!/usr/bin/env python3
"""Regression tests for goalflight_dispatch.py capacity + identity registration."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("dispatch capacity tests launch POSIX bash workers")

import datetime as dt
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
STATUS = ROOT / "scripts" / "goalflight_status.py"


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    # Tests assert the instant DISPATCH-BLOCKED path; disable the capacity
    # queue (lane defaults wait minutes, which would hang subprocess asserts).
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    return env


def _status(env: dict[str, str]) -> dict:
    proc = subprocess.run(
        [sys.executable, str(STATUS), "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(proc.stdout)


def _wait_for(fn, timeout_s: float = 10.0):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"condition not met before timeout; last={last!r}")


def _record(payload: dict, dispatch_id: str) -> dict | None:
    for row in payload["dispatch"].get("records", []):
        if row.get("dispatch_id") == dispatch_id:
            return row
    return None


def _leases(payload: dict, dispatch_id: str) -> list[dict]:
    return [
        lease
        for lease in payload["capacity_state"].get("leases", {}).values()
        if lease.get("dispatch_id") == dispatch_id
    ]


def _process_exists(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _kill_if_alive(pid: int | None) -> None:
    if not _process_exists(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _dispatch_command(
    tmp: Path,
    dispatch_id: str,
    worker_code: str,
    *,
    status_path: Path | None = None,
    poll_secs: str = "0.2",
    max_idle_secs: str = "20",
    controller_pid: int | None = None,
    from_queue: bool = False,
    launch_detached: bool = False,
) -> list[str]:
    cmd = [
        sys.executable,
        str(DISPATCH),
        "--agent",
        "test-dispatch",
        "--dispatch-id",
        dispatch_id,
        "--tail",
        str(tmp / f"{dispatch_id}.tail"),
        "--status-json",
        str(status_path or (tmp / f"{dispatch_id}.status.json")),
        "--poll-secs",
        poll_secs,
        "--max-idle-secs",
        max_idle_secs,
    ]
    if controller_pid is not None:
        cmd += ["--controller-pid", str(controller_pid)]
    if from_queue:
        cmd += ["--from-queue"]
    if launch_detached:
        cmd += ["--launch-detached"]
    cmd += ["--", sys.executable, "-c", worker_code]
    return cmd


def _run_dispatch(
    tmp: Path,
    env: dict[str, str],
    dispatch_id: str,
    worker_code: str,
    *,
    status_path: Path | None = None,
    poll_secs: str = "0.2",
    max_idle_secs: str = "20",
    controller_pid: int | None = None,
    from_queue: bool = False,
    launch_detached: bool = False,
    timeout_s: float = 90.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _dispatch_command(
            tmp,
            dispatch_id,
            worker_code,
            status_path=status_path,
            poll_secs=poll_secs,
            max_idle_secs=max_idle_secs,
            controller_pid=controller_pid,
            from_queue=from_queue,
            launch_detached=launch_detached,
        ),
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
    )


def _worker_pid_from_stdout(stdout: str) -> int | None:
    for line in stdout.splitlines():
        if line.startswith("DISPATCH-START "):
            return json.loads(line.split(" ", 1)[1]).get("worker_pid")
    return None


def _dispatch_end(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("DISPATCH-END "):
            return json.loads(line.split(" ", 1)[1])
    raise AssertionError(f"missing DISPATCH-END in stdout:\n{stdout}")


def _dispatch_launched(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("DISPATCH-LAUNCHED "):
            return json.loads(line.split(" ", 1)[1])
    raise AssertionError(f"missing DISPATCH-LAUNCHED in stdout:\n{stdout}")


def _capacity_release_stale(env: dict[str, str]) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "goalflight_capacity.py"),
            "release-stale",
            "--state",
            "expired",
            "--reason",
            "test_release_stale",
            "--keep",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(proc.stdout)


def _assert_terminal_record_and_lease(env: dict[str, str], dispatch_id: str, state: str) -> None:
    payload = _status(env)
    row = _record(payload, dispatch_id)
    assert row and row.get("state") == state, row
    assert row.get("classification") == state, row
    expected_terminal = "error" if state == "failed" else state
    assert row.get("terminal_state") == expected_terminal, row
    assert row.get("engine") == "test-dispatch", row
    assert row.get("shape") == "bash", row
    assert row.get("account") == "default", row
    assert row.get("ended_at"), row
    assert isinstance(row.get("elapsed_s"), (int, float)), row
    assert row.get("worker_still_alive") is not None, row
    if expected_terminal != "complete":
        assert row.get("reason") or row.get("error"), row
    leases = _leases(payload, dispatch_id)
    assert leases, f"lease missing for {dispatch_id}"
    assert all(lease.get("state") == state for lease in leases), leases
    assert all(lease.get("released_at") for lease in leases), leases


def case_status_sees_dispatch_and_lease_releases() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        dispatch_id = "dispatch-visible-release"
        worker_code = (
            "import time; "
            "print('worker-start', flush=True); "
            "time.sleep(15); "
            "print('COMPLETE: done', flush=True); "
            "time.sleep(0.2)"
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                dispatch_id,
                "--tail",
                str(tmp / "tail.txt"),
                "--status-json",
                str(tmp / "status.json"),
                "--poll-secs",
                "0.2",
                "--max-idle-secs",
                "20",
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
            running = _wait_for(
                lambda: (
                    payload
                    if (payload := _status(env))
                    and (row := _record(payload, dispatch_id))
                    and row.get("state") == "running"
                    and row.get("classification") == "expected_live"
                    and row.get("worker_pid")
                    and any(
                        lease.get("state") == "active"
                        and lease.get("worker_pid") == row.get("worker_pid")
                        for lease in _leases(payload, dispatch_id)
                    )
                    else None
                ),
                timeout_s=30.0,
            )
            row = _record(running, dispatch_id)
            active = [
                lease
                for lease in _leases(running, dispatch_id)
                if lease.get("state") == "active" and lease.get("worker_pid")
            ]
            assert row and row["state"] == "running", row
            assert row["classification"] == "expected_live", row
            assert row["worker_pid"], row
            assert active, active
            assert active[0].get("worker_pid") == row["worker_pid"], active

            stdout, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)

        assert proc.returncode == 0, f"dispatch rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
        after = _status(env)
        released = _leases(after, dispatch_id)
        assert released and all(lease.get("state") != "active" for lease in released), released
        assert all(lease.get("released_at") for lease in released), released
        row = _record(after, dispatch_id)
        assert row and row.get("state") == "complete", row


def case_live_controller_pidfile_preserves_blocked_worker_for_reattach() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        dispatch_id = "dispatch-blocked-reattach"
        worker_pid = None
        try:
            proc = _run_dispatch(
                tmp,
                env,
                dispatch_id,
                "import time; print('BLOCKED: needs controller', flush=True); time.sleep(60)",
                controller_pid=os.getpid(),
            )
            worker_pid = _worker_pid_from_stdout(proc.stdout)
            assert proc.returncode == 4, f"dispatch rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
            assert worker_pid and _process_exists(worker_pid), f"worker not left for cleanup: {worker_pid}"
            pidfiles = list((tmp / "pids").glob("*.jsonl"))
            assert pidfiles, "dispatch pidfile missing"
            assert pidfiles[0].name.startswith(f"{os.getpid()}."), pidfiles[0]

            cleanup = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.path.insert(0, 'scripts'); "
                    "import goalflight_acp_client; print(goalflight_acp_client.cleanup_ghosts())",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            assert int(cleanup.stdout.strip()) == 0, cleanup
            assert _process_exists(worker_pid), "cleanup killed live worker owned by live controller"
            assert list((tmp / "pids").glob("*.jsonl")), "pidfile removed before reattach"
            _assert_terminal_record_and_lease(env, dispatch_id, "blocked")
        finally:
            _kill_if_alive(worker_pid)


def case_from_queue_detached_launch_stamps_pidfile() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        dispatch_id = "dispatch-detached-pidfile"
        worker_pid = None
        watcher_pid = None
        try:
            proc = _run_dispatch(
                tmp,
                env,
                dispatch_id,
                "import time; print('worker-start', flush=True); time.sleep(60)",
                poll_secs="0.1",
                max_idle_secs="20",
                from_queue=True,
                launch_detached=True,
            )
            assert proc.returncode == 0, (
                f"dispatch rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
            )
            launched = _dispatch_launched(proc.stdout)
            worker_pid = int(launched["worker_pid"])
            watcher_pid = int(launched["watcher_pid"])
            assert worker_pid and _process_exists(worker_pid), launched

            pidfiles = list((tmp / "pids").glob("*.jsonl"))
            assert len(pidfiles) == 1, pidfiles
            pid_entry = json.loads(pidfiles[0].read_text().splitlines()[0])
            assert pid_entry.get("detached") is True, pid_entry
        finally:
            _kill_if_alive(worker_pid)
            _kill_if_alive(watcher_pid)


def case_from_queue_detached_launch_reparents_lease_and_survives_release_stale() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        dispatch_id = "dispatch-detached-lease"
        worker_pid = None
        watcher_pid = None
        try:
            proc = _run_dispatch(
                tmp,
                env,
                dispatch_id,
                "import time; print('worker-start', flush=True); time.sleep(60)",
                poll_secs="0.1",
                max_idle_secs="20",
                from_queue=True,
                launch_detached=True,
            )
            assert proc.returncode == 0, (
                f"dispatch rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
            )
            launched = _dispatch_launched(proc.stdout)
            worker_pid = int(launched["worker_pid"])
            watcher_pid = int(launched["watcher_pid"])
            assert worker_pid and _process_exists(worker_pid), launched

            payload = _status(env)
            leases = _leases(payload, dispatch_id)
            assert len(leases) == 1, leases
            lease = leases[0]
            assert lease.get("state") == "active", lease
            assert lease.get("worker_pid") == worker_pid, lease
            assert lease.get("controller_pid") == worker_pid, lease
            assert lease.get("detached_controller_pid") not in (None, worker_pid), lease
            assert lease.get("detached_at"), lease
            assert lease.get("detached_reason") == "bash_launch_detached", lease

            live_release = _capacity_release_stale(env)
            assert live_release["count"] == 0, live_release
            payload = _status(env)
            live_lease = _leases(payload, dispatch_id)[0]
            assert live_lease.get("state") == "active", live_lease

            _kill_if_alive(worker_pid)
            _wait_for(lambda: not _process_exists(worker_pid), timeout_s=10.0)
            dead_release = _capacity_release_stale(env)
            assert dead_release["count"] == 1, dead_release
            payload = _status(env)
            dead_lease = _leases(payload, dispatch_id)[0]
            assert dead_lease.get("state") == "expired", dead_lease
            assert dead_lease.get("reason") == "test_release_stale", dead_lease
        finally:
            _kill_if_alive(worker_pid)
            _kill_if_alive(watcher_pid)


def case_capacity_block_does_not_spawn() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "1"
        held = subprocess.run(
            [
                sys.executable,
                "scripts/goalflight_capacity.py",
                "acquire",
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                "held-capacity",
                "--project-root",
                str(ROOT),
                "--ttl-s",
                "60",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        assert json.loads(held.stdout)["decision"] == "allow", held.stdout

        marker = tmp / "should-not-exist"
        status_path = tmp / "blocked.status.json"
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                "blocked-capacity",
                "--tail",
                str(tmp / "blocked.tail"),
                "--status-json",
                str(status_path),
                "--",
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('spawned')",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.returncode == 2, f"expected capacity block rc=2; stdout={proc.stdout} stderr={proc.stderr}"
        assert "DISPATCH-START " not in proc.stdout, proc.stdout
        assert not marker.exists(), "blocked dispatch spawned worker"
        assert json.loads(status_path.read_text())["state"] == "blocked_capacity"
        # Instant fail must report the queue context (waited_s ~0, 1 attempt).
        reason = json.loads(status_path.read_text())["reason"]
        assert reason.get("attempts") == 1, reason


def case_capacity_wait_queues_until_slot_frees() -> None:
    """The capacity queue: a dispatch with a wait budget re-attempts acquire
    and proceeds when the blocking lease is released (no controller re-dispatch
    loop needed)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "1"
        env.pop("GOALFLIGHT_CAPACITY_WAIT_S", None)  # use the CLI flag below
        held = subprocess.run(
            [
                sys.executable, "scripts/goalflight_capacity.py", "acquire",
                "--agent", "test-dispatch", "--dispatch-id", "held-for-queue",
                "--project-root", str(ROOT), "--ttl-s", "60",
            ],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        lease_id = json.loads(held.stdout)["lease"]["lease_id"]

        marker = tmp / "queued-then-spawned"
        status_path = tmp / "queued.status.json"
        worker_code = (
            f"from pathlib import Path; import time; "
            f"Path({str(marker)!r}).write_text('spawned'); "
            f"print('COMPLETE: queued worker done', flush=True); time.sleep(0.3)"
        )
        proc = subprocess.Popen(
            [
                sys.executable, str(DISPATCH),
                "--agent", "test-dispatch", "--dispatch-id", "queued-dispatch",
                "--capacity-wait-s", "60",
                "--poll-secs", "0.2", "--max-idle-secs", "20",
                "--tail", str(tmp / "queued.tail"),
                "--status-json", str(status_path),
                "--", sys.executable, "-c", worker_code,
            ],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # Let it enter the queue, confirm the interim status, then free the slot.
        deadline = time.time() + 12
        waited_status_seen = False
        while time.time() < deadline:
            if status_path.exists():
                try:
                    if json.loads(status_path.read_text()).get("state") == "waiting_capacity":
                        waited_status_seen = True
                        break
                except (json.JSONDecodeError, OSError):
                    pass
            time.sleep(0.25)
        assert waited_status_seen, "dispatch never reported waiting_capacity"

        # While queued: --done must report LIVE (exit 1), not done/ambiguous —
        # the pre-recorded ledger entry classifies as queued_capacity.
        done = subprocess.run(
            [sys.executable, str(STATUS), "--done", "queued-dispatch"],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert done.returncode == 1, f"--done on queued dispatch: rc={done.returncode}\n{done.stdout}\n{done.stderr}"

        # While queued: reusing the same explicit dispatch-id must be refused
        # (the reused-id guard sees the non-terminal waiting_capacity record).
        reuse = subprocess.run(
            [
                sys.executable, str(DISPATCH),
                "--agent", "test-dispatch", "--dispatch-id", "queued-dispatch",
                "--capacity-wait-s", "0",
                "--tail", str(tmp / "reuse.tail"),
                "--status-json", str(tmp / "reuse.status.json"),
                "--", sys.executable, "-c", "print('nope')",
            ],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert reuse.returncode != 0, "duplicate dispatch-id accepted during capacity wait"
        assert "non-terminal" in (reuse.stderr + reuse.stdout), (reuse.stdout, reuse.stderr)

        subprocess.run(
            [
                sys.executable, "scripts/goalflight_capacity.py", "release",
                "--lease-id", lease_id,
            ],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, f"queued dispatch rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
        assert "CAPACITY-WAIT " in stdout, stdout
        assert "DISPATCH-START " in stdout, stdout
        assert marker.exists(), "queued dispatch never spawned after slot freed"


def case_capacity_wait_interrupt_writes_terminal_status() -> None:
    """SIGTERM during the queue must not strand a non-terminal
    waiting_capacity status: the launcher writes blocked_capacity
    (wait_interrupted) and the ledger record finishes terminal."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "1"
        subprocess.run(
            [
                sys.executable, "scripts/goalflight_capacity.py", "acquire",
                "--agent", "test-dispatch", "--dispatch-id", "held-for-interrupt",
                "--project-root", str(ROOT), "--ttl-s", "60",
            ],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        status_path = tmp / "interrupted.status.json"
        proc = subprocess.Popen(
            [
                sys.executable, str(DISPATCH),
                "--agent", "test-dispatch", "--dispatch-id", "interrupted-dispatch",
                "--capacity-wait-s", "120",
                "--tail", str(tmp / "interrupted.tail"),
                "--status-json", str(status_path),
                "--", sys.executable, "-c", "print('never runs')",
            ],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _wait_for(
            lambda: status_path.exists()
            and json.loads(status_path.read_text()).get("state") == "waiting_capacity",
            timeout_s=15.0,
        )
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=20)
        final = json.loads(status_path.read_text())
        assert final["state"] == "blocked_capacity", final
        assert final["reason"].get("reason") == "wait_interrupted", final
        payload = _status(env)
        row = _record(payload, "interrupted-dispatch")
        assert row and row.get("state") == "blocked_capacity", row


def case_require_prompt_before_side_effects() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "codex",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.returncode == 64, f"expected usage rc=64; stdout={proc.stdout} stderr={proc.stderr}"
        assert "requires --prompt or --prompt-file" in proc.stderr, proc.stderr
        assert not (tmp / "state").exists(), "prompt/id/lease side effects happened before prompt guard"


def case_account_guard_before_prompt_materialization() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        missing = f"missing-account-{os.getpid()}"
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "codex",
                "--account",
                missing,
                "--prompt",
                "COMPLETE: should not materialize",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.returncode == 64, f"expected usage rc=64; stdout={proc.stdout} stderr={proc.stderr}"
        assert "Refusing to bill the wrong account" in proc.stderr, proc.stderr
        assert not (tmp / "state").exists(), "prompt/id/lease side effects happened before account guard"


def case_codex_routed_subscription_strips_openai_api_key() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        env["OPENAI_API_KEY"] = "must-not-leak"

        for agent in ("codex", "codex-acp"):
            dispatch_id = f"billing-strip-{agent}"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(DISPATCH),
                    "--agent",
                    agent,
                    "--dispatch-id",
                    dispatch_id,
                    "--tail",
                    str(tmp / f"{dispatch_id}.tail"),
                    "--status-json",
                    str(tmp / f"{dispatch_id}.status.json"),
                    "--poll-secs",
                    "0.1",
                    "--max-idle-secs",
                    "5",
                    "--billing",
                    "sub",
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "import os; "
                        "print(('BLOCKED' if os.environ.get('OPENAI_API_KEY') else 'COMPLETE') "
                        "+ ': openai env', flush=True)"
                    ),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=90,
            )
            assert proc.returncode == 0, f"{agent} leaked OPENAI_API_KEY or failed\nstdout={proc.stdout}\nstderr={proc.stderr}"
            assert json.loads((tmp / f"{dispatch_id}.status.json").read_text())["state"] == "complete"


def case_state_dir_auto_paths() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--poll-secs",
                "0.1",
                "--max-idle-secs",
                "5",
                "--",
                sys.executable,
                "-c",
                "print('COMPLETE: state-dir', flush=True)",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
        )
        assert proc.returncode == 0, f"dispatch rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        end = _dispatch_end(proc.stdout)
        dispatch_dir = tmp / "state" / "dispatch"
        assert str(dispatch_dir) in end["tail"], end
        assert str(dispatch_dir) in end["status_json"], end
        assert Path(end["tail"]).exists(), end
        assert Path(end["status_json"]).exists(), end


def case_dispatch_end_worker_still_alive_flags() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        alive_proc = _run_dispatch(
            tmp,
            env,
            "worker-still-alive-true",
            "import time; print('COMPLETE: stays alive', flush=True); time.sleep(5)",
            poll_secs="0.1",
            max_idle_secs="10",
            timeout_s=90,
        )
        alive_end = _dispatch_end(alive_proc.stdout)
        try:
            assert alive_proc.returncode == 0, alive_proc
            assert alive_end.get("worker_still_alive") is False, alive_end
        finally:
            _kill_if_alive(alive_end.get("worker_pid"))

        dead_proc = _run_dispatch(
            tmp,
            env,
            "worker-still-alive-false",
            "print('COMPLETE: exits', flush=True)",
            poll_secs="0.2",
            max_idle_secs="10",
            timeout_s=90,
        )
        dead_end = _dispatch_end(dead_proc.stdout)
        assert dead_proc.returncode == 0, dead_proc
        assert dead_end.get("worker_still_alive") is False, dead_end


def case_dispatch_id_collision_suffix() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        env["GOALFLIGHT_DISPATCH_ID_SEED"] = "collision-id"
        env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "2"

        def start_once() -> subprocess.Popen[str]:
            return subprocess.Popen(
                [
                    sys.executable,
                    str(DISPATCH),
                    "--agent",
                    "test-dispatch",
                    "--poll-secs",
                    "0.1",
                    "--max-idle-secs",
                    "5",
                    "--",
                    sys.executable,
                    "-c",
                    "print('COMPLETE: collision', flush=True)",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        procs = [start_once(), start_once()]
        completed = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=90)
            assert proc.returncode in {0, 2}, f"dispatch rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
            if proc.returncode == 0:
                completed.append(_dispatch_end(stdout))
            else:
                assert stdout.startswith("DISPATCH-BLOCKED "), stdout

        id_locks = sorted((tmp / "state" / "dispatch" / ".dispatch-ids").glob("*.json"))
        ids = {json.loads(path.read_text(encoding="utf-8"))["dispatch_id"] for path in id_locks}
        assert ids == {"collision-id", "collision-id-2"}, completed
        dispatch_dir = tmp / "state" / "dispatch"
        tail_paths = {str(dispatch_dir / f"{dispatch_id}.tail") for dispatch_id in ids}
        status_paths = {str(dispatch_dir / f"{dispatch_id}.status.json") for dispatch_id in ids}
        assert len(tail_paths) == 2, tail_paths
        assert len(status_paths) == 2, status_paths
        assert all(Path(path).exists() for path in status_paths), status_paths
        if len(completed) == 2:
            assert {item["tail"] for item in completed} == tail_paths, completed
            assert {item["status_json"] for item in completed} == status_paths, completed


def case_worker_dead_state_releases_and_classifies_terminal() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        dispatch_id = "dispatch-worker-dead"
        proc = _run_dispatch(
            tmp,
            env,
            dispatch_id,
            "import sys; print('worker-start', flush=True); sys.exit(7)",
        )
        assert proc.returncode == 1, f"dispatch rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        assert json.loads((tmp / f"{dispatch_id}.status.json").read_text())["state"] == "worker_dead"
        _assert_terminal_record_and_lease(env, dispatch_id, "worker_dead")


def case_idle_timeout_state_releases_and_classifies_terminal() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        env["GOALFLIGHT_TEST_MODE"] = "1"
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        dispatch_id = "dispatch-idle-timeout"
        worker_pid = None
        try:
            proc = _run_dispatch(
                tmp,
                env,
                dispatch_id,
                "import time; time.sleep(60)",
                poll_secs="0.1",
                max_idle_secs="0.2",
                timeout_s=90,
            )
            worker_pid = _worker_pid_from_stdout(proc.stdout)
            assert proc.returncode == 2, f"dispatch rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
            assert json.loads((tmp / f"{dispatch_id}.status.json").read_text())["state"] == "idle_timeout"
            _assert_terminal_record_and_lease(env, dispatch_id, "idle_timeout")
        finally:
            _kill_if_alive(worker_pid)


def case_watcher_failure_releases_as_failed() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        dispatch_id = "dispatch-watcher-failure"
        bad_status_path = tmp / "status.json"
        bad_status_path.mkdir()
        worker_pid = None
        try:
            proc = _run_dispatch(
                tmp,
                env,
                dispatch_id,
                "import time; print('worker-start', flush=True); time.sleep(60)",
                status_path=bad_status_path,
                timeout_s=90,
            )
            worker_pid = _worker_pid_from_stdout(proc.stdout)
            assert proc.returncode == 1, f"dispatch rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
            assert '"watcher_exit": 1' in proc.stdout, proc.stdout
            _assert_terminal_record_and_lease(env, dispatch_id, "failed")
        finally:
            _kill_if_alive(worker_pid)


def case_nonzero_watcher_running_status_finalizes_failed() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        dispatch_id = "dispatch-watcher-running-failure"
        status_path = tmp / f"{dispatch_id}.status.json"
        sys.path.insert(0, str(ROOT / "scripts"))
        import goalflight_dispatch as dispatch_mod  # noqa: E402

        old_argv = sys.argv[:]
        old_spawn = dispatch_mod._spawn_daemonized_process
        old_wait = dispatch_mod._wait_for_detached_watcher
        old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
        old_pidfile_dir = os.environ.get("GOAL_FLIGHT_PIDFILE_DIR")

        def fake_spawn(argv, *args, **kwargs):
            if kwargs.get("label") == "watcher":
                return 999999
            return old_spawn(argv, *args, **kwargs)

        def fake_wait(**kwargs):
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(
                json.dumps(
                    {
                        "state": "running",
                        "worker_alive": True,
                    }
                ),
                encoding="utf-8",
            )
            return 9, {"state": "running", "worker_alive": True}, "watcher_exit_9"

        try:
            os.environ.update(env)
            sys.argv = _dispatch_command(
                tmp,
                dispatch_id,
                "import sys; print('worker-start', flush=True); sys.exit(0)",
                status_path=status_path,
            )[1:]
            dispatch_mod._spawn_daemonized_process = fake_spawn
            dispatch_mod._wait_for_detached_watcher = fake_wait
            rc = dispatch_mod.main()
        finally:
            dispatch_mod._spawn_daemonized_process = old_spawn
            dispatch_mod._wait_for_detached_watcher = old_wait
            sys.argv = old_argv
            if old_state_dir is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
            if old_pidfile_dir is None:
                os.environ.pop("GOAL_FLIGHT_PIDFILE_DIR", None)
            else:
                os.environ["GOAL_FLIGHT_PIDFILE_DIR"] = old_pidfile_dir

        assert rc == 9, rc
        payload = _status(env)
        row = _record(payload, dispatch_id)
        assert row and row.get("state") == "failed", row
        assert row.get("terminal_state") == "error", row
        assert row.get("reason") == "watcher_exit_9", row
        assert all(lease.get("state") == "failed" for lease in _leases(payload, dispatch_id)), payload


def case_wait_ignores_stale_terminal_status_for_prior_worker() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        status_path = tmp / "status.json"
        tail = tmp / "tail.txt"
        tail.write_text("", encoding="utf-8")
        dispatch_id = "dispatch-reused-status"
        current_worker_pid = 222222
        status_path.write_text(
            json.dumps(
                {
                    "schema": "goalflight.status.v1",
                    "dispatch_id": dispatch_id,
                    "worker_pid": 111111,
                    "state": "complete",
                    "reason": "old-complete",
                    "updated_at": 1,
                }
            ),
            encoding="utf-8",
        )

        sys.path.insert(0, str(ROOT / "scripts"))
        import goalflight_dispatch as dispatch_mod  # noqa: E402

        old_sleep = dispatch_mod.time.sleep
        old_pid_alive = dispatch_mod.goalflight_compat.pid_alive
        sleeps = 0

        def fake_sleep(_secs: float) -> None:
            nonlocal sleeps
            sleeps += 1
            status_path.write_text(
                json.dumps(
                    {
                        "schema": "goalflight.status.v1",
                        "dispatch_id": dispatch_id,
                        "worker_pid": current_worker_pid,
                        "state": "complete",
                        "reason": "current-complete",
                        "updated_at": 2,
                    }
                ),
                encoding="utf-8",
            )

        try:
            dispatch_mod.time.sleep = fake_sleep
            dispatch_mod.goalflight_compat.pid_alive = lambda pid: pid == 999999
            rc, payload, reason = dispatch_mod._wait_for_detached_watcher(
                status_json=status_path,
                watcher_pid=999999,
                poll_secs=0.2,
                args=SimpleNamespace(dispatch_id=dispatch_id, agent="test-dispatch"),
                tail=tail,
                worker_pid=current_worker_pid,
                worker_identity=None,
                pgid=current_worker_pid,
                prompt_path=None,
            )
        finally:
            dispatch_mod.time.sleep = old_sleep
            dispatch_mod.goalflight_compat.pid_alive = old_pid_alive

        assert sleeps == 1
        assert rc == 0
        assert payload["worker_pid"] == current_worker_pid
        assert reason == "current-complete"


def case_post_spawn_registration_failure_still_runs_watcher() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        pidfile_path = tmp / "pidfile-dir-is-file"
        pidfile_path.write_text("not a directory", encoding="utf-8")
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(pidfile_path)
        dispatch_id = "dispatch-registration-failure"
        proc = _run_dispatch(
            tmp,
            env,
            dispatch_id,
            "import time; print('COMPLETE: registered enough', flush=True); time.sleep(2)",
        )
        assert proc.returncode == 0, f"dispatch rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        assert "DISPATCH-REGISTRATION-WARN " in proc.stderr, proc.stderr
        assert "write_pidfile" in proc.stderr, proc.stderr
        assert "DISPATCH-END " in proc.stdout, proc.stdout
        assert json.loads((tmp / f"{dispatch_id}.status.json").read_text())["state"] == "complete"
        _assert_terminal_record_and_lease(env, dispatch_id, "complete")


def case_dispatch_stats_window_and_legacy_records() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        runs = tmp / "state" / "runs.d"
        runs.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

        def iso(hours_ago: float) -> str:
            return (now - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")

        def write_record(dispatch_id: str, **updates) -> None:
            record = {
                "schema": "goalflight.dispatch.v1",
                "dispatch_id": dispatch_id,
                "agent": "alpha",
                "engine": "alpha",
                "shape": "bash",
                "transport": "dispatch",
                "state": "complete",
                "terminal_state": "complete",
                "started_at": iso(1.2),
                "ended_at": iso(1.0),
                "elapsed_s": 12.0,
            }
            record.update(updates)
            (runs / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")

        write_record("stats-ok", elapsed_s=10.0)
        write_record(
            "stats-dead",
            state="worker_dead",
            terminal_state="worker_dead",
            started_at=iso(2.2),
            ended_at=iso(2.0),
            elapsed_s=20.0,
            reason="worker exited",
        )
        write_record(
            "stats-running",
            state="running",
            terminal_state=None,
            started_at=iso(1.5),
            ended_at=None,
            elapsed_s=None,
        )
        write_record(
            "stats-legacy-blocked",
            agent="beta-acp",
            transport="acp",
            state="blocked",
            started_at=iso(3.2),
            ended_at=iso(3.0),
            reason="needs input",
        )
        legacy_path = runs / "stats-legacy-blocked.json"
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        for field in ("engine", "shape", "terminal_state", "elapsed_s"):
            legacy.pop(field, None)
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        write_record(
            "stats-old",
            started_at=(now - dt.timedelta(days=10, minutes=5)).isoformat(timespec="seconds"),
            ended_at=(now - dt.timedelta(days=10)).isoformat(timespec="seconds"),
            elapsed_s=30.0,
        )

        proc = subprocess.run(
            [sys.executable, str(DISPATCH), "--stats", "24h", "--json"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.returncode == 0, f"stats rc={proc.returncode} stdout={proc.stdout} stderr={proc.stderr}"
        payload = json.loads(proc.stdout)
        assert payload["records_considered"] == 4, payload
        assert payload["by_engine"]["alpha"]["total"] == 3, payload
        assert payload["by_engine"]["alpha"]["outcomes"] == 2, payload
        assert payload["by_engine"]["alpha"]["in_flight"] == 1, payload
        assert payload["by_engine"]["alpha"]["success_rate"] == 0.5, payload
        assert payload["by_engine"]["alpha"]["failure_modes"] == {"worker_dead": 1}, payload
        assert payload["by_engine"]["beta"]["failure_modes"] == {"blocked": 1}, payload
        assert payload["by_shape"]["bash"]["total"] == 3, payload
        assert payload["by_shape"]["bash"]["outcomes"] == 2, payload
        assert payload["by_shape"]["bash"]["in_flight"] == 1, payload
        assert payload["by_shape"]["acp"]["failure_modes"] == {"blocked": 1}, payload
        assert payload["by_shape"]["bash"]["mean_elapsed_s"] == 15.0, payload
        assert payload["by_shape"]["bash"]["p95_elapsed_s"] == 20.0, payload
        assert payload["by_engine"]["alpha"]["recent_failures"][0]["dispatch_id"] == "stats-dead", payload

        default_proc = subprocess.run(
            [sys.executable, str(DISPATCH), "--stats", "--json"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert default_proc.returncode == 0, default_proc.stderr
        assert json.loads(default_proc.stdout)["records_considered"] == 4, default_proc.stdout

        thirty_proc = subprocess.run(
            [sys.executable, str(DISPATCH), "--stats", "30", "--json"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert thirty_proc.returncode == 0, thirty_proc.stderr
        assert json.loads(thirty_proc.stdout)["records_considered"] == 5, thirty_proc.stdout

        table_proc = subprocess.run(
            [sys.executable, str(DISPATCH), "--stats", "7d"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert table_proc.returncode == 0, table_proc.stderr
        assert "by engine:" in table_proc.stdout and "by shape:" in table_proc.stdout, table_proc.stdout

        malformed = subprocess.run(
            [sys.executable, str(DISPATCH), "--stats", "nope"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert malformed.returncode == 64, malformed
        assert "malformed window" in malformed.stderr, malformed.stderr

        no_worker = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--prompt",
                "COMPLETE: do not materialize",
                "--stats",
                "24h",
                "--json",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert no_worker.returncode == 0, no_worker.stderr
        assert not (tmp / "state" / "dispatch").exists(), "stats materialized dispatch side effects"


def main() -> None:
    case_status_sees_dispatch_and_lease_releases()
    case_live_controller_pidfile_preserves_blocked_worker_for_reattach()
    case_from_queue_detached_launch_stamps_pidfile()
    case_from_queue_detached_launch_reparents_lease_and_survives_release_stale()
    case_capacity_block_does_not_spawn()
    case_capacity_wait_queues_until_slot_frees()
    case_capacity_wait_interrupt_writes_terminal_status()
    case_require_prompt_before_side_effects()
    case_account_guard_before_prompt_materialization()
    case_codex_routed_subscription_strips_openai_api_key()
    case_state_dir_auto_paths()
    case_dispatch_end_worker_still_alive_flags()
    case_dispatch_id_collision_suffix()
    case_worker_dead_state_releases_and_classifies_terminal()
    case_idle_timeout_state_releases_and_classifies_terminal()
    case_watcher_failure_releases_as_failed()
    case_nonzero_watcher_running_status_finalizes_failed()
    case_wait_ignores_stale_terminal_status_for_prior_worker()
    case_post_spawn_registration_failure_still_runs_watcher()
    case_dispatch_stats_window_and_legacy_records()
    print("OK: dispatch capacity/ledger tests pass")


if __name__ == "__main__":
    main()
