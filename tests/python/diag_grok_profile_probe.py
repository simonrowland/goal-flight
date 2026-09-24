#!/usr/bin/env python3
"""Probe every filesystem failure in the Grok read-only wait flow.

This is a host-only diagnostic for the macOS sandbox profile.  The temporary
``sitecustomize`` is inherited by the waiter and its detached cleanup helpers,
so failures in either process are printed and collected in one private log.
"""

from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_os_sandbox as sandbox  # noqa: E402
import goalflight_steer_mailbox as steer  # noqa: E402


SITE_CUSTOMIZE = r'''
import builtins
import io
import os
import sys


_ORIGINAL_OPEN = builtins.open
_ORIGINAL_IO_OPEN = io.open
_ORIGINAL_OS_OPEN = os.open
_ORIGINAL_OS_WRITE = os.write
_ORIGINAL_OS_CLOSE = os.close


def _display(value):
    try:
        return os.fsdecode(value)
    except (TypeError, ValueError, OSError):
        return repr(value)


def _fd_path(fd):
    try:
        return os.readlink(f"/dev/fd/{fd}")
    except (OSError, TypeError, ValueError):
        return f"<fd:{fd}>"


def _record(operation, path, flags, error):
    line = (
        "GROK-PROFILE-DIAG-FAIL "
        f"op={operation!r} path={_display(path)!r} "
        f"flags={flags!r} errno={getattr(error, 'errno', None)!r} "
        f"error={type(error).__name__}: {error}"
    )
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except BaseException:
        pass
    log_path = os.environ.get("GOALFLIGHT_DIAG_FAILURE_LOG")
    if not log_path:
        return
    try:
        fd = _ORIGINAL_OS_OPEN(
            log_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            _ORIGINAL_OS_WRITE(fd, (line + "\n").encode("utf-8", "replace"))
        finally:
            _ORIGINAL_OS_CLOSE(fd)
    except BaseException:
        pass


def _wrapped_open(*args, **kwargs):
    try:
        return _ORIGINAL_OPEN(*args, **kwargs)
    except OSError as error:
        path = args[0] if args else kwargs.get("file")
        flags = args[1] if len(args) > 1 else kwargs.get("mode")
        _record("open", path, flags, error)
        raise


def _wrapped_io_open(*args, **kwargs):
    try:
        return _ORIGINAL_IO_OPEN(*args, **kwargs)
    except OSError as error:
        path = args[0] if args else kwargs.get("file")
        flags = args[1] if len(args) > 1 else kwargs.get("mode")
        _record("open", path, flags, error)
        raise


def _wrapped_os_open(*args, **kwargs):
    try:
        return _ORIGINAL_OS_OPEN(*args, **kwargs)
    except OSError as error:
        path = args[0] if args else kwargs.get("path")
        flags = args[1] if len(args) > 1 else kwargs.get("flags")
        _record("os.open", path, flags, error)
        raise


def _wrapped_mkdir(*args, **kwargs):
    try:
        return _ORIGINAL_MKDIR(*args, **kwargs)
    except OSError as error:
        path = args[0] if args else kwargs.get("path")
        flags = args[1] if len(args) > 1 else kwargs.get("mode")
        _record("os.mkdir", path, flags, error)
        raise


def _wrapped_rename(*args, **kwargs):
    try:
        return _ORIGINAL_RENAME(*args, **kwargs)
    except OSError as error:
        path = args[:2] if len(args) >= 2 else args
        _record("os.rename", path, kwargs, error)
        raise


def _wrapped_replace(*args, **kwargs):
    try:
        return _ORIGINAL_REPLACE(*args, **kwargs)
    except OSError as error:
        path = args[:2] if len(args) >= 2 else args
        _record("os.replace", path, kwargs, error)
        raise


def _wrapped_fsync(*args, **kwargs):
    try:
        return _ORIGINAL_FSYNC(*args, **kwargs)
    except OSError as error:
        fd = args[0] if args else kwargs.get("fd")
        _record("os.fsync", _fd_path(fd), fd, error)
        raise


_ORIGINAL_MKDIR = os.mkdir
_ORIGINAL_RENAME = os.rename
_ORIGINAL_REPLACE = os.replace
_ORIGINAL_FSYNC = os.fsync
builtins.open = _wrapped_open
io.open = _wrapped_io_open
os.open = _wrapped_os_open
os.mkdir = _wrapped_mkdir
os.rename = _wrapped_rename
os.replace = _wrapped_replace
os.fsync = _wrapped_fsync
'''


