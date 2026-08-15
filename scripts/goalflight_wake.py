#!/usr/bin/env python3
"""Kernel-backed liveness ledger for controller wake waiters.

Lockfile names are addresses and identities only.  Their contents are never
read or written: a waiter is live iff its process still holds the file lock.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time
import tempfile
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
_FILE_VERSION = "v2"
_GENERATION_FILE_VERSION = "generation-v1"
_LEASE_EVENT_SCHEMA = "goalflight.lease-generation-event.v1"


@dataclass(frozen=True)
class WaiterRecord:
    kind: str
    label_hash: str
    pid: int
    start_hash: str
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
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    state_home = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return state_home / "goal-flight" / "wake-ledger"


def ledger_dir(project_root: Path | str) -> Path:
    return ledger_base_dir() / _project_key(project_root)


def lease_generation_event_path(project_root: Path | str, *, controller_label: str) -> Path:
    label = str(controller_label or "").strip()
    if not label:
        raise ValueError("controller label is required")
    return ledger_dir(project_root) / f"lease-event-v1.{_label_hash(label)}.json"


def publish_lease_generation_event(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    generation: int,
    state: str,
) -> Path:
    """Publish the small nonce/generation token watched by the cheap beacon tick."""
    nonce = str(lease_nonce or "").strip()
    if not nonce or generation < 1:
        raise ValueError("lease nonce and positive generation are required")
    path = lease_generation_event_path(
        project_root,
        controller_label=controller_label,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": _LEASE_EVENT_SCHEMA,
        "label": str(controller_label).strip(),
        "nonce": nonce,
        "generation": generation,
        "state": str(state or "").strip(),
    }
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except OSError:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return path


def lease_generation_event_stamp(
    project_root: Path | str,
    *,
    controller_label: str,
) -> tuple[int, int, int] | None:
    path = lease_generation_event_path(
        project_root,
        controller_label=controller_label,
    )
    try:
        observed = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return observed.st_ino, observed.st_mtime_ns, observed.st_size


def read_lease_generation_event(
    project_root: Path | str,
    *,
    controller_label: str,
) -> dict[str, object] | None:
    path = lease_generation_event_path(
        project_root,
        controller_label=controller_label,
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"lease generation event is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _LEASE_EVENT_SCHEMA:
        raise RuntimeError(f"lease generation event is malformed: {path}")
    return payload


def _parse_waiter_path(path: Path) -> WaiterRecord | None:
    parts = path.name.split(".")
    if len(parts) != 7 or parts[0] != _FILE_VERSION or parts[-1] != "lock":
        return None
    _version, kind, label_hash, raw_pid, start_hash, instance_id, _suffix = parts
    if kind not in WAITER_KINDS:
        return None
    if len(label_hash) != 16 or len(start_hash) != 16 or len(instance_id) != 32:
        return None
    try:
        pid = int(raw_pid)
    except ValueError:
        return None
    if pid <= 0:
        return None
    return WaiterRecord(kind, label_hash, pid, start_hash, instance_id, path)


def _start_hash(start_token: object) -> str:
    value = str(start_token or "")
    if not value:
        raise ValueError("held-lock owner start token is required")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _set_cloexec(fd: int) -> None:
    """Make exec drop the witness even on runtimes without O_CLOEXEC."""
    os.set_inheritable(fd, False)
    if fcntl is not None:
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)


def _open_flags(*, create_exclusive: bool = False) -> int:
    flags = os.O_RDWR
    if create_exclusive:
        flags |= os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _lock_nonblocking(fd: int) -> None:
    assert fcntl is not None
    _set_cloexec(fd)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _generation_lock_path(
    project_root: Path | str,
    *,
    kind: str,
    label: str,
    generation_key: str,
) -> Path:
    identity_hash = hashlib.sha256(
        f"{label}\0{generation_key}".encode("utf-8")
    ).hexdigest()[:32]
    return ledger_dir(project_root) / (
        f"{_GENERATION_FILE_VERSION}.{kind}.{_label_hash(label)}.{identity_hash}.lock"
    )


def _acquire_contended_lock(path: Path) -> int:
    """Acquire one well-known lock without publishing an unlocked inode."""
    assert fcntl is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(path, _open_flags())
        except FileNotFoundError:
            pending = path.parent / f".{path.name}.{uuid.uuid4().hex}.pending"
            pending_fd = os.open(pending, _open_flags(create_exclusive=True), 0o600)
            try:
                _lock_nonblocking(pending_fd)
                try:
                    os.link(pending, path)
                except FileExistsError:
                    fcntl.flock(pending_fd, fcntl.LOCK_UN)
                    os.close(pending_fd)
                    pending_fd = -1
                    pending.unlink(missing_ok=True)
                    continue
                pending.unlink(missing_ok=True)
                return pending_fd
            except BaseException:
                if pending_fd >= 0:
                    os.close(pending_fd)
                pending.unlink(missing_ok=True)
                raise
        try:
            _lock_nonblocking(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd


class WaiterRegistration:
    """One held-flock address.  The kernel owns the liveness fact."""

    def __init__(
        self,
        project_root: Path | str,
        label: str,
        kind: str,
        *,
        generation_key: str | None = None,
    ) -> None:
        self._fd = -1
        self._generation_path = None
        self._generation_fd = -1
        if fcntl is None:
            raise RuntimeError("held-flock wake ledger is unavailable on this platform")
        normalized_label = str(label or "").strip()
        if not normalized_label:
            raise ValueError("waiter controller label is required")
        if kind not in LOCK_KINDS:
            raise ValueError(f"unknown held-lock kind: {kind}")
        directory = ledger_dir(project_root)
        directory.mkdir(parents=True, exist_ok=True)
        identity = goalflight_compat.process_start_identity(os.getpid())
        if not isinstance(identity, dict) or not identity.get("start_token"):
            raise RuntimeError("held-lock owner process generation is unavailable")
        start_hash = _start_hash(identity["start_token"])
        instance_id = uuid.uuid4().hex
        self.record = WaiterRecord(
            kind=kind,
            label_hash=_label_hash(normalized_label),
            pid=os.getpid(),
            start_hash=start_hash,
            instance_id=instance_id,
            path=directory
            / f"{_FILE_VERSION}.{kind}.{_label_hash(normalized_label)}."
            f"{os.getpid()}.{start_hash}.{instance_id}.lock",
        )
        if generation_key is not None:
            normalized_generation = str(generation_key or "").strip()
            if not normalized_generation:
                raise ValueError("waiter generation key is required")
            self._generation_path = _generation_lock_path(
                project_root,
                kind=kind,
                label=normalized_label,
                generation_key=normalized_generation,
            )
            self._generation_fd = _acquire_contended_lock(self._generation_path)
        pending_path = directory / f".{self.record.path.name}.{uuid.uuid4().hex}.pending"
        self._fd = os.open(pending_path, _open_flags(create_exclusive=True), 0o600)
        try:
            _lock_nonblocking(self._fd)
            # Publish only after the kernel witness exists.  A concurrent probe
            # can therefore never acquire-and-prune a not-yet-locked address.
            os.replace(pending_path, self.record.path)
        except BaseException:
            os.close(self._fd)
            self._fd = -1
            if self._generation_fd >= 0:
                fcntl.flock(self._generation_fd, fcntl.LOCK_UN)
                os.close(self._generation_fd)
                self._generation_fd = -1
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
        generation_fd = self._generation_fd
        self._generation_fd = -1
        if generation_fd >= 0:
            try:
                fcntl.flock(generation_fd, fcntl.LOCK_UN)
            finally:
                os.close(generation_fd)

    def __enter__(self) -> "WaiterRegistration":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - process death is authority.
        try:
            self.close()
        except (OSError, RuntimeError):
            pass


def register_waiter(
    project_root: Path | str,
    *,
    controller_label: str,
    kind: str,
    generation_key: str | None = None,
) -> WaiterRegistration:
    if kind not in WAITER_KINDS:
        raise ValueError(f"unknown waiter kind: {kind}")
    return WaiterRegistration(
        project_root,
        controller_label,
        kind,
        generation_key=generation_key,
    )


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
) -> "LeaseHolderRegistration":
    """Hold the kernel liveness witness for one controller lease generation."""
    identity = _lease_lock_identity(controller_label, lease_nonce)
    return LeaseHolderRegistration(
        _generation_lock_path(
            project_root,
            kind=LEASE_KIND,
            label=identity,
            generation_key=lease_nonce,
        ),
        label_hash=_label_hash(identity),
    )


class LeaseHolderRegistration:
    """One contended generation witness whose unlocked path is retained."""

    def __init__(self, path: Path, *, label_hash: str) -> None:
        if fcntl is None:
            raise RuntimeError("held-flock wake ledger is unavailable on this platform")
        self._fd = _acquire_contended_lock(path)
        self.record = WaiterRecord(
            kind=LEASE_KIND,
            label_hash=label_hash,
            pid=os.getpid(),
            start_hash="0" * 16,
            instance_id=path.stem.rsplit(".", 1)[-1],
            path=path,
        )

    def close(self) -> None:
        fd = self._fd
        if fd < 0:
            return
        self._fd = -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "LeaseHolderRegistration":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def _probe_locked_once(path: Path) -> bool:
    """Take one kernel-lock sample; file existence/content proves nothing."""
    return _probe_locked_state(path) is True


def _probe_locked_state(path: Path) -> bool | None:
    """Return held/unheld, or UNKNOWN when the address cannot be opened."""
    if fcntl is None:
        return None
    try:
        fd = os.open(path, _open_flags())
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {11, 35}:
                return True
            return None
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


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
        lock_state = _probe_locked_state(path)
        identity = goalflight_compat.process_start_identity(record.pid)
        owner_matches = bool(
            isinstance(identity, dict)
            and identity.get("start_token")
            and _start_hash(identity["start_token"]) == record.start_hash
            and goalflight_compat.pid_is_zombie(record.pid) is False
        )
        if owner_matches and lock_state is True:
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
) -> bool | None:
    """Return held/unheld, or UNKNOWN when the active witness path vanished."""
    del prune_dead
    identity = _lease_lock_identity(controller_label, lease_nonce)
    path = _generation_lock_path(
        project_root,
        kind=LEASE_KIND,
        label=identity,
        generation_key=lease_nonce,
    )
    return _probe_locked_state(path)


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
    waiters = live_waiters(
        project_root,
        controller_label=controller_label,
        kinds={"listener"},
    )
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
