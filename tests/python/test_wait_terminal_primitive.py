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
    progress_state: dict[str, dict] | None = None,
) -> dict:
    if progress_state is None:
        progress_state = {}
    rows = status._wait_snapshot(
        payload,
        [dispatch_id],
        progress_state=progress_state,
        now=now,
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
    bound_text = (
        marker_text
        if marker_text == dispatch_id or marker_text.startswith(f"{dispatch_id} ")
        else f"{dispatch_id} — {marker_text}"
    )
    marker = {
        "kind": marker_kind,
        "text": bound_text,
        "line": 1,
    }
    status_path = directory / f"{dispatch_id}.status.json"
    status_path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "state": status_state or classification,
                "terminal_marker": marker,
            }
        ),
        encoding="utf-8",
    )
    return {
        "dispatch_id": dispatch_id,
        "classification": classification,
        "state": classification,
        "status_path": str(status_path),
        "terminal_marker": marker,
    }


def test_waiter_observes_dead_worker_without_casting_terminal_verdict() -> None:
    rec = {"dispatch_id": "dead-observer", "classification": "unknown_no_pid"}
    row = _row(
        _payload(rec),
        rec["dispatch_id"],
        now=10_000.0,
    )
    assert_eq("dead worker remains nonterminal", row["terminal"], False)
    assert_eq("classifier state preserved", row["state"], "unknown_no_pid")
    assert_eq("dead observation retained", row["progress"]["worker_alive"], False)


def test_waiter_observes_idle_live_worker_without_casting_stall_verdict() -> None:
    rec = {
        "dispatch_id": "idle-observer",
        "classification": "unknown",
        "worker_pid": 4242,
    }
    saved_alive = compat.pid_alive
    saved_cpu = status._wait_process_cpu_pct
    compat.pid_alive = lambda pid: True  # type: ignore[assignment]
    status._wait_process_cpu_pct = lambda record: 0.0  # type: ignore[assignment]
    try:
        row = _row(
            _payload(rec),
            rec["dispatch_id"],
            now=10_000.0,
        )
    finally:
        compat.pid_alive = saved_alive  # type: ignore[assignment]
        status._wait_process_cpu_pct = saved_cpu  # type: ignore[assignment]
    assert_eq("idle worker remains nonterminal", row["terminal"], False)
    assert_eq("classifier state preserved", row["state"], "unknown")
    assert_eq("idle observation retained", row["progress"]["cpu_idle"], True)


def test_completed_pid_dead_stays_complete_trust_clause() -> None:
    # reconcile-from-output already promoted this row to complete; --wait must
    # report complete, NOT worker_dead, even though the pid is gone.
    rec = {"dispatch_id": "done", "classification": "complete", "worker_pid": 2147480000}
    payload = _payload(rec)
    row = _row(payload, "done", now=10_000.0)
    assert_eq("complete terminal", row["terminal"], True)
    assert_eq("complete state", row["state"], "complete")


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
    row = _row(_payload(rec), "done", now=10_000.0)
    assert_eq("complete bare", status._wait_verdict_line(row), "done -> complete")
    assert_eq(
        "timeout bare",
        status._wait_verdict_line({"dispatch_id": "t", "state": "timeout", "marker_kind": "STATUS"}),
        "t -> timeout",
    )


def test_wait_snapshot_does_not_mix_stale_record_with_fresh_status_file() -> None:
    """One wait verdict uses one aggregate generation, never a fresh sidecar."""
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
                        "text": "chk — landing checkpoint - session abc123",
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
        row = _row(_payload(rec), "chk", now=10_000.0)
        assert_eq("fresh sidecar marker ignored", row.get("marker_kind"), None)
        line = status._wait_verdict_line(row)
        assert_eq("stale aggregate stays internally consistent", line, "chk -> blocked")

    # Unreadable/missing status file must degrade quietly, not raise.
    rec = {
        "dispatch_id": "gone",
        "classification": "blocked",
        "state": "blocked",
        "status_path": "/nonexistent/nope.json",
    }
    row = _row(_payload(rec), "gone", now=10_000.0)
    assert_eq("no kind, no crash", row.get("marker_kind"), None)
    assert_eq("bare verdict", status._wait_verdict_line(row), "gone -> blocked")


