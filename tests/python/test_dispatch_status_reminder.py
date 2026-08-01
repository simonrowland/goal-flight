#!/usr/bin/env python3
"""Unit tests for post-dispatch status-tooling reminder lines."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch  # noqa: E402


def _reminder_text(shape: str) -> tuple[str, Path, Path]:
    status_json = Path("/tmp/goal-flight-state/dispatch/reminder-dispatch-42.status.json")
    tail_path = Path("/tmp/goal-flight-state/dispatch/reminder-dispatch-42.tail")
    lines = goalflight_dispatch._status_reminder_lines(
        "reminder-dispatch-42",
        status_json=status_json,
        tail_path=tail_path,
        worker_pid=4242,
        shape=shape,
        skill_root=ROOT,
        agent="codex",
        controller_pid=9999,
        poll_secs=2.0,
        max_idle_secs=180.0,
    )
    return "\n".join(lines), status_json.resolve(), tail_path.resolve()


def test_status_reminder_bash_shape() -> None:
    text, _status_json, tail = _reminder_text("bash")
    assert "reminder-dispatch-42" in text
    assert "--dispatch reminder-dispatch-42" in text
    assert "--wait reminder-dispatch-42" in text
    assert "--done reminder-dispatch-42" in text
    assert "goalflight_messages.py relay" in text
    assert "current project, open + unread" in text
    assert "0=terminal" in text and "1=running" in text and "2=ambiguous" in text
    assert "do NOT hand-roll" in text
    assert "watch-dispatch-tail.sh" in text
    assert "--pid 4242" in text
    assert f"--tail {tail}" in text
    assert "--controller-pid 9999" in text
    assert "--agent codex-bash-tail" in text
    assert "--session-id reminder-dispatch-42" in text
    assert "--poll-secs 2.0" in text
    assert "--max-idle-secs 180.0" in text
    assert "goalflight_watch.py" not in text


def test_status_reminder_acp_shape() -> None:
    text, status_json, tail = _reminder_text("acp")
    assert "reminder-dispatch-42" in text
    assert "--dispatch reminder-dispatch-42" in text
    assert "--wait reminder-dispatch-42" in text
    assert "--done reminder-dispatch-42" in text
    assert "goalflight_messages.py relay" in text
    assert "current project, open + unread" in text
    assert "0=terminal" in text and "1=running" in text and "2=ambiguous" in text
    assert "do NOT hand-roll" in text
    assert "goalflight_watch.py" in text
    assert "--pid 4242" in text
    assert f"--tail {tail}" in text
    assert f"--status-json {status_json}" in text
    assert "watch-dispatch-tail.sh" not in text


def test_wait_hint_teaches_the_backgrounded_form() -> None:
    """The hint must show a wait a controller can actually run.

    Printed as a bare foreground command it collided with the rule against long
    foreground calls, leaving no legal move: the same hint forbids hand-rolled
    watchers, so controllers hand-rolled pollers anyway. One was seen polling a
    harness task file, which cannot observe `awaiting_user_confirm` at all -- a
    worker paused for approval looked exactly like a worker still working.

    Pinned because stripping this advice left every existing assertion green:
    the hint's *commands* were tested, the guidance that makes them usable was
    not.
    """
    for shape in ("bash", "acp"):
        text, _status_json, _tail = _reminder_text(shape)
        assert "--wait reminder-dispatch-42" in text, shape
        assert "BACKGROUND" in text, f"{shape}: hint must say to background the wait"
        assert "timer" in text, f"{shape}: hint must warn against substituting a timer"
        # The reason has to travel with the instruction, or it reads as arbitrary.
        assert "clock" in text, f"{shape}: hint must say why a timer is wrong"


def main() -> None:
    test_status_reminder_bash_shape()
    test_status_reminder_acp_shape()
    test_wait_hint_teaches_the_backgrounded_form()
    print("OK: dispatch status reminder tests pass")


if __name__ == "__main__":
    main()
