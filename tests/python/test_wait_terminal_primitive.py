"""Regression tests for the bounded-on-crash --wait primitive.

`goalflight_status.py --wait` must NEVER poll to the wait-timeout on a crashed /
premature-exited worker: an ambiguous/stale dispatch whose worker is confirmed
dead resolves to a terminal `worker_dead` verdict after a short grace, so the call
exits and the controller is bumped. It must equally NOT kill a genuinely-running
worker, and must keep trusting reconcile-from-output (a completed-but-pid-dead row
stays `complete`, not `worker_dead`).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
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


def _row(
    payload: dict,
    dispatch_id: str,
    *,
    now: float,
    grace: float,
    dead_since: dict[str, float],
    stale_grace: float = 600.0,
    stalled_since: dict[str, float] | None = None,
    progress_state: dict[str, dict] | None = None,
) -> dict:
    if stalled_since is None:
        stalled_since = {}
    if progress_state is None:
        progress_state = {}
    rows = status._wait_snapshot(
        payload,
        [dispatch_id],
        dead_since=dead_since,
        stalled_since=stalled_since,
        progress_state=progress_state,
        now=now,
        grace=grace,
        stale_grace=stale_grace,
    )
    return rows[0]


def _aggregate_record_with_status_marker(
    directory: Path,
    *,
    dispatch_id: str,
    marker_kind: str,
    marker_text: str,
    classification: str = "blocked",
    status_state: str | None = None,
) -> dict:
    status_path = directory / f"{dispatch_id}.status.json"
    status_path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "state": status_state or classification,
                "terminal_marker": {
                    "kind": marker_kind,
                    "text": marker_text,
                    "line": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "dispatch_id": dispatch_id,
        "classification": classification,
        "state": classification,
        "status_path": str(status_path),
    }


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


def test_identity_mismatch_with_growing_tail_vetoes_worker_dead() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "worker.tail"
        tail.write_text("started\n", encoding="utf-8")
        rec = {
            "dispatch_id": "identity-mismatch-growing",
            "classification": "unknown",
            "worker_pid": 4242,
            "worker_identity": {
                "pid": 4242,
                "lstart": "Tue Jul 21 12:00:00 2026",
                "comm": "sh",
            },
            "tail_path": str(tail),
        }
        payload = _payload(rec)
        saved_identity_matches = status.goalflight_ledger.identity_matches
        saved_cpu = status._wait_process_cpu_pct
        status.goalflight_ledger.identity_matches = (  # type: ignore[assignment]
            lambda record: (False, "pid_reused_comm")
        )
        status._wait_process_cpu_pct = lambda record: 0.0  # type: ignore[assignment]
        try:
            initial_size = tail.stat().st_size
            tail.write_text("started\nstill working\n", encoding="utf-8")
            dead_since: dict[str, float] = {}
            row = _row(
                payload,
                rec["dispatch_id"],
                now=10.0,
                grace=0.0,
                dead_since=dead_since,
                progress_state={
                    rec["dispatch_id"]: {
                        "tail_size": initial_size,
                        "last_growth_mono": 0.0,
                    }
                },
            )
            assert_eq("growing mismatch stays non-terminal", row["terminal"], False)
            assert_eq("growing mismatch reports running", row["state"], "running")
            assert_eq("growing mismatch reports alive via output", row["progress"]["worker_alive"], True)
            assert_true("growing mismatch does not arm death", rec["dispatch_id"] not in dead_since)
        finally:
            status.goalflight_ledger.identity_matches = saved_identity_matches  # type: ignore[assignment]
            status._wait_process_cpu_pct = saved_cpu  # type: ignore[assignment]


def test_identity_mismatch_with_quiet_tail_still_resolves_worker_dead() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "worker.tail"
        tail.write_text("stopped\n", encoding="utf-8")
        os.utime(tail, (time.time() - 100.0, time.time() - 100.0))
        rec = {
            "dispatch_id": "identity-mismatch-quiet",
            "classification": "unknown",
            "worker_pid": 4242,
            "worker_identity": {
                "pid": 4242,
                "lstart": "Tue Jul 21 12:00:00 2026",
                "comm": "sh",
            },
            "tail_path": str(tail),
        }
        payload = _payload(rec)
        saved_identity_matches = status.goalflight_ledger.identity_matches
        saved_cpu = status._wait_process_cpu_pct
        status.goalflight_ledger.identity_matches = (  # type: ignore[assignment]
            lambda record: (False, "pid_reused_comm")
        )
        status._wait_process_cpu_pct = lambda record: 0.0  # type: ignore[assignment]
        try:
            row = _row(
                payload,
                rec["dispatch_id"],
                now=100.0,
                grace=0.0,
                dead_since={},
                progress_state={
                    rec["dispatch_id"]: {
                        "tail_size": tail.stat().st_size,
                        "last_growth_mono": 0.0,
                    }
                },
            )
            assert_eq("quiet mismatch is terminal", row["terminal"], True)
            assert_eq("quiet mismatch stays worker_dead", row["state"], "worker_dead")
        finally:
            status.goalflight_ledger.identity_matches = saved_identity_matches  # type: ignore[assignment]
            status._wait_process_cpu_pct = saved_cpu  # type: ignore[assignment]


def test_dead_quiet_worker_with_success_marker_completes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "worker.tail"
        tail.write_text("DONE.\n", encoding="utf-8")
        os.utime(tail, (time.time() - 100.0, time.time() - 100.0))
        rec = {
            "dispatch_id": "dead-quiet-complete",
            "classification": "unknown",
            "worker_pid": 4242,
            "tail_path": str(tail),
        }
        payload = _payload(rec)
        saved_alive = compat.pid_alive
        saved_cpu = status._wait_process_cpu_pct
        compat.pid_alive = lambda pid: False  # type: ignore[assignment]
        status._wait_process_cpu_pct = lambda record: 0.0  # type: ignore[assignment]
        try:
            row = _row(
                payload,
                rec["dispatch_id"],
                now=100.0,
                grace=0.0,
                dead_since={},
                progress_state={
                    rec["dispatch_id"]: {
                        "tail_size": tail.stat().st_size,
                        "last_growth_mono": 0.0,
                    }
                },
            )
            assert_eq("dead marker is terminal", row["terminal"], True)
            assert_eq("dead marker completes", row["state"], "complete")
            assert_eq("dead marker done code", row["done_code"], 0)
            assert_eq("dead marker surfaced", row["terminal_marker"]["kind"], "COMPLETE")
        finally:
            compat.pid_alive = saved_alive  # type: ignore[assignment]
            status._wait_process_cpu_pct = saved_cpu  # type: ignore[assignment]


def test_wedged_alive_worker_resolves_worker_stalled_after_stale_grace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "worker.tail"
        tail.write_text("started\n", encoding="utf-8")
        rec = {
            "dispatch_id": "wedged",
            "classification": "unknown",
            "worker_pid": 4242,
            "tail_path": str(tail),
        }
        payload = _payload(rec)
        saved_alive = compat.pid_alive
        saved_cpu = status._wait_process_cpu_pct
        compat.pid_alive = lambda pid: True  # type: ignore[assignment]
        status._wait_process_cpu_pct = lambda record: 0.0  # type: ignore[assignment]
        try:
            dead_since: dict[str, float] = {}
            stalled_since: dict[str, float] = {}
            progress_state: dict[str, dict] = {}
            early = _row(
                payload,
                "wedged",
                now=0.0,
                grace=90.0,
                dead_since=dead_since,
                stale_grace=5.0,
                stalled_since=stalled_since,
                progress_state=progress_state,
            )
            assert_eq("wedged initial not terminal", early["terminal"], False)
            late = _row(
                payload,
                "wedged",
                now=5.0,
                grace=90.0,
                dead_since=dead_since,
                stale_grace=5.0,
                stalled_since=stalled_since,
                progress_state=progress_state,
            )
            assert_eq("wedged terminal", late["terminal"], True)
            assert_eq("wedged state", late["state"], "worker_stalled")
            assert_true("dead_since not armed for live stall", "wedged" not in dead_since)
        finally:
            compat.pid_alive = saved_alive  # type: ignore[assignment]
            status._wait_process_cpu_pct = saved_cpu  # type: ignore[assignment]


def test_growing_tail_worker_never_resolves_worker_stalled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "worker.tail"
        tail.write_text("0\n", encoding="utf-8")
        rec = {
            "dispatch_id": "grow",
            "classification": "unknown",
            "worker_pid": 4242,
            "tail_path": str(tail),
        }
        payload = _payload(rec)
        saved_alive = compat.pid_alive
        saved_cpu = status._wait_process_cpu_pct
        compat.pid_alive = lambda pid: True  # type: ignore[assignment]
        status._wait_process_cpu_pct = lambda record: 0.0  # type: ignore[assignment]
        try:
            stalled_since: dict[str, float] = {}
            progress_state: dict[str, dict] = {}
            dead_since: dict[str, float] = {}
            for i, now in enumerate((0.0, 10.0, 20.0, 30.0), start=1):
                tail.write_text(("x\n" * i), encoding="utf-8")
                row = _row(
                    payload,
                    "grow",
                    now=now,
                    grace=90.0,
                    dead_since=dead_since,
                    stale_grace=5.0,
                    stalled_since=stalled_since,
                    progress_state=progress_state,
                )
                assert_eq(f"growing not terminal at {now}", row["terminal"], False)
                assert_true("stalled_since clear on growth", "grow" not in stalled_since)
        finally:
            compat.pid_alive = saved_alive  # type: ignore[assignment]
            status._wait_process_cpu_pct = saved_cpu  # type: ignore[assignment]


def test_busy_cpu_worker_never_resolves_worker_stalled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "worker.tail"
        tail.write_text("started\n", encoding="utf-8")
        rec = {
            "dispatch_id": "busy",
            "classification": "unknown",
            "worker_pid": 4242,
            "tail_path": str(tail),
        }
        payload = _payload(rec)
        saved_alive = compat.pid_alive
        saved_cpu = status._wait_process_cpu_pct
        compat.pid_alive = lambda pid: True  # type: ignore[assignment]
        status._wait_process_cpu_pct = lambda record: 4.2  # type: ignore[assignment]
        try:
            stalled_since: dict[str, float] = {}
            progress_state: dict[str, dict] = {}
            dead_since: dict[str, float] = {}
            _row(
                payload,
                "busy",
                now=0.0,
                grace=90.0,
                dead_since=dead_since,
                stale_grace=5.0,
                stalled_since=stalled_since,
                progress_state=progress_state,
            )
            row = _row(
                payload,
                "busy",
                now=100.0,
                grace=90.0,
                dead_since=dead_since,
                stale_grace=5.0,
                stalled_since=stalled_since,
                progress_state=progress_state,
            )
            assert_eq("busy cpu not terminal", row["terminal"], False)
            assert_true("stalled_since clear on cpu", "busy" not in stalled_since)
        finally:
            compat.pid_alive = saved_alive  # type: ignore[assignment]
            status._wait_process_cpu_pct = saved_cpu  # type: ignore[assignment]


def test_unknown_cpu_worker_never_resolves_worker_stalled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "worker.tail"
        tail.write_text("started\n", encoding="utf-8")
        rec = {
            "dispatch_id": "unknown-cpu",
            "classification": "unknown",
            "worker_pid": 4242,
            "tail_path": str(tail),
        }
        payload = _payload(rec)
        saved_alive = compat.pid_alive
        saved_cpu = status._wait_process_cpu_pct
        compat.pid_alive = lambda pid: True  # type: ignore[assignment]
        status._wait_process_cpu_pct = lambda record: None  # type: ignore[assignment]
        try:
            stalled_since: dict[str, float] = {}
            progress_state: dict[str, dict] = {}
            dead_since: dict[str, float] = {}
            _row(
                payload,
                "unknown-cpu",
                now=0.0,
                grace=90.0,
                dead_since=dead_since,
                stale_grace=5.0,
                stalled_since=stalled_since,
                progress_state=progress_state,
            )
            row = _row(
                payload,
                "unknown-cpu",
                now=100.0,
                grace=90.0,
                dead_since=dead_since,
                stale_grace=5.0,
                stalled_since=stalled_since,
                progress_state=progress_state,
            )
            assert_eq("unknown cpu not terminal", row["terminal"], False)
            assert_true("stalled_since clear on unknown cpu", "unknown-cpu" not in stalled_since)
        finally:
            compat.pid_alive = saved_alive  # type: ignore[assignment]
            status._wait_process_cpu_pct = saved_cpu  # type: ignore[assignment]


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


def test_terminal_row_carries_marker_kind_and_verdict_distinguishes_checkpoint() -> None:
    # "blocked" is a collapsed state: USER-NEED (expected checkpoint) and
    # BLOCKED (wedged) must not print identically, or the reader opens every
    # tail to learn which. The row carries the kind; the verdict line shows it.
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        rec = _aggregate_record_with_status_marker(
            directory,
            dispatch_id="chk",
            marker_kind="USER-NEED",
            marker_text="landing checkpoint - session abc123; log /tmp/x.log",
        )
        row = _row(
            _payload(rec),
            "chk",
            now=10_000.0,
            grace=90.0,
            dead_since={},
        )
        assert_eq("terminal", row["terminal"], True)
        assert_eq("marker kind on row", row.get("marker_kind"), "USER-NEED")
        line = status._wait_verdict_line(row)
        assert_true("kind in verdict", "[USER-NEED]" in line)
        assert_true("headline in verdict", "landing checkpoint" in line)

        wedged = _aggregate_record_with_status_marker(
            directory,
            dispatch_id="wdg",
            marker_kind="BLOCKED",
            marker_text="sandbox refused bind",
        )
        wline = status._wait_verdict_line(
            _row(
                _payload(wedged),
                "wdg",
                now=10_000.0,
                grace=90.0,
                dead_since={},
            )
        )
        assert_true("wedged shows BLOCKED kind", "[BLOCKED]" in wline)
        assert_true("the two verdicts differ", line != wline)


def test_verdict_line_stays_bare_for_complete_and_timeout() -> None:
    rec = {
        "dispatch_id": "done",
        "classification": "complete",
        "terminal_marker": {"kind": "COMPLETE", "text": "all good", "line": 1},
    }
    row = _row(_payload(rec), "done", now=10_000.0, grace=90.0, dead_since={})
    assert_eq("complete bare", status._wait_verdict_line(row), "done -> complete")
    assert_eq(
        "timeout bare",
        status._wait_verdict_line({"dispatch_id": "t", "state": "timeout", "marker_kind": "STATUS"}),
        "t -> timeout",
    )


def test_marker_kind_is_read_from_the_status_FILE_not_just_the_record() -> None:
    """The aggregated payload record carries no marker fields - only the
    per-dispatch status JSON does. A lookup that reads the record alone passes
    against a hand-built record and yields nothing on every real dispatch,
    which is exactly how this shipped broken the first time.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        status_path = os.path.join(tmp, "chk.status.json")
        with open(status_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "dispatch_id": "chk",
                    "state": "blocked",
                    "terminal_marker": {
                        "kind": "USER-NEED",
                        "text": "landing checkpoint - session abc123",
                        "line": 5,
                    },
                },
                handle,
            )
        # Record shaped like the REAL aggregate: state + status_path, no marker.
        rec = {
            "dispatch_id": "chk",
            "classification": "blocked",
            "state": "blocked",
            "status_path": status_path,
        }
        row = _row(_payload(rec), "chk", now=10_000.0, grace=90.0, dead_since={})
        assert_eq("kind recovered from file", row.get("marker_kind"), "USER-NEED")
        line = status._wait_verdict_line(row)
        assert_true("kind in verdict", "[USER-NEED]" in line)

    # Unreadable/missing status file must degrade quietly, not raise.
    rec = {
        "dispatch_id": "gone",
        "classification": "blocked",
        "state": "blocked",
        "status_path": "/nonexistent/nope.json",
    }
    row = _row(_payload(rec), "gone", now=10_000.0, grace=90.0, dead_since={})
    assert_eq("no kind, no crash", row.get("marker_kind"), None)
    assert_eq("bare verdict", status._wait_verdict_line(row), "gone -> blocked")


