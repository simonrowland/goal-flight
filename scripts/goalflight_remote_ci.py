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
import shlex
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
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


CONFIG_SCHEMA = "goalflight.remote-ci.config.v2"
REQUEST_SCHEMA = "goalflight.remote-ci.request.v1"
RECEIPT_SCHEMA = "goalflight.remote-ci.receipt.v1"
RESULT_SCHEMA = "goalflight.remote-ci.result.v1"

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


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds")


def _owner_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class BoxConfig:
    name: str
    host: str
    p_cores: int
    token_pool_size: int
    remote_exec: tuple[str, ...]
    env: dict[str, str]
    # Every run/export path is minted below this node-local canonical root.
    managed_run_directory: Path = Path("/var/lib/goalflight/remote-ci/runs")


@dataclass(frozen=True)
class AdmissionConfig:
    queue_wait_seconds: float


@dataclass(frozen=True)
class RunnerConfig:
    command: tuple[str, ...]
    test_command: tuple[str, ...]
    env: dict[str, str]
    timeout_seconds: float
    self_cap: int
    self_cap_env: str
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
            removed = {"token_key", "load_command"} & set(box)
            if removed:
                raise ConfigError(
                    f"obsolete v2 box fields: {', '.join(sorted(removed))}"
                )
            host = _string(box.get("host"), f"boxes.{name}.host")
            remote_exec = _command(box.get("remote_exec"), f"boxes.{name}.remote_exec")
            if "{script}" not in remote_exec:
                raise ConfigError("remote_exec must include a separate {script} argument")
            boxes[name] = BoxConfig(
                name=name,
                host=host,
                p_cores=_positive_int(box.get("p_cores"), f"boxes.{name}.p_cores"),
                token_pool_size=_positive_int(
                    box.get("token_pool_size"), f"boxes.{name}.token_pool_size"
                ),
                remote_exec=remote_exec,
                env=_string_map(box.get("env", {}), f"boxes.{name}.env"),
                managed_run_directory=_managed_path(
                    box.get("managed_run_directory"),
                    f"boxes.{name}.managed_run_directory",
                ),
            )

        admission = _mapping(required("admission"), "admission")
        if any(key in admission for key in ("token_directory", "live_cap_file")):
            raise ConfigError("v2 admission state and caps belong under the node managed root")
        admission_config = AdmissionConfig(
            queue_wait_seconds=_positive_number(
                admission.get("queue_wait_seconds", 300),
                "admission.queue_wait_seconds",
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
        runner_config = RunnerConfig(
            command=_command(runner.get("command"), "runner.command"),
            test_command=_command(runner.get("test_command"), "runner.test_command"),
            env=_string_map(runner.get("env", {}), "runner.env"),
            timeout_seconds=_positive_number(
                runner.get("timeout_seconds", 7200), "runner.timeout_seconds"
            ),
            self_cap=_positive_int(runner.get("self_cap"), "runner.self_cap"),
            self_cap_env=self_cap_env,
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


def admission_poll_interval(queue_wait_seconds: float, poll_seconds: float) -> float:
    """How often to retry a free token.

    queue_wait_seconds is the caller's patience, not the sleep. Sleeping that
    long (and the same interval on the node) left a freed token idle for minutes.
    Poll at least once a second, sooner when either configured interval is shorter.
    """
    return min(queue_wait_seconds, poll_seconds, 1.0)


class RemoteNode:
    """One transport primitive; all admission operations execute on the node."""

    def __init__(self, box: BoxConfig, admission: AdmissionConfig, executor: Any,
                 *, poll_seconds: float) -> None:
        self.box, self.admission, self.executor = box, admission, executor
        self.poll_seconds = poll_seconds

    def call(self, operation: str, **values: Any) -> Any:
        payload = {
            "operation": operation, "box": self.box.name,
            "managed_root": str(self.box.managed_run_directory),
            "p_cores": self.box.p_cores, "token_pool_size": self.box.token_pool_size,
            "poll_seconds": admission_poll_interval(
                self.admission.queue_wait_seconds, self.poll_seconds
            ),
            **values,
        }
        source = Path(__file__).with_name("goalflight_remote_ci_node.py").read_text()
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        program = base64.b64encode(source.encode()).decode()
        script = "python3 -c " + shlex.quote(
            "import base64;exec(compile(base64.b64decode(" + repr(program) +
            "), '<remote-ci-node>', 'exec'))"
        ) + " " + shlex.quote(encoded)
        argv = expand_command(self.box.remote_exec, {"box": self.box.name, "host": self.box.host},
                              script=[script])
        result = self.executor(argv, {**os.environ, **self.box.env}, 30)
        # The helper prints one JSON document only after the operation finishes,
        # and errors go to stderr. ssh can still exit 255 after that write.
        # Discarding the body admits a holder the caller can never name.
        parsed: Any = None
        if not result.timed_out and result.stdout.strip():
            try:
                parsed = json.loads(result.stdout)
            except (ValueError, TypeError):
                parsed = None
        if isinstance(parsed, (dict, list)):
            return parsed
        if result.returncode != 0 or result.timed_out:
            raise RemoteCIError(f"node {operation} failed: {result.stderr.strip()}")
        raise RemoteCIError(f"node {operation} returned invalid JSON")


def list_remote_leases(config: DaemonConfig, *, executor: Any = None) -> list[dict[str, Any]]:
    runner = RemoteRunner(config, executor=executor)
    return [dict(record, box=name) for name, node in runner.nodes.items()
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
    for token in shlex.split(match.group("fields")):
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
    lease: dict[str, Any] | None = None

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
        if self.error:
            value["error"] = self.error
        return value


Executor = Callable[[Sequence[str], Mapping[str, str], float | None], CommandResult]


class RemoteRunner:
    """Admit, start and observe node-owned runs through one remote primitive."""

    def __init__(self, config: DaemonConfig, *, executor: Executor | None = None,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self.executor = executor or (lambda argv, env, timeout: run_command(argv, env=env, timeout=timeout))
        self.sleeper = sleeper
        self._inflight: set[tuple[str, str]] = set()
        self.nodes = {
            name: RemoteNode(box, config.admission, self.executor, poll_seconds=config.poll_seconds)
            for name, box in config.boxes.items()
        }

    @staticmethod
    def _owner() -> dict[str, Any]:
        return {"owner_identity": _owner_identity(), "owner_pid": os.getpid(),
                "owner_host": socket.gethostname()}

    def _lease_key(self, record: Mapping[str, Any]) -> tuple[str, str]:
        return (str(record["run_directory"]), str(record["lease_token"]))

    def _owns(self, record: Mapping[str, Any]) -> bool:
        return record.get("owner_host") == socket.gethostname() and record.get("owner_pid") == os.getpid()

    def _release_forgotten(self, node: "RemoteNode", record: Mapping[str, Any]) -> str:
        """Release a lease this live process admitted and then dropped.

        Reap treats a live owner as busy. That is right for a run this process
        is inside, and wrong for one whose enqueue or release reply was lost:
        the owner pid is still the daemon, so nobody else will cancel it.
        """
        key = {"run_dir": record["run_directory"], "lease_token": record["lease_token"]}
        fresh = node.call("status", **key)
        if fresh.get("state") == "released":
            return "finished"
        if not self._owns(fresh) or self._lease_key(fresh) in self._inflight:
            return "owned"
        expected = {name: fresh.get(name) for name in ("owner_host", "owner_pid", "owner_identity")}
        if fresh.get("state") == "running" and fresh.get("remote_run"):
            return node.call("cancel", **key, identity=fresh["remote_run"],
                             expected_owner=expected)["status"]
        try:
            return node.call("release", **key, expected_owner=expected)["status"]
        except RemoteCIError:
            if not fresh.get("remote_run"):
                return "unknown"
            return node.call("cancel", **key, identity=fresh["remote_run"],
                             expected_owner=expected)["status"]

    def _recover_other_forgotten(self, node: "RemoteNode") -> None:
        for record in node.call("list"):
            if record.get("state") == "released" or not self._owns(record):
                continue
            if self._lease_key(record) in self._inflight:
                continue
            self._release_forgotten(node, record)

    def run_arm(self, spec: ArmSpec) -> ArmOutcome:
        node = self.nodes[spec.box]
        key: dict[str, str] | None = None
        started = False
        try:
            record = node.call("enqueue", request_id=spec.request_id, arm=spec.arm, owner=self._owner())
            key = {"run_dir": record["run_directory"], "lease_token": record["lease_token"]}
            self._inflight.add((key["run_dir"], key["lease_token"]))
            while True:
                record = node.call("status", **key)
                if record["state"] == "admitted":
                    break
                if record.get("state") == "released" or not record.get("holder_alive"):
                    raise AdmissionError("node admission holder exited before admission")
                self._recover_other_forgotten(node)
                self.sleeper(admission_poll_interval(
                    self.config.admission.queue_wait_seconds, self.config.poll_seconds
                ))
            # Token ownership precedes rendering, checkout, push and test work.
            values = {
                "box": spec.box, "host": node.box.host, "arm": spec.arm,
                "sha": spec.sha, "tip_sha": spec.tip_sha, "candidate_sha": spec.candidate_sha,
                "request_id": spec.request_id, "run_id": record["lease_id"],
                "lease_id": record["lease_id"], "lease_token": record["lease_token"],
                "owner_identity": record["owner_identity"], "run_dir": key["run_dir"],
                "managed_run_directory": str(node.box.managed_run_directory),
                "token_index": str(record["token_index"]), "overlay": "1" if spec.overlay else "0",
            }
            command = expand_command(self.config.runner.command, values,
                                     selection=spec.selection, test_files=spec.test_files,
                                     test_command=self.config.runner.test_command)
            env = {**self.config.runner.env, **node.box.env}
            env.update({"GOALFLIGHT_REMOTE_CI_" + name.upper(): value for name, value in values.items()})
            env.update({
                "GOALFLIGHT_REMOTE_CI_TEST_FILES_JSON": json.dumps(list(spec.test_files)),
                "GOALFLIGHT_REMOTE_CI_SELECTION_JSON": json.dumps(list(spec.selection)),
                "GOALFLIGHT_REMOTE_CI_TEST_COMMAND_JSON": json.dumps(list(self.config.runner.test_command)),
                "GOALFLIGHT_REMOTE_CI_LEASE_RECORD_JSON": json.dumps(record),
                self.config.runner.self_cap_env: str(min(self.config.runner.self_cap,
                                                        record["sample"].get("self_cap", self.config.runner.self_cap))),
            })
            # A lost launch response must never free a successfully started run.
            started = True
            node.call("start", **key, command={"argv": command, "env": env,
                                              "timeout": self.config.runner.timeout_seconds})
            return self._watch(spec, node, key)
        except BaseException:
            if started and key is not None:
                # A lost response can follow a successful launch. Cancel the
                # fenced node identity; never just drop its admission token.
                with contextlib.suppress(RemoteCIError):
                    node.call("cancel", **key, identity=record["remote_run"])
            raise
        finally:
            if key is not None:
                try:
                    if not started:
                        # The holder is already forked. A release that never
                        # reaches the node is recovered by reap / the next arm.
                        with contextlib.suppress(RemoteCIError):
                            node.call("release", **key)
                finally:
                    self._inflight.discard((key["run_dir"], key["lease_token"]))

    def _watch(self, spec: ArmSpec, node: RemoteNode, key: dict[str, str]) -> ArmOutcome:
        deadline = time.monotonic() + self.config.runner.timeout_seconds
        while True:
            record = node.call("status", **key)
            identity = RemoteRunIdentity.from_mapping(record["remote_run"])
            completed = record.get("result")
            if completed is not None:
                if completed.get("timed_out"):
                    return ArmOutcome(spec.arm, "timeout", 124, None, identity,
                                      timed_out=True, cancelled=True, lease=record)
                try:
                    receipt = receipt_from_output(completed.get("stdout", "") + "\n" + completed.get("stderr", ""))
                    if receipt.hostname != record["sample"]["hostname"]:
                        raise ReceiptError("receipt hostname differs from the measured node")
                except ReceiptError as exc:
                    return ArmOutcome(spec.arm, "red", completed["returncode"], None, identity,
                                      error=str(exc), lease=record)
                status = "green" if completed["returncode"] == 0 and _receipt_is_green(receipt) else "red"
                return ArmOutcome(spec.arm, status, completed["returncode"], receipt, identity, lease=record)
            if not record["holder_alive"]:
                raise RemoteCIError("node holder died without a result; remote identity is unknown")
            if time.monotonic() >= deadline:
                result = node.call("cancel", **key, identity=identity.to_dict())
                cancelled = result["status"] in {"cancelled", "finished"}
                return ArmOutcome(spec.arm, "timeout", 124, None, identity,
                                  timed_out=True, cancelled=cancelled, lease=record,
                                  error=None if cancelled else "remote cancellation identity unknown")
            self.sleeper(min(self.config.poll_seconds, 1.0))

    def reattach_launch_log(self, launch_log: Path, spec: ArmSpec) -> ArmOutcome:
        identity = parse_launch_identity(launch_log.read_text(), default_host=self.config.boxes[spec.box].host)
        if identity is None:
            raise RemoteCIError("launch log does not contain a complete remote run identity")
        return self.reattach(spec, identity)

    def reattach(self, spec: ArmSpec, identity: RemoteRunIdentity) -> ArmOutcome:
        node = self.nodes[spec.box]
        key = {"run_dir": identity.run_dir, "lease_token": identity.lease_token}
        node.call("attach", **key, identity=identity.to_dict(), owner=self._owner())
        self._inflight.add((key["run_dir"], key["lease_token"]))
        try:
            return self._watch(spec, node, key)
        finally:
            self._inflight.discard((key["run_dir"], key["lease_token"]))

    def reap(self) -> list[dict[str, Any]]:
        results = []
        for name, node in self.nodes.items():
            for record in node.call("list"):
                if record["state"] == "released":
                    continue
                status = "unknown"
                # A PID from another controller host proves nothing locally.
                alive = (_pid_alive(record.get("owner_pid"))
                         if record.get("owner_host") == socket.gethostname() else None)
                forgotten = (
                    alive is True
                    and self._owns(record)
                    and self._lease_key(record) not in self._inflight
                )
                if forgotten:
                    status = self._release_forgotten(node, record)
                elif alive is True:
                    status = "owned"
                elif alive is False and record.get("remote_run"):
                    identity = RemoteRunIdentity.from_mapping(record["remote_run"])
                    status = node.call("cancel", run_dir=record["run_directory"],
                                       lease_token=record["lease_token"],
                                       identity=identity.to_dict(),
                                       expected_owner={key: record.get(key) for key in
                                                       ("owner_host", "owner_pid", "owner_identity")})["status"]
                results.append({"box": name, "lease_id": record["lease_id"], "status": status})
        return results


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


def health_census(config: DaemonConfig, *, executor: Any = None) -> dict[str, Any]:
    runner = RemoteRunner(config, executor=executor)
    boxes = []
    for name, node in runner.nodes.items():
        try:
            value = node.call("health")
            boxes.append({"box": name, "status": "ok", **value})
        except RemoteCIError as exc:
            boxes.append({"box": name, "status": "unknown", "error": str(exc)})
    return {"schema": "goalflight.remote-ci.health.v1", "measured_at": _utc_now(), "boxes": boxes}


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
            print(json.dumps(RemoteRunner(config).reap(), indent=2, sort_keys=True))
        return 0
    except (RemoteCIError, OSError) as exc:
        print(f"goalflight-remote-ci: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
