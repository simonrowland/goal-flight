#!/usr/bin/env python3
"""Focused tests for project-state layout doctor and init scaffolding."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_doctor  # noqa: E402
import goalflight_setup  # noqa: E402


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _write_ready_agents(repo: Path) -> None:
    repo.joinpath("AGENTS.md").write_text(
        "## Living state -- read the newest docs-private/RESUME-NOTES-*.md first\n"
        "## Goal Flight Routing\n"
        f"- skill-root: ${{GOALFLIGHT_ROOT:-{ROOT}}}\n"
        "- load order: AGENTS.md -> SKILL.md -> commands/*.md\n"
        "## Commands\n"
        "- test: `pytest`\n",
        encoding="utf-8",
    )


def test_scaffold_project_state_is_idempotent_and_respects_existing_files() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-") as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        repo.joinpath(".gitignore").write_text("docs-private/\n", encoding="utf-8")
        north_star = repo / "docs-private/NORTH-STAR.md"
        north_star.parent.mkdir()
        north_star.write_text("operator-owned\n", encoding="utf-8")

        first = goalflight_setup.scaffold_project_state(
            ROOT,
            repo,
            apply=True,
            today="2026-06-27",
        )
        second = goalflight_setup.scaffold_project_state(
            ROOT,
            repo,
            apply=True,
            today="2026-06-27",
        )

        assert_true("docs-private gitignore detected", first["docs_private_gitignored"] is True)
        assert_true("operator file preserved", north_star.read_text(encoding="utf-8") == "operator-owned\n")
        assert_true("existing file recorded as skipped", "docs-private/NORTH-STAR.md" in first["skipped_existing_files"])
        assert_true("reviews directory created", (repo / "docs-private/reviews").is_dir())
        assert_true("research directory created", (repo / "docs-private/research").is_dir())
        assert_true("questions html scaffolded", (repo / "docs-private/questions-for-user.html").is_file())
        assert_true("resume notes scaffolded", (repo / "docs-private/RESUME-NOTES-2026-06-27.md").is_file())
        assert_true("second run creates no files", second["created_files"] == [])
        assert_true("second run creates no dirs", second["created_dirs"] == [])


def test_doctor_state_layout_reports_exact_missing_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-missing-") as td:
        repo = Path(td)
        payload = goalflight_doctor.check_project_state_layout(repo, ROOT)
        messages = [item["message"] for item in payload["warnings"]]
        assert_true("state layout not ok", payload["ok"] is False)
        assert_true("missing docs-private directory exact", "docs-private/" in payload["missing_dirs"])
        assert_true("missing north star exact", "docs-private/NORTH-STAR.md" in payload["missing_files"])
        assert_true(
            "teaching message names source template",
            any("templates/state-skeleton/NORTH-STAR.md" in msg for msg in messages),
        )


def test_doctor_state_layout_reports_stale_html_sources() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-stale-") as td:
        repo = Path(td)
        goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        html = repo / "docs-private/questions-for-user.html"
        source = repo / "docs-private/questions-for-user.md"
        os.utime(html, (100, 100))
        os.utime(source, (200, 200))

        payload = goalflight_doctor.check_project_state_layout(repo, ROOT)
        stale = [
            item for item in payload["stale_html"]
            if item["html"] == "docs-private/questions-for-user.html"
        ]
        assert_true("stale html reported", stale)
        assert_true(
            "stale source exact",
            "docs-private/questions-for-user.md" in stale[0]["older_than"],
        )


def test_project_readiness_requires_agents_resume_pin() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-ready-") as td:
        repo = Path(td)
        goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        repo.joinpath("docs-private/env-caveats.md").write_text("ok\n", encoding="utf-8")
        repo.joinpath("SKILL.md").write_text("# Project\n", encoding="utf-8")
        repo.joinpath("AGENTS.md").write_text(
            "## Goal Flight Routing\n"
            f"- skill-root: ${{GOALFLIGHT_ROOT:-{ROOT}}}\n"
            "- load order: AGENTS.md -> SKILL.md -> commands/*.md\n"
            "## Commands\n"
            "- test: `pytest`\n",
            encoding="utf-8",
        )
        missing_pin = goalflight_doctor.check_project_goalflight_readiness(repo)
        assert_true(
            "missing newest pin warned",
            "AGENTS.md does not pin newest docs-private/RESUME-NOTES-*.md" in missing_pin["warnings"],
        )

        _write_ready_agents(repo)
        ready = goalflight_doctor.check_project_goalflight_readiness(repo)
        assert_true("ready project ok", ready["ok"] is True)
        assert_true("ready project state layout ok", ready["state_layout"]["ok"] is True)


def test_state_protocols_are_discoverable_from_index_and_commands() -> None:
    protocol_readme = ROOT.joinpath("protocols/README.md").read_text(encoding="utf-8")
    init_doc = ROOT.joinpath("commands/init.md").read_text(encoding="utf-8")
    doctor_doc = ROOT.joinpath("commands/doctor.md").read_text(encoding="utf-8")
    for protocol in (
        "project-state-layout.md",
        "task-lifecycle.md",
        "progress-dashboard.md",
    ):
        assert_true(f"protocol index names {protocol}", protocol in protocol_readme)
        assert_true(f"init command names {protocol}", protocol in init_doc)
        assert_true(f"doctor command names {protocol}", protocol in doctor_doc)


def main() -> None:
    tests = [
        test_scaffold_project_state_is_idempotent_and_respects_existing_files,
        test_doctor_state_layout_reports_exact_missing_paths,
        test_doctor_state_layout_reports_stale_html_sources,
        test_project_readiness_requires_agents_resume_pin,
        test_state_protocols_are_discoverable_from_index_and_commands,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
