#!/usr/bin/env python3
"""DrainClaimGuard restore must not mint an unadoptable restore_prepared envelope.

A second pre-worker release used to write state=restore_prepared under a new
txn id, then abort because the ledger was already queued with the previous
txn. Drain skipped those files (awaiting_owner_reconcile) and claim recovery
deferred them (unlinked_quarantine_deferred). Nothing adopted them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="dispatch restore-prepared tests use POSIX queue helpers",
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
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
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


def _worker_argv(tmp_path: Path, dispatch_id: str, marker: Path) -> list[str]:
    worker_code = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('x')\n"
        f"print('COMPLETE: {dispatch_id} — restore-prepared producer', flush=True)\n"
    )
    return [
        "--agent",
        "test-dispatch",
        "--unregistered-forced",
        "--dispatch-id",
        dispatch_id,
        "--cwd",
        str(tmp_path),
        "--tail",
        str(tmp_path / f"{dispatch_id}.tail"),
        "--status-json",
        str(tmp_path / f"{dispatch_id}.status.json"),
        "--poll-secs",
        "0.1",
        "--",
        sys.executable,
        "-c",
        worker_code,
    ]


def _claim_entry(
    tmp_path: Path,
    dispatch_id: str,
    *,
    token: str,
    marker: Path,
    label: str = "owner-label",
) -> dict:
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("", encoding="utf-8")
    return {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "claimed",
        "dispatch_id": dispatch_id,
        "agent": "test-dispatch",
        "shape": "bash",
        "project_root": str(tmp_path),
        "process_cwd": str(tmp_path),
        "created_at": "2000-01-01T00:00:00+00:00",
        "queue_launch_token": token,
        "dispatch_argv": _worker_argv(tmp_path, dispatch_id, marker),
        "request": {
            "agent": "test-dispatch",
            "cwd": str(tmp_path),
            "tail": str(tail),
            "status_json": str(tmp_path / f"{dispatch_id}.status.json"),
            "controller_label": label,
            "controller_beacon_pid": os.getpid(),
        },
    }


def _reclaim(queue: Path, target: Path, token: str) -> Path:
    queued = json.loads(target.read_text(encoding="utf-8"))
    claim = queue / f"{target.name}.claimed-2"
    queued["state"] = "claimed"
    queued["queue_launch_token"] = token
    claim.write_text(json.dumps(queued), encoding="utf-8")
    target.unlink()
    return claim


def test_second_drain_restore_republishes_queued_with_owner_and_launches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live producer: DrainClaimGuard restore of an already-queued row.

    First release stamps restore_txn_id on a queued ledger row. Second
    release must republish state=queued (launchable), not mint restore_prepared
    under a new txn that drain will never adopt.
    """
    queue = _queue_dir(tmp_path)
    dispatch_id = "second-restore-launch"
    marker = tmp_path / "launched.txt"
    entry = _claim_entry(tmp_path, dispatch_id, token="tok-1", marker=marker)
    claim = queue / f"{dispatch_id}.json.claimed-1"
    claim.write_text(json.dumps(entry), encoding="utf-8")

    restored, decision = D._restore_claim_if_incomplete(claim, entry, queue, stale_s=0.0)
    target = queue / f"{dispatch_id}.json"
    assert restored is not None and decision is None, (restored, decision)
    first = json.loads(target.read_text(encoding="utf-8"))
    assert first["state"] == "queued", first

    claim2 = _reclaim(queue, target, "tok-2")
    restored2, decision2 = D._restore_claim_if_incomplete(
        claim2, json.loads(claim2.read_text(encoding="utf-8")), queue, stale_s=0.0
    )
    assert target.exists(), list(queue.iterdir())
    second = json.loads(target.read_text(encoding="utf-8"))
    assert second["state"] == "queued", second
    assert restored2 is not None and decision2 is None, (restored2, decision2)
    assert not claim2.exists(), list(queue.iterdir())
    assert second.get("controller_label") == "owner-label", second
    row2 = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert row2["state"] == "queued", row2
    assert row2.get("controller_label") == "owner-label", row2

    rc = D._cmd_drain(["--queue-dir", str(queue), "--capacity-wait-s", "0", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["launched"] == 1, payload
    assert second["state"] != "restore_prepared"
    assert not target.exists(), list(queue.iterdir())


def test_stranded_restore_prepared_is_adopted_and_launches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Already-stranded shape: restore_prepared + queued ledger, no claim."""
    queue = _queue_dir(tmp_path)
    dispatch_id = "stranded-restore-prepared"
    marker = tmp_path / "stranded-launched.txt"
    entry = _claim_entry(tmp_path, dispatch_id, token="tok-strand", marker=marker)
    prepared = D._sanitize_restore_envelope(entry, increment_recovery_count=False)
    prepared.update(
        {
            "state": "restore_prepared",
            "restore_txn_id": "txn-stranded-b",
            "restore_reason": "normal_drain_restore",
        }
    )
    target = queue / f"{dispatch_id}.json"
    D._write_json_atomic(target, prepared)
    record = D._new_reconciliation_record(entry)
    record.update(
        {
            "state": "queued",
            "terminal_state": "unknown",
            "restore_txn_id": "txn-stranded-a",
            "queue_path": str(target),
        }
    )
    L.write_record(record)
    assert json.loads(target.read_text(encoding="utf-8"))["state"] == "restore_prepared"

    rc = D._cmd_drain(["--queue-dir", str(queue), "--capacity-wait-s", "0", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["launched"] == 1, payload
    assert not target.exists(), list(queue.iterdir())
    holds = payload.get("holds") or {}
    rp = holds.get("restore_prepared") or {}
    assert int(rp.get("count") or 0) == 0, holds


def test_completed_work_does_not_leave_restore_prepared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = _queue_dir(tmp_path)
    dispatch_id = "complete-restore-prepared"
    marker = tmp_path / "complete-should-not-launch.txt"
    entry = _claim_entry(tmp_path, dispatch_id, token="tok-complete", marker=marker)
    prepared = D._sanitize_restore_envelope(entry, increment_recovery_count=False)
    prepared.update(
        {
            "state": "restore_prepared",
            "restore_txn_id": "txn-complete",
            "restore_reason": "normal_drain_restore",
        }
    )
    target = queue / f"{dispatch_id}.json"
    D._write_json_atomic(target, prepared)
    record = D._new_reconciliation_record(entry)
    record.update(
        {
            "state": "complete",
            "terminal_state": "complete",
            "restore_txn_id": "txn-complete",
            "queue_path": str(target),
        }
    )
    L.write_record(record)

    rc = D._cmd_drain(["--queue-dir", str(queue), "--capacity-wait-s", "0", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["launched"] == 0, payload
    assert not target.exists(), "completed work left a restore_prepared envelope"
    assert not marker.exists(), "completed work was relaunched"
    rp = (payload.get("holds") or {}).get("restore_prepared") or {}
    assert int(rp.get("count") or 0) == 0, payload
