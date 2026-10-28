#!/usr/bin/env python3
"""Classify local worker and MCP processes before any cleanup.

An orphan MCP server has a kernel-reported executable matching a registered
interpreter, an exact registered entrypoint in argv[1], a verified ancestry
chain to PID 1, and only the registered launcher for that server among its
ancestors. Dispatch workers and their descendants are never orphans. ``--reap``
rechecks each recorded process start token before sending SIGTERM.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tomllib
from typing import Any, Iterable, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat
import goalflight_fleet_store
import goalflight_traffic


DISPATCH_WORKER = "dispatch worker"
DESCENDANT = "descendant of worker/exec session"
MCP_LIVE_PARENT = "MCP server with a live parent session"
MCP_ORPHAN = "orphan MCP server"
UNKNOWN = "unknown"
CLASSES = (DISPATCH_WORKER, DESCENDANT, MCP_LIVE_PARENT, MCP_ORPHAN, UNKNOWN)

_SCRIPT_INTERPRETERS = frozenset(
    {"node", "nodejs", "python", "python2", "python3", "ruby", "deno", "bun", "perl", "php", "sh"}
)
_COMMON_INTERPRETERS = _SCRIPT_INTERPRETERS | {"java"}


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    command: str
    argv: tuple[str, ...] | None = None
    executable_path: str | None = None
    start_token: str | None = None
    possible_mcp: bool = False
    identity_verified: bool = False


@dataclass(frozen=True)
class Launcher:
    executable: str
    args: tuple[str, ...]
    interpreter_paths: frozenset[str]


@dataclass(frozen=True)
class RegisteredServer:
    interpreter_paths: frozenset[str]
    launcher: Launcher | None


@dataclass(frozen=True)
class WorkerPidSources:
    pids: frozenset[int]
    complete: bool
    errors: tuple[str, ...] = ()


def _positive_pid(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _read_mapping(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"{path}: {type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, f"{path}: expected an object"
    return payload, None


def _local_state_dirs() -> tuple[list[Path], list[str]]:
    """Return the configured/default and locally present registered fleet roots."""
    roots = {
        goalflight_compat.default_state_dir().expanduser(),
        goalflight_compat.resolve_state_dir().expanduser(),
    }
    errors: list[str] = []
    fleet_path = goalflight_fleet_store.default_fleet_dir() / "fleet.json"
    try:
        fleet_path.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        errors.append(f"{fleet_path}: {type(exc).__name__}")
    else:
        fleet, error = _read_mapping(fleet_path)
        nodes = fleet.get("nodes") if fleet else None
        if error:
            errors.append(error)
        elif not isinstance(nodes, dict):
            errors.append(f"{fleet_path}: missing nodes map")
        else:
            for node in nodes.values():
                if not isinstance(node, dict):
                    errors.append(f"{fleet_path}: invalid node record")
                    continue
                raw = node.get("state_dir")
                if raw in (None, ""):
                    continue
                if not isinstance(raw, str):
                    errors.append(f"{fleet_path}: invalid node state_dir")
                    continue
                roots.add(Path(raw).expanduser())
    return sorted(roots, key=lambda path: str(path)), errors


def _read_status_payloads(directory: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every local dispatch status file; missing directories are empty."""
    try:
        directory.stat()
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"{directory}: {type(exc).__name__}"]
    payloads, errors, _count = goalflight_traffic._dispatch_status_payloads(
        directory, limit=0
    )
    if errors:
        return payloads, [errors]
    return payloads, []


