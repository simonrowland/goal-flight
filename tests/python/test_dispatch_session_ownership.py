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


def _args(
    dispatch_id: str,
    *,
    shape: str = "bash",
    controller_label: str | None = None,
    controller_beacon_pid: int | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        dispatch_id=dispatch_id,
        agent="codex",
        shape=shape,
        account=None,
        read_only=False,
        os_sandbox=None,
        controller_pid=os.getpid(),  # legacy flag must not become ownership
        controller_session_id=None,
        controller_label=controller_label,
        controller_beacon_pid=controller_beacon_pid,
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
        billing="subscription",
        capacity_wait_s=0,
        max_idle_secs=5.0,
        poll_secs=0.1,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        interactive=False,
        fast=False,
        web_research_ok=False,
        web_qa=False,
        ignore_git_warn=False,
        context_mode=None,
    )


def _ledger_args(
    dispatch_id: str,
    *,
    controller_session_id: str | None,
    controller_pid: int | None,
    state: str,
    controller_label: str | None = None,
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
        controller_label=controller_label,
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
    env.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
    env.pop("GOALFLIGHT_CONTROLLER_PID", None)
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
    controller_label: str | None = None,
    controller_beacon_pid: int | None = None,
) -> tuple[subprocess.Popen[str], Path, dict[str, str]]:
    status_path = base / f"{dispatch_id}.status.json"
    env = _dispatch_env(base)
    if controller_label is not None:
        env["GOALFLIGHT_CONTROLLER_LABEL"] = controller_label
    if controller_beacon_pid is not None:
        env["GOALFLIGHT_CONTROLLER_PID"] = str(controller_beacon_pid)
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
            controller_label = "battery-main"
            startup = sessions.claim_controller_startup(
                root,
                pid=beacon.pid,
                environ={"GOALFLIGHT_CONTROLLER_LABEL": controller_label},
            )
            assert startup.get("claimed") is True, startup
            claimed = startup["session"]
            args = _args(
                "owned",
                controller_label=controller_label,
                controller_beacon_pid=beacon.pid,
            )
            dispatch._stamp_controller_session(args, root)
            expected = (claimed["id"], beacon.pid)
            assert (
                dispatch._controller_session_id(args),
                dispatch._controller_pid(args),
            ) == expected
            assert dispatch._controller_label(args) == controller_label

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
                    assert queued.get("controller_label") == controller_label

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
                    assert bash.get("controller_label") == controller_label

                    acp_args = _args(
                        "owned-acp",
                        shape="acp",
                        controller_label=controller_label,
                        controller_beacon_pid=beacon.pid,
                    )
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
                    assert acp.get("controller_label") == controller_label

                    cfg = dispatch._build_acp_cfg(
                        acp_args,
                        status_json=base / "owned-acp.cfg.status.json",
                        base=base / "dispatch",
                    )
                    assert (cfg.controller_session_id, cfg.controller_pid) == expected
                    assert cfg.controller_label == controller_label

            status_meta = dispatch._prelaunch_status_metadata(args)
            assert (
                status_meta.get("controller_session_id"),
                status_meta.get("controller_pid"),
            ) == expected
            assert status_meta.get("controller_label") == controller_label

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
            assert pid_record.get("controller_label") == controller_label

            launched, launched_status, env = _launch(
                base,
                root,
                "owned-live-launch",
                "import time; print('worker-start', flush=True); time.sleep(60)",
                max_idle_secs=2.0,
                controller_label=controller_label,
                controller_beacon_pid=beacon.pid,
            )
            running = _wait_for_status(
                launched_status,
                lambda payload: payload.get("state") == "running",
            )
            assert (
                running.get("controller_session_id"),
                running.get("controller_pid"),
            ) == expected
            assert running.get("controller_label") == controller_label
            launched_record = _record_from_env(env, "owned-live-launch")
            assert (
                launched_record.get("controller_session_id"),
                launched_record.get("controller_pid"),
            ) == expected
            assert launched_record.get("controller_label") == controller_label
            launch_pidfiles = list((base / "pids").glob("*.jsonl"))
            assert len(launch_pidfiles) == 1, launch_pidfiles
            launch_pid_record = json.loads(
                launch_pidfiles[0].read_text(encoding="utf-8")
            )
            assert (
                launch_pid_record.get("controller_session_id"),
                launch_pid_record.get("controller_pid"),
            ) == expected
            assert launch_pid_record.get("controller_label") == controller_label

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
            assert orphaned.get("controller_label") == controller_label
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
        assert dispatch._controller_label(args) is None

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
        assert record.get("controller_label") is None
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
            assert payload.get("controller_label") is None, payload
            assert payload.get("controller_pid") != launched.pid, payload


