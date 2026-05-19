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

try:
    import goalflight_rate_pressure
except Exception:  # pragma: no cover - doctor still reports partial state
    goalflight_rate_pressure = None


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


def cursor_models_probe() -> dict:
    """Probe cursor-agent for available models and pick the leading internal one.

    Discovery mechanism for Cursor's "domestic" (internal-tier) models —
    composer-*. Avoids hardcoding model names in docs that age fast.

    Cursor exposes models via `cursor-agent --list-models`. Output format:

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
    if not shutil.which("cursor-agent"):
        return out
    result = run(["cursor-agent", "--list-models"], timeout=10)
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
        "cursor": {
            "desktop_present": cursor_desktop,
            "cli": version("cursor", "--version"),
            "agent": version("cursor-agent", "--version"),
            "models": cursor_models_probe(),
        },
        "grok": check_grok(),
        "acp": check_acp(),
        "project": git_state(repo),
        "capacity": goalflight_capacity.profile(argparse.Namespace()) if goalflight_capacity else None,
        "rate_pressure": _rate_pressure_summary(),
    }
    return payload


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
        pressure = goalflight_rate_pressure.pressure_per_provider(records)
        current_caps = dict(goalflight_capacity.DEFAULT_AGENT_CAPS) if goalflight_capacity else {}
        rec = goalflight_rate_pressure.recommend(pressure, current_caps)
        rec["records_examined"] = len(records)
        rec["state_dir"] = str(state_dir)
        return rec
    except Exception as exc:  # pragma: no cover - keep doctor resilient
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


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

    # Worker model currency. Today the cursor probe is the only one
    # implemented. Other workers (codex/grok/claude/claude-code-cli-acp)
    # would each need their own model-discovery: codex doesn't expose a
    # native --list-models, claude's model is set per-Agent dispatch, etc.
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
    lines.append(status_line(
        None,
        "other-worker model currency",
        "codex/grok/claude model-discovery not yet probed; run `<cli> update --check` manually"
    ))

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
