#!/usr/bin/env python3
"""A submit prints its OWN claim alerts, not every project's.

A drain pass walks the whole shared queue, so it meets claim carriers belonging
to every project on the box. Observed 2026-08-25: a single `--submit` printed
ten CLAIM-RECOVERY-ALERT rows, all of them `preserve` no-ops for other
projects' dispatches. That wall is what makes the output easy to ignore, and it
lands in the controller's context on every dispatch.

The rule mirrors the one already applied to per-entry drain reasons in
`_report_why_this_entry_did_not_launch`: other entries' reasons belong to
whoever submitted them. Alerts that CHANGED state still print regardless of
owner -- silently dropping a state mutation is the worse failure.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


def _emit(payload: dict, *, focus: str | None) -> str:
    """Run one emission and return what reached stderr."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        if focus is None:
            D._emit_claim_recovery_alert(payload)
        else:
            with D._claim_alert_focus(focus):
                D._emit_claim_recovery_alert(payload)
    return err.getvalue()


def test_foreign_no_op_alert_is_folded_into_one_summary() -> None:
    out = _emit(
        {
            "dispatch_id": "someone-elses-dispatch",
            "action": "preserve_unlinked_ledger_orphan",
            "reason": "claim_carrier_missing_unlinked",
            "state": "running",
        },
        focus="my-dispatch",
    )
    assert "CLAIM-RECOVERY-ALERT" not in out, out
    assert "CLAIM-RECOVERY-SUMMARY" in out, out
    # The count and the reason survive, so the fold stays diagnosable.
    assert '"folded": 1' in out, out
    assert "claim_carrier_missing_unlinked" in out, out


def test_my_own_alert_still_prints_in_full() -> None:
    out = _emit(
        {
            "dispatch_id": "my-dispatch",
            "action": "preserve",
            "reason": "identity_indeterminate",
            "where": "ledger",
        },
        focus="my-dispatch",
    )
    assert "CLAIM-RECOVERY-ALERT" in out, out
    assert "identity_indeterminate" in out, out
    # Nothing was folded, so no summary line is emitted.
    assert "CLAIM-RECOVERY-SUMMARY" not in out, out


def test_state_changing_alert_prints_even_when_foreign() -> None:
    """Quarantine MOVED a carrier. Folding that would hide a real mutation."""
    out = _emit(
        {
            "dispatch_id": "someone-elses-dispatch",
            "action": "quarantine",
            "reason": "unlinked_complete_carrier",
            "path": "/tmp/quarantine/x",
        },
        focus="my-dispatch",
    )
    assert "CLAIM-RECOVERY-ALERT" in out, out
    assert "quarantine" in out, out


def test_without_focus_everything_prints() -> None:
    """The drain daemon and direct callers keep the unfolded behaviour.

    This is what keeps the existing b-065 tests honest: the fold only ever
    narrows what an interactive submit prints.
    """
    out = _emit(
        {
            "dispatch_id": "someone-elses-dispatch",
            "action": "preserve_unlinked_ledger_orphan",
            "reason": "claim_carrier_missing_unlinked",
        },
        focus=None,
    )
    assert "CLAIM-RECOVERY-ALERT" in out, out


