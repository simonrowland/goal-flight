#!/usr/bin/env python3
"""Focused tests for project-state layout doctor and init scaffolding."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import contextlib
import io
import json
import re

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


PRE_V11_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>goal-flight</title>
</head>
<body>
<main>
<h1>goal-flight</h1>
<h2>To do</h2>
<ul>
  <li><a class="id" href="task-decomposition.md#t-014">t-014</a><span>dashboard generator</span></li>
</ul>
<h2>Done</h2>
<ul class="done-list">
  <li class="done"><a class="id" href="tasks-done.md#t-011">t-011</a><span>state-layout spec</span></li>
</ul>
<h2>Documents</h2>
<p class="docs">
  <a href="NORTH-STAR.md">north star</a>
  <a href="SRS.md">SRS</a>
  <a href="ARCHITECTURE.md">architecture</a>
</p>
</main>
<footer>generated 2026-06-20</footer>
</body>
</html>
"""


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
        assert_true("questions html scaffolded", (repo / "dashboard/questions-for-user.html").is_file())
        assert_true("dashboard data scaffolded", (repo / "dashboard/tasks-data.js").is_file())
        assert_true("resume notes scaffolded", (repo / "docs-private/RESUME-NOTES-2026-06-27.md").is_file())
        assert_true("second run creates no files", second["created_files"] == [])
        assert_true("second run creates no dirs", second["created_dirs"] == [])
        assert_true("second run no backup", second["backup"] is None)


def test_scaffold_project_state_dry_run_makes_no_changes() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-dry-run-") as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        result = goalflight_setup.scaffold_project_state(
            ROOT,
            repo,
            apply=False,
            today="2026-06-27",
        )

        assert_true("dry run reports files", "docs-private/NORTH-STAR.md" in result["would_create_files"])
        assert_true("dry run reports AGENTS", result["agents"]["action"] == "would_create")
        assert_true("dry run did not create docs-private", not repo.joinpath("docs-private").exists())
        assert_true("dry run did not create AGENTS", not repo.joinpath("AGENTS.md").exists())
        assert_true("dry run created no backup", result["backup"] is None)


def test_scaffold_project_state_creates_backup_before_apply() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-backup-") as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        repo.joinpath("AGENTS.md").write_text(
            "Living current state is `handoff.md`; read it before coding.\n",
            encoding="utf-8",
        )

        result = goalflight_setup.scaffold_project_state(
            ROOT,
            repo,
            apply=True,
            today="2026-06-27",
        )

        backup = result["backup"]
        assert_true("backup recorded", isinstance(backup, dict))
        manifest = repo / backup["manifest"]
        assert_true("backup manifest exists", manifest.is_file())
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert_true("backup manifest schema", payload["schema"] == "goalflight.project-state-backup.v1")
        assert_true("AGENTS backup exists", repo.joinpath(backup["path"], "AGENTS.md").is_file())
        assert_true("AGENTS rewritten to RESUME", "RESUME-NOTES-*.md" in repo.joinpath("AGENTS.md").read_text())
        assert_true(
            "AGENTS handoff removed",
            re.search(r"(?<!state-)handoff\.md", repo.joinpath("AGENTS.md").read_text()) is None,
        )


def test_scaffold_project_state_preserves_unmanaged_handoff_mentions() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-agents-prose-") as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        repo.joinpath("AGENTS.md").write_text(
            "Operator note: legacy docs mention handoff.md for historical context.\n",
            encoding="utf-8",
        )

        result = goalflight_setup.scaffold_project_state(
            ROOT,
            repo,
            apply=True,
            today="2026-06-27",
        )

        text = repo.joinpath("AGENTS.md").read_text(encoding="utf-8")
        assert_true("unmanaged handoff mention preserved", "handoff.md for historical context" in text)
        assert_true("resume pin added", "RESUME-NOTES-*.md" in text)
        assert_true("dry-run warning carried in message", "left unmanaged handoff.md mention untouched" in result["agents"]["message"])


def test_scaffold_project_state_preserves_generic_living_state_prose() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-agents-living-") as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        repo.joinpath("AGENTS.md").write_text(
            "## Living state\n"
            "\n"
            "Operator prose says handoff.md documents old incident practice.\n",
            encoding="utf-8",
        )

        result = goalflight_setup.scaffold_project_state(
            ROOT,
            repo,
            apply=True,
            today="2026-06-27",
        )

        text = repo.joinpath("AGENTS.md").read_text(encoding="utf-8")
        assert_true("generic Living state prose preserved", "handoff.md documents old incident practice" in text)
        assert_true("resume pin still added", "RESUME-NOTES-*.md" in text)
        assert_true("manual review message emitted", "left unmanaged handoff.md mention untouched" in result["agents"]["message"])


