"""Regression tests for the bounded-on-crash --wait primitive.

`goalflight_status.py --wait` must NEVER poll to the wait-timeout on a crashed /
premature-exited worker: an ambiguous/stale dispatch whose worker is confirmed
dead resolves to a terminal `worker_dead` verdict after a short grace, so the call
exits and the controller is bumped. It must equally NOT kill a genuinely-running
worker, and must keep trusting reconcile-from-output (a completed-but-pid-dead row
stays `complete`, not `worker_dead`).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat as compat  # noqa: E402
import goalflight_status as status  # noqa: E402


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _payload(*records: dict) -> dict:
    return {"dispatch": {"records": list(records)}}


def _row(payload: dict, dispatch_id: str, *, now: float, grace: float,
         dead_since: dict[str, float]) -> dict:
    rows = status._wait_snapshot(
        payload, [dispatch_id], dead_since=dead_since, now=now, grace=grace
    )
    return rows[0]


def test_crashed_worker_resolves_worker_dead_after_grace_not_before() -> None:
    # Ambiguous/stale class + no live worker (unknown_no_pid has no pid at all).
    rec = {"dispatch_id": "crash", "classification": "unknown_no_pid"}
    payload = _payload(rec)
    dead_since: dict[str, float] = {}

    # done_code must classify this as ambiguous (2), not terminal, not live.
    assert_eq("done_code ambiguous", status.done_code(rec), 2)

    # Within grace: not yet terminal, but already tracked as dead.
    early = _row(payload, "crash", now=0.0, grace=90.0, dead_since=dead_since)
    assert_eq("early not terminal", early["terminal"], False)
    assert_eq("early state", early["state"], "worker_dead_pending")
    assert_true("dead_since armed", "crash" in dead_since)

    # Past grace (same dead_since carried across polls): terminal worker_dead.
    late = _row(payload, "crash", now=90.0, grace=90.0, dead_since=dead_since)
    assert_eq("late terminal", late["terminal"], True)
    assert_eq("late state", late["state"], "worker_dead")


def test_stale_dead_with_dead_pid_resolves_worker_dead() -> None:
    rec = {"dispatch_id": "stale", "classification": "stale_dead", "worker_pid": 2147480000}
    payload = _payload(rec)
    saved = compat.pid_alive
    compat.pid_alive = lambda pid: False  # type: ignore[assignment]
    try:
        assert_eq("done_code stale=2", status.done_code(rec), 2)
        assert_true("confirmed dead", status._wait_worker_confirmed_dead(rec))
        row = _row(payload, "stale", now=100.0, grace=90.0, dead_since={"stale": 0.0})
        assert_eq("stale terminal", row["terminal"], True)
        assert_eq("stale state", row["state"], "worker_dead")
    finally:
        compat.pid_alive = saved  # type: ignore[assignment]


def test_live_but_ambiguous_worker_is_not_killed() -> None:
    # Ambiguous class but the worker pid is alive -> must keep waiting forever
    # (bounded only by --wait-timeout), never flip to worker_dead.
    rec = {"dispatch_id": "live", "classification": "unknown", "worker_pid": 4242}
    payload = _payload(rec)
    saved = compat.pid_alive
    compat.pid_alive = lambda pid: True  # type: ignore[assignment]
    try:
        assert_true("not confirmed dead", not status._wait_worker_confirmed_dead(rec))
        dead_since: dict[str, float] = {}
        row = _row(payload, "live", now=10_000.0, grace=90.0, dead_since=dead_since)
        assert_eq("live not terminal even past grace", row["terminal"], False)
        assert_true("dead_since not armed for live worker", "live" not in dead_since)
    finally:
        compat.pid_alive = saved  # type: ignore[assignment]


def test_completed_pid_dead_stays_complete_trust_clause() -> None:
    # reconcile-from-output already promoted this row to complete; --wait must
    # report complete, NOT worker_dead, even though the pid is gone.
    rec = {"dispatch_id": "done", "classification": "complete", "worker_pid": 2147480000}
    payload = _payload(rec)
    dead_since: dict[str, float] = {}
    row = _row(payload, "done", now=10_000.0, grace=90.0, dead_since=dead_since)
    assert_eq("complete terminal", row["terminal"], True)
    assert_eq("complete state", row["state"], "complete")
    assert_true("dead_since untouched for complete", "done" not in dead_since)


def test_wait_returns_bounded_on_crash_not_at_timeout() -> None:
    # End-to-end: a crashed dispatch must make wait_for_dispatches RETURN well
    # before the wait-timeout. Poison: without the anti-hang clause this row never
    # goes terminal and the call would run the full 30s timeout then return 1.
    payload = _payload({"dispatch_id": "z", "classification": "unknown_no_pid"})
    saved_status, saved_scope = status.status_payload, status.scope_payload
    status.status_payload = lambda: payload  # type: ignore[assignment]
    status.scope_payload = lambda p, root: p  # type: ignore[assignment]
    try:
        t0 = time.monotonic()
        rc = status.wait_for_dispatches(
            ["z"], project_root=None, timeout_s=30.0, poll_s=0.05,
            crash_grace_s=0.0, json_output=True,
        )
        elapsed = time.monotonic() - t0
    finally:
        status.status_payload = saved_status  # type: ignore[assignment]
        status.scope_payload = saved_scope  # type: ignore[assignment]
    assert_eq("rc all-terminal", rc, 0)
    assert_true(f"returned promptly (elapsed={elapsed:.2f}s << 30s)", elapsed < 5.0)


def main() -> None:
    tests = [
        test_crashed_worker_resolves_worker_dead_after_grace_not_before,
        test_stale_dead_with_dead_pid_resolves_worker_dead,
        test_live_but_ambiguous_worker_is_not_killed,
        test_completed_pid_dead_stays_complete_trust_clause,
        test_wait_returns_bounded_on_crash_not_at_timeout,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_wait_terminal_primitive.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