def _read_fleet_statuses(directory: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read ``dispatches/<id>/status.json`` files written on fleet workers."""
    try:
        directory.stat()
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"{directory}: {type(exc).__name__}"]
    try:
        dispatches = list(directory.iterdir())
    except OSError as exc:
        return [], [f"{directory}: {type(exc).__name__}"]
    payloads: list[dict[str, Any]] = []
    errors: list[str] = []
    for dispatch in dispatches:
        try:
            if not stat.S_ISDIR(dispatch.stat().st_mode):
                continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{dispatch}: {type(exc).__name__}")
            continue
        status_path = dispatch / "status.json"
        try:
            status_path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{status_path}: {type(exc).__name__}")
            continue
        payload, error = _read_mapping(status_path)
        if error:
            errors.append(error)
        else:
            payloads.append(payload)
    return payloads, errors


def worker_pid_sources(
    state_dirs: Sequence[Path], *, extra_dispatch_dir: Path | None = None
) -> WorkerPidSources:
    """Collect worker PIDs from every state root's status files and runs.d."""
    pids: set[int] = set()
    errors: list[str] = []
    dispatch_dirs = {root / "dispatch" for root in state_dirs}
    fleet_dispatch_dirs = {root / "dispatches" for root in state_dirs}
    if extra_dispatch_dir is not None:
        dispatch_dirs.add(extra_dispatch_dir.expanduser())

    for directory in sorted(dispatch_dirs, key=str):
        payloads, found_errors = _read_status_payloads(directory)
        errors.extend(found_errors)
        for payload in payloads:
            if pid := _positive_pid(payload.get("worker_pid")):
                pids.add(pid)

    for directory in sorted(fleet_dispatch_dirs, key=str):
        payloads, found_errors = _read_fleet_statuses(directory)
        errors.extend(found_errors)
        for payload in payloads:
            if pid := _positive_pid(payload.get("worker_pid")):
                pids.add(pid)

    for root in state_dirs:
        runs_dir = root / "runs.d"
        try:
            runs_dir.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{runs_dir}: {type(exc).__name__}")
            continue
        try:
            records = list(runs_dir.iterdir())
        except OSError as exc:
            errors.append(f"{runs_dir}: {type(exc).__name__}")
            continue
        for record_path in records:
            if record_path.suffix != ".json":
                continue
            payload, error = _read_mapping(record_path)
            if error:
                errors.append(error)
            elif pid := _positive_pid(payload.get("worker_pid")):
                pids.add(pid)

    return WorkerPidSources(frozenset(pids), not errors, tuple(errors))


def _json_servers(payload: Mapping[str, Any], key: str) -> tuple[list[dict[str, Any]], bool]:
    servers = payload.get(key)
    if servers is None:
        return [], True
    if not isinstance(servers, dict):
        return [], False
    return [item for item in servers.values() if isinstance(item, dict)], all(
        isinstance(item, dict) for item in servers.values()
    )


def _command_argv(server: Mapping[str, Any]) -> list[str] | None:
    command = server.get("command")
    if isinstance(command, str) and command:
        argv = [command]
    elif isinstance(command, list) and command and all(isinstance(item, str) for item in command):
        argv = list(command)
    else:
        return None
    args = server.get("args", [])
    if args is None:
        args = []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return None
    return argv + args


def _optional_config(path: Path, *, toml: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError) as exc:
        return None, f"{path}: {type(exc).__name__}"
    try:
        payload = tomllib.loads(raw) if toml else json.loads(raw)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        return None, f"{path}: {type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, f"{path}: expected an object"
    return payload, None


def registered_mcp_commands(
    *, home: Path | None = None, cwd: Path | None = None
) -> tuple[list[list[str]], list[str]]:
    """Read the standard Codex, Claude, Cursor, and OpenCode MCP registrations."""
    home = (home or Path.home()).expanduser()
    cwd = (cwd or Path.cwd()).expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
    cursor_home = Path(os.environ.get("CURSOR_CONFIG_DIR", str(home / ".cursor"))).expanduser()
    xdg_home = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))).expanduser()
    config_paths = [
        (codex_home / "config.toml", "mcp_servers", True),
        (home / ".claude.json", "mcpServers", False),
        (home / ".claude" / "settings.json", "mcpServers", False),
        (home / ".claude" / "settings.local.json", "mcpServers", False),
        (cwd / ".mcp.json", "mcpServers", False),
        (cwd / ".claude" / "settings.json", "mcpServers", False),
        (cwd / ".claude" / "settings.local.json", "mcpServers", False),
        (cursor_home / "mcp.json", "mcpServers", False),
        (cwd / ".cursor" / "mcp.json", "mcpServers", False),
        (xdg_home / "opencode" / "opencode.json", "mcp", False),
        (cwd / "opencode.json", "mcp", False),
        (cwd / ".opencode" / "opencode.json", "mcp", False),
    ]
    commands: list[list[str]] = []
    errors: list[str] = []
    seen: set[Path] = set()
    for path, key, is_toml in config_paths:
        path = path.resolve(strict=False)
        if path in seen:
            continue
        seen.add(path)
        payload, error = _optional_config(path, toml=is_toml)
        if error:
            errors.append(error)
            continue
        if payload is None:
            continue
        servers, valid = _json_servers(payload, key)
        if not valid:
            errors.append(f"{path}: invalid {key} map")
            continue
        for server in servers:
            argv = _command_argv(server)
            if argv is None:
                errors.append(f"{path}: registered server has no valid command")
            else:
                commands.append(argv)
    return commands, errors


