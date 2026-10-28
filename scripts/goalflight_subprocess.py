"""Subprocess helpers for commands bounded by a shared monotonic deadline."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from typing import Any


_PROCESS_CLEANUP_GRACE_SECONDS = 0.5
_MAX_COMMAND_TIMEOUT_SECONDS = 5 * 60


def _kill_process_group(process: subprocess.Popen[Any]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    with contextlib.suppress(OSError):
        process.kill()


def run_with_deadline(
    args: Any,
    *,
    deadline: float | None = None,
    timeout: float | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run a command with a capped timeout and kill its full process group.

    ``deadline`` is an absolute ``time.monotonic()`` value shared by the
    caller's whole operation. ``timeout`` is the per-command cap. Reserving a
    short cleanup window inside the shared deadline leaves time to kill the
    process group and close captured pipes before the caller unwinds.
    """
    if deadline is None:
        return subprocess.run(args, timeout=timeout, **kwargs)

    check = bool(kwargs.pop("check", False))
    capture_output = bool(kwargs.pop("capture_output", False))
    input_data = kwargs.pop("input", None)
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input_data is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE

    command_cap = min(
        timeout if timeout is not None else _MAX_COMMAND_TIMEOUT_SECONDS,
        _MAX_COMMAND_TIMEOUT_SECONDS,
    )
    remaining = deadline - time.monotonic()
    deadline_cap = remaining - _PROCESS_CLEANUP_GRACE_SECONDS
    deadline_limited = deadline_cap <= command_cap
    effective_timeout = min(command_cap, deadline_cap)
    if effective_timeout <= 0:
        if remaining > 0:
            time.sleep(remaining)
        raise subprocess.TimeoutExpired(args, max(0.0, remaining))
    if effective_timeout is not None and effective_timeout <= 0:
        raise subprocess.TimeoutExpired(args, effective_timeout)

    kwargs.setdefault("start_new_session", os.name == "posix")
    process = subprocess.Popen(args, **kwargs)
    try:
        try:
            stdout, stderr = process.communicate(
                input=input_data, timeout=effective_timeout
            )
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(process)
            try:
                stdout, stderr = process.communicate(
                    timeout=_PROCESS_CLEANUP_GRACE_SECONDS / 2
                )
            except subprocess.TimeoutExpired as cleanup_exc:
                stdout = cleanup_exc.output if cleanup_exc.output is not None else exc.output
                stderr = cleanup_exc.stderr if cleanup_exc.stderr is not None else exc.stderr
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        with contextlib.suppress(OSError):
                            pipe.close()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=_PROCESS_CLEANUP_GRACE_SECONDS / 2)
            if deadline_limited:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
            raise subprocess.TimeoutExpired(
                args,
                effective_timeout,
                output=stdout if stdout is not None else exc.output,
                stderr=stderr if stderr is not None else exc.stderr,
            ) from exc
        completed = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check:
            completed.check_returncode()
        return completed
    finally:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()


# lsof prints this three-line warning family, with exit status 0, for every
# mounted filesystem it cannot stat() (observed for smbfs volumes under
# /Volumes on 2026-10-27). It concerns device numbers for files on that mount.
# The device numbers lsof uses for matching (including +D's device/inode walk)
# come from per-file stat and libproc, not from this mount-table fallback, and
# every caller (seat cwd census, seat open-handle probe, scratch retention)
# probes local seat or state directories, so these warnings leave the answer
# complete. Any other diagnostic (an unreadable process, a permission
# error, a truncated read) keeps the answer unknown, because an omitted process
# could be using the path.
_LSOF_MOUNT_STAT_WARNING = re.compile(
    r"lsof: WARNING: can't stat\(\) \S+ file system .+"
)
_LSOF_MOUNT_STAT_CONTINUATIONS = (
    re.compile(r"Output information may be incomplete\."),
    re.compile(r'assuming "dev=[0-9A-Fa-f]+" from mount table'),
)


def lsof_stderr_is_mount_stat_warnings_only(stderr: str | bytes) -> bool:
    """True when lsof stderr holds only mount stat() warnings (or nothing)."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    # Continuation lines count only inside a group opened by a warning line:
    # lsof also prints a bare "Output information may be incomplete." when it
    # drops a mount it cannot resolve, and that one is not harmless.
    in_group = False
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _LSOF_MOUNT_STAT_WARNING.fullmatch(line):
            in_group = True
            continue
        if in_group and any(
            pattern.fullmatch(line) for pattern in _LSOF_MOUNT_STAT_CONTINUATIONS
        ):
            continue
        return False
    return True