def test_many_foreign_alerts_collapse_to_a_single_line() -> None:
    """Ten rows became one. This is the observed defect, in miniature."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with D._claim_alert_focus("my-dispatch"):
            for n in range(10):
                D._emit_claim_recovery_alert(
                    {
                        "dispatch_id": f"foreign-{n}",
                        "action": "preserve",
                        "reason": "identity_indeterminate",
                    }
                )
    out = err.getvalue()
    assert out.count("CLAIM-RECOVERY-ALERT") == 0, out
    assert out.count("CLAIM-RECOVERY-SUMMARY") == 1, out
    assert '"folded": 10' in out, out


def test_focus_is_cleared_even_when_the_drain_raises() -> None:
    """A failed drain must not leave the fold armed for the next caller."""
    with contextlib.suppress(RuntimeError):
        with contextlib.redirect_stderr(io.StringIO()):
            with D._claim_alert_focus("my-dispatch"):
                raise RuntimeError("drain blew up")
    assert D._ALERT_FOCUS is None
    # And a later foreign alert prints, because no focus is active.
    out = _emit(
        {"dispatch_id": "foreign", "action": "preserve", "reason": "x"},
        focus=None,
    )
    assert "CLAIM-RECOVERY-ALERT" in out, out


def test_a_launch_attempt_is_never_folded() -> None:
    """`launched_carrier_cleanup_pending` is spelled "preserve" but a worker ran.

    The first version of the fold matched `action.startswith("preserve")` --
    a lexical proxy for the structural fact "the drain touched nothing". This
    alert is emitted after a launch ATTEMPT, so folding it would hide that a
    worker was started. How an alert is SPELLED is not evidence of what the
    code did.
    """
    out = _emit(
        {
            "dispatch_id": "someone-elses-dispatch",
            "action": "preserve",
            "reason": "launched_carrier_cleanup_pending",
            "where": "ledger",
        },
        focus="my-dispatch",
    )
    assert "CLAIM-RECOVERY-ALERT" in out, out
    assert "CLAIM-RECOVERY-SUMMARY" not in out, out


def test_an_unknown_reason_prints_rather_than_folding() -> None:
    """A newly added alert must be loud until someone allowlists it."""
    out = _emit(
        {
            "dispatch_id": "someone-elses-dispatch",
            "action": "preserve",
            "reason": "some_reason_invented_next_year",
        },
        focus="my-dispatch",
    )
    assert "CLAIM-RECOVERY-ALERT" in out, out


def test_summary_carries_bounded_dispatch_id_samples() -> None:
    """A count alone is unrecoverable; ids make the fold diagnosable.

    Bounded, so the summary cannot grow back into the wall it replaced.
    """
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with D._claim_alert_focus("my-dispatch"):
            for n in range(8):
                D._emit_claim_recovery_alert(
                    {
                        "dispatch_id": f"foreign-{n}",
                        "action": "preserve",
                        "reason": "identity_indeterminate",
                    }
                )
    out = err.getvalue()
    assert "foreign-0" in out, out
    # Capped: the 8th id must not be there.
    assert "foreign-7" not in out, out
    assert '"folded": 8' in out, out


def test_nested_focus_does_not_fold_the_inner_dispatchs_own_alert() -> None:
    """An inner dispatch is ours too, not a foreigner."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with D._claim_alert_focus("outer-dispatch"):
            with D._claim_alert_focus("inner-dispatch"):
                D._emit_claim_recovery_alert(
                    {
                        "dispatch_id": "inner-dispatch",
                        "action": "preserve",
                        "reason": "identity_indeterminate",
                    }
                )
    out = err.getvalue()
    assert "CLAIM-RECOVERY-ALERT" in out, out
    assert "inner-dispatch" in out, out


def test_drain_on_submit_actually_wraps_the_drain() -> None:
    """Binds the WIRING, not just the helper.

    Every other test in this file drives the helpers directly, so deleting or
    mis-scoping the `_claim_alert_focus(...)` wrapper inside `_drain_on_submit`
    would leave them all green. Review flagged exactly that hole.
    """
    calls: list[str] = []

    def fake_drain(_args):
        calls.append("drained")
        D._emit_claim_recovery_alert(
            {
                "dispatch_id": "some-other-projects-dispatch",
                "action": "preserve",
                "reason": "identity_indeterminate",
            }
        )
        return {"failed": 0, "details": []}

    real_drain = D._drain_queue_once
    real_warn = D._warn_if_stranded_without_drainer
    D._drain_queue_once = fake_drain
    D._warn_if_stranded_without_drainer = lambda *a, **k: None
    try:
        args = argparse.Namespace(drain_on_submit=True, dispatch_id="my-dispatch")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            D._drain_on_submit(args, Path("/nonexistent/queue/my-dispatch.json"))
        out = err.getvalue()
    finally:
        D._drain_queue_once = real_drain
        D._warn_if_stranded_without_drainer = real_warn

    assert calls == ["drained"], calls
    assert "CLAIM-RECOVERY-ALERT" not in out, out
    assert "CLAIM-RECOVERY-SUMMARY" in out, out