def test_scaffold_project_state_migrates_pre_v11_dashboard_index_to_root_dashboard() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-pre-v11-index-") as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        docs_private = repo / "docs-private"
        docs_private.mkdir()
        view = docs_private / "index.html"
        view.write_text(PRE_V11_INDEX_HTML, encoding="utf-8")

        status = goalflight_doctor.classify_managed_view_asset(
            ROOT / "templates/state-skeleton/index.html",
            view,
        )
        assert_true("pre-v1.1 dashboard classified legacy", status["status"] == "legacy")
        assert_true("pre-v1.1 dashboard needs refresh", status["needs_refresh"] is True)

        result = goalflight_setup.scaffold_project_state(
            ROOT,
            repo,
            apply=True,
            today="2026-06-27",
        )

        dashboard_view = repo / "dashboard/index.html"
        assert_true("pre-v1.1 dashboard regenerated at root dashboard", "dashboard/index.html" in result["created_files"])
        assert_true(
            "dashboard view equals template",
            dashboard_view.read_text(encoding="utf-8")
            == ROOT.joinpath("templates/state-skeleton/index.html").read_text(encoding="utf-8"),
        )
        assert_true("legacy docs-private dashboard left untouched", view.read_text(encoding="utf-8") == PRE_V11_INDEX_HTML)
        assert_true(
            "dashboard migration message emitted",
            any(
                "dashboard/ is not gitignored" in message
                for message in result["messages"]
            ),
        )


def test_scaffold_project_state_refreshes_known_legacy_hash_with_backup() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-known-hash-") as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        view = repo / "dashboard/gf.js"
        legacy = "legacy hash fixture\n"
        legacy_hash = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
        original_hashes = goalflight_doctor.MANAGED_VIEW_LEGACY_SHA256["gf.js"]
        goalflight_doctor.MANAGED_VIEW_LEGACY_SHA256["gf.js"] = original_hashes + (legacy_hash,)
        try:
            view.write_text(legacy, encoding="utf-8")
            status = goalflight_doctor.classify_managed_view_asset(
                ROOT / "templates/state-skeleton/gf.js",
                view,
            )
            assert_true("known hash classified legacy", status["status"] == "legacy")

            result = goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")

            assert_true("known hash refreshed", "dashboard/gf.js" in result["refreshed_managed_views"])
            assert_true(
                "known hash preimage backed up",
                repo.joinpath(result["backup"]["path"], "dashboard/gf.js").read_text(encoding="utf-8")
                == legacy,
            )
        finally:
            goalflight_doctor.MANAGED_VIEW_LEGACY_SHA256["gf.js"] = original_hashes


def test_scaffold_project_state_preserves_customized_current_managed_view() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-view-custom-") as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        view = repo / "dashboard/index.html"
        customized = ROOT.joinpath("templates/state-skeleton/index.html").read_text(encoding="utf-8")
        customized = customized.replace(
            "</style>",
            ".operator-note{border:1px solid var(--accent)}\n</style>",
        )
        view.write_text(customized, encoding="utf-8")

        result = goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")

        assert_true("customized managed view preserved", view.read_text(encoding="utf-8") == customized)
        assert_true("customized managed view not refreshed", result["refreshed_managed_views"] == [])
        assert_true("customized managed view no backup", result["backup"] is None)
        assert_true(
            "customized managed view manual review message",
            any("preserve customized current managed view asset" in message for message in result["messages"]),
        )


def test_scaffold_project_state_preserves_foreign_managed_path_file() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-view-foreign-") as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        view = repo / "dashboard/index.html"
        foreign = "<!doctype html><title>operator portal</title><h1>operator portal</h1>\n"
        view.write_text(foreign, encoding="utf-8")

        status = goalflight_doctor.classify_managed_view_asset(
            ROOT / "templates/state-skeleton/index.html",
            view,
        )
        assert_true("foreign managed path classified foreign", status["status"] == "foreign")
        assert_true("foreign managed path does not refresh", status["needs_refresh"] is False)

        result = goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")

        assert_true("foreign managed path preserved", view.read_text(encoding="utf-8") == foreign)
        assert_true("foreign managed path not refreshed", result["refreshed_managed_views"] == [])
        assert_true("foreign managed path no backup", result["backup"] is None)
        assert_true(
            "foreign managed path manual review message",
            any(
                "unrecognized file at managed view path; left for manual review" in message
                for message in result["messages"]
            ),
        )


