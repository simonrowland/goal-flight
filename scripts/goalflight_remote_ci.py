#!/usr/bin/env python3
"""Project-neutral remote CI runner and gate daemon.

The transport, test command, hosts, paths, and selection policy are supplied by
one project configuration.  This module owns the cross-project contracts:
shared flock-token admission, one FIFO queue runner, matched BASE/CAND verdicts,
receipt validation, timeout cancellation, orphan cleanup, and health census.
"""

from __future__ import annotations

import argparse
import base64
import copy
import shlex
import contextlib
import concurrent.futures
import datetime as _datetime
import errno
import fcntl
import gzip
import hashlib
import io
import ipaddress
import json
import logging
import math
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import resource
import socket
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, BinaryIO, Callable, Mapping, Sequence

import goalflight_compat
import goalflight_dispatch_paths
import goalflight_ledger


CONFIG_SCHEMA = "goalflight.remote-ci.config.v2"
# Owner saturation rule (2026-10-19): refuse at 2x physical cores and 95% busy.
SATURATION_LOAD_FACTOR = 2
SATURATION_BUSY_FRACTION = 0.95
PROFILE_ENV_UNAVAILABLE_REASON = "profile-env-unavailable-locally"
REQUEST_SCHEMA = "goalflight.remote-ci.request.v1"
RECEIPT_SCHEMA = "goalflight.remote-ci.receipt.v1"
RESULT_SCHEMA = "goalflight.remote-ci.result.v1"
NODE_PROTOCOL_VERSION = 5
NODE_HELPER_INSTALL_REQUIRED = 73
REMOTE_UNAVAILABLE_EXIT = 69
DEFAULT_RESULT_TAIL_KIB = 16
MAX_RESULT_TAIL_KIB = 64
SSH_CONTROL_PATH_LIMIT = 104
REMOTE_STATUS_TIMEOUT_SECONDS = 30.0
# Direct SSH measurement was unavailable from this worker seat. Incident traces
# show 7–9s enqueue transport failures with no allowance, so reserve 12s for SSH
# setup and request transfer above the node-side seat wait.
REMOTE_TRANSPORT_ALLOWANCE_SECONDS = 12.0
REMOTE_HEALTH_PROBE_TIMEOUT_SECONDS = 12.0
REMOTE_SETUP_TIMEOUT_SECONDS = 300.0
REMOTE_FINALIZATION_TIMEOUT_SECONDS = 300.0
SUBMITTED_TICKET_TTL_SECONDS = 24 * 60 * 60
SNAPSHOT_SETUP_TIMEOUT_SECONDS = 120.0
DAEMON_REATTACH_WINDOW_SECONDS = 120.0
DAEMON_DRAIN_TIMEOUT_SECONDS = 1800.0
BUNDLE_BASIS_TIMEOUT_SECONDS = 30.0
# The configured goal-flight repo currently produces a 9.6 MB full bundle.
# Five minutes gives first-contact bundling ample room while matching the
# existing bundle-transfer ceiling; this work is independent of admission.
BUNDLE_PREPARATION_TIMEOUT_SECONDS = 300.0
BUNDLE_TRANSFER_TIMEOUT_SECONDS = 300.0
REMOTE_STREAM_JOIN_TIMEOUT_SECONDS = 5.0
REMOTE_PROCESS_REAP_TIMEOUT_SECONDS = 5.0
MAX_SUBMIT_BUNDLE_BYTES = 512 * 1024 * 1024
_LOG = logging.getLogger(__name__)
REMOTE_ENV_ALLOWLIST = frozenset({
    "CI", "EXERCISE_FAIL", "LANG", "LC_ALL", "NODE_OPTIONS", "PYTHONHASHSEED",
    "PYTHONPATH", "TZ",
})
RECEIPT_METADATA_FIELDS = frozenset({"dispatch_id", "controller_label"})

def _remote_phase_budgets(config):
    return {
        "setup": REMOTE_SETUP_TIMEOUT_SECONDS,
        "execution": config.runner.timeout_seconds,
        "finalization": REMOTE_FINALIZATION_TIMEOUT_SECONDS,
    }


def _remote_result_budget_seconds(config, *, executions: int = 1):
    phase_budgets = _remote_phase_budgets(config)
    single_request_budget = (
        SNAPSHOT_SETUP_TIMEOUT_SECONDS + BUNDLE_BASIS_TIMEOUT_SECONDS
        + BUNDLE_PREPARATION_TIMEOUT_SECONDS + BUNDLE_TRANSFER_TIMEOUT_SECONDS
        + sum(phase_budgets.values())
        # Command requests keep the local spool cap and start a fresh Studio
        # admission window once their snapshot is ready.
        + 2 * config.admission.max_wait_seconds
        + max(30.0, 2.0 * config.poll_seconds + 10.0)
    )
    return single_request_budget * executions

_REMOTE_NODE_LAUNCHER = r'''import base64,hashlib,json,os,stat,sys,tempfile
stream_submit=sys.argv[1:]==["--stream-submit"]
stream_install=len(sys.argv)>2 and sys.argv[2]=="--install-helper-from-stdin"
def read_exact(stream,size):
    chunks=[]
    remaining=size
    while remaining:
        chunk=stream.read(min(65536,remaining))
        if not chunk:
            raise ValueError("remote CI submit stream ended before its frame was complete")
        chunks.append(chunk)
        remaining-=len(chunk)
    return b"".join(chunks)
source_arg=None
if stream_submit:
    stream=sys.stdin.buffer
    magic=b"GFRCI-SUBMIT-STREAM/1\n"
    if read_exact(stream,len(magic))!=magic:
        raise ValueError("invalid remote CI submit stream header")
    header_size=int.from_bytes(read_exact(stream,8),"big")
    if header_size<1 or header_size>16*1024*1024:
        raise ValueError("remote CI submit header size is invalid")
    request_bytes=read_exact(stream,header_size)
    request=json.loads(request_bytes)
    expected_size=request.get("helper_size_bytes")
    source_size=int.from_bytes(read_exact(stream,8),"big")
    if (isinstance(expected_size,bool) or not isinstance(expected_size,int)
            or expected_size<1 or source_size!=expected_size):
        raise ValueError("remote CI submit helper frame size mismatch")
    source_arg=read_exact(stream,source_size)
    request64=base64.b64encode(request_bytes).decode("ascii")
else:
    request64=sys.argv[1]
    request_bytes=base64.b64decode(request64,validate=True)
    request=json.loads(request_bytes)
expected=request.get("helper_sha256")
expected_size=request.get("helper_size_bytes")
if (not isinstance(expected,str) or len(expected)!=64 or any(c not in "0123456789abcdef" for c in expected)
        or isinstance(expected_size,bool) or not isinstance(expected_size,int) or expected_size<1):
    raise ValueError("invalid remote CI helper hash")
if stream_install:
    source_arg=read_exact(sys.stdin.buffer,expected_size)
    if sys.stdin.buffer.read(1):
        raise ValueError("remote CI helper stream has trailing bytes")
if source_arg is not None and hashlib.sha256(source_arg).hexdigest()!=expected:
    raise ValueError("remote CI helper upload hash mismatch")
if stream_submit:
    bundle_size=request.get("candidate_bundle_size")
    if (request.get("operation")!="submit" or request.get("candidate_bundle_stream") is not True
            or isinstance(bundle_size,bool) or not isinstance(bundle_size,int) or bundle_size<1):
        raise ValueError("invalid remote CI streamed submit request")
    if bundle_size>512*1024*1024:
        raise ValueError("remote CI submit bundle exceeds the 512 MiB limit")
root=__import__("pathlib").Path(request["managed_root"]).expanduser()
directory=root/"helpers"/"remote-ci"
helper=directory/("goalflight_remote_ci_node-"+expected+".py")
def digest(data):
    return hashlib.sha256(data).hexdigest()
def installed_bytes():
    descriptor=-1
    try:
        flags=os.O_RDONLY|getattr(os,"O_NONBLOCK",0)|getattr(os,"O_NOFOLLOW",0)
        descriptor=os.open(str(helper),flags)
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size!=expected_size:
            return None
        with os.fdopen(descriptor,"rb") as stream:
            descriptor=-1
            data=stream.read(expected_size+1)
        return data if len(data)==expected_size else None
    except OSError:
        return None
    finally:
        if descriptor>=0:
            os.close(descriptor)
program=installed_bytes()
if program is None or digest(program)!=expected:
    if source_arg is None:
        print(json.dumps({"bootstrap":"install-required","expected_sha256":expected,
                          "observed_sha256":digest(program) if program is not None else None}))
        sys.exit(73)
    source=source_arg
    if digest(source)!=expected:
        raise ValueError("remote CI helper upload hash mismatch")
    validation={"__name__":"_goalflight_remote_ci_install_check","__file__":str(helper)}
    exec(compile(source,str(helper),"exec"),validation)
    validation["pin_managed_root"](root)
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(directory,0o700)
    fd,temp_name=tempfile.mkstemp(prefix="."+helper.name+".",suffix=".tmp",dir=str(directory))
    try:
        with os.fdopen(fd,"wb") as output:
            output.write(source)
            output.flush()
            os.fchmod(output.fileno(),0o600)
            os.fsync(output.fileno())
        os.replace(temp_name,helper)
        dir_fd=os.open(str(directory),os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    program=installed_bytes()
    if program is None or digest(program)!=expected:
        raise RuntimeError("remote CI helper install could not be verified")
sys.argv=[str(helper),request64]+(["--stream-submit"] if stream_submit else [])
exec(compile(program,str(helper),"exec"),{"__name__":"__main__","__file__":str(helper)})
'''


class _SubmitBodyStream:
    """Read a small framed prefix followed by a bundle without loading it whole."""

    def __init__(self, prefix: bytes, bundle: BinaryIO) -> None:
        self._prefix = io.BytesIO(prefix)
        self._bundle = bundle

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            raise ValueError("submit stream requires bounded reads")
        prefix = self._prefix.read(size)
        if len(prefix) == size:
            return prefix
        return prefix + self._bundle.read(size - len(prefix))

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROFILE_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_JSON_LINE_RE = re.compile(r"^\s*(\{.*\})\s*$")


class RemoteCIError(RuntimeError):
    """A configuration, admission, transport, or receipt contract failure."""


class RemoteCIBundleTooLargeError(RemoteCIError):
    """A candidate bundle exceeded the streamed submit size limit."""


class RemoteTransportTimeoutError(RemoteCIError):
    """A remote operation did not return before its transport deadline."""


class RemoteTransportError(RemoteCIError):
    """The remote transport refused a command before returning a result."""


class RemoteSubmitRejectedError(RemoteCIError):
    """The node helper rejected submit before publishing a resident ticket."""


class AmbiguousRemoteError(RemoteCIError):
    """A remote command has no clean, verified exit status."""


class NoHealthyBox(RemoteCIError):
    """No configured Studio is eligible for remote placement."""


class HealthProbeUnknown(RemoteCIError):
    """Placement could not be measured because one or more health probes failed."""


class ConfigError(RemoteCIError):
    """The project configuration is invalid."""


class ReceiptError(RemoteCIError):
    """A remote result does not contain a trustworthy receipt."""


class AdmissionError(RemoteCIError):
    """A box cannot be admitted safely."""


class ProjectIsolationError(RemoteCIError):
    """A spool request belongs to a different project daemon."""

    def __init__(self, message: str, *, fallback_reason: str = "project-mismatch") -> None:
        super().__init__(message)
        self.fallback_reason = fallback_reason


class ProfileIneligible(RemoteCIError):
    """A request cannot be applied with the configured project profile."""

    def __init__(self, message: str, *, fallback_reason: str = "profile-ineligible") -> None:
        super().__init__(message)
        self.fallback_reason = fallback_reason


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


def _local_env_map(value: Any) -> dict[str, str | None]:
    raw = _mapping(value, "local_env")
    inherited: dict[str, None] = {}
    values: dict[str, Any] = {}
    for key, child in raw.items():
        if child is None:
            if not isinstance(key, str):
                raise ConfigError("local_env must contain only string keys")
            inherited[key] = None
        else:
            values[key] = child
    return {**_string_map(values, "local_env"), **inherited}


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
    raw = _string(value, label)
    path = Path(os.path.normpath(raw))
    if not path.is_absolute():
        raise ConfigError(f"{label} must be absolute")
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


def _is_fully_pinned_requirement(value: str) -> bool:
    return re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?=="
        r"[A-Za-z0-9][A-Za-z0-9.!+_-]*",
        value,
    ) is not None


def _validate_profile_requirement_file(config_path: Path, relative: str) -> None:
    root = config_path.parent.parent if config_path.parent.name == ".goal-flight" else config_path.parent
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ConfigError("profile repository root is unavailable") from exc
    pending = [Path(relative)]
    seen: set[Path] = set()
    include_pattern = re.compile(
        r"^(?:-r|--requirement|-c|--constraint)(?:\s*=\s*|\s+)?(\S+)$"
    )
    while pending:
        selected = pending.pop()
        try:
            resolved = (root / selected).resolve(strict=True)
            if root not in resolved.parents or not resolved.is_file():
                raise ConfigError("profile requirements file must stay inside the repository")
            if resolved in seen:
                continue
            seen.add(resolved)
            lines = resolved.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise ConfigError(f"profile requirements file is unavailable: {selected}") from exc
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            include = include_pattern.fullmatch(line)
            if include:
                included = (resolved.parent / include.group(1)).resolve(strict=False)
                try:
                    pending.append(included.relative_to(root))
                except ValueError as exc:
                    raise ConfigError(
                        "profile requirements include must stay inside the repository"
                    ) from exc
            elif not _is_fully_pinned_requirement(line):
                raise ConfigError(
                    f"profile requirements must use exact == pins: {line!r}"
                )


