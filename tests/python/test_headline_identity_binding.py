#!/usr/bin/env python3
"""A success headline must belong to the worker whose tail it came from (t-289).

`harvest_headline_marker` validates identity on its primary path, then had four
fallback scrapes that checked only the marker KIND. A COMPLETE naming a
different dispatch therefore became this worker's headline, and terminal mail
delivered another worker's result. A reviewer confirmed a marker the identity
predicate had already rejected was harvested anyway.

The rule these pin is the one the predicate documents and the sibling
`_recorded_terminal_success_marker` already applied: a SUCCESS marker must
carry this dispatch's id.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_watch as W  # noqa: E402


MINE = "codex-111-1700000000"
THEIRS = "codex-999-1700000000"


def _marker(dispatch_id: str, line: int = 12) -> dict:
    return {
        "kind": "COMPLETE",
        "line": line,
        "text": f"!COMPLETE: {dispatch_id} — did the thing",
        "dispatch_id": dispatch_id,
    }


@pytest.mark.parametrize("slot", ["terminal_marker", "last_marker"])
def test_a_foreign_marker_in_the_payload_is_not_our_headline(
    tmp_path: Path, slot: str
) -> None:
    tail = tmp_path / "t.tail"
    tail.write_text("worker said things\n", encoding="utf-8")
    payload = {slot: _marker(THEIRS)}
    got = W.harvest_headline_marker(payload, tail, expected_dispatch_id=MINE)
    assert got is None, f"harvested another dispatch's COMPLETE from {slot}: {got}"


def test_a_foreign_marker_in_the_markers_list_is_not_our_headline(tmp_path: Path) -> None:
    tail = tmp_path / "t.tail"
    tail.write_text("worker said things\n", encoding="utf-8")
    payload = {"markers": [_marker(THEIRS, line=3), _marker(THEIRS, line=9)]}
    got = W.harvest_headline_marker(payload, tail, expected_dispatch_id=MINE)
    assert got is None, f"harvested another dispatch's COMPLETE from markers: {got}"


@pytest.mark.parametrize("slot", ["terminal_marker", "last_marker"])
def test_our_own_marker_is_still_harvested(tmp_path: Path, slot: str) -> None:
    """The fix must not narrow the legitimate case it exists to protect."""
    tail = tmp_path / "t.tail"
    tail.write_text("worker said things\n", encoding="utf-8")
    payload = {slot: _marker(MINE)}
    got = W.harvest_headline_marker(payload, tail, expected_dispatch_id=MINE)
    assert got is not None and got.get("kind") == "COMPLETE", got


def test_without_an_expected_id_the_predicate_stays_permissive(tmp_path: Path) -> None:
    """Callers that cannot say which dispatch they are must not lose harvesting.

    The identity predicate is deliberately permissive when the caller has no
    expected id; tightening that would break every caller that does not track
    one, which is a different bug from the one being fixed.
    """
    tail = tmp_path / "t.tail"
    tail.write_text("worker said things\n", encoding="utf-8")
    payload = {"last_marker": _marker(THEIRS)}
    got = W.harvest_headline_marker(payload, tail, expected_dispatch_id=None)
    assert got is not None, "no expected id => nothing to bind against"
