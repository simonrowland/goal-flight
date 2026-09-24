#!/usr/bin/env python3
"""OS process sandbox helpers for goal-flight worker subprocesses."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import os
from pathlib import Path
import platform
import re
import shutil
import tempfile

from goalflight_codex_sandbox import (
    _git_path,
    linked_worktree_writable_roots,
    worker_task_store_root,
)
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
        root_path = Path(root).expanduser().resolve(strict=False)
        child_path = Path(child).expanduser().resolve(strict=False)
        return root_path == child_path or root_path in child_path.parents
    except OSError:
        return False


def _path_intersects(left: str | Path, right: str | Path) -> bool:
    """Return whether two canonical paths overlap in either direction."""
    try:
        left_path = Path(left).expanduser().resolve(strict=False)
        right_path = Path(right).expanduser().resolve(strict=False)
    except OSError as exc:
        raise OsSandboxError(
            f"cannot canonicalize sandbox path {left!r} or {right!r}: {exc}"
        ) from exc
    return (
        left_path == right_path
        or left_path in right_path.parents
        or right_path in left_path.parents
    )


def _protected_worktree_paths(cwd: str) -> list[str]:
    """Return the repository, worktree, and linked-worktree Git boundaries."""
    cwd_path = Path(cwd).expanduser().resolve(strict=False)
    protected: list[Path] = [cwd_path]
    repository_root = _git_path(cwd_path, "--show-toplevel")
    git_dir = _git_path(cwd_path, "--git-dir")
    common_dir = _git_path(cwd_path, "--git-common-dir")
    for path in (repository_root, git_dir, common_dir):
        if path is not None:
            protected.append(path)
    if common_dir is not None and common_dir.name == ".git":
        # A linked worktree's common .git directory owns every sibling
        # worktree plus shared objects and refs; protect the repository root
        # and worktrees subtree in addition to the metadata directories.
        protected.extend((common_dir.parent, common_dir.parent / "worktrees"))
    return _unique_real_paths([str(path) for path in protected])


def _is_bash_grok(agent: str | None, command: str) -> bool:
    label = (agent or "").lower()
    binary = Path(command).name.lower()
    if label in {"grok-code", "grok-research"}:
        return True
    return binary == "grok" and not label.endswith("-acp")


def _resolved_grok_account_home(environment: Mapping[str, str]) -> Path | None:
    raw_home = str(environment.get("HOME") or "").strip()
    if not raw_home:
        return None
    try:
        home = Path(raw_home).expanduser().resolve(strict=False)
        accounts_root = (Path.home() / ".goal-flight" / "accounts").resolve(strict=False)
    except OSError:
        return None
    if home.name != "grok" or accounts_root not in home.parents:
        return None
    return home


def _scheme_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _grok_read_only_cleanup_lock_filters(steer_file: str) -> list[str]:
    """Return regex grants for this dispatch's dynamically named cleanup locks."""
    steer_path = Path(steer_file).expanduser()
    parent = re.escape(str(steer_path.parent.resolve(strict=False)))
    stem = re.escape(steer_path.stem)
    prefix = f"{parent}/\\.{stem}"
    return [
        f'(regex #"^{prefix}\\.cleanup\\.(?:receipt|end)'
        f'\\.[A-Za-z0-9]{{1,128}}\\.[1-9][0-9]*\\.lock$")'
    ]


def goalflight_worker_channel_roots() -> list[str]:
    """Paths a sandboxed worker needs in order to SEND mail.

    A worker under workspace-write could not write the messages directory at
    all, so its only way to reach a controller was a marker in its console log
    that the watcher happened to scrape. That is why controllers ended up
    inventing side-channel files: the real channel was write-locked against the
    agents that most needed it.

    The task-store parent is here for the same reason: workers are instructed
    to send out-of-scope findings to the store's deferred lane, and the store
    moved out of the repo to the durable state home without the grant
    following it. Only ``task-stores/`` is granted -- the state base above it
    holds the cross-project index and setup backups, which stay denied.

    Scope is deliberately those two. Journal state is claimed by the
    unsandboxed watcher, so the seatbelt does not grant the journal lock
    directory. The fleet directory holds the registry and the derived
    aggregate -- state a worker consumes but must never author -- and
    stays denied.
    """
    return [
        str(Path.home() / ".goal-flight" / "messages"),
        str(worker_task_store_root()),
    ]


