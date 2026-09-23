"""Regression tests for chunk-summary worker liveness reconciliation."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, TypeVar
from unittest.mock import patch

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
        "worker_identity": {
            "lstart": "Tue Jun  9 09:00:00 2026",
            "start_token": "worker-token",
            "comm": "python3",
        },
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
        tail.write_text(
            "finished\nCOMPLETE: dead-complete — done\n",
            encoding="utf-8",
        )
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


def test_missing_identity_does_not_claim_pid_ownership() -> None:
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

    assert_eq("pid-only liveness is unknown", live, None)


def test_unknown_liveness_does_not_suggest_takeover() -> None:
    assert_eq("unknown liveness hint", summary.decision_hint("running", None, 1), "unknown")


def test_confirmed_live_worker_with_scraped_complete_stays_running_wait() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-summary-live-marker-") as d:
        base = Path(d)
        state_dir = base / "state"
        tail = base / "live-marker.tail"
        status_path = base / "live-marker.status.json"
        tail.write_text("shell loop\nfor x in y; do :; done\n", encoding="utf-8")
        record = _record(
            dispatch_id="live-marker",
            state="running",
            tail=tail,
            status_path=status_path,
        )
        _write_dispatch(
            state_dir,
            record,
            {
                "dispatch_id": "live-marker",
                "state": "running",
                "worker_pid": 4242,
                "tail_path": str(tail),
                "terminal_marker": {"kind": "COMPLETE", "text": "", "line": 2},
            },
        )

        payload = _with_identity(
            True,
            "live",
            lambda: summary.summarize("live-marker", state_dir),
        )

    assert_eq("live scraped marker state", payload["state"], "running")
    assert_eq("live scraped marker hint", payload["decision_hint"], "wait")
    assert_eq("live scraped marker remains diagnostic", payload["last_marker"], "COMPLETE")


def test_summary_agrees_with_status_tail_reconciled_complete() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-summary-agree-") as d:
        base = Path(d)
        state_dir = base / "state"
        tail = base / "agree.tail"
        status_path = base / "agree.status.json"
        tail.write_text(
            "READY: agree-complete — docs-private/research/findings.md\n"
            "trailing summary\n",
            encoding="utf-8",
        )
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


def test_capacity_failure_is_distinct_from_empty_in_json_and_text() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-summary-capacity-") as d:
        state_dir = Path(d)
        with patch.object(summary, "status_candidates", return_value=[]):
            empty_capacity = summary.run_capacity_status(state_dir)
            empty = summary.summarize("absent", state_dir)
            empty_text = summary.text_summary(empty)
            assert empty_capacity["active"] == []
            assert empty["state"] == "missing"
            assert empty.get("measured") is not False
            assert "error" not in empty
            assert "error=" not in empty_text

            (state_dir / "capacity.json").write_text("{broken", encoding="utf-8")
            failed_capacity = summary.run_capacity_status(state_dir)
            assert failed_capacity.get("measured") is False, failed_capacity
            assert failed_capacity["reason"] == "capacity_state_unreadable"
            failed = summary.summarize("absent", state_dir)
            assert failed["measured"] is False
            assert failed["error"] == failed_capacity["error"]
            assert failed["reason"] == failed_capacity["reason"]
            assert str(state_dir / "capacity.json") in failed["error"]
            assert failed != empty
            assert failed["decision_hint"] == "investigate"
            for mode in ("--json", "--text"):
                output = io.StringIO()
                with redirect_stdout(output):
                    summary.main(["--slug", "absent", "--state-dir", str(state_dir), mode])
                if mode == "--json":
                    assert json.loads(output.getvalue())["error"] == failed["error"]
                else:
                    assert "measured=false" in output.getvalue()
                    assert failed["reason"] in output.getvalue()
                    assert str(state_dir / "capacity.json") in output.getvalue()
                    assert output.getvalue().strip() != empty_text


def test_capacity_subprocess_errors_are_not_empty_leases() -> None:
    cases = [
        (subprocess.CompletedProcess([], 0, "not-json", ""), "JSON"),
        (subprocess.CompletedProcess([], 0, "[]", ""), "object"),
        (subprocess.CompletedProcess([], 7, "", "capacity exploded"), "capacity exploded"),
        (subprocess.CompletedProcess([], 7, '{"active":[]}', "capacity exploded"), "capacity exploded"),
        (OSError("capacity executable unavailable"), "capacity executable unavailable"),
    ]
    with tempfile.TemporaryDirectory(prefix="gf-summary-capacity-errors-") as d:
        with patch.object(summary.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, '{"active":[],"state":{"leases":{}}}', ""
        )), patch.object(summary, "status_candidates", return_value=[]):
            empty = summary.summarize("absent", Path(d))
        assert empty["state"] == "missing"
        assert empty.get("measured") is not False
        assert "error" not in empty
        for result, error in cases:
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with patch.object(summary.subprocess, "run", **kwargs), patch.object(summary, "status_candidates", return_value=[]):
                failed = summary.summarize("absent", Path(d))
            assert failed.get("measured") is False, (result, failed)
            assert failed != empty
            assert error in failed["error"], failed
            assert "measured=false" in summary.text_summary(failed)
            assert "measured=false" not in summary.text_summary(empty)


def test_corrupt_newer_ledger_row_qualifies_older_success() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-summary-ledger-") as d:
        state_dir = Path(d)
        _write_json(state_dir / "runs.d" / "older.json", {
            "dispatch_id": "older", "slug": "chunk", "state": "complete",
            "updated_at": "2026-09-11T00:00:00Z",
        })
        # Its name reveals neither project nor slug; unreadable evidence cannot
        # be ruled irrelevant to this shared ledger's requested chunk.
        corrupt = state_dir / "runs.d" / "newer.json"
        corrupt.write_text('{"updated_at":"2026-09-12T00:00:00Z",', encoding="utf-8")
        with patch.object(summary, "run_capacity_status", return_value={"active": []}), \
                patch.object(summary, "status_candidates", return_value=[]):
            payload = summary.summarize("chunk", state_dir)
            assert_eq("older success remains diagnostic", payload["dispatch_id"], "older")
            assert_eq("unreadable newer evidence is unmeasured", payload.get("measured"), False)
            assert_eq("unreadable newer evidence is not done", payload["decision_hint"], "investigate")
            assert str(corrupt) in payload["error"], payload
            for mode in ("--json", "--text"):
                output = io.StringIO()
                with redirect_stdout(output):
                    summary.main(["--slug", "chunk", "--state-dir", str(state_dir), mode])
                if mode == "--json":
                    assert json.loads(output.getvalue())["measured"] is False
                else:
                    assert "measured=false" in output.getvalue()
                assert str(corrupt) in output.getvalue()

        with patch.object(summary, "run_capacity_status", return_value={
            "measured": False, "error": "capacity unavailable", "reason": "capacity_state_unreadable",
        }), patch.object(summary, "status_candidates", return_value=[]):
            both = summary.summarize("chunk", state_dir)
        assert both["reason"] == "capacity_state_unreadable"
        assert "capacity unavailable" in both["error"]
        assert str(corrupt) in both["error"]
        assert both["decision_hint"] == "investigate"


def test_corrupt_ledger_row_is_distinct_from_genuinely_empty_slug() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-summary-ledger-empty-") as d:
        state_dir = Path(d)
        with patch.object(summary, "run_capacity_status", return_value={"active": []}), \
                patch.object(summary, "status_candidates", return_value=[]):
            empty = summary.summarize("absent", state_dir)
            assert empty["state"] == "missing"
            assert empty["dispatch_id"] is None
            assert empty.get("measured") is not False
            assert "error" not in empty
            corrupt = state_dir / "runs.d" / "unrelated-name.json"
            corrupt.parent.mkdir()
            corrupt.write_text("{broken", encoding="utf-8")
            failed = summary.summarize("absent", state_dir)
        assert_eq("corrupt-only ledger is unmeasured", failed.get("measured"), False)
        assert failed != empty
        assert failed["dispatch_id"] is None
        assert str(corrupt) in failed["error"]
        assert "measured=false" in summary.text_summary(failed)
        assert "measured=false" not in summary.text_summary(empty)


def test_clean_ledger_pair_chooses_newer_attempt() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-summary-ledger-clean-") as d:
        state_dir = Path(d)
        for dispatch_id, state, updated_at in (
            ("older", "complete", "2026-09-11T00:00:00Z"),
            ("newer", "failed", "2026-09-12T00:00:00Z"),
        ):
            _write_json(state_dir / "runs.d" / f"{dispatch_id}.json", {
                "dispatch_id": dispatch_id, "slug": "chunk", "state": state,
                "updated_at": updated_at,
            })
        with patch.object(summary, "run_capacity_status", return_value={"active": []}), \
                patch.object(summary, "status_candidates", return_value=[]):
            payload = summary.summarize("chunk", state_dir)
        assert payload["dispatch_id"] == "newer", payload
        assert payload["state"] == "failed", payload
        assert payload.get("measured") is not False
        assert "error" not in payload


def main() -> None:
    tests = [
        test_corrupt_newer_ledger_row_qualifies_older_success,
        test_corrupt_ledger_row_is_distinct_from_genuinely_empty_slug,
        test_clean_ledger_pair_chooses_newer_attempt,
        test_capacity_failure_is_distinct_from_empty_in_json_and_text,
        test_capacity_subprocess_errors_are_not_empty_leases,
        test_idle_detached_identity_live_reads_running_wait,
        test_dead_worker_complete_tail_reads_complete,
        test_recycled_pid_identity_mismatch_is_not_alive,
        test_missing_identity_does_not_claim_pid_ownership,
        test_confirmed_live_worker_with_scraped_complete_stays_running_wait,
        test_summary_agrees_with_status_tail_reconciled_complete,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_chunk_summary_liveness.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
