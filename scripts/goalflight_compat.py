"""Cross-platform compatibility shim for goal-flight scripts.

Why this exists: the read/plan layer eagerly ``import fcntl`` and calls
``os.getuid()`` / ``os.kill(pid, 0)`` at import time or in liveness probes. All
three break on native Windows (``os.name == "nt"``):

  * ``fcntl`` does not exist on Windows -> ``import`` of the whole status layer
    raises ``ModuleNotFoundError`` before anything runs.
  * ``os.getuid()`` does not exist on Windows -> ``AttributeError`` at import for
    module-level state-dir computations.
  * ``os.kill(pid, 0)`` is NOT a liveness probe on Windows: signal ``0`` collides
    with ``CTRL_C_EVENT``, so CPython routes it to
    ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`` -> for a normal worker pid it
    RAISES ``OSError`` WinError 87, and for a ``CREATE_NEW_PROCESS_GROUP`` console
    leader it delivers a real Ctrl+C. Used as ``try: os.kill(pid,0)`` it reads
    every pid as dead (false-dead lease release). See
    ``docs-private/research/2026-05-29-windows-CONVERGED-HANDOFF.md`` P0-A.

This module is a drop-in subset of ``fcntl`` (``flock`` + ``LOCK_*``) PLUS the
Windows-safe helpers ``is_windows()`` / ``pid_alive()`` / ``default_state_dir()`` /
``resolve_state_dir()``.
POSIX behavior is preserved byte-for-byte (paths stay under ``/tmp``; ``flock``
delegates to the real ``fcntl``; ``pid_alive`` is the same ``os.kill(pid, 0)``).
The Windows branches use stdlib ``ctypes`` / ``msvcrt`` only -- zero new wheels.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "is_windows",
    "is_macos",
    "is_linux",
    "is_wsl",
    "is_wsl_drvfs_path",
    "python_executable",
    "is_test_mode",
    "env_override_warning",
    "allowed_env_override",
    "path_is_under",
    "windows_dispatch_refusal",
    "windows_os_sandbox_refusal",
    "windows_hooks_skip",
    "windows_watcher_skip",
    "wsl_decline_stamp_path",
    "wsl_install_declined",
    "record_wsl_install_declined",
    "probe_wsl",
    "LOCK_EX",
    "LOCK_SH",
    "LOCK_NB",
    "LOCK_UN",
    "flock",
    "pid_alive",
    "windows_process_identity",
    "kill_pid",
    "default_state_dir",
    "temp_base",
    "nearest_existing_path",
    "safe_dispatch_filename",
    "tokenize_args",
]


log = logging.getLogger("goal-flight.compat")

_WSL_WINDOWS_FS_TYPES = {"drvfs", "9p", "v9fs"}


def safe_dispatch_filename(dispatch_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in dispatch_id)
    if safe != dispatch_id:
        safe = f"{safe}-{hashlib.sha256(dispatch_id.encode()).hexdigest()[:8]}"
    return safe


def tokenize_args(values: Iterable[str]) -> list[str]:
    tokens: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            tokens.extend(shlex.split(value))
        except ValueError:
            tokens.append(value)
    return tokens


def is_windows() -> bool:
    """True on native Windows (``os.name == "nt"``). NOT true under WSL."""
    return os.name == "nt"


def is_macos() -> bool:
    """True on macOS/Darwin."""
    return sys.platform == "darwin"


def is_linux() -> bool:
    """True on Linux, including WSL."""
    return sys.platform.startswith("linux")


def is_wsl() -> bool:
    """True when Python is already running inside WSL, not native Windows.

    WSL reports as Linux/POSIX to Python, so ``is_windows()`` must stay false
    there. If this misdetects on a real WSL box, check
    ``/proc/sys/kernel/osrelease`` and ``/proc/version`` first; WSL1/WSL2 both
    include a Microsoft/WSL marker in one of those files.
    """
    if is_windows() or sys.platform != "linux":
        return False
    for marker_path in (Path("/proc/sys/kernel/osrelease"), Path("/proc/version")):
        try:
            text = marker_path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if "microsoft" in text or "wsl" in text:
            return True
    return False


def _syntactic_wsl_drive_path(path: Path) -> bool:
    parts = path.parts
    return (
        len(parts) >= 3
        and parts[0] == "/"
        and parts[1] == "mnt"
        and len(parts[2]) == 1
        and parts[2].isalpha()
    )


def nearest_existing_path(path: Path) -> Path | None:
    current = path.expanduser()
    while True:
        if current.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _nearest_existing_path(path: Path) -> Path | None:
    return nearest_existing_path(path)


def _decode_mountinfo_path(value: str) -> str:
    return (
        value.replace(r"\040", " ")
        .replace(r"\011", "\t")
        .replace(r"\012", "\n")
        .replace(r"\134", "\\")
    )


def _mount_path_matches(target: Path, mount_point: str) -> bool:
    target_text = os.path.normpath(str(target))
    mount_text = os.path.normpath(mount_point)
    if mount_text == os.sep:
        return target_text.startswith(os.sep)
    return target_text == mount_text or target_text.startswith(f"{mount_text}{os.sep}")


def _mountinfo_fstype_from_lines(path: Path, lines: list[str]) -> str | None:
    best_mount = ""
    best_fstype: str | None = None
    for line in lines:
        before, sep, after = line.partition(" - ")
        if not sep:
            continue
        fields = before.split()
        after_fields = after.split()
        if len(fields) < 5 or not after_fields:
            continue
        mount_point = _decode_mountinfo_path(fields[4])
        if not _mount_path_matches(path, mount_point):
            continue
        normalized = os.path.normpath(mount_point)
        if len(normalized) > len(best_mount):
            best_mount = normalized
            best_fstype = after_fields[0].lower()
    return best_fstype


def _mountinfo_fstype_for_path(path: Path) -> str | None:
    try:
        lines = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return None
    return _mountinfo_fstype_from_lines(path, lines)


def _findmnt_fstype_for_path(path: Path) -> str | None:
    if shutil.which("findmnt") is None:
        return None
    try:
        proc = subprocess.run(
            ["findmnt", "-T", str(path), "-n", "-o", "FSTYPE"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    first = proc.stdout.splitlines()[0].strip().lower() if proc.stdout.splitlines() else ""
    return first or None


def _mount_fstype_for_path(path: Path) -> str | None:
    return _mountinfo_fstype_for_path(path) or _findmnt_fstype_for_path(path)


def is_wsl_drvfs_path(path: str | os.PathLike[str] | None) -> bool:
    """True when a WSL path is backed by a Windows filesystem mount.

    Prefer mount fstype evidence (``drvfs`` / ``9p`` / ``v9fs``) so custom
    automounts and symlinked roots are detected without false-warning on a real
    Linux filesystem mounted under ``/mnt/<letter>``. If mount metadata is not
    available, retain the legacy syntactic ``/mnt/<drive>`` fallback.
    """
    if path is None:
        return False
    original = Path(path).expanduser()
    nearest = _nearest_existing_path(original)
    target = nearest or original
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    fstype = _mount_fstype_for_path(resolved)
    original_syntactic = _syntactic_wsl_drive_path(original)
    target_syntactic = _syntactic_wsl_drive_path(target)
    resolved_syntactic = _syntactic_wsl_drive_path(resolved)
    if fstype:
        if fstype.lower() in _WSL_WINDOWS_FS_TYPES:
            return True
        if target_syntactic or resolved_syntactic or not original_syntactic:
            return False
    return original_syntactic or target_syntactic or resolved_syntactic


def python_executable() -> str:
    """Python executable for internal re-invokes, overrideable for launchers."""
    # Env interpreter selectors are accepted-watch per the SC-13 sweep: command
    # source overrides, but outside the source/write/safety-disable predicate.
    return os.environ.get("GOALFLIGHT_PYTHON") or sys.executable


def is_test_mode() -> bool:
    """True only when test-only env hooks may affect runtime behavior."""
    return os.environ.get("GOALFLIGHT_TEST_MODE") == "1"


def env_override_warning(
    env_name: str,
    action: str,
    reason: str,
    *,
    source: str | os.PathLike[str] | None = None,
    extra: dict[str, object] | None = None,
    stream=None,
) -> None:
    """Emit the uniform one-line env override warning/status convention."""
    if stream is None:
        stream = sys.stderr

    def field(key: str, value: object) -> str:
        return f"{key}={shlex.quote(str(value))}"

    parts = [
        "GOALFLIGHT_ENV_OVERRIDE",
        field("env", env_name),
        field("action", action),
        field("reason", reason),
    ]
    if source is not None:
        parts.append(field("source", source))
    for key, value in (extra or {}).items():
        parts.append(field(key, value))
    print(" ".join(parts), file=stream)


def allowed_env_override(
    env_name: str,
    allow_env: str,
    *,
    test_mode: bool = False,
    source: str | os.PathLike[str] | None = None,
    stream=None,
) -> str | None:
    """Return a gated env override value, warning when active or ignored."""
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return None
    if test_mode and is_test_mode():
        env_override_warning(
            env_name,
            "active",
            "GOALFLIGHT_TEST_MODE=1",
            source=source or raw,
            stream=stream,
        )
        return raw
    if os.environ.get(allow_env) == "1":
        env_override_warning(
            env_name,
            "active",
            f"{allow_env}=1",
            source=source or raw,
            stream=stream,
        )
        return raw
    reason = "GOALFLIGHT_TEST_MODE_not_1" if test_mode else f"{allow_env}_not_1"
    env_override_warning(
        env_name,
        "ignored",
        reason,
        source=source or raw,
        stream=stream,
    )
    return None


def path_is_under(path: str | os.PathLike[str], roots: list[str | os.PathLike[str]]) -> bool:
    """True when path resolves under one of roots; missing paths are OK."""
    candidate = Path(path).expanduser()
    try:
        candidate_resolved = candidate.resolve(strict=False)
    except OSError:
        candidate_resolved = candidate.absolute()
    for root in roots:
        root_path = Path(root).expanduser()
        try:
            root_resolved = root_path.resolve(strict=False)
        except OSError:
            root_resolved = root_path.absolute()
        try:
            candidate_resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def windows_dispatch_refusal() -> str:
    return (
        "native Windows dispatch is intentionally unsupported: run `wsl --install`, "
        "open an installed distro, and use the WSL install for dispatch. "
        "Native Windows keeps read/plan plus degraded per-pid cleanup only; "
        "there is no native Win32 dispatch port. See "
        "docs/hosts/windows.md#wsl-required-dispatch-baseline."
    )


def windows_os_sandbox_refusal() -> str:
    return (
        "OS sandbox is macOS-only; on Windows you get worktree isolation + "
        "the worker's own --sandbox. Drop --os-sandbox to proceed, or use WSL. "
        "See docs/hosts/windows.md#capability-matrix."
    )


def windows_hooks_skip() -> str:
    return (
        "context-discipline hooks are POSIX/Git-Bash-only and are not installed "
        "on native Windows; context protection is advisory here (see SKILL.md State) "
        "or use WSL. See docs/hosts/windows.md#context-discipline-hooks."
    )


def windows_watcher_skip() -> str:
    return (
        "bash-tail watcher is POSIX/Git-Bash-only and is skipped on native Windows; "
        "use WSL for dispatch watching. See docs/hosts/windows.md#capability-matrix."
    )


def wsl_decline_stamp_path(project_root: str | os.PathLike[str]) -> Path:
    """Per-project stamp recording that the operator declined WSL install.

    Init is allowed to ask once, but repeated native-Windows runs should not nag.
    The orchestrator writes this file only after an explicit user-question decline;
    doctor and init read it to decide whether to surface the WSL install prompt
    again.
    """
    return Path(project_root) / "docs-private" / "windows-wsl-install-declined.json"


def wsl_install_declined(project_root: str | os.PathLike[str] | None) -> bool:
    if project_root is None:
        return False
    try:
        return wsl_decline_stamp_path(project_root).is_file()
    except OSError:
        return False


def record_wsl_install_declined(
    project_root: str | os.PathLike[str],
    *,
    reason: str = "user_declined_wsl_install",
) -> Path:
    """Write the no-nag WSL decline stamp used by init.

    This helper exists so Windows orchestrators do not invent incompatible stamp
    formats. If writing fails on Windows, check that ``docs-private`` exists and
    that the orchestrator has write permission to the project checkout.
    """
    path = wsl_decline_stamp_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "goalflight.windows_wsl_decline.v1",
        "reason": reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _decode_wsl_list_output(raw: bytes | str | None) -> str:
    """Decode ``wsl -l -q`` output from real Windows.

    ``wsl.exe`` often emits UTF-16LE with NUL bytes even when Python expects the
    console code page. If a machine has distros but the probe sees zero, suspect
    this decode/NUL handling before changing the WSL readiness logic.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.replace("\x00", "")
    candidates = ("utf-16le", "utf-8-sig", "utf-8")
    if b"\x00" not in raw:
        candidates = ("utf-8-sig", "utf-8", "utf-16le")
    for encoding in candidates:
        try:
            text = raw.decode(encoding)
            return text.replace("\x00", "")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").replace("\x00", "")