def _child_code(steer_file: Path, dispatch_id: str) -> str:
    return textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(SCRIPTS)!r})
        import goalflight_steer_mailbox as steer

        def notify(event):
            if event.get('state') == 'armed':
                print('ARMED', flush=True)
            elif event.get('state') == 'messages':
                print('DELIVERED', flush=True)

        result = steer.wait_for_worker_entries(
            Path({str(steer_file)!r}),
            dispatch_id={dispatch_id!r},
            acked_seqs=set(),
            question_kind='USER-NEED',
            question_text='diagnostic reply',
            timeout_secs=5.0,
            poll_secs=0.05,
            notify=notify,
        )
        print('RESULT', result['state'], flush=True)
        """,
    )


def _read_arm(process: subprocess.Popen[str], steer_file: Path) -> dict:
    if process.stdout is None:
        raise RuntimeError("diagnostic child has no stdout")
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=10):
            raise RuntimeError("diagnostic child did not arm within 10 seconds")
        line = process.stdout.readline().strip()
    finally:
        selector.close()
    if line != "ARMED":
        raise RuntimeError(f"diagnostic child output before arm: {line!r}")
    arms = [
        entry
        for entry in steer.read_steer_entries(steer_file)
        if entry.get("kind") == steer.WORKER_WAIT_STARTED_KIND
    ]
    if not arms:
        raise RuntimeError("diagnostic child announced arm without a durable arm row")
    return arms[-1]


def _failure_lines(log_path: Path) -> list[str]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def main() -> int:
    if not sandbox.os_sandbox_available():
        print(
            "GROK-PROFILE-DIAG-FAIL sandbox-exec is unavailable on this host",
            file=sys.stderr,
        )
        return 2

    private_tmp = Path(tempfile.mkdtemp(prefix="gf-grok-profile-diag-tmp-"))
    dispatch_dir = Path(tempfile.mkdtemp(prefix="gf-grok-profile-diag-dispatch-"))
    probe_dir = ROOT / f".goalflight-grok-profile-diag-{os.getpid()}"
    steer_file = dispatch_dir / "current.steer.jsonl"
    steer_file.touch()
    dispatch_id = "grok-profile-diagnostic"
    account_home = Path.home() / ".goal-flight" / "accounts" / "probe" / "grok"
    failure_log = private_tmp / "filesystem-failures.log"
    (private_tmp / "sitecustomize.py").write_text(
        SITE_CUSTOMIZE,
        encoding="utf-8",
    )
    environment = {
        "HOME": str(account_home),
        "XDG_CONFIG_HOME": str(account_home / ".config"),
        "XDG_STATE_HOME": str(account_home / ".local" / "state"),
        "XDG_DATA_HOME": str(account_home / ".local" / "share"),
        "TMPDIR": str(private_tmp),
        "GOALFLIGHT_STEER_FILE": str(steer_file),
        "GOALFLIGHT_DIAG_FAILURE_LOG": str(failure_log),
        "PYTHONPATH": os.pathsep.join((str(private_tmp), str(SCRIPTS))),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process: subprocess.Popen[str] | None = None
    try:
        shutil.rmtree(probe_dir, ignore_errors=True)
        probe_dir.mkdir()
        prepared = sandbox.prepare_os_sandbox_command(
            sys.executable,
            ["-c", _child_code(steer_file, dispatch_id)],
            cwd=str(probe_dir),
            os_sandbox=sandbox.OS_SANDBOX_READ_ONLY,
            agent="grok-code",
            environment=environment,
        )
        print(f"GROK-PROFILE-DIAG sandbox={prepared.command}", file=sys.stderr)
        process = subprocess.Popen(
            [prepared.command, *prepared.args],
            cwd=str(probe_dir),
            env={**os.environ, **environment},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        arm = _read_arm(process, steer_file)
        reply = steer.append_worker_wait_reply(
            steer_file,
            dispatch_id=dispatch_id,
            wait_id=str(arm["question_id"]),
            text="diagnostic reply",
        )
        output, error = process.communicate(timeout=15)
        child_returncode = process.returncode
        process = None
        print(output, end="", file=sys.stderr)
        print(error, end="", file=sys.stderr)
        if child_returncode != 0 or "RESULT messages" not in output:
            raise RuntimeError("diagnostic child did not complete reply consumption")

        identity = (str(arm["question_id"]), int(reply["seq"]))
        entries: list[dict] = []
        receipts: set[tuple[str, int]] = set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            entries = steer.read_steer_entries(steer_file)
            receipts = steer.consumed_worker_wait_receipts(
                {},
                marker_entries=[],
                mailbox_path=steer_file,
            )
            has_end = any(
                entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
                for entry in entries
            )
            if identity in receipts and has_end:
                break
            time.sleep(0.05)
        failures = _failure_lines(failure_log)
        print(
            "GROK-PROFILE-DIAG result="
            f"receipts={identity in receipts} "
            f"ended={any(entry.get('kind') == steer.WORKER_WAIT_ENDED_KIND for entry in entries)} "
            f"failures={len(failures)}",
            file=sys.stderr,
        )
        for line in failures:
            print(line, file=sys.stderr)
        if failures:
            return 1
        if identity not in receipts:
            print("GROK-PROFILE-DIAG-FAIL durable reply receipt is missing", file=sys.stderr)
            return 1
        if not any(
            entry.get("kind") == steer.WORKER_WAIT_ENDED_KIND
            and entry.get("decision") == "reply"
            for entry in entries
        ):
            print("GROK-PROFILE-DIAG-FAIL reply end row is missing", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                child_output, child_error = process.communicate(timeout=5)
                if child_output:
                    print(child_output, end="", file=sys.stderr)
                if child_error:
                    print(child_error, end="", file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as child_exc:
                print(
                    f"GROK-PROFILE-DIAG child output unavailable: {child_exc}",
                    file=sys.stderr,
                )
        print(f"GROK-PROFILE-DIAG-FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        if failure_log.exists():
            for line in _failure_lines(failure_log):
                print(line, file=sys.stderr)
        return 1
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        shutil.rmtree(probe_dir, ignore_errors=True)
        shutil.rmtree(private_tmp, ignore_errors=True)
        shutil.rmtree(dispatch_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