def _expand_profile_value(value: str, env: Mapping[str, str], label: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise ConfigError(f"{label} references missing box environment variable {name}")
        return env[name]

    return _PROFILE_ENV_RE.sub(replace, value)


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds")


def _owner_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class BoxConfig:
    name: str
    host: str
    expected_hostname: str
    python_interpreter: str
    p_cores: int
    token_pool_size: int
    max_concurrent_seats: int
    remote_exec: tuple[str, ...]
    env: dict[str, str]
    # Every run/export path is minted below this node-local canonical root.
    managed_run_directory: Path = Path("~/.goal-flight/remote-ci-managed")
    required_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoxPlacement:
    box_name: str
    measured_hostname: str
    peer_address: str


@dataclass(frozen=True)
class AdmissionConfig:
    queue_wait_seconds: float
    max_wait_seconds: float


@dataclass(frozen=True)
class RunnerConfig:
    command: tuple[str, ...]
    test_command: tuple[str, ...]
    env: dict[str, str]
    timeout_seconds: float
    self_cap: int
    pytest_workers_cap: int | None
    self_cap_env: str
    base_collection_option: str
    heavy_command_ids: frozenset[str]
    command_overrides: dict[str, "CommandOverride"]


@dataclass(frozen=True)
class CommandOverride:
    env_set: dict[str, str]
    env_unset: tuple[str, ...]


@dataclass(frozen=True)
class SelectionRules:
    path_prefixes: tuple[str, ...]
    allowed_options: frozenset[str]
    value_options: frozenset[str]
    allowed_option_prefixes: tuple[str, ...]
    require_selector: bool
    max_targeted_files: int


@dataclass(frozen=True)
class ConfigIdentity:
    sha256: str
    mtime_ns: int
    size_bytes: int


@dataclass(frozen=True)
class DaemonConfig:
    path: Path
    queue_dir: Path
    state_dir: Path
    result_dir: Path
    lock_file: Path
    pid_file: Path | None
    poll_seconds: float
    result_tail_kib: int
    boxes: dict[str, BoxConfig]
    admission: AdmissionConfig
    admission_mode: str
    runner: RunnerConfig
    selection: SelectionRules
    # Stable repository id. Slots and result lines are per repo; omitted means
    # "default" so an existing config keeps its current directory.
    repo: str = "default"
    fallback: str = "local"
    repo_url: str | None = None
    environment: dict[str, Any] = field(default_factory=lambda: {
        "requirements_file": "requirements-test.txt",
    })
    checkout_basename: str = ""
    receipt_probes: dict[str, dict[str, Any]] = field(default_factory=dict)
    base_overlays: tuple[dict[str, str], ...] = ()
    never_route: tuple[str, ...] = ()
    profile_enabled: bool = False
    local_env: dict[str, str | None] = field(default_factory=dict)
    config_identity: ConfigIdentity | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, path: Path) -> "DaemonConfig":
        pending = [raw]
        while pending:
            value = pending.pop()
            if isinstance(value, Mapping):
                for key, child in value.items():
                    normalized = "".join(
                        character for character in str(key).casefold()
                        if character.isalnum()
                    )
                    if "retention" in normalized or "retainedoutput" in normalized:
                        raise ConfigError(
                            "output retention is unsupported"
                        )
                    pending.append(child)
            elif isinstance(value, list):
                pending.extend(value)

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
        result_tail_kib = _positive_int(
            daemon.get("result_tail_kib", DEFAULT_RESULT_TAIL_KIB),
            "daemon.result_tail_kib",
        )
        if result_tail_kib > MAX_RESULT_TAIL_KIB:
            raise ConfigError(
                f"daemon.result_tail_kib must be at most {MAX_RESULT_TAIL_KIB} KiB"
            )

        raw_boxes = _mapping(required("boxes"), "boxes")
        if not raw_boxes:
            raise ConfigError("boxes must not be empty")
        boxes: dict[str, BoxConfig] = {}
        for raw_name, raw_box in raw_boxes.items():
            name = _safe_id(raw_name, "box name")
            box = _mapping(raw_box, f"boxes.{name}")
            removed = {"token_key", "load_command"} & set(box)
            if removed:
                raise ConfigError(
                    f"obsolete v2 box fields: {', '.join(sorted(removed))}"
                )
            host = _string(box.get("host"), f"boxes.{name}.host")
            python_interpreter = _string(
                box.get("python_interpreter", "/opt/homebrew/bin/python3"),
                f"boxes.{name}.python_interpreter",
            )
            if not Path(python_interpreter).is_absolute():
                raise ConfigError(f"boxes.{name}.python_interpreter must be absolute")
            expected_hostname = _string(
                box.get("expected_hostname"), f"boxes.{name}.expected_hostname"
            )
            remote_exec = _command(box.get("remote_exec"), f"boxes.{name}.remote_exec")
            if "{script}" not in remote_exec:
                raise ConfigError("remote_exec must include a separate {script} argument")
            box_env = _string_map(box.get("env", {}), f"boxes.{name}.env")
            required_paths = _strings(
                box.get("required_paths", []), f"boxes.{name}.required_paths",
            )
            for required_path in required_paths:
                expanded = _expand_profile_value(
                    required_path, box_env, f"boxes.{name}.required_paths",
                )
                if not Path(expanded).is_absolute():
                    raise ConfigError(
                        f"boxes.{name}.required_paths entries must resolve to absolute paths"
                    )
            token_pool_size = _positive_int(
                box.get("token_pool_size"), f"boxes.{name}.token_pool_size"
            )
            max_concurrent_seats = _nonnegative_int(
                box.get("max_concurrent_seats", token_pool_size),
                f"boxes.{name}.max_concurrent_seats",
            )
            if max_concurrent_seats > token_pool_size:
                raise ConfigError(
                    f"boxes.{name}.max_concurrent_seats cannot exceed token_pool_size"
                )
            boxes[name] = BoxConfig(
                name=name,
                host=host,
                expected_hostname=expected_hostname,
                python_interpreter=python_interpreter,
                p_cores=_positive_int(box.get("p_cores"), f"boxes.{name}.p_cores"),
                token_pool_size=token_pool_size,
                max_concurrent_seats=max_concurrent_seats,
                remote_exec=remote_exec,
                env=box_env,
                managed_run_directory=(
                    _managed_path(
                        box["managed_run_directory"],
                        f"boxes.{name}.managed_run_directory",
                    ) if "managed_run_directory" in box else
                    Path("~/.goal-flight/remote-ci-managed")
                ),
                required_paths=required_paths,
            )

        raw_admission = required("admission")
        if isinstance(raw_admission, str):
            if raw_admission != "box":
                raise ConfigError('admission string must be "box"')
            admission_mode = "box"
            admission = {}
        else:
            admission_mode = "blocking"
            admission = _mapping(raw_admission, "admission")
        if any(key in admission for key in ("token_directory", "live_cap_file")):
            raise ConfigError("v2 admission state and caps belong under the node managed root")
        admission_config = AdmissionConfig(
            queue_wait_seconds=_positive_number(
                admission.get("queue_wait_seconds", 300),
                "admission.queue_wait_seconds",
            ),
            max_wait_seconds=_positive_number(
                admission.get("max_wait_seconds", 30),
                "admission.max_wait_seconds",
            ),
        )

        runner = _mapping(required("runner"), "runner")
        obsolete = {"watch_command", "collect_command", "cancel_command", "chunk_size", "verbose_option"} & runner.keys()
        if obsolete:
            raise ConfigError(f"obsolete v2 runner fields: {', '.join(sorted(obsolete))}")
        self_cap_env = _string(runner.get("self_cap_env", "GOALFLIGHT_REMOTE_CI_SELF_CAP"), "runner.self_cap_env")
        if not _ENV_RE.fullmatch(self_cap_env):
            raise ConfigError("runner.self_cap_env must be an environment variable name")
        base_collection_option = _string(
            runner.get("base_collection_option", "--continue-on-collection-errors"),
            "runner.base_collection_option",
        )
        command_overrides: dict[str, CommandOverride] = {}
        raw_overrides = _mapping(
            runner.get("command_overrides", {}), "runner.command_overrides",
        )
        for command_id, raw_override in raw_overrides.items():
            try:
                _safe_id(command_id, "runner.command_overrides command id")
            except RemoteCIError as exc:
                raise ConfigError(str(exc)) from exc
            label = f"runner.command_overrides.{command_id}"
            override = _mapping(raw_override, label)
            unknown = override.keys() - {"env_set", "env_unset"}
            if unknown:
                raise ConfigError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")
            env_set = _string_map(override.get("env_set", {}), f"{label}.env_set")
            invalid_env_names = sorted(name for name in env_set if not _ENV_RE.fullmatch(name))
            env_unset = _strings(override.get("env_unset", []), f"{label}.env_unset")
            invalid_env_names.extend(name for name in env_unset if not _ENV_RE.fullmatch(name))
            if invalid_env_names:
                raise ConfigError(f"{label} environment names must be valid variable names")
            if len(set(env_unset)) != len(env_unset):
                raise ConfigError(f"{label}.env_unset must not contain duplicates")
            command_overrides[command_id] = CommandOverride(env_set, env_unset)

        runner_config = RunnerConfig(
            command=_command(runner.get("command"), "runner.command"),
            test_command=_command(runner.get("test_command"), "runner.test_command"),
            env=_string_map(runner.get("env", {}), "runner.env"),
            timeout_seconds=_positive_number(
                runner.get("timeout_seconds", 7200), "runner.timeout_seconds"
            ),
            self_cap=_positive_int(runner.get("self_cap"), "runner.self_cap"),
            pytest_workers_cap=(
                _positive_int(runner["pytest_workers_cap"], "runner.pytest_workers_cap")
                if "pytest_workers_cap" in runner else None
            ),
            self_cap_env=self_cap_env,
            base_collection_option=base_collection_option,
            heavy_command_ids=frozenset(
                _strings(runner.get("heavy_command_ids", []), "runner.heavy_command_ids")
            ),
            command_overrides=command_overrides,
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
        if "repo" in raw and raw.get("repo") not in (None, ""):
            repo = _safe_id(raw.get("repo"), "repo")
            if len(repo) > 64:
                raise ConfigError("repo must be at most 64 characters")
        else:
            repo = "default"
        fallback = _string(raw.get("fallback", "local"), "fallback")
        if fallback not in {"local", "queue", "fail"}:
            raise ConfigError("fallback must be local, queue, or fail")
        local_env = _local_env_map(raw.get("local_env", {}))
        if fallback == "local":
            box_env_keys = {key for box in boxes.values() for key in box.env}
            missing_local_env = sorted(box_env_keys.difference(local_env))
            if missing_local_env:
                raise ConfigError(
                    f"{PROFILE_ENV_UNAVAILABLE_REASON}: local_env is missing box environment "
                    f"keys: {', '.join(missing_local_env)}"
                )
        if "environment" in raw and "test_requirements" in raw:
            raise ConfigError("use environment or legacy test_requirements, not both")
        raw_environment = raw.get("environment")
        if raw_environment is None:
            requirements_file = _string(
                raw.get("test_requirements", "requirements-test.txt"),
                "test_requirements",
            )
            environment: dict[str, Any] = {"requirements_file": requirements_file}
        else:
            recipe = _mapping(raw_environment, "environment")
            if set(recipe) == {"requirements_file"}:
                environment = {
                    "requirements_file": _string(
                        recipe["requirements_file"], "environment.requirements_file",
                    ),
                }
            elif set(recipe) == {"requirements"}:
                requirements = _strings(
                    recipe["requirements"], "environment.requirements", nonempty=True,
                )
                if any(item.startswith("-") for item in requirements):
                    raise ConfigError("environment.requirements entries must be package requirements")
                environment = {"requirements": list(requirements)}
            elif set(recipe) == {"interpreter", "version_probe"}:
                environment = {
                    "interpreter": _string(
                        recipe["interpreter"], "environment.interpreter",
                    ),
                    "version_probe": list(_command(
                        recipe["version_probe"], "environment.version_probe",
                    )),
                }
            else:
                raise ConfigError(
                    "environment must declare one requirements_file, requirements list, "
                    "or interpreter with version_probe"
                )
        if "requirements_file" in environment:
            requirements_file = environment["requirements_file"]
            if (Path(requirements_file).is_absolute()
                    or ".." in Path(requirements_file).parts):
                raise ConfigError("environment.requirements_file must be repository-relative")
        for box in boxes.values():
            if box.max_concurrent_seats == 0:
                continue
            if "interpreter" in environment:
                interpreter = _expand_profile_value(
                    environment["interpreter"], box.env, "environment.interpreter",
                )
                if not Path(interpreter).is_absolute():
                    raise ConfigError("environment.interpreter must resolve to an absolute path")
            for value in environment.get("version_probe", []):
                _expand_profile_value(value, box.env, "environment.version_probe")

        raw_probes = _mapping(raw.get("receipt_probes", {}), "receipt_probes")
        receipt_probes: dict[str, dict[str, Any]] = {}
        for raw_name, raw_probe in raw_probes.items():
            name = _safe_id(raw_name, "receipt probe name")
            probe = _mapping(raw_probe, f"receipt_probes.{name}")
            if set(probe) - {"command", "expected"}:
                raise ConfigError(f"receipt_probes.{name} accepts only command and expected")
            command = list(_command(probe.get("command"), f"receipt_probes.{name}.command"))
            normalized: dict[str, Any] = {"command": command}
            if "expected" in probe:
                normalized["expected"] = _string(
                    probe["expected"], f"receipt_probes.{name}.expected",
                )
            receipt_probes[name] = normalized
            for box in boxes.values():
                if box.max_concurrent_seats == 0:
                    continue
                for value in command:
                    _expand_profile_value(value, box.env, f"receipt_probes.{name}.command")
                if "expected" in normalized:
                    _expand_profile_value(
                        normalized["expected"], box.env,
                        f"receipt_probes.{name}.expected",
                    )

        raw_overlays = raw.get("base_overlays", [])
        if not isinstance(raw_overlays, list):
            raise ConfigError("base_overlays must be an array")
        base_overlays: list[dict[str, str]] = []
        for index, raw_overlay in enumerate(raw_overlays):
            label = f"base_overlays[{index}]"
            overlay = _mapping(raw_overlay, label)
            if set(overlay) != {"kind", "source", "destination"}:
                raise ConfigError(
                    f"{label} accepts only kind, source, and destination"
                )
            kind = _string(overlay.get("kind"), f"{label}.kind")
            if kind not in {"copy", "symlink"}:
                raise ConfigError(f"{label}.kind must be copy or symlink")
            source = _string(overlay.get("source"), f"{label}.source")
            destination = _string(
                overlay.get("destination"), f"{label}.destination",
            )
            relative = PurePosixPath(destination)
            if (not destination or relative.is_absolute() or "\\" in destination
                    or not relative.parts
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or relative.parts[0] == ".git"):
                raise ConfigError(
                    f"{label}.destination must be a safe checkout-relative path"
                )
            normalized_overlay = {
                "kind": kind,
                "source": source,
                "destination": relative.as_posix(),
            }
            for box in boxes.values():
                if box.max_concurrent_seats == 0:
                    continue
                expanded = _expand_profile_value(
                    source, box.env, f"{label}.source",
                )
                if not Path(expanded).is_absolute():
                    raise ConfigError(f"{label}.source must resolve to an absolute path")
            base_overlays.append(normalized_overlay)

        checkout_basename = raw.get("checkout_basename", "")
        if (not isinstance(checkout_basename, str)
                or (checkout_basename and not checkout_basename.strip())):
            raise ConfigError("checkout_basename must be a string")
        if checkout_basename and (
            checkout_basename in {".", ".."}
            or "/" in checkout_basename
            or "\\" in checkout_basename
        ):
            raise ConfigError("checkout_basename must be one directory name")
        if checkout_basename == "retained":
            raise ConfigError(
                "checkout_basename 'retained' conflicts with preserved legacy seat data"
            )
        never_route = _strings(raw.get("never_route", []), "never_route")
        profile_root = path.parent.parent if path.parent.name == ".goal-flight" else path.parent
        _validate_never_route_entries(never_route, profile_root)
        repo_url_raw = raw.get("repo_url")
        repo_url = None if repo_url_raw is None else _string(repo_url_raw, "repo_url")
        profile_enabled = (
            raw_environment is not None
            or environment != {"requirements_file": "requirements-test.txt"}
            or bool(checkout_basename)
            or bool(receipt_probes)
            or bool(base_overlays)
            or bool(never_route)
            or any(box.required_paths for box in boxes.values())
            or runner_config.pytest_workers_cap is not None
        )
        if profile_enabled:
            if runner_config.pytest_workers_cap is not None:
                runner_config = replace(
                    runner_config,
                    command=_cap_profile_pytest_workers(
                        runner_config.command, runner_config.pytest_workers_cap,
                        label="runner.command", config=True,
                    ),
                    test_command=_cap_profile_pytest_workers(
                        runner_config.test_command, runner_config.pytest_workers_cap,
                        label="runner.test_command", config=True,
                    ),
                )
            _validate_profile_runner_template(
                runner_config.command, label="runner.command",
            )
            _validate_profile_runner_template(
                runner_config.test_command, label="runner.test_command",
            )
            if runner_config.base_collection_option not in _PROFILE_VALUELESS_FLAGS:
                raise ConfigError(
                    "profile runner.base_collection_option must be an allowlisted value-less flag"
                )
            if "requirements" in environment:
                for requirement in environment["requirements"]:
                    if not _is_fully_pinned_requirement(requirement):
                        raise ConfigError(
                            "profile environment.requirements entries must use exact == pins"
                        )
            elif "requirements_file" in environment:
                _validate_profile_requirement_file(path, environment["requirements_file"])
            if (any(box.required_paths for box in boxes.values())
                    and not receipt_probes):
                raise ConfigError(
                    "profile required_paths need expected receipt_probes for identity checks"
                )
            if any("expected" not in probe for probe in receipt_probes.values()):
                raise ConfigError(
                    "profile receipt_probes must declare expected output for admission pins"
                )
        return cls(
            path=path,
            queue_dir=queue_dir,
            state_dir=state_dir,
            result_dir=result_dir,
            lock_file=lock_file,
            pid_file=pid_file,
            poll_seconds=poll_seconds,
            result_tail_kib=result_tail_kib,
            boxes=boxes,
            admission=admission_config,
            admission_mode=admission_mode,
            runner=runner_config,
            selection=selection_rules,
            repo=repo,
            fallback=fallback,
            repo_url=repo_url,
            environment=environment,
            checkout_basename=checkout_basename,
            receipt_probes=receipt_probes,
            base_overlays=tuple(base_overlays),
            never_route=never_route,
            profile_enabled=profile_enabled,
            local_env=local_env,
        )


def _read_config_snapshot(path: Path) -> tuple[bytes, ConfigIdentity]:
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            contents = source.read()
            after = os.fstat(source.fileno())
        # Atomic replacement can leave the descriptor stable after the path moves.
        current_path = path.stat()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    before_state = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_state = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    path_state = (current_path.st_dev, current_path.st_ino)
    if (before_state != after_state or len(contents) != after.st_size
            or path_state != (after.st_dev, after.st_ino)):
        raise ConfigError(f"config {path} changed while it was being read")
    return contents, ConfigIdentity(
        sha256=hashlib.sha256(contents).hexdigest(),
        mtime_ns=after.st_mtime_ns,
        size_bytes=after.st_size,
    )


def _read_config_identity(path: Path) -> ConfigIdentity:
    return _read_config_snapshot(path)[1]


def load_config(path: str | os.PathLike[str]) -> DaemonConfig:
    config_path = Path(path).expanduser().resolve()
    contents, identity = _read_config_snapshot(config_path)
    try:
        raw = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError("config root must be an object")
    return replace(
        DaemonConfig.from_mapping(raw, path=config_path),
        config_identity=identity,
    )


def admission_poll_interval(queue_wait_seconds: float, poll_seconds: float) -> float:
    """How often to retry a free token.

    queue_wait_seconds is the caller's patience, not the sleep. Sleeping that
    long (and the same interval on the node) left a freed token idle for minutes.
    Poll at least once a second, sooner when either configured interval is shorter.
    """
    return min(queue_wait_seconds, poll_seconds, 1.0)


def _ssh_control_path(box_name: str, remote_exec: Sequence[str],
                      control_dir: Path, pinned_host: str | None = None) -> Path | None:
    identity = json.dumps(
        (box_name, tuple(remote_exec), pinned_host),
        separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    name = hashlib.sha256(identity).hexdigest()[:16]
    path = control_dir / name
    return path if len(os.fsencode(path)) < SSH_CONTROL_PATH_LIMIT else None


def _ssh_control_directory_problem(info: Any, expected_uid: int) -> str | None:
    if stat.S_ISLNK(info.st_mode):
        return "directory is a symlink"
    if not stat.S_ISDIR(info.st_mode):
        return "path is not a directory"
    if info.st_uid != expected_uid:
        return f"directory owner uid {info.st_uid} is not {expected_uid}"
    permissions = stat.S_IMODE(info.st_mode)
    if permissions != 0o700:
        return f"directory permissions {permissions:04o} are not private mode 0700"
    return None


class RemoteNode:
    """One transport primitive; all admission operations execute on the node."""

    def __init__(self, box: BoxConfig, admission: AdmissionConfig, executor: Any,
                 *, poll_seconds: float, result_tail_bytes: int,
                 control_dir: Path, helper_source: bytes,
                 pinned_host: str | None = None) -> None:
        self.box, self.admission, self.executor = box, admission, executor
        self.poll_seconds = poll_seconds
        self.result_tail_bytes = result_tail_bytes
        self.control_dir = control_dir
        self.helper_source = helper_source
        self.pinned_host = pinned_host
        self.pinned_host_key_alias: str | None = None

    def _ssh_effective_config(
        self, *, pinned_host: str | None = None,
    ) -> dict[str, tuple[str, ...]]:
        command = self.box.remote_exec
        try:
            host_index = command.index("{host}")
        except ValueError as exc:
            raise RemoteCIError(
                "configured SSH command cannot identify the destination"
            ) from exc
        values = {"box": self.box.name, "host": self.box.host}
        options_before_host = expand_command(command[1:host_index], values)
        options_after_host = []
        for token in command[host_index + 1:]:
            if "{script}" in token:
                break
            options_after_host.extend(expand_command((token,), values))
        probe = [
            command[0],
            *((f"-oHostName={pinned_host}",) if pinned_host is not None else ()),
            *options_before_host,
            "-G", self.box.host,
            *options_after_host,
        ]
        try:
            result = subprocess.run(
                probe, capture_output=True, text=True,
                env={**os.environ, **self.box.env}, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RemoteCIError(
                f"cannot resolve SSH host-key identity for {self.box.name}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "ssh -G failed").strip()
            raise RemoteCIError(
                f"cannot resolve SSH host-key identity config for {self.box.name}: "
                f"{detail}"
            )
        options: dict[str, list[str]] = {}
        for line in result.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                options.setdefault(parts[0].lower(), []).append(parts[1].strip())
        return {key: tuple(values) for key, values in options.items()}

    def _configured_ssh_host_key_name(
        self, unpinned: Mapping[str, tuple[str, ...]],
    ) -> str:
        lookup_name = next(iter(unpinned.get("hostkeyalias", ())), None)
        if not lookup_name or lookup_name.lower() == "none":
            hostname = next(iter(unpinned.get("hostname", ())), None)
            port = next(iter(unpinned.get("port", ())), None)
            if not hostname or hostname.lower() == "none":
                lookup_name = None
            elif not port or not port.isdecimal() or not 1 <= int(port) <= 65535:
                lookup_name = None
            elif int(port) == 22:
                lookup_name = hostname
            else:
                lookup_name = f"[{hostname}]:{port}"
        if (not lookup_name or lookup_name.lower() == "none"
                or any(character.isspace() or ord(character) < 32
                       for character in lookup_name)):
            raise RemoteCIError(
                f"ssh -G returned no usable host-key identity for {self.box.name}"
            )
        return lookup_name

    def with_pinned_host(self, host: str) -> "RemoteNode":
        try:
            address = ipaddress.ip_address(host).compressed
        except (TypeError, ValueError) as exc:
            raise RemoteCIError("measured peer address is not an IP address") from exc
        host_key_alias = None
        if Path(self.box.remote_exec[0]).name == "ssh":
            if self.pinned_host == address and self.pinned_host_key_alias:
                host_key_alias = self.pinned_host_key_alias
            else:
                unpinned = self._ssh_effective_config()
                pinned = self._ssh_effective_config(pinned_host=address)
                overridden_options = {
                    "hostname", "hostkeyalias", "controlpath", "controlmaster",
                    "controlpersist",
                }
                for key in sorted((set(unpinned) | set(pinned)) - overridden_options):
                    if unpinned.get(key) != pinned.get(key):
                        raise RemoteCIError(
                            f"pinning SSH destination changes effective option {key} "
                            f"for {self.box.name}; refusing to pin"
                        )
                host_key_alias = self._configured_ssh_host_key_name(unpinned)
        node = copy.copy(self)
        node.pinned_host = address
        node.pinned_host_key_alias = host_key_alias
        return node

    def _host_template_value(self) -> str:
        command = self.box.remote_exec
        if Path(command[0]).name == "ssh":
            return self.box.host
        if self.pinned_host is not None:
            if "{host}" not in command:
                raise RemoteCIError(
                    "configured remote command cannot honor the measured host pin"
                )
            return self.pinned_host
        return self.box.host

    def _command_template(
        self, *, disable_multiplexing: bool = False,
    ) -> tuple[str, ...]:
        command = self.box.remote_exec
        if Path(command[0]).name != "ssh":
            return command
        if self.pinned_host:
            if not self.pinned_host_key_alias:
                raise RemoteCIError("pinned SSH transport has no host-key identity")
            pin = (
                f"-oHostName={self.pinned_host}",
                f"-oHostKeyAlias={self.pinned_host_key_alias}",
            )
        else:
            pin = ()
        no_multiplexing = (
            command[0],
            *pin,
            "-oControlMaster=no",
            "-oControlPersist=no",
            "-oControlPath=none",
            *command[1:],
        )
        if disable_multiplexing:
            return no_multiplexing
        control_path = _ssh_control_path(
            self.box.name, command, self.control_dir, self.pinned_host,
        )
        if control_path is None:
            _LOG.warning(
                "SSH ControlPath exceeds %s bytes; disabling SSH multiplexing",
                SSH_CONTROL_PATH_LIMIT - 1,
            )
            return no_multiplexing
        try:
            try:
                self.control_dir.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = os.lstat(self.control_dir)
            problem = _ssh_control_directory_problem(info, os.geteuid())
            if problem:
                raise ValueError(problem)
        except OSError as exc:
            _LOG.warning(
                "cannot prepare SSH control directory %s (%s); disabling SSH multiplexing",
                self.control_dir, exc,
            )
            return no_multiplexing
        except ValueError as exc:
            _LOG.warning(
                "SSH control directory %s is unsafe (%s); disabling SSH multiplexing",
                self.control_dir, exc,
            )
            return no_multiplexing
        options = (
            "-oControlMaster=auto",
            "-oControlPersist=5m",
            f"-oControlPath={control_path}",
        )
        return (command[0], *pin, *options, *command[1:])

    @staticmethod
    def _mux_transport_failed(template: Sequence[str], result: CommandResult) -> bool:
        if ("-oControlMaster=auto" not in template
                or (result.returncode != 255 and not result.timed_out)):
            return False
        message = result.stderr.casefold()
        return any(signature in message for signature in (
            "mm_send_fd",
            "sendmsg(2): message too long",
            "mux_client_request_session",
            "send fds failed",
            "session open refused by peer",
            "control socket connect(",
        ))

    @classmethod
    def _mux_retry_allowed(
        cls, template: Sequence[str], result: CommandResult, *, idempotent: bool,
    ) -> bool:
        if not cls._mux_transport_failed(template, result):
            return False
        if idempotent:
            return True
        if result.returncode != 255 or result.timed_out:
            return False
        message = result.stderr.casefold()
        return any(signature in message for signature in (
            "mm_send_fd",
            "send fds failed",
            "message too long",
        ))

    @staticmethod
    def _mux_retry_timeout(
        timeout: float, deadline: float | None,
    ) -> float | None:
        if deadline is None:
            return timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return min(timeout, remaining)

    def _run_transport_command(
        self, script: str, *, timeout: float, deadline: float | None = None,
        input_data: bytes | None = None, idempotent: bool,
    ) -> CommandResult:
        values = {"box": self.box.name, "host": self._host_template_value()}
        env = {**os.environ, **self.box.env}
        template = self._command_template()
        argv = expand_command(template, values, script=[script])
        executor_kwargs = {} if input_data is None else {"input_data": input_data}
        result = self.executor(argv, env, timeout, **executor_kwargs)
        if not self._mux_retry_allowed(template, result, idempotent=idempotent):
            return result
        retry_timeout = self._mux_retry_timeout(timeout, deadline)
        if retry_timeout is None:
            return result
        _LOG.warning(
            "SSH mux transport failed for %s; retrying without multiplexing",
            self.box.name,
        )
        fallback_argv = expand_command(
            self._command_template(disable_multiplexing=True), values,
            script=[script],
        )
        return self.executor(
            fallback_argv, env, retry_timeout, **executor_kwargs,
        )

    def _invoke(
        self, payload: Mapping[str, Any], source: bytes | None = None, *,
        timeout: float = 30, deadline: float | None = None,
        idempotent: bool,
    ) -> CommandResult:
        encoded = base64.b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).decode("ascii")
        script = (
            shlex.quote(self.box.python_interpreter) + " -c "
            + shlex.quote(_REMOTE_NODE_LAUNCHER) + " " + shlex.quote(encoded)
        )
        if source is not None:
            script += " --install-helper-from-stdin"
        return self._run_transport_command(
            script, timeout=timeout, deadline=deadline, input_data=source,
            idempotent=idempotent,
        )

    def _invoke_streamed_submit(
        self, payload: Mapping[str, Any], bundle_path: Path, *, timeout: float,
        deadline: float | None = None,
    ) -> CommandResult:
        header = json.dumps(
            payload, separators=(",", ":"), sort_keys=True,
        ).encode()
        source = self.helper_source
        prefix = (
            b"GFRCI-SUBMIT-STREAM/1\n"
            + len(header).to_bytes(8, "big") + header
            + len(source).to_bytes(8, "big") + source
        )
        script = (
            shlex.quote(self.box.python_interpreter) + " -c "
            + shlex.quote(_REMOTE_NODE_LAUNCHER) + " --stream-submit"
        )
        argv = expand_command(
            self._command_template(), {"box": self.box.name, "host": self.box.host},
            script=[script],
        )
        env = {**os.environ, **self.box.env}
        try:
            with bundle_path.open("rb") as bundle:
                result = self.executor(
                    argv, env, timeout,
                    input_stream=_SubmitBodyStream(prefix, bundle),
                )
            template = self._command_template()
            if not self._mux_retry_allowed(template, result, idempotent=False):
                return result
            retry_timeout = self._mux_retry_timeout(timeout, deadline)
            if retry_timeout is None:
                return result
            _LOG.warning(
                "SSH mux transport failed for %s; retrying without multiplexing",
                self.box.name,
            )
            fallback_argv = expand_command(
                self._command_template(disable_multiplexing=True),
                {"box": self.box.name, "host": self._host_template_value()},
                script=[script],
            )
            with bundle_path.open("rb") as bundle:
                return self.executor(
                    fallback_argv, env, retry_timeout,
                    input_stream=_SubmitBodyStream(prefix, bundle),
                )
        except OSError as exc:
            raise RemoteCIError(f"could not stream candidate bundle: {exc}") from exc

    def call(self, operation: str, *, _transport_timeout: float | None = None,
             **values: Any) -> Any:
        bundle_path = None
        if operation == "submit":
            raw_bundle_path = values.pop("candidate_bundle_path", None)
            if raw_bundle_path is None:
                raise RemoteCIError("submit requires a local candidate_bundle_path")
            try:
                bundle_path = Path(raw_bundle_path)
                bundle_info = bundle_path.stat()
            except (OSError, TypeError, ValueError) as exc:
                raise RemoteCIError(f"cannot read candidate bundle: {exc}") from exc
            if not stat.S_ISREG(bundle_info.st_mode):
                raise RemoteCIError("candidate bundle must be a regular file")
            if bundle_info.st_size > MAX_SUBMIT_BUNDLE_BYTES:
                raise RemoteCIBundleTooLargeError(
                    f"candidate bundle exceeds the {MAX_SUBMIT_BUNDLE_BYTES // (1024 * 1024)} MiB limit"
                )
            if bundle_info.st_size < 1:
                raise RemoteCIError("candidate bundle is empty")
            values["candidate_bundle_stream"] = True
            values["candidate_bundle_size"] = bundle_info.st_size
            digest = hashlib.sha256()
            try:
                with bundle_path.open("rb") as bundle:
                    while chunk := bundle.read(64 * 1024):
                        digest.update(chunk)
            except OSError as exc:
                raise RemoteCIError(f"cannot hash candidate bundle: {exc}") from exc
            values["candidate_bundle_sha256"] = digest.hexdigest()
        if _transport_timeout is None:
            _transport_timeout = (
                BUNDLE_TRANSFER_TIMEOUT_SECONDS + REMOTE_TRANSPORT_ALLOWANCE_SECONDS
                if operation == "submit" else 30
            )
        transport_deadline = time.monotonic() + max(0.0, _transport_timeout)

        def remaining_timeout() -> float:
            remaining = transport_deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteTransportTimeoutError(
                    f"remote {operation} exceeded its transport deadline"
                )
            return remaining

        source = self.helper_source
        idempotent = operation in {"health", "list", "status", "watch", "fetch"}
        payload = {
            "operation": operation, "box": self.box.name,
            "managed_root": str(self.box.managed_run_directory),
            "p_cores": self.box.p_cores, "token_pool_size": self.box.token_pool_size,
            "poll_seconds": admission_poll_interval(
                self.admission.queue_wait_seconds, self.poll_seconds
            ),
            "controller_poll_seconds": self.poll_seconds,
            "queue_wait_seconds": self.admission.max_wait_seconds,
            "result_tail_bytes": self.result_tail_bytes,
            "python_interpreter": self.box.python_interpreter,
            "node_path": self.box.env.get("PATH"),
            **values,
        }
        payload["protocol_version"] = NODE_PROTOCOL_VERSION
        payload["helper_sha256"] = hashlib.sha256(source).hexdigest()
        payload["helper_size_bytes"] = len(source)
        if operation == "submit":
            result = self._invoke_streamed_submit(
                payload, bundle_path, timeout=remaining_timeout(),
                deadline=transport_deadline,
            )
        else:
            result = self._invoke(
                payload, timeout=remaining_timeout(), deadline=transport_deadline,
                idempotent=idempotent,
            )
        parsed: Any = None
        if not result.timed_out and result.stdout.strip():
            try:
                parsed = json.loads(result.stdout)
            except (ValueError, TypeError):
                parsed = None
        if (result.returncode == NODE_HELPER_INSTALL_REQUIRED
                and isinstance(parsed, dict)
                and parsed.get("bootstrap") == "install-required"):
            result = self._invoke(
                payload, source, timeout=remaining_timeout(),
                deadline=transport_deadline, idempotent=idempotent,
            )
            parsed = None
            if not result.timed_out and result.stdout.strip():
                try:
                    parsed = json.loads(result.stdout)
                except (ValueError, TypeError):
                    parsed = None
            if (result.returncode == NODE_HELPER_INSTALL_REQUIRED
                    and isinstance(parsed, dict)
                    and parsed.get("bootstrap") == "install-required"):
                observed = parsed.get("observed_sha256")
                raise RemoteCIError(
                    "remote CI helper install could not be verified "
                    f"(expected {payload['helper_sha256']}, observed {observed!r})"
                )
        if isinstance(parsed, dict) and "protocol_version" in parsed:
            version = parsed["protocol_version"]
            if version != NODE_PROTOCOL_VERSION:
                raise RemoteCIError(
                    f"remote CI protocol mismatch: expected {NODE_PROTOCOL_VERSION}, "
                    f"received {version!r}; refusing legacy response"
                )
            value = parsed.get("result")
            if isinstance(value, (dict, list)):
                return value
            raise RemoteCIError("remote CI protocol v5 response has no object or list result")
        if isinstance(parsed, (dict, list)):
            raise RemoteCIError(
                f"remote CI protocol mismatch: expected {NODE_PROTOCOL_VERSION}, "
                "received an unversioned legacy response; refusing legacy full-body behavior"
            )
        if result.timed_out:
            raise RemoteTransportTimeoutError(
                f"node {operation} failed: {result.stderr.strip()}"
            )
        if result.returncode != 0:
            if operation == "submit" and result.returncode == 2:
                raise RemoteSubmitRejectedError(
                    f"node submit was rejected: {result.stderr.strip()}"
                )
            if result.returncode == 255:
                raise RemoteTransportError(
                    f"node {operation} failed: {result.stderr.strip()}"
                )
            raise RemoteCIError(f"node {operation} failed: {result.stderr.strip()}")
        raise RemoteCIError(f"node {operation} returned invalid JSON")

    def transfer_bundle(self, bundle_path: Path, run_dir: str) -> str:
        """Stream a local git bundle through the configured SSH transport."""
        remote_path = str(Path(run_dir) / "source.bundle")
        script = "cat > " + shlex.quote(remote_path)
        try:
            bundle = bundle_path.read_bytes()
            deadline = time.monotonic() + BUNDLE_TRANSFER_TIMEOUT_SECONDS
            result = self._run_transport_command(
                script, timeout=BUNDLE_TRANSFER_TIMEOUT_SECONDS,
                deadline=deadline, input_data=bundle, idempotent=False,
            )
        except (OSError, TypeError, RemoteCIError) as exc:
            raise RemoteCIError(f"could not ship source bundle: {exc}") from exc
        if result.returncode != 0 or result.timed_out:
            raise RemoteCIError(f"could not ship source bundle: {result.stderr.strip()}")
        return remote_path


def list_remote_leases(config: DaemonConfig, *, executor: Any = None) -> list[dict[str, Any]]:
    runner = RemoteRunner(config, executor=executor)
    # Listing discovers persisted routes; lease-specific follow-up uses each
    # record's destination_address through _node_for_lease.
    return [dict(record, box=name)
            for name, node in runner.nodes.items()
            for record in node.call("list")]


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


def _command_test_file_args(
    command: Sequence[str], rules: SelectionRules, *, repo_root: str, cwd_relative: str,
) -> tuple[str, ...]:
    files: list[str] = []
    waiting_for_value = False
    for raw in command:
        if waiting_for_value:
            waiting_for_value = False
            continue
        if raw.startswith("-"):
            if raw in rules.value_options:
                waiting_for_value = True
            continue
        path = raw.split("::", 1)[0].replace("\\", "/")
        if posixpath.isabs(path):
            path = posixpath.relpath(path, repo_root)
        else:
            path = posixpath.normpath(posixpath.join(cwd_relative, path))
        if path == ".." or path.startswith("../"):
            continue
        if any(path == prefix.rstrip("/") or path.startswith(prefix)
               for prefix in rules.path_prefixes):
            files.append(path)
    return tuple(dict.fromkeys(files))


def _is_python_interpreter(value: str) -> bool:
    return re.fullmatch(r"python(?:3(?:\.\d+)?)?", posixpath.basename(value)) is not None


def _profile_root(config: DaemonConfig) -> Path:
    return config.path.parent.parent if config.path.parent.name == ".goal-flight" else config.path.parent


_PROFILE_VALUELESS_FLAGS = frozenset({
    "-q", "-v", "-x", "-rs", "-rfE", "--continue-on-collection-errors",
})
_PROFILE_GLOB_CHARS = frozenset("*?[]")


def _profile_grammar_error(label: str, message: str, *, config: bool) -> None:
    detail = f"{label} {message}"
    if config:
        raise ConfigError(detail)
    raise ProfileIneligible(
        detail, fallback_reason="command-outside-profile-grammar",
    )


def _profile_file_token(value: str, *, label: str, config: bool) -> None:
    if (not value or "\x00" in value or value in {".", ".."} or Path(value).is_absolute()
            or posixpath.isabs(value) or "\\" in value or "::" in value
            or any(char in _PROFILE_GLOB_CHARS for char in value)
            or any(part == ".." for part in PurePosixPath(value).parts)
            or not PurePosixPath(value).parts):
        _profile_grammar_error(
            label, f"contains a file outside the profile grammar: {value!r}",
            config=config,
        )


def _profile_parse_arguments(
    arguments: Sequence[str], *, label: str, allow_files: bool,
    require_files: bool, config: bool = False,
) -> tuple[str, ...]:
    files: list[str] = []
    worker_option_seen = False
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value.startswith("@"):
            _profile_grammar_error(
                label, f"contains an argument-file token: {value!r}", config=config,
            )
        if not files and value in _PROFILE_VALUELESS_FLAGS:
            index += 1
            continue
        if not files and value == "-p":
            if index + 1 >= len(arguments) or arguments[index + 1] != "no:cacheprovider":
                _profile_grammar_error(label, "has an unsupported -p option", config=config)
            index += 2
            continue
        if not files and value == "-n":
            if worker_option_seen:
                _profile_grammar_error(label, "may specify -n only once", config=config)
            if (index + 1 >= len(arguments)
                    or re.fullmatch(r"[+-]?\d+", arguments[index + 1]) is None):
                _profile_grammar_error(label, "requires -n followed by an integer", config=config)
            if int(arguments[index + 1]) < 0:
                _profile_grammar_error(label, "requires -n to be non-negative", config=config)
            worker_option_seen = True
            index += 2
            continue
        if value.startswith("-"):
            _profile_grammar_error(
                label, f"contains an unsupported flag or attached value: {value!r}",
                config=config,
            )
        if not allow_files:
            _profile_grammar_error(
                label, f"must not contain test paths: {value!r}", config=config,
            )
        _profile_file_token(value, label=label, config=config)
        files.append(value)
        index += 1
    if require_files and not files:
        _profile_grammar_error(label, "must name an explicit test file", config=config)
    return tuple(dict.fromkeys(files))


def _profile_python_prefix(command: Sequence[str]) -> bool:
    return (len(command) >= 3 and _is_python_interpreter(command[0])
            and tuple(command[1:3]) == ("-m", "pytest"))


def _cap_profile_worker_arguments(
    arguments: Sequence[str], cap: int, *, label: str, config: bool,
) -> tuple[str, ...]:
    normalized = list(arguments)
    found = False
    index = 0
    while index < len(normalized):
        value = normalized[index]
        if value == "-n":
            if found:
                _profile_grammar_error(label, "may specify -n only once", config=config)
            if index + 1 >= len(normalized):
                _profile_grammar_error(label, "requires -n followed by an integer", config=config)
            requested = normalized[index + 1]
            if requested == "auto":
                normalized[index + 1] = str(cap)
            elif re.fullmatch(r"[+-]?\d+", requested):
                workers = int(requested)
                if workers < 0:
                    _profile_grammar_error(label, "requires -n to be non-negative", config=config)
                normalized[index + 1] = "0" if workers == 0 else str(min(workers, cap))
            else:
                _profile_grammar_error(label, "requires -n followed by an integer or auto", config=config)
            found = True
            index += 2
        elif value == "-p":
            index += 2
        elif value in _PROFILE_VALUELESS_FLAGS:
            index += 1
        elif value.startswith("-"):
            index += 1
        else:
            break
    if not found:
        normalized[index:index] = ["-n", str(cap)]
    return tuple(normalized)


def _cap_profile_pytest_workers(
    command: Sequence[str], cap: int, *, label: str, config: bool,
) -> tuple[str, ...]:
    if not _profile_python_prefix(command):
        return tuple(command)
    return (
        *command[:3],
        *_cap_profile_worker_arguments(command[3:], cap, label=label, config=config),
    )


def _validate_profile_runner_template(command: Sequence[str], *, label: str) -> None:
    if not _profile_python_prefix(command):
        raise ConfigError(
            f"{label} must be a Python interpreter followed by -m pytest"
        )
    _profile_parse_arguments(
        command[3:], label=label, allow_files=False,
        require_files=False, config=True,
    )


def _profile_command_files(
    command: Sequence[str], *, label: str, generated_node_id: str | None = None,
) -> tuple[str, ...]:
    if not _profile_python_prefix(command):
        _profile_grammar_error(
            label, "must be a Python interpreter followed by -m pytest", config=False,
        )
    arguments = list(command[3:])
    if generated_node_id is not None:
        if not arguments or arguments[-1] != generated_node_id:
            _profile_grammar_error(
                label, "does not end with its generated node selector", config=False,
            )
        file_path, separator, test_selector = generated_node_id.partition("::")
        if not separator or not test_selector:
            _profile_grammar_error(
                label, "has an invalid generated node selector", config=False,
            )
        _profile_file_token(file_path, label=label, config=False)
        arguments[-1] = file_path
    return _profile_parse_arguments(
        arguments, label=label, allow_files=True,
        require_files=True,
    )


def _repo_relative_path(path: Path, root: Path, *, label: str, config: bool) -> str:
    try:
        selected = path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        _profile_grammar_error(label, "is missing or cannot be resolved", config=config)
        raise AssertionError from exc
    relative_parts: list[str] = []
    cursor = selected
    while True:
        try:
            if os.path.samefile(cursor, resolved_root):
                break
        except OSError as exc:
            _profile_grammar_error(label, "is missing or cannot be resolved", config=config)
            raise AssertionError from exc
        parent = cursor.parent
        if parent == cursor:
            _profile_grammar_error(
                label, "must resolve inside the repository", config=config,
            )
        relative_parts.append(cursor.name)
        cursor = parent
    if not relative_parts:
        _profile_grammar_error(label, "must identify a path inside the repository", config=config)
    return PurePosixPath(*reversed(relative_parts)).as_posix()


def _validate_never_route_entries(entries: Sequence[str], root: Path) -> None:
    for entry in entries:
        if (not entry or Path(entry).is_absolute() or posixpath.isabs(entry)
                or "\\" in entry or "::" in entry
                or any(char in _PROFILE_GLOB_CHARS for char in entry)
                or any(part == ".." for part in PurePosixPath(entry).parts)
                or not PurePosixPath(entry).parts):
            raise ConfigError("never_route entries must be explicit repository-relative paths")
        resolved = (root / entry).resolve(strict=False)
        _repo_relative_path(resolved, root, label="never_route entry", config=True)
        if not resolved.is_file() and not resolved.is_dir():
            raise ConfigError("never_route entries must be files or directories")


def _profile_file_identity(
    value: str, *, repo_root: Path, cwd_relative: str, label: str,
    snapshot_files: frozenset[str] | None = None,
) -> str:
    _profile_file_token(value, label=label, config=False)
    if snapshot_files is not None:
        relative_path = PurePosixPath(cwd_relative, value).as_posix()
        if relative_path not in snapshot_files:
            _profile_grammar_error(
                label, f"is missing or cannot be resolved: {value!r}", config=False,
            )
        return relative_path
    candidate = repo_root / cwd_relative / value
    try:
        resolved_file = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        _profile_grammar_error(label, f"is missing or cannot be resolved: {value!r}", config=False)
        raise AssertionError from exc
    if not resolved_file.is_file():
        _profile_grammar_error(
            label, f"must be an existing regular file: {value!r}", config=False,
        )
    return _repo_relative_path(resolved_file, repo_root, label=label, config=False)


def _profile_request_files(
    request: Mapping[str, Any], config: DaemonConfig, *,
    snapshot_files: frozenset[str] | None = None,
) -> tuple[str, ...]:
    if request.get("command") is not None:
        return _profile_command_files(request["command"], label="profiled command")
    selected = _profile_parse_arguments(
        request.get("selection", ()), label="profile selection",
        allow_files=True, require_files=True,
    )
    root = _profile_root(config)
    checked = [
        _profile_file_identity(
            path, repo_root=root,
            cwd_relative=str(request.get("cwd_relative", "")),
            label="profile test selector",
            snapshot_files=snapshot_files,
        )
        for path in selected
    ]
    for path in request.get("test_files", ()):
        checked.append(_profile_file_identity(
            path, repo_root=root,
            cwd_relative=str(request.get("cwd_relative", "")),
            label="profile test_files entry",
            snapshot_files=snapshot_files,
        ))
    return tuple(dict.fromkeys(checked))


def _never_route_entry(
    path: str, entries: Sequence[str], *, repo_root: str,
) -> str | None:
    root = Path(repo_root).resolve(strict=True)
    selected = (root / path).resolve(strict=True)
    for entry in entries:
        blocked = (root / entry).resolve(strict=True)
        try:
            if os.path.samefile(selected, blocked):
                return entry
        except OSError as exc:
            raise ProfileIneligible(
                f"could not verify never_route entry {entry!r}",
                fallback_reason="never-route-check-failed",
            ) from exc
        if blocked.is_dir():
            for parent in selected.parents:
                try:
                    if os.path.samefile(parent, blocked):
                        return entry
                except OSError as exc:
                    raise ProfileIneligible(
                        f"could not verify never_route entry {entry!r}",
                        fallback_reason="never-route-check-failed",
                    ) from exc
    return None


def validate_request(payload: Mapping[str, Any], config: DaemonConfig) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RemoteCIError("request must be an object")
    if payload.get("schema") != REQUEST_SCHEMA:
        raise RemoteCIError(f"request schema must be {REQUEST_SCHEMA}")
    request = dict(payload)
    has_command_tree_identity = (
        request.get("command") is not None
        and all(name in request for name in ("head_sha", "snapshot_sha", "dirty"))
    )
    request_id = _safe_id(request.get("request_id"), "request_id")
    if "project_id" not in request:
        raise ProjectIsolationError(
            "request must identify its project", fallback_reason="missing-project-id",
        )
    project_id = request["project_id"]
    if not isinstance(project_id, str) or not _ID_RE.fullmatch(project_id):
        raise ProjectIsolationError(
            "request must identify its project", fallback_reason="invalid-project-id",
        )
    if project_id != config.repo:
        raise ProjectIsolationError(
            f"request project {project_id!r} does not match daemon project {config.repo!r}"
        )
    request["project_id"] = project_id
    kind = _string(request.get("kind"), "kind")
    if kind not in {"targeted", "gate"}:
        raise RemoteCIError("kind must be targeted or gate")
    command = request.get("command")
    test_file_args: tuple[str, ...] = ()
    if command is None:
        for field in ("tip_sha", "candidate_sha"):
            value = request.get(field)
            if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
                raise RemoteCIError(f"{field} must be a hexadecimal commit SHA")
    if request.get("base_sha") is not None:
        value = request.get("base_sha")
        if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
            raise RemoteCIError("base_sha must be a hexadecimal commit SHA")
        if command is not None and kind != "gate":
            raise RemoteCIError("base_sha command requests must use kind gate")
        request["base_sha"] = value.lower()
        if command is not None:
            recheck = request.get("recheck", 0)
            if not isinstance(recheck, int) or isinstance(recheck, bool) or recheck < 0:
                raise RemoteCIError("recheck must be a non-negative integer")
            if recheck > 5:
                raise RemoteCIError("recheck must be at most 5")
    for field in ("head_sha", "snapshot_sha"):
        value = request.get(field, request.get("candidate_sha"))
        if value is None and command is not None:
            continue
        if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
            raise RemoteCIError(f"{field} must be a hexadecimal commit SHA")
        request[field] = value.lower()
    dirty = request.get("dirty", False)
    if not isinstance(dirty, bool):
        raise RemoteCIError("dirty must be a boolean")
    request["dirty"] = dirty
    cwd_relative = request.get("cwd_relative", "")
    if (not isinstance(cwd_relative, str) or Path(cwd_relative).is_absolute()
            or ".." in Path(cwd_relative).parts):
        raise RemoteCIError("cwd_relative must stay inside the repository")
    request["cwd_relative"] = cwd_relative
    caller_env = request.get("caller_env", {})
    if not isinstance(caller_env, Mapping):
        raise RemoteCIError("caller_env must be an object")
    request["caller_env"] = _string_map(caller_env, "caller_env")
    if set(request["caller_env"]) - REMOTE_ENV_ALLOWLIST:
        raise RemoteCIError("caller_env contains a variable outside the remote allowlist")
    receipt_metadata = request.get("receipt_metadata", {})
    if not isinstance(receipt_metadata, Mapping):
        raise RemoteCIError("receipt_metadata must be an object")
    if set(receipt_metadata) - RECEIPT_METADATA_FIELDS:
        raise RemoteCIError("receipt_metadata contains an unsupported field")
    request["receipt_metadata"] = {
        key: value for key, value in _string_map(receipt_metadata, "receipt_metadata").items()
        if value
    }
    bundle_path = request.get("bundle_path")
    if bundle_path is not None:
        expected_bundle = config.queue_dir / f"{request_id}.bundle"
        if Path(str(bundle_path)).resolve(strict=False) != expected_bundle.resolve(strict=False):
            raise RemoteCIError("bundle_path must be this request's spool bundle")
    if command is not None:
        if kind != "gate":
            raise RemoteCIError("command requests must use kind gate")
        repo_root = _string(request.get("repo_root"), "repo_root")
        if not Path(repo_root).is_absolute():
            raise RemoteCIError("repo_root must be absolute")
        request["repo_root"] = str(Path(repo_root).resolve(strict=False))
        if (not isinstance(command, list) or not command
                or any(not isinstance(item, str) or not item for item in command)):
            raise RemoteCIError("command must be a non-empty argv list")
        request["command_id"] = _safe_id(request.get("command_id"), "command_id")
        if config.profile_enabled:
            if config.runner.pytest_workers_cap is not None:
                command = list(_cap_profile_pytest_workers(
                    command, config.runner.pytest_workers_cap,
                    label="profiled command", config=False,
                ))
                request["command"] = command
            selected = _profile_command_files(
                command, label="profiled command",
            )
            test_file_args = tuple(
                _profile_file_identity(
                    path, repo_root=Path(request["repo_root"]),
                    cwd_relative=cwd_relative, label="profile test selector",
                )
                for path in selected
            )
        else:
            test_file_args = _command_test_file_args(
                command, config.selection, repo_root=request["repo_root"],
                cwd_relative=cwd_relative,
            )
            if config.never_route and not test_file_args:
                raise RemoteCIError(
                    "custom command must select test paths when never_route is configured"
                )
        test_files = request.get("test_files", [])
        selection = request.get("selection", [])
        if test_files != [] or selection != []:
            raise RemoteCIError("command requests cannot include test selection fields")
        if request.get("base_sha") is not None:
            request["test_files"] = list(test_file_args)
            request["selection"] = []
        if request.get("base_sha") is not None:
            request["tip_sha"] = request["base_sha"]
        else:
            request.pop("tip_sha", None)
        request.pop("candidate_sha", None)
        if not has_command_tree_identity:
            request.pop("head_sha", None)
            request.pop("snapshot_sha", None)
            request.pop("dirty", None)
    else:
        test_files = request.get("test_files")
        if config.profile_enabled:
            if (not isinstance(test_files, list) or not test_files
                    or any(not isinstance(item, str) or not item for item in test_files)):
                _profile_grammar_error(
                    "profile test_files", "must name explicit test files", config=False,
                )
        else:
            if (not isinstance(test_files, list) or not test_files
                    or any(not isinstance(item, str) for item in test_files)):
                raise RemoteCIError("test_files must be a non-empty list of strings")
            for path in test_files:
                normalized = path.replace("\\", "/")
                if normalized.startswith("/") or normalized == ".." or normalized.startswith("../") or "/../" in f"/{normalized}/":
                    raise RemoteCIError(f"test file escapes configured roots: {path!r}")
                if not any(normalized.startswith(prefix) for prefix in config.selection.path_prefixes):
                    raise RemoteCIError(f"test file is outside configured roots: {path!r}")
        test_file_args = tuple(test_files)
        selection = request.get("selection")
        if config.profile_enabled:
            if (not isinstance(selection, list)
                    or any(not isinstance(item, str) or not item for item in selection)):
                _profile_grammar_error(
                    "profile selection", "contains an invalid token list", config=False,
                )
            if config.runner.pytest_workers_cap is not None:
                selection = list(_cap_profile_worker_arguments(
                    selection, config.runner.pytest_workers_cap,
                    label="profile selection", config=False,
                ))
                request["selection"] = selection
            selector_files = _profile_request_files(request, config)
            overlay_files = tuple(
                _profile_file_identity(
                    path, repo_root=_profile_root(config),
                    cwd_relative=cwd_relative, label="profile test_files entry",
                )
                for path in test_files
            )
            test_file_args = tuple(dict.fromkeys((*selector_files, *overlay_files)))
        else:
            if not isinstance(selection, list):
                raise RemoteCIError("selection must be a list")
            validate_selection(selection, config.selection)
    for path in test_file_args:
        if config.never_route:
            repository = (
                request["repo_root"] if command is not None
                else str(_profile_root(config))
            )
            entry = _never_route_entry(
                path, config.never_route, repo_root=str(repository),
            )
            if entry is not None:
                raise ProfileIneligible(
                    f"test file {path!r} matches never_route entry {entry!r}",
                    fallback_reason="never-route",
                )
    requested_box = request.get("box")
    if requested_box is not None and requested_box not in config.boxes:
        raise RemoteCIError(f"unknown box: {requested_box!r}")
    purpose = _string(request.get("purpose", "remote CI"), "purpose")
    if request.get("route_reason") is not None:
        request["route_reason"] = _safe_id(request.get("route_reason"), "route_reason")
    worker = request.get("worker")
    if worker is not None:
        _mapping(worker, "worker")
    if command is None and kind == "targeted" and len(test_files) > config.selection.max_targeted_files:
        raise RemoteCIError(
            f"targeted request has {len(test_files)} files; maximum is "
            f"{config.selection.max_targeted_files}"
        )
    request["request_id"] = request_id
    if command is None:
        request["tip_sha"] = str(request["tip_sha"]).lower()
        request["candidate_sha"] = str(request["candidate_sha"]).lower()
        request["test_files"] = list(dict.fromkeys(test_files))
        request["selection"] = list(selection)
    else:
        request["test_files"] = (
            list(dict.fromkeys(test_file_args)) if request.get("base_sha") is not None else []
        )
        request["selection"] = []
    request["purpose"] = purpose
    return request


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


_RESIDENT_RUNTIME_PLACEHOLDERS = frozenset({
    "run_id", "lease_id", "lease_token", "owner_identity", "run_dir",
    "managed_run_directory", "token_index", "slot_dir", "checkout_dir",
})


def _resident_template_argv(
    template: Sequence[str], values: Mapping[str, str], **lists: Sequence[str],
) -> list[str]:
    expanded: list[str] = []
    for token in template:
        list_key = token[1:-1] if token.startswith("{") and token.endswith("}") else ""
        if list_key in lists:
            expanded.extend(str(item) for item in lists[list_key])
            continue
        result = token
        for key, value in values.items():
            result = result.replace("{" + key + "}", str(value))
        unresolved = set(re.findall(r"\{([^{}]+)\}", result))
        if unresolved - _RESIDENT_RUNTIME_PLACEHOLDERS:
            raise RemoteCIError(f"unknown command placeholder in {token!r}")
        # The placeholder set above handles valid runtime fields; this catches
        # malformed braces without rewriting those fields.
        scrubbed = result
        for name in _RESIDENT_RUNTIME_PLACEHOLDERS:
            scrubbed = scrubbed.replace("{" + name + "}", "")
        if "{" in scrubbed or "}" in scrubbed:
            raise RemoteCIError(f"unknown command placeholder in {token!r}")
        expanded.append(result)
    return expanded


def _build_command_environment(
    caller_env: Mapping[str, str], runner_env: Mapping[str, str],
    box_env: Mapping[str, str], *, profile_enabled: bool,
    profile_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if profile_enabled:
        return {**caller_env, **runner_env, **box_env, **(profile_env or {})}
    return {**runner_env, **box_env, **caller_env}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    unknown: bool = False


def _signal_process_group(
    process: subprocess.Popen[Any], pgid: int | None,
    leader_identity: Mapping[str, Any] | None, sig: signal.Signals
) -> bool:
    """Signal a process group only while its recorded leader still matches."""
    if process.poll() is not None:
        return False
    if (not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0
            or not isinstance(leader_identity, Mapping)):
        return False
    try:
        leader_pid = int(leader_identity["pid"])
        leader_start = str(leader_identity["start_token"])
    except (KeyError, TypeError, ValueError):
        return False
    if leader_pid != process.pid or not leader_start:
        return False
    if (goalflight_compat.process_identity_matches(leader_pid, leader_start)
            is not True):
        return False
    try:
        if os.getpgid(process.pid) != pgid:
            return False
    except OSError:
        return False
    try:
        os.killpg(pgid, sig)
    except OSError:
        return False
    return True


def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    cwd: Path | None = None,
    input_data: bytes | None = None,
    input_stream: BinaryIO | None = None,
) -> CommandResult:
    if input_data is not None and input_stream is not None:
        raise ValueError("input_data and input_stream are mutually exclusive")

    def decode(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    if input_stream is not None:
        return _run_command_stream(
            argv, env=env, timeout=timeout, cwd=cwd,
            input_stream=input_stream, decode=decode,
        )

    try:
        # A watcher/driver gets its own session so queue-runner exit cannot kill it.
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise RemoteCIError(f"configured command could not start: {argv[0] if argv else '<empty>'}: {exc}") from exc
    leader_identity = goalflight_compat.process_start_identity(process.pid)
    try:
        process_group = os.getpgid(process.pid)
    except OSError:
        process_group = None
    try:
        if input_data is None:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if not _signal_process_group(process, process_group, leader_identity, signal.SIGTERM):
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            return CommandResult(
                124,
                decode(exc.stdout),
                decode(exc.stderr) + "\nprocess group identity became unknown",
                True,
                True,
            )
        _start_process_reaper(process, process_group, leader_identity)
        return CommandResult(
            124,
            decode(exc.stdout),
            decode(exc.stderr) + f"\ncommand timed out after {timeout}s",
            True,
        )
    return CommandResult(process.returncode, decode(stdout), decode(stderr), False)


def _run_command_stream(
    argv: Sequence[str], *, env: Mapping[str, str] | None,
    timeout: float | None, cwd: Path | None, input_stream: BinaryIO,
    decode: Callable[[str | bytes | None], str],
) -> CommandResult:
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
    except OSError as exc:
        raise RemoteCIError(
            f"configured command could not start: {argv[0] if argv else '<empty>'}: {exc}"
        ) from exc
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RemoteCIError("configured command did not expose its streamed pipes")
    leader_identity = goalflight_compat.process_start_identity(process.pid)
    try:
        process_group = os.getpgid(process.pid)
    except OSError:
        process_group = None

    output = {"stdout": [], "stderr": []}
    writer_errors: list[BaseException] = []

    def pump_input() -> None:
        try:
            while True:
                chunk = input_stream.read(64 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = process.stdin.write(view)
                    if not written:
                        raise BrokenPipeError("remote submit stream was not consumed")
                    view = view[written:]
        except (BrokenPipeError, OSError, ValueError) as exc:
            writer_errors.append(exc)
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    def drain(stream, chunks) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                chunks.append(chunk)
        except OSError:
            return

    writer = threading.Thread(target=pump_input, name="remote-ci-stream-writer", daemon=True)
    readers = [
        threading.Thread(
            target=drain, args=(process.stdout, output["stdout"]),
            name="remote-ci-stdout-reader", daemon=True,
        ),
        threading.Thread(
            target=drain, args=(process.stderr, output["stderr"]),
            name="remote-ci-stderr-reader", daemon=True,
        ),
    ]
    writer.start()
    for reader in readers:
        reader.start()

    timed_out = False
    unknown = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if not _signal_process_group(
                process, process_group, leader_identity, signal.SIGTERM):
            unknown = True
        else:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if _signal_process_group(
                        process, process_group, leader_identity, signal.SIGKILL):
                    try:
                        process.wait(timeout=REMOTE_PROCESS_REAP_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        unknown = True
                else:
                    unknown = True

    if unknown:
        try:
            process.stdin.close()
        except OSError:
            pass

        def reap_stream() -> None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if _signal_process_group(
                        process, process_group, leader_identity, signal.SIGKILL):
                    try:
                        process.wait(timeout=REMOTE_PROCESS_REAP_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        pass
            for thread in (writer, *readers):
                thread.join(REMOTE_STREAM_JOIN_TIMEOUT_SECONDS)

        threading.Thread(
            target=reap_stream, name="remote-ci-stream-reaper", daemon=True,
        ).start()
        stdout = b"".join(output["stdout"])
        stderr = b"".join(output["stderr"])
        return CommandResult(
            124, decode(stdout), decode(stderr) + "\nprocess group identity became unknown",
            True, True,
        )

    stream_deadline = time.monotonic() + REMOTE_STREAM_JOIN_TIMEOUT_SECONDS
    for thread in (writer, *readers):
        thread.join(max(0.0, stream_deadline - time.monotonic()))
    streams_open = any(thread.is_alive() for thread in (writer, *readers))
    stdout = b"".join(output["stdout"])
    stderr = b"".join(output["stderr"])
    if writer_errors and process.returncode == 0:
        raise RemoteCIError(f"could not stream submit request: {writer_errors[0]}")
    if streams_open:
        return CommandResult(
            124, decode(stdout),
            decode(stderr) + "\ntransport streams did not close after subprocess exit",
            True, True,
        )
    if timed_out:
        return CommandResult(
            124, decode(stdout), decode(stderr) + f"\ncommand timed out after {timeout}s", True,
        )
    return CommandResult(process.returncode, decode(stdout), decode(stderr), False)


def _start_process_reaper(
    process: subprocess.Popen[Any], process_group: int | None,
    leader_identity: Mapping[str, Any] | None,
) -> None:
    """Finish draining a timed-out transport process off the placement path."""
    def reap() -> None:
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            if _signal_process_group(
                process, process_group, leader_identity, signal.SIGKILL,
            ):
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    process.communicate(timeout=REMOTE_PROCESS_REAP_TIMEOUT_SECONDS)
        except OSError:
            pass

    threading.Thread(target=reap, name="remote-ci-process-reaper", daemon=True).start()


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
    skipped: int = 0
    duration_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "hostname": self.hostname,
            "status": self.status,
            "returncode": self.returncode,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
            "failing_node_ids": list(self.failing_node_ids),
            "collection_import_errors": list(self.collection_import_errors),
            "first_error_lines": list(self.first_error_lines),
        }
        if self.duration_seconds is not None:
            result["duration_seconds"] = self.duration_seconds
        result.update(self.metadata)
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
        skipped=count("skipped"),
        errors=count("errors"),
        failing_node_ids=_list_field(raw.get("failing_node_ids"), "receipt failing_node_ids"),
        collection_import_errors=tuple(dict.fromkeys(collection_errors)),
        first_error_lines=_list_field(raw.get("first_error_lines"), "receipt first_error_lines"),
        duration_seconds=duration,
        metadata={
            key: raw[key]
            for key in (
                "box_id", "expected_hostname", "measured_hostname", "tip_sha",
                "candidate_sha", "command_id", "command_override_id",
                "process_cpu_seconds", "wall_seconds",
                "load_average_start", "load_average_end", "route", "route_reason",
                "fallback_reason", "requirements_sha256", "venv_path",
                "profile", "project_id", "policy_outcome",
                "head_sha", "snapshot_sha", "dirty",
                "dispatch_id", "controller_label", "command_sha",
                "daemon_config_sha256", "daemon_config_mtime_ns",
                "daemon_config_size_bytes",
                "failure_summary", "failure_excerpt", "failure_summary_complete",
                "stdout_path",
            )
            if key in raw
        },
    )


def _execution_receipt_metadata(
    argv: Sequence[str], selection: Sequence[str], env: Mapping[str, str],
    receipt_metadata: Mapping[str, str] | None = None,
) -> dict[str, str]:
    normalized_argv = [str(value) for value in argv]
    normalized_selection = [str(value) for value in selection]
    digest_input = json.dumps(
        {"argv": normalized_argv, "selection": normalized_selection},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    metadata = {
        "command_sha": hashlib.sha256(digest_input).hexdigest(),
    }
    for source, target in (
        ("GOALFLIGHT_DISPATCH_ID", "dispatch_id"),
        ("GOALFLIGHT_CONTROLLER_LABEL", "controller_label"),
    ):
        value = env.get(source)
        if isinstance(value, str) and value:
            metadata[target] = value
    for key in RECEIPT_METADATA_FIELDS:
        value = (receipt_metadata or {}).get(key)
        if isinstance(value, str) and value:
            metadata[key] = value
    return metadata


def _daemon_config_receipt_metadata(config: DaemonConfig) -> dict[str, Any]:
    identity = config.config_identity
    if identity is None:
        return {}
    return {
        "daemon_config_sha256": identity.sha256,
        "daemon_config_mtime_ns": identity.mtime_ns,
        "daemon_config_size_bytes": identity.size_bytes,
    }


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
    command: tuple[str, ...] | None = None
    command_id: str | None = None
    route_reason: str | None = None
    head_sha: str | None = None
    snapshot_sha: str | None = None
    dirty: bool = False
    cwd_relative: str = ""
    caller_env: Mapping[str, str] = field(default_factory=dict)
    bundle_path: Path | None = None
    receipt_metadata: Mapping[str, str] = field(default_factory=dict)
    snapshot_root: Path | None = None
    snapshot_ref: str | None = None
    caller_identity: Mapping[str, Any] | None = None
    generated_node_id: str | None = None
    spool_path: Path | None = None


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
    lease: dict[str, Any] | None = None
    bundle_bytes: int | None = None
    setup_failure_type: str | None = None
    remote_ticket_accepted: bool | None = None

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
            value["lease"] = self.lease
        if self.bundle_bytes is not None:
            value["bundle_bytes"] = self.bundle_bytes
        if self.error:
            value["error"] = self.error
        if self.setup_failure_type:
            value["setup_failure_type"] = self.setup_failure_type
        if self.remote_ticket_accepted is not None:
            value["remote_ticket_accepted"] = self.remote_ticket_accepted
        if self.status == "setup-failed" and not self.error:
            value["error"] = "remote setup failed"
        return value


def _setup_failure_detail(outcome: ArmOutcome) -> str | None:
    if outcome.status != "setup-failed":
        return None
    return outcome.error or "remote setup failed"


Executor = Callable[..., CommandResult]


class RemoteRunner:
    """Admit, start and observe node-owned runs through one remote primitive."""

    def __init__(self, config: DaemonConfig, *, executor: Executor | None = None,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self.executor = executor or (
            lambda argv, env, timeout, **kwargs:
            run_command(argv, env=env, timeout=timeout, **kwargs)
        )
        self.sleeper = sleeper
        self._inflight: set[tuple[str, str]] = set()
        self._placement_lock = threading.Lock()
        self._placement_cursor = 0
        try:
            helper_source = Path(__file__).with_name(
                "goalflight_remote_ci_node.py"
            ).read_bytes()
        except OSError as exc:
            raise RemoteCIError(f"cannot read local remote-CI node helper: {exc}") from exc
        self.nodes = {
            name: RemoteNode(
                box, config.admission, self.executor,
                poll_seconds=config.poll_seconds,
                result_tail_bytes=config.result_tail_kib * 1024,
                control_dir=Path("/tmp") / f"gf-ssh-{os.geteuid()}",
                helper_source=helper_source,
            )
            for name, box in config.boxes.items()
        }

    @staticmethod
    def _owner() -> dict[str, Any]:
        identity = goalflight_compat.process_start_identity(os.getpid())
        start_token = identity.get("start_token") if isinstance(identity, Mapping) else ""
        return {"owner_identity": _owner_identity(), "owner_pid": os.getpid(),
                "owner_start_token": str(start_token or ""),
                "owner_host": socket.gethostname()}

    def _lease_key(self, record: Mapping[str, Any]) -> tuple[str, str]:
        return (str(record["run_directory"]), str(record["lease_token"]))

    @staticmethod
    def _normalize_hostname(value: str) -> str:
        normalized = value.strip().lower()
        return normalized[:-6] if normalized.endswith(".local") else normalized

    def choose_box(
        self, requested: str | None = None, *, excluding: Sequence[str] = (),
        deadline: float | None = None, _with_host: bool = False,
    ) -> str | BoxPlacement:
        ranked: list[tuple[int, float, int, int, str, str, str]] = []
        excluded = set(excluding)
        names = ([requested] if requested is not None and requested not in excluded
                 else [] if requested is not None else
                 [name for name in sorted(self.nodes) if name not in excluded])
        measured_boxes: list[tuple[str, float, int, str, str | None]] = []
        skip_causes: dict[str, str] = {}
        if deadline is None:
            deadline = time.monotonic() + self.config.admission.max_wait_seconds
        eligible: list[str] = []
        for name in names:
            node = self.nodes.get(name)
            if node is None:
                skip_causes[name] = "not-configured"
            elif node.box.max_concurrent_seats <= 0:
                skip_causes[name] = "no-seats-configured"
            else:
                eligible.append(name)

        probe_inputs: dict[str, list[str]] = {}
        if time.monotonic() >= deadline:
            for name in eligible:
                skip_causes[name] = "probe-not-started (placement deadline expired)"
        else:
            for name in eligible:
                node = self.nodes[name]
                try:
                    probe_inputs[name] = [
                        _expand_profile_value(
                            path, node.box.env, f"boxes.{name}.required_paths",
                        )
                        for path in node.box.required_paths
                    ]
                except RemoteCIError as exc:
                    skip_causes[name] = f"probe-error ({exc})"

        def record_health(name: str, health: Any) -> None:
            if not isinstance(health, Mapping):
                skip_causes[name] = "bad-load (malformed health response)"
                return
            if health.get("status") == "unknown":
                reason = health.get("reason")
                detail = reason if isinstance(reason, str) and reason else "reason unavailable"
                skip_causes[name] = f"health-unknown ({detail})"
                return
            missing_paths = health.get("missing_required_paths", [])
            if missing_paths:
                skip_causes[name] = (
                    "missing-paths (" + ", ".join(str(path) for path in missing_paths) + ")"
                )
                return
            measured = health.get("load", {})
            if not isinstance(measured, Mapping):
                skip_causes[name] = "bad-load (missing load sample)"
                return
            hostname = measured.get("hostname")
            peer_address = health.get("peer_address")
            busy = measured.get("cpu_busy_fraction")
            load = measured.get("load1")
            tokens = health.get("tokens", {})
            if (not isinstance(hostname, str) or not hostname.strip()
                    or any(character.isspace() or ord(character) < 32
                           for character in hostname)):
                skip_causes[name] = "bad-load (missing measured hostname)"
                return
            node = self.nodes[name]
            expected = node.box.expected_hostname
            if self._normalize_hostname(hostname) != self._normalize_hostname(expected):
                skip_causes[name] = (
                    f"hostname-mismatch (expected={expected}, measured={hostname})"
                )
                return
            if _with_host:
                try:
                    peer_address = ipaddress.ip_address(peer_address).compressed
                except (TypeError, ValueError):
                    skip_causes[name] = "bad-load (missing or invalid SSH peer address)"
                    return
            if (isinstance(load, bool) or not isinstance(load, (int, float))
                    or isinstance(busy, bool) or not isinstance(busy, (int, float))
                    or not math.isfinite(load) or not math.isfinite(busy)
                    or not 0 <= busy <= 1):
                skip_causes[name] = f"bad-load (load1={load!r}, busy={busy!r})"
                return
            if (float(load) >= SATURATION_LOAD_FACTOR * node.box.p_cores
                    and float(busy) >= SATURATION_BUSY_FRACTION):
                skip_causes[name] = f"saturated (load1={float(load):g}, busy={float(busy):.3f})"
                return
            score = max(
                float(load) / (SATURATION_LOAD_FACTOR * node.box.p_cores),
                float(busy),
            )
            free = tokens.get("free", 0) if isinstance(tokens, Mapping) else 0
            if isinstance(free, bool) or not isinstance(free, int):
                free = 0
            measured_boxes.append((name, score, free, hostname, peer_address))

        def probe_health(name: str, required_paths: list[str]) -> Any:
            transient_signatures = (
                "connection reset by peer",
                "read from remote host",
                "connection closed by remote host",
                "closed by remote host",
                "connection timed out",
                "port 22 timed out",
                "operation timed out",
                "connection refused",
                "broken pipe",
            )
            for attempt in range(3):
                try:
                    return self.nodes[name].call(
                        "health",
                        _transport_timeout=REMOTE_HEALTH_PROBE_TIMEOUT_SECONDS,
                        required_paths=required_paths,
                    )
                except RemoteTransportError as exc:
                    message = str(exc).lower()
                    if attempt == 2 or not any(
                        signature in message for signature in transient_signatures
                    ):
                        raise
                    backoff = min(max(self.config.poll_seconds, 0.05), 0.25)
                    if (time.monotonic() + backoff
                            + REMOTE_HEALTH_PROBE_TIMEOUT_SECONDS >= deadline):
                        raise
                    self.sleeper(backoff)

        if probe_inputs:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(probe_inputs), thread_name_prefix="remote-ci-health",
            ) as probes:
                futures = {
                    probes.submit(
                        probe_health, name, required_paths,
                    ): name
                    for name, required_paths in probe_inputs.items()
                }
                for future in concurrent.futures.as_completed(futures):
                    name = futures[future]
                    try:
                        health = future.result()
                    except RemoteTransportTimeoutError as exc:
                        skip_causes[name] = f"timeout ({exc})"
                    except RemoteCIError as exc:
                        skip_causes[name] = f"probe-error ({exc})"
                    else:
                        record_health(name, health)
        if not measured_boxes:
            details = "; ".join(
                f"{name}: {skip_causes.get(name, 'no eligible health result')}"
                for name in names
            )
            if any(cause.startswith((
                    "probe-error", "timeout", "probe-not-started", "health-unknown"))
                   for cause in skip_causes.values()):
                raise HealthProbeUnknown(
                    "could not probe Studio health; placement is unknown"
                    + (f"; {details}" if details else "; no boxes to probe")
                )
            raise NoHealthyBox(
                "no healthy Studio" + (f"; {details}" if details else "; no boxes to probe")
            )
        with self._placement_lock:
            cursor = self._placement_cursor % max(1, len(names))
            for name, score, free, hostname, peer_address in measured_boxes:
                position = names.index(name)
                rotation = (position - cursor) % max(1, len(names))
                ranked.append((0 if free > 0 else 1, score, -free, rotation,
                               name, hostname, peer_address))
            selected = min(ranked)
            if requested is None:
                self._placement_cursor = (names.index(selected[4]) + 1) % len(names)
            if _with_host:
                assert selected[6] is not None
                return BoxPlacement(selected[4], selected[5], selected[6])
            return selected[4]

    def _node_for_lease(
        self, node: RemoteNode, record: Mapping[str, Any],
    ) -> RemoteNode:
        identity = record.get("remote_run")
        if identity is not None and not isinstance(identity, Mapping):
            raise AmbiguousRemoteError("remote lease has an invalid run identity")
        hostname = record.get("measured_hostname")
        if not isinstance(hostname, str) or not hostname.strip():
            raise AmbiguousRemoteError("remote lease has no measured node hostname")
        if self._normalize_hostname(hostname) != self._normalize_hostname(
            node.box.expected_hostname,
        ):
            raise AmbiguousRemoteError(
                f"remote lease host {hostname!r} differs from configured box "
                f"{node.box.expected_hostname!r}"
            )
        address = record.get("destination_address")
        if not isinstance(address, str) or not address:
            raise AmbiguousRemoteError("remote lease has no persisted peer address")
        if isinstance(identity, Mapping):
            host = identity.get("host")
            if (not isinstance(host, str)
                    or self._normalize_hostname(host) != self._normalize_hostname(hostname)):
                raise AmbiguousRemoteError(
                    "remote run hostname differs from its persisted lease hostname"
                )
            if identity.get("address") != address:
                raise AmbiguousRemoteError(
                    "remote run address differs from its persisted lease address"
                )
        try:
            normalized_address = ipaddress.ip_address(address).compressed
        except ValueError as exc:
            raise AmbiguousRemoteError(
                f"remote lease peer address {address!r} is invalid"
            ) from exc
        if node.pinned_host is not None and normalized_address != node.pinned_host:
            raise AmbiguousRemoteError(
                f"remote lease address {address!r} differs from pinned peer "
                f"{node.pinned_host!r}"
            )
        return node.with_pinned_host(normalized_address)

    def _owns(self, record: Mapping[str, Any]) -> bool:
        return (
            record.get("owner_host") == socket.gethostname()
            and record.get("owner_pid") == os.getpid()
            and self._owner_alive(record) is True
        )

    @staticmethod
    def _owner_alive(record: Mapping[str, Any]) -> bool | None:
        if record.get("owner_host") != socket.gethostname():
            return None
        pid = record.get("owner_pid")
        token = record.get("owner_start_token")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        if not isinstance(token, str) or not token:
            return None
        return goalflight_compat.process_identity_matches(pid, token)

    @staticmethod
    def _status(
        node: "RemoteNode", key: Mapping[str, str], *, phase: str,
    ) -> Mapping[str, Any]:
        for attempt in range(2):
            try:
                return node.call(
                    "status", **key,
                    _transport_timeout=REMOTE_STATUS_TIMEOUT_SECONDS,
                )
            except RemoteTransportTimeoutError as exc:
                if attempt == 1:
                    raise RemoteTransportTimeoutError(
                        f"{phase} failed: {exc}"
                    ) from exc
            except RemoteCIError as exc:
                raise RemoteCIError(f"{phase} failed: {exc}") from exc
        raise AssertionError("status retry loop exhausted")

    def _release_forgotten(
        self, node: "RemoteNode", record: Mapping[str, Any], *,
        transport_timeout: float = 30.0,
    ) -> str:
        """Release a lease this live process admitted and then dropped.

        Reap treats a live owner as busy. That is right for a run this process
        is inside, and wrong for one whose enqueue or release reply was lost:
        the owner pid is still the daemon, so nobody else will cancel it.
        """
        node = self._node_for_lease(node, record)
        key = {"run_dir": record["run_directory"], "lease_token": record["lease_token"]}
        fresh = self._status(node, key, phase="forgotten-run status")
        if fresh.get("state") == "released":
            return "finished"
        if not self._owns(fresh) or self._lease_key(fresh) in self._inflight:
            return "owned"
        expected = {name: fresh.get(name) for name in
                    ("owner_host", "owner_pid", "owner_start_token", "owner_identity")}
        if fresh.get("state") == "running" and fresh.get("remote_run"):
            return node.call("cancel", **key, identity=fresh["remote_run"],
                             _transport_timeout=transport_timeout,
                             expected_owner=expected)["status"]
        try:
            return node.call("release", **key, expected_owner=expected,
                             _transport_timeout=transport_timeout)["status"]
        except RemoteCIError:
            if not fresh.get("remote_run"):
                return "unknown"
            return node.call("cancel", **key, identity=fresh["remote_run"],
                             _transport_timeout=transport_timeout,
                             expected_owner=expected)["status"]

    def _recover_other_forgotten(
        self, node: "RemoteNode", *, deadline: float | None = None,
    ) -> None:
        timeout = 30.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            timeout = min(timeout, remaining / 2)
            if timeout < REMOTE_TRANSPORT_ALLOWANCE_SECONDS:
                return
        for record in node.call("list", _transport_timeout=timeout):
            if record.get("state") == "released" or not self._owns(record):
                continue
            if self._lease_key(record) in self._inflight:
                continue
            record_timeout = timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                record_timeout = max(
                    REMOTE_TRANSPORT_ALLOWANCE_SECONDS,
                    min(record_timeout, remaining / 4),
                )
            self._release_forgotten(
                node, record, transport_timeout=record_timeout,
            )

    def _cancel_best_effort(
        self, node: "RemoteNode", key: Mapping[str, str] | None,
        expected_owner: Mapping[str, Any], request_id: str | None = None,
        arm: str | None = None, *, transport_timeout: float = 30.0,
    ) -> None:
        records: list[Mapping[str, Any]] = []
        if key is not None:
            try:
                records = [self._status(node, key, phase="cleanup status")]
            except RemoteCIError:
                pass
        elif request_id is not None:
            try:
                records = [value for value in node.call(
                    "list", _transport_timeout=transport_timeout,
                )
                           if value.get("request_id") == request_id
                           and value.get("arm") == arm]
            except RemoteCIError:
                pass
        for record in records:
            run_key = key or {
                "run_dir": str(record.get("run_directory") or ""),
                "lease_token": str(record.get("lease_token") or ""),
            }
            try:
                identity = record.get("remote_run")
                if isinstance(identity, Mapping):
                    node.call("cancel", **run_key, identity=identity,
                              expected_owner=dict(expected_owner),
                              _transport_timeout=transport_timeout)
                elif run_key.get("run_dir") and run_key.get("lease_token"):
                    node.call("release", **run_key,
                              expected_owner=dict(expected_owner),
                              _transport_timeout=transport_timeout)
            except RemoteCIError:
                continue

    def _resident_command_spec(self, spec: ArmSpec, node: RemoteNode) -> dict[str, Any]:
        command_override = self.config.runner.command_overrides.get(spec.command_id or "")
        known = {
            "box": spec.box, "host": node.box.host, "arm": spec.arm,
            "sha": spec.sha, "tip_sha": spec.tip_sha,
            "candidate_sha": spec.candidate_sha, "request_id": spec.request_id,
            "overlay": "1" if spec.overlay else "0",
            "managed_run_directory": str(node.box.managed_run_directory),
        }
        if spec.command is not None:
            argv = list(spec.command)
            expand_template = False
        elif self.config.profile_enabled:
            argv = _resident_template_argv(
                [*self.config.runner.command, *spec.selection], known,
                selection=spec.selection, test_files=spec.test_files,
                test_command=self.config.runner.test_command,
            )
            expand_template = True
        else:
            argv = _resident_template_argv(
                self.config.runner.command, known,
                selection=spec.selection, test_files=spec.test_files,
                test_command=self.config.runner.test_command,
            )
            expand_template = True

        env = _build_command_environment(
            spec.caller_env, self.config.runner.env, node.box.env,
            profile_enabled=self.config.profile_enabled,
        )
        env.update({"GOALFLIGHT_REMOTE_CI_" + name.upper(): str(value)
                    for name, value in known.items()})
        environment_recipe = dict(self.config.environment)
        if "interpreter" in environment_recipe:
            environment_recipe["interpreter"] = _expand_profile_value(
                environment_recipe["interpreter"], node.box.env,
                "environment.interpreter",
            )
            environment_recipe["version_probe"] = [
                _expand_profile_value(value, node.box.env, "environment.version_probe")
                for value in environment_recipe["version_probe"]
            ]
        base_overlays = [
            {
                **overlay,
                "source": _expand_profile_value(
                    overlay["source"], node.box.env,
                    f"base_overlays[{index}].source",
                ),
            }
            for index, overlay in enumerate(self.config.base_overlays)
        ]
        receipt_probes = {}
        for name, probe in self.config.receipt_probes.items():
            receipt_probes[name] = {
                **probe,
                "command": [
                    _expand_profile_value(value, node.box.env,
                                          f"receipt_probes.{name}.command")
                    for value in probe["command"]
                ],
                **({"expected": _expand_profile_value(
                    probe["expected"], node.box.env,
                    f"receipt_probes.{name}.expected",
                )} if "expected" in probe else {}),
            }
        env.update({
            "GOALFLIGHT_REMOTE_CI_RESIDENT_CWD": "1",
            "GOALFLIGHT_REMOTE_CI_CWD_RELATIVE": spec.cwd_relative,
            "GOALFLIGHT_REMOTE_CI_EXPAND_RUNNER_TEMPLATE": (
                "1" if expand_template else "0"
            ),
            "GOALFLIGHT_REMOTE_CI_BOX_PYTHONPATH": node.box.env.get("PYTHONPATH", ""),
            "GOALFLIGHT_REMOTE_CI_TEST_FILES_JSON": json.dumps(list(spec.test_files)),
            "GOALFLIGHT_REMOTE_CI_OVERLAY_PATHS_JSON": json.dumps(
                list(self.config.selection.path_prefixes)),
            "GOALFLIGHT_REMOTE_CI_SELECTION_JSON": json.dumps(list(spec.selection)),
            "GOALFLIGHT_REMOTE_CI_TEST_COMMAND_JSON": json.dumps(
                list(self.config.runner.test_command)),
            "GOALFLIGHT_REMOTE_CI_REPO_URL": self.config.repo_url or "",
            "GOALFLIGHT_REMOTE_CI_ENVIRONMENT_JSON": json.dumps(environment_recipe),
            "GOALFLIGHT_REMOTE_CI_PROFILE_ENABLED": (
                "1" if self.config.profile_enabled else "0"
            ),
            "GOALFLIGHT_REMOTE_CI_RECEIPT_PROBES_JSON": json.dumps(receipt_probes),
            "GOALFLIGHT_REMOTE_CI_BASE_OVERLAYS_JSON": json.dumps(base_overlays),
            "GOALFLIGHT_REMOTE_CI_PYTHON_INTERPRETER": node.box.python_interpreter,
            "GOALFLIGHT_REMOTE_CI_CHECKOUT_BASENAME": self.config.checkout_basename,
            "GOALFLIGHT_REMOTE_CI_EXPECTED_HOSTNAME": node.box.expected_hostname or "",
            "GOALFLIGHT_REMOTE_CI_HEAD_SHA": spec.head_sha or spec.sha,
            "GOALFLIGHT_REMOTE_CI_SNAPSHOT_SHA": spec.snapshot_sha or spec.sha,
            "GOALFLIGHT_REMOTE_CI_DIRTY": "true" if spec.dirty else "false",
            "GOALFLIGHT_REMOTE_CI_COMMAND_ID": spec.command_id or "",
            "GOALFLIGHT_REMOTE_CI_ROUTE_REASON": spec.route_reason or "heavy-command",
            "GOALFLIGHT_REMOTE_CI_EMIT_RECEIPT": (
                "1" if spec.command is not None or self.config.profile_enabled
                or command_override else "0"
            ),
            "GOALFLIGHT_REMOTE_CI_QUEUE_WAIT_SECONDS": str(
                self.config.admission.queue_wait_seconds),
            "GOALFLIGHT_REMOTE_CI_PROJECT_ID": self.config.repo,
            "GOALFLIGHT_REMOTE_CI_SELF_CAP": str(self.config.runner.self_cap),
            "GOALFLIGHT_REMOTE_CI_SELF_CAP_ENV_NAME": self.config.runner.self_cap_env,
        })
        env[self.config.runner.self_cap_env] = str(self.config.runner.self_cap)
        if command_override is not None:
            env["GOALFLIGHT_REMOTE_CI_COMMAND_OVERRIDE_ID"] = spec.command_id or ""
        command: dict[str, Any] = {
            "argv": argv,
            "env": env,
            "timeout": self.config.runner.timeout_seconds,
            "cwd": "",
            "emit_receipt": (
                spec.command is not None or self.config.profile_enabled
                or command_override is not None
            ),
        }
        if command_override is not None:
            command["env_set"] = command_override.env_set
            command["env_unset"] = list(command_override.env_unset)
        return command

    def _resident_terminal_outcome(
        self, spec: ArmSpec, node: RemoteNode, key: Mapping[str, str],
        record: Mapping[str, Any],
    ) -> ArmOutcome | None:
        completed = record.get("result")
        if not isinstance(completed, Mapping):
            return None
        raw_identity = record.get("remote_run")
        identity = (RemoteRunIdentity.from_mapping(raw_identity)
                    if isinstance(raw_identity, Mapping) else None)
        kind = completed.get("status")
        setup_failure_type = completed.get("setup_failure_type")
        if not isinstance(setup_failure_type, str) or not setup_failure_type:
            setup_failure_type = None
        if kind == "capacity-refused":
            return ArmOutcome(spec.arm, "capacity", 75, None, identity,
                              lease=dict(record), remote_ticket_accepted=True)
        if kind == "cancelled":
            return ArmOutcome(spec.arm, "cancelled", int(completed.get("returncode", 130)),
                              None, identity, cancelled=True, lease=dict(record),
                              remote_ticket_accepted=True)
        if kind == "deadline" or completed.get("timed_out"):
            return ArmOutcome(spec.arm, "timeout", 124, None, identity,
                              timed_out=True, cancelled=True, lease=dict(record),
                              remote_ticket_accepted=(
                                  False if kind == "expired"
                                  and completed.get("command_started") is False else True
                              ))
        command_started = completed.get("command_started") is True
        exit_known = completed.get("exit_known") is True
        if ((kind == "setup-failed" or not exit_known) and not command_started):
            return ArmOutcome(
                spec.arm, "setup-failed", 2, None, identity,
                error=str(completed.get("error") or "remote setup failed"),
                lease=dict(record), setup_failure_type=setup_failure_type,
                remote_ticket_accepted=True,
            )
        try:
            output_parts = []
            omitted = []
            for name in ("stdout", "stderr"):
                fetched = node.call(
                    "fetch", **key, stream=name, last_kb=256,
                    _transport_timeout=REMOTE_STATUS_TIMEOUT_SECONDS,
                )
                if (not isinstance(fetched, Mapping) or fetched.get("status") != "ok"
                        or fetched.get("compression") != "gzip"):
                    raise ReceiptError(f"node could not fetch the {name} stream")
                body = gzip.decompress(base64.b64decode(fetched["body_gzip_base64"]))
                if (len(body) != fetched.get("size_bytes")
                        or hashlib.sha256(body).hexdigest() != fetched.get("sha256")):
                    raise ReceiptError(f"node {name} stream digest did not verify")
                output_parts.append(body.decode("utf-8", errors="replace"))
                if fetched.get("truncated") is True:
                    omitted.append(f"{name} omitted {fetched.get('omitted_bytes', 0)} bytes")
            receipt = receipt_from_output("\n".join(output_parts))
            execution_argv = spec.command or self.config.runner.command
            receipt = replace(
                receipt,
                metadata={
                    **receipt.metadata,
                    **_daemon_config_receipt_metadata(self.config),
                    **_execution_receipt_metadata(
                        execution_argv, spec.selection, spec.caller_env,
                        spec.receipt_metadata,
                    ),
                },
            )
            sample = record.get("sample")
            sampled_hostname = sample.get("hostname") if isinstance(sample, Mapping) else None
            expected_hostname = self._normalize_hostname(node.box.expected_hostname)
            if (not isinstance(sampled_hostname, str)
                    or self._normalize_hostname(sampled_hostname) != expected_hostname
                    or self._normalize_hostname(receipt.hostname) != expected_hostname):
                raise ReceiptError("receipt hostname differs from the measured node")
        except (ReceiptError, KeyError, ValueError, OSError) as exc:
            if isinstance(exc, (KeyError, ValueError, OSError)):
                raise RemoteCIError(f"resident result stream could not be verified: {exc}") from exc
            detail = str(exc)
            if omitted:
                detail += "; truncated output: " + ", ".join(omitted)
            return ArmOutcome(
                spec.arm, "setup-failed", 2, None, identity, error=detail,
                lease=dict(record), setup_failure_type=setup_failure_type,
                remote_ticket_accepted=True,
            )
        returncode = completed.get("returncode")
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            return ArmOutcome(
                spec.arm, "setup-failed", 2, None, identity,
                error="remote exit status is unknown", lease=dict(record),
                setup_failure_type=setup_failure_type,
                remote_ticket_accepted=True,
            )
        status = "green" if returncode == 0 and _receipt_is_green(receipt) else "red"
        return ArmOutcome(spec.arm, status, returncode, receipt, identity,
                          lease=dict(record), remote_ticket_accepted=True)

    def _lookup_resident_submission(
        self, node: RemoteNode, spec: ArmSpec,
    ) -> Mapping[str, Any] | None:
        records = node.call("list", _transport_timeout=REMOTE_STATUS_TIMEOUT_SECONDS)
        if not isinstance(records, list):
            raise RemoteCIError("resident ticket lookup response was not a list")
        if any(isinstance(record, Mapping) and record.get("state") == "UNREADABLE"
               for record in records):
            raise RemoteCIError("resident ticket lookup contains unreadable metadata")
        existing = next((record for record in records
                         if isinstance(record, Mapping)
                         and record.get("request_id") == spec.request_id
                         and record.get("arm") == spec.arm
                         and record.get("resident_submission") is True), None)
        if (existing is not None
                and (not isinstance(existing.get("run_directory"), str)
                     or not isinstance(existing.get("lease_token"), str))):
            raise RemoteCIError("resident ticket lookup did not identify its ticket")
        return existing

    def _run_resident_arm(self, spec: ArmSpec) -> ArmOutcome:
        state = _load_resident_arm(spec.spool_path, spec.arm)
        if (state.get("phase") == "preparing"
                and _request_cancelled(self.config, spec.request_id, spec.caller_identity)):
            return ArmOutcome(spec.arm, "cancelled", 130, None, cancelled=True,
                              remote_ticket_accepted=None)
        if state.get("phase") == "failed":
            return ArmOutcome(
                spec.arm, "setup-failed", 2, None,
                error=str(state.get("error") or "resident submit failed"),
                remote_ticket_accepted=(
                    state.get("remote_ticket_accepted")
                    if isinstance(state.get("remote_ticket_accepted"), bool) else None
                ),
            )
        if not state:
            if _request_cancelled(self.config, spec.request_id, spec.caller_identity):
                return ArmOutcome(spec.arm, "cancelled", 130, None, cancelled=True,
                                  remote_ticket_accepted=None)
            try:
                self.choose_box(
                    requested=spec.box,
                    deadline=time.monotonic() + self.config.admission.max_wait_seconds,
                )
            except RemoteCIError as exc:
                return ArmOutcome(spec.arm, "setup-failed", 2, None, error=str(exc),
                                  remote_ticket_accepted=None)
            state = {
                "phase": "preparing", "box": spec.box, "owner": self._owner(),
            }
            try:
                _save_resident_arm(spec.spool_path, spec.arm, state)
            except RemoteCIError as exc:
                return ArmOutcome(spec.arm, "setup-failed", 2, None, error=str(exc),
                                  remote_ticket_accepted=None)
        box = str(state.get("box") or spec.box)
        if box not in self.nodes:
            phase = state.get("phase")
            error = f"resident ticket names unknown box {box!r}; collection is pending"
            if phase in {"submitting", "accepted", "collecting"}:
                return ArmOutcome(
                    spec.arm, "submitted", 0, None, error=error,
                    remote_ticket_accepted=(
                        True if phase in {"accepted", "collecting"} else None
                    ),
                )
            return ArmOutcome(
                spec.arm, "setup-failed", 2, None,
                error=f"resident ticket names unknown box {box!r}",
                remote_ticket_accepted=(
                    None if phase == "preparing" else True
                ),
            )
        spec = replace(spec, box=box)
        node = self.nodes[box]
        owner = state.get("owner")
        if not isinstance(owner, Mapping):
            return ArmOutcome(spec.arm, "setup-failed", 2, None,
                              error="resident ticket owner checkpoint is missing",
                              remote_ticket_accepted=(
                                  None if state.get("phase") == "preparing" else True
                              ))
        phase = state.get("phase")
        if phase == "submitting":
            try:
                existing = self._lookup_resident_submission(node, spec)
            except RemoteCIError as exc:
                return ArmOutcome(spec.arm, "submitted", 0, None, error=str(exc),
                                  remote_ticket_accepted=None)
            if existing is None:
                # A list miss does not prove the submit failed: its private
                # upload is intentionally absent from list until publication.
                # Keep ownership ambiguous and retry this lookup on a later
                # awake-time daemon pass.
                return ArmOutcome(
                    spec.arm, "submitted", 0, None,
                    error="resident submit ticket is not visible yet; reconciliation will retry",
                    remote_ticket_accepted=None,
                )
            state.update(
                phase="accepted",
                ticket={
                    "run_dir": existing["run_directory"],
                    "lease_token": existing["lease_token"],
                    "lease_id": existing.get("lease_id"),
                    "ticket": existing.get("ticket"),
                },
            )
            try:
                _save_resident_arm(spec.spool_path, spec.arm, state)
            except RemoteCIError as exc:
                return ArmOutcome(spec.arm, "submitted", 0, None, error=str(exc),
                                  remote_ticket_accepted=None)
            phase = "accepted"
        if phase in {"preparing", "submitting"}:
            try:
                bundle_path_value = state.get("bundle_path")
                if isinstance(bundle_path_value, str):
                    bundle_path = Path(bundle_path_value)
                else:
                    if (spec.snapshot_root is None or not spec.snapshot_ref
                            or spec.bundle_path is None):
                        raise RemoteCIError(
                            "box admission requires a durable candidate snapshot bundle"
                        )
                    basis = None
                    try:
                        basis_result = node.call(
                            "bundle-basis", repo=self.config.repo,
                            _transport_timeout=BUNDLE_BASIS_TIMEOUT_SECONDS,
                        )
                        candidate = (basis_result.get("commit")
                                     if isinstance(basis_result, Mapping) else None)
                        if isinstance(candidate, str):
                            basis = candidate
                    except RemoteCIError:
                        basis = None
                    bundle_path = _create_snapshot_bundle(
                        spec.snapshot_root, spec.snapshot_ref, spec.bundle_path,
                        spec.snapshot_sha or spec.sha, basis,
                        extra_refs=(
                            (f"refs/goalflight/remote-ci/{spec.request_id}/candidate",)
                            if (spec.arm == "base"
                                and spec.candidate_sha != (spec.snapshot_sha or spec.sha))
                            else ()
                        ),
                        deadline=time.monotonic() + BUNDLE_PREPARATION_TIMEOUT_SECONDS,
                    )
                    if bundle_path is None:
                        # submit requires a streamed body even when the box
                        # already confirms this exact snapshot.
                        basis = None
                        bundle_path = _create_snapshot_bundle(
                            spec.snapshot_root, spec.snapshot_ref, spec.bundle_path,
                            spec.snapshot_sha or spec.sha, None,
                            extra_refs=(
                                (f"refs/goalflight/remote-ci/{spec.request_id}/candidate",)
                                if (spec.arm == "base"
                                    and spec.candidate_sha != (spec.snapshot_sha or spec.sha))
                                else ()
                            ),
                            deadline=time.monotonic() + BUNDLE_PREPARATION_TIMEOUT_SECONDS,
                        )
                    if bundle_path is None:
                        raise RemoteCIError("could not build a candidate bundle for submit")
                    state.update(
                        basis_commit=basis.lower() if basis else None,
                        bundle_path=str(bundle_path),
                    )
                if not isinstance(state.get("command"), Mapping):
                    state["command"] = self._resident_command_spec(spec, node)
                state["phase"] = "submitting"
                _save_resident_arm(spec.spool_path, spec.arm, state)
            except (OSError, RemoteCIError, subprocess.SubprocessError, ValueError) as exc:
                state.update(phase="failed", error=str(exc), remote_ticket_accepted=None)
                with contextlib.suppress(RemoteCIError):
                    _save_resident_arm(spec.spool_path, spec.arm, state)
                return ArmOutcome(spec.arm, "setup-failed", 2, None, error=str(exc),
                                  remote_ticket_accepted=None)
            try:
                record = node.call(
                    "submit",
                    candidate_bundle_path=state["bundle_path"],
                    request_id=spec.request_id, arm=spec.arm,
                    owner=dict(owner), repo=self.config.repo,
                    max_concurrent_seats=node.box.max_concurrent_seats,
                    checkout_basename=self.config.checkout_basename,
                    python_interpreter=node.box.python_interpreter,
                    command=dict(state["command"]),
                    basis_commit=state.get("basis_commit"),
                )
            except RemoteSubmitRejectedError as exc:
                try:
                    existing = self._lookup_resident_submission(node, spec)
                except RemoteCIError as list_error:
                    return ArmOutcome(
                        spec.arm, "submitted", 0, None,
                        error=f"submit reply was ambiguous: {exc}; ticket lookup failed: {list_error}",
                        remote_ticket_accepted=None,
                    )
                if isinstance(existing, Mapping):
                    state.update(
                        phase="accepted",
                        ticket={
                            "run_dir": existing.get("run_directory"),
                            "lease_token": existing.get("lease_token"),
                            "lease_id": existing.get("lease_id"),
                            "ticket": existing.get("ticket"),
                        },
                    )
                    with contextlib.suppress(RemoteCIError):
                        _save_resident_arm(spec.spool_path, spec.arm, state)
                    return ArmOutcome(spec.arm, "submitted", 0, None,
                                      lease=dict(existing),
                                      remote_ticket_accepted=True)
                state.update(
                    phase="failed", error=str(exc), remote_ticket_accepted=False,
                )
                with contextlib.suppress(RemoteCIError):
                    _save_resident_arm(spec.spool_path, spec.arm, state)
                return ArmOutcome(spec.arm, "setup-failed", 2, None, error=str(exc),
                                  remote_ticket_accepted=False)
            except RemoteCIBundleTooLargeError as exc:
                state.update(
                    phase="failed", error=str(exc), remote_ticket_accepted=False,
                )
                with contextlib.suppress(RemoteCIError):
                    _save_resident_arm(spec.spool_path, spec.arm, state)
                return ArmOutcome(spec.arm, "setup-failed", 2, None, error=str(exc),
                                  remote_ticket_accepted=False)
            except RemoteCIError as exc:
                # A transport failure can arrive after the node published the
                # ticket. The saved submitting phase makes retry idempotent.
                return ArmOutcome(spec.arm, "submitted", 0, None, error=str(exc),
                                  remote_ticket_accepted=None)
            if (not isinstance(record, Mapping)
                    or not isinstance(record.get("run_directory"), str)
                    or not isinstance(record.get("lease_token"), str)):
                return ArmOutcome(spec.arm, "submitted", 0, None,
                                  error="submit response did not identify its ticket",
                                  remote_ticket_accepted=None)
            state.update(
                phase="accepted",
                ticket={
                    "run_dir": record["run_directory"],
                    "lease_token": record["lease_token"],
                    "lease_id": record.get("lease_id"),
                    "ticket": record.get("ticket"),
                },
            )
            try:
                _save_resident_arm(spec.spool_path, spec.arm, state)
            except RemoteCIError as exc:
                # The durable submitting checkpoint is sufficient to repeat
                # this request safely because node submit is idempotent.
                return ArmOutcome(spec.arm, "submitted", 0, None, error=str(exc),
                                  remote_ticket_accepted=None)
            bundle_bytes = None
            with contextlib.suppress(OSError, TypeError):
                bundle_bytes = Path(state["bundle_path"]).stat().st_size
            return ArmOutcome(spec.arm, "submitted", 0, None,
                              lease=dict(record), bundle_bytes=bundle_bytes,
                              remote_ticket_accepted=True)

        ticket = state.get("ticket")
        if not isinstance(ticket, Mapping):
            return ArmOutcome(spec.arm, "submitted", 0, None,
                              error="resident ticket checkpoint is not yet available",
                              remote_ticket_accepted=None)
        key = {
            "run_dir": str(ticket.get("run_dir") or ""),
            "lease_token": str(ticket.get("lease_token") or ""),
        }
        if not all(key.values()):
            return ArmOutcome(spec.arm, "submitted", 0, None,
                              error="resident ticket key is incomplete",
                              remote_ticket_accepted=None)
        if (state.get("cancel_acknowledged") is not True
                and _request_cancelled(
                    self.config, spec.request_id, spec.caller_identity,
                )):
            try:
                cancel_result = node.call("cancel", **key, expected_owner=dict(owner))
                state["cancel_requested"] = True
                if (isinstance(cancel_result, Mapping)
                        and cancel_result.get("status") == "cancelled"):
                    state["cancel_acknowledged"] = True
                _save_resident_arm(spec.spool_path, spec.arm, state)
            except RemoteCIError as exc:
                return ArmOutcome(spec.arm, "submitted", 0, None, error=str(exc),
                                  remote_ticket_accepted=True)
        try:
            record = node.call(
                "status", **key, result_tail_bytes=self.config.result_tail_kib * 1024,
                _transport_timeout=REMOTE_STATUS_TIMEOUT_SECONDS,
            )
            if not isinstance(record, Mapping):
                raise RemoteCIError("resident status response was not an object")
            outcome = self._resident_terminal_outcome(spec, node, key, record)
            if outcome is not None:
                return outcome
            if state.get("phase") != "collecting":
                state["phase"] = "collecting"
                _save_resident_arm(spec.spool_path, spec.arm, state)
            return ArmOutcome(spec.arm, "submitted", 0, None,
                              lease=dict(record), remote_ticket_accepted=True)
        except RemoteCIError as exc:
            # Sleep and a disconnected SSH session are expected for resident
            # tickets. Keep ownership and retry on the next daemon pass.
            return ArmOutcome(spec.arm, "submitted", 0, None, error=str(exc),
                              remote_ticket_accepted=True)

    def run_arm(
        self, spec: ArmSpec, *, admission_deadline: float | None = None,
    ) -> ArmOutcome:
        if _request_cancelled(self.config, spec.request_id, spec.caller_identity):
            if (self.config.admission_mode == "box"
                    and _load_resident_arm(spec.spool_path, spec.arm)):
                return self._run_resident_arm(spec)
            return ArmOutcome(spec.arm, "cancelled", 130, None, cancelled=True)
        resident_state = (
            _load_resident_arm(spec.spool_path, spec.arm)
            if self.config.admission_mode == "box" else {}
        )
        collecting_accepted_ticket = resident_state.get("phase") in {
            "submitting", "accepted", "collecting",
        }
        command_override = self.config.runner.command_overrides.get(spec.command_id or "")
        profile_enabled = self.config.profile_enabled
        if self.config.profile_enabled and not collecting_accepted_ticket:
            try:
                snapshot_files = None
                profile_root = _profile_root(self.config)
                if spec.snapshot_root is not None and spec.snapshot_sha:
                    profile_root = spec.snapshot_root
                    snapshot_files = frozenset(filter(None, _git_output(
                        profile_root,
                        ["ls-tree", "-r", "--name-only", "-z", spec.snapshot_sha],
                    ).split("\0")))
                if spec.command is not None:
                    selected = _profile_command_files(
                        spec.command, label="profiled command",
                        generated_node_id=spec.generated_node_id,
                    )
                    for path in selected:
                        _profile_file_identity(
                            path, repo_root=profile_root,
                            cwd_relative=spec.cwd_relative,
                            label="profile test selector",
                            snapshot_files=snapshot_files,
                        )
                else:
                    _profile_request_files({
                        "selection": spec.selection,
                        "test_files": spec.test_files,
                        "cwd_relative": spec.cwd_relative,
                    }, self.config, snapshot_files=snapshot_files)
            except ProfileIneligible as exc:
                return ArmOutcome(
                    spec.arm, "setup-failed", 2, None,
                    error=str(exc),
                )
            except RemoteCIError as exc:
                return ArmOutcome(
                    spec.arm, "setup-failed", 2, None,
                    error=str(exc),
                )
        if self.config.admission_mode == "box":
            return self._run_resident_arm(spec)
        node = self.nodes[spec.box]
        # An explicit box choice is a placement pin, not proof that the alias
        # still names the configured physical Studio. Verify on every run.
        deadline = (
            admission_deadline if admission_deadline is not None
            else time.monotonic() + self.config.admission.max_wait_seconds
        )
        placement = self.choose_box(
            requested=spec.box, deadline=deadline, _with_host=True,
        )
        if not isinstance(placement, BoxPlacement):
            raise RemoteCIError("box selection did not return a measured destination")
        if placement.box_name != spec.box:
            raise RemoteCIError("box selection changed the requested placement")
        # Keep every operation for this lease on the server verified during
        # placement; lease identity is checked before work starts.
        node = self.nodes[placement.box_name].with_pinned_host(placement.peer_address)
        key: dict[str, str] | None = None
        started = False
        expected_owner = self._owner()
        try:
            if _request_cancelled(self.config, spec.request_id, spec.caller_identity):
                return ArmOutcome(spec.arm, "cancelled", 130, None, cancelled=True)
            enqueue_args = {
                "request_id": spec.request_id, "arm": spec.arm,
                "owner": expected_owner, "repo": self.config.repo,
                "max_concurrent_seats": node.box.max_concurrent_seats,
                "checkout_basename": self.config.checkout_basename,
            }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ArmOutcome(spec.arm, "capacity", 75, None)
            queue_wait_seconds = min(
                self.config.admission.max_wait_seconds, remaining,
            )
            node_queue_wait_seconds = (
                0 if self.config.fallback == "fail" else queue_wait_seconds
            )
            enqueue_args["queue_wait_seconds"] = node_queue_wait_seconds
            record = node.call(
                "enqueue",
                _transport_timeout=(
                    node_queue_wait_seconds + REMOTE_TRANSPORT_ALLOWANCE_SECONDS
                ),
                **enqueue_args,
            )
            key = {"run_dir": record["run_directory"], "lease_token": record["lease_token"]}
            self._inflight.add((key["run_dir"], key["lease_token"]))
            self._node_for_lease(node, record)
            if (record.get("state") == "queued"
                    and time.monotonic() >= deadline):
                record = self._status(
                    node, key, phase="post-enqueue admission deadline status",
                )
            first_status_after_enqueue = True
            while True:
                self._node_for_lease(node, record)
                completed = record.get("result")
                if isinstance(completed, Mapping) and completed.get("status") == "capacity-refused":
                    return ArmOutcome(spec.arm, "capacity", 75, None, lease=record)
                if isinstance(completed, Mapping):
                    started = True
                    return self._watch(spec, node, key, expected_owner)
                if record["state"] == "admitted":
                    break
                if record.get("command_present") or record["state"] in {"running", "draining", "released"}:
                    started = True
                    break
                if (record.get("state") == "queued"
                        and time.monotonic() >= deadline):
                    # The holder can admit between this status and release.
                    # Release and start both hold command.lock, so if release
                    # wins, this only falls back for a run that never started.
                    released = node.call(
                        "release", **key, expected_owner=expected_owner,
                        _transport_timeout=REMOTE_TRANSPORT_ALLOWANCE_SECONDS,
                    )
                    if not isinstance(released, Mapping) or released.get("status") != "releasing":
                        raise AmbiguousRemoteError(
                            f"could not confirm queued run {spec.request_id}/{spec.arm} was withdrawn"
                        )
                    return ArmOutcome(spec.arm, "capacity", 75, None, lease=record)
                if (not first_status_after_enqueue
                        and not record.get("holder_alive")):
                    raise AdmissionError("node admission holder exited before admission")
                if first_status_after_enqueue:
                    first_status_after_enqueue = False
                else:
                    self._recover_other_forgotten(node, deadline=deadline)
                    # This status check crosses SSH. Keep controller polling at
                    # the configured steady interval; token-file retries happen
                    # locally on the node and stay on their separate fast path.
                    self.sleeper(min(
                        self.config.poll_seconds,
                        max(0.0, deadline - time.monotonic()),
                    ))
                record = self._status(node, key, phase="post-enqueue status")
            # Token ownership precedes rendering, checkout, push and test work.
            values = {
                "box": spec.box, "host": node.box.host, "arm": spec.arm,
                "sha": spec.sha, "tip_sha": spec.tip_sha, "candidate_sha": spec.candidate_sha,
                "request_id": spec.request_id, "run_id": record["lease_id"],
                "lease_id": record["lease_id"], "lease_token": record["lease_token"],
                "owner_identity": record["owner_identity"], "run_dir": key["run_dir"],
                "managed_run_directory": str(node.box.managed_run_directory),
                "token_index": str(record["token_index"]), "overlay": "1" if spec.overlay else "0",
                "slot_dir": str(record.get("slot") or ""),
                "checkout_dir": str(record.get("checkout_dir") or record.get("slot") or ""),
            }
            command = (
                list(spec.command)
                if spec.command is not None
                else ([*self.config.runner.command, *spec.selection]
                      if profile_enabled
                      else expand_command(
                          self.config.runner.command, values,
                          selection=spec.selection, test_files=spec.test_files,
                          test_command=self.config.runner.test_command,
                      ))
            )
            profile_env = {}
            if profile_enabled and self.config.checkout_basename:
                profile_paths = [str(Path(values["checkout_dir"]).parent)]
                box_pythonpath = node.box.env.get("PYTHONPATH", "")
                if box_pythonpath:
                    profile_paths.append(box_pythonpath)
                profile_env["PYTHONPATH"] = os.pathsep.join(profile_paths)
            env = _build_command_environment(
                spec.caller_env, self.config.runner.env, node.box.env,
                profile_enabled=profile_enabled,
                profile_env=profile_env,
            )
            env.update({"GOALFLIGHT_REMOTE_CI_" + name.upper(): value for name, value in values.items()})
            environment_recipe = dict(self.config.environment)
            if "interpreter" in environment_recipe:
                environment_recipe["interpreter"] = _expand_profile_value(
                    environment_recipe["interpreter"], node.box.env,
                    "environment.interpreter",
                )
                environment_recipe["version_probe"] = [
                    _expand_profile_value(value, node.box.env, "environment.version_probe")
                    for value in environment_recipe["version_probe"]
                ]
            base_overlays = [
                {
                    **overlay,
                    "source": _expand_profile_value(
                        overlay["source"], node.box.env,
                        f"base_overlays[{index}].source",
                    ),
                }
                for index, overlay in enumerate(self.config.base_overlays)
            ]
            env.update({
                "GOALFLIGHT_REMOTE_CI_TEST_FILES_JSON": json.dumps(list(spec.test_files)),
                "GOALFLIGHT_REMOTE_CI_OVERLAY_PATHS_JSON": json.dumps(
                    list(self.config.selection.path_prefixes)),
                "GOALFLIGHT_REMOTE_CI_SELECTION_JSON": json.dumps(list(spec.selection)),
                "GOALFLIGHT_REMOTE_CI_TEST_COMMAND_JSON": json.dumps(list(self.config.runner.test_command)),
                "GOALFLIGHT_REMOTE_CI_LEASE_RECORD_JSON": json.dumps(record),
                self.config.runner.self_cap_env: str(min(self.config.runner.self_cap,
                                                        record["sample"].get("self_cap", self.config.runner.self_cap))),
                "GOALFLIGHT_REMOTE_CI_REPO_URL": self.config.repo_url or "",
                "GOALFLIGHT_REMOTE_CI_ENVIRONMENT_JSON": json.dumps(environment_recipe),
                "GOALFLIGHT_REMOTE_CI_PROFILE_ENABLED": (
                    "1" if profile_enabled else "0"
                ),
                "GOALFLIGHT_REMOTE_CI_RECEIPT_PROBES_JSON": json.dumps({
                    name: {
                        **probe,
                        "command": [
                            _expand_profile_value(value, node.box.env,
                                                  f"receipt_probes.{name}.command")
                            for value in probe["command"]
                        ],
                        **({"expected": _expand_profile_value(
                            probe["expected"], node.box.env,
                            f"receipt_probes.{name}.expected",
                        )} if "expected" in probe else {}),
                    }
                    for name, probe in self.config.receipt_probes.items()
                }),
                "GOALFLIGHT_REMOTE_CI_BASE_OVERLAYS_JSON": json.dumps(base_overlays),
                "GOALFLIGHT_REMOTE_CI_PYTHON_INTERPRETER": node.box.python_interpreter,
                "GOALFLIGHT_REMOTE_CI_CHECKOUT_BASENAME": self.config.checkout_basename,
                "GOALFLIGHT_REMOTE_CI_EXPECTED_HOSTNAME": node.box.expected_hostname or "",
                "GOALFLIGHT_REMOTE_CI_HEAD_SHA": spec.head_sha or spec.sha,
                "GOALFLIGHT_REMOTE_CI_SNAPSHOT_SHA": spec.snapshot_sha or spec.sha,
                "GOALFLIGHT_REMOTE_CI_DIRTY": "true" if spec.dirty else "false",
                "GOALFLIGHT_REMOTE_CI_COMMAND_ID": spec.command_id or "",
                **({"GOALFLIGHT_REMOTE_CI_COMMAND_OVERRIDE_ID": spec.command_id}
                   if command_override is not None else {}),
                "GOALFLIGHT_REMOTE_CI_ROUTE_REASON": spec.route_reason or "heavy-command",
                "GOALFLIGHT_REMOTE_CI_EMIT_RECEIPT": (
                    "1" if spec.command is not None or profile_enabled or command_override else "0"
                ),
                "GOALFLIGHT_REMOTE_CI_QUEUE_WAIT_SECONDS": str(self.config.admission.queue_wait_seconds),
                "GOALFLIGHT_REMOTE_CI_PROJECT_ID": self.config.repo,
                "GOALFLIGHT_REMOTE_CI_POLICY_OUTCOME": (
                    "queued" if record.get("queue_waited") is True else "ran_remote"
                ),
            })
            bundle_path = spec.bundle_path
            if (spec.snapshot_root is not None and spec.snapshot_ref
                    and spec.bundle_path is not None):
                basis = None
                try:
                    response = node.call(
                        "bundle-basis", repo=self.config.repo,
                        lease_id=record["lease_id"],
                        lease_token=record["lease_token"],
                        _transport_timeout=BUNDLE_BASIS_TIMEOUT_SECONDS,
                    )
                    candidate = response.get("commit") if isinstance(response, Mapping) else None
                    if isinstance(candidate, str):
                        basis = candidate
                except RemoteCIError:
                    # An unreadable basis requires a self-contained bundle.
                    basis = None
                bundle_deadline = (
                    time.monotonic() + BUNDLE_PREPARATION_TIMEOUT_SECONDS
                )
                bundle_path = _create_snapshot_bundle(
                    spec.snapshot_root, spec.snapshot_ref, spec.bundle_path,
                    spec.snapshot_sha or spec.sha, basis,
                    deadline=bundle_deadline,
                )
            if bundle_path is not None:
                try:
                    bundle_bytes = bundle_path.stat().st_size
                except OSError:
                    bundle_bytes = None
                try:
                    remote_bundle = node.transfer_bundle(bundle_path, key["run_dir"])
                except RemoteCIError as exc:
                    return ArmOutcome(spec.arm, "setup-failed", 2, None,
                                      error=str(exc), lease=record,
                                      bundle_bytes=bundle_bytes)
                env["GOALFLIGHT_REMOTE_CI_SOURCE_BUNDLE"] = remote_bundle
            else:
                bundle_bytes = 0
            if not record.get("command_present"):
                self._node_for_lease(node, record)
                if _request_cancelled(self.config, spec.request_id, spec.caller_identity):
                    return ArmOutcome(
                        spec.arm, "cancelled", 130, None, cancelled=True, lease=record,
                    )
                started = True
                slot = Path(record.get("slot") or "")
                checkout = Path(record.get("checkout_dir") or slot)
                cwd = checkout / spec.cwd_relative if spec.cwd_relative else checkout
                command_spec = {
                    "argv": command, "env": env,
                    "timeout": self.config.runner.timeout_seconds,
                    "cwd": str(cwd),
                    "emit_receipt": (
                        spec.command is not None or profile_enabled or command_override is not None
                    ),
                }
                if command_override is not None:
                    command_spec["env_set"] = command_override.env_set
                    command_spec["env_unset"] = list(command_override.env_unset)
                node.call("start", **key, command=command_spec)
            else:
                started = True
            outcome = self._watch(spec, node, key, expected_owner)
            return replace(outcome, bundle_bytes=bundle_bytes)
        except BaseException as exc:
            if isinstance(exc, RemoteCIError):
                cleanup_timeout = (
                    REMOTE_TRANSPORT_ALLOWANCE_SECONDS if not started else 30.0
                )
                self._cancel_best_effort(
                    node, key, expected_owner, spec.request_id, spec.arm,
                    transport_timeout=cleanup_timeout,
                )
                raise AmbiguousRemoteError(
                    f"remote run {spec.request_id}/{spec.arm} has no clean verified outcome: {exc}"
                ) from exc
            raise
        finally:
            if key is not None:
                try:
                    if not started:
                        # The holder is already forked. If it has admitted,
                        # cancel the unstarted lease; queued runs are withdrawn.
                        identity = record.get("remote_run")
                        if isinstance(identity, Mapping):
                            node.call(
                                "cancel", **key, identity=identity,
                                expected_owner=expected_owner,
                                _transport_timeout=REMOTE_TRANSPORT_ALLOWANCE_SECONDS,
                            )
                        else:
                            node.call(
                                "release", **key, expected_owner=expected_owner,
                                _transport_timeout=REMOTE_TRANSPORT_ALLOWANCE_SECONDS,
                            )
                except RemoteCIError:
                    pass
                finally:
                    self._inflight.discard((key["run_dir"], key["lease_token"]))

    def _watch(self, spec: ArmSpec, node: RemoteNode, key: dict[str, str],
               expected_owner: Mapping[str, Any]) -> ArmOutcome:
        phase = "setup"
        phase_budgets = _remote_phase_budgets(self.config)
        phase_deadline = time.monotonic() + phase_budgets[phase]
        execution_timed_out = False
        timeout_cancelled = False
        cancel_requested = False
        while True:
            record = self._status(node, key, phase="watch status")
            raw_identity = record.get("remote_run")
            identity = (RemoteRunIdentity.from_mapping(raw_identity)
                        if isinstance(raw_identity, Mapping) else None)
            if getattr(node, "pinned_host", None) is not None:
                self._node_for_lease(node, record)
            if (_request_cancel_path(self.config, spec.request_id).exists()
                    and not cancel_requested):
                try:
                    if identity is not None:
                        result = node.call("cancel", **key, identity=identity.to_dict(),
                                           expected_owner=dict(expected_owner))
                    else:
                        result = node.call("release", **key,
                                           expected_owner=dict(expected_owner))
                    timeout_cancelled = result.get("status") in {"cancelled", "finished"}
                except RemoteCIError:
                    pass
                cancel_requested = True
                phase = "finalization"
                phase_deadline = time.monotonic() + phase_budgets[phase]
            completed = record.get("result")
            if completed is not None:
                kind = completed.get("status")
                setup_failure_type = completed.get("setup_failure_type")
                if not isinstance(setup_failure_type, str) or not setup_failure_type:
                    setup_failure_type = None
                if kind == "capacity-refused":
                    return ArmOutcome(spec.arm, "capacity", 75, None, identity, lease=record)
                if kind == "cancelled":
                    if execution_timed_out:
                        return ArmOutcome(spec.arm, "timeout", 124, None, identity,
                                          timed_out=True, cancelled=True,
                                          lease=record)
                    return ArmOutcome(spec.arm, "cancelled", int(completed.get("returncode", 130)),
                                      None, identity, cancelled=True,
                                      lease=record)
                if kind == "deadline" or completed.get("timed_out"):
                    return ArmOutcome(spec.arm, "timeout", 124, None, identity,
                                      timed_out=True, cancelled=True,
                                      lease=record)
                exit_known = completed.get("exit_known") is True
                command_started = completed.get("command_started") is True
                completed_error = completed.get("error")
                if not isinstance(completed_error, str) or not completed_error.strip():
                    completed_error = None
                streams = completed.get("streams")
                stderr = streams.get("stderr") if isinstance(streams, Mapping) else None
                tail = stderr.get("tail") if isinstance(stderr, Mapping) else None
                stderr_tail = tail.get("text") if isinstance(tail, Mapping) else None
                if not isinstance(stderr_tail, str) or not stderr_tail.strip():
                    stderr_tail = None
                else:
                    stderr_tail = stderr_tail.strip()
                    if tail.get("truncated") is True:
                        stderr_tail = f"{stderr_tail} (stderr tail truncated)"
                setup_error = completed_error or stderr_tail or "remote setup failed"
                if ((kind == "setup-failed" or not exit_known)
                        and not command_started):
                    return ArmOutcome(spec.arm, "setup-failed", 2, None, identity,
                                      error=setup_error,
                                      lease=record,
                                      setup_failure_type=setup_failure_type)
                omitted: list[str] = []
                try:
                    streams = completed.get("streams")
                    if not isinstance(streams, Mapping):
                        raise ReceiptError("node result omitted bounded stdout/stderr receipts")
                    output_parts: list[str] = []
                    for name in ("stdout", "stderr"):
                        stream = streams.get(name)
                        tail = stream.get("tail") if isinstance(stream, Mapping) else None
                        if not isinstance(tail, Mapping) or not isinstance(tail.get("text"), str):
                            raise ReceiptError(f"node result has an invalid {name} tail receipt")
                        output_parts.append(tail["text"])
                        if tail.get("truncated") is True:
                            omitted_bytes = tail.get("omitted_bytes")
                            if isinstance(omitted_bytes, int) and omitted_bytes > 0:
                                omitted.append(f"{name} omitted {omitted_bytes} bytes")
                    receipt = receipt_from_output("\n".join(output_parts))
                    execution_argv = (
                        spec.command if spec.command is not None
                        else self.config.runner.command
                    )
                    sampled_hostname = record.get("sample", {}).get("hostname")
                    if (not isinstance(sampled_hostname, str)
                            or self._normalize_hostname(sampled_hostname)
                            != self._normalize_hostname(receipt.hostname)):
                        raise ReceiptError("receipt hostname differs from the measured node")
                    receipt = replace(
                        receipt,
                        metadata={
                            **receipt.metadata,
                            "box_id": spec.box,
                            "expected_hostname": self.config.boxes[spec.box].expected_hostname,
                            "measured_hostname": sampled_hostname,
                            **_daemon_config_receipt_metadata(self.config),
                            **_execution_receipt_metadata(
                                execution_argv, spec.selection, spec.caller_env,
                                spec.receipt_metadata,
                            ),
                        },
                    )
                except ReceiptError as exc:
                    if "hostname differs" in str(exc):
                        return ArmOutcome(spec.arm, "setup-failed", 2, None, identity,
                                          error=str(exc), lease=record,
                                          setup_failure_type=setup_failure_type)
                    if omitted:
                        exc = ReceiptError(f"{exc}; truncated output: {', '.join(omitted)}")
                    if stderr_tail and stderr_tail not in str(exc):
                        exc = ReceiptError(f"{exc}; stderr tail: {stderr_tail}")
                    return ArmOutcome(spec.arm, "setup-failed", 2, None, identity,
                                      error=str(exc), lease=record,
                                      setup_failure_type=setup_failure_type)
                if ((kind == "setup-failed" or not exit_known)
                        and not command_started):
                    return ArmOutcome(spec.arm, "setup-failed", 2, None, identity,
                                      error=setup_error,
                                      lease=record,
                                      setup_failure_type=setup_failure_type)
                if kind == "setup-failed" or not exit_known:
                    return ArmOutcome(spec.arm, "setup-failed", 2, None, identity,
                                      error=completed_error or stderr_tail or "remote exit status unknown",
                                      lease=record,
                                      setup_failure_type=setup_failure_type)
                returncode = completed["returncode"]
                status = (
                    "green" if returncode == 0
                    and _receipt_is_green(receipt) else "red"
                )
                return ArmOutcome(spec.arm, status, returncode, receipt, identity, lease=record)
            now = time.monotonic()
            state = record.get("state")
            if phase == "setup" and (
                    (state == "running" and record.get("command_started") is True)
                    or (state is None and identity is not None)):
                phase = "execution"
                phase_deadline = now + self.config.runner.timeout_seconds
            if state in {"draining", "released"} and phase != "finalization":
                phase = "finalization"
                phase_deadline = now + phase_budgets[phase]
            if identity is None:
                if not record.get("holder_alive"):
                    raise AmbiguousRemoteError(
                        "node holder died before publishing a remote run identity or result")
                if phase == "setup" and now >= phase_deadline:
                    raise AmbiguousRemoteError(
                        "remote setup exceeded its 300 second wait deadline")
                if phase == "execution" and now >= phase_deadline:
                    raise AmbiguousRemoteError(
                        "remote execution timed out before its identity could be verified")
                if phase == "finalization" and now >= phase_deadline:
                    if execution_timed_out:
                        return ArmOutcome(
                            spec.arm, "timeout", 124, None, identity,
                            timed_out=True, cancelled=timeout_cancelled, lease=record,
                            error=None if timeout_cancelled
                            else "remote cancellation identity unknown",
                        )
                    if cancel_requested:
                        return ArmOutcome(
                            spec.arm, "cancelled", 130, None, identity,
                            cancelled=True,
                            error="remote cancellation did not finish before its deadline",
                            lease=record,
                        )
                    raise AmbiguousRemoteError(
                        "remote finalization exceeded its 300 second wait deadline")
                self.sleeper(self.config.poll_seconds)
                continue
            if not record["holder_alive"]:
                raise RemoteCIError("node holder died without a result; remote identity is unknown")
            if phase == "setup" and now >= phase_deadline:
                raise AmbiguousRemoteError("remote setup exceeded its 300 second deadline")
            if phase == "execution" and now >= phase_deadline:
                result = node.call("cancel", **key, identity=identity.to_dict(),
                                   expected_owner=dict(expected_owner))
                timeout_cancelled = result["status"] in {"cancelled", "finished"}
                execution_timed_out = True
                phase = "finalization"
                phase_deadline = now + phase_budgets[phase]
            elif phase == "finalization" and now >= phase_deadline:
                if execution_timed_out:
                    return ArmOutcome(
                        spec.arm, "timeout", 124, None, identity,
                        timed_out=True, cancelled=timeout_cancelled, lease=record,
                        error=None if timeout_cancelled else "remote cancellation identity unknown",
                    )
                if cancel_requested:
                    return ArmOutcome(
                        spec.arm, "cancelled", 130, None, identity,
                        cancelled=True,
                        error="remote cancellation did not finish before its deadline",
                        lease=record,
                    )
                raise AmbiguousRemoteError(
                    "remote finalization exceeded its 300 second wait deadline")
            self.sleeper(self.config.poll_seconds)

    def reap(self) -> list[dict[str, Any]]:
        results = []
        for name, node in self.nodes.items():
            # The list call discovers durable routes; lease operations below
            # use destination_address from each record.
            for record in node.call("list"):
                if record.get("state") == "UNREADABLE":
                    results.append({
                        "box": name, "lease_id": None, "status": "unreadable",
                        "path": record.get("path"),
                    })
                    continue
                if record["state"] == "released":
                    continue
                if record.get("resident_submission") is True:
                    results.append({
                        "box": name, "lease_id": record.get("lease_id"),
                        "status": "resident", "request_id": record.get("request_id"),
                    })
                    continue
                try:
                    lease_node = self._node_for_lease(node, record)
                except AmbiguousRemoteError:
                    results.append({
                        "box": name, "lease_id": record["lease_id"],
                        "status": "unresolved",
                    })
                    continue
                status = "unknown"
                # A PID from another controller host proves nothing locally.
                alive = self._owner_alive(record)
                forgotten = (
                    alive is True
                    and self._owns(record)
                    and self._lease_key(record) not in self._inflight
                )
                if forgotten:
                    status = self._release_forgotten(lease_node, record)
                elif alive is True:
                    status = "owned"
                elif alive is False and record.get("remote_run"):
                    identity = RemoteRunIdentity.from_mapping(record["remote_run"])
                    status = lease_node.call(
                        "cancel", run_dir=record["run_directory"],
                        lease_token=record["lease_token"],
                        identity=identity.to_dict(),
                        expected_owner={key: record.get(key) for key in
                                        ("owner_host", "owner_pid", "owner_start_token",
                                         "owner_identity")},
                    )["status"]
                results.append({"box": name, "lease_id": record["lease_id"], "status": status})
        return results


def _receipt_has_test_nodes(receipt: RemoteReceipt) -> bool:
    # Node counts come from the runner's per-run JUnit file; stdout summaries
    # remain diagnostics and cannot make echoed or stale output look green.
    if receipt.collection_import_errors:
        return False
    return bool(
        receipt.passed or receipt.failed or receipt.skipped or receipt.errors
    )


def _receipt_is_green(receipt: RemoteReceipt) -> bool:
    return (
        receipt.status.lower() in {"pass", "passed", "green"}
        and receipt.failed == 0
        and receipt.errors == 0
        and not receipt.collection_import_errors
        and not receipt.failing_node_ids
        and _receipt_has_test_nodes(receipt)
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
    box_value = request.get("box")
    if not isinstance(box_value, str) or box_value not in config.boxes:
        raise RemoteCIError("matched arms must be pinned to a selected box")
    box = box_value
    selection = tuple(str(value) for value in request["selection"])
    base_selection = list(selection)
    if request.get("command") is None and config.runner.base_collection_option not in base_selection:
        if config.profile_enabled:
            base_selection.insert(0, config.runner.base_collection_option)
        else:
            base_selection.append(config.runner.base_collection_option)
    test_files = tuple(str(value) for value in request["test_files"])
    command = tuple(str(value) for value in request["command"]) if request.get("command") is not None else None
    command_id = str(request["command_id"]) if request.get("command_id") is not None else None
    route_reason = str(request["route_reason"]) if request.get("route_reason") is not None else None
    common = {
        "tip_sha": str(request["tip_sha"]),
        "candidate_sha": str(request["candidate_sha"]),
        "test_files": test_files,
        "request_id": str(request["request_id"]),
        "box": box,
        "command": command,
        "command_id": command_id,
        "route_reason": route_reason,
        "head_sha": str(request.get("head_sha", request["candidate_sha"])),
        "dirty": bool(request.get("dirty", False)),
        "cwd_relative": str(request.get("cwd_relative", "")),
        "caller_env": dict(request.get("caller_env", {})),
        "receipt_metadata": dict(request.get("receipt_metadata", {})),
        "caller_identity": request.get("caller_identity"),
        "spool_path": Path(request["_spool_path"]) if request.get("_spool_path") else None,
    }
    snapshot_root = Path(request["snapshot_root"]) if request.get("snapshot_root") else None
    if (config.admission_mode != "box"
            and not _request_has_resident_ticket_checkpoint(request)):
        # Blocking mode historically runs BASE against the same candidate
        # snapshot and bundle as CANDIDATE, then overlays the candidate tests.
        common.update(
            snapshot_sha=str(request.get("snapshot_sha", common["candidate_sha"])),
            bundle_path=(Path(request["bundle_path"])
                         if request.get("bundle_path") else None),
            snapshot_root=snapshot_root,
            snapshot_ref=(str(request["snapshot_ref"])
                          if request.get("snapshot_ref") else None),
        )
        base_values = common
        candidate_values = common
    else:
        base_values = {
            **common,
            "snapshot_sha": str(request.get("base_snapshot_sha", common["tip_sha"])),
            "bundle_path": (Path(request["base_bundle_path"])
                            if request.get("base_bundle_path") else None),
            "snapshot_root": snapshot_root,
            "snapshot_ref": (str(request["base_snapshot_ref"])
                             if request.get("base_snapshot_ref") else None),
        }
        candidate_values = {
            **common,
            "snapshot_sha": str(request.get("snapshot_sha", common["candidate_sha"])),
            "bundle_path": (Path(request["bundle_path"])
                            if request.get("bundle_path") else None),
            "snapshot_root": snapshot_root,
            "snapshot_ref": (str(request["snapshot_ref"])
                             if request.get("snapshot_ref") else None),
        }
    return (
        ArmSpec(arm="base", sha=common["tip_sha"], selection=tuple(base_selection),
                overlay=request.get("command") is None, **base_values),
        ArmSpec(
            arm="candidate", sha=common["candidate_sha"], selection=selection,
            overlay=False,
            **candidate_values,
        ),
    )


def _command_for_node(
    spec: ArmSpec, node_id: str, rules: SelectionRules, *, repo_root: str,
) -> tuple[str, ...]:
    command: list[str] = []
    waiting_for_value = False
    for value in spec.command if spec.command is not None else spec.selection:
        if waiting_for_value:
            command.append(value)
            waiting_for_value = False
            continue
        if value in rules.value_options:
            command.append(value)
            waiting_for_value = True
            continue
        if value.startswith("-"):
            command.append(value)
            continue
        path = value.split("::", 1)[0].replace("\\", "/")
        if posixpath.isabs(path):
            path = posixpath.relpath(path, repo_root)
        else:
            path = posixpath.normpath(posixpath.join(spec.cwd_relative, path))
        if path not in spec.test_files:
            command.append(value)
    command.append(node_id)
    return tuple(command)


def _publish_pair_recheck_count(request: Mapping[str, Any], runner: RemoteRunner, count: int) -> None:
    request_id = str(request["request_id"])
    queued = runner.config.queue_dir / f"{request_id}.json"
    if queued.is_file():
        selected = json.loads(queued.read_text(encoding="utf-8"))
        selected["_pair_rechecked_ids"] = count
        _atomic_write_json(queued, selected)


def _pin_request_box(
    request: Mapping[str, Any], runner: RemoteRunner, *, excluding: Sequence[str] = (),
    deadline: float | None = None,
) -> dict[str, Any]:
    selected = dict(request)
    if selected.get("box") is not None and selected["box"] not in excluding:
        return selected
    if deadline is not None:
        box = runner.choose_box(excluding=excluding, deadline=deadline)
    else:
        box = runner.choose_box(excluding=excluding) if excluding else runner.choose_box()
    selected["box"] = box
    request_id = str(selected["request_id"])
    queued = runner.config.queue_dir / f"{request_id}.json"
    if queued.is_file():
        # Keep both arms of the matched request pinned to one measured box.
        _persist_daemon_request(queued, selected)
    return selected


def _run_arm_with_placement_retries(
    request: Mapping[str, Any], runner: RemoteRunner, spec: ArmSpec,
    *, deadline: float,
) -> ArmOutcome:
    tried: set[str] = set()
    selected = dict(request)
    selected["box"] = spec.box
    boxes = max(1, len(runner.config.boxes))
    current = spec
    unknown_health: HealthProbeUnknown | None = None
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        available = max(1, boxes - len(tried))
        attempt_deadline = min(deadline, time.monotonic() + remaining / available)
        try:
            outcome = runner.run_arm(current, admission_deadline=attempt_deadline)
        except HealthProbeUnknown as exc:
            unknown_health = exc
            outcome = None
        except NoHealthyBox:
            outcome = None
        if outcome is not None and outcome.status != "capacity":
            return outcome
        tried.add(current.box)
        if time.monotonic() >= deadline or len(tried) >= boxes:
            if outcome is not None:
                if unknown_health is not None and outcome.status == "capacity":
                    raise unknown_health
                return outcome
            if unknown_health is not None:
                raise unknown_health
            raise NoHealthyBox("no Studio with headroom remains after placement retries")
        try:
            selected = _pin_request_box(
                selected, runner, excluding=tuple(sorted(tried)), deadline=deadline,
            )
        except NoHealthyBox:
            if unknown_health is not None:
                raise unknown_health
            raise
        current = replace(current, box=str(selected["box"]))


def _request_admission_deadline(
    request: Mapping[str, Any], runner: RemoteRunner,
) -> float:
    deadline = request.get("_admission_deadline")
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        return float(deadline)
    return time.monotonic() + runner.config.admission.max_wait_seconds


def _run_request_arm(
    request: Mapping[str, Any], runner: RemoteRunner, spec: ArmSpec, *,
    deadline: float,
) -> ArmOutcome:
    if runner.config.admission_mode == "box":
        return runner.run_arm(spec)
    if _request_has_resident_ticket_checkpoint(request):
        return runner._run_resident_arm(spec)
    return _run_arm_with_placement_retries(request, runner, spec, deadline=deadline)


def _arm_acceptance(*outcomes: ArmOutcome) -> bool | None:
    if any(outcome.remote_ticket_accepted is True for outcome in outcomes):
        return True
    if any(outcome.remote_ticket_accepted is None for outcome in outcomes):
        return None
    return False


def _submitted_response(request_id: str, **arms: ArmOutcome) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "request_id": request_id,
        "status": "SUBMITTED",
        **{name: outcome.to_dict() for name, outcome in arms.items()},
        "remote_ticket_accepted": _arm_acceptance(*arms.values()),
    }


def run_matched_pair(request: Mapping[str, Any], runner: RemoteRunner) -> dict[str, Any]:
    admission_deadline = _request_admission_deadline(request, runner)
    resident_recovery = _request_has_resident_ticket_checkpoint(request)
    if (runner.config.admission_mode != "box" and not resident_recovery
            and admission_deadline <= time.monotonic()):
        return {
            "schema": RESULT_SCHEMA, "request_id": request["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": "queue-deadline-exceeded",
        }
    selected = _pin_request_box(request, runner, deadline=admission_deadline)
    base, candidate = build_pair_specs(selected, runner.config)
    if runner.config.admission_mode == "box":
        base_outcome = runner.run_arm(base)
    elif resident_recovery:
        base_outcome = runner._run_resident_arm(base)
    else:
        base_outcome = runner.run_arm(base, admission_deadline=admission_deadline)
    if base_outcome.status == "submitted":
        return _submitted_response(str(request["request_id"]), base=base_outcome)
    if base_outcome.status not in {"green", "red"}:
        result = {
            "schema": RESULT_SCHEMA, "request_id": request["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": f"base-{base_outcome.status}",
            "base": base_outcome.to_dict(),
        }
        if runner.config.admission_mode == "box" or resident_recovery:
            result["remote_ticket_accepted"] = _arm_acceptance(base_outcome)
        error = _setup_failure_detail(base_outcome)
        if error is not None:
            result["error"] = error
        return result
    candidate_deadline = (
        time.monotonic() + runner.config.admission.max_wait_seconds
    )
    if runner.config.admission_mode == "box":
        candidate_outcome = runner.run_arm(candidate)
    elif resident_recovery:
        candidate_outcome = runner._run_resident_arm(candidate)
    else:
        candidate_outcome = runner.run_arm(
            candidate, admission_deadline=candidate_deadline,
        )
    if candidate_outcome.status == "submitted":
        return _submitted_response(
            str(request["request_id"]), base=base_outcome,
            candidate=candidate_outcome,
        )
    if candidate_outcome.status not in {"green", "red"}:
        result = {
            "schema": RESULT_SCHEMA,
            "request_id": request["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": f"candidate-{candidate_outcome.status}",
            "base": base_outcome.to_dict(),
            "candidate": candidate_outcome.to_dict(),
        }
        if runner.config.admission_mode == "box" or resident_recovery:
            result["remote_ticket_accepted"] = _arm_acceptance(
                base_outcome, candidate_outcome,
            )
        error = _setup_failure_detail(candidate_outcome)
        if error is not None:
            result["error"] = error
        return result
    verdict = matched_pair_verdict(base_outcome, candidate_outcome)
    recheck_count = int(request.get("recheck", 0))
    if recheck_count and verdict["new_failure_node_ids"]:
        _publish_pair_recheck_count(request, runner, len(verdict["new_failure_node_ids"]))
        classifications: dict[str, dict[str, Any]] = {}
        absent_ids: list[str] = []
        for node_id in verdict["new_failure_node_ids"]:
            outcomes: dict[str, list[bool]] = {"base": [], "candidate": []}
            base_absent_count = 0
            for _ in range(recheck_count):
                for template in (base, candidate):
                    spec = (
                        replace(template, command=_command_for_node(
                            template, node_id, runner.config.selection,
                            repo_root=str(request["repo_root"]),
                        ), generated_node_id=node_id)
                        if template.command is not None
                        else replace(template, selection=(node_id,))
                    )
                    deadline = time.monotonic() + runner.config.admission.max_wait_seconds
                    outcome = runner.run_arm(spec, admission_deadline=deadline)
                    if outcome.status not in {"green", "red"}:
                        result = {
                            "schema": RESULT_SCHEMA, "request_id": request["request_id"],
                            "status": "LOCAL_FALLBACK_REQUIRED",
                            "fallback_reason": f"recheck-{spec.arm}-{outcome.status}",
                            "base": base_outcome.to_dict(),
                            "candidate": candidate_outcome.to_dict(),
                            "rechecks": classifications,
                        }
                        if runner.config.admission_mode == "box" or resident_recovery:
                            result["remote_ticket_accepted"] = _arm_acceptance(
                                base_outcome, candidate_outcome,
                            )
                        return result
                    receipt = outcome.receipt
                    is_absent_at_base = (
                        spec.arm == "base" and outcome.status == "red"
                        and outcome.returncode in {4, 5}
                        and receipt is not None and receipt.passed == 0
                        and receipt.failed == 0 and receipt.errors == 0
                    )
                    if is_absent_at_base:
                        base_absent_count += 1
                    outcomes[spec.arm].append(_arm_is_red(outcome))
            base_passes = all(not failed for failed in outcomes["base"])
            candidate_fails = all(outcomes["candidate"])
            base_fails = all(outcomes["base"])
            classification = (
                "absent-at-base" if base_absent_count == recheck_count
                else "regression" if base_passes and candidate_fails
                else "base-failing" if base_fails
                else "flake"
            )
            if classification == "absent-at-base":
                absent_ids.append(node_id)
            classifications[node_id] = {
                "classification": classification,
                "base_failed": sum(outcomes["base"]),
                "base_passed": recheck_count - sum(outcomes["base"]),
                "candidate_failed": sum(outcomes["candidate"]),
                "candidate_passed": recheck_count - sum(outcomes["candidate"]),
            }
        verdict["rechecks"] = classifications
        if absent_ids:
            verdict["absent_at_base_node_ids"] = sorted(absent_ids)
            verdict["new_failure_node_ids"] = sorted(
                set(verdict["new_failure_node_ids"]) - set(absent_ids)
            )
            candidate_receipt = candidate_outcome.receipt
            candidate_failures = (
                set(candidate_receipt.failing_node_ids)
                | set(candidate_receipt.collection_import_errors)
                if candidate_receipt is not None else set()
            )
            if (candidate_receipt is not None and candidate_failures
                    and candidate_failures.issubset(absent_ids)
                    and candidate_receipt.failed == len(candidate_failures)
                    and candidate_receipt.errors == 0
                    and not candidate_receipt.collection_import_errors):
                verdict["candidate_red"] = False
            base_receipt = base_outcome.receipt
            if (base_receipt is not None and base_outcome.returncode in {4, 5}
                    and base_receipt.failed == 0 and base_receipt.errors == 0
                    and not base_receipt.collection_import_errors):
                verdict["base_red"] = False
            verdict["status"] = (
                "RED" if verdict["base_red"] or verdict["candidate_red"] else "GREEN"
            )
    if runner.config.admission_mode == "box" or resident_recovery:
        verdict["remote_ticket_accepted"] = _arm_acceptance(
            base_outcome, candidate_outcome,
        )
    return verdict



def run_targeted(request: Mapping[str, Any], runner: RemoteRunner) -> dict[str, Any]:
    admission_deadline = _request_admission_deadline(request, runner)
    resident_recovery = _request_has_resident_ticket_checkpoint(request)
    if (runner.config.admission_mode != "box" and not resident_recovery
            and admission_deadline <= time.monotonic()):
        return {
            "schema": RESULT_SCHEMA, "request_id": request["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": "queue-deadline-exceeded",
        }
    selected = _pin_request_box(request, runner, deadline=admission_deadline)
    box = str(selected["box"])
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
        head_sha=str(request.get("head_sha", request["candidate_sha"])),
        snapshot_sha=str(request.get("snapshot_sha", request["candidate_sha"])),
        dirty=bool(request.get("dirty", False)),
        cwd_relative=str(request.get("cwd_relative", "")),
        caller_env=dict(request.get("caller_env", {})),
        bundle_path=Path(request["bundle_path"]) if request.get("bundle_path") else None,
        receipt_metadata=dict(request.get("receipt_metadata", {})),
        snapshot_root=Path(request["snapshot_root"]) if request.get("snapshot_root") else None,
        snapshot_ref=str(request["snapshot_ref"]) if request.get("snapshot_ref") else None,
        caller_identity=request.get("caller_identity"),
        spool_path=Path(request["_spool_path"]) if request.get("_spool_path") else None,
    )
    outcome = _run_request_arm(selected, runner, spec, deadline=admission_deadline)
    if outcome.status == "submitted":
        return _submitted_response(str(request["request_id"]), candidate=outcome)
    if outcome.status not in {"green", "red"}:
        result = {
            "schema": RESULT_SCHEMA, "request_id": request["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": f"candidate-{outcome.status}",
            "candidate": outcome.to_dict(),
        }
        if runner.config.admission_mode == "box" or resident_recovery:
            result["remote_ticket_accepted"] = outcome.remote_ticket_accepted
        error = _setup_failure_detail(outcome)
        if error is not None:
            result["error"] = error
        return result
    return {
        "schema": RESULT_SCHEMA,
        "status": "GREEN" if not _arm_is_red(outcome) else "RED",
        "candidate": outcome.to_dict(),
        "measured_hostnames": {"candidate": _outcome_hostname(outcome)},
    }


def run_remote_command(request: Mapping[str, Any], runner: RemoteRunner) -> dict[str, Any]:
    admission_deadline = _request_admission_deadline(request, runner)
    resident_recovery = _request_has_resident_ticket_checkpoint(request)
    if (runner.config.admission_mode != "box" and not resident_recovery
            and admission_deadline <= time.monotonic()):
        return {
            "schema": RESULT_SCHEMA,
            "request_id": request["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": "queue-deadline-exceeded",
        }
    selected = _pin_request_box(request, runner, deadline=admission_deadline)
    spec = ArmSpec(
        arm="candidate",
        sha=str(request["candidate_sha"]),
        tip_sha=str(request["candidate_sha"]),
        candidate_sha=str(request["candidate_sha"]),
        test_files=(),
        selection=(),
        request_id=str(request["request_id"]),
        box=str(selected["box"]),
        overlay=False,
        command=tuple(str(value) for value in request["command"]),
        command_id=str(request["command_id"]),
        route_reason=str(request.get("route_reason") or "heavy-command"),
        head_sha=str(request.get("head_sha", request["candidate_sha"])),
        snapshot_sha=str(request.get("snapshot_sha", request["candidate_sha"])),
        dirty=bool(request.get("dirty", False)),
        cwd_relative=str(request.get("cwd_relative", "")),
        caller_env=dict(request.get("caller_env", {})),
        bundle_path=Path(request["bundle_path"]) if request.get("bundle_path") else None,
        receipt_metadata=dict(request.get("receipt_metadata", {})),
        snapshot_root=Path(request["snapshot_root"]) if request.get("snapshot_root") else None,
        snapshot_ref=str(request["snapshot_ref"]) if request.get("snapshot_ref") else None,
        caller_identity=request.get("caller_identity"),
        spool_path=Path(request["_spool_path"]) if request.get("_spool_path") else None,
    )
    outcome = _run_request_arm(selected, runner, spec, deadline=admission_deadline)
    if outcome.status == "submitted":
        return _submitted_response(str(request["request_id"]), candidate=outcome)
    if outcome.status not in {"green", "red"}:
        result = {
            "schema": RESULT_SCHEMA, "request_id": request["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": f"candidate-{outcome.status}",
            "candidate": outcome.to_dict(),
        }
        if runner.config.admission_mode == "box" or resident_recovery:
            result["remote_ticket_accepted"] = outcome.remote_ticket_accepted
        error = _setup_failure_detail(outcome)
        if error is not None:
            result["error"] = error
        return result
    return {
        "schema": RESULT_SCHEMA,
        "request_id": request["request_id"],
        "status": "GREEN" if not _arm_is_red(outcome) else "RED",
        "candidate": outcome.to_dict(),
        "measured_hostnames": {"candidate": _outcome_hostname(outcome)},
    }


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _daemon_request_for_disk(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in request.items()
        if key not in {"_spool_path", "_admission_deadline"}
    }


def _persist_daemon_request(path: Path | None, request: Mapping[str, Any]) -> None:
    if path is not None:
        _atomic_write_json(path, _daemon_request_for_disk(request))


def _load_resident_arm(path: Path | None, arm: str) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
        resident = request.get("_resident") if isinstance(request, Mapping) else None
        arms = resident.get("arms") if isinstance(resident, Mapping) else None
        value = arms.get(arm) if isinstance(arms, Mapping) else None
        return dict(value) if isinstance(value, Mapping) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_resident_arm(
    path: Path | None, arm: str, value: Mapping[str, Any],
) -> None:
    if path is None:
        return
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteCIError(f"resident request spool became unreadable: {exc}") from exc
    if not isinstance(request, dict):
        raise RemoteCIError("resident request spool is not an object")
    resident = request.setdefault("_resident", {})
    if not isinstance(resident, dict):
        raise RemoteCIError("resident request checkpoint is invalid")
    arms = resident.setdefault("arms", {})
    if not isinstance(arms, dict):
        raise RemoteCIError("resident arm checkpoints are invalid")
    arms[arm] = dict(value)
    _atomic_write_json(path, request)


def _request_resident_acceptance(path: Path) -> bool | None:
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
        resident = request.get("_resident") if isinstance(request, Mapping) else None
        arms = resident.get("arms") if isinstance(resident, Mapping) else None
        if not isinstance(arms, Mapping) or not arms:
            return None
        values = [value for value in arms.values() if isinstance(value, Mapping)]
        if any(value.get("phase") in {"accepted", "collecting"} for value in values):
            return True
        if any(
            value.get("phase") in {"preparing", "submitting"}
            or value.get("remote_ticket_accepted") is None
            for value in values
        ):
            return None
        if values and all(
            value.get("phase") == "failed"
            and value.get("remote_ticket_accepted") is False
            for value in values
        ):
            return False
        return None
    except (OSError, json.JSONDecodeError):
        return None


def _request_has_resident_ticket_checkpoint(request: Any) -> bool:
    resident = request.get("_resident") if isinstance(request, Mapping) else None
    arms = resident.get("arms") if isinstance(resident, Mapping) else None
    return isinstance(arms, Mapping) and any(
        isinstance(value, Mapping)
        and value.get("phase") in {"submitting", "accepted", "collecting"}
        for value in arms.values()
    )


def _resident_arm_accepted(request: Mapping[str, Any], arm: str) -> bool:
    resident = request.get("_resident")
    arms = resident.get("arms") if isinstance(resident, Mapping) else None
    state = arms.get(arm) if isinstance(arms, Mapping) else None
    return isinstance(state, Mapping) and state.get("phase") in {"accepted", "collecting"}


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
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(request, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def _request_cancel_path(config: DaemonConfig, request_id: str) -> Path:
    return config.queue_dir / f".{request_id}.cancel"


def _mark_request_cancelled(config: DaemonConfig, request_id: str, reason: str) -> None:
    _atomic_write_json(_request_cancel_path(config, request_id), {
        "request_id": request_id, "reason": reason, "cancelled_at": _utc_now(),
    })


def _request_cancelled(
    config: DaemonConfig, request_id: str,
    caller_identity: Any = None,
) -> bool:
    if _request_cancel_path(config, request_id).exists():
        return True
    if not isinstance(caller_identity, Mapping):
        return False
    pid = caller_identity.get("pid")
    start_token = caller_identity.get("start_token")
    if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            or not isinstance(start_token, str) or not start_token):
        return False
    return goalflight_compat.process_identity_matches(pid, start_token) is False


def _request_path_has_resident_checkpoint(request_path: Path) -> bool:
    if not request_path.exists():
        return False
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return True
    if not isinstance(request, Mapping):
        return True
    return _request_has_resident_ticket_checkpoint(request)


def _resident_checkpoint_resolved(
    config: DaemonConfig, request_path: Path, request: Mapping[str, Any] | None = None,
) -> bool:
    if request is None:
        try:
            value = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return not request_path.exists()
        if not isinstance(value, Mapping):
            return not request_path.exists()
        request = value
    resident = request.get("_resident")
    arms = resident.get("arms") if isinstance(resident, Mapping) else None
    if not isinstance(arms, Mapping):
        return True
    checkpoints = {
        arm: state for arm, state in arms.items()
        if isinstance(state, Mapping)
        and state.get("phase") in {"submitting", "accepted", "collecting"}
    }
    if not checkpoints:
        return True
    try:
        result = json.loads(
            (config.result_dir / f"{request_path.stem}.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, UnicodeError):
        result = None
    if not isinstance(result, Mapping) or result.get("status") == "SUBMITTED":
        return False
    for arm, state in checkpoints.items():
        if state.get("cancel_acknowledged") is True:
            continue
        outcome = result.get(str(arm))
        lease = outcome.get("lease") if isinstance(outcome, Mapping) else None
        if not isinstance(lease, Mapping) or not isinstance(lease.get("result"), Mapping):
            return False
    return True


def _delete_request_checkpoint_files(
    config: DaemonConfig, request_path: Path,
) -> bool:
    if request_path.exists():
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return False
        if not isinstance(request, Mapping):
            return False
        if (_request_has_resident_ticket_checkpoint(request)
                and not _resident_checkpoint_resolved(config, request_path, request)):
            return False
    request_path.unlink(missing_ok=True)
    _request_cancel_path(config, request_path.stem).unlink(missing_ok=True)
    return True


@contextlib.contextmanager
def _cancel_request_on_signals(config: DaemonConfig, request_id: str):
    previous: dict[int, Any] = {}

    def cancel(signum: int, _frame: Any) -> None:
        if signum == getattr(signal, "SIGHUP", None):
            print(
                "SIGHUP cancels this submitted request; it does not reload daemon "
                "config. Restart the daemon to load config changes.",
                file=sys.stderr,
                flush=True,
            )
        with contextlib.suppress(OSError):
            _mark_request_cancelled(
                config, request_id, f"wrapper-{signal.Signals(signum).name.lower()}",
            )
        raise SystemExit(128 + signum)

    try:
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous[signum] = signal.signal(signum, cancel)
            except (OSError, ValueError):
                continue
        yield
    finally:
        for signum, handler in previous.items():
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signum, handler)


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
        self._queue_started: dict[str, float] = {}

    def _new_admission_refusal(self, request_path: Path) -> dict[str, Any] | None:
        # Only queue entries with no active future or resident ticket are
        # refused; already admitted work continues through its existing path.
        if _request_path_has_resident_checkpoint(request_path):
            return None
        if self.config.config_identity is None:
            reason = "daemon-config-identity-missing-restart-required"
            error = (
                "daemon config identity was not captured at startup; restart the "
                "remote-CI daemon to load and pin its config"
            )
        else:
            try:
                current_identity = _read_config_identity(self.config.path)
            except ConfigError as exc:
                reason = "daemon-config-unreadable-restart-required"
                error = (
                    f"cannot verify daemon config {self.config.path}: {exc}; "
                    "restart the remote-CI daemon after restoring the config"
                )
            else:
                if current_identity == self.config.config_identity:
                    return None
                reason = "daemon-config-changed-restart-required"
                error = (
                    "daemon config changed since this process loaded it; restart "
                    "the remote-CI daemon to load the current config"
                )
        return {
            "schema": RESULT_SCHEMA,
            "request_id": request_path.stem,
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": reason,
            "error": error,
            "finished_at": _utc_now(),
        }

    def _external_result_is_final(self, request_id: str) -> bool:
        result_path = self.config.result_dir / f"{request_id}.json"
        if not result_path.exists():
            return False
        request_path = self.config.queue_dir / f"{request_id}.json"
        if _request_path_has_resident_checkpoint(request_path):
            return _resident_checkpoint_resolved(self.config, request_path)
        return not _request_cancel_path(self.config, request_id).exists()

    def _queue_waited_seconds(self, request_id: str) -> float:
        # A restart grants a fresh queue window. Never reconstruct awake time
        # from wall-clock file age, which includes laptop sleep.
        now = time.monotonic()
        return max(0.0, now - self._queue_started.setdefault(request_id, now))

    def pending_paths(self) -> list[Path]:
        self.config.queue_dir.mkdir(parents=True, exist_ok=True)
        return sorted(
            (path for path in self.config.queue_dir.glob("*.json")
             if not path.name.endswith(".local.json")),
            key=lambda path: path.name,
        )

    def _snapshot_command_request(
        self, request: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        saved = {
            name: request.get(name) for name in
            ("head_sha", "snapshot_sha", "dirty", "bundle_path", "snapshot_root", "snapshot_ref")
        }
        request_id = str(request["request_id"])
        expected_bundle = self.config.queue_dir / f"{request_id}.bundle"
        expected_ref = f"refs/goalflight/remote-ci/{request_id}"
        accepted = _resident_arm_accepted(request, "candidate")
        cache_matches = (
            all(value is not None for value in saved.values())
            and Path(str(saved["bundle_path"])).resolve(strict=False)
            == expected_bundle.resolve(strict=False)
            and str(saved["snapshot_ref"]) == expected_ref
        )
        if (self.config.admission_mode == "box" and cache_matches
                and (accepted or Path(str(saved["snapshot_root"])).is_dir())):
            if not accepted:
                try:
                    pinned = _git_output(
                        Path(str(saved["snapshot_root"])),
                        ["rev-parse", "--verify", f"{expected_ref}^{{commit}}"],
                    ).lower()
                except RemoteCIError as exc:
                    raise RemoteCIError("cached candidate snapshot ref is missing") from exc
                if pinned != str(saved["snapshot_sha"]).lower():
                    raise RemoteCIError("cached candidate snapshot ref changed")
            request.update(
                head_sha=str(saved["head_sha"]),
                snapshot_sha=str(saved["snapshot_sha"]),
                candidate_sha=str(saved["snapshot_sha"]),
                tip_sha=str(saved["snapshot_sha"]),
                dirty=bool(saved["dirty"]),
                bundle_path=str(expected_bundle),
                snapshot_root=str(saved["snapshot_root"]),
                snapshot_ref=expected_ref,
            )
            return request, {
                "head_sha": saved["head_sha"],
                "snapshot_sha": saved["snapshot_sha"],
                "dirty": saved["dirty"],
                "bundle_path": str(expected_bundle),
                "root": saved["snapshot_root"],
                "ref": expected_ref,
            }
        setup_deadline = time.monotonic() + SNAPSHOT_SETUP_TIMEOUT_SECONDS
        root = Path(str(request["repo_root"])).resolve()
        actual_root = Path(_git_output(
            root, ["rev-parse", "--show-toplevel"], deadline=setup_deadline,
        )).resolve()
        if actual_root != root:
            raise RemoteCIError("repo_root is not the worktree root")
        snapshot = _snapshot_worktree(
            root, str(request["request_id"]), self.config.queue_dir,
            cwd_relative=str(request.get("cwd_relative", "")),
            caller_env=request.get("caller_env", {}),
            setup_deadline=setup_deadline,
        )
        try:
            expected_head = request.get("head_sha")
            expected_tree = request.get("snapshot_sha")
            expected_dirty = request.get("dirty")
            if (
                isinstance(expected_head, str)
                and (expected_head != snapshot["head_sha"]
                     or expected_tree != snapshot["tree_sha"]
                     or expected_dirty != snapshot["dirty"])
            ):
                raise RemoteCIError(
                    "fallback-tree-changed: requested HEAD "
                    f"{expected_head} snapshot {expected_tree}; current HEAD "
                    f"{snapshot['head_sha']} snapshot {snapshot['tree_sha']}"
                )
            request.update(
                head_sha=snapshot["head_sha"],
                snapshot_sha=snapshot["snapshot_sha"],
                candidate_sha=snapshot["snapshot_sha"],
                tip_sha=(str(request["base_sha"])
                         if request.get("base_sha") else snapshot["snapshot_sha"]),
                dirty=snapshot["dirty"],
                bundle_path=str(snapshot["bundle_path"]),
                snapshot_root=str(snapshot["root"]),
                snapshot_ref=str(snapshot["ref"]),
            )
            if self.config.admission_mode == "box":
                _persist_daemon_request(
                    Path(str(request["_spool_path"]))
                    if request.get("_spool_path") else None,
                    request,
                )
            return request, snapshot
        except BaseException:
            _release_snapshot(Path(snapshot["root"]), str(snapshot["ref"]),
                              str(snapshot["snapshot_sha"]), Path(snapshot["bundle_path"]))
            raise

    def _snapshot_commit_request(
        self, request: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        request_id = str(request["request_id"])
        root_value = request.get("repo_root")
        if isinstance(root_value, str):
            root = Path(root_value).resolve()
        else:
            root = self.config.path.parent
            if root.name == ".goal-flight":
                root = root.parent
            root = root.resolve()
        actual_root = Path(_git_output(
            root, ["rev-parse", "--show-toplevel"],
            deadline=time.monotonic() + SNAPSHOT_SETUP_TIMEOUT_SECONDS,
        )).resolve()
        if actual_root != root:
            raise RemoteCIError("project config does not resolve to the repository root")

        def pin(arm: str, sha: str, ref: str, bundle_path: Path) -> None:
            resolved = _git_output(root, ["rev-parse", "--verify", f"{sha}^{{commit}}"])
            if resolved.lower() != sha.lower():
                raise RemoteCIError(f"{arm} SHA does not resolve to the requested commit")
            try:
                current = _git_output(root, ["rev-parse", "--verify", f"{ref}^{{commit}}"]).lower()
            except RemoteCIError:
                current = None
            if current is None:
                _git_output(root, ["update-ref", ref, sha, "0" * len(sha)])
            elif current != sha.lower():
                raise RemoteCIError(f"cached {arm} snapshot ref changed")
            if bundle_path.resolve(strict=False).parent != self.config.queue_dir.resolve(strict=False):
                raise RemoteCIError(f"{arm} bundle path is outside the request spool")

        candidate_sha = str(request["candidate_sha"])
        candidate_ref = f"refs/goalflight/remote-ci/{request_id}/candidate"
        candidate_bundle = self.config.queue_dir / f"{request_id}.bundle"
        base_sha = str(request["tip_sha"])
        base_ref = f"refs/goalflight/remote-ci/{request_id}/base"
        base_bundle = self.config.queue_dir / f"{request_id}.base.bundle"
        accepted_candidate = _resident_arm_accepted(request, "candidate")
        accepted_base = _resident_arm_accepted(request, "base")
        saved_root = request.get("snapshot_root")
        root_matches = (
            isinstance(saved_root, str)
            and Path(saved_root).resolve(strict=False) == root.resolve(strict=False)
        )
        candidate_cache_matches = (
            root_matches
            and request.get("snapshot_ref") == candidate_ref
            and Path(str(request.get("bundle_path") or "")).resolve(strict=False)
            == candidate_bundle.resolve(strict=False)
        )
        if accepted_candidate and not candidate_cache_matches:
            raise RemoteCIError("accepted candidate checkpoint is missing its snapshot identity")
        if not accepted_candidate:
            pin("candidate", candidate_sha, candidate_ref, candidate_bundle)

        is_pair = request.get("kind") == "gate"
        base_cache_matches = (
            root_matches
            and request.get("base_snapshot_ref") == base_ref
            and Path(str(request.get("base_bundle_path") or "")).resolve(strict=False)
            == base_bundle.resolve(strict=False)
            and str(request.get("base_snapshot_sha") or "").lower() == base_sha.lower()
        )
        if is_pair and accepted_base and not base_cache_matches:
            raise RemoteCIError("accepted base checkpoint is missing its snapshot identity")
        if is_pair and not accepted_base:
            pin("base", base_sha, base_ref, base_bundle)

        request.update(
            head_sha=str(request.get("head_sha") or candidate_sha),
            snapshot_sha=candidate_sha,
            dirty=False,
            bundle_path=str(candidate_bundle),
            snapshot_root=str(root),
            snapshot_ref=candidate_ref,
        )
        if is_pair:
            request.update(
                base_snapshot_sha=base_sha,
                base_bundle_path=str(base_bundle),
                base_snapshot_ref=base_ref,
            )
        else:
            request.pop("base_snapshot_sha", None)
            request.pop("base_bundle_path", None)
            request.pop("base_snapshot_ref", None)
        if self.config.admission_mode == "box":
            _persist_daemon_request(
                Path(str(request["_spool_path"]))
                if request.get("_spool_path") else None,
                request,
            )
        return request, {
            "head_sha": request["head_sha"], "snapshot_sha": candidate_sha,
            "dirty": False, "root": str(root), "ref": candidate_ref,
            "bundle_path": str(candidate_bundle),
        }

    def _release_request_snapshots(self, request_path: Path) -> None:
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(request, Mapping):
            return
        resident_checkpointed = _request_has_resident_ticket_checkpoint(request)
        if (resident_checkpointed
                and not _resident_checkpoint_resolved(
                    self.config, request_path, request,
                )):
            return
        if self.config.admission_mode != "box" and not resident_checkpointed:
            return
        request_id = request_path.stem
        root_value = request.get("snapshot_root")
        if not isinstance(root_value, str):
            return
        root = Path(root_value)
        snapshots = (
            (request.get("snapshot_ref"), request.get("snapshot_sha"),
             request.get("bundle_path"),
             f"refs/goalflight/remote-ci/{request_id}"),
            (request.get("base_snapshot_ref"), request.get("base_snapshot_sha"),
             request.get("base_bundle_path"),
             f"refs/goalflight/remote-ci/{request_id}/base"),
        )
        for ref, sha, bundle_value, expected_ref in snapshots:
            acceptable_refs = {expected_ref}
            if expected_ref == f"refs/goalflight/remote-ci/{request_id}":
                acceptable_refs.add(f"refs/goalflight/remote-ci/{request_id}/candidate")
            if (ref not in acceptable_refs or not isinstance(sha, str)
                    or not isinstance(bundle_value, str)):
                continue
            bundle_path = Path(bundle_value)
            if bundle_path.resolve(strict=False).parent != self.config.queue_dir.resolve(strict=False):
                continue
            _release_snapshot(root, str(ref), sha, bundle_path)

    def _publish_final_result(self, request_path: Path, result: Mapping[str, Any]) -> None:
        request_id = str(result.get("request_id") or request_path.stem)
        _atomic_write_json(self.config.result_dir / f"{request_id}.json", result)
        self._release_request_snapshots(request_path)

    def _process_request(
        self, request_path: Path, *, immediate_failure_reason: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_path.stem
        snapshot: dict[str, Any] | None = None
        request_project_id: str | None = None
        request_queue_deadline: float | None = None
        admission_deadline: float | None = None
        daemon_queue_waited = False
        resident_checkpointed = False

        def finish(value: dict[str, Any]) -> dict[str, Any]:
            value.setdefault("project_id", request_project_id)
            status = str(value.get("status", "")).upper()

            def queued(item: Any) -> bool:
                if isinstance(item, Mapping):
                    return item.get("queue_waited") is True or any(
                        queued(child) for child in item.values()
                    )
                if isinstance(item, list):
                    return any(queued(child) for child in item)
                return False

            value["policy_outcome"] = (
                "queued" if status in {"GREEN", "RED"}
                and (daemon_queue_waited or queued(value))
                else "ran_remote" if status in {"GREEN", "RED"}
                else "failed"
            )
            return value

        try:
            waited = self._queue_waited_seconds(request_id)
            daemon_queue_waited = waited > self.config.poll_seconds
            request_queue_deadline = (
                time.monotonic()
                + max(0.0, self.config.admission.max_wait_seconds - waited)
            )
            raw = json.loads(request_path.read_text(encoding="utf-8"))
            resident_checkpointed = _request_has_resident_ticket_checkpoint(raw)
            raw_project_id = raw.get("project_id") if isinstance(raw, Mapping) else None
            if isinstance(raw_project_id, str):
                request_project_id = raw_project_id
            if resident_checkpointed:
                # Resident ticket state was reached only after validation and
                # snapshotting. Reconciliation, collection, and cancellation
                # must survive loss of the original source tree.
                if not isinstance(raw, Mapping) or raw.get("schema") != REQUEST_SCHEMA:
                    raise RemoteCIError("accepted request checkpoint has an invalid schema")
                if raw_project_id != self.config.repo:
                    raise ProjectIsolationError(
                        f"request project {raw_project_id!r} does not match daemon project "
                        f"{self.config.repo!r}"
                    )
                accepted_id = _safe_id(raw.get("request_id"), "request_id")
                if accepted_id != request_path.stem:
                    raise RemoteCIError("accepted request checkpoint id does not match its spool")
                request = dict(raw)
                request["request_id"] = accepted_id
            else:
                request = validate_request(raw, self.config)
            request["_spool_path"] = str(request_path)
            if request.get("command") is None or request.get("base_sha") is not None:
                admission_deadline = request_queue_deadline
                request["_admission_deadline"] = admission_deadline
        except ProjectIsolationError as exc:
            return finish({
                "schema": RESULT_SCHEMA, "request_id": request_id,
                "status": "PROJECT_MISMATCH", "error": str(exc),
                "project_id": request_project_id,
                "fallback_reason": exc.fallback_reason,
                "finished_at": _utc_now(),
            })
        except ProfileIneligible as exc:
            return finish({
                "schema": RESULT_SCHEMA, "request_id": request_id,
                "status": "LOCAL_FALLBACK_REQUIRED",
                "fallback_reason": exc.fallback_reason,
                "error": str(exc), "finished_at": _utc_now(),
            })
        except (OSError, json.JSONDecodeError, RemoteCIError) as exc:
            return finish({
                "schema": RESULT_SCHEMA, "request_id": request_id,
                "status": "ERROR", "error": str(exc), "finished_at": _utc_now(),
            })
        request_id = str(request["request_id"])
        if (self.config.admission_mode != "box" and not resident_checkpointed
                and request_queue_deadline is not None
                and request_queue_deadline <= time.monotonic()):
            return finish({
                "schema": RESULT_SCHEMA, "request_id": request_id,
                "status": "LOCAL_FALLBACK_REQUIRED",
                "fallback_reason": "queue-deadline-exceeded",
                "finished_at": _utc_now(),
            })
        if immediate_failure_reason is not None and not resident_checkpointed:
            return finish({
                "schema": RESULT_SCHEMA, "request_id": request_id,
                "status": "LOCAL_FALLBACK_REQUIRED",
                "fallback_reason": immediate_failure_reason,
                "finished_at": _utc_now(),
            })
        if (self.config.admission_mode != "box" and not resident_checkpointed
                and _request_cancelled(
            self.config, request_id, request.get("caller_identity"),
        )):
            return finish({
                "schema": RESULT_SCHEMA, "request_id": request_id,
                "status": "LOCAL_FALLBACK_REQUIRED", "fallback_reason": "wrapper-cancelled",
                "finished_at": _utc_now(),
            })
        try:
            try:
                if request.get("command") is not None:
                    if not resident_checkpointed:
                        try:
                            request, snapshot = self._snapshot_command_request(request)
                            admission_deadline = (
                                time.monotonic() + self.config.admission.max_wait_seconds
                            )
                            request["_admission_deadline"] = admission_deadline
                        except (OSError, subprocess.SubprocessError, RemoteCIError, ValueError) as exc:
                            accepted = (
                                _request_resident_acceptance(request_path)
                                if self.config.admission_mode == "box"
                                or resident_checkpointed else False
                            )
                            if accepted is not False:
                                return finish({
                                    "schema": RESULT_SCHEMA, "request_id": request_id,
                                    "status": "SUBMITTED", "error": str(exc),
                                    "remote_ticket_accepted": accepted,
                                })
                            return finish({
                                "schema": RESULT_SCHEMA, "request_id": request_id,
                                "status": "LOCAL_FALLBACK_REQUIRED",
                                "fallback_reason": (
                                    "fallback-tree-changed"
                                    if str(exc).startswith("fallback-tree-changed:")
                                    else "snapshot-failed"
                                ), "error": str(exc),
                                "finished_at": _utc_now(),
                            })
                elif self.config.admission_mode == "box":
                    if not resident_checkpointed:
                        try:
                            request, snapshot = self._snapshot_commit_request(request)
                        except (OSError, subprocess.SubprocessError, RemoteCIError, ValueError) as exc:
                            accepted = _request_resident_acceptance(request_path)
                            if accepted is not False:
                                return finish({
                                    "schema": RESULT_SCHEMA, "request_id": request_id,
                                    "status": "SUBMITTED", "error": str(exc),
                                    "remote_ticket_accepted": accepted,
                                })
                            return finish({
                                "schema": RESULT_SCHEMA, "request_id": request_id,
                                "status": "LOCAL_FALLBACK_REQUIRED",
                                "fallback_reason": "snapshot-failed", "error": str(exc),
                                "finished_at": _utc_now(),
                            })
                if (self.config.admission_mode != "box" and not resident_checkpointed
                        and admission_deadline is not None
                        and admission_deadline <= time.monotonic()):
                    return finish({
                        "schema": RESULT_SCHEMA, "request_id": request_id,
                        "status": "LOCAL_FALLBACK_REQUIRED",
                        "fallback_reason": "queue-deadline-exceeded",
                        "finished_at": _utc_now(),
                    })
                if (self.config.admission_mode != "box" and not resident_checkpointed
                        and _request_cancelled(
                    self.config, request_id, request.get("caller_identity"),
                )):
                    result = {
                        "schema": RESULT_SCHEMA, "request_id": request_id,
                        "status": "LOCAL_FALLBACK_REQUIRED",
                        "fallback_reason": "wrapper-cancelled", "finished_at": _utc_now(),
                    }
                elif request.get("command") is not None and request.get("base_sha") is None:
                    result = run_remote_command(request, self.runner)
                elif request["kind"] == "gate":
                    result = run_matched_pair(request, self.runner)
                else:
                    result = run_targeted(request, self.runner)
                result["request_id"] = request_id
                result["finished_at"] = _utc_now()
                if snapshot:
                    result["snapshot"] = {
                        "head_sha": snapshot["head_sha"],
                        "snapshot_sha": snapshot["snapshot_sha"],
                        "dirty": snapshot["dirty"],
                    }
            except NoHealthyBox as exc:
                accepted = (
                    _request_resident_acceptance(request_path)
                    if self.config.admission_mode == "box" or resident_checkpointed else False
                )
                result = ({
                    "schema": RESULT_SCHEMA, "request_id": request_id,
                    "status": "SUBMITTED", "error": str(exc),
                    "remote_ticket_accepted": accepted,
                } if accepted is not False else {
                    "schema": RESULT_SCHEMA,
                    "request_id": request_id,
                    "status": "LOCAL_FALLBACK_REQUIRED",
                    "fallback_reason": "no-healthy-box",
                    "error": str(exc),
                    "finished_at": _utc_now(),
                })
            except RemoteCIError as exc:
                accepted = (
                    _request_resident_acceptance(request_path)
                    if self.config.admission_mode == "box" or resident_checkpointed else False
                )
                result = ({
                    "schema": RESULT_SCHEMA, "request_id": request_id,
                    "status": "SUBMITTED", "error": str(exc),
                    "remote_ticket_accepted": accepted,
                } if accepted is not False else {
                    "schema": RESULT_SCHEMA,
                    "request_id": request_id,
                    "status": "LOCAL_FALLBACK_REQUIRED",
                    "fallback_reason": (
                        "health-unknown" if isinstance(exc, HealthProbeUnknown)
                        else "remote-transport-or-setup-failed"
                    ),
                    "error": str(exc),
                    "finished_at": _utc_now(),
                })
            except Exception as exc:
                accepted = (
                    _request_resident_acceptance(request_path)
                    if self.config.admission_mode == "box" or resident_checkpointed else False
                )
                result = ({
                    "schema": RESULT_SCHEMA, "request_id": request_id,
                    "status": "SUBMITTED", "error": str(exc),
                    "remote_ticket_accepted": accepted,
                } if accepted is not False else {
                    "schema": RESULT_SCHEMA,
                    "request_id": request_id,
                    "status": "ERROR",
                    "error": str(exc),
                    "finished_at": _utc_now(),
                })
            if snapshot:
                result["snapshot"] = {
                    "head_sha": snapshot["head_sha"],
                    "snapshot_sha": snapshot["snapshot_sha"],
                    "dirty": snapshot["dirty"],
                }
            return finish(result)
        finally:
            if snapshot and self.config.admission_mode != "box":
                _release_snapshot(Path(snapshot["root"]), str(snapshot["ref"]),
                                  str(snapshot["snapshot_sha"]), Path(snapshot["bundle_path"]))

    def poll_once(self) -> dict[str, Any] | None:
        self._reap_orphaned()
        for request_path in self.pending_paths():
            request_id = request_path.stem
            result_path = self.config.result_dir / f"{request_id}.json"
            if self._external_result_is_final(request_id):
                try:
                    prior = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return None
                self._release_request_snapshots(request_path)
                if not _delete_request_checkpoint_files(self.config, request_path):
                    return None
                self._queue_started.pop(request_id, None)
                return prior
            result = self._new_admission_refusal(request_path)
            if result is None:
                result = self._process_request(request_path)
            if result.get("status") == "SUBMITTED":
                return result
            request_id = str(result.get("request_id") or request_id)
            self._publish_final_result(request_path, result)
            deleted = _delete_request_checkpoint_files(self.config, request_path)
            if not deleted:
                return result
            self._queue_started.pop(request_id, None)
            return result
        return None

    def _reap_orphaned(self) -> None:
        reap = getattr(self.runner, "reap", None)
        if callable(reap):
            with contextlib.suppress(Exception):
                reap()

    def run(self, *, once: bool = False) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.result_dir.mkdir(parents=True, exist_ok=True)
        drain_path = self.config.state_dir / "drain.request"
        status_path = self.config.state_dir / "daemon.status.json"
        started_at = _utc_now()
        status_started = False
        draining_since: float | None = None
        drain_started_at: str | None = None
        drain_timeout_reported = False
        process_pid = os.getpid()

        def drain_flag_state() -> tuple[bool, int | None]:
            try:
                raw_pid = drain_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                return False, None
            except (OSError, UnicodeError):
                return True, None
            try:
                target_pid = int(raw_pid)
            except ValueError:
                return True, None
            return True, target_pid if target_pid > 0 else None

        def drain_requested() -> bool:
            exists, target_pid = drain_flag_state()
            return exists and (target_pid is None or target_pid == process_pid)

        def publish_status(status: str, **details: Any) -> None:
            _atomic_write_json(status_path, {
                "pid": os.getpid(),
                "started_at": started_at,
                "status": status,
                **details,
            })

        def begin_draining() -> None:
            nonlocal draining_since, drain_started_at
            if draining_since is not None:
                return
            draining_since = time.monotonic()
            drain_started_at = _utc_now()
            try:
                publish_status("draining", drain_started_at=drain_started_at)
            except OSError as exc:
                print(
                    f"goalflight-remote-ci: cannot publish drain status: {exc}",
                    file=sys.stderr,
                )

        with DaemonLock(self.config.lock_file):
            try:
                drain_exists, target_pid = drain_flag_state()
                if drain_exists and target_pid != process_pid:
                    drain_path.unlink(missing_ok=True)
                if self.config.pid_file is not None:
                    identity = goalflight_compat.process_start_identity(os.getpid())
                    token = identity.get("start_token") if isinstance(identity, Mapping) else ""
                    _atomic_write_json(self.config.pid_file, {
                        "pid": os.getpid(),
                        "start_token": str(token or ""),
                        "started_at": started_at,
                    })
                publish_status("running")
                status_started = True
                if once:
                    self.poll_once()
                    return
                active: dict[str, tuple[Path, concurrent.futures.Future[dict[str, Any]]]] = {}
                poll_after: dict[str, float] = {}
                worker_count = max(
                    1, sum(box.max_concurrent_seats for box in self.config.boxes.values())
                )
                with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
                    while True:
                        self._reap_orphaned()
                        if draining_since is None and drain_requested():
                            begin_draining()
                        for request_id, (path, future) in list(active.items()):
                            if not future.done():
                                if (self.config.admission_mode != "box"
                                        and not _request_path_has_resident_checkpoint(path)
                                        and not future.running()):
                                    waited = self._queue_waited_seconds(request_id)
                                    if (waited >= self.config.admission.max_wait_seconds
                                            and future.cancel()):
                                        result = self._process_request(path)
                                        self._publish_final_result(path, result)
                                        if _delete_request_checkpoint_files(self.config, path):
                                            self._queue_started.pop(request_id, None)
                                        active.pop(request_id, None)
                                continue
                            try:
                                result = future.result()
                            except Exception as exc:
                                result = {
                                    "schema": RESULT_SCHEMA, "request_id": request_id,
                                    "status": "ERROR", "error": str(exc),
                                    "finished_at": _utc_now(),
                                }
                            if result.get("status") == "SUBMITTED":
                                active.pop(request_id, None)
                                poll_after[request_id] = (
                                    time.monotonic() + self.config.poll_seconds
                                )
                                continue
                            self._publish_final_result(path, result)
                            if _delete_request_checkpoint_files(self.config, path):
                                self._queue_started.pop(request_id, None)
                            active.pop(request_id, None)
                        if draining_since is None:
                            for path in self.pending_paths():
                                if drain_requested():
                                    begin_draining()
                                    break
                                request_id = path.stem
                                self._queue_waited_seconds(request_id)
                                if request_id in active:
                                    continue
                                if time.monotonic() < poll_after.get(request_id, 0.0):
                                    continue
                                result_path = self.config.result_dir / f"{request_id}.json"
                                if self._external_result_is_final(request_id):
                                    self._release_request_snapshots(path)
                                    if _delete_request_checkpoint_files(self.config, path):
                                        self._queue_started.pop(request_id, None)
                                    else:
                                        poll_after[request_id] = (
                                            time.monotonic() + self.config.poll_seconds
                                        )
                                    continue
                                result = self._new_admission_refusal(path)
                                if result is not None:
                                    self._publish_final_result(path, result)
                                    if _delete_request_checkpoint_files(self.config, path):
                                        self._queue_started.pop(request_id, None)
                                    continue
                                occupied_workers = sum(
                                    not future.done() for _, future in active.values()
                                )
                                if (self.config.admission_mode != "box"
                                        and not _request_path_has_resident_checkpoint(path)
                                        and self.config.fallback == "fail"
                                        and occupied_workers >= worker_count):
                                    result = self._process_request(
                                        path, immediate_failure_reason="daemon-capacity-refused",
                                    )
                                    self._publish_final_result(path, result)
                                    if _delete_request_checkpoint_files(self.config, path):
                                        self._queue_started.pop(request_id, None)
                                    continue
                                active[request_id] = (path, pool.submit(self._process_request, path))
                        if draining_since is not None:
                            if not active:
                                return
                            if (not drain_timeout_reported
                                    and time.monotonic() - draining_since
                                    >= DAEMON_DRAIN_TIMEOUT_SECONDS):
                                drain_timeout_reported = True
                                try:
                                    publish_status(
                                        "draining",
                                        drain_started_at=drain_started_at,
                                        drain_timeout_reported=True,
                                        active_requests=len(active),
                                    )
                                except OSError as exc:
                                    print(
                                        f"goalflight-remote-ci: cannot publish drain timeout status: {exc}",
                                        file=sys.stderr,
                                    )
                                print(
                                    "goalflight-remote-ci: drain timed out after "
                                    f"{DAEMON_DRAIN_TIMEOUT_SECONDS:g}s; still waiting for "
                                    f"{len(active)} in-flight request(s)",
                                    file=sys.stderr,
                                )
                        time.sleep(self.config.poll_seconds)
            finally:
                if status_started:
                    try:
                        publish_status(
                            "stopped",
                            **({"drain_timeout_reported": True}
                               if drain_timeout_reported else {}),
                        )
                    except OSError as exc:
                        print(
                            f"goalflight-remote-ci: cannot publish stopped status: {exc}",
                            file=sys.stderr,
                        )
                if self.config.pid_file is not None:
                    self.config.pid_file.unlink(missing_ok=True)


def daemon_is_running(config: DaemonConfig) -> bool | None:
    if config.pid_file is not None:
        try:
            value = json.loads(config.pid_file.read_text(encoding="utf-8"))
            pid = value.get("pid") if isinstance(value, Mapping) else None
            token = value.get("start_token") if isinstance(value, Mapping) else None
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                if isinstance(token, str) and token:
                    if goalflight_compat.process_identity_matches(pid, token) is True:
                        return True
                else:
                    os.kill(pid, 0)
                    return True
        except (OSError, ValueError, TypeError):
            pass
    try:
        with config.lock_file.open("r") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return True
                return None
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return None


def _load_local_config(path: Path) -> DaemonConfig:
    return load_config(path)


def _local_load() -> tuple[list[float], int]:
    try:
        load = list(os.getloadavg())
    except (AttributeError, OSError):
        load = []
    return load, os.cpu_count() or 1


def _dispatch_waiting() -> bool:
    queue = goalflight_dispatch_paths.dispatch_queue_dir()
    try:
        if any(queue.glob("*.json")) or any(queue.glob("*.json.claimed-*")):
            return True
    except OSError:
        pass
    try:
        return any(record.get("state") == "waiting_capacity"
                   for record in goalflight_ledger.read_records(
                       skip_terminal=True, recent_window_days=1,
                   ))
    except (OSError, ValueError):
        return False


def _write_wrapper_result(result_dir: Path, request_id: str, result: Mapping[str, Any]) -> None:
    _atomic_write_json(result_dir / f"{request_id}.json", result)


def _git_common_dir(root: Path) -> Path | None:
    try:
        common_dir = Path(_git_output(root, ["rev-parse", "--git-common-dir"], timeout=5))
        if not common_dir.is_absolute():
            common_dir = root / common_dir
        return common_dir.resolve()
    except (RemoteCIError, OSError, ValueError):
        return None


def _same_git_repository(first: Path, second: Path) -> bool:
    first_common = _git_common_dir(first)
    second_common = _git_common_dir(second)
    if first_common is not None and second_common is not None:
        return first_common == second_common
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return False


def _git_repository_id(root: Path) -> str | None:
    config_path = root / ".goal-flight" / "remote-ci.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(raw, Mapping):
            configured_repo = raw.get("repo")
            if configured_repo in (None, ""):
                return "default"
            if isinstance(configured_repo, str):
                return _safe_id(configured_repo, "repo")
    except (OSError, json.JSONDecodeError, RemoteCIError, ValueError):
        pass

    try:
        remote = _git_output(root, ["config", "--get", "remote.origin.url"], timeout=5)
    except (RemoteCIError, OSError):
        remote = ""
    if remote:
        if "://" in remote:
            remote_path = remote.split("://", 1)[1].partition("/")[2]
        elif ":" in remote and not remote.startswith("/"):
            remote_path = remote.rsplit(":", 1)[-1]
        else:
            remote_path = remote
        name = posixpath.basename(remote_path.rstrip("/"))
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name

    common_dir = _git_common_dir(root)
    if common_dir is not None:
        name = common_dir.parent.name if common_dir.name == ".git" else common_dir.name
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name
    return None


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _remote_config_choice_message(reason: str) -> str:
    return (
        f"{reason}; pass --config <path> or set GOALFLIGHT_REMOTE_CI_CONFIG"
    )


def _resolve_wrapper_config(
    explicit_config: Path | None, cwd: Path, repo_root: Path | None,
) -> tuple[Path, str | None]:
    skill_root = Path(__file__).resolve().parent.parent
    skill_config = skill_root / ".goal-flight" / "remote-ci.json"
    skill_repo = _git_repository_id(skill_root)

    if repo_root is None:
        if not _path_is_within(cwd, skill_root):
            raise ConfigError(_remote_config_choice_message(
                "cannot determine the caller repository"
            ))
        caller_repo = skill_repo
    else:
        caller_repo = _git_repository_id(repo_root)

    is_skill_repository = (
        _path_is_within(cwd, skill_root) if repo_root is None
        else _same_git_repository(repo_root, skill_root)
    )
    if caller_repo is None and not is_skill_repository:
        raise ConfigError(_remote_config_choice_message(
            "cannot determine the caller repository"
        ))

    if explicit_config is not None:
        return explicit_config, caller_repo
    env_config = os.environ.get("GOALFLIGHT_REMOTE_CI_CONFIG")
    if env_config is not None:
        return Path(env_config), caller_repo

    if is_skill_repository:
        return skill_config, caller_repo

    caller_config = (
        repo_root / ".goal-flight" / "remote-ci.json" if repo_root is not None else None
    )
    detail = f"no implicit remote-CI config is selected for caller repository {caller_repo!r}"
    if caller_config is not None and caller_config.is_file():
        detail += f" (found {caller_config})"
    raise ConfigError(_remote_config_choice_message(detail))


def _run_local_command(
    command: Sequence[str], *,
    request_id: str,
    command_id: str,
    candidate_sha: str,
    route: str,
    route_reason: str,
    fallback_reason: str | None,
    result_dir: Path,
    error: str | None = None,
    timeout: float = 7200,
    cwd: Path | None = None,
    head_sha: str | None = None,
    snapshot_sha: str | None = None,
    dirty: bool = False,
    project_id: str | None = None,
    policy_outcome: str = "local",
    env: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    load_start, cores = _local_load()
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(command), cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )
        leader_identity = goalflight_compat.process_start_identity(proc.pid)
        try:
            process_group = os.getpgid(proc.pid)
        except OSError:
            process_group = None
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if not _signal_process_group(proc, process_group, leader_identity, signal.SIGTERM):
                with contextlib.suppress(OSError):
                    proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if not _signal_process_group(proc, process_group, leader_identity, signal.SIGKILL):
                    with contextlib.suppress(OSError):
                        proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=REMOTE_PROCESS_REAP_TIMEOUT_SECONDS)
            returncode = 124
    except OSError as exc:
        print(f"goalflight-remote-run: local command could not start: {exc}", file=sys.stderr)
        returncode = 127
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    wall_seconds = max(0.0, time.monotonic() - started)
    load_end, _ = _local_load()
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "hostname": socket.gethostname(),
        "status": "pass" if returncode == 0 else "fail",
        "returncode": returncode,
        "passed": 0,
        "failed": 0 if returncode == 0 else 1,
        "errors": 0,
        "failing_node_ids": [],
        "collection_import_errors": [],
        "first_error_lines": [],
        "box_id": None,
        "expected_hostname": None,
        "measured_hostname": socket.gethostname(),
        "tip_sha": candidate_sha,
        "candidate_sha": candidate_sha,
        "head_sha": head_sha or candidate_sha,
        "snapshot_sha": snapshot_sha if snapshot_sha is not None else (
            candidate_sha if not dirty else None
        ),
        "dirty": dirty,
        "command_id": command_id,
        "process_cpu_seconds": max(
            0.0,
            usage_after.ru_utime + usage_after.ru_stime
            - usage_before.ru_utime - usage_before.ru_stime,
        ),
        "wall_seconds": wall_seconds,
        "duration_seconds": wall_seconds,
        "load_average_start": load_start,
        "load_average_end": load_end,
        "route": route,
        "route_reason": route_reason,
        "fallback_reason": fallback_reason,
        "project_id": project_id,
        "policy_outcome": policy_outcome,
        "requirements_sha256": None,
        "venv_path": None,
        **_execution_receipt_metadata(command, (), os.environ),
    }
    if error:
        receipt["error"] = error
    try:
        _atomic_write_json(result_dir / f"{request_id}.local.json", {
            "schema": RESULT_SCHEMA,
            "request_id": request_id,
            "status": "LOCAL_FALLBACK" if fallback_reason else "LOCAL",
            "returncode": returncode,
            "project_id": project_id,
            "policy_outcome": policy_outcome,
            "receipt": receipt,
            "finished_at": _utc_now(),
        })
    except OSError:
        # The worker may have no spool write grant. The local command still
        # has a truthful exit status and receipt on stdout.
        pass
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return returncode, receipt


def _print_remote_result(result: Mapping[str, Any], *, include_result: bool = True) -> None:
    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            streams = value.get("streams")
            if isinstance(streams, Mapping):
                for name in ("stdout", "stderr"):
                    stream = streams.get(name)
                    tail = stream.get("tail") if isinstance(stream, Mapping) else None
                    text = tail.get("text") if isinstance(tail, Mapping) else None
                    if isinstance(text, str) and text:
                        target = sys.stderr if name == "stderr" else sys.stdout
                        print(text, end="" if text.endswith("\n") else "\n", file=target)
            for key, child in value.items():
                if key != "streams":
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(result)
    if include_result:
        print(json.dumps(result, sort_keys=True), flush=True)


def _remote_queue_lease(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    for name in ("candidate", "base"):
        outcome = value.get(name)
        lease = outcome.get("lease") if isinstance(outcome, Mapping) else None
        if isinstance(lease, Mapping):
            return lease
    return None


def _print_queue_progress(
    config: DaemonConfig, request_path: Path, request_id: str,
    waited_seconds: float, daemon_live: bool | None,
    result: Mapping[str, Any] | None,
) -> None:
    request: Mapping[str, Any] = {}
    try:
        loaded = json.loads(request_path.read_text(encoding="utf-8"))
        if isinstance(loaded, Mapping):
            request = loaded
    except (OSError, json.JSONDecodeError):
        pass
    resident = request.get("_resident")
    arms = resident.get("arms") if isinstance(resident, Mapping) else None
    arm = next((value for value in arms.values() if isinstance(value, Mapping)), {}) \
        if isinstance(arms, Mapping) else {}
    outcomes = [result.get(name) for name in ("candidate", "base")
                if isinstance(result, Mapping) and isinstance(result.get(name), Mapping)]
    lease = next((outcome["lease"] for outcome in outcomes
                  if isinstance(outcome.get("lease"), Mapping)), None)
    box = (
        lease.get("box") if isinstance(lease, Mapping) else None
    ) or arm.get("box") or request.get("box")
    box_name = box if isinstance(box, str) and box in config.boxes else None
    queue = sorted(
        (path for path in config.queue_dir.glob("*.json")
         if not path.name.endswith(".local.json")),
        key=lambda path: path.name,
    )
    index = next((position for position, path in enumerate(queue, 1)
                  if path.stem == request_id), None)
    position = f"{index}/{len(queue)}" if index is not None else "unknown"
    if isinstance(lease, Mapping):
        state = str(lease.get("state") or "submitted")
        ticket = lease.get("ticket")
        if isinstance(ticket, int) and not isinstance(ticket, bool):
            state += f" ticket={ticket}"
    else:
        state = str(arm.get("phase") or "waiting-for-daemon")
    daemon = "running" if daemon_live is True else "stopped" if daemon_live is False else "unknown"
    runner = RemoteRunner(config)
    targets = [box_name] if box_name else sorted(runner.nodes)

    def probe(name: str) -> str:
        node = runner.nodes[name]
        try:
            measured = node.call(
                "health", _transport_timeout=REMOTE_HEALTH_PROBE_TIMEOUT_SECONDS,
            )
        except RemoteCIError:
            return f"{name}:unknown"
        if isinstance(measured, Mapping) and measured.get("status") == "unknown":
            return f"{name}:unknown"
        load = measured.get("load") if isinstance(measured, Mapping) else None
        hostname = load.get("hostname") if isinstance(load, Mapping) else None
        expected = node.box.expected_hostname
        healthy = (
            isinstance(hostname, str) and bool(hostname.strip())
            and RemoteRunner._normalize_hostname(hostname)
            == RemoteRunner._normalize_hostname(expected)
        )
        status = "healthy" if healthy else "identity-mismatch"
        load1 = load.get("load1") if isinstance(load, Mapping) else None
        tokens = measured.get("tokens") if isinstance(measured, Mapping) else None
        free = tokens.get("free") if isinstance(tokens, Mapping) else None
        details = [status]
        if isinstance(hostname, str):
            details.append(f"host={hostname}")
        if isinstance(load1, (int, float)) and not isinstance(load1, bool):
            details.append(f"load1={load1:g}")
        if isinstance(free, int) and not isinstance(free, bool):
            details.append(f"free={free}")
        return f"{name}:" + ",".join(details)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, len(targets)), thread_name_prefix="remote-ci-progress",
    ) as probes:
        health = ";".join(probes.map(probe, targets))
    destination = box_name or "unassigned"
    health_label = f"box_health={health or 'unknown'}" if box_name else f"pool_health={health or 'unknown'}"
    print(
        f"goalflight-remote-run: request={request_id} state={state} "
        f"age={waited_seconds:.0f}s controller_queue={position} daemon={daemon} "
        f"box={destination} {health_label}",
        file=sys.stderr, flush=True,
    )


def _expired_queue_without_execution(value: Any) -> bool:
    if isinstance(value, Mapping):
        candidate = value.get("candidate")
        lease = candidate.get("lease") if isinstance(candidate, Mapping) else None
        completed = lease.get("result") if isinstance(lease, Mapping) else None
        if (isinstance(completed, Mapping)
                and completed.get("status") == "expired"
                and completed.get("command_started") is False):
            return True
    return False


def _remote_returncode(result: Mapping[str, Any]) -> int:
    status = str(result.get("status", "")).upper()
    codes: list[int] = []
    red_receipt = False
    found_receipt = False
    for key in ("candidate", "base"):
        value = result.get(key)
        if not isinstance(value, Mapping):
            continue
        arm_status = str(value.get("status", "")).lower()
        if arm_status and arm_status not in {"green", "pass", "passed"}:
            red_receipt = True
        receipt_value = value.get("receipt")
        if isinstance(receipt_value, Mapping):
            found_receipt = True
            try:
                receipt = parse_receipt(receipt_value)
            except ReceiptError:
                red_receipt = True
            else:
                red_receipt = red_receipt or not _receipt_is_green(receipt)
        elif status == "GREEN":
            red_receipt = True
        code = value.get("returncode")
        if isinstance(code, int) and not isinstance(code, bool):
            codes.append(code)
        nested = value.get("candidate")
        if isinstance(nested, Mapping):
            code = nested.get("returncode")
            if isinstance(code, int) and not isinstance(code, bool):
                codes.append(code)
    if status == "GREEN" and not found_receipt:
        red_receipt = True
    if status == "RED" or red_receipt:
        return next((code for code in codes if code != 0), 1)
    if status == "GREEN" and codes:
        return codes[0]
    value = result.get("returncode")
    return value if isinstance(value, int) and not isinstance(value, bool) else 2


def _zero_test_node_arms(result: Mapping[str, Any]) -> list[str]:
    empty: list[str] = []
    for name in ("candidate", "base"):
        outcome = result.get(name)
        receipt_value = outcome.get("receipt") if isinstance(outcome, Mapping) else None
        if not isinstance(receipt_value, Mapping):
            continue
        try:
            receipt = parse_receipt(receipt_value)
        except ReceiptError:
            continue
        if not _receipt_has_test_nodes(receipt):
            empty.append(name)
    return empty


def _zero_test_node_failure_message(result: Mapping[str, Any], arms: Sequence[str]) -> str:
    details: list[str] = []
    for name in arms:
        outcome = result.get(name)
        receipt_value = outcome.get("receipt") if isinstance(outcome, Mapping) else None
        if not isinstance(receipt_value, Mapping):
            continue
        try:
            receipt = parse_receipt(receipt_value)
        except ReceiptError:
            continue
        details.append(
            f"{name}: status={receipt.status} returncode={receipt.returncode} "
            f"passed={receipt.passed} failed={receipt.failed} skipped={receipt.skipped} "
            f"errors={receipt.errors}"
        )
    return (
        "goalflight-remote-run: harness failure: no verified pytest test-node count was recorded for "
        f"{', '.join(arms)} "
        f"({'; '.join(details) or ', '.join(arms)})"
    )


def _matched_pair_summary(result: Mapping[str, Any], result_path: Path) -> dict[str, Any]:
    arms: dict[str, Any] = {}
    for name in ("base", "candidate"):
        arm = result.get(name)
        arm = arm if isinstance(arm, Mapping) else {}
        receipt = arm.get("receipt")
        receipt = receipt if isinstance(receipt, Mapping) else {}
        lease = arm.get("lease")
        lease = lease if isinstance(lease, Mapping) else {}
        counts = {
            key: int(receipt.get(key, 0))
            for key in ("passed", "failed", "skipped")
            if isinstance(receipt.get(key, 0), int)
        }
        arms[name] = {
            "box_id": receipt.get("box_id") or lease.get("box") or lease.get("box_id"),
            "counts": counts,
        }
    return {
        "status": result.get("status"),
        "base": arms["base"],
        "candidate": arms["candidate"],
        "new_failure_node_ids": sorted(str(value) for value in result.get("new_failure_node_ids", [])),
        "fixed_failure_node_ids": sorted(str(value) for value in result.get("fixed_failure_node_ids", [])),
        "absent_at_base_node_ids": sorted(
            str(value) for value in result.get("absent_at_base_node_ids", [])
        ),
        "rechecks": result.get("rechecks", {}),
        "result_path": str(result_path),
    }


def _print_matched_pair_summary(summary: Mapping[str, Any]) -> None:
    print(f"matched-pair status: {summary.get('status')}")
    for name in ("base", "candidate"):
        arm = summary.get(name, {})
        arm = arm if isinstance(arm, Mapping) else {}
        counts = arm.get("counts", {})
        counts = counts if isinstance(counts, Mapping) else {}
        print(
            f"{name}: box={arm.get('box_id')} pass={counts.get('passed', 0)} "
            f"fail={counts.get('failed', 0)} skipped={counts.get('skipped', 0)}"
        )
    print(f"candidate-only failures: {', '.join(summary.get('new_failure_node_ids', [])) or '(none)'}")
    print(f"fixed failures: {', '.join(summary.get('fixed_failure_node_ids', [])) or '(none)'}")
    if summary.get("absent_at_base_node_ids"):
        print(f"absent at base: {', '.join(summary['absent_at_base_node_ids'])}")
    for node_id, value in summary.get("rechecks", {}).items():
        print(f"recheck {node_id}: {value.get('classification')}")
    print(f"result: {summary.get('result_path')}")


def wrapper_main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command_separator = arguments.index("--") if "--" in arguments else len(arguments)
    parser = argparse.ArgumentParser(prog="goalflight-remote-run")
    parser.add_argument("--kind", choices=("gate",), default="gate")
    parser.add_argument("--cmd-id", required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--pair", action="store_true")
    parser.add_argument("--base")
    parser.add_argument("--recheck", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(arguments)
    local_command = list(args.command)
    if local_command and local_command[0] == "--":
        local_command = local_command[1:]
    if not local_command:
        parser.error("a local command is required after --")
    if args.pair and not args.base:
        parser.error("--pair requires --base <commit>")
    if not args.pair and (args.base is not None or args.recheck or args.json):
        parser.error("--base, --recheck, and --json require --pair")
    if args.recheck < 0:
        parser.error("--recheck must be a non-negative integer")
    if args.recheck > 5:
        parser.error("--recheck must be at most 5")
    try:
        command_id = _safe_id(args.cmd_id, "cmd-id")
    except ConfigError as exc:
        parser.error(str(exc))
    request_id = uuid.uuid4().hex
    cwd = Path.cwd().resolve()
    dirty: bool | None = None
    repo_root: Path | None = None
    try:
        repo_root = Path(_git_output(cwd, ["rev-parse", "--show-toplevel"])).resolve()
        cwd_relative = cwd.relative_to(repo_root).as_posix()
        head_sha, expected_snapshot_sha, dirty = _worktree_snapshot_identity(repo_root)
        candidate_sha = head_sha
    except (RemoteCIError, OSError, ValueError) as exc:
        if dirty is not True and repo_root is not None:
            try:
                dirty = bool(_worktree_status(repo_root)) or _worktree_index_hides_changes(
                    repo_root,
                )
            except (RemoteCIError, OSError):
                pass
        denied_write = isinstance(exc, PermissionError) or any(
            message in str(exc).casefold()
            for message in ("operation not permitted", "permission denied", "read-only file system")
        )
        if dirty is True and denied_write:
            print(
                "goalflight-remote-run: read-only dispatch with uncommitted changes cannot submit; "
                "commit or run from a writable worker",
                file=sys.stderr,
            )
            return 2
        if repo_root is None and not _path_is_within(
            cwd, Path(__file__).resolve().parent.parent,
        ):
            print(
                "goalflight-remote-run: "
                + _remote_config_choice_message("cannot determine the caller repository"),
                file=sys.stderr,
            )
            return 2
        print(f"goalflight-remote-run: {exc}", file=sys.stderr)
        return 2
    try:
        config_path, caller_repo = _resolve_wrapper_config(args.config, cwd, repo_root)
    except ConfigError as exc:
        print(f"goalflight-remote-run: {exc}", file=sys.stderr)
        return 2
    spool_raw = str(os.environ.get("GOALFLIGHT_REMOTE_CI_SPOOL_DIR") or "").strip()
    spool_error = (
        os.environ.get("GOALFLIGHT_REMOTE_CI_SPOOL_ERROR") == "spool-unavailable"
    )
    spool = Path(spool_raw).expanduser() if spool_raw else goalflight_dispatch_paths.remote_ci_spool_dir()
    request_dir = spool / "requests"
    result_dir = spool / "results"
    config: DaemonConfig | None = None
    config_error: str | None = None
    route_reason = "configuration-unavailable"
    try:
        config = _load_local_config(config_path)
        if caller_repo is not None and config.repo != caller_repo:
            print(
                "goalflight-remote-run: "
                + _remote_config_choice_message(
                    f"config repo {config.repo!r} does not match caller repository "
                    f"{caller_repo!r}"
                ),
                file=sys.stderr,
            )
            return 2
        if spool_raw and (
            config.queue_dir.resolve(strict=False) != request_dir.resolve(strict=False)
            or config.result_dir.resolve(strict=False) != result_dir.resolve(strict=False)
        ):
            raise ConfigError("invalid-spool-layout")
    except (OSError, RemoteCIError, ValueError) as exc:
        config_error = str(exc)

    def run_local(*, route: str, reason: str, fallback_reason: str | None,
                  snapshot_info: Mapping[str, Any] | None = None,
                  error: str | None = None) -> int:
        timeout = config.runner.timeout_seconds if config else 7200
        local_env = None
        if config is not None:
            local_env = {**os.environ, **config.runner.env}
            for name, value in config.local_env.items():
                if value is None:
                    if name in os.environ:
                        local_env[name] = os.environ[name]
                    else:
                        local_env.pop(name, None)
                else:
                    local_env[name] = value
        try:
            local_head, local_snapshot, local_dirty = _worktree_snapshot_identity(repo_root)
        except (RemoteCIError, OSError) as exc:
            return remote_unavailable(
                "fallback-tree-changed",
                error=f"requested HEAD {head_sha}; current worktree could not be identified: {exc}",
                snapshot_info=snapshot_info,
            )
        if fallback_reason:
            if local_head != head_sha or local_snapshot != expected_snapshot_sha:
                return remote_unavailable(
                    "fallback-tree-changed",
                    error=(f"requested HEAD {head_sha} snapshot {expected_snapshot_sha}; "
                           f"current HEAD {local_head} snapshot {local_snapshot}"),
                    snapshot_info=snapshot_info,
                )
        result, _receipt = _run_local_command(
            local_command, request_id=request_id, command_id=command_id,
            candidate_sha=local_head, route=route,
            route_reason=reason, fallback_reason=fallback_reason,
            error=error,
            result_dir=request_dir, timeout=timeout,
            cwd=cwd, head_sha=local_head, snapshot_sha=local_snapshot,
            dirty=local_dirty,
            project_id=config.repo if config else None,
            policy_outcome="fell_back" if fallback_reason else "local",
            env=local_env,
        )
        return result

    queued_request: Path | None = None

    def cancel_queued(reason: str) -> None:
        if queued_request is not None and queued_request.exists():
            with contextlib.suppress(OSError):
                _mark_request_cancelled(config, request_id, reason)

    def remote_unavailable(
        reason: str, *, error: str | None = None,
        snapshot_info: Mapping[str, Any] | None = None,
    ) -> int:
        cancel_queued(reason)
        metadata = snapshot_info if isinstance(snapshot_info, Mapping) else {}
        error_text = error.strip() if isinstance(error, str) and error.strip() else reason
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "request_id": request_id,
            "hostname": socket.gethostname(),
            "status": "fail",
            "returncode": REMOTE_UNAVAILABLE_EXIT,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "failing_node_ids": [],
            "collection_import_errors": [],
            "first_error_lines": [],
            "project_id": config.repo if config else None,
            "policy_outcome": "failed",
            "route": "remote-unavailable",
            "route_reason": route_reason,
            "fallback_reason": reason,
            "error": error_text,
            "head_sha": metadata.get("head_sha", head_sha),
            "snapshot_sha": metadata.get("snapshot_sha", candidate_sha if not dirty else None),
            "dirty": metadata.get("dirty", dirty),
            "command_id": command_id,
        }
        result = {
            "schema": RESULT_SCHEMA,
            "request_id": request_id,
            "status": "REMOTE_UNAVAILABLE",
            "failure_type": "RemoteUnavailable",
            "returncode": REMOTE_UNAVAILABLE_EXIT,
            "project_id": config.repo if config else None,
            "policy_outcome": "failed",
            "fallback_reason": reason,
            "error": error_text,
            "receipt": receipt,
            "finished_at": _utc_now(),
        }
        try:
            _write_wrapper_result(result_dir, request_id, result)
        except OSError:
            pass
        print(json.dumps(receipt, sort_keys=True), flush=True)
        return REMOTE_UNAVAILABLE_EXIT

    def fallback_remote(
        reason: str, *, error: str | None = None,
        snapshot_info: Mapping[str, Any] | None = None,
        remote_ticket_accepted: bool | None = None,
    ) -> int:
        nonlocal queued_request
        if args.pair:
            return remote_unavailable(reason, error=error, snapshot_info=snapshot_info)
        if queued_request is not None:
            if (config is not None and remote_ticket_accepted is False
                    and _delete_request_checkpoint_files(config, queued_request)):
                queued_request = None
            else:
                return remote_unavailable(
                    "remote-ownership-unresolved",
                    error=f"request {request_id}: {error or reason}; local execution refused after submission",
                    snapshot_info=snapshot_info,
                )
        if (config is None and config_error
                and config_error.startswith(f"{PROFILE_ENV_UNAVAILABLE_REASON}:")):
            return remote_unavailable(
                PROFILE_ENV_UNAVAILABLE_REASON,
                error=config_error,
                snapshot_info=snapshot_info,
            )
        if (config is not None and config.fallback != "local") or (
            config is None and config_error and config_path.exists()
        ):
            return remote_unavailable(reason, error=error, snapshot_info=snapshot_info)
        cancel_queued(reason)
        return run_local(
            route="local-fallback", reason=route_reason,
            fallback_reason=reason, snapshot_info=snapshot_info, error=error,
        )

    force_local = os.environ.get("GOALFLIGHT_REMOTE_CPU_ROUTE", "").strip().lower() == "local"
    load, cores = _local_load()
    waiting = _dispatch_waiting()
    heavy = config is not None and command_id in config.runner.heavy_command_ids
    pressure_reason = (
        "dispatch-queue-pressure" if waiting
        else "laptop-load-pressure" if load and load[0] > cores
        else None
    )
    if force_local:
        route_reason = "forced-local"
        fallback_reason = None
    elif heavy:
        route_reason = "heavy-command-allowlist"
        fallback_reason = None
    elif pressure_reason:
        route_reason = pressure_reason
        fallback_reason = None
    else:
        route_reason = "laptop-idle"
        fallback_reason = None
    policy_requires_remote = config is not None and config.fallback != "local"
    if policy_requires_remote and force_local:
        route_reason = "policy-requires-remote"
    remote_first = args.pair or policy_requires_remote or (
        not force_local and (heavy or pressure_reason is not None)
    )
    if args.pair and force_local:
        return remote_unavailable(
            "forced-local", error="matched-pair requests require a remote Studio",
        )
    caller_pytest_reason = (
        "pytest-addopts-set" if os.environ.get("PYTEST_ADDOPTS")
        else "pytest-plugins-set" if os.environ.get("PYTEST_PLUGINS")
        else None
    )
    if config_error and (config is not None or config_path.exists()):
        return fallback_remote(config_error)
    if spool_error:
        return fallback_remote("spool-unavailable")
    if config is not None and config.profile_enabled and caller_pytest_reason:
        return fallback_remote(caller_pytest_reason)
    if config is None and config_error:
        if args.pair:
            return remote_unavailable(
                "configuration-unavailable", error=config_error,
            )
        return run_local(route="local-fallback", reason="configuration-unavailable",
                         fallback_reason=config_error or "config-unavailable")
    if not remote_first:
        return run_local(route="local", reason=route_reason, fallback_reason=None)
    remote_command = list(local_command)
    if config is not None and config.profile_enabled \
            and config.runner.pytest_workers_cap is not None:
        try:
            remote_command = list(_cap_profile_pytest_workers(
                remote_command, config.runner.pytest_workers_cap,
                label="profiled command", config=False,
            ))
        except ProfileIneligible as exc:
            return fallback_remote(exc.fallback_reason)
    initial_daemon_state = daemon_is_running(config)
    if initial_daemon_state is False:
        return fallback_remote(
            "daemon-unavailable" if not config_error else config_error,
        )
    if initial_daemon_state is None:
        reattach_deadline = time.monotonic() + DAEMON_REATTACH_WINDOW_SECONDS
        last_daemon_state = initial_daemon_state
        while time.monotonic() < reattach_deadline:
            last_daemon_state = daemon_is_running(config)
            if last_daemon_state is True:
                break
            time.sleep(min(config.poll_seconds, 0.2))
        else:
            if last_daemon_state is False:
                return fallback_remote("daemon-unavailable")
            return fallback_remote(
                "daemon-liveness-unknown",
                error="could not verify daemon liveness from the PID file or daemon lock",
            )
    payload = {
        "schema": REQUEST_SCHEMA,
        "request_id": request_id,
        "project_id": config.repo,
        "kind": "gate",
        "command_id": command_id,
        "command": remote_command,
        **({"base_sha": args.base, "recheck": args.recheck} if args.pair else {}),
        "repo_root": str(repo_root),
        "cwd_relative": "" if cwd_relative == "." else cwd_relative,
        "caller_env": {name: os.environ[name] for name in REMOTE_ENV_ALLOWLIST if name in os.environ},
        "receipt_metadata": {
            target: os.environ[source]
            for source, target in (
                ("GOALFLIGHT_DISPATCH_ID", "dispatch_id"),
                ("GOALFLIGHT_CONTROLLER_LABEL", "controller_label"),
            )
            if os.environ.get(source)
        },
        "caller_identity": goalflight_compat.process_start_identity(os.getpid()),
        "head_sha": head_sha,
        "snapshot_sha": expected_snapshot_sha,
        "dirty": dirty,
    }
    source_name: str | None = None
    try:
        # Refuse unsupported commands while local execution is still safe.
        validate_request(payload, config)
        descriptor, source_name = tempfile.mkstemp(
            prefix=".request.", suffix=".tmp", dir=str(request_dir)
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Publication may succeed before submit_request raises (for example,
        # directory fsync can fail). Treat the entire submission as ambiguous.
        queued_request = config.queue_dir / f"{request_id}.json"
        queued_request = submit_request(Path(source_name), config)
    except (OSError, RemoteCIError) as exc:
        return fallback_remote(f"spool-unavailable: {exc}")
    finally:
        if source_name:
            Path(source_name).unlink(missing_ok=True)
    result_wait_started = time.monotonic()
    counted_rechecks = 0
    executions = 2 if args.pair else 1
    result_deadline = result_wait_started + _remote_result_budget_seconds(
        config, executions=executions,
    )
    queue_budget_per_execution = (
        SNAPSHOT_SETUP_TIMEOUT_SECONDS + BUNDLE_BASIS_TIMEOUT_SECONDS
        + BUNDLE_PREPARATION_TIMEOUT_SECONDS + BUNDLE_TRANSFER_TIMEOUT_SECONDS
        + 2 * config.admission.max_wait_seconds
        + max(30.0, 2.0 * config.poll_seconds + 10.0)
    )
    queue_deadline = min(
        result_deadline, result_wait_started + queue_budget_per_execution * executions,
    )
    queue_deadline_active = config.admission_mode == "box"
    deadline = queue_deadline if queue_deadline_active else result_deadline
    next_progress_at = (
        result_wait_started + min(30.0, queue_budget_per_execution / 4)
        if queue_deadline_active else float("inf")
    )
    daemon_down_since: float | None = None
    with _cancel_request_on_signals(config, request_id):
        result_path = result_dir / f"{request_id}.json"
        while time.monotonic() < deadline:
            if args.pair and args.recheck and queued_request.is_file():
                try:
                    queued = json.loads(queued_request.read_text(encoding="utf-8"))
                    rechecked_ids = queued.get("_pair_rechecked_ids", 0)
                except (OSError, json.JSONDecodeError):
                    rechecked_ids = 0
                if (isinstance(rechecked_ids, int) and not isinstance(rechecked_ids, bool)
                        and rechecked_ids > counted_rechecks):
                    counted_rechecks = rechecked_ids
                    executions = 2 + 2 * args.recheck * counted_rechecks
                    result_deadline = result_wait_started + _remote_result_budget_seconds(
                        config, executions=executions,
                    )
                    queue_deadline = min(
                        result_deadline,
                        result_wait_started + queue_budget_per_execution * executions,
                    )
                    if queue_deadline_active:
                        deadline = queue_deadline
                    else:
                        deadline = result_deadline
            now = time.monotonic()
            if now >= next_progress_at:
                progress_result = None
                if result_path.is_file():
                    try:
                        candidate_progress = json.loads(
                            result_path.read_text(encoding="utf-8")
                        )
                        if isinstance(candidate_progress, Mapping):
                            progress_result = candidate_progress
                    except (OSError, json.JSONDecodeError):
                        pass
                progress_daemon_state = daemon_is_running(config)
                _print_queue_progress(
                    config, queued_request, request_id,
                    now - result_wait_started, progress_daemon_state, progress_result,
                )
                next_progress_at = now + 30.0
            if result_path.is_file():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    time.sleep(min(config.poll_seconds, 0.2))
                    continue
                if (not isinstance(result, Mapping)
                        or result.get("request_id") != request_id):
                    return fallback_remote("invalid-or-stale-daemon-result")
                if result.get("status") == "PROJECT_MISMATCH":
                    error = result.get("error")
                    return fallback_remote(
                        str(result.get("fallback_reason") or "project-mismatch"),
                        error=error if isinstance(error, str) else None,
                        snapshot_info=result.get("snapshot"),
                    )
                if result.get("status") == "LOCAL_FALLBACK_REQUIRED":
                    _print_remote_result(result)
                    reason = str(result.get("fallback_reason") or "remote-capacity-unavailable")
                    error = result.get("error")
                    remote_ticket_accepted = result.get("remote_ticket_accepted")
                    if (not args.pair and reason == "candidate-timeout"
                            and remote_ticket_accepted is False
                            and _expired_queue_without_execution(result)):
                        return fallback_remote(
                            "remote-admission-deadline",
                            error="remote ticket expired before the test command started",
                            snapshot_info=result.get("snapshot"),
                            remote_ticket_accepted=False,
                        )
                    if (reason == "health-unknown" and queued_request is not None
                            and remote_ticket_accepted is None):
                        return remote_unavailable(
                            reason,
                            error=(f"request {request_id}: {error or reason}; "
                                   "local execution refused after submission"),
                            snapshot_info=result.get("snapshot"),
                        )
                    return fallback_remote(
                        reason, error=error if isinstance(error, str) else None,
                        snapshot_info=result.get("snapshot"),
                        remote_ticket_accepted=remote_ticket_accepted,
                    )
                if result.get("status") == "SUBMITTED":
                    lease = _remote_queue_lease(result)
                    if (isinstance(lease, Mapping)
                            and lease.get("state") not in {"queued", "uploading"}):
                        queue_deadline_active = False
                        deadline = result_deadline
                    time.sleep(min(config.poll_seconds, 0.2))
                    continue
                if result.get("status") not in {"GREEN", "RED"}:
                    error = result.get("error")
                    return fallback_remote(
                        str(result.get("error") or "remote-run-failed"),
                        error=error if isinstance(error, str) else None,
                        snapshot_info=result.get("snapshot"),
                        remote_ticket_accepted=result.get("remote_ticket_accepted"),
                    )
                if args.pair:
                    missing_pair_parts: list[str] = []
                    for name in ("base", "candidate"):
                        arm = result.get(name)
                        if not isinstance(arm, Mapping):
                            missing_pair_parts.append(f"{name} arm")
                            continue
                        if not str(arm.get("status", "")).strip():
                            missing_pair_parts.append(f"{name} status")
                        if not isinstance(arm.get("receipt"), Mapping):
                            missing_pair_parts.append(f"{name} receipt")
                    if missing_pair_parts:
                        print(
                            "goalflight-remote-run: harness failure: incomplete matched-pair result; "
                            f"missing {', '.join(missing_pair_parts)}",
                            file=sys.stderr,
                        )
                        return 1
                    empty_arms = _zero_test_node_arms(result)
                    if empty_arms:
                        print(_zero_test_node_failure_message(result, empty_arms), file=sys.stderr)
                        return 1
                    summary = _matched_pair_summary(result, result_path)
                    result_returncode = _remote_returncode(result)
                    if result_returncode and summary["status"] == "GREEN":
                        summary["status"] = "RED"
                    if args.json:
                        print(json.dumps(summary, sort_keys=True), flush=True)
                    else:
                        _print_matched_pair_summary(summary)
                    return result_returncode
                empty_arms = _zero_test_node_arms(result)
                if empty_arms:
                    _print_remote_result(result, include_result=False)
                    print(_zero_test_node_failure_message(result, empty_arms), file=sys.stderr)
                    return 1
                result_returncode = _remote_returncode(result)
                if result_returncode and result.get("status") == "GREEN":
                    _print_remote_result(result, include_result=False)
                    print(
                        "goalflight-remote-run: result declared GREEN but its receipt records "
                        "a red outcome; refusing GREEN",
                        file=sys.stderr,
                    )
                    return result_returncode
                _print_remote_result(result)
                return result_returncode
            daemon_live = daemon_is_running(config)
            if daemon_live is True:
                daemon_down_since = None
            else:
                if daemon_down_since is None:
                    daemon_down_since = time.monotonic()
                elif time.monotonic() - daemon_down_since >= DAEMON_REATTACH_WINDOW_SECONDS:
                    if daemon_live is False:
                        return fallback_remote("daemon-died")
                    return fallback_remote(
                        "daemon-liveness-unknown",
                        error=("could not verify daemon liveness from the PID file or daemon lock "
                               "after submission"),
                    )
            time.sleep(min(config.poll_seconds, 0.2))
        if queue_deadline_active:
            return fallback_remote(
                "remote-admission-deadline",
                error=(f"request {request_id} did not reach an admitted remote command "
                       "within the bounded queue window"),
            )
        return fallback_remote("remote-result-deadline")


def _resolve_commit(value: str | None, cwd: Path) -> str:
    if value is None:
        raise RemoteCIError("missing commit")
    sha = _git_output(
        cwd, ["rev-parse", "--verify", f"{value}^{{commit}}"], timeout=15,
    ).lower()
    if not _SHA_RE.fullmatch(sha):
        raise RemoteCIError(f"cannot resolve commit {value!r}")
    return sha


def _snapshot_git_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    values = dict(os.environ if env is None else env)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_NAMESPACE"):
        values.pop(name, None)
    if env is None or "GIT_INDEX_FILE" not in env:
        values.pop("GIT_INDEX_FILE", None)
    return values


def _git_output(
    cwd: Path, argv: Sequence[str], *, env: Mapping[str, str] | None = None,
    timeout: float = 120, deadline: float | None = None,
) -> str:
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemoteCIError("snapshot setup deadline exceeded")
        timeout = min(timeout, remaining)
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *argv], capture_output=True, text=True,
            env=_snapshot_git_environment(env), timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteCIError(f"git {' '.join(argv)} exceeded {timeout:g}s") from exc
    if result.returncode != 0:
        raise RemoteCIError((result.stderr or result.stdout or "git command failed").strip())
    return result.stdout.rstrip("\n")


def _snapshot_timeout(deadline: float | None, default: float = 30) -> float:
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RemoteCIError("snapshot setup deadline exceeded")
    return min(default, remaining)


def _is_git_lfs_pointer(content: bytes) -> bool:
    lines = content.splitlines()
    if len(lines) < 3 or lines[0] != b"version https://git-lfs.github.com/spec/v1":
        return False
    index = 1
    while index < len(lines) and lines[index].startswith(b"ext-"):
        index += 1
    return (
        index + 1 < len(lines)
        and re.fullmatch(rb"oid sha256:[0-9a-f]{64}", lines[index]) is not None
        and re.fullmatch(rb"size [0-9]+", lines[index + 1]) is not None
    )


def _symlink_target_escapes_snapshot(
    relative: str, target: str, symlinks: Mapping[str, str] | None = None,
    *, root: Path | None = None,
) -> bool:
    if target.startswith("/"):
        return True
    if root is not None:
        link = root.joinpath(*PurePosixPath(relative).parts)
        try:
            if link.is_symlink() and os.readlink(link) == target:
                resolved = link.resolve(strict=False)
                root_stat = root.stat()
                candidate = resolved
                while True:
                    try:
                        info = candidate.stat()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        return True
                    else:
                        if (info.st_dev, info.st_ino) == (root_stat.st_dev, root_stat.st_ino):
                            return False
                    if candidate == candidate.parent:
                        return True
                    candidate = candidate.parent
        except (OSError, RuntimeError):
            return True
    links = symlinks or {}
    parts = [part for part in posixpath.dirname(relative).split("/") if part]
    pending = target.split("/")
    expansions = 0
    while pending:
        part = pending.pop(0)
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return True
            parts.pop()
            continue
        parts.append(part)
        chained = links.get("/".join(parts))
        if chained is not None:
            expansions += 1
            if expansions > 40:
                return False
            parts.pop()
            if chained.startswith("/"):
                return True
            pending = chained.split("/") + pending
    return False


def _inspect_worktree_paths(
    root: Path, paths: Sequence[str], ignored_dirs: Sequence[str],
    tracked_paths: Sequence[str] = (),
) -> dict[str, Any]:
    symlinks: dict[str, str] = {}
    for relative in paths:
        path = root / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            return {"reason": "repository changed during snapshot inspection"}
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if len(os.fsencode(target)) > 4096:
                return {"reason": "repository contains an oversized symlink target"}
            symlinks[relative] = target
            continue
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            return {"reason": "repository contains unsupported special files"}
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                return {"reason": "repository contains unsupported special files"}
            if path.name == ".gitattributes":
                if info.st_size > 1024 * 1024:
                    return {"reason": "repository has oversized .gitattributes that cannot be inspected safely"}
                content = os.read(descriptor, 1024 * 1024 + 1)
                if re.search(rb"(?:^|\s)filter\s*=\s*lfs(?:\s|$)", content, re.MULTILINE):
                    return {"reason": "repository uses Git LFS"}
            elif _is_git_lfs_pointer(os.read(descriptor, 4096)):
                return {"reason": "repository contains Git LFS pointer files"}
        finally:
            os.close(descriptor)
    ignored = {value.rstrip("/") for value in ignored_dirs}
    known_paths = set(paths)
    tracked = set(tracked_paths)
    untracked_nested_repositories: list[str] = []

    walk_changed = False

    def fail_walk(error: OSError) -> None:
        nonlocal walk_changed
        if isinstance(error, (FileNotFoundError, NotADirectoryError)):
            walk_changed = True
            return
        raise error

    for directory, subdirectories, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=fail_walk,
    ):
        relative_dir = Path(directory).relative_to(root).as_posix()
        prefix = "" if relative_dir == "." else relative_dir + "/"
        if prefix and (".git" in subdirectories or ".git" in filenames):
            tracked_here = any(
                path == relative_dir or path.startswith(relative_dir + "/")
                for path in tracked
            )
            if not tracked_here:
                untracked_nested_repositories.append(relative_dir)
                subdirectories[:] = []
                continue
        subdirectories[:] = [name for name in subdirectories
                             if name != ".git"
                             and (prefix + name) not in ignored]
        for name in filenames:
            relative = prefix + name
            if relative in ignored or relative in known_paths:
                continue
            try:
                mode = (Path(directory) / name).lstat().st_mode
            except FileNotFoundError:
                continue
            if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                return {"reason": "repository contains unsupported special files"}
    if walk_changed:
        return {"reason": "repository changed during snapshot inspection"}
    for relative, target in symlinks.items():
        if _symlink_target_escapes_snapshot(relative, target, symlinks, root=root):
            reason = ("repository contains absolute symlink targets"
                      if target.startswith("/") else
                      "repository contains relative symlink targets outside the snapshot")
            return {"reason": reason}
    return {"untracked_nested_repositories": untracked_nested_repositories}


def _inspect_worktree_paths_bounded(
    root: Path, paths: Sequence[str], ignored_dirs: Sequence[str],
    *, tracked_paths: Sequence[str] = (), deadline: float | None,
) -> dict[str, Any]:
    script = (
        "import json,sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r}); "
        "import goalflight_remote_ci as ci; "
        "data=json.load(sys.stdin); "
        "print(json.dumps(ci._inspect_worktree_paths(Path(sys.argv[1]), "
        "data['paths'], data['ignored_dirs'], data['tracked_paths'])))"
    )
    timeout = _snapshot_timeout(deadline)
    try:
        inspected = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            input=json.dumps({
                "paths": list(paths), "ignored_dirs": list(ignored_dirs),
                "tracked_paths": list(tracked_paths),
            }),
            capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteCIError("snapshot setup deadline exceeded while inspecting worktree") from exc
    if inspected.returncode != 0:
        raise RemoteCIError("could not inspect worktree snapshot content")
    try:
        result = json.loads(inspected.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteCIError("could not inspect worktree snapshot content") from exc
    if not isinstance(result, dict):
        raise RemoteCIError("could not inspect worktree snapshot content")
    return result


def _unsupported_snapshot_reason(
    root: Path, *, deadline: float | None = None,
    excluded_nested_repositories: list[str] | None = None,
) -> str | None:
    git_env = _snapshot_git_environment()
    staged = _git_output(root, ["ls-files", "--stage", "-z"], deadline=deadline)
    tracked_paths = [
        entry.partition("\t")[2] for entry in staged.split("\0") if entry
    ]
    if any(entry.split(" ", 1)[0] == "160000"
           for entry in staged.split("\0") if entry):
        return "repository contains submodules"
    for revision in ("HEAD",):
        tree = _git_output(root, ["ls-tree", "-r", "-z", revision], deadline=deadline)
        entries = [entry for entry in tree.split("\0") if entry]
        symlinks: dict[str, str] = {}
        for entry in entries:
            _snapshot_timeout(deadline)
            metadata, _separator, relative = entry.partition("\t")
            mode, _kind, object_id = metadata.split(" ", 2)
            if mode == "160000":
                return "repository contains submodules"
            if mode == "120000":
                try:
                    object_size = subprocess.run(
                        ["git", "-C", str(root), "cat-file", "-s", object_id],
                        capture_output=True, env=git_env,
                        timeout=_snapshot_timeout(deadline),
                    )
                    if object_size.returncode != 0 or not object_size.stdout.strip().isdigit():
                        raise RemoteCIError("could not inspect symlink targets")
                    if int(object_size.stdout) > 4096:
                        return "repository contains an oversized symlink target"
                    target = subprocess.run(
                        ["git", "-C", str(root), "cat-file", "blob", object_id],
                        capture_output=True, env=git_env,
                        timeout=_snapshot_timeout(deadline),
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise RemoteCIError("could not inspect symlink targets") from exc
                if target.returncode != 0:
                    raise RemoteCIError("could not inspect symlink targets")
                symlinks[relative] = os.fsdecode(target.stdout)
        for relative, target in symlinks.items():
            if _symlink_target_escapes_snapshot(relative, target, symlinks):
                reason = ("repository contains absolute symlink targets"
                          if target.startswith("/") else
                          "repository contains relative symlink targets outside the snapshot")
                return reason
        attributes = [entry.partition("\t")[2] for entry in entries
                      if entry.endswith(".gitattributes")]
        for relative in attributes:
            _snapshot_timeout(deadline)
            found = subprocess.run(
                ["git", "-C", str(root), "grep", "-I", "-E", "-e",
                 r"(^|[[:space:]])filter[[:space:]]*=[[:space:]]*lfs([[:space:]]|$)",
                 revision, "--", relative],
                capture_output=True, env=git_env,
                timeout=_snapshot_timeout(deadline),
            )
            if found.returncode == 0:
                return "repository uses Git LFS"
            if found.returncode not in {1}:
                raise RemoteCIError("could not inspect Git LFS attributes")
        pointers = subprocess.run(
            ["git", "-C", str(root), "grep", "-I", "-l", "-z", "-E", "-e",
             "^version https://git-lfs.github.com/spec/v1$", revision, "--"],
            capture_output=True, env=git_env,
            timeout=_snapshot_timeout(deadline),
        )
        if pointers.returncode == 0:
            for relative in pointers.stdout.split(b"\0"):
                _snapshot_timeout(deadline)
                if not relative:
                    continue
                prefix = os.fsencode(revision) + b":"
                if relative.startswith(prefix):
                    relative = relative[len(prefix):]
                object_spec = f"{revision}:{os.fsdecode(relative)}"
                size = subprocess.run(
                    ["git", "-C", str(root), "cat-file", "-s", object_spec],
                    capture_output=True, env=git_env,
                    timeout=_snapshot_timeout(deadline),
                )
                if size.returncode != 0:
                    raise RemoteCIError("could not inspect Git LFS pointers")
                if not size.stdout.strip().isdigit() or int(size.stdout) > 4096:
                    continue
                content = subprocess.run(
                    ["git", "-C", str(root), "cat-file", "blob",
                     object_spec],
                    capture_output=True, env=git_env,
                    timeout=_snapshot_timeout(deadline),
                )
                if content.returncode != 0:
                    raise RemoteCIError("could not inspect Git LFS pointers")
                if _is_git_lfs_pointer(content.stdout):
                    return "repository contains Git LFS pointer files"
        if pointers.returncode not in {0, 1}:
            raise RemoteCIError((pointers.stderr or "could not inspect Git LFS pointers").strip())
    paths = _git_output(
        root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        deadline=deadline,
    )
    ignored_dirs = _git_output(
        root, ["ls-files", "--others", "--ignored", "--exclude-standard",
              "--directory", "-z"],
        deadline=deadline,
    )
    worktree = _inspect_worktree_paths_bounded(
        root, [path for path in paths.split("\0") if path],
        [path for path in ignored_dirs.split("\0") if path],
        tracked_paths=tracked_paths, deadline=deadline,
    )
    if excluded_nested_repositories is not None:
        excluded_nested_repositories.extend(
            str(path) for path in worktree.get("untracked_nested_repositories", [])
        )
    if worktree.get("reason"):
        return str(worktree["reason"])
    return None


def _snapshot_tree_sha(
    root: Path, head_sha: str, excluded_nested_repositories: Sequence[str] = (),
    *, deadline: float | None = None,
) -> str:
    """Return the Git tree identity used by remote snapshots without touching the caller index."""
    with tempfile.TemporaryDirectory(
        prefix="goalflight-remote-ci-tree-", dir=os.environ.get("TMPDIR") or None,
    ) as scratch:
        index_path = Path(scratch) / "index"
        git_env = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
        _git_output(root, ["read-tree", head_sha], env=git_env, deadline=deadline)
        pathspecs = [".", *(
            f":(top,exclude,literal){path}" for path in excluded_nested_repositories
        )]
        _git_output(root, ["-c", "core.filemode=true", "add", "-A", "--", *pathspecs],
                    env=git_env, deadline=deadline)
        tree_sha = _git_output(root, ["write-tree"], env=git_env, deadline=deadline)
    if not _SHA_RE.fullmatch(tree_sha):
        raise RemoteCIError("cannot identify worktree snapshot tree")
    return tree_sha.lower()


def _worktree_status(root: Path, *, deadline: float | None = None) -> str:
    return _git_output(
        root,
        ["-c", "core.filemode=true", "status", "--porcelain", "--untracked-files=all"],
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        deadline=deadline,
    )


def _worktree_index_hides_changes(root: Path, *, deadline: float | None = None) -> bool:
    entries = _git_output(
        root, ["ls-files", "-v", "-z"],
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        deadline=deadline,
    )
    return any(
        entry and (entry[0].islower() or entry[0].upper() == "S")
        for entry in entries.split("\0")
    )


def _worktree_snapshot_tree_sha(
    root: Path, head_sha: str, *, dirty: bool | None = None,
) -> str:
    excluded_nested_repositories: list[str] = []
    _unsupported_snapshot_reason(
        root, excluded_nested_repositories=excluded_nested_repositories,
    )
    if dirty is None:
        dirty = bool(_worktree_status(root)) or _worktree_index_hides_changes(root)
    elif not dirty:
        dirty = _worktree_index_hides_changes(root)
    if not dirty:
        tree_sha = _git_output(root, ["rev-parse", f"{head_sha}^{{tree}}"])
        if not _SHA_RE.fullmatch(tree_sha):
            raise RemoteCIError("cannot identify worktree snapshot tree")
        return tree_sha.lower()
    return _snapshot_tree_sha(root, head_sha, excluded_nested_repositories)


def _worktree_snapshot_identity(root: Path) -> tuple[str, str, bool]:
    head_sha = _resolve_commit("HEAD", root)
    status = _worktree_status(root)
    hidden_index_state = _worktree_index_hides_changes(root)
    dirty = bool(status) or hidden_index_state
    snapshot_sha = _worktree_snapshot_tree_sha(root, head_sha, dirty=dirty)

    confirmed_head = _resolve_commit("HEAD", root)
    confirmed_status = _worktree_status(root)
    confirmed_hidden_index_state = _worktree_index_hides_changes(root)
    if (confirmed_head != head_sha or confirmed_status != status
            or confirmed_hidden_index_state != hidden_index_state):
        # The worktree changed after the initial status check. Build its tree
        # from a temporary index, then make sure that snapshot stayed stable.
        snapshot_sha = _worktree_snapshot_tree_sha(root, confirmed_head, dirty=True)
        final_head = _resolve_commit("HEAD", root)
        final_status = _worktree_status(root)
        final_hidden_index_state = _worktree_index_hides_changes(root)
        if (final_head != confirmed_head or final_status != confirmed_status
                or final_hidden_index_state != confirmed_hidden_index_state):
            raise RemoteCIError("worktree changed while identifying snapshot")
        return confirmed_head, snapshot_sha, bool(final_status) or final_hidden_index_state
    return head_sha, snapshot_sha, dirty


def _snapshot_worktree(
    cwd: Path, request_id: str, bundle_dir: Path, *,
    cwd_relative: str, caller_env: Mapping[str, str],
    setup_deadline: float | None = None,
) -> dict[str, Any]:
    """Create a daemon-owned snapshot ref without touching the worker's index."""
    root = Path(_git_output(
        cwd, ["rev-parse", "--show-toplevel"], deadline=setup_deadline,
    )).resolve()
    relative_cwd = Path(cwd_relative)
    if relative_cwd.is_absolute() or ".." in relative_cwd.parts:
        raise RemoteCIError("cwd_relative must stay inside the repository")
    workdir = (root / relative_cwd).resolve()
    _snapshot_timeout(setup_deadline)
    if root not in workdir.parents and workdir != root:
        raise RemoteCIError("cwd_relative resolves outside the repository")
    if not workdir.is_dir():
        raise RemoteCIError("forwarded working directory does not exist in the repository")
    excluded_nested_repositories: list[str] = []
    unsupported = _unsupported_snapshot_reason(
        root, deadline=setup_deadline,
        excluded_nested_repositories=excluded_nested_repositories,
    )
    if unsupported:
        raise RemoteCIError(unsupported)
    head_sha = _git_output(
        root, ["rev-parse", "--verify", "HEAD^{commit}"], deadline=setup_deadline,
    ).lower()
    if not _SHA_RE.fullmatch(head_sha):
        raise RemoteCIError("cannot resolve HEAD commit")
    status = _worktree_status(root, deadline=setup_deadline)
    dirty = bool(status) or _worktree_index_hides_changes(root, deadline=setup_deadline)
    tree_sha = _snapshot_tree_sha(
        root, head_sha, excluded_nested_repositories, deadline=setup_deadline,
    )
    snapshot_sha = head_sha
    ref = f"refs/goalflight/remote-ci/{request_id}"
    if dirty:
        if any(
            entry.split(" ", 1)[0] == "160000"
            for entry in _git_output(
                root, ["ls-tree", "-r", "-z", tree_sha], deadline=setup_deadline,
            ).split("\0") if entry
        ):
            raise RemoteCIError("snapshot contains submodules")
        commit_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Goal Flight Remote CI",
            "GIT_AUTHOR_EMAIL": "goalflight-remote-ci@localhost",
            "GIT_COMMITTER_NAME": "Goal Flight Remote CI",
            "GIT_COMMITTER_EMAIL": "goalflight-remote-ci@localhost",
        }
        snapshot_sha = _git_output(
            root, ["commit-tree", tree_sha, "-p", head_sha, "-m", "Remote CI worktree snapshot"],
            env=commit_env, deadline=setup_deadline,
        )
    zero = "0" * len(head_sha)
    _git_output(root, ["update-ref", ref, snapshot_sha, zero], deadline=setup_deadline)
    bundle_path = bundle_dir / f"{request_id}.bundle"
    return {
        "root": root,
        "head_sha": head_sha,
        "snapshot_sha": snapshot_sha,
        "tree_sha": tree_sha,
        "dirty": dirty,
        "ref": ref,
        "bundle_path": bundle_path,
        "cwd_relative": "" if relative_cwd == Path(".") else relative_cwd.as_posix(),
        "caller_env": dict(caller_env),
    }


def _create_snapshot_bundle(
    root: Path, ref: str, bundle_path: Path, snapshot_sha: str,
    basis_commit: str | None, *, extra_refs: Sequence[str] = (),
    deadline: float | None = None,
) -> Path | None:
    """Build a full or incremental bundle only after the selected node is known."""
    _snapshot_timeout(deadline)
    basis = None
    if isinstance(basis_commit, str) and _SHA_RE.fullmatch(basis_commit.lower()):
        basis_commit = basis_commit.lower()
        try:
            _git_output(
                root, ["cat-file", "-e", f"{basis_commit}^{{commit}}"],
                timeout=BUNDLE_PREPARATION_TIMEOUT_SECONDS,
                deadline=deadline,
            )
            ancestry = subprocess.run(
                ["git", "-C", str(root), "merge-base", "--is-ancestor",
                 basis_commit, snapshot_sha],
                capture_output=True, text=True,
                env=_snapshot_git_environment(),
                timeout=_snapshot_timeout(deadline),
            )
        except (OSError, subprocess.SubprocessError, RemoteCIError):
            ancestry = None
        if ancestry is not None and ancestry.returncode == 0:
            if basis_commit == snapshot_sha and not extra_refs:
                bundle_path.unlink(missing_ok=True)
                return None
            basis = basis_commit
    temporary_bundle = bundle_path.with_name(f".{bundle_path.name}.tmp")
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        args = ["bundle", "create", str(temporary_bundle), ref, *extra_refs]
        if basis is not None:
            args.append(f"^{basis}")
        _git_output(
            root, args, timeout=BUNDLE_PREPARATION_TIMEOUT_SECONDS,
            deadline=deadline,
        )
        os.chmod(temporary_bundle, 0o600)
        os.replace(temporary_bundle, bundle_path)
    except BaseException:
        temporary_bundle.unlink(missing_ok=True)
        raise
    return bundle_path


def _release_snapshot(root: Path, ref: str, sha: str, bundle_path: Path) -> None:
    with contextlib.suppress(OSError, RemoteCIError):
        subprocess.run(
            ["git", "-C", str(root), "update-ref", "-d", ref, sha],
            capture_output=True, text=True, env=_snapshot_git_environment(), timeout=5,
        )
    bundle_path.unlink(missing_ok=True)


def health_census(config: DaemonConfig, *, executor: Any = None) -> dict[str, Any]:
    runner = RemoteRunner(config, executor=executor)
    try:
        daemon_status = json.loads(
            (config.state_dir / "daemon.status.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        daemon_status = {}
    if not isinstance(daemon_status, Mapping):
        daemon_status = {}
    boxes = []
    for name, node in runner.nodes.items():
        try:
            value = node.call("health")
            boxes.append({"box": name, "status": "ok", **value})
        except RemoteCIError as exc:
            boxes.append({"box": name, "status": "unknown", "error": str(exc)})
    return {
        "schema": "goalflight.remote-ci.health.v1",
        "measured_at": _utc_now(),
        "daemon": {
            "status": daemon_status.get("status", "unknown"),
            "drain_timeout_reported": bool(daemon_status.get("drain_timeout_reported", False)),
        },
        "boxes": boxes,
    }


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
    subparsers.add_parser("health")
    subparsers.add_parser("list", help="list durable remote leases")
    subparsers.add_parser("reap")
    clear = subparsers.add_parser("clear", help="audit and release a dead holder")
    clear.add_argument("--box", required=True)
    clear.add_argument("--run-dir", required=True)
    clear.add_argument("--lease-token", required=True)
    clear.add_argument("--pid", required=True)
    clear.add_argument("--start-token", required=True)
    gc = subparsers.add_parser("gc", help="show or delete retained run bodies")
    gc.add_argument("--apply", action="store_true")
    legacy_slots = subparsers.add_parser(
        "cleanup-legacy-slots", help="remove old full-copy slot directories after the CoW soak",
    )
    legacy_slots.add_argument("--box", required=True)
    legacy_slots.add_argument("--apply", action="store_true")
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
        elif args.command == "health":
            print(json.dumps(health_census(config), indent=2, sort_keys=True))
        elif args.command == "list":
            print(json.dumps(list_remote_leases(config), indent=2, sort_keys=True))
        elif args.command == "reap":
            print(json.dumps(RemoteRunner(config).reap(), indent=2, sort_keys=True))
        elif args.command == "clear":
            runner = RemoteRunner(config)
            configured_node = runner.nodes[args.box]
            leases = configured_node.call("list")
            matches = [record for record in leases
                       if record.get("run_directory") == args.run_dir
                       and record.get("lease_token") == args.lease_token]
            if len(matches) != 1:
                raise RemoteCIError("clear requires one matching durable lease")
            lease_node = runner._node_for_lease(configured_node, matches[0])
            print(json.dumps(lease_node.call(
                "clear",
                run_dir=args.run_dir,
                lease_token=args.lease_token,
                identity={"pid": args.pid, "start_token": args.start_token, "run_dir": args.run_dir},
                operator=RemoteRunner._owner(),
            ), sort_keys=True))
        elif args.command == "gc":
            runner = RemoteRunner(config)
            print(json.dumps([
                {"box": name, "rows": node.call("gc", apply=bool(args.apply))}
                for name, node in runner.nodes.items()
            ], indent=2, sort_keys=True))
        elif args.command == "cleanup-legacy-slots":
            runner = RemoteRunner(config)
            if args.box not in runner.nodes:
                raise RemoteCIError(f"unknown configured box: {args.box}")
            result = runner.nodes[args.box].call(
                "cleanup-legacy-slots", repo=config.repo, apply=bool(args.apply),
            )
            print(json.dumps({"box": args.box, **result}, indent=2, sort_keys=True))
        return 0
    except (RemoteCIError, OSError) as exc:
        print(f"goalflight-remote-ci: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
