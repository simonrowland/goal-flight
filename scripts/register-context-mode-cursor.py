#!/usr/bin/env python3
"""Register the context-mode MCP server for Cursor.

Cursor reads MCP servers from JSON, not Codex TOML:

- global: ~/.cursor/mcp.json
- project: <project>/.cursor/mcp.json

This helper is intentionally idempotent. It never starts context-mode and never
contacts the npm registry; it only writes the MCP config block Cursor will load.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any


SERVER_NAME = "context-mode"
SERVER_ARGS = ["-y", "context-mode@latest"]


def _config_path(scope: str, project_root: Path) -> Path:
    if scope == "global":
        return Path.home() / ".cursor/mcp.json"
    return project_root / ".cursor/mcp.json"


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


def _has_server(data: dict[str, Any]) -> bool:
    servers = data.get("mcpServers")
    return isinstance(servers, dict) and SERVER_NAME in servers


def _server(data: dict[str, Any]) -> dict[str, Any] | None:
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    server = servers.get(SERVER_NAME)
    return server if isinstance(server, dict) else None


def _command_resolves(command: Any) -> bool:
    if not isinstance(command, str) or not command:
        return False
    path = Path(command).expanduser()
    if path.is_absolute() or "/" in command:
        return path.exists()
    return shutil.which(command) is not None


def _default_server() -> dict[str, Any]:
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("ERROR: npx is required before writing the Cursor context-mode MCP entry")
    return {"command": npx, "args": list(SERVER_ARGS)}


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
    existing_server = _server(data)
    if existing_server is not None and _command_resolves(existing_server.get("command")):
        print(f"cursor: {SERVER_NAME} already registered in {path}")
        return 0
    if existing_server is not None and args.check:
        print(f"CHECK: cursor {SERVER_NAME} MCP command is not executable in {path}")
        return 1

    if args.check:
        print(f"CHECK: cursor missing {SERVER_NAME} MCP server in {path}")
        if shutil.which("npx") is None:
            print("CHECK: npx is missing; install Node.js/npm before registration")
        return 1

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"ERROR: {path} mcpServers must be an object")
    servers[SERVER_NAME] = _default_server()
    backup = None if args.no_sidecar_backup else _backup(path)
    _atomic_write(path, data)
    print(f"registered {SERVER_NAME} for cursor in {path}")
    if backup:
        print(f"  prior config backed up to {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
