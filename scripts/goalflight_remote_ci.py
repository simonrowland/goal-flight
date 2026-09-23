#!/usr/bin/env python3
"""Project-neutral remote CI runner and gate daemon.

The transport, test command, hosts, paths, and selection policy are supplied by
one project configuration.  This module owns the cross-project contracts:
shared flock-token admission, one FIFO queue runner, matched BASE/CAND verdicts,
receipt validation, timeout cancellation, orphan cleanup, and health census.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _datetime
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import socket
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence


CONFIG_SCHEMA = "goalflight.remote-ci.config.v1"
REQUEST_SCHEMA = "goalflight.remote-ci.request.v1"
RECEIPT_SCHEMA = "goalflight.remote-ci.receipt.v1"
RESULT_SCHEMA = "goalflight.remote-ci.result.v1"
LEASE_SCHEMA = "goalflight.remote-ci.lease.v1"

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LAUNCH_RE = re.compile(r"(?:^|\n)REMOTE_RUN_LAUNCHED (?P<fields>[^\n]+)")
_JSON_LINE_RE = re.compile(r"^\s*(\{.*\})\s*$")


class RemoteCIError(RuntimeError):
    """A configuration, admission, transport, or receipt contract failure."""


class ConfigError(RemoteCIError):
    """The project configuration is invalid."""


class ReceiptError(RemoteCIError):
    """A remote result does not contain a trustworthy receipt."""


class AdmissionError(RemoteCIError):
    """A box cannot be admitted safely."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ConfigError(f"{label} must be a single-line string")
    return value


def _strings(value: Any, label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{label} must be a list of strings")
    result = tuple(value)
    if nonempty and not result:
        raise ConfigError(f"{label} must not be empty")
    for index, item in enumerate(result):
        if not item or "\x00" in item or "\n" in item or "\r" in item:
            raise ConfigError(f"{label}[{index}] must be a non-empty single-line string")
    return result


def _string_map(value: Any, label: str) -> dict[str, str]:
    raw = _mapping(value, label)
    result: dict[str, str] = {}
    for key, child in raw.items():
        if not isinstance(key, str) or not isinstance(child, str):
            raise ConfigError(f"{label} must contain only string keys and values")
        if "\x00" in child or "\n" in child or "\r" in child:
            raise ConfigError(f"{label}.{key} must be a single-line string")
        result[key] = child
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{label} must be a non-negative integer")
    return value


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{label} must be positive")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{label} must be finite")
    return result


def _absolute_path(value: Any, label: str) -> Path:
    raw = _string(value, label)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{label} must be absolute")
    return path.resolve()


def _managed_path(value: Any, label: str) -> Path:
    path = _absolute_path(value, label)
    # A managed root is durable node state, not a per-invocation scratch path.
    # Reject both spellings used by macOS so a project cannot silently bypass
    # the node's lease namespace with an ad-hoc temporary directory.
    temporary_roots = (Path("/tmp"), Path("/private/tmp"))
    if any(path == root or root in path.parents for root in temporary_roots):
        raise ConfigError(f"{label} must not be under /tmp")
    return path


def _safe_id(value: Any, label: str) -> str:
    result = _string(value, label)
    if not _ID_RE.fullmatch(result) or ".." in result:
        raise ConfigError(f"{label} must be a safe identifier")
    return result


def _command(value: Any, label: str) -> tuple[str, ...]:
    return _strings(value, label, nonempty=True)


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds")


def _owner_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class BoxConfig:
    name: str
    host: str
    token_key: str
    p_cores: int
    token_pool_size: int
    load_command: tuple[str, ...]
    env: dict[str, str]
    # Every run/export path is minted below this node-local canonical root.
    managed_run_directory: Path = Path("/var/lib/goalflight/remote-ci/runs")


@dataclass(frozen=True)
class AdmissionConfig:
    token_directory: Path
    queue_wait_seconds: float
    live_cap_file: Path | None


@dataclass(frozen=True)
class RunnerConfig:
    command: tuple[str, ...]
    watch_command: tuple[str, ...]
    collect_command: tuple[str, ...]
    cancel_command: tuple[str, ...]
    test_command: tuple[str, ...]
    env: dict[str, str]
    timeout_seconds: float
    self_cap: int
    self_cap_env: str
    chunk_size: int
    verbose_option: str
    base_collection_option: str


@dataclass(frozen=True)
class SelectionRules:
    path_prefixes: tuple[str, ...]
    allowed_options: frozenset[str]
    value_options: frozenset[str]
    allowed_option_prefixes: tuple[str, ...]
    require_selector: bool
    max_targeted_files: int


@dataclass(frozen=True)
class DaemonConfig:
    path: Path
    queue_dir: Path
    state_dir: Path
    result_dir: Path
    lock_file: Path
    pid_file: Path | None
    poll_seconds: float
    boxes: dict[str, BoxConfig]
    admission: AdmissionConfig
    runner: RunnerConfig
    selection: SelectionRules

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, path: Path) -> "DaemonConfig":
        if raw.get("schema") != CONFIG_SCHEMA:
            raise ConfigError(f"config schema must be {CONFIG_SCHEMA}")

        def required(name: str) -> Any:
            if name not in raw:
                raise ConfigError(f"config missing {name}")
            return raw[name]

        paths = _mapping(required("paths"), "paths")
        queue_dir = _absolute_path(paths.get("queue_dir"), "paths.queue_dir")
        state_dir = _absolute_path(paths.get("state_dir"), "paths.state_dir")
        result_dir = _absolute_path(paths.get("result_dir"), "paths.result_dir")

        daemon = _mapping(required("daemon"), "daemon")
        lock_file = _absolute_path(daemon.get("lock_file"), "daemon.lock_file")
        raw_pid = daemon.get("pid_file")
        pid_file = None if raw_pid is None else _absolute_path(raw_pid, "daemon.pid_file")
        poll_seconds = _positive_number(daemon.get("poll_seconds", 5), "daemon.poll_seconds")
        if poll_seconds > 60:
            raise ConfigError("daemon.poll_seconds must be at most 60 seconds")

        raw_boxes = _mapping(required("boxes"), "boxes")
        if not raw_boxes:
            raise ConfigError("boxes must not be empty")
        boxes: dict[str, BoxConfig] = {}
        for raw_name, raw_box in raw_boxes.items():
            name = _safe_id(raw_name, "box name")
            box = _mapping(raw_box, f"boxes.{name}")
            host = _string(box.get("host"), f"boxes.{name}.host")
            token_key = _safe_id(box.get("token_key", host), f"boxes.{name}.token_key")
            boxes[name] = BoxConfig(
                name=name,
                host=host,
                token_key=token_key,
                p_cores=_positive_int(box.get("p_cores"), f"boxes.{name}.p_cores"),
                token_pool_size=_positive_int(
                    box.get("token_pool_size"), f"boxes.{name}.token_pool_size"
                ),
                load_command=_command(box.get("load_command"), f"boxes.{name}.load_command"),
                env=_string_map(box.get("env", {}), f"boxes.{name}.env"),
                managed_run_directory=_managed_path(
                    box.get("managed_run_directory"),
                    f"boxes.{name}.managed_run_directory",
                ),
            )

        admission = _mapping(required("admission"), "admission")
        raw_caps = admission.get("live_cap_file")
        live_cap_file = None if raw_caps is None else _absolute_path(raw_caps, "admission.live_cap_file")
        admission_config = AdmissionConfig(
            token_directory=_absolute_path(
                admission.get("token_directory"), "admission.token_directory"
            ),
            queue_wait_seconds=_positive_number(
                admission.get("queue_wait_seconds", 300),
                "admission.queue_wait_seconds",
            ),
            live_cap_file=live_cap_file,
        )

        runner = _mapping(required("runner"), "runner")
        self_cap_env = _string(runner.get("self_cap_env", "GOALFLIGHT_REMOTE_CI_SELF_CAP"), "runner.self_cap_env")
        if not _ENV_RE.fullmatch(self_cap_env):
            raise ConfigError("runner.self_cap_env must be an environment variable name")
        verbose_option = _string(runner.get("verbose_option", "-v"), "runner.verbose_option")
        base_collection_option = _string(
            runner.get("base_collection_option", "--continue-on-collection-errors"),
            "runner.base_collection_option",
        )
        runner_config = RunnerConfig(
            command=_command(runner.get("command"), "runner.command"),
            watch_command=_command(runner.get("watch_command"), "runner.watch_command"),
            collect_command=_command(runner.get("collect_command"), "runner.collect_command"),
            cancel_command=_command(runner.get("cancel_command"), "runner.cancel_command"),
            test_command=_command(runner.get("test_command"), "runner.test_command"),
            env=_string_map(runner.get("env", {}), "runner.env"),
            timeout_seconds=_positive_number(
                runner.get("timeout_seconds", 7200), "runner.timeout_seconds"
            ),
            self_cap=_positive_int(runner.get("self_cap"), "runner.self_cap"),
            self_cap_env=self_cap_env,
            chunk_size=_positive_int(runner.get("chunk_size", 20), "runner.chunk_size"),
            verbose_option=verbose_option,
            base_collection_option=base_collection_option,
        )

        selection = _mapping(required("selection"), "selection")
        path_prefixes = _strings(
            selection.get("path_prefixes"), "selection.path_prefixes", nonempty=True
        )
        if any(not prefix or prefix.startswith("/") for prefix in path_prefixes):
            raise ConfigError("selection.path_prefixes must be relative non-empty prefixes")
        raw_require_selector = selection.get("require_selector", True)
        if not isinstance(raw_require_selector, bool):
            raise ConfigError("selection.require_selector must be a boolean")
        selection_rules = SelectionRules(
            path_prefixes=path_prefixes,
            allowed_options=frozenset(_strings(selection.get("allowed_options", []), "selection.allowed_options")),
            value_options=frozenset(_strings(selection.get("value_options", []), "selection.value_options")),
            allowed_option_prefixes=_strings(
                selection.get("allowed_option_prefixes", []),
                "selection.allowed_option_prefixes",
            ),
            require_selector=raw_require_selector,
            max_targeted_files=_positive_int(
                selection.get("max_targeted_files", 20),
                "selection.max_targeted_files",
            ),
        )
        return cls(
            path=path,
            queue_dir=queue_dir,
            state_dir=state_dir,
            result_dir=result_dir,
            lock_file=lock_file,
            pid_file=pid_file,
            poll_seconds=poll_seconds,
            boxes=boxes,
            admission=admission_config,
            runner=runner_config,
            selection=selection_rules,
        )


