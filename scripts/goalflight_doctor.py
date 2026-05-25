#!/usr/bin/env python3
"""Procedural doctor for goal-flight.

The model should read this script's compact JSON or checklist instead of
hand-running a long environment probe sequence into the context window.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

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


def run(cmd: list[str], cwd: Path | None = None, timeout: float = 8.0) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
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
    if not path:
        return {"present": False}
    result = run([path, *args], timeout=4)
    return {"present": True, "path": path, "version": first_line(result["stdout"] or result["stderr"]), "ok": result["ok"]}


def check_gstack() -> dict:
    cli = version("gstack", "--version")
    if cli.get("present"):
        cli["kind"] = "cli"
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
        }

    return {"present": False}


def app_exists(name: str, bundle_id: str | None = None) -> bool:
    direct = Path("/Applications") / f"{name}.app"
    if direct.exists():
        return True
    if bundle_id and shutil.which("mdfind"):
        result = run(["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"], timeout=3)
        return bool(result["stdout"])
    return False


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


def check_context_mode(repo: Path) -> dict:
    script = repo / "scripts/register-context-mode-codex.py"
    out = {"register_script": str(script), "register_script_exists": script.exists()}
    if script.exists():
        result = run(["python3", str(script), "--check"], cwd=repo, timeout=10)
        out.update({"check_returncode": result["returncode"], "check_ok": result["ok"], "stderr": result["stderr"][:500]})
    out["npx_present"] = bool(shutil.which("npx"))
    return out


def check_cursor_context_mode(skill_root: Path, project_root: Path) -> dict:
    script = skill_root / "scripts/register-context-mode-cursor.py"
    out = {"register_script": str(script), "register_script_exists": script.exists()}
    if not script.exists():
        return out
    global_result = run(["python3", str(script), "--scope", "global", "--project-root", str(project_root), "--check"], timeout=10)
    project_result = run(["python3", str(script), "--scope", "project", "--project-root", str(project_root), "--check"], timeout=10)
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
    script = skill_root / "scripts/register-context-mode-opencode.py"
    out = {"register_script": str(script), "register_script_exists": script.exists()}
    if not script.exists():
        return out
    global_result = run(["python3", str(script), "--scope", "global", "--project-root", str(project_root), "--check"], timeout=10)
    project_result = run(["python3", str(script), "--scope", "project", "--project-root", str(project_root), "--check"], timeout=10)
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
            "present": (SCRIPT_DIR / "opencode_bash_tail.py").is_file(),
            "script": str(SCRIPT_DIR / "opencode_bash_tail.py"),
        },
    }


def check_acp_sdk() -> dict:
    python = Path.home() / ".goal-flight/venvs/acp-0.10/bin/python"
    hint = "SDK missing -- run install: /init installs agent-client-protocol into ~/.goal-flight/venvs/acp-0.10/"
    runner = SCRIPT_DIR / "goalflight_acp_run.py"
    system_python = shutil.which("python3") or "python3"
    runner_help = run([system_python, str(runner), "--help"], timeout=8)
    runner_reexec_ok = runner_help["ok"] and "--agent" in runner_help.get("stdout", "")
    if not python.exists():
        return {
            "ok": False,
            "python": str(python),
            "error": hint,
            "runner_reexec_ok": runner_reexec_ok,
            "runner_reexec_detail": f"python3 --help rc={runner_help['returncode']}",
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
            "runner_reexec_detail": f"python3 --help rc={runner_help['returncode']}",
        }
    return {
        "ok": True,
        "python": str(python),
        "version": "agent-client-protocol==0.10.*",
        "runner_reexec_ok": runner_reexec_ok,
        "runner_reexec_detail": f"python3 --help rc={runner_help['returncode']}",
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


def _agent_instructions(repo: Path) -> tuple[Path | None, str]:
    for name in ("AGENTS.md", "AGENT.md"):
        path = repo / name
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
    python = shutil.which("python3") or "python3"
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


def doctor(repo: Path, *, fleet: bool = False, fleet_dir: Path | None = None, fleet_probe: bool = False) -> dict:
    skill_root = SCRIPT_DIR.parent
    codex_desktop = app_exists("Codex", "com.openai.codex")
    codex_cli = version("codex", "--version")
    cursor_desktop = app_exists("Cursor", "com.todesktop.230313mzl4w4u92")
    payload = {
        "schema": "goalflight.doctor.v1",
        "repo": str(repo),
        "plugin": check_plugin(repo),
        "host_goalflight_install": check_host_goalflight_install(),
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
        "cursor": {
            "desktop_present": cursor_desktop,
            "cli": version("cursor", "--version"),
            "agent": version("cursor-agent", "--version"),
            "models": cursor_models_probe(),
        },
        "opencode": version("opencode", "--version"),
        "grok": check_grok(),
        "acp": check_acp(),
        "project": git_state(repo),
        "project_goalflight_readiness": check_project_goalflight_readiness(repo),
        "router": check_router(repo),
        "capacity": goalflight_capacity.profile(argparse.Namespace()) if goalflight_capacity else None,
        "fleet_reconcile": _fleet_reconcile_summary(),
        "rate_pressure": _rate_pressure_summary(),
        "worker_currency": worker_currency_probe(),
    }
    if fleet:
        payload["fleet"] = _fleet_auth_summary(fleet_dir, refresh=fleet_probe)
        payload["fleet_dispatches"] = _fleet_dispatch_report(fleet_dir)
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


def _rate_pressure_summary() -> dict:
    """Compact rate-pressure summary for doctor output.

    The controller's job is to not overheat provider services in a way
    that takes the live session down. This surfaces the same probe
    `goalflight_rate_pressure.py` emits, so the doctor is a one-stop
    "is everything OK" check.
    """
    if goalflight_rate_pressure is None:
        return {"available": False, "reason": "goalflight_rate_pressure import failed"}
    try:
        state_dir = Path(os.environ.get("GOALFLIGHT_STATE_DIR", f"/tmp/goal-flight-{os.getuid()}"))
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
        status_line(payload["gstack"].get("present"), "gstack", payload["gstack"].get("version")),
        status_line(payload["cursor"].get("desktop_present"), "Cursor Desktop", None),
        status_line(payload["cursor"]["agent"].get("present"), "cursor-agent ACP", payload["cursor"]["agent"].get("version")),
        status_line(payload["opencode"].get("present"), "opencode ACP", payload["opencode"].get("version")),
        status_line(payload["grok"].get("present"), "Grok Build binary", payload["grok"].get("version")),
        status_line(payload["grok"].get("headless_flags"), "Grok headless flags", None),
    ]
    for host, item in (payload.get("host_goalflight_install") or {}).items():
        lines.append(status_line(item.get("ok"), f"{host} goal-flight host install", item.get("detail")))
    for name, item in payload["acp"].items():
        if name == "sdk":
            lines.append(status_line(item.get("ok"), "ACP SDK venv", item.get("python")))
            lines.append(status_line(item.get("runner_reexec_ok"), "ACP runner python3 re-exec", item.get("runner_reexec_detail")))
        else:
            lines.append(status_line(item.get("present"), f"ACP {name}", item.get("version")))
    cap = payload.get("capacity") or {}
    lines.append(status_line(True, "capacity profile", f"operating={cap.get('operating_cap')} raw={cap.get('raw_ram_ceiling')} ram={cap.get('ram_mb')}MB"))
    project = payload["project"]
    lines.append(status_line(project.get("present"), "git project", f"{project.get('branch')} {project.get('head')} dirty={project.get('dirty')}"))
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

    # Rate-limit pressure summary — the controller's responsibility is to
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
