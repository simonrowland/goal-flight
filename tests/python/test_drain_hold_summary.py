#!/usr/bin/env python3
"""drain --json must say WHY the queue is holding, not only THAT it held.

t-345 (observed 2026-08-27): 21 of 28 shared-queue entries were
`restore_prepared` mid-restore envelopes, some two days old. Drain deferred
them every pass and reported launched:0 — correct per entry, but the summary
never carried the cause, and a deliberately-waiting queue read exactly like a
broken one. Seven more entries carried quota-aware `not_before` holds that the
summary likewise did not mention.

This module pins:
- the holds aggregate (waiting-on-quota count + earliest wake, restore_prepared
  count by owner-generation state, reconcile-pending reason histogram,
  quarantined count);
- per-entry detail rows for restore_prepared envelopes (previously invisible);
- proof-gated attention: only a PROVABLY dead owning controller generation
  (gone pid or start-token reuse, via the existing claim-identity adjudicator)
  lands in `attention`. A live owner is reported as held-because-owner-alive;
  anything unresolvable is owner_state "unknown" and HELD — never attention,
  never expired. Drain must not mutate any of these entries.
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


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(state / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER", str(tmp_path / "wake-ledger.json"))
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
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


def _write_entry(queue: Path, dispatch_id: str, **extra: object) -> Path:
    body = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "dispatch_id": dispatch_id,
        "state": "restore_prepared",
        "restore_txn_id": f"txn-{dispatch_id}",
        "restore_reason": "stale_claim_pre_spawn",
        "agent": "codex",
        "shape": "bash",
        "project_root": "/tmp/some-project",
        "created_at": "2026-08-25T00:00:00+00:00",
        "updated_at": "2026-08-26T00:00:00+00:00",
        "request": {"cwd": "/tmp/some-project"},
    }
    body.update(extra)
    path = queue / f"{dispatch_id}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _record(tmp_path: Path, dispatch_id: str, **extra: object) -> dict:
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("prelaunch; no worker output\n", encoding="utf-8")
    status = tmp_path / f"{dispatch_id}.status.json"
    payload = {
        "schema": L.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "shape": "bash",
        "transport": "dispatch",
        "project_root": "/tmp/some-project",
        "worker_cwd": "/tmp/some-project",
        "hostname": socket.gethostname(),
        "state": "queued",
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


def _drain_json(queue: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    rc = D._cmd_drain(["--queue-dir", str(queue), "--claim-stale-s", "9999", "--json"])
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_holds_report_quota_waits_and_restore_prepared_by_owner_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = _queue_dir(tmp_path)

    # Quota-aware hold: a plain queued entry with a future not_before.
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).isoformat(timespec="seconds")
    (queue / "quota-held.json").write_text(
        json.dumps(
            {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "dispatch_id": "quota-held",
                "state": "queued",
                "agent": "codex",
                "shape": "bash",
                "project_root": "/tmp/some-project",
                "not_before": future,
                "created_at": "2026-08-27T00:00:00+00:00",
                "request": {"cwd": "/tmp/some-project"},
            }
        ),
        encoding="utf-8",
    )

    live_proc = _spawn_sleeper()
    try:
        live_identity = L.process_identity(live_proc.pid)
        assert live_identity is not None and live_identity.get("start_token")

        # Owner LIVE: ledger record names a running controller generation.
        _write_entry(queue, "owner-live")
        _record(
            tmp_path,
            "owner-live",
            controller_pid=live_proc.pid,
            controller_identity=live_identity,
        )

        # Owner DEAD: identity captured from a real process that then exited —
        # the stored start_token proves the generation is gone.
        dead_proc = _spawn_sleeper()
        dead_pid = dead_proc.pid
        dead_identity = L.process_identity(dead_pid)
        assert dead_identity is not None and dead_identity.get("start_token")
        _reap(dead_proc)
        assert L.identity_matches({"worker_pid": dead_pid, "worker_identity": dead_identity}) == (
            False,
            "dead",
        )
        _write_entry(queue, "owner-dead")
        _record(
            tmp_path,
            "owner-dead",
            controller_pid=dead_pid,
            controller_identity=dead_identity,
        )

        # Owner UNKNOWN: no ledger record at all — nothing to adjudicate.
        _write_entry(queue, "owner-unknown")

        # Owner UNKNOWN via pid-only record: a live pid with no stored
        # generation identity cannot prove live OR dead.
        _write_entry(queue, "owner-inconclusive")
        _record(tmp_path, "owner-inconclusive", controller_pid=live_proc.pid)

        before = _snapshot(queue)
        payload = _drain_json(queue, capsys)

        holds = payload["holds"]
        assert holds["not_before"]["count"] == 1, holds
        assert holds["not_before"]["until"] == future, holds
        assert holds["not_before"]["winner"] in {"probe", "dispatch", "none"}, holds
        assert holds["not_before"]["winner_age"], holds
        rp = holds["restore_prepared"]
        assert rp["count"] == 4, rp
        assert rp["owner_live"] == 1, rp
        assert rp["owner_dead"] == 1, rp
        assert rp["owner_unknown"] == 2, rp
        assert payload["launched"] == 0
        assert payload["left_queued"] == 5  # 4 restore_prepared + 1 quota hold

        attention = payload["attention"]
        assert [item["dispatch_id"] for item in attention] == ["owner-dead"], attention
        assert attention[0]["attention"] == "owner_generation_dead"
        assert attention[0]["state"] == "restore_prepared"
        assert attention[0]["owner_reason"] == "dead"

        by_id = {row["dispatch_id"]: row for row in payload["details"]}
        for dispatch_id in ("owner-live", "owner-dead", "owner-unknown", "owner-inconclusive"):
            row = by_id[dispatch_id]
            assert row["state"] == "restore_prepared"
            assert row["reason"] == "awaiting_owner_reconcile"
        # The required distinction: held-because-owner-ALIVE vs held-because-UNKNOWN.
        assert by_id["owner-live"]["owner_state"] == "live"
        assert by_id["owner-unknown"]["owner_state"] == "unknown"
        assert by_id["owner-unknown"]["detail"] == "owner_unknown:no_ledger_record"
        assert by_id["owner-inconclusive"]["owner_state"] == "unknown"
        assert by_id["owner-dead"]["owner_state"] == "dead"
        assert by_id["quota-held"]["reason"] == "not_before"
        assert by_id["quota-held"]["not_before"] == future

        # Reporting only: nothing in the queue was mutated or removed.
        assert _snapshot(queue) == before
    finally:
        _reap(live_proc)


def test_reconcile_pending_reasons_are_aggregated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unlinked claims that reconcile defers show up as a reason histogram."""
    queue = _queue_dir(tmp_path)
    for dispatch_id in ("orphan-a", "orphan-b"):
        (queue / f"{dispatch_id}.json.claimed-1").write_text(
            json.dumps(
                {
                    "schema": D.DISPATCH_QUEUE_SCHEMA,
                    "dispatch_id": dispatch_id,
                    "state": "claimed",
                    "agent": "codex",
                    "shape": "bash",
                    "project_root": "/tmp/some-project",
                    "orphan_first_seen_at": "2026-08-20T00:00:00+00:00",
                    "created_at": "2026-08-20T00:00:00+00:00",
                    "request": {"cwd": "/tmp/some-project"},
                }
            ),
            encoding="utf-8",
        )
        _record(tmp_path, dispatch_id)
    payload = _drain_json(queue, capsys)
    pending = payload["holds"]["reconcile_pending"]
    assert pending.get("unlinked_quarantine_deferred") == 2, pending