def load_config(path: str | os.PathLike[str]) -> DaemonConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError("config root must be an object")
    return DaemonConfig.from_mapping(raw, path=config_path)


@dataclass
class RemoteLeaseRecord:
    """Durable identity for one managed remote run/export directory."""

    lease_id: str
    box: str
    owner_identity: str
    owner_pid: int
    lease_token: str
    managed_run_directory: str
    run_directory: str
    state: str
    acquired_at: str
    released_at: str | None = None
    release_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": LEASE_SCHEMA,
            "lease_id": self.lease_id,
            "box": self.box,
            "owner_identity": self.owner_identity,
            "owner_pid": self.owner_pid,
            "lease_token": self.lease_token,
            "managed_run_directory": self.managed_run_directory,
            "run_directory": self.run_directory,
            "state": self.state,
            "acquired_at": self.acquired_at,
        }
        if self.released_at is not None:
            value["released_at"] = self.released_at
        if self.release_reason is not None:
            value["release_reason"] = self.release_reason
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RemoteLeaseRecord":
        if value.get("schema") != LEASE_SCHEMA:
            raise RemoteCIError(f"lease schema must be {LEASE_SCHEMA}")
        required = (
            "lease_id",
            "box",
            "owner_identity",
            "owner_pid",
            "lease_token",
            "managed_run_directory",
            "run_directory",
            "state",
            "acquired_at",
        )
        for name in required:
            if name not in value:
                raise RemoteCIError(f"lease missing {name}")
        try:
            owner_pid = int(value["owner_pid"])
        except (TypeError, ValueError) as exc:
            raise RemoteCIError("lease owner_pid must be an integer") from exc
        state = str(value["state"])
        if state not in {"active", "released"}:
            raise RemoteCIError(f"unknown lease state {state!r}")
        lease_id = _safe_id(value["lease_id"], "lease_id")
        managed_root = _managed_path(
            value["managed_run_directory"], "lease.managed_run_directory"
        )
        run_directory = Path(_string(value["run_directory"], "lease.run_directory")).resolve(
            strict=False
        )
        if run_directory != managed_root / lease_id:
            raise RemoteCIError("lease run_directory is not its managed lease child")
        return cls(
            lease_id=lease_id,
            box=_safe_id(value["box"], "lease.box"),
            owner_identity=_string(value["owner_identity"], "lease.owner_identity"),
            owner_pid=owner_pid,
            lease_token=_string(value["lease_token"], "lease.lease_token"),
            managed_run_directory=str(managed_root),
            run_directory=str(run_directory),
            state=state,
            acquired_at=_string(value["acquired_at"], "lease.acquired_at"),
            released_at=(None if value.get("released_at") is None else _string(value["released_at"], "lease.released_at")),
            release_reason=(None if value.get("release_reason") is None else _string(value["release_reason"], "lease.release_reason")),
        )


class RemoteLease:
    """A remote lease held by a flock until normal release or process death."""

    def __init__(self, registry: "RemoteLeaseRegistry", record: RemoteLeaseRecord, handle: Any) -> None:
        self.registry = registry
        self.record = record
        self.handle = handle
        self.released = False

    def release(self, reason: str = "completed") -> None:
        if self.released:
            return
        self.released = True
        self.record.state = "released"
        self.record.released_at = _utc_now()
        self.record.release_reason = reason
        try:
            _atomic_write_json(self.registry.record_path(self.record), self.record.to_dict())
        finally:
            # Kernel ownership, not a cleanup file, protects the lease.
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()

    def __enter__(self) -> "RemoteLease":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


class RemoteLeaseRegistry:
    """Lease records and locks for paths below each box's managed root."""

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config

    def record_path(self, record: RemoteLeaseRecord) -> Path:
        return Path(record.run_directory) / "lease.json"

    def lock_path(self, record: RemoteLeaseRecord) -> Path:
        return Path(record.run_directory) / ".goalflight-lease.lock"

    def acquire(self, box: BoxConfig, *, request_id: str, arm: str) -> RemoteLease:
        request_part = _safe_id(request_id, "request_id")
        arm_part = _safe_id(arm, "arm")
        managed_root = _managed_path(
            str(box.managed_run_directory),
            f"boxes.{box.name}.managed_run_directory",
        )
        try:
            managed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise RemoteCIError(f"cannot create managed run directory {managed_root}: {exc}") from exc
        for _attempt in range(10):
            lease_id = f"{request_part}-{arm_part}-{uuid.uuid4().hex[:12]}"
            run_directory = managed_root / lease_id
            try:
                run_directory.mkdir(mode=0o700)
            except FileExistsError:
                continue
            lock_path = run_directory / ".goalflight-lease.lock"
            handle = lock_path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    continue
                raise RemoteCIError(f"cannot lock remote lease {lock_path}: {exc}") from exc
            record = RemoteLeaseRecord(
                lease_id=lease_id,
                box=box.name,
                owner_identity=_owner_identity(),
                owner_pid=os.getpid(),
                lease_token=uuid.uuid4().hex,
                managed_run_directory=str(managed_root),
                run_directory=str(run_directory),
                state="active",
                acquired_at=_utc_now(),
            )
            try:
                _atomic_write_json(self.record_path(record), record.to_dict())
            except BaseException:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                raise
            return RemoteLease(self, record, handle)
        raise RemoteCIError(f"could not allocate a unique remote lease for {box.name}")

    def records(self, *, active_only: bool = False) -> list[RemoteLeaseRecord]:
        records: list[RemoteLeaseRecord] = []
        roots: dict[str, Path] = {}
        for box in self.config.boxes.values():
            try:
                root = _managed_path(
                    str(box.managed_run_directory),
                    f"boxes.{box.name}.managed_run_directory",
                )
            except ConfigError:
                continue
            roots[str(root)] = root
        seen: set[tuple[str, str]] = set()
        for root in roots.values():
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*/lease.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    record = RemoteLeaseRecord.from_mapping(value)
                except (OSError, json.JSONDecodeError, RemoteCIError):
                    continue
                key = (record.lease_id, record.run_directory)
                if key in seen:
                    continue
                seen.add(key)
                if active_only and record.state != "active":
                    continue
                records.append(record)
        return records

    def release_record(self, record: RemoteLeaseRecord, *, reason: str) -> bool:
        """Release a record only after taking its lock without blocking."""
        if record.state != "active":
            return False
        lock_path = self.lock_path(record)
        try:
            handle = lock_path.open("a+")
        except OSError:
            return False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                raise
            path = self.record_path(record)
            try:
                current = RemoteLeaseRecord.from_mapping(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, RemoteCIError):
                return False
            if current.state != "active" or current.lease_token != record.lease_token:
                return False
            current.state = "released"
            current.released_at = _utc_now()
            current.release_reason = reason
            _atomic_write_json(path, current.to_dict())
            record.state = current.state
            record.released_at = current.released_at
            record.release_reason = current.release_reason
            return True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def release_matching(self, *, run_directory: str, lease_token: str, reason: str) -> bool:
        for record in self.records(active_only=True):
            if record.run_directory == run_directory and record.lease_token == lease_token:
                return self.release_record(record, reason=reason)
        return False


