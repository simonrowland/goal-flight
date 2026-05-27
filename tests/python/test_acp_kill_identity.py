#!/usr/bin/env python3
"""Regression tests for ACP worker PID identity checks."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from goalflight_acp_client import _same_process  # noqa: E402


def case_exec_comm_change_keeps_identity() -> None:
    started = ("Wed May 20 17:55:24 2026", "bash")
    live = ("Wed May 20 17:55:24 2026", "/Users/example/.local/bin/cursor-agent")
    assert _same_process(started, live) is True


def case_pid_reuse_lstart_change_is_different() -> None:
    started = ("Wed May 20 17:55:24 2026", "cursor-agent")
    live = ("Wed May 20 17:55:25 2026", "cursor-agent")
    assert _same_process(started, live) is False


def case_unavailable_meta_preserves_kill_fallthrough() -> None:
    live = ("Wed May 20 17:55:24 2026", "cursor-agent")
    assert _same_process(None, live) is True
    assert _same_process(live, None) is True


def main() -> None:
    case_exec_comm_change_keeps_identity()
    case_pid_reuse_lstart_change_is_different()
    case_unavailable_meta_preserves_kill_fallthrough()
    print("OK: ACP kill identity tests pass")


if __name__ == "__main__":
    main()

