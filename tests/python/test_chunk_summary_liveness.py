"""Regression tests for chunk-summary worker liveness reconciliation."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Callable, TypeVar

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_chunk_summary as summary  # noqa: E402
import goalflight_status as status  # noqa: E402

T = TypeVar("T")


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _with_identity(ok: bool, reason: str, fn: Callable[[], T]) -> T:
    saved = status.goalflight_ledger.identity_matches
    status.goalflight_ledger.identity_matches = lambda _record: (ok, reason)
    try:
        return fn()
    finally:
        status.goalflight_ledger.identity_matches = saved


def _record(
    *,
    dispatch_id: str,
    state: str,
    tail: Path,
    status_path: Path,
) -> dict:
    return {
        "dispatch_id": dispatch_id,
        "state": state,
        "classification": state,
        "terminal_state": status.dispatch_states.terminal_state_for(state),
        "worker_pid": 4242,
        "worker_identity": {"lstart": "Tue Jun  9 09:00:00 2026", "comm": "python3"},
        "stdout_path": str(tail),
        "status_path": str(status_path),
        "project_root": str(ROOT),
    }


def _write_dispatch(state_dir: Path, record: dict, status_payload: dict) -> None:
    _write_json(state_dir / "runs.d" / f"{record['dispatch_id']}.json", record)
    _write_json(Path(record["status_path"]), status_payload)


def test_idle_detached_identity_live_reads_running_wait() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-summary-live-") as d:
        base = Path(d)
        state_dir = base / "state"
        tail = base / "idle.tail"
        status_path = base / "idle.status.json"
        tail.write_text("still researching\n", encoding="utf-8")
        record = _record(dispatch_id="idle-live", state="idle_timeout", tail=tail, status_path=status_path)
        _write_dispatch(
            state_dir,
            record,
            {"state": "idle_timeout", "worker_pid": 4242, "tail_path": str(tail), "seconds_since_event": 5},
        )

        payload = _with_identity(True, "live", lambda: summary.summarize("idle-live", state_dir))

    assert_eq("idle detached live state", payload["state"], "running")
    assert_eq("idle detached live worker", payload["worker_pid_alive"], True)
    assert_eq("idle detached live hint", payload["decision_hint"], "wait")


def test_dead_worker_complete_tail_reads_complete() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-summary-complete-") as d:
        base = Path(d)
        state_dir = base / "state"
        tail = base / "complete.tail"
        status_path = base / "complete.status.json"
        tail.write_text("finished\nCOMPLETE: done\n", encoding="utf-8")
        record = _record(dispatch_id="dead-complete", state="worker_dead", tail=tail, status_path=status_path)
        _write_dispatch(
            state_dir,
            record,
            {"state": "worker_dead", "worker_pid": 4242, "tail_path": str(tail)},
        )

        payload = _with_identity(False, "dead", lambda: summary.summarize("dead-complete", state_dir))

    assert_eq("dead worker complete tail state", payload["state"], "complete")
    assert_eq("dead worker complete tail marker", payload["last_marker"], "COMPLETE")
    assert_eq("dead worker complete tail hint", payload["decision_hint"], "done")


def test_recycled_pid_identity_mismatch_is_not_alive() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-summary-reuse-") as d:
        base = Path(d)
        record = _record(
            dispatch_id="pid-reuse",
            state="idle_timeout",
            tail=base / "reuse.tail",
            status_path=base / "reuse.status.json",
        )
        live = _with_identity(False, "pid_reused_lstart", lambda: summary.worker_alive_at_read_time(record))

    assert_eq("identity mismatch is not live", live, False)


def test_missing_identity_falls_back_to_pid_liveness() -> None:
    saved = status.goalflight_compat.pid_alive
    try:
        status.goalflight_compat.pid_alive = lambda _pid: True
        live = summary.worker_alive_at_read_time(
            {
                "dispatch_id": "pid-only",
                "state": "idle_timeout",
                "classification": "idle_timeout",
                "worker_pid": 4242,
            }
        )
    finally:
        status.goalflight_compat.pid_alive = saved

    assert_eq("pid-only liveness fallback", live, True)


def test_summary_agrees_with_status_tail_reconciled_complete() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-summary-agree-") as d:
        base = Path(d)
        state_dir = base / "state"
        tail = base / "agree.tail"
        status_path = base / "agree.status.json"
        tail.write_text("READY: docs-private/research/findings.md\ntrailing summary\n", encoding="utf-8")
        record = _record(dispatch_id="agree-complete", state="worker_dead", tail=tail, status_path=status_path)
        _write_dispatch(
            state_dir,
            record,
            {"state": "worker_dead", "worker_pid": 4242, "tail_path": str(tail)},
        )

        def check() -> tuple[dict, dict]:
            return status._reconcile_output_tail_record(record), summary.summarize("agree-complete", state_dir)

        reconciled, payload = _with_identity(False, "dead", check)

    assert_eq("status reconciled classification", reconciled.get("classification"), "complete")
    assert_eq("summary reconciled state", payload["state"], "complete")
    assert_eq("summary reconciled marker", payload["last_marker"], "READY")


def main() -> None:
    tests = [
        test_idle_detached_identity_live_reads_running_wait,
        test_dead_worker_complete_tail_reads_complete,
        test_recycled_pid_identity_mismatch_is_not_alive,
        test_missing_identity_falls_back_to_pid_liveness,
        test_summary_agrees_with_status_tail_reconciled_complete,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_chunk_summary_liveness.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