def test_marker_helpers_share_production_status_file_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rec = _aggregate_record_with_status_marker(
            Path(tmp),
            dispatch_id="done-from-status",
            marker_kind="COMPLETE",
            marker_text="finished",
            classification="expected_live",
            status_state="complete",
        )

        assert_eq(
            "terminal marker kind",
            status._record_terminal_marker_kind(rec),
            "COMPLETE",
        )
        assert_eq("terminal marker present", status._record_has_terminal_marker(rec), True)
        assert_eq("done code sees status marker", status.done_code(rec), 0)


def test_status_marker_fallback_rejects_nonterminal_and_wrong_dispatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        status_path = Path(tmp) / "marker.status.json"
        rec = {
            "dispatch_id": "expected",
            "classification": "blocked",
            "state": "blocked",
            "status_path": str(status_path),
        }
        status_path.write_text(
            json.dumps(
                {
                    "dispatch_id": "expected",
                    "last_marker": {"kind": "STATUS", "text": "still working"},
                }
            ),
            encoding="utf-8",
        )
        assert_eq("STATUS is not terminal", status._record_marker_info(rec), None)
        assert_eq(
            "STATUS omitted from verdict",
            status._wait_verdict_line(
                _row(
                    _payload(rec),
                    "expected",
                    now=10_000.0,
                    grace=90.0,
                    dead_since={},
                )
            ),
            "expected -> blocked",
        )

        status_path.write_text(
            json.dumps(
                {
                    "dispatch_id": "different",
                    "terminal_marker": {
                        "kind": "BLOCKED",
                        "text": "wrong dispatch",
                    },
                }
            ),
            encoding="utf-8",
        )
        assert_eq(
            "mismatched dispatch ignored",
            status._record_marker_info(rec),
            None,
        )


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