def _package_name(spec: str) -> str:
    value = spec.strip()
    if value.startswith("@"):
        slash = value.find("/")
        version = value.rfind("@")
        return value[:version] if version > slash else value
    return value.split("@", 1)[0]


def _npx_package_invocations(
    command: str, args: Sequence[str]
) -> list[tuple[str, str | None]]:
    packages: list[str] = []
    positional: list[str] = []
    after_separator: list[str] | None = None
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            after_separator = list(args[index + 1 :])
            break
        if value in {"-p", "--package"}:
            index += 1
            if index < len(args):
                packages.append(_package_name(args[index]))
        elif value.startswith("--package="):
            packages.append(_package_name(value.split("=", 1)[1]))
        elif value.startswith("-"):
            pass
        else:
            positional.append(value)
        index += 1
    if not packages:
        if positional:
            packages.append(_package_name(positional[0]))
        return [(package, None) for package in packages if package]

    invoked = (after_separator or positional)
    executable = Path(invoked[0]).name if invoked else None
    if command == "npm" and not executable and positional:
        executable = Path(positional[0]).name
    return [(package, executable) for package in packages if package]


def _package_bin_paths(
    package: str, *, cwd: Path, npm_cache: Path, executable: str | None = None
) -> tuple[set[Path], list[str]]:
    package_dirs = [cwd / "node_modules" / package]
    npx_root = npm_cache / "_npx"
    try:
        npx_children = list(npx_root.iterdir())
    except FileNotFoundError:
        npx_children = []
    except OSError as exc:
        return set(), [f"{npx_root}: {type(exc).__name__}"]
    package_dirs.extend(child / "node_modules" / package for child in npx_children)

    entrypoints: set[Path] = set()
    errors: list[str] = []
    found_package = False
    for package_dir in package_dirs:
        manifest = package_dir / "package.json"
        try:
            manifest.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{manifest}: {type(exc).__name__}")
            continue
        payload, error = _read_mapping(manifest)
        if error:
            errors.append(error)
            continue
        found_package = True
        bins = payload.get("bin")
        if isinstance(bins, str):
            default_name = package.rsplit("/", 1)[-1]
            bins = {default_name: bins}
        if not isinstance(bins, dict):
            continue
        default_name = package.rsplit("/", 1)[-1]
        selected_name = executable or (
            default_name if default_name in bins else next(iter(bins)) if len(bins) == 1 else None
        )
        target = bins.get(selected_name) if selected_name else None
        if not isinstance(selected_name, str) or not isinstance(target, str):
            errors.append(
                f"unresolved registered npm executable for {package}"
                + (f": {executable}" if executable else "")
            )
            continue
        script = package_dir / target
        try:
            if not script.is_file():
                continue
            entrypoints.add(script.resolve(strict=True))
            shim = package_dir.parent / ".bin" / selected_name
            if shim.exists():
                entrypoints.add(shim.resolve(strict=True))
        except OSError:
            continue
    if not found_package:
        errors.append(f"unresolved registered npm package: {package}")
    return entrypoints, errors


