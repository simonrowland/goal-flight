"""Cursor worker MCP isolation, shared by text and ACP launch boundaries."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path


def context_mode_enabled(env: dict[str, str]) -> bool:
    return env.get("GOALFLIGHT_CURSOR_CONTEXT_MODE", "").strip().lower() in {
        "1", "true", "yes", "enabled", "on",
    }


def isolate_context_mode(
    agent: str, env: dict[str, str], *, cwd: str, dispatch_id: str | None = None,
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
    # Cursor's utils/git.ts walks lexical parents, without Git or realpath.
    # ACP resolves its session cwd with path.resolve, preserving symlink aliases.
    workspace = os.path.abspath(cwd)
    candidate = Path(workspace)
    while True:
        if (candidate / ".git").exists():
            workspace = str(candidate)
            break
        if candidate.parent == candidate or candidate.parent == Path(candidate.anchor):
            break
        candidate = candidate.parent
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", workspace).strip("-")
    source_data = Path(env.get("CURSOR_DATA_DIR") or Path(env.get("HOME") or Path.home()) / ".cursor")
    source = source_data / "projects" / slug
    # Temp is already writable under the worker's OS sandbox. In particular,
    # a custom dispatch ledger directory need not be writable by the worker.
    try:
        disabled = json.loads((source / "mcp-disabled.json").read_text())
    except FileNotFoundError:
        disabled = []
    if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled):
        raise ValueError(f"Invalid Cursor MCP disabled list: {source / 'mcp-disabled.json'}")
    for identifier in ("context-mode", "plugin-context-mode-context-mode"):
        if identifier not in disabled:
            disabled.append(identifier)
    data = Path(tempfile.mkdtemp(prefix="goalflight-cursor-data-"))
    try:
        # Persist ownership before copying credentials. Unknown legacy dirs are
        # deliberately retained: their workers cannot be proved dead.
        (data / "owner.json").write_text(json.dumps({
            "dispatch_id": dispatch_id or env["GOALFLIGHT_DISPATCH_ID"],
            "launcher_pid": os.getpid(),
        }))
        project = data / "projects" / slug
        project.mkdir(parents=True, mode=0o700)
        for name in ("mcp-auth.json", "mcp-approvals.json", "mcp-group-selections.json"):
            try:
                content = (source / name).read_bytes()
            except FileNotFoundError:
                continue
            (project / name).write_bytes(content)
            (project / name).chmod(0o600)
        (project / "mcp-disabled.json").write_text(json.dumps(disabled) + "\n")
    except BaseException:
        shutil.rmtree(data)
        raise
    env["CURSOR_DATA_DIR"] = str(data)


def cleanup_dispatch_data(
    dispatch_id: str | None = None, *, launcher_finished: bool = False,
    prelaunch_failure: bool = False,
) -> None:
    """Reap terminal dispatch data only after launcher and worker scope death.

    Called by launch finalizers, watcher terminal publication, and reconciliation.
    Probe errors, missing ownership, and unknown worker groups retain the data.
    PID reuse also retains it; a false retention is safer than deleting live data.
    """
    import goalflight_ledger
    import goalflight_dispatch_states

    for data in Path(tempfile.gettempdir()).glob("goalflight-cursor-data-*"):
        try:
            if data.is_symlink() or not data.is_dir():
                continue
            owner = json.loads((data / "owner.json").read_text())
            owned_id = owner["dispatch_id"]
            if not isinstance(owned_id, str) or not owned_id or (dispatch_id is not None and owned_id != dispatch_id):
                continue
            record = json.loads(goalflight_ledger.record_path(owned_id, create=False).read_text())
            if not isinstance(record, dict) or not goalflight_dispatch_states.is_terminal_state(record.get("state")):
                continue
            launcher = int(owner["launcher_pid"])
            finishing_here = launcher_finished and launcher == os.getpid()
            if not finishing_here:
                try:
                    os.kill(launcher, 0)
                except ProcessLookupError:
                    pass
                else:
                    continue
            pid = record.get("worker_pid")
            if pid:
                # Both transports start the worker in its own process group.
                for probe, value in ((os.kill, int(pid)), (os.killpg, int(record.get("worker_pgid") or pid))):
                    try:
                        probe(value, 0)
                    except ProcessLookupError:
                        continue
                    break
                else:
                    shutil.rmtree(data)
            elif finishing_here and prelaunch_failure:
                # The launch finalizer knows no worker was returned by spawn.
                shutil.rmtree(data)
        except (OSError, ValueError, KeyError, TypeError):
            continue
