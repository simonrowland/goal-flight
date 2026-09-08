#!/usr/bin/env python3
"""Durable-completion must judge DERIVED rows, not raw stamps (CR-C).

`_task_row_durably_complete` consults `derived_status` as its third branch, but
its only call site built `by_id` from `store.load_items(...)`, and load_items
does not emit that field: measured 0 of 758 raw rows carried it. So the derived
branch was dead code on this path and only the raw `done` / `done_reviewed`
stamps decided. 26 rows in the live store flip to complete once the deriver runs.

This is the same defect family as a row carrying `done: True` + `closed_at`
while its derived status is still `working`: a stamp is an INPUT to the
deriver, never a substitute for it.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


def test_a_worker_finished_row_is_complete_only_once_derived() -> None:
    """The row the raw path misses: finished, reviewed-pending, not stamped done."""
    raw = {"id": "t-1", "done": False}
    assert D._task_row_durably_complete(raw) is False, (
        "precondition: without derived_status the raw row reads incomplete"
    )
    derived = dict(raw, derived_status="worker-finished")
    assert D._task_row_durably_complete(derived) is True, (
        "with the deriver run, the same row is durably complete"
    )


def test_awaiting_review_is_durably_complete_when_derived() -> None:
    assert D._task_row_durably_complete({"id": "t-2", "derived_status": "awaiting-review"}) is True


def test_a_genuinely_open_row_stays_incomplete_either_way() -> None:
    """The fix must not turn every row complete."""
    for row in ({"id": "t-3", "done": False},
                {"id": "t-3", "derived_status": "working"},
                {"id": "t-3", "derived_status": "pending"}):
        assert D._task_row_durably_complete(row) is False, row


def test_the_call_site_feeds_the_predicate_derived_rows() -> None:
    """Pin the WIRING, not just the predicate.

    The predicate was always correct; the caller handed it rows the deriver had
    never touched. Passing in isolation is exactly what hid this, so assert the
    call site runs the deriver.
    """
    src = (ROOT / "scripts" / "goalflight_dispatch.py").read_text(encoding="utf-8")
    head = src.split("_task_row_durably_complete(row)")[0]
    tail = head[-1200:]
    assert "derived_rows_for_items(" in tail, (
        "the by_id feeding _task_row_durably_complete must come from the deriver"
    )