def _resolve_script(path_value: str, *, cwd: Path) -> Path | None:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve(strict=True) if path.is_file() else None
    except OSError:
        return None


def _resolve_executable(value: str, *, cwd: Path) -> Path | None:
    path = Path(value).expanduser()
    if path.is_absolute() or "/" in value or "\\" in value:
        return _resolve_script(value, cwd=cwd)
    found = shutil.which(value)
    return _resolve_script(found, cwd=cwd) if found else None


def _shebang_command(path: Path) -> str | None:
    try:
        first_line = path.open("rb").readline(256).decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    if not first_line.startswith("#!"):
        return None
    try:
        parts = shlex.split(first_line[2:].strip())
    except ValueError:
        return None
    if not parts:
        return None
    if Path(parts[0]).name == "env":
        parts = [part for part in parts[1:] if not part.startswith("-")]
    return parts[0] if parts else None


def _shebang_interpreters(path: Path) -> set[str]:
    command = _shebang_command(path)
    return {_command_name(command)} if command else set()


def _script_interpreter_paths(path: Path, *, cwd: Path) -> frozenset[str]:
    command = _shebang_command(path)
    executable = _resolve_executable(command, cwd=cwd) if command else None
    return frozenset({_path_key(executable)}) if executable else frozenset()


def _launcher_for(argv: Sequence[str], *, cwd: Path) -> Launcher | None:
    if not argv:
        return None
    executable = _resolve_executable(argv[0], cwd=cwd)
    if executable is None:
        return None
    return Launcher(
        executable=_path_key(executable),
        args=tuple(argv[1:]),
        interpreter_paths=_script_interpreter_paths(executable, cwd=cwd),
    )


def resolve_mcp_entrypoints(
    commands: Iterable[Sequence[str]], *, cwd: Path | None = None, home: Path | None = None
) -> tuple[dict[str, tuple[RegisteredServer, ...]], frozenset[str], tuple[str, ...]]:
    """Resolve each registered server path with its interpreter and launcher."""
    cwd = (cwd or Path.cwd()).expanduser()
    home = (home or Path.home()).expanduser()
    npm_cache = Path(os.environ.get("NPM_CONFIG_CACHE", str(home / ".npm"))).expanduser()
    registrations: dict[str, list[RegisteredServer]] = {}
    interpreters: set[str] = set()
    errors: list[str] = []

    for raw_argv in commands:
        argv = [str(part) for part in raw_argv if isinstance(part, str) and part]
        if not argv:
            continue
        if Path(argv[0]).name.lower() == "env" and len(argv) > 1:
            argv = argv[1:]
        command_name = Path(argv[0]).name.lower()
        args = argv[1:]
        launcher = _launcher_for(argv, cwd=cwd)

        def register(path: Path, runtime_paths: frozenset[str]) -> None:
            key = _path_key(path)
            registration = RegisteredServer(runtime_paths, launcher)
            entries = registrations.setdefault(key, [])
            if registration not in entries:
                entries.append(registration)

        if command_name in {"npx", "npm"}:
            interpreters.add("node")
            package_args = args[1:] if command_name == "npm" and args[:1] in (["exec"], ["x"]) else args
            for package, executable in _npx_package_invocations(command_name, package_args):
                paths, found_errors = _package_bin_paths(
                    package, cwd=cwd, npm_cache=npm_cache, executable=executable
                )
                for path in paths:
                    runtime_paths = _script_interpreter_paths(path, cwd=cwd)
                    register(path, runtime_paths)
                    interpreters.update(_shebang_interpreters(path))
                errors.extend(found_errors)
            continue
        if command_name in _SCRIPT_INTERPRETERS:
            interpreters.add(command_name)
            if args and not args[0].startswith("-"):
                script = _resolve_script(args[0], cwd=cwd)
                if script:
                    executable = _resolve_executable(argv[0], cwd=cwd)
                    runtime_paths = frozenset({_path_key(executable)}) if executable else frozenset()
                    register(script, runtime_paths)
            continue

        command = _resolve_executable(argv[0], cwd=cwd)
        if command is not None:
            shebangs = _shebang_interpreters(command)
            interpreters.update(shebangs)
            if shebangs or command.suffix.lower() in {
                ".js", ".mjs", ".cjs", ".py", ".rb", ".pl", ".php"
            }:
                register(command, _script_interpreter_paths(command, cwd=cwd))

    return (
        {path: tuple(entries) for path, entries in registrations.items()},
        frozenset(interpreters),
        tuple(errors),
    )


