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
   in ~/.claude.json / ~/.claude/settings.json, OR plugin form with an
   mcpServers entry inside any plugin.json under ~/.claude/plugins/).
2. Checks whether [mcp_servers.context-mode] is ALREADY registered in
   ~/.codex/config.toml via tomllib parse (handles bracket-table form,
   quoted-key form, AND inline-table form; ignores commented-out blocks).
   If present, no-op (preserves the user's chosen command — they may have
   intentionally configured a different launch).
3. If Claude has it but codex doesn't, appends the canonical codex
   registration (`npx -y context-mode@latest`) to ~/.codex/config.toml.
   This is the recommended form per context-mode's docs and avoids the
   ${CLAUDE_PLUGIN_ROOT} variable that Claude's plugin form embeds (codex
   doesn't expand that variable).

Idempotency + safety:
- Backs up existing ~/.codex/config.toml with a collision-resistant suffix.
- Uses flock + atomic rename for concurrent-init safety.
- Exits silently if codex isn't installed (no ~/.codex/ to mutate).
- Refuses-with-error if npx is absent on PATH (codex needs to find it later).
- Requires Python 3.11+ (for tomllib parsing).

Usage
-----
  register-context-mode-codex.py           # detect + register if needed
  register-context-mode-codex.py --check   # report state, write nothing
  register-context-mode-codex.py --help

Re-running is safe — duplicates are not created.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import goalflight_compat as fcntl

try:
    import tomllib
except ImportError:
    print(
        "ERROR: this script requires Python 3.11+ for tomllib (got Python "
        + sys.version.split()[0]
        + ").",
        file=sys.stderr,
    )
    print(
        "  Upgrade Python (e.g. `brew install python@3.12`) and re-run.",
        file=sys.stderr,
    )
    sys.exit(2)


# Sentinel returned by append_atomically() when another runner won the race.
# Using a module-level singleton keeps the type signature `Optional[Path]`
# clean while preserving the distinction between "raced" and "wrote, but no
# prior config existed so no backup made" — a Round-2 reviewer caught the
# original conflated `Path()` sentinel.
class _RaceSentinel:
    __slots__ = ()


RACED = _RaceSentinel()


