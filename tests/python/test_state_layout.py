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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_task  # noqa: E402
import goalflight_doctor  # noqa: E402
import goalflight_setup  # noqa: E402

os.environ.setdefault(
    "GOALFLIGHT_TASK_STORE_DIR",
    tempfile.mkdtemp(prefix="goalflight-state-layout-store-"),
)


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _dashboard_items(repo: Path) -> list[dict]:
    text = repo.joinpath("dashboard/tasks-data.js").read_text(encoding="utf-8")
    prefix = "window.GF_ITEMS = "
    start = text.index(prefix) + len(prefix)
    end = text.index(";\nif (typeof module", start)
    return json.loads(text[start:end])


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


@contextlib.contextmanager
def _task_store_base(path: Path):
    old = os.environ.get("GOALFLIGHT_TASK_STORE_DIR")
    os.environ["GOALFLIGHT_TASK_STORE_DIR"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("GOALFLIGHT_TASK_STORE_DIR", None)
        else:
            os.environ["GOALFLIGHT_TASK_STORE_DIR"] = old


def _read_view_manifest(repo: Path) -> dict:
    return json.loads(goalflight_task.view_manifest_path(repo).read_text(encoding="utf-8"))


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


def test_scaffold_project_state_empty_store_has_no_ready_frontier() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-empty-store-") as td:
        repo = Path(td)
        result = goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")

        assert_true("tasks store scaffolded empty", repo.joinpath("docs-private/tasks.jsonl").read_text(encoding="utf-8") == "")
        assert_true("dashboard mirror scaffolded empty", _dashboard_items(repo) == [])
        for rel in ("docs-private/task-decomposition.md", "docs-private/tasks-done.md", "docs-private/bug-patterns.md", "dashboard/gf.js"):
            text = repo.joinpath(rel).read_text(encoding="utf-8")
            for stale_id in ("t-001", "t-002", "bp-001"):
                assert_true(f"{rel} has no scaffold stub {stale_id}", stale_id not in text)
        assert_true("empty store created", "docs-private/tasks.jsonl" in result["created_files"])
        proc = subprocess.run(
            [sys.executable, str(ROOT / "goalflight_task.py"), "--project-root", str(repo), "next", "--json"],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        assert_true(f"next exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("fresh scaffold has no ready frontier", json.loads(proc.stdout) == [])


def test_scaffold_project_state_corrupt_legacy_store_fails_clean() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-corrupt-store-") as td:
        repo = Path(td)
        docs_private = repo / "docs-private"
        docs_private.mkdir()
        docs_private.joinpath("tasks.jsonl").write_text("{not-json}\n", encoding="utf-8")

        try:
            goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        except Exception as exc:
            assert_true("corrupt store reported", "invalid JSON" in str(exc))
        else:
            raise AssertionError("corrupt legacy store should fail scaffold")

        assert_true("no dashboard dir after preflight failure", not repo.joinpath("dashboard").exists())
        assert_true("no AGENTS after preflight failure", not repo.joinpath("AGENTS.md").exists())
        assert_true("no tasks-data partial after failure", not repo.joinpath("dashboard/tasks-data.js").exists())


def test_scaffold_project_state_generates_dashboard_mirror_from_legacy_store() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-legacy-store-") as td:
        repo = Path(td)
        docs_private = repo / "docs-private"
        docs_private.mkdir()
        docs_private.joinpath("tasks.jsonl").write_text(
            json.dumps({
                "schema_version": 1,
                "id": "t-009",
                "kind": "task",
                "title": "Legacy imported task",
                "blocked_by": [],
                "links": [],
                "done": False,
            }, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )

        goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        items = _dashboard_items(repo)
        assert_true("legacy mirror has one item", len(items) == 1)
        assert_true("legacy mirror keeps id", items[0]["id"] == "t-009")
        assert_true("legacy mirror derives status", items[0]["derived_status"] == "pending")
        assert_true("legacy mirror not skeleton stub", "t-001" not in repo.joinpath("dashboard/tasks-data.js").read_text(encoding="utf-8"))


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
        original = "Operator note: legacy docs mention handoff.md for historical context.\n"
        repo.joinpath("AGENTS.md").write_text(original, encoding="utf-8")

        result = goalflight_setup.scaffold_project_state(
            ROOT,
            repo,
            apply=True,
            today="2026-06-27",
        )

        text = repo.joinpath("AGENTS.md").read_text(encoding="utf-8")
        assert_true("custom AGENTS unchanged", text == original)
        assert_true("custom AGENTS not rewritten", result["agents"]["action"] == "skip")
        assert_true("guidance emitted", "custom AGENTS.md left unchanged" in result["agents"]["message"])
        assert_true("manual review warning carried in message", "unmanaged handoff.md mention requires manual review" in result["agents"]["message"])


def test_scaffold_project_state_preserves_generic_living_state_prose() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-agents-living-") as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        original = (
            "## Living state\n"
            "\n"
            "Operator prose says handoff.md documents old incident practice.\n"
        )
        repo.joinpath("AGENTS.md").write_text(original, encoding="utf-8")

        result = goalflight_setup.scaffold_project_state(
            ROOT,
            repo,
            apply=True,
            today="2026-06-27",
        )

        text = repo.joinpath("AGENTS.md").read_text(encoding="utf-8")
        assert_true("generic Living state prose unchanged", text == original)
        assert_true("custom living prose not rewritten", result["agents"]["action"] == "skip")
        assert_true("guidance emitted", "custom AGENTS.md left unchanged" in result["agents"]["message"])


def test_scaffold_project_state_rejects_symlinked_state_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-symlink-") as td:
        base = Path(td)
        repo = base / "repo"
        repo.mkdir()
        outside = base / "outside-state"
        outside.mkdir()
        repo.joinpath("docs-private").symlink_to(outside, target_is_directory=True)

        try:
            goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        except goalflight_setup.SetupError as exc:
            assert_true("symlink rejected", "docs-private is a symlink" in str(exc))
        else:
            raise AssertionError("symlinked docs-private should be rejected")

        assert_true("dashboard not created after symlink failure", not repo.joinpath("dashboard").exists())


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
        goalflight_task.view_manifest_path(repo).unlink()
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


def test_scaffold_project_state_writes_managed_view_manifest() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-view-manifest-") as td:
        base = Path(td)
        repo = base / "repo"
        repo.mkdir()
        with _task_store_base(base / "store"):
            result = goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
            manifest = _read_view_manifest(repo)

        entry = manifest["views"]["dashboard/gf.js"]
        expected = hashlib.sha256((ROOT / "templates/state-skeleton/gf.js").read_bytes()).hexdigest()
        assert_true("view manifest reports schema", manifest["schema"] == goalflight_task.VIEW_MANIFEST_SCHEMA)
        assert_true("scaffold reports manifest update", "dashboard/gf.js" in result["view_manifest"]["updated"])
        assert_true("view manifest records source hash", entry["installed_source_sha256"] == expected)
        assert_true("view manifest records skill version", entry["skill_version"] == goalflight_task.current_skill_version())
        assert_true("view manifest records timestamp", isinstance(entry["installed_at"], str) and entry["installed_at"])


def test_refresh_views_updates_managed_stale_manifest_install_with_backup() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-refresh-stale-") as td:
        base = Path(td)
        repo = base / "repo"
        repo.mkdir()
        with _task_store_base(base / "store"):
            goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
            view = repo / "dashboard/gf.js"
            old_install = "global.GF = {}; // old official install\n"
            old_hash = hashlib.sha256(old_install.encode("utf-8")).hexdigest()
            view.write_text(old_install, encoding="utf-8")
            goalflight_task.update_view_manifest(
                repo,
                [{"rel_path": "dashboard/gf.js", "installed_source_sha256": old_hash}],
                skill_version="1.0.0",
                now="2026-01-01T00:00:00+00:00",
            )
            manifest = goalflight_task.load_view_manifest(repo)
            status = goalflight_doctor.classify_managed_view_asset(
                ROOT / "templates/state-skeleton/gf.js",
                view,
                manifest=manifest,
                rel_path="dashboard/gf.js",
            )

            result = goalflight_setup.refresh_managed_views(ROOT, repo)
            refreshed_manifest = _read_view_manifest(repo)

        expected = (ROOT / "templates/state-skeleton/gf.js").read_text(encoding="utf-8")
        assert_true("manifest install classified managed-stale", status["status"] == "managed-stale")
        assert_true("manifest install needs refresh", status["needs_refresh"] is True)
        assert_true("managed-stale refreshed", "dashboard/gf.js" in result["refreshed"])
        assert_true("managed-stale backup contains old install", repo.joinpath(result["backup"]["path"], "dashboard/gf.js").read_text(encoding="utf-8") == old_install)
        assert_true("managed-stale target updated", view.read_text(encoding="utf-8") == expected)
        assert_true(
            "manifest updated to current source",
            refreshed_manifest["views"]["dashboard/gf.js"]["installed_source_sha256"]
            == hashlib.sha256(expected.encode("utf-8")).hexdigest(),
        )


def test_refresh_views_skips_operator_customization_with_manifest() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-refresh-custom-") as td:
        base = Path(td)
        repo = base / "repo"
        repo.mkdir()
        with _task_store_base(base / "store"):
            goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
            view = repo / "dashboard/gf.js"
            old_install = "global.GF = {}; // old official install\n"
            old_hash = hashlib.sha256(old_install.encode("utf-8")).hexdigest()
            custom = "global.GF = {}; // operator customization\n"
            view.write_text(custom, encoding="utf-8")
            goalflight_task.update_view_manifest(
                repo,
                [{"rel_path": "dashboard/gf.js", "installed_source_sha256": old_hash}],
                skill_version="1.0.0",
                now="2026-01-01T00:00:00+00:00",
            )

            result = goalflight_setup.refresh_managed_views(ROOT, repo)

        assert_true("operator customization preserved", view.read_text(encoding="utf-8") == custom)
        assert_true("operator customization not refreshed", result["refreshed"] == [])
        assert_true("operator customization no backup", result["backup"] is None)
        assert_true("operator customization reported", any(row["action"] == "skip-customized" for row in result["rows"]))


def test_refresh_views_dry_run_mutates_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-refresh-dry-") as td:
        base = Path(td)
        repo = base / "repo"
        repo.mkdir()
        with _task_store_base(base / "store"):
            goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
            view = repo / "dashboard/gf.js"
            old_install = "global.GF = {}; // old official install\n"
            old_hash = hashlib.sha256(old_install.encode("utf-8")).hexdigest()
            view.write_text(old_install, encoding="utf-8")
            goalflight_task.update_view_manifest(
                repo,
                [{"rel_path": "dashboard/gf.js", "installed_source_sha256": old_hash}],
                skill_version="1.0.0",
                now="2026-01-01T00:00:00+00:00",
            )
            manifest_before = goalflight_task.view_manifest_path(repo).read_text(encoding="utf-8")
            backup_root = repo / "docs-private/log/project-state-backups"
            backups_before = sorted(str(path.relative_to(backup_root)) for path in backup_root.rglob("*")) if backup_root.exists() else []

            result = goalflight_setup.refresh_managed_views(ROOT, repo, dry_run=True)
            manifest_after = goalflight_task.view_manifest_path(repo).read_text(encoding="utf-8")
            backups_after = sorted(str(path.relative_to(backup_root)) for path in backup_root.rglob("*")) if backup_root.exists() else []

        assert_true("dry-run reports would refresh", "dashboard/gf.js" in result["would_refresh"])
        assert_true("dry-run target unchanged", view.read_text(encoding="utf-8") == old_install)
        assert_true("dry-run manifest unchanged", manifest_after == manifest_before)
        assert_true("dry-run no new backup", backups_after == backups_before)


def test_project_registry_upserted_from_scaffold_and_task_store_save() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-registry-") as td:
        base = Path(td)
        repo_scaffold = base / "repo-scaffold"
        repo_store = base / "repo-store"
        repo_scaffold.mkdir()
        repo_store.mkdir()
        with _task_store_base(base / "store"):
            scaffold_result = goalflight_setup.scaffold_project_state(ROOT, repo_scaffold, apply=True, today="2026-06-27")
            store = goalflight_task.TaskStore(repo_store)
            store.save_items_atomic([])
            projects = goalflight_task.read_project_registry()
            scaffold_meta = json.loads((goalflight_task.resolve_task_store_dir(repo_scaffold) / "store-meta.json").read_text(encoding="utf-8"))
            store_meta = json.loads((goalflight_task.resolve_task_store_dir(repo_store) / "store-meta.json").read_text(encoding="utf-8"))

        roots = {item["project_root"] for item in projects}
        assert_true("scaffold registry result ok", scaffold_result["registry"]["ok"] is True)
        assert_true("scaffold project indexed", str(repo_scaffold.resolve()) in roots)
        assert_true("task store project indexed", str(repo_store.resolve()) in roots)
        assert_true("scaffold store meta written", scaffold_meta["project_root"] == str(repo_scaffold.resolve()))
        assert_true("task store meta written", store_meta["project_root"] == str(repo_store.resolve()))


def test_refresh_views_all_reports_missing_registry_project_without_deleting() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-registry-missing-") as td:
        base = Path(td)
        missing = base / "missing-project"
        with _task_store_base(base / "store"):
            goalflight_task.upsert_project_registry(missing, skill_version="1.0.0", now="2026-07-07T00:00:00+00:00")
            before = goalflight_task.read_project_registry()
            result = goalflight_setup.refresh_managed_views_all(ROOT, dry_run=True)
            after = goalflight_task.read_project_registry()

        assert_true("missing root reported as gc candidate", result["gc_candidates"] and result["gc_candidates"][0]["project_root"] == str(missing.resolve(strict=False)))
        assert_true("missing root not deleted from registry", before == after)


def test_refresh_views_all_contains_hostile_registry_entries_per_entry() -> None:
    # rC P1: one malformed registry row (e.g. embedded NUL) must degrade to a
    # reported error row, not abort the sweep before valid projects.
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-registry-hostile-") as td:
        base = Path(td)
        good = base / "good-project"
        good.mkdir()
        with _task_store_base(base / "store"):
            goalflight_setup.scaffold_project_state(ROOT, good, apply=True, today="2026-07-07")
            index_path = goalflight_task.project_registry_index_path()
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            payload["projects"].insert(0, {"project_root": "bad\x00path"})
            index_path.write_text(json.dumps(payload), encoding="utf-8")
            before = goalflight_task.read_project_registry()
            result = goalflight_setup.refresh_managed_views_all(ROOT, dry_run=True)
            after = goalflight_task.read_project_registry()

        assert_true("hostile entry reported as error row", result["errors"] and result["errors"][0]["project_root"] == "bad\x00path")
        assert_true("valid project still swept after hostile entry", any(item["project_root"] == str(good.resolve()) for item in result["results"]))
        assert_true("hostile entry not deleted from registry", before == after)


def test_refresh_views_all_table_escapes_control_characters() -> None:
    # rC P3: control chars in untrusted roots must not break one-row-per-line.
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-registry-newline-") as td:
        base = Path(td)
        missing = base / "line\nbreak"
        with _task_store_base(base / "store"):
            goalflight_task.upsert_project_registry(missing, skill_version="1.0.0", now="2026-07-07T00:00:00+00:00")
            result = goalflight_setup.refresh_managed_views_all(ROOT, dry_run=True)
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            goalflight_setup._print_refresh_views_result(result)
        lines = buf.getvalue().splitlines()
        gc_lines = [l for l in lines if "gc-candidate" in l]
        assert_true("gc row stays on one physical line", len(gc_lines) == 1 and "\\n" in gc_lines[0])


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
            any("preserve customized managed view asset" in message for message in result["messages"]),
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
            "foreign managed path protected by manifest as customization",
            any("preserve customized managed view asset" in message for message in result["messages"]),
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


def test_loop_prompt_is_store_first() -> None:
    text = (ROOT / "templates/goalflight-loop-prompt.md").read_text(encoding="utf-8")
    status_idx = text.index("goalflight_session_status.py --text")
    next_idx = text.index("goalflight_task.py next")
    notes_idx = text.index("RESUME-NOTES")
    assert_true("loop prompt starts from session-status", status_idx < next_idx)
    assert_true("loop prompt reads notes after next", next_idx < notes_idx)
    assert_true("loop prompt names CONTINUE directive", "CONTINUE:" in text)
    assert_true("loop prompt names store as task state", "The living task state is the\nstore" in text)


def test_resume_command_is_store_first() -> None:
    text = (ROOT / "commands/resume.md").read_text(encoding="utf-8")
    assert_true("resume command runs list outstanding", "goalflight_task.py list outstanding" in text)
    assert_true("resume command runs next", "goalflight_task.py next" in text)
    assert_true("resume command names CONTINUE directive", "CONTINUE:" in text)
    assert_true("resume command drops pre-store queue wording", "next non-DONE queue item" not in text)


def test_task_lifecycle_names_nudge_consumption_path() -> None:
    text = (ROOT / "protocols/task-lifecycle.md").read_text(encoding="utf-8")
    assert_true("lifecycle names task-store pseudo-inbox", "task-store:<slug>" in text)
    assert_true("lifecycle names status consumption path", "goalflight_status.py" in text)
    assert_true("lifecycle names read-side mail summary", "read-side\nmail summary" in text)
    assert_true("lifecycle is v1.2 as-built", "AS-BUILT (v1.2)" in text)
    assert_true("lifecycle names append verb", "`append <id> [<id> ...]" in text)
    assert_true("lifecycle names pipe verb", "`pipe [--agent AGENT]" in text)
    assert_true("lifecycle names harvest source", "`harvest [--dry-run] [--source GLOB]" in text)
    assert_true("lifecycle names migrate source", "`migrate --source GLOB" in text)
    assert_true("mirror check names both roots", "check_tasks_mirror.js docs-private dashboard" in text)


def test_opencode_remote_bash_tail_wording_is_historical() -> None:
    text = (ROOT / "docs/hosts/opencode.md").read_text(encoding="utf-8")
    assert_true("opencode wording marks 1.0 as historical", "was introduced as a 1.0-era beta surface" in text)
    assert_true("opencode wording drops present-tense 1.0 beta", "is **beta** in 1.0.0" not in text)


def test_changelog_names_v12_task_surface_and_dispatch_timing() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = text.split("## [1.2.0]", 1)[1].split("## [1.0.11]", 1)[0]
    for phrase in ("harvest --source", "migrate --source", "`append` notes", "`pipe`"):
        assert_true(f"changelog names {phrase}", phrase in section)
    assert_true("changelog names operator-visible timing", "operator-visible timing\n  change" in section)
    assert_true("changelog tells blocking callers to pass foreground", "must pass\n  `--foreground`" in section)
    assert_true("changelog drops not-breaking claim", "Not a breaking\n  change" not in section)


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
        goalflight_task.view_manifest_path(repo).unlink()
        repo.joinpath("dashboard/gf.js").write_text("legacy worker-finished done vocabulary\n", encoding="utf-8")

        payload = goalflight_doctor.check_project_state_layout(repo, ROOT)
        skew = [
            item for item in payload["view_schema_skew"]
            if item["asset"] == "dashboard/gf.js"
        ]
        assert_true("managed view schema skew reported", skew)
        assert_true("managed view skew makes layout warning", payload["ok"] is False)
        assert_true("refresh path named", "--refresh-views" in skew[0]["message"])


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


def test_scaffold_project_state_rewrites_legacy_goalflight_root_pin() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-root-pin-") as td:
        repo = Path(td)
        repo.joinpath("AGENTS.md").write_text(
            "## Living state -- read the newest docs-private/RESUME-NOTES-*.md first\n"
            "## Goal Flight Routing\n"
            "- skill-root: `${GOALFLIGHT_ROOT:-~/.goal-flight}`\n"
            "- activation check: `python3 ${GOALFLIGHT_ROOT:-~/.goal-flight}/scripts/goalflight_session_status.py --text`\n"
            "- load order: AGENTS.md -> SKILL.md -> commands/*.md\n"
            "## Commands\n"
            "- test: `pytest`\n",
            encoding="utf-8",
        )

        result = goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        text = repo.joinpath("AGENTS.md").read_text(encoding="utf-8")
        assert_true("init updates legacy skill root pin", "${GOALFLIGHT_ROOT:-~/.goal-flight/skill}" in text)
        assert_true("init rewrites activation check pin", "${GOALFLIGHT_ROOT:-~/.goal-flight}/scripts" not in text)
        assert_true("rewrite message reported", "rewrote goal-flight skill-root pin" in result["agents"]["message"])


def test_project_readiness_requires_session_status_under_skill_root() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-state-layout-root-script-") as td:
        repo = Path(td)
        goalflight_setup.scaffold_project_state(ROOT, repo, apply=True, today="2026-06-27")
        repo.joinpath("docs-private/env-caveats.md").write_text("ok\n", encoding="utf-8")
        repo.joinpath("SKILL.md").write_text("# Project\n", encoding="utf-8")
        fake_root = repo / "fake-skill"
        fake_root.mkdir()
        repo.joinpath("AGENTS.md").write_text(
            "## Living state -- read the newest docs-private/RESUME-NOTES-*.md first\n"
            "## Goal Flight Routing\n"
            f"- skill-root: `{fake_root}`\n"
            "- load order: AGENTS.md -> SKILL.md -> commands/*.md\n"
            "## Commands\n"
            "- test: `pytest`\n",
            encoding="utf-8",
        )

        payload = goalflight_doctor.check_project_goalflight_readiness(repo)
        assert_true("missing session status script warned", "skill-root missing scripts/goalflight_session_status.py" in payload["warnings"])


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
        test_scaffold_project_state_empty_store_has_no_ready_frontier,
        test_scaffold_project_state_corrupt_legacy_store_fails_clean,
        test_scaffold_project_state_generates_dashboard_mirror_from_legacy_store,
        test_scaffold_project_state_dry_run_makes_no_changes,
        test_scaffold_project_state_creates_backup_before_apply,
        test_scaffold_project_state_preserves_unmanaged_handoff_mentions,
        test_scaffold_project_state_preserves_generic_living_state_prose,
        test_scaffold_project_state_rejects_symlinked_state_dir,
        test_scaffold_project_state_migrates_pre_v11_dashboard_index_to_root_dashboard,
        test_scaffold_project_state_refreshes_known_legacy_hash_with_backup,
        test_scaffold_project_state_preserves_customized_current_managed_view,
        test_scaffold_project_state_preserves_foreign_managed_path_file,
        test_scaffold_project_state_respects_gitignore_branches,
        test_scaffold_project_state_backfills_derivable_inflight_ledger_task_ids,
        test_state_layout_references_newest_resume_notes_not_handoff_file,
        test_loop_prompt_is_store_first,
        test_resume_command_is_store_first,
        test_task_lifecycle_names_nudge_consumption_path,
        test_opencode_remote_bash_tail_wording_is_historical,
        test_changelog_names_v12_task_surface_and_dispatch_timing,
        test_doctor_state_layout_reports_exact_missing_paths,
        test_doctor_state_layout_ignores_static_html_mtime,
        test_doctor_state_layout_reports_managed_view_schema_skew,
        test_doctor_state_layout_reports_customized_view_as_advisory,
        test_project_readiness_requires_agents_resume_pin,
        test_scaffold_project_state_rewrites_legacy_goalflight_root_pin,
        test_project_readiness_requires_session_status_under_skill_root,
        test_project_readiness_treats_initialized_state_layout_absence_as_warning,
        test_state_protocols_are_discoverable_from_index_and_commands,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
