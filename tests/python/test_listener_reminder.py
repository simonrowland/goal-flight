"""Tests for the listener/registration reminder.

The process table is injected as text, so nothing here depends on what happens
to be running on the machine.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

MODULE_PATH = SCRIPTS / "goalflight_messages.py"
SPEC = importlib.util.spec_from_file_location("test_target_gf_messages", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
msgs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = msgs
SPEC.loader.exec_module(msgs)

PROJECT = str(ROOT)


def _ps(label: str, root: str = PROJECT, pid: int = 4242) -> str:
    return (
        f"{pid} /usr/bin/python3 /somewhere/goalflight_messages.py listen "
        f"--controller {label} --project-root {root}"
    )


def test_matches_only_this_controllers_listener() -> None:
    """One repo can host several controllers, so a sibling's listener must not
    count as mine -- battery-tool-v2 runs three."""
    mine = msgs.live_listener_pids(PROJECT, "battery-bugs", ps_output=_ps("battery-bugs"))
    assert mine == [4242]

    sibling = msgs.live_listener_pids(
        PROJECT, "battery-bugs", ps_output=_ps("battery-webui")
    )
    assert sibling == [], "a sibling controller's listener is not mine"


def test_a_listener_for_another_project_does_not_count() -> None:
    other = msgs.live_listener_pids(
        PROJECT, "bugs", ps_output=_ps("bugs", root="/somewhere/else")
    )
    assert other == []


@pytest.mark.parametrize(
    "line",
    [
        "1 /usr/bin/python3 /x/goalflight_messages.py relay --new",  # not listen
        "2 /usr/bin/python3 /x/other_tool.py listen --controller bugs",  # not ours
        "3 grep listen --controller bugs",  # someone grepping for it
    ],
)
def test_non_listener_processes_are_not_counted(line: str) -> None:
    assert msgs.live_listener_pids(PROJECT, "bugs", ps_output=line) == []


def test_unreadable_process_table_is_cannot_tell_not_absent(monkeypatch) -> None:
    """None and [] must stay distinct.

    If an unreadable table collapsed to "none found", a controller that is in
    fact listening would be nagged whenever ps failed.
    """
    def boom(*args, **kwargs):
        raise OSError("no ps here")

    monkeypatch.setattr(msgs.subprocess, "run", boom)
    assert msgs.live_listener_pids(PROJECT, "bugs") is None

    stream = io.StringIO()
    assert (
        msgs.emit_listener_reminder(
            project_root=PROJECT, controller_label="bugs", exposure=5, stream=stream
        )
        is None
    )
    assert stream.getvalue() == "", "must not assert an absence it never measured"


def test_reminds_when_measured_absent_and_something_is_at_stake() -> None:
    stream = io.StringIO()
    line = msgs.emit_listener_reminder(
        project_root=PROJECT, controller_label="bugs", exposure=1,
        stream=stream, ps_output="",
    )
    assert line is not None
    assert "no live mail listener" in line
    # the reminder must carry the command, not merely report the absence
    assert "listen --controller bugs" in line
    assert stream.getvalue().strip() == line


def test_silent_when_a_listener_is_running() -> None:
    stream = io.StringIO()
    assert (
        msgs.emit_listener_reminder(
            project_root=PROJECT, controller_label="bugs", exposure=9,
            stream=stream, ps_output=_ps("bugs"),
        )
        is None
    )
    assert stream.getvalue() == ""


def test_silent_when_nothing_is_at_stake() -> None:
    """No open dispatches and no unread mail: a listener buys nothing, so this
    stays quiet instead of nagging. That gating is also why no throttle
    timestamp is needed."""
    stream = io.StringIO()
    assert (
        msgs.emit_listener_reminder(
            project_root=PROJECT, controller_label="bugs", exposure=0,
            stream=stream, ps_output="",
        )
        is None
    )
    assert stream.getvalue() == ""


def test_an_unidentifiable_caller_is_told_to_register() -> None:
    """Measured while building this: every owned dispatch on a live project
    carried no controller label and no registered label had a live session, so
    the caller could not be named. Reporting only the missing listener would
    stay silent for exactly the controller that has none."""
    stream = io.StringIO()
    line = msgs.emit_listener_reminder(
        project_root=PROJECT, controller_label=None, exposure=4,
        stream=stream, ps_output="",
    )
    assert line is not None
    assert "not registered" in line
    assert "--controller-startup" in line, "must carry the registration command"


def test_summary_exposes_the_controller_label_the_reminder_needs() -> None:
    """The reminder asks about THIS controller, so the summary has to say who
    that is; without the key the reminder silently never fires."""
    summary = msgs.controller_mail_summary(task_store_project_root=Path(PROJECT))
    assert "controller_label" in summary
