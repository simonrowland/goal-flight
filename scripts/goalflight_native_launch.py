#!/usr/bin/env python3
"""Resolve Codex wrapper commands to the native binaries they launch."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Mapping, Sequence


_CODEX_TARGETS = {
    ("linux", "x64"): ("x86_64-unknown-linux-musl", "@openai/codex-linux-x64"),
    ("linux", "arm64"): ("aarch64-unknown-linux-musl", "@openai/codex-linux-arm64"),
    ("android", "x64"): ("x86_64-unknown-linux-musl", "@openai/codex-linux-x64"),
    ("android", "arm64"): ("aarch64-unknown-linux-musl", "@openai/codex-linux-arm64"),
    ("darwin", "x64"): ("x86_64-apple-darwin", "@openai/codex-darwin-x64"),
    ("darwin", "arm64"): ("aarch64-apple-darwin", "@openai/codex-darwin-arm64"),
    ("win32", "x64"): ("x86_64-pc-windows-msvc", "@openai/codex-win32-x64"),
    ("win32", "arm64"): ("aarch64-pc-windows-msvc", "@openai/codex-win32-arm64"),
}
_ACP_TARGETS = {
    ("darwin", "x64"): "@zed-industries/codex-acp-darwin-x64",
    ("darwin", "arm64"): "@zed-industries/codex-acp-darwin-arm64",
    ("linux", "x64"): "@zed-industries/codex-acp-linux-x64",
    ("linux", "arm64"): "@zed-industries/codex-acp-linux-arm64",
    ("win32", "x64"): "@zed-industries/codex-acp-win32-x64",
    ("win32", "arm64"): "@zed-industries/codex-acp-win32-arm64",
}
_ARCH_ALIASES = {
    "x64": "x64",
    "x86_64": "x64",
    "amd64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
}
_MANAGED_MARKERS = (
    "CODEX_MANAGED_BY_NPM",
    "CODEX_MANAGED_BY_BUN",
    "CODEX_MANAGED_BY_PNPM",
    "CODEX_MANAGED_BY_VITE_PLUS",
)
_FILE_PREFIX_BYTES = 512
_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


@dataclass(frozen=True)
class NativeLaunch:
    """One prepared command plus the lock-ownership fact for its status."""

    argv: tuple[str, ...]
    env: dict[str, str]
    lock_holder: str | None
    warning: str | None


def _normalized_arch(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return _ARCH_ALIASES.get(raw, raw)


def _resolved_worker_cwd(cwd: str | os.PathLike[str] | None) -> Path | None:
    if cwd is None or not str(cwd).strip():
        return None
    return Path(cwd).expanduser().resolve(strict=False)


def _has_explicit_relative_path(command: str) -> bool:
    expanded = os.path.expanduser(command)
    if os.path.isabs(expanded):
        return False
    separators = {os.sep}
    if os.altsep:
        separators.add(os.altsep)
    return any(
        expanded == prefix
        or any(expanded.startswith(prefix + sep) for sep in separators)
        for prefix in (".", "..")
    )


def _worker_command_path(
    command: str,
    env: Mapping[str, str],
    cwd: str | os.PathLike[str] | None,
) -> tuple[Path | None, str | None]:
    """Resolve a command as ``execve`` would from the worker's cwd and PATH."""

    command_path = Path(os.path.expanduser(command))
    worker_cwd = _resolved_worker_cwd(cwd)
    if command_path.is_absolute():
        return command_path, None
    if command_path.parent != Path(".") or _has_explicit_relative_path(command):
        if worker_cwd is None:
            return None, f"relative command {command!r} requires worker cwd"
        return worker_cwd / command_path, None

    path_value = env.get("PATH")
    if path_value is None:
        path_value = os.defpath
    absolute_path_entries: list[str] = []
    for raw_entry in str(path_value).split(os.pathsep):
        entry = Path(raw_entry or ".")
        if not entry.is_absolute():
            if worker_cwd is None:
                return None, f"relative PATH entry {raw_entry!r} requires worker cwd"
            entry = worker_cwd / entry
        absolute_path_entries.append(str(entry))
    found = shutil.which(
        str(command_path),
        path=os.pathsep.join(absolute_path_entries),
    )
    if not found:
        return None, f"{command!r} is not on the worker PATH"
    return Path(found), None


