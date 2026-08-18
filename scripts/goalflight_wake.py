#!/usr/bin/env python3
"""Kernel-backed liveness ledger for controller wake waiters.

Lockfile names are addresses and identities only.  Their contents are never
read or written: a waiter is live iff its process still holds the file lock.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
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
_LISTENER_SLOT_FILE_VERSION = "listener-slot-v1"
_RING_STAMP_FILE_VERSION = "ring-stamp-v1"
_PENDING_REPORT_FILE_VERSION = "pending-report-v1"
_LEASE_EVENT_SCHEMA = "goalflight.lease-generation-event.v1"
# Depth is resilience, not efficiency: one event wakes exactly one slot, so
# the remaining slots are the margin for a controller that forgets to re-arm.
# 4 survives three consecutive missed re-arms. Override with
# GOALFLIGHT_LISTENER_SLOTS; MAX_LISTENER_SLOTS stays 32.
DEFAULT_LISTENER_SLOTS = 4
MAX_LISTENER_SLOTS = 32


class ListenerSlotsFull(BlockingIOError):
    """Every bounded one-shot listener slot is held by a live process."""

    def __init__(self, slots: int) -> None:
        self.slots = slots
        super().__init__(errno.EAGAIN, f"all {slots} listener slots are held")


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


def controller_session_digest(value: object) -> str | None:
    """Digest a controller capability with the established publication helper."""
    return _label_hash(value) if isinstance(value, str) and value else None


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


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


def _open_ledger_directory_path(directory: Path, *, create: bool) -> int:
    if create:
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)
    # If writers followed a ledger-leaf symlink that readers refuse, a valid
    # waiter could be written into a permanently UNKNOWN write/read wedge.
    return os.open(directory, _directory_open_flags())


def _unlink_at(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


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


def _listener_slot_lock_path(
    project_root: Path | str,
    *,
    label: str,
    generation_key: str,
    slot: int,
) -> Path:
    generation_hash = hashlib.sha256(generation_key.encode("utf-8")).hexdigest()[:16]
    return ledger_dir(project_root) / (
        f"{_LISTENER_SLOT_FILE_VERSION}.{_label_hash(label)}.{generation_hash}."
        f"listener-slot-{slot}.lock"
    )


def _ring_stamp_path(project_root: Path | str, *, controller_label: str) -> Path:
    return ledger_dir(project_root) / (
        f"{_RING_STAMP_FILE_VERSION}.{_label_hash(controller_label)}.cursor"
    )


def _pending_report_path(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> Path:
    return ledger_dir(project_root) / (
        f"{_PENDING_REPORT_FILE_VERSION}.{_label_hash(controller_label)}."
        f"{_label_hash(lease_nonce)}.claimed"
    )


def claim_pending_report(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> bool:
    """First --report-pending arm in this lease generation emits the backlog.

    Later arms keep waiting without reprinting. Two arms racing: exclusive
    create, exactly one wins. The loser still raises its local high-water
    from its own peek so the same backlog does not pop every slot. Peek
    skew (mail arriving between the two peeks) is handled by the earlier
    listener, whose high-water is older and will still ring.
    """
    label = str(controller_label or "").strip()
    nonce = str(lease_nonce or "").strip()
    if not label or not nonce:
        raise ValueError("controller label and lease nonce are required")
    path = _pending_report_path(
        project_root,
        controller_label=label,
        lease_nonce=nonce,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, _open_flags(create_exclusive=True), 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, b"claimed\n")
    finally:
        os.close(fd)
    return True


def _ring_stamp_lock_path(project_root: Path | str, *, controller_label: str) -> Path:
    return ledger_dir(project_root) / (
        f"{_RING_STAMP_FILE_VERSION}.{_label_hash(controller_label)}.lock"
    )


def _acquire_contended_lock(path: Path, *, directory_fd: int) -> int:
    """Acquire one well-known lock without publishing an unlocked inode."""
    assert fcntl is not None
    name = path.name
    while True:
        try:
            fd = os.open(name, _open_flags(), dir_fd=directory_fd)
        except FileNotFoundError:
            pending = f".{name}.{uuid.uuid4().hex}.pending"
            pending_fd = os.open(
                pending,
                _open_flags(create_exclusive=True),
                0o600,
                dir_fd=directory_fd,
            )
            try:
                _lock_nonblocking(pending_fd)
                try:
                    os.link(
                        pending,
                        name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                except FileExistsError:
                    fcntl.flock(pending_fd, fcntl.LOCK_UN)
                    os.close(pending_fd)
                    pending_fd = -1
                    _unlink_at(directory_fd, pending)
                    continue
                _unlink_at(directory_fd, pending)
                return pending_fd
            except BaseException:
                if pending_fd >= 0:
                    os.close(pending_fd)
                _unlink_at(directory_fd, pending)
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
        generation_slots: int | None = None,
    ) -> None:
        self._fd = -1
        self._directory_fd = -1
        self._generation_path = None
        self._generation_fd = -1
        self.slot_index: int | None = None
        if fcntl is None:
            raise RuntimeError("held-flock wake ledger is unavailable on this platform")
        normalized_label = str(label or "").strip()
        if not normalized_label:
            raise ValueError("waiter controller label is required")
        if kind not in LOCK_KINDS:
            raise ValueError(f"unknown held-lock kind: {kind}")
        directory = ledger_dir(project_root)
        pending_name: str | None = None
        record_name: str | None = None
        try:
            self._directory_fd = _open_ledger_directory_path(directory, create=True)
            identity = goalflight_compat.process_start_identity(os.getpid())
            if not isinstance(identity, dict) or not identity.get("start_token"):
                raise RuntimeError("held-lock owner process generation is unavailable")
            start_hash = _start_hash(identity["start_token"])
            instance_id = uuid.uuid4().hex
            record_name = (
                f"{_FILE_VERSION}.{kind}.{_label_hash(normalized_label)}."
                f"{os.getpid()}.{start_hash}.{instance_id}.lock"
            )
            self.record = WaiterRecord(
                kind=kind,
                label_hash=_label_hash(normalized_label),
                pid=os.getpid(),
                start_hash=start_hash,
                instance_id=instance_id,
                path=directory / record_name,
            )
            if generation_key is not None:
                normalized_generation = str(generation_key or "").strip()
                if not normalized_generation:
                    raise ValueError("waiter generation key is required")
                if generation_slots is None:
                    candidates = [
                        _generation_lock_path(
                            project_root,
                            kind=kind,
                            label=normalized_label,
                            generation_key=normalized_generation,
                        )
                    ]
                else:
                    if kind != "listener":
                        raise ValueError("generation slots are only valid for listeners")
                    if not 1 <= generation_slots <= MAX_LISTENER_SLOTS:
                        raise ValueError(
                            f"listener slots must be between 1 and {MAX_LISTENER_SLOTS}"
                        )
                    candidates = [
                        _listener_slot_lock_path(
                            project_root,
                            label=normalized_label,
                            generation_key=normalized_generation,
                            slot=slot,
                        )
                        for slot in range(generation_slots)
                    ]
                for slot, candidate in enumerate(candidates):
                    try:
                        generation_fd = _acquire_contended_lock(
                            candidate,
                            directory_fd=self._directory_fd,
                        )
                    except BlockingIOError:
                        continue
                    self._generation_path = candidate
                    self._generation_fd = generation_fd
                    self.slot_index = slot if generation_slots is not None else None
                    break
                if self._generation_fd < 0:
                    raise ListenerSlotsFull(len(candidates))
            pending_name = f".{record_name}.{uuid.uuid4().hex}.pending"
            self._fd = os.open(
                pending_name,
                _open_flags(create_exclusive=True),
                0o600,
                dir_fd=self._directory_fd,
            )
            _lock_nonblocking(self._fd)
            # Publish only after the kernel witness exists.  A concurrent probe
            # can therefore never acquire-and-prune a not-yet-locked address.
            os.replace(
                pending_name,
                record_name,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            pending_name = None
        except BaseException:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            if self._generation_fd >= 0:
                fcntl.flock(self._generation_fd, fcntl.LOCK_UN)
                os.close(self._generation_fd)
                self._generation_fd = -1
            if self._directory_fd >= 0:
                if pending_name is not None:
                    _unlink_at(self._directory_fd, pending_name)
                if record_name is not None:
                    _unlink_at(self._directory_fd, record_name)
                os.close(self._directory_fd)
                self._directory_fd = -1
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
            if self._directory_fd >= 0:
                _unlink_at(self._directory_fd, self.record.path.name)
        generation_fd = self._generation_fd
        self._generation_fd = -1
        if generation_fd >= 0:
            try:
                fcntl.flock(generation_fd, fcntl.LOCK_UN)
            finally:
                os.close(generation_fd)
        directory_fd = self._directory_fd
        self._directory_fd = -1
        if directory_fd >= 0:
            os.close(directory_fd)

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
    generation_slots: int | None = None,
) -> WaiterRegistration:
    if kind not in WAITER_KINDS:
        raise ValueError(f"unknown waiter kind: {kind}")
    return WaiterRegistration(
        project_root,
        controller_label,
        kind,
        generation_key=generation_key,
        generation_slots=generation_slots,
    )


def listener_reserve_hint(
    live_waiters: int,
    target_waiters: int,
    command: str,
) -> str:
    """Operator hint when the listener pool is short of its configured depth.

    n=0 keeps the loud offline wording. Otherwise print the exact arm
    command once per missing slot (n=1/4 -> three pasteable lines).
    """
    if live_waiters == 0:
        return f"listener pool n=0; start: {command}"
    if int(live_waiters) > listener_low_water(int(target_waiters)):
        # Healthy depth: a missing slot is not news. Silence here is the
        # difference between a signal and a nag.
        return ""
    missing = max(0, int(target_waiters) - int(live_waiters))
    header = (
        f"listener pool n={live_waiters}/{target_waiters} — reserve down; "
        f"re-arm: {command}"
    )
    if missing <= 1:
        return header
    extra = "\n".join(command for _ in range(missing - 1))
    return f"{header}\n{extra}"


# One shell `&` loop is one harness task. The N doorbells it forks are
# invisible, so their exits wake nobody. The post-exit / lease-claim
# hint must be impossible to satisfy that way.
SEPARATE_TRACKED_ARM_RULE = (
    "issue each as its own tracked background task; "
    "a shell `&` loop is one untracked call and those wakes reach nobody"
)


def listener_depth_plan(
    live_waiters: int | None,
    target_waiters: int,
    command: str,
    *,
    work_in_flight: bool,
) -> dict[str, object]:
    """Remaining-depth machine plan after a listen exit or a lease claim.

    Entry nagging stays in ``listener_reserve_hint`` (silent above
    low-water). The numbered human list lives in ``listener_floor_hint``
    and is rendered only on the listen-exit surface. This plan carries
    the single command template: repeating it ``missing`` times is
    something a count already says.
    """
    target = int(target_waiters)
    live = 0 if live_waiters is None else int(live_waiters)
    missing = max(0, target - live)
    return {
        "live": live,
        "target": target,
        "missing": missing,
        "work_in_flight": bool(work_in_flight),
        "command": command,
        "separate_tracked_tasks": True,
    }


def listener_floor_hint(
    live_waiters: int,
    target_waiters: int,
    command: str,
    *,
    work_in_flight: bool,
) -> str:
    """Exact remaining-depth commands after a listen exit or lease claim.

    Numbered lines so the correct response is N separate tracked calls,
    not one detached loop. Empty when there is no in-flight work or the
    pool is already at target.
    """
    if not work_in_flight:
        return ""
    live = int(live_waiters)
    target = int(target_waiters)
    missing = max(0, target - live)
    if missing == 0:
        return ""
    if live == 0:
        header = (
            f"listener floor: work in flight and live=0/{target} — "
            f"{missing} slots missing; {SEPARATE_TRACKED_ARM_RULE}:"
        )
    else:
        slot_word = "slot" if missing == 1 else "slots"
        header = (
            f"listener pool n={live}/{target} — {missing} {slot_word} missing; "
            f"{SEPARATE_TRACKED_ARM_RULE}:"
        )
    numbered = "\n".join(f"{index}. {command}" for index in range(1, missing + 1))
    return f"{header}\n{numbered}"


ACTIVITY_DEPTH_SCHEMA = "goalflight.listener-activity-signal.v1"
_ACTIVITY_DEPTH_FILE_VERSION = "activity-depth-v1"


def listener_activity_hint(
    live_waiters: int,
    target_waiters: int,
    command: str,
    *,
    work_in_flight: bool,
) -> str:
    """One-line remaining-depth cue for relay/status/next.

    The numbered list stays on listen-exit. Empty when there is no
    in-flight work or the pool is already at target.
    """
    if not work_in_flight:
        return ""
    live = int(live_waiters)
    target = int(target_waiters)
    missing = max(0, target - live)
    if missing == 0:
        return ""
    return f"listener depth {live}/{target} — {missing} missing; {command}"


def _activity_depth_state_path(
    project_root: Path | str,
    *,
    controller_label: str,
) -> Path:
    label = str(controller_label or "").strip()
    if not label:
        raise ValueError("controller label is required")
    return ledger_dir(project_root) / (
        f"{_ACTIVITY_DEPTH_FILE_VERSION}.{_label_hash(label)}.json"
    )


def _activity_depth_key(plan: dict[str, object]) -> dict[str, object]:
    return {
        "live": int(plan["live"]),
        "target": int(plan["target"]),
        "work_in_flight": bool(plan["work_in_flight"]),
    }


def _load_activity_depth_state(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != ACTIVITY_DEPTH_SCHEMA:
        return None
    try:
        return _activity_depth_key(data)
    except (KeyError, TypeError, ValueError):
        return None


def _save_activity_depth_state(path: Path, key: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": ACTIVITY_DEPTH_SCHEMA, **key}
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


def consume_listener_activity_signal(
    project_root: Path | str,
    controller_label: str,
    plan: dict[str, object],
) -> str:
    """Return the one-line cue once per depth transition; empty otherwise.

    Same band-suppression idea as the context meter: a live controller
    runs these surfaces constantly, so a repeat of the same (live,
    target, work_in_flight) tuple is not news.
    """
    hint = listener_activity_hint(
        int(plan["live"]),
        int(plan["target"]),
        str(plan["command"]),
        work_in_flight=bool(plan["work_in_flight"]),
    )
    current = _activity_depth_key(plan)
    path = _activity_depth_state_path(
        project_root, controller_label=controller_label
    )
    last = _load_activity_depth_state(path)
    _save_activity_depth_state(path, current)
    if not hint or last == current:
        return ""
    return hint


def listener_low_water(target: int | None = None) -> int:
    """Depth at or below which the pool is 'running low' and worth a hint.

    A pool exists so a missed re-arm is survivable; nagging at the first
    missing slot (3/4) is noise, so hints stay silent while depth is
    healthy and speak only when the margin is genuinely thin. Default is
    half the target (4 -> 2), floored at 1 so a single-slot pool still
    warns when it empties.
    """
    resolved = int(target) if target is not None else listener_slot_count()
    raw = os.environ.get("GOALFLIGHT_LISTENER_LOW_WATER")
    if raw is not None:
        try:
            value = int(str(raw))
        except (TypeError, ValueError) as exc:
            raise ValueError("listener low water must be an integer") from exc
        return max(0, min(value, resolved))
    return max(1, resolved // 2)


def listener_slot_count(value: object = None) -> int:
    """Resolve and validate the bounded listener-pool size."""
    raw = value
    if raw is None:
        raw = os.environ.get("GOALFLIGHT_LISTENER_SLOTS", str(DEFAULT_LISTENER_SLOTS))
    try:
        slots = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("listener slots must be an integer") from exc
    if not 1 <= slots <= MAX_LISTENER_SLOTS:
        raise ValueError(f"listener slots must be between 1 and {MAX_LISTENER_SLOTS}")
    return slots


def register_listener_waiter(
    project_root: Path | str,
    *,
    controller_label: str,
    generation_key: str,
    slots: int | None = None,
) -> WaiterRegistration:
    """Take the first free listener-slot-N lock for one lease generation."""
    resolved_slots = listener_slot_count(slots)
    return register_waiter(
        project_root,
        controller_label=controller_label,
        kind="listener",
        generation_key=generation_key,
        generation_slots=resolved_slots,
    )


def listener_slot_holder_pids(
    project_root: Path | str,
    *,
    controller_label: str,
) -> list[int]:
    waiters = live_waiters(
        project_root,
        controller_label=controller_label,
        kinds={"listener"},
    )
    return sorted({record.pid for record in (waiters or [])})


def _ring_stamp_needs_claim(observed: int | None, cursor_version: int) -> bool:
    """A cursor mutation stales the last ring even when no process owns a lock."""
    return observed != cursor_version


def claim_ring(
    project_root: Path | str,
    *,
    controller_label: str,
    cursor_version: int,
) -> bool:
    """Atomically claim the one ring allowed for a controller cursor version."""
    label = str(controller_label or "").strip()
    if not label:
        raise ValueError("controller label is required")
    if (
        not isinstance(cursor_version, int)
        or isinstance(cursor_version, bool)
        or cursor_version < 0
    ):
        raise ValueError("cursor version must be a non-negative integer")
    path = _ring_stamp_path(project_root, controller_label=label)
    lock_path = _ring_stamp_lock_path(project_root, controller_label=label)
    directory_fd = _open_ledger_directory_path(path.parent, create=True)
    lock_fd = -1
    try:
        try:
            lock_fd = _acquire_contended_lock(lock_path, directory_fd=directory_fd)
        except BlockingIOError:
            # Another listener is comparing/writing. It will publish the version
            # before releasing; this listener can observe the result next poll.
            return False
        stamp_fd = -1
        try:
            stamp_fd = os.open(path.name, _open_flags(), dir_fd=directory_fd)
            raw = os.read(stamp_fd, 65)
        except FileNotFoundError:
            raw = b""
        finally:
            if stamp_fd >= 0:
                os.close(stamp_fd)
        malformed = len(raw) > 64
        if raw:
            try:
                observed = int(raw.decode("ascii").strip())
            except (UnicodeDecodeError, ValueError):
                malformed = True
                observed = None
            if observed is not None and observed < 0:
                malformed = True
        else:
            observed = None
        if not malformed and not _ring_stamp_needs_claim(observed, cursor_version):
            return False

        corrupt_name: str | None = None
        if malformed:
            corrupt_epoch = time.time_ns()
            while True:
                corrupt_name = f"{path.name}.corrupt-{corrupt_epoch}"
                try:
                    os.stat(
                        corrupt_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    break
                corrupt_epoch += 1
            os.rename(
                path.name,
                corrupt_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        encoded = f"{cursor_version}\n".encode("ascii")
        pending = f".{path.name}.{uuid.uuid4().hex}.pending"
        pending_fd = -1
        try:
            pending_fd = os.open(
                pending,
                _open_flags(create_exclusive=True),
                0o600,
                dir_fd=directory_fd,
            )
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(pending_fd, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, "short listener ring stamp write")
                remaining = remaining[written:]
            os.fsync(pending_fd)
            os.replace(
                pending,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            pending = ""
            os.fsync(directory_fd)
        finally:
            if pending_fd >= 0:
                os.close(pending_fd)
            if pending:
                _unlink_at(directory_fd, pending)
        if corrupt_name is not None:
            print(
                f"listener ring stamp quarantined as {corrupt_name}; "
                f"rewrote cursor version {cursor_version}",
                file=sys.stderr,
            )
        return True
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(directory_fd)


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
        self._fd = -1
        self._directory_fd = -1
        if fcntl is None:
            raise RuntimeError("held-flock wake ledger is unavailable on this platform")
        try:
            self._directory_fd = _open_ledger_directory_path(path.parent, create=True)
            self._fd = _acquire_contended_lock(
                path,
                directory_fd=self._directory_fd,
            )
        except BaseException:
            if self._directory_fd >= 0:
                os.close(self._directory_fd)
                self._directory_fd = -1
            raise
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
            directory_fd = self._directory_fd
            self._directory_fd = -1
            if directory_fd >= 0:
                os.close(directory_fd)

    def __enter__(self) -> "LeaseHolderRegistration":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def _probe_locked_once(path: Path) -> bool:
    """Take one kernel-lock sample; file existence/content proves nothing."""
    return _probe_locked_state(path) is True


def _probe_locked_state_at(directory_fd: int, name: str) -> bool | None:
    """Return held/unheld, or UNKNOWN when the address cannot be opened."""
    if fcntl is None:
        return None
    try:
        fd = os.open(name, _open_flags(), dir_fd=directory_fd)
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


def _probe_locked_state(path: Path) -> bool | None:
    if fcntl is None:
        return None
    try:
        directory_fd = _open_ledger_directory_path(path.parent, create=False)
    except OSError:
        return None
    try:
        return _probe_locked_state_at(directory_fd, path.name)
    finally:
        os.close(directory_fd)


def live_waiters(
    project_root: Path | str,
    *,
    controller_label: str | None = None,
    kinds: Iterable[str] = WAITER_KINDS,
    prune_dead: bool = True,
) -> list[WaiterRecord] | None:
    directory = ledger_dir(project_root)
    try:
        directory_fd = _open_ledger_directory_path(directory, create=False)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            return None
        # Missing/unreadable storage is also probe-unavailable, never known-zero.
        return None
    try:
        with os.scandir(directory_fd) as entries:
            names = [
                entry.name
                for entry in entries
                if entry.name.startswith(f"{_FILE_VERSION}.")
                and entry.name.endswith(".lock")
            ]
        accepted_kinds = set(kinds)
        wanted_label = _label_hash(controller_label) if controller_label else None
        live: list[WaiterRecord] = []
        for name in names:
            path = directory / name
            record = _parse_waiter_path(path)
            if record is None or record.kind not in accepted_kinds:
                continue
            if wanted_label is not None and record.label_hash != wanted_label:
                continue
            lock_state = _probe_locked_state_at(directory_fd, name)
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
                    _unlink_at(directory_fd, name)
                except OSError:
                    pass
        return sorted(live, key=lambda row: (row.kind, row.pid, row.instance_id))
    except OSError:
        return None
    finally:
        os.close(directory_fd)


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
    target_waiters = listener_slot_count()
    if not controller_label:
        return {
            "covered": False,
            "reason": "missing-label",
            "live_waiters": 0,
            "target_waiters": target_waiters,
            "waiters": [],
            "monitor": {"required": False, "state": "not-applicable"},
        }
    waiters = live_waiters(
        project_root,
        controller_label=controller_label,
        kinds={"listener"},
    )
    if waiters is None:
        return {
            "covered": False,
            "reason": "waiter-probe-unavailable",
            "live_waiters": None,
            "target_waiters": target_waiters,
            "waiters": [],
            "monitor": {"required": False, "state": "not-applicable"},
        }
    return {
        "covered": bool(waiters),
        "reason": "held-flock" if waiters else "no-live-waiter-lock",
        "live_waiters": len(waiters),
        "target_waiters": target_waiters,
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
        "listen",
        "--project-root",
        str(Path(project_root).expanduser().resolve(strict=False)),
    ]
    if controller_label:
        argv.extend(["--controller-label", controller_label])
    argv.append("--report-pending")
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
    live_waiters = status.get("live_waiters")
    target_waiters = int(status.get("target_waiters") or listener_slot_count())
    if isinstance(live_waiters, int) and live_waiters >= target_waiters:
        return status
    output = sys.stderr if stream is None else stream
    command = listener_start_command(project_root, controller_label=controller_label)
    if status.get("reason") == "waiter-probe-unavailable":
        print(
            "listener coverage UNKNOWN (probe unavailable); "
            f"if you have no listener, start: {command}",
            file=output,
        )
    else:
        print(
            listener_reserve_hint(int(live_waiters or 0), target_waiters, command),
            file=output,
        )
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
