#!/usr/bin/env python3
"""A mail backlog must not consume every doorbell slot.

Arming against a backlog used to ring once PER ITEM through the ordinary
one-line path, so N doorbells armed against a backlog of N or more fired
instantly on old mail and left ZERO coverage. Observed repeatedly across
projects on this box, including a 13-message backlog firing all four of a
controller's tracked doorbells at once.

That is a bug, not a mode. Controllers were inventing a "drain first, confirm
none pending, THEN arm" ceremony purely to avoid it -- and a rule people must
invent to work around a default is a bug in the default.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MESSAGES = ROOT / "scripts" / "goalflight_messages.py"


def _help() -> str:
    return subprocess.run(
        [sys.executable, str(MESSAGES), "listen", "--help"],
        capture_output=True, text=True, timeout=120,
    ).stdout


def test_report_pending_is_on_by_default() -> None:
    """The safe behaviour is the one you get without asking for it.

    Checks argparse's GENERATED usage line, not the help prose. An earlier
    version of this test matched the string "--no-report-pending" anywhere in
    the help -- which the prose itself contains -- so reverting the action to
    store_true left it green. The usage alternation is emitted only by
    BooleanOptionalAction, so it reflects behaviour rather than wording.
    """
    help_text = _help()
    usage = help_text.split("options:")[0]
    assert "--report-pending | --no-report-pending" in usage, (
        "expected argparse to emit the BooleanOptionalAction alternation in "
        "usage, meaning the flag defaults on; store_true emits only "
        f"[--report-pending] and the backlog-eats-your-slots bug returns. "
        f"usage was:\n{usage}"
    )


def test_opt_out_still_exists_and_is_described_by_its_consequence() -> None:
    """Someone choosing the legacy path must be told what it costs them."""
    help_text = _help()
    assert "consume every listener slot" in help_text, (
        "the opt-out must state its consequence; describing it neutrally is how "
        "the original flag went unused for so long"
    )


def test_watch_follow_does_not_warn_about_an_untyped_default() -> None:
    """A warning about a choice nobody made is noise on every watchdog arm.

    `--watch-follow` is watchdog-only and ignores --report-pending. Testing the
    VALUE rather than whether it was typed would print "ignoring
    --report-pending" on every single watchdog arm once the default flipped.
    """
    import ast

    src = MESSAGES.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Find the guard that appends "--report-pending" to the ignored list.
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body_src = ast.get_source_segment(src, node) or ""
        if '"--report-pending"' in body_src and "ignored.append" in body_src:
            hits.append(ast.get_source_segment(src, node.test) or "")
    assert hits, "could not locate the watch-follow ignore guard; re-derive this pin"
    for test_src in hits:
        assert "argv" in test_src, (
            "the guard must key on whether the flag was TYPED (sys.argv), not on "
            f"its value, or every watchdog arm warns; got: {test_src!r}"
        )


def main() -> None:
    test_report_pending_is_on_by_default()
    test_opt_out_still_exists_and_is_described_by_its_consequence()
    test_watch_follow_does_not_warn_about_an_untyped_default()
    print("OK: report-pending default tests pass")


if __name__ == "__main__":
    main()
