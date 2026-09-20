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

import pytest  # noqa: E402
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


@pytest.mark.parametrize(
    "existing_intent", [False, True], ids=["first-lodge", "regeneration"]
)
def test_whole_ledger_failure_retains_linked_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    existing_intent: bool,
) -> None:
    parent_id = f"scan-failure-parent-{existing_intent}"
    successor_id = f"scan-failure-successor-{existing_intent}"
    child_id = f"{parent_id}-retry-cafecafe"
    task_ids = ["t-scan-failure"]
    parent_ended = _iso_hours_ago(1)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    requeue = (
        {"child_id": child_id, "requeued_at": parent_ended}
        if existing_intent
        else None
    )
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=parent_ended,
        requeue=requeue,
    )
    _write_quota_record(
        successor_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=_iso_days_ago(0),
        state="complete",
    )
    reads = 0

    def unavailable(*_args: object, **_kwargs: object) -> list[dict]:
        nonlocal reads
        reads += 1
        raise OSError("ledger busy")

    monkeypatch.setattr(L, "read_records", unavailable)

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    ) is False
    parent = _read_record(parent_id)
    assert reads >= 1
    if existing_intent:
        assert parent["requeue"].get("disposition") not in D.REQUEUE_TERMINAL_DISPOSITIONS
    else:
        assert "requeue" not in parent
    assert not list(queue_dir.glob("*.json"))


def test_unreadable_successor_row_retains_linked_work(tmp_path: Path) -> None:
    parent_id = "scan-unreadable-parent"
    child_id = "scan-unreadable-parent-retry-cafecafe"
    task_ids = ["t-scan-unreadable"]
    parent_ended = _iso_hours_ago(1)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=parent_ended,
        requeue={"child_id": child_id, "requeued_at": parent_ended},
    )
    (L.runs_dir() / "concealed-successor.json").write_text(
        "{not-json", encoding="utf-8"
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    ) is False
    parent = _read_record(parent_id)
    assert parent["requeue"].get("disposition") not in D.REQUEUE_TERMINAL_DISPOSITIONS
    assert not D._queue_entry_path(child_id, queue_dir=queue_dir).exists()