def list_remote_leases(config: DaemonConfig) -> list[dict[str, Any]]:
    """List active and released remote leases from the durable registry."""
    return [record.to_dict() for record in RemoteLeaseRegistry(config).records()]


def cleanup_dead_leases(
    config: DaemonConfig,
    *,
    owner_alive: Callable[[Mapping[str, Any]], bool | None] | None = None,
) -> list[dict[str, Any]]:
    """Release only leases whose recorded owner is proven dead."""
    probe = owner_alive or (lambda record: _pid_alive(record.get("owner_pid")))
    registry = RemoteLeaseRegistry(config)
    results: list[dict[str, Any]] = []
    for record in registry.records(active_only=True):
        alive = probe(record.to_dict())
        if alive is True:
            results.append({"lease_id": record.lease_id, "status": "owned"})
            continue
        if alive is not False:
            # Unknown is not proof of death; keep both the record and capacity.
            results.append({"lease_id": record.lease_id, "status": "unknown"})
            continue
        released = registry.release_record(record, reason="owner-death")
        results.append(
            {
                "lease_id": record.lease_id,
                "status": "released" if released else "busy",
            }
        )
    return results


def validate_selection(selection: Sequence[str], rules: SelectionRules) -> None:
    if not isinstance(selection, (list, tuple)) or not selection:
        raise RemoteCIError("selection must be a non-empty argv list")
    found_selector = False
    waiting_for_value = False
    for value in selection:
        if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
            raise RemoteCIError("selection entries must be non-empty single-line strings")
        if waiting_for_value:
            waiting_for_value = False
            continue
        if value in rules.value_options:
            if value in {"-k", "-m", "--keyword", "--mark"}:
                found_selector = True
            waiting_for_value = True
            continue
        if value.startswith("-"):
            if value not in rules.allowed_options and not any(
                value.startswith(prefix) for prefix in rules.allowed_option_prefixes
            ):
                raise RemoteCIError(f"selection option is not allowed: {value!r}")
            if value in {"-k", "-m", "--keyword", "--mark"}:
                found_selector = True
            continue
        path = value.split("::", 1)[0].replace("\\", "/")
        if path.startswith("/") or path == ".." or path.startswith("../") or "/../" in f"/{path}/":
            raise RemoteCIError(f"selection path escapes configured roots: {value!r}")
        if not any(path.startswith(prefix) for prefix in rules.path_prefixes):
            raise RemoteCIError(f"selection path is outside configured roots: {value!r}")
        found_selector = True
    if waiting_for_value:
        raise RemoteCIError("selection option is missing its value")
    if rules.require_selector and not found_selector:
        raise RemoteCIError("selection must contain a configured path or selector")


def selection_test_files(selection: Sequence[str], rules: SelectionRules) -> tuple[str, ...]:
    files: list[str] = []
    waiting_for_value = False
    for value in selection:
        if waiting_for_value:
            waiting_for_value = False
            continue
        if value in rules.value_options:
            waiting_for_value = True
            continue
        if value.startswith("-"):
            continue
        path = value.split("::", 1)[0]
        if path not in files:
            files.append(path)
    return tuple(files)


def chunk_test_files(
    test_files: Sequence[str], *, size: int = 20, verbose_option: str = "-v"
) -> list[tuple[str, ...]]:
    """Split broad selections into named, verbose chunks."""
    # Small verbose chunks identify a hanging file without burning a whole gate.
    if size <= 0:
        raise ValueError("chunk size must be positive")
    files = tuple(test_files)
    return [tuple(files[start : start + size]) + (verbose_option,) for start in range(0, len(files), size)]


def validate_request(payload: Mapping[str, Any], config: DaemonConfig) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RemoteCIError("request must be an object")
    if payload.get("schema") != REQUEST_SCHEMA:
        raise RemoteCIError(f"request schema must be {REQUEST_SCHEMA}")
    request = dict(payload)
    request_id = _safe_id(request.get("request_id"), "request_id")
    kind = _string(request.get("kind"), "kind")
    if kind not in {"targeted", "gate"}:
        raise RemoteCIError("kind must be targeted or gate")
    for field in ("tip_sha", "candidate_sha"):
        value = request.get(field)
        if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
            raise RemoteCIError(f"{field} must be a hexadecimal commit SHA")
    test_files = request.get("test_files")
    if not isinstance(test_files, list) or not test_files or any(not isinstance(item, str) for item in test_files):
        raise RemoteCIError("test_files must be a non-empty list of strings")
    for path in test_files:
        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or normalized == ".." or normalized.startswith("../") or "/../" in f"/{normalized}/":
            raise RemoteCIError(f"test file escapes configured roots: {path!r}")
        if not any(normalized.startswith(prefix) for prefix in config.selection.path_prefixes):
            raise RemoteCIError(f"test file is outside configured roots: {path!r}")
    selection = request.get("selection")
    if not isinstance(selection, list):
        raise RemoteCIError("selection must be a list")
    validate_selection(selection, config.selection)
    requested_box = request.get("box")
    if requested_box is not None and requested_box not in config.boxes:
        raise RemoteCIError(f"unknown box: {requested_box!r}")
    purpose = _string(request.get("purpose", "remote CI"), "purpose")
    worker = request.get("worker")
    if worker is not None:
        _mapping(worker, "worker")
    if kind == "targeted" and len(test_files) > config.selection.max_targeted_files:
        raise RemoteCIError(
            f"targeted request has {len(test_files)} files; maximum is "
            f"{config.selection.max_targeted_files}"
        )
    request["request_id"] = request_id
    request["tip_sha"] = str(request["tip_sha"]).lower()
    request["candidate_sha"] = str(request["candidate_sha"]).lower()
    request["test_files"] = list(dict.fromkeys(test_files))
    request["selection"] = list(selection)
    request["purpose"] = purpose
    return request


def _safe_token_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()) or "box"


@dataclass(frozen=True)
class LoadSample:
    hostname: str
    load1: float
    p_cores: int


def parse_load_sample(value: Mapping[str, Any] | str, *, default_p_cores: int) -> LoadSample:
    if isinstance(value, Mapping):
        raw = value
    else:
        text = value.strip()
        raw: Mapping[str, Any]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            fields = text.split()
            parsed = {}
            for field in fields:
                if "=" in field:
                    key, child = field.split("=", 1)
                    parsed[key] = child
        raw = _mapping(parsed, "load sample")
    hostname = raw.get("hostname")
    if not isinstance(hostname, str) or not hostname.strip():
        raise AdmissionError("load sample did not include the answering hostname")
    try:
        load1 = float(raw.get("load1"))
    except (TypeError, ValueError) as exc:
        raise AdmissionError("load sample load1 is not numeric") from exc
    if not math.isfinite(load1) or load1 < 0:
        raise AdmissionError("load sample load1 must be finite and non-negative")
    raw_p_cores = raw.get("p_cores", default_p_cores)
    if isinstance(raw_p_cores, bool) or not isinstance(raw_p_cores, (int, float)) or raw_p_cores <= 0:
        raise AdmissionError("load sample p_cores must be positive")
    return LoadSample(hostname.strip(), load1, int(raw_p_cores))