def test_wait_heartbeat_emits_progress_line_at_cadence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "worker.tail"
        tail.write_text('{"type":"tool_use"}\n', encoding="utf-8")
        payload = _payload(
            {
                "dispatch_id": "hb",
                "classification": "expected_live",
                "worker_pid": 4242,
                "tail_path": str(tail),
            }
        )
        saved_status, saved_scope = status.status_payload, status.scope_payload
        saved_alive = compat.pid_alive
        saved_cpu = status._wait_process_cpu_pct
        status.status_payload = lambda: payload  # type: ignore[assignment]
        status.scope_payload = lambda p, root: p  # type: ignore[assignment]
        compat.pid_alive = lambda pid: True  # type: ignore[assignment]
        status._wait_process_cpu_pct = lambda record: 3.0  # type: ignore[assignment]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = status.wait_for_dispatches(
                    ["hb"],
                    project_root=None,
                    timeout_s=0.12,
                    poll_s=0.05,
                    crash_grace_s=0.0,
                    stale_grace_s=0.05,
                    heartbeat_s=0.0,
                    json_output=False,
                )
            out = buf.getvalue()
        finally:
            status.status_payload = saved_status  # type: ignore[assignment]
            status.scope_payload = saved_scope  # type: ignore[assignment]
            compat.pid_alive = saved_alive  # type: ignore[assignment]
            status._wait_process_cpu_pct = saved_cpu  # type: ignore[assignment]
    assert_eq("heartbeat wait times out", rc, 1)
    assert_true("heartbeat line dispatch", "hb: running" in out)
    assert_true("heartbeat line append age", "last append" in out)
    assert_true("heartbeat line cpu", "cpu 3.0%" in out)
    assert_true("heartbeat counts json/tool", "tool-use 1/json 1" in out)


