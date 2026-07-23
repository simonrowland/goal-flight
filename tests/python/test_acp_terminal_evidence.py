#!/usr/bin/env python3
"""Regression tests for evidence-backed ACP terminal classification."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "python"))

_original_acp_python = os.environ.get("GOALFLIGHT_ACP_PYTHON")
os.environ["GOALFLIGHT_ACP_PYTHON"] = sys.executable
from goalflight_acp_run import (  # noqa: E402
    _state_after_actionable_terminal_markers,
    decide_terminal_state,
)
if _original_acp_python is None:
    os.environ.pop("GOALFLIGHT_ACP_PYTHON", None)
else:
    os.environ["GOALFLIGHT_ACP_PYTHON"] = _original_acp_python
BASE = {
    "result_ok": True,
    "result_error": None,
    "stop_reason": "end_turn",
    "heartbeat_outcome": None,
    "killed_by_heartbeat": False,
    "cancelled_for_marker": False,
    "early_marker": None,
    "heartbeat_error": None,
}


def test_empty_clean_close_fails_with_auth_hint() -> None:
    state, error = decide_terminal_state(
        **BASE,
        result_text="",
        agent="claude",
        events_seen=0,
        successful_terminal_marker=False,
    )

    assert state == "failed", (state, error)
    assert error and error["reason"] == "empty_session", error
    assert error["events_seen"] == 0, error
    assert "claude auth status" in str(error["hint"]), error


def test_nonempty_result_text_remains_complete() -> None:
    state, error = decide_terminal_state(
        **BASE,
        result_text="CANARY-OK",
        agent="claude",
        events_seen=7,
        successful_terminal_marker=False,
    )

    assert state == "complete", (state, error)
    assert error is None, error


def test_successful_terminal_marker_remains_complete_without_text() -> None:
    state, error = decide_terminal_state(
        **BASE,
        result_text="",
        agent="claude",
        events_seen=0,
        successful_terminal_marker=True,
    )

    assert state == "complete", (state, error)
    assert error is None, error


def test_blocking_terminal_marker_retains_existing_reconciliation() -> None:
    markers = {"BLOCKED": ["waiting for maintainer"]}
    state, error = decide_terminal_state(
        **BASE,
        result_text="",
        agent="claude",
        events_seen=0,
        terminal_marker_present=True,
        successful_terminal_marker=False,
    )

    assert state == "complete", (state, error)
    assert error is None, error
    assert _state_after_actionable_terminal_markers(state, markers) == "blocked"


def test_whitespace_only_result_text_is_empty() -> None:
    state, error = decide_terminal_state(
        **BASE,
        result_text=" \n\t ",
        agent="claude",
        events_seen=0,
        successful_terminal_marker=False,
    )

    assert state == "failed", (state, error)
    assert error and error["reason"] == "empty_session", error


def test_tool_events_without_output_are_not_completion_evidence() -> None:
    state, error = decide_terminal_state(
        **BASE,
        result_text=None,
        agent="claude",
        events_seen=75,
        successful_terminal_marker=False,
    )

    assert state == "failed", (state, error)
    assert error and error["reason"] == "empty_session", error
    assert error["events_seen"] == 75, error


def test_any_assistant_output_remains_completion_evidence() -> None:
    state, error = decide_terminal_state(
        **BASE,
        result_text="",
        assistant_output="Output from an earlier ACP turn.",
        agent="claude",
        events_seen=12,
        successful_terminal_marker=False,
    )

    assert state == "complete", (state, error)
    assert error is None, error


def test_nonempty_result_text_wins_over_empty_assistant_output_alias() -> None:
    state, error = decide_terminal_state(
        **BASE,
        result_text="Final response.",
        assistant_output="",
        agent="claude",
        events_seen=7,
        successful_terminal_marker=False,
    )

    assert state == "complete", (state, error)
    assert error is None, error


def test_bash_shape_terminal_regression_pair() -> None:
    # ACP's evidence gate must not leak into the independent bash-tail watcher.
    code = """
import test_dispatch_crash_safe as regressions
regressions.case_dispatch_clean_complete_preserves_reason_without_rate_signal()
regressions.case_dispatch_worker_dead_ledger_liveness()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "tests" / "python")},
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def main() -> None:
    test_empty_clean_close_fails_with_auth_hint()
    test_nonempty_result_text_remains_complete()
    test_successful_terminal_marker_remains_complete_without_text()
    test_blocking_terminal_marker_retains_existing_reconciliation()
    test_whitespace_only_result_text_is_empty()
    test_tool_events_without_output_are_not_completion_evidence()
    test_any_assistant_output_remains_completion_evidence()
    test_nonempty_result_text_wins_over_empty_assistant_output_alias()
    test_bash_shape_terminal_regression_pair()
    print("OK: ACP terminal evidence tests pass")


if __name__ == "__main__":
    main()
