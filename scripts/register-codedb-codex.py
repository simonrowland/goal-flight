#!/usr/bin/env python3
"""register-codedb-codex.py — register the codedb MCP server on codex.

Why this exists
---------------
codedb (a local code-intelligence binary: indexed symbol/find/search/callers/
deps over a repo) is the SAFE swap-in for the context-mode MCP server that
dispatched `codex exec` workers disable (#18). context-mode's ctx_index issues
an elicitation (`request_user_input`) that `codex exec` cannot service, so it
wedges headless workers; codedb is read-only and does not index-elicit, so it
is the search tool that *can* ride along in a headless worker.

But there is a sharp edge — the reason this script registers PER-TOOL approval
entries, not just the server block:

  In `codex exec`, an MCP tool call with NO `approval_mode` configured is
  CANCELLED ("user cancelled MCP tool call"), even in a trusted project and
  even with `approval_policy=never`. For most codedb tools that just aborts the
  one call; for `codedb_context` the cancellation surfaces upstream as
  `request_user_input is not supported in exec mode` and the worker WEDGES —
  the exact same failure class as context-mode (verified 2026-06-24: with the
  per-tool entries absent, `codedb_context` returns CONTEXT-WEDGE; with them
  present, CONTEXT-OK).

So registering codedb safely for headless codex means writing
`[mcp_servers.codedb.tools.<tool>] approval_mode = "approve"` for EVERY codedb
tool. That is what this script does. All codedb tools are read-only
code-intelligence, so blanket auto-approve is correct.

What this script does
---------------------
1. Detects the codedb binary on PATH (`shutil.which("codedb")`).
2. Classifies the existing registration (tomllib parse — handles bracket-table,
   quoted-key, inline-table, dotted-key forms; ignores comments):
   - "complete"   = server block present AND codedb_context approved -> no-op
     (preserves a user's curated tool set).
   - "incomplete" = server block present but codedb_context NOT approved -> the
     bare-server wedge state; the registrar REPAIRS it by appending the missing
     per-tool approvals (a server-only re-run otherwise leaves the wedge forever).
   - "absent"     = no server block -> writes the full registration.
3. The approved tool set is the codedb `mcp` server's LIVE advertised surface
   (queried via a tools/list handshake; falls back to a hardcoded read-only set
   if the query fails), minus the write tool `codedb_edit`. So a newer codedb
   that adds read tools is covered without a code change, and the approved set is
   never broader than what the server actually exposes.

Idempotency + safety:
- Backs up existing ~/.codex/config.toml with a collision-resistant suffix.
- Uses flock + atomic rename for concurrent-init safety.
- Exits silently if codex isn't installed (no ~/.codex/ to mutate).
- No-op (not an error) if codedb isn't on PATH — registering a server whose
  binary is missing can break codex dispatch (a failed MCP spawn aborts the
  session), so we refuse to write a block we can't back with a real binary.
- Requires Python 3.11+ (for tomllib parsing).

Usage
-----
  register-codedb-codex.py           # detect + register if needed
  register-codedb-codex.py --check   # report state, write nothing
  register-codedb-codex.py --help

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


# codedb tools we auto-approve: an explicit READ-ONLY query allowlist (fail-closed),
# NOT "everything codedb advertises minus a denylist". A denylist silently approves
# any NEW or misclassified mutating tool a future codedb exposes — e.g. codedb_edit
# WRITES files, codedb_index builds/writes an index, codedb_snapshot writes a
# snapshot, codedb_remote touches remote config. Approving only known read-only
# tools keeps the invariant "every auto-approved codedb tool is read-only" TRUE by
# construction; a tool NOT on this list is simply never approved (its call is
# cancelled, never wedged — only codedb_context's cancellation wedges, and it IS in
# this set). A genuinely new read tool needs a one-line allowlist add — cheap, and
# the right side to fail on for a safety boundary. This 19-tool set is also exactly
# the read-only surface of codedb 0.2.x (the 23 advertised tools minus edit/index/
# snapshot/remote), used as the fallback when the live tools/list query fails.
READONLY_CODEDB_TOOLS = frozenset({
    "codedb_callers",
    "codedb_callpath",
    "codedb_changes",
    "codedb_context",
    "codedb_deps",
    "codedb_diagnostics",
    "codedb_find",
    "codedb_glob",
    "codedb_hot",
    "codedb_ls",
    "codedb_outline",
    "codedb_projects",
    "codedb_query",
    "codedb_read",
    "codedb_search",
    "codedb_status",
    "codedb_symbol",
    "codedb_tree",
    "codedb_word",
})

# The one tool whose missing approval reproduces the exec-mode wedge. A codedb
# registration that lacks THIS approval is "incomplete" and must be repaired.
WEDGE_CRITICAL_TOOL = "codedb_context"


def advertised_codedb_tools(codedb_path: str, *, timeout_s: float = 15.0) -> Optional[list[str]]:
    """Return the tool names the installed `codedb mcp` advertises, or None on any failure.

    Spawns `codedb mcp`, performs the minimal JSON-RPC handshake (initialize ->
    initialized notification -> tools/list), and returns the advertised names.
    The caller (target_tools) intersects this with the read-only allowlist, so a
    newer codedb that adds READ tools is covered without a code bump while a new
    WRITE tool is never auto-approved. Best-effort — any error (spawn failure,
    protocol drift, timeout) returns None and the caller falls back to the
    READONLY_CODEDB_TOOLS allowlist.
    """
    import subprocess
    import select
    import time as _time

    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            [codedb_path, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        def _send(obj: dict) -> None:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        _send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "goalflight-register", "version": "1"}},
        })
        _send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        # A stdio MCP server does NOT exit after tools/list — it keeps serving — so we
        # must read line-by-line until the id:2 response (or a deadline), with stdin
        # kept OPEN (closing it makes some builds return nothing), and then kill it.
        # communicate() would block to EOF/exit and always time out -> dead discovery.
        deadline = _time.monotonic() + timeout_s
        assert proc.stdout is not None
        while _time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], deadline - _time.monotonic())
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 2 and isinstance(msg.get("result"), dict):
                tools = msg["result"].get("tools")
                if isinstance(tools, list):
                    names = [t.get("name") for t in tools if isinstance(t, dict) and t.get("name")]
                    return names or None
                return None
        return None
    except Exception:
        return None
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def target_tools(codedb_path: str) -> list[str]:
    """The codedb tools to auto-approve: the live advertised surface (or the
    read-only allowlist, when the live query fails) INTERSECTED with
    READONLY_CODEDB_TOOLS, WEDGE_CRITICAL_TOOL guaranteed present. Fail-closed: a
    tool codedb advertises that is NOT on the allowlist (a write tool, or a new tool
    of unknown semantics) is never auto-approved. Sorted for stable, diff-friendly
    output."""
    advertised = advertised_codedb_tools(codedb_path)
    base = set(advertised) if advertised else set(READONLY_CODEDB_TOOLS)
    keep = {t for t in base if t in READONLY_CODEDB_TOOLS}
    keep.add(WEDGE_CRITICAL_TOOL)  # never omit the wedge-critical tool
    return sorted(keep)


# Sentinel returned by append_atomically() when another runner won the race
# (mirrors register-context-mode-codex.py's RACED contract).
class _RaceSentinel:
    __slots__ = ()


RACED = _RaceSentinel()


def _codedb_tools_table(data: dict) -> dict:
    """The [mcp_servers.codedb.tools] sub-table (dict), or {} if absent."""
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    codedb = servers.get("codedb")
    if not isinstance(codedb, dict):
        return {}
    tools = codedb.get("tools")
    return tools if isinstance(tools, dict) else {}


def _approved_codedb_tools(data: dict) -> set[str]:
    """Tool names under [mcp_servers.codedb.tools.*] with approval_mode EXACTLY "approve".

    Any other value (e.g. "prompt", "ask") still triggers an approval interaction that
    wedges a codex-exec worker on codedb_context — so only "approve" counts as covered.
    """
    return {
        name for name, entry in _codedb_tools_table(data).items()
        if isinstance(entry, dict) and entry.get("approval_mode") == "approve"
    }


def _existing_codedb_tool_tables(data: dict) -> set[str]:
    """Tool names that ALREADY have a [mcp_servers.codedb.tools.<name>] table —
    with OR without approval_mode. Re-appending a table for any of these would
    double-define it (a TOML parse error). Repair skips the already-approved ones
    and hard-errors on any that exist WITHOUT approval_mode = "approve" (rather
    than silently leaving them cancel-prone)."""
    return set(_codedb_tools_table(data).keys())


def _codedb_tools_is_malformed(data: dict) -> bool:
    """True iff [mcp_servers.codedb] has a `tools` key that is NOT a table (e.g.
    `tools = "bad"`). Appending `[mcp_servers.codedb.tools.X]` under such a key
    yields invalid TOML ("Cannot overwrite a value"), so repair must refuse."""
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return False
    codedb = servers.get("codedb")
    if not isinstance(codedb, dict):
        return False
    return "tools" in codedb and not isinstance(codedb["tools"], dict)


def classify_registration(codex_config: Path) -> tuple[str, set[str]]:
    """Classify the codedb registration state of `codex_config`.

    Returns (state, approved_tools) where state is one of:
      - "absent":     no [mcp_servers.codedb] server block at all.
      - "incomplete": server block present BUT the wedge-critical tool
                      (codedb_context) has no approval_mode — i.e. the exact
                      bare-server state that wedges a codex-exec worker. The
                      registrar REPAIRS this by appending the missing approvals
                      (a plain server-only re-run cannot, which is the P1 the
                      pre-landing review caught).
      - "complete":  server block present AND codedb_context approved. We leave
                      it untouched (no-op) — this preserves a user's curated tool
                      set rather than force-approving tools they omitted.

    Uses tomllib so it handles bracket-table, quoted-key, inline-table, and
    dotted-key forms and ignores comments / incidental string matches.
    """
    if not codex_config.exists():
        return "absent", set()
    try:
        text = codex_config.read_text()
    except OSError:
        return "absent", set()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # A non-empty file that does not parse is MALFORMED, not absent. Treating it
        # as absent would append a codedb block and atomically replace the user's
        # config with still-invalid TOML while reporting success — destroying a
        # recoverable config. Surface it so the caller refuses to touch the file.
        # (An empty / whitespace-only file parses to {} above, so this is only
        # reached for real syntax errors.)
        if text.strip():
            return "malformed", set()
        return "absent", set()
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict) or "codedb" not in servers:
        return "absent", set()
    approved = _approved_codedb_tools(data)
    if WEDGE_CRITICAL_TOOL in approved:
        return "complete", approved
    return "incomplete", approved


def render_tool_approvals(tools: list[str]) -> str:
    """Render `[mcp_servers.codedb.tools.<tool>] approval_mode = "approve"` blocks."""
    lines: list[str] = []
    for tool in tools:
        lines.append(f"[mcp_servers.codedb.tools.{tool}]")
        lines.append('approval_mode = "approve"')
    return "\n".join(lines)


def render_block(codedb_path: str, tools: list[str]) -> str:
    """Render the full codedb registration: server launch + per-tool approvals.

    `codedb_path` is run through json.dumps so any TOML-special chars in the
    resolved path are escaped correctly (TOML basic strings are a superset of
    JSON strings). `tools` is the auto-approve set from target_tools().
    """
    lines = [
        "",
        "# codedb — indexed code-intelligence for codex workers (the safe swap-in",
        "# for the disabled context-mode, #18). Per-tool approve entries are",
        "# LOAD-BEARING: codex exec cancels an unconfigured MCP tool call, and for",
        "# codedb_context that cancellation wedges the worker (an exec-mode",
        "# user-input elicitation the headless runner cannot answer). All",
        "# auto-approved codedb tools are read-only (write/index tools are",
        "# intentionally omitted; see register-codedb-codex.py READONLY_CODEDB_TOOLS).",
        "[mcp_servers.codedb]",
        f"command = {json.dumps(codedb_path)}",
        'args = ["mcp"]',
        "startup_timeout_sec = 30",
        "",
        render_tool_approvals(tools),
        "",
    ]
    return "\n".join(lines)


def render_repair(tools: list[str]) -> str:
    """Render just the missing-approval blocks to repair an incomplete codedb config."""
    return (
        "\n"
        "# codedb: repair missing per-tool approvals (server block already present).\n"
        "# Without these, codex exec cancels the calls; codedb_context wedges.\n"
        + render_tool_approvals(tools)
        + "\n"
    )


def register_atomically(
    codex_config: Path, codedb_path: str
) -> _RaceSentinel | tuple[str, Optional[Path]]:
    """Register or repair codedb in `codex_config`, mutex'd via flock.

    The block to write is decided UNDER the lock from a fresh classification, so a
    concurrent runner can't make us double-write: absent -> full server block +
    approvals; incomplete -> just the missing approvals; complete -> RACED no-op.

    Returns:
      - RACED sentinel if, under the lock, the config is already complete.
      - (action, backup_path|None) on a successful write, where action is
        "registered" (was absent) or "repaired" (was incomplete).
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
    # NOTE: the lock file is intentionally NEVER unlinked. Unlinking after
    # releasing the flock lets a second process keep a handle to the now-unlinked
    # inode while a third creates a fresh file at the same path — two writers, two
    # different locks, mutex defeated (pre-landing review P2). A stable empty
    # lock file in ~/.codex is harmless.
    lock_path = codex_config.parent / ".register-codedb.lock"
    try:
        lock_file = open(lock_path, "w")
    except OSError as e:
        print(
            f"ERROR: could not acquire lock at {lock_path} ({e}). "
            "Check ~/.codex/ permissions and re-run.",
            file=sys.stderr,
        )
        sys.exit(3)
    with lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        # Re-classify under the lock — another runner may have changed state.
        state, approved = classify_registration(codex_config)
        if state == "complete":
            return RACED
        if state == "malformed":
            print(
                f"ERROR: {codex_config} is not valid TOML; refusing to modify it. "
                "codex cannot load an invalid config either — hand-fix the syntax "
                "error and re-run (the file is left untouched).",
                file=sys.stderr,
            )
            sys.exit(4)
        tools = target_tools(codedb_path)
        if state == "absent":
            block = render_block(codedb_path, tools)
            action = "registered"
        else:  # incomplete -> append approvals for tools with NO existing table
            try:
                data = tomllib.loads(codex_config.read_text())
            except (OSError, tomllib.TOMLDecodeError):
                data = {}
            # Malformed `tools` (a non-table value) — appending tool tables under it
            # corrupts the file. Refuse rather than write invalid TOML.
            if _codedb_tools_is_malformed(data):
                print(
                    f"ERROR: [mcp_servers.codedb] in {codex_config} has a non-table `tools` "
                    "value; cannot append per-tool approvals without corrupting the file. "
                    "Hand-fix `tools` to a table (or remove it) and re-run.",
                    file=sys.stderr,
                )
                sys.exit(4)
            existing_tables = _existing_codedb_tool_tables(data)
            # A target tool whose table EXISTS but isn't approved="approve" cannot be
            # append-repaired (re-defining the table is a TOML parse error) — and silently
            # skipping it would leave it cancel-prone (codedb_context: wedge-prone) forever.
            # Surface every such table so the user hand-fixes it, rather than write a config
            # that looks repaired but isn't.
            broken = sorted(t for t in tools if t in existing_tables and t not in approved)
            if broken:
                joined = ", ".join(broken)
                print(
                    f"ERROR: these codedb tool tables exist in {codex_config} without "
                    f'approval_mode = "approve": {joined}. Append-repair would double-define '
                    'them. Hand-edit each to set approval_mode = "approve" and re-run.',
                    file=sys.stderr,
                )
                sys.exit(4)
            # Re-appending a tool that already has a table would double-define it, so repair
            # adds tables only for tools with NO existing table.
            missing = [t for t in tools if t not in existing_tables]
            if not missing:
                # All target tables already exist and are approved, yet codedb_context
                # wasn't — only reachable if codedb_context isn't in the target set, which
                # target_tools() guarantees against. Treat as already-satisfied.
                return RACED
            block = render_repair(missing)
            action = "repaired"
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
        # Defense in depth: never atomically replace the live config with content
        # that doesn't parse. If appending the block somehow produced invalid TOML
        # (an edge the classify/guard checks above didn't catch), abort BEFORE the
        # replace so the original config is left intact.
        try:
            tomllib.loads(new_content)
        except tomllib.TOMLDecodeError as e:
            print(
                f"ERROR: appending the codedb block to {codex_config} would produce "
                f"invalid TOML ({e}); aborting without modifying the file.",
                file=sys.stderr,
            )
            sys.exit(4)
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
                shutil.move(tmp_name, codex_config)
            else:
                raise
        return action, backup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Register codedb MCP on codex side (with per-tool auto-approve). Idempotent."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Report state; write nothing. Exit 0 if registered or codex/codedb absent; "
            "1 if codex needs the block."
        ),
    )
    args = parser.parse_args(argv)

    if not shutil.which("codex"):
        print("codex not installed; nothing to register. skipping.")
        return 0

    codex_config = Path.home() / ".codex/config.toml"
    state, _approved = classify_registration(codex_config)

    if state == "complete":
        print(f"codex: [mcp_servers.codedb] complete in {codex_config}; no-op.")
        return 0

    if state == "malformed":
        msg = (
            f"{codex_config} is not valid TOML; refusing to modify it (codex cannot "
            "load it either). Hand-fix the syntax error and re-run."
        )
        print(f"CHECK: {msg}" if args.check else f"ERROR: {msg}", file=sys.stderr)
        return 1

    codedb = shutil.which("codedb")
    if not codedb:
        msg = (
            "codedb binary not on PATH; not registering (a server whose binary "
            "is missing can break codex dispatch). Install codedb, then re-run."
        )
        if args.check:
            print(f"CHECK: codex codedb registration {state} AND {msg}")
            return 1
        print(msg)
        return 0

    if args.check:
        need = "MISSING" if state == "absent" else "INCOMPLETE (missing per-tool approvals; codedb_context would wedge)"
        print(
            f"CHECK: codex codedb registration {need}; codedb present at {codedb}. "
            "Run without --check to register/repair."
        )
        return 1

    result = register_atomically(codex_config, codedb)
    if result is RACED:
        print("raced: another runner completed the registration under lock. no-op.")
        return 0
    action, backup = result  # type: ignore[misc]
    verb = "registered" if action == "registered" else "repaired (appended missing per-tool approvals)"
    print(f"{verb} codedb for codex in {codex_config}")
    if backup is not None:
        print(f"  prior config backed up to {backup}")
    print(f"  codedb binary: {codedb}")
    print(f"  auto-approve tool entries: {len(target_tools(codedb))} (codedb_edit excluded; read-only set)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
