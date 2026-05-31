#!/usr/bin/env python3
"""OS process sandbox helpers for goal-flight worker subprocesses."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import shutil
import tempfile
from typing import Any

import goalflight_compat


OS_SANDBOX_OFF = "off"
OS_SANDBOX_READ_ONLY = "read-only"
OS_SANDBOX_WORKSPACE_WRITE = "workspace-write"
OS_SANDBOX_PROFILES = (
    OS_SANDBOX_OFF,
    OS_SANDBOX_READ_ONLY,
    OS_SANDBOX_WORKSPACE_WRITE,
)
OS_SANDBOX_ARG_CHOICES = (
    OS_SANDBOX_OFF,
    "host-default",
    "none",
    OS_SANDBOX_READ_ONLY,
    OS_SANDBOX_WORKSPACE_WRITE,
)


class OsSandboxError(RuntimeError):
    """Raised when a requested OS sandbox cannot be enforced."""


def os_sandbox_platform_key() -> str:
    system = platform.system()
    if goalflight_compat.is_windows() or system == "Windows":
        return "windows"
    if goalflight_compat.is_wsl():
        return "wsl"
    if system == "Darwin":
        return "darwin"
    if system == "Linux":
        return "linux"
    return (system or "unknown").lower()


def platform_supported_os_sandbox_profiles() -> list[str]:
    if os_sandbox_platform_key() == "darwin":
        return [OS_SANDBOX_OFF, OS_SANDBOX_READ_ONLY, OS_SANDBOX_WORKSPACE_WRITE]
    return [OS_SANDBOX_OFF]


def os_sandbox_available() -> bool:
    return os_sandbox_platform_key() == "darwin" and shutil.which("sandbox-exec") is not None


@dataclass(frozen=True)
class PreparedOsSandboxCommand:
    command: str
    args: list[str]
    requested: str
    profile: str
    enabled: bool
    implementation: str | None
    write_roots: list[str]

    def metadata(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "profile": self.profile,
            "enabled": self.enabled,
            "implementation": self.implementation,
            "write_roots": list(self.write_roots),
        }


def canonical_os_sandbox(value: str | None) -> str:
    raw = (value or OS_SANDBOX_OFF).strip().lower().replace("_", "-")
    aliases = {
        "": OS_SANDBOX_OFF,
        "false": OS_SANDBOX_OFF,
        "no": OS_SANDBOX_OFF,
        "off": OS_SANDBOX_OFF,
        "none": OS_SANDBOX_OFF,
        "host-default": OS_SANDBOX_OFF,
        "readonly": OS_SANDBOX_READ_ONLY,
        "read-only": OS_SANDBOX_READ_ONLY,
        "workspace": OS_SANDBOX_WORKSPACE_WRITE,
        "workspace-write": OS_SANDBOX_WORKSPACE_WRITE,
    }
    profile = aliases.get(raw)
    if profile is None:
        raise OsSandboxError(f"unsupported os sandbox profile: {value!r}")
    return profile


def preflight_os_sandbox(value: str | None) -> str:
    profile = canonical_os_sandbox(value)
    if profile == OS_SANDBOX_OFF:
        return profile
    if goalflight_compat.is_windows():
        raise OsSandboxError(goalflight_compat.windows_os_sandbox_refusal())
    if profile not in platform_supported_os_sandbox_profiles():
        raise OsSandboxError(
            f"os sandbox profile {profile!r} requires macOS sandbox-exec; "
            f"platform={platform.system() or 'unknown'}"
        )
    if shutil.which("sandbox-exec") is None:
        raise OsSandboxError("os sandbox requested but sandbox-exec is not installed")
    return profile


def _unique_real_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in paths:
        if not raw:
            continue
        candidates = [str(Path(raw).expanduser())]
        try:
            candidates.append(str(Path(raw).expanduser().resolve()))
        except OSError:
            pass
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
    return result


def _path_contains(root: str, child: str) -> bool:
    try:
        root_path = Path(root).expanduser().resolve()
        child_path = Path(child).expanduser().resolve()
        return root_path == child_path or root_path in child_path.parents
    except OSError:
        return False


def _scheme_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _agent_state_roots(agent: str | None, command: str) -> list[str]:
    label = (agent or "").lower()
    binary = Path(command).name.lower()
    home = Path.home()
    roots: list[Path] = []
    if "codex" in label or "codex" in binary:
        roots.extend([
            home / ".codex",
            home / ".config" / "codex",
            home / ".local" / "share" / "codex",
            home / ".cache" / "codex",
        ])
    if "grok" in label or "grok" in binary:
        roots.extend([
            home / ".grok",
            home / ".config" / "grok",
            home / ".local" / "share" / "grok",
            home / ".cache" / "grok",
        ])
    if "cursor" in label or "cursor" in binary:
        roots.extend([
            home / ".cursor",
            home / ".config" / "cursor",
            home / ".local" / "share" / "cursor",
            home / ".cache" / "cursor",
            home / "Library" / "Application Support" / "Cursor",
            home / "Library" / "Caches" / "Cursor",
        ])
    if "claude" in label or "claude" in binary:
        roots.extend([
            home / ".claude",
            home / ".config" / "claude",
            home / ".cache" / "claude",
        ])
    if "opencode" in label or "opencode" in binary:
        roots.extend([
            home / ".config" / "opencode",
            home / ".local" / "share" / "opencode",
            home / ".cache" / "opencode",
        ])
    return [str(path) for path in roots]


def macos_write_roots(cwd: str, profile: str, *, agent: str | None = None, command: str = "") -> list[str]:
    roots: list[str] = []
    if profile == OS_SANDBOX_WORKSPACE_WRITE:
        roots.append(cwd)
    tmpdir = tempfile.gettempdir()
    temp_roots = _unique_real_paths([
        tmpdir,
        os.environ.get("TMPDIR", ""),
        "/tmp",
        "/private/tmp",
    ])
    for root in temp_roots:
        if _path_contains(root, cwd):
            raise OsSandboxError(
                "os sandbox cannot enforce workspace boundaries when cwd is "
                f"inside allowed temp root {root!r}; move the worktree or use off"
            )
    roots.extend(temp_roots)
    agent_roots = _unique_real_paths(_agent_state_roots(agent, command))
    for root in agent_roots:
        if _path_contains(root, cwd):
            raise OsSandboxError(
                "os sandbox cannot enforce workspace boundaries when cwd is "
                f"inside allowed agent state root {root!r}; move the worktree or use off"
            )
    roots.extend(agent_roots)
    return _unique_real_paths(roots)


def macos_sandbox_profile(
    cwd: str,
    profile: str,
    *,
    agent: str | None = None,
    command: str = "",
) -> tuple[str, list[str]]:
    if profile not in {OS_SANDBOX_READ_ONLY, OS_SANDBOX_WORKSPACE_WRITE}:
        raise OsSandboxError(f"unsupported macOS sandbox profile: {profile!r}")
    write_roots = macos_write_roots(cwd, profile, agent=agent, command=command)
    write_filters = "\n".join(f"  (subpath {_scheme_string(path)})" for path in write_roots)
    # /dev/null and /dev/zero are safe write targets (a data sink and a zero
    # source — writing to them mutates no real filesystem state). git and many
    # tools redirect stderr/stdin to /dev/null; without an explicit allow rule
    # the workspace-write sandbox denies the open() for write and the worker
    # fails at the git step (observed 2026-05-28: 5+ codex-acp workers hit
    # BLOCKED on commit because `git ... 2>/dev/null` could not open the device).
    # These are device-node literals, NOT a /dev subpath grant — the rest of
    # /dev stays denied. Reads of /dev/null are already covered by file-read*.
    device_filters = "\n".join(
        f"  (literal {_scheme_string(path)})" for path in ("/dev/null", "/dev/zero")
    )
    profile_text = f"""(version 1)
(deny default)
(allow process*)
(allow signal)
(allow sysctl*)
(allow mach-lookup)
(allow network*)
(allow file-read*)
(allow file-write*
{write_filters}
{device_filters})
"""
    return profile_text, write_roots


def prepare_os_sandbox_command(
    command: str,
    args: list[str],
    *,
    cwd: str,
    os_sandbox: str | None = OS_SANDBOX_OFF,
    agent: str | None = None,
) -> PreparedOsSandboxCommand:
    requested = os_sandbox or OS_SANDBOX_OFF
    profile = preflight_os_sandbox(requested)
    if profile == OS_SANDBOX_OFF:
        return PreparedOsSandboxCommand(
            command=command,
            args=list(args),
            requested=requested,
            profile=profile,
            enabled=False,
            implementation=None,
            write_roots=[],
        )
    profile_text, write_roots = macos_sandbox_profile(cwd, profile, agent=agent, command=command)
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        raise OsSandboxError("os sandbox requested but sandbox-exec is not installed")
    return PreparedOsSandboxCommand(
        command=sandbox_exec,
        args=["-p", profile_text, command, *args],
        requested=requested,
        profile=profile,
        enabled=True,
        implementation="sandbox-exec",
        write_roots=write_roots,
    )
