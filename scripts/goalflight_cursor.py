"""Cursor worker MCP isolation, shared by text and ACP launch boundaries."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path


def context_mode_enabled(env: dict[str, str]) -> bool:
    return env.get("GOALFLIGHT_CURSOR_CONTEXT_MODE", "").strip().lower() in {
        "1", "true", "yes", "enabled", "on",
    }


def isolate_context_mode(
    agent: str, env: dict[str, str], *, cwd: str,
) -> None:
    """Disable only context-mode in private Cursor project data.

    Cursor 2026.09.18 uses CURSOR_DATA_DIR/projects/<workspace slug> for its
    disabled store, independently of HOME-based global/project/plugin MCP
    definitions. Its plugin identifier is plugin-<plugin name>-<server name>.
    Keep HOME/config untouched (including login), and copy MCP approvals/auth
    so unrelated servers retain their capabilities. No checkout files change.
    """
    if agent not in {"cursor", "cursor-agent"} or context_mode_enabled(env):
        return
    root = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    workspace = root.stdout.strip() if root.returncode == 0 else str(Path(cwd).resolve())
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", workspace).strip("-")
    source_data = Path(env.get("CURSOR_DATA_DIR") or Path(env.get("HOME") or Path.home()) / ".cursor")
    source = source_data / "projects" / slug
    # Temp is already writable under the worker's OS sandbox. In particular,
    # a custom dispatch ledger directory need not be writable by the worker.
    data = Path(tempfile.mkdtemp(prefix="goalflight-cursor-data-"))
    project = data / "projects" / slug
    project.mkdir(parents=True, mode=0o700)
    for name in ("mcp-auth.json", "mcp-approvals.json", "mcp-group-selections.json"):
        try:
            content = (source / name).read_bytes()
        except FileNotFoundError:
            continue
        (project / name).write_bytes(content)
        (project / name).chmod(0o600)
    try:
        disabled = json.loads((source / "mcp-disabled.json").read_text())
    except FileNotFoundError:
        disabled = []
    if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled):
        raise ValueError(f"Invalid Cursor MCP disabled list: {source / 'mcp-disabled.json'}")
    for identifier in ("context-mode", "plugin-context-mode-context-mode"):
        if identifier not in disabled:
            disabled.append(identifier)
    (project / "mcp-disabled.json").write_text(json.dumps(disabled) + "\n")
    env["CURSOR_DATA_DIR"] = str(data)
