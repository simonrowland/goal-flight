"""Per-test isolation of machine-global Goal Flight writable surfaces.

b-238: the wake suite coordinated through real files and locks. Isolation used
to be per-test discipline — some fixtures remembered ``GOAL_FLIGHT_PIDFILE_DIR``,
some forgot ``GOALFLIGHT_DISPATCH_DIR``, and tests that never requested
``isolated`` inherited the process environment (live ``/tmp/goal-flight-<uid>``,
``/tmp/goal-flight-acp-pids.d``, and ambient controller identity). Autouse this
fixture so a future test inherits isolation instead of having to remember it.

Shared-state inventory (what a wake-suite test can write that another can see)
---------------------------------------------------------------------------

Resource                  Env / default                                         Isolated by this fixture
------------------------- ----------------------------------------------------- -------------------------
journal sqlite + write lk ``GOALFLIGHT_JOURNAL_DIR`` else task-store parent     yes
wake-ledger dir           ``GOALFLIGHT_WAKE_LEDGER_DIR`` else XDG/home state    yes
  - listener-slot-N.lock  under wake-ledger / project-key                       yes (via ledger dir)
  - waiter / generation   under wake-ledger / project-key                       yes
  - pending-report claim  under wake-ledger / project-key                       yes
  - ring stamp            under wake-ledger / project-key                       yes
pidfile dir               ``GOAL_FLIGHT_PIDFILE_DIR`` else ``/tmp/goal-flight-acp-pids.d``
                          (alias ``GOALFLIGHT_PIDFILE_DIR`` used by some paths) yes, both names
state dir                 ``GOALFLIGHT_STATE_DIR`` else ``/tmp/goal-flight-<uid>``  yes
dispatch dir              ``GOALFLIGHT_DISPATCH_DIR`` else ``<state>/dispatch`` yes (set explicitly)
messages dir              ``GOALFLIGHT_MESSAGES_DIR``                           yes
task store                ``GOALFLIGHT_TASK_STORE_DIR``                         yes
fleet dir                 ``GOALFLIGHT_FLEET_DIR``                              yes
capacity conf             live ``~/.goal-flight/capacity.local.json``           ``/dev/null``
ambient identity          ``GOALFLIGHT_DISPATCH_ID`` / prompt / steer / lease   scrubbed

Pre-fix fixture completeness in the six-file selection (function-scoped
``isolated`` when requested; tests that omit it inherited process env):

file                           journal  ledger  pidfile*  state  dispatch  messages  fleet  ambient scrub
------------------------------ -------- ------- --------- ------ --------- --------- ------ --------------
test_wake_layer.py             Y        Y       GOAL_     Y      Y         Y         Y      partial
test_follow_listener.py        Y        Y       GOAL_     Y      Y         Y         Y      partial
test_listener_arm_pending.py   Y        Y       GOAL_     Y      no        Y         no     incomplete
test_listener_terse_startup.py Y        Y       GOAL_     Y      no        Y         no     incomplete
test_depth_without_ceremony.py Y        Y       GOAL_     Y      no        Y         no     incomplete
test_supervised_wake.py        Y        Y       GOAL_     Y      no        Y         no     incomplete

``*`` pidfile: fixtures set ``GOAL_FLIGHT_PIDFILE_DIR`` (the production name)
but not the ``GOALFLIGHT_PIDFILE_DIR`` alias. Tests without ``isolated`` in
those modules (string/hint unit tests, FakeHost supervisor tests) touched no
files, except where a helper used the repo/tmp default project and the process
wake-ledger/pidfile dir.

Opt out with ``@pytest.mark.live_machine_state`` for tests whose subject is
the live default path.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest
from support import AMBIENT_IDENTITY_ENV, MACHINE_PATH_ENV, isolated_machine_env


T = TypeVar("T")


def apply_isolated_machine_env(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra: dict[str, str] | None = None,
    keep: tuple[str, ...] = (),
) -> dict[str, str]:
    """Pin machine paths and scrub ambient controller/worker identity."""
    for key in AMBIENT_IDENTITY_ENV:
        if key in keep:
            continue
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GOALFLIGHT_WAKE_LEDGER", raising=False)
    env = isolated_machine_env(root)
    if extra:
        env.update(extra)
        for key, value in extra.items():
            if key in MACHINE_PATH_ENV and value and value != os.devnull:
                Path(value).mkdir(parents=True, exist_ok=True)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def wait_until(
    predicate: Callable[[], T],
    *,
    timeout_s: float,
    interval_s: float = 0.02,
    message: str = "condition",
) -> T:
    """Poll *predicate* until it returns a truthy value or *timeout_s* elapses."""
    deadline = time.monotonic() + timeout_s
    last: T | None = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    raise AssertionError(f"timed out waiting for {message}; last={last!r}")


def isolate_goalflight_machine_state_impl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> dict[str, str] | None:
    if request.node.get_closest_marker("live_machine_state") is not None:
        return None
    return apply_isolated_machine_env(tmp_path, monkeypatch)