def test_scaffold_project_state_respects_gitignore_branches() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-ignore-") as td:
        ignored = Path(td) / "ignored"
        tracked = Path(td) / "tracked"
        ignored.mkdir()
        tracked.mkdir()
        for repo in (ignored, tracked):
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ignored.joinpath(".gitignore").write_text("docs-private/\n", encoding="utf-8")

        ignored_result = goalflight_setup.scaffold_project_state(ROOT, ignored, apply=True, today="2026-06-27")
        tracked_result = goalflight_setup.scaffold_project_state(ROOT, tracked, apply=True, today="2026-06-27")

        assert_true("ignored branch detected", ignored_result["docs_private_gitignored"] is True)
        assert_true("tracked branch allowed", tracked_result["docs_private_gitignored"] is False)
        assert_true(
            "tracked branch teaching message",
            any("not gitignored" in message for message in tracked_result["messages"]),
        )


def test_scaffold_project_state_backfills_derivable_inflight_ledger_task_ids() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-ledger-") as td:
        repo = Path(td) / "repo"
        state = Path(td) / "state"
        repo.mkdir()
        state.mkdir()
        prompt = repo / "docs-private/prompts/t-031.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_text("Implement t-031 migration hardening.\n", encoding="utf-8")
        old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
        os.environ["GOALFLIGHT_STATE_DIR"] = str(state)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_setup.goalflight_ledger.cmd_record(
                    type(
                        "Args",
                        (),
                        {
                            "dispatch_id": "dispatch-t-031",
                            "prompt_id": None,
                            "prompt_path": str(prompt),
                            "task_id": None,
                            "task_ids": None,
                            "agent": "codex",
                            "engine": "codex",
                            "shape": "bash-tail",
                            "account": "default",
                            "transport": "dispatch",
                            "project_root": str(repo.resolve()),
                            "controller_pid": os.getpid(),
                            "worker_pid": None,
                            "acp_session_id": None,
                            "logical_session_id": "dispatch-t-031",
                            "lease_id": None,
                            "remote_lease_id": None,
                            "stdout_path": None,
                            "stderr_path": None,
                            "status_path": None,
                            "os_sandbox_json": None,
                            "queue_launch_token": None,
                            "state": "running",
                            "json": True,
                        },
                    )()
                )
            result = goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
            updated = result["ledger_task_id_backfill"]["updated"]
            record = json.loads((state / "runs.d/dispatch-t-031.json").read_text(encoding="utf-8"))
            manifest = json.loads((repo / result["backup"]["manifest"]).read_text(encoding="utf-8"))
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_setup._restore_from_manifest(repo / result["backup"]["manifest"])
            restored_record = json.loads((state / "runs.d/dispatch-t-031.json").read_text(encoding="utf-8"))
        finally:
            if old_state is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state

        assert_true("ledger backfill reported", updated and updated[0]["task_ids"] == ["t-031"])
        assert_true("ledger task_ids written", record["task_ids"] == ["t-031"])
        assert_true("ledger task_id compatibility written", record["task_id"] == "t-031")
        preimages = manifest["ledger_task_id_backfill_preimages"]
        assert_true("ledger preimage recorded", preimages and preimages[0]["dispatch_id"] == "dispatch-t-031")
        assert_true("ledger preimage lacks task_ids", "task_ids" not in preimages[0]["record"])
        assert_true("ledger restore removed task_ids", "task_ids" not in restored_record)
        assert_true("ledger restore removed task_id", "task_id" not in restored_record)