def _agent_state_roots(
    agent: str | None,
    command: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    label = (agent or "").lower()
    binary = Path(command).name.lower()
    env = os.environ if environment is None else environment
    home = Path(env.get("HOME") or Path.home())
    roots: list[Path] = []
    if "codex" in label or "codex" in binary:
        roots.extend([
            home / ".codex",
            home / ".config" / "codex",
            home / ".local" / "share" / "codex",
            home / ".cache" / "codex",
        ])
        # TEMPORARY (see the task-store item on deriving sandbox roots from the
        # worker's authorized toolchain rather than its own agent label).
        #
        # Dispatch does not use ~/.codex: it points CODEX_HOME at a per-dispatch
        # directory under the goal-flight state root. Granting only ~/.codex
        # means every nested codex fails to initialize with "Operation not
        # permitted" -- including a worker running its own MANDATORY independent
        # review, which then correctly refuses to commit and escalates BLOCKED.
        # Two workers did exactly that today and the work sat staged for hours.
        #
        # Grant the home actually in use, plus the shared parent so a nested
        # dispatch that mints its own home can start. Both stay well outside the
        # workspace boundary the profile exists to enforce.
        configured_home = os.environ.get("CODEX_HOME")
        if configured_home:
            roots.append(Path(configured_home))
        roots.append(home / ".goal-flight" / "dispatch-homes")
    if "grok" in label or "grok" in binary:
        xdg_config_home = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        xdg_state_home = Path(env.get("XDG_STATE_HOME") or home / ".local" / "state")
        xdg_data_home = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
        xdg_cache_home = Path(env.get("XDG_CACHE_HOME") or home / ".cache")
        roots.extend([
            # Grok's account HOME state: the adapter installs its skill below
            # ~/.grok, and grok_seats.ensure_project_trusted() writes
            # ~/.grok/trusted_folders.toml before launch.
            home / ".grok",
            # grok_seats.STATE_PATH is HOME/.goal-flight/grok-seat-states.json
            # when the account helper is invoked from this worker home.
            home / ".goal-flight",
            # XDG_CONFIG_HOME is the account-scoped config root selected by
            # _apply_home_env(); Grok's XDG config lives below its "grok" key.
            xdg_config_home / "grok",
            # The adapter records readiness below XDG_STATE_HOME/goal-flight.
            xdg_state_home / "goal-flight",
            # Grok's account-scoped runtime data uses XDG_DATA_HOME/grok.
            xdg_data_home / "grok",
            # Grok's account-scoped cache uses XDG_CACHE_HOME/grok (or the
            # standard HOME/.cache fallback when no XDG cache variable exists).
            xdg_cache_home / "grok",
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


def macos_write_roots(
    cwd: str,
    profile: str,
    *,
    agent: str | None = None,
    command: str = "",
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    roots: list[str] = []
    env = os.environ if environment is None else environment
    grok_bash_read_only = (
        profile == OS_SANDBOX_READ_ONLY and _is_bash_grok(agent, command)
    )
    account_home: Path | None = None
    if grok_bash_read_only:
        account_home = _resolved_grok_account_home(env)
        if account_home is None:
            raise OsSandboxError(
                "read-only Grok requires a resolved account-scoped HOME; "
                "refusing the host-seat launch"
            )
    if profile == OS_SANDBOX_WORKSPACE_WRITE:
        roots.append(cwd)
        label = (agent or "").lower()
        if label in {"codex", "codex-acp"}:
            roots.extend(linked_worktree_writable_roots(cwd))
    if grok_bash_read_only:
        private_tmp = str(env.get("TMPDIR") or "").strip()
        if not private_tmp:
            raise OsSandboxError(
                "read-only Grok requires a private per-dispatch TMPDIR"
            )
        private_tmp_path = Path(private_tmp).expanduser().resolve(strict=False)
        temp_root = Path(tempfile.gettempdir()).expanduser().resolve(strict=False)
        if (
            private_tmp_path == temp_root
            or private_tmp_path.parent != temp_root
            or not private_tmp_path.is_dir()
        ):
            raise OsSandboxError(
                "read-only Grok TMPDIR must be an existing private child of "
                f"{temp_root}"
            )
        # The dispatcher creates this 0700 directory for this dispatch. Never
        # add the shared system temp root, which contains sibling dispatches.
        temp_roots = _unique_real_paths([str(private_tmp_path)])
    else:
        tmpdir = tempfile.gettempdir()
        temp_roots = _unique_real_paths([
            tmpdir,
            env.get("TMPDIR", ""),
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
    if grok_bash_read_only:
        # _cmd_steer --wait calls append_worker_wait_started through
        # goalflight_steer_mailbox.py, so Grok writes only its own steer
        # carrier. carrier_transaction() also creates the exact mailbox lock
        # from goalflight_messages.mail_lock_path(), and
        # worker_wait_receipts_path() owns the same dispatch's reply receipt.
        # The dispatcher creates status/tail and redirects stdout to the
        # already-open tail descriptor; the Grok adapter and
        # configs/grok/skills/goal-flight/SKILL.md do not write the global
        # messages or task-store trees, so no sibling channel is granted.
        steer_file = str(env.get("GOALFLIGHT_STEER_FILE") or "").strip()
        if steer_file:
            steer_path = Path(steer_file).expanduser()
            steer_receipts = steer_path.with_name(
                f"{steer_path.stem}.receipts.jsonl"
            )
            worker_channel_roots = [
                str(steer_path),
                str(steer_path.with_name(f".{steer_path.name}.lock")),
                str(steer_receipts),
                str(steer_receipts.with_name(f".{steer_receipts.name}.lock")),
            ]
        else:
            worker_channel_roots = []
    else:
        worker_channel_roots = goalflight_worker_channel_roots()
    state_roots = _unique_real_paths(
        _agent_state_roots(agent, command, environment=environment)
    )
    if grok_bash_read_only:
        assert account_home is not None
        for grant in state_roots:
            if not _path_contains(str(account_home), grant):
                raise OsSandboxError(
                    "read-only Grok agent state grant "
                    f"{grant!r} resolves outside selected account "
                    f"{str(account_home)!r}; refusing the launch"
                )
    extra_roots = _unique_real_paths(state_roots + worker_channel_roots)
    for root in extra_roots:
        if _path_contains(root, cwd):
            raise OsSandboxError(
                "os sandbox cannot enforce workspace boundaries when cwd is "
                f"inside allowed agent state root {root!r}; move the worktree or use off"
            )
    roots.extend(extra_roots)
    if profile == OS_SANDBOX_READ_ONLY:
        protected = _protected_worktree_paths(cwd)
        for grant in _unique_real_paths(roots):
            for boundary in protected:
                if _path_intersects(grant, boundary):
                    raise OsSandboxError(
                        "read-only sandbox grant "
                        f"{grant!r} intersects protected repository/worktree/Git "
                        f"path {boundary!r}; refusing the launch"
                    )
    return _unique_real_paths(roots)


def macos_sandbox_profile(
    cwd: str,
    profile: str,
    *,
    agent: str | None = None,
    command: str = "",
    environment: Mapping[str, str] | None = None,
) -> tuple[str, list[str]]:
    if profile not in {OS_SANDBOX_READ_ONLY, OS_SANDBOX_WORKSPACE_WRITE}:
        raise OsSandboxError(f"unsupported macOS sandbox profile: {profile!r}")
    write_roots = macos_write_roots(
        cwd,
        profile,
        agent=agent,
        command=command,
        environment=environment,
    )
    write_filters = "\n".join(f"  (subpath {_scheme_string(path)})" for path in write_roots)
    regex_filters = ""
    if profile == OS_SANDBOX_READ_ONLY and _is_bash_grok(agent, command):
        steer_file = str((environment or os.environ).get("GOALFLIGHT_STEER_FILE") or "").strip()
        if steer_file:
            regex_filters = "\n".join(
                f"  {entry}" for entry in _grok_read_only_cleanup_lock_filters(steer_file)
            )
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
{regex_filters}
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
    environment: Mapping[str, str] | None = None,
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
    profile_text, write_roots = macos_sandbox_profile(
        cwd,
        profile,
        agent=agent,
        command=command,
        environment=environment,
    )
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