def _wsl_no_distro_line(line: str) -> bool:
    lower = line.lower()
    if "no installed distributions" in lower:
        return True
    if "windows subsystem for linux" in lower and (
        "no installed" in lower or "has no" in lower
    ):
        return True
    # German Windows output seen in acceptance: do not let localized no-distro
    # prose survive as a fake distro name.
    if (
        "keine" in lower
        and ("distribution" in lower or "verteilung" in lower)
        and ("install" in lower or "installiert" in lower)
    ):
        return True
    if (
        ("no hay" in lower or "ninguna" in lower)
        and "distrib" in lower
        and "instalad" in lower
    ):
        return True
    if "aucune" in lower and "distribution" in lower and "install" in lower:
        return True
    if "ディストリビューション" in lower and (
        "ありません" in lower or "インストール" in lower
    ):
        return True
    return False


def _wsl_guidance_or_no_distro_line(line: str) -> bool:
    lower = line.lower()
    if _wsl_no_distro_line(line):
        return True
    guidance_markers = (
        "distributions can be installed",
        "can be installed",
        "install a distribution",
        "install distributions",
        "wsl --install",
        "wsl.exe --install",
        "microsoft store",
        "https://",
        "http://",
        "aka.ms/",
    )
    return any(marker in lower for marker in guidance_markers)


