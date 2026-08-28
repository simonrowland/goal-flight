#!/usr/bin/env python3
"""Requeue intent lifecycle: disposition, successor, bounds, created_at.

Preconditions are real ledger rows in the isolated state dir. Doubling
"already complete" or "has a successor" onto the function under test would
not prove the production path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402


def _txn() -> SimpleNamespace:
    return SimpleNamespace(queue_locked=True, ledger_locked=True)


def _claimed_entry(
    tmp_path: Path,
    dispatch_id: str,
    *,
    task_ids: list[str] | None = None,
) -> tuple[dict, Path, Path]:
    queue_dir = D._dispatch_queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("quota exceeded\n", encoding="utf-8")
    request = {
        "agent": "codex",
        "cwd": str(tmp_path),
        "dispatch_id": dispatch_id,
        "tail": str(tail),
        "status_json": str(tmp_path / f"{dispatch_id}.status.json"),
    }
    if task_ids:
        request["task_ids"] = list(task_ids)
    entry = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "claimed",
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "shape": "bash",
        "project_root": str(tmp_path),
        "process_cwd": str(tmp_path),
        "created_at": L.utc_now(),
        "updated_at": L.utc_now(),
        "dispatch_argv": [
            "--agent",
            "codex",
            "--dispatch-id",
            dispatch_id,
            "--tail",
            str(tail),
            "--status-json",
            str(tmp_path / f"{dispatch_id}.status.json"),
            "--cwd",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        "request": request,
    }
    if task_ids:
        entry["task_ids"] = list(task_ids)
    return entry, queue_dir, tail


def _write_quota_record(
    dispatch_id: str,
    *,
    tmp_path: Path,
    task_ids: list[str] | None = None,
    ended_at: str,
    requeue: dict | None = None,
    state: str = "quota_exhausted",
) -> dict:
    record = {
        "schema": L.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "shape": "bash",
        "account": "default",
        "effective_account": "seat-r",
        "transport": "dispatch",
        "project_root": str(tmp_path),
        "state": state,
        "terminal_state": state,
        "started_at": ended_at,
        "ended_at": ended_at,
        "task_ids": list(task_ids or []),
    }
    if requeue is not None:
        record["requeue"] = requeue
    L.write_record(record)
    return json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))


def _read_record(dispatch_id: str) -> dict:
    return json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))


def _iso_days_ago(days: int) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(days=days)
    return stamp.isoformat(timespec="seconds")


def test_completed_successor_satisfies_intent_and_does_not_relodge(
    tmp_path: Path,
) -> None:
    parent_id = "fr-d1-r3"
    successor_id = "fr-d1-r4"
    child_id = "fr-d1-r3-retry-b8fa0aba"
    task_ids = ["t-successor"]
    parent_ended = _iso_days_ago(3)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=parent_ended,
        requeue={"child_id": child_id, "requeued_at": parent_ended},
    )
    _write_quota_record(
        successor_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=_iso_days_ago(0),
        state="complete",
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    parent = _read_record(parent_id)
    intent = parent["requeue"]
    assert intent["disposition"] == "satisfied"
    assert intent["satisfied_by"] == successor_id
    assert intent["disposition_reason"] == "successor_complete"
    child_path = D._queue_entry_path(child_id, queue_dir=queue_dir)
    assert not child_path.exists()
    assert D._terminal_ledger_requeue_pending(parent, entry, queue_dir=queue_dir) is False


def test_stale_intent_expires_by_age_and_does_not_relodge(tmp_path: Path) -> None:
    parent_id = "t746-r2"
    child_id = "t746-r2-retry-oldage01"
    requeued_at = _iso_days_ago(3)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        ended_at=requeued_at,
        requeue={"child_id": child_id, "requeued_at": requeued_at},
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    intent = _read_record(parent_id)["requeue"]
    assert intent["disposition"] == "expired"
    assert intent["disposition_reason"] == "max_age"
    assert not D._queue_entry_path(child_id, queue_dir=queue_dir).exists()


def test_regeneration_preserves_created_at_and_surfaces_attempt(
    tmp_path: Path,
) -> None:
    parent_id = "regen-created-at"
    child_id = "regen-created-at-retry-cafecafe"
    original_created = "2026-08-25T18:05:26+00:00"
    requeued_at = _iso_days_ago(0)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        ended_at=requeued_at,
        requeue={
            "child_id": child_id,
            "requeued_at": requeued_at,
            "child_created_at": original_created,
            "attempt_count": 1,
        },
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    child_path = D._queue_entry_path(child_id, queue_dir=queue_dir)
    child = json.loads(child_path.read_text(encoding="utf-8"))
    assert child["created_at"] == original_created
    assert child["created_at"] is not None
    assert child["requeue_attempt"] == 2
    assert child["request"]["requeue_attempt"] == 2
    intent = _read_record(parent_id)["requeue"]
    assert intent["child_created_at"] == original_created
    assert intent["attempt_count"] == 2

    child_path.unlink()
    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    child = json.loads(child_path.read_text(encoding="utf-8"))
    assert child["created_at"] == original_created
    assert child["requeue_attempt"] == 3


def test_attempt_bound_expires_instead_of_regenerating(tmp_path: Path) -> None:
    parent_id = "regen-attempts"
    child_id = "regen-attempts-retry-deadbeef"
    requeued_at = _iso_days_ago(0)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        ended_at=requeued_at,
        requeue={
            "child_id": child_id,
            "requeued_at": requeued_at,
            "attempt_count": D.REQUEUE_MAX_ATTEMPTS,
            "child_created_at": requeued_at,
        },
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    intent = _read_record(parent_id)["requeue"]
    assert intent["disposition"] == "expired"
    assert intent["disposition_reason"] == "max_attempts"
    assert not D._queue_entry_path(child_id, queue_dir=queue_dir).exists()


def test_unknown_age_and_unlinked_work_retains_and_relodges(
    tmp_path: Path,
) -> None:
    parent_id = "unknown-retain"
    child_id = "unknown-retain-retry-aaaaaaaa"
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        ended_at=_iso_days_ago(0),
        requeue={"child_id": child_id},
    )
    garbage = L.runs_dir() / "corrupt.json"
    garbage.write_text("{not-json", encoding="utf-8")

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    child_path = D._queue_entry_path(child_id, queue_dir=queue_dir)
    assert child_path.exists()
    child = json.loads(child_path.read_text(encoding="utf-8"))
    assert child["created_at"] not in (None, "")
    intent = _read_record(parent_id)["requeue"]
    assert intent.get("disposition") not in {"satisfied", "abandoned", "expired"}


def test_first_lodge_records_attempt_one(tmp_path: Path) -> None:
    parent_id = "first-lodge"
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        ended_at=_iso_days_ago(0),
    )
    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    intent = _read_record(parent_id)["requeue"]
    child_id = intent["child_id"]
    child = json.loads(
        D._queue_entry_path(child_id, queue_dir=queue_dir).read_text(encoding="utf-8")
    )
    assert intent["attempt_count"] == 1
    assert child["requeue_attempt"] == 1
    assert child["created_at"] == intent["child_created_at"]
    assert child["created_at"] not in (None, "")