def test_state_layout_references_newest_resume_notes_not_handoff_file() -> None:
    files = [
        ROOT / "templates/goalflight-loop-prompt.md",
        ROOT / "templates/project-agents.md",
        ROOT / "templates/state-skeleton/history.md",
        ROOT / "commands/init.md",
        ROOT / "protocols/project-state-layout.md",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert_true(f"{path.name} references RESUME-NOTES", "RESUME-NOTES" in text)
        assert_true(f"{path.name} drops handoff.md", re.search(r"(?<!state-)handoff\.md", text) is None)


def test_doctor_state_layout_reports_exact_missing_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-missing-") as td:
        repo = Path(td)
        payload = goalflight_doctor.check_project_state_layout(repo, ROOT)
        messages = [item["message"] for item in payload["advisories"]]
        assert_true("state layout advisory", payload["ok"] is None)
        assert_true("state layout warnings empty", payload["warnings"] == [])
        assert_true("missing docs-private directory exact", "docs-private/" in payload["missing_dirs"])
        assert_true("missing north star exact", "docs-private/NORTH-STAR.md" in payload["missing_files"])
        assert_true(
            "teaching message names source template",
            any("templates/state-skeleton/NORTH-STAR.md" in msg for msg in messages),
        )


def test_doctor_state_layout_ignores_static_html_mtime() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-stale-") as td:
        repo = Path(td)
        goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        html = repo / "dashboard/questions-for-user.html"
        source = repo / "docs-private/questions-for-user.md"
        os.utime(html, (100, 100))
        os.utime(source, (200, 200))

        payload = goalflight_doctor.check_project_state_layout(repo, ROOT)
        assert_true("stale html not reported", payload["stale_html"] == [])
        assert_true("static view remains healthy", payload["ok"] is True)


def test_doctor_state_layout_reports_managed_view_schema_skew() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-view-skew-") as td:
        repo = Path(td)
        goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        repo.joinpath("dashboard/gf.js").write_text("legacy worker-finished done vocabulary\n", encoding="utf-8")

        payload = goalflight_doctor.check_project_state_layout(repo, ROOT)
        skew = [
            item for item in payload["view_schema_skew"]
            if item["asset"] == "dashboard/gf.js"
        ]
        assert_true("managed view schema skew reported", skew)
        assert_true("managed view skew makes layout warning", payload["ok"] is False)
        assert_true("refresh path named", "run `/goal-flight init`" in skew[0]["message"])


def test_doctor_state_layout_reports_customized_view_as_advisory() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-view-custom-doctor-") as td:
        repo = Path(td)
        goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        view = repo / "dashboard/gf.js"
        view.write_text(
            ROOT.joinpath("templates/state-skeleton/gf.js").read_text(encoding="utf-8")
            + "\n/* operator customization */\n",
            encoding="utf-8",
        )

        payload = goalflight_doctor.check_project_state_layout(repo, ROOT)
        assert_true("customized view not schema skew", payload["view_schema_skew"] == [])
        assert_true("customized view advisory reported", payload["view_customizations"])
        assert_true("customized view advisory state", payload["ok"] is None)


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


def test_project_readiness_treats_initialized_state_layout_absence_as_warning() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-no-backlog-") as td:
        repo = Path(td)
        docs_private = repo / "docs-private"
        docs_private.mkdir()
        docs_private.joinpath("env-caveats.md").write_text("ok\n", encoding="utf-8")
        repo.joinpath("SKILL.md").write_text("# Project\n", encoding="utf-8")
        _write_ready_agents(repo)

        payload = goalflight_doctor.check_project_goalflight_readiness(repo)
        assert_true("initialized project readiness warns", payload["ok"] is False)
        assert_true("state layout absence warning", payload["state_layout"]["ok"] is False)
        assert_true("state layout warnings present", payload["state_layout"]["warnings"])
        assert_true(
            "missing skeleton is readiness warning",
            any("missing canonical docs-private file" in warning for warning in payload["warnings"]),
        )


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
        test_scaffold_project_state_dry_run_makes_no_changes,
        test_scaffold_project_state_creates_backup_before_apply,
        test_scaffold_project_state_preserves_unmanaged_handoff_mentions,
        test_scaffold_project_state_preserves_generic_living_state_prose,
        test_scaffold_project_state_migrates_pre_v11_dashboard_index_to_root_dashboard,
        test_scaffold_project_state_refreshes_known_legacy_hash_with_backup,
        test_scaffold_project_state_preserves_customized_current_managed_view,
        test_scaffold_project_state_preserves_foreign_managed_path_file,
        test_scaffold_project_state_respects_gitignore_branches,
        test_scaffold_project_state_backfills_derivable_inflight_ledger_task_ids,
        test_state_layout_references_newest_resume_notes_not_handoff_file,
        test_doctor_state_layout_reports_exact_missing_paths,
        test_doctor_state_layout_ignores_static_html_mtime,
        test_doctor_state_layout_reports_managed_view_schema_skew,
        test_doctor_state_layout_reports_customized_view_as_advisory,
        test_project_readiness_requires_agents_resume_pin,
        test_project_readiness_treats_initialized_state_layout_absence_as_warning,
        test_state_protocols_are_discoverable_from_index_and_commands,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
