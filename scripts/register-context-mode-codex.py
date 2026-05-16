#!/usr/bin/env python3
"""register-context-mode-codex.py — register the context-mode MCP server on codex.

Why this exists
---------------
context-mode (https://github.com/simonrowland/context-mode) is the FTS5 large-
output sandbox that makes 12-hour unattended goal-flight runs feasible. It's
typically installed Claude-side as a plugin; codex needs the same MCP server
registered in ~/.codex/config.toml under [mcp_servers.context-mode]. Doing
that registration by hand is fiddly — escaping rules, plugin-form variable
resolution, idempotency. This script does it for you.

What this script does
---------------------
1. Detects whether context-mode is installed Claude-side (mcpServers entry
   in ~/.claude.json / ~/.claude/settings.json, OR plugin form under
   ~/.claude/plugins/).
2. Checks whether [mcp_servers.context-mode] is ALREADY registered in
   ~/.codex/config.toml. If so, no-op (preserves the user's chosen command —
   they may have intentionally configured a different launch).
3. If Claude has it but codex doesn't, appends the canonical codex
   registration (`npx -y context-mode@latest`) to ~/.codex/config.toml.
   This is the recommended form per context-mode's docs and avoids the
   ${CLAUDE_PLUGIN_ROOT} variable that Claude's plugin form embeds (codex
   doesn't expand that variable).

Idempotency + safety:
- Backs up existing ~/.codex/config.toml with a collision-resistant suffix.
- Uses flock + atomic rename for concurrent-init safety.
- Exits silently if codex isn't installed (no ~/.codex/ to mutate).

Usage
-----
  register-context-mode-codex.py           # detect + register if needed
  register-context-mode-codex.py --check   # report state, write nothing
  register-context-mode-codex.py --help

Re-running is safe — duplicates are not created.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def claude_has_context_mode() -> str | None:
    """Return a one-line provenance string if context-mode is detected Claude-side, else None."""
    # Explicit mcpServers entries
    for src in [Path.home() / ".claude.json", Path.home() / ".claude/settings.json"]:
        if not src.exists():
            continue
        try:
            cfg = json.loads(src.read_text())
        except json.JSONDecodeError:
            continue
        if cfg.get("mcpServers", {}).get("context-mode"):
            return f"mcpServers entry in {src}"
    # Plugin form — presence of a plugin.json under ~/.claude/plugins/**/context-mode is enough
    plugins_root = Path.home() / ".claude/plugins"
    if plugins_root.exists():
        for plugin_json in plugins_root.rglob("plugin.json"):
            # Path-match on "context-mode" anywhere in the install path
            if "context-mode" not in str(plugin_json):
                continue
            try:
                cfg = json.loads(plugin_json.read_text())
            except json.JSONDecodeError:
                continue
            if cfg.get("mcpServers", {}).get("context-mode") or cfg.get("name") == "context-mode":
                return f"plugin form at {plugin_json.parent}"
    return None


def codex_already_registered(codex_config: Path) -> bool:
    if not codex_config.exists():
        return False
    content = codex_config.read_text()
    # Both bare-key and quoted-key forms are valid TOML; match either.
    return (
        "[mcp_servers.context-mode]" in content
        or '[mcp_servers."context-mode"]' in content
    )


def render_block() -> str:
    """Render the canonical codex registration for context-mode."""
    npx = shutil.which("npx") or "/usr/bin/env npx"
    # All values here are controlled by this script (not user input), so explicit
    # TOML basic-string escaping suffices. npx path is an absolute filesystem path
    # without TOML-special characters in any sane setup.
    return (
        "\n"
        "[mcp_servers.context-mode]\n"
        f'command = "{npx}"\n'
        'args = ["-y", "context-mode@latest"]\n'
    )


def append_atomically(codex_config: Path, block: str) -> Path:
    """Append `block` to `codex_config`, mutex'd against concurrent runs. Returns backup path."""
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    lock_path = codex_config.parent / ".register-context-mode.lock"
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Re-check under lock — another runner may have written it.
        if codex_already_registered(codex_config):
            return Path()  # signal: no-op (someone else wrote it)
        backup = Path()
        if codex_config.exists():
            ts = time.strftime("%Y%m%d-%H%M%S")
            backup_fd, backup_name = tempfile.mkstemp(
                prefix=f"config.toml.bak.{ts}.",
                dir=str(codex_config.parent),
            )
            os.close(backup_fd)
            shutil.copy2(codex_config, backup_name)
            backup = Path(backup_name)
        existing = codex_config.read_text() if codex_config.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        new_content = existing + block
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix="config.toml.new.",
            dir=str(codex_config.parent),
        )
        with os.fdopen(tmp_fd, "w") as f:
            f.write(new_content)
        os.replace(tmp_name, codex_config)
        return backup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Register context-mode MCP on codex side. Idempotent."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report state; write nothing. Exit 0 if registered or codex absent; 1 if missing.",
    )
    args = parser.parse_args(argv)

    if not shutil.which("codex"):
        print("codex not installed; nothing to mirror to. skipping.")
        return 0

    codex_config = Path.home() / ".codex/config.toml"

    if codex_already_registered(codex_config):
        print(
            f"codex: [mcp_servers.context-mode] already in {codex_config}; no-op."
        )
        return 0

    provenance = claude_has_context_mode()
    if not provenance:
        msg = (
            "context-mode not detected Claude-side. "
            "Install upstream (see https://github.com/simonrowland/context-mode) "
            "then re-run."
        )
        if args.check:
            print(f"CHECK: codex missing [mcp_servers.context-mode] AND {msg.lower()}")
            return 1
        print(msg)
        return 0

    if args.check:
        print(
            f"CHECK: codex MISSING [mcp_servers.context-mode]; "
            f"Claude has it ({provenance}). Run without --check to register."
        )
        return 1

    block = render_block()
    backup = append_atomically(codex_config, block)
    if backup == Path():
        print("raced: another runner registered it under lock. no-op.")
        return 0
    print(f"registered context-mode for codex in {codex_config}")
    if backup.name:
        print(f"  prior config backed up to {backup}")
    print(f"  detected Claude-side via: {provenance}")
    print("  block written:")
    for line in block.strip().splitlines():
        print(f"    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