def test_cwdless_nonterminal_is_drain_attention_not_a_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A readable running row with no cwd is a bookkeeping defect, not a gate.

    Drain still launches nothing from an empty queue, but attention names the
    dispatch id so a launched:0 pass is not silent. Occupancy skip does not
    become UNKNOWN of every tree.
    """
    queue = _queue_dir(tmp_path)
    _record(tmp_path, "cwdless-ghost", worker_cwd=None, state="running")
    rc = D._cmd_drain(["--queue-dir", str(queue), "--claim-stale-s", "9999", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    items = [
        row
        for row in payload.get("attention") or []
        if row.get("attention") == "cwdless_nonterminal"
    ]
    assert len(items) == 1, payload.get("attention")
    assert items[0]["dispatch_id"] == "cwdless-ghost"
    assert items[0]["state"] == "running"
    assert "cwdless-ghost" in captured.err
    assert "names no worker cwd" in captured.err
    assert "occupancy skip" in captured.err
    assert payload["holds"]["not_before"]["count"] == 0
    assert payload["launched"] == 0

    rc = D._cmd_drain(["--queue-dir", str(queue), "--claim-stale-s", "9999"])
    assert rc == 0
    line = capsys.readouterr().out
    text = line[line.index("{") :]
    summary = json.loads(text)
    assert summary["cwdless_nonterminal"] == 1
    assert summary["attention"] == 1


def test_empty_queue_reports_zero_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = _queue_dir(tmp_path)
    payload = _drain_json(queue, capsys)
    assert payload["holds"] == {
        "not_before": {
            "count": 0,
            "until": None,
            "winner": None,
            "winner_age": None,
        },
        "restore_prepared": {"count": 0, "owner_live": 0, "owner_dead": 0, "owner_unknown": 0},
        "reconcile_pending": {},
        "quarantined": 0,
    }
    assert payload["attention"] == []


def test_text_line_carries_hold_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    queue = _queue_dir(tmp_path)
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat(timespec="seconds")
    (queue / "later.json").write_text(
        json.dumps(
            {
                "schema": D.DISPATCH_QUEUE_SCHEMA,
                "dispatch_id": "later",
                "state": "queued",
                "agent": "codex",
                "shape": "bash",
                "not_before": future,
                "created_at": "2026-08-27T00:00:00+00:00",
                "request": {"cwd": "/tmp/some-project"},
            }
        ),
        encoding="utf-8",
    )
    _write_entry(queue, "parked")
    rc = D._cmd_drain(["--queue-dir", str(queue), "--claim-stale-s", "9999"])
    assert rc == 0
    line = capsys.readouterr().out
    text = line[line.index("{") :]
    summary = json.loads(text)
    assert summary["launched"] == 0
    assert summary["waiting_not_before"] == 1
    assert summary["waiting_not_before_until"] == future
    assert summary["waiting_not_before_winner"] in {"probe", "dispatch", "none"}
    assert summary["waiting_not_before_age"]
    assert summary["awaiting_owner_reconcile"] == 1
    assert summary["owner_generation_dead"] == 0
    assert summary["attention"] == 0