def test_marker_helpers_share_the_aggregate_snapshot() -> None:
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
        saved_cycle = status._wait_cycle_payload
        saved_alive = compat.pid_alive
        saved_cpu = status._wait_process_cpu_pct
        status._wait_cycle_payload = lambda *args, **kwargs: payload  # type: ignore[assignment]
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
                    heartbeat_s=0.0,
                    json_output=False,
                )
            out = buf.getvalue()
        finally:
            status._wait_cycle_payload = saved_cycle  # type: ignore[assignment]
            compat.pid_alive = saved_alive  # type: ignore[assignment]
            status._wait_process_cpu_pct = saved_cpu  # type: ignore[assignment]
    assert_eq("heartbeat wait times out", rc, 1)
    assert_true("heartbeat line dispatch", "hb: running" in out)
    assert_true("heartbeat line append age", "last append" in out)
    assert_true("heartbeat line cpu", "cpu 3.0%" in out)
    assert_true("heartbeat counts json/tool", "tool-use 1/json 1" in out)


def test_arming_a_wait_announces_mail_before_it_blocks() -> None:
    """The controller is awake when it arms a wait -- tell it about mail THEN.

    A backgrounded `--wait` is where a controller goes quiet for as long as its
    slowest worker takes. Announcing at arming costs nothing: no wake
    mechanism, no waiter teardown, no re-arm. Announcing anywhere else means
    the controller finds out at a scheduled wake-up several tool calls later.

    Three things are pinned, because each has its own way of silently breaking:

    1. The notice is emitted BEFORE the first poll. Ordering is checked against
       a shared call log, so moving the call after the loop -- or into the
       all-terminal branch, where a still-running wait never reaches it -- is
       caught. An "it was called at some point" assertion would not be.
    2. It goes to stderr. stdout is the ``--json`` data contract; a mail line
       there corrupts every machine consumer of a wait.
    3. Pre-existing mail does NOT shortcut the wait. The wait still runs to its
       own verdict (here: timeout, exit 1). A mail notice is information, not a
       terminal state -- reporting otherwise would be one more field asserting
       something it never measured.
    """
    calls: list[tuple[str, object]] = []

    import goalflight_messages as gm

    real_emit = gm.emit_controller_mail_notice
    real_cycle = status._wait_cycle_payload

    def fake_emit(**kwargs):
        calls.append(("mail-notice", kwargs.get("stream")))
        return "1 new mail"

    def fake_cycle(*args, **kwargs):
        calls.append(("poll", None))
        return real_cycle(*args, **kwargs)

    gm.emit_controller_mail_notice = fake_emit
    status._wait_cycle_payload = fake_cycle
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = status.wait_for_dispatches(
                ["no-such-dispatch-for-mail-arming-test"],
                project_root=str(ROOT),
                timeout_s=0.2,
                poll_s=0.05,
            )
    finally:
        gm.emit_controller_mail_notice = real_emit
        status._wait_cycle_payload = real_cycle

    assert_true("mail notice was emitted at all", any(c[0] == "mail-notice" for c in calls))
    assert_eq("mail notice is the FIRST thing the wait does", calls[0][0], "mail-notice")
    assert_eq("mail notice goes to stderr, never stdout", calls[0][1], sys.stderr)
    assert_true("the wait still polled after announcing", any(c[0] == "poll" for c in calls))
    assert_eq("pre-existing mail does not shortcut the wait", code, 1)
    assert_true(
        "no mail text leaked onto stdout (the --json contract)",
        "new mail" not in buf.getvalue(),
    )


def test_live_journal_attempt_suppresses_torn_terminal_sidecar() -> None:
    marker = {
        "kind": "COMPLETE",
        "text": "torn-live — stale terminal publication",
        "line": 1,
    }
    record = status._wait_record_from_snapshots(
        "torn-live",
        {
            "dispatch_id": "torn-live",
            "state": "complete",
            "terminal_state": "complete",
            "terminal_marker": marker,
        },
        {
            "dispatch_id": "torn-live",
            "state": "complete",
            "terminal_marker": marker,
        },
        {"lifecycle_state": "RUNNING"},
    )
    assert_true("journal live record exists", isinstance(record, dict))
    assert_eq("journal live state wins", record.get("state"), "running")
    assert_eq("torn marker removed", record.get("terminal_marker"), None)
    assert_true("torn terminal does not finish wait", status.done_code(record) != 0)


