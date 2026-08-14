#!/usr/bin/env python3
"""Kernel-backed liveness ledger for controller wake waiters.

Lockfile names are addresses and identities only.  Their contents are never
read or written: a waiter is live iff its process still holds the file lock.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shlex
import sys
import time
import uuid
from collections.abc import Callable, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - documented socket-backend platform.
    fcntl = None  # type: ignore[assignment]

import goalflight_compat


WAITER_KINDS = frozenset({"listener", "wait"})
LEASE_KIND = "lease"
LOCK_KINDS = WAITER_KINDS | {LEASE_KIND}
ENTRY_POLL_WINDOW_S = 1.0
ENTRY_POLL_INTERVAL_S = 0.1
WAITER_LOCK_CONFIRM_S = 0.01
_FILE_VERSION = "v1"


@dataclass(frozen=True)
class WaiterRecord:
    kind: str
    label_hash: str
    pid: int
    instance_id: str
    path: Path


def _project_key(project_root: Path | str) -> str:
    resolved = str(Path(project_root).expanduser().resolve(strict=False))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:20]


def _label_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:16]


def ledger_base_dir() -> Path:
    explicit = os.environ.get("GOALFLIGHT_WAKE_LEDGER_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    pidfiles = os.environ.get("GOAL_FLIGHT_PIDFILE_DIR", "").strip()
    if pidfiles:
        return Path(pidfiles).expanduser() / "wake-ledger"
    return goalflight_compat.resolve_state_dir() / "wake-ledger"


def ledger_dir(project_root: Path | str) -> Path:
    return ledger_base_dir() / _project_key(project_root)


def _parse_waiter_path(path: Path) -> WaiterRecord | None:
    parts = path.name.split(".")
    if len(parts) != 6 or parts[0] != _FILE_VERSION or parts[-1] != "lock":
        return None
    _version, kind, label_hash, raw_pid, instance_id, _suffix = parts
    if kind not in LOCK_KINDS:
        return None
    if len(label_hash) != 16 or len(instance_id) != 32:
        return None
    try:
        pid = int(raw_pid)
    except ValueError:
        return None
    if pid <= 0:
        return None
    return WaiterRecord(kind, label_hash, pid, instance_id, path)


class WaiterRegistration:
    """One held-flock address.  The kernel owns the liveness fact."""

    def __init__(self, project_root: Path | str, label: str, kind: str) -> None:
        if fcntl is None:
            raise RuntimeError("held-flock wake ledger is unavailable on this platform")
        normalized_label = str(label or "").strip()
        if not normalized_label:
            raise ValueError("waiter controller label is required")
        if kind not in LOCK_KINDS:
            raise ValueError(f"unknown held-lock kind: {kind}")
        directory = ledger_dir(project_root)
        directory.mkdir(parents=True, exist_ok=True)
        instance_id = uuid.uuid4().hex
        self.record = WaiterRecord(
            kind=kind,
            label_hash=_label_hash(normalized_label),
            pid=os.getpid(),
            instance_id=instance_id,
            path=directory
            / f"{_FILE_VERSION}.{kind}.{_label_hash(normalized_label)}."
            f"{os.getpid()}.{instance_id}.lock",
        )
        pending_path = directory / f".{self.record.path.name}.{uuid.uuid4().hex}.pending"
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._fd = os.open(pending_path, flags, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Publish only after the kernel witness exists.  A concurrent probe
            # can therefore never acquire-and-prune a not-yet-locked address.
            os.replace(pending_path, self.record.path)
        except BaseException:
            os.close(self._fd)
            self._fd = -1
            pending_path.unlink(missing_ok=True)
            self.record.path.unlink(missing_ok=True)
            raise

    def close(self) -> None:
        fd = self._fd
        if fd < 0:
            return
        self._fd = -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            self.record.path.unlink(missing_ok=True)

    def __enter__(self) -> "WaiterRegistration":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - process death is authority.
        try:
            self.close()
        except Exception:
            pass


def register_waiter(
    project_root: Path | str,
    *,
    controller_label: str,
    kind: str,
) -> WaiterRegistration:
    if kind not in WAITER_KINDS:
        raise ValueError(f"unknown waiter kind: {kind}")
    return WaiterRegistration(project_root, controller_label, kind)


def _lease_lock_identity(label: str, nonce: str) -> str:
    normalized_label = str(label or "").strip()
    normalized_nonce = str(nonce or "").strip()
    if not normalized_label or not normalized_nonce:
        raise ValueError("controller lease label and nonce are required")
    return f"{normalized_label}\0{normalized_nonce}"


def register_lease_holder(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> WaiterRegistration:
    """Hold the kernel liveness witness for one controller lease generation."""
    return WaiterRegistration(
        project_root,
        _lease_lock_identity(controller_label, lease_nonce),
        LEASE_KIND,
    )


def _probe_locked_once(path: Path) -> bool:
    """Take one kernel-lock sample; file existence/content proves nothing."""
    if fcntl is None:
        return False
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {11, 35}:
                return True
            return False
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def _probe_locked(path: Path, *, confirm_waiter: bool = False) -> bool:
    """Return stable kernel-lock state without consulting PID identity.

    ``flock`` survives ``fork`` until a close-on-exec descriptor reaches exec.
    A killed waiter can therefore leave a pre-exec child holding its lock for a
    scheduler tick under heavy load.  Lease holders do not fork and retain the
    exact one-sample rule; waiter coverage requires a second refusal so that a
    transient inherited descriptor cannot claim wake coverage.
    """
    if not _probe_locked_once(path):
        return False
    if not confirm_waiter:
        return True
    time.sleep(WAITER_LOCK_CONFIRM_S)
    return _probe_locked_once(path)


def live_waiters(
    project_root: Path | str,
    *,
    controller_label: str | None = None,
    kinds: Iterable[str] = WAITER_KINDS,
    prune_dead: bool = True,
) -> list[WaiterRecord]:
    directory = ledger_dir(project_root)
    try:
        paths = list(directory.glob(f"{_FILE_VERSION}.*.lock"))
    except OSError:
        return []
    accepted_kinds = set(kinds)
    wanted_label = _label_hash(controller_label) if controller_label else None
    live: list[WaiterRecord] = []
    for path in paths:
        record = _parse_waiter_path(path)
        if record is None or record.kind not in accepted_kinds:
            continue
        if wanted_label is not None and record.label_hash != wanted_label:
            continue
        if _probe_locked(path, confirm_waiter=record.kind in WAITER_KINDS):
            live.append(record)
        elif prune_dead:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return sorted(live, key=lambda row: (row.kind, row.pid, row.instance_id))


def lease_holder_alive(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    prune_dead: bool = True,
) -> bool:
    """Return kernel lock state for one exact lease generation."""
    return bool(
        live_waiters(
            project_root,
            controller_label=_lease_lock_identity(controller_label, lease_nonce),
            kinds={LEASE_KIND},
            prune_dead=prune_dead,
        )
    )


def coverage_status(
    project_root: Path | str,
    *,
    controller_label: str | None,
) -> dict[str, object]:
    if not controller_label:
        return {
            "covered": False,
            "reason": "missing-label",
            "waiters": [],
            "monitor": {"required": False, "state": "not-applicable"},
        }
    waiters = live_waiters(project_root, controller_label=controller_label)
    return {
        "covered": bool(waiters),
        "reason": "held-flock" if waiters else "no-live-waiter-lock",
        "waiters": [
            {
                "kind": row.kind,
                "pid": row.pid,
                "instance_id": row.instance_id,
                "path": str(row.path),
            }
            for row in waiters
        ],
        "monitor": {"required": False, "state": "not-applicable"},
    }


def listener_start_command(
    project_root: Path | str,
    *,
    controller_label: str | None,
) -> str:
    messages_script = Path(__file__).resolve().with_name("goalflight_messages.py")
    argv = [
        "python3",
        str(messages_script),
        "listen-auto",
        "--project-root",
        str(Path(project_root).expanduser().resolve(strict=False)),
    ]
    if controller_label:
        argv.extend(["--controller-label", controller_label])
    return shlex.join(argv)


def entry_poll_window_s() -> float:
    if os.environ.get("GOALFLIGHT_TEST_MODE") == "1":
        default = 0.05
    else:
        default = ENTRY_POLL_WINDOW_S
    raw = os.environ.get("GOALFLIGHT_WAKE_ENTRY_POLL_S", "").strip()
    try:
        return min(2.0, max(0.0, float(raw))) if raw else default
    except ValueError:
        return default


def check_tool_entry(
    project_root: Path | str,
    *,
    controller_label: str | None,
    controller_claimed: bool,
    mail_bearing: bool,
    pending_probe: Callable[[], str | None] | None = None,
    stream=None,
) -> dict[str, object]:
    """Warn/poll only for a claimed controller call that can surface mail."""
    if not mail_bearing:
        return {
            "covered": False,
            "reason": "not-mail-bearing",
            "waiters": [],
            "monitor": {"required": False, "state": "not-applicable"},
        }
    if not controller_claimed:
        return {
            "covered": False,
            "reason": "no-ambient-claimed-controller",
            "waiters": [],
            "monitor": {"required": False, "state": "not-applicable"},
        }
    status = coverage_status(project_root, controller_label=controller_label)
    if status["covered"]:
        return status
    output = sys.stderr if stream is None else stream
    command = listener_start_command(project_root, controller_label=controller_label)
    print(f"listener offline; start: {command}", file=output)
    status["start_command"] = command
    if pending_probe is None:
        return status
    deadline = time.monotonic() + entry_poll_window_s()
    while True:
        pending = pending_probe()
        if pending:
            print(pending, file=output)
            status["pending_notice"] = pending
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return status
        time.sleep(min(ENTRY_POLL_INTERVAL_S, remaining))
