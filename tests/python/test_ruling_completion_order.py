"""Ruling requests stay resumable until later completion evidence resolves them."""

from __future__ import annotations

import pytest

from test_durable_completion_reads_derived_rows import launch_authority
from test_watch_prompt_echo import _run_dead_worker_tail

import goalflight_dispatch as dispatch
import goalflight_watch as watch


@pytest.mark.parametrize("state_field", ["state", "terminal_state"])
@pytest.mark.parametrize("carrier", ["death_cause", "terminal_marker", "reason_marker", "reason_text", "reason_object"])
@pytest.mark.parametrize("cause, advanced", [
    ("attention_marker:BLOCKED", 0),
    ("attention_marker:USER-NEED", 0),
    ("attention_marker:USER-CONFIRM", 0),
    ("attention_marker:FAILED", 1),
    ("attention_marker:BLOCKED_OTHER", 1),
    (None, 1),
])
def test_worker_dead_death_cause_awaiting_ruling(
    launch_authority, state_field, carrier, cause, advanced,
):
    args, _store, _row, records = launch_authority
    record = {
        "dispatch_id": "stopped-earlier", "project_root": args.project_root,
        "task_ids": args.task_ids, state_field: "worker_dead",
    }
    kind = cause.removeprefix("attention_marker:") if cause else None
    # Structured evidence wins over contradictory legacy prose, in both directions.
    contrary = "BLOCKED" if advanced else "FAILED"
    reason = f"worker_dead_no_terminal_marker:death_cause=attention_marker:{contrary}"
    if carrier == "death_cause":
        record.update(death_cause=cause, reason=reason if cause else "no_evidence")
    elif carrier == "terminal_marker":
        record.update(terminal_marker={"kind": kind}, reason=reason if cause else "no_evidence")
    elif carrier == "reason_marker":
        record["reason"] = {"marker_kind": kind, "reason": reason if cause else "no_evidence"}
    else:
        reason = f"worker_dead_no_terminal_marker:death_cause={cause or 'no_evidence'}"
        record["reason"] = reason if carrier == "reason_text" else {"reason": reason}
    records.append(record)

    assert dispatch._ledger_task_ids_advanced(
        args.task_ids, self_dispatch_id=args.dispatch_id,
        self_project_root=args.project_root,
    ) == (0, advanced, "conclusive")


SIGNAL_ORDERS = [
    ("BLOCKED READY", "BLOCKED"),
    ("READY BLOCKED READY", "BLOCKED"),
    ("COMPLETE BLOCKED READY", "BLOCKED"),
    ("RESULT BLOCKED READY", "BLOCKED"),
    ("BLOCKED COMPLETE READY", "COMPLETE"),
    ("BLOCKED RESULT READY", "RESULT"),
    ("BLOCKED COMPLETE BLOCKED READY", "BLOCKED"),
    ("BLOCKED RESULT BLOCKED READY", "BLOCKED"),
]


@pytest.mark.parametrize("signals, expected", SIGNAL_ORDERS)
@pytest.mark.parametrize("scanner", ["dispatch", "stream", "rendered"])
def test_blocked_ready_order(tmp_path, signals, expected, scanner):
    dispatch_id = "ruling-worker"
    text = "".join(f"!{kind}: {dispatch_id} — evidence\n" for kind in signals.split())
    tail = tmp_path / "worker.tail"
    tail.write_text(text)
    if scanner == "dispatch":
        marker = dispatch._scan_entry_completion_marker({
            "dispatch_id": dispatch_id, "request": {"tail": str(tail)},
        })
    elif scanner == "stream":
        marker, _unbalanced = watch._stream_final_terminal_marker(
            tail, prompt_prefix=[], suppress_unfenced_prompt_markers=True,
            ignore_fences=False, kimi_output=False, expected_dispatch_id=dispatch_id,
        )
    else:
        marker = watch._scan_final_terminal_marker(
            text.splitlines(), prompt_echo_lines=set(), echo_anchor_found=False,
            prompt_line_set=set(), suppress_unfenced_prompt_markers=True,
            ignore_fences=False, expected_dispatch_id=dispatch_id, rendered_response=True,
        )
    assert marker is not None
    assert marker["kind"] == expected
    expected_line = max(i for i, kind in enumerate(signals.split(), 1) if kind == expected)
    assert marker["line"] == expected_line


@pytest.mark.parametrize("signals, expected", SIGNAL_ORDERS)
def test_dispatch_blocked_ready_order_beyond_tail_window(tmp_path, signals, expected):
    dispatch_id = "ruling-worker"
    kinds = signals.split()
    prefix = "".join(f"!{kind}: {dispatch_id} — evidence\n" for kind in kinds[:-1])
    tail = tmp_path / "worker.tail"
    tail.write_text(
        prefix + ("x" * 1000 + "\n") * 11_000
        + f"!READY: {dispatch_id} — blocker report\n",
        encoding="utf-8",
    )
    assert tail.stat().st_size - 10 * 1024 * 1024 > len(prefix.encode("utf-8"))

    marker = dispatch._scan_entry_completion_marker({
        "dispatch_id": dispatch_id, "request": {"tail": str(tail)},
    })

    assert marker is not None
    assert marker["kind"] == expected
    assert marker["line"] == max(i for i, kind in enumerate(kinds, 1) if kind == expected)


@pytest.mark.parametrize("signals, expected", SIGNAL_ORDERS)
def test_watcher_publishes_blocked_ready_order(signals, expected):
    dispatch_id = "ruling-worker"
    text = "".join(f"!{kind}: {dispatch_id} — evidence\n" for kind in signals.split())

    rc, _elapsed, _live_marker, payload = _run_dead_worker_tail(text, dispatch_id=dispatch_id)

    assert payload["terminal_marker"]["kind"] == expected
    assert payload["state"] == ("blocked" if expected == "BLOCKED" else "complete")
    assert payload["reason"] == f"marker:{expected}"
    assert rc == (4 if expected == "BLOCKED" else 0)


@pytest.mark.parametrize("signals", ["BLOCKED READY", "BLOCKED COMPLETE READY", "BLOCKED RESULT READY"])
def test_ordering_preserves_live_worker_timeout(signals):
    from test_dispatch_crash_safe import case_shared_cwd_complete_then_hang_is_inconclusive

    case_shared_cwd_complete_then_hang_is_inconclusive(signals)