def test_dead_beacon_dispatch_is_unowned() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        dead_beacon = _beacon()
        try:
            sessions.claim_session(
                root,
                pid=dead_beacon.pid,
                session_id="dead-controller-session",
                label="battery-dead",
            )
            dead_pid = dead_beacon.pid
        finally:
            dead_beacon.terminate()
            dead_beacon.wait(timeout=10)

        args = _args(
            "dead-beacon",
            controller_label="battery-dead",
            controller_beacon_pid=dead_pid,
        )
        dispatch._stamp_controller_session(args, root)
        assert dispatch._controller_session_id(args) is None
        assert dispatch._controller_pid(args) is None
        assert dispatch._controller_label(args) is None

        launched, status_path, env = _launch(
            base,
            root,
            "dead-beacon-launch",
            "print('COMPLETE: dead beacon launch', flush=True)",
            controller_label="battery-dead",
            controller_beacon_pid=dead_pid,
        )
        stdout, stderr = launched.communicate(timeout=10)
        assert launched.returncode == 0, (launched.returncode, stdout, stderr)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        record = _record_from_env(env, "dead-beacon-launch")
        for payload in (status, record):
            assert payload.get("controller_session_id") is None, payload
            assert payload.get("controller_pid") is None, payload
            assert payload.get("controller_label") is None, payload
            assert payload.get("controller_pid") != dead_pid, payload
            assert payload.get("controller_pid") != launched.pid, payload


def test_one_snapshot_cannot_mix_session_id_with_a_later_beacon() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        first = _beacon()
        second = None
        try:
            sessions.claim_session(
                root,
                pid=first.pid,
                session_id="first-session",
                label="battery-main",
            )
            args = _args(
                "stable-pair",
                controller_label="battery-main",
                controller_beacon_pid=first.pid,
            )
            dispatch._stamp_controller_session(args, root)
            first.terminate()
            first.wait(timeout=10)

            second = _beacon()
            sessions.claim_session(
                root,
                pid=second.pid,
                session_id="second-session",
                label="battery-bugs",
            )
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


def test_different_named_controllers_keep_their_own_dispatches() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        first, second = _beacon(), _beacon()
        try:
            first_claim = sessions.claim_session(
                root,
                pid=first.pid,
                session_id="battery-main-session",
                label="battery-main",
            )
            second_claim = sessions.claim_session(
                root,
                pid=second.pid,
                session_id="battery-bugs-session",
                label="battery-bugs",
            )
            first_args = _args(
                "battery-main-work",
                controller_label="battery-main",
                controller_beacon_pid=first.pid,
            )
            second_args = _args(
                "battery-bugs-work",
                controller_label="battery-bugs",
                controller_beacon_pid=second.pid,
            )
            dispatch._stamp_controller_session(first_args, root)
            dispatch._stamp_controller_session(second_args, root)

            assert (
                dispatch._controller_session_id(first_args),
                dispatch._controller_pid(first_args),
                dispatch._controller_label(first_args),
            ) == (first_claim["id"], first.pid, "battery-main")
            assert (
                dispatch._controller_session_id(second_args),
                dispatch._controller_pid(second_args),
                dispatch._controller_label(second_args),
            ) == (second_claim["id"], second.pid, "battery-bugs")

            with patch.dict(os.environ, {"GOALFLIGHT_STATE_DIR": str(base / "state")}):
                with _quiet_record_side_effects():
                    for args in (first_args, second_args):
                        dispatch._record_queued_ledger_fast(
                            args,
                            project_root=root,
                            prompt_path=None,
                            status_json=base / f"{args.dispatch_id}.status.json",
                            tail=base / f"{args.dispatch_id}.tail",
                        )
                first_record = _read_record("battery-main-work")
                second_record = _read_record("battery-bugs-work")
            assert first_record.get("controller_label") == "battery-main"
            assert first_record.get("controller_session_id") == first_claim["id"]
            assert second_record.get("controller_label") == "battery-bugs"
            assert second_record.get("controller_session_id") == second_claim["id"]
        finally:
            for proc in (first, second):
                proc.terminate()
                proc.wait(timeout=10)