def claude_has_context_mode() -> str | None:
    """Return a one-line provenance string if context-mode is detected Claude-side, else None.

    Detection is content-based: a JSON file must contain an `mcpServers["context-mode"]`
    entry. Path-substring filters are NOT used — a plugin's install directory may not
    contain "context-mode" in its name (bundle plugins, custom marketplace paths).
    """
    # Explicit mcpServers entries
    for src in [Path.home() / ".claude.json", Path.home() / ".claude/settings.json"]:
        if not src.exists():
            continue
        try:
            cfg = json.loads(src.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(cfg, dict):
            continue
        servers = cfg.get("mcpServers")
        if isinstance(servers, dict) and servers.get("context-mode"):
            return f"mcpServers entry in {src}"
    # Plugin form — scan all plugin.json files, content-check for mcpServers entry.
    # No path-substring prefilter (round-2 reviewer flag: bundle plugins or
    # non-context-mode-named install paths would be missed).
    plugins_root = Path.home() / ".claude/plugins"
    if plugins_root.exists():
        try:
            for plugin_json in plugins_root.rglob("plugin.json"):
                try:
                    cfg = json.loads(plugin_json.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(cfg, dict):
                    continue
                servers = cfg.get("mcpServers")
                if isinstance(servers, dict) and servers.get("context-mode"):
                    return f"plugin form at {plugin_json.parent}"
        except OSError:
            pass
    return None


def codex_already_registered(codex_config: Path) -> bool:
    """Return True iff [mcp_servers.context-mode] is an ACTIVE registration.

    Uses tomllib to parse the config — correctly handles bracket-table form,
    quoted-key form, AND inline-table / dotted-key forms; ignores comments
    and TOML strings that incidentally contain the registration text.
    """
    if not codex_config.exists():
        return False
    try:
        text = codex_config.read_text()
    except OSError:
        return False
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # Existing file is malformed — treat as "not registered" so the user
        # gets the proper error on append, rather than a false-positive skip.
        return False
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return False
    return "context-mode" in servers


def render_block(npx_path: str) -> str:
    """Render the canonical codex registration for context-mode.

    `npx_path` is run through json.dumps so any TOML-special chars in the
    resolved PATH dir are escaped correctly (TOML basic strings are a superset
    of JSON strings).
    """
    return (
        "\n"
        "[mcp_servers.context-mode]\n"
        f"command = {json.dumps(npx_path)}\n"
        'args = ["-y", "context-mode@latest"]\n'
    )


def append_atomically(
    codex_config: Path, block: str
) -> _RaceSentinel | Optional[Path]:
    """Append `block` to `codex_config`, mutex'd via flock against concurrent runs.

    Returns:
      - RACED sentinel if another runner won the lock and registered first.
      - None if the write succeeded and no prior config existed (no backup).
      - Path(backup_file) if the write succeeded and a prior config was backed up.
    """
    try:
        codex_config.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(
            f"ERROR: could not create {codex_config.parent} ({e}). "
            "Check that ~/.codex/ exists as a directory (not a file) and is writable.",
            file=sys.stderr,
        )
        sys.exit(3)
    lock_path = codex_config.parent / ".register-context-mode.lock"
    try:
        lock_file = open(lock_path, "w")
    except OSError as e:
        print(
            f"ERROR: could not acquire lock at {lock_path} ({e}). "
            "Check ~/.codex/ permissions and re-run.",
            file=sys.stderr,
        )
        sys.exit(3)
    try:
        with lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            # Re-check under lock — another runner may have written it.
            if codex_already_registered(codex_config):
                return RACED
            backup: Optional[Path] = None
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
            try:
                os.replace(tmp_name, codex_config)
            except OSError as e:
                if e.errno == errno.EXDEV:
                    # Cross-FS rename (e.g. tmpdir on different mount). Fall back
                    # to shutil.move which copies + unlinks. Loses atomicity but
                    # we still hold the lock, so concurrent readers won't tear.
                    shutil.move(tmp_name, codex_config)
                else:
                    raise
            return backup
    finally:
        # Best-effort lock-file cleanup. The flock is released on file close
        # above; unlinking the lock file itself prevents ~/.codex/ from
        # accumulating stale lock artifacts across many init invocations.
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Register context-mode MCP on codex side. Idempotent."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Report state; write nothing. Exit 0 if registered or codex absent; "
            "1 if codex needs the block; 4 if needs the block AND npx is missing on PATH."
        ),
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

    # Probe npx so --check surfaces the "Claude has it AND codex needs register
    # AND npx is missing" two-step failure pattern upfront rather than after a
    # subsequent write-mode attempt.
    npx = shutil.which("npx")

    if args.check:
        if not npx:
            print(
                f"CHECK: codex MISSING [mcp_servers.context-mode]; "
                f"Claude has it ({provenance}); npx ALSO missing on PATH. "
                "Install Node.js (which ships npx) before re-running without --check."
            )
            return 4
        print(
            f"CHECK: codex MISSING [mcp_servers.context-mode]; "
            f"Claude has it ({provenance}). Run without --check to register."
        )
        return 1

    if not npx:
        print(
            "ERROR: `npx` not on PATH; cannot write a working "
            "[mcp_servers.context-mode] block. Install Node.js (which "
            "ships npx) and re-run.",
            file=sys.stderr,
        )
        return 4

    block = render_block(npx)
    result = append_atomically(codex_config, block)
    if result is RACED:
        print("raced: another runner registered it under lock. no-op.")
        return 0
    backup = result  # type: Optional[Path]
    print(f"registered context-mode for codex in {codex_config}")
    if backup is not None:
        print(f"  prior config backed up to {backup}")
    print(f"  detected Claude-side via: {provenance}")
    print("  block written:")
    for line in block.strip().splitlines():
        print(f"    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