def test_unorderable_matching_successor_retains_linked_work(tmp_path: Path) -> None:
    parent_id = "scan-unorderable-parent"
    successor_id = "scan-unorderable-successor"
    child_id = "scan-unorderable-parent-retry-cafecafe"
    task_ids = ["t-scan-unorderable"]
    parent_ended = _iso_hours_ago(1)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=parent_ended,
        requeue={"child_id": child_id, "requeued_at": parent_ended},
    )
    successor = _write_quota_record(
        successor_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at="not-a-timestamp",
        state="complete",
    )
    successor["updated_at"] = "not-a-timestamp"
    L.record_path(successor_id).write_text(
        json.dumps(successor, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    ) is False
    assert not D._queue_entry_path(child_id, queue_dir=queue_dir).exists()


@pytest.mark.parametrize(
    "unavailable_by",
    ["whole-ledger", "unreadable-row", "unorderable-success"],
)
def test_first_lodge_unavailable_scan_expires_from_parent_event_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unavailable_by: str,
) -> None:
    parent_id = f"first-lodge-expiry-{unavailable_by}"
    task_ids = [f"t-{unavailable_by}"]
    parent_ended = "2026-08-01T12:00:00+00:00"
    parent_event_s = datetime.fromisoformat(parent_ended).timestamp()
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=parent_ended,
    )
    if unavailable_by == "whole-ledger":

        def unavailable(*_args: object, **_kwargs: object) -> list[dict]:
            raise OSError("ledger busy")

        monkeypatch.setattr(L, "read_records", unavailable)
    elif unavailable_by == "unreadable-row":
        (L.runs_dir() / "opaque-other-dispatch.json").write_text(
            "{not-json", encoding="utf-8"
        )
    else:
        successor_id = "unorderable-matching-success"
        successor = _write_quota_record(
            successor_id,
            tmp_path=tmp_path,
            task_ids=task_ids,
            ended_at="not-a-timestamp",
            state="complete",
        )
        successor["updated_at"] = "not-a-timestamp"
        L.record_path(successor_id).write_text(
            json.dumps(successor, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    for elapsed_s in (1.0, D.REQUEUE_MAX_AGE_S - 1.0):
        monkeypatch.setattr(D.time, "time", lambda value=elapsed_s: parent_event_s + value)
        assert D._maybe_requeue_terminal_claim(
            _txn(), entry, queue_dir=queue_dir, tail=tail
        ) is False
        assert "requeue" not in _read_record(parent_id)

    # A mutable updated_at at the deadline must not reset the immutable
    # terminal event-time bound.
    parent = _read_record(parent_id)
    deadline_s = parent_event_s + D.REQUEUE_MAX_AGE_S
    parent["updated_at"] = datetime.fromtimestamp(
        deadline_s, tz=timezone.utc
    ).isoformat(timespec="seconds")
    L.record_path(parent_id).write_text(
        json.dumps(parent, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(D.time, "time", lambda: deadline_s)

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    intent = _read_record(parent_id)["requeue"]
    assert intent["disposition"] == "expired"
    assert intent["disposition_reason"] == "max_age"
    assert not list(queue_dir.glob("*.json"))


def test_self_id_unreadable_placeholder_does_not_block_first_lodge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "self-placeholder-parent"
    task_ids = ["t-self-placeholder"]
    parent_ended = _iso_hours_ago(1)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=parent_ended,
    )
    monkeypatch.setattr(
        L,
        "read_records",
        lambda: [
            {
                "schema": L.SCHEMA,
                "dispatch_id": parent_id,
                "state": "unreadable",
                "path": str(L.record_path(parent_id)),
            }
        ],
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    child_id = _read_record(parent_id)["requeue"]["child_id"]
    assert D._queue_entry_path(child_id, queue_dir=queue_dir).exists()


@pytest.mark.parametrize(
    ("unavailable_by", "expected_detail"),
    [
        ("whole-ledger", "ledger_read_failure"),
        ("unreadable-row", "opaque-pending-successor"),
        ("unorderable-success", "unorderable-pending-successor"),
    ],
)
def test_reconciliation_pending_reason_names_successor_scan_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unavailable_by: str,
    expected_detail: str,
) -> None:
    parent_id = f"pending-reason-parent-{unavailable_by}"
    task_ids = [f"t-pending-{unavailable_by}"]
    parent_ended = _iso_hours_ago(1)
    entry, queue_dir, _tail = _claimed_entry(
        tmp_path, parent_id, task_ids=task_ids
    )
    entry.update(
        {
            "queue_launch_started": True,
            "queue_launcher_pid": 99_999_993,
            "queue_launcher_identity": {"pid": 99_999_993},
        }
    )
    parent = _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=parent_ended,
    )
    parent["request_envelope"] = entry
    L.write_record(parent)

    if unavailable_by == "whole-ledger":
        real_read_records = L.read_records
        reads = 0

        def snapshot_then_unavailable(
            *_args: object, **_kwargs: object
        ) -> list[dict]:
            nonlocal reads
            reads += 1
            if reads == 1:
                return real_read_records()
            raise OSError("ledger busy")

        monkeypatch.setattr(L, "read_records", snapshot_then_unavailable)
    elif unavailable_by == "unreadable-row":
        (L.runs_dir() / "opaque-pending-successor.json").write_text(
            "{not-json", encoding="utf-8"
        )
    else:
        successor_id = "unorderable-pending-successor"
        successor = _write_quota_record(
            successor_id,
            tmp_path=tmp_path,
            task_ids=task_ids,
            ended_at="not-a-timestamp",
            state="complete",
        )
        successor["updated_at"] = "not-a-timestamp"
        L.record_path(successor_id).write_text(
            json.dumps(successor, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    recovered = D._reconcile_ledger_prelaunch_orphans(
        queue_dir,
        stale_s=0.0,
    )
    parent_reasons = [
        str(item.get("reason") or "")
        for item in recovered["pending_reasons"]
        if item.get("dispatch_id") == parent_id
    ]
    assert len(parent_reasons) == 1, recovered
    assert "requeue_successor_scan_unavailable" in parent_reasons[0]
    assert expected_detail in parent_reasons[0]


@pytest.mark.parametrize(
    "initial_elapsed_hours", [0, 48], ids=["immediate", "after-48h"]
)
def test_reconciliation_malformed_stable_parent_time_falls_back_to_first_lodge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    initial_elapsed_hours: int,
) -> None:
    parent_id = "malformed-stable-parent-time"
    task_ids = ["t-malformed-stable-parent-time"]
    entry, queue_dir, _tail = _claimed_entry(
        tmp_path, parent_id, task_ids=task_ids
    )
    entry.update(
        {
            "queue_launch_started": True,
            "queue_launcher_pid": 99_999_992,
            "queue_launcher_identity": {"pid": 99_999_992},
        }
    )
    parent = _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at="malformed-ended-at",
    )
    parent["started_at"] = "malformed-started-at"
    parent["updated_at"] = "2026-08-01T12:00:00+00:00"
    parent["request_envelope"] = entry
    L.record_path(parent_id).write_text(
        json.dumps(parent, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (L.runs_dir() / "opaque-during-first-lodge.json").write_text(
        "{not-json", encoding="utf-8"
    )

    parent_updated_s = datetime.fromisoformat(parent["updated_at"]).timestamp()
    now_s = parent_updated_s + initial_elapsed_hours * 60 * 60
    monkeypatch.setattr(D.time, "time", lambda: now_s)
    recovered = D._reconcile_ledger_prelaunch_orphans(
        queue_dir, stale_s=0.0, now=now_s
    )
    intent = _read_record(parent_id).get("requeue")
    assert isinstance(intent, dict)
    assert intent.get("disposition") not in D.REQUEUE_TERMINAL_DISPOSITIONS
    child_id = intent.get("child_id")
    assert isinstance(child_id, str) and child_id
    assert D._queue_entry_path(child_id, queue_dir=queue_dir).exists()
    assert not any(
        item.get("dispatch_id") == parent_id
        for item in recovered["pending_reasons"]
    )


def test_first_lodge_fallback_still_honors_successor_proven_by_second_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "first-lodge-second-scan-parent"
    successor_id = "first-lodge-second-scan-successor"
    task_ids = ["t-first-lodge-second-scan"]
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    parent = _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at="malformed-ended-at",
    )
    parent["started_at"] = "malformed-started-at"
    parent["updated_at"] = "2026-08-01T12:00:00+00:00"
    L.record_path(parent_id).write_text(
        json.dumps(parent, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    successor = _write_quota_record(
        successor_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at="2026-08-02T12:00:00+00:00",
        state="complete",
    )
    opaque = {
        "dispatch_id": "opaque-first-scan",
        "state": "unreadable",
        "path": str(L.runs_dir() / "opaque-first-scan.json"),
    }
    reads = 0

    def unavailable_then_successor() -> list[dict]:
        nonlocal reads
        reads += 1
        return [opaque] if reads == 1 else [successor]

    monkeypatch.setattr(L, "read_records", unavailable_then_successor)
    monkeypatch.setattr(L, "utc_now", lambda: "2026-08-01T12:00:01+00:00")

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    intent = _read_record(parent_id)["requeue"]
    assert reads == 2
    assert intent["disposition"] == "satisfied"
    assert intent["satisfied_by"] == successor_id
    assert not list(queue_dir.glob("*.json"))


@pytest.mark.parametrize(
    ("existing_intent", "expected_attempt"),
    [(False, 1), (True, 2)],
    ids=["first-lodge", "regeneration"],
)
def test_nonfinite_age_anchor_does_not_create_an_unbounded_scan_hold(
    tmp_path: Path,
    existing_intent: bool,
    expected_attempt: int,
) -> None:
    parent_id = f"nonfinite-age-anchor-{existing_intent}"
    child_id = f"{parent_id}-retry-cafecafe"
    task_ids = ["t-nonfinite-age-anchor"]
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=_iso_hours_ago(1) if existing_intent else float("inf"),
        requeue=(
            {
                "child_id": child_id,
                "requeued_at": float("inf"),
                "attempt_count": 1,
            }
            if existing_intent
            else None
        ),
    )
    (L.runs_dir() / "opaque-during-nonfinite-age.json").write_text(
        "{not-json", encoding="utf-8"
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    intent = _read_record(parent_id)["requeue"]
    assert intent["attempt_count"] == expected_attempt
    assert D._queue_entry_path(intent["child_id"], queue_dir=queue_dir).exists()


def test_nonfinite_ended_at_falls_back_to_finite_started_at_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "nonfinite-ended-finite-started"
    task_ids = ["t-nonfinite-ended-finite-started"]
    started_at = "2026-08-01T12:00:00+00:00"
    started_s = datetime.fromisoformat(started_at).timestamp()
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    parent = _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=float("inf"),
    )
    parent["started_at"] = started_at
    L.record_path(parent_id).write_text(
        json.dumps(parent, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (L.runs_dir() / "opaque-during-started-bound.json").write_text(
        "{not-json", encoding="utf-8"
    )

    monkeypatch.setattr(D.time, "time", lambda: started_s + 1)
    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    ) is False
    assert "requeue" not in _read_record(parent_id)

    monkeypatch.setattr(D.time, "time", lambda: started_s + D.REQUEUE_MAX_AGE_S)
    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    intent = _read_record(parent_id)["requeue"]
    assert intent["disposition"] == "expired"
    assert intent["disposition_reason"] == "max_age"


def test_nonfinite_event_field_does_not_hide_finite_completed_successor(
    tmp_path: Path,
) -> None:
    parent_id = "nonfinite-event-field-parent"
    successor_id = "nonfinite-event-field-successor"
    task_ids = ["t-nonfinite-event-field"]
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    parent = _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=float("inf"),
    )
    parent["started_at"] = "2026-08-01T12:00:00+00:00"
    L.record_path(parent_id).write_text(
        json.dumps(parent, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_quota_record(
        successor_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at="2026-08-02T12:00:00+00:00",
        state="complete",
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    intent = _read_record(parent_id)["requeue"]
    assert intent["disposition"] == "satisfied"
    assert intent["satisfied_by"] == successor_id
    assert not list(queue_dir.glob("*.json"))


def test_unknown_intent_age_falls_back_to_regeneration_and_advances_attempts(
    tmp_path: Path,
) -> None:
    parent_id = "unknown-intent-age-regeneration"
    child_id = f"{parent_id}-retry-cafecafe"
    task_ids = ["t-unknown-intent-age-regeneration"]
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=_iso_hours_ago(1),
        requeue={
            "child_id": child_id,
            "requeued_at": "malformed-requeued-at",
            "attempt_count": 1,
        },
    )
    (L.runs_dir() / "opaque-during-regeneration.json").write_text(
        "{not-json", encoding="utf-8"
    )

    for expected_attempt in (2, 3):
        assert D._maybe_requeue_terminal_claim(
            _txn(), entry, queue_dir=queue_dir, tail=tail
        )
        intent = _read_record(parent_id)["requeue"]
        assert intent["attempt_count"] == expected_attempt
        child_path = D._queue_entry_path(child_id, queue_dir=queue_dir)
        assert child_path.exists()
        child_path.unlink()


@pytest.mark.parametrize(
    "existing_intent", [False, True], ids=["first-lodge", "regeneration"]
)
def test_readable_successor_scan_without_match_still_relodges(
    tmp_path: Path,
    existing_intent: bool,
) -> None:
    parent_id = f"scan-readable-parent-{existing_intent}"
    child_id = f"{parent_id}-retry-cafecafe"
    task_ids = ["t-scan-readable"]
    requeued_at = _iso_hours_ago(1)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=requeued_at,
        requeue=(
            {"child_id": child_id, "requeued_at": requeued_at}
            if existing_intent
            else None
        ),
    )

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    lodged_id = _read_record(parent_id)["requeue"]["child_id"]
    if existing_intent:
        assert lodged_id == child_id
    assert D._queue_entry_path(lodged_id, queue_dir=queue_dir).exists()


@pytest.mark.parametrize(
    "unlinked_by",
    ["no-task-ids", "no-project-root", "unorderable-timestamp"],
)
def test_unlinked_work_ignores_successor_scan_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unlinked_by: str,
) -> None:
    parent_id = f"scan-unlinked-{unlinked_by}"
    child_id = f"{parent_id}-retry-cafecafe"
    task_ids = None if unlinked_by == "no-task-ids" else ["t-scan-unlinked"]
    event_at = (
        "not-a-timestamp"
        if unlinked_by == "unorderable-timestamp"
        else _iso_hours_ago(1)
    )
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    if unlinked_by == "no-project-root":
        entry.pop("project_root", None)
        entry["request"].pop("cwd", None)
    record = _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=event_at,
        requeue={"child_id": child_id, "requeued_at": event_at},
        project_root=None if unlinked_by == "no-project-root" else "",
    )
    if unlinked_by == "unorderable-timestamp":
        record["updated_at"] = event_at
        L.record_path(parent_id).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    reads = 0

    def unavailable(*_args: object, **_kwargs: object) -> list[dict]:
        nonlocal reads
        reads += 1
        raise OSError("must not scan unlinked work")

    monkeypatch.setattr(L, "read_records", unavailable)

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    assert reads == 0
    assert D._queue_entry_path(child_id, queue_dir=queue_dir).exists()


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
    task_ids = ["t-age-expiry"]
    requeued_at = _iso_days_ago(3)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=requeued_at,
        requeue={"child_id": child_id, "requeued_at": requeued_at},
    )
    (L.runs_dir() / "unreadable-during-age-expiry.json").write_text(
        "{not-json", encoding="utf-8"
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
    task_ids = ["t-attempt-expiry"]
    requeued_at = _iso_days_ago(0)
    entry, queue_dir, tail = _claimed_entry(tmp_path, parent_id, task_ids=task_ids)
    _write_quota_record(
        parent_id,
        tmp_path=tmp_path,
        task_ids=task_ids,
        ended_at=requeued_at,
        requeue={
            "child_id": child_id,
            "requeued_at": requeued_at,
            "attempt_count": D.REQUEUE_MAX_ATTEMPTS,
            "child_created_at": requeued_at,
        },
    )
    (L.runs_dir() / "unreadable-during-attempt-expiry.json").write_text(
        "{not-json", encoding="utf-8"
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
