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


def test_no_entry_for_this_dispatch_prints_nothing(capsys) -> None:
    """Nothing to explain when the drain said nothing about this dispatch."""
    gd._report_why_this_entry_did_not_launch(_args("mine"), {"details": []})
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("entry", [
    # The two `state: launched` sites. One carries no reason at all, which a
    # reason-based test reads as "no reason given" and invents one for.
    {"dispatch_id": "mine", "state": "launched"},
    {"dispatch_id": "mine", "state": "launched", "reason": "worker_record_present"},
    # BOTH ledger-confirmed-launch-with-pending-carrier shapes. These are
    # structurally identical -- same `if ledger_confirmed:` block, same
    # preceding `_alert_launched_carrier_pending`, same accounting comment --
    # yet the first version of this fix covered only the first of them. The
    # second contains no form of the word "launch", so the tense-based pin that
    # shipped with it could not have caught the omission.
    {"dispatch_id": "mine", "state": "claimed",
     "reason": "launched_carrier_cleanup_pending"},
    {"dispatch_id": "mine", "state": "claimed",
     "reason": "worker_record_present_carrier_cleanup_pending"},
])
def test_a_launched_entry_is_never_reported_as_not_launched(capsys, entry) -> None:
    """Stating the opposite of what happened is worse than saying nothing.

    The predecessor of this test passed an EMPTY details list, so it never
    exercised a launched entry and stayed green while all three shapes printed
    "not launched". Measured 2026-08-24: a probe dispatch printed "not launched:
    unspecified" while running as pid 83652.
    """
    gd._report_why_this_entry_did_not_launch(_args("mine"), {"details": [entry]})
    assert capsys.readouterr().err == ""


def _carrier_alerts_and_their_appends(module):
    """Yield (alert_lineno, literal_or_None) for EVERY carrier-pending alert.

    `_alert_launched_carrier_pending` marks "launch confirmed, carrier not
    cleared", so the detail the drain appends after it is by construction a
    confirmed launch. Keying on that control-flow fact rather than on how the
    reason is spelled is the whole point: the shape an earlier version of this
    pin missed, `worker_record_present_carrier_cleanup_pending`, contains no
    form of the word "launch".

    Every alert is yielded, with None when no literal append follows it in the
    same suite, so a route that stops appending cannot silently drop out of the
    checked set. Searching the remainder of the suite rather than a fixed
    two-statement window, for the same reason -- but stopping at any
    control-flow terminator, since an append after a `continue` or `return`
    belongs to a different path and matching it would be a false pass.

    Known limits, stated rather than papered over: only literal dict appends
    are visible, and a confirmed-launch producer that emits NO alert (the two
    `state="launched"` sites) cannot be reached structurally from here -- those
    are covered by value examples instead. `_is_alert` also matches only a bare
    call to the exact name, so wrapping or aliasing the alert would evade it.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))

    def _is_alert(stmt):
        return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id == "_alert_launched_carrier_pending")

    def _is_terminator(stmt):
        return isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break))

    def _append_literal(stmt):
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            return None
        call = stmt.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "append"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "details"
                and call.args and isinstance(call.args[0], ast.Dict)):
            return None
        return {k.value: v.value
                for k, v in zip(call.args[0].keys, call.args[0].values)
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)}

    claimed: set[int] = set()
    for node in ast.walk(tree):
        # `body` alone misses `else:`, loop-`else:` and `finally:` suites, so a
        # route moved into one would silently leave the checked set.
        for attr in ("body", "orelse", "finalbody"):
            suite = getattr(node, attr, None)
            if not isinstance(suite, list):
                continue
            for i, stmt in enumerate(suite):
                if not _is_alert(stmt):
                    continue
                match = None
                for follower in suite[i + 1:]:
                    if _is_terminator(follower):
                        break
                    literal = _append_literal(follower)
                    if literal is not None:
                        # One append answers for one alert; letting two alerts
                        # share a match would hide a route that appends nothing.
                        if id(follower) in claimed:
                            break
                        claimed.add(id(follower))
                        match = literal
                        break
                yield stmt.lineno, match


def test_every_carrier_pending_alert_yields_a_confirmed_launch_detail() -> None:
    """A new confirmed-launch route must fail HERE, not become a false line.

    Checks EVERY alert rather than asserting a global match count: a count
    threshold is satisfied by the routes that already work, so adding a third
    route that appends nothing -- or appends something unclassified -- would
    slip past it.
    """
    import goalflight_dispatch as gd_mod

    found = list(_carrier_alerts_and_their_appends(gd_mod))
    # Zero matches would make this vacuous, and a vacuous test is exactly how
    # the defect this file exists for survived its first two fixes.
    assert len(found) >= 2, (
        f"expected at least the two carrier-pending routes, found {found!r} -- "
        "if the drain was restructured, re-derive this pin rather than relaxing it"
    )
    bad = [(ln, lit) for ln, lit in found
           if lit is None or not gd_mod._drain_detail_is_a_confirmed_launch(lit)]
    assert not bad, (
        "every detail appended after the launched-carrier alert is a confirmed "
        f"launch, but these are unclassified or absent: {bad}"
    )


@pytest.mark.parametrize("state", ["complete", "released"])
def test_success_terminal_states_are_still_reported(capsys, state) -> None:
    """Suppressing these would trade a misleading line for a vanished failure.

    A revision silenced `complete`/`released`, reasoning that a finished
    dispatch should not be called "not launched". Dispatch ids are reusable
    once terminal, and completion authority resolves a terminal record by
    dispatch id WITHOUT comparing `queue_launch_token` -- so a `complete` from
    an EARLIER attempt can be attached to a new one, and suppressing it turns a
    pre-spawn refusal of the CURRENT attempt into total silence.

    The residual is accepted deliberately: a genuinely finished dispatch can
    still print a misleading line. Misleading and loud beats silent and wrong.
    """
    import goalflight_dispatch as gd_mod
    assert not gd_mod._drain_detail_is_a_confirmed_launch({"state": state})
    gd._report_why_this_entry_did_not_launch(
        _args("mine"),
        {"details": [{"dispatch_id": "mine", "state": state,
                      "reason": "existing_terminal_record"}]},
    )
    assert "mine not launched: existing_terminal_record" in capsys.readouterr().err


@pytest.mark.parametrize("entry", [
    # Launch subprocess timed out: `failed` is incremented and nothing confirms
    # a worker started. Classifying this as a launch would silence a real
    # failure -- the opposite error to the one this module was fixed for, and
    # the more dangerous direction.
    {"dispatch_id": "mine", "state": "claimed",
     "reason": "launch_timeout_pending_ledger"},
    {"dispatch_id": "mine", "state": "queued", "reason": "capacity_unavailable"},
    {"dispatch_id": "mine", "state": "claimed", "reason": "active_queue_carrier"},
])
def test_an_unconfirmed_launch_is_still_reported(capsys, entry) -> None:
    gd._report_why_this_entry_did_not_launch(_args("mine"), {"details": [entry]})
    err = capsys.readouterr().err
    assert f"mine not launched: {entry['reason']}" in err


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
