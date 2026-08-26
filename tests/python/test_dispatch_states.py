#!/usr/bin/env python3
"""Regression tests for shared dispatch state vocabulary."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch_states as states  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_chunk_summary as chunk_summary  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402


def test_dispatch_state_aliases_and_lifecycle() -> None:
    assert states.normalize_dispatch_state("waiting_capacity") == "waiting"
    assert states.is_running_state("waiting_capacity") is True
    assert states.is_terminal_state("idle_timeout") is True
    assert states.is_terminal_state("blocked_capacity") is True
    assert states.is_running_state("watcher_stopped") is True
    assert states.is_terminal_state("watcher_stopped") is False
    assert states.is_terminal_state("controller_dead") is True
    assert states.is_terminal_state("rate_limited") is True
    assert states.is_terminal_state("quota_exhausted") is True
    assert states.is_terminal_state("transient_throttle") is True
    assert states.is_terminal_state("limit_unknown") is True
    assert states.normalize_dispatch_state("rate_limited") == "limit_unknown"
    assert (
        states.limit_kind_for_record(
            {
                "terminal_state": "rate_limited",
                "outcome": {"limit_kind": "exhausted"},
            }
        )
        == "unknown"
    )
    assert states.terminal_state_for("watcher_stopped") == "watcher_stopped"
    assert states.terminal_state_for("controller_dead") == "controller_dead"
    assert states.terminal_state_for("rate_limited") == "rate_limited"
    assert states.state_seq_rank("watcher_stopped") == 45
    # Live salvage observation: process still exists and may recover.
    # Distinct from ACP terminal `wedged` and from `worker_dead`.
    assert states.WORKER_STALLED_CANDIDATE_STATE == "worker_stalled_candidate"
    assert states.is_terminal_state("worker_stalled_candidate") is False
    assert states.is_running_state("worker_stalled_candidate") is False
    assert states.terminal_state_for("worker_stalled_candidate") == "unknown"
    assert states.is_terminal_state("wedged") is True
    assert states.terminal_state_for("wedged") == "error"
    assert states.is_terminal_state("worker_dead") is True
    assert states.terminal_state_for("worker_dead") == "worker_dead"


def test_limit_retry_policy_holds_exhausted_and_retries_transient() -> None:
    reset_at = "2033-05-18T03:33:20+00:00"
    exhausted = states.retry_policy_for_record(
        {"state": "quota_exhausted", "reset_at": reset_at},
        now=1_900_000_000.0,
    )
    transient = states.retry_policy_for_record(
        {"state": "transient_throttle"},
        now=1_900_000_000.0,
    )
    legacy = states.retry_policy_for_record(
        {"state": "rate_limited"},
        now=1_900_000_000.0,
    )

    assert exhausted == {
        "kind": "exhausted",
        "eligible": False,
        "not_before": reset_at,
        "mode": "retry_after_reset",
    }
    assert transient == {
        "kind": "transient",
        "eligible": True,
        "not_before": None,
        "mode": "retry_soon",
    }
    assert legacy == {
        "kind": "unknown",
        "eligible": None,
        "not_before": None,
        "mode": "legacy_cooldown",
    }
    assert chunk_summary.retryable_failure_present(
        {"state": "quota_exhausted", "reset_at": reset_at}, None
    ) is False
    assert chunk_summary.retryable_failure_present(
        {"state": "transient_throttle"}, None
    ) is True
    assert chunk_summary.retryable_failure_present(
        {"state": "rate_limited"}, None
    ) is True
    assert chunk_summary.decision_hint(
        "failed", False, 1, retry_policy=exhausted
    ) == "hold_until_reset"
    assert chunk_summary.decision_hint(
        "failed", False, 1, retry_policy=transient
    ) == "retry_soon"


def test_ledger_uses_shared_terminal_vocabulary() -> None:
    for state in (
        "controller_dead",
        "tool_timeout",
        "remote_turn_silence",
        "stalled",
        "failed_worktree",
        "blocked_adapter_gate",
        "rate_limited",
        "wedged",
        "error",
    ):
        assert ledger.classify({"state": state}) == state


def test_dispatch_controller_uses_shared_terminal_vocabulary() -> None:
    assert dispatch._is_status_terminal("watcher_stopped") is False
    assert dispatch._is_status_terminal("controller_dead") is True
    assert dispatch._is_live_watcher_stopped("watcher_stopped", True) is True
    assert dispatch._is_live_watcher_stopped("watcher_stopped", False) is False


def test_dispatch_reuse_guard_blocks_live_watcher_stopped() -> None:
    record = {
        "state": "watcher_stopped",
        "worker_pid": 12345,
        "worker_identity": {"pid": 12345, "comm": "python3", "lstart": "Tue Jun  9 09:00:00 2026"},
        "status_path": "/tmp/watcher.status.json",
    }
    orig_find = dispatch._find_dispatch_record
    orig_identity_matches = ledger.identity_matches
    try:
        dispatch._find_dispatch_record = lambda dispatch_id: record
        ledger.identity_matches = lambda candidate: (True, "live")
        reason = dispatch._nonterminal_dispatch_reuse_reason("watcher-live")
        assert reason is not None
        assert "classification=expected_live" in reason

        ledger.identity_matches = lambda candidate: (False, "dead")
        assert dispatch._nonterminal_dispatch_reuse_reason("watcher-dead") is None
    finally:
        dispatch._find_dispatch_record = orig_find
        ledger.identity_matches = orig_identity_matches


def main() -> None:
    test_dispatch_state_aliases_and_lifecycle()
    test_limit_retry_policy_holds_exhausted_and_retries_transient()
    test_ledger_uses_shared_terminal_vocabulary()
    test_dispatch_controller_uses_shared_terminal_vocabulary()
    test_dispatch_reuse_guard_blocks_live_watcher_stopped()
    print("OK: dispatch state tests pass")


if __name__ == "__main__":
    main()
