#!/usr/bin/env python3
"""Claim markers held by a dead or unidentified claimer must be visible.

Drain and reconcile-abandoned used to treat any ``.claimed-*`` file as a
healthy ``active_queue_carrier``. PID-only filenames cannot prove live or
dead (a recycled pid looks live; an absent pid is not proof of death), so
adjudication is live / dead / unknown via pid+start-token identity. Neither
command mutates those markers: they report them.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402


def _spawn_sleeping_worker() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


def _dead_claimer_identity() -> tuple[int, dict]:
    proc = _spawn_sleeping_worker()
    try:
        identity = L.process_identity(proc.pid)
        assert identity is not None
        assert identity.get("start_token"), identity
        pid = proc.pid
    finally:
        _reap(proc)
    assert L.identity_matches({"worker_pid": pid, "worker_identity": identity}) == (
        False,
        "dead",
    )
    return pid, identity


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
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)


def _queue_dir(tmp_path: Path) -> Path:
    queue = tmp_path / "state" / "dispatch-queue"
    queue.mkdir(parents=True, exist_ok=True)
    return queue


def _snapshot(queue: Path) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(queue.iterdir())
        if path.is_file()
    }


def _write_claim(
    queue: Path,
    dispatch_id: str,
    *,
    suffix: str,
    payload: dict | None = None,
) -> Path:
    path = queue / f"{dispatch_id}.json.claimed-{suffix}"
    body = {
        "dispatch_id": dispatch_id,
        "state": "claimed",
        "agent": "codex",
        "shape": "bash",
        "created_at": L.utc_now(),
    }
    if payload:
        body.update(payload)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _record(tmp_path: Path, dispatch_id: str, **extra: object) -> dict:
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("worker output stopped without a verdict\n", encoding="utf-8")
    status = tmp_path / f"{dispatch_id}.status.json"
    payload = {
        "schema": L.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "shape": "bash",
        "transport": "dispatch",
        "project_root": str(tmp_path),
        "worker_cwd": str(tmp_path),
        "hostname": socket.gethostname(),
        "state": "running",
        "terminal_state": "unknown",
        "started_at": (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        ).isoformat(timespec="seconds"),
        "stdout_path": str(tail),
        "status_path": str(status),
    }
    payload.update(extra)
    L.write_record(payload)
    return payload


def _json_after_prefix(text: str, prefix: str) -> dict:
    line = next(
        part for part in text.splitlines() if part.startswith(prefix) or prefix in part
    )
    return json.loads(line[line.index("{") :])


def test_dead_live_unknown_markers_are_reported_and_not_mutated(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    queue = _queue_dir(tmp_path)
    dead_pid, dead_identity = _dead_claimer_identity()
    live_proc = _spawn_sleeping_worker()
    try:
        live_identity = L.process_identity(live_proc.pid)
        assert live_identity is not None
        assert live_identity.get("start_token"), live_identity

        dead_claim = _write_claim(
            queue,
            "dead-claimer",
            suffix=f"{dead_pid}-1",
            payload={
                "queue_claimer_pid": dead_pid,
                "queue_claimer_identity": dead_identity,
            },
        )
        live_claim = _write_claim(
            queue,
            "live-claimer",
            suffix=f"{live_proc.pid}-1",
            payload={
                "queue_claimer_pid": live_proc.pid,
                "queue_claimer_identity": live_identity,
            },
        )
        unknown_unparsable = _write_claim(
            queue,
            "unknown-unparsable",
            suffix="notapid",
        )
        unknown_filename_only = _write_claim(
            queue,
            "unknown-filename-only",
            suffix=f"{dead_pid}-2",
        )
        _record(tmp_path, "dead-claimer")
        _record(tmp_path, "live-claimer")
        _record(tmp_path, "unknown-unparsable")
        _record(tmp_path, "unknown-filename-only")
        before = _snapshot(queue)

        dead_status = D._claim_has_active_carrier(queue, "dead-claimer")
        live_status = D._claim_has_active_carrier(queue, "live-claimer")
        unparsable_status = D._claim_has_active_carrier(queue, "unknown-unparsable")
        filename_status = D._claim_has_active_carrier(queue, "unknown-filename-only")

        assert dead_status
        assert dead_status.kind is D.ClaimCarrierKind.DEAD
        assert live_status
        assert live_status.kind is D.ClaimCarrierKind.LIVE
        assert unparsable_status
        assert unparsable_status.kind is D.ClaimCarrierKind.UNKNOWN
        assert unparsable_status.reason == "claim_pid_unparsable"
        assert filename_status
        assert filename_status.kind is D.ClaimCarrierKind.UNKNOWN
        assert filename_status.reason == "claim_identity_unavailable"
        assert filename_status.kind is not D.ClaimCarrierKind.DEAD
        assert filename_status.kind is not D.ClaimCarrierKind.LIVE

        reconcile = D.reconcile_abandoned_dispatches(
            queue_dir=queue,
            dry_run=True,
            now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=900),
        )
        reasons = {
            entry["dispatch_id"]: entry["reason"]
            for entry in reconcile["entries"]
            if entry.get("dispatch_id") in {
                "dead-claimer",
                "live-claimer",
                "unknown-unparsable",
                "unknown-filename-only",
            }
        }
        assert reasons["dead-claimer"] == "dead_claimer"
        assert reasons["live-claimer"] == "active_queue_carrier"
        assert reasons["unknown-unparsable"] == "unknown_claimer"
        assert reasons["unknown-filename-only"] == "unknown_claimer"
        assert "active_queue_carrier" not in {
            reasons["dead-claimer"],
            reasons["unknown-unparsable"],
            reasons["unknown-filename-only"],
        }
        assert reconcile["would_close"] == 0
        assert reconcile["closed"] == 0
        assert reconcile["dead_claimer"] == 1
        assert reconcile["unknown_claimer"] == 2
        assert reconcile["live_claimer"] == 1
        assert reconcile["kept_reasons"]["dead_claimer"] == 1
        assert reconcile["kept_reasons"]["unknown_claimer"] == 2
        assert reconcile["kept_reasons"]["active_queue_carrier"] == 1

        rec_rc = D._cmd_reconcile_abandoned(["--queue-dir", str(queue), "--stale-s", "0"])
        rec_out = capsys.readouterr().out
        assert rec_rc == 0
        rec_text = _json_after_prefix(rec_out, "RECONCILE-ABANDONED")
        assert rec_text["dead_claimer"] == 1
        assert rec_text["unknown_claimer"] == 2
        assert rec_text["live_claimer"] == 1
        assert rec_text["would_close"] == 0

        drain_json_rc = D._cmd_drain(
            ["--queue-dir", str(queue), "--claim-stale-s", "9999", "--json"]
        )
        drain_json_out = capsys.readouterr().out
        drain_json = json.loads(drain_json_out)
        assert drain_json_rc == 0
        assert drain_json["dead_claimer"] == 1
        assert drain_json["unknown_claimer"] == 2
        assert drain_json["live_claimer"] == 1
        assert drain_json["launched"] == 0
        assert drain_json["left_queued"] == 0
        assert drain_json["remaining"] == 0
        assert drain_json["failed"] == 0

        drain_text_rc = D._cmd_drain(
            ["--queue-dir", str(queue), "--claim-stale-s", "9999"]
        )
        drain_text_out = capsys.readouterr().out
        assert drain_text_rc == 0
        drain_text = _json_after_prefix(drain_text_out, "DRAIN")
        assert drain_text["dead_claimer"] == 1
        assert drain_text["unknown_claimer"] == 2
        assert drain_text["live_claimer"] == 1

        after = _snapshot(queue)
        assert after == before
        assert dead_claim.exists()
        assert live_claim.exists()
        assert unknown_unparsable.exists()
        assert unknown_filename_only.exists()
        for record_id in (
            "dead-claimer",
            "live-claimer",
            "unknown-unparsable",
            "unknown-filename-only",
        ):
            assert json.loads(L.record_path(record_id).read_text(encoding="utf-8"))[
                "state"
            ] == "running"
    finally:
        _reap(live_proc)


def test_queued_envelope_stays_healthy_carrier(tmp_path: Path) -> None:
    queue = _queue_dir(tmp_path)
    path = queue / "queued-only.json"
    path.write_text(
        json.dumps({"dispatch_id": "queued-only", "state": "queued"}),
        encoding="utf-8",
    )
    _record(tmp_path, "queued-only")
    status = D._claim_has_active_carrier(queue, "queued-only")
    assert status.kind is D.ClaimCarrierKind.QUEUED
    result = D.reconcile_abandoned_dispatches(queue_dir=queue, dry_run=True)
    reasons = {entry["dispatch_id"]: entry["reason"] for entry in result["entries"]}
    assert reasons["queued-only"] == "active_queue_carrier"
    assert result["dead_claimer"] == 0
    assert result["unknown_claimer"] == 0
    assert path.exists()