def test_terminal_journal_outbox_wins_live_ledger() -> None:
    record = status._wait_record_from_snapshots(
        "journal-need",
        {"dispatch_id": "journal-need", "state": "running"},
        {"dispatch_id": "journal-need", "state": "running"},
        {
            "lifecycle_state": "TERMINAL",
            "terminal_state": "blocked",
            "terminal_outcome_json": json.dumps(
                {"state": "blocked", "worker_still_alive": True, "outcome": {}}
            ),
            "event_type": "user_need",
            "payload_json": json.dumps({"text": "landing checkpoint"}),
        },
    )
    assert_true("journal terminal record exists", isinstance(record, dict))
    assert_eq(
        "journal terminal wins even if process remains live",
        status.done_code(record, worker_alive=True),
        0,
    )
    assert_eq(
        "outbox kind preserved",
        record.get("terminal_marker", {}).get("kind"),
        "USER-NEED",
    )
    assert_true(
        "synthetic marker remains id-bound",
        record.get("terminal_marker", {}).get("text", "").startswith("journal-need "),
    )


def test_journal_terminal_supersedes_contradictory_sidecar_marker() -> None:
    contradictory = {
        "dispatch_id": "journal-blocked",
        "state": "complete",
        "terminal_marker": {
            "kind": "COMPLETE",
            "text": "journal-blocked — late sidecar",
        },
    }
    record = status._wait_record_from_snapshots(
        "journal-blocked",
        {"dispatch_id": "journal-blocked", "state": "complete"},
        contradictory,
        {
            "lifecycle_state": "TERMINAL",
            "terminal_state": "blocked",
            "terminal_outcome_json": json.dumps(
                {"state": "complete", "outcome": {}}
            ),
            "event_type": "blocked",
            "payload_json": json.dumps({"text": "authoritative block"}),
        },
    )
    assert_true("journal terminal record exists", isinstance(record, dict))
    assert_eq("journal state is authoritative", record.get("state"), "blocked")
    assert_eq("journal marker meaning is authoritative", record["terminal_marker"]["kind"], "BLOCKED")
    assert_eq(
        "contradictory observation retained only as observation",
        record.get("terminal_observation_state"),
        "complete",
    )
    assert_eq(
        "late sidecar is explicitly superseded",
        record["superseded_terminal_marker"]["superseded"],
        True,
    )
    assert_eq(
        "superseded sidecar kind remains diagnostic only",
        record["superseded_terminal_marker"]["kind"],
        "COMPLETE",
    )

    matching = status._wait_record_from_snapshots(
        "journal-complete",
        {"dispatch_id": "journal-complete", "state": "complete"},
        {
            "dispatch_id": "journal-complete",
            "terminal_marker": {
                "kind": "COMPLETE",
                "text": "journal-complete — done",
            },
        },
        {
            "lifecycle_state": "TERMINAL",
            "terminal_state": "complete",
            "terminal_outcome_json": json.dumps({"state": "complete", "outcome": {}}),
            "event_type": "result",
            "payload_json": json.dumps({"text": "done"}),
        },
    )
    assert_eq("matching journal state remains complete", matching.get("state"), "complete")
    assert_eq("matching journal marker remains complete", matching["terminal_marker"]["kind"], "COMPLETE")


def test_unreadable_journal_never_promotes_file_terminal() -> None:
    marker = {
        "kind": "COMPLETE",
        "text": "journal-error — stale file terminal",
        "line": 1,
    }
    record = status._wait_record_from_snapshots(
        "journal-error",
        {
            "dispatch_id": "journal-error",
            "state": "complete",
            "terminal_state": "complete",
            "terminal_marker": marker,
        },
        {
            "dispatch_id": "journal-error",
            "state": "complete",
            "terminal_marker": marker,
        },
        {"_wait_journal_error": True},
    )
    assert_true("journal error record exists", isinstance(record, dict))
    assert_eq("journal error remains nonterminal", status.done_code(record), 2)
    assert_eq("file marker suppressed on authority failure", record.get("terminal_marker"), None)


