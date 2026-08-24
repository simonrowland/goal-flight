"""Cursor runs over bash-tail, and acp is refused rather than silently chosen.

Cursor's auto-resolved shape was acp. bash-tail is measured good for it: five of
five trials correct by parsed value, exit 0, 49-50 seconds each, both artifacts
written and a clean terminal marker every run. acp is the untested-and-suspect
path, so it is now blocked rather than merely deprioritised.

Blocking beats defaulting away. A shape that stays reachable by an explicit flag
will be reached — usually by someone who did not know it was the bad one — and a
refusal that names the alternative costs one line and saves the discovery.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch as gd  # noqa: E402


def test_cursor_acp_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("GOALFLIGHT_CURSOR_ACP", raising=False)
    assert gd._cursor_acp_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "enabled", "on", "ON", " Yes "])
def test_cursor_acp_opt_in_is_honoured(monkeypatch, value) -> None:
    """The escape hatch must work, or the block becomes unfixable-in-place."""
    monkeypatch.setenv("GOALFLIGHT_CURSOR_ACP", value)
    assert gd._cursor_acp_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
def test_only_affirmative_values_enable_acp(monkeypatch, value) -> None:
    monkeypatch.setenv("GOALFLIGHT_CURSOR_ACP", value)
    assert gd._cursor_acp_enabled() is False


def test_cursor_is_not_in_the_acp_auto_list() -> None:
    """Pin the resolution rule at its source.

    Asserting on the source keeps this honest: the rule is a one-line tuple in
    two places, and a change to either silently alters which transport every
    cursor dispatch takes.
    """
    import inspect
    src = inspect.getsource(gd.main)
    marker = 'shape = "acp" if args.agent in ('
    assert marker in src, "shape auto-resolution moved; update this test"
    line = next(l for l in src.splitlines() if marker in l)
    assert "cursor" not in line, "cursor must not auto-resolve to acp"
    assert "claude" in line, "claude should still auto-resolve to acp"


def test_the_early_probe_agrees_with_main() -> None:
    """Two sites resolve shape; they must not disagree.

    If the pre-import probe still sends cursor down the acp path it will import
    the acp runtime for a dispatch that main() then refuses — wasted work and a
    confusing error ordering.
    """
    import inspect
    src = inspect.getsource(gd)
    probe = [l for l in src.splitlines()
             if 'if agent in (' in l and "claude" in l]
    assert probe, "early shape probe not found; update this test"
    for line in probe:
        assert "cursor" not in line, "early probe still routes cursor to acp"