def _path_key(path: Path | str) -> str:
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return str(Path(path).expanduser())


def _command_name(value: str) -> str:
    return Path(value.strip()).name.lower()


def _is_interpreter(command: str, names: Iterable[str]) -> bool:
    name = _command_name(command)
    wanted = set(names)
    if name in wanted:
        return True
    return (name.startswith("python3.") and "python3" in wanted) or (
        name.startswith("python") and "python" in wanted
    )


def _argv_script_key(argv: Sequence[str] | None) -> str | None:
    if not argv or len(argv) < 2 or not Path(argv[1]).is_absolute():
        return None
    return _path_key(argv[1])


def _process_executable_path(pid: int) -> str | None:
    """Return the kernel-reported executable path, never a PATH guess."""
    if sys.platform.startswith("linux"):
        try:
            executable = os.readlink(f"/proc/{pid}/exe")
            return _path_key(executable) if Path(executable).is_absolute() else None
        except (OSError, RuntimeError):
            return None
    if sys.platform == "darwin":
        try:
            import ctypes

            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            libproc.proc_pidpath.argtypes = (
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
            )
            libproc.proc_pidpath.restype = ctypes.c_int
            buffer = ctypes.create_string_buffer(4096)
            length = libproc.proc_pidpath(
                pid, ctypes.cast(buffer, ctypes.c_void_p), len(buffer)
            )
            if length <= 0 or length >= len(buffer) or not buffer.value:
                return None
            executable = os.fsdecode(buffer.value)
            return _path_key(executable) if Path(executable).is_absolute() else None
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    return None


def _matches_launcher(process: Process, launcher: Launcher) -> bool:
    if not process.argv or not process.executable_path:
        return False
    if (
        process.executable_path == launcher.executable
        and tuple(process.argv[1:]) == launcher.args
    ):
        return True
    return (
        process.executable_path in launcher.interpreter_paths
        and _argv_script_key(process.argv) == launcher.executable
        and tuple(process.argv[2:]) == launcher.args
    )


