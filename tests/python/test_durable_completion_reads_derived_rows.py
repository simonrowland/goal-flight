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
    assert "goalflight_ledger.py reconcile-outbox" in output.err
    assert str(Path(D.__file__).with_name("goalflight_ledger.py")) in output.err
    assert "correct missing" not in output.err


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
    assert "resume" in output.err.lower()
    assert "reconcile-outbox" not in output.err
    assert "interim" in output.err


@pytest.mark.parametrize("status_kind", ["failed", "healthy", "foreign", "unreadable"])
def test_partial_supersession_surfaces_publication_failure(
    launch_authority, capsys, tmp_path, status_kind,
):
    args, store, row, records = launch_authority
    row.update(done=False, done_at=None, closed_at=None)
    store.tasks_path.write_text(json.dumps(row) + "\n")
    status_path = tmp_path / "earlier.status.json"
    status = {"dispatch_id": "earlier", "state": "running"}
    if status_kind in {"failed", "foreign"}:
        status.update(
            state="terminal_pending", terminal_pending_state="blocked",
            ledger_finalize_error={
                "type": "IntegrityError",
                "message": "CHECK constraint failed: event_type IN ('result', 'blocked')",
            },
        )
    if status_kind == "foreign":
        status["dispatch_id"] = "unrelated"
    status_path.write_text("{broken" if status_kind == "unreadable" else json.dumps(status))
    records.append({
        "dispatch_id": "earlier", "project_root": args.project_root,
        "task_ids": args.task_ids, "state": "running", "terminal_state": "unknown",
        "status_path": str(status_path),
    })

    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(args)
    output = capsys.readouterr()
    refusal = json.loads(output.out.removeprefix(D.DISPATCH_REFUSED_PREFIX))
    assert refusal["reason"] == "partial_task_supersession"
    assert "ended_at=null" in output.err
    if status_kind == "failed":
        assert "terminal publication FAILED" in output.err
        assert "IntegrityError" in output.err
        assert "CHECK constraint failed" in output.err
        assert str(status_path) in output.err
        assert "goalflight_ledger.py reconcile-outbox" in output.err
        assert str(Path(D.__file__).with_name("goalflight_ledger.py")) in output.err
    else:
        assert "terminal publication FAILED" not in output.err
        assert "IntegrityError" not in output.err


def _open_the_task(store, row) -> None:
    row.update(done=False, done_reviewed=False, done_at=None, done_reviewed_at=None, closed_at=None)
    store.tasks_path.write_text(json.dumps(row) + "\n")


def _dead_record(args, dispatch_id: str, **extra) -> dict:
    record = {
        "dispatch_id": dispatch_id,
        "project_root": args.project_root,
        "task_ids": list(args.task_ids),
        "state": "worker_dead",
        "terminal_state": "worker_dead",
        "ended_at": None,
        "worker_cwd": str(Path(args.project_root) / "worktrees" / "seat"),
    }
    record.update(extra)
    return record


def _find_in(records):
    def find(dispatch_id):
        for record in records:
            if str(record.get("dispatch_id") or "") == str(dispatch_id):
                return record
        return None

    return find


def test_resume_of_dead_parent_passes_its_own_hold(launch_authority, monkeypatch):
    """A resume is not blocked by the dead row it is continuing."""
    args, store, row, records = launch_authority
    _open_the_task(store, row)
    records.append(_dead_record(args, "dead-parent"))
    monkeypatch.setattr(D, "_find_dispatch_record", _find_in(records))
    args.parent_dispatch_id = "dead-parent"
    args.dispatch_id = "resume-child"

    D._refuse_launch_blocked_by_completion_authority(args)


def test_second_resume_passes_ancestor_hold(launch_authority, monkeypatch):
    """The grandparent's dead row still holds unless the whole chain is exempt."""
    args, store, row, records = launch_authority
    _open_the_task(store, row)
    records.append(_dead_record(args, "ancestor"))
    records.append(
        _dead_record(args, "middle", parent_dispatch_id="ancestor", state="superseded", terminal_state="superseded")
    )
    monkeypatch.setattr(D, "_find_dispatch_record", _find_in(records))
    args.parent_dispatch_id = "middle"
    args.dispatch_id = "resume-grandchild"

    D._refuse_launch_blocked_by_completion_authority(args)


