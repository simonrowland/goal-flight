#!/usr/bin/env python3
"""The controller session beacon: a stable, externally-answerable identity.

ensure_session() keys a record by the CALLER's pid. That is right for a human
at a long-lived terminal and useless for a controller that reaches the CLI
through one-shot tool calls -- every call is a fresh python3 process, so every
call minted a new id. Measured before this existed: three consecutive
--ensure-session invocations returned three different ids.

The beacon anchors identity to a long-running process instead. Its pid is
stable while the controller works, and it is observable from outside, which is
what makes "is this worker mine?" and "is that controller still alive?"
measurable rather than inferred.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_session_status as S  # noqa: E402


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _beacon() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def test_identity_is_stable_across_separate_resolutions() -> None:
    """The whole point: repeated lookups agree.

    This is the property ensure_session could not provide, and the one most
    likely to rot silently -- nothing else fails loudly if ids start drifting;
    ownership just quietly stops matching.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        try:
            claimed = S.claim_session(root, pid=proc.pid, label="controller")
            seen = {S.live_session(root)["id"] for _ in range(5)}
            assert_true("every resolution returns one id", seen == {claimed["id"]})
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_claim_is_idempotent_for_the_same_beacon() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        try:
            first = S.claim_session(root, pid=proc.pid)
            second = S.claim_session(root, pid=proc.pid)
            assert_true("re-claiming does not mint a new id", first["id"] == second["id"])
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_dead_beacon_resolves_to_none_not_a_stale_id() -> None:
    """A dead controller must not keep answering for its workers.

    Returning the last-known id here would be the defect this project keeps
    hitting: a field asserting a state nobody measured. Ownership would survive
    the owner.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        S.claim_session(root, pid=proc.pid)
        assert_true("live while the beacon runs", S.live_session(root) is not None)
        proc.terminate()
        proc.wait(timeout=10)
        assert_true("None once the beacon is gone", S.live_session(root) is None)


def test_no_beacon_is_none_rather_than_an_invented_owner() -> None:
    """None means 'nobody has claimed this project', not 'idle'."""
    with tempfile.TemporaryDirectory() as td:
        assert_true("unclaimed project has no session", S.live_session(Path(td)) is None)


def test_second_live_beacon_is_reported_not_silently_arbitrated() -> None:
    """Two controllers in one project is a takeover or a stray. Say so.

    Picking one without a word is how an operator ends up debugging why half
    their workers answer to a session they never started.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        first, second = _beacon(), _beacon()
        try:
            S.claim_session(root, pid=first.pid)
            S.claim_session(root, pid=second.pid)
            live = S.live_session(root)
            assert_true("a winner is still chosen", live is not None)
            assert_true("the collision is surfaced", live.get("conflicting_beacons") == 2)
        finally:
            for proc in (first, second):
                proc.terminate()
                proc.wait(timeout=10)


def test_beacon_slots_do_not_disturb_per_terminal_sessions() -> None:
    """ensure_session keeps its existing meaning alongside beacons."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        try:
            terminal = S.ensure_session(root)
            S.claim_session(root, pid=proc.pid)
            again = S.ensure_session(root)
            assert_true("per-terminal record survives a claim", terminal["id"] == again["id"])
            assert_true("and is not mistaken for the beacon",
                        S.live_session(root)["id"] != terminal["id"])
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_release_drops_the_beacon() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        try:
            S.claim_session(root, pid=proc.pid)
            assert_true("release reports removal", S.release_session(root, pid=proc.pid) is True)
            assert_true("session is gone", S.live_session(root) is None)
            assert_true("releasing twice is False", S.release_session(root, pid=proc.pid) is False)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def main() -> None:
    test_identity_is_stable_across_separate_resolutions()
    test_claim_is_idempotent_for_the_same_beacon()
    test_dead_beacon_resolves_to_none_not_a_stale_id()
    test_no_beacon_is_none_rather_than_an_invented_owner()
    test_second_live_beacon_is_reported_not_silently_arbitrated()
    test_beacon_slots_do_not_disturb_per_terminal_sessions()
    test_release_drops_the_beacon()
    print("OK: session beacon tests pass")


if __name__ == "__main__":
    main()