def _entrypoint_paths(
    command: str,
    env: Mapping[str, str],
    cwd: str | os.PathLike[str] | None,
) -> tuple[Path | None, Path | None, str]:
    """Return lexical and real command paths, plus a useful failure reason."""

    if not command:
        return None, None, "empty command"
    command_path, command_error = _worker_command_path(command, env, cwd)
    if command_path is None:
        return None, None, command_error or f"could not resolve {command!r}"
    lexical = Path(os.path.abspath(command_path))
    resolved = lexical.resolve(strict=False)
    if not resolved.exists():
        return lexical, resolved, f"wrapper does not exist at {resolved}"
    return lexical, resolved, ""


def _ancestor_dirs(start: Path) -> list[Path]:
    current = start
    result: list[Path] = []
    while True:
        result.append(current)
        if current.parent == current:
            return result
        current = current.parent


def _resolve_node_package_entry(
    package_starts: Sequence[Path], package_name: str, relative_path: str
) -> Path | None:
    """Mirror Node's upward ``node_modules`` lookup from a module entrypoint."""

    seen: set[Path] = set()
    for start in package_starts:
        for base in _ancestor_dirs(start):
            candidate = base / "node_modules" / package_name / relative_path
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.exists():
                return candidate.resolve(strict=False)
    return None


def _target_for(
    targets: Mapping[tuple[str, str], object],
    *,
    platform_name: str | None,
    arch: str | None,
) -> tuple[object | None, str | None]:
    current_platform = platform_name or sys.platform
    current_arch = _normalized_arch(arch)
    target = targets.get((current_platform, current_arch))
    if target is None:
        return None, f"unsupported platform/architecture {current_platform} ({current_arch})"
    return target, None


def _wrapper_context(
    command: str,
    env: Mapping[str, str],
    cwd: str | os.PathLike[str] | None,
) -> tuple[Path | None, Path | None, Path | None, str | None]:
    lexical, resolved, reason = _entrypoint_paths(command, env, cwd)
    if lexical is None or resolved is None:
        return lexical, resolved, None, reason
    package_root = resolved.parent.parent
    if not package_root.exists():
        return lexical, resolved, package_root, f"Codex package root does not exist at {package_root}"
    return lexical, resolved, package_root, None


def _resolve_codex_native_details(
    command: str,
    *,
    env: Mapping[str, str],
    platform_name: str | None,
    arch: str | None,
    cwd: str | os.PathLike[str] | None,
) -> tuple[Path | None, Path | None, str | None]:
    lexical, resolved, package_root, context_error = _wrapper_context(command, env, cwd)
    if context_error:
        return None, package_root, context_error
    assert lexical is not None and resolved is not None and package_root is not None
    resolved_arch, arch_error = _resolve_node_arch(
        resolved, env=env, cwd=cwd, requested_arch=arch
    )
    if arch_error:
        return None, package_root, arch_error
    target, target_error = _target_for(
        _CODEX_TARGETS, platform_name=platform_name, arch=resolved_arch
    )
    if target_error:
        return None, package_root, target_error
    target_triple, package_name = target  # type: ignore[misc]
    package_json = _resolve_node_package_entry(
        (resolved.parent,), package_name, "package.json"
    )
    vendor_root = (
        package_json.parent / "vendor"
        if package_json is not None
        else package_root / "vendor"
    )
    executable_name = "codex.exe" if (platform_name or sys.platform) == "win32" else "codex"
    native = vendor_root / str(target_triple) / "bin" / executable_name
    if not native.exists():
        package_detail = (
            f"optional package {package_name!r} was not resolved"
            if package_json is None
            else f"native binary is missing at {native}"
        )
        return None, package_root, package_detail
    return native.resolve(strict=False), package_root, None


