#!/usr/bin/env python3
"""Regression tests for drain --remote-node."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("remote drain fixtures use POSIX /tmp paths")

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
import goalflight_fleet as fleet  # noqa: E402
import goalflight_fleet_billing as billing  # noqa: E402

BASE_SHA = "0123456789abcdef0123456789abcdef01234567"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def green_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 0, "logged_in: true\n", ""


@contextlib.contextmanager
def isolated_env(tmp: Path, fleet_dir: Path):
    old = os.environ.copy()
    os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    os.environ["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    os.environ["GOALFLIGHT_FLEET_DIR"] = str(fleet_dir)
    os.environ.pop("GOALFLIGHT_LIVE_SSH", None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def _fixture_fleet(fleet_dir: Path) -> None:
    fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    fleet_doc["nodes"] = {
        "localhost": {
            "node_id": "localhost",
            "status": "active",
            "ssh": {"alias": "localhost", "hostname": "localhost"},
            "repo_root": str(ROOT),
            "state_dir": "/tmp/goal-flight-remote-drain-test",
            "billing_accounts": [],
            "added_at": "2026-06-21T12:00:00+00:00",
        }
    }
    fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)
    billing.link_account_to_node(fleet_dir, "openai/default", "localhost", runner=green_runner)


def _extract_wrapped_flag(argv: list[str], flag: str, default: str) -> str:
    joined = " ".join(argv)
    match = re.search(rf"{re.escape(flag)}\s+'?([^'\s]+)'?", joined)
    if match:
        return match.group(1)
    for idx, part in enumerate(argv):
        if part == flag and idx + 1 < len(argv):
            return argv[idx + 1]
    return default


def _receipt_runner(captured: list[list[str]]):
    def run(argv: list[str]) -> tuple[int, str, str]:
        captured.append(list(argv))
        if "goalflight_fleet_launch_detached.py" not in " ".join(argv):
            return 0, "{}", ""
        dispatch_id = _extract_wrapped_flag(argv, "--dispatch-id", "remote-drain")
        node_id = _extract_wrapped_flag(argv, "--node-id", "localhost")
        status_json = _extract_wrapped_flag(
            argv,
            "--status-json",
            f"/tmp/goal-flight-remote-drain-test/dispatches/{dispatch_id}/status.json",
        )
        base_sha = _extract_wrapped_flag(argv, "--base-sha", BASE_SHA)
        return 0, json.dumps(
            {
                "schema": "goalflight.fleet.launch_receipt.v1",
                "dispatch_id": dispatch_id,
                "node_id": node_id,
                "remote_pid": 4242,
                "remote_lstart": "Thu Jun 21 12:00:00 2026",
                "remote_identity": {
                    "pid": 4242,
                    "lstart": "Thu Jun 21 12:00:00 2026",
                    "comm": "python3",
                },
                "remote_status_path": status_json,
                "remote_state_dir": "/tmp/goal-flight-remote-drain-test",
                "launcher_log_path": f"/tmp/goal-flight-remote-drain-test/dispatches/{dispatch_id}/dispatcher.log",
                "started_at": "2026-06-21T12:00:00+00:00",
                "worktree_base_sha": base_sha,
            },
            sort_keys=True,
        ), ""

    return run


def _drain_args(queue: Path, fleet_dir: Path, *, remote_runner=None) -> argparse.Namespace:
    return argparse.Namespace(
        queue_dir=str(queue),
        capacity_wait_s=0.0,
        claim_stale_s=D.QUEUE_CLAIM_STALE_S,
        limit=0,
        remote_node="localhost",
        fleet_dir=str(fleet_dir),
        remote_runner=remote_runner,
    )


def _write_remote_queue_entry(queue: Path, dispatch_id: str) -> Path:
    prompt = queue.parent / f"{dispatch_id}.prompt.md"
    prompt.write_text("COMPLETE: remote drain test\n", encoding="utf-8")
    path = queue / f"{dispatch_id}.json"
    request = {
        "agent": "codex",
        "priority": "normal",
        "cwd": str(ROOT),
        "prompt_file": str(prompt),
        "tail": str(queue.parent / f"{dispatch_id}.tail"),
        "status_json": str(queue.parent / f"{dispatch_id}.status.json"),
        "base_sha": BASE_SHA,
    }
    D._write_json_atomic(
        path,
        {
            "schema": D.DISPATCH_QUEUE_SCHEMA,
            "state": "queued",
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "shape": "bash",
            "project_root": str(ROOT),
            "process_cwd": str(ROOT),
            "created_at": "2026-06-21T12:00:00+00:00",
            "updated_at": "2026-06-21T12:00:00+00:00",
            "queue_path": str(path),
            "base_sha": BASE_SHA,
            "dispatch_argv": [
                "--agent",
                "codex",
                "--dispatch-id",
                dispatch_id,
                "--prompt-file",
                str(prompt),
                "--cwd",
                str(ROOT),
            ],
            "request": request,
        },
    )
    return path


def _write_local_queue_entry(queue: Path, dispatch_id: str) -> Path:
    path = queue / f"{dispatch_id}.json"
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
            "created_at": "2026-06-21T12:00:00+00:00",
            "updated_at": "2026-06-21T12:00:00+00:00",
            "queue_path": str(path),
            "dispatch_argv": [
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                dispatch_id,
                "--tail",
                str(queue.parent / f"{dispatch_id}.tail"),
                "--status-json",
                str(queue.parent / f"{dispatch_id}.status.json"),
                "--cwd",
                str(ROOT),
                "--",
                sys.executable,
                "-c",
                "print('COMPLETE: local drain test')",
            ],
            "request": {
                "agent": "test-dispatch",
                "priority": "normal",
                "cwd": str(ROOT),
                "tail": str(queue.parent / f"{dispatch_id}.tail"),
                "status_json": str(queue.parent / f"{dispatch_id}.status.json"),
            },
        },
    )
    return path


def test_remote_drain_routes_claimed_entry_through_fleet_launch() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fleet_dir = tmp / "fleet"
        _fixture_fleet(fleet_dir)
        queue = tmp / "state" / "dispatch-queue"
        queue.mkdir(parents=True)
        _write_remote_queue_entry(queue, "remote-drain-route")
        captured: list[list[str]] = []

        old_run = D.subprocess.run

        def fail_local_dispatch(argv, **kwargs):
            if any("goalflight_dispatch.py" in str(part) for part in list(argv)):
                raise AssertionError("local dispatch replay must not run for --remote-node")
            return old_run(argv, **kwargs)

        with isolated_env(tmp, fleet_dir):
            D.subprocess.run = fail_local_dispatch
            try:
                payload = D._drain_queue_once(_drain_args(queue, fleet_dir, remote_runner=_receipt_runner(captured)))
            finally:
                D.subprocess.run = old_run

        assert_true("launched", payload["launched"] == 1)
        assert_true("queue empty", not list(queue.glob("*.json*")))
        assert_true(
            "fleet launch called",
            any("goalflight_fleet_launch_detached.py" in " ".join(argv) for argv in captured),
        )
        record = json.loads((tmp / "state" / "runs.d" / "remote-drain-route.json").read_text())
        assert_true("fleet transport", record.get("transport") == "fleet-ssh")
        assert_true("queue token on ledger", isinstance(record.get("queue_launch_token"), str))
        assert_true("remote receipt", record.get("remote_launch_receipt", {}).get("remote_pid") == 4242)
        meta = json.loads((fleet_dir / "register" / "dispatches" / "remote-drain-route" / "meta.json").read_text())
        assert_true("queue token on fleet meta", meta.get("queue_launch_token") == record.get("queue_launch_token"))


def test_remote_drain_unknown_node_is_clean_blocked_and_keeps_queue() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fleet_dir = tmp / "fleet"
        _fixture_fleet(fleet_dir)
        queue = tmp / "state" / "dispatch-queue"
        queue.mkdir(parents=True)
        queued = _write_remote_queue_entry(queue, "remote-drain-unknown")
        stdout = io.StringIO()
        with isolated_env(tmp, fleet_dir), redirect_stdout(stdout):
            code = D._cmd_drain([
                "--queue-dir",
                str(queue),
                "--remote-node",
                "missing-node",
                "--json",
            ])
        payload = json.loads(stdout.getvalue())
        assert_true("blocked rc", code == 2)
        assert_true("blocked payload", payload.get("blocked") is True)
        assert_true("dispatch blocked", "DISPATCH-BLOCKED" in payload.get("error", ""))
        assert_true("unknown code", payload.get("code") == "unknown_node")
        assert_true("queue kept", queued.exists())
        assert_true("no claim", not list(queue.glob("*.claimed-*")))


def test_remote_drain_claim_token_prevents_second_launch() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fleet_dir = tmp / "fleet"
        _fixture_fleet(fleet_dir)
        queue = tmp / "state" / "dispatch-queue"
        queue.mkdir(parents=True)
        _write_remote_queue_entry(queue, "remote-drain-once")
        captured: list[list[str]] = []
        args = _drain_args(queue, fleet_dir, remote_runner=_receipt_runner(captured))
        with isolated_env(tmp, fleet_dir):
            first = D._drain_queue_once(args)
            second = D._drain_queue_once(args)
        launch_calls = [argv for argv in captured if "goalflight_fleet_launch_detached.py" in " ".join(argv)]
        assert_true("first launched", first["launched"] == 1)
        assert_true("second no-op", second["launched"] == 0 and second["remaining"] == 0)
        assert_true("one remote launch", len(launch_calls) == 1)


def test_local_drain_without_remote_node_uses_existing_local_replay() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fleet_dir = tmp / "fleet"
        _fixture_fleet(fleet_dir)
        queue = tmp / "state" / "dispatch-queue"
        queue.mkdir(parents=True)
        _write_local_queue_entry(queue, "local-drain-still-local")
        captured: list[list[str]] = []
        old_run = D.subprocess.run
        old_remote = D._drain_launch_remote_claim

        def fake_run(argv, **_kwargs):
            argv = list(argv)
            if "--dispatch-id" not in argv:
                # Nested real subprocess (e.g. process_identity's `ps`) re-enters this
                # patched run; pass it through instead of parsing it as a launch argv.
                return old_run(argv, **_kwargs)
            captured.append(argv)
            dispatch_id = argv[argv.index("--dispatch-id") + 1]
            queue_launch_token = argv[argv.index("--queue-launch-token") + 1]
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
                    "stdout_path": str(tmp / "local.tail"),
                    "status_path": str(tmp / "local.status.json"),
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

        def fail_remote(*_args, **_kwargs):
            raise AssertionError("remote drain helper must not run without --remote-node")

        args = argparse.Namespace(
            queue_dir=str(queue),
            capacity_wait_s=0.0,
            claim_stale_s=D.QUEUE_CLAIM_STALE_S,
            limit=0,
        )
        with isolated_env(tmp, fleet_dir):
            D.subprocess.run = fake_run
            D._drain_launch_remote_claim = fail_remote
            try:
                payload = D._drain_queue_once(args)
            finally:
                D.subprocess.run = old_run
                D._drain_launch_remote_claim = old_remote

        assert_true("local launched", payload["launched"] == 1)
        assert_true("local replay used", len(captured) == 1)
        assert_true("local dispatch script", any("goalflight_dispatch.py" in str(part) for part in captured[0]))
        assert_true("launch detached", "--launch-detached" in captured[0])


def main() -> None:
    test_remote_drain_routes_claimed_entry_through_fleet_launch()
    test_remote_drain_unknown_node_is_clean_blocked_and_keeps_queue()
    test_remote_drain_claim_token_prevents_second_launch()
    test_local_drain_without_remote_node_uses_existing_local_replay()
    print("OK: remote drain tests pass")


if __name__ == "__main__":
    main()
