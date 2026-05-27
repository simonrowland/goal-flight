#!/usr/bin/env python3
"""Discover installed controller hosts and usable transports.

Emits compact JSON for test harness skip/run decisions. Does not invoke models.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

HOST_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOST_DIR.parents[2]
sys.path.insert(0, str(HOST_DIR))

from common import REPO_ROOT, SCHEMA  # noqa: E402


def _codex_plugin_installed() -> bool:
    home = Path.home()
    cache = home / ".codex/plugins/cache/goal-flight/goal-flight"
    if cache.is_dir() and any(cache.glob("*/.codex-plugin/plugin.json")):
        return True
    return (home / ".codex/skills/goal-flight/SKILL.md").is_file()


def _opencode_skill_installed() -> bool:
    home = Path.home()
    if (home / ".config/opencode/skills/goal-flight/SKILL.md").is_file():
        return True
    return (REPO_ROOT / "configs/opencode/skills/goal-flight/SKILL.md").is_file()


def _grok_skill_installed() -> bool:
    return (Path.home() / ".grok/skills/goal-flight/SKILL.md").is_file()


def _cursor_skill_installed() -> bool:
    home = Path.home()
    if (home / ".cursor/skills/goal-flight/SKILL.md").is_file():
        return True
    return (REPO_ROOT / "configs/cursor/skills/goal-flight/SKILL.md").is_file()


def probe_controller(controller_id: str) -> dict[str, Any]:
    probes: dict[str, dict[str, Any]] = {
        "codex": _probe_codex,
        "claude-acp": _probe_claude_acp,
        "opencode": _probe_opencode,
        "grok": _probe_grok,
        "cursor": _probe_cursor,
    }
    fn = probes.get(controller_id)
    if fn is None:
        return {
            "id": controller_id,
            "available": False,
            "transports": [],
            "skip_reason": "unknown controller id",
        }
    return fn()


def _probe_codex() -> dict[str, Any]:
    binary = shutil.which("codex")
    if not binary:
        return {
            "id": "codex",
            "available": False,
            "transports": [],
            "skip_reason": "codex not on PATH",
        }
    reasons: list[str] = []
    if not _codex_plugin_installed():
        reasons.append("goal-flight codex plugin/skill not installed")
    return {
        "id": "codex",
        "available": True,
        "binary": binary,
        "transports": ["bash_tail"],
        "skill_installed": _codex_plugin_installed(),
        "skip_reason": "; ".join(reasons) if reasons else None,
        "notes": "behavior harness uses codex exec bash-tail; auth required at runtime",
    }


def _probe_claude_acp() -> dict[str, Any]:
    binary = shutil.which("claude-code-cli-acp")
    if not binary:
        return {
            "id": "claude-acp",
            "available": False,
            "transports": [],
            "skip_reason": "claude-code-cli-acp not on PATH",
        }
    return {
        "id": "claude-acp",
        "available": True,
        "binary": binary,
        "transports": ["acp"],
        "skip_reason": None,
        "notes": "billing-safe controller regression path; do not use claude -p",
    }


def _probe_opencode() -> dict[str, Any]:
    binary = shutil.which("opencode")
    if not binary:
        return {
            "id": "opencode",
            "available": False,
            "transports": [],
            "skip_reason": "opencode not on PATH",
        }
    transports = ["acp", "bash_tail"]
    return {
        "id": "opencode",
        "available": True,
        "binary": binary,
        "transports": transports,
        "skill_installed": _opencode_skill_installed(),
        "skip_reason": None,
    }


def _probe_grok() -> dict[str, Any]:
    binary = shutil.which("grok")
    if not binary:
        return {
            "id": "grok",
            "available": False,
            "transports": [],
            "skip_reason": "grok not on PATH",
        }
    return {
        "id": "grok",
        "available": True,
        "binary": binary,
        "transports": ["bash_tail"],
        "skill_installed": _grok_skill_installed(),
        "skip_reason": None,
    }


def _probe_cursor() -> dict[str, Any]:
    binary = shutil.which("cursor-agent")
    if not binary:
        return {
            "id": "cursor",
            "available": False,
            "transports": [],
            "skip_reason": "cursor-agent not on PATH",
        }
    return {
        "id": "cursor",
        "available": True,
        "binary": binary,
        "transports": ["acp"],
        "skill_installed": _cursor_skill_installed(),
        "skip_reason": None,
    }


def build_probe_matrix(*, controllers: list[str] | None = None) -> dict[str, Any]:
    ids = controllers or ["codex", "claude-acp", "opencode", "grok", "cursor"]
    rows = {cid: probe_controller(cid) for cid in ids}
    available = [cid for cid, row in rows.items() if row.get("available")]
    return {
        "schema": SCHEMA,
        "kind": "probe_matrix",
        "repo_root": str(REPO_ROOT),
        "controllers": rows,
        "available_controllers": available,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Goal Flight controller probe matrix")
    parser.add_argument("--controller", action="append", dest="controllers", help="Limit to controller id(s)")
    parser.add_argument("--list-available", action="store_true", help="Print available controller ids, one per line")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    payload = build_probe_matrix(controllers=args.controllers)
    if args.list_available:
        for cid in payload["available_controllers"]:
            print(cid)
        return 0

    emit_json = args.json or not sys.stdout.isatty()
    if emit_json:
        print(json.dumps(payload, indent=2))
    else:
        for cid, row in payload["controllers"].items():
            mark = "yes" if row.get("available") else "no"
            transports = ",".join(row.get("transports") or []) or "-"
            print(f"{cid}: available={mark} transports={transports} skip={row.get('skip_reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