def resolve_codex_native_binary(
    command: str,
    *,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    arch: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Resolve a configured ``codex`` wrapper using the Node wrapper layout."""

    base_env = dict(os.environ if env is None else env)
    native, _package_root, _reason = _resolve_codex_native_details(
        command,
        env=base_env,
        platform_name=platform_name,
        arch=arch,
        cwd=cwd,
    )
    return native


def _resolve_codex_acp_native_details(
    command: str,
    *,
    env: Mapping[str, str],
    platform_name: str | None,
    arch: str | None,
    cwd: str | os.PathLike[str] | None,
) -> tuple[Path | None, str | None]:
    lexical, resolved, _package_root, context_error = _wrapper_context(command, env, cwd)
    if context_error:
        return None, context_error
    assert lexical is not None and resolved is not None
    resolved_arch, arch_error = _resolve_node_arch(
        resolved, env=env, cwd=cwd, requested_arch=arch
    )
    if arch_error:
        return None, arch_error
    package_name, target_error = _target_for(
        _ACP_TARGETS, platform_name=platform_name, arch=resolved_arch
    )
    if target_error:
        return None, target_error
    executable_name = (
        "codex-acp.exe" if (platform_name or sys.platform) == "win32" else "codex-acp"
    )
    native = _resolve_node_package_entry(
        (resolved.parent,),
        str(package_name),
        f"bin/{executable_name}",
    )
    if native is None:
        return None, f"native binary {package_name}/bin/{executable_name} was not resolved"
    return native, None


def _read_file_prefix(path: Path, limit: int = _FILE_PREFIX_BYTES) -> bytes:
    try:
        with path.open("rb") as stream:
            return stream.read(limit)
    except OSError:
        return b""


def _platform_binary_magic(prefix: bytes, platform_name: str | None) -> bool:
    platform_key = platform_name or sys.platform
    if platform_key == "darwin":
        return prefix[:4] in _MACHO_MAGICS
    if platform_key in {"linux", "android"}:
        return prefix.startswith(b"\x7fELF")
    if platform_key == "win32":
        return prefix.startswith(b"MZ")
    return False


def _looks_like_binary(prefix: bytes) -> bool:
    return (
        prefix.startswith(b"\x7fELF")
        or prefix[:4] in _MACHO_MAGICS
        or prefix.startswith(b"MZ")
    )


def _node_process_arch(
    wrapper: Path,
    *,
    env: Mapping[str, str],
    cwd: str | os.PathLike[str] | None,
) -> tuple[str | None, str | None]:
    """Read architecture from the Node binary that executes the wrapper."""

    node_command = "node"
    prefix = _read_file_prefix(wrapper)
    if _looks_like_binary(prefix):
        return None, "wrapper is not a Node entrypoint"
    first_line = (
        prefix.splitlines()[0].decode("utf-8", errors="replace") if prefix else ""
    )
    if first_line.startswith("#!"):
        try:
            shebang = shlex.split(first_line[2:])
        except ValueError:
            shebang = []
        if shebang:
            if Path(shebang[0]).name == "env" and len(shebang) > 1:
                node_command = next(
                    (part for part in shebang[1:] if not part.startswith("-")),
                    "",
                )
            else:
                node_command = shebang[0]
        if not node_command or Path(node_command).name.lower() not in {"node", "nodejs"}:
            return None, "wrapper is not a Node entrypoint"
    elif wrapper.suffix.lower() not in {".js", ".cjs", ".mjs"}:
        return None, "wrapper is not a Node entrypoint"
    node_path, node_error = _worker_command_path(node_command, env, cwd)
    if node_path is None:
        return None, node_error or "could not resolve the wrapper's Node binary"
    try:
        result = subprocess.run(
            [str(node_path), "-p", "process.arch"],
            cwd=str(_resolved_worker_cwd(cwd)) if cwd is not None else None,
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not inspect Node architecture: {type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return None, f"Node architecture probe exited {result.returncode}"
    value = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not value:
        return None, "Node architecture probe returned no value"
    return _normalized_arch(value), None


def _resolve_node_arch(
    wrapper: Path,
    *,
    env: Mapping[str, str],
    cwd: str | os.PathLike[str] | None,
    requested_arch: str | None,
) -> tuple[str | None, str | None]:
    if requested_arch is not None:
        return _normalized_arch(requested_arch), None
    return _node_process_arch(wrapper, env=env, cwd=cwd)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False


def _is_pnpm_owned_codex_install(node_modules_dir: Path, package_root: Path) -> bool:
    if not (node_modules_dir / ".modules.yaml").exists():
        return False
    candidate = node_modules_dir / "@openai" / "codex"
    return candidate.exists() and _same_path(candidate, package_root)


def _is_vite_plus_owned_codex_install(packages_dir: Path, package_root: Path) -> bool:
    if packages_dir.name != "packages":
        return False
    metadata_path = packages_dir / "@openai" / "codex.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    if not isinstance(metadata, dict) or metadata.get("name") != "@openai/codex":
        return False
    install_id = str(metadata.get("installId") or "")
    if install_id.startswith("#"):
        install_dir = packages_dir / f"@openai/codex{install_id}"
    else:
        install_dir = packages_dir / "@openai" / "codex" / install_id
    for node_modules_dir in (
        install_dir / "lib" / "node_modules",
        install_dir / "node_modules",
    ):
        candidate = node_modules_dir / "@openai" / "codex"
        if candidate.exists() and _same_path(candidate, package_root):
            return True
    return False


def _detect_codex_package_manager(
    package_root: Path,
    lexical_entrypoint_dir: Path,
    env: Mapping[str, str],
    *,
    resolved_entrypoint_dir: Path | None = None,
) -> str | None:
    starts = (package_root, lexical_entrypoint_dir)
    for start in starts:
        filesystem_root = Path(start.anchor)
        current = start
        while current != filesystem_root:
            if _is_vite_plus_owned_codex_install(current, package_root):
                return "vite-plus"
            if _is_pnpm_owned_codex_install(current / "node_modules", package_root):
                return "pnpm"
            current = current.parent
        if _is_pnpm_owned_codex_install(filesystem_root / "node_modules", package_root):
            return "pnpm"
    user_agent = str(env.get("npm_config_user_agent") or "")
    if re.search(r"\bbun/", user_agent):
        return "bun"
    exec_path = str(env.get("npm_execpath") or "")
    if "bun" in exec_path:
        return "bun"
    # codex.js checks __dirname here. __dirname is the canonical wrapper
    # directory, so a symlinked ~/.bun/bin/codex still receives Bun metadata.
    entrypoint_text = str(resolved_entrypoint_dir or package_root)
    if ".bun/install/global" in entrypoint_text or ".bun\\install\\global" in entrypoint_text:
        return "bun"
    return "npm" if user_agent else None


def _codex_wrapper_env(
    env: Mapping[str, str],
    package_root: Path,
    lexical_entrypoint_dir: Path,
    resolved_entrypoint_dir: Path | None = None,
) -> dict[str, str]:
    """Reproduce codex.js's managed-install environment exactly."""

    child_env = dict(env)
    child_env["CODEX_MANAGED_PACKAGE_ROOT"] = str(package_root)
    for marker in _MANAGED_MARKERS:
        child_env.pop(marker, None)
    manager = _detect_codex_package_manager(
        package_root,
        lexical_entrypoint_dir,
        child_env,
        resolved_entrypoint_dir=resolved_entrypoint_dir,
    )
    marker = {
        "bun": "CODEX_MANAGED_BY_BUN",
        "pnpm": "CODEX_MANAGED_BY_PNPM",
        "vite-plus": "CODEX_MANAGED_BY_VITE_PLUS",
    }.get(manager, "CODEX_MANAGED_BY_NPM")
    child_env[marker] = "1"
    return child_env


def _fallback(
    kind: str,
    command: str,
    args: Sequence[str],
    env: Mapping[str, str],
    reason: str,
) -> NativeLaunch:
    warning = (
        f"goalflight_native_launch: WARNING: native {kind} binary resolution "
        f"failed for {command!r}: {reason}; falling back to wrapper "
        f"(lock_holder=wrapper)"
    )
    print(warning, file=sys.stderr, flush=True)
    return NativeLaunch(
        argv=(str(command), *[str(arg) for arg in args]),
        env=dict(env),
        lock_holder="wrapper",
        warning=warning,
    )


def _native_target_suffixes(
    *, acp: bool, platform_name: str | None, arch: str | None
) -> tuple[tuple[str, ...], ...]:
    platform_key = platform_name or sys.platform
    arch_key = _normalized_arch(arch) if arch is not None else None
    targets = _ACP_TARGETS if acp else _CODEX_TARGETS
    suffixes: list[tuple[str, ...]] = []
    for (candidate_platform, candidate_arch), target in targets.items():
        if candidate_platform != platform_key:
            continue
        if arch_key is not None and candidate_arch != arch_key:
            continue
        if acp:
            executable = "codex-acp.exe" if platform_key == "win32" else "codex-acp"
            suffixes.append(
                (
                    "node_modules",
                    *str(target).split("/"),
                    "bin",
                    executable,
                )
            )
        else:
            target_triple, package_name = target  # type: ignore[misc]
            executable = "codex.exe" if platform_key == "win32" else "codex"
            suffixes.append(
                (
                    "node_modules",
                    *str(package_name).split("/"),
                    "vendor",
                    str(target_triple),
                    "bin",
                    executable,
                )
            )
    return tuple(suffixes)


def _path_has_suffix(path: Path, suffix: tuple[str, ...]) -> bool:
    parts = path.parts
    return len(parts) >= len(suffix) and parts[-len(suffix) :] == suffix


def _native_command_path(
    command: str,
    *,
    acp: bool,
    env: Mapping[str, str],
    cwd: str | os.PathLike[str] | None,
    platform_name: str | None,
    arch: str | None,
) -> Path | None:
    _lexical, resolved, _reason = _entrypoint_paths(command, env, cwd)
    if resolved is None or not resolved.is_file():
        return None
    if not any(
        _path_has_suffix(resolved, suffix)
        for suffix in _native_target_suffixes(
            acp=acp, platform_name=platform_name, arch=arch
        )
    ):
        return None
    return resolved


def prepare_codex_launch(
    command: str,
    args: Sequence[str] = (),
    *,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    arch: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> NativeLaunch:
    """Prepare direct native Codex execution, with loud safe fallback."""

    base_env = dict(os.environ if env is None else env)
    native_command_path = _native_command_path(
        command,
        acp=False,
        env=base_env,
        cwd=cwd,
        platform_name=platform_name,
        arch=arch,
    )
    if native_command_path is not None:
        if not _platform_binary_magic(
            _read_file_prefix(native_command_path), platform_name
        ):
            return _fallback(
                "Codex",
                command,
                args,
                base_env,
                "command path matches the native package layout but is not a platform binary",
            )
        return NativeLaunch(
            argv=(str(command), *[str(arg) for arg in args]),
            env=base_env,
            lock_holder="native",
            warning=None,
        )
    native, package_root, reason = _resolve_codex_native_details(
        command,
        env=base_env,
        platform_name=platform_name,
        arch=arch,
        cwd=cwd,
    )
    if native is None or package_root is None:
        return _fallback(
            "Codex", command, args, base_env, reason or "unknown resolution failure"
        )
    lexical, resolved, _reason = _entrypoint_paths(command, base_env, cwd)
    assert lexical is not None and resolved is not None
    child_env = _codex_wrapper_env(
        base_env,
        package_root,
        lexical.parent,
        resolved_entrypoint_dir=resolved.parent,
    )
    return NativeLaunch(
        argv=(str(native), *[str(arg) for arg in args]),
        env=child_env,
        lock_holder="native",
        warning=None,
    )


def prepare_codex_acp_launch(
    command: str,
    args: Sequence[str] = (),
    *,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    arch: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> NativeLaunch:
    """Prepare direct codex-acp execution; its wrapper adds no env values."""

    base_env = dict(os.environ if env is None else env)
    native_command_path = _native_command_path(
        command,
        acp=True,
        env=base_env,
        cwd=cwd,
        platform_name=platform_name,
        arch=arch,
    )
    if native_command_path is not None:
        if not _platform_binary_magic(
            _read_file_prefix(native_command_path), platform_name
        ):
            return _fallback(
                "codex-acp",
                command,
                args,
                base_env,
                "command path matches the native package layout but is not a platform binary",
            )
        return NativeLaunch(
            argv=(str(command), *[str(arg) for arg in args]),
            env=base_env,
            lock_holder="native",
            warning=None,
        )
    native, reason = _resolve_codex_acp_native_details(
        command,
        env=base_env,
        platform_name=platform_name,
        arch=arch,
        cwd=cwd,
    )
    if native is None:
        return _fallback(
            "codex-acp", command, args, base_env, reason or "unknown resolution failure"
        )
    return NativeLaunch(
        argv=(str(native), *[str(arg) for arg in args]),
        env=base_env,
        lock_holder="native",
        warning=None,
    )


def prepare_codex_launch_for_command(
    command: str,
    args: Sequence[str] = (),
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
    arch: str | None = None,
) -> NativeLaunch | None:
    """Choose the native resolver from the configured executable name."""

    name = Path(os.path.expanduser(str(command))).name.lower()
    if name in {"codex-acp", "codex-acp.js", "codex-acp.exe"}:
        return prepare_codex_acp_launch(
            command,
            args,
            env=env,
            cwd=cwd,
            platform_name=platform_name,
            arch=arch,
        )
    if name in {"codex", "codex.js", "codex.exe"}:
        return prepare_codex_launch(
            command,
            args,
            env=env,
            cwd=cwd,
            platform_name=platform_name,
            arch=arch,
        )
    return None
