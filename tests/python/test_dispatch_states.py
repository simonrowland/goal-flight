#!/usr/bin/env python3
"""Regression tests for shared dispatch state vocabulary."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch_states as states  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402


def test_dispatch_state_aliases_and_lifecycle() -> None:
    assert states.normalize_dispatch_state("waiting_capacity") == "waiting"
    assert states.is_running_state("waiting_capacity") is True
    assert states.is_terminal_state("idle_timeout") is True
    assert states.is_terminal_state("blocked_capacity") is True
    assert states.is_running_state("watcher_stopped") is True
    assert states.is_terminal_state("watcher_stopped") is False
    assert states.is_terminal_state("controller_dead") is True
    assert states.terminal_state_for("watcher_stopped") == "watcher_stopped"
    assert states.terminal_state_for("controller_dead") == "controller_dead"
    assert states.state_seq_rank("watcher_stopped") == 45


def test_ledger_uses_shared_terminal_vocabulary() -> None:
    for state in (
        "controller_dead",
        "tool_timeout",
        "remote_turn_silence",
        "stalled",
        "failed_worktree",
        "blocked_adapter_gate",
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
    test_ledger_uses_shared_terminal_vocabulary()
    test_dispatch_controller_uses_shared_terminal_vocabulary()
    test_dispatch_reuse_guard_blocks_live_watcher_stopped()
    print("OK: dispatch state tests pass")


if __name__ == "__main__":
    main()
