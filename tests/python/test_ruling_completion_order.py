"""Ruling requests stay resumable until later completion evidence resolves them."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_durable_completion_reads_derived_rows import launch_authority
from test_watch_prompt_echo import _run_dead_worker_tail
from test_ledger_sidecar_terminal_gate import spawn_worker, _mark_attempt_running, _write_ledger_record

import goalflight_dispatch as dispatch
import goalflight_ledger as ledger
import goalflight_journal as journal
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


@pytest.mark.parametrize("kind", ["COMPLETE", "RESULT"])
@pytest.mark.parametrize("placement", ["fenced", "fenced_then_unmatched", "before_cut", "after_cut"])
@pytest.mark.parametrize("large", [False, True])
def test_large_tail_completion_requires_whole_file_context(kind, placement, large):
    dispatch_id = "large-terminal-worker"
    marker = f"!{kind}: {dispatch_id} — finished\n"
    padding = ("x" * 1000 + "\n") * 11_000 if large else "tool output\n"
    text = {
        "fenced": "```text\n" + padding + marker + "```\nStill working.\n",
        "fenced_then_unmatched": "```text\n" + padding + marker + "```\ntool\n```bash\ntruncated\n",
        "before_cut": marker + padding,
        "after_cut": padding + marker,
    }[placement]
    rc, _elapsed, _live, payload = _run_dead_worker_tail(text, dispatch_id=dispatch_id)
    quoted = placement.startswith("fenced")
    assert payload["state"] == ("worker_dead" if quoted else "complete")
    assert rc == (1 if quoted else 0)
    if quoted:
        assert payload.get("terminal_marker") is None
    else:
        assert payload["terminal_marker"]["kind"] == kind


@pytest.mark.parametrize("earlier", ["Earlier response.", "!READY: footer-worker — earlier report"])
@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("snippet", [None, "python", ""])
def test_later_tool_footer_recovers_final_escalation(earlier, large, snippet):
    text = (
        "tokens used\n100\n" + earlier + "\ntool\n```bash\ntruncated\n"
        + (("x" * 1000 + "\n") * 11_000 if large else "")
        + "tokens used\n200\n!BLOCKED: footer-worker — need ruling\n"
        + (f"```{snippet}\nprint(1)\n```\n" if snippet is not None else "") + "summary\n"
    )
    rc, _elapsed, _live, payload = _run_dead_worker_tail(text, dispatch_id="footer-worker")
    assert payload["state"] == "blocked"
    assert payload["terminal_marker"]["kind"] == "BLOCKED"
    assert rc == 4


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("prior", ["READY", "BLOCKED", "COMPLETE"])
@pytest.mark.parametrize("quoted", [False, True])
def test_response_local_completion_preserves_quote_context(large, prior, quoted):
    text = (
        f"tokens used\n100\n!{prior}: footer-worker — earlier report\n"
        + ("!READY: footer-worker — report pointer\n" if prior == "BLOCKED" else "")
        + "tool\n```bash\n"
        + (("x" * 1000 + "\n") * 11_000 if large else "")
        + "tokens used\n200\n"
        + ("```\n" if quoted else "")
        + "!COMPLETE: footer-worker — response example\n"
        + ("```\n" if quoted else "") + "summary\n"
    )
    rc, _elapsed, _live, payload = _run_dead_worker_tail(text, dispatch_id="footer-worker")
    expected = prior if quoted else "COMPLETE"
    assert payload["terminal_marker"]["kind"] == expected
    assert payload["terminal_marker"]["text"] == (
        "footer-worker — earlier report" if quoted else "footer-worker — response example"
    )
    assert payload["state"] == ("blocked" if expected == "BLOCKED" else "complete")
    assert rc == (4 if expected == "BLOCKED" else 0)


@pytest.mark.parametrize("boundary, closing", [("example", ""), ("tool", "```\n")])
def test_later_quoted_footer_does_not_recover_escalation(boundary, closing):
    text = (
        "tokens used\n100\nEarlier response.\n" + boundary
        + "\n```text\ntokens used\n200\n!BLOCKED: footer-worker — example\n"
        + closing + "summary\n"
    )
    rc, _elapsed, _live, payload = _run_dead_worker_tail(text, dispatch_id="footer-worker")
    assert payload["state"] == "worker_dead"
    assert payload.get("terminal_marker") is None
    assert rc == 1


@pytest.mark.parametrize("scenario", ["later_global", "nested_local"])
def test_final_footer_context_keeps_real_question(scenario):
    prefix = "tokens used\n100\n!READY: footer-worker — earlier report\ntool\n```text\n"
    question = "!BLOCKED: footer-worker — need ruling\n"
    example = "tokens used\n300\n!USER-NEED: footer-worker — quoted example\n"
    if scenario == "later_global":
        text = prefix + example + "```\ntokens used\n400\n" + question + "summary\n"
    else:
        text = prefix + "tokens used\n200\n" + question + "```\n" + example + "```\nsummary\n"
    rc, _elapsed, _live, payload = _run_dead_worker_tail(text, dispatch_id="footer-worker")
    assert payload["state"] == "blocked" and rc == 4
    assert payload["terminal_marker"]["kind"] == "BLOCKED"
    assert payload["terminal_marker"]["text"] == "footer-worker — need ruling"


@pytest.fixture
def dead_terminal_entry(tmp_path, spawn_worker):
    worker = spawn_worker()
    identity = ledger.process_identity(worker.pid)
    assert identity
    worker.terminate()
    worker.wait(timeout=10)
    tail = tmp_path / "worker.tail"
    return {
        "dispatch_id": "dispatch-terminal-worker", "agent": "codex",
        "queue_worker_pid": worker.pid, "queue_worker_identity": identity,
        "request": {"tail": str(tail)},
    }, tail


def _dispatch_terminal_surface(entry, tail, surface):
    if surface == "repair":
        status_path = tail.with_suffix(".status.json")
        result = dispatch._repair_watcher_terminal_status(
            status_path, args=SimpleNamespace(dispatch_id=entry["dispatch_id"], agent="codex"),
            tail=tail, worker_pid=entry.get("queue_worker_pid"),
            worker_identity=entry.get("queue_worker_identity"), pgid=None, prompt_path=None,
            reason="watcher_stopped",
        )
        assert json.loads(status_path.read_text())["state"] == result["state"]
        return result["state"], result.get("terminal_marker")
    if surface == "claim":
        state, _reason, marker = dispatch._resolve_claim_terminal_outcome(
            entry, reason="worker_dead", tail=tail, ignore_prefix_lines=[], agent="codex",
        )
        return state, marker
    marker = dispatch._scan_entry_completion_marker(entry)
    return dispatch._marker_state_for_terminal(marker) if marker else "worker_dead", marker


@pytest.mark.parametrize("surface", ["repair", "claim"])
@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize("large", [False, True])
def test_dispatch_recovery_preserves_blocked_ready_order(dead_terminal_entry, surface, resolved, large):
    entry, tail = dead_terminal_entry
    dispatch_id = entry["dispatch_id"]
    tail.write_text(
        f"!BLOCKED: {dispatch_id} — need ruling\n"
        + (f"!COMPLETE: {dispatch_id} — resolved\n" if resolved else "")
        + (("x" * 1000 + "\n") * 11_000 if large else "")
        + f"!READY: {dispatch_id} — report\n"
    )
    state, marker = _dispatch_terminal_surface(entry, tail, surface)
    assert marker["kind"] == ("COMPLETE" if resolved else "BLOCKED")
    assert state == ("complete" if resolved else "blocked")


@pytest.mark.parametrize("surface", ["repair", "claim", "authority"])
@pytest.mark.parametrize("quoted", [False, True])
def test_dispatch_recovers_only_unquoted_final_question(dead_terminal_entry, surface, quoted):
    entry, tail = dead_terminal_entry
    marker = f"!USER-NEED: {entry['dispatch_id']} — Which release target?\n"
    tail.write_text("tokens used\n42\n" + ("> " if quoted else "") + marker + "Summary.\n")
    state, found = _dispatch_terminal_surface(entry, tail, surface)
    assert state == ("worker_dead" if quoted else "blocked")
    assert (found or {}).get("kind") == (None if quoted else "USER-NEED")


@pytest.mark.parametrize("surface", ["repair", "claim", "authority"])
@pytest.mark.parametrize("liveness", ["live", "unknown"])
def test_dispatch_final_response_recovery_requires_dead_identity(tmp_path, spawn_worker, surface, liveness):
    entry = {"dispatch_id": "not-dead-worker", "request": {}}
    if liveness == "live":
        worker = spawn_worker()
        entry.update(queue_worker_pid=worker.pid, queue_worker_identity=ledger.process_identity(worker.pid))
    tail = tmp_path / "worker.tail"
    entry["request"]["tail"] = str(tail)
    tail.write_text("tokens used\n42\n!USER-NEED: not-dead-worker — Which target?\nSummary.\n")
    _state, marker = _dispatch_terminal_surface(entry, tail, surface)
    assert marker is None


@pytest.mark.parametrize("kind", ["USER-NEED", "USER-CONFIRM", "BLOCKED", "FAILED"])
def test_claim_recovery_delivers_final_question(dead_terminal_entry, tmp_path, kind):
    entry, tail = dead_terminal_entry
    entry.update(queue_launch_token="terminal-claim-token", project_root=str(tmp_path))
    entry["request"]["cwd"] = str(tmp_path)
    question = f"{entry['dispatch_id']} — Which release target should I use?"
    tail.write_text(f"tokens used\n42\n!{kind}: {question}\nSummary.\n")
    txn = dispatch._begin_reconcile_transaction(
        entry, queue_dir=tmp_path / "queue", stale_s=0,
        need_queue=False, need_task_store=False, need_ledger=True,
        admission=dispatch.PreAdmitClass.CONFIRMED_DEAD,
    ).transaction
    assert txn is not None
    try:
        result, marker = dispatch._commit_claim_terminal_in_txn(txn, entry, reason="worker_dead")
    finally:
        txn.release()
    assert result.committed and result.durable_state == "blocked"
    assert marker["kind"] == kind
    rows = journal.Journal(tmp_path).read_all(
        "SELECT event_type, payload_json FROM terminal_outbox WHERE recipient = ?",
        (entry["dispatch_id"],),
    )
    expected_type = {"USER-NEED": "user_need", "USER-CONFIRM": "user_confirm"}.get(kind, "blocked")
    assert [(row["event_type"], json.loads(row["payload_json"])["text"]) for row in rows] == [
        (expected_type, question),
    ]


@pytest.mark.parametrize("kind", ["USER-NEED", "USER-CONFIRM", "BLOCKED", "FAILED", "READY", "COMPLETE", None])
def test_watcher_delivers_selected_terminal_marker(dead_terminal_entry, tmp_path, kind):
    entry, tail = dead_terminal_entry
    dispatch_id = entry["dispatch_id"]
    question = f"{dispatch_id} — Which release target should I use?"
    text = (
        f"tokens used\n100\n!READY: {dispatch_id} — earlier report\n"
        f"tool\n```bash\ntruncated\ntokens used\n200\n!{kind}: {question}\nSummary.\n"
    )
    success = kind in {"READY", "COMPLETE"}
    if success or kind is None:
        text = (
            "tokens used\n100\n"
            + (f"!{kind}: {question}\n" if kind else "No final report yet.\n")
            + "tool\n```bash\n"
            f"tokens used\n200\n```\n!COMPLETE: {dispatch_id} — quoted example\n```\nSummary.\n"
        )
    tail.write_text(text)
    status = tmp_path / "worker.status.json"
    _mark_attempt_running(tmp_path, dispatch_id, entry["queue_worker_identity"])
    _write_ledger_record(
        tmp_path, dispatch_id=dispatch_id, status_path=status,
        worker_pid=entry["queue_worker_pid"], worker_identity=entry["queue_worker_identity"],
    )
    watched = subprocess.run(
        [sys.executable, str(Path(watch.__file__)), "--pid", str(entry["queue_worker_pid"]),
         "--tail", str(tail), "--status-json", str(status), "--dispatch-id", dispatch_id,
         "--agent", "codex", "--poll-secs", "0.05", "--max-idle-secs", "0.2"],
        capture_output=True, text=True, timeout=20,
    )
    assert watched.returncode == (0 if success else 1 if kind is None else 4), watched.stderr
    payload = json.loads(status.read_text())
    assert payload["state"] == ("complete" if success else "worker_dead" if kind is None else "blocked")
    assert (payload.get("terminal_marker") or {}).get("kind") == kind
    rows = journal.Journal(tmp_path).read_all(
        "SELECT event_type, payload_json FROM terminal_outbox WHERE recipient = ?", (dispatch_id,),
    )
    if kind is None:
        assert len(rows) == 1 and rows[0]["event_type"] == "blocked"
        assert "quoted example" not in json.loads(rows[0]["payload_json"])["text"]
        return
    expected_type = {"USER-NEED": "user_need", "USER-CONFIRM": "user_confirm"}.get(kind, "blocked")
    if success:
        expected_type = "result"
    assert [(row["event_type"], json.loads(row["payload_json"])["text"]) for row in rows] == [
        (expected_type, question),
    ]
