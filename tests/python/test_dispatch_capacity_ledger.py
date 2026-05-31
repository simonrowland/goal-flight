#!/usr/bin/env python3
"""Regression tests for goalflight_dispatch.py capacity + identity registration."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
STATUS = ROOT / "scripts" / "goalflight_status.py"


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
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
    timeout_s: float = 15.0,
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


def _assert_terminal_record_and_lease(env: dict[str, str], dispatch_id: str, state: str) -> None:
    payload = _status(env)
    row = _record(payload, dispatch_id)
    assert row and row.get("state") == state, row
    assert row.get("classification") == state, row
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
            "time.sleep(3); "
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
                    and any(lease.get("state") == "active" for lease in _leases(payload, dispatch_id))
                    else None
                )
            )
            row = _record(running, dispatch_id)
            active = [lease for lease in _leases(running, dispatch_id) if lease.get("state") == "active"]
            assert row and row["worker_pid"], row
            assert active and active[0].get("worker_pid") == row["worker_pid"], active

            stdout, stderr = proc.communicate(timeout=15)
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
                timeout=10,
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
            timeout=10,
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
            timeout_s=10,
        )
        alive_end = _dispatch_end(alive_proc.stdout)
        try:
            assert alive_proc.returncode == 0, alive_proc
            assert alive_end.get("worker_still_alive") is True, alive_end
        finally:
            _kill_if_alive(alive_end.get("worker_pid"))

        dead_proc = _run_dispatch(
            tmp,
            env,
            "worker-still-alive-false",
            "print('COMPLETE: exits', flush=True)",
            poll_secs="0.2",
            max_idle_secs="10",
            timeout_s=10,
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
            stdout, stderr = proc.communicate(timeout=10)
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
                timeout_s=10,
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
                timeout_s=10,
            )
            worker_pid = _worker_pid_from_stdout(proc.stdout)
            assert proc.returncode == 1, f"dispatch rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
            assert '"watcher_exit": 1' in proc.stdout, proc.stdout
            _assert_terminal_record_and_lease(env, dispatch_id, "failed")
        finally:
            _kill_if_alive(worker_pid)


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


def main() -> None:
    case_status_sees_dispatch_and_lease_releases()
    case_live_controller_pidfile_preserves_blocked_worker_for_reattach()
    case_capacity_block_does_not_spawn()
    case_require_prompt_before_side_effects()
    case_account_guard_before_prompt_materialization()
    case_codex_routed_subscription_strips_openai_api_key()
    case_state_dir_auto_paths()
    case_dispatch_end_worker_still_alive_flags()
    case_dispatch_id_collision_suffix()
    case_worker_dead_state_releases_and_classifies_terminal()
    case_idle_timeout_state_releases_and_classifies_terminal()
    case_watcher_failure_releases_as_failed()
    case_post_spawn_registration_failure_still_runs_watcher()
    print("OK: dispatch capacity/ledger tests pass")


if __name__ == "__main__":
    main()
