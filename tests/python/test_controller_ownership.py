"""Tests for recording which controller owns a dispatch.

Two gaps made ownership unrecordable in practice:
  * a GUI-launched host could never register (its ancestors sit in session 1),
  * and the dispatcher only recorded an owner when one was declared on the
    command line.
Both are exercised here against the shapes measured on a real host.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_session_status as sessions  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402


def _proc(pid: int, ppid: int, session_id: int) -> dict:
    return {"pid": pid, "ppid": ppid, "session_id": session_id, "start_token": f"t:{pid}"}


# --- durable ancestor selection ----------------------------------------

def test_gui_launched_host_is_selectable_although_its_session_is_one() -> None:
    """Measured shape of a desktop-launched controller.

    Every ancestor above the transient tool-call session sits in session 1,
    whose leader is launchd. Refusing session 1 outright made registration
    impossible for this entire class of host -- which is a measurement gap, not
    a drift toward init: the spawner is the host process itself.
    """
    ancestry = (
        _proc(1561, 1560, 1558),   # this helper, transient session
        _proc(1560, 1558, 1558),
        _proc(1558, 80650, 1558),  # leader of the transient session
        _proc(80650, 80649, 1),    # <- the host process that spawned it
        _proc(80649, 30936, 1),
        _proc(30936, 1, 1),        # the enclosing application
    )
    selected = sessions._select_durable_controller_ancestor(ancestry)
    assert selected is not None, "a desktop-launched host must be registrable"
    assert selected["pid"] == 80650, "select the spawner, not the shared application"


def test_terminal_shape_still_selects_the_outer_session_leader() -> None:
    """Unchanged behaviour where an outer session leader does exist."""
    ancestry = (
        _proc(500, 499, 498),
        _proc(499, 498, 498),
        _proc(498, 400, 498),
        _proc(410, 400, 400),
        _proc(400, 1, 400),   # leader of the outer session
    )
    selected = sessions._select_durable_controller_ancestor(ancestry)
    assert selected is not None and selected["pid"] == 400


def test_init_is_never_selected() -> None:
    """The original intent stands: never bind identity to PID 1."""
    ancestry = (
        _proc(300, 299, 298),
        _proc(299, 298, 298),
        _proc(298, 1, 298),
        _proc(1, 0, 1),
    )
    assert sessions._select_durable_controller_ancestor(ancestry) is None


def test_no_outer_session_yields_nothing() -> None:
    ancestry = (_proc(300, 299, 298), _proc(299, 298, 298))
    assert sessions._select_durable_controller_ancestor(ancestry) is None


# --- owner inference at dispatch time ----------------------------------

def _fake_registry(monkeypatch, live: dict[str, int | None]) -> None:
    monkeypatch.setattr(
        dispatch.goalflight_session_status,
        "registered_controller_labels",
        lambda root: set(live),
    )

    def live_session(root, label=None, **kwargs):
        pid = live.get(label)
        return {"label": label, "pid": pid} if pid else None

    monkeypatch.setattr(
        dispatch.goalflight_session_status, "live_session", live_session
    )


def test_one_live_controller_is_adopted(monkeypatch, tmp_path: Path) -> None:
    """Input path: a registered, live controller dispatching without declaring
    itself -- which is every dispatch driven through short-lived shells."""
    _fake_registry(monkeypatch, {"goal-flight": 80650, "stale": None})
    assert dispatch._sole_live_controller(tmp_path) == ("goal-flight", 80650)


def test_several_live_controllers_are_never_guessed_between(
    monkeypatch, tmp_path: Path
) -> None:
    """One repo can host several controllers; battery-tool-v2 runs three.

    Attributing a dispatch to the wrong one is worse than leaving it unowned:
    unowned is visibly unknown, a wrong owner reads as fact and would route
    another controller's mail.
    """
    _fake_registry(monkeypatch, {"bugs": 1, "webui": 2, "main": 3})
    assert dispatch._sole_live_controller(tmp_path) is None


def test_no_live_controller_yields_nothing(monkeypatch, tmp_path: Path) -> None:
    _fake_registry(monkeypatch, {"dead": None})
    assert dispatch._sole_live_controller(tmp_path) is None


def test_a_registry_failure_cannot_fail_a_launch(monkeypatch, tmp_path: Path) -> None:
    """Ownership is metadata; it must never be able to stop a dispatch."""
    def boom(*args, **kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(
        dispatch.goalflight_session_status, "registered_controller_labels", boom
    )
    assert dispatch._sole_live_controller(tmp_path) is None


def test_a_conflicted_beacon_is_not_adopted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        dispatch.goalflight_session_status,
        "registered_controller_labels",
        lambda root: {"goal-flight"},
    )
    monkeypatch.setattr(
        dispatch.goalflight_session_status,
        "live_session",
        lambda root, label=None, **kw: {
            "label": label, "pid": 42, "conflicting_beacons": True
        },
    )
    assert dispatch._sole_live_controller(tmp_path) is None
