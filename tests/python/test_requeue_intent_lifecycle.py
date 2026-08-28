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
    project_root: Path | str | None = "",
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
        "state": state,
        "terminal_state": state,
        "started_at": ended_at,
        "ended_at": ended_at,
        "task_ids": list(task_ids or []),
    }
    if project_root == "":
        record["project_root"] = str(tmp_path)
    elif project_root is not None:
        record["project_root"] = str(project_root)
    if requeue is not None:
        record["requeue"] = requeue
    L.write_record(record)
    return json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))


def _plant_child_envelope(queue_dir: Path, child_id: str) -> Path:
    """A real retry file so successor unlink is observed, not vacuous."""
    child_path = D._queue_entry_path(child_id, queue_dir=queue_dir)
    child_path.write_text(
        json.dumps({"schema": D.DISPATCH_QUEUE_SCHEMA, "dispatch_id": child_id}),
        encoding="utf-8",
    )
    return child_path


def _read_record(dispatch_id: str) -> dict:
    return json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))


def _iso_days_ago(days: int) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(days=days)
    return stamp.isoformat(timespec="seconds")


def _iso_hours_ago(hours: int) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(hours=hours)
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
    child_path = _plant_child_envelope(queue_dir, child_id)
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
    assert not child_path.exists()
    assert D._terminal_ledger_requeue_pending(parent, entry, queue_dir=queue_dir) is False


def test_foreign_project_successor_does_not_satisfy_or_unlink(tmp_path: Path) -> None:
    """Same task id in another repo is not a successor (host-wide ledger).

    Task ids collide across projects by design. Matching on task_ids alone
    would unlink this project's retry when a different repo completes the
    same chunk id.
    """
    kiln_root = tmp_path / "kiln"
    papers_root = tmp_path / "papers-propulsion"
    kiln_root.mkdir()
    papers_root.mkdir()
    parent_id = "kiln-t022-quota"
    successor_id = "papers-t022-done"
    child_id = "kiln-t022-quota-retry-cafecafe"
    task_ids = ["t-022"]
    parent_ended = _iso_hours_ago(1)
    entry, queue_dir, tail = _claimed_entry(kiln_root, parent_id, task_ids=task_ids)
    child_path = _plant_child_envelope(queue_dir, child_id)
    _write_quota_record(
        parent_id,
        tmp_path=kiln_root,
        task_ids=task_ids,
        ended_at=parent_ended,
        requeue={"child_id": child_id, "requeued_at": parent_ended},
        project_root=kiln_root,
    )
    _write_quota_record(
        successor_id,
        tmp_path=papers_root,
        task_ids=task_ids,
        ended_at=_iso_days_ago(0),
        state="complete",
        project_root=papers_root,
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    parent = _read_record(parent_id)
    intent = parent["requeue"]
    assert intent.get("disposition") not in {"satisfied", "abandoned", "expired"}
    assert intent.get("satisfied_by") != successor_id
    assert child_path.exists(), "foreign complete must not unlink this project's retry"


def test_empty_project_root_successor_is_not_proof(tmp_path: Path) -> None:
    """Missing project_root is UNKNOWN, not a same-project successor."""
    parent_id = "empty-root-parent"
    successor_id = "empty-root-other"
    child_id = "empty-root-parent-retry-deadbeef"
    task_ids = ["t-022"]
    parent_ended = _iso_hours_ago(1)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    child_path = _plant_child_envelope(queue_dir, child_id)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=parent_ended,
        requeue={"child_id": child_id, "requeued_at": parent_ended},
        project_root=tmp_path,
    )
    _write_quota_record(
        successor_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=_iso_days_ago(0),
        state="complete",
        project_root=None,
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    parent = _read_record(parent_id)
    intent = parent["requeue"]
    assert intent.get("disposition") not in {"satisfied", "abandoned", "expired"}
    assert child_path.exists()


def test_ledger_task_ids_advanced_ignores_foreign_project(tmp_path: Path) -> None:
    """Same-class: drain completion authority must not count another repo's t-022."""
    kiln_root = tmp_path / "kiln"
    papers_root = tmp_path / "papers-propulsion"
    kiln_root.mkdir()
    papers_root.mkdir()
    _write_quota_record(
        "papers-t022-done",
        tmp_path=papers_root,
        task_ids=["t-022"],
        ended_at=_iso_days_ago(0),
        state="complete",
        project_root=papers_root,
    )
    complete, advanced, issue = D._ledger_task_ids_advanced(
        ["t-022"],
        self_dispatch_id="kiln-t022-quota",
        entry_created_timestamp_s=1.0,
        self_project_root=str(kiln_root),
    )
    assert complete == 0
    assert advanced == 0
    assert issue == "conclusive"


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
