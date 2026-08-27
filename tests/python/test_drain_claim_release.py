#!/usr/bin/env python3
"""A drain pass that dies must not leave a pre-worker claim stranded.

Claims are released on abnormal exit when no launch was attempted. After a
launch attempt, the carrier stays with pid+start-token identity so a later
pass can adjudicate it. This file does not add a sweeper.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="dispatch drain claim-release tests use POSIX queue helpers",
)


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


def _queue_dir(tmp_path: Path) -> Path:
    queue = tmp_path / "state" / "dispatch-queue"
    queue.mkdir(parents=True, exist_ok=True)
    return queue


def _write_entry(queue: Path, dispatch_id: str) -> Path:
    project_root = queue.parent.parent
    path = queue / f"{dispatch_id}.json"
    D._write_json_atomic(
        path,
        {
            "schema": D.DISPATCH_QUEUE_SCHEMA,
            "state": "queued",
            "dispatch_id": dispatch_id,
            "agent": "test-dispatch",
            "shape": "bash",
            "project_root": str(project_root),
            "process_cwd": str(project_root),
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "queue_path": str(path),
            "dispatch_argv": [
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                dispatch_id,
                "--cwd",
                str(project_root),
                "--",
                sys.executable,
                "-c",
                f"print('COMPLETE: {dispatch_id} — claim release')",
            ],
            "request": {
                "agent": "test-dispatch",
                "cwd": str(project_root),
                "tail": str(project_root / f"{dispatch_id}.tail"),
                "status_json": str(project_root / f"{dispatch_id}.status.json"),
            },
        },
    )
    return path


_REAL_SUBPROCESS_RUN = subprocess.run


def test_pre_launch_abort_releases_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exception before subprocess.run must restore the envelope, not strand it."""
    queue = _queue_dir(tmp_path)
    path = _write_entry(queue, "pre-launch-abort")
    before = {p.name for p in queue.glob("*.json")}

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated drain abort before launch")

    monkeypatch.setattr(D, "_drain_launch_argv", boom)
    rc = D._cmd_drain(["--queue-dir", str(queue), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1, payload
    assert payload.get("error", "").startswith("RuntimeError:"), payload
    assert path.exists(), "pre-launch abort deleted the envelope"
    assert not list(queue.glob("*.json.claimed-*")), list(queue.glob("*"))
    queued = json.loads(path.read_text(encoding="utf-8"))
    assert queued.get("state") == "queued", queued
    assert {p.name for p in queue.glob("*.json")} == before


def test_launch_exception_keeps_claim_with_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once launch is attempted, keep the carrier and its pid+start-token."""
    queue = _queue_dir(tmp_path)
    path = _write_entry(queue, "launch-abort")

    def explode(argv, *args, **kwargs):
        argv_list = list(argv)
        if any(str(part).endswith("goalflight_dispatch.py") for part in argv_list[:3]):
            raise RuntimeError("simulated launch abort")
        return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)

    monkeypatch.setattr(D.subprocess, "run", explode)
    rc = D._cmd_drain(["--queue-dir", str(queue), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1, payload
    claims = list(queue.glob("*.json.claimed-*"))
    assert claims, "launch-attempt exception restored a possibly-live launcher"
    assert not path.exists()
    parked = json.loads(claims[0].read_text(encoding="utf-8"))
    assert parked.get("queue_claimer_pid") == os.getpid()
    identity = parked.get("queue_claimer_identity")
    assert isinstance(identity, dict), parked
    assert identity.get("start_token"), parked
    status = D._adjudicate_claim_marker(claims[0], parked)
    assert status.kind is D.ClaimCarrierKind.LIVE
    assert claims[0].exists(), "launch-attempt path deleted the carrier"


def test_spawn_intent_exception_does_not_restore_or_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue_dir(tmp_path)
    path = _write_entry(queue, "spawn-intent-abort")
    claimed: list[Path] = []

    def explode_after_spawn(argv, *args, **kwargs):
        argv_list = list(argv)
        if "--queue-claim-path" not in argv_list:
            return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
        claim = Path(argv_list[argv_list.index("--queue-claim-path") + 1])
        claimed.append(claim)
        payload = json.loads(claim.read_text(encoding="utf-8"))
        payload["queue_launch_started"] = True
        payload["queue_worker_spawn_intent"] = True
        payload["queue_worker_spawn_intent_at"] = D.goalflight_ledger.utc_now()
        D._write_json_atomic(claim, payload)
        raise RuntimeError("simulated spawn-intent abort")

    monkeypatch.setattr(D.subprocess, "run", explode_after_spawn)
    with pytest.raises(RuntimeError, match="simulated spawn-intent abort"):
        D._drain_queue_once(
            __import__("argparse").Namespace(
                queue_dir=str(queue),
                capacity_wait_s=0.0,
                claim_stale_s=D.QUEUE_CLAIM_STALE_S,
                limit=0,
            )
        )
    assert claimed, "precondition: drain never took a claim"
    assert not path.exists()
    assert claimed[0].exists()
    parked = json.loads(claimed[0].read_text(encoding="utf-8"))
    assert parked.get("queue_worker_spawn_intent") is True
    assert parked.get("queue_claimer_pid") == os.getpid()
    identity = parked.get("queue_claimer_identity")
    assert isinstance(identity, dict)
    assert identity.get("start_token")
