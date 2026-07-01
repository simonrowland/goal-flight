#!/usr/bin/env python3
"""Hermetic tests for the fleet detached-launch helper."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet_launch_detached as fleet_launch
import goalflight_liveness

BASE_SHA = "0123456789abcdef0123456789abcdef01234567"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


class FakeProc:
    def __init__(self, *, pid: int = 4242, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


@contextmanager
def patched_spawn(returncode: int | None = None):
    calls: list[dict[str, Any]] = []
    old_popen = fleet_launch.subprocess.Popen
    old_identity = fleet_launch._process_identity_after_spawn
    old_which = fleet_launch.shutil.which

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
        if argv and Path(str(argv[0])).name == "ps":
            return old_popen(argv, **kwargs)
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return FakeProc(returncode=returncode)

    fleet_launch.subprocess.Popen = fake_popen
    fleet_launch._process_identity_after_spawn = lambda pid: {
        "pid": pid,
        "lstart": "Thu Jun 11 12:00:00 2026",
        "comm": "python3",
    }
    fleet_launch.shutil.which = lambda _name: None
    try:
        yield calls
    finally:
        fleet_launch.subprocess.Popen = old_popen
        fleet_launch._process_identity_after_spawn = old_identity
        fleet_launch.shutil.which = old_which


@contextmanager
def patched_process_identity(fn):
    old_identity = fleet_launch._process_identity
    fleet_launch._process_identity = fn
    try:
        yield
    finally:
        fleet_launch._process_identity = old_identity


def _prompt_b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _args(state_dir: Path, dispatch_id: str, prompt_text: str, *, recover: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        repo_root=str(ROOT),
        state_dir=str(state_dir),
        dispatch_id=dispatch_id,
        node_id="localhost",
        agent="codex-acp",
        prompt_b64=_prompt_b64(prompt_text),
        cwd=str(ROOT),
        status_json=str(state_dir / "dispatches" / dispatch_id / "status.json"),
        read_only=False,
        recover_unconfirmed=recover,
        base_sha=BASE_SHA,
    )


def _write_marker(state_dir: Path, dispatch_id: str, payload: dict[str, Any]) -> Path:
    marker_path = fleet_launch._launch_marker_path(state_dir, dispatch_id)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return marker_path


def _write_recoverable_marker(state_dir: Path, dispatch_id: str, prompt_text: str) -> Path:
    return _write_marker(
        state_dir,
        dispatch_id,
        {
            "schema": fleet_launch.MARKER_SCHEMA,
            "dispatch_id": dispatch_id,
            "node_id": "localhost",
            "state": "spawn_failed",
            "prompt_sha256": fleet_launch._prompt_sha256(prompt_text),
            "error": "prior spawn failed before worker start",
        },
    )


def _write_recovery_lock(
    state_dir: Path,
    dispatch_id: str,
    payload: dict[str, Any],
) -> Path:
    lock_path = fleet_launch._recovery_lock_path(state_dir, dispatch_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return lock_path


def test_clean_first_launch_creates_marker_and_spawns() -> None:
    dispatch_id = "acp-clean-launch"
    prompt_text = "first prompt"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        args = _args(state_dir, dispatch_id, prompt_text)
        stdout = io.StringIO()
        with patched_spawn() as calls, redirect_stdout(stdout):
            code = fleet_launch._launch(args)
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        marker = json.loads((dispatch_dir / "launch_marker.json").read_text(encoding="utf-8"))
        receipt = json.loads(stdout.getvalue())
        assert_true("launch ok", code == 0)
        assert_true("spawn once", len(calls) == 1)
        assert_true("prompt written", (dispatch_dir / "prompt.md").read_text(encoding="utf-8") == prompt_text)
        assert_true("receipt written", (dispatch_dir / "launch_receipt.json").exists())
        assert_true("stdout receipt", receipt.get("remote_pid") == 4242)
        assert_true("marker receipted", marker.get("state") == "receipted")
        assert_true("marker pid", marker.get("remote_pid") == 4242)


def test_duplicate_marker_recovery_without_no_worker_proof_refuses() -> None:
    dispatch_id = "acp-dup-marker"
    prompt_text = "retry prompt"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = dispatch_dir / "prompt.md"
        prompt_path.write_text("original prompt", encoding="utf-8")
        _write_marker(
            state_dir,
            dispatch_id,
            {
                "schema": fleet_launch.MARKER_SCHEMA,
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "state": "launching",
                "prompt_path": str(prompt_path),
                "prompt_sha256": fleet_launch._prompt_sha256(prompt_text),
            },
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        stderr = io.StringIO()
        with patched_spawn() as calls, redirect_stderr(stderr):
            code = fleet_launch._launch(args)
        assert_true("refused", code == 17)
        assert_true("no second spawn", len(calls) == 0)
        assert_true("prompt not overwritten", prompt_path.read_text(encoding="utf-8") == "original prompt")
        assert_true("warn refuse", "WARN-REFUSE duplicate dispatch-id" in stderr.getvalue())


def test_recovery_relaunch_resets_status_epoch() -> None:
    dispatch_id = "acp-recover-epoch-reset"
    prompt_text = "retry prompt"
    old_epoch = "status-deadbeef0001"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        status_json = dispatch_dir / "status.json"
        status_json.write_text(
            json.dumps(
                {
                    "schema": "goalflight.acp-run.v1",
                    "seq": 9,
                    "dispatch_id": dispatch_id,
                    "state": "spawn_failed",
                    "epoch": old_epoch,
                    "updated_at": "2026-06-11T12:00:00+00:00",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        _write_marker(
            state_dir,
            dispatch_id,
            {
                "schema": fleet_launch.MARKER_SCHEMA,
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "state": "spawn_failed",
                "prompt_sha256": fleet_launch._prompt_sha256(prompt_text),
                "error": "prior spawn failed before worker start",
            },
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        stdout = io.StringIO()
        with patched_spawn() as calls, redirect_stdout(stdout):
            code = fleet_launch._launch(args)
        assert_true("launch ok", code == 0)
        assert_true("spawn once", len(calls) == 1)
        assert_true("stale status removed", not status_json.exists())
        goalflight_liveness.write_status(
            status_json,
            {
                "schema": "goalflight.acp-run.v1",
                "seq": 1,
                "dispatch_id": dispatch_id,
                "state": "running",
            },
        )
        new_epoch = json.loads(status_json.read_text(encoding="utf-8"))["epoch"]
        assert_true("new epoch minted", new_epoch != old_epoch)


def test_read_only_recovery_inspection_preserves_status() -> None:
    dispatch_id = "acp-recover-readonly"
    prompt_text = "do not write this prompt"
    old_epoch = "status-cafebabe0002"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        status_json = dispatch_dir / "status.json"
        status_payload = {
            "schema": "goalflight.acp-run.v1",
            "seq": 4,
            "dispatch_id": dispatch_id,
            "state": "running",
            "worker_pid": 12345,
            "worker_identity": {
                "pid": 12345,
                "lstart": "Thu Jun 11 12:00:00 2026",
                "comm": "python3",
            },
            "epoch": old_epoch,
            "updated_at": "2026-06-11T12:00:00+00:00",
        }
        status_json.write_text(json.dumps(status_payload, sort_keys=True) + "\n", encoding="utf-8")
        reset_calls: list[Path] = []
        old_reset = goalflight_liveness.reset_status_lineage

        def track_reset(path: Path) -> bool:
            reset_calls.append(path)
            return old_reset(path)

        goalflight_liveness.reset_status_lineage = track_reset
        fleet_launch.reset_status_lineage = track_reset
        try:
            args = _args(state_dir, dispatch_id, prompt_text, recover=True)
            stdout = io.StringIO()
            def fake_identity(pid: int) -> dict[str, Any] | None:
                if pid == 12345:
                    return dict(status_payload["worker_identity"])
                return {"pid": pid, "lstart": "Thu Jun 11 12:00:01 2026", "comm": "python3"}

            with patched_process_identity(fake_identity), patched_spawn() as calls, redirect_stdout(stdout):
                code = fleet_launch._launch(args)
            receipt = json.loads(stdout.getvalue())
        finally:
            goalflight_liveness.reset_status_lineage = old_reset
            fleet_launch.reset_status_lineage = old_reset
        assert_true("inspection ok", code == 0)
        assert_true("no spawn", len(calls) == 0)
        assert_true("reused receipt", receipt.get("reused") is True)
        assert_true("recovered flag", receipt.get("recovered") is True)
        assert_true("status preserved", status_json.exists())
        preserved = json.loads(status_json.read_text(encoding="utf-8"))
        assert_true("epoch preserved", preserved.get("epoch") == old_epoch)
        assert_true("no lineage reset", len(reset_calls) == 0)
        assert_true("prompt not written", not (dispatch_dir / "prompt.md").exists())


def test_clean_first_launch_does_not_reset_status_lineage() -> None:
    dispatch_id = "acp-clean-no-reset"
    prompt_text = "first prompt"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        reset_calls: list[Path] = []
        old_reset = goalflight_liveness.reset_status_lineage

        def track_reset(path: Path) -> bool:
            reset_calls.append(path)
            return old_reset(path)

        goalflight_liveness.reset_status_lineage = track_reset
        fleet_launch.reset_status_lineage = track_reset
        try:
            args = _args(state_dir, dispatch_id, prompt_text, recover=False)
            stdout = io.StringIO()
            with patched_spawn(), redirect_stdout(stdout):
                code = fleet_launch._launch(args)
        finally:
            goalflight_liveness.reset_status_lineage = old_reset
            fleet_launch.reset_status_lineage = old_reset
        assert_true("launch ok", code == 0)
        assert_true("no lineage reset", len(reset_calls) == 0)


def test_recovery_with_no_worker_proof_relaunches() -> None:
    dispatch_id = "acp-recover-dead-marker"
    prompt_text = "retry prompt"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        _write_marker(
            state_dir,
            dispatch_id,
            {
                "schema": fleet_launch.MARKER_SCHEMA,
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "state": "spawn_failed",
                "prompt_sha256": fleet_launch._prompt_sha256(prompt_text),
                "error": "prior spawn failed before worker start",
            },
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        stdout = io.StringIO()
        with patched_spawn() as calls, redirect_stdout(stdout):
            code = fleet_launch._launch(args)
        marker = json.loads((dispatch_dir / "launch_marker.json").read_text(encoding="utf-8"))
        receipt = json.loads(stdout.getvalue())
        assert_true("launch ok", code == 0)
        assert_true("spawn once", len(calls) == 1)
        assert_true("receipt", receipt.get("remote_pid") == 4242)
        assert_true("marker receipted", marker.get("state") == "receipted")
        assert_true("proof kept", marker.get("no_worker_proof") == "marker_state_spawn_failed")
        assert_true("recovery lock cleared", not (dispatch_dir / "launch_recovery.lock").exists())


def test_recovery_ignores_dead_status_receipt_and_relaunches() -> None:
    dispatch_id = "acp-recover-dead-status-receipt"
    prompt_text = "retry prompt"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        status_json = dispatch_dir / "status.json"
        status_json.write_text(
            json.dumps(
                {
                    "schema": "goalflight.acp-run.v1",
                    "seq": 4,
                    "dispatch_id": dispatch_id,
                    "state": "running",
                    "worker_pid": 9999,
                    "worker_identity": {
                        "pid": 9999,
                        "lstart": "Thu Jun 11 12:00:00 2026",
                        "comm": "python3",
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        stdout = io.StringIO()

        def fake_identity(pid: int) -> dict[str, Any] | None:
            if pid == 9999:
                return None
            return {"pid": pid, "lstart": "Thu Jun 11 12:00:01 2026", "comm": "python3"}

        with patched_process_identity(fake_identity), patched_spawn() as calls, redirect_stdout(stdout):
            code = fleet_launch._launch(args)
        receipt = json.loads(stdout.getvalue())
        assert_true("launch ok", code == 0)
        assert_true("spawned replacement", len(calls) == 1)
        assert_true("new receipt", receipt.get("remote_pid") == 4242)
        assert_true("not reused dead receipt", receipt.get("reused") is not True)


def test_recovery_stale_launching_without_pid_relaunches() -> None:
    dispatch_id = "acp-recover-stale-launching-no-pid"
    prompt_text = "retry prompt"
    old_ts = (
        datetime.now(timezone.utc)
        - timedelta(seconds=fleet_launch.LAUNCHING_NO_PID_RECOVERY_SECONDS + 1)
    ).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        _write_marker(
            state_dir,
            dispatch_id,
            {
                "schema": fleet_launch.MARKER_SCHEMA,
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "state": "launching",
                "prompt_sha256": fleet_launch._prompt_sha256(prompt_text),
                "updated_at": old_ts,
            },
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        stdout = io.StringIO()
        with patched_spawn() as calls, redirect_stdout(stdout):
            code = fleet_launch._launch(args)
        marker = json.loads((dispatch_dir / "launch_marker.json").read_text(encoding="utf-8"))
        assert_true("launch ok", code == 0)
        assert_true("spawned recovery", len(calls) == 1)
        assert_true("stale launching proof", marker.get("no_worker_proof") == "marker_state_launching_no_pid_stale")


def test_recovery_timestampless_launching_marker_uses_bounded_mtime_recovery() -> None:
    dispatch_id = "acp-recover-timestampless-launching"
    prompt_text = "retry prompt"
    old_ts = datetime.now(timezone.utc) - timedelta(
        seconds=fleet_launch.LAUNCHING_NO_PID_RECOVERY_SECONDS + 1
    )
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        marker_path = _write_marker(
            state_dir,
            dispatch_id,
            {
                "schema": fleet_launch.MARKER_SCHEMA,
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "state": "launching",
                "prompt_sha256": fleet_launch._prompt_sha256(prompt_text),
            },
        )
        os.utime(marker_path, (old_ts.timestamp(), old_ts.timestamp()))
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        stdout = io.StringIO()
        with patched_spawn() as calls, redirect_stdout(stdout):
            code = fleet_launch._launch(args)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert_true("launch ok", code == 0)
        assert_true("spawned recovery", len(calls) == 1)
        assert_true("bounded no-timestamp proof", marker.get("no_worker_proof") == "marker_state_launching_no_pid_stale")


def test_recovery_fresh_launching_marker_beats_stale_dead_status() -> None:
    dispatch_id = "acp-recover-fresh-launching-beats-status"
    prompt_text = "retry prompt"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        status_json = dispatch_dir / "status.json"
        status_json.write_text(
            json.dumps(
                {
                    "schema": "goalflight.acp-run.v1",
                    "seq": 4,
                    "dispatch_id": dispatch_id,
                    "state": "running",
                    "worker_pid": 9999,
                    "worker_identity": {
                        "pid": 9999,
                        "lstart": "Thu Jun 11 12:00:00 2026",
                        "comm": "python3",
                    },
                    "updated_at": "2026-06-11T12:00:00+00:00",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        marker_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_marker(
            state_dir,
            dispatch_id,
            {
                "schema": fleet_launch.MARKER_SCHEMA,
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "state": "launching",
                "prompt_sha256": fleet_launch._prompt_sha256(prompt_text),
                "updated_at": marker_ts,
            },
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        stderr = io.StringIO()

        def fake_identity(pid: int) -> dict[str, Any] | None:
            if pid == 9999:
                return None
            return {"pid": pid, "lstart": "Thu Jun 11 12:00:01 2026", "comm": "python3"}

        with patched_process_identity(fake_identity), patched_spawn() as calls, redirect_stderr(stderr):
            code = fleet_launch._launch(args)
        assert_true("refused", code == 17)
        assert_true("no second spawn", len(calls) == 0)
        assert_true("fresh marker reason", "marker_state_launching_in_progress" in stderr.getvalue())


def test_recover_unconfirmed_dead_status_resets_lineage_before_spawn() -> None:
    dispatch_id = "acp-recover-dead-status-reset-before-spawn"
    prompt_text = "retry prompt"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        status_json = dispatch_dir / "status.json"
        status_json.write_text(
            json.dumps(
                {
                    "schema": "goalflight.acp-run.v1",
                    "seq": 7,
                    "dispatch_id": dispatch_id,
                    "state": "failed",
                    "worker_alive": False,
                    "worker_pid": 9999,
                    "worker_identity": {
                        "pid": 9999,
                        "lstart": "Thu Jun 11 12:00:00 2026",
                        "comm": "python3",
                    },
                    "epoch": "status-stale0003",
                    "updated_at": "2026-06-11T12:00:00+00:00",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        reset_calls: list[Path] = []
        status_present_at_spawn: list[bool] = []
        old_reset = goalflight_liveness.reset_status_lineage
        old_popen = fleet_launch.subprocess.Popen
        old_identity = fleet_launch._process_identity
        old_after_spawn = fleet_launch._process_identity_after_spawn
        old_which = fleet_launch.shutil.which

        def track_reset(path: Path) -> bool:
            reset_calls.append(path)
            return old_reset(path)

        def fake_identity(pid: int) -> dict[str, Any] | None:
            if pid == 9999:
                return None
            return {"pid": pid, "lstart": "Thu Jun 11 12:00:01 2026", "comm": "python3"}

        def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
            status_present_at_spawn.append(status_json.exists())
            return FakeProc()

        goalflight_liveness.reset_status_lineage = track_reset
        fleet_launch.reset_status_lineage = track_reset
        fleet_launch.subprocess.Popen = fake_popen
        fleet_launch._process_identity = fake_identity
        fleet_launch._process_identity_after_spawn = lambda pid: fake_identity(pid)
        fleet_launch.shutil.which = lambda _name: None
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = fleet_launch._launch(args)
        finally:
            goalflight_liveness.reset_status_lineage = old_reset
            fleet_launch.reset_status_lineage = old_reset
            fleet_launch.subprocess.Popen = old_popen
            fleet_launch._process_identity = old_identity
            fleet_launch._process_identity_after_spawn = old_after_spawn
            fleet_launch.shutil.which = old_which
        receipt = json.loads(stdout.getvalue())
        marker = json.loads((dispatch_dir / "launch_marker.json").read_text(encoding="utf-8"))
        assert_true("launch ok", code == 0)
        assert_true("new receipt", receipt.get("remote_pid") == 4242)
        assert_true("lineage reset", reset_calls == [status_json])
        assert_true("stale status gone before spawn", status_present_at_spawn == [False])
        assert_true("recovery marker", marker.get("no_worker_proof") == "preexisting_artifact_no_live_receipt")


def test_recovery_reclaims_dead_owner_lock() -> None:
    dispatch_id = "acp-recover-dead-lock-owner"
    prompt_text = "retry prompt"
    old_owner = {
        "pid": 7777,
        "lstart": "Thu Jun 11 12:00:00 2026",
        "comm": "python3",
    }
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        _write_recoverable_marker(state_dir, dispatch_id, prompt_text)
        lock_path = _write_recovery_lock(
            state_dir,
            dispatch_id,
            {
                "schema": fleet_launch.RECOVERY_LOCK_SCHEMA,
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "launcher_pid": old_owner["pid"],
                "launcher_identity": old_owner,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        stdout = io.StringIO()

        def fake_identity(pid: int) -> dict[str, Any] | None:
            if pid == old_owner["pid"]:
                return None
            return {"pid": pid, "lstart": "Thu Jun 11 12:00:01 2026", "comm": "python3"}

        with patched_process_identity(fake_identity), patched_spawn() as calls, redirect_stdout(stdout):
            code = fleet_launch._launch(args)
        marker = json.loads((dispatch_dir / "launch_marker.json").read_text(encoding="utf-8"))
        receipt = json.loads(stdout.getvalue())
        assert_true("launch ok", code == 0)
        assert_true("spawn once", len(calls) == 1)
        assert_true("receipt", receipt.get("remote_pid") == 4242)
        assert_true("dead lock reclaimed", marker.get("reclaimed_recovery_lock") == "owner_dead")
        assert_true("recovery lock cleared", not lock_path.exists())


def test_recovery_reclaims_reused_pid_owner_lock() -> None:
    dispatch_id = "acp-recover-reused-pid-lock-owner"
    prompt_text = "retry prompt"
    old_owner = {
        "pid": 7779,
        "lstart": "Thu Jun 11 12:00:00 2026",
        "comm": "python3",
    }
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        _write_recoverable_marker(state_dir, dispatch_id, prompt_text)
        lock_path = _write_recovery_lock(
            state_dir,
            dispatch_id,
            {
                "schema": fleet_launch.RECOVERY_LOCK_SCHEMA,
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "launcher_pid": old_owner["pid"],
                "launcher_identity": old_owner,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)

        def fake_identity(pid: int) -> dict[str, Any] | None:
            if pid == old_owner["pid"]:
                return {
                    "pid": pid,
                    "lstart": "Thu Jun 11 12:00:01 2026",
                    "comm": "python3",
                }
            return {"pid": pid, "lstart": "Thu Jun 11 12:00:02 2026", "comm": "python3"}

        with patched_process_identity(fake_identity), patched_spawn() as calls, redirect_stdout(io.StringIO()):
            code = fleet_launch._launch(args)
        marker = json.loads((dispatch_dir / "launch_marker.json").read_text(encoding="utf-8"))
        assert_true("launch ok", code == 0)
        assert_true("spawn once", len(calls) == 1)
        assert_true(
            "reused pid lock reclaimed",
            marker.get("reclaimed_recovery_lock") == "owner_pid_reused_lstart",
        )
        assert_true("recovery lock cleared", not lock_path.exists())


def test_recovery_live_owner_lock_refuses() -> None:
    dispatch_id = "acp-recover-live-lock-owner"
    prompt_text = "retry prompt"
    live_owner = {
        "pid": 7778,
        "lstart": "Thu Jun 11 12:00:00 2026",
        "comm": "python3",
    }
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        _write_recoverable_marker(state_dir, dispatch_id, prompt_text)
        lock_path = _write_recovery_lock(
            state_dir,
            dispatch_id,
            {
                "schema": fleet_launch.RECOVERY_LOCK_SCHEMA,
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "launcher_pid": live_owner["pid"],
                "launcher_identity": live_owner,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        stderr = io.StringIO()

        def fake_identity(pid: int) -> dict[str, Any] | None:
            if pid == live_owner["pid"]:
                return dict(live_owner)
            return {"pid": pid, "lstart": "Thu Jun 11 12:00:01 2026", "comm": "python3"}

        with patched_process_identity(fake_identity), patched_spawn() as calls, redirect_stderr(stderr):
            code = fleet_launch._launch(args)
        assert_true("refused", code == 17)
        assert_true("no second spawn", len(calls) == 0)
        assert_true("live lock preserved", lock_path.exists())
        assert_true("warn reason", "recovery_already_in_progress" in stderr.getvalue())


def test_recovery_reclaims_stale_unresolvable_owner_lock() -> None:
    dispatch_id = "acp-recover-stale-lock-owner"
    prompt_text = "retry prompt"
    old_created = datetime.now(timezone.utc) - timedelta(
        seconds=fleet_launch.RECOVERY_LOCK_TTL_SECONDS + 1
    )
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        _write_recoverable_marker(state_dir, dispatch_id, prompt_text)
        lock_path = _write_recovery_lock(
            state_dir,
            dispatch_id,
            {
                "schema": fleet_launch.RECOVERY_LOCK_SCHEMA,
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "launcher_pid": "not-a-pid",
                "created_at": old_created.isoformat(timespec="seconds"),
            },
        )
        args = _args(state_dir, dispatch_id, prompt_text, recover=True)
        stdout = io.StringIO()

        def fake_identity(pid: int) -> dict[str, Any] | None:
            return {"pid": pid, "lstart": "Thu Jun 11 12:00:01 2026", "comm": "python3"}

        with patched_process_identity(fake_identity), patched_spawn() as calls, redirect_stdout(stdout):
            code = fleet_launch._launch(args)
        marker = json.loads((dispatch_dir / "launch_marker.json").read_text(encoding="utf-8"))
        assert_true("launch ok", code == 0)
        assert_true("spawn once", len(calls) == 1)
        assert_true(
            "stale lock reclaimed",
            marker.get("reclaimed_recovery_lock") == "owner_no_pid_stale_created_at",
        )
        assert_true("recovery lock cleared", not lock_path.exists())


def test_ensure_local_bin_prepends_when_absent() -> None:
    env = {"HOME": "/Users/x", "PATH": "/usr/bin:/bin"}
    fleet_launch._ensure_local_bin_on_path(env)
    assert_true("local_bin prepended", env["PATH"] == "/Users/x/.local/bin:/usr/bin:/bin")


def test_ensure_local_bin_idempotent_when_present() -> None:
    env = {"HOME": "/Users/x", "PATH": "/Users/x/.local/bin:/usr/bin"}
    fleet_launch._ensure_local_bin_on_path(env)
    assert_true("unchanged when already present", env["PATH"] == "/Users/x/.local/bin:/usr/bin")


def test_ensure_local_bin_no_home_noop() -> None:
    env = {"PATH": "/usr/bin"}
    fleet_launch._ensure_local_bin_on_path(env)
    assert_true("no HOME leaves PATH untouched", env["PATH"] == "/usr/bin")
    assert_true("no HOME adds no HOME key", "HOME" not in env)


def test_ensure_local_bin_empty_path() -> None:
    env = {"HOME": "/Users/x", "PATH": ""}
    fleet_launch._ensure_local_bin_on_path(env)
    assert_true("empty PATH becomes local_bin", env["PATH"] == "/Users/x/.local/bin")


def test_ensure_local_bin_substring_not_false_match() -> None:
    # A PATH entry that only CONTAINS local_bin as a substring must not be read as
    # membership: split on pathsep, never a naive `local_bin in path` check.
    env = {"HOME": "/Users/x", "PATH": "/Users/x/.local/bin-extra:/usr/bin"}
    fleet_launch._ensure_local_bin_on_path(env)
    assert_true(
        "substring entry does not block prepend",
        env["PATH"] == "/Users/x/.local/bin:/Users/x/.local/bin-extra:/usr/bin",
    )


def test_sanitized_env_allows_oauth_token_exact_not_prefix() -> None:
    # claude-code-cli-acp reads CLAUDE_CODE_OAUTH_TOKEN for the headless subscription
    # seat. It must survive sanitization (the fix) but the allow must be EXACT-key:
    # other CLAUDE_CODE_* config must NOT be ferried (no CLAUDE_ prefix rule).
    source = {
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-placeholder-not-a-real-token",
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "PATH": "/usr/bin",
        "RANDOM_UNRELATED": "nope",
    }
    env = fleet_launch._sanitized_env(source)
    assert_true(
        "oauth token preserved",
        env.get("CLAUDE_CODE_OAUTH_TOKEN") == "oauth-placeholder-not-a-real-token",
    )
    assert_true("other CLAUDE_ config stripped (exact, not prefix)", "CLAUDE_CODE_ENABLE_TELEMETRY" not in env)
    assert_true("anthropic prefix still ferried", env.get("ANTHROPIC_BASE_URL") == "https://example.invalid")
    assert_true("ssh auth sock still denied", "SSH_AUTH_SOCK" not in env)
    assert_true("unrelated var stripped", "RANDOM_UNRELATED" not in env)
    assert_true("path preserved", env.get("PATH") == "/usr/bin")


def main() -> None:
    tests = [
        test_sanitized_env_allows_oauth_token_exact_not_prefix,
        test_ensure_local_bin_prepends_when_absent,
        test_ensure_local_bin_idempotent_when_present,
        test_ensure_local_bin_no_home_noop,
        test_ensure_local_bin_empty_path,
        test_ensure_local_bin_substring_not_false_match,
        test_clean_first_launch_creates_marker_and_spawns,
        test_duplicate_marker_recovery_without_no_worker_proof_refuses,
        test_recovery_relaunch_resets_status_epoch,
        test_read_only_recovery_inspection_preserves_status,
        test_clean_first_launch_does_not_reset_status_lineage,
        test_recovery_with_no_worker_proof_relaunches,
        test_recovery_ignores_dead_status_receipt_and_relaunches,
        test_recovery_stale_launching_without_pid_relaunches,
        test_recovery_timestampless_launching_marker_uses_bounded_mtime_recovery,
        test_recovery_fresh_launching_marker_beats_stale_dead_status,
        test_recover_unconfirmed_dead_status_resets_lineage_before_spawn,
        test_recovery_reclaims_dead_owner_lock,
        test_recovery_reclaims_reused_pid_owner_lock,
        test_recovery_live_owner_lock_refuses,
        test_recovery_reclaims_stale_unresolvable_owner_lock,
    ]
    for test in tests:
        test()
    print(f"OK: {len(tests)} fleet launch detached tests pass")


if __name__ == "__main__":
    main()