def _wsl_distro_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().strip("\ufeff")
        if not line:
            continue
        if _wsl_guidance_or_no_distro_line(line):
            continue
        lines.append(line)
    return lines


def _probe_wsl_default_launch(
    exe: str,
    *,
    runner=subprocess.run,
) -> tuple[bool, dict]:
    sentinel = "__goalflight_wsl_ready__"
    cmd = [exe, "-e", "sh", "-lc", f"printf {sentinel}"]
    try:
        proc = runner(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, {"launch_error": f"{type(exc).__name__}: {exc}"}
    stdout = _decode_wsl_list_output(getattr(proc, "stdout", b""))
    stderr = _decode_wsl_list_output(getattr(proc, "stderr", b""))
    return (
        getattr(proc, "returncode", 1) == 0 and sentinel in stdout,
        {
            "launch_returncode": getattr(proc, "returncode", None),
            "launch_stdout": stdout.strip()[:1000],
            "launch_stderr": stderr.strip()[:1000],
        },
    )


def probe_wsl(
    project_root: str | os.PathLike[str] | None = None,
    *,
    runner=subprocess.run,
    which=shutil.which,
) -> dict:
    """Probe whether native Windows can hand dispatch to an installed WSL distro.

    Native Windows is allowed to run the control plane, but full worker dispatch
    must run *inside* WSL. ``wsl.exe`` by itself is insufficient: Windows can have
    the feature enabled with zero installed distros, and that state cannot run a
    POSIX worker. If Monday's acceptance box reports ``no_installed_distributions``
    despite Ubuntu being installed, inspect the decoded ``stdout`` / ``stderr``
    fields in doctor JSON for encoding or enterprise-policy output.
    """
    declined = wsl_install_declined(project_root)
    base = {
        "schema": "goalflight.wsl_probe.v1",
        "is_windows": is_windows(),
        "is_wsl": is_wsl(),
        "usable": False,
        "present": False,
        "wsl_exe_present": False,
        "wsl_exe": None,
        "distributions": [],
        "declined": declined,
        "decline_stamp": str(wsl_decline_stamp_path(project_root)) if project_root else None,
        "install_command": "wsl --install",
        "requires_admin": True,
        "requires_reboot": True,
    }
    if not is_windows():
        base.update(
            {
                "state": "running_under_wsl" if is_wsl() else "not_native_windows",
                "usable": is_wsl(),
                "present": is_wsl(),
                "reason": "already inside WSL" if is_wsl() else "not native Windows",
            }
        )
        return base

    exe = which("wsl.exe") or which("wsl")
    if not exe:
        base.update(
            {
                "state": "missing_executable",
                "reason": "wsl.exe not found on PATH",
                "next_step": "Ask before running `wsl --install` (admin elevation and reboot may be required).",
            }
        )
        return base

    base["wsl_exe_present"] = True
    base["wsl_exe"] = exe
    try:
        # Keep this as bytes. ``text=True`` can misdecode UTF-16LE/NUL output
        # from ``wsl -l -q`` and produce a false "no distro" diagnosis.
        proc = runner(
            [exe, "-l", "-q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        base.update(
            {
                "state": "probe_failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "next_step": "Run `wsl -l -q` manually in PowerShell and inspect stderr.",
            }
        )
        return base

    stdout = _decode_wsl_list_output(getattr(proc, "stdout", b""))
    stderr = _decode_wsl_list_output(getattr(proc, "stderr", b""))
    distros = _wsl_distro_lines(stdout)
    no_distro_marker = any(
        _wsl_no_distro_line(line)
        for text in (stdout, stderr)
        for line in text.splitlines()
    )
    base.update(
        {
            "returncode": getattr(proc, "returncode", None),
            "stdout": stdout.strip()[:1000],
            "stderr": stderr.strip()[:1000],
            "distributions": distros,
        }
    )
    if distros:
        launch_ok, launch_probe = _probe_wsl_default_launch(exe, runner=runner)
        base.update(launch_probe)
        if not launch_ok:
            launch_no_distro_marker = any(
                _wsl_no_distro_line(line)
                for text in (
                    str(launch_probe.get("launch_stdout") or ""),
                    str(launch_probe.get("launch_stderr") or ""),
                )
                for line in text.splitlines()
            )
            if launch_no_distro_marker:
                base.update(
                    {
                        "state": "no_installed_distributions",
                        "usable": False,
                        "present": False,
                        "distributions": [],
                        "reason": "wsl.exe launch probe reported zero installed distros",
                        "next_step": "Ask before running `wsl --install` (admin elevation and reboot may be required).",
                    }
                )
                return base
            base.update(
                {
                    "state": "distro_launch_failed",
                    "reason": "wsl.exe listed distro(s), but default distro launch did not complete",
                    "next_step": "Open the listed distro once, then rerun doctor; inspect launch_stdout/stderr if this persists.",
                }
            )
            return base
        base.update(
            {
                "state": "ready",
                "present": True,
                "usable": True,
                "reason": "wsl.exe present and at least one installed distro listed",
                "next_step": "Open the distro and run Goal Flight dispatch from the WSL checkout.",
            }
        )
        return base

    # wsl.exe present + no distro is NOT usable. Treating it as ready would send
    # dispatch into a POSIX path that has no Linux filesystem or shell yet.
    if getattr(proc, "returncode", 1) == 0 or no_distro_marker:
        state = "no_installed_distributions"
        reason = "wsl.exe present but `wsl -l -q` listed zero installed distros"
    else:
        state = "probe_failed"
        reason = stderr.splitlines()[0] if stderr.splitlines() else "wsl -l -q failed"
    base.update(
        {
            "state": state,
            "reason": reason,
            "next_step": "Ask before running `wsl --install` (admin elevation and reboot may be required).",
        }
    )
    return base


# --------------------------------------------------------------------------- #
# flock: drop-in subset of fcntl.                                             #
# POSIX -> re-export the real fcntl. Windows -> msvcrt byte-range locking.    #
# --------------------------------------------------------------------------- #
if is_windows():  # pragma: no cover - exercised only on Windows
    import msvcrt

    # fcntl.LOCK_* numeric values are POSIX-specific; on Windows we only need a
    # self-consistent set. msvcrt has no shared lock, so LOCK_SH maps to the
    # exclusive lock (advisory note: goal-flight uses LOCK_EX|LOCK_NB in practice).
    LOCK_SH = 0x1
    LOCK_EX = 0x2
    LOCK_NB = 0x4
    LOCK_UN = 0x8

    # msvcrt.locking() locks ``nbytes`` from the CURRENT file position. We seek to
    # 0 and lock the whole 2GiB-1 range (the documented whole-file idiom); unlock
    # MUST match the same offset+length.
    _WHOLE_FILE = 0x7FFFFFFF

    def _fd(fd_or_file) -> int:
        return fd_or_file if isinstance(fd_or_file, int) else fd_or_file.fileno()

    def flock(fd_or_file, operation: int) -> None:
        fd = _fd(fd_or_file)
        os.lseek(fd, 0, os.SEEK_SET)
        if operation & LOCK_UN:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _WHOLE_FILE)
            return
        non_blocking = bool(operation & LOCK_NB)
        # Errnos msvcrt raises when the byte-range is already locked (contention).
        _contended = {errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
        if non_blocking:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, _WHOLE_FILE)
            except OSError as exc:
                # Contended non-blocking lock -> BlockingIOError so callers match
                # POSIX flock(LOCK_NB). Do NOT mask unrelated errors (EBADF/EINVAL).
                if exc.errno in _contended:
                    raise BlockingIOError(exc.errno, str(exc)) from exc
                raise
            return
        # Blocking lock: msvcrt.LK_LOCK only retries ~10x1s then raises, but POSIX
        # LOCK_EX blocks indefinitely -> loop until acquired so semantics match.
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, _WHOLE_FILE)
                return
            except OSError as exc:
                if exc.errno in _contended:
                    os.lseek(fd, 0, os.SEEK_SET)
                    continue
                raise
