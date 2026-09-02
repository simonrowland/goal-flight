"""Host wrappers resolve the installed Goal Flight skill root."""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WRAPPERS = sorted(
    path.relative_to(ROOT)
    for path in (ROOT / "configs").glob("*/skills/goal-flight/SKILL.md")
) + [Path("plugins/goal-flight/skills/goal-flight/SKILL.md")]


def _wrapper_id(path: Path) -> str:
    if path.parts[0] == "configs":
        return path.parts[1]
    return "codex-plugin"


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=_wrapper_id)
def test_host_wrapper_defines_skill_root(wrapper: Path) -> None:
    text = (ROOT / wrapper).read_text()

    assert "GOALFLIGHT_ROOT" in text, f"{wrapper}: missing GOALFLIGHT_ROOT"
    assert ".goal-flight/skill" in text, f"{wrapper}: missing installed skill pin"
