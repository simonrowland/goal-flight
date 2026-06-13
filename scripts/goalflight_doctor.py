#!/usr/bin/env python3
"""Procedural doctor for goal-flight.

The model should read this script's compact JSON or checklist instead of
hand-running a long environment probe sequence into the context window.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import goalflight_compat

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import goalflight_capacity
except Exception:  # pragma: no cover - doctor still reports partial state
    goalflight_capacity = None

try:
    import goalflight_rate_pressure
except Exception:  # pragma: no cover - doctor still reports partial state
    goalflight_rate_pressure = None

try:
    import goalflight_fleet
except Exception:  # pragma: no cover - doctor still reports partial state
    goalflight_fleet = None

try:
    import goalflight_os_sandbox
except Exception:  # pragma: no cover - doctor still reports partial state
    goalflight_os_sandbox = None

try:
    import goalflight_agent_traits
except Exception:  # pragma: no cover - doctor still reports partial state
    goalflight_agent_traits = None

CLAUDE_ACP_STOPGAP_MAX_VERSION = "0.1.1"
CLAUDE_ACP_PINNED_FIX_COMMIT = "14a5b0c"
CLAUDE_ACP_BUILD_SCRIPT = SCRIPT_DIR / "install_claude_acp_patch.sh"
GSTACK_MINIMAL_SKILLS = ("review", "plan-eng-review", "office-hours")
GSTACK_EXTERNAL_SKILLS = ("grill-me", "thermo-nuclear-code-quality-review")
GSTACK_MINIMAL_REQUIRED_SKILLS = GSTACK_MINIMAL_SKILLS + GSTACK_EXTERNAL_SKILLS
AUTOREVIEW_HELPER_OVERRIDE_GATE = "GOALFLIGHT_ALLOW_AUTOREVIEW_HELPER"
CLAUDE_ACP_BIN_OVERRIDE_GATE = "GOALFLIGHT_ALLOW_CLAUDE_ACP_BIN_OVERRIDE"
CLAUDE_ACP_VERSION_OVERRIDE_GATE = "GOALFLIGHT_ALLOW_CLAUDE_ACP_VERSION_OVERRIDE"


def run(cmd: list[str], cwd: Path | None = None, timeout: float = 8.0) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip()[:2000],
            "stderr": proc.stderr.strip()[:2000],
            "ok": proc.returncode == 0,
        }
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"cmd": cmd, "returncode": None, "stdout": "", "stderr": str(e), "ok": False}


def first_line(text: str | None) -> str | None:
    if not text:
        return None
    return text.splitlines()[0] if text.splitlines() else None


KEYCHAIN_LOCKED_RE = re.compile(r"keychain.*locked", re.I)


def version(binary: str, *args: str) -> dict:
    path = shutil.which(binary)
    if not path and binary == "cursor-agent":
        fallback = Path.home() / ".local/bin/cursor-agent"
        if fallback.exists():
            path = str(fallback)
    if not path and binary == "grok":
        fallback = Path.home() / ".grok/bin/grok"
        if fallback.exists():
            path = str(fallback)
    if not path and binary == "claude":
        fallback = Path.home() / ".local/bin/claude"
        if fallback.exists():
            path = str(fallback)
    if not path:
        return {"present": False}
    result = run([path, *args], timeout=4)
    combined = f"{result['stdout'] or ''}\n{result['stderr'] or ''}"
    version_text = first_line(result["stdout"] or result["stderr"])
    ok = result["ok"]
    out: dict = {"present": True, "path": path, "version": version_text, "ok": ok}
    if not ok and binary == "cursor-agent" and KEYCHAIN_LOCKED_RE.search(combined):
        # BatchMode SSH often hits a locked login keychain; binary is still usable for dispatch.
        out["ok"] = True
        out["keychain_locked"] = True
        if not version_text or KEYCHAIN_LOCKED_RE.search(version_text):
            out["version"] = "present (login keychain locked over SSH)"
    return out


def check_agent_traits() -> dict:
    """Non-fatal probe for opt-in global agent-behavior traits (claude host)."""
    if goalflight_agent_traits is None:
        return {
            "available": False,
            "reason": "goalflight_agent_traits import failed",
            "ok": True,
            "level": "info",
        }
    target = goalflight_agent_traits.default_target("claude")
    if target is None:
        return {
            "available": False,
            "reason": "no default claude target",
            "ok": True,
            "level": "info",
        }
    st = goalflight_agent_traits.status(target)
    installed = goalflight_agent_traits.installed_version(target)
    ok: bool | None
    if st == "absent":
        ok = None
    elif st == "current":
        ok = True
    else:
        ok = False
    out: dict = {
        "available": True,
        "target": str(target),
        "status": st,
        "installed_version": installed,
        "repo_version": goalflight_agent_traits.TRAITS_VERSION,
        "ok": ok,
        "level": "info" if st == "absent" else ("ok" if st == "current" else "warning"),
    }
    if st == "absent":
        out["detail"] = "opt-in available: install.sh --with-agent-traits"
    elif st == "stale":
        out["detail"] = (
            f"agent-traits block is v{installed}, repo is v{goalflight_agent_traits.TRAITS_VERSION}; "
            "re-run install.sh --with-agent-traits to update"
        )
    else:
        out["detail"] = "agent traits block current"
    return out


def _gstack_host_skill_roots() -> dict[str, Path]:
    return {
        "claude-code": Path.home() / ".claude/skills",
        "codex": Path.home() / ".codex/skills",
        "cursor": Path.home() / ".cursor/skills",
        "opencode": Path.home() / ".config/opencode/skills",
        "grok": Path.home() / ".grok/skills",
    }


def _gstack_root_subset_state(root: Path) -> dict:
    skills: dict[str, bool] = {}
    installed_as: dict[str, str | None] = {}
    for skill in GSTACK_MINIMAL_REQUIRED_SKILLS:
        flat = root / skill / "SKILL.md"
        prefixed = root / f"gstack-{skill}" / "SKILL.md"
        if flat.is_file():
            skills[skill] = True
            installed_as[skill] = skill
        elif prefixed.is_file():
            skills[skill] = True
            installed_as[skill] = f"gstack-{skill}"
        else:
            skills[skill] = False
            installed_as[skill] = None
    missing = [skill for skill, ok in skills.items() if not ok]
    return {
        "path": str(root),
        "skills": skills,
        "installed_as": installed_as,
        "complete": not missing,
        "missing": missing,
    }


def _gstack_subset_detail(host_roots: dict[str, dict], complete_hosts: list[str]) -> str:
    if complete_hosts:
        return "minimal subset hosts: " + ", ".join(complete_hosts)
    partial: list[str] = []
    for host, state in host_roots.items():
        present = [
            skill
            for skill, ok in (state.get("skills") or {}).items()
            if ok
        ]
        if present:
            partial.append(f"{host} has {','.join(present)}")
    if partial:
        return "partial minimal subset: " + "; ".join(partial)
    return "minimal subset not exposed in host skill roots; missing " + ",".join(GSTACK_MINIMAL_REQUIRED_SKILLS)


def check_gstack() -> dict:
    cli = version("gstack", "--version")
    host_roots = {
        host: _gstack_root_subset_state(root)
        for host, root in _gstack_host_skill_roots().items()
    }
    complete_hosts = [host for host, state in host_roots.items() if state["complete"]]
    if cli.get("present"):
        cli.update({
            "kind": "cli",
            "ok": True,
            "level": "ok",
            "minimal_required_skills": list(GSTACK_MINIMAL_REQUIRED_SKILLS),
            "host_skill_roots": host_roots,
            "minimal_subset_hosts": complete_hosts,
            "detail": _gstack_subset_detail(host_roots, complete_hosts),
        })
        return cli

    repo = Path.home() / ".gstack/repos/gstack"
    package_json = repo / "package.json"
    skills_dir = repo / ".agents/skills"
    if package_json.exists() and skills_dir.exists():
        try:
            package = json.loads(package_json.read_text())
        except Exception:
            package = {}
        version_text = package.get("version")
        return {
            "present": True,
            "path": str(repo),
            "kind": "skill_repo",
            "version": f"gstack {version_text}" if version_text else "gstack skill repo",
            "ok": True,
            "level": "ok",
            "minimal_required_skills": list(GSTACK_MINIMAL_REQUIRED_SKILLS),
            "host_skill_roots": host_roots,
            "minimal_subset_hosts": complete_hosts,
            "detail": _gstack_subset_detail(host_roots, complete_hosts),
        }

    if complete_hosts:
        return {
            "present": True,
            "kind": "minimal_subset",
            "version": "gstack minimal subset",
            "ok": True,
            "level": "warning",
            "minimal_required_skills": list(GSTACK_MINIMAL_REQUIRED_SKILLS),
            "host_skill_roots": host_roots,
            "minimal_subset_hosts": complete_hosts,
            "detail": f"minimal subset only: {', '.join(complete_hosts)}",
        }

    return {
        "present": False,
        "ok": False,
        "level": "warning",
        "minimal_required_skills": list(GSTACK_MINIMAL_REQUIRED_SKILLS),
        "host_skill_roots": host_roots,
        "minimal_subset_hosts": [],
        "detail": _gstack_subset_detail(host_roots, []),
    }


def check_autoreview(skill_root: Path) -> dict:
    script_path = skill_root / "scripts/autoreview.sh"
    helper_env = goalflight_compat.allowed_env_override(
        "AUTOREVIEW_HELPER",
        AUTOREVIEW_HELPER_OVERRIDE_GATE,
    )
    helper = (
        Path(helper_env).expanduser()
        if helper_env
        else skill_root / "autoreview/scripts/autoreview"
    )
    helper_source = "env" if helper_env else "vendored"
    script_ok = script_path.is_file() and os.access(script_path, os.X_OK)
    helper_ok = helper.is_file() and os.access(helper, os.X_OK)
    out = {
        "present": script_ok,
        "ok": script_ok and helper_ok,
        "script_path": str(script_path),
        "upstream_helper": str(helper) if helper_ok else None,
        "helper_source": helper_source,
        "claude_acp": str(skill_root / "scripts/autoreview_claude_acp"),
    }
    if script_ok and helper_ok:
        out["version"] = "goal-flight wrapper (vendored at autoreview/)"
    else:
        missing = []
        if not script_ok:
            missing.append("wrapper")
        if not helper_ok:
            missing.append("helper")
        out["install_hint"] = (
            "Vendored autoreview at autoreview/scripts/autoreview "
            f"(or AUTOREVIEW_HELPER); ensure scripts/autoreview.sh is executable; "
            f"missing: {', '.join(missing)}"
        )
    return out


def check_agents_md_state(project_root: Path) -> dict:
    """Probe the AGENTS.md tracking state for a project.

    Returns:
      present:           AGENTS.md exists on disk
      tracked:           git knows about it (informational — many downstream
                         projects intentionally keep AGENTS.md per-operator
                         and gitignored, not shared. The local file is still
                         the canonical skill entry point for this operator;
                         doctor does not flag the gitignored shape as an
                         error.)
      gitignored:        a gitignore rule matches it (informational only)
      has_goalflight_section:  the "Goal Flight Routing" header is present
                         (means goal-flight has been wired into this AGENTS.md)
    """
    agents = project_root / "AGENTS.md"
    out: dict = {
        "present": agents.is_file(),
        "tracked": False,
        "gitignored": False,
        "has_goalflight_section": False,
    }
    if not (project_root / ".git").exists():
        # Non-git project: presence alone isn't enough — empty / frontmatter-
        # only AGENTS.md should be ok=false. Treat as ok only when the file
        # has substantive content (>500 bytes is a generous floor).
        if out["present"]:
            try:
                size = (project_root / "AGENTS.md").stat().st_size
            except OSError:
                size = 0
            out["ok"] = size >= 500
        else:
            out["ok"] = False
        return out
    if out["present"]:
        try:
            out["has_goalflight_section"] = "## Goal Flight Routing" in agents.read_text(
                encoding="utf-8", errors="ignore"
            )
        except OSError:
            pass
        tracked = run(["git", "ls-files", "--error-unmatch", "AGENTS.md"], cwd=project_root)
        out["tracked"] = tracked.get("returncode") == 0
        ignored = run(["git", "check-ignore", "-q", "AGENTS.md"], cwd=project_root)
        out["gitignored"] = ignored.get("returncode") == 0
    # ok=true when AGENTS.md exists locally AND has the goal-flight section.
    # Tracking + gitignored state are informational only — many downstream
    # projects intentionally keep AGENTS.md per-operator-private.
    out["ok"] = bool(out["present"] and out["has_goalflight_section"])
    if not out["present"]:
        out["install_hint"] = (
            "AGENTS.md missing. Run `/goal-flight init` to write it from "
            "`templates/project-agents.md`. Init respects existing gitignore "
            "rules — if AGENTS.md is in your .gitignore, the file is created "
            "locally and left untracked (per-operator pattern)."
        )
    elif not out["has_goalflight_section"]:
        out["install_hint"] = (
            "AGENTS.md present but missing the `## Goal Flight Routing` "
            "section + activation directive. Run `/goal-flight init` to "
            "append the section. The file's tracked / gitignored state is "
            "preserved."
        )
    return out


def check_session_status(skill_root: Path, project_root: Path) -> dict:
    """Run the session-status helper and surface its verdict in the doctor
    payload. The helper unions queue/leases/RESUME-NOTES signals into a
    single `active` bool; doctor exposes that as the canonical "is
    goal-flight active here?" answer.
    """
    helper = skill_root / "scripts/goalflight_session_status.py"
    if not helper.exists():
        return {
            "ok": False,
            "present": False,
            "install_hint": (
                f"{helper} missing — reinstall goal-flight or check that "
                "the skill-root environment variable points at the right tree."
            ),
        }
    out = run(
        [sys.executable, str(helper), "--project-root", str(project_root), "--json"],
        timeout=10.0,
    )
    if out.get("returncode") != 0:
        return {
            "ok": False,
            "present": True,
            "error": out.get("stderr") or out.get("stdout"),
            "install_hint": (
                "session_status helper returned nonzero; reproduce with "
                f"`{sys.executable} {helper} --project-root {project_root} --text` "
                "and check the stderr."
            ),
        }
    try:
        payload = json.loads(out.get("stdout") or "{}")
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "present": True,
            "error": f"json decode: {exc}",
            "install_hint": (
                "session_status returned non-JSON stdout — the helper has "
                "a bug or the Python launcher is broken. Reproduce with "
                f"`{sys.executable} {helper} --project-root {project_root} --json`."
            ),
        }
    return {
        "ok": True,
        "present": True,
        "active": payload.get("active", False),
        "queue_file": payload.get("queue_file"),
        "queue_state": payload.get("queue_state"),
        "queue_reason": payload.get("queue_reason"),
        "active_leases_in_project": payload.get("active_leases_in_project", 0),
        "newest_resume_notes": payload.get("newest_resume_notes"),
        "resume_notes_active": payload.get("resume_notes_active"),
    }


def check_resume_notes_pattern(project_root: Path) -> dict:
    """Probe `docs-private/RESUME-NOTES-*.md` for the canonical filename
    pattern: RESUME-NOTES-<YYYY-MM-DD>[-rev<N>].md. Surfaces topic-prefixed
    variants (e.g. RESUME-NOTES-generalize-2026-05-20.md) as WARN — those
    break the find-newest-by-lexicographic-sort convention.

    `newest` filters to canonical files only — so the find-newest workflow
    described in protocols/state-handoff.md picks the right file even when
    historical topic-prefixed variants remain.
    """
    private = project_root / "docs-private"
    if not private.is_dir():
        return {"ok": True, "present": False, "count": 0, "pattern_violations": []}
    import re

    canonical = re.compile(r"^RESUME-NOTES-\d{4}-\d{2}-\d{2}(-rev\d+)?\.md$")
    files = sorted(private.glob("RESUME-NOTES-*.md"))
    canonical_files = [f for f in files if canonical.match(f.name)]
    violations: list[str] = [f.name for f in files if f not in canonical_files]
    return {
        "ok": not violations,
        "present": bool(files),
        "count": len(files),
        # Sweep D P2 fix: filter to canonical-only for newest. Lex-sort of
        # canonical-only files = chronological by construction.
        "newest": str(canonical_files[-1].relative_to(project_root)) if canonical_files else None,
        "newest_any": str(files[-1].relative_to(project_root)) if files else None,
        "pattern_violations": violations,
        "install_hint": (
            f"RESUME-NOTES files with non-canonical names: {violations}. "
            "Canonical: RESUME-NOTES-<YYYY-MM-DD>[-rev<N>].md (no topic prefix; "
            "topic goes inside the file). Historical exceptions may be left in "
            "place; new files should follow the canonical form. See "
            "protocols/state-handoff.md."
        ) if violations else None,
    }


def app_exists(name: str, bundle_id: str | None = None) -> bool | None:
    if not goalflight_compat.is_macos():
        if goalflight_compat.is_linux():
            desktop_names = {
                f"{name}.desktop",
                f"{name.lower()}.desktop",
                f"{name.replace(' ', '-').lower()}.desktop",
                f"{name.replace(' ', '').lower()}.desktop",
            }
            app_dirs = [
                Path.home() / ".local/share/applications",
                Path("/usr/local/share/applications"),
                Path("/usr/share/applications"),
                Path("/var/lib/snapd/desktop/applications"),
            ]
            for app_dir in app_dirs:
                try:
                    if any((app_dir / item).exists() for item in desktop_names):
                        return True
                except OSError:
                    continue
        return None
    direct = Path("/Applications") / f"{name}.app"
    if direct.exists():
        return True
    if bundle_id and shutil.which("mdfind"):
        result = run(["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"], timeout=3)
        return bool(result["stdout"])
    return False


def check_platform() -> dict:
    sandbox_available = (
        goalflight_os_sandbox.os_sandbox_available()
        if goalflight_os_sandbox is not None
        else False
    )
    sandbox_profiles = (
        goalflight_os_sandbox.platform_supported_os_sandbox_profiles()
        if goalflight_os_sandbox is not None
        else ["off"]
    )
    sandbox_platform = (
        goalflight_os_sandbox.os_sandbox_platform_key()
        if goalflight_os_sandbox is not None
        else "unknown"
    )
    return {
        "os_name": os.name,
        "sys_platform": sys.platform,
        "is_windows": goalflight_compat.is_windows(),
        "is_macos": goalflight_compat.is_macos(),
        "is_linux": goalflight_compat.is_linux(),
        "is_wsl": goalflight_compat.is_wsl(),
        "os_sandbox_available": sandbox_available,
        "os_sandbox_platform": sandbox_platform,
        "os_sandbox_supported_profiles": sandbox_profiles,
    }


def check_plugin(repo: Path) -> dict:
    manifest = repo / ".claude-plugin/plugin.json"
    package_repo = (repo / "plugins/goal-flight/.codex-plugin/plugin.json").exists() and (repo / "VERSION").exists()
    out = {
        "manifest": str(manifest),
        "manifest_exists": manifest.exists() if package_repo else None,
        "package_repo": package_repo,
        "skipped": not package_repo,
    }
    if not package_repo:
        out["skip_reason"] = "target_project_not_goal_flight_package"
        out["validate_ok"] = None
        return out
    if shutil.which("claude") and manifest.exists():
        result = run(["claude", "plugin", "validate", str(repo)], cwd=repo, timeout=20)
        out.update(
            {
                "validate_ok": result["ok"],
                "validate_first_line": first_line(result["stdout"] or result["stderr"]),
                "validate_excerpt": "\n".join((result["stdout"] + "\n" + result["stderr"]).splitlines()[:12]),
            }
        )
    else:
        out["validate_ok"] = None
    return out


def _path_state(path: Path) -> dict:
    return {"path": str(path), "exists": path.exists()}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_host_goalflight_install() -> dict:
    home = Path.home()
    codex_plugin_cache = sorted(
        str(path)
        for path in (home / ".codex/plugins/cache/goal-flight/goal-flight").glob("*/.codex-plugin/plugin.json")
    )
    codex_personal = home / ".codex/skills/goal-flight/SKILL.md"
    cursor_agents = home / ".cursor/AGENTS.md"
    cursor_skill = home / ".cursor/skills/goal-flight/SKILL.md"
    cursor_rules = home / ".cursor/rules/goal-flight.mdc"
    opencode_agents = home / ".config/opencode/AGENTS.md"
    opencode_skill = home / ".config/opencode/skills/goal-flight/SKILL.md"
    opencode_config = home / ".config/opencode/opencode.json"
    standard_agents_skill = home / ".agents/skills/goal-flight/SKILL.md"
    grok_skill = home / ".grok/skills/goal-flight/SKILL.md"
    claude_skill = home / ".claude/skills/goal-flight/SKILL.md"
    payload = {
        "codex": {
            "ok": bool(codex_plugin_cache) or codex_personal.exists(),
            "plugin_cache": codex_plugin_cache,
            "personal_skill": _path_state(codex_personal),
        },
        "cursor": {
            "ok": cursor_skill.exists() or standard_agents_skill.exists(),
            "global_agents": _path_state(cursor_agents),
            "skill": _path_state(cursor_skill),
            "standard_agents_skill": _path_state(standard_agents_skill),
            "rules": _path_state(cursor_rules),
        },
        "opencode": {
            "ok": opencode_skill.exists() or standard_agents_skill.exists(),
            "global_agents": _path_state(opencode_agents),
            "skill": _path_state(opencode_skill),
            "config": _path_state(opencode_config),
            "standard_agents_skill": _path_state(standard_agents_skill),
        },
        "grok": {
            "ok": grok_skill.exists(),
            "skill": _path_state(grok_skill),
        },
        "claude": {
            "ok": claude_skill.exists(),
            "skill": _path_state(claude_skill),
        },
    }
    for host, item in payload.items():
        if host == "codex":
            detail = "plugin_cache" if item["plugin_cache"] else item["personal_skill"]["path"]
        elif host == "cursor":
            detail = (
                f"{item['skill']['path']} standard={item['standard_agents_skill']['exists']} "
                f"agents={item['global_agents']['exists']} rules={item['rules']['exists']}"
            )
        elif host == "opencode":
            detail = (
                f"{item['skill']['path']} standard={item['standard_agents_skill']['exists']} "
                f"agents={item['global_agents']['exists']} config={item['config']['exists']}"
            )
        else:
            detail = item["skill"]["path"]
        item["detail"] = detail
    return payload


def _has_symlink_component(path: Path) -> bool:
    for candidate in (path, path.parent):
        try:
            if candidate.is_symlink():
                return True
        except OSError:
            return False
    return False


def _installed_skill_entry(
    *,
    host: str,
    sources: list[Path],
    installed: Path,
    source_root: Path,
    root_hash: str | None,
    resync_command: str,
) -> dict | None:
    if not installed.is_file():
        return None

    symlinked = _has_symlink_component(installed)
    installed_hash = _sha256_file(installed)
    source_hashes = [
        {"path": str(source), "hash": _sha256_file(source)}
        for source in sources
        if source.is_file()
    ]
    if symlinked and source_root.is_file() and root_hash:
        source_hashes = [{"path": str(source_root), "hash": root_hash}]

    primary = next((item for item in source_hashes if item["hash"] == installed_hash), None)
    if primary is None and source_hashes:
        primary = source_hashes[0]
    drift = bool(source_hashes and installed_hash not in {item["hash"] for item in source_hashes})
    return {
        "host": host,
        "path": str(installed),
        "source": primary["path"] if primary else None,
        "source_root_hash": root_hash,
        "source_hash": primary["hash"] if primary else None,
        "source_alternatives": source_hashes,
        "installed_hash": installed_hash,
        "drift": drift,
        "install_mode": "symlink" if symlinked else "copy",
        "resync_command": resync_command,
    }


def check_installed_skill_drift(skill_root: Path, project_root: Path) -> dict:
    home = Path.home()
    source_root = skill_root / "SKILL.md"
    root_hash = _sha256_file(source_root) if source_root.is_file() else None
    codex_plugin_cache = sorted(
        (home / ".codex/plugins/cache/goal-flight/goal-flight").glob("*/skills/goal-flight/SKILL.md")
    )
    codex_source = skill_root / "plugins/goal-flight/skills/goal-flight/SKILL.md"
    cursor_source = skill_root / "configs/cursor/skills/goal-flight/SKILL.md"
    opencode_source = skill_root / "configs/opencode/skills/goal-flight/SKILL.md"
    grok_source = skill_root / "configs/grok/skills/goal-flight/SKILL.md"
    candidates: list[dict] = [
        {
            "host": "codex",
            "sources": [codex_source],
            "installed": home / ".codex/skills/goal-flight/SKILL.md",
            "resync_command": "./install.sh codex",
        },
        {
            "host": "cursor",
            "sources": [cursor_source],
            "installed": home / ".cursor/skills/goal-flight/SKILL.md",
            "resync_command": "./install.sh cursor <project>",
        },
        {
            "host": "cursor",
            "sources": [cursor_source],
            "installed": project_root / ".cursor/skills/goal-flight/SKILL.md",
            "resync_command": "./install.sh cursor <project>",
        },
        {
            "host": "opencode",
            "sources": [opencode_source],
            "installed": home / ".config/opencode/skills/goal-flight/SKILL.md",
            "resync_command": "./install.sh opencode <project>",
        },
        {
            "host": "opencode",
            "sources": [opencode_source],
            "installed": project_root / ".opencode/skills/goal-flight/SKILL.md",
            "resync_command": "./install.sh opencode <project>",
        },
        {
            "host": "cursor",
            "sources": [cursor_source, opencode_source],
            "installed": home / ".agents/skills/goal-flight/SKILL.md",
            "resync_command": "./install.sh cursor <project> or ./install.sh opencode <project>",
        },
        {
            "host": "grok",
            "sources": [grok_source],
            "installed": home / ".grok/skills/goal-flight/SKILL.md",
            # `--agent grok` selects only the worker (grok-acp-worker); the
            # SKILL.md install lives under the orchestrator surface setup.
            # Resync needs the full setup path that copies SKILL.md sources.
            "resync_command": (
                "./setup.sh --apply --yes --agent grok && "
                "./install.sh grok <project>"
            ),
        },
        {
            "host": "claude-code",
            "sources": [source_root],
            "installed": home / ".claude/skills/goal-flight/SKILL.md",
            "resync_command": "git -C $HOME/.claude/skills/goal-flight pull --ff-only",
        },
    ]
    candidates.extend(
        {
            "host": "codex",
            "sources": [codex_source],
            "installed": path,
            "resync_command": "./install.sh codex",
        }
        for path in codex_plugin_cache
    )

    entries: list[dict] = []
    for candidate in candidates:
        entry = _installed_skill_entry(
            host=candidate["host"],
            sources=candidate["sources"],
            installed=candidate["installed"],
            source_root=source_root,
            root_hash=root_hash,
            resync_command=candidate["resync_command"],
        )
        if entry is not None:
            entries.append(entry)
    return {
        "source_root": str(source_root),
        "source_root_hash": root_hash,
        "entries": entries,
        "drift": any(item["drift"] for item in entries),
    }


def check_context_mode(repo: Path) -> dict:
    script = repo / "scripts/register-context-mode-codex.py"
    out = {"register_script": str(script), "register_script_exists": script.exists()}
    if script.exists():
        result = run([sys.executable, str(script), "--check"], cwd=repo, timeout=10)
        out.update({"check_returncode": result["returncode"], "check_ok": result["ok"], "stderr": result["stderr"][:500]})
    out["npx_present"] = bool(shutil.which("npx"))
    return out


def check_cursor_context_mode(skill_root: Path, project_root: Path) -> dict:
    script = skill_root / "scripts/register-context-mode-cursor.py"
    out = {"register_script": str(script), "register_script_exists": script.exists()}
    if not script.exists():
        return out
    global_result = run(
        [sys.executable, str(script), "--scope", "global", "--project-root", str(project_root), "--check"],
        timeout=10,
    )
    project_result = run(
        [sys.executable, str(script), "--scope", "project", "--project-root", str(project_root), "--check"],
        timeout=10,
    )
    out.update(
        {
            "global_check_returncode": global_result["returncode"],
            "global_check_ok": global_result["ok"],
            "project_check_returncode": project_result["returncode"],
            "project_check_ok": project_result["ok"],
            "global_path": str(Path.home() / ".cursor/mcp.json"),
            "project_path": str(project_root / ".cursor/mcp.json"),
            "npx_present": bool(shutil.which("npx")),
        }
    )
    return out


def check_opencode_context_mode(skill_root: Path, project_root: Path) -> dict:
    script = skill_root / "scripts/hosts/opencode/register_context_mode.py"
    out = {"register_script": str(script), "register_script_exists": script.exists()}
    if not script.exists():
        return out
    global_result = run(
        [sys.executable, str(script), "--scope", "global", "--project-root", str(project_root), "--check"],
        timeout=10,
    )
    project_result = run(
        [sys.executable, str(script), "--scope", "project", "--project-root", str(project_root), "--check"],
        timeout=10,
    )
    out.update(
        {
            "global_check_returncode": global_result["returncode"],
            "global_check_ok": global_result["ok"],
            "project_check_returncode": project_result["returncode"],
            "project_check_ok": project_result["ok"],
            "global_path": str(Path.home() / ".config/opencode/opencode.json"),
            "project_path": str(project_root / "opencode.json"),
            "npx_present": bool(shutil.which("npx")),
        }
    )
    return out


def check_grok() -> dict:
    path = shutil.which("grok") or str(Path.home() / ".grok/bin/grok")
    p = Path(path) if path else None
    if not p or not p.exists():
        return {"present": False}
    help_result = run([str(p), "--help"], timeout=4)
    version_result = run([str(p), "--version"], timeout=4)
    text = help_result["stdout"] + help_result["stderr"]
    return {
        "present": True,
        "path": str(p),
        "version": first_line(version_result["stdout"] or version_result["stderr"]),
        "grok_build": "Grok Build" in text,
        "headless_flags": "--prompt-file" in text and "--cwd" in text,
    }


def worker_write_file_probe(
    repo: Path,
    *,
    agent: str = "grok-code",
    enabled: bool = False,
    timeout_s: float = 90.0,
) -> dict:
    if not enabled:
        return {
            "enabled": False,
            "agent": agent,
            "ok": None,
            "state": "not_run",
            "detail": "run doctor with --worker-write-probe to validate engine writes a file e2e",
        }
    dispatch = SCRIPT_DIR / "goalflight_dispatch.py"
    if not dispatch.exists():
        return {
            "enabled": True,
            "agent": agent,
            "ok": False,
            "state": "blocked",
            "detail": f"missing dispatcher: {dispatch}",
        }
    state_dir = Path(os.environ.get("GOALFLIGHT_STATE_DIR", goalflight_compat.default_state_dir()))
    base = state_dir / "doctor-write-probe"
    base.mkdir(parents=True, exist_ok=True)
    dispatch_id = f"doctor-write-probe-{agent}-{os.getpid()}-{int(time.time())}"
    target = base / f"{dispatch_id}.target.txt"
    prompt = base / f"{dispatch_id}.prompt.md"
    status = base / f"{dispatch_id}.status.json"
    tail = base / f"{dispatch_id}.tail"
    expected = f"goalflight-doctor-write-probe:{dispatch_id}"
    prompt.write_text(
        "\n".join(
            [
                "Goal Flight doctor write-file e2e probe.",
                "",
                f"Write exactly this text to `{target}`:",
                "",
                "```text",
                expected,
                "```",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cmd = [
        sys.executable,
        str(dispatch),
        "--agent",
        agent,
        "--dispatch-id",
        dispatch_id,
        "--prompt-file",
        str(prompt),
        "--cwd",
        str(repo),
        "--poll-secs",
        "0.5",
        "--max-idle-secs",
        str(max(5.0, min(timeout_s, 180.0))),
        "--tail",
        str(tail),
        "--status-json",
        str(status),
    ]
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=max(timeout_s + 10.0, 15.0),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "enabled": True,
            "agent": agent,
            "ok": False,
            "state": "timeout",
            "detail": f"dispatch timed out after {exc.timeout}s",
            "dispatch_id": dispatch_id,
            "status_json": str(status),
            "tail": str(tail),
            "target": str(target),
        }

    status_payload: dict = {}
    if status.exists():
        try:
            parsed = json.loads(status.read_text(encoding="utf-8", errors="replace"))
            if isinstance(parsed, dict):
                status_payload = parsed
        except json.JSONDecodeError:
            status_payload = {}
    target_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    terminal_marker = status_payload.get("terminal_marker")
    marker_ok = bool(terminal_marker and terminal_marker.get("kind") in {"COMPLETE", "RESULT", "READY"})
    target_ok = target_text.strip() == expected
    ok = proc.returncode == 0 and target_ok and marker_ok
    state = status_payload.get("state") or ("complete" if proc.returncode == 0 else "failed")
    reason = status_payload.get("reason") or first_line(proc.stderr) or first_line(proc.stdout)
    return {
        "enabled": True,
        "agent": agent,
        "ok": ok,
        "state": state,
        "detail": (
            f"rc={proc.returncode} target_ok={target_ok} marker_ok={marker_ok}"
            + (f" reason={reason}" if reason else "")
        ),
        "dispatch_id": dispatch_id,
        "status_json": str(status),
        "tail": str(tail),
        "target": str(target),
    }


def _npm_registry_version(pkg: str, timeout: float = 6.0) -> str | None:
    """Return the published latest version of an npm package, or None on failure.

    Used as a CLI-currency proxy for codex / claude / claude-code-cli-acp.
    CLI version is the closest universal proxy for "model is current" — new
    models almost always ship with new CLI releases. Direct model-list APIs
    exist for some workers (cursor-agent models) but not codex/claude.
    """
    if not shutil.which("npm"):
        return None
    result = run(["npm", "view", pkg, "version"], timeout=timeout)
    if not result["ok"]:
        return None
    return first_line(result["stdout"])


def _version_tuple(s: str | None) -> tuple[int, ...]:
    """Parse semver-ish string into a comparison tuple, first token only.

    Examples (codex-r4 NIT #5 cases):
      `2.1.143 (Claude Code)`     → (2, 1, 143)
      `codex-cli 0.131.0`         → (0, 131, 0)
      `grok 0.1.213-alpha.1 (f42d66622d)` → (0, 1, 213)
    Stops at the first non-numeric chunk (the `-alpha.1` part) so trailing
    build metadata / git hashes don't pollute the tuple.
    """
    if not s:
        return ()
    # Extract the first whitespace-separated semver-ish token. Filters out
    # CLI-name prefixes like "codex-cli 0.131.0" → "0.131.0".
    token = None
    for piece in s.split():
        if any(c.isdigit() for c in piece) and "." in piece:
            token = piece
            break
    if token is None:
        return ()
    out: list[int] = []
    for chunk in token.split("."):
        # Take leading digits only — stop at the first non-digit (which
        # handles prerelease suffixes like "213-alpha" → leading "213").
        leading = ""
        for c in chunk:
            if c.isdigit():
                leading += c
            else:
                break
        if not leading:
            break
        out.append(int(leading))
        if len(leading) < len(chunk):
            # Chunk had a suffix (e.g., "213-alpha"). We grabbed the
            # leading number; stop here — don't try to parse subsequent
            # dot-separated chunks past a prerelease boundary.
            break
    return tuple(out)


def _sha256_path(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _claude_acp_installed_version() -> str | None:
    override = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_CLAUDE_ACP_VERSION",
        CLAUDE_ACP_VERSION_OVERRIDE_GATE,
    )
    if override:
        return override
    if shutil.which("npm"):
        npm_root = run(["npm", "root", "-g"], timeout=4)
        package_json = Path(npm_root.get("stdout") or "") / "claude-code-cli-acp/package.json"
        if npm_root.get("ok") and package_json.is_file():
            try:
                data = json.loads(package_json.read_text(encoding="utf-8"))
                version_text = data.get("version")
                if isinstance(version_text, str):
                    return version_text
            except (OSError, ValueError, json.JSONDecodeError):
                pass
    cli = version("claude-code-cli-acp", "--version")
    if cli.get("present"):
        return cli.get("version")
    return None


def _claude_acp_platform_binary() -> Path | None:
    override = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_CLAUDE_ACP_BIN_PATH",
        CLAUDE_ACP_BIN_OVERRIDE_GATE,
    )
    if override:
        return Path(override).expanduser()
    if shutil.which("npm"):
        npm_root = run(["npm", "root", "-g"], timeout=4)
        if npm_root.get("ok"):
            root = Path(npm_root.get("stdout") or "")
            platform_name = {
                "darwin": "darwin",
                "linux": "linux",
                "win32": "win32",
            }.get(sys.platform)
            machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
            arch = {
                "x86_64": "x64",
                "amd64": "x64",
                "arm64": "arm64",
                "aarch64": "arm64",
            }.get(machine)
            if platform_name and arch:
                exe = "claude-code-cli-acp.exe" if platform_name == "win32" else "claude-code-cli-acp"
                candidate = root / f"claude-code-cli-acp-{platform_name}-{arch}" / "bin" / exe
                if candidate.is_file():
                    return candidate
    path = shutil.which("claude-code-cli-acp")
    return Path(path) if path else None


def check_claude_acp_stopgap() -> dict:
    """Warn when the Claude ACP npm adapter still needs the pinned fixed build."""
    installed = _claude_acp_installed_version()
    binary = _claude_acp_platform_binary()
    present = bool(installed or (binary and binary.exists()))
    out: dict = {
        "present": present,
        "installed_version": installed,
        "max_stopgap_version": CLAUDE_ACP_STOPGAP_MAX_VERSION,
        "pinned_fix_commit": CLAUDE_ACP_PINNED_FIX_COMMIT,
        "binary": str(binary) if binary else None,
        "build_script": str(CLAUDE_ACP_BUILD_SCRIPT),
        "cargo_present": bool(shutil.which("cargo")),
    }
    if not present:
        out.update({
            "ok": None,
            "level": "info",
            "detail": "claude-code-cli-acp not installed",
        })
        return out
    installed_tuple = _version_tuple(installed)
    max_tuple = _version_tuple(CLAUDE_ACP_STOPGAP_MAX_VERSION)
    if not installed_tuple:
        out.update({
            "ok": True,
            "level": "ok",
            "patched": None,
            "pinned_build_applied": None,
            "detail": f"installed version {installed or 'unknown'} was not parsed; pinned build check skipped",
        })
        return out
    if installed_tuple > max_tuple:
        out.update({
            "ok": True,
            "level": "ok",
            "patched": None,
            "pinned_build_applied": None,
            "detail": f"installed version {installed} is newer than {CLAUDE_ACP_STOPGAP_MAX_VERSION}; npm release should include the fix",
        })
        return out
    if not binary or not binary.exists():
        out.update({
            "ok": False,
            "level": "warning",
            "patched": False,
            "pinned_build_applied": False,
            "detail": f"claude-code-cli-acp {installed or 'unknown'} installed but platform binary was not resolved; re-run ./install.sh claude-acp",
        })
        return out

    current_sha = _sha256_path(binary)
    orig = Path(f"{binary}.orig")
    orig_sha = _sha256_path(orig) if orig.exists() else None
    patched = bool(orig_sha and current_sha and orig_sha != current_sha)
    cargo_present = bool(shutil.which("cargo"))
    out.update({
        "backup": str(orig),
        "backup_exists": orig.exists(),
        "current_sha256": current_sha,
        "orig_sha256": orig_sha,
        "patched": patched,
        "pinned_build_applied": patched,
        "cargo_present": cargo_present,
    })
    if patched:
        out.update({
            "ok": True,
            "level": "ok",
            "detail": f"pinned claude-code-cli-acp build {CLAUDE_ACP_PINNED_FIX_COMMIT} appears installed; backup at {orig}",
        })
    else:
        if cargo_present:
            detail = (
                f"claude-code-cli-acp {installed or 'unknown'} may still be the broken npm binary; "
                f"run ./install.sh claude-acp to build pinned fix {CLAUDE_ACP_PINNED_FIX_COMMIT}"
            )
        else:
            detail = (
                f"claude-code-cli-acp {installed or 'unknown'} may still be the broken npm binary; "
                "install Rust cargo, then run ./install.sh claude-acp "
                f"(or wait for npm > {CLAUDE_ACP_STOPGAP_MAX_VERSION})"
            )
        out.update({
            "ok": False,
            "level": "warning",
            "detail": detail,
        })
    return out


def worker_currency_probe() -> dict:
    """Currency check for CLI workers other than cursor (which has its own
    model-level probe in cursor_models_probe).

    Returns per-worker: {current, latest, behind, source}. `behind` is True
    when current < latest semver-ish; None when either side couldn't be
    determined (e.g., npm registry unreachable).

    Detection mechanisms:
    - grok: `grok update --check --json` (native, returns updateAvailable bool).
    - codex / claude / claude-code-cli-acp: compare installed `--version` to
      the npm registry's published version. CLI version is the closest
      universal proxy for "model is current" — new models ship with new CLI
      releases.

    Cursor isn't here because cursor_models_probe() handles its leading-model
    discovery directly via `cursor-agent models`, which is more
    granular than CLI-version-currency.
    """
    out: dict = {}

    # --- grok: native check ---
    grok_entry: dict = {"source": "grok update --check --json"}
    if shutil.which("grok"):
        result = run(["grok", "update", "--check", "--json"], timeout=8)
        if result["ok"]:
            try:
                data = json.loads(result["stdout"])
                grok_entry["current"] = data.get("currentVersion")
                grok_entry["latest"] = data.get("latestVersion")
                grok_entry["behind"] = bool(data.get("updateAvailable"))
                grok_entry["channel"] = data.get("channel")
            except (ValueError, json.JSONDecodeError):
                grok_entry["error"] = "non-JSON output"
        else:
            grok_entry["error"] = (result.get("stderr") or "")[:200]
    else:
        grok_entry["error"] = "grok CLI not on PATH"
    out["grok"] = grok_entry

    # --- npm-backed: codex / claude / claude-code-cli-acp ---
    npm_targets = [
        ("codex", "@openai/codex", "codex"),
        ("claude", "@anthropic-ai/claude-code", "claude"),
        ("claude-code-cli-acp", "claude-code-cli-acp", "claude-code-cli-acp"),
    ]
    for label, npm_pkg, cli_name in npm_targets:
        entry: dict = {"source": f"npm view {npm_pkg} version"}
        if not shutil.which(cli_name):
            entry["error"] = f"{cli_name} not on PATH"
            out[label] = entry
            continue
        ver_result = version(cli_name, "--version")
        entry["current"] = ver_result.get("version")
        latest = _npm_registry_version(npm_pkg)
        entry["latest"] = latest
        if entry["current"] and latest:
            entry["behind"] = _version_tuple(entry["current"]) < _version_tuple(latest)
        else:
            entry["behind"] = None
        out[label] = entry

    return out


def cursor_models_probe() -> dict:
    """Probe cursor-agent for available models and pick the leading internal one.

    Discovery mechanism for Cursor's "domestic" (internal-tier) models —
    composer-*. Avoids hardcoding model names in docs that age fast.

    Cursor exposes models via `cursor-agent models`. Output format:

        Available models

        auto - Auto
        composer-2-fast - Composer 2 Fast (default)
        composer-2 - Composer 2 (current)
        gpt-5.3-codex-low - Codex 5.3 Low
        ...
        composer-2.5 - Composer 2.5
        ...

    Internal-tier models start with `composer-` (covered by the unlimited
    Cursor subscription tier). Vendor-passthrough models (`gpt-*`,
    `claude-*`) burn the paid-passthrough budget — exclude from "leading
    internal" pick.

    Returns:
      leading_internal: the highest-numbered `composer-X.Y` (non-`-fast`)
      all_internal: full list of composer-* model IDs in listed order
      current_user_model: what ~/.cursor/cli-config.json `modelId` is set to
      user_behind: True when current_user_model != leading_internal AND
                   current_user_model is older internal (or unset)
    """
    out: dict = {
        "leading_internal": None,
        "all_internal": [],
        "current_user_model": None,
        "user_behind": None,
    }
    cursor_agent = shutil.which("cursor-agent") or str(Path.home() / ".local/bin/cursor-agent")
    if not Path(cursor_agent).exists() and not shutil.which("cursor-agent"):
        return out
    result = run([cursor_agent, "models"], timeout=10)
    if not result["ok"]:
        out["error"] = result.get("stderr", "")[:200]
        return out

    internal_with_fast = []  # all composer-*
    internal_no_fast = []    # composer-X.Y without -fast suffix
    for line in result["stdout"].splitlines():
        line = line.strip()
        if " - " not in line:
            continue
        model_id, _, _ = line.partition(" - ")
        model_id = model_id.strip()
        if not model_id.startswith("composer-"):
            continue
        internal_with_fast.append(model_id)
        if not model_id.endswith("-fast"):
            internal_no_fast.append(model_id)
    out["all_internal"] = internal_with_fast

    # Pick highest version. composer-X.Y → tuple of int parts; missing minor → 0.
    def _version_key(m: str) -> tuple[int, ...]:
        suffix = m[len("composer-"):]
        parts = []
        for chunk in suffix.split("."):
            digits = "".join(c for c in chunk if c.isdigit())
            parts.append(int(digits) if digits else 0)
        return tuple(parts)

    if internal_no_fast:
        out["leading_internal"] = max(internal_no_fast, key=_version_key)

    # Read user's current modelId.
    cli_config = Path("~/.cursor/cli-config.json").expanduser()
    if cli_config.exists():
        try:
            data = json.loads(cli_config.read_text())
            current = data.get("model", {}).get("modelId")
            if current:
                out["current_user_model"] = current
        except (OSError, ValueError):
            pass

    # User-behind check: only set if we know both endpoints.
    if out["leading_internal"] and out["current_user_model"]:
        if out["current_user_model"] == out["leading_internal"]:
            out["user_behind"] = False
        elif out["current_user_model"].startswith("composer-"):
            # Both internal; behind if version lower.
            try:
                out["user_behind"] = (
                    _version_key(out["current_user_model"])
                    < _version_key(out["leading_internal"])
                )
            except (ValueError, IndexError):
                out["user_behind"] = None
        else:
            # User is on passthrough — not "behind" exactly, but flagged
            # because the recipe prefers internal-tier.
            out["user_behind"] = True

    return out


def check_acp() -> dict:
    grok = check_grok()
    sdk = check_acp_sdk()
    return {
        "sdk": sdk,
        "codex-acp": {"present": bool(shutil.which("codex-acp"))},
        "cursor-agent": {"present": bool(version("cursor-agent", "--version").get("present")), "version": version("cursor-agent", "--version").get("version")},
        "claude-code-cli-acp": {"present": bool(shutil.which("claude-code-cli-acp"))},
        "grok-agent-stdio": {"present": grok["present"], "headless_hint": grok.get("headless_flags")},
        "opencode-acp": {
            "present": bool(version("opencode", "--version").get("present")),
            "version": version("opencode", "--version").get("version"),
        },
        "opencode-bash-tail": {
            "present": (SCRIPT_DIR / "hosts/opencode/bash_tail.py").is_file(),
            "script": str(SCRIPT_DIR / "hosts/opencode/bash_tail.py"),
        },
    }


def check_acp_sdk() -> dict:
    venv_root = Path.home() / ".goal-flight/venvs/acp-0.10"
    python = venv_root / ("Scripts/python.exe" if goalflight_compat.is_windows() else "bin/python")
    hint = "SDK missing -- run install: /init installs agent-client-protocol into ~/.goal-flight/venvs/acp-0.10/"
    runner = SCRIPT_DIR / "goalflight_acp_run.py"
    system_python = sys.executable
    runner_help = run([system_python, str(runner), "--help"], timeout=8)
    runner_reexec_ok = runner_help["ok"] and "--agent" in runner_help.get("stdout", "")
    if not python.exists():
        return {
            "ok": False,
            "python": str(python),
            "error": hint,
            "runner_reexec_ok": runner_reexec_ok,
            "runner_reexec_detail": f"{system_python} --help rc={runner_help['returncode']}",
        }
    result = run(
        [
            str(python),
            "-c",
            "import acp, pydantic; print('acp ok')",
        ],
        timeout=5,
    )
    if not result["ok"]:
        return {
            "ok": False,
            "python": str(python),
            "error": hint,
            "stderr": result.get("stderr", "")[:500],
            "runner_reexec_ok": runner_reexec_ok,
            "runner_reexec_detail": f"{system_python} --help rc={runner_help['returncode']}",
        }
    return {
        "ok": True,
        "python": str(python),
        "version": "agent-client-protocol==0.10.*",
        "runner_reexec_ok": runner_reexec_ok,
        "runner_reexec_detail": f"{system_python} --help rc={runner_help['returncode']}",
    }


def acp_venv_executable_sanity() -> dict:
    venv_root = Path.home() / ".goal-flight/venvs/acp-0.10"
    linux_python = venv_root / "bin/python"
    windows_python = venv_root / "Scripts/python.exe"
    expected = windows_python if goalflight_compat.is_windows() else linux_python
    wrong_platform_present = (
        linux_python.exists() if goalflight_compat.is_windows() else windows_python.exists()
    )
    return {
        "venv_root": str(venv_root),
        "expected_python": str(expected),
        "expected_exists": expected.exists(),
        "windows_python": str(windows_python),
        "windows_python_exists": windows_python.exists(),
        "linux_python": str(linux_python),
        "linux_python_exists": linux_python.exists(),
        "wrong_platform_python_present": wrong_platform_present,
        "ok": expected.exists() and not wrong_platform_present,
        "recommendation": (
            "Use a Linux-built ACP venv with bin/python inside WSL/Linux; "
            "do not share a Windows Scripts/python.exe venv."
        ),
    }


def wsl_version_info() -> dict:
    if not goalflight_compat.is_wsl():
        return {"is_wsl": False, "version": None}
    osrelease = ""
    proc_version = ""
    for path, key in (
        (Path("/proc/sys/kernel/osrelease"), "osrelease"),
        (Path("/proc/version"), "proc_version"),
    ):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            text = ""
        if key == "osrelease":
            osrelease = text
        else:
            proc_version = text
    joined = f"{osrelease}\n{proc_version}".lower()
    if "wsl2" in joined or "microsoft-standard" in joined:
        version = "2"
    elif "microsoft" in joined or "wsl" in joined:
        version = "1_or_unknown"
    else:
        version = "unknown"
    return {
        "is_wsl": True,
        "version": version,
        "osrelease": osrelease,
        "proc_version": proc_version[:300],
    }


def _nearest_existing_path(path: Path) -> Path | None:
    current = path.expanduser()
    while True:
        if current.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def filesystem_type(path: Path) -> dict:
    target = _nearest_existing_path(path)
    if target is None or goalflight_compat.is_windows():
        return {"path": str(path), "stat_path": str(target) if target else None, "type": None, "ok": False}
    if goalflight_compat.is_linux():
        result = run(["stat", "-f", "-c", "%T", str(target)], timeout=3)
    elif goalflight_compat.is_macos():
        result = run(["stat", "-f", "%T", str(target)], timeout=3)
    else:
        result = run(["stat", "-f", "-c", "%T", str(target)], timeout=3)
        if not result["ok"]:
            result = run(["stat", "-f", "%T", str(target)], timeout=3)
    return {
        "path": str(path),
        "stat_path": str(target),
        "type": first_line(result.get("stdout")) if result["ok"] else None,
        "ok": bool(result["ok"]),
        "error": None if result["ok"] else first_line(result.get("stderr")),
    }


def check_wsl_filesystems(repo: Path, *, fleet_dir: Path | None = None) -> dict:
    state_dir = Path(os.environ.get("GOALFLIGHT_STATE_DIR", goalflight_compat.default_state_dir())).expanduser()
    if fleet_dir is not None:
        resolved_fleet_dir = fleet_dir.expanduser()
    elif goalflight_fleet is not None:
        resolved_fleet_dir = goalflight_fleet.default_fleet_dir()
    else:
        resolved_fleet_dir = Path(os.environ.get("GOALFLIGHT_FLEET_DIR", Path.home() / ".goal-flight/fleet")).expanduser()
    paths = {
        "project_root": repo,
        "state_dir": state_dir,
        "fleet_dir": resolved_fleet_dir,
        "fleet_lock_dir": resolved_fleet_dir / "locks",
        "worktree_root": repo / "worktrees",
    }
    details = []
    warnings = []
    for label, path in paths.items():
        drvfs_path = goalflight_compat.is_wsl_drvfs_path(path)
        item = {
            "label": label,
            "path": str(path),
            "drvfs_path": drvfs_path,
            "filesystem": filesystem_type(path),
        }
        details.append(item)
        if goalflight_compat.is_wsl() and drvfs_path:
            warnings.append(
                f"{label} is under /mnt/<drive> (DrvFs); use a WSL-native path for reliable flock and worktrees"
            )
    return {
        "schema": "goalflight.wsl_filesystems.v1",
        "is_wsl": goalflight_compat.is_wsl(),
        "ok": not warnings,
        "details": details,
        "warnings": warnings,
        "recommendation": "Keep project, state, fleet, and worktree roots on the WSL-native filesystem.",
    }


def git_state(repo: Path) -> dict:
    if not (repo / ".git").exists():
        return {"present": False}
    return {
        "present": True,
        "branch": run(["git", "branch", "--show-current"], cwd=repo, timeout=3)["stdout"],
        "head": run(["git", "log", "-1", "--oneline"], cwd=repo, timeout=3)["stdout"],
        "dirty": bool(run(["git", "status", "--short"], cwd=repo, timeout=3)["stdout"]),
    }


def _parse_git_worktree_porcelain(text: str) -> list[dict]:
    records: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if line.startswith("worktree "):
            if current:
                records.append(current)
            current = {"path": line.split(" ", 1)[1]}
            continue
        if current is None:
            continue
        if " " in line:
            key, value = line.split(" ", 1)
            current[key] = value
        elif line:
            current[line] = True
    if current:
        records.append(current)
    return records


def _active_capacity_dispatches() -> tuple[set[tuple[str, str]], str | None]:
    if goalflight_capacity is None:
        return set(), "goalflight_capacity import failed"
    try:
        with goalflight_capacity.StateLock():
            data = goalflight_capacity.load_state()
            goalflight_capacity.prune_state(data)
            active: set[tuple[str, str]] = set()
            for lease in goalflight_capacity.active_leases(data):
                dispatch_id = lease.get("dispatch_id")
                active_root = (
                    lease.get("worktree_path")
                    or lease.get("worker_cwd")
                    or lease.get("project_root")
                )
                if not dispatch_id or not active_root:
                    continue
                try:
                    project_path = str(Path(str(active_root)).expanduser().resolve())
                except OSError:
                    project_path = str(active_root)
                active.add((str(dispatch_id), project_path))
            return active, None
    except Exception as exc:
        return set(), f"{type(exc).__name__}: {exc}"


def check_worktrees(project_root: Path) -> dict:
    """Report managed per-dispatch worktrees and stale completed ones.

    Only paths under ``<project_root>/worktrees/<dispatch-id>`` are managed by
    local ACP dispatch. Other git worktrees belong to operators or other tools.
    """
    result = run(["git", "worktree", "list", "--porcelain"], cwd=project_root, timeout=8)
    if not result.get("ok"):
        return {
            "count": 0,
            "paths": [],
            "stale": [],
            "ok": False,
            "error": result.get("stderr") or result.get("stdout"),
        }

    managed_root_path = project_root / "worktrees"
    if managed_root_path.is_symlink():
        return {
            "count": 0,
            "paths": [],
            "stale": [],
            "escaped": [],
            "ok": False,
            "error": f"managed worktree root must not be a symlink: {managed_root_path}",
        }
    # Use resolve() to canonicalize /var/folders → /private/var/folders on macOS
    # so the prefix-match below works against `git worktree list`'s resolved paths.
    try:
        managed_root = managed_root_path.resolve()
    except OSError:
        managed_root = managed_root_path.absolute()
    paths: list[str] = []
    dispatch_by_path: dict[str, str] = {}
    details: list[dict] = []
    escaped: list[str] = []
    for record in _parse_git_worktree_porcelain(result.get("stdout") or ""):
        raw = record.get("path")
        if not raw:
            continue
        raw_path = Path(str(raw))
        path = raw_path if raw_path.is_absolute() else project_root / raw_path
        path = path.absolute()
        # Try literal-parent match first (handles the symlink-leaf-escape case:
        # a symlink whose own path sits under managed_root_path but resolves
        # outside — we want to flag, not skip). Fall back to resolved-match
        # for the /private/var ↔ /var resolution case.
        rel = None
        try:
            rel = path.relative_to(managed_root_path.absolute())
        except ValueError:
            try:
                rel = path.resolve().relative_to(managed_root)
            except (ValueError, OSError):
                continue
        if rel is None or len(rel.parts) != 1:
            continue
        if path.is_symlink():
            escaped.append(str(path))
            continue
        try:
            resolved_path = path.resolve()
            resolved_path.relative_to(managed_root)
        except (ValueError, OSError):
            escaped.append(str(path))
            continue
        dispatch_id = rel.parts[0]
        path_text = str(resolved_path)
        paths.append(path_text)
        dispatch_by_path[path_text] = dispatch_id
        status = run(["git", "status", "--short"], cwd=resolved_path, timeout=4)
        head = run(["git", "rev-parse", "--short", "HEAD"], cwd=resolved_path, timeout=4)
        branch = run(["git", "branch", "--show-current"], cwd=resolved_path, timeout=4)
        details.append(
            {
                "path": path_text,
                "dispatch_id": dispatch_id,
                "dirty": bool(status.get("stdout")),
                "head": head.get("stdout") or None,
                "branch": branch.get("stdout") or None,
            }
        )

    blocking_paths: list[str] = []
    if managed_root_path.is_dir():
        registered = {str(Path(path).absolute()) for path in paths}
        registered_resolved = {str(Path(path).resolve()) for path in paths}
        for child in sorted(managed_root_path.iterdir()):
            if child.is_symlink():
                continue
            if not child.is_dir():
                continue
            child_abs = str(child.absolute())
            child_resolved = str(child.resolve())
            if child_abs not in registered and child_resolved not in registered_resolved:
                blocking_paths.append(child_abs)

    active_dispatches, capacity_error = _active_capacity_dispatches()
    project_root_key = str(project_root.resolve())
    stale = []
    if capacity_error is None:
        stale = [
            path
            for path, dispatch_id in sorted(dispatch_by_path.items())
            if (dispatch_id, path) not in active_dispatches
            and (dispatch_id, project_root_key) not in active_dispatches
        ]
    return {
        "count": len(paths),
        "paths": sorted(paths),
        "stale": stale,
        "blocking_paths": blocking_paths,
        "details": sorted(details, key=lambda item: item["path"]),
        "escaped": sorted(escaped),
        "ok": capacity_error is None and not escaped and not stale and not blocking_paths,
        "error": (
            capacity_error
            or (f"managed worktree path escaped root: {escaped}" if escaped else None)
            or (f"unregistered blocking worktree paths: {blocking_paths}" if blocking_paths else None)
        ),
    }


def _agent_instructions(repo: Path) -> tuple[Path | None, str]:
    path = repo / "AGENTS.md"
    if path.exists():
        return path, path.read_text(errors="replace")
    return None, ""


def _command_entry(text: str, key: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        for prefix in (f"- {key}:", f"* {key}:", f"{key}:"):
            if lower.startswith(prefix):
                return stripped.split(":", 1)[1].strip()
    return None


def _goalflight_skill_root(agent_text: str) -> dict:
    source = "package"
    raw = None
    resolved = SCRIPT_DIR.parent
    env_value = os.environ.get("GOALFLIGHT_ROOT")
    for line in agent_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("- skill-root:") or stripped.lower().startswith("skill-root:"):
            raw = stripped.split(":", 1)[1].strip().strip("`")
            break
    if env_value:
        resolved = Path(env_value).expanduser()
        source = "GOALFLIGHT_ROOT"
    elif raw:
        match = re.search(r"\$\{GOALFLIGHT_ROOT:-([^}]+)\}", raw)
        candidate = match.group(1) if match else raw
        if "<path-to-goal-flight-clone>" not in candidate:
            resolved = Path(candidate).expanduser()
            source = "AGENTS.md"
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "source": source,
        "raw": raw,
    }


def check_router(repo: Path) -> dict:
    """Track B: single-surface router readiness (optional field for JSON consumers)."""
    actions_script = repo / "scripts" / "goalflight_actions.py"
    bin_goalflight = repo / "bin" / "goalflight"
    python = sys.executable
    out: dict = {
        "bin_goalflight": _path_state(bin_goalflight),
        "actions_script": _path_state(actions_script),
        "recommended_entrypoint": "bin/goalflight <domain> <resource> <verb>",
    }
    if not actions_script.exists():
        out["ok"] = False
        out["reason"] = "goalflight_actions.py missing"
        return out
    validate = run([python, str(actions_script), "validate"], cwd=repo, timeout=15)
    route = run(
        [python, str(actions_script), "route", "core", "doctor", "read"],
        cwd=repo,
        timeout=10,
    )
    out["actions_validate"] = {
        "ok": validate["ok"],
        "returncode": validate["returncode"],
    }
    out["doctor_route_dry_run"] = {
        "ok": route["ok"],
        "returncode": route["returncode"],
        "command_preview": first_line(route.get("stdout")),
    }
    out["ok"] = bool(validate["ok"] and route["ok"] and bin_goalflight.exists())
    if not bin_goalflight.exists():
        out["reason"] = "bin/goalflight missing — use scripts/goalflight_actions.py route"
    elif not validate["ok"]:
        out["reason"] = "action registry validate failed"
    elif not route["ok"]:
        out["reason"] = "core.doctor.read route failed"
    return out


def check_project_goalflight_readiness(repo: Path) -> dict:
    docs_private = repo / "docs-private"
    env_caveats = docs_private / "env-caveats.md"
    repo_skill = repo / "SKILL.md"
    cursor_project_skill = repo / ".cursor/skills/goal-flight/SKILL.md"
    cursor_project_rules = repo / ".cursor/rules/goal-flight.mdc"
    opencode_project_skill = repo / ".opencode/skills/goal-flight/SKILL.md"
    opencode_project_config = repo / "opencode.json"
    opencode_canonical_config = repo / "configs/opencode/opencode.json"
    agent_path, agent_text = _agent_instructions(repo)
    lower = agent_text.casefold()
    has_routing = bool(
        agent_path
        and "goal-flight" in lower
        and ("skill-root" in lower or "goalflight_root" in lower or "commands/" in lower)
        and "skill.md" in lower
    )
    commands = {
        "test": _command_entry(agent_text, "test"),
        "lint": _command_entry(agent_text, "lint"),
        "build": _command_entry(agent_text, "build"),
    }
    resume_notes = sorted(str(path) for path in docs_private.glob("RESUME-NOTES*.md"))
    skill_root = _goalflight_skill_root(agent_text)
    warnings: list[str] = []
    if not env_caveats.exists():
        warnings.append("missing docs-private/env-caveats.md")
    if not repo_skill.exists():
        warnings.append("missing repo SKILL.md")
    if not has_routing:
        warnings.append("AGENTS.md lacks goal-flight routing")
    if not skill_root.get("exists"):
        warnings.append("skill-root not resolvable")
    if not commands["test"]:
        warnings.append("project test command not recorded")
    return {
        "init_done": env_caveats.exists(),
        "env_caveats": str(env_caveats),
        "repo_skill": {"path": str(repo_skill), "exists": repo_skill.exists()},
        "cursor_project": {
            "skill": _path_state(cursor_project_skill),
            "rules": _path_state(cursor_project_rules),
        },
        "opencode_project": {
            "skill": _path_state(opencode_project_skill),
            "config": _path_state(opencode_project_config),
            "canonical_config": _path_state(opencode_canonical_config),
        },
        "routing": {
            "path": str(agent_path) if agent_path else None,
            "exists": bool(agent_path),
            "has_goalflight_block": has_routing,
        },
        "commands": commands,
        "resume_notes": resume_notes,
        "skill_root": skill_root,
        "warnings": warnings,
        "ok": not warnings,
    }


def check_wsl(repo: Path) -> dict:
    """Report Windows/WSL dispatch capability honestly.

    Native Windows may run doctor/status/plan, but dispatch remains a POSIX path
    and must run under WSL. ``probe_wsl`` distinguishes "wsl.exe exists" from
    "at least one distro is installed"; the latter is the real readiness gate.
    """
    probe = goalflight_compat.probe_wsl(repo)
    if goalflight_compat.is_windows():
        usable = bool(probe.get("usable"))
        return {
            "host": "native_windows",
            "is_windows": True,
            "is_wsl": False,
            "baseline": "wsl_required_for_dispatch",
            "usable": usable,
            "probe": probe,
            "dispatch_capability": "refused_native_use_wsl",
            "native_control_plane": True,
            "native_cleanup": "degraded_per_pid",
            "false_no_distro_debug": (
                "If a distro is installed but probe.state is no_installed_distributions, "
                "inspect probe.stdout/stderr/distributions, UTF-16LE/NUL decoding from "
                "`wsl -l -q`, localized no-distro text, and enterprise-policy guidance output."
            ),
            "next_step": (
                "Open the installed WSL distro and run Goal Flight dispatch there."
                if usable
                else "Ask before running `wsl --install`; admin elevation and a reboot may be required."
            ),
        }
    if goalflight_compat.is_wsl():
        return {
            "host": "wsl",
            "is_windows": False,
            "is_wsl": True,
            "wsl_version": wsl_version_info(),
            "acp_venv": acp_venv_executable_sanity(),
            "baseline": "wsl",
            "usable": True,
            "probe": probe,
            "dispatch_capability": "full",
            "native_control_plane": True,
            "native_cleanup": "posix_process_group",
            "next_step": "Dispatch is supported from this WSL session.",
        }
    return {
        "host": "posix",
        "is_windows": False,
        "is_wsl": False,
        "baseline": "posix",
        "usable": True,
        "probe": probe,
        "dispatch_capability": "full",
        "native_control_plane": True,
        "native_cleanup": "posix_process_group",
        "next_step": None,
    }


def doctor(
    repo: Path,
    *,
    fleet: bool = False,
    fleet_dir: Path | None = None,
    fleet_probe: bool = False,
    worker_write_probe: bool = False,
    write_probe_agent: str = "grok-code",
) -> dict:
    skill_root = SCRIPT_DIR.parent
    codex_desktop = app_exists("Codex", "com.openai.codex")
    codex_cli = version("codex", "--version")
    cursor_desktop = app_exists("Cursor", "com.todesktop.230313mzl4w4u92")
    payload = {
        "schema": "goalflight.doctor.v1",
        "repo": str(repo),
        "platform": check_platform(),
        "plugin": check_plugin(repo),
        "host_goalflight_install": check_host_goalflight_install(),
        "installed_skill_drift": check_installed_skill_drift(skill_root, repo),
        "claude": version("claude", "--version"),
        "codex": {
            "desktop_present": codex_desktop,
            "cli": codex_cli,
            "desktop_without_cli": bool(codex_desktop and not codex_cli.get("present")),
            "install_hint": "npm install -g @openai/codex && codex login" if codex_desktop and not codex_cli.get("present") else None,
        },
        "context_mode": check_context_mode(skill_root),
        "cursor_context_mode": check_cursor_context_mode(skill_root, repo),
        "opencode_context_mode": check_opencode_context_mode(skill_root, repo),
        "gstack": check_gstack(),
        "agent_traits": check_agent_traits(),
        "autoreview": check_autoreview(skill_root),
        "agents_md_state": check_agents_md_state(repo),
        "session_status": check_session_status(skill_root, repo),
        "resume_notes_pattern": check_resume_notes_pattern(repo),
        "cursor": {
            "desktop_present": cursor_desktop,
            "cli": version("cursor", "--version"),
            "agent": version("cursor-agent", "--version"),
            "models": cursor_models_probe(),
        },
        "opencode": version("opencode", "--version"),
        "grok": check_grok(),
        "worker_write_probe": worker_write_file_probe(
            repo,
            agent=write_probe_agent,
            enabled=worker_write_probe,
        ),
        "acp": check_acp(),
        "claude_acp_stopgap": check_claude_acp_stopgap(),
        "project": git_state(repo),
        "worktrees": check_worktrees(repo),
        "project_goalflight_readiness": check_project_goalflight_readiness(repo),
        "router": check_router(repo),
        "capacity": goalflight_capacity.profile(argparse.Namespace()) if goalflight_capacity else None,
        "fleet_reconcile": _fleet_reconcile_summary(),
        "rate_pressure": _rate_pressure_summary(),
        "worker_currency": worker_currency_probe(),
        "wsl": check_wsl(repo),
        "wsl_filesystems": check_wsl_filesystems(repo, fleet_dir=fleet_dir),
    }
    if goalflight_compat.is_windows():
        payload["platform"].update({
            "resolved_python": goalflight_compat.python_executable(),
            "native_windows_support": (
                "read/plan OK; dispatch refused on native Windows; run dispatch inside WSL. "
                "Cleanup is degraded to tracked pid-only."
            ),
        })
    if fleet:
        payload["fleet"] = _fleet_auth_summary(fleet_dir, refresh=fleet_probe)
        payload["fleet_tool_smoke"] = _fleet_tool_smoke_summary(fleet_dir)
        payload["fleet_dispatches"] = _fleet_dispatch_report(fleet_dir)
        payload["fleet_bash_tail_probes"] = _fleet_bash_tail_probe_summary(fleet_dir)
    return payload


def _fleet_auth_summary(
    fleet_dir: Path | None = None,
    *,
    refresh: bool = False,
) -> dict:
    if goalflight_fleet is None:
        return {"available": False, "reason": "goalflight_fleet import failed", "nodes": []}
    try:
        import goalflight_fleet_billing as fleet_billing

        target = fleet_dir or goalflight_fleet.default_fleet_dir()
        return fleet_billing.fleet_auth_doctor(target, refresh=refresh)
    except Exception as exc:  # pragma: no cover
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}", "nodes": []}


def _fleet_tool_smoke_summary(fleet_dir: Path | None = None) -> dict:
    if goalflight_fleet is None:
        return {"available": False, "reason": "goalflight_fleet import failed", "canaries": []}
    try:
        import goalflight_fleet_tool_smoke as fleet_tool_smoke

        target = fleet_dir or goalflight_fleet.default_fleet_dir()
        return fleet_tool_smoke.fleet_tool_smoke_doctor(target)
    except Exception as exc:  # pragma: no cover
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}", "canaries": []}


def _rate_pressure_summary() -> dict:
    """Compact rate-pressure summary for doctor output.

    The orchestrator's job is to not overheat provider services in a way
    that takes the live session down. This surfaces the same probe
    `goalflight_rate_pressure.py` emits, so the doctor is a one-stop
    "is everything OK" check.
    """
    if goalflight_rate_pressure is None:
        return {"available": False, "reason": "goalflight_rate_pressure import failed"}
    try:
        state_dir = Path(os.environ.get("GOALFLIGHT_STATE_DIR", goalflight_compat.default_state_dir()))
        records = goalflight_rate_pressure.collect_records(state_dir)
        billing = goalflight_rate_pressure.load_billing_accounts()
        pool_map = goalflight_rate_pressure.agent_limit_pool_map(billing)
        pressure = goalflight_rate_pressure.pressure_per_provider(records, pool_map=pool_map)
        current_caps = dict(goalflight_capacity.DEFAULT_AGENT_CAPS) if goalflight_capacity else {}
        rec = goalflight_rate_pressure.recommend(pressure, current_caps, pool_map=pool_map)
        rec["records_examined"] = len(records)
        rec["state_dir"] = str(state_dir)
        return rec
    except Exception as exc:  # pragma: no cover - keep doctor resilient
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def _fleet_reconcile_summary(*, release_stale: bool = False) -> dict:
    if goalflight_fleet is None:
        return {"available": False, "reason": "goalflight_fleet import failed"}
    try:
        fleet_dir = goalflight_fleet.default_fleet_dir()
        if release_stale:
            import goalflight_fleet_stale as fleet_stale

            return fleet_stale.doctor_fleet_stale_release(fleet_dir, mutate=True)
        return goalflight_fleet.reconcile_fleet(fleet_dir, release_stale=False)
    except Exception as exc:  # pragma: no cover
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def _fleet_bash_tail_probe_summary(fleet_dir: Path | None = None) -> dict:
    if goalflight_fleet is None:
        return {"available": False, "reason": "goalflight_fleet import failed", "nodes": []}
    try:
        import goalflight_fleet_bash_tail_probe as bash_probe

        target = fleet_dir or goalflight_fleet.default_fleet_dir()
        fleet_path = target / "fleet.json"
        if not fleet_path.exists():
            return {"available": True, "fleet_dir": str(target), "nodes": []}
        fleet_doc = goalflight_fleet.read_json(fleet_path)
        nodes_out: list[dict] = []
        for node_id in sorted((fleet_doc.get("nodes") or {}).keys()):
            probes = bash_probe.load_latest_probes(target, node_id)
            nodes_out.append(
                {
                    "node_id": node_id,
                    "bash_tail_probe": {
                        adapter: {
                            "ok": doc.get("ok"),
                            "marker_seen": doc.get("marker_seen"),
                            "probed_at": doc.get("probed_at"),
                        }
                        for adapter, doc in sorted(probes.items())
                    },
                }
            )
        return {"available": True, "fleet_dir": str(target), "nodes": nodes_out}
    except Exception as exc:  # pragma: no cover
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}", "nodes": []}


def _fleet_dispatch_report(fleet_dir: Path | None = None) -> dict:
    if goalflight_fleet is None:
        return {"available": False, "reason": "goalflight_fleet import failed", "dispatches": []}
    try:
        import goalflight_fleet_reconcile as fleet_reconcile

        target = fleet_dir or goalflight_fleet.default_fleet_dir()
        rows = fleet_reconcile.classify_fleet_dispatches(target)
        return {"available": True, "fleet_dir": str(target), "dispatches": rows}
    except Exception as exc:  # pragma: no cover
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}", "dispatches": []}


def status_line(ok: bool | None, label: str, detail: str | None = None) -> str:
    prefix = "[INFO]" if ok is None else ("[OK]" if ok else "[WARN]")
    return f"{prefix} {label}" + (f" — {detail}" if detail else "")


def print_human(payload: dict) -> None:
    plugin = payload["plugin"]
    lines = [
        status_line(
            None if plugin.get("skipped") else plugin.get("manifest_exists"),
            "package plugin manifest",
            plugin.get("skip_reason") if plugin.get("skipped") else plugin.get("manifest"),
        ),
        status_line(plugin.get("validate_ok"), "claude plugin validate", plugin.get("validate_first_line")),
        status_line(payload["claude"].get("present"), "claude CLI", payload["claude"].get("version")),
        status_line(payload["codex"]["cli"].get("present"), "codex CLI", payload["codex"]["cli"].get("version")),
        status_line(not payload["codex"].get("desktop_without_cli"), "Codex Desktop/CLI pairing", payload["codex"].get("install_hint")),
        status_line(
            payload["context_mode"].get("register_script_exists") and payload["context_mode"].get("check_returncode") == 0,
            "context-mode registration probe",
            f"return={payload['context_mode'].get('check_returncode')} script_exists={payload['context_mode'].get('register_script_exists')}",
        ),
        status_line(
            payload["cursor_context_mode"].get("global_check_returncode") == 0,
            "Cursor context-mode MCP global",
            f"{payload['cursor_context_mode'].get('global_path')} npx={payload['cursor_context_mode'].get('npx_present')}",
        ),
        status_line(
            payload["cursor_context_mode"].get("project_check_returncode") == 0,
            "Cursor context-mode MCP project",
            payload["cursor_context_mode"].get("project_path"),
        ),
        status_line(
            payload["opencode_context_mode"].get("global_check_returncode") == 0,
            "OpenCode context-mode MCP global",
            f"{payload['opencode_context_mode'].get('global_path')} npx={payload['opencode_context_mode'].get('npx_present')}",
        ),
        status_line(
            payload["opencode_context_mode"].get("project_check_returncode") == 0,
            "OpenCode context-mode MCP project",
            payload["opencode_context_mode"].get("project_path"),
        ),
        status_line(
            False if payload["gstack"].get("level") == "warning" else payload["gstack"].get("present"),
            "gstack",
            payload["gstack"].get("detail") or payload["gstack"].get("version"),
        ),
        status_line(
            (payload.get("agent_traits") or {}).get("ok"),
            "agent traits (global opt-in)",
            (payload.get("agent_traits") or {}).get("detail"),
        ),
        status_line(payload["autoreview"].get("present"), "autoreview", payload["autoreview"].get("version") or payload["autoreview"].get("install_hint")),
        status_line(payload["cursor"].get("desktop_present"), "Cursor Desktop", None),
        status_line(payload["cursor"]["agent"].get("present"), "cursor-agent ACP", payload["cursor"]["agent"].get("version")),
        status_line(payload["opencode"].get("present"), "opencode ACP", payload["opencode"].get("version")),
        status_line(payload["grok"].get("present"), "Grok Build binary", payload["grok"].get("version")),
        status_line(payload["grok"].get("headless_flags"), "Grok headless flags", None),
        status_line(
            (payload.get("claude_acp_stopgap") or {}).get("ok"),
            "Claude ACP pinned TUI-submit fix",
            (payload.get("claude_acp_stopgap") or {}).get("detail"),
        ),
    ]
    write_probe = payload.get("worker_write_probe") or {}
    lines.append(status_line(
        write_probe.get("ok"),
        f"{write_probe.get('agent', 'worker')} write-file e2e probe",
        write_probe.get("detail"),
    ))
    if goalflight_compat.is_windows():
        platform_info = payload.get("platform") or {}
        wsl_info = payload.get("wsl") or {}
        probe = wsl_info.get("probe") or {}
        lines = [
            status_line(
                None,
                "platform",
                f"os={platform_info.get('os_name')} sys={platform_info.get('sys_platform')} "
                f"python={platform_info.get('resolved_python')}",
            ),
            status_line(
                None,
                "native Windows support",
                platform_info.get("native_windows_support"),
            ),
            status_line(
                bool(wsl_info.get("usable")),
                "WSL dispatch baseline",
                f"{probe.get('state')} — {wsl_info.get('next_step')}",
            ),
            *lines,
        ]
    elif (payload.get("wsl") or {}).get("is_wsl"):
        wsl_info = payload.get("wsl") or {}
        lines = [
            status_line(
                True,
                "WSL dispatch baseline",
                wsl_info.get("next_step"),
            ),
            *lines,
        ]
    wsl_filesystems = payload.get("wsl_filesystems") or {}
    if wsl_filesystems.get("warnings"):
        lines = [
            status_line(
                False,
                "WSL DrvFs guard",
                "; ".join(wsl_filesystems.get("warnings") or []),
            ),
            *lines,
        ]
    for host, item in (payload.get("host_goalflight_install") or {}).items():
        lines.append(status_line(item.get("ok"), f"{host} goal-flight host install", item.get("detail")))
    for item in (payload.get("installed_skill_drift") or {}).get("entries", []):
        detail = (
            f"{item.get('path')} source={str(item.get('source_hash'))[:12]} "
            f"installed={str(item.get('installed_hash'))[:12]}"
        )
        if item.get("drift"):
            detail = f"{detail} resync={item.get('resync_command')}"
        lines.append(status_line(not item.get("drift"), f"{item.get('host')} installed_skill_md_hash", detail))
    for name, item in payload["acp"].items():
        if name == "sdk":
            lines.append(status_line(item.get("ok"), "ACP SDK venv", item.get("python")))
            lines.append(status_line(item.get("runner_reexec_ok"), "ACP runner Python re-exec", item.get("runner_reexec_detail")))
        else:
            lines.append(status_line(item.get("present"), f"ACP {name}", item.get("version")))
    cap = payload.get("capacity") or {}
    lines.append(status_line(True, "capacity profile", f"operating={cap.get('operating_cap')} raw={cap.get('raw_ram_ceiling')} ram={cap.get('ram_mb')}MB"))
    project = payload["project"]
    lines.append(status_line(project.get("present"), "git project", f"{project.get('branch')} {project.get('head')} dirty={project.get('dirty')}"))
    fleet_tool_smoke = payload.get("fleet_tool_smoke")
    if fleet_tool_smoke:
        canaries = fleet_tool_smoke.get("canaries") or []
        states: dict[str, int] = {}
        for item in canaries:
            state = str(item.get("cache_state") or item.get("status") or "unknown")
            states[state] = states.get(state, 0) + 1
        unhealthy = sum(states.get(key, 0) for key in ("red", "stale", "missing", "unknown"))
        detail = (
            f"{len(canaries)} cached; "
            f"green={states.get('green', 0)} red={states.get('red', 0)} stale={states.get('stale', 0)}"
        )
        lines.append(status_line(unhealthy == 0 if canaries else None, "fleet tool-smoke canaries", detail))
    worktrees = payload.get("worktrees") or {}
    stale_worktrees = worktrees.get("stale") or []
    if stale_worktrees:
        details = payload.get("worktrees", {}).get("details") or []
        stale_detail = [item for item in details if item.get("path") in set(stale_worktrees)]
        dirty_count = sum(1 for item in stale_detail if item.get("dirty"))
        heads = ", ".join(
            f"{Path(str(item.get('path'))).name}@{item.get('head') or '?'}"
            for item in stale_detail[:3]
        )
        lines.append(status_line(
            False,
            "parallel worktrees",
            f"stale={len(stale_worktrees)} managed={worktrees.get('count')}; "
            f"dirty={dirty_count} heads={heads or 'n/a'}; "
            "inspect, run `git worktree remove <path>`, then `git worktree prune`",
        ))
    elif worktrees.get("blocking_paths"):
        lines.append(status_line(
            False,
            "parallel worktrees",
            f"blocking={len(worktrees.get('blocking_paths') or [])} managed={worktrees.get('count')}; "
            "inspect/remove unregistered paths before reusing dispatch ids",
        ))
    elif worktrees.get("ok") is False:
        lines.append(status_line(
            False,
            "parallel worktrees",
            f"managed={worktrees.get('count', 0)} stale=unknown; {worktrees.get('error')}",
        ))
    else:
        lines.append(status_line(
            worktrees.get("ok"),
            "parallel worktrees",
            f"managed={worktrees.get('count', 0)} stale=0",
        ))
    readiness = payload.get("project_goalflight_readiness") or {}
    router = payload.get("router") or {}
    lines.extend([
        status_line(readiness.get("init_done"), "project init", readiness.get("env_caveats")),
        status_line(readiness.get("repo_skill", {}).get("exists"), "project SKILL.md", readiness.get("repo_skill", {}).get("path")),
        status_line(readiness.get("routing", {}).get("has_goalflight_block"), "project AGENTS goal-flight routing", readiness.get("routing", {}).get("path")),
        status_line(
            readiness.get("skill_root", {}).get("exists"),
            "skill-root resolvable",
            f"{readiness.get('skill_root', {}).get('path')} source={readiness.get('skill_root', {}).get('source')}",
        ),
    ])
    lines.append(
        status_line(
            router.get("ok"),
            "action router",
            router.get("recommended_entrypoint") if router.get("ok") else router.get("reason"),
        )
    )
    commands = readiness.get("commands") or {}
    for name in ("test", "lint", "build"):
        value = commands.get(name)
        lines.append(status_line(bool(value) if name == "test" else (True if value else None), f"project {name} command", value or "not recorded"))
    resume_notes = readiness.get("resume_notes") or []
    lines.append(status_line(True if resume_notes else None, "resume notes", f"{len(resume_notes)} found" if resume_notes else "none"))

    # Worker model currency. Today the cursor probe is the only one
    # implemented. Other workers (codex/grok/claude/claude-code-cli-acp)
    # would each need their own model-discovery: codex doesn't expose a
    # native model-list command, claude's model is set per-Agent dispatch, etc.
    # Surface cursor explicitly; flag the others as not-yet-probed so the
    # user knows the doctor's currency check is partial.
    cursor_models = payload["cursor"].get("models") or {}
    leading = cursor_models.get("leading_internal")
    current = cursor_models.get("current_user_model")
    if cursor_models.get("user_behind") is True:
        lines.append(status_line(
            False,
            "cursor model currency",
            f"on {current}; leading internal is {leading}. "
            f"Edit ~/.cursor/cli-config.json model.modelId to {leading} for sharper output."
        ))
    elif cursor_models.get("user_behind") is False:
        lines.append(status_line(True, "cursor model currency", f"on {current} (leading internal)"))
    else:
        lines.append(status_line(None, "cursor model currency", "could not determine — manual check needed"))
    # Worker CLI currency (proxy for model currency — new models ship with
    # new CLI releases). Surface only when behind; silent when current to
    # avoid being a nag.
    wc = payload.get("worker_currency") or {}
    behind_workers = []
    for name, entry in sorted(wc.items()):
        if entry.get("behind") is True:
            current = entry.get("current", "?")
            latest = entry.get("latest", "?")
            behind_workers.append(f"{name} ({current} → {latest})")
    # Verified-current and probe-failed workers go on DIFFERENT lines.
    # Earlier version lumped probe-failed entries under [OK] "treated as
    # current"; codex-r4 RECOMMENDED #2 correctly pointed out that an
    # npm timeout / 404 shouldn't render as healthy. Now [OK] is only
    # for behind=False (explicitly verified); probe-failed gets its own
    # [INFO] line.
    if behind_workers:
        lines.append(status_line(
            False,
            "worker CLI currency",
            f"behind: {'; '.join(behind_workers)}. Run /goal-flight update."
        ))
    verified_current = sorted(n for n, e in wc.items() if e.get("behind") is False)
    if verified_current and not behind_workers:
        lines.append(status_line(True, "worker CLI currency", f"current: {', '.join(verified_current)}"))
    probe_failed = sorted(
        n for n, e in wc.items()
        if e.get("current") and e.get("behind") is None
    )
    if probe_failed:
        lines.append(status_line(
            None,
            "worker currency probe",
            f"could not verify (npm/grok registry unreachable or 404): {', '.join(probe_failed)}"
        ))
    if not (behind_workers or verified_current or probe_failed):
        lines.append(status_line(None, "worker CLI currency", "no workers probed"))

    # Rate-limit pressure summary — the orchestrator's responsibility is to
    # not overheat services in a way that crashes the live session.
    rp = payload.get("rate_pressure") or {}
    pressured = rp.get("providers_under_pressure") or []
    if pressured:
        for entry in pressured:
            provider = entry.get("provider")
            count = entry.get("count")
            fallback = ",".join(entry.get("fallback_providers", []))
            lines.append(status_line(
                False,
                f"rate-pressure {provider}",
                f"count={count} in 10min window. Recommended caps {entry.get('recommended_caps')}. "
                f"Fallback providers: {fallback or 'none configured'}."
            ))
    else:
        records = rp.get("records_examined", 0)
        lines.append(status_line(True, "rate-pressure", f"no provider under pressure ({records} records examined)"))

    for line in lines[:80]:
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight doctor")
    parser.add_argument("--project-root", default=os.getcwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--fleet-reconcile-stale",
        action="store_true",
        help="Release stale capacity leases and expired account locks before reporting",
    )
    parser.add_argument(
        "--fleet",
        action="store_true",
        help="Include fleet auth probe summary (nodes[].accounts[].auth_probe)",
    )
    parser.add_argument(
        "--fleet-dir",
        type=Path,
        help="Fleet store directory for --fleet (default ~/.goal-flight/fleet)",
    )
    parser.add_argument(
        "--fleet-probe",
        action="store_true",
        help="Refresh auth probes when used with --fleet",
    )
    parser.add_argument(
        "--worker-write-probe",
        action="store_true",
        help="Run a real worker dispatch that writes a file and emits a terminal marker",
    )
    parser.add_argument(
        "--write-probe-agent",
        default="grok-code",
        help="Agent for --worker-write-probe (default: grok-code)",
    )
    args = parser.parse_args(argv)
    if args.fleet_reconcile_stale and goalflight_fleet is not None:
        import goalflight_fleet_stale as fleet_stale

        fleet_stale.doctor_fleet_stale_release(goalflight_fleet.default_fleet_dir(), mutate=True)
    fleet_dir = args.fleet_dir
    if fleet_dir is None and args.fleet and goalflight_fleet is not None:
        fleet_dir = goalflight_fleet.default_fleet_dir()
    payload = doctor(
        Path(args.project_root).resolve(),
        fleet=args.fleet,
        fleet_dir=fleet_dir,
        fleet_probe=args.fleet_probe,
        worker_write_probe=args.worker_write_probe,
        write_probe_agent=args.write_probe_agent,
    )
    if args.fleet_reconcile_stale:
        payload["fleet_reconcile"] = _fleet_reconcile_summary(release_stale=True)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print_human(payload)
    plugin = payload["plugin"]
    if not plugin.get("skipped") and (
        plugin.get("manifest_exists") is False or plugin.get("validate_ok") is False
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