else:
    # POSIX: re-export the genuine article so behavior is identical.
    from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN, flock  # noqa: F401


# --------------------------------------------------------------------------- #
# State directory.                                                            #
# POSIX path is preserved EXACTLY (/tmp/goal-flight-<uid>) -- note: on macOS  #
# tempfile.gettempdir() is $TMPDIR (/var/folders/...), NOT /tmp, so we must   #
# NOT route POSIX through gettempdir() or we relocate existing state.         #
# Windows has no /tmp and no getuid() -> gettempdir() + USERNAME.             #
# --------------------------------------------------------------------------- #
def _uid_tag() -> str:
    if is_windows():  # pragma: no cover - Windows only
        return os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return str(os.getuid())


def temp_base() -> Path:
    """Base temp dir: ``/tmp`` on POSIX (unchanged), ``gettempdir()`` on Windows."""
    if is_windows():  # pragma: no cover - Windows only
        return Path(tempfile.gettempdir())
    return Path("/tmp")


def default_state_dir() -> Path:
    """``<temp_base>/goal-flight-<uid-or-username>``.

    On POSIX this equals the historical ``/tmp/goal-flight-<os.getuid()>`` exactly.
    Callers that honor ``$GOALFLIGHT_STATE_DIR`` should use
    ``resolve_state_dir()`` so blank values fall back consistently.
    """
    return temp_base() / f"goal-flight-{_uid_tag()}"


