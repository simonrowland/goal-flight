"""Host wrappers resolve the installed Goal Flight skill root.

Every non-native host wrapper (cursor, grok, grok-bot, opencode, and the codex
plugin) must tell a controller where ``<skill-root>`` lives, because in a
downstream project there is no repository root ``SKILL.md`` to fall back on.
The invariant is a dedicated section, not a passing mention: a wrapper that
only said "do NOT use ~/.goal-flight/skill" would still contain both substrings.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WRAPPERS = sorted(
    path.relative_to(ROOT)
    for path in (ROOT / "configs").glob("*/skills/goal-flight/SKILL.md")
) + [Path("plugins/goal-flight/skills/goal-flight/SKILL.md")]

# grok-bot landed first with "## Skill pin"; the others use "## Skill root".
# Either heading is the contract; the section body must name both resolutions.
SECTION_HEADING = re.compile(r"^## Skill (?:root|pin)\s*$", re.M)


def _wrapper_id(path: Path) -> str:
    if path.parts[0] == "configs":
        return path.parts[1]
    return "codex-plugin"


def _section_body(text: str) -> str:
    match = SECTION_HEADING.search(text)
    assert match, "no '## Skill root' / '## Skill pin' section"
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=_wrapper_id)
def test_host_wrapper_defines_skill_root(wrapper: Path) -> None:
    text = (ROOT / wrapper).read_text()
    body = _section_body(text)

    assert "GOALFLIGHT_ROOT" in body, f"{wrapper}: section does not name $GOALFLIGHT_ROOT"
    assert ".goal-flight/skill" in body, f"{wrapper}: section does not name the installed pin"
    assert "--project-root" in body, f"{wrapper}: section does not say when to pass --project-root"
