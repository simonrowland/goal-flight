"""Codex installer surfaces must point at the end-user install, not dev checkouts."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

CODEX_INSTALL_SURFACES = [
    "AGENTS.md",
    "commands/register-codex.md",
    "config/commands.env",
    "configs/codex/config.toml",
    "docs/install/context-discipline-hook.md",
    "docs/install/session-start-watchdog-hook.md",
    "plugins/goal-flight/.codex-plugin/plugin.json",
    "prompts/gstack-codex-challenge.md",
    "scripts/goalflight_actions.py",
    "scripts/goalflight_setup.py",
    "scripts/install-codex-overrides.sh",
    "scripts/register-context-mode-codex.py",
    "templates/codex-goal-prompt.md.tpl",
    "templates/project-agents.md",
]

FORBIDDEN_DEV_PATHS = [
    "/Repos/" + "goal-flight",
    "goal-flight" + "-phase0",
]


def test_codex_install_surfaces_do_not_reference_dev_checkouts():
    hits: list[str] = []
    for relpath in CODEX_INSTALL_SURFACES:
        path = ROOT / relpath
        content = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(content.splitlines(), start=1):
            for needle in FORBIDDEN_DEV_PATHS:
                if needle in line:
                    hits.append(f"{relpath}:{line_no}: contains {needle}")
    assert not hits, "\n".join(hits)


def _run_tests():
    failed = []
    passed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            passed += 1
        except Exception as exc:
            failed.append((name, str(exc)))
    return passed, failed


if __name__ == "__main__":
    passed, failed = _run_tests()
    if failed:
        print(f"FAIL tests/python/test_codex_install_path_hygiene.py ({len(failed)} failed)")
        for name, err in failed:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print(f"PASS tests/python/test_codex_install_path_hygiene.py ({passed} tests)")