def classify_processes(
    processes: Sequence[Process],
    worker_pids: Iterable[int],
    entrypoints: Mapping[str, Sequence[RegisteredServer]],
    *,
    worker_sources_complete: bool = True,
) -> dict[int, str]:
    """Classify candidate processes using the exact orphan population rule."""
    by_pid = {process.pid: process for process in processes if process.pid > 0}
    workers = set(worker_pids)
    registrations = {_path_key(path): values for path, values in entrypoints.items()}
    registered_paths = set(registrations)
    worker_rows = workers & by_pid.keys()

    def ancestors(pid: int) -> tuple[set[int], bool]:
        seen = {pid}
        found: set[int] = set()
        if not by_pid[pid].identity_verified:
            return found, False
        parent = by_pid[pid].ppid
        while True:
            if parent == 1:
                return found, True
            if parent <= 0 or parent in seen or parent not in by_pid:
                return found, False
            seen.add(parent)
            found.add(parent)
            parent_row = by_pid[parent]
            if not parent_row.identity_verified:
                return found, False
            parent = parent_row.ppid

    classifications: dict[int, str] = {}
    for pid, row in by_pid.items():
        parents, ancestry_complete = ancestors(pid)
        worker_parent = bool(parents & worker_rows)
        script_path = _argv_script_key(row.argv)
        path_registered = script_path in registered_paths if script_path else False
        matching_registrations = [
            registration
            for registration in registrations.get(script_path, ())
            if row.executable_path in registration.interpreter_paths
        ] if script_path else []
        is_candidate = (
            pid in workers
            or worker_parent
            or path_registered
            or row.possible_mcp
        )
        if not is_candidate:
            continue
        if pid in workers:
            classifications[pid] = DISPATCH_WORKER
        elif not worker_sources_complete:
            classifications[pid] = UNKNOWN
        elif not ancestry_complete:
            classifications[pid] = UNKNOWN
        elif worker_parent:
            classifications[pid] = DESCENDANT
        elif path_registered and not matching_registrations:
            classifications[pid] = UNKNOWN
        elif matching_registrations:
            orphan_registration = any(
                all(
                    registration.launcher is not None
                    and _matches_launcher(by_pid[parent], registration.launcher)
                    for parent in parents
                )
                for registration in matching_registrations
            )
            classifications[pid] = MCP_ORPHAN if orphan_registration else MCP_LIVE_PARENT
        else:
            classifications[pid] = UNKNOWN
    return classifications


