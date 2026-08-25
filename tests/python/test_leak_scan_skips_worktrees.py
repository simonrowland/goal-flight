#!/usr/bin/env python3
"""The host-tool leak scan must not read gitignored per-dispatch worktrees.

`.gitignore` declares `worktrees/` to be goal-flight's own per-dispatch
worktrees, auto-managed by `/goal-flight execute --parallel`. Each one is a full
checkout of this repo, so it carries its own CHANGELOG.md and SKILL.md.

`Path.rglob` does not honour gitignore. Observed 2026-08-25: with four worktrees
present, the validator reported

    worktrees/t319-r4/CHANGELOG.md: raw host tool leak 'TodoWrite'
    worktrees/b212-supersede/SKILL.md: raw host tool leak 'AskUserQuestion'

for files that ARE allowlisted at the repo root -- the allowlist is keyed on the
exact relative path, so `worktrees/<id>/CHANGELOG.md` never matches it. Using
the repo's own documented worktree convention therefore broke its own suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_validate_adapters as V  # noqa: E402

# A real signature the scanner looks for, taken from the observed failure.
LEAKY = "This mentions TodoWrite directly.\n"


def test_a_worktree_copy_is_not_scanned(tmp_path: Path) -> None:
    wt = tmp_path / "worktrees" / "some-dispatch"
    wt.mkdir(parents=True)
    (wt / "CHANGELOG.md").write_text(LEAKY)
    (wt / "SKILL.md").write_text(LEAKY)

    assert V.validate_no_host_tool_leaks(tmp_path) == []


def test_the_same_content_outside_worktrees_is_still_caught(tmp_path: Path) -> None:
    """Control: the skip must be narrow, not a way to disable the scan.

    Without this, deleting the whole scan would also make the test above pass.
    """
    doc = tmp_path / "docs" / "guide.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(LEAKY)

    errors = V.validate_no_host_tool_leaks(tmp_path)
    assert errors, "a leak outside worktrees/ must still be reported"
    assert "docs/guide.md" in errors[0], errors
