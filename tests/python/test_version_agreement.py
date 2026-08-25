"""Every in-tree version declaration must agree with the VERSION file.

SKILL.md sat at 1.4.1 through the 1.4.x and 1.5.0 releases. Nothing was broken
by it and nothing complained: the release steps touch VERSION, CHANGELOG.md and
both plugin.json files, so the one declaration nobody edits simply stopped
tracking. Version skew fails silently by nature -- the stale number is still a
valid version string, and the only reader who notices is a human wondering why
the skill reports an old release.

There is no release script to fix, so a checklist line would be the third place
the rule lives and the third place it can be missed. This test makes the gate
the enforcement point instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PLUGIN_MANIFESTS = (
    ".claude-plugin/plugin.json",
    "plugins/goal-flight/.codex-plugin/plugin.json",
)


def _expected() -> str:
    return (ROOT / "VERSION").read_text().strip()


def test_version_file_is_a_release_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", _expected()), (
        f"VERSION must be a bare semver triple, got {_expected()!r}"
    )


def test_skill_md_declares_the_release_version() -> None:
    """The declaration that actually drifted."""
    text = (ROOT / "SKILL.md").read_text()
    match = re.search(r"^version:\s*(\S+)\s*$", text, re.M)
    assert match is not None, "SKILL.md has no 'version:' line in its front matter"
    assert match.group(1) == _expected(), (
        f"SKILL.md says {match.group(1)}, VERSION says {_expected()}. "
        "Bump SKILL.md too -- it is the declaration the release steps omit."
    )


def test_plugin_manifests_declare_the_release_version() -> None:
    expected = _expected()
    skewed = {}
    for rel in PLUGIN_MANIFESTS:
        path = ROOT / rel
        assert path.exists(), f"missing plugin manifest: {rel}"
        found = json.loads(path.read_text()).get("version")
        if found != expected:
            skewed[rel] = found
    assert not skewed, f"version skew against VERSION={expected}: {skewed}"


def test_every_tracked_manifest_is_covered() -> None:
    """Guard the guard: a new tracked manifest must join PLUGIN_MANIFESTS.

    Without this, adding a third plugin.json would leave it unchecked and the
    suite would still pass -- the same shape as the bug being fixed, where the
    rule was enforced everywhere except the one place it was not.

    docs-private/ is gitignored (vendored copies of shipped packages), so it is
    excluded deliberately rather than by oversight.
    """
    # Both exclusions are gitignored trees that rglob cannot see are excluded:
    # docs-private/ holds vendored copies of shipped packages, and worktrees/
    # holds per-dispatch worktrees, each a full checkout carrying its own
    # manifests. This test asserts something about TRACKED files, so anything
    # git ignores must not reach it.
    ignored_prefixes = ("docs-private/", "worktrees/")
    tracked = {
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("plugin.json")
        if ".git/" not in p.as_posix()
        and not p.relative_to(ROOT).as_posix().startswith(ignored_prefixes)
    }
    assert tracked == set(PLUGIN_MANIFESTS), (
        f"tracked plugin.json set changed: {sorted(tracked)}. "
        f"Update PLUGIN_MANIFESTS in {Path(__file__).name} so the new manifest "
        "is version-checked too."
    )