@dataclass
class TokenLease:
    pool: "TokenPool"
    index: int
    path: Path
    handle: Any
    sample: LoadSample | None = None
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()

    def __enter__(self) -> "TokenLease":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


class TokenPool:
    """A shared per-box token pool backed solely by kernel-held flocks."""

    def __init__(
        self,
        box: BoxConfig,
        admission: AdmissionConfig,
        *,
        load_probe: Callable[[BoxConfig], LoadSample] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.box = box
        self.admission = admission
        self.load_probe = load_probe or self._default_load_probe
        self.sleeper = sleeper

    def _cap_limits(self) -> tuple[int, int]:
        p_cores = self.box.p_cores
        pool_size = self.box.token_pool_size
        path = self.admission.live_cap_file
        if path is not None and path.exists():
            # Operators can lower caps safely without editing a live runner.
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AdmissionError(f"cannot read live cap file {path}: {exc}") from exc
            caps = _mapping(raw, "live cap file")
            boxes = caps.get("boxes", caps)
            if isinstance(boxes, Mapping) and self.box.name in boxes:
                box_caps = _mapping(boxes[self.box.name], f"live cap file boxes.{self.box.name}")
                if "p_cores" in box_caps:
                    p_cores = _positive_int(box_caps["p_cores"], "live cap p_cores")
                if "token_pool_size" in box_caps:
                    pool_size = _positive_int(box_caps["token_pool_size"], "live cap token_pool_size")
        return p_cores, pool_size

    def _token_path(self, index: int) -> Path:
        return self.admission.token_directory / (
            f"{_safe_token_name(self.box.token_key)}.token-{index:04d}.lock"
        )

    def try_acquire(self) -> TokenLease | None:
        """Claim the first free token without probing load."""
        # The kernel owns the lock, so SIGKILL cannot strand a token file.
        _p_cores, pool_size = self._cap_limits()
        self.admission.token_directory.mkdir(parents=True, exist_ok=True)
        for index in range(pool_size):
            path = self._token_path(index)
            handle = path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    continue
                raise
            return TokenLease(self, index, path, handle)
        return None

    def acquire(self) -> TokenLease:
        """Wait in the queue until a token and a safe load sample coexist.

        A failed attempt sleeps once for the configured queue interval.  It
        never spins through token probes or starts one retry loop per arm.
        """
        while True:
            lease = self.try_acquire()
            if lease is None:
                self.sleeper(self.admission.queue_wait_seconds)
                continue
            try:
                p_cores, _pool_size = self._cap_limits()
                sample = self.load_probe(self.box)
                if sample.load1 <= p_cores:
                    lease.sample = sample
                    return lease
            except Exception:
                # Unknown load is not an admission; queue until it is measured.
                lease.release()
                self.sleeper(self.admission.queue_wait_seconds)
                continue
            lease.release()
            # Queue after an unsafe load instead of re-probing in a tight loop.
            self.sleeper(self.admission.queue_wait_seconds)

    def census(self) -> dict[str, int]:
        _p_cores, pool_size = self._cap_limits()
        free = 0
        for _index in range(pool_size):
            lease = self.try_acquire()
            if lease is None:
                continue
            free += 1
            lease.release()
        return {"total": pool_size, "free": free, "in_use": pool_size - free}

    def _default_load_probe(self, box: BoxConfig) -> LoadSample:
        command = expand_command(
            box.load_command,
            {
                "box": box.name,
                "host": box.host,
                "p_cores": str(box.p_cores),
            },
        )
        result = run_command(command, env=box.env, timeout=30)
        if result.returncode != 0:
            raise AdmissionError(
                f"load probe failed for {box.name}: {result.returncode} {result.stderr.strip()}"
            )
        return parse_load_sample(result.stdout, default_p_cores=box.p_cores)


def expand_command(template: Sequence[str], values: Mapping[str, str], **lists: Sequence[str]) -> list[str]:
    """Expand argv placeholders without invoking a shell."""
    expanded: list[str] = []
    for token in template:
        list_key = token[1:-1] if token.startswith("{") and token.endswith("}") else ""
        if list_key in lists:
            expanded.extend(str(item) for item in lists[list_key])
            continue
        result = token
        for key, value in values.items():
            result = result.replace("{" + key + "}", str(value))
        if "{" in result or "}" in result:
            raise RemoteCIError(f"unknown command placeholder in {token!r}")
        expanded.append(result)
    return expanded


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    cwd: Path | None = None,
) -> CommandResult:
    try:
        # A watcher/driver gets its own session so queue-runner exit cannot kill it.
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        raise RemoteCIError(f"configured command could not start: {argv[0] if argv else '<empty>'}: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return CommandResult(
            124,
            (stdout or exc.stdout or ""),
            (stderr or exc.stderr or "") + f"\ncommand timed out after {timeout}s",
            True,
        )
    return CommandResult(process.returncode, stdout, stderr, False)


@dataclass(frozen=True)
class RemoteRunIdentity:
    host: str
    pid: str
    start_token: str
    run_dir: str
    lock_dir: str = ""
    lease_id: str = ""
    lease_token: str = ""
    owner_identity: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, default_host: str = "") -> "RemoteRunIdentity":
        try:
            pid = str(value["pid"])
            start_token = str(value["start_token"])
            run_dir = str(value["run_dir"])
        except KeyError as exc:
            raise RemoteCIError(f"remote run identity missing {exc.args[0]}") from exc
        if not pid or not start_token or not run_dir:
            raise RemoteCIError("remote run identity fields must be non-empty")
        return cls(
            host=str(value.get("host", default_host)),
            pid=pid,
            start_token=start_token,
            run_dir=run_dir,
            lock_dir=str(value.get("lock_dir", "")),
            lease_id=str(value.get("lease_id", "")),
            lease_token=str(value.get("lease_token", "")),
            owner_identity=str(value.get("owner_identity", "")),
        )

    def to_dict(self) -> dict[str, str]:
        value = {
            "host": self.host,
            "pid": self.pid,
            "start_token": self.start_token,
            "run_dir": self.run_dir,
            "lock_dir": self.lock_dir,
        }
        if self.lease_id:
            value["lease_id"] = self.lease_id
        if self.lease_token:
            value["lease_token"] = self.lease_token
        if self.owner_identity:
            value["owner_identity"] = self.owner_identity
        return value


def parse_launch_identity(text: str, *, default_host: str = "") -> RemoteRunIdentity | None:
    match = _LAUNCH_RE.search(text)
    if match is None:
        return None
    fields: dict[str, str] = {}
    for token in match.group("fields").split():
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
    return RemoteRunIdentity.from_mapping(fields, default_host=default_host)


@dataclass(frozen=True)
class RemoteReceipt:
    hostname: str
    status: str
    returncode: int
    passed: int
    failed: int
    errors: int
    failing_node_ids: tuple[str, ...]
    collection_import_errors: tuple[str, ...]
    first_error_lines: tuple[str, ...]
    duration_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "hostname": self.hostname,
            "status": self.status,
            "returncode": self.returncode,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "failing_node_ids": list(self.failing_node_ids),
            "collection_import_errors": list(self.collection_import_errors),
            "first_error_lines": list(self.first_error_lines),
        }
        if self.duration_seconds is not None:
            result["duration_seconds"] = self.duration_seconds
        return result