def resolve_state_dir() -> Path:
    """Resolve ``$GOALFLIGHT_STATE_DIR`` with blank/whitespace falling back.

    ``os.environ.get("GOALFLIGHT_STATE_DIR", default)`` does not use the default
    when the variable is set to ``""``. Returning ``Path("")`` scatters state
    into the caller's cwd, so all state-dir users go through this call-time
    resolver.
    """
    raw = os.environ.get("GOALFLIGHT_STATE_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_state_dir()


def resolve_env_path(var: str, default) -> Path:
    """Resolve a path-valued env var with blank/whitespace falling back."""
    raw = os.environ.get(var, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(default).expanduser()


def gstack_browse_bin_candidates() -> list[Path]:
    """Candidate paths for the gstack headless browse binary.

    Order: explicit GSTACK_BROWSE_BIN, Claude-host install, canonical
    ~/.gstack install. Shared by doctor, setup, and dispatch provisioning so a
    ~/.gstack-only install is not falsely reported absent (ADAPTER-4).
    """
    candidates: list[Path] = []
    raw = os.environ.get("GSTACK_BROWSE_BIN", "").strip()
    if raw:
        candidates.append(Path(raw).expanduser())
    home = Path.home()
    candidates.extend(
        [
            home / ".claude/skills/gstack/browse/dist/browse",
            home / ".gstack/repos/gstack/browse/dist/browse",
        ]
    )
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def resolve_gstack_browse_bin() -> Path | None:
    """First executable gstack browse binary among known install locations."""
    for candidate in gstack_browse_bin_candidates():
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Non-destructive liveness probe.                                            #
# POSIX: os.kill(pid, 0) (unchanged). Windows: OpenProcess +                  #
# GetExitCodeProcess -- NEVER os.kill(pid, 0). ctypes, zero wheels.          #
# --------------------------------------------------------------------------- #
def pid_alive(pid) -> bool:
    """True if ``pid`` is a live process. Non-destructive on every platform."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if not is_windows():
        # Any OSError (ProcessLookupError, PermissionError, ...) -> not alive.
        # This matches the historical per-site `except OSError: return False`
        # behavior exactly, so POSIX liveness stays byte-identical to the
        # pre-port code (goal-flight pids are same-user, so PermissionError does
        # not arise here in practice).
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    # Windows: query-only handle + exit code. A handle can still open a
    # just-exited process, so the exit-code check (STILL_ACTIVE == 259) is what
    # actually decides liveness. Access-denied means the process exists but is
    # protected -> treat as alive.  pragma: no cover - Windows only
    import ctypes  # pragma: no cover
    from ctypes import wintypes  # pragma: no cover

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # pragma: no cover
    STILL_ACTIVE = 259  # pragma: no cover
    ERROR_ACCESS_DENIED = 5  # pragma: no cover

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # pragma: no cover
    kernel32.OpenProcess.restype = wintypes.HANDLE  # pragma: no cover
    kernel32.OpenProcess.argtypes = (  # pragma: no cover
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    handle = kernel32.OpenProcess(  # pragma: no cover
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:  # pragma: no cover
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:  # pragma: no cover
        # Declare signatures so 64-bit HANDLE/exit-code marshal correctly (ctypes
        # defaults to int=32-bit, which truncates handles on 64-bit Windows).
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        if not ok:
            return True  # could not query exit code; assume alive (conservative)
        return code.value == STILL_ACTIVE
    finally:  # pragma: no cover
        kernel32.CloseHandle(handle)


def windows_process_identity(pid) -> dict | None:
    """Native-Windows process identity from creation FILETIME.

    A pid alone is unsafe for cleanup because Windows can reuse it. The creation
    time from GetProcessTimes is the minimum identity token we require before
    terminating a stale pidfile entry.
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0 or not is_windows():
        return None

    import ctypes  # pragma: no cover
    from ctypes import wintypes  # pragma: no cover

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # pragma: no cover
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # pragma: no cover
    kernel32.OpenProcess.restype = wintypes.HANDLE  # pragma: no cover
    kernel32.OpenProcess.argtypes = (  # pragma: no cover
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    handle = kernel32.OpenProcess(  # pragma: no cover
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid_int
    )
    if not handle:  # pragma: no cover
        return None
    try:  # pragma: no cover
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        creation_time = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return {
            "pid": pid_int,
            "creation_time": str(creation_time),
            "identity_source": "windows_get_process_times",
        }
    finally:  # pragma: no cover
        kernel32.CloseHandle(handle)


def _windows_identity_matches(expected_identity: dict | None, current_identity: dict | None) -> tuple[bool, str]:
    if not isinstance(expected_identity, dict):
        return False, "missing_expected_identity"
    expected = (
        expected_identity.get("creation_time")
        or expected_identity.get("creation_time_filetime")
        or expected_identity.get("create_time")
    )
    if expected in (None, ""):
        return False, "missing_expected_creation_time"
    if not isinstance(current_identity, dict):
        return False, "missing_current_identity"
    current = (
        current_identity.get("creation_time")
        or current_identity.get("creation_time_filetime")
        or current_identity.get("create_time")
    )
    if current in (None, ""):
        return False, "missing_current_creation_time"
    if str(expected) != str(current):
        return False, "creation_time_mismatch"
    return True, "matched"


def kill_pid(
    pid,
    sig: int | None = None,
    *,
    pgid=None,
    process_group: bool = True,
    expected_identity: dict | None = None,
) -> bool:
    """Best-effort stale-worker kill with native-Windows degradation.

    POSIX callers historically reap a worker's whole process group with
    ``os.killpg`` because bash-tail / ACP workers can leave child processes.
    Native Windows has no ``os.killpg`` and this project intentionally does not
    add Job Objects for a native dispatch port. A bare pid is unsafe there
    because pid reuse can target an unrelated process, so Windows kills require
    a recorded creation-time identity that still matches the live pid.
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    kill_signal = sig if sig is not None else getattr(signal, "SIGTERM", 15)

    if is_windows():  # pragma: no cover - covered by mocks on non-Windows
        current_identity = windows_process_identity(pid_int)
        identity_ok, identity_reason = _windows_identity_matches(
            expected_identity, current_identity
        )
        if not identity_ok:
            log.warning(
                "windows kill_pid skipped pid=%s reason=%s expected=%r current=%r",
                pid_int,
                identity_reason,
                expected_identity,
                current_identity,
            )
            return False
        try:
            os.kill(pid_int, kill_signal)
            return True
        except (OSError, ValueError):
            # Windows supports only a narrow signal set. OSError usually means
            # the tracked stale pid already exited or is access-denied; ValueError
            # means the requested signal is unsupported. Treat as "not killed" but
            # never as an orchestrator crash; the native control plane is degraded by
            # design.
            return False

    if process_group:
        try:
            target = int(pgid) if pgid is not None else pid_int
        except (TypeError, ValueError):
            target = pid_int
        try:
            os.killpg(target, kill_signal)
            return True
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid_int, kill_signal)
                return True
            except (ProcessLookupError, PermissionError):
                return False

    try:
        os.kill(pid_int, kill_signal)
        return True
    except (ProcessLookupError, PermissionError):
        return False