def _process_table() -> tuple[list[tuple[int, int, str, str]], str | None]:
    if goalflight_compat.is_windows():
        return [], "the process census currently requires a POSIX ps process table"
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,uid=,stat=,comm="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"process table unavailable: {type(exc).__name__}"
    if result.returncode != 0:
        return [], f"process table unavailable (ps exit {result.returncode})"
    try:
        uid = os.getuid()
    except AttributeError:
        return [], "current process user id unavailable"
    rows: list[tuple[int, int, str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) != 5 or not fields[0].isdigit() or not fields[1].isdigit() or not fields[2].isdigit():
            continue
        pid, ppid, owner = int(fields[0]), int(fields[1]), int(fields[2])
        state, command = fields[3], fields[4]
        if owner != uid or state.startswith("Z"):
            continue
        rows.append((pid, ppid, state, command))
    return rows, None


def _capture_processes(
    rows: Sequence[tuple[int, int, str, str]],
    interpreters: Iterable[str],
    worker_pids: Iterable[int] = (),
) -> list[Process]:
    candidate_interpreters = {name.lower() for name in interpreters}
    wanted_interpreters = candidate_interpreters | _COMMON_INTERPRETERS
    by_pid = {pid: (ppid, command) for pid, ppid, _state, command in rows}
    worker_set = set(worker_pids)
    needed: set[int] = set()
    for pid, (ppid, command) in by_pid.items():
        name = _command_name(command)
        if not (_is_interpreter(name, wanted_interpreters) or pid in worker_set):
            continue
        current = pid
        seen: set[int] = set()
        while current in by_pid and current not in seen:
            seen.add(current)
            needed.add(current)
            parent = by_pid[current][0]
            if parent == 1 or parent <= 0:
                break
            current = parent
    processes: list[Process] = []
    for pid in sorted(needed):
        ppid, command = by_pid[pid]
        name = _command_name(command)
        possible_mcp = _is_interpreter(name, candidate_interpreters)
        argv: tuple[str, ...] | None = None
        token: str | None = None
        identity_verified = False
        before = goalflight_compat.process_start_identity(pid, include_ancestry=True)
        before_token = before.get("start_token") if isinstance(before, dict) else None
        snapshot_ppid = before.get("ppid") if isinstance(before, dict) else None
        if (
            isinstance(before, dict)
            and before.get("pid") == pid
            and isinstance(before_token, str)
            and isinstance(snapshot_ppid, int)
            and not isinstance(snapshot_ppid, bool)
        ):
            token = before_token
        captured = goalflight_compat.process_argv(pid, expected_basename=name)
        executable_path = _process_executable_path(pid)
        after = goalflight_compat.process_start_identity(pid, include_ancestry=True)
        after_token = after.get("start_token") if isinstance(after, dict) else None
        after_ppid = after.get("ppid") if isinstance(after, dict) else None
        if (
            captured
            and before_token
            and isinstance(after, dict)
            and after_token == before_token
            and after.get("pid") == pid
            and after_ppid == snapshot_ppid == ppid
        ):
            identity_verified = True
            argv = tuple(captured)
        processes.append(
            Process(
                pid=pid,
                ppid=snapshot_ppid if isinstance(snapshot_ppid, int) else ppid,
                command=command,
                argv=argv,
                executable_path=executable_path if identity_verified else None,
                start_token=token,
                possible_mcp=possible_mcp,
                identity_verified=identity_verified,
            )
        )
    return processes


def reap_orphans(
    classifications: Mapping[int, str],
    processes: Sequence[Process],
    *,
    identity_matches=None,
    signal_process=None,
) -> list[dict[str, Any]]:
    """SIGTERM only census orphans whose original start token still matches."""
    identity_matches = identity_matches or goalflight_compat.process_identity_matches
    signal_process = signal_process or os.kill
    by_pid = {process.pid: process for process in processes}
    actions: list[dict[str, Any]] = []
    for pid in sorted(pid for pid, kind in classifications.items() if kind == MCP_ORPHAN):
        process = by_pid.get(pid)
        if process is None or not process.start_token or not process.identity_verified:
            actions.append({"pid": pid, "action": "skipped", "reason": "start token unavailable"})
            continue
        if identity_matches(pid, process.start_token) is not True:
            actions.append({"pid": pid, "action": "skipped", "reason": "process identity changed or unavailable"})
            continue
        try:
            signal_process(pid, signal.SIGTERM)
        except ProcessLookupError:
            actions.append({"pid": pid, "action": "skipped", "reason": "process exited"})
        except OSError as exc:
            actions.append({"pid": pid, "action": "failed", "reason": type(exc).__name__})
        else:
            actions.append({"pid": pid, "action": "signalled", "signal": "SIGTERM"})
    return actions


def _print_report(classifications: Mapping[int, str], *, reap: bool, processes: Sequence[Process]) -> None:
    counts = {kind: 0 for kind in CLASSES}
    for kind in classifications.values():
        counts[kind] = counts.get(kind, 0) + 1
    for kind in CLASSES:
        print(f"{kind}: {counts[kind]}")
    orphan_pids = sorted(pid for pid, kind in classifications.items() if kind == MCP_ORPHAN)
    print("orphan pids: " + (", ".join(str(pid) for pid in orphan_pids) if orphan_pids else "none"))
    if reap:
        for action in reap_orphans(classifications, processes):
            suffix = action.get("signal") or action.get("reason")
            print(f"reap pid={action['pid']} action={action['action']} detail={suffix}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reap", action="store_true", help="send SIGTERM to verified orphan MCP servers")
    args = parser.parse_args(argv)

    table, table_error = _process_table()
    if table_error:
        print(table_error, file=sys.stderr)
        return 1

    state_dirs, state_errors = _local_state_dirs()
    extra_dispatch_dir = os.environ.get("GOALFLIGHT_DISPATCH_DIR", "").strip()
    worker_sources = worker_pid_sources(
        state_dirs,
        extra_dispatch_dir=Path(extra_dispatch_dir) if extra_dispatch_dir else None,
    )
    commands, config_errors = registered_mcp_commands()
    entrypoints, interpreters, resolve_errors = resolve_mcp_entrypoints(commands)
    processes = _capture_processes(table, interpreters, worker_sources.pids)
    classifications = classify_processes(
        processes,
        worker_sources.pids,
        entrypoints,
        worker_sources_complete=worker_sources.complete and not state_errors,
    )
    _print_report(classifications, reap=args.reap, processes=processes)

    errors = [*state_errors, *worker_sources.errors, *config_errors, *resolve_errors]
    if errors:
        print("census uncertainty:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