def test_wait_journal_presence_distinguishes_absent_from_unobservable() -> None:
    original_resolve = status.goalflight_journal.resolve_journal_path
    original_lstat = status.os.lstat
    status.goalflight_journal.resolve_journal_path = lambda _root: Path("/journal/state.sqlite3")  # type: ignore[assignment]
    try:
        def absent(_path: object) -> object:
            raise FileNotFoundError(2, "missing")

        status.os.lstat = absent  # type: ignore[assignment]
        assert_eq("ENOENT is genuine legacy absence", status._wait_journal_presence("/project"), None)

        def denied(_path: object) -> object:
            raise PermissionError(13, "denied")

        status.os.lstat = denied  # type: ignore[assignment]
        assert_eq("EACCES is unobservable authority", status._wait_journal_presence("/project"), False)
    finally:
        status.goalflight_journal.resolve_journal_path = original_resolve  # type: ignore[assignment]
        status.os.lstat = original_lstat  # type: ignore[assignment]


def test_narrow_snapshot_reuses_one_sidecar_generation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        status_path = Path(tmp) / "cached.status.json"
        status_path.write_text(
            json.dumps({"dispatch_id": "cached", "pgroup_cpu_pct": 99.0}),
            encoding="utf-8",
        )
        record = status._wait_record_from_snapshots(
            "cached",
            {
                "dispatch_id": "cached",
                "state": "running",
                "status_path": str(status_path),
            },
            {"dispatch_id": "cached", "pgroup_cpu_pct": 3.0},
            None,
        )
        assert_true("cached narrow record exists", isinstance(record, dict))
        assert_eq("CPU comes from cycle snapshot", status._wait_process_cpu_pct(record), 3.0)
        assert_eq(
            "sidecar helper does not reopen newer file",
            status._status_json_payload(record).get("pgroup_cpu_pct"),
            3.0,
        )


def test_wait_hot_loop_never_calls_machine_status_payload() -> None:
    saved_status = status.status_payload
    saved_cycle = status._wait_cycle_payload
    saved_mail = status._mail_watermark

    def reject_machine_aggregate() -> dict:
        raise AssertionError("wait hot loop rebuilt machine aggregate")

    record = {
        "dispatch_id": "narrow-only",
        "classification": "complete",
        "state": "complete",
        "terminal_state": "complete",
        "_wait_snapshot_complete": True,
        "_wait_ledger_snapshot": {},
        "_wait_status_snapshot": {},
    }
    status.status_payload = reject_machine_aggregate  # type: ignore[assignment]
    status._wait_cycle_payload = lambda *args, **kwargs: _payload(record)  # type: ignore[assignment]
    status._mail_watermark = lambda *args, **kwargs: None  # type: ignore[assignment]
    try:
        code = status._wait_for_dispatches_registered(
            ["narrow-only"],
            project_root=str(ROOT),
            timeout_s=1.0,
            poll_s=0.05,
        )
    finally:
        status.status_payload = saved_status  # type: ignore[assignment]
        status._wait_cycle_payload = saved_cycle  # type: ignore[assignment]
        status._mail_watermark = saved_mail  # type: ignore[assignment]
    assert_eq("narrow wait completes", code, 0)


def main() -> None:
    tests = [
        test_arming_a_wait_announces_mail_before_it_blocks,
        test_waiter_observes_dead_worker_without_casting_terminal_verdict,
        test_waiter_observes_idle_live_worker_without_casting_stall_verdict,
        test_completed_pid_dead_stays_complete_trust_clause,
        test_terminal_row_carries_marker_kind_and_verdict_distinguishes_checkpoint,
        test_verdict_line_stays_bare_for_complete_and_timeout,
        test_wait_snapshot_does_not_mix_stale_record_with_fresh_status_file,
        test_marker_helpers_share_the_aggregate_snapshot,
        test_status_marker_fallback_rejects_nonterminal_and_wrong_dispatch,
        test_wait_heartbeat_emits_progress_line_at_cadence,
        test_live_journal_attempt_suppresses_torn_terminal_sidecar,
        test_terminal_journal_outbox_wins_live_ledger,
        test_journal_terminal_supersedes_contradictory_sidecar_marker,
        test_unreadable_journal_never_promotes_file_terminal,
        test_wait_journal_presence_distinguishes_absent_from_unobservable,
        test_narrow_snapshot_reuses_one_sidecar_generation,
        test_wait_hot_loop_never_calls_machine_status_payload,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_wait_terminal_primitive.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
