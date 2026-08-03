#!/usr/bin/env python3
"""Dispatch ownership comes only from one live controller-beacon snapshot."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from support import skip_posix_on_native_windows

skip_posix_on_native_windows(
    "dispatch ownership integration launches POSIX process groups and ACP wrappers"
)

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402


def _beacon() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    process.wait(timeout=10)
    return process.pid


def _args(dispatch_id: str, *, shape: str = "bash") -> argparse.Namespace:
    return argparse.Namespace(
        dispatch_id=dispatch_id,
        agent="codex",
        shape=shape,
        account=None,
        read_only=False,
        os_sandbox=None,
        controller_pid=os.getpid(),  # legacy flag must not become ownership
        controller_session_id=None,
        _controller_beacon_pid=None,
        task_ids=[],
        launch_detached=False,
        queue_launch_token=None,
        codex_session_id=None,
        codex_resume_home=None,
        codex_home_owner_dispatch_id=None,
        parent_dispatch_id=None,
        cwd=None,
        prompt="ownership test",
        prompt_file=None,
        no_orientation=True,
        model=None,
        priority="normal",
        capacity_wait_s=0,
        max_idle_secs=5.0,
        poll_secs=0.1,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        interactive=False,
        context_mode=None,
    )


def _ledger_args(
    dispatch_id: str,
    *,
    controller_session_id: str | None,
    controller_pid: int | None,
    state: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        dispatch_id=dispatch_id,
        prompt_id=None,
        prompt_path=None,
        task_ids=[],
        agent="codex",
        engine="codex",
        shape="bash",
        account="default",
        effective_account=None,
        transport="dispatch",
        project_root=str(ROOT),
        controller_pid=controller_pid,
        controller_session_id=controller_session_id,
        claimant_pid=None,
        worker_pid=None,
        acp_session_id=None,
        logical_session_id=dispatch_id,
        lease_id=None,
        stdout_path=None,
        stderr_path=None,
        status_path=None,
        os_sandbox_json=None,
        queue_launch_token=None,
        detached=False,
        state=state,
        json=True,
    )


def _read_record(dispatch_id: str) -> dict:
    return json.loads(ledger.record_path(dispatch_id).read_text(encoding="utf-8"))


@contextlib.contextmanager
def _quiet_record_side_effects():
    with patch.object(dispatch, "_export_dashboard_status_for_project"), patch.object(
        dispatch, "_upsert_project_registry_for_dispatch"
    ), patch.object(dispatch, "_start_dashboard_refresh_for_project"):
        yield


def _dispatch_env(base: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("GOALFLIGHT_STEER_FILE", None)
    env.pop("GOALFLIGHT_PROMPT_FILE", None)
    env["GOALFLIGHT_STATE_DIR"] = str(base / "state")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(base / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = os.devnull
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    return env


def _wait_for_status(path: Path, predicate, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            last = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            last = {}
        if predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"status condition timed out: {last!r}")


def _launch(
    base: Path,
    root: Path,
    dispatch_id: str,
    worker_code: str,
    *,
    max_idle_secs: float = 5.0,
) -> tuple[subprocess.Popen[str], Path, dict[str, str]]:
    status_path = base / f"{dispatch_id}.status.json"
    env = _dispatch_env(base)
    process = subprocess.Popen(
        [
            sys.executable,
            str(DISPATCH),
            "--agent",
            "test-dispatch",
            "--dispatch-id",
            dispatch_id,
            "--cwd",
            str(root),
            "--tail",
            str(base / f"{dispatch_id}.tail"),
            "--status-json",
            str(status_path),
            "--poll-secs",
            "0.1",
            "--max-idle-secs",
            str(max_idle_secs),
            "--controller-pid",
            str(os.getpid()),
            "--foreground",
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
    return process, status_path, env


def _record_from_env(env: dict[str, str], dispatch_id: str) -> dict:
    with patch.dict(os.environ, env, clear=False):
        return _read_record(dispatch_id)


def _kill_worker(status_path: Path) -> None:
    try:
        worker_pid = int(
            json.loads(status_path.read_text(encoding="utf-8")).get("worker_pid") or 0
        )
    except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError):
        return
    if not worker_pid:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(worker_pid, signal.SIGKILL)


def test_live_beacon_pair_reaches_queue_bash_acp_and_status_records() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        beacon = _beacon()
        launched: subprocess.Popen[str] | None = None
        launched_status: Path | None = None
        try:
            claimed = sessions.claim_session(
                root, pid=beacon.pid, session_id="controller-session-one"
            )
            args = _args("owned")
            dispatch._stamp_controller_session(args, root)
            expected = (claimed["id"], beacon.pid)
            assert (
                dispatch._controller_session_id(args),
                dispatch._controller_pid(args),
            ) == expected

            with patch.dict(os.environ, {"GOALFLIGHT_STATE_DIR": str(base / "state")}):
                with _quiet_record_side_effects():
                    dispatch._record_queued_ledger_fast(
                        args,
                        project_root=root,
                        prompt_path=None,
                        status_json=base / "owned.status.json",
                        tail=base / "owned.tail",
                    )
                    queued = _read_record("owned")
                    assert (queued.get("controller_session_id"), queued.get("controller_pid")) == expected

                    dispatch._record_ledger(
                        args,
                        project_root=root,
                        prompt_path=None,
                        status_json=base / "owned.status.json",
                        tail=base / "owned.tail",
                        lease_id=None,
                        worker_pid=None,
                        state="starting",
                    )
                    bash = _read_record("owned")
                    assert (bash.get("controller_session_id"), bash.get("controller_pid")) == expected

                    acp_args = _args("owned-acp", shape="acp")
                    dispatch._stamp_controller_session(acp_args, root)
                    dispatch._record_test_acp_running_fast(
                        acp_args,
                        project_root=root,
                        prompt_path=None,
                        status_json=base / "owned-acp.status.json",
                        tail=base / "owned-acp.tail",
                        worker_pid=os.getpid(),
                    )
                    acp = _read_record("owned-acp")
                    assert (acp.get("controller_session_id"), acp.get("controller_pid")) == expected

                    cfg = dispatch._build_acp_cfg(
                        acp_args,
                        status_json=base / "owned-acp.cfg.status.json",
                        base=base / "dispatch",
                    )
                    assert (cfg.controller_session_id, cfg.controller_pid) == expected

            status_meta = dispatch._prelaunch_status_metadata(args)
            assert (
                status_meta.get("controller_session_id"),
                status_meta.get("controller_pid"),
            ) == expected

            identity = ledger.process_identity(os.getpid())
            assert identity, "test process identity unavailable"
            with patch.dict(
                os.environ,
                {"GOAL_FLIGHT_PIDFILE_DIR": str(base / "unit-pids")},
            ):
                pidfile = dispatch._write_pidfile(
                    args,
                    worker_pid=os.getpid(),
                    pgid=os.getpgrp(),
                    identity=identity,
                )
            assert pidfile is not None
            pid_record = json.loads(pidfile.read_text(encoding="utf-8"))
            assert pidfile.name.startswith(f"{beacon.pid}."), pidfile
            assert (
                pid_record.get("controller_session_id"),
                pid_record.get("controller_pid"),
            ) == expected

            launched, launched_status, env = _launch(
                base,
                root,
                "owned-live-launch",
                "import time; print('worker-start', flush=True); time.sleep(60)",
                max_idle_secs=2.0,
            )
            running = _wait_for_status(
                launched_status,
                lambda payload: payload.get("state") == "running",
            )
            assert (
                running.get("controller_session_id"),
                running.get("controller_pid"),
            ) == expected
            launched_record = _record_from_env(env, "owned-live-launch")
            assert (
                launched_record.get("controller_session_id"),
                launched_record.get("controller_pid"),
            ) == expected
            launch_pidfiles = list((base / "pids").glob("*.jsonl"))
            assert len(launch_pidfiles) == 1, launch_pidfiles
            launch_pid_record = json.loads(
                launch_pidfiles[0].read_text(encoding="utf-8")
            )
            assert (
                launch_pid_record.get("controller_session_id"),
                launch_pid_record.get("controller_pid"),
            ) == expected

            beacon.terminate()
            beacon.wait(timeout=10)
            stdout, stderr = launched.communicate(timeout=10)
            assert launched.returncode == 3, (launched.returncode, stdout, stderr)
            orphaned = json.loads(launched_status.read_text(encoding="utf-8"))
            assert orphaned.get("state") == "orphaned", orphaned
            assert (
                orphaned.get("controller_session_id"),
                orphaned.get("controller_pid"),
            ) == expected
        finally:
            if launched_status is not None:
                _kill_worker(launched_status)
            if launched is not None and launched.poll() is None:
                launched.terminate()
                launched.wait(timeout=10)
            if beacon.poll() is None:
                beacon.terminate()
                beacon.wait(timeout=10)


def test_unclaimed_dispatch_stays_unowned_instead_of_using_launcher_pid() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        args = _args("unowned")
        dispatch._stamp_controller_session(args, root)
        assert dispatch._controller_session_id(args) is None
        assert dispatch._controller_pid(args) is None

        with patch.dict(os.environ, {"GOALFLIGHT_STATE_DIR": str(base / "state")}):
            with _quiet_record_side_effects():
                dispatch._record_queued_ledger_fast(
                    args,
                    project_root=root,
                    prompt_path=None,
                    status_json=base / "unowned.status.json",
                    tail=base / "unowned.tail",
                )
            record = _read_record("unowned")
        assert record.get("controller_session_id") is None
        assert record.get("controller_pid") is None
        assert record.get("controller_pid") != os.getpid()

        launched, status_path, env = _launch(
            base,
            root,
            "unowned-launch",
            "print('COMPLETE: unowned launch', flush=True)",
        )
        stdout, stderr = launched.communicate(timeout=10)
        assert launched.returncode == 0, (launched.returncode, stdout, stderr)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        launched_record = _record_from_env(env, "unowned-launch")
        for payload in (status, launched_record):
            assert payload.get("controller_session_id") is None, payload
            assert payload.get("controller_pid") is None, payload
            assert payload.get("controller_pid") != launched.pid, payload


def test_dead_beacon_dispatch_is_unowned() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        dead_pid = _dead_pid()
        sessions.claim_session(
            root,
            pid=dead_pid,
            session_id="dead-controller-session",
        )

        args = _args("dead-beacon")
        dispatch._stamp_controller_session(args, root)
        assert dispatch._controller_session_id(args) is None
        assert dispatch._controller_pid(args) is None

        launched, status_path, env = _launch(
            base,
            root,
            "dead-beacon-launch",
            "print('COMPLETE: dead beacon launch', flush=True)",
        )
        stdout, stderr = launched.communicate(timeout=10)
        assert launched.returncode == 0, (launched.returncode, stdout, stderr)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        record = _record_from_env(env, "dead-beacon-launch")
        for payload in (status, record):
            assert payload.get("controller_session_id") is None, payload
            assert payload.get("controller_pid") is None, payload
            assert payload.get("controller_pid") != dead_pid, payload
            assert payload.get("controller_pid") != launched.pid, payload


def test_one_snapshot_cannot_mix_session_id_with_a_later_beacon() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        first = _beacon()
        second = None
        try:
            sessions.claim_session(root, pid=first.pid, session_id="first-session")
            args = _args("stable-pair")
            dispatch._stamp_controller_session(args, root)
            first.terminate()
            first.wait(timeout=10)

            second = _beacon()
            sessions.claim_session(root, pid=second.pid, session_id="second-session")
            assert (
                dispatch._controller_session_id(args),
                dispatch._controller_pid(args),
            ) == ("first-session", first.pid)
        finally:
            if first.poll() is None:
                first.terminate()
                first.wait(timeout=10)
            if second is not None:
                second.terminate()
                second.wait(timeout=10)


def test_cmd_record_merge_preserves_owned_pair_in_only_the_null_direction() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        owner_pid = os.getpid()
        with patch.dict(
            os.environ,
            {"GOALFLIGHT_STATE_DIR": str(state_dir)},
        ), contextlib.redirect_stdout(io.StringIO()):
            ledger.cmd_record(
                _ledger_args(
                    "owned-then-null",
                    controller_session_id="established-owner",
                    controller_pid=owner_pid,
                    state="starting",
                )
            )
            ledger.cmd_record(
                _ledger_args(
                    "owned-then-null",
                    controller_session_id=None,
                    controller_pid=None,
                    state="running",
                )
            )
            preserved = _read_record("owned-then-null")
            assert (
                preserved.get("controller_session_id"),
                preserved.get("controller_pid"),
            ) == ("established-owner", owner_pid)
            assert preserved.get("controller_identity"), preserved

            ledger.cmd_record(
                _ledger_args(
                    "null-then-owned",
                    controller_session_id=None,
                    controller_pid=None,
                    state="starting",
                )
            )
            ledger.cmd_record(
                _ledger_args(
                    "null-then-owned",
                    controller_session_id="later-owner",
                    controller_pid=owner_pid,
                    state="running",
                )
            )
            established = _read_record("null-then-owned")
            assert (
                established.get("controller_session_id"),
                established.get("controller_pid"),
            ) == ("later-owner", owner_pid)
            assert established.get("controller_identity"), established


def main() -> None:
    tests = [
        test_live_beacon_pair_reaches_queue_bash_acp_and_status_records,
        test_unclaimed_dispatch_stays_unowned_instead_of_using_launcher_pid,
        test_dead_beacon_dispatch_is_unowned,
        test_one_snapshot_cannot_mix_session_id_with_a_later_beacon,
        test_cmd_record_merge_preserves_owned_pair_in_only_the_null_direction,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_dispatch_session_ownership.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