def _list_field(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ReceiptError(f"{label} must be a string list")
    return tuple(value)


def parse_receipt(value: Mapping[str, Any] | str | os.PathLike[str]) -> RemoteReceipt:
    if isinstance(value, Mapping):
        raw = value
    else:
        path_or_text = os.fspath(value)
        try:
            if isinstance(value, os.PathLike) or not str(path_or_text).lstrip().startswith("{"):
                path = Path(path_or_text)
                text = path.read_text(encoding="utf-8")
            else:
                text = str(path_or_text)
            raw = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise ReceiptError(f"cannot parse receipt: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ReceiptError("receipt must be an object")
    if raw.get("schema", RECEIPT_SCHEMA) != RECEIPT_SCHEMA:
        raise ReceiptError(f"receipt schema must be {RECEIPT_SCHEMA}")
    hostname = raw.get("hostname")
    if not isinstance(hostname, str) or not hostname.strip():
        raise ReceiptError("receipt must contain the remote answering hostname")
    status = raw.get("status", "pass")
    if not isinstance(status, str) or not status.strip():
        raise ReceiptError("receipt status must be non-empty")
    def count(name: str) -> int:
        value = raw.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReceiptError(f"receipt {name} must be a non-negative integer")
        return value
    returncode = raw.get("returncode", raw.get("exit_code", 0))
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise ReceiptError("receipt returncode must be an integer")
    duration = raw.get("duration_seconds")
    if duration is not None:
        try:
            duration = float(duration)
        except (TypeError, ValueError) as exc:
            raise ReceiptError("receipt duration_seconds must be numeric") from exc
        if not math.isfinite(duration) or duration < 0:
            raise ReceiptError("receipt duration_seconds must be finite and non-negative")
    collection_errors = list(_list_field(raw.get("collection_import_errors"), "receipt collection_import_errors"))
    singular = raw.get("collection_import_error")
    if singular is not None:
        collection_errors.extend(_list_field(singular, "receipt collection_import_error"))
    return RemoteReceipt(
        hostname=hostname.strip(),
        status=status.strip(),
        returncode=returncode,
        passed=count("passed"),
        failed=count("failed"),
        errors=count("errors"),
        failing_node_ids=_list_field(raw.get("failing_node_ids"), "receipt failing_node_ids"),
        collection_import_errors=tuple(dict.fromkeys(collection_errors)),
        first_error_lines=_list_field(raw.get("first_error_lines"), "receipt first_error_lines"),
        duration_seconds=duration,
    )


def receipt_from_output(text: str) -> RemoteReceipt:
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        try:
            return parse_receipt(parsed)
        except ReceiptError:
            pass
    for line in reversed(text.splitlines()):
        match = _JSON_LINE_RE.match(line)
        if not match:
            continue
        try:
            return parse_receipt(match.group(1))
        except ReceiptError:
            continue
    raise ReceiptError("remote output did not contain a valid receipt")


@dataclass(frozen=True)
class ArmSpec:
    arm: str
    sha: str
    tip_sha: str
    candidate_sha: str
    test_files: tuple[str, ...]
    selection: tuple[str, ...]
    request_id: str
    box: str
    overlay: bool


@dataclass(frozen=True)
class ArmOutcome:
    arm: str
    status: str
    returncode: int
    receipt: RemoteReceipt | None
    identity: RemoteRunIdentity | None = None
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None
    lease: RemoteLeaseRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "arm": self.arm,
            "status": self.status,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
        }
        if self.receipt is not None:
            value["receipt"] = self.receipt.to_dict()
        if self.identity is not None:
            value["remote_run"] = self.identity.to_dict()
        if self.lease is not None:
            value["lease"] = self.lease.to_dict()
        if self.error:
            value["error"] = self.error
        return value


Executor = Callable[[Sequence[str], Mapping[str, str], float | None], CommandResult]


class RemoteRunner:
    """Run one arm only after shared admission and cancel timed-out arms."""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        executor: Executor | None = None,
        load_probe: Callable[[BoxConfig], LoadSample] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.executor = executor or (lambda argv, env, timeout: run_command(argv, env=env, timeout=timeout))
        self.load_probe = load_probe
        self.sleeper = sleeper
        self.leases = RemoteLeaseRegistry(config)
        self.pools = {
            name: TokenPool(
                box,
                config.admission,
                load_probe=load_probe,
                sleeper=sleeper,
            )
            for name, box in config.boxes.items()
        }

    def _box(self, name: str) -> BoxConfig:
        try:
            return self.config.boxes[name]
        except KeyError as exc:
            raise RemoteCIError(f"unknown box {name!r}") from exc

    @staticmethod
    def _managed_run_directory(box: BoxConfig, run_directory: str) -> str:
        root = box.managed_run_directory.resolve(strict=False)
        child = Path(run_directory).resolve(strict=False)
        if not child.is_absolute():
            raise RemoteCIError("remote run directory must be absolute")
        try:
            child.relative_to(root)
        except ValueError as exc:
            raise RemoteCIError(
                f"remote run directory is outside managed root {root}: {run_directory}"
            ) from exc
        return str(child)

    def _self_cap(self, box: BoxConfig) -> int:
        cap_file = self.config.admission.live_cap_file
        if cap_file is None or not cap_file.exists():
            return self.config.runner.self_cap
        try:
            raw = json.loads(cap_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdmissionError(f"cannot read live cap file {cap_file}: {exc}") from exc
        caps = _mapping(raw, "live cap file")
        raw_self_cap = caps.get("self_cap")
        boxes = caps.get("boxes")
        if isinstance(boxes, Mapping) and box.name in boxes:
            box_caps = _mapping(boxes[box.name], f"live cap file boxes.{box.name}")
            raw_self_cap = box_caps.get("self_cap", raw_self_cap)
        return (
            self.config.runner.self_cap
            if raw_self_cap is None
            else _positive_int(raw_self_cap, "live cap self_cap")
        )

    def run_arm(self, spec: ArmSpec) -> ArmOutcome:
        box = self._box(spec.box)
        pool = self.pools[spec.box]
        # The token is deliberately acquired before argv expansion: rendering,
        # object pushing, and remote run creation all happen inside this lease.
        with pool.acquire() as lease:
            remote_lease = self.leases.acquire(
                box,
                request_id=spec.request_id,
                arm=spec.arm,
            )
            release_reason = "completed"
            try:
                run_id = remote_lease.record.lease_id
                values = {
                    "box": box.name,
                    "host": box.host,
                    "arm": spec.arm,
                    "sha": spec.sha,
                    "tip_sha": spec.tip_sha,
                    "candidate_sha": spec.candidate_sha,
                    "request_id": spec.request_id,
                    "run_id": run_id,
                    "lease_id": remote_lease.record.lease_id,
                    "lease_token": remote_lease.record.lease_token,
                    "owner_identity": remote_lease.record.owner_identity,
                    "run_dir": remote_lease.record.run_directory,
                    "managed_run_directory": remote_lease.record.managed_run_directory,
                    "token_index": str(lease.index),
                    "overlay": "1" if spec.overlay else "0",
                    "chunk_size": str(self.config.runner.chunk_size),
                    "verbose_option": self.config.runner.verbose_option,
                }
                command = expand_command(
                    self.config.runner.command,
                    values,
                    selection=spec.selection,
                    test_files=spec.test_files,
                    test_command=self.config.runner.test_command,
                )
                env = os.environ.copy()
                env.update(self.config.runner.env)
                env.update(box.env)
                env.update(
                    {
                        "GOALFLIGHT_REMOTE_CI_ARM": spec.arm,
                        "GOALFLIGHT_REMOTE_CI_SHA": spec.sha,
                        "GOALFLIGHT_REMOTE_CI_TIP_SHA": spec.tip_sha,
                        "GOALFLIGHT_REMOTE_CI_CANDIDATE_SHA": spec.candidate_sha,
                        "GOALFLIGHT_REMOTE_CI_REQUEST_ID": spec.request_id,
                        "GOALFLIGHT_REMOTE_CI_RUN_ID": run_id,
                        "GOALFLIGHT_REMOTE_CI_LEASE_ID": remote_lease.record.lease_id,
                        "GOALFLIGHT_REMOTE_CI_LEASE_TOKEN": remote_lease.record.lease_token,
                        "GOALFLIGHT_REMOTE_CI_OWNER_IDENTITY": remote_lease.record.owner_identity,
                        "GOALFLIGHT_REMOTE_CI_RUN_DIR": remote_lease.record.run_directory,
                        "GOALFLIGHT_REMOTE_CI_MANAGED_RUN_DIRECTORY": remote_lease.record.managed_run_directory,
                        "GOALFLIGHT_REMOTE_CI_LEASE_RECORD_JSON": json.dumps(
                            remote_lease.record.to_dict(), sort_keys=True
                        ),
                        "GOALFLIGHT_REMOTE_CI_OVERLAY": "1" if spec.overlay else "0",
                        "GOALFLIGHT_REMOTE_CI_CHUNK_SIZE": str(self.config.runner.chunk_size),
                        "GOALFLIGHT_REMOTE_CI_VERBOSE_OPTION": self.config.runner.verbose_option,
                        "GOALFLIGHT_REMOTE_CI_TEST_FILES_JSON": json.dumps(list(spec.test_files)),
                        "GOALFLIGHT_REMOTE_CI_SELECTION_JSON": json.dumps(list(spec.selection)),
                        "GOALFLIGHT_REMOTE_CI_TEST_COMMAND_JSON": json.dumps(
                            list(self.config.runner.test_command)
                        ),
                        self.config.runner.self_cap_env: str(self._self_cap(box)),
                    }
                )
                # The cap keeps one arm from consuming the entire shared box.
                completed = self.executor(command, env, self.config.runner.timeout_seconds)
                identity = parse_launch_identity(
                    completed.stdout + "\n" + completed.stderr,
                    default_host=box.host,
                )
                if identity is not None:
                    self._managed_run_directory(box, identity.run_dir)
                    identity = RemoteRunIdentity(
                        identity.host,
                        identity.pid,
                        identity.start_token,
                        identity.run_dir,
                        identity.lock_dir,
                        identity.lease_id or remote_lease.record.lease_id,
                        identity.lease_token or remote_lease.record.lease_token,
                        identity.owner_identity or remote_lease.record.owner_identity,
                    )
                if completed.timed_out:
                    # A local timeout must actively end the remote process tree.
                    release_reason = "cancelled"
                    cancelled = False
                    error = "local timeout"
                    if identity is not None:
                        try:
                            self.cancel_remote(spec, box, identity, reason="local-timeout")
                            cancelled = True
                        except RemoteCIError as exc:
                            error = f"local timeout; remote cancellation failed: {exc}"
                    else:
                        error = "local timeout without a remote run identity"
                    return ArmOutcome(
                        spec.arm,
                        "timeout",
                        completed.returncode,
                        None,
                        identity,
                        timed_out=True,
                        cancelled=cancelled,
                        error=error,
                        lease=remote_lease.record,
                    )
                try:
                    # Hostname comes from the measured remote receipt, never the alias.
                    receipt = receipt_from_output(completed.stdout + "\n" + completed.stderr)
                except ReceiptError as exc:
                    return ArmOutcome(
                        spec.arm,
                        "red",
                        completed.returncode,
                        None,
                        identity,
                        error=str(exc),
                        lease=remote_lease.record,
                    )
                status = "green" if completed.returncode == 0 and _receipt_is_green(receipt) else "red"
                return ArmOutcome(
                    spec.arm,
                    status,
                    completed.returncode,
                    receipt,
                    identity,
                    lease=remote_lease.record,
                )
            finally:
                remote_lease.release(release_reason)

    def reattach_launch_log(self, launch_log: Path, spec: ArmSpec) -> ArmOutcome:
        """Resume a remote run from its recorded launch identity."""
        try:
            text = launch_log.read_text(encoding="utf-8")
        except OSError as exc:
            raise RemoteCIError(f"cannot read launch log {launch_log}: {exc}") from exc
        identity = parse_launch_identity(text, default_host=self._box(spec.box).host)
        if identity is None:
            raise RemoteCIError("launch log does not contain a complete remote run identity")
        return self.reattach(spec, identity)

    def _finish_reattach(
        self,
        outcome: ArmOutcome,
        identity: RemoteRunIdentity,
        *,
        reason: str,
    ) -> ArmOutcome:
        if identity.lease_token:
            self.leases.release_matching(
                run_directory=identity.run_dir,
                lease_token=identity.lease_token,
                reason=reason,
            )
        return outcome

    def reattach(self, spec: ArmSpec, identity: RemoteRunIdentity) -> ArmOutcome:
        """Watch and collect a run without ever starting a second test command."""
        box = self._box(spec.box)
        self._managed_run_directory(box, identity.run_dir)
        values = {
            "box": box.name,
            "host": box.host,
            "arm": spec.arm,
            "sha": spec.sha,
            "tip_sha": spec.tip_sha,
            "candidate_sha": spec.candidate_sha,
            "request_id": spec.request_id,
            "run_id": f"{spec.request_id}-{spec.arm}",
            "lease_id": identity.lease_id,
            "lease_token": identity.lease_token,
            "owner_identity": identity.owner_identity,
            "pid": identity.pid,
            "start_token": identity.start_token,
            "run_dir": identity.run_dir,
            "managed_run_directory": str(box.managed_run_directory),
            "lock_dir": identity.lock_dir,
        }
        watch_command = expand_command(self.config.runner.watch_command, values)
        collect_command = expand_command(self.config.runner.collect_command, values)
        env = os.environ.copy()
        env.update(self.config.runner.env)
        env.update(box.env)
        env.update(
            {
                "GOALFLIGHT_REMOTE_CI_REATTACH": "1",
                "GOALFLIGHT_REMOTE_CI_REMOTE_PID": identity.pid,
                "GOALFLIGHT_REMOTE_CI_REMOTE_START_TOKEN": identity.start_token,
                "GOALFLIGHT_REMOTE_CI_REMOTE_RUN_DIR": identity.run_dir,
                "GOALFLIGHT_REMOTE_CI_LEASE_ID": identity.lease_id,
                "GOALFLIGHT_REMOTE_CI_LEASE_TOKEN": identity.lease_token,
                "GOALFLIGHT_REMOTE_CI_OWNER_IDENTITY": identity.owner_identity,
            }
        )
        # Re-attach is watch/collect only; a dead watcher must never rerun tests.
        watched = self.executor(watch_command, env, self.config.runner.timeout_seconds)
        if watched.timed_out:
            cancelled = False
            try:
                self.cancel_remote(spec, box, identity, reason="reattach-timeout")
                cancelled = True
            except RemoteCIError as exc:
                return self._finish_reattach(ArmOutcome(
                    spec.arm,
                    "timeout",
                    watched.returncode,
                    None,
                    identity,
                    timed_out=True,
                    error=f"reattach timeout; cancellation failed: {exc}",
                ), identity, reason="cancelled")
            return self._finish_reattach(ArmOutcome(
                spec.arm,
                "timeout",
                watched.returncode,
                None,
                identity,
                timed_out=True,
                cancelled=cancelled,
                error="reattach timeout",
            ), identity, reason="cancelled")
        collected = self.executor(collect_command, env, self.config.runner.timeout_seconds)
        if collected.timed_out:
            try:
                self.cancel_remote(spec, box, identity, reason="reattach-collect-timeout")
            except RemoteCIError as exc:
                return self._finish_reattach(ArmOutcome(
                    spec.arm,
                    "timeout",
                    collected.returncode,
                    None,
                    identity,
                    timed_out=True,
                    error=f"reattach collection timeout; cancellation failed: {exc}",
                ), identity, reason="cancelled")
            return self._finish_reattach(ArmOutcome(
                spec.arm,
                "timeout",
                collected.returncode,
                None,
                identity,
                timed_out=True,
                cancelled=True,
                error="reattach collection timeout",
            ), identity, reason="cancelled")
        try:
            receipt = receipt_from_output(collected.stdout + "\n" + collected.stderr)
        except ReceiptError:
            try:
                receipt = receipt_from_output(watched.stdout + "\n" + watched.stderr)
            except ReceiptError as exc:
                return self._finish_reattach(ArmOutcome(
                    spec.arm,
                    "red",
                    collected.returncode,
                    None,
                    identity,
                    error=str(exc),
                ), identity, reason="completed")
        status = (
            "green"
            if watched.returncode == 0
            and collected.returncode == 0
            and _receipt_is_green(receipt)
            else "red"
        )
        return self._finish_reattach(
            ArmOutcome(spec.arm, status, collected.returncode, receipt, identity),
            identity,
            reason="completed",
        )

    def cancel_remote(
        self,
        spec: ArmSpec,
        box: BoxConfig,
        identity: RemoteRunIdentity,
        *,
        reason: str,
    ) -> CommandResult:
        self._managed_run_directory(box, identity.run_dir)
        values = {
            "box": box.name,
            "host": box.host,
            "arm": spec.arm,
            "sha": spec.sha,
            "tip_sha": spec.tip_sha,
            "candidate_sha": spec.candidate_sha,
            "request_id": spec.request_id,
            "run_id": f"{spec.request_id}-{spec.arm}",
            "lease_id": identity.lease_id,
            "lease_token": identity.lease_token,
            "owner_identity": identity.owner_identity,
            "pid": identity.pid,
            "start_token": identity.start_token,
            "run_dir": identity.run_dir,
            "managed_run_directory": str(box.managed_run_directory),
            "lock_dir": identity.lock_dir,
            "reason": reason,
        }
        command = expand_command(self.config.runner.cancel_command, values)
        env = os.environ.copy()
        env.update(self.config.runner.env)
        env.update(box.env)
        env.update(
            {
                "GOALFLIGHT_REMOTE_CI_CANCEL_REASON": reason,
                "GOALFLIGHT_REMOTE_CI_REMOTE_PID": identity.pid,
                "GOALFLIGHT_REMOTE_CI_REMOTE_START_TOKEN": identity.start_token,
                "GOALFLIGHT_REMOTE_CI_REMOTE_RUN_DIR": identity.run_dir,
                "GOALFLIGHT_REMOTE_CI_LEASE_ID": identity.lease_id,
                "GOALFLIGHT_REMOTE_CI_LEASE_TOKEN": identity.lease_token,
                "GOALFLIGHT_REMOTE_CI_OWNER_IDENTITY": identity.owner_identity,
            }
        )
        result = self.executor(command, env, min(60.0, self.config.runner.timeout_seconds))
        if result.returncode != 0:
            raise RemoteCIError(
                f"remote cancellation failed for {identity.run_dir}: {result.returncode}"
            )
        return result


def _receipt_is_green(receipt: RemoteReceipt) -> bool:
    return (
        receipt.status.lower() in {"pass", "passed", "green"}
        and receipt.failed == 0
        and receipt.errors == 0
        and not receipt.collection_import_errors
    )


def _arm_is_red(outcome: Mapping[str, Any] | ArmOutcome) -> bool:
    if isinstance(outcome, ArmOutcome):
        if outcome.status.lower() not in {"green", "pass", "passed"}:
            return True
        receipt = outcome.receipt
        return receipt is None or not _receipt_is_green(receipt)
    status = str(outcome.get("status", "red")).lower()
    if status not in {"green", "pass", "passed"}:
        return True
    receipt_value = outcome.get("receipt")
    if not isinstance(receipt_value, Mapping):
        return True
    try:
        return not _receipt_is_green(parse_receipt(receipt_value))
    except ReceiptError:
        return True


def matched_pair_verdict(base: ArmOutcome | Mapping[str, Any], candidate: ArmOutcome | Mapping[str, Any]) -> dict[str, Any]:
    """Return a verdict where BASE collection ImportErrors are RED."""
    # The pair prevents a candidate-only green from hiding a broken tip.
    base_red = _arm_is_red(base)
    candidate_red = _arm_is_red(candidate)

    def failures(value: ArmOutcome | Mapping[str, Any]) -> set[str]:
        receipt = value.receipt if isinstance(value, ArmOutcome) else value.get("receipt")
        if isinstance(receipt, RemoteReceipt):
            return set(receipt.failing_node_ids) | set(receipt.collection_import_errors)
        if isinstance(receipt, Mapping):
            return set(_list_field(receipt.get("failing_node_ids"), "failing_node_ids")) | set(
                _list_field(receipt.get("collection_import_errors"), "collection_import_errors")
            )
        return set()

    base_failures = failures(base)
    candidate_failures = failures(candidate)
    base_dict = base.to_dict() if isinstance(base, ArmOutcome) else dict(base)
    candidate_dict = candidate.to_dict() if isinstance(candidate, ArmOutcome) else dict(candidate)
    return {
        "schema": RESULT_SCHEMA,
        "status": "RED" if base_red or candidate_red else "GREEN",
        "base": base_dict,
        "candidate": candidate_dict,
        "base_red": base_red,
        "candidate_red": candidate_red,
        "new_failure_node_ids": sorted(candidate_failures - base_failures),
        "fixed_failure_node_ids": sorted(base_failures - candidate_failures),
        "measured_hostnames": {
            "base": _outcome_hostname(base),
            "candidate": _outcome_hostname(candidate),
        },
    }


def _outcome_hostname(value: ArmOutcome | Mapping[str, Any]) -> str | None:
    receipt = value.receipt if isinstance(value, ArmOutcome) else value.get("receipt")
    if isinstance(receipt, RemoteReceipt):
        return receipt.hostname
    if isinstance(receipt, Mapping):
        hostname = receipt.get("hostname")
        return hostname if isinstance(hostname, str) and hostname else None
    return None


def build_pair_specs(request: Mapping[str, Any], config: DaemonConfig) -> tuple[ArmSpec, ArmSpec]:
    box = str(request.get("box") or sorted(config.boxes)[0])
    selection = tuple(str(value) for value in request["selection"])
    base_selection = list(selection)
    if config.runner.base_collection_option not in base_selection:
        base_selection.append(config.runner.base_collection_option)
    test_files = tuple(str(value) for value in request["test_files"])
    common = {
        "tip_sha": str(request["tip_sha"]),
        "candidate_sha": str(request["candidate_sha"]),
        "test_files": test_files,
        "request_id": str(request["request_id"]),
        "box": box,
    }
    return (
        ArmSpec(arm="base", sha=common["tip_sha"], selection=tuple(base_selection), overlay=True, **common),
        ArmSpec(arm="candidate", sha=common["candidate_sha"], selection=selection, overlay=False, **common),
    )


def run_matched_pair(request: Mapping[str, Any], runner: RemoteRunner) -> dict[str, Any]:
    base, candidate = build_pair_specs(request, runner.config)
    base_outcome = runner.run_arm(base)
    candidate_outcome = runner.run_arm(candidate)
    return matched_pair_verdict(base_outcome, candidate_outcome)


def run_targeted(request: Mapping[str, Any], runner: RemoteRunner) -> dict[str, Any]:
    box = str(request.get("box") or sorted(runner.config.boxes)[0])
    spec = ArmSpec(
        arm="candidate",
        sha=str(request["candidate_sha"]),
        tip_sha=str(request["tip_sha"]),
        candidate_sha=str(request["candidate_sha"]),
        test_files=tuple(str(value) for value in request["test_files"]),
        selection=tuple(str(value) for value in request["selection"]),
        request_id=str(request["request_id"]),
        box=box,
        overlay=False,
    )
    outcome = runner.run_arm(spec)
    return {
        "schema": RESULT_SCHEMA,
        "status": "GREEN" if not _arm_is_red(outcome) else "RED",
        "candidate": outcome.to_dict(),
        "measured_hostnames": {"candidate": _outcome_hostname(outcome)},
    }


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def submit_request(source: Path, config: DaemonConfig) -> Path:
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteCIError(f"cannot read request {source}: {exc}") from exc
    request = validate_request(payload, config)
    destination = config.queue_dir / f"{request['request_id']}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RemoteCIError(f"request already queued: {request['request_id']}")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise RemoteCIError(f"request already queued: {request['request_id']}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return destination


class DaemonLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any | None = None

    def __enter__(self) -> "DaemonLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RemoteCIError(f"daemon lock is already held: {self.path}") from exc
            raise
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class GateDaemon:
    def __init__(self, config: DaemonConfig, *, runner: RemoteRunner | None = None) -> None:
        self.config = config
        self.runner = runner or RemoteRunner(config)

    def pending_paths(self) -> list[Path]:
        self.config.queue_dir.mkdir(parents=True, exist_ok=True)
        return sorted(self.config.queue_dir.glob("*.json"), key=lambda path: path.name)

    def poll_once(self) -> dict[str, Any] | None:
        # The daemon is the single queue runner; waiting belongs to this lane.
        for request_path in self.pending_paths():
            try:
                raw = json.loads(request_path.read_text(encoding="utf-8"))
                request = validate_request(raw, self.config)
            except (OSError, json.JSONDecodeError, RemoteCIError) as exc:
                result = {"schema": RESULT_SCHEMA, "status": "ERROR", "error": str(exc)}
                request_id = request_path.stem
            else:
                request_id = str(request["request_id"])
                run_state = self.config.state_dir / "runs" / f"{request_id}.json"
                _atomic_write_json(
                    run_state,
                    {
                        "schema": RESULT_SCHEMA,
                        "request_id": request_id,
                        "state": "running",
                        "owner_pid": os.getpid(),
                        "started_at": _utc_now(),
                    },
                )
                try:
                    if request["kind"] == "gate":
                        result = run_matched_pair(request, self.runner)
                    else:
                        result = run_targeted(request, self.runner)
                    result["request_id"] = request_id
                    result["finished_at"] = _utc_now()
                except Exception as exc:
                    result = {
                        "schema": RESULT_SCHEMA,
                        "request_id": request_id,
                        "status": "ERROR",
                        "error": str(exc),
                        "finished_at": _utc_now(),
                    }
                _atomic_write_json(
                    run_state,
                    {
                        "schema": RESULT_SCHEMA,
                        "request_id": request_id,
                        "state": "finished",
                        "owner_pid": os.getpid(),
                        "finished_at": _utc_now(),
                        "result": result,
                    },
                )
            _atomic_write_json(self.config.result_dir / f"{request_id}.json", result)
            request_path.unlink(missing_ok=True)
            return result
        return None

    def run(self, *, once: bool = False) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.result_dir.mkdir(parents=True, exist_ok=True)
        with DaemonLock(self.config.lock_file):
            if self.config.pid_file is not None:
                _atomic_write_json(self.config.pid_file, {"pid": os.getpid(), "started_at": _utc_now()})
            try:
                while True:
                    self.poll_once()
                    if once:
                        return
                    time.sleep(self.config.poll_seconds)
            finally:
                if self.config.pid_file is not None:
                    self.config.pid_file.unlink(missing_ok=True)


def _pid_alive(pid: Any) -> bool | None:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def reap_orphans(
    records: Iterable[Mapping[str, Any]],
    *,
    owner_alive: Callable[[Mapping[str, Any]], bool | None] = lambda record: _pid_alive(record.get("owner_pid")),
    cancel: Callable[[RemoteRunIdentity, str], None],
    release_lease: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Cancel recorded remote runs whose local owner is gone.

    Cancellation always carries pid, start token, and run directory so a reused
    remote pid cannot be mistaken for the original run.
    """
    # Exact remote identity prevents PID reuse from cancelling an unrelated run.
    results: list[dict[str, Any]] = []
    for record in records:
        if str(record.get("state", "")) != "running":
            continue
        liveness = owner_alive(record)
        if liveness is True:
            results.append({"request_id": record.get("request_id"), "status": "owned"})
            continue
        if liveness is not False:
            # A failed liveness probe is not evidence that the owner died.
            results.append({"request_id": record.get("request_id"), "status": "unknown"})
            continue
        try:
            identity = RemoteRunIdentity.from_mapping(record.get("remote_run", {}))
        except RemoteCIError as exc:
            results.append(
                {"request_id": record.get("request_id"), "status": "manual", "error": str(exc)}
            )
            continue
        cancel(identity, "orphaned-remote-run")
        lease_released = None if release_lease is None else release_lease(record)
        results.append(
            {
                "request_id": record.get("request_id"),
                "status": "cancelled",
                "remote_run": identity.to_dict(),
                **({"lease_released": lease_released} if lease_released is not None else {}),
            }
        )
    return results


def health_census(
    config: DaemonConfig,
    *,
    load_probe: Callable[[BoxConfig], LoadSample] | None = None,
) -> dict[str, Any]:
    """Collect one measured hostname/load and one token census per box."""
    # One bounded sample per box exposes pressure without creating probe churn.
    pools = {
        name: TokenPool(box, config.admission, load_probe=load_probe)
        for name, box in config.boxes.items()
    }
    boxes: list[dict[str, Any]] = []
    for name in sorted(config.boxes):
        box = config.boxes[name]
        try:
            sample = (load_probe or pools[name].load_probe)(box)
            load = {
                "hostname": sample.hostname,
                "load1": sample.load1,
                "p_cores": sample.p_cores,
                "load_within_p_cores": sample.load1 <= box.p_cores,
            }
            status = "ok"
        except Exception as exc:
            load = {"hostname": None, "error": str(exc)}
            status = "unknown"
        boxes.append(
            {
                "box": name,
                "status": status,
                "load": load,
                "tokens": pools[name].census(),
            }
        )
    return {"schema": "goalflight.remote-ci.health.v1", "measured_at": _utc_now(), "boxes": boxes}


def _load_records(config: DaemonConfig) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    run_dir = config.state_dir / "runs"
    if not run_dir.is_dir():
        return records
    for path in sorted(run_dir.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            records.append(dict(value))
    return records


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="goalflight_remote_ci")
    parser.add_argument("--config", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    submit = subparsers.add_parser("submit")
    submit.add_argument("request", type=Path)
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--once", action="store_true")
    run_once = subparsers.add_parser("run-once")
    del run_once
    reattach = subparsers.add_parser("reattach")
    reattach.add_argument("launch_log", type=Path)
    reattach.add_argument("--box", required=True)
    reattach.add_argument("--request-id", default="reattach")
    reattach.add_argument("--arm", default="candidate")
    reattach.add_argument("--sha", default="0" * 40)
    reattach.add_argument("--tip-sha", default="0" * 40)
    reattach.add_argument("--candidate-sha", default="0" * 40)
    subparsers.add_parser("health")
    subparsers.add_parser("list", help="list durable remote leases")
    subparsers.add_parser("reap")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "validate":
            print(f"valid: {config.path}")
        elif args.command == "submit":
            print(submit_request(args.request, config))
        elif args.command == "daemon":
            GateDaemon(config).run(once=args.once)
        elif args.command == "run-once":
            result = GateDaemon(config).poll_once()
            if result is not None:
                print(json.dumps(result, sort_keys=True))
        elif args.command == "reattach":
            runner = RemoteRunner(config)
            spec = ArmSpec(
                args.arm,
                args.sha,
                args.tip_sha,
                args.candidate_sha,
                (),
                (),
                args.request_id,
                args.box,
                False,
            )
            print(json.dumps(runner.reattach_launch_log(args.launch_log, spec).to_dict(), sort_keys=True))
        elif args.command == "health":
            print(json.dumps(health_census(config), indent=2, sort_keys=True))
        elif args.command == "list":
            print(json.dumps(list_remote_leases(config), indent=2, sort_keys=True))
        elif args.command == "reap":
            runner = RemoteRunner(config)
            registry = RemoteLeaseRegistry(config)

            def release_lease(record: Mapping[str, Any]) -> bool:
                value = record.get("lease")
                if isinstance(value, Mapping):
                    try:
                        lease = RemoteLeaseRecord.from_mapping(value)
                    except RemoteCIError:
                        return False
                    return registry.release_record(lease, reason="owner-death")
                remote_run = record.get("remote_run")
                if isinstance(remote_run, Mapping):
                    run_directory = remote_run.get("run_dir")
                    lease_token = remote_run.get("lease_token")
                    if isinstance(run_directory, str) and isinstance(lease_token, str):
                        return registry.release_matching(
                            run_directory=run_directory,
                            lease_token=lease_token,
                            reason="owner-death",
                        )
                return False

            def cancel(identity: RemoteRunIdentity, reason: str) -> None:
                box_name = next(
                    (name for name, box in config.boxes.items() if box.host == identity.host),
                    sorted(config.boxes)[0],
                )
                spec = ArmSpec(
                    "reap", "0" * 40, "0" * 40, "0" * 40, (), (),
                    str(identity.run_dir), box_name, False,
                )
                runner.cancel_remote(spec, config.boxes[box_name], identity, reason=reason)
            print(
                json.dumps(
                    {
                        "remote_runs": reap_orphans(
                            _load_records(config),
                            cancel=cancel,
                            release_lease=release_lease,
                        ),
                        "leases": cleanup_dead_leases(config),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        return 0
    except (RemoteCIError, OSError) as exc:
        print(f"goalflight-remote-ci: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
