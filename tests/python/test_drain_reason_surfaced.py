"""A queued dispatch should say WHY it is queued, not merely that it is.

The drain already computes a per-entry reason and returns it. Printing only a
failure count discards that, so a controller cannot tell a capacity wait from a
permanently parked entry. Measured 2026-08-24: three dispatches sat at `queued`
behind `active_queue_carrier` — claim carriers whose claimant pids were all
dead — while the submit output said only "1 drain failure(s)". Diagnosing it
required importing the dispatch module and calling the drain by hand.

The reason existed inside the process the whole time.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch as gd  # noqa: E402


def _args(dispatch_id: str | None):
    return types.SimpleNamespace(dispatch_id=dispatch_id)


def test_reason_for_this_dispatch_is_printed(capsys) -> None:
    payload = {"details": [
        {"dispatch_id": "other-one", "reason": "capacity_wait"},
        {"dispatch_id": "mine", "reason": "active_queue_carrier"},
    ]}
    gd._report_why_this_entry_did_not_launch(_args("mine"), payload)
    err = capsys.readouterr().err
    assert "mine not launched: active_queue_carrier" in err


def test_detail_is_included_when_present(capsys) -> None:
    """process_evidence is the field that distinguishes dead from indeterminate."""
    payload = {"details": [{
        "dispatch_id": "mine",
        "reason": "worker_live_or_indeterminate",
        "process_evidence": "worker:dead:dead,claimant:dead:dead",
    }]}
    gd._report_why_this_entry_did_not_launch(_args("mine"), payload)
    err = capsys.readouterr().err
    assert "worker:dead:dead,claimant:dead:dead" in err


def test_other_entries_reasons_are_not_printed(capsys) -> None:
    """Another submitter's parked entry is not this caller's problem, and a wall
    of them is what makes the existing alert output easy to ignore."""
    payload = {"details": [
        {"dispatch_id": "someone-else", "reason": "nonlocal_or_unknown_host"},
        {"dispatch_id": "third-party", "reason": "active_queue_carrier"},
    ]}
    gd._report_why_this_entry_did_not_launch(_args("mine"), payload)
    assert capsys.readouterr().err == ""


def test_launched_entry_prints_nothing(capsys) -> None:
    """Absent from `skipped` means it launched; silence is correct there."""
    gd._report_why_this_entry_did_not_launch(_args("mine"), {"details": []})
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("dispatch_id", [None, "", "   "])
def test_missing_dispatch_id_is_a_no_op(capsys, dispatch_id) -> None:
    payload = {"details": [{"dispatch_id": "mine", "reason": "active_queue_carrier"}]}
    gd._report_why_this_entry_did_not_launch(_args(dispatch_id), payload)
    assert capsys.readouterr().err == ""


def test_absent_reason_still_reports_rather_than_crashing(capsys) -> None:
    """A skipped entry with no reason is still worth reporting as skipped."""
    gd._report_why_this_entry_did_not_launch(
        _args("mine"), {"details": [{"dispatch_id": "mine"}]})
    err = capsys.readouterr().err
    assert "mine not launched: unspecified" in err


def test_missing_details_key_is_tolerated(capsys) -> None:
    gd._report_why_this_entry_did_not_launch(_args("mine"), {})
    assert capsys.readouterr().err == ""


def test_uses_the_real_payload_key_not_an_invented_one() -> None:
    """Pin the key against the shape `_drain_queue_once` actually returns.

    The first version of this reporter read a `skipped` key the payload has never
    contained. It reported nothing, and its tests passed because they built the
    payload from the same wrong assumption — a diagnostic that looks present and
    is inert, which is worse than none because silence reads as "no reason to
    give". This asserts against the real key list.
    """
    import inspect
    import goalflight_dispatch as gd_mod
    src = inspect.getsource(gd_mod._report_why_this_entry_did_not_launch)
    assert 'payload.get("details")' in src, "must read the real per-entry key"
    assert '"skipped"' not in src, "must not read a key the payload does not have"


def test_not_before_is_surfaced_as_detail(capsys) -> None:
    """A scheduled retry is waiting for a time; say which."""
    gd._report_why_this_entry_did_not_launch(_args("mine"), {"details": [
        {"dispatch_id": "mine", "reason": "not_before",
         "not_before": "2026-08-29T01:00:00-04:00"},
    ]})
    err = capsys.readouterr().err
    assert "not_before" in err and "2026-08-29" in err