def test_resume_while_sibling_is_live_stays_refused(launch_authority, monkeypatch):
    """CONTROL: a live sibling is not this resume's lineage, so it still holds."""
    args, store, row, records = launch_authority
    _open_the_task(store, row)
    records.append(_dead_record(args, "dead-parent"))
    records.append(
        {
            "dispatch_id": "live-sibling",
            "project_root": args.project_root,
            "task_ids": list(args.task_ids),
            "state": "running",
            "terminal_state": "unknown",
            "ended_at": None,
        }
    )
    monkeypatch.setattr(D, "_find_dispatch_record", _find_in(records))
    args.parent_dispatch_id = "dead-parent"
    args.dispatch_id = "resume-child"

    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(args)


def test_fresh_dispatch_on_a_dead_hold_stays_refused(launch_authority):
    """CONTROL: exemption is resume-only. A new --task on the same id still refuses."""
    args, store, row, records = launch_authority
    _open_the_task(store, row)
    records.append(_dead_record(args, "dead-parent"))
    args.dispatch_id = "fresh-child"
    assert getattr(args, "parent_dispatch_id", None) in (None, "")

    with pytest.raises(D.DispatchUsageError) as raised:
        D._refuse_launch_blocked_by_completion_authority(args)
    assert str(raised.value) == "partial_task_supersession"


def test_resume_of_one_dead_dispatch_stays_blocked_by_another(launch_authority, monkeypatch):
    """CONTROL: a dead row that is not an ancestor still refuses the resume."""
    args, store, row, records = launch_authority
    _open_the_task(store, row)
    records.append(_dead_record(args, "dead-parent"))
    records.append(_dead_record(args, "other-dead"))
    monkeypatch.setattr(D, "_find_dispatch_record", _find_in(records))
    args.parent_dispatch_id = "dead-parent"
    args.dispatch_id = "resume-child"

    with pytest.raises(D.DispatchUsageError) as raised:
        D._refuse_launch_blocked_by_completion_authority(args)
    assert str(raised.value) == "partial_task_supersession"


def test_lineage_exemption_is_not_applied_by_the_ledger_scan(launch_authority):
    """CONTROL: restore, reconcile, and drain share the scan and must still count the parent."""
    args, _store, _row, records = launch_authority
    records.append(_dead_record(args, "dead-parent"))

    assert D._ledger_task_ids_advanced(
        args.task_ids,
        self_dispatch_id="resume-child",
        self_project_root=args.project_root,
    ) == (0, 1, "conclusive")


@pytest.mark.parametrize("state", ["worker_dead", "superseded", "abandoned"])
def test_dead_hold_refusal_names_resume_not_reconcile(launch_authority, capsys, state):
    """The remedy follows the blocking row's state, not the synthetic decision state."""
    args, store, row, records = launch_authority
    _open_the_task(store, row)
    records.append(
        _dead_record(
            args,
            "stopped-earlier",
            state=state,
            terminal_state=state if state != "abandoned" else "unknown",
        )
    )

    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(args)
    output = capsys.readouterr()
    refusal = json.loads(output.out.removeprefix(D.DISPATCH_REFUSED_PREFIX))
    assert refusal["reason"] == "partial_task_supersession"
    # The machine line is synthetic for every partial hold. The prose must not
    # follow it: a live sibling prints the same "state": "worker_dead".
    assert refusal["state"] == "worker_dead"
    assert "stopped-earlier" in output.err
    assert f'state="{state}"' in output.err
    assert "worker_cwd=" in output.err
    assert "reconcile-outbox" not in output.err
    assert "resume" in output.err.lower()
    assert "interim" in output.err


def test_live_sibling_refusal_keeps_wait_guidance(launch_authority, capsys):
    """CONTROL: a live sibling still says wait, even though the JSON state is worker_dead."""
    args, store, row, records = launch_authority
    _open_the_task(store, row)
    records.append(
        {
            "dispatch_id": "live-sibling",
            "project_root": args.project_root,
            "task_ids": list(args.task_ids),
            "state": "running",
            "terminal_state": "unknown",
            "ended_at": None,
        }
    )

    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(args)
    output = capsys.readouterr()
    refusal = json.loads(output.out.removeprefix(D.DISPATCH_REFUSED_PREFIX))
    assert refusal["state"] == "worker_dead"
    assert refusal["reason"] == "partial_task_supersession"
    assert 'state="running"' in output.err
    assert "remaining task IDs" in output.err
    assert "interim" not in output.err


def test_dispatch_resume_doc_names_the_dead_hold():
    text = (ROOT / "protocols" / "dispatch-resume.md").read_text(encoding="utf-8")
    assert "partial_task_supersession" in text
    assert "worker_dead" in text
    assert "reconcile-outbox" in text
    assert "does not clear" in text