def test_failed_startup_claim_keeps_dispatch_unowned() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        unrelated = _beacon()
        try:
            sessions.claim_session(
                root,
                pid=unrelated.pid,
                session_id="other-session",
                label="battery-main",
            )
            with patch.object(sessions, "claim_session", side_effect=OSError("read only")):
                startup = sessions.claim_controller_startup(
                    root,
                    pid=os.getpid(),
                    label="battery-main",
                )
            assert startup.get("claimed") is False, startup
            assert startup.get("reason") == "claim_failed", startup

            args = _args(
                "claim-failed",
                controller_label="battery-main",
                controller_beacon_pid=os.getpid(),
            )
            dispatch._stamp_controller_session(args, root)
            assert dispatch._controller_session_id(args) is None
            assert dispatch._controller_pid(args) is None
            assert dispatch._controller_label(args) is None

            with patch.dict(os.environ, {"GOALFLIGHT_STATE_DIR": str(base / "state")}):
                with _quiet_record_side_effects():
                    dispatch._record_queued_ledger_fast(
                        args,
                        project_root=root,
                        prompt_path=None,
                        status_json=base / "claim-failed.status.json",
                        tail=base / "claim-failed.tail",
                    )
                record = _read_record("claim-failed")
            assert record.get("controller_session_id") is None, record
            assert record.get("controller_pid") is None, record
            assert record.get("controller_label") is None, record

            launched, status_path, env = _launch(
                base,
                root,
                "claim-failed-launch",
                "print('COMPLETE: claim failure stayed nonfatal', flush=True)",
                controller_label="battery-main",
                controller_beacon_pid=os.getpid(),
            )
            stdout, stderr = launched.communicate(timeout=10)
            assert launched.returncode == 0, (launched.returncode, stdout, stderr)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            launched_record = _record_from_env(env, "claim-failed-launch")
            for payload in (status, launched_record):
                assert payload.get("controller_session_id") is None, payload
                assert payload.get("controller_pid") is None, payload
                assert payload.get("controller_label") is None, payload
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=10)


def test_duplicate_live_same_label_dispatch_is_honestly_unowned() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        first, second = _beacon(), _beacon()
        try:
            sessions.claim_session(root, pid=first.pid, label="battery-main")
            sessions.claim_session(root, pid=second.pid, label="battery-main")
            winner = sessions.live_session(root, label="battery-main")
            assert winner is not None and winner.get("conflicting_beacons") == 2
            args = _args(
                "ambiguous-owner",
                controller_label="battery-main",
                controller_beacon_pid=int(winner["pid"]),
            )
            dispatch._stamp_controller_session(args, root)
            assert dispatch._controller_session_id(args) is None
            assert dispatch._controller_pid(args) is None
            assert dispatch._controller_label(args) is None
        finally:
            for proc in (first, second):
                proc.terminate()
                proc.wait(timeout=10)


def test_queue_replay_carries_declaration_and_remeasures_live_beacon() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        beacon = _beacon()
        try:
            claimed = sessions.claim_session(
                root,
                pid=beacon.pid,
                session_id="queued-controller",
                label="battery-main",
            )
            args = _args(
                "queued-owner",
                controller_label="battery-main",
                controller_beacon_pid=beacon.pid,
            )
            dispatch._stamp_controller_session(args, root)
            replay_argv = dispatch._canonical_replay_argv(
                args,
                [sys.executable, "-c", "print('COMPLETE: replay')"],
                tail=base / "queued-owner.tail",
                status_json=base / "queued-owner.status.json",
            )
            label_index = replay_argv.index("--controller-label")
            pid_index = replay_argv.index("--controller-beacon-pid")
            assert replay_argv[label_index + 1] == "battery-main"
            assert int(replay_argv[pid_index + 1]) == beacon.pid

            replay_args = _args(
                "queued-owner",
                controller_label=replay_argv[label_index + 1],
                controller_beacon_pid=int(replay_argv[pid_index + 1]),
            )
            with patch.dict(os.environ, {}, clear=True):
                dispatch._stamp_controller_session(replay_args, root)
            assert (
                dispatch._controller_session_id(replay_args),
                dispatch._controller_pid(replay_args),
                dispatch._controller_label(replay_args),
            ) == (claimed["id"], beacon.pid, "battery-main")

            beacon.terminate()
            beacon.wait(timeout=10)
            dead_replay_args = _args(
                "queued-owner-dead",
                controller_label="battery-main",
                controller_beacon_pid=beacon.pid,
            )
            dispatch._stamp_controller_session(dead_replay_args, root)
            assert dispatch._controller_session_id(dead_replay_args) is None
            assert dispatch._controller_pid(dead_replay_args) is None
            assert dispatch._controller_label(dead_replay_args) is None
        finally:
            if beacon.poll() is None:
                beacon.terminate()
                beacon.wait(timeout=10)


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
        test_different_named_controllers_keep_their_own_dispatches,
        test_failed_startup_claim_keeps_dispatch_unowned,
        test_duplicate_live_same_label_dispatch_is_honestly_unowned,
        test_queue_replay_carries_declaration_and_remeasures_live_beacon,
        test_cmd_record_merge_preserves_owned_pair_in_only_the_null_direction,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_dispatch_session_ownership.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
