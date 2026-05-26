#!/usr/bin/env python3
"""Register Goal Flight OpenCode config: context-mode MCP and skill permissions.

OpenCode reads MCP servers from JSON config:

- global: ~/.config/opencode/opencode.json
- project: <project>/opencode.json

This helper is idempotent. It never starts context-mode and never contacts npm;
it only merges the checked-in fragment and context-mode MCP entry.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SERVER_NAME = "context-mode"
HOST_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOST_DIR.parents[2]
FRAGMENT_PATH = REPO_ROOT / "configs/opencode/opencode.json"


def _config_path(scope: str, project_root: Path) -> Path:
    if scope == "global":
        return Path.home() / ".config/opencode/opencode.json"
    return project_root / "opencode.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: {path} must contain a JSON object")
    return data


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _command_resolves(command: Any) -> bool:
    if isinstance(command, list) and command:
        first = command[0]
        if isinstance(first, str):
            path = Path(first).expanduser()
            if path.is_absolute() or "/" in first:
                return path.exists()
            return shutil.which(first) is not None
    if isinstance(command, str) and command:
        path = Path(command).expanduser()
        if path.is_absolute() or "/" in command:
            return path.exists()
        return shutil.which(command) is not None
    return False


def _default_mcp_server() -> dict[str, Any]:
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("ERROR: npx is required before writing the OpenCode context-mode MCP entry")
    return {
        "type": "local",
        "command": [npx, "-y", "context-mode@latest"],
        "enabled": True,
    }


def _existing_context_mode(data: dict[str, Any]) -> dict[str, Any] | None:
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return None
    server = mcp.get(SERVER_NAME)
    return server if isinstance(server, dict) else None


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    shutil.copy2(path, backup)
    return backup


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _load_fragment() -> dict[str, Any]:
    if not FRAGMENT_PATH.exists():
        return {}
    try:
        data = json.loads(FRAGMENT_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: invalid fragment JSON in {FRAGMENT_PATH}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _needs_write(data: dict[str, Any], merged: dict[str, Any]) -> bool:
    return merged != data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("global", "project"), default="global")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-sidecar-backup", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).expanduser().resolve()
    path = _config_path(args.scope, project_root)
    data = _load_json(path)
    fragment = _load_fragment()
    merged = _deep_merge(data, fragment)

    existing = _existing_context_mode(merged)
    if existing is None or not _command_resolves(existing.get("command")):
        mcp = merged.setdefault("mcp", {})
        if not isinstance(mcp, dict):
            raise SystemExit(f"ERROR: {path} mcp must be an object")
        mcp[SERVER_NAME] = _default_mcp_server()

    if not _needs_write(data, merged):
        print(f"opencode: goal-flight config already current in {path}")
        return 0

    if args.check:
        print(f"CHECK: opencode missing goal-flight config updates in {path}")
        if shutil.which("npx") is None:
            print("CHECK: npx is missing; install Node.js/npm before registration")
        return 1

    backup = None if args.no_sidecar_backup else _backup(path)
    _atomic_write(path, merged)
    print(f"registered goal-flight OpenCode config in {path}")
    if backup:
        print(f"  prior config backed up to {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
