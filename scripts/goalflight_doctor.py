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
import shutil
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import goalflight_capacity
except Exception:  # pragma: no cover - doctor still reports partial state
    goalflight_capacity = None


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
    if not path:
        return {"present": False}
    result = run([path, *args], timeout=4)
    return {"present": True, "path": path, "version": first_line(result["stdout"] or result["stderr"]), "ok": result["ok"]}


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
    out = {"manifest": str(manifest), "manifest_exists": manifest.exists()}
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


def check_context_mode(repo: Path) -> dict:
    script = repo / "scripts/register-context-mode-codex.py"
    out = {"register_script": str(script), "register_script_exists": script.exists()}
    if script.exists():
        result = run(["python3", str(script), "--check"], cwd=repo, timeout=10)
        out.update({"check_returncode": result["returncode"], "check_ok": result["ok"], "stderr": result["stderr"][:500]})
    out["npx_present"] = bool(shutil.which("npx"))
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


def check_acp() -> dict:
    grok = check_grok()
    return {
        "codex-acp": {"present": bool(shutil.which("codex-acp"))},
        "cursor-agent": {"present": bool(shutil.which("cursor-agent")), "version": version("cursor-agent", "--version").get("version")},
        "claude-code-cli-acp": {"present": bool(shutil.which("claude-code-cli-acp"))},
        "grok-agent-stdio": {"present": grok["present"], "headless_hint": grok.get("headless_flags")},
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


def doctor(repo: Path) -> dict:
    codex_desktop = app_exists("Codex", "com.openai.codex")
    codex_cli = version("codex", "--version")
    cursor_desktop = app_exists("Cursor", "com.todesktop.230313mzl4w4u92")
    payload = {
        "schema": "goalflight.doctor.v1",
        "repo": str(repo),
        "plugin": check_plugin(repo),
        "claude": version("claude", "--version"),
        "codex": {
            "desktop_present": codex_desktop,
            "cli": codex_cli,
            "desktop_without_cli": bool(codex_desktop and not codex_cli.get("present")),
            "install_hint": "npm install -g @openai/codex && codex login" if codex_desktop and not codex_cli.get("present") else None,
        },
        "context_mode": check_context_mode(repo),
        "gstack": version("gstack", "--version"),
        "cursor": {"desktop_present": cursor_desktop, "cli": version("cursor", "--version"), "agent": version("cursor-agent", "--version")},
        "grok": check_grok(),
        "acp": check_acp(),
        "project": git_state(repo),
        "capacity": goalflight_capacity.profile(argparse.Namespace()) if goalflight_capacity else None,
    }
    return payload


def status_line(ok: bool | None, label: str, detail: str | None = None) -> str:
    prefix = "[INFO]" if ok is None else ("[OK]" if ok else "[WARN]")
    return f"{prefix} {label}" + (f" — {detail}" if detail else "")


def print_human(payload: dict) -> None:
    lines = [
        status_line(payload["plugin"].get("manifest_exists"), "plugin manifest", payload["plugin"].get("manifest")),
        status_line(payload["plugin"].get("validate_ok"), "claude plugin validate", payload["plugin"].get("validate_first_line")),
        status_line(payload["claude"].get("present"), "claude CLI", payload["claude"].get("version")),
        status_line(payload["codex"]["cli"].get("present"), "codex CLI", payload["codex"]["cli"].get("version")),
        status_line(not payload["codex"].get("desktop_without_cli"), "Codex Desktop/CLI pairing", payload["codex"].get("install_hint")),
        status_line(
            payload["context_mode"].get("register_script_exists") and payload["context_mode"].get("check_returncode") == 0,
            "context-mode registration probe",
            f"return={payload['context_mode'].get('check_returncode')} script_exists={payload['context_mode'].get('register_script_exists')}",
        ),
        status_line(payload["gstack"].get("present"), "gstack", payload["gstack"].get("version")),
        status_line(payload["cursor"].get("desktop_present"), "Cursor Desktop", None),
        status_line(payload["cursor"]["agent"].get("present"), "cursor-agent ACP", payload["cursor"]["agent"].get("version")),
        status_line(payload["grok"].get("present"), "Grok Build binary", payload["grok"].get("version")),
        status_line(payload["grok"].get("headless_flags"), "Grok headless flags", None),
    ]
    for name, item in payload["acp"].items():
        lines.append(status_line(item.get("present"), f"ACP {name}", item.get("version")))
    cap = payload.get("capacity") or {}
    lines.append(status_line(True, "capacity profile", f"operating={cap.get('operating_cap')} raw={cap.get('raw_ram_ceiling')} ram={cap.get('ram_mb')}MB"))
    project = payload["project"]
    lines.append(status_line(project.get("present"), "git project", f"{project.get('branch')} {project.get('head')} dirty={project.get('dirty')}"))
    for line in lines[:80]:
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight doctor")
    parser.add_argument("--project-root", default=os.getcwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = doctor(Path(args.project_root).resolve())
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print_human(payload)
    if not payload["plugin"].get("manifest_exists") or payload["plugin"].get("validate_ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
