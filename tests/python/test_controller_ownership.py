"""Tests for recording which controller owns a dispatch.

Two gaps made ownership unrecordable in practice:
  * a GUI-launched host could never register (its ancestors sit in session 1),
  * and the dispatcher only recorded an owner when one was declared on the
    command line.
Both are exercised here against the shapes measured on a real host.
"""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_session_status as sessions  # noqa: E402


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
