#!/usr/bin/env python3
"""Agent-aware setup registrar for Goal Flight host wrappers.

The default mode is a dry run. Mutating setup requires both --apply and --yes.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import sys
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat  # noqa: E402
import goalflight_doctor  # noqa: E402
import goalflight_dispatch_states  # noqa: E402
import goalflight_ledger  # noqa: E402
from goalflight_adapter_gate import validate_adapter_gate  # noqa: E402


BACKUP_SCHEMA = "goalflight.setup-backup.v1"
MERGE_START = "# >>> goal-flight"
MERGE_END = "# <<< goal-flight"
SETUP_ALLOWED_GATE_REASONS = {"allowed", "candidate", "config_only", "not_installed", "probe_required", "unsupported"}
TARGET_PROJECT_TOKEN = "${GOALFLIGHT_TARGET_PROJECT}"
GSTACK_MINIMAL_SKILLS = ("review", "plan-eng-review", "office-hours")
GSTACK_EXTERNAL_SKILL_SOURCES = {
    "grill-me": (
        "https://raw.githubusercontent.com/udecode/plate/"
        "8aec9b9ebbb3d403eca5f84f962f18ab88691715/"
        "templates/plate-template/.agents/skills/grill-me/SKILL.md"
    ),
    "thermo-nuclear-code-quality-review": (
        "https://raw.githubusercontent.com/cursor/plugins/"
        "74dd2291e8e37b12fd6dc49b2acbd655c6bdaf12/"
        "cursor-team-kit/agents/thermo-nuclear-code-quality-review.md"
    ),
}
GSTACK_EXTERNAL_SKILLS = tuple(GSTACK_EXTERNAL_SKILL_SOURCES)
GSTACK_MINIMAL_REQUIRED_SKILLS = GSTACK_MINIMAL_SKILLS + GSTACK_EXTERNAL_SKILLS
GSTACK_EXTERNAL_DOWNLOAD_TIMEOUT = 10.0
GSTACK_EXTERNAL_DOWNLOAD_MAX_BYTES = 512 * 1024
GSTACK_EXTERNAL_SOURCE_OVERRIDE_GATE = "GOALFLIGHT_ALLOW_EXTERNAL_SOURCE_OVERRIDE"
GSTACK_SOURCE_OVERRIDE_GATE = "GOALFLIGHT_ALLOW_GSTACK_SOURCE_OVERRIDE"
GSTACK_SKILLS_DIR_OVERRIDE_GATE = "GOALFLIGHT_ALLOW_GSTACK_SKILLS_DIR_OVERRIDE"
GSTACK_INSTALL_CHOICES = {"minimal", "full", "skip"}
GSTACK_FULL_INSTALL_HOSTS = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}
STATE_BACKUP_REL = Path("docs-private/log/project-state-backups")
TASK_ID_PATTERN = re.compile(r"\b[tb]-\d+\b")
RESUME_PIN_TEXT = "newest docs-private/RESUME-NOTES-*.md"
HANDOFF_FILE_RE = re.compile(r"(?<!state-)(?<![A-Za-z0-9_/-])handoff\.md(?![A-Za-z0-9_.-])")
MANAGED_POINTER_PHRASES = (
    "living current state",
    "living state",
    "living pin",
    "current-state pin",
    "state pointer",
    "handoff-<date>",
)
MANAGED_STATE_HEADINGS = {
    "## living state — read the newest docs-private/resume-notes-*.md first",
    "## living state -- read the newest docs-private/resume-notes-*.md first",
}


class SetupError(RuntimeError): pass


@dataclass(frozen=True)
class SetupPlanItem:
    agent: str
    manifest: dict[str, Any]
    destination_ids: set[str]
    addon_ids: set[str] | None


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _expand_target(raw: str, target_project: Path | None = None) -> Path:
    if TARGET_PROJECT_TOKEN in raw:
        if target_project is None:
            raise SetupError(f"target path requires --target-project: {raw}")
        raw = raw.replace(TARGET_PROJECT_TOKEN, str(target_project))
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def _state_root() -> Path:
    raw = os.environ.get("XDG_STATE_HOME")
    if raw:
        return Path(raw).expanduser() / "goal-flight"
    return Path.home() / ".local/state/goal-flight"


def _git_check_ignored(project_root: Path, relpath: str) -> bool | None:
    if not (project_root / ".git").exists():
        return None
    proc = subprocess.run(
        ["git", "check-ignore", "-q", relpath],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _template_text(path: Path, replacements: dict[str, str]) -> str:
    text = path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _relative_to_project(project_root: Path, path: Path) -> str:
    return path.relative_to(project_root).as_posix()


def _is_managed_state_heading(line: str) -> bool:
    return line.strip().casefold() in MANAGED_STATE_HEADINGS


def _project_agents_template(repo_root: Path, target_project: Path) -> str:
    return _template_text(
        repo_root / "templates/project-agents.md",
        {"<PROJECT_NAME>": target_project.name},
    )


def _replace_state_pointer_line(line: str) -> str:
    replacements = (
        ("Living current state is `handoff.md`;", "Living current state is the newest `docs-private/RESUME-NOTES-*.md`;"),
        ("`handoff.md` stays the living pin", "the newest `docs-private/RESUME-NOTES-*.md` stays the living pin"),
        ("docs-private/handoff.md", RESUME_PIN_TEXT),
        ("`handoff.md`", "the newest `docs-private/RESUME-NOTES-*.md`"),
        ("`handoff-<DATE>.md` files", "`history.md` entries"),
        ("handoff-<DATE>.md", "history.md"),
    )
    out = line
    for old, new in replacements:
        out = out.replace(old, new)
    out = HANDOFF_FILE_RE.sub(RESUME_PIN_TEXT, out)
    return out


def _replace_state_pointer_text(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_managed_comment = False
    in_managed_section = False
    for line in lines:
        lower = line.casefold()
        stripped = line.lstrip()
        if "<!-- >>> goal-flight" in lower:
            in_managed_comment = True
        if stripped.startswith("## "):
            in_managed_section = (
                stripped.strip().casefold() == "## goal flight routing"
                or _is_managed_state_heading(stripped)
            )
        managed_pointer_line = HANDOFF_FILE_RE.search(line) and any(
            phrase in lower for phrase in MANAGED_POINTER_PHRASES
        )
        if in_managed_comment or in_managed_section or managed_pointer_line:
            out.append(_replace_state_pointer_line(line))
        else:
            out.append(line)
        if "<!-- <<< goal-flight" in lower:
            in_managed_comment = False
    return "".join(out)


def _has_unmanaged_handoff_pointer(text: str) -> bool:
    return HANDOFF_FILE_RE.search(_replace_state_pointer_text(text)) is not None


def _ensure_agents_state_pin(repo_root: Path, target_project: Path) -> dict[str, Any]:
    agents = target_project / "AGENTS.md"
    template = _project_agents_template(repo_root, target_project)
    if agents.exists() and not agents.is_file():
        raise SetupError(
            "refusing to update AGENTS.md because it is not a regular file; "
            "move it aside or convert it to a file, then rerun init"
        )
    if not agents.exists():
        return {
            "path": "AGENTS.md",
            "action": "create",
            "content": template,
            "message": "AGENTS.md absent; will create local Goal Flight routing and newest RESUME-NOTES pin",
        }

    text = agents.read_text(encoding="utf-8", errors="replace")
    updated = _replace_state_pointer_text(text)
    unmanaged_handoff_pointer = _has_unmanaged_handoff_pointer(text)
    lower = updated.casefold()
    changes: list[str] = []
    if updated != text:
        changes.append("rewrote handoff.md state pointer to newest docs-private/RESUME-NOTES-*.md")
    if unmanaged_handoff_pointer:
        changes.append("left unmanaged handoff.md mention untouched; review manually if it is a Goal Flight state pointer")

    if not ("newest" in lower and "resume-notes" in lower and "docs-private" in lower):
        living_block = "\n".join(template.splitlines()[18:34]).rstrip() + "\n\n"
        updated = f"{living_block}{updated}"
        changes.append("added living-state pin for newest docs-private/RESUME-NOTES-*.md")
        lower = updated.casefold()

    if "## goal flight routing" not in lower:
        routing_start = template.find("## Goal Flight Routing")
        routing_end = template.find("## Git workflow")
        routing_block = template[routing_start:routing_end].rstrip()
        updated = updated.rstrip() + "\n\n" + routing_block + "\n"
        changes.append("appended Goal Flight routing block")
        lower = updated.casefold()

    if updated == text:
        return {
            "path": "AGENTS.md",
            "action": "skip",
            "content": None,
            "message": "; ".join(changes) if changes else "AGENTS.md already pins newest docs-private/RESUME-NOTES-*.md; no rewrite needed",
        }
    return {
        "path": "AGENTS.md",
        "action": "update",
        "content": updated,
        "message": "; ".join(changes),
    }


def _extract_task_ids_from_text(text: str) -> list[str]:
    out: list[str] = []
    for task_id in TASK_ID_PATTERN.findall(text):
        if task_id not in out:
            out.append(task_id)
    return out


def _existing_task_ids(record: dict[str, Any]) -> list[str]:
    values: list[str] = []
    raw_many = record.get("task_ids")
    if isinstance(raw_many, list):
        values.extend(str(value) for value in raw_many if isinstance(value, str))
    raw_one = record.get("task_id")
    if isinstance(raw_one, str):
        values.append(raw_one)
    out: list[str] = []
    for value in values:
        if TASK_ID_PATTERN.fullmatch(value) and value not in out:
            out.append(value)
    return out


def _derive_task_ids_from_record(record: dict[str, Any]) -> list[str]:
    haystacks: list[str] = []
    for key in ("dispatch_id", "prompt_id", "prompt_path", "stdout_path", "stderr_path", "status_path"):
        value = record.get(key)
        if isinstance(value, str):
            haystacks.append(value)
    prompt_path = record.get("prompt_path")
    if isinstance(prompt_path, str):
        path = Path(prompt_path).expanduser()
        try:
            if path.is_file() and path.stat().st_size <= 1024 * 1024:
                haystacks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return _extract_task_ids_from_text("\n".join(haystacks))


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True))


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _managed_view_needs_refresh(source: Path, dest: Path) -> bool:
    return bool(goalflight_doctor.classify_managed_view_asset(source, dest).get("needs_refresh"))


def _plan_ledger_task_id_backfill(target_project: Path) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    target = str(target_project)
    for record in goalflight_ledger.read_records():
        if record.get("project_root") != target:
            continue
        if goalflight_dispatch_states.is_terminal_state(record.get("state")):
            continue
        if _existing_task_ids(record):
            continue
        derived = _derive_task_ids_from_record(record)
        if not derived:
            continue
        updates.append({
            "dispatch_id": record.get("dispatch_id"),
            "task_ids": derived,
            "preimage": _json_copy(record),
            "message": "derived task_ids from dispatch metadata/prompt path",
        })
    return updates


def _apply_ledger_task_id_backfill(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    with goalflight_ledger.StateLock():
        for update in updates:
            dispatch_id = update.get("dispatch_id")
            if not isinstance(dispatch_id, str):
                continue
            path = goalflight_ledger.record_path(dispatch_id)
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if _existing_task_ids(record):
                continue
            task_ids = list(update["task_ids"])
            record["task_ids"] = task_ids
            if len(task_ids) == 1:
                record["task_id"] = task_ids[0]
            goalflight_ledger.write_record(record)
            applied.append(update)
    return applied


def _create_project_state_backup(
    target_project: Path,
    rel_paths: list[str],
    *,
    reason: str,
    ledger_preimages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    backup_root = target_project / STATE_BACKUP_REL / _now_slug()
    records: list[dict[str, Any]] = []
    backup_root.mkdir(parents=True, exist_ok=True)
    for rel in sorted(set(rel_paths)):
        source = target_project / rel
        entry: dict[str, Any] = {"path": rel, "exists": source.exists()}
        if source.exists():
            backup = backup_root / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(source, backup, symlinks=True)
            elif source.is_file() or source.is_symlink():
                shutil.copy2(source, backup)
            else:
                entry["skipped"] = "not a regular file or directory"
            if backup.exists():
                entry["backup"] = _relative_to_project(target_project, backup)
        records.append(entry)
    manifest = {
        "schema": "goalflight.project-state-backup.v1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
        "entries": records,
    }
    if ledger_preimages:
        manifest["ledger_task_id_backfill_preimages"] = ledger_preimages
    _atomic_write(backup_root / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {
        "path": _relative_to_project(target_project, backup_root),
        "manifest": _relative_to_project(target_project, backup_root / "manifest.json"),
        "entries": records,
    }


def scaffold_project_state(
    repo_root: Path,
    target_project: Path,
    *,
    apply: bool = False,
    today: str | None = None,
) -> dict[str, Any]:
    """Create the canonical docs-private store and repo-root dashboard."""
    repo_root = repo_root.resolve()
    target_project = target_project.resolve()
    skeleton = repo_root / goalflight_doctor.STATE_SKELETON_REL
    if not skeleton.is_dir():
        raise SetupError(f"state skeleton missing: {skeleton}")
    docs_private = target_project / "docs-private"
    dashboard = target_project / "dashboard"
    if docs_private.exists() and not docs_private.is_dir():
        raise SetupError(
            f"refusing to scaffold project state because docs-private exists but "
            f"is not a directory: {docs_private}. Move or rename that path, then "
            "rerun `/goal-flight init`; init only creates a docs-private directory "
            "and never overwrites operator files."
        )
    if dashboard.exists() and not dashboard.is_dir():
        raise SetupError(
            f"refusing to scaffold project dashboard because dashboard exists but "
            f"is not a directory: {dashboard}. Move or rename that path, then "
            "rerun `/goal-flight init`; init only creates a dashboard directory "
            "and never overwrites operator files."
        )
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()

    would_create_dirs: list[str] = []
    would_create_files: list[str] = []
    skipped_existing_files: list[str] = []
    managed_view_replacements: list[dict[str, str]] = []
    messages: list[str] = []

    dirs = [docs_private, dashboard] + [docs_private / rel for rel in goalflight_doctor.CANONICAL_STATE_DIRS]
    for path in dirs:
        rel = _relative_to_project(target_project, path) + "/"
        if path.is_dir():
            continue
        would_create_dirs.append(rel)

    for source in sorted(skeleton.rglob("*")):
        if not source.is_file():
            continue
        rel = source.relative_to(skeleton)
        rel_key = rel.as_posix()
        dest_root = dashboard if rel_key in goalflight_doctor.DASHBOARD_ASSETS else docs_private
        dest = dest_root / rel
        rel_out = _relative_to_project(target_project, dest)
        if dest.exists():
            if (
                rel_key in goalflight_doctor.MANAGED_VIEW_ASSETS
                and dest.is_file()
            ):
                view_status = goalflight_doctor.classify_managed_view_asset(source, dest)
                if view_status.get("needs_refresh"):
                    managed_view_replacements.append({
                        "path": rel_out,
                        "template": f"{goalflight_doctor.STATE_SKELETON_REL.as_posix()}/{rel.as_posix()}",
                        "reason": view_status.get("reason"),
                        "message": "legacy goal-flight view (pre-v1.1 renderer); backing up and refreshing",
                    })
                    messages.append(
                        f"legacy goal-flight view (pre-v1.1 renderer); backing up and refreshing: {rel_out}"
                    )
                    continue
                if view_status.get("status") == "current" and view_status.get("customized"):
                    skipped_existing_files.append(rel_out)
                    messages.append(
                        f"preserve customized current managed view asset: {rel_out} "
                        "(manual review; v1.1 renderer contract present)"
                    )
                    continue
                if view_status.get("status") == "foreign":
                    skipped_existing_files.append(rel_out)
                    messages.append(
                        f"unrecognized file at managed view path; left for manual review: {rel_out}"
                    )
                    continue
            skipped_existing_files.append(rel_out)
            messages.append(f"skip existing operator file: {rel_out} (create-if-absent never overwrites)")
            continue
        would_create_files.append(rel_out)

    resume_notes = sorted(docs_private.glob("RESUME-NOTES-*.md")) if docs_private.is_dir() else []
    if not resume_notes:
        resume_dest = docs_private / f"RESUME-NOTES-{today}.md"
        rel_out = _relative_to_project(target_project, resume_dest)
        if resume_dest.exists():
            skipped_existing_files.append(rel_out)
            messages.append(f"skip existing resume note: {rel_out} (create-if-absent never overwrites)")
        elif rel_out not in would_create_files:
            would_create_files.append(rel_out)

    agents_plan = _ensure_agents_state_pin(repo_root, target_project)
    if agents_plan["action"] == "skip":
        messages.append(agents_plan["message"])

    ledger_backfill_plan = _plan_ledger_task_id_backfill(target_project)
    if ledger_backfill_plan:
        messages.append(
            f"will backfill task_ids on {len(ledger_backfill_plan)} in-flight ledger record(s) where derivable"
        )

    planned_changes = bool(
        would_create_dirs
        or would_create_files
        or agents_plan["action"] in {"create", "update"}
        or ledger_backfill_plan
        or managed_view_replacements
    )
    backup = None
    created_dirs: list[str] = []
    created_files: list[str] = []
    touched_html: list[str] = []
    refreshed_managed_views: list[str] = []
    agents_result = {
        "path": agents_plan["path"],
        "action": "would_" + agents_plan["action"] if agents_plan["action"] != "skip" else "skip",
        "message": agents_plan["message"],
    }
    ledger_backfills: list[dict[str, Any]] = []

    if apply and planned_changes:
        backup_paths = ["AGENTS.md"]
        if would_create_dirs or would_create_files:
            backup_paths.extend(skipped_existing_files)
        backup_paths.extend(item["path"] for item in managed_view_replacements)
        ledger_preimages = [
            {
                "dispatch_id": item["dispatch_id"],
                "task_ids": item["task_ids"],
                "record": item["preimage"],
            }
            for item in ledger_backfill_plan
        ]
        backup = _create_project_state_backup(
            target_project,
            backup_paths,
            reason="project-state scaffold/migration before apply",
            ledger_preimages=ledger_preimages,
        )

    if apply:
        for rel in would_create_dirs:
            path = target_project / rel.rstrip("/")
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(rel)
        for rel_out in would_create_files:
            dest = target_project / rel_out
            if dest.exists():
                skipped_existing_files.append(rel_out)
                messages.append(f"skip existing operator file: {rel_out} (create-if-absent never overwrites)")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.name.startswith("RESUME-NOTES-"):
                resume_template = repo_root / "templates/resume-notes.md"
                _atomic_write(dest, _template_text(resume_template, {"<DATE>": today}))
            else:
                rel_from_root = dest.relative_to(dashboard if rel_out.startswith("dashboard/") else docs_private)
                source = skeleton / rel_from_root
                _copy_atomic(source, dest)
            created_files.append(rel_out)
        for item in managed_view_replacements:
            rel_out = item["path"]
            dest = target_project / rel_out
            source = skeleton / Path(rel_out).relative_to("dashboard")
            if dest.exists() and _managed_view_needs_refresh(source, dest):
                _copy_atomic(source, dest)
                refreshed_managed_views.append(rel_out)
        if agents_plan["action"] in {"create", "update"}:
            _atomic_write(target_project / "AGENTS.md", agents_plan["content"])
            agents_result["action"] = agents_plan["action"]
        ledger_backfills = _apply_ledger_task_id_backfill(ledger_backfill_plan)
    else:
        messages.append("dry-run only: no files, directories, managed view assets, AGENTS.md, backups, or ledger records were changed")

    docs_private_gitignored = _git_check_ignored(target_project, "docs-private/")
    dashboard_gitignored = _git_check_ignored(target_project, "dashboard/")
    if docs_private_gitignored is True:
        messages.append("docs-private/ is gitignored here; scaffold leaves private state untracked")
    elif docs_private_gitignored is False:
        messages.append("docs-private/ is not gitignored here; scaffold preserves this repo's tracking policy")
    else:
        messages.append("docs-private/ gitignore status unknown; scaffold does not force-ignore or force-add it")
    if dashboard_gitignored is True:
        messages.append("dashboard/ is gitignored here; scaffold leaves generated browser views untracked")
    elif dashboard_gitignored is False:
        messages.append("dashboard/ is not gitignored here; add `/dashboard/` to .gitignore for generated browser views")
    else:
        messages.append("dashboard/ gitignore status unknown; scaffold does not force-ignore or force-add it")

    would_create_dirs_out = [] if apply else would_create_dirs
    would_create_files_out = [] if apply else would_create_files
    would_refresh_managed_views = [] if apply else managed_view_replacements
    skipped = sorted(set(skipped_existing_files))
    return {
        "schema": "goalflight.project-state-scaffold.v1",
        "project_root": str(target_project),
        "docs_private": str(docs_private),
        "dashboard": str(dashboard),
        "skeleton": str(skeleton),
        "apply": apply,
        "docs_private_gitignored": docs_private_gitignored,
        "dashboard_gitignored": dashboard_gitignored,
        "backup": backup,
        "created_dirs": created_dirs,
        "would_create_dirs": would_create_dirs_out,
        "created_files": created_files,
        "would_create_files": would_create_files_out,
        "touched_created_html": touched_html,
        "refreshed_managed_views": refreshed_managed_views,
        "would_refresh_managed_views": would_refresh_managed_views,
        "skipped_existing_files": skipped,
        "agents": agents_result,
        "ledger_task_id_backfill": {
            "would_update": ledger_backfill_plan if not apply else [],
            "updated": ledger_backfills,
        },
        "messages": messages,
        "summary": (
            f"created_files={len(created_files)} would_create_files={len(would_create_files_out)} "
            f"refreshed_managed_views={len(refreshed_managed_views)} skipped_existing_files={len(skipped)}"
        ),
    }


def _load_manifest(repo_root: Path, agent: str) -> dict[str, Any]:
    path = repo_root / "adapters" / f"{agent}.json"
    if not path.exists():
        raise SetupError(f"adapter manifest not found: {path}")
    data = json.loads(path.read_text())
    if data.get("agent_id") != agent:
        raise SetupError(f"adapter id mismatch: {path}")
    return data


def _load_manifests(repo_root: Path) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    for path in sorted((repo_root / "adapters").glob("*.json")):
        if path.name.endswith(".schema.json"):
            continue
        data = json.loads(path.read_text())
        if data.get("agent_id"):
            manifests.append(data)
    return manifests


def _actions(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return list(manifest.get("packaging", {}).get("install_actions", []))


def _selected_actions(manifest: dict[str, Any], destination_ids: set[str] | None) -> list[dict[str, Any]]:
    actions = _actions(manifest)
    if not destination_ids:
        return actions
    selected: list[dict[str, Any]] = []
    for action in actions:
        declared = set(action.get("destinations", []))
        if not declared or declared.intersection(destination_ids):
            selected.append(action)
    return selected


def _plugin_action(manifest: dict[str, Any]) -> str:
    plugin = manifest.get("packaging", {}).get("plugin_manifest", {})
    if plugin.get("supported") and plugin.get("path"):
        return f"PLUGIN register_plugin source={plugin['path']} api_status={plugin.get('api_status')}"
    supported = str(bool(plugin.get("supported", False))).lower()
    return f"PLUGIN skip supported={supported} api_status={plugin.get('api_status')}"


def _codex_plugin_commands(repo_root: Path) -> list[list[str]]:
    return [
        ["codex", "plugin", "remove", "goal-flight@goal-flight"],
        ["codex", "plugin", "marketplace", "add", str(repo_root)],
        ["codex", "plugin", "add", "goal-flight@goal-flight"],
    ]


def _codex_plugin_unregister_commands() -> list[list[str]]:
    return [["codex", "plugin", "remove", "goal-flight@goal-flight"], ["codex", "plugin", "marketplace", "remove", "goal-flight"]]


def _format_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def _run_codex_plugin_registration(repo_root: Path) -> None:
    fake_log = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_SETUP_FAKE_CODEX_LOG",
        "",
        test_mode=True,
    )
    commands = _codex_plugin_commands(repo_root)
    if fake_log:
        fake_path = Path(fake_log).expanduser()
        fake_path.parent.mkdir(parents=True, exist_ok=True)
        with fake_path.open("a") as handle:
            for argv in commands:
                handle.write(_format_command(argv) + "\n")
                print(f"CODEX {_format_command(argv)}")
        if os.environ.get("GOALFLIGHT_SETUP_FAKE_CODEX_FAIL_VERIFY"):
            raise SetupError("fake codex plugin registration failed")
        return

    for argv in commands:
        result = subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
        combined = f"{result.stdout}\n{result.stderr}".lower()
        allowed = ("already", "exists", "not installed", "not found")
        if result.returncode != 0 and not any(word in combined for word in allowed):
            raise SetupError(
                "codex plugin registration failed: "
                f"{_format_command(argv)}\n{result.stderr.strip() or result.stdout.strip()}"
            )
        print(f"CODEX {_format_command(argv)}")
    result = subprocess.run(
        ["codex", "plugin", "list", "--marketplace", "goal-flight"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not re.search(
        r"goal-flight@goal-flight\b.*\binstalled\b.*\benabled\b",
        result.stdout,
        re.I | re.S,
    ):
        raise SetupError(
            "codex plugin registration did not verify as installed and enabled\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    cached_manifest = (
        Path.home()
        / ".codex/plugins/cache/goal-flight/goal-flight"
        / json.loads((repo_root / "plugins/goal-flight/.codex-plugin/plugin.json").read_text())["version"]
        / ".codex-plugin/plugin.json"
    )
    if cached_manifest.exists():
        cached = json.loads(cached_manifest.read_text())
        if cached.get("interface", {}).get("displayName") != "goal-flight":
            raise SetupError(f"codex plugin cache is stale: {cached_manifest}")
    print("VERIFY codex plugin goal-flight@goal-flight installed enabled")


def _run_codex_plugin_unregistration() -> None:
    fake_log = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_SETUP_FAKE_CODEX_LOG",
        "",
        test_mode=True,
    )
    commands = _codex_plugin_unregister_commands()
    if fake_log:
        fake_path = Path(fake_log).expanduser()
        fake_path.parent.mkdir(parents=True, exist_ok=True)
        with fake_path.open("a") as handle:
            for argv in commands:
                handle.write(_format_command(argv) + "\n")
                print(f"CODEX {_format_command(argv)}")
        return

    for argv in commands:
        result = subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
        combined = f"{result.stdout}\n{result.stderr}".lower()
        allowed = ("not installed", "not found", "unknown", "missing")
        if result.returncode != 0 and not any(word in combined for word in allowed):
            raise SetupError(
                "codex plugin unregistration failed: "
                f"{_format_command(argv)}\n{result.stderr.strip() or result.stdout.strip()}"
            )
        print(f"CODEX {_format_command(argv)}")


def _codex_context_mode_optin() -> bool:
    """Whether to REGISTER context-mode for codex at install. Default OFF: dispatched
    codex workers disable context-mode at the worker boundary (goalflight_dispatch
    `_codex_context_mode_enabled`) because its ctx_index elicitation wedges `codex exec`.
    Registering it by default would write a `~/.codex/config.toml` MCP entry the runtime
    always turns off. Opt in with the SAME switch the runtime honors so install and
    dispatch agree. Other engines (cursor/opencode) keep context-mode."""
    return os.environ.get("GOALFLIGHT_CODEX_CONTEXT_MODE", "").strip().lower() in {
        "1", "true", "yes", "enabled", "on"}


def _codedb_codex_optin() -> bool:
    """Whether to REGISTER codedb for codex at install. Default ON: codedb is the
    SAFE, read-only swap-in for the disabled context-mode (#18) — it gives codex
    workers indexed code-intelligence without the exec-mode elicitation wedge,
    *provided* the per-tool approve entries are present (which register-codedb-codex.py
    writes; an unconfigured codedb_context call cancels and wedges the worker).
    The registrar itself no-ops when the codedb binary is absent, so default-on is
    safe. Opt out with GOALFLIGHT_CODEX_CODEDB in {0,false,no,off,disabled}."""
    raw = os.environ.get("GOALFLIGHT_CODEX_CODEDB", "").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _run_codex_codedb_registration(repo_root: Path, *, dry_run: bool) -> None:
    script = repo_root / "scripts" / "register-codedb-codex.py"
    if not script.exists():
        raise SetupError(f"codedb registration script missing: {script}")
    check_argv = [sys.executable, str(script), "--check"]
    apply_argv = [sys.executable, str(script)]
    if dry_run:
        print(f"BOOTSTRAP {_format_command(check_argv)}")
        print(f"BOOTSTRAP {_format_command(apply_argv)}")
        return
    result = subprocess.run(apply_argv, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if result.returncode != 0:
        raise SetupError(
            "codedb registration failed: "
            f"{_format_command(apply_argv)}\n{result.stderr.strip() or result.stdout.strip()}"
        )
    print(f"BOOTSTRAP {_format_command(apply_argv)}")


def _run_codex_context_mode_registration(repo_root: Path, *, dry_run: bool) -> None:
    script = repo_root / "scripts" / "register-context-mode-codex.py"
    if not script.exists():
        raise SetupError(f"context-mode registration script missing: {script}")
    check_argv = [sys.executable, str(script), "--check"]
    apply_argv = [sys.executable, str(script)]
    if dry_run:
        print(f"BOOTSTRAP {_format_command(check_argv)}")
        print(f"BOOTSTRAP {_format_command(apply_argv)}")
        return

    fake_log = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_SETUP_FAKE_CONTEXT_MODE_LOG",
        "",
        test_mode=True,
    )
    if fake_log:
        fake_path = Path(fake_log).expanduser()
        fake_path.parent.mkdir(parents=True, exist_ok=True)
        with fake_path.open("a") as handle:
            handle.write(_format_command(apply_argv) + "\n")
        print(f"BOOTSTRAP {_format_command(apply_argv)}")
        return

    result = subprocess.run(apply_argv, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if result.returncode != 0:
        raise SetupError(
            "context-mode registration failed: "
            f"{_format_command(apply_argv)}\n{result.stderr.strip() or result.stdout.strip()}"
        )
    print(f"BOOTSTRAP {_format_command(apply_argv)}")


def _run_cursor_context_mode_registration(
    repo_root: Path,
    *,
    dry_run: bool,
    target_project: Path,
    destination_ids: set[str] | None,
    backups_root: Path | None = None,
    backup_manifest: Path | None = None,
    agent: str = "cursor",
    existing_records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    script = repo_root / "scripts" / "register-context-mode-cursor.py"
    if not script.exists():
        raise SetupError(f"cursor context-mode registration script missing: {script}")

    selected = destination_ids or set()
    global_destinations = {
        "cursor-desktop-controller",
        "cursor-agents-standard-controller",
        "cursor-claude-link-controller",
        "cursor-cli-worker",
    }
    scopes: list[str] = []
    if not selected or selected.intersection(global_destinations):
        scopes.append("global")
    if "cursor-project-controller" in selected:
        scopes.append("project")

    records: list[dict[str, Any]] = []
    for scope in scopes:
        argv = [sys.executable, str(script), "--scope", scope, "--project-root", str(target_project)]
        if dry_run:
            print(f"BOOTSTRAP {_format_command(argv + ['--check'])}")
            print(f"BOOTSTRAP {_format_command(argv)}")
            continue
        argv.append("--no-sidecar-backup")
        if backups_root is not None:
            target = Path.home() / ".cursor/mcp.json"
            if scope == "project":
                target = target_project / ".cursor/mcp.json"
            record = _record_backup(
                {"kind": "merge_config", "target": str(target), "rollback": "restore_backup"},
                target,
                backups_root,
            )
            records.append(record)
            if backup_manifest is not None and existing_records is not None:
                _write_backup_manifest(backup_manifest, agent, existing_records + records)
        result = subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
        if result.returncode != 0:
            raise SetupError(
                "cursor context-mode registration failed: "
                f"{_format_command(argv)}\n{result.stderr.strip() or result.stdout.strip()}"
            )
        print(f"BOOTSTRAP {_format_command(argv)}")
    return records


def _run_opencode_context_mode_registration(
    repo_root: Path,
    *,
    dry_run: bool,
    target_project: Path,
    destination_ids: set[str] | None,
    backups_root: Path | None = None,
    backup_manifest: Path | None = None,
    agent: str = "opencode",
    existing_records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    script = repo_root / "scripts" / "hosts" / "opencode" / "register_context_mode.py"
    if not script.exists():
        raise SetupError(f"opencode context-mode registration script missing: {script}")

    selected = destination_ids or set()
    global_destinations = {
        "opencode-global-controller",
        "opencode-agents-standard-controller",
        "opencode-claude-link-controller",
        "opencode-acp-worker",
    }
    scopes: list[str] = []
    if not selected or selected.intersection(global_destinations):
        scopes.append("global")
    if "opencode-project-controller" in selected:
        scopes.append("project")

    records: list[dict[str, Any]] = []
    for scope in scopes:
        argv = [sys.executable, str(script), "--scope", scope, "--project-root", str(target_project)]
        if dry_run:
            print(f"BOOTSTRAP {_format_command(argv + ['--check'])}")
            print(f"BOOTSTRAP {_format_command(argv)}")
            continue
        argv.append("--no-sidecar-backup")
        if backups_root is not None:
            target = Path.home() / ".config/opencode/opencode.json"
            if scope == "project":
                target = target_project / "opencode.json"
            record = _record_backup(
                {"kind": "merge_config", "target": str(target), "rollback": "restore_backup"},
                target,
                backups_root,
            )
            records.append(record)
            if backup_manifest is not None and existing_records is not None:
                _write_backup_manifest(backup_manifest, agent, existing_records + records)
        result = subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
        if result.returncode != 0:
            raise SetupError(
                "opencode context-mode registration failed: "
                f"{_format_command(argv)}\n{result.stderr.strip() or result.stdout.strip()}"
            )
        print(f"BOOTSTRAP {_format_command(argv)}")
    return records


def _check_codex_cli_worker_surface(*, dry_run: bool) -> None:
    commands = [
        ["codex", "--version"],
        ["codex", "exec", "--help"],
    ]
    if dry_run:
        for argv in commands:
            print(f"WORKER_CHECK {_format_command(argv)}")
        return
    for argv in commands:
        result = subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
        if result.returncode != 0:
            raise SetupError(
                "codex CLI worker check failed: "
                f"{_format_command(argv)}\n{result.stderr.strip() or result.stdout.strip()}"
            )
        first_line = (result.stdout or result.stderr).strip().splitlines()
        detail = first_line[0] if first_line else "ok"
        print(f"WORKER_CHECK {_format_command(argv)} status=ok detail={detail[:120]}")


def _check_cursor_cli_worker_surface(*, dry_run: bool) -> None:
    command = "cursor-agent --version"
    if dry_run:
        print(f"WORKER_CHECK {command}")
        return
    path = shutil.which("cursor-agent") or str(Path.home() / ".local/bin/cursor-agent")
    if not Path(path).exists() and shutil.which("cursor-agent") is None:
        raise SetupError("cursor-agent worker check failed: cursor-agent not found")
    result = subprocess.run([path, "--version"], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    combined = f"{result.stdout or ''}\n{result.stderr or ''}"
    keychain_locked = result.returncode != 0 and re.search(r"keychain.*locked", combined, re.I)
    if result.returncode != 0 and not keychain_locked:
        raise SetupError(
            "cursor-agent worker check failed: "
            f"{path} --version\n{result.stderr.strip() or result.stdout.strip()}"
        )
    first_line = (result.stdout or result.stderr).strip().splitlines()
    detail = first_line[0] if first_line else "ok"
    print(f"WORKER_CHECK cursor-agent --version status=ok detail={detail[:120]}")


def _check_opencode_acp_worker_surface(*, dry_run: bool) -> None:
    if dry_run:
        print("WORKER_CHECK opencode --version")
        print("WORKER_CHECK opencode acp --help")
        return
    path = shutil.which("opencode") or str(Path.home() / ".local/bin/opencode")
    if not Path(path).exists() and shutil.which("opencode") is None:
        raise SetupError("opencode ACP worker check failed: opencode not found")
    version = subprocess.run([path, "--version"], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if version.returncode != 0:
        raise SetupError(
            "opencode ACP worker check failed: "
            f"{path} --version\n{version.stderr.strip() or version.stdout.strip()}"
        )
    acp = subprocess.run([path, "acp", "--help"], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if acp.returncode != 0:
        help_result = subprocess.run([path, "--help"], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
        help_text = (help_result.stdout or help_result.stderr or "")
        if "acp" not in help_text.casefold():
            raise SetupError(
                "opencode ACP worker check failed: "
                f"{path} acp --help unavailable and acp not listed in {path} --help"
            )
    first_line = (version.stdout or version.stderr).strip().splitlines()
    detail = first_line[0] if first_line else "ok"
    print(f"WORKER_CHECK opencode --version status=ok detail={detail[:120]}")


def _run_mac_worker_path_setup(repo_root: Path, *, dry_run: bool) -> None:
    if dry_run or sys.platform != "darwin":
        return
    script = repo_root / "scripts" / "hosts" / "fleet" / "setup_worker_path.sh"
    if not script.exists():
        return
    result = subprocess.run(["bash", str(script)], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SetupError(f"mac worker PATH setup failed: {detail}")


def _run_host_bootstrap(
    repo_root: Path,
    agent: str,
    *,
    dry_run: bool,
    target_project: Path,
    destination_ids: set[str] | None = None,
    addon_ids: set[str] | None = None,
    backups_root: Path | None = None,
    backup_manifest: Path | None = None,
    existing_records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    manifest = _load_manifest(repo_root, agent)
    selected = destination_ids or _agent_default_destinations(repo_root, agent)
    destinations = _selected_destinations(manifest, selected)
    if any(destination.get("role") == "worker" for destination in destinations):
        _run_mac_worker_path_setup(repo_root, dry_run=dry_run)
    records: list[dict[str, Any]] = []
    for destination in destinations:
        role = destination.get("role")
        surface = destination.get("surface")
        if role == "controller":
            cap = manifest.get("support", {}).get("controller", {}).get("capability")
            suffix = " candidate" if cap == "candidate" else ""
            print(f"CONTROLLER_SURFACE {agent} {surface}{suffix}")
        elif role == "worker":
            if agent == "codex" and destination["id"] == "codex-cli-worker":
                _check_codex_cli_worker_surface(dry_run=dry_run)
            elif agent == "cursor" and destination["id"] == "cursor-cli-worker":
                _check_cursor_cli_worker_surface(dry_run=dry_run)
            elif agent == "opencode" and destination["id"] == "opencode-acp-worker":
                _check_opencode_acp_worker_surface(dry_run=dry_run)
            else:
                commands = destination.get("commands", [])
                detail = commands[0] if commands else destination["id"]
                print(f"WORKER_CHECK {detail}")

    addons = _selected_addons(manifest, selected, addon_ids)
    for addon_id in _skipped_addon_ids(manifest, selected, addon_ids):
        print(f"ADDON_SKIP {agent} {addon_id} reason=incompatible_destinations")
    for addon in addons:
        mode = addon.get("install_mode")
        if goalflight_compat.is_windows() and addon.get("id") == "context-mode":
            print(
                f"ADDON_SKIP {agent} {addon['id']} reason=windows_hooks_unsupported "
                f"detail={goalflight_compat.windows_hooks_skip()}"
            )
            continue
        if addon.get("id") == "gstack":
            records.extend(
                _run_gstack_addon(
                    agent,
                    dry_run=dry_run,
                    backups_root=None if dry_run else backups_root,
                )
            )
            continue
        if mode == "setup" and agent == "codex" and addon.get("id") == "context-mode":
            if not _codex_context_mode_optin():
                print(
                    f"ADDON_SKIP {agent} {addon['id']} reason=codex_context_mode_default_off "
                    "detail=dispatched codex workers disable context-mode at the worker "
                    "boundary (exec-mode ctx_index elicitation wedge), so registering it "
                    "would write a config the runtime always turns off; opt in with "
                    "GOALFLIGHT_CODEX_CONTEXT_MODE=1"
                )
                continue
            _run_codex_context_mode_registration(repo_root, dry_run=dry_run)
        elif mode == "setup" and agent == "codex" and addon.get("id") == "codedb":
            if not _codedb_codex_optin():
                print(
                    f"ADDON_SKIP {agent} {addon['id']} reason=codex_codedb_opt_out "
                    "detail=GOALFLIGHT_CODEX_CODEDB set to a disabling value; skipping "
                    "codedb registration"
                )
                continue
            _run_codex_codedb_registration(repo_root, dry_run=dry_run)
        elif mode == "setup" and agent == "cursor" and addon.get("id") == "context-mode":
            records.extend(
                _run_cursor_context_mode_registration(
                    repo_root,
                    dry_run=dry_run,
                    target_project=target_project,
                    destination_ids=selected,
                    backups_root=None if dry_run else backups_root,
                    backup_manifest=None if dry_run else backup_manifest,
                    agent=agent,
                    existing_records=existing_records,
                )
            )
        elif mode == "setup" and agent == "opencode" and addon.get("id") == "context-mode":
            records.extend(
                _run_opencode_context_mode_registration(
                    repo_root,
                    dry_run=dry_run,
                    target_project=target_project,
                    destination_ids=selected,
                    backups_root=None if dry_run else backups_root,
                    backup_manifest=None if dry_run else backup_manifest,
                    agent=agent,
                    existing_records=existing_records,
                )
            )
        elif mode == "deferred":
            print(f"ADDON_DEFERRED {agent} {addon['id']} reason=plugin_or_hook_api_unverified")
        elif mode == "init_self_check":
            _run_addon_self_check(agent, addon)

    if any(destination.get("requires_restart") for destination in destinations):
        if agent == "codex":
            print("RESTART_REQUIRED codex reload plugin and skill registries")
        elif agent == "cursor":
            print("RESTART_REQUIRED cursor reload instructions and skills")
        elif agent == "opencode":
            print("RESTART_REQUIRED opencode reload instructions and skills")
        else:
            print(f"RESTART_REQUIRED {agent} reload host instructions")
    return records


def _safe_discovery_summary(manifest: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for probe in manifest.get("discovery", {}).get("probes", []):
        if not probe.get("safe_for_setup"):
            continue
        if probe.get("model_consuming") or probe.get("network"):
            continue
        argv = probe.get("argv")
        if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
            continue
        try:
            result = subprocess.run(
                argv,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=5,
                check=False,
                env=_probe_env(probe),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            lines.append(f"PROBE {probe.get('id')} status=not_installed detail={type(exc).__name__}")
            continue
        status = "ok" if result.returncode == 0 else "not_installed"
        first_line = (result.stdout or result.stderr).strip().splitlines()
        detail = first_line[0][:120] if first_line else ""
        suffix = f" detail={detail}" if detail else ""
        lines.append(f"PROBE {probe.get('id')} status={status}{suffix}")
    return lines


def _run_addon_self_check(agent: str, addon: dict[str, Any]) -> None:
    commands = addon.get("commands", [])
    if not commands:
        print(f"ADDON_CHECK {agent} {addon['id']} mode=init_self_check status=skipped detail=no_commands")
        return
    for command in commands:
        try:
            result = subprocess.run(
                ["sh", "-lc", command],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=5,
                check=False,
                env=_probe_env({"env_scrub": True}),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(
                f"ADDON_CHECK {agent} {addon['id']} mode=init_self_check "
                f"status=not_installed command={command!r} detail={type(exc).__name__}"
            )
            continue
        detail_lines = (result.stdout or result.stderr).strip().splitlines()
        detail = detail_lines[0][:120] if detail_lines else ""
        status = "ok" if result.returncode == 0 else "not_installed"
        detail_part = f" detail={detail}" if detail else ""
        print(
            f"ADDON_CHECK {agent} {addon['id']} mode=init_self_check "
            f"status={status} command={command!r}{detail_part}"
        )


def _gstack_host_name(agent: str) -> str:
    return GSTACK_FULL_INSTALL_HOSTS.get(agent, agent)


def _gstack_host_skills_dir(agent: str) -> Path:
    override = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_GSTACK_SKILLS_DIR",
        GSTACK_SKILLS_DIR_OVERRIDE_GATE,
    )
    if override:
        return Path(override).expanduser()
    return {
        "claude-code": Path.home() / ".claude/skills",
        "codex": Path.home() / ".codex/skills",
        "cursor": Path.home() / ".cursor/skills",
        "opencode": Path.home() / ".config/opencode/skills",
        "grok": Path.home() / ".grok/skills",
    }.get(agent, Path.home() / ".agents/skills")


def _gstack_host_skills_dir_source() -> str:
    if os.environ.get("GOALFLIGHT_GSTACK_SKILLS_DIR"):
        if os.environ.get(GSTACK_SKILLS_DIR_OVERRIDE_GATE) == "1":
            return "env:GOALFLIGHT_GSTACK_SKILLS_DIR"
        return "default:ignored_env"
    return "default"


def _gstack_target_skill_name(agent: str, skill: str) -> str:
    if agent == "claude-code":
        return skill
    return f"gstack-{skill}"


def _gstack_external_source_url(skill: str) -> str:
    env_suffix = re.sub(r"[^A-Za-z0-9]+", "_", skill).strip("_").upper()
    env_name = f"GOALFLIGHT_GSTACK_EXTERNAL_SOURCE_{env_suffix}"
    override = os.environ.get(env_name)
    if override:
        if os.environ.get(GSTACK_EXTERNAL_SOURCE_OVERRIDE_GATE) == "1":
            print(
                "ADDON_GSTACK_EXTERNAL "
                f"skill={skill} warning=external_source_override_active "
                f"env={env_name} reason={GSTACK_EXTERNAL_SOURCE_OVERRIDE_GATE}=1"
            )
            return override
        print(
            "ADDON_GSTACK_EXTERNAL "
            f"skill={skill} warning=external_source_override_ignored "
            f"env={env_name} reason={GSTACK_EXTERNAL_SOURCE_OVERRIDE_GATE}_not_1"
        )
    return GSTACK_EXTERNAL_SKILL_SOURCES[skill]


def _gstack_external_source_is_gated_override(skill: str, source_url: str) -> bool:
    env_suffix = re.sub(r"[^A-Za-z0-9]+", "_", skill).strip("_").upper()
    override = os.environ.get(f"GOALFLIGHT_GSTACK_EXTERNAL_SOURCE_{env_suffix}")
    return bool(
        override
        and source_url == override
        and os.environ.get(GSTACK_EXTERNAL_SOURCE_OVERRIDE_GATE) == "1"
    )


def _gstack_existing_skill_path(skills_dir: Path, skill: str) -> Path | None:
    for name in (skill, f"gstack-{skill}"):
        target = skills_dir / name / "SKILL.md"
        if target.is_file():
            return target
    return None


def _gstack_source_candidates() -> list[Path]:
    candidates: list[Path] = []
    raw = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_GSTACK_SOURCE",
        GSTACK_SOURCE_OVERRIDE_GATE,
    )
    if raw:
        candidates.append(Path(raw).expanduser())
    candidates.extend([
        Path.home() / ".claude/skills/gstack",
        Path.home() / ".gstack/repos/gstack",
    ])
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _gstack_source_has_skill(root: Path, skill: str) -> bool:
    return (
        (root / skill / "SKILL.md").is_file()
        or (root / ".agents/skills" / f"gstack-{skill}" / "SKILL.md").is_file()
        or (root / f"gstack-{skill}" / "SKILL.md").is_file()
    )


def _find_gstack_source() -> Path | None:
    for candidate in _gstack_source_candidates():
        if all(_gstack_source_has_skill(candidate, skill) for skill in GSTACK_MINIMAL_SKILLS):
            return candidate
    return None


def _gstack_skill_source(root: Path, agent: str, skill: str) -> Path:
    if agent != "claude-code":
        generated = root / ".agents/skills" / f"gstack-{skill}"
        if (generated / "SKILL.md").is_file():
            return generated
        generated = root / f"gstack-{skill}"
        if (generated / "SKILL.md").is_file():
            return generated
    source = root / skill
    if (source / "SKILL.md").is_file():
        return source
    generated = root / ".agents/skills" / f"gstack-{skill}"
    if (generated / "SKILL.md").is_file():
        return generated
    generated = root / f"gstack-{skill}"
    if (generated / "SKILL.md").is_file():
        return generated
    raise SetupError(f"gstack skill source missing: {skill} under {root}")


def _gstack_installed_skills(agent: str) -> dict[str, bool]:
    skills_dir = _gstack_host_skills_dir(agent)
    state = goalflight_doctor._gstack_root_subset_state(skills_dir)
    return dict(state["skills"])


def _gstack_minimal_installed(agent: str, installed: dict[str, bool] | None = None) -> bool:
    if installed is None:
        installed = _gstack_installed_skills(agent)
    return bool(installed) and all(installed.values())


def _select_gstack_install_choice(*, dry_run: bool) -> str:
    raw = os.environ.get("GOALFLIGHT_GSTACK_INSTALL") or os.environ.get("GOALFLIGHT_GSTACK_INSTALL_CHOICE")
    if raw:
        choice = raw.strip().lower()
        if choice not in GSTACK_INSTALL_CHOICES:
            raise SetupError(f"unknown gstack install choice: {raw}")
        return choice
    if dry_run:
        return "skip"
    if sys.stdin.isatty():
        print("gstack not installed. Install minimal Goal Flight subset plus community skills, full gstack pack, or skip?")
        print("  1. minimal subset (default)")
        print("  2. full pack")
        print("  3. skip")
        answer = input("gstack install choice [minimal/full/skip, empty=minimal]: ").strip().lower()
        if not answer:
            return "minimal"
        if answer in {"1", "minimal", "m"}:
            return "minimal"
        if answer in {"2", "full", "f"}:
            return "full"
        if answer in {"3", "skip", "s", "no", "n"}:
            return "skip"
        raise SetupError(f"unknown gstack install choice: {answer}")
    return "minimal"


def _copy_gstack_tree(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    shutil.copytree(source, target, symlinks=True)


def _record_gstack_target(
    target: Path,
    backups_root: Path,
    *,
    source: str = "gstack",
) -> dict[str, Any]:
    action = {
        "kind": "gstack_skill",
        "source": source,
        "target": str(target),
        "rollback": "restore_backup",
    }
    return _record_backup(action, target, backups_root)


class ExternalSkillDownloadError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _fetch_external_skill_text(url: str, *, allow_file: bool = False) -> str:
    parsed = urllib.parse.urlparse(url)
    allowed_schemes = {"https", "file"} if allow_file else {"https"}
    if parsed.scheme not in allowed_schemes:
        scheme = parsed.scheme or "missing"
        raise ExternalSkillDownloadError(f"unsupported_scheme={scheme}")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "goal-flight-setup/1"})
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=GSTACK_EXTERNAL_DOWNLOAD_TIMEOUT) as response:
            status = response.getcode()
            if status not in (None, 200):
                raise ExternalSkillDownloadError(f"http_status={status}")
            data = response.read(GSTACK_EXTERNAL_DOWNLOAD_MAX_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ExternalSkillDownloadError(type(exc).__name__) from exc
    if len(data) > GSTACK_EXTERNAL_DOWNLOAD_MAX_BYTES:
        raise ExternalSkillDownloadError("size_cap_exceeded")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExternalSkillDownloadError("utf8_decode_failed") from exc


def _skill_doc_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return re.sub(r"\s+", " ", stripped.lstrip("#").strip()) or fallback
    return fallback


def _shape_external_skill(skill: str, source_url: str, text: str) -> str:
    source_line = f"source_url: {json.dumps(source_url)}"
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            frontmatter = text[4:end]
            body = text[end + len("\n---"):]
            lines = [
                line
                for line in frontmatter.rstrip().splitlines()
                if not re.match(r"\s*source_url\s*:", line)
            ]
            lines.append(source_line)
            shaped_frontmatter = "\n".join(lines)
            return f"---\n{shaped_frontmatter}\n---{body}"
    description = _skill_doc_title(text, skill)
    return (
        "---\n"
        f"name: {json.dumps(skill)}\n"
        f"description: {json.dumps(description)}\n"
        f"{source_line}\n"
        "---\n"
        f"{text}"
    )


def _write_external_skill(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.mkdir(parents=True, exist_ok=True)
    _atomic_write(target / "SKILL.md", content)


def _install_gstack_external_skills(
    agent: str,
    *,
    dry_run: bool,
    backups_root: Path | None = None,
) -> list[dict[str, Any]]:
    skills_dir = _gstack_host_skills_dir(agent)
    skills_dir_source = _gstack_host_skills_dir_source()
    records: list[dict[str, Any]] = []
    for skill in GSTACK_EXTERNAL_SKILL_SOURCES:
        existing = _gstack_existing_skill_path(skills_dir, skill)
        if existing is not None:
            print(
                "ADDON_GSTACK_EXTERNAL "
                f"skill={skill} status=ok detail=already_installed "
                f"target={existing.parent} target_source={skills_dir_source}"
            )
            continue
        source_url = _gstack_external_source_url(skill)
        target = skills_dir / _gstack_target_skill_name(agent, skill)
        if dry_run:
            print(
                "ADDON_GSTACK_EXTERNAL "
                f"skill={skill} install=download source={source_url} "
                f"target={target} target_source={skills_dir_source}"
            )
            continue
        if backups_root is None:
            raise SetupError("gstack external skill install requires backup root")
        try:
            text = _fetch_external_skill_text(
                source_url,
                allow_file=_gstack_external_source_is_gated_override(skill, source_url),
            )
            content = _shape_external_skill(skill, source_url, text)
        except ExternalSkillDownloadError as exc:
            print(
                "ADDON_GSTACK_EXTERNAL "
                f"skill={skill} install=blocked reason=network/source detail={exc} source={source_url}"
            )
            continue
        records.append(_record_gstack_target(target, backups_root, source=source_url))
        _write_external_skill(target, content)
        print(
            "ADDON_GSTACK_EXTERNAL_APPLY "
            f"skill={target.name} source={source_url} "
            f"target={target} target_source={skills_dir_source}"
        )
    return records


def _install_gstack_minimal(
    agent: str,
    source: Path,
    *,
    dry_run: bool,
    backups_root: Path | None = None,
) -> list[dict[str, Any]]:
    skills_dir = _gstack_host_skills_dir(agent)
    skills_dir_source = _gstack_host_skills_dir_source()
    targets = [
        skills_dir / _gstack_target_skill_name(agent, skill)
        for skill in GSTACK_MINIMAL_SKILLS
    ]
    license_source = source / "LICENSE"
    license_target = skills_dir / "gstack-LICENSE"
    if dry_run:
        target_names = ",".join(target.name for target in targets)
        print(
            "ADDON_GSTACK install=minimal "
            f"source={source} target={skills_dir} "
            f"target_source={skills_dir_source} skills={target_names}"
        )
        if license_source.is_file():
            print(
                "ADDON_GSTACK attribution "
                f"source={license_source} target={license_target} "
                f"target_source={skills_dir_source}"
            )
        _install_gstack_external_skills(agent, dry_run=True, backups_root=None)
        return []
    if backups_root is None:
        raise SetupError("gstack minimal install requires backup root")
    records: list[dict[str, Any]] = []
    for skill, target in zip(GSTACK_MINIMAL_SKILLS, targets, strict=True):
        source_dir = _gstack_skill_source(source, agent, skill)
        records.append(_record_gstack_target(target, backups_root))
        _copy_gstack_tree(source_dir, target)
        print(
            "ADDON_GSTACK_APPLY "
            f"skill={target.name} source={source_dir} target={target} "
            f"target_source={skills_dir_source}"
        )
    if license_source.is_file():
        records.append(_record_gstack_target(license_target, backups_root))
        _copy_atomic(license_source, license_target)
        print(
            "ADDON_GSTACK_APPLY "
            f"attribution={license_target} target_source={skills_dir_source}"
        )
    records.extend(_install_gstack_external_skills(agent, dry_run=False, backups_root=backups_root))
    return records


def _run_gstack_full_install(
    agent: str,
    source: Path,
    *,
    dry_run: bool,
    backups_root: Path | None = None,
) -> list[dict[str, Any]]:
    if agent not in GSTACK_FULL_INSTALL_HOSTS:
        print(
            "ADDON_GSTACK install=full status=unsupported "
            f"agent={agent} detail=upstream_gstack_setup_host_unsupported fallback=minimal"
        )
        return _install_gstack_minimal(agent, source, dry_run=dry_run, backups_root=backups_root)
    host = _gstack_host_name(agent)
    setup = source / "setup"
    if not setup.is_file():
        raise SetupError(f"gstack setup helper missing: {setup}")
    argv = ["bash", str(setup), "--host", host]
    if dry_run:
        print(f"ADDON_GSTACK install=full command={_format_command(argv)}")
        _install_gstack_external_skills(agent, dry_run=True, backups_root=None)
        return []
    result = subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SetupError(f"gstack full install failed: {detail}")
    print(f"ADDON_GSTACK_APPLY full source={source} host={host}")
    return _install_gstack_external_skills(agent, dry_run=False, backups_root=backups_root)


def _run_gstack_addon(
    agent: str,
    *,
    dry_run: bool,
    backups_root: Path | None = None,
) -> list[dict[str, Any]]:
    installed = _gstack_installed_skills(agent)
    source = _find_gstack_source()
    if _gstack_minimal_installed(agent, installed):
        print(
            "ADDON_CHECK "
            f"{agent} gstack mode=install status=ok detail=minimal_subset "
            f"skills={','.join(GSTACK_MINIMAL_REQUIRED_SKILLS)}"
        )
        return []
    missing = [skill for skill, ok in installed.items() if not ok]
    print(
        "ADDON_CHECK "
        f"{agent} gstack mode=install status=not_installed "
        f"missing={','.join(missing)}"
    )
    print("ADDON_GSTACK choices=minimal,full,skip default=minimal")
    choice = _select_gstack_install_choice(dry_run=dry_run)
    if choice == "skip":
        if dry_run and source is not None:
            _install_gstack_minimal(agent, source, dry_run=True, backups_root=None)
        print("ADDON_GSTACK install=skip")
        return []
    if choice == "minimal":
        if all(installed.get(skill, False) for skill in GSTACK_MINIMAL_SKILLS):
            print("ADDON_GSTACK install=minimal status=ok detail=gstack_subset_present")
            return _install_gstack_external_skills(
                agent,
                dry_run=dry_run,
                backups_root=None if dry_run else backups_root,
            )
        if source is None:
            print("ADDON_GSTACK install=blocked reason=source_missing")
            return _install_gstack_external_skills(
                agent,
                dry_run=dry_run,
                backups_root=None if dry_run else backups_root,
            )
        return _install_gstack_minimal(agent, source, dry_run=dry_run, backups_root=backups_root)
    if source is None:
        print("ADDON_GSTACK install=blocked reason=source_missing")
        return _install_gstack_external_skills(
            agent,
            dry_run=dry_run,
            backups_root=None if dry_run else backups_root,
        )
    return _run_gstack_full_install(agent, source, dry_run=dry_run, backups_root=backups_root)


def _probe_env(probe: dict[str, Any]) -> dict[str, str] | None:
    if not probe.get("env_scrub"):
        return None
    path_items = [
        str(Path.home() / ".local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    return {
        "HOME": str(Path.home()),
        "PATH": os.pathsep.join(path_items),
        "SHELL": os.environ.get("SHELL", "/bin/sh"),
        "GOALFLIGHT_PYTHON": goalflight_compat.python_executable(),
    }


def _run_probe(probe: dict[str, Any]) -> dict[str, Any]:
    argv = probe.get("argv")
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        return {"id": probe.get("id"), "status": "invalid", "detail": "bad argv"}
    if not probe.get("safe_for_setup") or probe.get("network") or probe.get("model_consuming"):
        return {"id": probe.get("id"), "status": "skipped", "detail": "unsafe for setup"}
    try:
        result = subprocess.run(
            argv,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=5,
            check=False,
            env=_probe_env(probe),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"id": probe.get("id"), "status": "not_installed", "detail": type(exc).__name__}
    detail_lines = (result.stdout or result.stderr).strip().splitlines()
    detail = detail_lines[0][:120] if detail_lines else ""
    return {
        "id": probe.get("id"),
        "status": "ok" if result.returncode == 0 else "not_installed",
        "detail": detail,
    }


def _probe_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(result["id"]): result
        for result in (_run_probe(probe) for probe in manifest.get("discovery", {}).get("probes", []))
        if result.get("id")
    }


def _destination_ready(destination: dict[str, Any], probes: dict[str, dict[str, Any]]) -> bool:
    probe_ids = destination.get("probe_ids", [])
    return all(probes.get(probe_id, {}).get("status") == "ok" for probe_id in probe_ids)


def _setup_destinations(manifest: dict[str, Any], role: str) -> list[dict[str, Any]]:
    key = "controller_destinations" if role == "controller" else "worker_destinations"
    return list(manifest.get("setup", {}).get(key, []))


def _all_setup_destinations(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        destination
        for role in ("controller", "worker")
        for destination in _setup_destinations(manifest, role)
    ]


def _selected_destinations(
    manifest: dict[str, Any],
    destination_ids: set[str] | None,
) -> list[dict[str, Any]]:
    destinations = _all_setup_destinations(manifest)
    if not destination_ids:
        return destinations
    return [destination for destination in destinations if destination["id"] in destination_ids]


def _setup_addons(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return list(manifest.get("setup", {}).get("addons", []))


def _all_setup_destination_ids(manifest: dict[str, Any]) -> set[str]:
    return {
        destination["id"]
        for role in ("controller", "worker")
        for destination in _setup_destinations(manifest, role)
    }


def _destination_index(manifests: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for manifest in manifests:
        for role in ("controller", "worker"):
            for destination in _setup_destinations(manifest, role):
                index[destination["id"]] = (manifest, destination)
    return index


def _addon_index(manifests: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for manifest in manifests:
        for addon in _setup_addons(manifest):
            index.setdefault(addon["id"], []).append(addon)
    return index


def _selected_addons(
    manifest: dict[str, Any],
    destination_ids: set[str] | None,
    addon_ids: set[str] | None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    destinations = destination_ids or _all_setup_destination_ids(manifest)
    for addon in _setup_addons(manifest):
        compatible = set(addon.get("compatible_destination_ids", []))
        if compatible and destinations and not compatible.intersection(destinations):
            continue
        if addon_ids is None:
            if addon.get("default"):
                selected.append(addon)
        elif addon.get("id") in addon_ids:
            selected.append(addon)
    return selected


def _skipped_addon_ids(
    manifest: dict[str, Any],
    destination_ids: set[str] | None,
    addon_ids: set[str] | None,
) -> list[str]:
    if addon_ids is None:
        return []
    selected = {addon["id"] for addon in _selected_addons(manifest, destination_ids, addon_ids)}
    return sorted(addon_ids - selected)


def _validate_requested_addons(manifests: list[dict[str, Any]], addon_ids: set[str] | None) -> None:
    if addon_ids is None:
        return
    known = {addon["id"] for manifest in manifests for addon in _setup_addons(manifest)}
    unknown = sorted(addon_ids - known)
    if unknown:
        raise SetupError(f"unknown setup add-on(s): {', '.join(unknown)}")


def _parse_csv(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _parse_optional_csv(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    return _parse_csv(raw)


def _list_agents(repo_root: Path, manifests: list[dict[str, Any]]) -> None:
    print("Goal Flight setup discovery")
    for manifest in manifests:
        probes = _probe_manifest(manifest)
        print(f"AGENT {manifest['agent_id']} name={manifest.get('display_name')}")
        for role in ("controller", "worker"):
            for destination in _setup_destinations(manifest, role):
                ready = _destination_ready(destination, probes)
                default = "default" if destination.get("default") else "optional"
                print(
                    f"  {role} {destination['id']} status={'installed' if ready else 'missing'} "
                    f"{default} surface={destination.get('surface')}"
                )
                for command in destination.get("commands", []):
                    print(f"    command {command}")
                for path_value in destination.get("paths", []):
                    print(f"    path {path_value}")
        for addon in _setup_addons(manifest):
            print(
                f"  addon {addon['id']} default={str(addon.get('default')).lower()} "
                f"mode={addon.get('install_mode')} compatible={','.join(addon.get('compatible_destination_ids', []))}"
            )


def _default_destinations(manifests: list[dict[str, Any]], role: str) -> set[str]:
    selected: set[str] = set()
    for manifest in manifests:
        probes = _probe_manifest(manifest)
        for destination in _setup_destinations(manifest, role):
            if destination.get("default") and _destination_ready(destination, probes):
                selected.add(destination["id"])
    return selected


def _agent_default_destinations(repo_root: Path, agent: str) -> set[str]:
    manifest = _load_manifest(repo_root, agent)
    probes = _probe_manifest(manifest)
    selected: set[str] = set()
    for role in ("controller", "worker"):
        for destination in _setup_destinations(manifest, role):
            if destination.get("default") and _destination_ready(destination, probes):
                selected.add(destination["id"])
    return selected


def _prompt_csv(label: str, choices: list[str], defaults: set[str]) -> set[str]:
    print(f"{label}:")
    for idx, choice in enumerate(choices, start=1):
        marker = "*" if choice in defaults else " "
        print(f"  {idx}. [{marker}] {choice}")
    raw = input(f"{label} numbers or ids, empty=defaults: ").strip()
    if not raw:
        return set(defaults)
    selected: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if item.isdigit() and 1 <= int(item) <= len(choices):
            selected.add(choices[int(item) - 1])
        elif item:
            selected.add(item)
    return selected


def _interactive_selection(manifests: list[dict[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    controller_choices = [dest["id"] for manifest in manifests for dest in _setup_destinations(manifest, "controller")]
    worker_choices = [dest["id"] for manifest in manifests for dest in _setup_destinations(manifest, "worker")]
    controller_defaults = _default_destinations(manifests, "controller")
    worker_defaults = _default_destinations(manifests, "worker")
    controllers = _prompt_csv("Orchestrator destinations", controller_choices, controller_defaults)
    workers = _prompt_csv("Worker destinations", worker_choices, worker_defaults)
    addon_choices = sorted({addon["id"] for manifest in manifests for addon in _setup_addons(manifest)})
    addon_defaults = {addon["id"] for manifest in manifests for addon in _setup_addons(manifest) if addon.get("default")}
    addons = _prompt_csv("Recommended add-ons", addon_choices, addon_defaults)
    return controllers, workers, addons


def _build_selection_plan(
    manifests: list[dict[str, Any]],
    destination_ids: set[str],
    addon_ids: set[str] | None,
) -> list[SetupPlanItem]:
    index = _destination_index(manifests)
    unknown = sorted(destination_ids - set(index))
    if unknown:
        raise SetupError(f"unknown setup destination(s): {', '.join(unknown)}")
    _validate_requested_addons(manifests, addon_ids)

    by_agent: dict[str, set[str]] = {}
    for destination_id in destination_ids:
        manifest, _ = index[destination_id]
        by_agent.setdefault(manifest["agent_id"], set()).add(destination_id)
    return [
        SetupPlanItem(
            agent=manifest["agent_id"],
            manifest=manifest,
            destination_ids=selected,
            addon_ids=addon_ids,
        )
        for manifest in manifests
        if (selected := by_agent.get(manifest["agent_id"]))
    ]


def _ensure_setup_gate(manifest: dict[str, Any]) -> None:
    gate = validate_adapter_gate(manifest, role="controller", argv=[], live_entry="setup_apply")
    reason = str(gate.get("reason"))
    if not gate.get("allowed") and reason not in SETUP_ALLOWED_GATE_REASONS:
        raise SetupError(f"setup gate denied: {gate.get('reason')} fields={gate.get('blocked_fields')}")


def _ensure_setup_actions(
    repo_root: Path,
    manifest: dict[str, Any],
    destination_ids: set[str] | None,
) -> list[dict[str, Any]]:
    actions = _selected_actions(manifest, destination_ids)
    for action in actions:
        if (action.get("writes_repo") or action.get("writes_user_config")) and not action.get("user_gate"):
            raise SetupError(f"refusing ungated write action: {action.get('kind')} {action.get('target')}")
        source = _source_path(repo_root, action)
        if not source.exists():
            raise SetupError(f"setup source missing: {source}")
    return actions


def _run_selection(
    repo_root: Path,
    manifests: list[dict[str, Any]],
    destination_ids: set[str],
    addon_ids: set[str] | None,
    *,
    apply: bool,
    yes: bool,
    target_project: Path,
) -> None:
    plan = _build_selection_plan(manifests, destination_ids, addon_ids)
    if apply:
        for item in plan:
            _ensure_setup_gate(item.manifest)
            _ensure_setup_actions(repo_root, item.manifest, item.destination_ids)
    if addon_ids is not None:
        print(f"ADDONS selected={','.join(sorted(addon_ids))}")
    for item in plan:
        if apply:
            _apply(
                repo_root,
                item.agent,
                item.manifest,
                yes,
                item.destination_ids,
                item.addon_ids,
                target_project=target_project,
                gate_checked=True,
            )
        else:
            _dry_run(
                repo_root,
                item.agent,
                item.manifest,
                item.destination_ids,
                item.addon_ids,
                target_project=target_project,
            )


def _source_path(repo_root: Path, action: dict[str, Any]) -> Path:
    source = Path(os.path.expandvars(os.path.expanduser(action["source"])))
    if not source.is_absolute():
        source = repo_root / source
    return source


def _backup_manifest_path(agent: str) -> Path:
    root = _state_root() / "setup-backups"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{_now_slug()}-{agent}.json"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(content)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.read_bytes())
        tmp_path.replace(target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _merged_content(agent: str, target: Path, source: Path) -> str:
    source_text = source.read_text()
    block = f"{MERGE_START} {agent}\n{source_text.rstrip()}\n{MERGE_END} {agent}\n"
    if not target.exists():
        return block
    existing = target.read_text()
    if f"{MERGE_START} {agent}" in existing:
        return existing
    prefix = existing.rstrip()
    if prefix:
        return f"{prefix}\n\n{block}"
    return block


def _record_backup(action: dict[str, Any], target: Path, backups_root: Path) -> dict[str, Any]:
    existed = target.exists()
    backup: Path | None = None
    if existed:
        backups_root.mkdir(parents=True, exist_ok=True)
        backup = backups_root / f"{len(list(backups_root.iterdir())):04d}-{target.name}.bak"
        if target.is_dir() and not target.is_symlink():
            shutil.copytree(target, backup)
        else:
            shutil.copy2(target, backup)
    return {
        "kind": action["kind"],
        "target": str(target),
        "existed": existed,
        "backup": str(backup) if backup else None,
        "rollback": action["rollback"],
    }


def _apply_action(
    repo_root: Path,
    agent: str,
    action: dict[str, Any],
    backups_root: Path,
    target_project: Path,
) -> dict[str, Any]:
    source = _source_path(repo_root, action)
    target = _expand_target(action["target"], target_project)

    record = _record_backup(action, target, backups_root)
    kind = action["kind"]
    if kind == "register_plugin":
        try:
            _run_codex_plugin_registration(repo_root)
        except Exception:
            with contextlib.suppress(Exception):
                _run_codex_plugin_unregistration()
            raise
    elif kind == "copy":
        _copy_atomic(source, target)
    elif kind in {"copy_or_merge", "merge_config"}:
        _atomic_write(target, _merged_content(agent, target, source))
    elif kind == "link":
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        target.symlink_to(source)
    else:
        raise SetupError(f"unsupported setup action kind: {kind}")
    return record


def _codex_legacy_personal_skill_cleanup_needed(actions: list[dict[str, Any]]) -> bool:
    return any(action["kind"] == "register_plugin" for action in actions)


def _codex_legacy_personal_skill_path() -> Path:
    return Path.home() / ".codex/skills/goal-flight"


def _cleanup_codex_legacy_personal_skill(backups_root: Path) -> dict[str, Any] | None:
    target = _codex_legacy_personal_skill_path()
    if not target.exists():
        return None
    action = {
        "kind": "remove_tree",
        "source": "legacy-codex-personal-skill",
        "target": str(target),
        "rollback": "restore_backup",
    }
    record = _record_backup(action, target, backups_root)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()
    return record


def _write_backup_manifest(path: Path, agent: str, records: list[dict[str, Any]]) -> None:
    data = {
        "schema": BACKUP_SCHEMA,
        "agent": agent,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "actions": records,
    }
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _project_from_state_backup_manifest(path: Path) -> Path:
    root = path.resolve().parent
    for _ in range(len(STATE_BACKUP_REL.parts) + 1):
        root = root.parent
    return root


def _remove_restore_target(target: Path) -> None:
    if not target.exists() and not target.is_symlink():
        return
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()


def _restore_project_state_manifest(path: Path, data: dict[str, Any]) -> None:
    target_project = _project_from_state_backup_manifest(path)
    for entry in reversed(data.get("entries", [])):
        rel = entry.get("path")
        if not isinstance(rel, str):
            continue
        target = target_project / rel
        if entry.get("exists"):
            backup_rel = entry.get("backup")
            if not isinstance(backup_rel, str):
                raise SetupError(f"project-state backup entry missing backup path: {rel}")
            backup = target_project / backup_rel
            if not backup.exists() and not backup.is_symlink():
                raise SetupError(f"backup missing: {backup}")
            _remove_restore_target(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if backup.is_dir() and not backup.is_symlink():
                shutil.copytree(backup, target, symlinks=True)
            else:
                _copy_atomic(backup, target)
        else:
            _remove_restore_target(target)
        print(f"RESTORE {target}")
    preimages = data.get("ledger_task_id_backfill_preimages") or []
    if preimages:
        with goalflight_ledger.StateLock():
            for item in preimages:
                record = item.get("record") if isinstance(item, dict) else None
                if not isinstance(record, dict) or not isinstance(record.get("dispatch_id"), str):
                    continue
                goalflight_ledger.write_record(record)
                print(f"RESTORE ledger {record['dispatch_id']}")


def _restore_from_manifest(path: Path) -> None:
    data = json.loads(path.read_text())
    if data.get("schema") == "goalflight.project-state-backup.v1":
        _restore_project_state_manifest(path, data)
        return
    if data.get("schema") != BACKUP_SCHEMA:
        raise SetupError(f"not a Goal Flight setup backup manifest: {path}")
    for record in reversed(data.get("actions", [])):
        target = Path(record["target"])
        rollback = record.get("rollback")
        if record.get("kind") == "register_plugin":
            _run_codex_plugin_unregistration()
        if rollback == "restore_backup":
            if record.get("existed"):
                backup = Path(record["backup"])
                if not backup.exists():
                    raise SetupError(f"backup missing: {backup}")
                if backup.is_dir():
                    if target.exists() or target.is_symlink():
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    shutil.copytree(backup, target)
                else:
                    _copy_atomic(backup, target)
            elif target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        elif rollback == "delete_link":
            if target.exists() or target.is_symlink():
                target.unlink()
        elif rollback == "unregister_plugin":
            _run_codex_plugin_unregistration()
        else:
            raise SetupError(f"unsupported rollback kind: {rollback}")
        print(f"RESTORE {target}")


def _dry_run(
    repo_root: Path,
    agent: str,
    manifest: dict[str, Any],
    destination_ids: set[str] | None = None,
    addon_ids: set[str] | None = None,
    *,
    target_project: Path,
) -> None:
    print(f"DRY-RUN setup agent={agent}")
    if destination_ids:
        print(f"DESTINATIONS selected={','.join(sorted(destination_ids))}")
    selected_addons = _selected_addons(manifest, destination_ids, addon_ids)
    if addon_ids is not None:
        print(f"ADDONS selected={','.join(sorted(addon_ids))}")
    elif selected_addons:
        print(f"ADDONS default={','.join(sorted(addon['id'] for addon in selected_addons))}")
    for line in _safe_discovery_summary(manifest):
        print(line)
    actions = _selected_actions(manifest, destination_ids)
    for action in actions:
        source = _source_path(repo_root, action)
        target = _expand_target(action["target"], target_project)
        print(
            "ACTION "
            f"{action['kind']} source={source} target={target} "
            f"writes_repo={str(action['writes_repo']).lower()} "
            f"writes_user_config={str(action['writes_user_config']).lower()} "
            f"user_gate={str(action['user_gate']).lower()} rollback={action['rollback']}"
        )
        if action["kind"] == "register_plugin":
            for argv in _codex_plugin_commands(repo_root):
                print(f"CODEX {_format_command(argv)}")
    if agent == "codex" and _codex_legacy_personal_skill_cleanup_needed(actions):
        print(f"CLEANUP remove_tree target={_codex_legacy_personal_skill_path()} reason=desktop_plugin_supersedes_personal_skill")
    _run_host_bootstrap(
        repo_root,
        agent,
        dry_run=True,
        target_project=target_project,
        destination_ids=destination_ids,
        addon_ids=addon_ids,
    )
    plugin_supported = bool(manifest.get("packaging", {}).get("plugin_manifest", {}).get("supported"))
    if plugin_supported and destination_ids and not any(action["kind"] == "register_plugin" for action in actions):
        print("PLUGIN skip selected_destinations")
    else:
        print(_plugin_action(manifest))
    print("NO MUTATION: pass --apply --yes to write approved setup actions")


def _apply(
    repo_root: Path,
    agent: str,
    manifest: dict[str, Any],
    yes: bool,
    destination_ids: set[str] | None = None,
    addon_ids: set[str] | None = None,
    *,
    target_project: Path,
    gate_checked: bool = False,
) -> None:
    if not yes:
        raise SetupError("refusing mutation without --yes")
    if not gate_checked:
        _ensure_setup_gate(manifest)
    actions = _ensure_setup_actions(repo_root, manifest, destination_ids)
    backup_manifest = _backup_manifest_path(agent)
    backups_root = backup_manifest.with_suffix("")
    records: list[dict[str, Any]] = []
    for action in actions:
        records.append(_apply_action(repo_root, agent, action, backups_root, target_project))
        _write_backup_manifest(backup_manifest, agent, records)
    if agent == "codex" and _codex_legacy_personal_skill_cleanup_needed(actions):
        cleanup_record = _cleanup_codex_legacy_personal_skill(backups_root)
        if cleanup_record:
            records.append(cleanup_record)
            _write_backup_manifest(backup_manifest, agent, records)
    bootstrap_records = _run_host_bootstrap(
        repo_root,
        agent,
        dry_run=False,
        target_project=target_project,
        destination_ids=destination_ids,
        addon_ids=addon_ids,
        backups_root=backups_root,
        backup_manifest=backup_manifest,
        existing_records=records,
    )
    if bootstrap_records:
        records.extend(bootstrap_records)
        _write_backup_manifest(backup_manifest, agent, records)
    if not backup_manifest.exists():
        _write_backup_manifest(backup_manifest, agent, records)
    if not records:
        print(f"NO_WRITES setup agent={agent}")
    for record in records:
        print(f"APPLY {record['kind']} {record['target']}")
    print(f"BACKUP_MANIFEST {backup_manifest}")


def _expand_host_install_shortcuts(args: argparse.Namespace) -> None:
    """Map one-shot install flags onto the existing cursor/opencode/codex setup paths."""
    cursor_install = getattr(args, "cursor_install", None)
    if cursor_install is not None:
        args.apply = True
        args.yes = True
        args.cursor = True
        args.cursor_project = cursor_install
    opencode_install = getattr(args, "opencode_install", None)
    if opencode_install is not None:
        args.apply = True
        args.yes = True
        args.opencode = True
        args.opencode_project = opencode_install
    if getattr(args, "codex_install", False):
        args.apply = True
        args.yes = True
        args.agent = args.agent or "codex"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Flight setup registrar")
    parser.add_argument("--agent", help="adapter id, e.g. codex or cursor")
    parser.add_argument(
        "--cursor-install",
        nargs="?",
        const=".",
        metavar="PATH",
        help="one-shot Cursor install (global + project at PATH); implies --apply --yes",
    )
    parser.add_argument(
        "--opencode-install",
        nargs="?",
        const=".",
        metavar="PATH",
        help="one-shot OpenCode install (global + project at PATH); implies --apply --yes",
    )
    parser.add_argument(
        "--codex-install",
        action="store_true",
        help="one-shot Codex plugin + CLI worker install; implies --apply --yes --agent codex",
    )
    parser.add_argument("--cursor", action="store_true", help="shortcut for the default Cursor controller/worker setup")
    parser.add_argument(
        "--cursor-project",
        nargs="?",
        const=".",
        metavar="PATH",
        help="install Cursor project-local wrappers under PATH/.cursor",
    )
    parser.add_argument(
        "--cursor-agents-standard",
        action="store_true",
        help="install the Cursor wrapper under ~/.agents/skills/goal-flight",
    )
    parser.add_argument(
        "--cursor-link-claude",
        action="store_true",
        help="symlink the Cursor skill directory to an existing Claude skill checkout",
    )
    parser.add_argument("--opencode", action="store_true", help="shortcut for the default OpenCode controller/worker setup")
    parser.add_argument(
        "--opencode-project",
        nargs="?",
        const=".",
        metavar="PATH",
        help="install OpenCode project-local wrappers under PATH/.opencode",
    )
    parser.add_argument(
        "--opencode-agents-standard",
        action="store_true",
        help="install the OpenCode wrapper under ~/.agents/skills/goal-flight",
    )
    parser.add_argument(
        "--opencode-link-claude",
        action="store_true",
        help="symlink the OpenCode skill directory to an existing Claude skill checkout",
    )
    parser.add_argument("--list-agents", action="store_true", help="show installed controller/worker destinations")
    parser.add_argument("--tui", action="store_true", help="prompt for orchestrator, worker, and add-on destinations")
    parser.add_argument("--controllers", help="comma-separated orchestrator destination ids")
    parser.add_argument("--workers", help="comma-separated worker destination ids")
    parser.add_argument("--addons", help="comma-separated add-on ids")
    parser.add_argument(
        "--gstack-install",
        choices=sorted(GSTACK_INSTALL_CHOICES),
        help="choice when the gstack add-on is selected but host skills are missing",
    )
    parser.add_argument("--target-project", default=".", help="target project for project-local install destinations")
    parser.add_argument(
        "--scaffold-project-state",
        action="store_true",
        help="scaffold target docs-private/ from templates/state-skeleton without overwriting existing files",
    )
    parser.add_argument("--scaffold-date", help=argparse.SUPPRESS)
    parser.add_argument("--apply", action="store_true", help="perform approved writes")
    parser.add_argument("--yes", action="store_true", help="confirm writes for --apply")
    parser.add_argument("--uninstall", action="store_true", help="rollback using --from-manifest")
    parser.add_argument("--from-manifest", help="backup manifest created by --apply")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.gstack_install:
        os.environ["GOALFLIGHT_GSTACK_INSTALL"] = args.gstack_install
    _expand_host_install_shortcuts(args)

    try:
        repo_root = Path(args.repo_root).resolve()
        target_project = Path(
            args.cursor_project or args.opencode_project or args.target_project
        ).expanduser().resolve()
        manifests = _load_manifests(repo_root)
        if args.scaffold_project_state:
            if args.apply and not args.yes:
                raise SetupError("--scaffold-project-state --apply requires --yes")
            result = scaffold_project_state(
                repo_root,
                target_project,
                apply=args.apply,
                today=args.scaffold_date,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.uninstall:
            if not args.from_manifest:
                raise SetupError("--uninstall requires --from-manifest")
            _restore_from_manifest(Path(args.from_manifest).expanduser())
            return 0
        if args.list_agents:
            _list_agents(repo_root, manifests)
            return 0
        host_install = (
            args.cursor
            or args.cursor_project
            or args.opencode
            or args.opencode_project
            or getattr(args, "cursor_install", None) is not None
            or getattr(args, "opencode_install", None) is not None
            or getattr(args, "codex_install", False)
        )
        if args.tui or (
            not args.agent
            and not args.controllers
            and not args.workers
            and not host_install
            and sys.stdin.isatty()
        ):
            controllers, workers, addons = _interactive_selection(manifests)
            _run_selection(
                repo_root,
                manifests,
                controllers | workers,
                addons,
                apply=args.apply,
                yes=args.yes,
                target_project=target_project,
            )
            return 0
        if args.cursor or args.cursor_project or args.cursor_agents_standard or args.cursor_link_claude:
            destinations = set()
            if args.cursor:
                destinations.add("cursor-desktop-controller")
                defaults = _agent_default_destinations(repo_root, "cursor")
                if "cursor-cli-worker" in defaults:
                    destinations.add("cursor-cli-worker")
            if args.cursor_project:
                destinations.add("cursor-project-controller")
            if args.cursor_agents_standard:
                destinations.add("cursor-agents-standard-controller")
            if args.cursor_link_claude:
                destinations.add("cursor-claude-link-controller")
            if not destinations:
                destinations.update(_agent_default_destinations(repo_root, "cursor"))
            addons = _parse_optional_csv(args.addons)
            _validate_requested_addons(manifests, addons)
            _run_selection(
                repo_root,
                manifests,
                destinations,
                addons,
                apply=args.apply,
                yes=args.yes,
                target_project=target_project,
            )
            return 0
        if args.opencode or args.opencode_project or args.opencode_agents_standard or args.opencode_link_claude:
            destinations = set()
            if args.opencode:
                destinations.add("opencode-global-controller")
                defaults = _agent_default_destinations(repo_root, "opencode")
                if "opencode-acp-worker" in defaults:
                    destinations.add("opencode-acp-worker")
            if args.opencode_project:
                destinations.add("opencode-project-controller")
            if args.opencode_agents_standard:
                destinations.add("opencode-agents-standard-controller")
            if args.opencode_link_claude:
                destinations.add("opencode-claude-link-controller")
            if not destinations:
                destinations.update(_agent_default_destinations(repo_root, "opencode"))
            addons = _parse_optional_csv(args.addons)
            _validate_requested_addons(manifests, addons)
            _run_selection(
                repo_root,
                manifests,
                destinations,
                addons,
                apply=args.apply,
                yes=args.yes,
                target_project=target_project,
            )
            return 0
        if args.controllers or args.workers:
            controllers = _parse_csv(args.controllers)
            workers = _parse_csv(args.workers)
            addons = _parse_optional_csv(args.addons)
            _run_selection(
                repo_root,
                manifests,
                controllers | workers,
                addons,
                apply=args.apply,
                yes=args.yes,
                target_project=target_project,
            )
            return 0
        if not args.agent:
            _list_agents(repo_root, manifests)
            print("NO MUTATION: pass --tui, --controllers/--workers, or --agent")
            return 0
        if not args.agent:
            raise SetupError("--agent is required unless --uninstall is used")
        manifest = _load_manifest(repo_root, args.agent)
        destinations = _agent_default_destinations(repo_root, args.agent)
        if not destinations and _setup_destinations(manifest, "controller") + _setup_destinations(manifest, "worker"):
            raise SetupError(f"no ready default setup destinations for agent: {args.agent}")
        addons = _parse_optional_csv(args.addons)
        _validate_requested_addons(manifests, addons)
        if args.apply:
            _apply(
                repo_root,
                args.agent,
                manifest,
                args.yes,
                destinations,
                addons,
                target_project=target_project,
            )
        else:
            _dry_run(repo_root, args.agent, manifest, destinations, addons, target_project=target_project)
        return 0
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