def main() -> None:
    tests = [
        test_crashed_worker_resolves_worker_dead_after_grace_not_before,
        test_stale_dead_with_dead_pid_resolves_worker_dead,
        test_live_but_ambiguous_worker_is_not_killed,
        test_identity_mismatch_with_growing_tail_vetoes_worker_dead,
        test_identity_mismatch_with_quiet_tail_still_resolves_worker_dead,
        test_dead_quiet_worker_with_success_marker_completes,
        test_wedged_alive_worker_resolves_worker_stalled_after_stale_grace,
        test_growing_tail_worker_never_resolves_worker_stalled,
        test_busy_cpu_worker_never_resolves_worker_stalled,
        test_unknown_cpu_worker_never_resolves_worker_stalled,
        test_completed_pid_dead_stays_complete_trust_clause,
        test_wait_returns_bounded_on_crash_not_at_timeout,
        test_wait_heartbeat_emits_progress_line_at_cadence,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_wait_terminal_primitive.py ({len(tests)} tests)")
    test_terminal_row_carries_marker_kind_and_verdict_distinguishes_checkpoint()
    test_verdict_line_stays_bare_for_complete_and_timeout()
    test_marker_kind_is_read_from_the_status_FILE_not_just_the_record()


if __name__ == "__main__":
    main()
