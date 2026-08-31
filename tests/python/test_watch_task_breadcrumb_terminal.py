#!/usr/bin/env python3
"""Watch task breadcrumbs must not flatten inconclusive terminals to failure."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_task  # noqa: E402
import goalflight_watch  # noqa: E402


def case_task_state_for_terminal_keeps_inconclusive_distinct() -> None:
    """Reverting the mapper to `complete else worker-failed` fails this case."""
    mapper = goalflight_watch._task_state_for_terminal
    assert mapper("complete") == "worker-finished"
    assert mapper("worker_dead") == "worker-failed"
    assert mapper("blocked") == "worker-failed"
    assert mapper("error") == "worker-failed"
    assert mapper("failed") == "worker-failed"
    assert mapper("killed") == "worker-failed"
    for state in (
        "inconclusive_timeout",
        "inconclusive_no_final",
        "liveness_indeterminate",
        "idle_timeout",
    ):
        assert mapper(state) == "worker-inconclusive", state
        assert mapper(state) not in {"worker-failed", "worker-finished"}, state
        assert mapper(state) in goalflight_task.TASK_DISPATCH_STATES, state


def main() -> None:
    case_task_state_for_terminal_keeps_inconclusive_distinct()
    print("OK: watch task breadcrumb terminal mapping tests pass")


if __name__ == "__main__":
    main()
