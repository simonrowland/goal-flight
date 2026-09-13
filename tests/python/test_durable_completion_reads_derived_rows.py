#!/usr/bin/env python3
"""OPEN dispatch follow-ups and actionable durable-completion refusals."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


def test_a_worker_finished_row_stays_open_when_derived() -> None:
    raw = {"id": "t-1", "done": False}
    assert D._task_row_durably_complete(raw) is False, (
        "precondition: without derived_status the raw row reads incomplete"
    )
    derived = dict(raw, derived_status="worker-finished")
    assert D._task_row_durably_complete(derived) is False


def test_awaiting_review_is_not_durably_complete_when_derived() -> None:
    assert D._task_row_durably_complete({"id": "t-2", "derived_status": "awaiting-review"}) is False


def test_a_genuinely_open_row_stays_incomplete_either_way() -> None:
    """The fix must not turn every row complete."""
    for row in ({"id": "t-3", "done": False},
                {"id": "t-3", "derived_status": "working"},
                {"id": "t-3", "derived_status": "pending"}):
        assert D._task_row_durably_complete(row) is False, row


def test_the_call_site_feeds_the_predicate_derived_rows() -> None:
    """Pin the WIRING, not just the predicate.

    All other task truth still consumes the authoritative deriver.
    """
    src = (ROOT / "scripts" / "goalflight_dispatch.py").read_text(encoding="utf-8")
    head = src.split("_task_row_durably_complete(row)")[0]
    tail = head[-1200:]
    assert "derived_rows_for_items(" in tail, (
        "the by_id feeding _task_row_durably_complete must come from the deriver"
    )


@pytest.fixture
def launch_authority(tmp_path, monkeypatch):
    # Reuse the queue suite's complete environment isolation and task writer.
    from test_dispatch_queue import _isolated_completion_authority, _write_completed_task

    with _isolated_completion_authority(tmp_path):
        _write_completed_task(tmp_path, done_at="2026-01-03T00:00:00+00:00")
        store = D.goalflight_task.TaskStore(tmp_path)
        row = json.loads(store.tasks_path.read_text())
        records = []
        monkeypatch.setattr(D, "_pass_ledger_records", lambda: records)
        monkeypatch.setattr(D, "_find_dispatch_record", lambda _id: None)
        monkeypatch.setattr(store.__class__, "_records_by_task", lambda _self: {})
        monkeypatch.setattr(D.goalflight_ledger, "utc_now", lambda: "2026-01-02T00:00:00+00:00")
        args = SimpleNamespace(dispatch_id="followup", task_ids=[row["id"]], cwd=str(tmp_path), project_root=str(tmp_path))
        yield args, store, row, records


@pytest.mark.parametrize("ledger_time", ["absent", "2026-01-01T00:00:00+00:00"])
def test_open_row_with_prior_complete_dispatch_can_launch(launch_authority, ledger_time, capsys):
    args, store, row, records = launch_authority
    row.update(done=False, done_at=None, closed_at=None, dispatches=[{
        "dispatch_id": "earlier", "state": "worker-finished",
        "marker": {"kind": "COMPLETE", "text": "earlier — finished"},
    }])
    store.tasks_path.write_text(json.dumps(row) + "\n")
    assert store.derived_rows()[0]["derived_status"] == "awaiting-review"
    if ledger_time != "absent":
        records.append({"dispatch_id": "earlier", "project_root": args.project_root,
                        "task_ids": args.task_ids, "state": "complete", "ended_at": ledger_time})

    D._refuse_launch_blocked_by_completion_authority(args)

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("ended_at", [None, "not-a-time"])
def test_open_row_untimed_ledger_refusal_names_record(launch_authority, ended_at, capsys):
    args, store, row, records = launch_authority
    row.update(done=False, done_at=None, closed_at=None)
    store.tasks_path.write_text(json.dumps(row) + "\n")
    records.append({"dispatch_id": "untimed-earlier", "project_root": args.project_root,
                    "task_ids": args.task_ids, "state": "complete", "ended_at": ended_at})

    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(args)
    output = capsys.readouterr()
    refusal = json.loads(output.out.removeprefix(D.DISPATCH_REFUSED_PREFIX))
    assert refusal["permanent"] is True
    assert refusal["reason"] == D.COMPLETION_AUTHORITY_TIMESTAMP_UNREADABLE
    assert str(D.goalflight_ledger.record_path("untimed-earlier", create=False)) in output.err
    assert "ended_at=" + json.dumps(ended_at) in output.err
    assert 'dispatch_id="untimed-earlier"' in output.err
    assert row["id"] in output.err
    assert "2026-01-02T00:00:00+00:00" in output.err
    assert "correct" in output.err


@pytest.mark.parametrize("stamp", ["done", "done_reviewed"])
@pytest.mark.parametrize("completion_time", [None, "2026-01-03T00:00:00+00:00"])
def test_durably_complete_row_refuses_with_store_evidence(launch_authority, stamp, completion_time, capsys):
    args, store, row, _records = launch_authority
    row.update(done=False, done_reviewed=False, done_at=None, closed_at=None)
    row[stamp] = True
    row[stamp + "_at"] = completion_time
    store.tasks_path.write_text(json.dumps(row) + "\n")

    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(args)
    output = capsys.readouterr()
    refusal = json.loads(output.out.removeprefix(D.DISPATCH_REFUSED_PREFIX))
    assert refusal["permanent"] is True
    assert refusal["reason"] == ("task_store:all_complete" if completion_time else D.COMPLETION_AUTHORITY_TIMESTAMP_UNREADABLE)
    assert str(store.tasks_path) in output.err
    assert row["id"] in output.err
    assert stamp + "=true" in output.err
    assert stamp + "_at=" + json.dumps(completion_time) in output.err
    assert "dispatch_id=unbound" in output.err


def test_partial_supersession_names_advanced_record(launch_authority, capsys):
    args, store, row, records = launch_authority
    row.update(done=False, done_at=None, closed_at=None)
    store.tasks_path.write_text(json.dumps(row) + "\n")
    records.append({"dispatch_id": "stopped-earlier", "project_root": args.project_root,
                    "task_ids": args.task_ids, "state": "worker_dead", "ended_at": None})

    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(args)
    output = capsys.readouterr()
    refusal = json.loads(output.out.removeprefix(D.DISPATCH_REFUSED_PREFIX))
    assert refusal["permanent"] is True
    assert refusal["reason"] == "partial_task_supersession"
    assert str(D.goalflight_ledger.record_path("stopped-earlier", create=False)) in output.err
    assert 'state="worker_dead"' in output.err
    assert "ended_at=null" in output.err
    assert row["id"] in output.err
    assert "remaining task IDs" in output.err
