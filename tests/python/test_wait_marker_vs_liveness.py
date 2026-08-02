#!/usr/bin/env python3
"""A terminal marker must not outrank a confirmed-live worker.

Live failure this pins: `--wait` reported `1/1 terminal ... [COMPLETE]` for a
worker that was still running. Its status file carried
`{"kind": "COMPLETE", "line": 678, "text": ""}` -- produced by the bare sign-off
pattern matching `done`, the loop terminator of a shell script the worker had
echoed into its own tail -- while `state` was `running` and `worker_alive` true.

The watcher was not fooled; it kept the run `running`. Only `done_code` was: it
returned 0 on the marker before consulting liveness at all. A controller acting
on that verdict gates, commits and pushes unfinished work.

Rule: a marker never outranks CONFIRMED liveness. Unconfirmed liveness keeps the
marker verdict, because failing to observe a process is not evidence it lives --
the opposite choice would convert every unobservable worker into a hang.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_status  # noqa: E402


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _record_with_marker(
    tmp: Path,
    *,
    pid: int,
    state: str,
    kind: str = "COMPLETE",
    final: bool = True,
) -> dict:
    """Mirror the real shape: markers live in the status FILE, not the record.

    ``final`` controls whether the watcher promoted the marker to
    ``terminal_marker`` -- i.e. whether the line was still the last non-empty
    line when it re-checked. A scraped-but-not-final marker appears only in
    ``markers``/``last_marker``.
    """
    status: dict = {
        "dispatch_id": "d",
        "state": state,
        "markers": [{"kind": kind, "line": 678, "text": ""}],
        "last_marker": {"kind": kind, "line": 678, "text": ""},
    }
    if final:
        status["terminal_marker"] = {"kind": kind, "line": 678, "text": ""}
    status_path = tmp / "d.status.json"
    status_path.write_text(json.dumps(status), encoding="utf-8")
    return {
        "dispatch_id": "d",
        "state": state,
        # The real row carried classification "expected_live" with the status
        # file's state at "running" -- reproduce that pairing, not a synthetic one.
        "classification": goalflight_status._LIVE_CLASS,
        "status_path": str(status_path),
        "worker_pid": pid,
    }


def case_marker_does_not_beat_a_live_worker() -> None:
    with tempfile.TemporaryDirectory() as td:
        record = _record_with_marker(Path(td), pid=os.getpid(), state="running")
        # os.getpid() is this test process: definitively alive.
        code = goalflight_status.done_code(record, worker_alive=True)
        assert_true(
            "confirmed-live worker with a COMPLETE marker is NOT done",
            code == 1,
        )


def case_marker_resolves_a_dead_worker() -> None:
    with tempfile.TemporaryDirectory() as td:
        record = _record_with_marker(Path(td), pid=os.getpid(), state="running")
        code = goalflight_status.done_code(record, worker_alive=False)
        assert_true(
            "dead worker with a COMPLETE marker is done",
            code == 0,
        )


def case_attention_markers_wake_the_controller_while_the_worker_lives() -> None:
    """The signals --wait exists to deliver must not be suppressed by liveness.

    A worker parked on USER-CONFIRM:/USER-NEED:/BLOCKED: is alive PRECISELY
    because it stopped to wait for the controller. The rule that liveness
    outranks a marker is right for COMPLETE: (a running process contradicts
    "I am finished") and inverted here (a running process confirms "I need
    you"). Applying it to all terminal markers left a controller blocked on
    --wait until the wait timeout while its worker sat asking for approval.

    Pinned per marker rather than once, because the bug was introduced by a
    change that treated the whole terminal-marker set as one thing.
    """
    # FAILED is here, not with the completion markers: the marker contract says
    # FAILED stops the dispatch loop and surfaces to the controller. A worker
    # that reports a real failure and then hangs in teardown has still failed --
    # the live process does not invalidate the report.
    for kind in ("USER-CONFIRM", "USER-NEED", "BLOCKED", "FAILED"):
        with tempfile.TemporaryDirectory() as td:
            record = _record_with_marker(
                Path(td), pid=os.getpid(), state="running", kind=kind
            )
            assert_true(
                f"live worker with a {kind} marker IS terminal for --wait",
                goalflight_status.done_code(record, worker_alive=True) == 0,
            )


def case_acp_park_states_wake_the_controller() -> None:
    """The path that actually carries USER-CONFIRM in production.

    ACP does not write the watcher's marker shape at all: its ``last_marker``
    is ``{"USER-CONFIRM": text}`` -- the kind is the KEY, with no "kind" field
    -- and ``markers`` is a dict of lists, not a list. A fix written against
    the watcher shape is dead code here, which is exactly what happened and
    what a watcher-shaped test failed to catch.

    So this pins the STATE, which the ACP runner writes from a real protocol
    event and which no marker parser has to understand.
    """
    for state in ("running_user_confirm", "awaiting_user_confirm", "awaiting_permission"):
        record = {
            "dispatch_id": "d",
            "state": state,
            "classification": goalflight_status._LIVE_CLASS,
            "markers": {"USER-CONFIRM": ["may I run the migration?"]},
            "last_marker": {"USER-CONFIRM": "may I run the migration?"},
        }
        assert_true(
            f"live ACP worker parked in {state} IS terminal for --wait",
            goalflight_status.done_code(record, worker_alive=True) == 0,
        )
    running = {
        "dispatch_id": "d",
        "state": "running",
        "classification": goalflight_status._LIVE_CLASS,
        "markers": {"STATUS": ["writing tests"]},
        "last_marker": {"STATUS": "writing tests"},
    }
    assert_true(
        "a genuinely working ACP worker is NOT terminal",
        goalflight_status.done_code(running, worker_alive=True) == 1,
    )


def case_a_nonfinal_attention_marker_does_not_end_the_wait() -> None:
    """`BLOCKED: example` mid-explanation must not tear down the monitor.

    Attention markers bypass the liveness check, so without a position gate any
    worker that merely PRINTS one -- in a diagnostic, a quoted log, a plan --
    would end its controller's wait while it kept working. Position is the
    evidence that the worker actually stopped.
    """
    for kind in ("BLOCKED", "USER-CONFIRM", "USER-NEED"):
        with tempfile.TemporaryDirectory() as td:
            record = _record_with_marker(
                Path(td), pid=os.getpid(), state="running", kind=kind, final=False
            )
            assert_true(
                f"a scraped-but-not-final {kind} marker does NOT end the wait",
                goalflight_status.done_code(record, worker_alive=True) != 0,
            )


def case_completion_markers_still_lose_to_a_live_worker() -> None:
    """The other half of the split -- guards against over-correcting.

    Widening the attention set to cover COMPLETE/READY/RESULT/FAILED would make
    every mid-run marker echo report a working worker as finished, which is the
    false-done bug the liveness rule was added to fix.
    """
    for kind in ("COMPLETE", "READY", "RESULT"):
        with tempfile.TemporaryDirectory() as td:
            record = _record_with_marker(
                Path(td), pid=os.getpid(), state="running", kind=kind
            )
            assert_true(
                f"live worker with a {kind} marker is NOT terminal",
                goalflight_status.done_code(record, worker_alive=True) == 1,
            )


def case_unconfirmed_liveness_keeps_the_marker_verdict() -> None:
    """No pid to check -> cannot confirm alive -> marker still resolves.

    Guards the failure direction we must NOT introduce: treating unobservable
    workers as live would hang every wait that cannot see its process.
    """
    with tempfile.TemporaryDirectory() as td:
        record = _record_with_marker(Path(td), pid=0, state="running")
        record.pop("worker_pid", None)
        assert_true(
            "unconfirmable worker with a COMPLETE marker is still done",
            goalflight_status.done_code(record) == 0,
        )


def case_pid_only_live_process_is_indeterminate_and_marker_resolves() -> None:
    """A live PID without identity may belong to an unrelated recycled process."""
    with tempfile.TemporaryDirectory() as td:
        record = _record_with_marker(Path(td), pid=os.getpid(), state="running")
        assert_true(
            "pid-only live process is indeterminate",
            goalflight_status._wait_worker_liveness(record)
            == goalflight_status._WAIT_LIVENESS_INDETERMINATE,
        )
        assert_true(
            "pid-only indeterminate ownership does not suppress COMPLETE",
            goalflight_status.done_code(record) == 0,
        )


def case_explicitly_indeterminate_identity_is_neither_alive_nor_dead() -> None:
    record = {
        "worker_pid": os.getpid(),
        "worker_identity": {"creation_time": 123},
    }
    saved = goalflight_status.goalflight_ledger.identity_matches
    goalflight_status.goalflight_ledger.identity_matches = (
        lambda _record: (False, "identity_indeterminate")
    )
    try:
        assert_true(
            "explicitly indeterminate identity stays tri-state indeterminate",
            goalflight_status._wait_worker_liveness(record)
            == goalflight_status._WAIT_LIVENESS_INDETERMINATE,
        )
        assert_true(
            "indeterminate identity is not confirmed alive",
            not goalflight_status._wait_worker_confirmed_alive(record),
        )
        assert_true(
            "indeterminate identity is not confirmed dead",
            not goalflight_status._wait_worker_confirmed_dead(record),
        )
    finally:
        goalflight_status.goalflight_ledger.identity_matches = saved


def main() -> None:
    case_marker_does_not_beat_a_live_worker()
    case_marker_resolves_a_dead_worker()
    case_attention_markers_wake_the_controller_while_the_worker_lives()
    case_acp_park_states_wake_the_controller()
    case_a_nonfinal_attention_marker_does_not_end_the_wait()
    case_completion_markers_still_lose_to_a_live_worker()
    case_unconfirmed_liveness_keeps_the_marker_verdict()
    case_pid_only_live_process_is_indeterminate_and_marker_resolves()
    case_explicitly_indeterminate_identity_is_neither_alive_nor_dead()
    print("OK: wait marker-vs-liveness tests pass")


if __name__ == "__main__":
    main()
