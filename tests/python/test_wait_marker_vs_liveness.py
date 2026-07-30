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


def _record_with_marker(tmp: Path, *, pid: int, state: str) -> dict:
    """Mirror the real shape: markers live in the status FILE, not the record."""
    status_path = tmp / "d.status.json"
    status_path.write_text(
        json.dumps(
            {
                "dispatch_id": "d",
                "state": state,
                "markers": [{"kind": "COMPLETE", "line": 678, "text": ""}],
            }
        ),
        encoding="utf-8",
    )
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
    case_unconfirmed_liveness_keeps_the_marker_verdict()
    case_pid_only_live_process_is_indeterminate_and_marker_resolves()
    case_explicitly_indeterminate_identity_is_neither_alive_nor_dead()
    print("OK: wait marker-vs-liveness tests pass")


if __name__ == "__main__":
    main()
