"""Transport and bounded-result contracts for the remote CI runner."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import time
from threading import Barrier
import signal
import threading
from types import SimpleNamespace

import pytest
import goalflight_codex_sandbox as codex_sandbox
import goalflight_os_sandbox as os_sandbox
import goalflight_remote_ci as ci
import goalflight_remote_ci_node as ci_node
import goalflight_seat_dedupe as seat_dedupe
from goalflight_remote_ci import CommandResult, RemoteRunner
import support as test_support
from support import (
    launchctl_unavailable_reason,
    node_unavailable_reason,
    sandbox_exec_unavailable_reason,
)
from test_remote_ci import (
    ROOT, ScriptedExecutor, _config, _git, _init_repo, _safe_managed_temp_base, _test_source,
    _load_fake_node_config, _raw_config, enqueue, key, spec, start, wait_state,
)


@contextmanager
def _independent_deadline(seconds=30):
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise TimeoutError(f"test loop exceeded its independent {seconds}s deadline")

    signal.signal(signal.SIGALRM, expired)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.0, previous_timer[0] - (time.monotonic() - started)),
                previous_timer[1],
            )

LEASE_ROUTE = {
    "measured_hostname": "measured-node",
    "destination_address": "192.0.2.10",
}

def _profile_raw_config(tmp_path, *, token_pool_size=2):
    raw = _raw_config(tmp_path, token_pool_size=token_pool_size)
    managed_base = _safe_managed_temp_base(tmp_path)
    if managed_base == Path("/var/tmp"):
        managed_base /= f"goalflight-test-{tmp_path.name}"
    raw["boxes"]["box-a"]["managed_run_directory"] = str(managed_base / "fake-node")
    raw["runner"]["command"] = ["python3", "-m", "pytest"]
    raw["runner"]["test_command"] = ["python3", "-m", "pytest"]
    return raw


def _write_workload_deadline(run, *, timeout=20):
    started_at = time.time()
    (run / "lease.json").write_text(json.dumps({
        "started_at": started_at, "deadline_epoch": started_at + timeout,
    }), encoding="utf-8")

@pytest.mark.parametrize(("caller", "runner", "box", "derived", "expected"), [
    ({"KEY": "caller"}, {}, {}, {}, "caller"),
    ({"KEY": "caller"}, {"KEY": "runner"}, {}, {}, "runner"),
    ({"KEY": "caller"}, {"KEY": "runner"}, {"KEY": "box"}, {}, "box"),
    ({"KEY": "caller"}, {"KEY": "runner"}, {"KEY": "box"},
     {"KEY": "derived"}, "derived"),
])
def test_profile_command_environment_layers_override_in_order(
    caller, runner, box, derived, expected,
):
    result = ci._build_command_environment(
        caller, runner, box, profile_enabled=True, profile_env=derived,
    )
    assert result["KEY"] == expected

@pytest.fixture
def node_env(tmp_path):
    config = _config(tmp_path)
    executor = ScriptedExecutor(tmp_path / "fake-node")
    runner = RemoteRunner(config, executor=executor)
    yield config, executor, runner, runner.nodes["box-a"]
    executor.close(config)

@pytest.mark.parametrize("environment", [
    {"requirements_file": "requirements-test.txt"},
    {"requirements": ["numpy==2.4.6"]},
    {"interpreter": sys.executable,
     "version_probe": [sys.executable, "--version"]},
])
def test_project_environment_recipe_is_preserved(tmp_path, environment):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = environment
    if "requirements_file" in environment:
        (tmp_path / environment["requirements_file"]).write_text(
            "pytest==9.1.1\n", encoding="utf-8",
        )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    config = _load_fake_node_config(path)

    assert config.environment == environment

@pytest.mark.parametrize("requirement", ["numpy", "numpy>=2.4.6", "numpy~=2.4"])
def test_profile_rejects_unpinned_inline_requirements(tmp_path, requirement):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": [requirement]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ci.ConfigError, match="exact == pins"):
        _load_fake_node_config(path)

def test_profile_rejects_unpinned_requirements_file_and_nested_input(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements_file": "requirements-test.txt"}
    root = tmp_path / "repo"
    (root / "nested").mkdir(parents=True)
    (root / "requirements-test.txt").write_text(
        "-r nested/requirements-extra.txt\n", encoding="utf-8",
    )
    (root / "nested" / "requirements-extra.txt").write_text(
        "pytest>=9\n", encoding="utf-8",
    )
    path = root / ".goal-flight" / "remote-ci.json"
    path.parent.mkdir()
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ci.ConfigError, match="exact == pins"):
        _load_fake_node_config(path)

def test_legacy_test_requirements_becomes_a_file_recipe(tmp_path):
    raw = _raw_config(tmp_path)
    raw["test_requirements"] = "requirements-test.txt"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    config = _load_fake_node_config(path)

    assert config.environment == {"requirements_file": "requirements-test.txt"}


def test_box_admission_mode_is_explicit_and_legacy_mapping_stays_blocking(tmp_path):
    raw = _raw_config(tmp_path)
    legacy_path = tmp_path / "legacy-config.json"
    legacy_path.write_text(json.dumps(raw), encoding="utf-8")
    assert _load_fake_node_config(legacy_path).admission_mode == "blocking"

    raw["admission"] = "box"
    box_path = tmp_path / "box-config.json"
    box_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(box_path)
    assert config.admission_mode == "box"
    assert config.admission.max_wait_seconds == 30


def test_unknown_admission_string_is_rejected(tmp_path):
    raw = _raw_config(tmp_path)
    raw["admission"] = "eventually"
    path = tmp_path / "bad-admission.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ci.ConfigError, match='admission string must be "box"'):
        _load_fake_node_config(path)


@pytest.mark.parametrize(("arms", "expected"), [
    ({}, None),
    ({"candidate": {"phase": "preparing"}}, None),
    ({"candidate": {"phase": "submitting"}}, None),
    ({"candidate": {"phase": "failed", "remote_ticket_accepted": None}}, None),
    ({"candidate": {"phase": "failed", "remote_ticket_accepted": False}}, False),
    ({"candidate": {"phase": "accepted"}}, True),
    ({"base": {"phase": "failed", "remote_ticket_accepted": False},
     "candidate": {"phase": "accepted"}}, True),
])
def test_resident_acceptance_only_allows_fallback_after_definitive_submit_rejection(
    tmp_path, arms, expected,
):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"_resident": {"arms": arms}}), encoding="utf-8")

    assert ci._request_resident_acceptance(path) is expected


@pytest.mark.parametrize("resume_state", ["queued", "running", "finished"])
def test_box_mode_re_adopts_ticket_after_daemon_restart(
    tmp_path, monkeypatch, resume_state,
):
    config = replace(_config(tmp_path), admission_mode="box")
    executor = ScriptedExecutor(tmp_path / "fake-node")
    root, sha, _temporary = _test_source()
    request_id = f"resident-restart-{resume_state}"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    spool = config.queue_dir / f"{request_id}.json"
    spool.write_text(json.dumps({"request_id": request_id}), encoding="utf-8")
    bundle = config.queue_dir / f"{request_id}.bundle"
    spec = ci.ArmSpec(
        arm="candidate", sha=sha, tip_sha=sha, candidate_sha=sha,
        test_files=(), selection=(), request_id=request_id, box="box-a",
        overlay=False, command=(sys.executable, "-c", "pass"),
        command_id="resident-restart-test", head_sha=sha,
        snapshot_sha=sha, bundle_path=bundle, snapshot_root=root,
        snapshot_ref="HEAD", spool_path=spool,
    )
    ticket = {
        "run_directory": f"/fake/runs/{request_id}",
        "lease_token": f"token-{request_id}",
        "lease_id": request_id, "ticket": 1,
        "request_id": request_id, "arm": "candidate",
        "resident_submission": True,
        "owner_host": "controller", "owner_pid": 10,
        "owner_start_token": "start", "owner_identity": "owner",
        "sample": {"hostname": "measured-node", "self_cap": 1},
        "remote_run": {
            "pid": "20", "start_token": "remote-start",
            "run_dir": f"/fake/runs/{request_id}",
        },
    }
    calls = []
    status_calls = 0
    receipt = json.dumps({
        "schema": ci.RECEIPT_SCHEMA, "hostname": "measured-node",
        "status": "pass", "returncode": 0, "passed": 1,
    }).encode() + b"\n"

    def install_node_call(node):
        def call(operation, **values):
            nonlocal status_calls
            calls.append(operation)
            if operation == "bundle-basis":
                return {"commit": None}
            if operation == "submit":
                assert Path(values["candidate_bundle_path"]).is_file()
                return dict(ticket, state="queued")
            if operation == "status":
                status_calls += 1
                if resume_state != "finished" and status_calls == 1:
                    return dict(ticket, state=resume_state)
                return dict(ticket, state="released", result={
                    "status": "completed", "returncode": 0,
                    "exit_known": True, "command_started": True,
                })
            if operation == "fetch":
                body = receipt if values["stream"] == "stdout" else b""
                return {
                    "status": "ok", "compression": "gzip",
                    "body_gzip_base64": base64.b64encode(
                        gzip.compress(body, mtime=0),
                    ).decode(),
                    "size_bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "truncated": False, "omitted_bytes": 0,
                }
            raise AssertionError(f"unexpected node operation {operation}")
        monkeypatch.setattr(node, "call", call)

    try:
        first_runner = RemoteRunner(config, executor=executor)
        monkeypatch.setattr(first_runner, "choose_box", lambda **_kwargs: "box-a")
        install_node_call(first_runner.nodes["box-a"])
        submitted = first_runner.run_arm(spec)
        assert submitted.status == "submitted"
        assert submitted.remote_ticket_accepted is True

        restarted_runner = RemoteRunner(config, executor=executor)
        monkeypatch.setattr(restarted_runner, "choose_box", lambda **_kwargs: "box-a")
        install_node_call(restarted_runner.nodes["box-a"])
        if resume_state == "finished":
            outcome = restarted_runner.run_arm(spec)
        else:
            pending = restarted_runner.run_arm(spec)
            assert pending.status == "submitted"
            outcome = restarted_runner.run_arm(spec)
        assert outcome.status == "green"
        assert outcome.remote_ticket_accepted is True
        assert calls.count("submit") == 1
    finally:
        executor.close(config)


def test_box_mode_cancels_withdrawn_resident_ticket(tmp_path, monkeypatch):
    config = replace(_config(tmp_path), admission_mode="box")
    executor = ScriptedExecutor(tmp_path / "fake-node")
    runner = RemoteRunner(config, executor=executor)
    monkeypatch.setattr(runner, "choose_box", lambda **_kwargs: "box-a")
    request_id = "resident-cancelled"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    spool = config.queue_dir / f"{request_id}.json"
    spool.write_text(json.dumps({"request_id": request_id}), encoding="utf-8")
    owner = runner._owner()
    ci._save_resident_arm(spool, "candidate", {
        "phase": "accepted", "box": "box-a", "owner": owner,
        "ticket": {"run_dir": "/fake/run", "lease_token": "lease"},
    })
    ci._mark_request_cancelled(config, request_id, "worker-withdrawn")
    calls = []
    record = {
        "state": "released", "request_id": request_id,
        "resident_submission": True,
        "result": {"status": "cancelled", "returncode": 130},
    }

    def call(operation, **_values):
        calls.append(operation)
        if operation == "cancel":
            return {"status": "cancelled"}
        if operation == "status":
            return record
        raise AssertionError(f"unexpected node operation {operation}")

    monkeypatch.setattr(runner.nodes["box-a"], "call", call)
    spec = ci.ArmSpec(
        arm="candidate", sha="b" * 40, tip_sha="a" * 40,
        candidate_sha="b" * 40, test_files=(), selection=(),
        request_id=request_id, box="box-a", overlay=False,
        spool_path=spool,
    )
    try:
        outcome = runner.run_arm(spec)
        assert outcome.status == "cancelled"
        assert outcome.remote_ticket_accepted is True
        assert calls == ["cancel", "status"]
    finally:
        executor.close(config)


def test_gate_daemon_keeps_submitted_request_until_collected(tmp_path, monkeypatch):
    raw = _raw_config(tmp_path)
    raw["admission"] = "box"
    config_path = tmp_path / "box-daemon-config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    request_id = "resident-daemon-queue"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    config.result_dir.mkdir(parents=True, exist_ok=True)
    request_path = config.queue_dir / f"{request_id}.json"
    request_path.write_text(json.dumps({
        "schema": ci.REQUEST_SCHEMA, "request_id": request_id,
        "project_id": config.repo, "kind": "gate", "command_id": "heavy-gate",
        "command": [sys.executable, "-c", "pass"],
        "repo_root": str(tmp_path), "cwd_relative": "", "caller_env": {},
        "receipt_metadata": {}, "head_sha": "a" * 40,
        "snapshot_sha": "a" * 40, "dirty": False,
    }), encoding="utf-8")
    daemon = ci.GateDaemon(config, runner=SimpleNamespace(reap=lambda: None))
    monkeypatch.setattr(
        daemon, "_snapshot_command_request", lambda request: (request, {}),
    )
    calls = []

    def collect(_request, _runner):
        calls.append(None)
        return ({
            "schema": ci.RESULT_SCHEMA, "request_id": request_id,
            "status": "SUBMITTED",
            "remote_ticket_accepted": True,
        } if len(calls) == 1 else {
            "schema": ci.RESULT_SCHEMA, "request_id": request_id,
            "status": "GREEN", "candidate": {"returncode": 0},
        })

    monkeypatch.setattr(ci, "run_remote_command", collect)
    assert daemon.poll_once()["status"] == "SUBMITTED"
    assert request_path.is_file()
    assert not (config.result_dir / f"{request_id}.json").exists()
    assert daemon.poll_once()["status"] == "GREEN"
    assert not request_path.exists()
    assert json.loads((config.result_dir / f"{request_id}.json").read_text())[
        "status"
    ] == "GREEN"


def test_box_daemon_processes_cancellation_before_wrapper_result(tmp_path, monkeypatch):
    raw = _raw_config(tmp_path)
    raw["admission"] = "box"
    config_path = tmp_path / "box-cancel-config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    request_id = "resident-wrapper-cancel"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    config.result_dir.mkdir(parents=True, exist_ok=True)
    request_path = config.queue_dir / f"{request_id}.json"
    request_path.write_text(json.dumps({
        "schema": ci.REQUEST_SCHEMA, "request_id": request_id,
        "project_id": config.repo, "kind": "gate", "command_id": "heavy-gate",
        "command": [sys.executable, "-c", "pass"],
        "repo_root": str(tmp_path), "cwd_relative": "", "caller_env": {},
        "receipt_metadata": {}, "head_sha": "a" * 40,
        "snapshot_sha": "a" * 40, "dirty": False,
    }), encoding="utf-8")
    ci._atomic_write_json(config.result_dir / f"{request_id}.json", {
        "schema": ci.RESULT_SCHEMA, "request_id": request_id,
        "status": "REMOTE_UNAVAILABLE",
    })
    ci._mark_request_cancelled(config, request_id, "wrapper-give-up")
    daemon = ci.GateDaemon(config, runner=SimpleNamespace(reap=lambda: None))
    monkeypatch.setattr(
        daemon, "_snapshot_command_request", lambda request: (request, {}),
    )
    calls = []

    def collect(_request, _runner):
        calls.append(None)
        return {
            "schema": ci.RESULT_SCHEMA, "request_id": request_id,
            "status": "GREEN", "candidate": {"returncode": 0},
        }

    monkeypatch.setattr(ci, "run_remote_command", collect)

    assert daemon.poll_once()["status"] == "GREEN"
    assert calls == [None]
    assert not request_path.exists()
    assert not ci._request_cancel_path(config, request_id).exists()
    assert json.loads((config.result_dir / f"{request_id}.json").read_text())[
        "status"
    ] == "GREEN"

def test_checkout_basename_is_a_single_directory_name(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["checkout_basename"] = "pm2"
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    assert config.checkout_basename == "pm2"

    raw["checkout_basename"] = "../pm2"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ci.ConfigError, match="checkout_basename"):
        _load_fake_node_config(path)

    raw["checkout_basename"] = "retained"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ci.ConfigError, match="preserved legacy seat data"):
        _load_fake_node_config(path)


def test_box_required_paths_gate_remote_health_with_a_reason(tmp_path):
    required = tmp_path / "external-input"
    required.mkdir()
    raw = _profile_raw_config(tmp_path)
    raw["boxes"]["box-a"]["env"]["EXTERNAL_INPUT"] = str(required)
    raw["local_env"] = {"EXTERNAL_INPUT": str(required)}
    raw["boxes"]["box-a"]["required_paths"] = ["${EXTERNAL_INPUT}"]
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    raw["receipt_probes"] = {
        "external_input": {
            "command": [sys.executable, "-c", "import sys; print(sys.argv[1])", "${EXTERNAL_INPUT}"],
            "expected": str(required),
        },
    }
    path = tmp_path / "remote-ci.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    executor = ScriptedExecutor(tmp_path / "fake-node")
    try:
        runner = RemoteRunner(config, executor=executor)
        assert runner.choose_box() == "box-a"
        required.rmdir()
        with pytest.raises(ci.NoHealthyBox, match="missing-paths"):
            runner.choose_box()
    finally:
        executor.close(config)

def test_profile_receipt_probes_require_named_expected_outputs(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    raw["boxes"]["box-a"]["env"]["KILN"] = str(tmp_path / "kiln")
    raw["local_env"] = {"KILN": str(tmp_path / "kiln")}
    raw["receipt_probes"] = {
        "kiln_commit": {
            "command": ["git", "-C", "${KILN}", "rev-parse", "--short=8", "HEAD"],
            "expected": "b1c53115",
        },
        "engine_version": {"command": [sys.executable, "--version"]},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ci.ConfigError, match="declare expected output"):
        _load_fake_node_config(config_path)

    raw["receipt_probes"]["engine_version"]["expected"] = subprocess.run(
        [sys.executable, "--version"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)

    assert config.receipt_probes["kiln_commit"] == raw["receipt_probes"]["kiln_commit"]
    assert config.receipt_probes["engine_version"] == raw["receipt_probes"]["engine_version"]

@pytest.mark.parametrize("entry", [
    "tests/rf/test_gate*.py", "tests/rf/test_gate?.py", "tests/rf/test_gate[1].py",
])
def test_profile_rejects_glob_never_route_entries(tmp_path, entry):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    raw["never_route"] = [entry]
    root = tmp_path / "repo"
    (root / ".goal-flight").mkdir(parents=True)
    config_path = root / ".goal-flight" / "remote-ci.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ci.ConfigError, match="explicit repository-relative"):
        _load_fake_node_config(config_path)

def test_profile_rejects_nonfile_never_route_entries(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    raw["never_route"] = ["tests/not-a-file"]
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    os.mkfifo(root / "tests" / "not-a-file")
    config_path = root / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ci.ConfigError, match="files or directories"):
        _load_fake_node_config(config_path)

def test_profile_never_route_uses_file_identity_and_directory_containment(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    raw["never_route"] = ["tests/rf/test_gate.py", "tests/private"]
    root = tmp_path / "repo"
    nested = root / "tests" / "private" / "nested"
    nested.mkdir(parents=True)
    blocked = root / "tests" / "rf" / "test_gate.py"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("", encoding="utf-8")
    (blocked.parent / "test_gate_alias.py").hardlink_to(blocked)
    (nested / "test_secret.py").write_text("", encoding="utf-8")
    safe = root / "tests" / "safe.py"
    safe.write_text("", encoding="utf-8")
    config_path = root / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)

    def request(path):
        return {
            "schema": ci.REQUEST_SCHEMA,
            "request_id": "profile-command",
            "project_id": config.repo,
            "kind": "gate",
            "repo_root": str(root),
            "command_id": "profile-command",
            "command": ["python3", "-m", "pytest", path],
        }

    for path in ("tests/rf/test_gate_alias.py", "tests/private/nested/test_secret.py"):
        with pytest.raises(ci.ProfileIneligible, match="never_route"):
            ci.validate_request(request(path), config)
    assert ci.validate_request(request("tests/safe.py"), config)["command"][-1] == "tests/safe.py"

@pytest.mark.parametrize(("entry", "selected"), [
    ("tests/private/test_gate.py", "tests/private/test_gate.py"),
    ("tests/private", "tests/private/nested/test_gate.py"),
])
def test_profile_never_route_fails_closed_when_identity_check_errors(
    tmp_path, monkeypatch, entry, selected,
):
    root = tmp_path / "repo"
    blocked = root / entry
    selected_path = root / selected
    selected_path.parent.mkdir(parents=True, exist_ok=True)
    selected_path.write_text("", encoding="utf-8")
    if blocked != selected_path:
        blocked.mkdir(parents=True, exist_ok=True)
    original_samefile = ci.os.path.samefile

    def unavailable(left, right):
        if Path(left) == blocked.resolve() or Path(right) == blocked.resolve():
            raise OSError("identity metadata unavailable")
        return original_samefile(left, right)

    monkeypatch.setattr(ci.os.path, "samefile", unavailable)

    with pytest.raises(ci.ProfileIneligible) as exc_info:
        ci._never_route_entry(selected, (entry,), repo_root=str(root))

    assert exc_info.value.fallback_reason == "never-route-check-failed"

@pytest.mark.parametrize("command", [
    ["python3", "-m", "pytest", "tests/safe.py", "."],
    ["python3", "-m", "pytest"],
    ["python3", "-m", "pytest", "--collect-only", "tests/safe.py"],
    ["python3", "-m", "pytest", "-k", "tests/private.py", "tests/safe.py"],
    ["python3", "-m", "pytest", "-k=tests/private.py", "tests/safe.py"],
    ["python3", "-m", "pytest", "@args"],
    ["python3", "-m", "pytest", "--opt=value", "tests/safe.py"],
    ["python3", "-m", "pytest", "-n", "tests/safe.py"],
    ["python3", "-m", "pytest", "-n4", "tests/safe.py"],
    ["python3", "-m", "pytest", "-n", "4", "-p=no:cacheprovider", "tests/safe.py"],
    ["python3", "-m", "pytest", "tests/safe.py", "-q"],
    ["python3", "-m", "pytest", "tests/safe.py::test_one"],
    ["python3", "-m", "pytest", "tests/*.py"],
    ["python3", "-m", "pytest", "tests"],
    ["python3", "-m", "pytest", "../tests/safe.py"],
    ["python3", "-m", "pytest", "{absolute_laptop_path}"],
    ["pytest", "tests/safe.py"],
])
def test_profile_custom_command_defaults_to_local_outside_strict_grammar(
    tmp_path, command,
):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "safe.py").write_text("", encoding="utf-8")
    if "@args" in command:
        (root / "@args").write_text("", encoding="utf-8")
        (root / "args").write_text("tests/private.py\n", encoding="utf-8")
    config_path = root / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    if command[-1] == "{absolute_laptop_path}":
        command[-1] = str((root / "tests" / "safe.py").resolve())
    request = {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "unsafe-profile-command",
        "project_id": config.repo,
        "kind": "gate",
        "repo_root": str(root),
        "command_id": "profile-command",
        "command": command,
    }

    with pytest.raises(ci.ProfileIneligible) as caught:
        ci.validate_request(request, config)
    assert caught.value.fallback_reason == "command-outside-profile-grammar"

def test_profile_command_accepts_only_allowlisted_flags_before_files(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "safe.py").write_text("", encoding="utf-8")
    config_path = root / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    request = {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "allowed-profile-command",
        "project_id": config.repo,
        "kind": "gate",
        "repo_root": str(root),
        "command_id": "profile-command",
        "command": [
            "python3", "-m", "pytest", "-q", "-v", "-x", "-rs", "-rfE",
            "-p", "no:cacheprovider", "-n", "4", "--continue-on-collection-errors",
            "tests/safe.py",
        ],
    }

    assert ci.validate_request(request, config)["command"][-1] == "tests/safe.py"

def test_profile_targeted_selection_uses_the_strict_flag_allowlist(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "safe.py").write_text("", encoding="utf-8")
    config_path = root / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    selection = [
        "-q", "-v", "-x", "-rs", "-rfE", "-p", "no:cacheprovider",
        "-n", "4", "--continue-on-collection-errors", "tests/safe.py",
    ]
    request = {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "allowed-profile-selection",
        "project_id": config.repo,
        "kind": "targeted",
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tests/safe.py"],
        "selection": selection,
    }

    assert ci.validate_request(request, config)["selection"] == selection

def test_profile_targeted_selection_uses_repo_containment_not_legacy_roots(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "smoke.py").write_text("", encoding="utf-8")
    config_path = root / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    request = {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "in-repo-profile-selection",
        "project_id": config.repo,
        "kind": "targeted",
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tools/smoke.py"],
        "selection": ["tools/smoke.py"],
    }

    assert ci.validate_request(request, config)["test_files"] == ["tools/smoke.py"]

def test_profile_targeted_selection_rejects_attached_option_values(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "safe.py").write_text("", encoding="utf-8")
    config_path = root / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    request = {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "invalid-profile-selection",
        "project_id": config.repo,
        "kind": "targeted",
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tests/safe.py"],
        "selection": ["-k=tests/private.py", "tests/safe.py"],
    }

    with pytest.raises(ci.ProfileIneligible) as caught:
        ci.validate_request(request, config)
    assert caught.value.fallback_reason == "command-outside-profile-grammar"

@pytest.mark.parametrize("field", ["command", "test_command"])
@pytest.mark.parametrize("tail", [
    ["tests/safe.py"], ["{selection}"], ["--opt=value"], ["-k", "secret"],
    ["@args"],
])
def test_profile_runner_templates_cannot_carry_paths_or_unlisted_flags(
    tmp_path, field, tail,
):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    raw["runner"][field] = ["python3", "-m", "pytest", *tail]
    path = tmp_path / "remote-ci.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ci.ConfigError):
        _load_fake_node_config(path)

def test_profile_runner_templates_accept_allowlisted_flags(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    for field in ("command", "test_command"):
        raw["runner"][field] = [
            "python3", "-m", "pytest", "-p", "no:cacheprovider", "-n", "4", "-q",
        ]
    path = tmp_path / "remote-ci.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    _load_fake_node_config(path)

def test_profile_custom_command_rejects_symlink_escaping_repo(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["numpy==1.26.4"]}
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    (repo / "tests" / "escape.py").symlink_to(outside)
    config_path = repo / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    request = {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "escaping-profile-command",
        "project_id": config.repo,
        "kind": "gate",
        "repo_root": str(repo),
        "command_id": "profile-command",
        "command": ["python3", "-m", "pytest", "tests/escape.py"],
    }

    with pytest.raises(ci.ProfileIneligible, match="inside the repository") as caught:
        ci.validate_request(request, config)
    assert caught.value.fallback_reason == "command-outside-profile-grammar"

def test_profile_selection_rejection_returns_local_fallback_reason(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["numpy==1.26.4"]}
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = repo / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    request_path = config.queue_dir / "unsafe.json"
    request_path.parent.mkdir(parents=True)
    request_path.write_text(json.dumps({
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "unsafe-profile",
        "project_id": config.repo,
        "kind": "gate",
        "repo_root": str(repo),
        "command_id": "profile-command",
        "command": ["python3", "-m", "pytest", "-k=tests/private.py", "tests/private.py"],
    }), encoding="utf-8")

    result = ci.GateDaemon(config)._process_request(request_path)

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert result["fallback_reason"] == "command-outside-profile-grammar"


def test_profile_selector_uses_request_snapshot_and_missing_path_has_reason(
    tmp_path, monkeypatch,
):
    daemon_root = tmp_path / "daemon-repo"
    daemon_root.mkdir()
    raw = _profile_raw_config(daemon_root)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    config_path = daemon_root / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    config.queue_dir.mkdir(parents=True)

    worker_root = tmp_path / "worker-repo"
    _init_repo(worker_root)
    _git(worker_root, "config", "user.name", "Remote CI test")
    _git(worker_root, "config", "user.email", "remote-ci-test@example.invalid")
    (worker_root / "README").write_text("base\n", encoding="utf-8")
    _git(worker_root, "add", "README")
    _git(worker_root, "commit", "-m", "base")
    new_test = worker_root / "tests" / "test_new.py"
    new_test.parent.mkdir()
    new_test.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = ci._snapshot_worktree(
        worker_root, "selector-snapshot", config.queue_dir,
        cwd_relative="", caller_env={},
    )
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    command = (sys.executable, "-m", "pytest", "tests/test_new.py")
    accepted_spec = replace(
        spec("selector-snapshot-accepted"), command=command,
        snapshot_root=worker_root, snapshot_sha=snapshot["snapshot_sha"],
    )
    monkeypatch.setattr(
        runner, "choose_box",
        lambda **_kwargs: (_ for _ in ()).throw(ci.NoHealthyBox("profile check passed")),
    )
    try:
        with pytest.raises(ci.NoHealthyBox, match="profile check passed"):
            runner.run_arm(accepted_spec)

        missing = "profile test selector is missing or cannot be resolved: 'tests/test_missing.py'"
        result = ci.run_remote_command({
            "request_id": "selector-snapshot-missing",
            "tip_sha": snapshot["snapshot_sha"],
            "candidate_sha": snapshot["snapshot_sha"],
            "snapshot_sha": snapshot["snapshot_sha"],
            "snapshot_root": str(worker_root),
            "snapshot_ref": snapshot["ref"],
            "dirty": snapshot["dirty"],
            "command": [sys.executable, "-m", "pytest", "tests/test_missing.py"],
            "command_id": "profile-command",
            "box": "box-a",
        }, runner)
        assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
        assert result["fallback_reason"] == "candidate-setup-failed"
        assert result["candidate"]["status"] == "setup-failed"
        assert missing in result["candidate"]["error"]
        assert result["error"] == result["candidate"]["error"]
    finally:
        ci._release_snapshot(
            Path(snapshot["root"]), snapshot["ref"],
            snapshot["snapshot_sha"], snapshot["bundle_path"],
        )

def test_profiled_pair_rechecks_generated_node_but_rejects_user_node_selector(
    tmp_path,
):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "safe.py").write_text("", encoding="utf-8")
    config_path = root / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    request = {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "profiled-pair-recheck",
        "project_id": config.repo,
        "kind": "gate",
        "repo_root": str(root),
        "command_id": "profile-command",
        "command": [sys.executable, "-m", "pytest", "tests/safe.py"],
        "box": "box-a",
        "base_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "head_sha": "b" * 40,
        "snapshot_sha": "b" * 40,
        "dirty": False,
        "recheck": 1,
    }
    unsafe_request = {
        **request,
        "request_id": "profiled-user-node-selector",
        "command": [sys.executable, "-m", "pytest", "tests/safe.py::test_one"],
    }
    with pytest.raises(ci.ProfileIneligible, match="::test_one"):
        ci.validate_request(unsafe_request, config)
    normalized = ci.validate_request(request, config)
    normalized["candidate_sha"] = request["candidate_sha"]

    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    specs = []

    def no_placement(*_args, **_kwargs):
        raise ci.NoHealthyBox("profile validation reached placement")

    runner.choose_box = no_placement

    def run_profiled_arm(arm_spec, *, admission_deadline=None):
        specs.append(arm_spec)
        with pytest.raises(ci.NoHealthyBox, match="profile validation reached placement"):
            RemoteRunner.run_arm(
                runner, arm_spec, admission_deadline=admission_deadline,
            )
        failed = arm_spec.arm == "candidate"
        node_ids = ("tests/safe.py::test_one",) if failed else ()
        receipt = ci.RemoteReceipt(
            hostname="profiled-studio", status="fail" if failed else "pass",
            returncode=1 if failed else 0, passed=0 if failed else 1,
            failed=1 if failed else 0, errors=0,
            failing_node_ids=node_ids, collection_import_errors=(),
            first_error_lines=(), metadata={"box_id": "box-a"},
        )
        return ci.ArmOutcome(
            arm_spec.arm, "red" if failed else "green",
            1 if failed else 0, receipt,
        )

    runner.run_arm = run_profiled_arm
    result = ci.run_matched_pair(normalized, runner)

    assert result["rechecks"]["tests/safe.py::test_one"]["classification"] == "regression"
    assert specs[-1].generated_node_id == "tests/safe.py::test_one"
    assert specs[-1].command == (
        sys.executable, "-m", "pytest", "tests/safe.py::test_one",
    )


def test_profile_never_route_resolves_case_variants_on_case_insensitive_volumes(tmp_path):
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    raw["never_route"] = ["tests/rf/test_gate.py"]
    repo = tmp_path / "repo"
    blocked = repo / "tests" / "rf" / "test_gate.py"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("", encoding="utf-8")
    try:
        case_insensitive = os.path.samefile(repo / "tests" / "rf", repo / "tests" / "RF")
    except OSError:
        case_insensitive = False
    if not case_insensitive:
        pytest.skip("requires a case-insensitive filesystem")
    config_path = repo / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    request = {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "case-profile-file",
        "project_id": config.repo,
        "kind": "gate",
        "repo_root": str(repo),
        "command_id": "profile-command",
        "command": ["python3", "-m", "pytest", "tests/RF/TEST_GATE.py"],
    }

    with pytest.raises(ci.ProfileIneligible, match="never_route"):
        ci.validate_request(request, config)

def _completed_run(tmp_path: Path, monkeypatch, *, stdout: bytes, stderr: bytes):
    import builtins
    import goalflight_remote_ci_node as node

    managed = tmp_path / "managed-node"
    run = managed / "runs" / "completed"
    run.mkdir(parents=True)
    (run / "lease.json").write_text(json.dumps({
        "lease_token": "lease-token", "state": "released",
    }), encoding="utf-8")
    (run / "owner.json").write_text("{}", encoding="utf-8")
    (run / "result.json").write_text(json.dumps({
        "status": "completed", "returncode": 0, "timed_out": False,
    }), encoding="utf-8")
    (run / "stdout").write_bytes(stdout)
    (run / "stderr").write_bytes(stderr)
    monkeypatch.setattr(
        builtins, "_GOALFLIGHT_REMOTE_CI_AUTHORITY",
        str(tmp_path / "node-authority.json"), raising=False)
    return node, {
        "managed_root": str(managed), "p_cores": 4, "token_pool_size": 1,
        "run_dir": str(run), "lease_token": "lease-token",
    }

@pytest.mark.parametrize("text", [
    'POINTER = "version https://git-lfs.github.com/spec/v1"\n',
    "version https://git-lfs.github.com/spec/v1\n"
    "This documentation line has no oid or size fields.\n",
])
def test_lfs_marker_in_regular_text_does_not_mark_repo_unsupported(tmp_path: Path, text: str):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README").write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "source")

    assert ci._unsupported_snapshot_reason(repo) is None

@pytest.mark.parametrize("escape_is_tracked", [False, True])
def test_symlink_chain_escaping_snapshot_routes_local(tmp_path: Path, escape_is_tracked: bool):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a").symlink_to(".")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "in-tree-link")
    if escape_is_tracked:
        (repo / "escape").symlink_to("a/../outside")
        _git(repo, "add", "escape")
        _git(repo, "commit", "-m", "escaping-link")
    else:
        (repo / "escape").symlink_to("a/../outside")

    assert "relative symlink targets outside" in ci._unsupported_snapshot_reason(repo)

def test_case_variant_symlink_chain_uses_filesystem_resolution(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    deep = repo / "deep"
    deep.mkdir()
    if not os.path.samefile(deep, repo / "DEEP"):
        pytest.skip("requires a case-insensitive filesystem")
    (deep / "alias").symlink_to("..")
    (repo / "link").symlink_to("DEEP/alias/../../outside")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "case-variant-link-chain")

    assert "relative symlink targets outside" in ci._unsupported_snapshot_reason(repo)

def test_snapshot_inspection_handles_directory_replaced_by_file(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    nested = repo / "nested"
    nested.mkdir()
    target = nested / "file"
    target.write_text("value\n", encoding="utf-8")
    original_lstat = Path.lstat

    def replaced_lstat(path):
        if path == target:
            raise NotADirectoryError("directory changed during inspection")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", replaced_lstat)

    result = ci._inspect_worktree_paths(repo, ["nested/file"], [])

    assert result["reason"] == "repository changed during snapshot inspection"

def test_snapshot_inspection_rejects_an_expired_setup_deadline(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "commit", "--allow-empty", "-m", "empty")

    with pytest.raises(ci.RemoteCIError, match="setup deadline"):
        ci._unsupported_snapshot_reason(repo, deadline=time.monotonic() - 1)

def test_snapshot_inspection_subprocess_observes_setup_deadline(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "note.txt").write_text("content\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "source")
    original_run = subprocess.run

    def timeout_inspector(argv, *args, **kwargs):
        if len(argv) > 1 and argv[1] == "-c" and "_inspect_worktree_paths" in argv[2]:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(ci.subprocess, "run", timeout_inspector)
    with pytest.raises(ci.RemoteCIError, match="setup deadline"):
        ci._unsupported_snapshot_reason(repo, deadline=time.monotonic() + 10)

def test_snapshot_setup_ignores_expired_admission_and_short_workload_deadlines(tmp_path):
    config = _config(tmp_path)
    config = replace(config, runner=replace(config.runner, timeout_seconds=0.001))
    daemon = ci.GateDaemon(config)
    repo = tmp_path / "snapshot-deadline-source"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")
    (repo / "tracked.txt").write_text("dirty snapshot\n", encoding="utf-8")
    request = {
        "request_id": "snapshot-independent-budget", "command": ["pytest"],
        "repo_root": str(repo), "cwd_relative": "", "caller_env": {},
        "_admission_deadline": time.monotonic() - 1,
    }

    updated, snapshot = daemon._snapshot_command_request(request)
    try:
        assert updated["_admission_deadline"] < time.monotonic()
        assert snapshot["dirty"] is True
        assert snapshot["snapshot_sha"] != snapshot["head_sha"]
    finally:
        ci._release_snapshot(
            Path(snapshot["root"]), snapshot["ref"], snapshot["snapshot_sha"],
            Path(snapshot["bundle_path"]),
        )

def test_snapshot_inspection_routes_special_files_local(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "commit", "--allow-empty", "-m", "source")
    os.mkfifo(repo / "pipe")

    assert "unsupported special files" in ci._unsupported_snapshot_reason(repo)

def test_untracked_nested_repository_is_not_treated_as_submodule(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README").write_text("repo\n", encoding="utf-8")
    nested = repo / "deps" / "nested"
    _init_repo(nested)
    (nested / "source.txt").write_text("nested\n", encoding="utf-8")
    _git(nested, "add", "-A")
    _git(nested, "commit", "-m", "nested")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "parent")

    config_root = tmp_path / "config"
    config_root.mkdir()
    config = _config(config_root)
    config.queue_dir.mkdir(parents=True)
    request = _command_request("nested-repo", repo_root=repo)
    request_path = config.queue_dir / "nested-repo.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    result = ci.GateDaemon(config)._process_request(request_path)

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert "box-a:" in result["error"]
    assert any(cause in result["error"] for cause in ("timeout", "probe-error"))

def test_lfs_marker_symlink_to_fifo_is_not_opened(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    fifo = repo / "fifo"
    os.mkfifo(fifo)
    (repo / "link").symlink_to("fifo")
    _git(repo, "add", "link")
    _git(repo, "commit", "-m", "fifo-link")

    script = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(Path(ci.__file__).parent)!r}); "
        "import goalflight_remote_ci as m; "
        f"print(m._unsupported_snapshot_reason(Path({str(repo)!r})))"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=30, check=True)
    assert result.stdout.strip() == "repository contains unsupported special files"

def test_node_overlay_unlinks_deleted_test_symlink_without_deleting_target(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src/value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests/test_link.py").symlink_to("../src/value.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "tests/test_link.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "remove-link")
    candidate = _git(repo, "rev-parse", "HEAD").stdout.strip()
    slot = tmp_path / "slot"
    _git(repo, "worktree", "add", "--detach", str(slot), base)

    try:
        assert ci_node.overlay_candidate_tests(slot, candidate, ["tests/test_link.py"])
        assert (slot / "src/value.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert not (slot / "tests/test_link.py").exists()
    finally:
        _git(repo, "worktree", "remove", "--force", str(slot))

def test_legacy_receipt_runner_keeps_known_nonzero_exit(tmp_path: Path):
    config = _config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *args, **kwargs: None)
    spec_value = spec("legacy-runner")
    receipt = json.loads((Path(ci.__file__).parent.parent / "tests/fixtures/remote_ci/receipt-pass.json")
                         .read_text(encoding="utf-8"))
    receipt.update(status="fail", returncode=7, passed=0, failed=1, hostname="measured-node")
    text = json.dumps(receipt)
    stream = {"tail": {"text": text, "truncated": False, "omitted_bytes": 0}}
    identity = {
        "host": "measured-node", "pid": "4", "start_token": "tok",
        "run_dir": "/runs/legacy", "lease_id": "legacy", "lease_token": "lease",
    }

    class Node:
        box = config.boxes["box-a"]

        def call(self, operation, **kwargs):
            assert operation == "status"
            return {
                "remote_run": identity, "holder_alive": False,
                "sample": {"hostname": "measured-node"},
                "result": {
                    "status": "died", "returncode": 7, "exit_known": True,
                    "command_started": False, "streams": {
                        "stdout": stream, "stderr": stream,
                    },
                },
            }

    outcome = runner._watch(
        spec_value, Node(), {"run_dir": "/runs/legacy", "lease_token": "lease"}, {},
    )

    assert outcome.status == "red"
    assert outcome.returncode == 7

def test_watch_cancels_when_wrapper_marks_request_cancelled(tmp_path: Path):
    config = _config(tmp_path)
    config.queue_dir.mkdir(parents=True)
    runner = RemoteRunner(config, executor=lambda *args, **kwargs: None)
    spec_value = spec("cancel-watch")
    ci._mark_request_cancelled(config, spec_value.request_id, "deadline")
    identity = {
        "host": "measured-node", "pid": "4", "start_token": "tok",
        "run_dir": "/runs/cancel", "lease_id": "cancel", "lease_token": "lease",
    }
    calls = []
    polls = 0

    class Node:
        box = config.boxes["box-a"]

        def call(self, operation, **kwargs):
            nonlocal polls
            calls.append(operation)
            if operation == "status":
                polls += 1
                if polls == 1:
                    return {"remote_run": identity, "holder_alive": True, "result": None}
                if polls == 2:
                    return {
                        "remote_run": identity, "holder_alive": True,
                        "phase": "finalization",
                        "phase_deadline_epoch": time.time() + 300,
                        "result": None,
                    }
                return {
                    "remote_run": identity, "holder_alive": False,
                    "phase": "finalization",
                    "result": {"status": "cancelled", "returncode": 130},
                }
            assert operation == "cancel"
            return {"status": "cancelled"}

    outcome = runner._watch(
        spec_value, Node(), {"run_dir": "/runs/cancel", "lease_token": "lease"}, {},
    )

    assert calls == ["status", "cancel", "status", "status"]
    assert outcome.status == "cancelled"


def test_caller_death_before_start_releases_admitted_run(tmp_path, monkeypatch):
    config = _config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    alive = {"value": True}
    monkeypatch.setattr(
        ci.goalflight_compat, "process_identity_matches",
        lambda _pid, _token: alive["value"],
    )
    queued = {
        "run_directory": str(tmp_path / "run"),
        "lease_token": "lease-token",
        "lease_id": "lease-id",
        **LEASE_ROUTE,
        "owner_identity": "owner",
        "token_index": 0,
        "sample": {"self_cap": 4},
        "slot": str(tmp_path / "slot"),
        "state": "admitted",
        "holder_alive": True,
    }
    calls = []

    class Node:
        box = runner.nodes["box-a"].box
        pinned_host = None

        def with_pinned_host(self, host):
            self.pinned_host = host
            return self

        def call(self, operation, **_kwargs):
            calls.append(operation)
            if operation == "enqueue":
                alive["value"] = False
                return queued
            if operation == "release":
                return {"status": "releasing"}
            if operation == "start":
                pytest.fail("dead caller's admitted run was started")
            raise AssertionError(f"unexpected operation {operation}")

    runner.nodes["box-a"] = Node()
    runner.choose_box = lambda requested=None, **_kwargs: ci.BoxPlacement(
        requested or "box-a", "measured-node", "192.0.2.10",
    )

    outcome = runner.run_arm(replace(
        spec("caller-died-before-start"),
        caller_identity={"pid": 123, "start_token": "caller-token"},
    ))

    assert outcome.status == "cancelled"
    assert calls == ["enqueue", "release"]


def test_gate_daemon_reaps_orphaned_runs_on_each_poll(tmp_path: Path):
    config = _config(tmp_path)

    class Runner:
        def __init__(self):
            self.reaps = 0

        def reap(self):
            self.reaps += 1

    runner = Runner()
    daemon = ci.GateDaemon(config, runner=runner)
    assert daemon.poll_once() is None
    assert daemon.poll_once() is None
    assert runner.reaps == 2

def test_cancelled_spool_request_is_not_started(tmp_path: Path):
    config = _config(tmp_path)
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README").write_text("tree\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    config.queue_dir.mkdir(parents=True)
    request = _command_request(repo_root=repo)
    request_path = config.queue_dir / f"{request['request_id']}.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    ci._mark_request_cancelled(config, request["request_id"], "wrapper-deadline")

    class Runner:
        def run_arm(self, *_args):
            pytest.fail("cancelled request reached remote execution")

    result = ci.GateDaemon(config, runner=Runner())._process_request(request_path)

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert result["fallback_reason"] == "wrapper-cancelled"

def test_remote_node_installs_helper_once_and_then_sends_only_operation_payload(node_env):
    _, executor, _, node = node_env
    helper = Path(ci.__file__).with_name("goalflight_remote_ci_node.py").read_bytes()
    encoded_program = base64.b64encode(helper).decode("ascii")

    node.call("health")
    node.call("health")

    assert encoded_program not in executor.remote_scripts[-1], (
        "second call still uploads the complete node helper")


def test_remote_runner_keeps_the_helper_source_loaded_at_startup(tmp_path, monkeypatch):
    original_source = Path(ci.__file__).with_name("goalflight_remote_ci_node.py").read_bytes()
    original_digest = hashlib.sha256(original_source).hexdigest()
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    controller_source = daemon_dir / "goalflight_remote_ci.py"
    controller_source.write_text("# test daemon path\n", encoding="utf-8")
    helper_source = daemon_dir / "goalflight_remote_ci_node.py"
    helper_source.write_bytes(original_source)
    monkeypatch.setattr(ci, "__file__", str(controller_source))

    config = _config(tmp_path)
    executor = ScriptedExecutor(tmp_path / "fake-node")
    try:
        runner = RemoteRunner(config, executor=executor)
        node = runner.nodes["box-a"]
        node.call("health")

        installed_helper = (
            config.boxes["box-a"].managed_run_directory / "helpers" / "remote-ci" /
            f"goalflight_remote_ci_node-{original_digest}.py"
        )
        assert installed_helper.read_bytes() == original_source

        helper_source.write_bytes(b"changed helper source on disk")
        installed_helper.unlink()
        node.call("health")

        script = executor.remote_scripts[-1]
        shell = shlex.split(script)
        payload = json.loads(base64.b64decode(shell[3]))
        assert payload["helper_sha256"] == original_digest
        assert payload["helper_size_bytes"] == len(original_source)
        assert shell[-1] == "--install-helper-from-stdin"
        assert base64.b64encode(original_source).decode("ascii") not in script
        assert installed_helper.read_bytes() == original_source
    finally:
        executor.close(config)


def test_remote_node_forwards_box_path_in_operation_payload(node_env):
    config, executor, _, node = node_env
    configured_path = "/opt/homebrew/bin:/usr/bin:/bin"
    config.boxes["box-a"].env["PATH"] = configured_path

    node.call("health")

    payload = json.loads(base64.b64decode(shlex.split(executor.remote_scripts[-1])[3]))
    assert payload["node_path"] == configured_path

def test_remote_node_reinstalls_helper_when_content_hash_differs(node_env):
    config, _, _, node = node_env
    source = Path(ci.__file__).with_name("goalflight_remote_ci_node.py").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    helper = (config.boxes["box-a"].managed_run_directory / "helpers" / "remote-ci" /
              f"goalflight_remote_ci_node-{digest}.py")
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"stale helper")

    node.call("health")

    assert helper.read_bytes() == source, "hash mismatch did not force atomic reinstall"

def test_remote_node_reinstalls_helper_when_hash_is_unreadable(node_env):
    config, _, _, node = node_env
    source = Path(ci.__file__).with_name("goalflight_remote_ci_node.py").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    helper = (config.boxes["box-a"].managed_run_directory / "helpers" / "remote-ci" /
              f"goalflight_remote_ci_node-{digest}.py")
    helper.parent.mkdir(parents=True)
    helper.symlink_to(helper)

    node.call("health")

    assert helper.is_file() and helper.read_bytes() == source, (
        "unverifiable helper was assumed current instead of reinstalled")

def test_concurrent_helper_installs_are_atomic(node_env):
    config, _, _, node = node_env
    source = Path(ci.__file__).with_name("goalflight_remote_ci_node.py").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    helper_dir = config.boxes["box-a"].managed_run_directory / "helpers" / "remote-ci"
    helper = helper_dir / f"goalflight_remote_ci_node-{digest}.py"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: node.call("health"), range(2)))

    assert len(results) == 2
    assert helper.is_file(), "concurrent calls left no installed helper"
    assert helper.read_bytes() == source
    assert list(helper_dir.iterdir()) == [helper], (
        "concurrent install left a partial or temporary helper visible")

def test_concurrent_first_installs_claim_the_managed_root_before_writing(tmp_path):
    root_a = tmp_path / "node-a"
    root_b = tmp_path / "node-b"
    raw_a = _raw_config(tmp_path / "config-a")
    raw_a["boxes"]["box-a"]["managed_run_directory"] = str(root_a)
    path_a = tmp_path / "config-a.json"
    path_a.write_text(json.dumps(raw_a), encoding="utf-8")
    config_a = _load_fake_node_config(path_a)
    raw_b = _raw_config(tmp_path / "config-b")
    raw_b["boxes"]["box-a"]["managed_run_directory"] = str(root_b)
    path_b = tmp_path / "config-b.json"
    path_b.write_text(json.dumps(raw_b), encoding="utf-8")
    config_b = _load_fake_node_config(path_b)
    executor = ScriptedExecutor(tmp_path / "fake-node")
    probe_barrier = Barrier(2)

    def simultaneous_probes(argv, env, timeout, **kwargs):
        result = executor(argv, env, timeout, **kwargs)
        if len(shlex.split(argv[2])) == 4 and result.returncode == 73:
            probe_barrier.wait(timeout=5)
        return result

    node_a = RemoteRunner(config_a, executor=simultaneous_probes).nodes["box-a"]
    node_b = RemoteRunner(config_b, executor=simultaneous_probes).nodes["box-a"]

    def call(node):
        try:
            node.call("health")
            return None
        except ci.RemoteCIError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        failures = list(pool.map(call, (node_a, node_b)))

    assert sum(failure is None for failure in failures) == 1
    pinned = json.loads((executor.root / "authority.json").read_text(encoding="utf-8"))[
        "managed_root"]
    loser_root = root_b if pinned == str(root_a) else root_a
    assert not (loser_root / "helpers").exists(), (
        "rejected concurrent root received a helper install")

def test_watch_uses_configured_poll_interval_in_steady_state(tmp_path):
    config = replace(_config(tmp_path), poll_seconds=5.0)
    sleeps = []
    runner = RemoteRunner(config, executor=lambda argv, env, timeout: None,
                          sleeper=sleeps.append)
    identity = {
        "host": "measured-node", "pid": "4", "start_token": "tok",
        "run_dir": "/runs/abc", "lease_id": "abc", "lease_token": "lease",
    }

    class Node:
        box = config.boxes["box-a"]

        def __init__(self):
            self.polls = 0

        def call(self, operation, **kwargs):
            assert operation == "status"
            self.polls += 1
            if self.polls == 1:
                return {"remote_run": identity, "holder_alive": True, "result": None}
            return {"remote_run": identity, "holder_alive": True,
                    "result": {"status": "capacity-refused", "returncode": 75}}

    runner._watch(spec(), Node(), {"run_dir": "/runs/abc", "lease_token": "lease"}, {})

    assert sleeps == [5.0], "steady-state watch poll was capped below daemon.poll_seconds"

def test_watch_waits_for_setup_result_before_remote_identity_exists(tmp_path):
    runner = RemoteRunner(_config(tmp_path), executor=lambda argv, env, timeout: None,
                          sleeper=lambda seconds: None)

    class Node:
        def __init__(self):
            self.polls = 0

        def call(self, operation, **kwargs):
            assert operation == "status"
            self.polls += 1
            if self.polls == 1:
                return {"state": "admitted", "command_present": True,
                        "holder_alive": True, "result": None}
            return {"state": "released", "holder_alive": True,
                    "result": {"status": "died", "returncode": 2,
                               "error": "test virtualenv setup failed"}}

    node = Node()
    outcome = runner._watch(
        spec(), node, {"run_dir": "/runs/setup", "lease_token": "lease"}, {})

    assert outcome.status == "setup-failed"
    assert outcome.returncode == 2
    assert outcome.error == "test virtualenv setup failed"
    assert outcome.identity is None
    assert node.polls == 2


def test_setup_failure_uses_stderr_tail_when_node_has_no_error(tmp_path):
    runner = RemoteRunner(_config(tmp_path), executor=lambda *_args, **_kwargs: None)

    class Node:
        def call(self, operation, **_kwargs):
            assert operation == "status"
            return {
                "state": "released", "holder_alive": True,
                "result": {
                    "status": "setup-failed", "returncode": 2,
                    "exit_known": False, "command_started": False,
                    "streams": {
                        "stderr": {"tail": {
                            "text": "node setup could not resolve selector\n",
                            "truncated": False,
                        }},
                    },
                },
            }

    outcome = runner._watch(
        spec("stderr-setup-failure"), Node(),
        {"run_dir": "/runs/setup", "lease_token": "lease"}, {},
    )
    assert outcome.status == "setup-failed"
    assert outcome.to_dict()["error"] == "node setup could not resolve selector"

def test_watch_times_setup_execution_and_finalization_as_separate_phases(
        tmp_path, monkeypatch):
    base_config = _config(tmp_path)
    config = replace(base_config, runner=replace(base_config.runner, timeout_seconds=0.1))
    clock = [100.0]
    monkeypatch.setattr(ci.time, "monotonic", lambda: clock[0])
    calls = []
    identity = {
        "host": "measured-node", "pid": "4", "start_token": "tok",
        "run_dir": "/runs/phases", "lease_id": "phases", "lease_token": "lease",
    }
    states = [
        ("provisioning", False), ("running", None), ("running", False),
        ("running", True), ("running", True), ("draining", True),
        ("released", True),
    ]

    class Node:
        box = config.boxes["box-a"]

        def call(self, operation, **_kwargs):
            calls.append(operation)
            if operation == "cancel":
                return {"status": "cancelled"}
            assert operation == "status"
            state, command_started = states.pop(0)
            result = ({"status": "cancelled", "returncode": 130,
                       "timed_out": False} if state == "released" else None)
            status = {
                "state": state, "remote_run": identity, "holder_alive": True,
                "sample": {"hostname": "measured-node"}, "result": result,
            }
            if command_started is not None:
                status["command_started"] = command_started
            return status

    def advance(_seconds):
        clock[0] += 0.11

    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None,
                          sleeper=advance)
    outcome = runner._watch(
        spec("phase-timing"), Node(),
        {"run_dir": "/runs/phases", "lease_token": "lease"}, {},
    )

    assert outcome.status == "timeout"
    assert outcome.timed_out is True
    assert outcome.cancelled is True
    assert calls == [
        "status", "status", "status", "status", "status", "cancel", "status", "status",
    ]
    assert clock[0] > 100.5, "the finalization result was not allowed after execution timeout"

def test_remote_result_budget_uses_the_watch_phase_budgets(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config = replace(config, runner=replace(config.runner, timeout_seconds=1200))
    monkeypatch.setattr(ci, "REMOTE_SETUP_TIMEOUT_SECONDS", 11)
    monkeypatch.setattr(ci, "REMOTE_FINALIZATION_TIMEOUT_SECONDS", 13)
    monkeypatch.setattr(ci, "SNAPSHOT_SETUP_TIMEOUT_SECONDS", 17)
    monkeypatch.setattr(ci, "BUNDLE_BASIS_TIMEOUT_SECONDS", 19)
    monkeypatch.setattr(ci, "BUNDLE_PREPARATION_TIMEOUT_SECONDS", 23)
    monkeypatch.setattr(ci, "BUNDLE_TRANSFER_TIMEOUT_SECONDS", 29)

    phases = ci._remote_phase_budgets(config)
    expected_margin = max(30.0, 2.0 * config.poll_seconds + 10.0)

    assert phases == {"setup": 11, "execution": 1200, "finalization": 13}
    assert ci._remote_result_budget_seconds(config) == (
        17 + 19 + 23 + 29 + sum(phases.values())
        + 2 * config.admission.max_wait_seconds + expected_margin
    )

def test_pair_result_budget_scales_with_recheck_arms(tmp_path):
    config = _config(tmp_path)
    single_request_budget = ci._remote_result_budget_seconds(config)

    assert ci._remote_result_budget_seconds(
        config, executions=2 + 2 * 3 * 2,
    ) == single_request_budget * (2 + 2 * 3 * 2)

def test_watch_gives_setup_execution_and_finalization_separate_deadlines(
    tmp_path, monkeypatch,
):
    raw = _raw_config(tmp_path)
    raw["runner"]["timeout_seconds"] = 7200
    config_path = tmp_path / "phase-config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(ci.time, "monotonic", clock.monotonic)
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None,
                          sleeper=clock.advance)
    node = runner.nodes["box-a"]
    identity = {
        "host": "measured-node", "pid": "4", "start_token": "tok",
        "run_dir": "/runs/phases", "lease_id": "phase", "lease_token": "lease",
    }
    receipt = json.loads(
        (Path(__file__).resolve().parents[2]
         / "tests" / "fixtures" / "remote_ci" / "receipt-pass.json").read_text(
             encoding="utf-8",
         )
    )
    receipt["hostname"] = "measured-node"
    records = [
        (50, {"phase": "setup"}),
        (50, {"phase": "execution"}),
        (7150, {"phase": "finalization"}),
        (7205, {"phase": "finalization"}),
        (7250, {"phase": "finalization", "result": {
                    "status": "completed", "exit_known": True,
                    "command_started": True, "returncode": 0,
                    "streams": {
                        "stdout": {"tail": {"text": json.dumps(receipt), "truncated": False}},
                        "stderr": {"tail": {"text": "", "truncated": False}},
                    },
                }}),
    ]
    calls = []

    class Node:
        box = node.box

        def call(self, operation, **kwargs):
            calls.append(operation)
            assert operation == "status"
            target, values = records.pop(0)
            clock.now = max(clock.now, target)
            return {
                "remote_run": identity, "holder_alive": True,
                "sample": {"hostname": "measured-node"}, **values,
            }

    outcome = runner._watch(
        spec("phase-deadlines"), Node(),
        {"run_dir": "/runs/phases", "lease_token": "lease"}, {},
    )

    assert outcome.status == "green"
    assert calls == ["status"] * 5

def test_watch_setup_deadline_applies_to_provisioning_identity(tmp_path, monkeypatch):
    raw = _raw_config(tmp_path)
    raw["runner"]["timeout_seconds"] = 7200
    config_path = tmp_path / "setup-phase-config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    clock = Clock()
    monkeypatch.setattr(ci.time, "monotonic", clock.monotonic)
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None,
                          sleeper=lambda _seconds: None)
    identity = {
        "host": "measured-node", "pid": "4", "start_token": "tok",
        "run_dir": "/runs/setup-phase", "lease_id": "setup", "lease_token": "lease",
    }

    class Node:
        box = runner.nodes["box-a"].box
        operations = []

        def call(self, operation, **kwargs):
            self.operations.append(operation)
            if operation == "status":
                clock.now = 301
                return {
                    "state": "provisioning", "remote_run": identity,
                    "holder_alive": True, "result": None,
                }
            raise AssertionError(f"unexpected operation: {operation}")

    node = Node()
    with pytest.raises(ci.AmbiguousRemoteError, match="remote setup exceeded"):
        runner._watch(
            spec("delayed-setup-phase"), node,
            {"run_dir": "/runs/setup-phase", "lease_token": "lease"}, {},
        )

    assert node.operations == ["status"]

def test_reaper_uses_the_current_phase_deadline(monkeypatch):
    monkeypatch.setattr(ci_node.time, "time", lambda: 1000.0)

    assert not ci_node.past_deadline({
        "phase": "finalization", "phase_deadline_epoch": 1300.0,
        "deadline_epoch": 900.0,
    })
    assert ci_node.past_deadline({
        "phase": "finalization", "phase_deadline_epoch": 999.0,
        "deadline_epoch": 1300.0,
    })

def test_queued_arm_status_poll_uses_configured_interval(tmp_path):
    raw = _raw_config(tmp_path)
    raw["daemon"]["poll_seconds"] = 5.0
    raw["admission"]["queue_wait_seconds"] = 30.0
    path = tmp_path / "queued-poll.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    sleeps = []
    phase = {"state": "queued"}
    runner = RemoteRunner(config, executor=lambda argv, env, timeout: None,
                          sleeper=lambda seconds: (sleeps.append(seconds),
                                                   phase.update(state="admitted")))
    remote_node = runner.nodes["box-a"]
    queued = {
        "run_directory": "/managed/runs/queued",
        "lease_token": "lease-token",
        "lease_id": "lease-id",
        **LEASE_ROUTE,
        "owner_identity": "owner",
        "token_index": 0,
        "sample": {"self_cap": 4},
        "slot": "/managed/repos/default/seats/s-01",
        "holder_alive": True,
    }

    class QueuedNode:
        box = remote_node.box
        pinned_host = None

        def with_pinned_host(self, host):
            self.pinned_host = host
            return self

        def call(self, operation, **values):
            if operation == "enqueue":
                return dict(queued, state="queued")
            if operation == "status":
                return dict(queued, state=phase["state"])
            if operation == "list":
                return []
            if operation == "start":
                return {"status": "started"}
            raise AssertionError(f"unexpected node operation: {operation}")

    runner.nodes["box-a"] = QueuedNode()
    runner.choose_box = lambda requested=None, **_kwargs: ci.BoxPlacement(
        requested or "box-a", "measured-node", "192.0.2.10",
    )
    expected = ci.ArmOutcome(spec().arm, "green", 0, None,
                             bundle_bytes=0)
    runner._watch = lambda arm_spec, node, key, owner: expected

    assert runner.run_arm(spec()) == expected
    assert sleeps == [5.0], "queued status poll was capped below daemon.poll_seconds"

def test_admission_command_window_outlives_a_60_second_controller_poll(tmp_path):
    config = replace(_config(tmp_path), poll_seconds=60.0)
    requests = []

    def executor(argv, env, timeout):
        shell = shlex.split(argv[2])
        requests.append(json.loads(base64.b64decode(shell[3])))
        return CommandResult(
            0, json.dumps({"protocol_version": ci.NODE_PROTOCOL_VERSION,
                           "result": {"state": "queued"}}), "", False
        )

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    node.call("enqueue")

    request = requests[0]
    controller_poll = request.get("controller_poll_seconds")
    assert controller_poll == 60.0, (
        "enqueue request omitted the controller's configured polling interval"
    )
    command_window = ci_node.admission_command_window_seconds(controller_poll)
    assert command_window == (
        ci_node.BUNDLE_BASIS_TIMEOUT_SECONDS
        + ci_node.BUNDLE_PREPARATION_TIMEOUT_SECONDS
        + ci_node.BUNDLE_TRANSFER_TIMEOUT_SECONDS
        + 130.0
    )
    assert command_window > (
        ci.BUNDLE_PREPARATION_TIMEOUT_SECONDS
        + ci.BUNDLE_TRANSFER_TIMEOUT_SECONDS
        + ci.REMOTE_STATUS_TIMEOUT_SECONDS
    ), (
        "holder expires before basis negotiation, bundle creation, and upload finish"
    )

def test_bundle_basis_pool_wait_does_not_hold_admission_lock(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    lease_id = "a" * 32
    run = managed / "runs" / lease_id
    repo_root = managed / "repos" / "goal-flight"
    mirror = repo_root / "mirror.git"
    (run).mkdir(parents=True)
    mirror.mkdir(parents=True)
    admission.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "lease_id": lease_id, "lease_token": "lease-token",
        "repo": "goal-flight", "state": "admitted",
    })
    commit = "b" * 40
    pool_entered = threading.Event()
    continue_pool = threading.Event()

    @contextmanager
    def delayed_pool_lock(_managed, _repo, *, deadline=None, **_kwargs):
        assert deadline is not None, "basis pool-lock wait must be bounded"
        pool_entered.set()
        assert continue_pool.wait(2)
        yield repo_root, 123

    monkeypatch.setattr(ci_node, "_repo_pool_lock", delayed_pool_lock)
    monkeypatch.setattr(ci_node, "_pool_git", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=commit))
    monkeypatch.setattr(ci_node, "_mirror_has_commit", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ci_node, "_update_pool_ref", lambda *_args, **_kwargs: None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        basis = pool.submit(
            ci_node.pin_bundle_basis, managed, "goal-flight", lease_id, "lease-token",
        )
        assert pool_entered.wait(1)
        admission_lock = pool.submit(ci_node._with_queue_lock, admission).result(timeout=0.5)
        admission_lock.close()
        continue_pool.set()
        assert basis.result(timeout=2) == commit

def test_released_basis_pin_cleanup_runs_after_admission_unlock(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    lease_id = "c" * 32
    run = managed / "runs" / lease_id
    admission.mkdir(parents=True)
    run.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "lease_id": lease_id, "lease_token": "lease-token",
        "repo": "goal-flight", "state": "running",
        "pending_result": {"status": "completed", "returncode": 0},
    })
    release_entered = threading.Event()
    continue_release = threading.Event()

    def delayed_pin_release(_managed, _lease):
        release_entered.set()
        assert continue_release.wait(2)

    monkeypatch.setattr(ci_node, "clear_tree", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", delayed_pin_release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finishing = pool.submit(
            ci_node.finish_dead_workload, admission, run, {}, "operator-clear",
        )
        if not release_entered.wait(1):
            finishing.result(timeout=1)
            pytest.fail("pin release never started")
        admission_lock = pool.submit(ci_node._with_queue_lock, admission).result(timeout=0.5)
        admission_lock.close()
        continue_release.set()
        assert finishing.result(timeout=2)["status"] == "cleared"

def test_status_returns_stream_sizes_hashes_bounded_tails_and_omitted_counts(
        tmp_path, monkeypatch):
    stdout = b"out-prefix\n" + b"o" * 2048
    stderr = b"err-prefix\n" + b"e" * 1536
    node, request = _completed_run(tmp_path, monkeypatch, stdout=stdout, stderr=stderr)

    status = node.dispatch({**request, "operation": "status", "result_tail_bytes": 1024})
    streams = status.get("result", {}).get("streams")
    assert isinstance(streams, dict), "status reply lacks bounded stream receipts"

    for name, body in (("stdout", stdout), ("stderr", stderr)):
        receipt = streams[name]
        tail = receipt["tail"]
        assert receipt["size_bytes"] == len(body)
        assert receipt["sha256"] == hashlib.sha256(body).hexdigest()
        assert tail["text"].encode("utf-8") == body[-1024:]
        assert base64.b64decode(tail["bytes_base64"]) == body[-1024:]
        assert tail["truncated"] is True
        assert tail["omitted_bytes"] == len(body) - 1024
    assert "stdout" not in status["result"] and "stderr" not in status["result"]

def test_status_receipt_marks_streams_missing_from_legacy_retained_runs(tmp_path, monkeypatch):
    node, request = _completed_run(tmp_path, monkeypatch, stdout=b"out", stderr=b"err")
    run = Path(request["run_dir"])
    (run / "stdout").unlink()
    (run / "stderr").unlink()

    status = node.dispatch({**request, "operation": "status"})

    for name in ("stdout", "stderr"):
        receipt = status["result"]["streams"][name]
        assert receipt["available"] is False
        assert receipt["size_bytes"] == 0
        assert receipt["sha256"] == hashlib.sha256(b"").hexdigest()
        assert receipt["tail"] == {
            "text": "", "bytes_base64": "", "truncated": False, "omitted_bytes": 0,
        }

def test_status_receipt_preserves_non_utf8_tail_bytes(tmp_path, monkeypatch):
    body = b"valid-prefix\xff\x00invalid-tail"
    node, request = _completed_run(tmp_path, monkeypatch, stdout=body, stderr=b"")

    status = node.dispatch({**request, "operation": "status", "result_tail_bytes": 64})

    tail = status["result"]["streams"]["stdout"]["tail"]
    assert tail["truncated"] is False
    assert base64.b64decode(tail["bytes_base64"]) == body
    assert "\ufffd" in tail["text"]

def test_result_tail_size_has_a_sane_configured_limit(tmp_path):
    assert _config(tmp_path).result_tail_kib == 16
    raw = _raw_config(tmp_path)
    raw["daemon"]["result_tail_kib"] = 65
    path = tmp_path / "oversized-tail.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ci.ConfigError, match="result_tail_kib must be at most 64 KiB"):
        ci.load_config(path)

def test_fetch_returns_compressed_tail_and_refuses_requests_above_its_cap(
        tmp_path, monkeypatch):
    stdout = b"log-prefix" + b"z" * (300 * 1024)
    node, request = _completed_run(tmp_path, monkeypatch, stdout=stdout, stderr=b"")

    try:
        fetched = node.dispatch({**request, "operation": "fetch", "stream": "stdout", "last_kb": 2})
    except Exception as exc:
        fetched = {"status": "error", "error": str(exc)}
    assert fetched.get("compression") == "gzip", "explicit fetch op did not return compressed bytes"
    body = gzip.decompress(base64.b64decode(fetched["body_gzip_base64"]))
    assert body == stdout[-(2 * 1024):]
    assert fetched["size_bytes"] == len(body)
    assert fetched.get("truncated") is True, "fetched tail truncation was not marked"
    assert fetched.get("omitted_bytes") == len(stdout) - len(body), (
        "fetched tail omitted-byte count was not reported"
    )

    refused = node.dispatch({**request, "operation": "fetch", "stream": "stdout", "last_kb": 257})
    assert refused["status"] == "refused"
    assert refused["stream_size_bytes"] == len(stdout)
    assert refused["requested_bytes"] == 257 * 1024
    assert refused["cap_bytes"] == 256 * 1024
    assert str(257 * 1024) in refused["error"] and str(256 * 1024) in refused["error"]

def test_remote_node_fails_closed_on_protocol_version_mismatch(tmp_path):
    config = _config(tmp_path)

    def old_node(argv, env, timeout):
        return CommandResult(0, json.dumps({"protocol_version": 1,
                                           "result": {"stdout": "full old body"}}), "", False)

    runner = RemoteRunner(config, executor=old_node)

    with pytest.raises(ci.RemoteCIError, match="protocol mismatch.*expected 5.*received 1"):
        runner.nodes["box-a"].call("status")

def test_remote_node_fails_closed_on_unversioned_response_after_ssh_error(tmp_path):
    config = _config(tmp_path)

    def old_node(argv, env, timeout):
        return CommandResult(255, json.dumps({"stdout": "full old body"}),
                            "connection closed", False)

    runner = RemoteRunner(config, executor=old_node)

    with pytest.raises(ci.RemoteCIError, match="protocol mismatch.*unversioned legacy response"):
        runner.nodes["box-a"].call("status")

def test_node_rejects_controller_request_with_wrong_protocol_version(node_env):
    _, executor, _, node = node_env
    executor.before = lambda payload: payload.__setitem__("protocol_version", 1)

    with pytest.raises(ci.RemoteCIError, match="protocol mismatch: expected 5, received 1"):
        node.call("health")

def test_direct_ssh_uses_private_persistent_control_socket(tmp_path):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    commands = []

    def executor(argv, env, timeout):
        commands.append(tuple(argv))
        return CommandResult(0, json.dumps({"protocol_version": ci.NODE_PROTOCOL_VERSION,
                                            "result": []}), "", False)

    runner = RemoteRunner(config, executor=executor)
    node = runner.nodes["box-a"]
    assert node.call("list") == []
    assert node.call("list") == []

    control_dir = Path("/tmp") / f"gf-ssh-{os.geteuid()}"
    assert control_dir.is_dir(), "direct SSH did not create the private per-user socket directory"
    info = control_dir.lstat()
    assert stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    assert info.st_uid == os.geteuid()
    assert stat.S_IMODE(info.st_mode) == 0o700
    socket_paths = []
    for command in commands:
        assert "-oControlMaster=auto" in command
        assert "-oControlPersist=5m" in command
        option = next(value for value in command if value.startswith("-oControlPath="))
        socket_path = Path(option.partition("=")[2])
        socket_paths.append(socket_path)
        assert socket_path.parent == control_dir
        assert len(os.fsencode(socket_path)) < 104
        assert len(socket_path.name) == 16
        assert all(character in "0123456789abcdef" for character in socket_path.name)
    assert socket_paths[0] == socket_paths[1]

def test_control_socket_key_separates_box_names_for_same_host():
    argv = ("ssh", "same-worker.example.invalid", "{script}")
    control_dir = Path("/tmp/gf-ssh-501")

    first = ci._ssh_control_path("box-a", argv, control_dir)
    second = ci._ssh_control_path("box-b", argv, control_dir)

    assert first != second, "two configured boxes shared a control socket"

def test_control_socket_key_separates_changed_remote_exec_template():
    control_dir = Path("/tmp/gf-ssh-501")
    first = ci._ssh_control_path(
        "box-a", ("ssh", "-p", "22", "worker.example.invalid", "{script}"), control_dir
    )
    second = ci._ssh_control_path(
        "box-a", ("ssh", "-p", "2222", "worker.example.invalid", "{script}"), control_dir
    )

    assert first != second, "changed remote_exec template reused a control socket"


def test_control_socket_key_separates_pinned_destinations():
    control_dir = Path("/tmp/gf-ssh-501")
    argv = ("ssh", "worker-alias", "{script}")

    first = ci._ssh_control_path("box-a", argv, control_dir, "worker-a")
    second = ci._ssh_control_path("box-a", argv, control_dir, "worker-b")

    assert first != second, "different pinned hosts reused one SSH control socket"

def test_long_state_directory_still_uses_short_ssh_socket_path(tmp_path):
    raw = _raw_config(tmp_path)
    raw["paths"]["state_dir"] = str(tmp_path / ("state" * 24))
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-long-state.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    runner = RemoteRunner(_load_fake_node_config(path), executor=lambda argv, env, timeout: None)

    command = runner.nodes["box-a"]._command_template()
    control_path = next(value.partition("=")[2] for value in command
                        if value.startswith("-oControlPath="))

    assert len(os.fsencode(control_path)) < 104
    assert Path(control_path).parent == Path("/tmp") / f"gf-ssh-{os.geteuid()}"

def _inject_control_directory_metadata(node, monkeypatch, *, mode, owner_uid):
    node.control_dir = Path("/tmp") / f"gf-test-{os.getpid()}"
    monkeypatch.setattr(Path, "mkdir", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(
        ci.os, "lstat",
        lambda target: SimpleNamespace(st_mode=mode, st_uid=owner_uid),
    )

def test_insecure_ssh_socket_directory_disables_multiplexing_with_a_warning(
        tmp_path, monkeypatch, caplog):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-insecure-socket.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    result = CommandResult(0, json.dumps({"protocol_version": ci.NODE_PROTOCOL_VERSION,
                                         "result": []}), "", False)
    commands = []

    def executor(argv, env, timeout):
        commands.append(tuple(argv))
        return result

    runner = RemoteRunner(_load_fake_node_config(path), executor=executor)
    node = runner.nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o755, owner_uid=os.geteuid()
    )

    assert node.call("list") == []
    assert commands[0][1:4] == (
        "-oControlMaster=no", "-oControlPersist=no", "-oControlPath=none",
    )
    assert "-oControlMaster=auto" not in commands[0]
    assert "-oControlPersist=5m" not in commands[0]
    assert "-oControlPath=none" in commands[0]
    assert "permissions 0755 are not private mode 0700" in caplog.text

def test_foreign_owned_ssh_socket_directory_disables_multiplexing(
        tmp_path, monkeypatch, caplog):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-foreign-socket.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    result = CommandResult(0, json.dumps({"protocol_version": ci.NODE_PROTOCOL_VERSION,
                                         "result": []}), "", False)
    commands = []

    def executor(argv, env, timeout):
        commands.append(tuple(argv))
        return result

    runner = RemoteRunner(_load_fake_node_config(path), executor=executor)
    node = runner.nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o700, owner_uid=os.geteuid() + 1
    )

    assert node.call("list") == []
    assert commands[0][1:4] == (
        "-oControlMaster=no", "-oControlPersist=no", "-oControlPath=none",
    )
    assert "directory owner uid" in caplog.text

def test_overlong_ssh_socket_directory_disables_multiplexing(tmp_path, caplog):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-long-socket.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    runner = RemoteRunner(_load_fake_node_config(path), executor=lambda argv, env, timeout: None)
    node = runner.nodes["box-a"]
    node.control_dir = Path("/tmp") / ("x" * 120)

    command = node._command_template()

    assert command[1:4] == (
        "-oControlMaster=no", "-oControlPersist=no", "-oControlPath=none",
    )
    assert "ControlPath" in caplog.text and "disabling SSH multiplexing" in caplog.text


@pytest.mark.parametrize("stderr", [
    "mm_send_fd: sendmsg(2): Message too long",
    "mux_client_request_session: send fds failed",
    "mux_client_request_session: session request failed: Session open refused by peer",
    "mux_client_request_session: unexpected response",
    "Control socket connect(/tmp/control.sock): Connection refused",
])
def test_ssh_mux_status_failure_retries_without_multiplexing_and_collects_result(
    tmp_path, monkeypatch, stderr,
):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh-retry.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    commands = []
    status_record = {
        "state": "finished",
        "result": {"status": "passed", "returncode": 0, "exit_known": True},
    }

    def executor(argv, _env, _timeout, **_kwargs):
        commands.append(tuple(argv))
        if len(commands) == 1:
            return CommandResult(255, "", stderr, False)
        return CommandResult(0, json.dumps({
            "protocol_version": ci.NODE_PROTOCOL_VERSION, "result": status_record,
        }), "", False)

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o700, owner_uid=os.geteuid(),
    )

    assert node.call("status", run_dir="/managed/run", lease_token="token") == status_record
    assert len(commands) == 2
    assert "-oControlMaster=auto" in commands[0]
    assert "-oControlMaster=no" in commands[1]
    assert "-oControlPath=none" in commands[1]


def test_timed_out_ssh_mux_status_retries_and_collects_result(tmp_path, monkeypatch):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh-timeout-retry.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    commands = []
    status_record = {
        "state": "finished",
        "result": {"status": "passed", "returncode": 0, "exit_known": True},
    }

    def executor(argv, _env, _timeout, **_kwargs):
        commands.append(tuple(argv))
        if len(commands) == 1:
            return CommandResult(
                124, "", "Session open refused by peer", True,
            )
        return CommandResult(0, json.dumps({
            "protocol_version": ci.NODE_PROTOCOL_VERSION, "result": status_record,
        }), "", False)

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o700, owner_uid=os.geteuid(),
    )

    assert node.call("status", run_dir="/managed/run", lease_token="token") == status_record
    assert len(commands) == 2
    assert "-oControlPath=none" in commands[1]


def test_ssh_mux_status_timeout_retries_within_remaining_deadline(
    tmp_path, monkeypatch,
):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh-retry-deadline.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    commands = []
    timeouts = []
    clock = iter((10.0, 11.0, 13.0))
    monkeypatch.setattr(ci.time, "monotonic", lambda: next(clock))

    def executor(argv, _env, timeout, **_kwargs):
        commands.append(tuple(argv))
        timeouts.append(timeout)
        if len(commands) == 1:
            return CommandResult(255, "", "Session open refused by peer", False)
        return _remote_response({"state": "finished"})

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o700, owner_uid=os.geteuid(),
    )

    assert node.call(
        "status", _transport_timeout=5,
        run_dir="/managed/run", lease_token="token",
    ) == {"state": "finished"}
    assert len(commands) == 2
    assert timeouts == [4.0, 2.0]


def test_exhausted_ssh_retry_deadline_does_not_retry(tmp_path, monkeypatch):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh-expired-deadline.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    commands = []
    clock = iter((10.0, 11.0, 16.0))
    monkeypatch.setattr(ci.time, "monotonic", lambda: next(clock))

    def executor(argv, _env, _timeout, **_kwargs):
        commands.append(tuple(argv))
        return CommandResult(255, "", "Session open refused by peer", False)

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o700, owner_uid=os.geteuid(),
    )

    with pytest.raises(ci.RemoteCIError, match="Session open refused"):
        node.call(
            "start", _transport_timeout=5,
            run_dir="/managed/run", lease_token="token",
        )
    assert len(commands) == 1


def test_non_idempotent_submit_timeout_with_mux_error_does_not_retry(
    tmp_path, monkeypatch,
):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh-submit-timeout.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    commands = []
    bundle_path = tmp_path / "source.bundle"
    bundle_path.write_bytes(b"bundle")

    def executor(argv, _env, _timeout, **_kwargs):
        commands.append(tuple(argv))
        return CommandResult(
            124, "", "mux_client_request_session: Session open refused by peer",
            True,
        )

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o700, owner_uid=os.geteuid(),
    )

    with pytest.raises(ci.RemoteTransportTimeoutError, match="submit failed"):
        node.call("submit", candidate_bundle_path=bundle_path)
    assert len(commands) == 1


def test_non_idempotent_start_does_not_retry_session_refused(tmp_path, monkeypatch):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh-start-refused.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    commands = []

    def executor(argv, _env, _timeout, **_kwargs):
        commands.append(tuple(argv))
        return CommandResult(255, "", "Session open refused by peer", False)

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o700, owner_uid=os.geteuid(),
    )

    with pytest.raises(ci.RemoteCIError, match="Session open refused"):
        node.call("start", run_dir="/managed/run", lease_token="token")
    assert len(commands) == 1


def test_mux_refusal_fallback_side_effect_transport_failure_does_not_replay(
    tmp_path, monkeypatch,
):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh-submit-fallback-side-effect.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    commands = []
    remote_side_effects = []
    bundle_path = tmp_path / "source.bundle"
    bundle_path.write_bytes(b"bundle")

    def executor(argv, _env, _timeout, **_kwargs):
        commands.append(tuple(argv))
        # One ssh process sees the mux refusal, opens its automatic direct
        # fallback, performs the submit, then loses the response channel.
        remote_side_effects.append("ticket-created")
        return CommandResult(
            255, "", "mux_client_request_session: Session open refused by peer; "
            "direct fallback executed; connection reset",
            False,
        )

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o700, owner_uid=os.geteuid(),
    )

    with pytest.raises(ci.RemoteCIError, match="submit failed"):
        node.call("submit", candidate_bundle_path=bundle_path)

    assert len(commands) == 1
    assert remote_side_effects == ["ticket-created"]


def test_node_handler_timing_is_opt_in_and_written_per_call(tmp_path):
    managed = tmp_path / "managed"
    run = managed / "runs" / "run-one"
    run.mkdir(parents=True)
    (run / "lease.json").write_text(json.dumps({
        "lease_token": "token-one",
        "run_directory": str(run),
        "state": "admitted",
    }), encoding="utf-8")
    (run / "owner.json").write_text("{}", encoding="utf-8")
    timing_dir = tmp_path / "timings"
    request = {
        "protocol_version": ci_node.PROTOCOL_VERSION,
        "operation": "status",
        "request_id": "timing-status-one",
        "managed_root": str(managed),
        "p_cores": 2,
        "token_pool_size": 1,
        "run_dir": str(run),
        "lease_token": "token-one",
    }
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "GOALFLIGHT_REMOTE_CI_TIMING_DIR": str(timing_dir),
    }
    encoded = base64.b64encode(json.dumps(request).encode()).decode("ascii")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "goalflight_remote_ci_node.py"), encoded],
        capture_output=True, text=True, env=env, timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"]["lease_token"] == "token-one"
    records = list(timing_dir.glob("*.json"))
    assert len(records) == 1
    timing = json.loads(records[0].read_text(encoding="utf-8"))
    assert timing["operation"] == "status"
    assert isinstance(timing["hostname"], str) and timing["hostname"]
    assert len(timing["load_average"]) == 3
    steps = [event["step"] for event in timing["events"]]
    assert steps[0] == "handler_entry"
    assert "admission_lock_acquired" in steps
    assert "status_snapshot_start" in steps
    assert "response_write_start" in steps
    assert steps[-1] == "response_write_end"
    timestamps = [event["monotonic_ns"] for event in timing["events"]]
    assert timestamps == sorted(timestamps)


def test_ssh_non_mux_failure_does_not_retry(tmp_path, monkeypatch):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh-no-retry.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    commands = []

    def executor(argv, _env, _timeout, **_kwargs):
        commands.append(tuple(argv))
        return CommandResult(255, "", "Permission denied (publickey).", False)

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o700, owner_uid=os.geteuid(),
    )

    with pytest.raises(ci.RemoteCIError, match="Permission denied"):
        node.call("status", run_dir="/managed/run", lease_token="token")
    assert len(commands) == 1


def test_non_idempotent_start_retries_fatal_fd_send_failure(tmp_path, monkeypatch):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh-start-retry.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    commands = []

    def executor(argv, _env, _timeout, **_kwargs):
        commands.append(tuple(argv))
        if len(commands) == 1:
            return CommandResult(
                255, "", "mux_client_request_session: send fds failed", False,
            )
        return CommandResult(0, json.dumps({
            "protocol_version": ci.NODE_PROTOCOL_VERSION,
            "result": {"started": True},
        }), "", False)

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFDIR | 0o700, owner_uid=os.geteuid(),
    )

    assert node.call("start", run_dir="/managed/run", lease_token="token") == {
        "started": True,
    }
    assert len(commands) == 2
    assert "-oControlPath=none" in commands[1]

def test_symlinked_ssh_socket_directory_disables_multiplexing(tmp_path, monkeypatch, caplog):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-symlink-socket.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    runner = RemoteRunner(_load_fake_node_config(path), executor=lambda argv, env, timeout: None)
    node = runner.nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFLNK | 0o777, owner_uid=os.geteuid()
    )
    command = node._command_template()

    assert command[1:4] == (
        "-oControlMaster=no", "-oControlPersist=no", "-oControlPath=none",
    )
    assert "directory is a symlink" in caplog.text

def _wrapper_config(
    tmp_path: Path, *, repo_id: str | None = None,
) -> tuple[Path, Path]:
    raw = _raw_config(tmp_path, token_pool_size=1)
    if repo_id is None:
        repo_id = ci._git_repository_id(ROOT) or "default"
    raw["repo"] = repo_id
    spool = tmp_path / "remote-ci" / "spool"
    raw["paths"].update({
        "queue_dir": str(spool / "requests"),
        "result_dir": str(spool / "results"),
        "state_dir": str(tmp_path / "remote-ci" / "state"),
    })
    raw["daemon"]["lock_file"] = str(tmp_path / "remote-ci" / "state" / "daemon.lock")
    raw["daemon"]["pid_file"] = str(tmp_path / "remote-ci" / "state" / "daemon.pid")
    raw["runner"]["heavy_command_ids"] = ["heavy-gate"]
    raw["admission"]["max_wait_seconds"] = 0.05
    raw["boxes"]["box-a"]["managed_run_directory"] = "/var/lib/goalflight/remote-ci-test"
    (spool / "requests").mkdir(parents=True)
    (spool / "results").mkdir(parents=True)
    path = tmp_path / "remote-ci.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path, spool


def _declare_repo_identity(repo_root: Path, repo_id: str) -> None:
    identity_path = repo_root / ".goal-flight" / "remote-ci.json"
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    if identity_path.exists():
        configured_repo = json.loads(identity_path.read_text(encoding="utf-8")).get("repo")
        assert configured_repo == repo_id
    else:
        identity_path.write_text(json.dumps({"repo": repo_id}) + "\n", encoding="utf-8")
    _git(repo_root, "add", str(identity_path.relative_to(repo_root)))
    _git(repo_root, "commit", "--quiet", "-m", "declare test repository identity")


def test_matched_pair_refuses_forced_local_route_without_running_locally(
    tmp_path, monkeypatch, capsys,
):
    config_path, _spool = _wrapper_config(tmp_path)
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_CONFIG", str(config_path))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", raising=False)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CPU_ROUTE", "local")
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("matched pair ran locally"),
    )

    result = ci.wrapper_main([
        "--pair", "--base", "a" * 40, "--cmd-id", "heavy-gate", "--",
        "python3", "-m", "pytest", "tests/test_one.py",
    ])

    assert result == ci.REMOTE_UNAVAILABLE_EXIT
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["route"] == "remote-unavailable"
    assert receipt["fallback_reason"] == "forced-local"


def test_matched_pair_refuses_missing_config_without_running_locally(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CPU_ROUTE", raising=False)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("matched pair ran locally"),
    )

    result = ci.wrapper_main([
        "--pair", "--base", "a" * 40, "--cmd-id", "heavy-gate", "--",
        "python3", "-m", "pytest", "tests/test_one.py",
    ])

    assert result == ci.REMOTE_UNAVAILABLE_EXIT
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["route"] == "remote-unavailable"
    assert receipt["fallback_reason"] == "configuration-unavailable"


def test_pair_wrapper_submits_base_and_prints_json_summary(tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["repo"] = ci._git_repository_id(Path.cwd())
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_CONFIG", str(config_path))
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(spool))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CPU_ROUTE", raising=False)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    submitted = {}

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        submitted.update(payload)

        def receipt(status, passed, failed, failing_nodes=()):
            return {
                "schema": ci.RECEIPT_SCHEMA,
                "hostname": "measured-node",
                "status": status,
                "returncode": 0,
                "passed": passed,
                "failed": failed,
                "skipped": 0,
                "errors": 0,
                "failing_node_ids": list(failing_nodes),
                "collection_import_errors": [],
                "first_error_lines": [],
            }
        ci._atomic_write_json(config.result_dir / f"{payload['request_id']}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": payload["request_id"],
            "status": "RED",
            "base": {
                "status": "green", "returncode": 0,
                "receipt": {**receipt("pass", 5, 0), "box_id": "studio-a"},
            },
            "candidate": {
                "status": "red", "returncode": 0,
                "receipt": {**receipt("fail", 4, 1, ["test_new"]), "box_id": "studio-a"},
            },
            "new_failure_node_ids": ["test_new"],
            "fixed_failure_node_ids": ["test_fixed"],
            "rechecks": {"test_new": {"classification": "regression"}},
        })
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)

    result = ci.wrapper_main([
        "--pair", "--base", "9" * 40, "--recheck", "3", "--json",
        "--cmd-id", "heavy-gate", "--", "python3", "-m", "pytest",
        "tests/python/test_one.py",
    ])

    assert result == 1
    assert submitted["base_sha"] == "9" * 40
    assert submitted["recheck"] == 3
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["base"]["box_id"] == summary["candidate"]["box_id"] == "studio-a"
    assert summary["base"]["counts"] == {"passed": 5, "failed": 0, "skipped": 0}
    assert summary["new_failure_node_ids"] == ["test_new"]
    assert summary["fixed_failure_node_ids"] == ["test_fixed"]
    assert summary["rechecks"]["test_new"]["classification"] == "regression"
    assert summary["result_path"].endswith(f"{submitted['request_id']}.json")


def test_green_pair_wrapper_requires_both_green_arms(tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["repo"] = ci._git_repository_id(Path.cwd())
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_CONFIG", str(config_path))
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(spool))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CPU_ROUTE", raising=False)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)

    def receipt():
        return {
            "schema": ci.RECEIPT_SCHEMA,
            "hostname": "measured-node",
            "status": "pass",
            "returncode": 0,
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "failing_node_ids": [],
            "collection_import_errors": [],
            "first_error_lines": [],
        }

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        ci._atomic_write_json(config.result_dir / f"{payload['request_id']}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": payload["request_id"],
            "status": "GREEN",
            "base": {"status": "green", "returncode": 0, "receipt": receipt()},
            "candidate": {"status": "green", "returncode": 0, "receipt": receipt()},
        })
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    result = ci.wrapper_main([
        "--pair", "--base", "9" * 40, "--json", "--cmd-id", "heavy-gate",
        "--", "python3", "-m", "pytest", "tests/test_one.py",
    ])

    output = capsys.readouterr()
    assert result == 0, (result, output)
    assert json.loads(output.out.splitlines()[-1])["status"] == "GREEN"
    assert output.err == ""


def test_pair_wrapper_rejects_missing_base_arm(tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["repo"] = ci._git_repository_id(Path.cwd())
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_CONFIG", str(config_path))
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(spool))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CPU_ROUTE", raising=False)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)

    receipt = {
        "schema": ci.RECEIPT_SCHEMA,
        "hostname": "measured-node",
        "status": "pass",
        "returncode": 0,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "failing_node_ids": [],
        "collection_import_errors": [],
        "first_error_lines": [],
    }

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        ci._atomic_write_json(config.result_dir / f"{payload['request_id']}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": payload["request_id"],
            "status": "GREEN",
            "candidate": {"status": "green", "returncode": 0, "receipt": receipt},
        })
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    result = ci.wrapper_main([
        "--pair", "--base", "9" * 40, "--json", "--cmd-id", "heavy-gate",
        "--", "python3", "-m", "pytest", "tests/test_one.py",
    ])

    output = capsys.readouterr()
    assert result == 1, (result, output)
    assert output.out == ""
    assert "incomplete matched-pair result; missing base arm" in output.err


def test_wrapper_rejects_zero_test_nodes_as_harness_failure(tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["repo"] = ci._git_repository_id(Path.cwd())
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_CONFIG", str(config_path))
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(spool))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CPU_ROUTE", raising=False)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        ci._atomic_write_json(config.result_dir / f"{payload['request_id']}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": payload["request_id"],
            "status": "GREEN",
            "candidate": {
                "status": "green",
                "returncode": 0,
                "receipt": {
                    "schema": ci.RECEIPT_SCHEMA,
                    "hostname": "measured-node",
                    "status": "pass",
                    "returncode": 0,
                    "passed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "errors": 0,
                    "failing_node_ids": [],
                    "collection_import_errors": [],
                    "first_error_lines": [],
                },
                "streams": {
                    "stdout": {"tail": {"text": "1 passed in 0.1s\n"}},
                },
            },
        })
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    result = ci.wrapper_main([
        "--cmd-id", "heavy-gate", "--", "python3", "-m", "pytest",
        "tests/test_one.py",
    ])

    output = capsys.readouterr()
    assert result == 1, (result, output)
    assert output.out == "1 passed in 0.1s\n"
    assert '"status": "GREEN"' not in output.out
    assert "harness failure: no verified pytest test-node count was recorded for candidate" in output.err


def test_pair_worker_footer_names_exact_command(tmp_path):
    config_path = tmp_path / "remote-ci.json"
    config_path.write_text(json.dumps({
        "runner": {"heavy_command_ids": ["studio-gate"]},
    }), encoding="utf-8")

    prompt = ci.goalflight_dispatch_paths.remote_ci_worker_prompt({
        "GOALFLIGHT_REMOTE_RUN": "/repo/bin/goalflight-remote-run",
        "GOALFLIGHT_REMOTE_CI_CONFIG": str(config_path),
    })

    assert prompt is not None
    assert ('run the matched pair: "$GOALFLIGHT_REMOTE_RUN" --pair --base '
            '<base-sha> --cmd-id studio-gate -- <pytest command>') in prompt

def test_remote_ci_worker_environment_preserves_explicit_config(tmp_path, monkeypatch):
    explicit = tmp_path / "operator-remote-ci.json"
    old_worker_checkout = tmp_path / "old-worker-checkout"
    old_worker_checkout.mkdir()
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))

    environment = ci.goalflight_dispatch_paths.remote_ci_worker_environment(
        old_worker_checkout, {"GOALFLIGHT_REMOTE_CI_CONFIG": str(explicit)},
    )

    assert environment["GOALFLIGHT_REMOTE_CI_CONFIG"] == str(explicit)
    assert Path(environment["GOALFLIGHT_REMOTE_CI_SPOOL_DIR"]).is_dir()
    remote_run = Path(environment["GOALFLIGHT_REMOTE_RUN"])
    installed_wrapper = Path(ci.goalflight_dispatch_paths.__file__).resolve().parent.parent / "bin" / "goalflight-remote-run"
    assert remote_run == installed_wrapper.resolve()
    assert remote_run.is_file()
    assert os.access(remote_run, os.X_OK)
    assert not (old_worker_checkout / "bin" / "goalflight-remote-run").exists()


def test_remote_ci_worker_environment_omits_runner_without_project_config(
    tmp_path, monkeypatch
):
    project_root = tmp_path / "project-without-remote-ci"
    project_root.mkdir()
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))

    environment = ci.goalflight_dispatch_paths.remote_ci_worker_environment(
        project_root, {}
    )

    assert "GOALFLIGHT_REMOTE_RUN" not in environment
    assert ci.goalflight_dispatch_paths.remote_ci_worker_prompt(environment) is None


def test_remote_ci_worker_environment_uses_each_projects_spool(tmp_path, monkeypatch):
    state = tmp_path / "controller-state"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    goalflight_root = tmp_path / "goal-flight"
    pm2_root = tmp_path / "pm2"
    goalflight_config = goalflight_root / ".goal-flight" / "remote-ci.json"
    pm2_config = pm2_root / ".goal-flight" / "remote-ci.json"
    goalflight_spool = state / "remote-ci" / "spool"
    pm2_spool = state / "remote-ci" / "projects" / "pm2" / "spool"
    projects = (
        (goalflight_root, goalflight_config, goalflight_spool, {}),
        (pm2_root, pm2_config, pm2_spool, {}),
    )
    worktree = tmp_path / "worker-worktree"
    worktree.mkdir()

    for project_root, config_path, spool, _existing in projects:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"paths": {
            "queue_dir": str(spool / "requests"),
            "result_dir": str(spool / "results"),
        }}), encoding="utf-8")

    project_spools = [entry[2] for entry in projects]
    for index, (project_root, config_path, spool, existing) in enumerate(projects):
        environment = ci.goalflight_dispatch_paths.remote_ci_worker_environment(
            project_root, existing,
        )
        assert environment["GOALFLIGHT_REMOTE_CI_CONFIG"] == str(config_path)
        assert Path(environment["GOALFLIGHT_REMOTE_CI_SPOOL_DIR"]) == spool
        assert spool != project_spools[1 - index]

        sandbox_environment = {**os.environ, **environment}
        own_requests = (spool / "requests").resolve()
        other_requests = (project_spools[1 - index] / "requests").resolve()
        codex_roots = {
            Path(path).resolve()
            for path in codex_sandbox.worker_channel_roots(sandbox_environment)
        }
        assert own_requests in codex_roots
        assert other_requests not in codex_roots
        codex_args = codex_sandbox.codex_workspace_write_args(
            str(worktree), "workspace-write", environment=sandbox_environment,
        )
        codex_roots = {
            Path(path).resolve()
            for path in json.loads(codex_args[1].split("=", 1)[1])
        }
        assert own_requests in codex_roots
        assert other_requests not in codex_roots

        _profile, seatbelt_roots = os_sandbox.macos_sandbox_profile(
            str(Path.home()), os_sandbox.OS_SANDBOX_WORKSPACE_WRITE,
            agent="grok", command="grok", environment=sandbox_environment,
        )
        resolved_seatbelt_roots = {Path(path).resolve() for path in seatbelt_roots}
        assert own_requests in resolved_seatbelt_roots
        assert other_requests not in resolved_seatbelt_roots

def test_pm2_worker_routes_to_its_project_daemon(tmp_path, monkeypatch):
    project_root = tmp_path / "pm2"
    config_path = project_root / ".goal-flight" / "remote-ci.json"
    _init_repo(project_root)
    state = tmp_path / "controller-state"
    spool = state / "remote-ci" / "projects" / "pm2" / "spool"
    state_dir = state / "remote-ci" / "projects" / "pm2" / "state"
    raw = _raw_config(tmp_path, token_pool_size=1)
    raw["repo"] = "pm2"
    raw["fallback"] = "local"
    raw["paths"].update({
        "queue_dir": str(spool / "requests"),
        "result_dir": str(spool / "results"),
        "state_dir": str(state_dir),
    })
    raw["daemon"]["lock_file"] = str(state_dir / "daemon.lock")
    raw["daemon"]["pid_file"] = str(state_dir / "daemon.pid")
    raw["runner"]["heavy_command_ids"] = ["heavy-gate"]
    raw["admission"]["max_wait_seconds"] = 0.05
    raw["boxes"]["box-a"]["managed_run_directory"] = "/var/lib/goalflight/remote-ci-test"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    _declare_repo_identity(project_root, "pm2")

    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CPU_ROUTE", raising=False)
    monkeypatch.chdir(project_root)
    environment = ci.goalflight_dispatch_paths.remote_ci_worker_environment(
        project_root, {},
    )
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    submitted = {}

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        submitted.update({
            "payload": payload,
            "repo": config.repo,
            "queue_dir": config.queue_dir,
            "result_dir": config.result_dir,
        })
        ci._atomic_write_json(
            config.result_dir / f"{payload['request_id']}.json",
            _green_wrapper_result(payload["request_id"]),
        )
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("pm2 worker fell back locally"),
    )

    assert ci.wrapper_main([
        "--kind", "gate", "--cmd-id", "heavy-gate", "--",
        sys.executable, "-c", "print('local')",
    ]) == 0

    assert submitted["repo"] == "pm2"
    assert submitted["payload"]["project_id"] == "pm2"
    assert submitted["queue_dir"] == spool / "requests"
    assert submitted["result_dir"] == spool / "results"

def test_invalid_remote_ci_spool_shape_falls_back_with_typed_reason(
    tmp_path, monkeypatch, capsys,
):
    project_root = tmp_path / "pm2"
    config_path = project_root / ".goal-flight" / "remote-ci.json"
    _init_repo(project_root)
    state = tmp_path / "controller-state"
    configured_spool = state / "remote-ci" / "projects" / "pm2" / "spool"
    wrong_results = state / "remote-ci" / "projects" / "pm2" / "other-results"
    raw = _raw_config(tmp_path, token_pool_size=1)
    raw["repo"] = "pm2"
    raw["fallback"] = "local"
    raw["paths"].update({
        "queue_dir": str(configured_spool / "requests"),
        "result_dir": str(wrong_results),
        "state_dir": str(configured_spool.parent / "state"),
    })
    raw["daemon"]["lock_file"] = str(configured_spool.parent / "state" / "daemon.lock")
    raw["daemon"]["pid_file"] = str(configured_spool.parent / "state" / "daemon.pid")
    raw["boxes"]["box-a"]["managed_run_directory"] = "/var/lib/goalflight/remote-ci-test"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    _declare_repo_identity(project_root, "pm2")

    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CPU_ROUTE", raising=False)
    environment = ci.goalflight_dispatch_paths.remote_ci_worker_environment(
        project_root, {},
    )
    assert Path(environment["GOALFLIGHT_REMOTE_CI_SPOOL_DIR"]) == state / "remote-ci" / "spool"
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(project_root)
    monkeypatch.setattr(
        ci, "daemon_is_running",
        lambda _config: pytest.fail("invalid spool shape reached daemon routing"),
    )

    assert ci.wrapper_main([
        "--kind", "gate", "--cmd-id", "heavy-gate", "--",
        sys.executable, "-c", "print('local')",
    ]) == 0

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["route"] == "local-fallback"
    assert receipt["fallback_reason"] == "invalid-spool-layout"

@pytest.mark.parametrize(("fallback", "expected_rc"), [("local", 0), ("fail", 69)])
def test_spool_creation_failure_launches_without_grant_and_follows_fallback(
    tmp_path, monkeypatch, capsys, fallback, expected_rc,
):
    project_root = tmp_path / "pm2"
    config_path = project_root / ".goal-flight" / "remote-ci.json"
    _init_repo(project_root)
    state = tmp_path / "controller-state"
    spool = state / "remote-ci" / "projects" / "pm2" / "spool"
    state_dir = state / "remote-ci" / "projects" / "pm2" / "state"
    raw = _raw_config(tmp_path, token_pool_size=1)
    raw["repo"] = "pm2"
    raw["fallback"] = fallback
    raw["paths"].update({
        "queue_dir": str(spool / "requests"),
        "result_dir": str(spool / "results"),
        "state_dir": str(state_dir),
    })
    raw["daemon"]["lock_file"] = str(state_dir / "daemon.lock")
    raw["daemon"]["pid_file"] = str(state_dir / "daemon.pid")
    raw["runner"]["heavy_command_ids"] = ["heavy-gate"]
    raw["boxes"]["box-a"]["managed_run_directory"] = "/var/lib/goalflight/remote-ci-test"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    _declare_repo_identity(project_root, "pm2")
    spool.parent.mkdir(parents=True)
    spool.write_text("blocks spool directory creation", encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_SPOOL_ERROR", raising=False)

    environment = ci.goalflight_dispatch_paths.remote_ci_worker_environment(
        project_root, {},
    )

    assert environment["GOALFLIGHT_REMOTE_CI_CONFIG"] == str(config_path)
    assert environment["GOALFLIGHT_REMOTE_CI_SPOOL_DIR"] == ""
    assert environment["GOALFLIGHT_REMOTE_CI_SPOOL_ERROR"] == "spool-unavailable"
    assert environment["GOALFLIGHT_REMOTE_RUN"]
    for key_name, value in environment.items():
        monkeypatch.setenv(key_name, value)
    monkeypatch.chdir(project_root)
    monkeypatch.setattr(
        ci, "daemon_is_running",
        lambda _config: pytest.fail("unavailable spool reached daemon routing"),
    )
    local_calls = []
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **kwargs: (
            local_calls.append(kwargs) or (0, {"fallback_reason": kwargs["fallback_reason"]})
        ),
    )

    result = ci.wrapper_main([
        "--kind", "gate", "--cmd-id", "heavy-gate", "--",
        sys.executable, "-c", "print('local')",
    ])

    assert result == expected_rc
    if fallback == "local":
        assert local_calls[0]["fallback_reason"] == "spool-unavailable"
        assert local_calls[0]["route"] == "local-fallback"
    else:
        receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert receipt["route"] == "remote-unavailable"
        assert receipt["fallback_reason"] == "spool-unavailable"

def test_node_defaults_managed_root_and_authority_under_user_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config_raw = _raw_config(tmp_path)
    config_raw["boxes"]["box-a"].pop("managed_run_directory")
    config_path = tmp_path / "remote-ci-default-root.json"
    config_path.write_text(json.dumps(config_raw), encoding="utf-8")
    config = ci.load_config(config_path)

    assert config.boxes["box-a"].managed_run_directory == Path(
        "~/.goal-flight/remote-ci-managed"
    )
    authority = tmp_path / ".goal-flight" / "remote-ci" / "authority.json"
    monkeypatch.setattr(ci_node, "AUTHORITY_FILE", authority)

    managed = ci_node.pin_managed_root(config.boxes["box-a"].managed_run_directory)

    assert managed == Path.home() / ".goal-flight" / "remote-ci-managed"
    assert ci_node.authority_file() == authority
    assert authority.is_file()

def test_remote_node_bootstrap_expands_default_root_before_helper_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = Path(ci_node.__file__).read_bytes()
    request = {
        "managed_root": "~/.goal-flight/remote-ci-managed",
        "helper_sha256": hashlib.sha256(source).hexdigest(),
        "helper_size_bytes": len(source),
    }
    request64 = base64.b64encode(json.dumps(request).encode()).decode()
    environment = {**os.environ, "HOME": str(home)}

    completed = subprocess.run(
        [sys.executable, "-c", ci._REMOTE_NODE_LAUNCHER, request64,
         "--install-helper-from-stdin"],
        cwd=tmp_path, env=environment, input=source, capture_output=True,
        check=False,
    )

    helper_dir = home / ".goal-flight/remote-ci-managed/helpers/remote-ci"
    assert list(helper_dir.glob("goalflight_remote_ci_node-*.py"))
    assert completed.returncode != 73, completed.stderr.decode(errors="replace")
    assert not (tmp_path / "~").exists()

def _command_request(
    request_id: str = "request-1", *, repo_root: Path | None = None,
    project_id: str = "default",
) -> dict:
    return {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": request_id,
        "project_id": project_id,
        "kind": "gate",
        "command_id": "heavy-gate",
        "command": [sys.executable, "-c", "print('remote')"],
        "repo_root": str(repo_root or Path(ci.__file__).resolve().parents[1]),
        "cwd_relative": "",
        "caller_env": {},
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": [],
        "selection": [],
        "route_reason": "heavy-command-allowlist",
        "purpose": "remote CI command",
    }


class _DaemonResidentNode:
    def __init__(
        self, config, *, cancelled=False, lose_submit_reply=False,
        lookup_unreachable=False, unreadable=False, running_status_polls=0,
        publish_ticket=True,
    ):
        self.box = config.boxes["box-a"]
        self.cancelled = cancelled
        self.lose_submit_reply = lose_submit_reply
        self.lookup_unreachable = lookup_unreachable
        self.unreadable = unreadable
        self.running_status_polls = running_status_polls
        self.publish_ticket = publish_ticket
        self.ticket_record = None
        self.pending_ticket_record = None
        self.calls = []
        self.receipt = json.dumps({
            "schema": ci.RECEIPT_SCHEMA, "hostname": "measured-node",
            "status": "pass", "returncode": 0, "passed": 1,
            "failed": 0, "errors": 0, "failing_node_ids": [],
            "collection_import_errors": [], "first_error_lines": [],
            "duration_seconds": 0.1,
        }).encode() + b"\n"

    def call(self, operation, **kwargs):
        self.calls.append(operation)
        if operation == "bundle-basis":
            return {"commit": None}
        if operation == "submit":
            bundle = Path(kwargs["candidate_bundle_path"])
            assert bundle.is_file()
            ticket_record = {
                "run_directory": f"/runs/{kwargs['request_id']}",
                "lease_token": f"lease-{kwargs['request_id']}",
                "lease_id": kwargs["request_id"], "ticket": 1,
                "request_id": kwargs["request_id"], "arm": kwargs["arm"],
                "resident_submission": True,
            }
            self.pending_ticket_record = ticket_record
            self.ticket_record = ticket_record if self.publish_ticket else None
            if self.lose_submit_reply:
                self.lose_submit_reply = False
                raise ci.RemoteCIError("simulated lost submit reply")
            return self.ticket_record
        if operation == "list":
            if self.lookup_unreachable:
                raise ci.RemoteCIError("simulated ticket lookup outage")
            if self.unreadable:
                return [{"state": "UNREADABLE", "run_directory": "/runs/unreadable"}]
            return [self.ticket_record] if self.ticket_record is not None else []
        if operation == "cancel":
            self.cancelled = True
            return {"status": "cancelled"}
        if operation == "status":
            if self.running_status_polls:
                self.running_status_polls -= 1
                return {"state": "running", "sample": {"hostname": "measured-node"}}
            result = (
                {"status": "cancelled", "returncode": 130}
                if self.cancelled else
                {"status": "completed", "returncode": 0,
                 "exit_known": True, "command_started": True,
                 "timed_out": False}
            )
            return {"sample": {"hostname": "measured-node", "self_cap": 1},
                    "result": result}
        if operation == "fetch":
            body = self.receipt if kwargs["stream"] == "stdout" else b""
            return {
                "status": "ok", "compression": "gzip",
                "body_gzip_base64": base64.b64encode(gzip.compress(body)).decode(),
                "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                "truncated": False, "omitted_bytes": 0,
            }
        raise AssertionError(f"unexpected node operation: {operation}")

def test_project_request_is_served_only_by_its_own_daemon(tmp_path, monkeypatch):
    configs = {}
    for project_id in ("project-a", "project-b"):
        root = tmp_path / project_id
        root.mkdir()
        raw = _raw_config(root, token_pool_size=2)
        raw["repo"] = project_id
        raw["boxes"]["box-a"]["host"] = project_id
        path = root / "remote-ci.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        configs[project_id] = _load_fake_node_config(path)

    payload = _command_request("isolation", project_id="project-a")
    request_path = configs["project-a"].queue_dir / "isolation.json"
    request_path.parent.mkdir(parents=True)
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    observed = []

    def run_remote(request, runner):
        observed.append((request["project_id"], runner.config.repo))
        return {"schema": ci.RESULT_SCHEMA, "status": "GREEN", "candidate": {"returncode": 0}}

    monkeypatch.setattr(ci.GateDaemon, "_snapshot_command_request", lambda _self, request: (request, None))
    monkeypatch.setattr(ci, "run_remote_command", run_remote)

    result_a = ci.GateDaemon(configs["project-a"])._process_request(request_path)
    result_b = ci.GateDaemon(configs["project-b"])._process_request(request_path)

    assert result_a["status"] == "GREEN"
    assert result_a["project_id"] == "project-a"
    assert result_a["policy_outcome"] == "ran_remote"
    assert result_b["status"] == "PROJECT_MISMATCH"
    assert result_b["project_id"] == "project-a"
    assert observed == [("project-a", "project-a")]

def test_request_without_project_id_is_refused_without_borrowing_daemon_identity(
    tmp_path, monkeypatch,
):
    root = tmp_path / "project-b"
    root.mkdir()
    raw = _raw_config(root, token_pool_size=1)
    raw["repo"] = "project-b"
    path = root / "remote-ci.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    payload = _command_request("missing-identity", repo_root=root)
    payload.pop("project_id")
    request_path = config.queue_dir / "missing-identity.json"
    request_path.parent.mkdir(parents=True)
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        ci.GateDaemon, "_snapshot_command_request",
        lambda _self, _request: pytest.fail("unidentified request was snapshotted"),
    )
    monkeypatch.setattr(
        ci, "run_remote_command",
        lambda *_args: pytest.fail("unidentified request was run remotely"),
    )

    result = ci.GateDaemon(config)._process_request(request_path)

    assert result["status"] == "PROJECT_MISMATCH"
    assert result["project_id"] is None
    assert result["fallback_reason"] == "missing-project-id"

def test_profile_pytest_workers_cap_rewrites_commands_and_selections(tmp_path):
    root = tmp_path / "example-sim"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_cap.py").write_text("def test_cap(): pass\n", encoding="utf-8")
    raw = _profile_raw_config(root)
    raw["repo"] = "example-sim"
    raw["fallback"] = "queue"
    raw["boxes"]["box-a"]["max_concurrent_seats"] = 1
    raw["runner"]["pytest_workers_cap"] = 4
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    path = root / "remote-ci.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)

    command_request = _command_request("cap-command", repo_root=root, project_id="example-sim")
    command_request["command"] = [
        sys.executable, "-m", "pytest", "-n", "auto", "tests/test_cap.py",
    ]
    normalized_command = ci.validate_request(command_request, config)

    selection_request = {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "cap-selection",
        "project_id": "example-sim",
        "kind": "targeted",
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tests/test_cap.py"],
        "selection": ["-n", "12", "tests/test_cap.py"],
    }
    normalized_selection = ci.validate_request(selection_request, config)

    assert config.runner.command[-2:] == ("-n", "4")
    assert config.runner.test_command[-2:] == ("-n", "4")
    assert normalized_command["command"][-3:-1] == ["-n", "4"]
    assert normalized_selection["selection"] == ["-n", "4", "tests/test_cap.py"]

def test_profile_pytest_workers_cap_preserves_serial_execution(tmp_path):
    root = tmp_path / "example-sim"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_cap.py").write_text("def test_cap(): pass\n", encoding="utf-8")
    raw = _profile_raw_config(root)
    raw["runner"]["pytest_workers_cap"] = 4
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    path = root / "remote-ci.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    request = _command_request("serial-cap", repo_root=root)
    request["command"] = [sys.executable, "-m", "pytest", "-n", "0", "tests/test_cap.py"]

    normalized = ci.validate_request(request, config)

    assert normalized["command"][-3:] == ["-n", "0", "tests/test_cap.py"]

@pytest.mark.parametrize("worker_args", [
    ["-n16"], ["-n=16"], ["--numprocesses=8"],
    ["-n", "2", "tests/test_cap.py", "-n", "16"],
    ["-n", "2", "-n", "16", "tests/test_cap.py"],
])
def test_profile_worker_cap_rejects_unsupported_or_duplicate_worker_options(
    tmp_path, worker_args,
):
    root = tmp_path / "example-sim"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_cap.py").write_text("def test_cap(): pass\n", encoding="utf-8")
    raw = _profile_raw_config(root)
    raw["runner"]["pytest_workers_cap"] = 4
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    path = root / "remote-ci.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    request = _command_request("invalid-worker-option", repo_root=root)
    request["command"] = [sys.executable, "-m", "pytest", *worker_args]

    with pytest.raises(ci.ProfileIneligible) as caught:
        ci.validate_request(request, config)
    assert caught.value.fallback_reason == "command-outside-profile-grammar"

def test_node_enforces_project_seat_cap_while_sharing_box_tokens(tmp_path):
    config = _config(tmp_path, token_pool_size=2)
    executor = ScriptedExecutor(tmp_path / "fake-node")
    runner = RemoteRunner(config, executor=executor)
    node = runner.nodes["box-a"]
    try:
        first = node.call(
            "enqueue", request_id="project-a-1", arm="candidate",
            owner=RemoteRunner._owner(), repo="project-a",
            max_concurrent_seats=1, queue_wait_seconds=2,
        )
        held = wait_state(node, first, {"admitted"})
        start(node, held, "import time; time.sleep(2)")

        second = node.call(
            "enqueue", request_id="project-a-2", arm="candidate",
            owner=RemoteRunner._owner(), repo="project-a",
            max_concurrent_seats=1, queue_wait_seconds=0.1,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            refused = node.call("status", **key(second))
            if refused.get("result"):
                break
            time.sleep(0.01)
        assert refused["result"]["status"] == "capacity-refused"
        assert refused["result"]["queue_waited"] is True

        other_project = node.call(
            "enqueue", request_id="project-b-1", arm="candidate",
            owner=RemoteRunner._owner(), repo="project-b",
            max_concurrent_seats=1, queue_wait_seconds=2,
        )
        other_held = wait_state(node, other_project, {"admitted"})
        assert Path(other_held["slot"]).parts[-3:-1] == ("project-b", "seats")
    finally:
        executor.close(config)

def test_saturated_project_ticket_does_not_block_another_project(tmp_path):
    config = _config(tmp_path, token_pool_size=2)
    executor = ScriptedExecutor(tmp_path / "fake-node")
    runner = RemoteRunner(config, executor=executor)
    node = runner.nodes["box-a"]
    try:
        first = node.call(
            "enqueue", request_id="project-a-active", arm="candidate",
            owner=RemoteRunner._owner(), repo="project-a",
            max_concurrent_seats=1, queue_wait_seconds=2,
        )
        first_held = wait_state(node, first, {"admitted"})
        start(node, first_held, "import time; time.sleep(5)")

        blocked = node.call(
            "enqueue", request_id="project-a-waiting", arm="candidate",
            owner=RemoteRunner._owner(), repo="project-a",
            max_concurrent_seats=1, queue_wait_seconds=10,
        )
        other_project = node.call(
            "enqueue", request_id="project-b-free-seat", arm="candidate",
            owner=RemoteRunner._owner(), repo="project-b",
            max_concurrent_seats=1, queue_wait_seconds=10,
        )

        deadline = time.monotonic() + 0.5
        other_held = None
        while time.monotonic() < deadline:
            state = node.call("status", **key(other_project))
            if state.get("state") == "admitted":
                other_held = state
                break
            time.sleep(0.01)
        assert other_held is not None
        assert node.call("status", **key(blocked)).get("state") == "queued"
        start(node, other_held, "pass")
    finally:
        executor.close(config)

def test_command_request_accepts_base_sha_for_pair_mode(tmp_path):
    config = _config(tmp_path)
    request = _command_request()
    request["base_sha"] = "a" * 40

    normalized = ci.validate_request(request, config)
    assert normalized["tip_sha"] == "a" * 40
    assert normalized["base_sha"] == "a" * 40

def test_command_request_refuses_more_than_five_rechecks(tmp_path):
    config = _config(tmp_path)
    request = _command_request()
    request.update({"base_sha": "a" * 40, "recheck": 6})

    with pytest.raises(ci.RemoteCIError, match="at most 5"):
        ci.validate_request(request, config)


@pytest.mark.parametrize(("base_recheck", "candidate_recheck", "expected"), [
    ((False, False), (True, True), "regression"),
    ((False, True), (True, False), "flake"),
    ((True, True), (True, True), "base-failing"),
])
def test_matched_pair_command_rechecks_new_failures_on_same_box(
    tmp_path, base_recheck, candidate_recheck, expected,
):
    config = _two_box_config(tmp_path)

    class PairRecheckRunner(SuccessfulCommandRunner):
        def run_arm(self, arm_spec, *, admission_deadline=None):
            self.calls.append(arm_spec)
            index = len(self.calls)
            if index == 1:
                failures = ("fixed-node", "shared-node")
            elif index == 2:
                failures = ("new-node", "shared-node")
            else:
                repetition = (index - 3) // 2
                failed = (
                    base_recheck[repetition]
                    if arm_spec.arm == "base"
                    else candidate_recheck[repetition]
                )
                failures = ("new-node",) if failed else ()
            passed = 4 - len(failures)
            receipt = ci.RemoteReceipt(
                hostname="measured-node", status="fail" if failures else "pass",
                returncode=1 if failures else 0, passed=passed,
                failed=len(failures), errors=0, failing_node_ids=failures,
                collection_import_errors=(), first_error_lines=(),
                duration_seconds=0.1,
                metadata={"box_id": arm_spec.box},
            )
            return ci.ArmOutcome(
                arm_spec.arm, "red" if failures else "green",
                1 if failures else 0, receipt,
            )

    runner = PairRecheckRunner(config)
    runner.choose_box = lambda requested=None, **_kwargs: "studio-two"
    result = ci.run_matched_pair({
        "request_id": "pair-command-recheck", "kind": "gate", "box": None,
        "tip_sha": "a" * 40, "base_sha": "a" * 40,
        "candidate_sha": "b" * 40, "head_sha": "b" * 40,
        "snapshot_sha": "b" * 40, "dirty": False,
        "test_files": ["tests/test_one.py"], "selection": [],
        "repo_root": str(tmp_path), "cwd_relative": "",
        "command": ["python3", "-m", "pytest", "tests/test_one.py"],
        "command_id": "heavy-gate", "recheck": 2,
    }, runner)

    assert result["new_failure_node_ids"] == ["new-node"]
    assert result["fixed_failure_node_ids"] == ["fixed-node"]
    assert result["rechecks"]["new-node"]["classification"] == expected
    assert [call.arm for call in runner.calls] == ["base", "candidate"] * 3
    assert {call.box for call in runner.calls} == {"studio-two"}
    assert all(runner.calls[index].overlay is False for index in (0, 2, 4))
    assert all(runner.calls[index].overlay is False for index in (1, 3, 5))
    assert all(call.command[:4] == ("python3", "-m", "pytest", "tests/test_one.py")
               for call in runner.calls[:2])
    assert all(call.command == ("python3", "-m", "pytest", "new-node")
               for call in runner.calls[2:])

def test_pair_rechecks_only_requested_node_when_other_base_test_fails(tmp_path):
    config = _two_box_config(tmp_path)

    class NodeOnlyRunner(SuccessfulCommandRunner):
        def run_arm(self, arm_spec, *, admission_deadline=None):
            self.calls.append(arm_spec)
            if len(self.calls) == 1:
                failures = ("tests/test_b.py::test_b",)
            elif len(self.calls) == 2:
                failures = (
                    "tests/test_a.py::test_a", "tests/test_b.py::test_b",
                )
            elif arm_spec.arm == "base":
                failures = ()
            else:
                failures = ("tests/test_a.py::test_a",)
            receipt = ci.RemoteReceipt(
                hostname="measured-node", status="fail" if failures else "pass",
                returncode=1 if failures else 0, passed=0 if failures else 1,
                failed=len(failures), errors=0, failing_node_ids=failures,
                collection_import_errors=(), first_error_lines=(),
                metadata={"box_id": arm_spec.box},
            )
            return ci.ArmOutcome(
                arm_spec.arm, "red" if failures else "green",
                1 if failures else 0, receipt,
            )

    runner = NodeOnlyRunner(config)
    result = ci.run_matched_pair({
        "request_id": "node-only-recheck", "kind": "gate", "box": "studio-two",
        "tip_sha": "a" * 40, "base_sha": "a" * 40,
        "candidate_sha": "b" * 40, "head_sha": "b" * 40,
        "snapshot_sha": "b" * 40, "dirty": False,
        "test_files": ["tests/test_a.py", "tests/test_b.py"], "selection": [],
        "repo_root": str(tmp_path),
        "cwd_relative": "",
        "command": [
            "python3", "-m", "pytest", "tests/test_a.py", "tests/test_b.py",
        ],
        "command_id": "heavy-gate", "recheck": 1,
    }, runner)

    assert result["rechecks"]["tests/test_a.py::test_a"]["classification"] == "regression"
    assert runner.calls[2].command == (
        "python3", "-m", "pytest", "tests/test_a.py::test_a",
    )
    assert runner.calls[3].command == runner.calls[2].command

def test_pair_reports_candidate_only_test_missing_at_exact_base(tmp_path):
    config = _two_box_config(tmp_path)

    class AbsentAtBaseRunner(SuccessfulCommandRunner):
        def run_arm(self, arm_spec, *, admission_deadline=None):
            self.calls.append(arm_spec)
            base = arm_spec.arm == "base"
            if base:
                status, code, passed, failed, ids = "red", 4, 0, 0, ()
            else:
                status, code, passed, failed, ids = (
                    "red", 1, 0, 1, ("tests/test_candidate_only.py::test_new",)
                )
            receipt = ci.RemoteReceipt(
                hostname="measured-node", status="fail", returncode=code,
                passed=passed, failed=failed, errors=0, failing_node_ids=ids,
                collection_import_errors=(), first_error_lines=(),
                metadata={"box_id": arm_spec.box},
            )
            return ci.ArmOutcome(arm_spec.arm, status, code, receipt)

    runner = AbsentAtBaseRunner(config)
    result = ci.run_matched_pair({
        "request_id": "absent-at-base", "kind": "gate", "box": "studio-two",
        "tip_sha": "a" * 40, "base_sha": "a" * 40,
        "candidate_sha": "b" * 40, "head_sha": "b" * 40,
        "snapshot_sha": "b" * 40, "dirty": False,
        "test_files": ["tests/test_candidate_only.py"], "selection": [],
        "repo_root": str(tmp_path), "cwd_relative": "",
        "command": ["python3", "-m", "pytest", "tests/test_candidate_only.py"],
        "command_id": "heavy-gate", "recheck": 1,
    }, runner)

    assert runner.calls[0].overlay is False
    assert runner.calls[2].command == (
        "python3", "-m", "pytest", "tests/test_candidate_only.py::test_new",
    )
    node_id = "tests/test_candidate_only.py::test_new"
    assert result["rechecks"][node_id]["classification"] == "absent-at-base"
    assert result["absent_at_base_node_ids"] == [node_id]
    assert result["new_failure_node_ids"] == []
    assert result["status"] == "GREEN"

class SuccessfulCommandRunner:
    def __init__(self, config: ci.DaemonConfig):
        self.config = config
        self.calls = []

    def choose_box(self, requested=None, **_kwargs):
        return requested or "box-a"

    def run_arm(self, spec, *, admission_deadline=None):
        self.calls.append(spec)
        receipt = ci.RemoteReceipt(
            hostname="measured-node", status="pass", returncode=0,
            passed=1, failed=0, errors=0, failing_node_ids=(),
            collection_import_errors=(), first_error_lines=(), duration_seconds=0.2,
            metadata={
                "box_id": spec.box,
                "expected_hostname": "measured-node",
                "measured_hostname": "measured-node",
                "tip_sha": spec.tip_sha,
                "candidate_sha": spec.candidate_sha,
                "command_id": spec.command_id,
                "process_cpu_seconds": 0.1,
                "wall_seconds": 0.2,
                "load_average_start": [1.0, 0.0, 0.0],
                "load_average_end": [1.0, 0.0, 0.0],
                "route": "studio",
                "route_reason": spec.route_reason,
                "fallback_reason": None,
                "requirements_sha256": "c" * 64,
                "venv_path": "/var/lib/goalflight/remote-ci/venvs/default",
            },
        )
        return ci.ArmOutcome(spec.arm, "green", 0, receipt)

def test_spool_submit_and_daemon_results_are_atomic_for_unique_requests(tmp_path):
    config = _config(tmp_path)
    assert config.environment == {"requirements_file": "requirements-test.txt"}
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "suite.py").write_text("print('suite')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "source")
    runner = SuccessfulCommandRunner(config)
    daemon = ci.GateDaemon(config, runner=runner)
    source = tmp_path / "request.json"
    source.write_text(json.dumps(_command_request(repo_root=repo)), encoding="utf-8")

    queued = ci.submit_request(source, config)

    assert queued.is_file()
    assert not list(config.queue_dir.glob("*.tmp"))
    result = daemon.poll_once()
    assert result["status"] == "GREEN"
    result_path = config.result_dir / "request-1.json"
    assert json.loads(result_path.read_text())["candidate"]["receipt"]["route"] == "studio"
    assert not queued.exists()
    assert not list(config.result_dir.glob("*.tmp"))

    # New work has a new id; it gets its own result instead of reusing old state.
    request = _command_request("request-2", repo_root=repo)
    source.write_text(json.dumps(request), encoding="utf-8")
    ci.submit_request(source, config)
    second = daemon.poll_once()
    assert second["status"] == "GREEN"
    assert second["request_id"] == "request-2"
    assert (config.result_dir / "request-2.json").is_file()
    assert len(runner.calls) == 2

def test_daemon_ignores_local_fallback_receipts_in_request_spool(tmp_path):
    config = _config(tmp_path)
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    receipt = config.queue_dir / "request-1.local.json"
    receipt.write_text(json.dumps({"request_id": "request-1", "status": "LOCAL_FALLBACK"}))

    assert ci.GateDaemon(config).pending_paths() == []

def _two_box_config(tmp_path: Path, *, first_hostname: str = "MEASURED-NODE") -> ci.DaemonConfig:
    raw = _raw_config(tmp_path, token_pool_size=1)
    first = raw["boxes"].pop("box-a")
    first.update({"p_cores": 10, "expected_hostname": first_hostname})
    second = dict(first)
    second.update({
        "host": "studio-two",
        "managed_run_directory": str(tmp_path / "fake-node-two"),
        "p_cores": 20,
        "expected_hostname": "MEASURED-NODE.local",
    })
    raw["boxes"] = {"studio-one": first, "studio-two": second}
    path = tmp_path / "two-boxes.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return _load_fake_node_config(path)

def test_load_rank_chooses_the_box_with_more_headroom(tmp_path, monkeypatch):
    config = _two_box_config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_: None)
    samples = {
        "studio-one": {"load": {"hostname": "measured-node", "load1": 6.0,
                                  "cpu_busy_fraction": 0.5},
                       "tokens": {"free": 1}},
        "studio-two": {"load": {"hostname": "measured-node", "load1": 1.0,
                                  "cpu_busy_fraction": 0.1},
                       "tokens": {"free": 1}},
    }
    for name, node in runner.nodes.items():
        monkeypatch.setattr(node, "call", lambda operation, _name=name, **kwargs:
                            samples[_name] if operation == "health" else {})

    assert runner.choose_box() == "studio-two"


def test_health_probes_run_concurrently_with_fixed_timeout_and_place_slow_healthy_box(
    tmp_path, monkeypatch,
):
    config = _two_box_config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_: None)
    calls = []
    second_started = threading.Event()

    for name, node in runner.nodes.items():
        def health(operation, *, _name=name, _transport_timeout=None, **_values):
            assert operation == "health"
            calls.append((_name, _transport_timeout))
            if _name == "studio-one":
                # Model a healthy response that needs 3.1s of transport budget,
                # beyond the old serial probe slice, without sleeping in the test.
                if _transport_timeout <= 3.1 or not second_started.wait(timeout=1):
                    raise ci.RemoteTransportTimeoutError("simulated slow probe")
                load1, busy = 0.1, 0.05
            else:
                second_started.set()
                load1, busy = 5.0, 0.4
            return {
                "load": {"hostname": "measured-node", "load1": load1,
                         "cpu_busy_fraction": busy},
                "tokens": {"free": 1},
            }

        node.call = health

    assert runner.choose_box(deadline=time.monotonic() + 30) == "studio-one"
    assert {name for name, _timeout in calls} == {"studio-one", "studio-two"}
    assert {timeout for _name, timeout in calls} == {
        ci.REMOTE_HEALTH_PROBE_TIMEOUT_SECONDS,
    }
    assert ci.REMOTE_HEALTH_PROBE_TIMEOUT_SECONDS > 3.1


@pytest.mark.parametrize(
    ("stderr", "expected_attempts"),
    [
        ("Read from remote host studio-1.example-tailnet.ts.net: Connection reset by peer", 3),
        ("Connection to studio.example closed by remote host", 3),
        ("Connection to studio.example port 22 timed out", 3),
        ("ssh: connect to host studio.example port 22: Connection refused", 3),
        ("Connection timed out", 3),
        ("client_loop: send disconnect: Broken pipe", 3),
        ("studio.example: Permission denied (publickey).", 1),
        ("No ED25519 host key is known for studio.example and strict checking is enabled.\nHost key verification failed.", 1),
        ("ssh: Could not resolve hostname studio.example: Name or service not known", 1),
    ],
)
def test_health_probe_retries_only_transient_transport_refusals(
    tmp_path, stderr, expected_attempts,
):
    config = _config(tmp_path)
    attempts = []
    backoffs = []

    def executor(argv, _env, _timeout, **_kwargs):
        attempts.append(argv)
        return CommandResult(255, "", stderr)

    runner = RemoteRunner(config, executor=executor, sleeper=backoffs.append)

    with pytest.raises(ci.RemoteCIError, match="could not probe") as raised:
        runner.choose_box(deadline=time.monotonic() + 30)

    assert "unknown" in str(raised.value).lower()
    assert stderr in str(raised.value)
    assert len(attempts) == expected_attempts
    assert len(backoffs) == expected_attempts - 1
    if backoffs:
        assert all(backoff > 0 for backoff in backoffs)


def test_no_healthy_box_reports_each_box_skip_cause(tmp_path, monkeypatch):
    raw = _raw_config(tmp_path)
    template = raw["boxes"]["box-a"]
    names = (
        "hostname-box", "saturated-box", "bad-load-box",
        "missing-paths-box",
    )
    raw["boxes"] = {}
    for name in names:
        box = dict(template)
        box.update(
            host=f"{name}.example.invalid",
            expected_hostname=f"measured-{name}",
            managed_run_directory=str(tmp_path / name),
        )
        raw["boxes"][name] = box
    path = tmp_path / "four-boxes.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    runner = RemoteRunner(config, executor=lambda *_: None)

    def health(node, operation, **kwargs):
        assert operation == "health"
        assert kwargs["_transport_timeout"] == ci.REMOTE_HEALTH_PROBE_TIMEOUT_SECONDS
        name = node.box.name
        if name == "missing-paths-box":
            return {"missing_required_paths": ["/configured/input"]}
        hostname = "wrong-host" if name == "hostname-box" else f"measured-{name}"
        if name == "saturated-box":
            load1, busy = 41.0, 0.96
        elif name == "bad-load-box":
            load1, busy = float("nan"), 0.1
        else:
            load1, busy = 1.0, 0.1
        return {
            "load": {"hostname": hostname, "load1": load1,
                     "cpu_busy_fraction": busy},
            "tokens": {"free": 1},
        }

    for node in runner.nodes.values():
        monkeypatch.setattr(node, "call", lambda operation, _node=node, **kwargs:
                            health(_node, operation, **kwargs))

    with pytest.raises(ci.NoHealthyBox, match="no healthy") as raised:
        runner.choose_box()
    reason = str(raised.value)
    for expected in (
        "hostname-box: hostname-mismatch",
        "saturated-box: saturated", "bad-load-box: bad-load",
        "missing-paths-box: missing-paths",
    ):
        assert expected in reason
    assert "load1=41" in reason and "busy=0.960" in reason
    assert "/configured/input" in reason


def test_choose_box_classifies_node_reported_unknown_health(node_env, monkeypatch):
    _config_value, _executor, runner, node = node_env
    monkeypatch.setattr(node, "call", lambda *_args, **_kwargs: {
        "status": "unknown", "reason": "admission-lock-timeout",
    })

    with pytest.raises(ci.HealthProbeUnknown):
        runner.choose_box()


def test_blocking_placement_retries_an_unknown_box(tmp_path):
    attempts = []

    class RetryRunner:
        config = SimpleNamespace(
            boxes={"box-a": object(), "box-b": object()}, queue_dir=tmp_path,
        )

        def run_arm(self, arm_spec, *, admission_deadline):
            attempts.append(arm_spec.box)
            if arm_spec.box == "box-a":
                raise ci.HealthProbeUnknown("box-a health is unknown")
            return ci.ArmOutcome("candidate", "green", 0, None)

        def choose_box(self, *, excluding, deadline):
            assert tuple(excluding) == ("box-a",)
            return "box-b"

    outcome = ci._run_arm_with_placement_retries(
        {"request_id": "unknown-then-healthy", "box": "box-a"},
        RetryRunner(), spec("unknown-then-healthy"), deadline=time.monotonic() + 5,
    )

    assert outcome.status == "green"
    assert attempts == ["box-a", "box-b"]


def test_blocking_placement_preserves_unknown_when_every_box_is_unknown(
    node_env, monkeypatch,
):
    _config_value, _executor, runner, node = node_env
    monkeypatch.setattr(node, "call", lambda *_args, **_kwargs: {
        "status": "unknown", "reason": "admission-lock-timeout",
    })
    monkeypatch.setattr(
        runner, "run_arm",
        lambda arm_spec, *, admission_deadline: runner.choose_box(
            requested=arm_spec.box, deadline=admission_deadline,
        ),
    )

    with pytest.raises(ci.HealthProbeUnknown):
        ci._run_arm_with_placement_retries(
            {"request_id": "all-health-unknown", "box": "box-a"},
            runner, spec("all-health-unknown"), deadline=time.monotonic() + 5,
        )


def test_helper_hash_mismatch_keeps_source_out_of_remote_command(node_env):
    _config_value, _scripted, _runner, node = node_env
    sources = [b"a" * 120_000, b"b" * 240_000]
    helper_commands = []
    helper_stdin = []

    def executor(argv, _env, _timeout, **kwargs):
        command = argv[2]
        shell = shlex.split(command)
        payload = json.loads(base64.b64decode(shell[3]))
        if "input_data" in kwargs or len(shell) > 4:
            helper_commands.append(command)
            helper_stdin.append(kwargs.get("input_data"))
            return CommandResult(0, json.dumps({
                "protocol_version": ci.NODE_PROTOCOL_VERSION,
                "result": {"health": "ok"},
            }))
        return CommandResult(73, json.dumps({
            "bootstrap": "install-required",
            "expected_sha256": payload["helper_sha256"],
            "observed_sha256": None,
        }))

    node.executor = executor
    for source in sources:
        node.helper_source = source
        assert node.call("health") == {"health": "ok"}

    command_sizes = [len(command.encode()) for command in helper_commands]
    assert len(command_sizes) == 2
    assert max(command_sizes) < 10_000, command_sizes
    assert command_sizes[0] == command_sizes[1], command_sizes
    assert helper_stdin == sources


def test_health_unknown_uses_distinct_daemon_fallback(tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path, repo_id="source")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["admission"]["max_wait_seconds"] = 2
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "suite.py").write_text("print('suite')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "source")
    _declare_repo_identity(repo, "source")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("submitted health-unknown request ran locally"),
    )
    submit = ci.submit_request
    processed = []

    class UnknownHealthRunner:
        def __init__(self, config):
            self.config = config

        def choose_box(self, _requested=None, **_kwargs):
            raise ci.HealthProbeUnknown("could not probe Studio health; placement is unknown")

    def submit_and_process(source, config):
        queued = submit(source, config)
        processed.append(ci.GateDaemon(
            config, runner=UnknownHealthRunner(config),
        ).poll_once())
        return queued

    monkeypatch.setattr(ci, "submit_request", submit_and_process)

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert processed[0]["fallback_reason"] == "health-unknown"
    assert "could not probe" in processed[0]["error"]
    assert "could not probe" in receipt["error"]
    assert receipt["fallback_reason"] == "health-unknown"


def test_wrapper_keeps_submitting_when_live_daemon_pid_file_disappears(
    tmp_path, monkeypatch,
):
    config_path, spool = _wrapper_config(tmp_path, repo_id="source")
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "suite.py").write_text("print('suite')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "source")
    _declare_repo_identity(repo, "source")
    monkeypatch.chdir(repo)
    config = ci.load_config(config_path)
    assert config.pid_file is not None
    submitted = {}
    submit = ci.submit_request

    def submit_and_remove_pid(source, loaded_config):
        queued = submit(source, loaded_config)
        submitted["request_id"] = queued.stem
        loaded_config.pid_file.unlink()
        assert ci.daemon_is_running(loaded_config) is True
        return queued

    def publish_result(_seconds):
        if "request_id" not in submitted:
            return
        request_id = submitted["request_id"]
        (config.queue_dir / f"{request_id}.json").unlink(missing_ok=True)
        ci._atomic_write_json(
            config.result_dir / f"{request_id}.json",
            _green_wrapper_result(request_id),
        )

    monkeypatch.setattr(ci, "submit_request", submit_and_remove_pid)
    monkeypatch.setattr(ci.time, "sleep", publish_result)

    with ci.DaemonLock(config.lock_file):
        ci._atomic_write_json(config.pid_file, {"pid": os.getpid()})
        assert ci.daemon_is_running(config) is True
        assert _run_wrapper(monkeypatch, config_path, spool) == 0

    assert submitted
    assert not list((spool / "requests").glob("*.local.json"))


def test_wrapper_reports_unknown_when_pid_and_daemon_lock_are_absent(
    tmp_path, monkeypatch, capsys,
):
    config_path, spool = _wrapper_config(tmp_path, repo_id="source")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["fallback"] = "fail"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "suite.py").write_text("print('suite')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "source")
    _declare_repo_identity(repo, "source")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ci, "DAEMON_REATTACH_WINDOW_SECONDS", 0)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("unmeasured daemon ran locally"),
    )
    config = ci.load_config(config_path)
    assert config.pid_file is not None
    assert not config.pid_file.exists()
    assert not config.lock_file.exists()
    assert ci.daemon_is_running(config) is None

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["fallback_reason"] == "daemon-liveness-unknown"
    assert "could not verify daemon liveness" in receipt["error"]


def test_box_placement_skips_boxes_full_on_both_signals(tmp_path, monkeypatch):
    config = _two_box_config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_: None)
    samples = {
        "studio-one": {"load": {"hostname": "measured-node", "load1": 20,
                                  "cpu_busy_fraction": 0.95},
                       "tokens": {"free": 1}},
        "studio-two": {"load": {"hostname": "measured-node", "load1": 27,
                                  "cpu_busy_fraction": 0.1},
                       "tokens": {"free": 1}},
    }
    for name, node in runner.nodes.items():
        monkeypatch.setattr(node, "call", lambda operation, _name=name, **kwargs:
                            samples[_name] if operation == "health" else {})

    assert runner.choose_box() == "studio-two"


@pytest.mark.parametrize(
    ("load1", "busy", "saturated"),
    [
        (55.999, 0.95, False),
        (56, 0.949, False),
        (56, 0.95, True),
    ],
)
def test_client_saturation_threshold_boundaries(tmp_path, monkeypatch, load1, busy, saturated):
    config = _two_box_config(tmp_path)
    box = replace(config.boxes["studio-one"], p_cores=28)
    config = replace(config, boxes={**config.boxes, "studio-one": box})
    runner = RemoteRunner(config, executor=lambda *_: None)
    sample = {
        "load": {"hostname": "measured-node", "load1": load1,
                 "cpu_busy_fraction": busy},
        "tokens": {"free": 1},
    }
    monkeypatch.setattr(runner.nodes["studio-one"], "call",
                        lambda operation, **kwargs: sample)
    if saturated:
        with pytest.raises(ci.NoHealthyBox):
            runner.choose_box(requested="studio-one")
    else:
        assert runner.choose_box(requested="studio-one") == "studio-one"


def test_client_placement_score_scales_load_by_saturation_factor(tmp_path, monkeypatch):
    config = _two_box_config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_: None)
    samples = {
        "studio-one": {"load": {"hostname": "measured-node", "load1": 15,
                                  "cpu_busy_fraction": 0.1}, "tokens": {"free": 1}},
        "studio-two": {"load": {"hostname": "measured-node", "load1": 5,
                                  "cpu_busy_fraction": 0.8}, "tokens": {"free": 1}},
    }
    for name, node in runner.nodes.items():
        monkeypatch.setattr(node, "call", lambda operation, _name=name, **kwargs:
                            samples[_name])

    assert runner.choose_box() == "studio-one"

def test_node_admits_high_load_when_cpu_has_headroom(node_env):
    _, executor, _, node = node_env
    executor.load1 = node.box.p_cores
    executor.cpu_busy_fraction = 0.2
    queued = enqueue(node, "load-high-cpu-headroom")

    admitted = wait_state(node, queued, {"admitted"})
    assert admitted["sample"]["load1"] == node.box.p_cores

def test_hostname_mismatch_is_skipped_and_local_suffix_is_normalized(tmp_path, monkeypatch):
    config = _two_box_config(tmp_path, first_hostname="wrong-host")
    runner = RemoteRunner(config, executor=lambda *_: None)
    for name, node in runner.nodes.items():
        sample = {
            "load": {"hostname": "measured-node", "load1": 2.0,
                     "cpu_busy_fraction": 0.2},
            "tokens": {"free": 1},
        }
        monkeypatch.setattr(node, "call", lambda operation, **kwargs: sample)

    assert runner.choose_box() == "studio-two"

def test_persisted_box_is_rechecked_against_its_expected_hostname(tmp_path, monkeypatch):
    config = _config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_: None)
    node = runner.nodes["box-a"]

    def wrong_host(operation, **kwargs):
        assert operation == "health"
        return {
            "load": {"hostname": "wrong-studio", "load1": 0.1,
                     "cpu_busy_fraction": 0.1},
            "tokens": {"free": 1},
        }

    monkeypatch.setattr(node, "call", wrong_host)
    with pytest.raises(ci.NoHealthyBox, match="hostname-mismatch"):
        runner.run_arm(spec())

def test_missing_expected_hostname_is_a_configuration_error(tmp_path):
    raw = _raw_config(tmp_path)
    del raw["boxes"]["box-a"]["expected_hostname"]
    raw["boxes"]["box-a"]["managed_run_directory"] = str(
        Path.home() / "goalflight-remote-ci-test-node"
    )
    path = tmp_path / "missing-host.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ci.ConfigError, match="expected_hostname"):
        ci.load_config(path)

def test_load_rank_prefers_free_token_over_lower_load_busy_box(tmp_path, monkeypatch):
    config = _two_box_config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_: None)
    samples = {
        "studio-one": {"load": {"hostname": "measured-node", "load1": 1.0,
                                  "cpu_busy_fraction": 0.1},
                       "tokens": {"free": 0}},
        "studio-two": {"load": {"hostname": "measured-node", "load1": 6.0,
                                  "cpu_busy_fraction": 0.5},
                       "tokens": {"free": 1}},
    }
    for name, node in runner.nodes.items():
        monkeypatch.setattr(node, "call", lambda operation, _name=name, **kwargs:
                            samples[_name] if operation == "health" else {})

    assert runner.choose_box() == "studio-two"

def test_matched_arms_are_pinned_to_the_selected_box(tmp_path):
    config = _two_box_config(tmp_path)

    class PairRunner(SuccessfulCommandRunner):
        def choose_box(self, requested=None, **_kwargs):
            return "studio-two"

    runner = PairRunner(config)
    request = {
        "request_id": "pair-1", "kind": "gate", "box": None,
        "tip_sha": "a" * 40, "candidate_sha": "b" * 40,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py", "-q"],
    }

    ci.run_matched_pair(request, runner)

    assert [spec.arm for spec in runner.calls] == ["base", "candidate"]
    assert {spec.box for spec in runner.calls} == {"studio-two"}

def test_daemon_falls_back_after_ambiguous_arm_without_retrying_it(tmp_path):
    config = _two_box_config(tmp_path)
    request = {
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "pair-restart",
        "project_id": config.repo,
        "kind": "gate",
        "box": None,
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py", "-q"],
    }
    source = tmp_path / "pair-request.json"
    source.write_text(json.dumps(request), encoding="utf-8")
    queued = ci.submit_request(source, config)

    class RestartRunner(SuccessfulCommandRunner):
        def __init__(self, daemon_config):
            super().__init__(daemon_config)
            self.selection_calls = 0
            self.started = set()
            self.outcomes = {}
            self.lose_candidate_reply = True
            self.successful = SuccessfulCommandRunner(daemon_config)

        def choose_box(self, requested=None, **_kwargs):
            self.selection_calls += 1
            assert requested is None
            return "studio-two"

        def run_arm(self, arm_spec, *, admission_deadline=None):
            key = (arm_spec.request_id, arm_spec.arm)
            self.calls.append(arm_spec)
            self.started.add(key)
            if arm_spec.arm == "candidate" and self.lose_candidate_reply:
                self.lose_candidate_reply = False
                raise ci.AmbiguousRemoteError("candidate start response was lost")
            if key not in self.outcomes:
                self.outcomes[key] = self.successful.run_arm(
                    arm_spec, admission_deadline=admission_deadline,
                )
            return self.outcomes[key]

    runner = RestartRunner(config)
    daemon = ci.GateDaemon(config, runner=runner)

    result = daemon.poll_once()

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert result["fallback_reason"] == "remote-transport-or-setup-failed"
    assert runner.selection_calls == 1
    assert len(runner.started) == 2
    assert [(arm.arm, arm.box) for arm in runner.calls] == [
        ("base", "studio-two"), ("candidate", "studio-two"),
    ]
    assert not queued.exists()

def test_busy_token_wait_is_bounded_and_returns_capacity(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config = replace(config, admission=ci.AdmissionConfig(0.01, 0.3))
    runner = RemoteRunner(config, executor=lambda *_: None, sleeper=lambda _: None)
    node = runner.nodes["box-a"]
    calls = []
    slow_release_seconds = []
    queued = {
        "run_directory": str(tmp_path / "queued"), "lease_token": "token",
        "lease_id": "lease", "state": "queued", "duplicate_request": False,
        "holder_alive": True, **LEASE_ROUTE,
    }

    def call(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "list":
            return []
        if operation == "release":
            if sum(name == "release" for name, _ in calls) == 1:
                started_at = time.monotonic()
                time.sleep(2)
                slow_release_seconds.append(time.monotonic() - started_at)
            return {"status": "releasing"}
        return dict(queued)

    monkeypatch.setattr(node, "call", call)
    monkeypatch.setattr(
        runner, "choose_box", lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )
    runner.sleeper = time.sleep

    outcome = runner.run_arm(spec("busy-request"))

    assert outcome.status == "capacity"
    assert calls[0][0] == "enqueue"
    assert 0 < calls[0][1]["queue_wait_seconds"] <= 0.3
    assert calls[-1][0] == "release"
    assert slow_release_seconds[0] >= 2
    assert calls[-2][1]["_transport_timeout"] == ci.REMOTE_TRANSPORT_ALLOWANCE_SECONDS
    assert all(kwargs.get("_transport_timeout", 30)
               >= ci.REMOTE_TRANSPORT_ALLOWANCE_SECONDS
               for _, kwargs in calls)
    assert "status" in [name for name, _ in calls]
    assert "start" not in [name for name, _ in calls]

def test_busy_timeout_requires_release_ack_before_fallback(tmp_path, monkeypatch):
    config = replace(_config(tmp_path), admission=ci.AdmissionConfig(0.01, 0.01))
    runner = RemoteRunner(config, executor=lambda *_: None, sleeper=time.sleep)
    node = runner.nodes["box-a"]
    queued = {
        "run_directory": str(tmp_path / "queued"), "lease_token": "token",
        "lease_id": "lease", "state": "queued", "duplicate_request": False,
        "holder_alive": True, **LEASE_ROUTE,
    }
    calls = []

    def call(operation, **kwargs):
        calls.append(operation)
        if operation == "list":
            return []
        if operation == "release":
            return {"status": "owned"}
        return dict(queued)

    monkeypatch.setattr(node, "call", call)
    monkeypatch.setattr(
        runner, "choose_box", lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )

    with pytest.raises(ci.AmbiguousRemoteError, match="could not confirm.*withdrawn"):
        runner.run_arm(spec("busy-unconfirmed"))

    assert calls[-1] == "release"
    assert "start" not in calls

def test_arm_token_wait_uses_the_remaining_placement_deadline(tmp_path, monkeypatch):
    config = replace(_config(tmp_path), admission=ci.AdmissionConfig(0.01, 1.0))
    runner = RemoteRunner(config, executor=lambda *_: None,
                          sleeper=lambda seconds: time.sleep(min(seconds, 0.005)))
    node = runner.nodes["box-a"]
    calls = []
    queued = {
        "run_directory": str(tmp_path / "queued"), "lease_token": "token",
        "lease_id": "lease", "state": "queued", "duplicate_request": False,
        "holder_alive": True, **LEASE_ROUTE,
    }

    def call(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "list":
            return []
        if operation == "release":
            return {"status": "releasing"}
        return dict(queued)

    monkeypatch.setattr(node, "call", call)
    monkeypatch.setattr(
        runner, "choose_box", lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )
    deadline = time.monotonic() + 0.3

    outcome = runner.run_arm(spec("shared-deadline"), admission_deadline=deadline)

    assert outcome.status == "capacity"
    assert 0 < calls[0][1]["queue_wait_seconds"] <= 0.3
    enqueue = calls[0][1]
    assert enqueue["_transport_timeout"] == pytest.approx(
        enqueue["queue_wait_seconds"] + ci.REMOTE_TRANSPORT_ALLOWANCE_SECONDS,
    )
    assert all(kwargs["_transport_timeout"] == ci.REMOTE_STATUS_TIMEOUT_SECONDS
               for operation, kwargs in calls if operation == "status")
    assert all(kwargs["_transport_timeout"] >= ci.REMOTE_TRANSPORT_ALLOWANCE_SECONDS
               for operation, kwargs in calls if operation == "list")
    assert "list" not in [operation for operation, _ in calls]
    assert "start" not in [operation for operation, _ in calls]

def test_expired_admission_after_placement_refuses_before_enqueue(tmp_path, monkeypatch):
    config = _config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    node = runner.nodes["box-a"]
    clock = [100.0]
    monkeypatch.setattr(ci.time, "monotonic", lambda: clock[0])
    calls = []

    def call(operation, **kwargs):
        calls.append((operation, kwargs))
        raise AssertionError(f"unexpected remote operation: {operation}")

    monkeypatch.setattr(node, "call", call)

    def placement(requested=None, **_kwargs):
        clock[0] = 100.11
        return ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10")

    monkeypatch.setattr(runner, "choose_box", placement)
    outcome = runner.run_arm(
        spec("expired-before-enqueue"), admission_deadline=100.1,
    )

    assert outcome.status == "capacity"
    assert calls == []

def test_admission_cleanup_status_checks_use_bounded_transport(tmp_path, monkeypatch):
    config = replace(_config(tmp_path), admission=ci.AdmissionConfig(0.01, 1.0))
    runner = RemoteRunner(config, executor=lambda *_: None)
    node = runner.nodes["box-a"]
    calls = []
    queued = {
        "run_directory": str(tmp_path / "queued"), "lease_token": "token",
        "lease_id": "lease", "state": "queued", "duplicate_request": False,
        "holder_alive": True, **LEASE_ROUTE,
    }
    status_calls = 0

    def call(operation, **kwargs):
        nonlocal status_calls
        calls.append((operation, kwargs))
        if operation == "status":
            status_calls += 1
            if status_calls == 1:
                raise ci.RemoteCIError("status transport failed")
            return dict(queued)
        if operation == "release":
            return {"status": "releasing"}
        return dict(queued)

    monkeypatch.setattr(node, "call", call)
    monkeypatch.setattr(
        runner, "choose_box", lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )

    with pytest.raises(ci.AmbiguousRemoteError, match="no clean verified outcome"):
        runner.run_arm(spec("bounded-cleanup"), admission_deadline=time.monotonic() + 0.3)

    assert [operation for operation, _kwargs in calls].count("status") == 2
    assert all(kwargs["_transport_timeout"] == ci.REMOTE_STATUS_TIMEOUT_SECONDS
               for operation, kwargs in calls if operation == "status")
    enqueue = next(kwargs for operation, kwargs in calls if operation == "enqueue")
    assert enqueue["_transport_timeout"] == pytest.approx(
        enqueue["queue_wait_seconds"] + ci.REMOTE_TRANSPORT_ALLOWANCE_SECONDS,
    )
    assert all(kwargs.get("_transport_timeout", 30)
               >= ci.REMOTE_TRANSPORT_ALLOWANCE_SECONDS
               for _, kwargs in calls)

def test_persistent_post_enqueue_status_timeout_names_failed_phase(tmp_path, monkeypatch):
    config = _config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    node = runner.nodes["box-a"]
    queued = {
        "run_directory": str(tmp_path / "queued"), "lease_token": "token",
        "lease_id": "lease", "state": "queued", "duplicate_request": False,
        "holder_alive": True, **LEASE_ROUTE,
    }
    status_timeouts = []

    def call(operation, **kwargs):
        if operation == "enqueue":
            return dict(queued)
        if operation == "status":
            status_timeouts.append(kwargs["_transport_timeout"])
            raise ci.RemoteTransportTimeoutError("node status timed out")
        if operation == "release":
            return {"status": "releasing"}
        raise AssertionError(f"unexpected remote operation: {operation}")

    monkeypatch.setattr(node, "call", call)
    monkeypatch.setattr(
        runner, "choose_box", lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )

    with pytest.raises(ci.AmbiguousRemoteError, match="post-enqueue status failed"):
        runner.run_arm(spec("post-enqueue-status-timeout"))

    assert status_timeouts[:2] == [
        ci.REMOTE_STATUS_TIMEOUT_SECONDS, ci.REMOTE_STATUS_TIMEOUT_SECONDS,
    ]

def test_remote_node_classifies_executor_timeout_as_transport_timeout(tmp_path):
    config = _config(tmp_path)
    node = RemoteRunner(
        config,
        executor=lambda *_args, **_kwargs: CommandResult(
            124, stderr="command timed out after 0.1s", timed_out=True,
        ),
    ).nodes["box-a"]

    with pytest.raises(ci.RemoteTransportTimeoutError, match="timed out after 0.1s"):
        node.call("status", run_dir="/run", lease_token="token")

def test_transient_post_start_status_timeout_retries_and_verifies(node_env, monkeypatch):
    config, _executor, _, _ = node_env
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    node = runner.nodes["box-a"]
    receipt = json.dumps({
        "schema": ci.RECEIPT_SCHEMA,
        "hostname": "measured-node",
        "status": "pass",
        "returncode": 0,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "failing_node_ids": [],
        "collection_import_errors": [],
        "first_error_lines": [],
        "duration_seconds": 0.1,
    })
    status_calls = []

    def timeout_once(operation, **kwargs):
        assert operation == "status"
        status_calls.append(kwargs.get("_transport_timeout"))
        if len(status_calls) == 1:
            raise ci.RemoteTransportTimeoutError("node status timed out")
        return {
            "state": "released",
            "holder_alive": True,
            "remote_run": {
                "pid": "123", "start_token": "token", "run_dir": "/run",
            },
            "sample": {"hostname": "measured-node"},
            "result": {
                "status": "completed", "exit_known": True,
                "command_started": True, "returncode": 0,
                "streams": {
                    "stdout": {"tail": {"text": receipt, "truncated": False}},
                    "stderr": {"tail": {"text": "", "truncated": False}},
                },
            },
        }

    monkeypatch.setattr(node, "call", timeout_once)

    outcome = runner._watch(
        spec("transient-status-timeout"), node,
        {"run_dir": "/run", "lease_token": "token"},
        RemoteRunner._owner(),
    )

    assert outcome.status == "green"
    assert status_calls == [
        ci.REMOTE_STATUS_TIMEOUT_SECONDS, ci.REMOTE_STATUS_TIMEOUT_SECONDS,
    ]

def test_persistent_post_start_status_timeout_falls_back_with_typed_reason(
    tmp_path, monkeypatch,
):
    config = _config(tmp_path, token_pool_size=1)
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    node = runner.nodes["box-a"]
    started = False
    status_timeouts = []
    admitted_record = {
        "run_directory": str(tmp_path / "remote-run"),
        "lease_token": "lease-token",
        "lease_id": "lease-id",
        **LEASE_ROUTE,
        "owner_identity": "owner-identity",
        "state": "admitted",
        "sample": {"hostname": "measured-node", "self_cap": 4},
        "slot": str(tmp_path / "slot"),
        "checkout_dir": str(tmp_path / "slot"),
        "token_index": "0",
    }

    def always_timeout_status(operation, **kwargs):
        nonlocal started
        if operation == "list":
            return []
        if operation == "enqueue":
            return dict(admitted_record)
        if operation == "start":
            started = True
            return {"status": "started"}
        if operation == "status" and not started:
            return {**admitted_record, "result": None}
        if operation == "status" and started:
            status_timeouts.append(kwargs.get("_transport_timeout"))
            raise ci.RemoteTransportTimeoutError(
                "node status failed: command timed out after 30s",
            )
        raise AssertionError(f"unexpected remote operation: {operation}")

    node.call = always_timeout_status
    monkeypatch.setattr(
        runner, "choose_box", lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request_path = config.queue_dir / "persistent-status-timeout.json"
    request_path.write_text(json.dumps({
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "persistent-status-timeout",
        "project_id": config.repo,
        "kind": "targeted",
        "box": "box-a",
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py", "-q"],
    }), encoding="utf-8")

    result = ci.GateDaemon(config, runner=runner).poll_once()

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert result["fallback_reason"] == "remote-transport-or-setup-failed"
    assert "watch status failed" in result["error"]
    assert "node status failed: command timed out after 30s" in result["error"]
    assert status_timeouts[:2] == [
        ci.REMOTE_STATUS_TIMEOUT_SECONDS, ci.REMOTE_STATUS_TIMEOUT_SECONDS,
    ]

def _run_wrapper(
    monkeypatch, config_path: Path, spool: Path, *, local_command=None, force_local=False,
) -> int:
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_CONFIG", str(config_path))
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(spool))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(config_path.parent / "state"))
    if force_local:
        monkeypatch.setenv("GOALFLIGHT_REMOTE_CPU_ROUTE", "local")
    else:
        monkeypatch.delenv("GOALFLIGHT_REMOTE_CPU_ROUTE", raising=False)
    command = local_command or [sys.executable, "-c", "print('local')"]
    return ci.wrapper_main(["--kind", "gate", "--cmd-id", "heavy-gate", "--", *command])


def _green_wrapper_result(request_id: str) -> dict:
    receipt = ci.RemoteReceipt(
        hostname="measured-node", status="pass", returncode=0,
        passed=1, failed=0, errors=0, failing_node_ids=(),
        collection_import_errors=(), first_error_lines=(),
    ).to_dict()
    return {
        "schema": ci.RESULT_SCHEMA,
        "request_id": request_id,
        "status": "GREEN",
        "candidate": {"status": "green", "returncode": 0, "receipt": receipt},
    }


@pytest.mark.parametrize("publish_before_poll", [True, False])
def test_wrapper_returns_spooled_result_within_one_poll_interval(
    tmp_path, monkeypatch, capsys, publish_before_poll,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    poll_seconds = 0.05
    expected_hostname = "example-box"
    peer_address = "100.64.0.60"
    raw["daemon"]["poll_seconds"] = poll_seconds
    raw["boxes"]["box-a"]["expected_hostname"] = expected_hostname
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = ci.load_config(config_path)
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    node = runner.nodes["box-a"].with_pinned_host(peer_address)
    managed_root = str(node.box.managed_run_directory)
    run_dir = str(Path(managed_root) / "runs" / "lease-a")
    receipt = {
        "schema": ci.RECEIPT_SCHEMA, "hostname": expected_hostname,
        "status": "fail", "returncode": 1, "passed": 0, "failed": 1,
        "errors": 0, "failing_node_ids": ["tests/test_remote.py::test_failed"],
        "collection_import_errors": [], "first_error_lines": ["assert False"],
    }
    record = {
        "state": "released", "managed_run_directory": managed_root,
        "measured_hostname": expected_hostname,
        "destination_address": peer_address,
        "sample": {"hostname": expected_hostname},
        "remote_run": {
            "host": expected_hostname, "address": peer_address,
            "pid": "123", "start_token": "started",
            "run_dir": run_dir, "lease_id": "lease-a", "lease_token": "token-a",
        },
        "result": {
            "status": "completed", "command_started": True,
            "exit_known": True, "returncode": 1,
            "streams": {
                "stdout": {"tail": {"text": json.dumps(receipt), "truncated": False}},
                "stderr": {"tail": {"text": "", "truncated": False}},
            },
        },
    }
    monkeypatch.setattr(runner, "choose_box", lambda **_kwargs: "box-a")
    monkeypatch.setattr(runner, "_status", lambda *_args, **_kwargs: record)
    owner = RemoteRunner._owner()
    monkeypatch.setattr(
        runner, "run_arm",
        lambda arm_spec, **_kwargs: runner._watch(
            arm_spec, node,
            {"run_dir": run_dir, "lease_token": "token-a"}, owner,
        ),
    )
    pending_result = {}
    published_at = []
    sleep_calls = []

    def publish(config_value, result):
        ci._atomic_write_json(
            config_value.result_dir / f"{result['request_id']}.json", result,
        )
        published_at.append(time.monotonic())

    def submit(source, config_value):
        request = json.loads(Path(source).read_text(encoding="utf-8"))
        request["candidate_sha"] = request["snapshot_sha"]
        request["tip_sha"] = request["snapshot_sha"]
        result = ci.run_remote_command(request, runner)
        result["request_id"] = request["request_id"]
        if publish_before_poll:
            publish(config_value, result)
        else:
            pending_result.update(config=config_value, result=result)
        return config_value.queue_dir / f"{request['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    monkeypatch.setattr(ci, "_local_load", lambda: ([0.1], 8))
    if not publish_before_poll:
        original_sleep = ci.time.sleep

        def publish_during_poll(seconds):
            if pending_result and abs(seconds - poll_seconds) < 1e-9:
                sleep_calls.append(seconds)
                if not published_at:
                    publish(pending_result["config"], pending_result["result"])
                return
            original_sleep(seconds)

        monkeypatch.setattr(ci.time, "sleep", publish_during_poll)

    assert _run_wrapper(monkeypatch, config_path, spool) == 1
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["status"] == "RED"
    assert result["candidate"]["receipt"]["measured_hostname"] == expected_hostname
    assert sleep_calls == ([] if publish_before_poll else [poll_seconds])


def test_watch_rejects_a_run_host_outside_its_pinned_node(tmp_path, monkeypatch):
    expected_hostname = "example-box"
    pinned_address = "100.64.0.10"
    other_address = "100.64.0.11"
    config = _two_box_config(tmp_path, first_hostname=expected_hostname)
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    pinned_node = runner.nodes["studio-one"].with_pinned_host(pinned_address)
    identity = {
        "host": expected_hostname, "address": other_address,
        "pid": "123", "start_token": "started",
        "run_dir": "/managed/runs/lease-a", "lease_id": "lease-a",
        "lease_token": "token-a",
    }
    record = {
        "measured_hostname": expected_hostname,
        "destination_address": other_address,
        "remote_run": identity, "holder_alive": True, "result": None,
    }
    calls = []

    def status(operation, **_kwargs):
        calls.append(operation)
        return record

    monkeypatch.setattr(pinned_node, "call", status)

    with pytest.raises(ci.AmbiguousRemoteError, match="differs from pinned peer"):
        runner._watch(
            replace(spec("wrong-run-host"), box="studio-one"), pinned_node,
            {"run_dir": "/managed/runs/lease-a", "lease_token": "token-a"},
            RemoteRunner._owner(),
        )

    assert calls == ["status"]


def _ssh_pin_runner(tmp_path, expected_hostname, *, host_key_alias=None,
                    ssh_config_text=None, remote_exec_options_after_host=()):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["host"] = "box-a.alias"
    raw["boxes"]["box-a"]["expected_hostname"] = expected_hostname
    ssh_config = tmp_path / "ssh_config"
    if ssh_config_text is None:
        ssh_config_text = (
            "Host box-a.alias\n"
            f"  HostName {expected_hostname}\n"
            + (f"  HostKeyAlias {host_key_alias}\n" if host_key_alias else "")
        )
    ssh_config.write_text(ssh_config_text, encoding="utf-8")
    raw["boxes"]["box-a"]["remote_exec"] = [
        "ssh", "-F", str(ssh_config), "-i", "/test/ssh-identity",
        "{host}", *remote_exec_options_after_host, "{script}",
    ]
    path = tmp_path / "remote-ci-ssh-pin.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    runner.nodes["box-a"].control_dir = tmp_path / "ssh-control"
    return config, runner


def _ssh_payload(argv):
    return json.loads(base64.b64decode(shlex.split(argv[-1])[3]))


def _ssh_pinned_host(argv):
    return next((value.partition("=")[2] for value in argv
                 if value.startswith("-oHostName=")), None)


def _ssh_host_key_alias(argv):
    return next((value.partition("=")[2] for value in argv
                 if value.startswith("-oHostKeyAlias=")), None)


def _ssh_destination(argv, alias_address):
    return _ssh_pinned_host(argv) or alias_address


def _mux_failure_ssh_node(tmp_path, monkeypatch):
    pin = "100.64.0.25"
    known_hosts = tmp_path / "known_hosts"
    ssh_config = tmp_path / "ssh_config"
    ssh_config_text = (
        "Host box-a.alias\n"
        "  HostName example-box.local\n"
        "  HostKeyAlias Studio-Known-Host\n"
        f"  UserKnownHostsFile {known_hosts}\n"
        "  StrictHostKeyChecking yes\n"
    )
    _config, runner = _ssh_pin_runner(
        tmp_path, "example-box.local", ssh_config_text=ssh_config_text,
    )
    node = runner.nodes["box-a"]
    fake_ssh = tmp_path / "bin" / "ssh"
    fake_ssh.parent.mkdir()
    call_log = tmp_path / "ssh-calls.jsonl"
    transferred_bundle = tmp_path / "transferred.bundle"
    fake_ssh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"CALL_LOG = {str(call_log)!r}\n"
        f"KNOWN_HOSTS = {str(known_hosts)!r}\n"
        f"TRANSFERRED_BUNDLE = {str(transferred_bundle)!r}\n"
        "args = sys.argv[1:]\n"
        "with open(CALL_LOG, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "if '-G' in args:\n"
        "    pinned = next((arg.partition('=')[2] for arg in args "
        "if arg.startswith('-oHostName=')), None)\n"
        "    print('hostname ' + (pinned or 'example-box.local'))\n"
        "    print('hostkeyalias Studio-Known-Host')\n"
        "    print('userknownhostsfile ' + KNOWN_HOSTS)\n"
        "    print('stricthostkeychecking yes')\n"
        "    print('port 22')\n"
        "    print('identityfile /test/ssh-identity')\n"
        "    print('controlmaster auto')\n"
        "    print('controlpersist 5m')\n"
        "    print('controlpath none')\n"
        "    raise SystemExit(0)\n"
        "if '-oControlMaster=auto' in args:\n"
        "    sys.stderr.write('mm_send_fd: sendmsg(2): Message too long\\n')\n"
        "    sys.stderr.write('mux_client_request_session: send fds failed\\n')\n"
        "    raise SystemExit(255)\n"
        "if args[-1].startswith('cat > '):\n"
        "    with open(TRANSFERRED_BUNDLE, 'wb') as stream:\n"
        "        stream.write(sys.stdin.buffer.read())\n"
        "    raise SystemExit(0)\n"
        "print(json.dumps({'protocol_version': "
        f"{ci.NODE_PROTOCOL_VERSION}, 'result': {{'started': True}}" + "}))\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    node.box = replace(
        node.box, remote_exec=(str(fake_ssh), *node.box.remote_exec[1:]),
    )
    node.control_dir = tmp_path / "ssh-control"
    node.executor = lambda argv, env, timeout, **kwargs: ci.run_command(
        argv, env=env, timeout=timeout, **kwargs,
    )
    monkeypatch.setattr(
        ci, "_ssh_control_path",
        lambda *_args, **_kwargs: node.control_dir / "control.sock",
    )
    pinned_node = node.with_pinned_host(pin)
    return pinned_node, pin, call_log, ssh_config, known_hosts, transferred_bundle


def _assert_pinned_mux_fallback(
    session_calls, *, pinned_node, pin, ssh_config, known_hosts,
):
    assert len(session_calls) == 2
    multiplexed, fallback = session_calls
    assert "-oControlMaster=auto" in multiplexed
    assert "-oControlMaster=no" in fallback
    assert "-oControlPersist=no" in fallback
    assert "-oControlPath=none" in fallback

    def without_mux_options(args):
        return [
            arg for arg in args
            if not arg.startswith((
                "-oControlMaster=", "-oControlPersist=", "-oControlPath=",
            ))
        ]

    assert without_mux_options(multiplexed) == without_mux_options(fallback)
    for args in session_calls:
        assert f"-oHostName={pin}" in args
        assert "-oHostKeyAlias=Studio-Known-Host" in args
        assert pinned_node.box.host in args
        assert "-F" in args and str(ssh_config) in args
        assert "-i" in args and "/test/ssh-identity" in args
    config_text = ssh_config.read_text(encoding="utf-8")
    assert f"  UserKnownHostsFile {known_hosts}\n" in config_text
    assert "  StrictHostKeyChecking yes\n" in config_text


def test_pinned_ssh_retries_mux_fd_send_failure_without_multiplexing(
    tmp_path, monkeypatch,
):
    pinned_node, pin, call_log, ssh_config, known_hosts, _transferred = (
        _mux_failure_ssh_node(tmp_path, monkeypatch)
    )

    assert pinned_node.call(
        "start", run_dir="/managed/runs/run-1", lease_token="lease-token",
    ) == {"started": True}

    calls = [json.loads(line) for line in call_log.read_text().splitlines()]
    session_calls = [args for args in calls if "-G" not in args]
    _assert_pinned_mux_fallback(
        session_calls, pinned_node=pinned_node, pin=pin,
        ssh_config=ssh_config, known_hosts=known_hosts,
    )


def test_pinned_ssh_retries_mux_fd_send_failure_during_bundle_transfer(
    tmp_path, monkeypatch,
):
    pinned_node, pin, call_log, ssh_config, known_hosts, transferred_bundle = (
        _mux_failure_ssh_node(tmp_path, monkeypatch)
    )
    bundle_path = tmp_path / "source.bundle"
    bundle_bytes = b"git bundle data\x00"
    bundle_path.write_bytes(bundle_bytes)

    assert pinned_node.transfer_bundle(
        bundle_path, "/managed/runs/run-1",
    ) == "/managed/runs/run-1/source.bundle"
    assert transferred_bundle.read_bytes() == bundle_bytes

    calls = [json.loads(line) for line in call_log.read_text().splitlines()]
    session_calls = [args for args in calls if "-G" not in args]
    _assert_pinned_mux_fallback(
        session_calls, pinned_node=pinned_node, pin=pin,
        ssh_config=ssh_config, known_hosts=known_hosts,
    )


@pytest.mark.parametrize(("configured_alias", "expected_key_name"), [
    ("Studio-Known-Host", "studio-known-host"),
    (None, "example-box.local"),
])
def test_pinned_ssh_preserves_the_configured_host_key_lookup_name(
    tmp_path, configured_alias, expected_key_name,
):
    _config, runner = _ssh_pin_runner(
        tmp_path, "example-box.local", host_key_alias=configured_alias,
    )
    node = runner.nodes["box-a"].with_pinned_host("100.64.0.25")

    command = node._command_template()

    assert _ssh_pinned_host(command) == "100.64.0.25"
    assert _ssh_host_key_alias(command) == expected_key_name


def test_ssh_host_key_lookup_is_reused_only_for_the_same_pin(tmp_path, monkeypatch):
    _config, runner = _ssh_pin_runner(tmp_path, "example-box.local")
    probes = []
    run = ci.subprocess.run

    def count_probe(argv, *args, **kwargs):
        if "-G" in argv:
            probes.append(tuple(argv))
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(ci.subprocess, "run", count_probe)
    base_node = runner.nodes["box-a"]
    node = base_node.with_pinned_host("100.64.0.25")
    node.with_pinned_host("100.64.0.25")
    assert _ssh_host_key_alias(node._command_template()) == "example-box.local"

    (tmp_path / "ssh_config").write_text(
        "Host box-a.alias\n  HostName updated-studio.local\n", encoding="utf-8",
    )
    updated = base_node.with_pinned_host("100.64.0.25")

    assert _ssh_host_key_alias(updated._command_template()) == "updated-studio.local"
    assert len(probes) == 4
    assert "-F" in probes[0]
    assert str(tmp_path / "ssh_config") in probes[0]
    assert "-oHostName=100.64.0.25" not in probes[0]
    assert "-oHostName=100.64.0.25" in probes[1]


@pytest.mark.parametrize("ssh_config_text", [
    "Host box-a.alias\n  HostName studio.example\n  Port 2222\n",
    "Host box-a.alias\n  HostName studio.example\n"
    "Match originalhost box-a.alias\n  Port 2222\n",
])
def test_nondefault_port_uses_port_qualified_known_hosts_name(
    tmp_path, ssh_config_text,
):
    _config, runner = _ssh_pin_runner(
        tmp_path, "example-box.local", ssh_config_text=ssh_config_text,
    )
    node = runner.nodes["box-a"].with_pinned_host("100.64.0.25")
    lookup_name = _ssh_host_key_alias(node._command_template())

    assert lookup_name == "[studio.example]:2222"

    key_path = tmp_path / "known-host-key"
    generated = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        capture_output=True, text=True,
    )
    assert generated.returncode == 0, generated.stderr
    known_hosts = tmp_path / "known_hosts"
    public_key = Path(f"{key_path}.pub").read_text(encoding="utf-8").strip()
    known_hosts.write_text(
        f"[studio.example]:2222 {public_key}\n", encoding="utf-8",
    )
    result = subprocess.run(
        ["ssh-keygen", "-F", lookup_name, "-f", str(known_hosts)],
        capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert "[studio.example]:2222" in result.stdout


def test_pinned_ssh_honors_port_option_after_host_in_template(tmp_path):
    _config, runner = _ssh_pin_runner(
        tmp_path, "example-box.local",
        remote_exec_options_after_host=("-p", "2222"),
    )

    node = runner.nodes["box-a"].with_pinned_host("100.64.0.25")

    assert _ssh_host_key_alias(node._command_template()) == (
        "[example-box.local]:2222"
    )


@pytest.mark.parametrize("match_block", [
    "Match host studio.example\n  ProxyJump jump.example\n",
    "Match final host studio.example\n  ProxyJump jump.example\n",
])
def test_pinned_ssh_refuses_config_drift_in_match_blocks(tmp_path, match_block):
    _config, runner = _ssh_pin_runner(
        tmp_path, "example-box.local",
        ssh_config_text=(
            "Host box-a.alias\n  HostName studio.example\n" + match_block
        ),
    )

    with pytest.raises(ci.RemoteCIError, match="effective option proxyjump"):
        runner.nodes["box-a"].with_pinned_host("100.64.0.25")


def test_pinned_ssh_preserves_openssh_lookup_for_bracketed_ipv6_hostname(tmp_path):
    _config, runner = _ssh_pin_runner(
        tmp_path, "example-box.local",
        ssh_config_text=(
            "Host box-a.alias\n  HostName [2001:db8::25]\n  Port 2222\n"
        ),
    )

    node = runner.nodes["box-a"].with_pinned_host("100.64.0.25")
    lookup_name = "[[2001:db8::25]]:2222"

    assert _ssh_host_key_alias(node._command_template()) == lookup_name

    key_path = tmp_path / "known-host-key"
    generated = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        capture_output=True, text=True,
    )
    assert generated.returncode == 0, generated.stderr
    known_hosts = tmp_path / "known_hosts"
    public_key = Path(f"{key_path}.pub").read_text(encoding="utf-8").strip()
    known_hosts.write_text(f"{lookup_name} {public_key}\n", encoding="utf-8")
    result = subprocess.run(
        ["ssh-keygen", "-F", lookup_name, "-f", str(known_hosts)],
        capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert lookup_name in result.stdout


def test_pinned_ssh_allows_destination_dependent_control_path(tmp_path):
    _config, runner = _ssh_pin_runner(
        tmp_path, "example-box.local",
        ssh_config_text=(
            "Host box-a.alias\n  HostName studio.example\n"
            "  ControlPath ~/.ssh/cm-%C\n"
        ),
    )

    node = runner.nodes["box-a"].with_pinned_host("100.64.0.25")

    assert _ssh_pinned_host(node._command_template()) == "100.64.0.25"


@pytest.mark.parametrize("ssh_config_text", [
    "Host box-a.alias\n  UnknownOption invalid\n",
    "Host box-a.alias\n  HostName none\n",
])
def test_ssh_host_key_lookup_failure_refuses_to_pin(tmp_path, ssh_config_text):
    _config, runner = _ssh_pin_runner(
        tmp_path, "example-box.local",
        ssh_config_text=ssh_config_text,
    )

    with pytest.raises(ci.RemoteCIError, match="host-key identity"):
        runner.nodes["box-a"].with_pinned_host("100.64.0.25")


def _remote_response(value):
    return CommandResult(
        0, json.dumps({"protocol_version": ci.NODE_PROTOCOL_VERSION, "result": value}),
        "", False,
    )


def test_admission_pins_health_destination_before_alias_drift(tmp_path, monkeypatch):
    measured_hostname = "example-box"
    health_address = "100.64.0.20"
    later_alias_address = "100.64.0.21"
    config, runner = _ssh_pin_runner(tmp_path, f"{measured_hostname}.local")
    alias_target = {"address": health_address}
    address_hostname = {
        health_address: measured_hostname,
        later_alias_address: "Studio-1",
    }
    commands = []
    run_dir = str(config.boxes["box-a"].managed_run_directory / "runs" / "lease-a")
    admitted = {
        "state": "admitted", "run_directory": run_dir,
        "lease_token": "token-a", "lease_id": "lease-a", "token_index": 0,
        "measured_hostname": measured_hostname,
        "destination_address": health_address,
        "sample": {"hostname": measured_hostname, "self_cap": 4},
        "slot": str(tmp_path / "slot"), "checkout_dir": str(tmp_path / "slot"),
        "owner_identity": "owner", "command_present": False,
        "remote_run": {
            "host": measured_hostname, "address": health_address,
            "pid": "123", "start_token": "started",
            "run_dir": run_dir, "lease_id": "lease-a", "lease_token": "token-a",
        },
    }

    def executor(argv, _env, _timeout, **_kwargs):
        payload = _ssh_payload(argv)
        operation = payload["operation"]
        pin = _ssh_pinned_host(argv)
        address = _ssh_destination(argv, alias_target["address"])
        hostname = address_hostname[address]
        commands.append((operation, tuple(argv), pin, address, hostname))
        if operation == "health":
            assert pin is None
            assert address == health_address
            alias_target["address"] = later_alias_address
            return _remote_response({
                "load": {"hostname": hostname, "load1": 0.1,
                         "cpu_busy_fraction": 0.1},
                "peer_address": address,
                "tokens": {"free": 1}, "missing_required_paths": [],
            })
        assert alias_target["address"] == later_alias_address
        assert pin == health_address
        assert address == health_address
        assert hostname == measured_hostname
        assert "box-a.alias" in argv
        assert "-i" in argv and "/test/ssh-identity" in argv
        if operation == "enqueue":
            return _remote_response(admitted)
        if operation == "status":
            return _remote_response(admitted)
        return _remote_response({"status": "started"})

    runner.nodes["box-a"].executor = executor
    monkeypatch.setattr(
        runner, "_watch",
        lambda arm_spec, node, key, _owner: (
            node.call("status", **key)
            and ci.ArmOutcome(arm_spec.arm, "red", 1, None)
        ),
    )

    outcome = runner.run_arm(spec("alias-drift"))

    assert outcome.status == "red"
    assert [operation for operation, _argv, _pin, _address, _hostname in commands] == [
        "health", "enqueue", "start", "status",
    ]
    assert all(pin == health_address and address == health_address
               for operation, _argv, pin, address, _hostname in commands
               if operation != "health")


def test_wrong_enqueue_identity_is_cancelled_before_start(tmp_path):
    measured_hostname = "example-box"
    wrong_hostname = "Studio-1"
    health_address = "100.64.0.30"
    later_alias_address = "100.64.0.31"
    config, runner = _ssh_pin_runner(tmp_path, measured_hostname)
    alias_target = {"address": health_address}
    commands = []
    run_dir = str(config.boxes["box-a"].managed_run_directory / "runs" / "lease-b")
    wrong_lease = {
        "state": "admitted", "run_directory": run_dir,
        "lease_token": "token-b", "lease_id": "lease-b",
        "measured_hostname": wrong_hostname,
        "destination_address": health_address,
        "remote_run": {
            "host": wrong_hostname, "address": health_address,
            "pid": "456", "start_token": "started-b", "run_dir": run_dir,
            "lease_id": "lease-b", "lease_token": "token-b",
        },
    }

    def executor(argv, _env, _timeout, **_kwargs):
        operation = _ssh_payload(argv)["operation"]
        pin = _ssh_pinned_host(argv)
        address = _ssh_destination(argv, alias_target["address"])
        commands.append((operation, pin, address))
        if operation == "health":
            alias_target["address"] = later_alias_address
            return _remote_response({
                "load": {"hostname": measured_hostname, "load1": 0.1,
                         "cpu_busy_fraction": 0.1},
                "peer_address": health_address,
                "tokens": {"free": 1}, "missing_required_paths": [],
            })
        if operation in {"enqueue", "status"}:
            return _remote_response(wrong_lease)
        if operation == "cancel":
            return _remote_response({"status": "cancelled"})
        raise AssertionError(f"unexpected operation {operation}")

    runner.nodes["box-a"].executor = executor
    with pytest.raises(ci.AmbiguousRemoteError, match="differs from configured box"):
        runner.run_arm(spec("wrong-enqueue-identity"))

    operations = [operation for operation, _pin, _address in commands]
    assert operations[:3] == ["health", "enqueue", "status"]
    assert "cancel" in operations
    assert all(pin == health_address and address == health_address
               for operation, pin, address in commands if operation != "health")
    assert "start" not in [operation for operation, _pin, _address in commands]


@pytest.mark.parametrize("missing_field", [
    "peer_address", "measured_hostname", "destination_address",
])
def test_missing_pinned_identity_fields_refuse_before_start(tmp_path, missing_field):
    measured_hostname = "example-box"
    peer_address = "100.64.0.35"
    config, runner = _ssh_pin_runner(tmp_path, measured_hostname)
    commands = []
    run_dir = str(config.boxes["box-a"].managed_run_directory / "runs" / "lease-missing")
    lease = {
        "state": "admitted", "run_directory": run_dir,
        "lease_token": "token-missing", "lease_id": "lease-missing",
        "measured_hostname": measured_hostname,
        "destination_address": peer_address,
        "remote_run": {
            "host": measured_hostname, "address": peer_address,
            "pid": "456", "start_token": "started-missing", "run_dir": run_dir,
            "lease_id": "lease-missing", "lease_token": "token-missing",
        },
    }

    def executor(argv, _env, _timeout, **_kwargs):
        operation = _ssh_payload(argv)["operation"]
        commands.append((operation, _ssh_pinned_host(argv)))
        if operation == "health":
            health = {
                "load": {"hostname": measured_hostname, "load1": 0.1,
                         "cpu_busy_fraction": 0.1},
                "peer_address": peer_address,
                "tokens": {"free": 1}, "missing_required_paths": [],
            }
            if missing_field == "peer_address":
                del health[missing_field]
            return _remote_response(health)
        if operation in {"enqueue", "status"}:
            response = dict(lease)
            if missing_field != "peer_address":
                response.pop(missing_field)
            return _remote_response(response)
        if operation == "cancel":
            return _remote_response({"status": "cancelled"})
        raise AssertionError(f"unexpected operation {operation}")

    runner.nodes["box-a"].executor = executor
    with pytest.raises(ci.RemoteCIError):
        runner.run_arm(spec(f"missing-{missing_field}"))

    operations = [operation for operation, _pin in commands]
    assert "start" not in operations
    if missing_field == "peer_address":
        assert operations == ["health"]
    else:
        assert operations[:3] == ["health", "enqueue", "status"]
        assert "cancel" in operations
        assert all(pin == peer_address for operation, pin in commands
                   if operation != "health")


def test_status_error_cleanup_stays_on_the_pinned_destination(tmp_path, monkeypatch):
    measured_host = "example-box"
    peer_address = "100.64.0.40"
    config, runner = _ssh_pin_runner(tmp_path, measured_host)
    commands = []
    queued = {
        "state": "queued", "run_directory": str(
            config.boxes["box-a"].managed_run_directory / "runs" / "lease-a"
        ),
        "lease_token": "token-a", "lease_id": "lease-a", "holder_alive": True,
        "measured_hostname": measured_host, "destination_address": peer_address,
    }

    def executor(argv, _env, _timeout, **_kwargs):
        payload = _ssh_payload(argv)
        operation = payload["operation"]
        pin = _ssh_pinned_host(argv)
        commands.append((operation, pin))
        if operation == "health":
            return _remote_response({
                "load": {"hostname": measured_host, "load1": 0.1,
                         "cpu_busy_fraction": 0.1},
                "peer_address": peer_address,
                "tokens": {"free": 1}, "missing_required_paths": [],
            })
        if operation == "enqueue":
            return _remote_response(queued)
        if operation == "status":
            return CommandResult(2, stderr="simulated status failure")
        if operation == "release":
            return _remote_response({"status": "releasing"})
        raise AssertionError(f"unexpected operation {operation}")

    runner.nodes["box-a"].executor = executor
    with pytest.raises(ci.AmbiguousRemoteError, match="no clean verified outcome"):
        runner.run_arm(spec("pinned-cleanup"))

    assert [operation for operation, _pin in commands] == [
        "health", "enqueue", "status", "status", "release",
    ]
    assert all(pin == peer_address for operation, pin in commands
               if operation != "health")


def test_reaper_reattaches_using_the_host_persisted_on_the_lease(
    tmp_path, monkeypatch,
):
    stored_host = "example-box"
    stored_address = "100.64.0.50"
    config, runner = _ssh_pin_runner(tmp_path, f"{stored_host}.local")
    commands = []
    run_dir = str(config.boxes["box-a"].managed_run_directory / "runs" / "lease-a")
    identity = {
        "host": stored_host, "address": stored_address,
        "pid": "123", "start_token": "started",
        "run_dir": run_dir, "lease_id": "lease-a", "lease_token": "token-a",
    }
    lease = {
        "state": "running", "lease_id": "lease-a", "lease_token": "token-a",
        "measured_hostname": stored_host, "destination_address": stored_address,
        "run_directory": run_dir, "remote_run": identity,
        "owner_host": ci.socket.gethostname(), "owner_pid": 1,
        "owner_start_token": "old-controller", "owner_identity": "old-controller",
    }
    monkeypatch.setattr(ci.goalflight_compat, "process_identity_matches", lambda *_: False)

    def executor(argv, _env, _timeout, **_kwargs):
        payload = _ssh_payload(argv)
        operation = payload["operation"]
        pin = _ssh_pinned_host(argv)
        commands.append((operation, pin))
        if operation == "list":
            return _remote_response([lease])
        if operation == "cancel":
            return _remote_response({"status": "cancelled"})
        raise AssertionError(f"unexpected operation {operation}")

    runner.nodes["box-a"].executor = executor

    result = runner.reap()

    assert result == [{"box": "box-a", "lease_id": "lease-a", "status": "cancelled"}]
    assert commands == [("list", None), ("cancel", stored_address)]

def test_project_mismatch_after_submit_refuses_local_fallback(
    tmp_path, monkeypatch, capsys,
):
    config_path, spool = _wrapper_config(tmp_path)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)

    def submit(_source, config):
        request_id = json.loads(Path(_source).read_text(encoding="utf-8"))["request_id"]
        ci._atomic_write_json(config.result_dir / f"{request_id}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": request_id,
            "status": "PROJECT_MISMATCH",
            "fallback_reason": "missing-project-id",
        })
        return config.queue_dir / f"{request_id}.json"

    monkeypatch.setattr(ci, "submit_request", submit)

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["route"] == "remote-unavailable"
    assert receipt["fallback_reason"] == "remote-ownership-unresolved"
    assert receipt["project_id"] == ci.load_config(config_path).repo

@pytest.mark.parametrize(("policy", "force_local"), [
    ("queue", False), ("queue", True), ("fail", False), ("fail", True),
])
def test_nonlocal_policy_routes_idle_or_forced_local_commands_remotely(
    tmp_path, monkeypatch, policy, force_local,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["fallback"] = policy
    raw["runner"]["heavy_command_ids"] = []
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(ci, "_local_load", lambda: ([0.1, 0.1], 8))
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    captured = {}

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        captured.update(payload)
        ci._atomic_write_json(
            config.result_dir / f"{payload['request_id']}.json",
            _green_wrapper_result(payload["request_id"]),
        )
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("nonlocal policy ran locally"),
    )

    assert _run_wrapper(
        monkeypatch, config_path, spool, force_local=force_local,
    ) == 0
    assert captured["project_id"] == ci.load_config(config_path).repo

@pytest.mark.parametrize("policy", ["queue", "fail"])
def test_nonlocal_policy_with_down_daemon_returns_typed_failure(
    tmp_path, monkeypatch, capsys, policy,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["fallback"] = policy
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: False)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("nonlocal policy ran locally"),
    )

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["policy_outcome"] == "failed"
    assert receipt["fallback_reason"] == "daemon-unavailable"
    assert receipt["passed"] == 0
    assert receipt["failed"] == 0
    assert receipt["errors"] == 0
    assert receipt["error"]

def test_remote_unavailable_preserves_daemon_error_text(
    tmp_path, monkeypatch, capsys,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["fallback"] = "queue"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    error = "remote run run-1/candidate has no clean verified outcome: node status timed out"
    captured = {}
    monkeypatch.setattr(ci, "_local_load", lambda: ([0.1, 0.1], 8))
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("queued policy ran locally"),
    )

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        captured["request_id"] = payload["request_id"]
        ci._atomic_write_json(config.result_dir / f"{payload['request_id']}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": payload["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": "remote-transport-or-setup-failed",
            "error": error,
        })
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    result = json.loads(
        (spool / "results" / f"{captured['request_id']}.json").read_text(encoding="utf-8")
    )
    assert receipt["fallback_reason"] == "remote-ownership-unresolved"
    assert error in receipt["error"]
    assert result["status"] == "REMOTE_UNAVAILABLE"
    assert error in result["error"]


def test_setup_failure_after_submit_returns_unavailability(
    tmp_path, monkeypatch, capsys,
):
    config_path, spool = _wrapper_config(tmp_path)
    error = "profile test selector is missing from the request snapshot"
    captured = {}
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)

    def submit(source, config):
        request_id = json.loads(Path(source).read_text(encoding="utf-8"))["request_id"]
        captured["request_id"] = request_id
        ci._atomic_write_json(config.result_dir / f"{request_id}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": request_id,
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": "candidate-setup-failed",
            "error": error,
            "candidate": {"status": "setup-failed", "error": error},
        })
        return config.queue_dir / f"{request_id}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    assert _run_wrapper(
        monkeypatch, config_path, spool,
        local_command=[sys.executable, "-c", "pass"],
    ) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["fallback_reason"] == "remote-ownership-unresolved"
    assert error in receipt["error"]
    assert not list((spool / "requests").glob("*.local.json"))

@pytest.mark.parametrize(("policy", "variable", "value", "reason"), [
    (policy, variable, value, reason)
    for policy in ("queue", "fail")
    for variable, value, reason in (
        ("PYTEST_ADDOPTS", "-v", "pytest-addopts-set"),
        ("PYTEST_PLUGINS", "caller-plugin", "pytest-plugins-set"),
    )
])
def test_nonlocal_policy_rejects_caller_pytest_environment_without_local_run(
    tmp_path, monkeypatch, capsys, policy, variable, value, reason,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["fallback"] = policy
    raw["runner"]["command"] = ["python3", "-m", "pytest"]
    raw["runner"]["test_command"] = ["python3", "-m", "pytest"]
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("nonlocal policy ran locally"),
    )

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["policy_outcome"] == "failed"
    assert receipt["fallback_reason"] == reason

def test_invalid_existing_config_fails_closed_without_local_run(tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path)
    config_path.write_text('{"fallback":"queue",', encoding="utf-8")
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("invalid configured policy ran locally"),
    )

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["policy_outcome"] == "failed"
    assert "cannot read config" in receipt["fallback_reason"]

def test_local_pytest_command_is_unchanged_by_remote_worker_cap(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["runner"]["command"] = ["python3", "-m", "pytest"]
    raw["runner"]["test_command"] = ["python3", "-m", "pytest"]
    raw["runner"]["pytest_workers_cap"] = 4
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    command = [sys.executable, "-m", "pytest", "-n", "0", "tests/test_remote_ci_transport.py"]
    observed = []
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda actual, **_kwargs: (observed.append(list(actual)) or 0, {}),
    )

    assert _run_wrapper(
        monkeypatch, config_path, spool, local_command=command, force_local=True,
    ) == 0
    assert observed == [command]

def test_local_fallback_keeps_invalid_remote_worker_options_unchanged(
    tmp_path, monkeypatch,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["runner"]["command"] = ["python3", "-m", "pytest"]
    raw["runner"]["test_command"] = ["python3", "-m", "pytest"]
    raw["runner"]["pytest_workers_cap"] = 4
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    command = [sys.executable, "-m", "pytest", "tests/test_remote_ci_transport.py", "-n", "16"]
    observed = []
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        ci._atomic_write_json(config.result_dir / f"{payload['request_id']}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": payload["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": "command-outside-profile-grammar",
        })
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda actual, **_kwargs: (observed.append(list(actual)) or 0, {}),
    )

    assert _run_wrapper(
        monkeypatch, config_path, spool, local_command=command,
    ) == 0
    assert observed == [command]

@pytest.mark.parametrize(("variable", "value", "reason"), [
    ("PYTEST_ADDOPTS", "-v", "pytest-addopts-set"),
    ("PYTEST_PLUGINS", "node-only-plugin", "pytest-plugins-set"),
])
def test_profile_wrapper_runs_local_when_pytest_environment_is_set(
    tmp_path, monkeypatch, capsys, variable, value, reason,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["runner"]["command"] = ["python3", "-m", "pytest"]
    raw["runner"]["test_command"] = ["python3", "-m", "pytest"]
    raw["environment"] = {"requirements": ["pytest==9.1.1"]}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(
        ci, "daemon_is_running",
        lambda _config: pytest.fail("profile with PYTEST_ADDOPTS must stay local"),
    )

    assert _run_wrapper(monkeypatch, config_path, spool) == 0

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["route"] == "local-fallback"
    assert receipt["fallback_reason"] == reason
    assert receipt["project_id"] == ci.load_config(config_path).repo
    assert receipt["policy_outcome"] == "fell_back"

def test_local_fallback_applies_project_local_environment(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["boxes"]["box-a"]["env"] = {
        "EXAMPLE_DEP_ROOT": "/remote/pm2",
        "PYTHONPATH": "/remote/python",
    }
    raw["runner"]["env"]["RUNNER_PROFILE"] = "runner-value"
    raw["local_env"] = {
        "EXAMPLE_DEP_ROOT": str(tmp_path / "local-pm2"),
        "PYTHONPATH": "local-python",
    }
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: False)
    command = [
        sys.executable, "-c",
        "import os; assert os.environ['EXAMPLE_DEP_ROOT'].endswith('local-pm2'); "
        "assert os.environ['PYTHONPATH'] == 'local-python'; "
        "assert os.environ['RUNNER_PROFILE'] == 'runner-value'",
    ]

    assert _run_wrapper(monkeypatch, config_path, spool, local_command=command) == 0

    result = json.loads(next((spool / "requests").glob("*.local.json")).read_text())
    assert result["receipt"]["route"] == "local-fallback"
    assert result["receipt"]["fallback_reason"] == "daemon-unavailable"

def test_local_route_applies_project_local_environment(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["boxes"]["box-a"]["env"].update({
        "PATH": "/remote/homebrew/bin",
        "EXAMPLE_DEP_ROOT": "/remote/pm2",
    })
    raw["runner"]["env"].update({
        "PATH": "runner-path",
        "RUNNER_PROFILE": "runner-value",
    })
    raw["local_env"] = {"PATH": None, "EXAMPLE_DEP_ROOT": "local-pm2"}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    caller_path = os.environ["PATH"]
    command = [
        sys.executable, "-c",
        "import os; assert os.environ['PATH'] == " + repr(caller_path) + "; "
        "assert os.environ['EXAMPLE_DEP_ROOT'] == 'local-pm2'; "
        "assert os.environ['RUNNER_PROFILE'] == 'runner-value'",
    ]

    assert _run_wrapper(
        monkeypatch, config_path, spool, local_command=command, force_local=True,
    ) == 0

    result = json.loads(next((spool / "requests").glob("*.local.json")).read_text())
    assert result["receipt"]["route"] == "local"
    assert result["receipt"]["fallback_reason"] is None

def test_local_fallback_null_inherits_caller_path(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["boxes"]["box-a"]["env"]["PATH"] = "/remote/homebrew/bin"
    raw["runner"]["env"]["PATH"] = "runner-path"
    raw["local_env"] = {"PATH": None}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    caller_path = os.environ["PATH"]
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: False)
    command = [
        sys.executable, "-c",
        f"import os; assert os.environ['PATH'] == {caller_path!r}",
    ]

    assert _run_wrapper(monkeypatch, config_path, spool, local_command=command) == 0

    result = json.loads(next((spool / "requests").glob("*.local.json")).read_text())
    assert result["receipt"]["fallback_reason"] == "daemon-unavailable"

@pytest.mark.parametrize("force_local", [False, True])
def test_local_execution_refuses_box_environment_without_local_equivalent(
    tmp_path, monkeypatch, capsys, force_local,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["boxes"]["box-a"]["env"].update({
        "EXAMPLE_DEP_ROOT": "/remote/pm2",
        "PYTHONPATH": "/remote/python",
    })
    raw["local_env"] = {"PYTHONPATH": "local-python"}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: False)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("fallback ran without its profile environment"),
    )

    assert _run_wrapper(
        monkeypatch, config_path, spool, force_local=force_local,
    ) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["route"] == "remote-unavailable"
    assert receipt["fallback_reason"] == "profile-env-unavailable-locally"
    assert "EXAMPLE_DEP_ROOT" in receipt["error"]

def test_wrapper_requires_pair_mode_for_base_argument(monkeypatch, capfd):
    local_calls = []
    monkeypatch.setattr(ci, "_run_local_command", lambda *args, **kwargs: local_calls.append(args))

    with pytest.raises(SystemExit) as caught:
        ci.wrapper_main([
            "--kind", "gate", "--cmd-id", "heavy-gate", "--base", "HEAD", "--",
            sys.executable, "-c", "pass",
        ])

    assert caught.value.code == 2
    assert "--base, --recheck, and --json require --pair" in capfd.readouterr().err
    assert not local_calls

def test_claimed_seat_wait_triggers_remote_offload(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["runner"]["heavy_command_ids"] = []
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    dispatch_queue = tmp_path / "dispatch-queue"
    dispatch_queue.mkdir()
    (dispatch_queue / "dispatch-1.json.claimed-123-456").write_text("claimed")
    monkeypatch.setattr(ci.goalflight_dispatch_paths, "dispatch_queue_dir", lambda: dispatch_queue)
    monkeypatch.setattr(ci, "_local_load", lambda: ([0.1, 0.1, 0.1], 8))
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    captured = {}

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        captured.update(payload)
        ci.validate_request(payload, config)
        ci._atomic_write_json(
            config.result_dir / f"{payload['request_id']}.json",
            _green_wrapper_result(payload["request_id"]),
        )
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    assert _run_wrapper(monkeypatch, config_path, spool) == 0
    assert captured["command_id"] == "heavy-gate"

def test_waiting_capacity_ledger_triggers_remote_offload(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["runner"]["heavy_command_ids"] = []
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    dispatch_queue = tmp_path / "dispatch-queue"
    dispatch_queue.mkdir()
    monkeypatch.setattr(ci.goalflight_dispatch_paths, "dispatch_queue_dir", lambda: dispatch_queue)
    monkeypatch.setattr(ci.goalflight_ledger, "read_records", lambda **kwargs: [
        {"dispatch_id": "waiting-seat", "state": "waiting_capacity"},
    ])
    monkeypatch.setattr(ci, "_local_load", lambda: ([0.1, 0.1, 0.1], 8))
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)

    captured = {}

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        captured.update(payload)
        ci.validate_request(payload, config)
        ci._atomic_write_json(
            config.result_dir / f"{payload['request_id']}.json",
            _green_wrapper_result(payload["request_id"]),
        )
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    assert _run_wrapper(monkeypatch, config_path, spool) == 0
    assert captured["command_id"] == "heavy-gate"

def test_daemon_down_runs_local_and_records_fallback_reason(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: False)
    monkeypatch.setattr(ci, "submit_request", lambda *_: pytest.fail("submitted without daemon"))

    assert _run_wrapper(monkeypatch, config_path, spool) == 0

    result = next((spool / "requests").glob("*.local.json"))
    receipt = json.loads(result.read_text())["receipt"]
    assert receipt["route"] == "local-fallback"
    assert receipt["fallback_reason"] == "daemon-unavailable"
    assert receipt["project_id"] == ci.load_config(config_path).repo
    assert receipt["policy_outcome"] == "fell_back"

@pytest.mark.parametrize("policy", ["queue", "fail"])
def test_nonlocal_fallback_policies_return_typed_remote_unavailable(
    tmp_path, monkeypatch, capsys, policy,
):
    config_path, spool = _wrapper_config(tmp_path, repo_id="example-sim-main")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["repo"] = "example-sim-main"
    raw["fallback"] = policy
    raw["admission"]["max_wait_seconds"] = 0.05
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    project_root = tmp_path / "example-sim-main"
    _init_repo(project_root)
    _declare_repo_identity(project_root, "example-sim-main")
    monkeypatch.chdir(project_root)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        ci._atomic_write_json(config.result_dir / f"{payload['request_id']}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": payload["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": "candidate-capacity",
        })
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("non-local fallback policy ran locally"),
    )

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["status"] == "fail"
    assert receipt["returncode"] == ci.REMOTE_UNAVAILABLE_EXIT
    assert receipt["project_id"] == "example-sim-main"
    assert receipt["policy_outcome"] == "failed"

def test_daemon_death_after_submit_refuses_local_run(
        tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path)
    monkeypatch.setattr(ci, "DAEMON_REATTACH_WINDOW_SECONDS", 120)
    checks = iter((True, False, False))
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: next(checks))
    ticks = iter((0.0, 0.0, 0.0, 120.0))
    monkeypatch.setattr(ci, "time", SimpleNamespace(
        monotonic=lambda: next(ticks, 1000.0), sleep=lambda _seconds: None,
    ))

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["fallback_reason"] == "remote-ownership-unresolved"
    assert receipt["request_id"] in receipt["error"]
    assert not list((spool / "requests").glob("*.local.json"))
    requests = spool / "requests"
    pending = [path for path in requests.glob("*.json") if not path.name.endswith(".local.json")]
    markers = list(requests.glob(".*.cancel"))
    assert len(pending) == len(markers) == 1
    assert json.loads(markers[0].read_text())["reason"] == "remote-ownership-unresolved"


def _init_wrapper_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Remote CI Test")
    _git(path, "config", "user.email", "remote-ci@example.invalid")
    (path / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "--quiet", "-m", "initial")


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
def test_snapshot_identity_includes_edits_hidden_by_index_flags(tmp_path: Path, flag: str):
    repo = tmp_path / "repo"
    _init_wrapper_repo(repo)
    _git(repo, "update-index", f"--{flag}", "tracked.txt")
    (repo / "tracked.txt").write_text(f"changed under {flag}\n", encoding="utf-8")
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert not ci._worktree_status(repo)

    actual_head, snapshot_sha, dirty = ci._worktree_snapshot_identity(repo)
    daemon_snapshot = ci._snapshot_worktree(
        repo, f"hidden-{flag}", tmp_path / "bundles",
        cwd_relative="", caller_env={},
    )
    try:
        assert actual_head == head_sha
        assert dirty is True
        assert snapshot_sha != _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
        assert daemon_snapshot["dirty"] is True
        assert daemon_snapshot["tree_sha"] == snapshot_sha
    finally:
        ci._release_snapshot(
            Path(daemon_snapshot["root"]), daemon_snapshot["ref"],
            daemon_snapshot["snapshot_sha"], Path(daemon_snapshot["bundle_path"]),
        )


def test_snapshot_identity_rebuilds_after_edit_between_status_checks(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_wrapper_repo(repo)
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    original_status = ci._worktree_status
    status_checks = 0

    def edit_after_status(root, *, deadline=None):
        nonlocal status_checks
        status = original_status(root, deadline=deadline)
        status_checks += 1
        if status_checks == 1:
            (repo / "tracked.txt").write_text("edit after status\n", encoding="utf-8")
        return status

    monkeypatch.setattr(ci, "_worktree_status", edit_after_status)

    actual_head, snapshot_sha, dirty = ci._worktree_snapshot_identity(repo)

    assert actual_head == head_sha
    assert dirty is True
    assert status_checks >= 3
    assert snapshot_sha != _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()


def test_sandbox_exec_probe_skips_refusal_but_fails_on_other_errors(monkeypatch):
    monkeypatch.setattr(test_support.sys, "platform", "darwin")
    monkeypatch.setattr(
        test_support.shutil, "which", lambda _name: "/usr/bin/sandbox-exec",
    )
    monkeypatch.setattr(
        test_support.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    assert sandbox_exec_unavailable_reason() is None

    monkeypatch.setattr(
        test_support.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 71, stdout="", stderr="sandbox_apply: Operation not permitted",
        ),
    )
    assert sandbox_exec_unavailable_reason() == "sandbox denies exec of sandbox-exec"

    monkeypatch.setattr(
        test_support.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 65, stdout="", stderr="invalid sandbox profile",
        ),
    )
    with pytest.raises(AssertionError, match="without a sandbox refusal"):
        sandbox_exec_unavailable_reason()


def test_node_probe_skips_refusal_but_fails_on_other_errors(monkeypatch):
    monkeypatch.setattr(test_support.shutil, "which", lambda _name: "/usr/bin/node")
    monkeypatch.setattr(
        test_support.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "v22\n", ""),
    )
    assert node_unavailable_reason() is None

    def denied(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(test_support.subprocess, "run", denied)
    assert node_unavailable_reason() == "sandbox denies exec of Node"

    monkeypatch.setattr(
        test_support.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "", "invalid node option",
        ),
    )
    with pytest.raises(AssertionError, match="without a sandbox refusal"):
        node_unavailable_reason()


@pytest.mark.parametrize("dirty", [False, True])
def test_read_only_sandbox_remote_run_submits_clean_commit_and_refuses_dirty_tree(
    tmp_path, dirty,
):
    reason = sandbox_exec_unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    repo = tmp_path / "repo"
    _init_wrapper_repo(repo)
    _declare_repo_identity(repo, "repo")
    if dirty:
        (repo / "tracked.txt").write_text("uncommitted\n", encoding="utf-8")

    config_path, spool = _wrapper_config(tmp_path, repo_id="repo")
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    raw_config["daemon"]["pid_file"] = None
    config_path.write_text(json.dumps(raw_config), encoding="utf-8")
    lock_path = Path(raw_config["daemon"]["lock_file"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    daemon_lock = lock_path.open("a+")
    ci.fcntl.flock(daemon_lock.fileno(), ci.fcntl.LOCK_EX | ci.fcntl.LOCK_NB)
    blocked_tmp = tmp_path / "blocked-tmp"
    blocked_tmp.mkdir()
    child_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(blocked_tmp),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GOALFLIGHT_REMOTE_CI_CONFIG": str(config_path),
        "GOALFLIGHT_REMOTE_CI_SPOOL_DIR": str(spool),
        "GOALFLIGHT_STATE_DIR": str(tmp_path / "state"),
    }
    command = [
        "--kind", "gate", "--cmd-id", "heavy-gate", "--",
        sys.executable, "-c", "print('remote')",
    ]
    module_dir = Path(ci.__file__).resolve().parent
    code = "\n".join((
        "import os, sys",
        f"sys.path.insert(0, {str(module_dir)!r})",
        "for target in (os.path.join(os.environ['TMPDIR'], 'write-probe'),",
        "               os.path.join(os.getcwd(), '.write-probe')):",
        "    try:",
        "        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
        "    except OSError:",
        "        continue",
        "    os.close(descriptor)",
        "    os.unlink(target)",
        "    raise SystemExit('read-only sandbox unexpectedly allowed a write')",
        "import goalflight_remote_ci as ci",
        f"raise SystemExit(ci.wrapper_main({command!r}))",
    ))
    prepared = os_sandbox.prepare_os_sandbox_command(
        sys.executable,
        ["-c", code],
        cwd=str(repo),
        os_sandbox=os_sandbox.OS_SANDBOX_READ_ONLY,
        agent="remote-ci-test",
        environment=child_env,
    )
    assert str(repo.resolve()) not in prepared.write_roots
    assert str(blocked_tmp.resolve()) not in prepared.write_roots
    assert str((spool / "requests").resolve()) in prepared.write_roots

    submitted = []
    daemon_errors = []
    stop_daemon = threading.Event()

    def provide_result():
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline and not stop_daemon.is_set():
                for request_path in (spool / "requests").glob("*.json"):
                    request = json.loads(request_path.read_text(encoding="utf-8"))
                    submitted.append(request)
                    receipt = {
                        "schema": ci.RECEIPT_SCHEMA,
                        "hostname": "studio-test",
                        "status": "pass",
                        "returncode": 0,
                        "passed": 1,
                        "failed": 0,
                        "errors": 0,
                    }
                    ci._atomic_write_json(spool / "results" / f"{request['request_id']}.json", {
                        "schema": ci.RESULT_SCHEMA,
                        "request_id": request["request_id"],
                        "status": "GREEN",
                        "candidate": {"returncode": 0, "receipt": receipt},
                    })
                    return
                time.sleep(0.01)
            daemon_errors.append("no spool request appeared")
        except Exception as exc:  # surface helper failures in the test thread
            daemon_errors.append(exc)

    daemon_thread = None
    if not dirty:
        daemon_thread = threading.Thread(target=provide_result, daemon=True)
        daemon_thread.start()
    try:
        result = subprocess.run(
            [prepared.command, *prepared.args],
            cwd=repo,
            env=child_env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    finally:
        ci.fcntl.flock(daemon_lock.fileno(), ci.fcntl.LOCK_UN)
        daemon_lock.close()
        if daemon_thread is not None:
            stop_daemon.set()
            daemon_thread.join(timeout=2)

    assert not list(blocked_tmp.iterdir())
    assert not (repo / ".write-probe").exists()
    if dirty:
        assert result.returncode == 2
        assert (
            "read-only dispatch with uncommitted changes cannot submit; "
            "commit or run from a writable worker"
        ) in result.stderr
        assert not submitted
        assert not list((spool / "requests").glob("*.json"))
    else:
        assert result.returncode == 0, result.stderr
        assert not daemon_errors
        assert len(submitted) == 1
        assert submitted[0]["dirty"] is False
        assert submitted[0]["head_sha"] == _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert submitted[0]["snapshot_sha"] == _git(
            repo, "rev-parse", "HEAD^{tree}",
        ).stdout.strip()
        remote_result = json.loads(result.stdout.splitlines()[-1])
        assert remote_result["candidate"]["receipt"]["hostname"] == "studio-test"


@pytest.mark.parametrize("commit_change", [False, True])
def test_direct_local_fallback_checks_requested_tree(tmp_path, monkeypatch, capsys, commit_change):
    repo = tmp_path / "repo"
    _init_wrapper_repo(repo)
    _declare_repo_identity(repo, "repo")
    monkeypatch.chdir(repo)
    config_path, spool = _wrapper_config(tmp_path / "config", repo_id="repo")

    def daemon_down(_config):
        (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        if commit_change:
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "--quiet", "-m", "changed")
        return False

    monkeypatch.setattr(ci, "daemon_is_running", daemon_down)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("changed tree ran locally"),
    )

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["fallback_reason"] == "fallback-tree-changed"
    assert "requested HEAD" in receipt["error"] and "current HEAD" in receipt["error"]


def test_unknown_daemon_liveness_after_submit_refuses_local_run(tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path)
    monkeypatch.setattr(ci, "DAEMON_REATTACH_WINDOW_SECONDS", 0)
    states = iter((True, None, None))
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: next(states))

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["fallback_reason"] == "remote-ownership-unresolved"
    assert "could not verify daemon liveness" in receipt["error"]
    assert not list((spool / "requests").glob("*.local.json"))


def test_unknown_initial_daemon_liveness_waits_for_daemon(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    states = iter((None, True))

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        ci._atomic_write_json(
            config.result_dir / f"{payload['request_id']}.json",
            _green_wrapper_result(payload["request_id"]),
        )
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "DAEMON_REATTACH_WINDOW_SECONDS", 1)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: next(states))
    monkeypatch.setattr(ci, "submit_request", submit)

    assert _run_wrapper(monkeypatch, config_path, spool) == 0
    assert not list((spool / "requests").glob("*.local.json"))

@pytest.mark.parametrize("scenario", [
    "empty-cache-admission-race", "transport-give-up", "unconfirmed-cancellation",
    "publication-error", "box-ticket-accepted",
])
def test_submitted_ownership_failures_never_run_locally(tmp_path, monkeypatch, capsys, scenario):
    config_path, spool = _wrapper_config(tmp_path)
    if scenario == "box-ticket-accepted":
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["admission"] = "box"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    monkeypatch.setattr(ci, "_run_local_command", lambda *_a, **_k: pytest.fail("double run"))

    def submit(source, config):
        payload = json.loads(source.read_text())
        request_id = payload["request_id"]
        destination = config.queue_dir / f"{request_id}.json"
        ci._atomic_write_json(destination, payload)
        if scenario == "publication-error":
            raise OSError("directory fsync failed after publication")
        result = {
            "request_id": request_id, "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": scenario,
            "candidate": {"status": "timeout", "cancelled": False,
                          "lease": {"holder_alive": True}},
        }
        if scenario == "box-ticket-accepted":
            result["remote_ticket_accepted"] = True
        ci._atomic_write_json(config.result_dir / f"{request_id}.json", result)
        return destination

    monkeypatch.setattr(ci, "submit_request", submit)
    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["fallback_reason"] == "remote-ownership-unresolved"
    assert receipt["request_id"] in receipt["error"]
    assert not list((spool / "requests").glob("*.local.json"))


def test_box_submit_rejection_can_use_configured_local_fallback(
    tmp_path, monkeypatch,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["admission"] = "box"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    local_calls = []

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        destination = config.queue_dir / f"{payload['request_id']}.json"
        ci._atomic_write_json(destination, payload)
        ci._atomic_write_json(config.result_dir / f"{payload['request_id']}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": payload["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": "submit-rejected",
            "remote_ticket_accepted": False,
        })
        return destination

    monkeypatch.setattr(ci, "submit_request", submit)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **kwargs: (
            local_calls.append(kwargs) or (0, {"fallback_reason": kwargs["fallback_reason"]})
        ),
    )
    assert _run_wrapper(monkeypatch, config_path, spool) == 0
    assert local_calls and local_calls[0]["fallback_reason"] == "submit-rejected"


def test_box_wrapper_give_up_cancels_submitted_ticket(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["admission"] = "box"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    monkeypatch.setattr(
        ci, "_remote_result_budget_seconds",
        lambda _config, *, executions=1: -1,
    )

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        destination = config.queue_dir / f"{payload['request_id']}.json"
        ci._atomic_write_json(destination, payload)
        return destination

    monkeypatch.setattr(ci, "submit_request", submit)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("accepted resident ticket ran locally"),
    )

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    request = next((spool / "requests").glob("*.json"))
    request_id = request.stem
    cancellation = json.loads(
        (request.parent / f".{request_id}.cancel").read_text(encoding="utf-8")
    )
    assert cancellation["request_id"] == request_id


def test_sleep_does_not_expire_queue_or_collection_wait(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["admission"] = "box"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    clock = {"mono": 100.0, "wall": 1000.0}
    monkeypatch.setattr(ci, "time", SimpleNamespace(
        monotonic=lambda: clock["mono"], time=lambda: clock["wall"],
        sleep=lambda _seconds: None,
    ))
    config = ci.load_config(config_path)
    assert ci._remote_result_budget_seconds(config) < ci.SUBMITTED_TICKET_TTL_SECONDS
    daemon = ci.GateDaemon(config)
    assert daemon._queue_waited_seconds("sleeping") == 0
    checks = []

    def daemon_state(_config):
        checks.append(None)
        if len(checks) == 2:
            clock["wall"] += 3600
            assert daemon._queue_waited_seconds("sleeping") == 0
        if len(checks) == 3:
            request = next((spool / "requests").glob("*.json"))
            payload = json.loads(request.read_text())
            ci._atomic_write_json(
                spool / "results" / request.name,
                _green_wrapper_result(payload["request_id"]),
            )
        return True

    monkeypatch.setattr(ci, "daemon_is_running", daemon_state)
    monkeypatch.setattr(ci, "_run_local_command", lambda *_a, **_k: pytest.fail("local run"))
    assert _run_wrapper(monkeypatch, config_path, spool) == 0
    assert len(checks) == 3
    clock["mono"] += 2
    assert daemon._queue_waited_seconds("sleeping") == 2


def test_queue_progress_reports_age_position_box_and_health(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    for request_id in ("a-before", "m-pending", "z-after"):
        (config.queue_dir / f"{request_id}.json").write_text("{}", encoding="utf-8")
    pending = config.queue_dir / "m-pending.json"
    ci._atomic_write_json(pending, {
        "_resident": {"arms": {"candidate": {"box": "box-a", "phase": "accepted"}}},
    })

    class FakeNode:
        box = config.boxes["box-a"]
        response = {"load": {"hostname": "measured-node", "load1": 53},
                    "tokens": {"free": 0}}

        def call(self, operation, **_kwargs):
            assert operation == "health"
            return self.response

    class FakeRunner:
        nodes = {"box-a": FakeNode()}

        def __init__(self, _config):
            pass

        @staticmethod
        def _normalize_hostname(value):
            normalized = value.strip().lower()
            return normalized.removesuffix(".local")

    monkeypatch.setattr(ci, "RemoteRunner", FakeRunner)
    ci._print_queue_progress(
        config, pending, "m-pending", 62, True,
        {"status": "SUBMITTED", "candidate": {"lease": {
            "box": "box-a", "state": "queued", "ticket": 42,
        }}},
    )

    line = capsys.readouterr().err
    assert "age=62s controller_queue=2/3 daemon=running" in line
    assert "box=box-a box_health=box-a:healthy,host=measured-node,load1=53,free=0" in line
    assert "ticket=42" in line

    FakeNode.response = {"status": "unknown", "reason": "admission-lock-timeout"}
    ci._print_queue_progress(
        config, pending, "m-pending", 92, True,
        {"status": "SUBMITTED", "candidate": {"lease": {
            "box": "box-a", "state": "queued", "ticket": 42,
        }}},
    )
    assert "box=box-a box_health=box-a:unknown" in capsys.readouterr().err


def test_health_probe_returns_unknown_when_admission_lock_is_busy(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    admission.mkdir(parents=True)
    monkeypatch.setattr(ci_node, "pin_managed_root", lambda path: Path(path))
    monkeypatch.setattr(ci_node, "HEALTH_LOCK_TIMEOUT_SECONDS", 0.05)
    request = {
        "managed_root": str(managed), "operation": "health",
        "p_cores": 8, "token_pool_size": 2,
    }

    with (admission / "queue.lock").open("a+") as held_lock:
        ci_node.fcntl.flock(held_lock, ci_node.fcntl.LOCK_EX)
        with _independent_deadline(2):
            result = ci_node._dispatch(request)
        ci_node.fcntl.flock(held_lock, ci_node.fcntl.LOCK_UN)

    assert result == {"status": "unknown", "reason": "admission-lock-timeout"}


def test_expired_queued_ticket_is_known_not_to_have_run(node_env):
    _config_value, _executor, runner, node = node_env
    arm = spec("expired-queued-ticket")
    record = {
        "state": "released",
        "result": {
            "status": "expired", "timed_out": True, "command_started": False,
        },
    }

    outcome = runner._resident_terminal_outcome(
        arm, node, {"run_dir": "/fake/run", "lease_token": "lease-token"}, record,
    )

    assert outcome is not None
    assert outcome.status == "timeout"
    assert outcome.remote_ticket_accepted is False
    assert ci._expired_queue_without_execution({"candidate": outcome.to_dict()})


def test_wrapper_queue_wait_has_a_separate_bounded_verdict(
    tmp_path, monkeypatch, capsys,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["admission"] = "box"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(ci, "SNAPSHOT_SETUP_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(ci, "BUNDLE_BASIS_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(ci, "BUNDLE_PREPARATION_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(ci, "BUNDLE_TRANSFER_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(ci, "_remote_result_budget_seconds", lambda *_a, **_k: 1000)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    monkeypatch.setattr(ci, "_print_queue_progress", lambda *_args: None)
    monkeypatch.setattr(
        ci, "_worktree_snapshot_identity",
        lambda _root: ("a" * 40, "b" * 40, False),
    )

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        payload["box"] = "box-a"
        destination = config.queue_dir / f"{payload['request_id']}.json"
        ci._atomic_write_json(destination, payload)
        return destination

    monkeypatch.setattr(ci, "submit_request", submit)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: pytest.fail("unresolved remote request ran locally"),
    )
    clock = {"now": 0.0}

    class Clock:
        @staticmethod
        def monotonic():
            return clock["now"]

        @staticmethod
        def sleep(_seconds):
            clock["now"] += 8

        @staticmethod
        def time():
            return 1000 + clock["now"]

    monkeypatch.setattr(ci, "time", Clock)
    result = _run_wrapper(monkeypatch, config_path, spool)

    assert result == ci.REMOTE_UNAVAILABLE_EXIT
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["route"] == "remote-unavailable"
    assert receipt["hostname"] == ci.socket.gethostname()
    assert "bounded queue window" in receipt["error"]


def test_blocking_wrapper_keeps_execution_deadline_while_command_runs(
    tmp_path, monkeypatch, capsys,
):
    config_path, spool = _wrapper_config(tmp_path)
    monkeypatch.setattr(ci, "SNAPSHOT_SETUP_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(ci, "BUNDLE_BASIS_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(ci, "BUNDLE_PREPARATION_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(ci, "BUNDLE_TRANSFER_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(ci, "_remote_result_budget_seconds", lambda *_a, **_k: 1000)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    monkeypatch.setattr(
        ci, "_print_queue_progress",
        lambda *_args: pytest.fail("blocking execution emitted queue progress"),
    )
    monkeypatch.setattr(
        ci, "_worktree_snapshot_identity",
        lambda _root: ("a" * 40, "b" * 40, False),
    )

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        destination = config.queue_dir / f"{payload['request_id']}.json"
        ci._atomic_write_json(destination, payload)
        return destination

    monkeypatch.setattr(ci, "submit_request", submit)
    clock = {"now": 0.0, "polls": 0}

    class Clock:
        @staticmethod
        def monotonic():
            return clock["now"]

        @staticmethod
        def sleep(_seconds):
            clock["now"] += 40

        @staticmethod
        def time():
            return 1000 + clock["now"]

    def daemon_state(_config):
        clock["polls"] += 1
        if clock["polls"] == 3:
            request = next((spool / "requests").glob("*.json"))
            payload = json.loads(request.read_text(encoding="utf-8"))
            ci._atomic_write_json(
                spool / "results" / request.name,
                _green_wrapper_result(payload["request_id"]),
            )
        return True

    monkeypatch.setattr(ci, "daemon_is_running", daemon_state)
    monkeypatch.setattr(ci, "time", Clock)

    assert _run_wrapper(monkeypatch, config_path, spool) == 0
    assert clock["now"] > 30
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "GREEN"


@pytest.mark.parametrize("fallback", ["local", "fail"])
def test_expired_unstarted_remote_ticket_respects_fallback_policy(
    tmp_path, monkeypatch, capsys, fallback,
):
    config_path, spool = _wrapper_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["admission"] = "box"
    raw["fallback"] = fallback
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    monkeypatch.setattr(ci, "_remote_result_budget_seconds", lambda *_a, **_k: 1000)
    monkeypatch.setattr(
        ci, "_worktree_snapshot_identity",
        lambda _root: ("a" * 40, "b" * 40, False),
    )
    local_calls = []

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        request_id = payload["request_id"]
        payload["_resident"] = {"arms": {"candidate": {
            "phase": "accepted", "box": "box-a",
            "ticket": {"run_dir": "/fake/run", "lease_token": "lease-token"},
        }}}
        request_path = config.queue_dir / f"{request_id}.json"
        ci._atomic_write_json(request_path, payload)
        ci._atomic_write_json(config.result_dir / f"{request_id}.json", {
            "request_id": request_id, "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": "candidate-timeout", "remote_ticket_accepted": False,
            "candidate": {
                "status": "timeout", "remote_ticket_accepted": False,
                "lease": {"result": {
                    "status": "expired", "timed_out": True,
                    "command_started": False,
                }},
            },
        })
        return request_path

    monkeypatch.setattr(ci, "submit_request", submit)
    def run_local(*_args, **kwargs):
        local_calls.append(kwargs)
        receipt = {
            "hostname": ci.socket.gethostname(), "route": kwargs["route"],
            "fallback_reason": kwargs["fallback_reason"],
        }
        print(json.dumps(receipt))
        return 0, receipt

    monkeypatch.setattr(ci, "_run_local_command", run_local)

    expected_exit = 0 if fallback == "local" else ci.REMOTE_UNAVAILABLE_EXIT
    assert _run_wrapper(monkeypatch, config_path, spool) == expected_exit

    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    if fallback == "local":
        assert len(local_calls) == 1
        assert local_calls[0]["route"] == "local-fallback"
        assert local_calls[0]["fallback_reason"] == "remote-admission-deadline"
        assert output["route"] == "local-fallback"
        assert output["hostname"] == ci.socket.gethostname()
    else:
        assert local_calls == []
        assert output["route"] == "remote-unavailable"
        assert output["fallback_reason"] == "remote-admission-deadline"
        assert output["hostname"] == ci.socket.gethostname()


def test_force_local_bypasses_daemon_and_records_local_route(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: pytest.fail("forced local probed daemon"))
    monkeypatch.setattr(ci, "submit_request", lambda *_: pytest.fail("forced local submitted spool"))

    assert _run_wrapper(monkeypatch, config_path, spool, force_local=True) == 0

    receipt = json.loads(next((spool / "requests").glob("*.local.json")).read_text())["receipt"]
    assert receipt["route"] == "local"
    assert receipt["route_reason"] == "forced-local"
    assert receipt["fallback_reason"] is None


def test_local_receipt_uses_identity_at_local_execution(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_wrapper_repo(repo)
    _declare_repo_identity(repo, "repo")
    monkeypatch.chdir(repo)
    config_path, spool = _wrapper_config(tmp_path / "config", repo_id="repo")
    changed = False

    def change_after_initial_identity():
        nonlocal changed
        if not changed:
            (repo / "tracked.txt").write_text("changed before local execution\n", encoding="utf-8")
            changed = True
        return ([0.1, 0.1, 0.1], 8)

    monkeypatch.setattr(ci, "_local_load", change_after_initial_identity)

    assert _run_wrapper(monkeypatch, config_path, spool, force_local=True) == 0

    receipt = json.loads(next((spool / "requests").glob("*.local.json")).read_text())["receipt"]
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    snapshot_sha = ci._snapshot_tree_sha(repo, head_sha)
    assert receipt["head_sha"] == head_sha
    assert receipt["candidate_sha"] == head_sha
    assert receipt["snapshot_sha"] == snapshot_sha
    assert receipt["dirty"] is True


def test_missing_config_runs_local_fallback_with_reason(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    config_path.unlink()

    assert _run_wrapper(monkeypatch, config_path, spool) == 0

    result = next((spool / "requests").glob("*.local.json"))
    receipt = json.loads(result.read_text())["receipt"]
    assert receipt["route"] == "local-fallback"
    assert receipt["route_reason"] == "configuration-unavailable"
    assert receipt["fallback_reason"].startswith("cannot read config")

def test_no_healthy_box_after_submit_refuses_local_run(tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path, repo_id="source")
    raw = json.loads(config_path.read_text())
    raw["admission"]["max_wait_seconds"] = 2
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "suite.py").write_text("print('suite')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "source")
    _declare_repo_identity(repo, "source")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    submit = ci.submit_request

    class UnhealthyRunner:
        def __init__(self, config):
            self.config = config

        def choose_box(self, _requested=None, **_kwargs):
            raise ci.NoHealthyBox("no matching host")

    def submit_and_process(source, config):
        queued = submit(source, config)
        ci.GateDaemon(config, runner=UnhealthyRunner(config)).poll_once()
        return queued

    monkeypatch.setattr(ci, "submit_request", submit_and_process)

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["fallback_reason"] == "remote-ownership-unresolved"
    assert not list((spool / "requests").glob("*.local.json"))

def test_busy_box_token_deadline_returns_unavailability(tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)

    def submit_capacity(source, config):
        request = json.loads(source.read_text())
        ci._atomic_write_json(config.result_dir / f"{request['request_id']}.json", {
            "schema": ci.RESULT_SCHEMA,
            "request_id": request["request_id"],
            "status": "LOCAL_FALLBACK_REQUIRED",
            "fallback_reason": "token-wait-expired",
        })
        return config.queue_dir / f"{request['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit_capacity)

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["fallback_reason"] == "remote-ownership-unresolved"
    assert "token-wait-expired" in receipt["error"]

def test_workload_receipt_ignores_pytest_shaped_stdout_without_junit_nodes(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GOALFLIGHT_REMOTE_CI_BOX": "studio-one",
        "GOALFLIGHT_REMOTE_CI_EXPECTED_HOSTNAME": "measured-node",
        "GOALFLIGHT_REMOTE_CI_TIP_SHA": "a" * 40,
        "GOALFLIGHT_REMOTE_CI_CANDIDATE_SHA": "b" * 40,
        "GOALFLIGHT_REMOTE_CI_HEAD_SHA": "d" * 40,
        "GOALFLIGHT_REMOTE_CI_SNAPSHOT_SHA": "b" * 40,
        "GOALFLIGHT_REMOTE_CI_DIRTY": "true",
        "GOALFLIGHT_REMOTE_CI_COMMAND_ID": "heavy-gate",
        "GOALFLIGHT_REMOTE_CI_ROUTE_REASON": "heavy-command-allowlist",
        "GOALFLIGHT_REMOTE_CI_REQUIREMENTS_SHA256": "c" * 64,
        "GOALFLIGHT_REMOTE_CI_VENV_PATH": "/var/lib/goalflight/remote-ci/venvs/default",
        "GOALFLIGHT_REMOTE_CI_PROFILE_RECEIPT_JSON": json.dumps({
            "environment_version": "Python 3.14.1",
            "receipt_probes": {"fixture_pin": "fixture-pin"},
        }),
    }
    spec_path = tmp_path / "workload.json"
    (tmp_path / "stdout").write_text("", encoding="utf-8")
    (tmp_path / "stderr").write_text("", encoding="utf-8")
    _write_workload_deadline(tmp_path)
    gate = tmp_path / "start"
    gate.touch()
    spec_path.write_text(json.dumps({
        "argv": [
            sys.executable, "-c",
            "sum(i*i for i in range(10000000)); print('109 passed in 54.68s')",
        ],
        "env": env, "emit_receipt": True,
    }), encoding="utf-8")

    with (tmp_path / "stdout").open("w", encoding="utf-8") as stdout, \
            (tmp_path / "stderr").open("w", encoding="utf-8") as stderr:
        result = subprocess.run(
            [sys.executable, "-c", ci_node._WORKLOAD_WRAPPER, str(spec_path), str(gate)],
            stdout=stdout, stderr=stderr, check=False, timeout=15,
        )

    assert result.returncode == 0
    receipt = json.loads((tmp_path / "stdout").read_text(encoding="utf-8").splitlines()[-1])
    assert receipt["passed"] == 0
    assert receipt["failed"] == 0
    assert receipt["skipped"] == 0
    assert receipt["errors"] == 0
    assert not ci._receipt_has_test_nodes(ci.parse_receipt(receipt))
    assert receipt["box_id"] == "studio-one"
    assert receipt["expected_hostname"] == "measured-node"
    assert receipt["measured_hostname"]
    assert receipt["tip_sha"] == "a" * 40 and receipt["candidate_sha"] == "b" * 40
    assert receipt["head_sha"] == "d" * 40
    assert receipt["snapshot_sha"] == "b" * 40 and receipt["dirty"] is True
    assert receipt["command_id"] == "heavy-gate"
    assert receipt["process_cpu_seconds"] > 0.05
    assert isinstance(receipt["wall_seconds"], (int, float))
    assert receipt["load_average_start"] and receipt["load_average_end"]
    assert receipt["route"] == "studio"
    assert receipt["requirements_sha256"] == "c" * 64
    assert receipt["profile"] == {
        "environment_version": "Python 3.14.1",
        "receipt_probes": {"fixture_pin": "fixture-pin"},
    }


def test_workload_receipt_catches_pipeline_red_when_tail_exits_zero(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    _write_workload_deadline(run)
    (run / "stdout").touch()
    (run / "stderr").touch()
    test_file = tmp_path / "test_deliberately_red.py"
    test_file.write_text("def test_red():\n    assert False\n", encoding="utf-8")
    command = (
        f"{shlex.quote(sys.executable)} -m pytest -q {shlex.quote(str(test_file))}"
        " | tail -n 1"
    )
    spec_path = run / "workload.json"
    gate = run / "start"
    gate.touch()
    spec_path.write_text(json.dumps({
        "cwd": str(tmp_path),
        "argv": ["/bin/bash", "-c", command],
        "env": {"PATH": os.environ.get("PATH", "")}, "emit_receipt": True,
    }), encoding="utf-8")

    with (run / "stdout").open("w", encoding="utf-8") as stdout, \
            (run / "stderr").open("w", encoding="utf-8") as stderr:
        result = subprocess.run(
            [sys.executable, "-c", ci_node._WORKLOAD_WRAPPER, str(spec_path), str(gate)],
            stdout=stdout, stderr=stderr, check=False, timeout=30,
        )

    receipt = json.loads((run / "stdout").read_text(encoding="utf-8").splitlines()[-1])
    assert result.returncode == 0  # `tail` masks pytest's failing process status.
    assert receipt["returncode"] == 0
    assert receipt["failed"] == 1
    assert receipt["status"] == "pass"  # The shell status is green, but JUnit records a red case.
    assert ci._remote_returncode({
        "status": "GREEN",
        "candidate": {
            "status": "green", "returncode": 0, "receipt": receipt,
        },
    }) == 1


@pytest.mark.parametrize(("test_source", "pytest_args", "expected_rc", "counts"), [
    (
        "import pytest\n@pytest.mark.xfail(reason='expected')\n"
        "def test_outcome():\n    assert False\n",
        [], 0, (0, 0, 1, 0),
    ),
    (
        "import pytest\n@pytest.mark.xfail(reason='unexpected pass')\n"
        "def test_outcome():\n    assert True\n",
        [], 0, (1, 0, 0, 0),
    ),
    (
        "import pytest\n@pytest.mark.xfail(reason='strict unexpected pass')\n"
        "def test_outcome():\n    assert True\n",
        ["-o", "xfail_strict=true"], 1, (0, 1, 0, 0),
    ),
])
def test_workload_receipt_counts_xfail_and_xpass_from_junit_nodes(
    tmp_path, test_source, pytest_args, expected_rc, counts,
):
    run = tmp_path / "run"
    run.mkdir()
    _write_workload_deadline(run)
    (run / "stdout").touch()
    (run / "stderr").touch()
    test_file = tmp_path / "test_outcome.py"
    test_file.write_text(test_source, encoding="utf-8")
    spec_path = run / "workload.json"
    gate = run / "start"
    gate.touch()
    spec_path.write_text(json.dumps({
        "cwd": str(tmp_path),
        "argv": [sys.executable, "-m", "pytest", "-q", *pytest_args, str(test_file)],
        "env": {"PATH": os.environ.get("PATH", "")}, "emit_receipt": True,
    }), encoding="utf-8")

    with (run / "stdout").open("w", encoding="utf-8") as stdout, \
            (run / "stderr").open("w", encoding="utf-8") as stderr:
        result = subprocess.run(
            [sys.executable, "-c", ci_node._WORKLOAD_WRAPPER, str(spec_path), str(gate)],
            stdout=stdout, stderr=stderr, check=False, timeout=30,
        )

    receipt = json.loads((run / "stdout").read_text(encoding="utf-8").splitlines()[-1])
    assert result.returncode == expected_rc
    assert (receipt["passed"], receipt["failed"], receipt["skipped"], receipt["errors"]) == counts
    parsed = ci.parse_receipt(receipt)
    assert ci._receipt_has_test_nodes(parsed)
    assert ci._receipt_is_green(parsed) is (expected_rc == 0)
    assert receipt["status"] == ("pass" if expected_rc == 0 else "fail")

def test_workload_receipt_includes_pytest_failure_details(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    _write_workload_deadline(run)
    (run / "stdout").write_text(
        "FAILED tests/captured.py::not_a_test - captured log\n"
        "ERROR tests/captured.py::not_a_test - captured log\n"
        "=========================== short test summary info ============================\n"
        "E   AssertionError: expected value\nE   details\n\n"
        "E   ImportError: missing module\n\n"
        "FAILED tests/test file.py::test value[param - x] - assertion failed\n"
        "ERROR tests/test_two.py::test_import - import failed\n"
        "ERROR collecting tests/test_three.py\n"
        "ERROR fixture teardown failed\n"
        "========================= 1 failed, 3 errors in 1.2s =========================\n",
        encoding="utf-8",
    )
    spec_path = run / "workload.json"
    gate = run / "start"
    gate.touch()
    spec_path.write_text(json.dumps({
        "argv": [sys.executable, "-c", "raise SystemExit(1)"],
        "env": {"PATH": os.environ.get("PATH", "")}, "emit_receipt": True,
    }), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", ci_node._WORKLOAD_WRAPPER, str(spec_path), str(gate)],
        capture_output=True, text=True, check=False, timeout=15,
    )

    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt["failure_summary"] == {
        "failing_node_ids": [
            "tests/test file.py::test value[param - x]", "tests/test_two.py::test_import",
        ],
        "collection_import_errors": ["tests/test_three.py"],
    }
    assert receipt["failing_node_ids"] == receipt["failure_summary"]["failing_node_ids"]
    assert receipt["collection_import_errors"] == receipt["failure_summary"]["collection_import_errors"]
    assert receipt["first_error_lines"] == [
        "ERROR tests/test_two.py::test_import - import failed",
        "ERROR collecting tests/test_three.py",
        "ERROR fixture teardown failed",
    ]
    assert receipt["failure_excerpt"] == [
        "E   AssertionError: expected value", "E   details", "E   ImportError: missing module",
    ]
    assert receipt["failure_summary_complete"] is True
    assert receipt["stdout_path"] == str((run / "stdout").resolve())


@pytest.mark.parametrize("stdout", [
    "=========================== short test summary info ============================\n"
    "FAILED tests/test.py::test_lost - failure\n" + "x" * 65536,
    "ordinary command output with no pytest summary\n",
])
def test_workload_receipt_marks_missing_summary_incomplete(tmp_path, stdout):
    run = tmp_path / "run"
    run.mkdir()
    _write_workload_deadline(run)
    (run / "stdout").write_text(stdout, encoding="utf-8")
    spec_path = run / "workload.json"
    gate = run / "start"
    gate.touch()
    spec_path.write_text(json.dumps({
        "argv": [sys.executable, "-c", "raise SystemExit(1)"],
        "env": {"PATH": os.environ.get("PATH", "")}, "emit_receipt": True,
    }), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", ci_node._WORKLOAD_WRAPPER, str(spec_path), str(gate)],
        capture_output=True, text=True, check=False, timeout=15,
    )

    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt["failure_summary"] == {
        "failing_node_ids": [], "collection_import_errors": [],
    }
    assert receipt["failure_summary_complete"] is False

def test_arm_result_reports_bundle_bytes():
    outcome = ci.ArmOutcome("candidate", "green", 0, None, bundle_bytes=1234)
    assert outcome.to_dict()["bundle_bytes"] == 1234

def test_released_lease_removes_its_remote_scratch(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    run = managed / "runs" / "lease-dead-holder"
    scratch = managed / "tmp" / "lease-dead-holder"
    run.mkdir(parents=True)
    scratch.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "lease_id": "lease-dead-holder", "state": "draining",
        "acquired_at": 1, "lease_token": "token",
    })
    ci_node.write_json(run / "owner.json", {})
    admission = managed / "admission"
    admission.mkdir()
    monkeypatch.setattr(ci_node, "clear_tree", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])
    monkeypatch.setattr(ci_node, "_remove_job", lambda _label: True)

    result = ci_node.finish_dead_workload(admission, run, {}, "reap")

    assert result["status"] == "cleared"
    assert ci_node.read_json(run / "lease.json")["state"] == "released"
    assert not scratch.exists()

def test_gc_removes_old_scratch_for_released_or_missing_leases(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    (managed / "tmp").mkdir(parents=True)
    released = managed / "tmp" / "released-lease"
    orphan = managed / "tmp" / "orphan-lease"
    released.mkdir()
    orphan.mkdir()
    released_lease = managed / "runs" / "released-lease"
    released_lease.mkdir(parents=True)
    ci_node.write_json(released_lease / "lease.json", {
        "lease_id": "released-lease", "state": "released",
        "acquired_at": 1, "deadline_epoch": 2,
    })
    old = time.time() - 9000
    os.utime(released, (old, old))
    os.utime(orphan, (old, old))
    monkeypatch.setattr(ci_node, "result_ids", lambda _managed: set())

    rows = ci_node.gc_run_bodies(managed, {}, True)

    removed = {Path(row["path"]).name for row in rows if row.get("class") == "SCRATCH"}
    assert removed == {"released-lease", "orphan-lease"}
    assert not released.exists() and not orphan.exists()

def _seed_gc_run(managed, run_id, *, acquired_at, returncode):
    run = managed / "runs" / run_id
    run.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "lease_id": run_id, "state": "released", "acquired_at": acquired_at,
    })
    ci_node.write_json(run / "result.json", {"returncode": returncode})
    index = managed / "results" / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with index.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"run_id": run_id, "finish": acquired_at}) + "\n")
    return run

def test_gc_count_caps_keep_all_run_bodies_younger_than_24_hours(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    now = time.time()
    runs = [
        _seed_gc_run(managed, f"young-{index}", acquired_at=now - 3600,
                     returncode=0)
        for index in range(60)
    ]
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])

    rows = ci_node.gc_run_bodies(managed, {}, True)

    assert not [row for row in rows if row.get("class") == "ELIGIBLE"]
    assert all(run.exists() for run in runs)

def test_gc_count_caps_trim_only_run_bodies_past_24_hours(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    now = time.time()
    runs = {}
    for kind, count, returncode in (("success", 60, 0), ("failure", 60, 1)):
        runs[kind] = [
            _seed_gc_run(managed, f"{kind}-{index}",
                         acquired_at=now - ci_node.RUN_BODY_RETENTION_FLOOR_SECONDS - 3600,
                         returncode=returncode)
            for index in range(count)
        ]
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])

    ci_node.gc_run_bodies(managed, {}, True)

    assert sum(run.exists() for run in runs["success"]) == ci_node.SUCCESS_KEEP
    assert sum(run.exists() for run in runs["failure"]) == ci_node.FAILURE_KEEP

def test_gc_never_evicts_a_live_lease_under_count_pressure(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    now = time.time()
    live = _seed_gc_run(managed, "live", acquired_at=now - 48 * 3600, returncode=0)
    live_lease = ci_node.read_json(live / "lease.json")
    live_lease["state"] = "running"
    ci_node.write_json(live / "lease.json", live_lease)
    released = [
        _seed_gc_run(managed, f"released-{index}", acquired_at=now - 48 * 3600,
                     returncode=0)
        for index in range(21)
    ]
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])

    ci_node.gc_run_bodies(managed, {}, True)

    assert live.exists()
    assert sum(run.exists() for run in released) == ci_node.SUCCESS_KEEP

def test_await_command_started_uses_existing_marker(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "command-started").touch()
    monkeypatch.setattr(ci_node, "_same_process", lambda *_args: True)
    assert ci_node._await_command_started(run, 42, [1, 2])

def test_project_test_venv_is_cached_by_requirements_hash(tmp_path):
    slot = tmp_path / "slot"
    slot.mkdir()
    requirements = slot / "requirements-test.txt"
    requirements.write_text("", encoding="utf-8")
    managed = tmp_path / "managed"

    first_path, first_hash = ci_node.prepare_venv(
        managed, "goal-flight", slot, "requirements-test.txt", sys.executable,
    )
    reused_path, reused_hash = ci_node.prepare_venv(
        managed, "goal-flight", slot, "requirements-test.txt", sys.executable,
    )
    requirements.write_text("# updated requirements\n", encoding="utf-8")
    changed_path, changed_hash = ci_node.prepare_venv(
        managed, "goal-flight", slot, "requirements-test.txt", sys.executable,
    )

    assert first_path == reused_path
    assert first_hash == reused_hash
    assert changed_path != first_path
    assert changed_hash != first_hash
    assert first_path.is_dir() and changed_path.is_dir()

def test_inline_requirements_recipe_is_cached_and_passed_as_argv(tmp_path, monkeypatch):
    calls = []

    def setup(argv, **kwargs):
        del kwargs
        calls.append(list(argv))
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, "Python 3.14.1\n", "")
        if argv[1:3] == ["-m", "venv"]:
            python = Path(argv[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ci_node, "_run_setup", setup)
    first, first_hash = ci_node.prepare_venv(
        tmp_path / "managed", "pm2", tmp_path / "checkout",
        ["numpy==2.4.6", "pytest==9.1.1"], sys.executable,
    )
    reused, reused_hash = ci_node.prepare_venv(
        tmp_path / "managed", "pm2", tmp_path / "checkout",
        ["numpy==2.4.6", "pytest==9.1.1"], sys.executable,
    )

    assert first == reused
    assert first_hash == reused_hash
    assert sum(call[1:3] == ["-m", "venv"] for call in calls) == 1
    assert [call[5:] for call in calls if call[1:4] == ["-m", "pip", "install"]] == [
        ["numpy==2.4.6", "pytest==9.1.1"]
    ]

@pytest.mark.parametrize("route", ["command", "targeted", "matched"])
@pytest.mark.parametrize("expected_probe", ["fixture-pin", "wrong-pin"])
def test_profile_setup_and_pins_apply_to_every_route_on_fake_node(
    tmp_path, route, expected_probe, monkeypatch,
):
    raw = _profile_raw_config(tmp_path, token_pool_size=1)
    raw["repo"] = "profile-project"
    raw["runner"]["env"]["PYTHONPATH"] = "runner-only-path"
    raw["environment"] = {
        "interpreter": sys.executable,
        "version_probe": [sys.executable, "--version"],
    }
    raw["never_route"] = ["tests/private"]
    raw["checkout_basename"] = "pm2"
    raw["boxes"]["box-a"]["env"]["PYTHONPATH"] = "profile-only-path"
    external = tmp_path / "external-input"
    external.mkdir()
    raw["receipt_probes"] = {
        "node_environment": {
            "command": [
                sys.executable, "-c",
                "import os; print('|'.join(os.environ.get(k, '') for k in "
                "('GIT_DIR', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS')))"
            ],
            "expected": "||",
        },
        "external_input": {
            "command": [sys.executable, "-c", "import sys; print(sys.argv[1])", "${PROFILE_INPUT}"],
            "expected": str(external),
        },
        "fixture_pin": {
            "command": [sys.executable, "-c", "print('fixture-pin')"],
            "expected": expected_probe,
        },
    }
    raw["boxes"]["box-a"]["env"]["PROFILE_INPUT"] = str(external)
    raw["local_env"] = {
        "PYTHONPATH": "local-profile-path",
        "PROFILE_INPUT": str(external),
    }
    raw["boxes"]["box-a"]["required_paths"] = ["${PROFILE_INPUT}"]
    launch_bin = tmp_path / "bin"
    launch_bin.mkdir()
    launchctl = launch_bin / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = list ]; then exit 0; fi\n"
        "echo 'service not loaded' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    launchctl.chmod(0o700)
    monkeypatch.setenv("PATH", str(launch_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("VIRTUAL_ENV", "/daemon/venv")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_VENV_PATH", "/daemon/venv")
    executor = ScriptedExecutor(tmp_path / "fake-node")

    source = tmp_path / "source"
    _init_repo(source)
    (source / ".gitignore").write_text(".goal-flight/\n", encoding="utf-8")
    (source / "__init__.py").write_text("ORIGIN = 'fixture'\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "private").mkdir()
    (source / "tests" / "private" / "test_private.py").write_text(
        "def test_private(): pass\n", encoding="utf-8",
    )
    (source / "tests" / "test_profile.py").write_text(
        "def test_profile():\n    assert True\n", encoding="utf-8",
    )
    config_path = source / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    config.queue_dir.mkdir(parents=True)
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "source")
    head = _git(source, "rev-parse", "HEAD").stdout.strip()
    (source / "snapshot-marker.txt").write_text("dirty snapshot\n", encoding="utf-8")
    snapshot = ci._snapshot_worktree(
        source, "profile-run", config.queue_dir,
        cwd_relative="", caller_env={},
    )
    node_git = tmp_path / "node-git"
    _init_repo(node_git)
    monkeypatch.setenv("GIT_DIR", str(node_git / ".git"))
    monkeypatch.setenv("PYTEST_ADDOPTS", "tests/rf/test_gate.py")
    monkeypatch.setenv("PYTEST_PLUGINS", "node-only-plugin")
    seen = {}

    def before(payload):
        if payload["operation"] == "start":
            seen.update(payload["command"])

    executor.before = before
    try:
        runner = RemoteRunner(config, executor=executor)
        caller_env = {"PYTHONPATH": "caller-only-path"}
        if route == "command":
            outcome = runner.run_arm(replace(
                spec("profile-run"),
                sha=snapshot["snapshot_sha"],
                tip_sha=head,
                candidate_sha=snapshot["snapshot_sha"],
                test_files=(),
                selection=(),
                command=("python3", "-m", "pytest", "tests/test_profile.py"),
                command_id="profile-smoke",
                head_sha=head,
                snapshot_sha=snapshot["snapshot_sha"],
                dirty=snapshot["dirty"],
                caller_env=caller_env,
                bundle_path=snapshot["bundle_path"],
                snapshot_root=snapshot["root"],
                snapshot_ref=snapshot["ref"],
            ))
        else:
            request = {
                "schema": ci.REQUEST_SCHEMA,
                "request_id": "profile-run",
                "project_id": config.repo,
                "kind": "targeted" if route == "targeted" else "gate",
                "tip_sha": head,
                "candidate_sha": snapshot["snapshot_sha"],
                "head_sha": head,
                "snapshot_sha": snapshot["snapshot_sha"],
                "dirty": snapshot["dirty"],
                "test_files": ["tests/test_profile.py"],
                "selection": ["tests/test_profile.py"],
                "caller_env": caller_env,
                "bundle_path": str(snapshot["bundle_path"]),
            }
            request = ci.validate_request(request, config)
            request["snapshot_root"] = str(snapshot["root"])
            request["snapshot_ref"] = snapshot["ref"]
            result = (ci.run_targeted(request, runner) if route == "targeted"
                      else ci.run_matched_pair(request, runner))
            outcome = result.get("candidate") or result["base"]

        lease = outcome.lease if isinstance(outcome, ci.ArmOutcome) else outcome["lease"]
        status = outcome.status if isinstance(outcome, ci.ArmOutcome) else outcome["status"]
        bundle_bytes = (outcome.bundle_bytes if isinstance(outcome, ci.ArmOutcome)
                        else outcome["bundle_bytes"])
        assert isinstance(bundle_bytes, int) and bundle_bytes > 0
        checkout = Path(lease["checkout_dir"])
        start_command = seen
        assert Path(start_command["cwd"]).name == "pm2"
        assert start_command["env"]["GOALFLIGHT_REMOTE_CI_PROFILE_ENABLED"] == "1"
        assert start_command["env"]["GOALFLIGHT_REMOTE_CI_EMIT_RECEIPT"] == "1"
        assert "never_route" not in start_command
        assert "never_route_version" not in start_command
        assert start_command["env"]["PYTHONPATH"].split(os.pathsep) == [
            str(checkout.parent), "profile-only-path",
        ]
        assert not lease.get("venv_path")
        if expected_probe == "fixture-pin":
            assert status == "setup-failed"
            assert "service not loaded" in lease["result"]["error"]
            workload_spec = json.loads((Path(lease["run_directory"])
                                        / "workload-spec.json").read_text(encoding="utf-8"))
            assert "VIRTUAL_ENV" not in workload_spec["env"]
            assert "GOALFLIGHT_REMOTE_CI_VENV_PATH" not in workload_spec["env"]
            assert "GIT_DIR" not in workload_spec["env"]
            assert "PYTEST_ADDOPTS" not in workload_spec["env"]
            assert "PYTEST_PLUGINS" not in workload_spec["env"]
            assert workload_spec["argv"][0] == sys.executable
            assert "caller-only-path" not in workload_spec["env"]["PYTHONPATH"]
        else:
            assert status == "setup-failed"
            assert "receipt probe 'fixture_pin' expected" in lease["result"]["error"]
            assert not (Path(lease["run_directory"]) / "workload-spec.json").exists()
    finally:
        monkeypatch.delenv("GIT_DIR", raising=False)
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
        ci._release_snapshot(source, snapshot["ref"], snapshot["snapshot_sha"], snapshot["bundle_path"])
        executor.close(config)

@pytest.mark.parametrize("probe_mode", ["pass", "mismatch", "missing"])
def test_declared_base_overlays_and_probes_run_before_workload(
    tmp_path, monkeypatch, probe_mode,
):
    raw = _profile_raw_config(tmp_path, token_pool_size=1)
    raw["repo"] = "example-sim"
    raw["fallback"] = "queue"
    raw["runner"]["command"] = ["python3", "-m", "pytest", "-n", "4"]
    raw["environment"] = {
        "interpreter": sys.executable,
        "version_probe": [sys.executable, "--version"],
    }
    box_inputs = tmp_path / "box-inputs"
    box_inputs.mkdir()
    docs_private = box_inputs / "docs-private"
    docs_private.mkdir()
    (docs_private / "fixture.md").write_text("private test input\n", encoding="utf-8")
    venv = box_inputs / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    example_dep = box_inputs / "example_dep"
    (example_dep / "src" / "example_dep").mkdir(parents=True)
    (example_dep / "src" / "example_dep" / "__init__.py").write_text(
        "fixture = True\n", encoding="utf-8",
    )
    _git(example_dep, "init", "--quiet")
    _git(example_dep, "add", "-A")
    _git(example_dep, "commit", "--quiet", "-m", "example_dep fixture")
    example_dep_commit = _git(example_dep, "rev-parse", "--short=7", "HEAD").stdout.strip()
    source_config = box_inputs / "config.toml"
    config_bytes = b"[dependency]\nfixture = true\n"
    source_config.write_bytes(config_bytes)
    expected_hash = hashlib.sha256(config_bytes).hexdigest()
    raw["boxes"]["box-a"]["env"].update({
        "EXAMPLE_DEP_CONFIG": str(source_config),
        "EXAMPLE_SIM_DOCS_PRIVATE": str(docs_private),
        "EXAMPLE_SIM_VENV": str(venv),
        "EXAMPLE_DEP_ROOT": str(example_dep),
        "PYTHONPATH": str(example_dep / "src"),
    })
    raw["boxes"]["box-a"]["required_paths"] = [
        "${EXAMPLE_DEP_CONFIG}", "${EXAMPLE_SIM_DOCS_PRIVATE}",
        "${EXAMPLE_SIM_VENV}/bin/python", "${EXAMPLE_DEP_ROOT}/src",
    ]
    raw["checkout_basename"] = "checkout"
    raw["base_overlays"] = [
        {
            "kind": "copy", "source": "${EXAMPLE_SIM_DOCS_PRIVATE}",
            "destination": "docs-private",
        },
        {
            "kind": "copy", "source": "${EXAMPLE_DEP_CONFIG}",
            "destination": "example-data/config.toml",
        },
        {
            "kind": "symlink", "source": "${EXAMPLE_SIM_VENV}",
            "destination": ".venv",
        },
    ]
    raw["receipt_probes"] = {
        "example_dep_config_sha256": {
            "command": [
                sys.executable, "-c",
                "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('example-data/config.toml').read_bytes()).hexdigest())",
            ],
            "expected": expected_hash,
        },
        "example_dep_module_file": {
            "command": (
                ["goalflight-missing-example_dep-probe"] if probe_mode == "missing"
                else [sys.executable, "-c", "import example_dep; print(example_dep.__file__)"]
            ),
            "expected": str(
                example_dep / "src" / "example_dep" / "__init__.py"
                if probe_mode == "pass" else box_inputs / "wrong.py"
            ),
        },
        "example_dep_tree_commit": {
            "command": ["git", "-C", "${EXAMPLE_DEP_ROOT}", "rev-parse", "--short=7", "HEAD"],
            "expected": example_dep_commit,
        },
    }
    launch_bin = tmp_path / "bin"
    launch_bin.mkdir()
    launchctl = launch_bin / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = list ]; then exit 0; fi\n"
        "echo 'service not loaded' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    launchctl.chmod(0o700)
    monkeypatch.setenv("PATH", str(launch_bin) + os.pathsep + os.environ.get("PATH", ""))
    executor = ScriptedExecutor(tmp_path / "fake-node")
    source = tmp_path / "source"
    _init_repo(source)
    (source / ".gitignore").write_text(
        ".goal-flight/\nexample-data/config.toml\n.docs-private/\n.venv\n",
        encoding="utf-8",
    )
    (source / "tests").mkdir()
    (source / "tests" / "test_profile.py").write_text("def test_profile(): pass\n", encoding="utf-8")
    config_path = source / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    config.queue_dir.mkdir(parents=True)
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "source")
    head = _git(source, "rev-parse", "HEAD").stdout.strip()
    snapshot = ci._snapshot_worktree(
        source, "example-sim-profile", config.queue_dir,
        cwd_relative="", caller_env={},
    )
    ci._create_snapshot_bundle(
        snapshot["root"], snapshot["ref"], snapshot["bundle_path"],
        snapshot["snapshot_sha"], None,
    )
    try:
        runner = RemoteRunner(config, executor=executor)
        outcome = runner.run_arm(replace(
            spec("example-sim-profile"),
            sha=snapshot["snapshot_sha"],
            tip_sha=head,
            candidate_sha=snapshot["snapshot_sha"],
            command=("python3", "-m", "pytest", "tests/test_profile.py"),
            head_sha=head,
            snapshot_sha=snapshot["snapshot_sha"],
            dirty=snapshot["dirty"],
            bundle_path=snapshot["bundle_path"],
        ))

        checkout = Path(outcome.lease["checkout_dir"])
        assert (checkout / "example-data" / "config.toml").read_bytes() == config_bytes
        assert (checkout / "docs-private" / "fixture.md").read_text() == "private test input\n"
        assert (checkout / ".venv").is_symlink()
        assert (checkout / ".venv").resolve() == venv.resolve()
        command = json.loads(
            (Path(outcome.lease["run_directory"]) / "command.json").read_text()
        )
        assert str(example_dep / "src") in command["env"]["PYTHONPATH"]
        assert outcome.status == "setup-failed"
        if probe_mode == "pass":
            assert "service not loaded" in outcome.lease["result"]["error"]
        else:
            assert outcome.setup_failure_type == "profile-probe-failed"
            assert outcome.lease["result"]["setup_failure_type"] == "profile-probe-failed"
            expected_error = "could not run" if probe_mode == "missing" else "expected"
            assert (
                f"receipt probe 'example_dep_module_file' {expected_error}"
                in outcome.lease["result"]["error"]
            )

        retained_marker = Path(outcome.lease["slot"]) / "retained" / "previous" / "proof.txt"
        retained_marker.parent.mkdir(parents=True)
        retained_marker.write_text("keep outside the checkout\n", encoding="utf-8")
        stale_sibling = Path(outcome.lease["slot"]) / "stale.txt"
        stale_sibling.write_text("remove between runs\n", encoding="utf-8")
        overlays = [
            {**overlay, "source": overlay["source"].replace(
                "${EXAMPLE_SIM_DOCS_PRIVATE}", str(docs_private),
            ).replace(
                "${EXAMPLE_DEP_CONFIG}", str(source_config),
            ).replace("${EXAMPLE_SIM_VENV}", str(venv))}
            for overlay in raw["base_overlays"]
        ]
        assert ci_node.prepare_slot(
            config.boxes["box-a"].managed_run_directory, config.repo,
            Path(outcome.lease["slot"]), snapshot["snapshot_sha"],
            checkout_basename="checkout", base_overlays=overlays,
            lease_path=Path(outcome.lease["run_directory"]) / "lease.json",
        )
        assert (checkout / "example-data" / "config.toml").read_bytes() == config_bytes
        assert (checkout / "docs-private" / "fixture.md").is_file()
        assert (checkout / ".venv").is_symlink()
        assert retained_marker.read_text(encoding="utf-8") == "keep outside the checkout\n"
        assert not stale_sibling.exists()
    finally:
        ci._release_snapshot(source, snapshot["ref"], snapshot["snapshot_sha"], snapshot["bundle_path"])
        executor.close(config)

@pytest.mark.parametrize(("location", "key"), [
    ("root", "retained_outputs"),
    ("root", "retention"),
    ("root", "output-retention"),
    ("root", "retained-output-paths"),
    ("runner", "outputRetention"),
])
def test_config_rejects_output_retention_keys(
    tmp_path, location, key,
):
    raw = _profile_raw_config(tmp_path)
    target = raw if location == "root" else raw[location]
    target[key] = {"enabled": True}
    config_path = tmp_path / "remote-ci.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ci.ConfigError) as caught:
        _load_fake_node_config(config_path)

    assert str(caught.value) == (
        "output retention is unsupported"
    )
    assert "\n" not in str(caught.value)


def test_example_sim_config_has_bounded_remote_policy():
    config_path = (
        Path(__file__).resolve().parents[2]
        / "configs" / "remote-ci-example-sim.example.json"
    )

    config = _load_fake_node_config(config_path)

    assert config.repo == "example-project"
    assert config.fallback == "queue"
    assert config.admission.max_wait_seconds == 30
    assert config.boxes["example-box"].max_concurrent_seats == 1
    assert config.boxes["example-box-2"].max_concurrent_seats == 0
    assert config.runner.pytest_workers_cap == 4
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert "retained_outputs" not in raw
    assert {overlay["destination"] for overlay in config.base_overlays} == {
        "private-data", "example-data/config.toml", ".venv",
    }

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_NAMESPACE"):
        env.pop(name, None)
    env.update({
        "GIT_AUTHOR_NAME": "Remote CI Test",
        "GIT_AUTHOR_EMAIL": "remote-ci@example.invalid",
        "GIT_COMMITTER_NAME": "Remote CI Test",
        "GIT_COMMITTER_EMAIL": "remote-ci@example.invalid",
    })
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env,
    )
    if check:
        assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result

def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")

def _pool_lease(managed: Path, lease_id: str, repo: str = "goal-flight"):
    run = managed / "runs" / lease_id
    run.mkdir(parents=True)
    token = f"token-{lease_id}"
    lease_path = run / "lease.json"
    lease_path.write_text(json.dumps({
        "lease_id": lease_id, "lease_token": token, "repo": repo, "state": "running",
    }), encoding="utf-8")
    return run, lease_path, token

def _pool_bundle(source: Path, run: Path, ref: str = "HEAD") -> Path:
    bundle = run / "source.bundle"
    _git(source, "bundle", "create", str(bundle), ref)
    return bundle

def test_snapshot_bundles_modified_and_untracked_files_without_changing_worker_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / ".gitignore").write_text("ignored.bin\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    (repo / "subdir").mkdir()
    (repo / "subdir" / "suite.txt").write_text("suite\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("final worktree value\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked value\n", encoding="utf-8")
    (repo / "ignored.bin").write_text("must not ship\n", encoding="utf-8")
    status_before = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    index_before = (repo / ".git" / "index").read_bytes()
    branch_before = _git(repo, "symbolic-ref", "HEAD").stdout.strip()
    heads_before = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads").stdout
    bundle_dir = tmp_path / "spool" / "requests"
    bundle_dir.mkdir(parents=True)

    snapshot = ci._snapshot_worktree(
        repo / "subdir", "snapshot-1", bundle_dir,
        cwd_relative="subdir", caller_env={},
    )
    try:
        assert snapshot["head_sha"] == head
        assert snapshot["snapshot_sha"] != head
        assert snapshot["dirty"] is True
        assert snapshot["cwd_relative"] == "subdir"
        assert _git(repo, "show", f"{snapshot['snapshot_sha']}:tracked.txt").stdout == "final worktree value\n"
        assert _git(repo, "show", f"{snapshot['snapshot_sha']}:untracked.txt").stdout == "untracked value\n"
        assert _git(repo, "cat-file", "-e", f"{snapshot['snapshot_sha']}:ignored.bin", check=False).returncode != 0
        bundle = ci._create_snapshot_bundle(
            snapshot["root"], snapshot["ref"], snapshot["bundle_path"],
            snapshot["snapshot_sha"], None,
        )
        _git(repo, "bundle", "verify", str(bundle))
        assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout == status_before
        assert (repo / ".git" / "index").read_bytes() == index_before
        assert _git(repo, "symbolic-ref", "HEAD").stdout.strip() == branch_before
        assert _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads").stdout == heads_before
        added_refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/goalflight/remote-ci").stdout.splitlines()
        assert added_refs == [snapshot["ref"]]
    finally:
        ci._release_snapshot(repo, snapshot["ref"], snapshot["snapshot_sha"], snapshot["bundle_path"])
    assert not snapshot["bundle_path"].exists()
    assert _git(repo, "for-each-ref", "--format=%(refname)", "refs/goalflight/remote-ci").stdout == ""

def test_snapshot_bundle_is_self_contained_on_first_contact(tmp_path):
    source = tmp_path / "source"
    _init_repo(source)
    (source / "source.txt").write_text("first contact\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "first")
    snapshot = ci._snapshot_worktree(
        source, "first-contact", tmp_path / "bundles",
        cwd_relative="", caller_env={},
    )
    try:
        bundle = ci._create_snapshot_bundle(
            snapshot["root"], snapshot["ref"], snapshot["bundle_path"],
            snapshot["snapshot_sha"], None,
        )
        mirror = tmp_path / "first-mirror.git"
        _git(tmp_path, "init", "--quiet", "--bare", str(mirror))
        assert _git(mirror, "bundle", "verify", str(bundle)).returncode == 0
    finally:
        ci._release_snapshot(source, snapshot["ref"], snapshot["snapshot_sha"],
                             snapshot["bundle_path"])

def test_first_contact_bundle_can_outlive_admission_deadline(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _init_repo(source)
    (source / "source.txt").write_text("first contact\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "first contact")
    snapshot = ci._snapshot_worktree(
        source, "slow-first-contact", tmp_path / "spool" / "requests",
        cwd_relative="", caller_env={},
    )
    mirror = tmp_path / "mirror.git"
    _git(tmp_path, "init", "--quiet", "--bare", str(mirror))
    config = replace(
        _config(tmp_path),
        admission=ci.AdmissionConfig(0.01, 2.0),
    )
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    base_node = runner.nodes["box-a"]
    monkeypatch.setattr(
        runner, "choose_box", lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )
    admission_deadline = time.monotonic() + 0.2
    admitted = {
        "run_directory": str(tmp_path / "run"),
        "lease_token": "lease-token",
        "lease_id": "lease-id",
        **LEASE_ROUTE,
        "owner_identity": "owner",
        "state": "admitted",
        "sample": {"self_cap": 4},
        "slot": str(tmp_path / "slot"),
        "checkout_dir": str(tmp_path / "slot"),
        "token_index": 0,
        "command_present": False,
    }
    # The real enqueue RPC always returns queued; admission is learned from
    # the follow-up status call even if the holder admitted in the meantime.
    queued = {**admitted, "state": "queued"}
    calls = []

    class Node:
        box = base_node.box
        pinned_host = None

        def with_pinned_host(self, host):
            self.pinned_host = host
            return self

        def call(self, operation, **kwargs):
            calls.append((operation, kwargs))
            if operation == "enqueue":
                time.sleep(0.35)
                return dict(queued)
            if operation == "status":
                return dict(admitted)
            if operation == "bundle-basis":
                assert kwargs["_transport_timeout"] == ci.BUNDLE_BASIS_TIMEOUT_SECONDS
                time.sleep(0.2)
                return {}
            if operation == "start":
                return {"status": "started"}
            raise AssertionError(f"unexpected operation: {operation}")

        def transfer_bundle(self, bundle_path, run_dir):
            assert _git(mirror, "bundle", "verify", str(bundle_path)).returncode == 0
            return str(Path(run_dir) / "source.bundle")

    node = Node()
    runner.nodes["box-a"] = node
    real_create = ci._create_snapshot_bundle

    def slow_first_bundle(*args, **kwargs):
        time.sleep(0.15)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(ci, "_create_snapshot_bundle", slow_first_bundle)
    outcome = ci.ArmOutcome("candidate", "green", 0, None)
    monkeypatch.setattr(runner, "_watch", lambda *_args: outcome)
    arm = replace(
        spec("slow-first-contact"),
        snapshot_root=snapshot["root"],
        snapshot_ref=snapshot["ref"],
        snapshot_sha=snapshot["snapshot_sha"],
        bundle_path=snapshot["bundle_path"],
    )

    try:
        result = runner.run_arm(arm, admission_deadline=admission_deadline)

        assert time.monotonic() > admission_deadline
        assert result.status == "green"
        assert result.bundle_bytes == snapshot["bundle_path"].stat().st_size > 0
        assert [operation for operation, _kwargs in calls] == [
            "enqueue", "status", "bundle-basis", "start",
        ]
    finally:
        ci._release_snapshot(
            source, snapshot["ref"], snapshot["snapshot_sha"],
            snapshot["bundle_path"],
        )

def test_warm_snapshot_bundle_requires_and_fetches_confirmed_basis(tmp_path):
    source = tmp_path / "source"
    _init_repo(source)
    (source / "source.txt").write_text("first\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "first")
    basis = _git(source, "rev-parse", "HEAD").stdout.strip()
    (source / "source.txt").write_text("second\n", encoding="utf-8")
    _git(source, "commit", "--quiet", "-am", "second")
    snapshot = ci._snapshot_worktree(
        source, "warm", tmp_path / "bundles", cwd_relative="", caller_env={},
    )
    try:
        incremental = ci._create_snapshot_bundle(
            snapshot["root"], snapshot["ref"], snapshot["bundle_path"],
            snapshot["snapshot_sha"], basis,
        )
        seed = tmp_path / "seed.bundle"
        _git(source, "update-ref", "refs/heads/bundle-basis", basis)
        _git(source, "bundle", "create", str(seed), "refs/heads/bundle-basis")
        mirror = tmp_path / "warm-mirror.git"
        _git(tmp_path, "init", "--quiet", "--bare", str(mirror))
        _git(mirror, "fetch", "--no-tags", str(seed), basis)
        assert _git(mirror, "bundle", "verify", str(incremental)).returncode == 0
        empty_mirror = tmp_path / "empty-mirror.git"
        _git(tmp_path, "init", "--quiet", "--bare", str(empty_mirror))
        assert _git(empty_mirror, "bundle", "verify", str(incremental), check=False).returncode != 0
    finally:
        ci._release_snapshot(source, snapshot["ref"], snapshot["snapshot_sha"],
                             snapshot["bundle_path"])

def test_uncertain_bundle_basis_falls_back_to_full_history(tmp_path):
    source = tmp_path / "source"
    _init_repo(source)
    (source / "source.txt").write_text("content\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "source")
    snapshot = ci._snapshot_worktree(
        source, "uncertain", tmp_path / "bundles", cwd_relative="", caller_env={},
    )
    try:
        bundle = ci._create_snapshot_bundle(
            snapshot["root"], snapshot["ref"], snapshot["bundle_path"],
            snapshot["snapshot_sha"], "f" * 40,
        )
        mirror = tmp_path / "uncertain-mirror.git"
        _git(tmp_path, "init", "--quiet", "--bare", str(mirror))
        assert _git(mirror, "bundle", "verify", str(bundle)).returncode == 0
    finally:
        ci._release_snapshot(source, snapshot["ref"], snapshot["snapshot_sha"],
                             snapshot["bundle_path"])

def test_divergent_bundle_basis_falls_back_to_full_history(tmp_path):
    source = tmp_path / "source"
    _init_repo(source)
    (source / "source.txt").write_text("main history\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "main")
    divergent = tmp_path / "divergent"
    _init_repo(divergent)
    (divergent / "other.txt").write_text("other history\n", encoding="utf-8")
    _git(divergent, "add", "-A")
    _git(divergent, "commit", "--quiet", "-m", "other")
    basis = _git(divergent, "rev-parse", "HEAD").stdout.strip()
    _git(source, "fetch", str(divergent), basis)
    snapshot = ci._snapshot_worktree(
        source, "divergent", tmp_path / "bundles", cwd_relative="", caller_env={},
    )
    try:
        bundle = ci._create_snapshot_bundle(
            snapshot["root"], snapshot["ref"], snapshot["bundle_path"],
            snapshot["snapshot_sha"], basis,
        )
        mirror = tmp_path / "divergent-mirror.git"
        _git(tmp_path, "init", "--quiet", "--bare", str(mirror))
        assert _git(mirror, "bundle", "verify", str(bundle)).returncode == 0
        assert _git(mirror, "cat-file", "-e", f"{basis}^{{commit}}", check=False).returncode != 0
    finally:
        ci._release_snapshot(source, snapshot["ref"], snapshot["snapshot_sha"],
                             snapshot["bundle_path"])

def test_node_checks_out_the_uploaded_source_bundle_without_origin_access(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet")
    (source / "parent.txt").write_text("snapshot parent\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "parent")
    parent_sha = _git(source, "rev-parse", "HEAD").stdout.strip()
    (source / "suite.txt").write_text("bundled source\n", encoding="utf-8")
    (source / "clone-proof.bin").write_bytes(b"clone data\n" * 16384)
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "source")
    sha = _git(source, "rev-parse", "HEAD").stdout.strip()
    managed = tmp_path / "managed"
    lease_id = "a" * 32
    run, lease_path, _token = _pool_lease(managed, lease_id)
    bundle = _pool_bundle(source, run)
    slot = managed / "repos" / "goal-flight" / "seats" / "s-01"

    assert ci_node.prepare_slot(
        managed, "goal-flight", slot, sha, source_bundle=str(bundle),
        checkout_basename="checkout", run=run, lease_path=lease_path,
    )

    checkout = slot / "checkout"
    assert (checkout / "suite.txt").read_text() == "bundled source\n"
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == sha
    assert _git(checkout, "rev-parse", "HEAD^").stdout.strip() == parent_sha
    assert (checkout / ".git" / "index").is_file()
    assert (checkout / ".git" / "objects" / "info" / "alternates").read_text().strip() == str(
        managed / "repos" / "goal-flight" / "mirror.git" / "objects"
    )
    assert _git(checkout, "rev-parse", "--is-shallow-repository").stdout.strip() == "false"
    base_file = (managed / "repos" / "goal-flight" / "bases" / sha
                 / "checkout" / "clone-proof.bin")
    assert seat_dedupe._private_bytes(base_file) == 0
    assert seat_dedupe._private_bytes(checkout / "clone-proof.bin") == 0
    assert not bundle.exists()

def test_remote_node_streams_bundle_bytes_through_the_configured_transport(tmp_path):
    config = _config(tmp_path)
    captured = []
    payload = b"git bundle\x00bytes"
    bundle = tmp_path / "source.bundle"
    bundle.write_bytes(payload)

    def executor(argv, env, timeout, **kwargs):
        captured.append((argv, timeout, kwargs.get("input_data")))
        return CommandResult(0)

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    remote_path = node.transfer_bundle(bundle, "/managed/runs/run-1")

    assert remote_path == "/managed/runs/run-1/source.bundle"
    assert captured[0][1] == 300
    assert captured[0][2] == payload
    assert shlex.split(captured[0][0][2]) == ["cat", ">", remote_path]

def test_bundle_ship_failure_is_a_remote_setup_error(tmp_path):
    config = _config(tmp_path)
    bundle = tmp_path / "source.bundle"
    bundle.write_bytes(b"git bundle")

    def executor(argv, env, timeout, **kwargs):
        return CommandResult(255, stderr="ssh dropped during bundle transfer")

    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    with pytest.raises(ci.RemoteCIError, match="could not ship source bundle"):
        node.transfer_bundle(bundle, "/managed/runs/run-1")

def test_wrapper_refuses_local_run_when_submission_write_fails(tmp_path, monkeypatch, capsys):
    config_path, spool = _wrapper_config(tmp_path)
    requests = spool / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    (spool / "results").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)

    def denied(*_args):
        raise PermissionError("spool write denied")

    monkeypatch.setattr(ci, "submit_request", denied)

    assert _run_wrapper(monkeypatch, config_path, spool) == ci.REMOTE_UNAVAILABLE_EXIT

    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["route"] == "remote-unavailable"
    assert receipt["fallback_reason"] == "remote-ownership-unresolved"
    assert "spool write denied" in receipt["error"]

def test_wrapper_forwards_cwd_and_records_requested_tree_identity(tmp_path, monkeypatch):
    config_path, spool = _wrapper_config(tmp_path)
    captured = {}
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    monkeypatch.setenv("EXERCISE_FAIL", "1")
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", "dispatch-wrapper")
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "beta")

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        captured.update(payload)
        ci.validate_request(payload, config)
        ci._atomic_write_json(
            config.result_dir / f"{payload['request_id']}.json",
            _green_wrapper_result(payload["request_id"]),
        )
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    assert _run_wrapper(monkeypatch, config_path, spool) == 0

    assert captured["repo_root"] == str(Path.cwd().resolve())
    assert captured["cwd_relative"] == ""
    assert captured["caller_env"] == {
        name: os.environ[name] for name in ci.REMOTE_ENV_ALLOWLIST if name in os.environ
    }
    assert captured["receipt_metadata"] == {
        "dispatch_id": "dispatch-wrapper", "controller_label": "beta",
    }
    command_env = ci._build_command_environment(
        captured["caller_env"], {}, {}, profile_enabled=False,
    )
    assert "GOALFLIGHT_DISPATCH_ID" not in command_env
    assert "GOALFLIGHT_CONTROLLER_LABEL" not in command_env
    assert captured["caller_identity"] == ci.goalflight_compat.process_start_identity(os.getpid())
    assert captured["head_sha"] == ci._resolve_commit("HEAD", Path.cwd())
    assert len(captured["snapshot_sha"]) == 40
    assert isinstance(captured["dirty"], bool)
    assert not {"candidate_sha", "tip_sha", "bundle_path", "base_sha"} & captured.keys()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_wrapper_marks_request_cancelled_on_termination_signal(
    tmp_path, monkeypatch, signum,
):
    config_path, spool = _wrapper_config(tmp_path)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: True)
    monkeypatch.setattr(ci, "_local_load", lambda: ([0.1], 8))
    captured = {}

    def submit(source, config):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        captured.update(payload)
        return config.queue_dir / f"{payload['request_id']}.json"

    monkeypatch.setattr(ci, "submit_request", submit)
    handlers = {}
    original_signal = signal.signal

    def capture_handler(selected, handler):
        prior = original_signal(selected, handler)
        if callable(handler):
            handlers[selected] = handler
        return prior

    monkeypatch.setattr(ci.signal, "signal", capture_handler)
    original_sleep = time.sleep

    def deliver_signal(_seconds):
        if signum in handlers:
            handlers[signum](signum, None)
        original_sleep(_seconds)

    monkeypatch.setattr(ci.time, "sleep", deliver_signal)
    with pytest.raises(SystemExit, match=str(128 + signum)):
        _run_wrapper(monkeypatch, config_path, spool)

    config = ci._load_local_config(config_path)
    marker = ci._request_cancel_path(config, captured["request_id"])
    assert json.loads(marker.read_text(encoding="utf-8"))["reason"] == (
        f"wrapper-{signal.Signals(signum).name.lower()}"
    )


def test_red_matched_pair_exits_nonzero_when_base_fails():
    assert ci._remote_returncode({
        "status": "RED", "base": {"returncode": 7}, "candidate": {"returncode": 0},
    }) == 7
    assert ci._remote_returncode({
        "status": "RED", "base": {"returncode": 0}, "candidate": {"returncode": 0},
    }) == 1

def test_base_capacity_refusal_does_not_run_candidate(tmp_path):
    config = _two_box_config(tmp_path)

    class CapacityRunner(SuccessfulCommandRunner):
        def run_arm(self, arm_spec, *, admission_deadline=None):
            self.calls.append(arm_spec)
            return ci.ArmOutcome(arm_spec.arm, "capacity", 75, None)

    runner = CapacityRunner(config)
    runner.choose_box = lambda requested=None, **_kwargs: requested or "studio-two"
    result = ci.run_matched_pair({
        "request_id": "base-busy", "kind": "gate", "box": None,
        "tip_sha": "a" * 40, "candidate_sha": "b" * 40,
        "test_files": ["tests/test_one.py"], "selection": ["tests/test_one.py"],
    }, runner)

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert [arm.arm for arm in runner.calls] == ["base"]

def test_matched_pair_candidate_gets_fresh_admission_window_after_base(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config = replace(
        config,
        admission=replace(config.admission, max_wait_seconds=0.2),
    )
    clock = {"now": 100.0}
    monkeypatch.setattr(ci, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    submitted_deadline = clock["now"] + 0.1

    class DelayedBaseRunner(SuccessfulCommandRunner):
        def __init__(self, runner_config):
            super().__init__(runner_config)
            self.deadlines = []
            self.base_finished_at = None

        def run_arm(self, arm_spec, *, admission_deadline=None):
            self.deadlines.append(admission_deadline)
            if arm_spec.arm == "base":
                clock["now"] = submitted_deadline + 0.01
            outcome = super().run_arm(arm_spec, admission_deadline=admission_deadline)
            if arm_spec.arm == "base":
                self.base_finished_at = clock["now"]
            return outcome

    runner = DelayedBaseRunner(config)
    result = ci.run_matched_pair({
        "request_id": "fresh-candidate-window", "project_id": config.repo,
        "kind": "gate", "box": "box-a", "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py"],
        "_admission_deadline": submitted_deadline,
    }, runner)

    assert result["status"] == "GREEN"
    assert runner.deadlines[0] == submitted_deadline
    assert runner.base_finished_at > submitted_deadline
    assert runner.deadlines[1] > runner.base_finished_at
    assert runner.deadlines[1] - runner.base_finished_at == pytest.approx(0.2)
    assert [arm.box for arm in runner.calls] == ["box-a", "box-a"]

def test_single_command_retries_another_box_after_capacity_refusal(tmp_path, monkeypatch):
    config = _two_box_config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_args: None)
    runner.calls = []
    runner.deadlines = []
    for name, node in runner.nodes.items():
        monkeypatch.setattr(node, "call", lambda operation, _name=name, **_kwargs: {
            "load": {"hostname": "measured-node", "load1": 1.0,
                     "cpu_busy_fraction": 0.1},
            "peer_address": "192.0.2.10",
            "tokens": {"free": 1},
        } if operation == "health" else {})

    def run_arm(arm_spec, *, admission_deadline=None):
        runner.deadlines.append(admission_deadline)
        if len(runner.deadlines) == 1:
            runner.calls.append(arm_spec)
            return ci.ArmOutcome(arm_spec.arm, "capacity", 75, None)
        return SuccessfulCommandRunner.run_arm(
            runner, arm_spec, admission_deadline=admission_deadline,
        )

    monkeypatch.setattr(runner, "run_arm", run_arm)
    result = ci.run_remote_command(_command_request("retry-capacity"), runner)

    assert result["status"] == "GREEN"
    assert [arm.box for arm in runner.calls] == ["studio-one", "studio-two"]
    assert len(runner.deadlines) == 2
    assert runner.deadlines[0] < runner.deadlines[1]


def test_local_fallback_honors_the_configured_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", "dispatch-local-1")
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "beta-main")
    result_dir = tmp_path / "spool" / "requests"
    code, _receipt = ci._run_local_command(
        [sys.executable, "-c", "import time; time.sleep(5)", "--token=SYNTHETIC_SECRET"],
        request_id="timeout-local", command_id="heavy-gate",
        candidate_sha="a" * 40, route="local-fallback", route_reason="test",
        fallback_reason="test-timeout", result_dir=result_dir, timeout=0.05,
        cwd=tmp_path,
    )
    assert code == 124
    receipt = json.loads((result_dir / "timeout-local.local.json").read_text())
    assert receipt["receipt"]["returncode"] == 124
    assert receipt["receipt"]["dispatch_id"] == "dispatch-local-1"
    assert receipt["receipt"]["controller_label"] == "beta-main"
    assert len(receipt["receipt"]["command_sha"]) == 64
    assert b"SYNTHETIC_SECRET" not in (result_dir / "timeout-local.local.json").read_bytes()

def test_setup_subprocess_is_killed_at_its_deadline(tmp_path):
    started = time.monotonic()
    with pytest.raises(ci_node.SetupDeadlineExceeded, match="setup deadline"):
        ci_node._run_setup(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            deadline=started + 0.5,
        )
    assert time.monotonic() - started < 20


def test_setup_cleanup_bounds_waits_after_sigkill(monkeypatch):
    class Process:
        pid = 4246

        def __init__(self):
            self.wait_timeouts = []

        def poll(self):
            return None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(["fake"], timeout)

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(["fake"], timeout)

    process = Process()
    monkeypatch.setattr(ci_node.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(ci_node.os, "killpg", lambda *_args: None)
    with pytest.raises(ci_node.SetupDeadlineExceeded):
        ci_node._run_setup(["fake"], deadline=time.monotonic() + 0.02)

    assert process.wait_timeouts == [1, ci_node.PROCESS_REAP_TIMEOUT_SECONDS]
    ci_node._stop_setup_child(process)
    assert process.wait_timeouts[-2:] == [0.1, ci_node.PROCESS_REAP_TIMEOUT_SECONDS]


def test_clone_cleanup_bounds_communicate_after_sigkill(monkeypatch, tmp_path):
    class Process:
        pid = 4247

        def __init__(self):
            self.wait_timeouts = []
            self.communicate_timeouts = []

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(["fake"], timeout)

        def communicate(self, timeout=None):
            self.communicate_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(["fake"], timeout)

    process = Process()
    deadline_checks = iter((None, ci_node.SetupDeadlineExceeded("setup deadline expired")))
    monkeypatch.setattr(ci_node.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(ci_node.os, "killpg", lambda *_args: None)

    def check_deadline(*_args, **_kwargs):
        result = next(deadline_checks)
        if isinstance(result, BaseException):
            raise result

    monkeypatch.setattr(ci_node, "_check_setup_deadline", check_deadline)

    with pytest.raises(ci_node.SetupDeadlineExceeded):
        ci_node._run_clonefile_process(
            tmp_path / "source", tmp_path / "destination", deadline=time.monotonic() + 10,
        )

    assert process.wait_timeouts == [0.1, ci_node.PROCESS_REAP_TIMEOUT_SECONDS]
    assert process.communicate_timeouts == [ci_node.PROCESS_REAP_TIMEOUT_SECONDS]


def test_timed_out_transport_reaping_runs_off_the_caller_thread(monkeypatch):
    class Stream:
        def close(self):
            pass

    class Process:
        pid = 4242
        returncode = 124

        def __init__(self):
            self.stdout = Stream()
            self.stderr = Stream()
            self.calls = 0
            self.reaper_started = threading.Event()

        def poll(self):
            return None

        def communicate(self, timeout=None, input=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["fake"], timeout)
            assert threading.current_thread() is not threading.main_thread()
            self.reaper_started.set()
            return b"", b""

    process = Process()
    monkeypatch.setattr(ci.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(ci.goalflight_compat, "process_start_identity",
                        lambda pid: {"pid": pid, "start_token": "old"})
    monkeypatch.setattr(ci.goalflight_compat, "process_identity_matches", lambda *_: True)
    monkeypatch.setattr(ci.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(ci.os, "killpg", lambda *_: None)

    result = ci.run_command(["fake"], timeout=0.1)

    assert result.timed_out
    assert not result.unknown
    assert process.reaper_started.wait(10)


def test_background_transport_reaper_bounds_communicate_after_sigkill(monkeypatch):
    class Process:
        pid = 4244

        def __init__(self):
            self.timeouts = []
            self.finished = threading.Event()

        def communicate(self, timeout=None):
            self.timeouts.append(timeout)
            if len(self.timeouts) == 1:
                raise subprocess.TimeoutExpired(["fake"], timeout)
            self.finished.set()
            raise subprocess.TimeoutExpired(["fake"], timeout)

    process = Process()
    monkeypatch.setattr(
        ci, "_signal_process_group",
        lambda *_args: True,
    )
    ci._start_process_reaper(process, 4244, {"pid": 4244, "start_token": "start"})

    assert process.finished.wait(1)
    assert process.timeouts == [5, ci.REMOTE_PROCESS_REAP_TIMEOUT_SECONDS]


def test_local_fallback_bounds_wait_after_sigkill(tmp_path, monkeypatch):
    class Process:
        pid = 4245
        returncode = None

        def __init__(self):
            self.wait_timeouts = []

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(["fake"], timeout)

    process = Process()
    signals = []
    monkeypatch.setattr(ci.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(ci.goalflight_compat, "process_start_identity",
                        lambda pid: {"pid": pid, "start_token": "start"})
    monkeypatch.setattr(ci.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        ci, "_signal_process_group",
        lambda _process, _group, _identity, sig: signals.append(sig) or True,
    )

    code, _receipt = ci._run_local_command(
        [sys.executable, "-c", "pass"], request_id="bounded-local-reap",
        command_id="heavy-gate", candidate_sha="a" * 40,
        route="local-fallback", route_reason="test", fallback_reason="test-timeout",
        result_dir=tmp_path / "results", timeout=0.01, cwd=tmp_path,
    )

    assert code == 124
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.wait_timeouts == [0.01, 5, ci.REMOTE_PROCESS_REAP_TIMEOUT_SECONDS]


def test_streamed_transport_bounds_joins_after_subprocess_exit(monkeypatch):
    monkeypatch.setattr(ci, "REMOTE_STREAM_JOIN_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    result = ci.run_command(
        [sys.executable, "-c",
         "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
         "'import time; time.sleep(0.4)']); print('done', flush=True)"],
        timeout=2, input_stream=io.BytesIO(b""),
    )

    assert time.monotonic() - started < 1
    assert result.timed_out and result.unknown
    assert "transport streams did not close" in result.stderr


def test_streamed_transport_bounds_process_reaping_after_sigkill(monkeypatch):
    class Process:
        pid = 4243
        returncode = None

        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.wait_timeouts = []
            self.reaper_done = threading.Event()

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if threading.current_thread() is not threading.main_thread():
                self.reaper_done.set()
            raise subprocess.TimeoutExpired(["fake"], timeout)

    process = Process()
    signals = []
    monkeypatch.setattr(ci.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(ci.goalflight_compat, "process_start_identity",
                        lambda pid: {"pid": pid, "start_token": "start"})
    monkeypatch.setattr(ci.os, "getpgid", lambda _pid: 4243)
    monkeypatch.setattr(ci, "_signal_process_group",
                        lambda _process, _group, _identity, sig: signals.append(sig) or True)

    result = ci.run_command(
        ["fake"], timeout=0.01, input_stream=io.BytesIO(b"body"),
    )

    assert result.timed_out and result.unknown
    assert signals[:2] == [signal.SIGTERM, signal.SIGKILL]
    assert process.reaper_done.wait(1)
    assert process.wait_timeouts[:3] == [0.01, 2, ci.REMOTE_PROCESS_REAP_TIMEOUT_SECONDS]
    assert all(timeout is not None for timeout in process.wait_timeouts)


def test_venv_hashes_nested_requirements_and_keeps_console_script_shebang(tmp_path, monkeypatch):
    slot = tmp_path / "slot"
    slot.mkdir()
    (slot / "requirements-test.txt").write_text("-r requirements.txt\n", encoding="utf-8")
    requirements = slot / "requirements.txt"
    requirements.write_text("pytest==9.0.3\n", encoding="utf-8")

    def fake_setup(argv, *, cwd=None, run=None, deadline=None):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "Python fake 3.14\n", "")
        if argv[1:3] == ["-m", "venv"]:
            venv = Path(argv[3])
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").write_text("python", encoding="utf-8")
        elif argv[1:4] == ["-m", "pip", "install"]:
            script = Path(argv[0]).parent / "pytest"
            script.write_text(f"#!{argv[0]}\n", encoding="utf-8")
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ci_node, "_run_setup", fake_setup)
    managed = tmp_path / "managed"
    first, first_hash = ci_node.prepare_venv(
        managed, "goal-flight", slot, "requirements-test.txt", sys.executable,
    )
    script = first / "bin" / "pytest"
    assert script.read_text().splitlines()[0] == f"#!{first / 'bin' / 'python'}"
    requirements.write_text("pytest==9.0.4\n", encoding="utf-8")
    second, second_hash = ci_node.prepare_venv(
        managed, "goal-flight", slot, "requirements-test.txt", sys.executable,
    )
    assert second_hash != first_hash
    assert second != first
    assert (second / "bin" / "pytest").read_text().splitlines()[0] == f"#!{second / 'bin' / 'python'}"

def test_prepare_slot_removes_ignored_files_between_runs(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _init_repo(source)
    (source / ".gitignore").write_text("ignored-output\n", encoding="utf-8")
    (source / "source.txt").write_text("source\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "source")
    sha = _git(source, "rev-parse", "HEAD").stdout.strip()
    managed = tmp_path / "managed"
    lease_id = "b" * 32
    run, lease_path, _token = _pool_lease(managed, lease_id)
    bundle = _pool_bundle(source, run)
    slot = managed / "repos" / "goal-flight" / "seats" / "s-01"

    assert ci_node.prepare_slot(
        managed, "goal-flight", slot, sha, source_bundle=str(bundle),
        run=run, lease_path=lease_path,
    )
    (slot / "ignored-output").write_text("previous job artifact\n", encoding="utf-8")

    assert ci_node.prepare_slot(
        managed, "goal-flight", slot, sha, run=run, lease_path=lease_path,
    )
    assert not (slot / "ignored-output").exists()
    assert not (slot / "retained").exists()


def test_prepare_slot_only_refuses_base_retained_when_seat_has_retained(tmp_path):
    source = tmp_path / "source"
    _init_repo(source)
    base_retained = source / "retained" / "base.txt"
    base_retained.parent.mkdir()
    base_retained.write_text("base data\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "source")
    sha = _git(source, "rev-parse", "HEAD").stdout.strip()
    managed = tmp_path / "managed"
    lease_id = "c" * 32
    run, lease_path, _token = _pool_lease(managed, lease_id)
    bundle = _pool_bundle(source, run)
    slot = managed / "repos" / "goal-flight" / "seats" / "s-01"
    slot.mkdir(parents=True)

    assert ci_node.prepare_slot(
        managed, "goal-flight", slot, sha, source_bundle=str(bundle),
        run=run, lease_path=lease_path,
    )
    assert (slot / "retained" / "base.txt").read_text() == "base data\n"

    legacy_marker = slot / "retained" / "legacy.txt"
    legacy_marker.write_text("preserve this data\n", encoding="utf-8")
    with pytest.raises(ci_node.SeatSetupFailure, match="preserved seat data"):
        ci_node.prepare_slot(
            managed, "goal-flight", slot, sha, run=run, lease_path=lease_path,
        )
    assert legacy_marker.read_text() == "preserve this data\n"


def test_legacy_slot_cleanup_waits_for_active_leases(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    repo_root = managed / "repos" / "goal-flight"
    legacy = repo_root / "slots"
    (legacy / "s-01").mkdir(parents=True)
    (legacy / "s-01" / "old-checkout.txt").write_text("old slot\n", encoding="utf-8")
    lease_id = "3" * 32
    _run, lease_path, _token = _pool_lease(managed, lease_id)
    lease = json.loads(lease_path.read_text())
    lease.update(state="running", slot=str(legacy / "s-01"))
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])

    blocked = ci_node.cleanup_legacy_slots(managed, "goal-flight", apply=True)
    assert blocked["status"] == "active"
    assert (legacy / "s-01" / "old-checkout.txt").exists()

    lease["state"] = "released"
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    preview = ci_node.cleanup_legacy_slots(managed, "goal-flight")
    assert preview["status"] == "eligible"
    removed = ci_node.cleanup_legacy_slots(managed, "goal-flight", apply=True)
    assert removed["status"] == "removed"
    assert not legacy.exists()

def test_legacy_slot_cleanup_preserves_repo_less_lease_and_held_slot_lock(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    legacy = managed / "repos" / "default" / "slots"
    slot = legacy / "s-01"
    slot.mkdir(parents=True)
    (slot / "old-checkout.txt").write_text("old slot\n", encoding="utf-8")
    lease_id = "4" * 32
    run, lease_path, _token = _pool_lease(managed, lease_id, repo="default")
    lease = json.loads(lease_path.read_text())
    lease.pop("repo")
    lease.update(state="running", slot=str(slot))
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    (slot / "SLOT.json").write_text(json.dumps({
        "state": "leased", "run_dir": str(run),
    }), encoding="utf-8")
    slot_lock = ci_node.try_lock(slot / "slot.lock")
    assert slot_lock is not None
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])

    try:
        blocked = ci_node.cleanup_legacy_slots(managed, "default", apply=True)
        assert blocked["status"] == "active"
        assert blocked["slot"] == str(slot)
        assert (slot / "old-checkout.txt").exists()
    finally:
        slot_lock.close()

    # The default pool still protects a repo-less active lease with no slot path.
    (slot / "SLOT.json").write_text(json.dumps({"state": "free"}), encoding="utf-8")
    lease.pop("slot")
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    default_blocked = ci_node.cleanup_legacy_slots(managed, "default", apply=True)
    assert default_blocked["status"] == "active"

    # With no recorded slot path, a held legacy slot lock and then its leased
    # marker still protect a slot while the repo-less lease remains active.
    other_legacy = managed / "repos" / "goal-flight" / "slots"
    other_slot = other_legacy / "s-01"
    other_slot.mkdir(parents=True)
    (other_slot / "SLOT.json").write_text(json.dumps({
        "state": "leased", "run_dir": str(run),
    }), encoding="utf-8")
    other_lock = ci_node.try_lock(other_slot / "slot.lock")
    assert other_lock is not None
    try:
        lock_blocked = ci_node.cleanup_legacy_slots(managed, "goal-flight", apply=True)
        assert lock_blocked["status"] == "active"
        assert lock_blocked["reason"] == "legacy-slot-lock-held"
    finally:
        other_lock.close()
    marker_blocked = ci_node.cleanup_legacy_slots(managed, "goal-flight", apply=True)
    assert marker_blocked["status"] == "active"
    assert marker_blocked["reason"] == "active-legacy-slot-marker"
    (other_slot / "SLOT.json").write_text(json.dumps({"state": "free"}), encoding="utf-8")
    assert ci_node.cleanup_legacy_slots(managed, "goal-flight", apply=True)["status"] == "removed"

    lease["state"] = "released"
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    preview = ci_node.cleanup_legacy_slots(managed, "default")
    assert preview["status"] == "eligible"
    removed = ci_node.cleanup_legacy_slots(managed, "default", apply=True)
    assert removed["status"] == "removed"
    assert not legacy.exists()

def test_legacy_slot_cleanup_deletes_detached_tree_outside_admission(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    legacy = managed / "repos" / "goal-flight" / "slots"
    repo_root = legacy.parent
    old_slot = legacy / "s-01"
    old_slot.mkdir(parents=True)
    (old_slot / "old-checkout.txt").write_text("old slot\n", encoding="utf-8")
    admission_root = managed / "admission"
    delete_reached = threading.Event()
    continue_delete = threading.Event()
    real_rmtree = ci_node.shutil.rmtree

    def pause_at_delete(path, *args, **kwargs):
        if Path(path) == repo_root / ".slots-cleanup":
            delete_reached.set()
            assert continue_delete.wait(5)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])
    monkeypatch.setattr(ci_node.shutil, "rmtree", pause_at_delete)

    def admit_legacy_slot():
        admission_lock = ci_node._with_queue_lock(admission_root)
        try:
            active_slot = legacy / "s-01"
            active_slot.mkdir(parents=True, exist_ok=True)
            (active_slot / "active-output.txt").write_text(
                "active output\n", encoding="utf-8",
            )
        finally:
            admission_lock.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        cleanup = pool.submit(
            ci_node.cleanup_legacy_slots, managed, "goal-flight", apply=True,
        )
        assert delete_reached.wait(5)
        admission = pool.submit(admit_legacy_slot)
        try:
            admission.result(timeout=5)
            assert cleanup.done() is False
        finally:
            continue_delete.set()
        result = cleanup.result(timeout=5)

    assert result["status"] == "removed"
    assert (legacy / "s-01" / "active-output.txt").read_text() == "active output\n"

def test_legacy_slot_cleanup_bounds_pool_and_admission_lock_waits(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    legacy = managed / "repos" / "goal-flight" / "slots"
    (legacy / "s-01").mkdir(parents=True)
    (legacy / "s-01" / "old-checkout.txt").write_text("old slot\n", encoding="utf-8")
    monkeypatch.setattr(ci_node, "POOL_LOCK_OPERATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])

    with ci_node._repo_pool_lock(managed, "goal-flight"):
        started = time.monotonic()
        blocked = ci_node.cleanup_legacy_slots(managed, "goal-flight", apply=True)
        elapsed = time.monotonic() - started
        assert blocked["status"] == "unknown"
        assert blocked["reason"] == "pool-lock-timeout"
        assert elapsed < 1
        # The pool-lock wait must not hold the box-wide admission lock.
        admission = ci_node._with_queue_lock(managed / "admission", deadline=time.monotonic() + 0.2)
        admission.close()

    admission = ci_node._with_queue_lock(managed / "admission")
    try:
        started = time.monotonic()
        blocked = ci_node.cleanup_legacy_slots(managed, "goal-flight", apply=True)
        elapsed = time.monotonic() - started
        assert blocked["status"] == "unknown"
        assert blocked["reason"] == "admission-lock-timeout"
        assert elapsed < 1
        assert legacy.exists()
    finally:
        admission.close()

def test_active_seat_keeps_its_base_during_refresh(tmp_path):
    source = tmp_path / "source"
    _init_repo(source)
    (source / "source.txt").write_text("base one\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "base one")
    first_sha = _git(source, "rev-parse", "HEAD").stdout.strip()
    managed = tmp_path / "managed"
    first_id = "c" * 32
    first_run, first_lease, _ = _pool_lease(managed, first_id)
    first_slot = managed / "repos" / "goal-flight" / "seats" / "s-01"
    assert ci_node.prepare_slot(
        managed, "goal-flight", first_slot, first_sha,
        source_bundle=str(_pool_bundle(source, first_run)),
        run=first_run, lease_path=first_lease,
    )
    (first_slot / "source.txt").write_text("active seat work\n", encoding="utf-8")

    (source / "source.txt").write_text("base two\n", encoding="utf-8")
    _git(source, "commit", "--quiet", "-am", "base two")
    second_sha = _git(source, "rev-parse", "HEAD").stdout.strip()
    second_id = "d" * 32
    second_run, second_lease, _ = _pool_lease(managed, second_id)
    second_slot = managed / "repos" / "goal-flight" / "seats" / "s-02"
    assert ci_node.prepare_slot(
        managed, "goal-flight", second_slot, second_sha,
        source_bundle=str(_pool_bundle(source, second_run)),
        run=second_run, lease_path=second_lease,
    )

    repo_root = managed / "repos" / "goal-flight"
    assert (repo_root / "bases" / first_sha).is_dir()
    assert (repo_root / "bases" / "current").read_text().strip() == second_sha
    assert (first_slot / "source.txt").read_text() == "active seat work\n"
    assert (second_slot / "source.txt").read_text() == "base two\n"

def test_gc_keeps_a_bundle_basis_commit_pinned_by_an_active_lease(tmp_path):
    first_source = tmp_path / "first-source"
    _init_repo(first_source)
    (first_source / "first.txt").write_text("first history\n", encoding="utf-8")
    _git(first_source, "add", "-A")
    _git(first_source, "commit", "--quiet", "-m", "first")
    first_sha = _git(first_source, "rev-parse", "HEAD").stdout.strip()
    managed = tmp_path / "managed"
    first_id = "e" * 32
    first_run, first_lease, _ = _pool_lease(managed, first_id)
    first_slot = managed / "repos" / "goal-flight" / "seats" / "s-01"
    assert ci_node.prepare_slot(
        managed, "goal-flight", first_slot, first_sha,
        source_bundle=str(_pool_bundle(first_source, first_run)),
        run=first_run, lease_path=first_lease,
    )
    first_state = json.loads(first_lease.read_text())
    first_state["state"] = "released"
    first_lease.write_text(json.dumps(first_state), encoding="utf-8")

    (managed / "admission").mkdir()
    pin_id = "f" * 32
    _pin_run, _pin_lease, pin_token = _pool_lease(managed, pin_id)
    assert ci_node.pin_bundle_basis(managed, "goal-flight", pin_id, pin_token) == first_sha

    second_source = tmp_path / "second-source"
    _init_repo(second_source)
    (second_source / "second.txt").write_text("divergent history\n", encoding="utf-8")
    _git(second_source, "add", "-A")
    _git(second_source, "commit", "--quiet", "-m", "second")
    second_sha = _git(second_source, "rev-parse", "HEAD").stdout.strip()
    second_id = "1" * 32
    second_run, second_lease, _ = _pool_lease(managed, second_id)
    second_slot = managed / "repos" / "goal-flight" / "seats" / "s-02"
    assert ci_node.prepare_slot(
        managed, "goal-flight", second_slot, second_sha,
        source_bundle=str(_pool_bundle(second_source, second_run)),
        run=second_run, lease_path=second_lease,
    )

    repo_root = managed / "repos" / "goal-flight"
    mirror = repo_root / "mirror.git"
    assert not (repo_root / "bases" / first_sha).exists()
    assert ci_node._mirror_has_commit(mirror, first_sha)
    pin_ref = f"refs/goalflight/remote-ci/bundle-bases/{pin_id}"
    assert _git(mirror, "rev-parse", pin_ref).stdout.strip() == first_sha

def test_pool_lock_wait_expires_as_a_typed_setup_failure(tmp_path):
    managed = tmp_path / "managed"
    run, lease_path, _token = _pool_lease(managed, "3" * 32)
    slot = managed / "repos" / "goal-flight" / "seats" / "s-01"

    with ci_node._repo_pool_lock(managed, "goal-flight"):
        started = time.monotonic()
        deadline = started + 0.15
        with pytest.raises(ci_node.SetupDeadlineExceeded) as raised:
            ci_node.prepare_slot(
                managed, "goal-flight", slot, "b" * 40,
                run=run, lease_path=lease_path, deadline=deadline,
            )

    assert raised.value.failure_type == "setup-timeout"
    assert time.monotonic() - started < 0.8
    assert not slot.exists()

def test_hung_clone_is_killed_at_setup_deadline_and_releases_pool_lock(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    repo_root = managed / "repos" / "goal-flight"
    mirror = repo_root / "mirror.git"
    bases = repo_root / "bases"
    seats = repo_root / "seats"
    generation = bases / ("b" * 40)
    for path in (mirror, generation, seats):
        path.mkdir(parents=True)
    (generation / "tracked.txt").write_text("base\n", encoding="utf-8")

    fake_site = tmp_path / "child-site"
    fake_site.mkdir()
    (fake_site / "sitecustomize.py").write_text(
        "import ctypes, os, time\n"
        "class _Clone:\n"
        " def __call__(self, source, destination, flags):\n"
        "  path = os.fsdecode(destination)\n"
        "  os.mkdir(path)\n"
        "  open(os.path.join(path, 'partial'), 'w').write('partial')\n"
        "  time.sleep(60)\n"
        "  return 0\n"
        "class _Library:\n"
        " clonefile = _Clone()\n"
        "ctypes.CDLL = lambda *args, **kwargs: _Library()\n",
        encoding="utf-8",
    )
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(fake_site), child_env.get("PYTHONPATH", ""),
    )))
    monkeypatch.setattr(ci_node, "_ensure_mirror", lambda *_args, **_kwargs: mirror)
    monkeypatch.setattr(ci_node, "_mirror_has_commit", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ci_node, "_set_confirmed_basis", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ci_node, "_build_base_generation", lambda *_args, **_kwargs: generation)
    monkeypatch.setattr(ci_node, "_update_pool_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ci_node, "_write_current_base", lambda *_args, **_kwargs: None)

    children = []
    real_popen = subprocess.Popen

    def record_child(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(ci_node.subprocess, "Popen", record_child)
    slot = seats / "s-01"
    started = time.monotonic()
    with pytest.raises(ci_node.SetupDeadlineExceeded) as raised:
        ci_node.prepare_slot(
            managed, "goal-flight", slot, "b" * 40,
            env=child_env, deadline=started + 0.8,
        )

    assert raised.value.failure_type == "setup-timeout"
    assert time.monotonic() - started < 2.0
    assert len(children) == 1 and children[0].returncode is not None
    assert not list(seats.glob(".s-01.clone-*"))
    with ci_node._repo_pool_lock(
            managed, "goal-flight", deadline=time.monotonic() + 1):
        pass

def test_repo_gc_reclaims_orphaned_staging_only_after_setup_releases_pool_lock(
    tmp_path,
):
    managed = tmp_path / "managed"
    repo_root = managed / "repos" / "goal-flight"
    bases = repo_root / "bases"
    seats = repo_root / "seats"
    bases.mkdir(parents=True)
    seats.mkdir(parents=True)
    mirror = ci_node._ensure_mirror(repo_root)
    orphan_base = bases / ("." + "a" * 40 + "." + "1" * 32 + ".tmp")
    orphan_index = bases / ("." + "b" * 40 + "." + "2" * 32 + ".index")
    orphan_clone = seats / (".s-01.clone-" + "3" * 32)
    live_base = bases / ("." + "c" * 40 + "." + "4" * 32 + ".tmp")
    for path in (orphan_base, orphan_clone):
        path.mkdir()
        (path / "partial").write_text("partial\n", encoding="utf-8")
    orphan_index.write_text("stale index\n", encoding="utf-8")

    owner_ready = threading.Event()
    release_owner = threading.Event()
    collector_started = threading.Event()

    def hold_setup_lock():
        with ci_node._repo_pool_lock(managed, "goal-flight"):
            live_base.mkdir()
            (live_base / "partial").write_text("partial\n", encoding="utf-8")
            owner_ready.set()
            assert release_owner.wait(4)

    def collect_after_lock():
        assert owner_ready.wait(4)
        collector_started.set()
        with ci_node._repo_pool_lock(
                managed, "goal-flight", deadline=time.monotonic() + 4) as (
                    locked_root, lock_fd):
            return ci_node._repo_gc_locked(
                managed, "goal-flight", locked_root, mirror, lock_fd,
                deadline=time.monotonic() + 3,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(hold_setup_lock)
        assert owner_ready.wait(2)
        collector = pool.submit(collect_after_lock)
        try:
            assert collector_started.wait(2)
            time.sleep(0.1)
            assert not collector.done()
            assert live_base.exists()
            assert orphan_base.exists() and orphan_clone.exists()
        finally:
            release_owner.set()
        owner.result(timeout=2)
        result = collector.result(timeout=5)

    assert result["status"] == "complete"
    assert not orphan_base.exists()
    assert not orphan_index.exists()
    assert not orphan_clone.exists()
    assert not live_base.exists()

def test_clonefile_failure_is_a_typed_setup_failure(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _init_repo(source)
    (source / "source.txt").write_text("source\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "source")
    sha = _git(source, "rev-parse", "HEAD").stdout.strip()
    managed = tmp_path / "managed"
    lease_id = "2" * 32
    run, lease_path, _ = _pool_lease(managed, lease_id)
    slot = managed / "repos" / "goal-flight" / "seats" / "s-01"

    def fail_clone(_source, _destination, **_kwargs):
        raise OSError("mock clonefile failure")

    monkeypatch.setattr(ci_node, "_run_clonefile_process", fail_clone)
    with pytest.raises(ci_node.SeatSetupFailure) as raised:
        ci_node.prepare_slot(
            managed, "goal-flight", slot, sha,
            source_bundle=str(_pool_bundle(source, run)),
            run=run, lease_path=lease_path,
        )
    assert raised.value.failure_type == "seat-clone-failed"
    assert not slot.exists()

def test_node_preflight_requires_node_git_and_normalizes_top_locale(tmp_path, monkeypatch):
    (tmp_path / "policy.json").write_text(json.dumps({
        "p_cores": 4, "token_pool_size": 1, "self_cap": 1,
    }), encoding="utf-8")
    monkeypatch.setattr(ci_node.shutil, "which", lambda name: None if name == "node" else "/usr/bin/git")
    with pytest.raises(ValueError, match="node"):
        ci_node.sample(tmp_path, sys.executable)

    seen = {}
    monkeypatch.setattr(ci_node.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ci_node.os, "getloadavg", lambda: (0.5, 0.4, 0.3))
    monkeypatch.setattr(ci_node.socket, "gethostname", lambda: "measured-node")

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "CPU usage: 5.0% user, 5.0% sys, 90.0% idle", "")

    monkeypatch.setattr(ci_node.subprocess, "run", fake_run)
    sample = ci_node.sample(tmp_path, sys.executable)
    assert sample["tools"] == {"python": True, "node": True, "git": True}
    assert seen["env"]["LC_ALL"] == "C"

def test_node_health_uses_box_path_for_tool_checks_and_subprocesses(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    admission.mkdir(parents=True)
    ci_node.write_json(admission / "policy.json", {
        "p_cores": 4, "token_pool_size": 1, "self_cap": 1,
    })
    authority = tmp_path / "authority.json"
    monkeypatch.setattr(ci_node, "AUTHORITY_FILE", authority)

    configured_path = tmp_path / "configured-bin"
    configured_path.mkdir()
    for name in ("node", "git"):
        tool = configured_path / name
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setenv(
        "SSH_CONNECTION", "192.0.2.2 51234 2001:db8::10 22",
    )
    monkeypatch.setattr(ci_node.os, "getloadavg", lambda: (0.5, 0.4, 0.3))
    monkeypatch.setattr(ci_node.socket, "gethostname", lambda: "measured-node")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(
            argv, 0, "CPU usage: 5.0% user, 5.0% sys, 90.0% idle", "",
        )

    monkeypatch.setattr(ci_node.subprocess, "run", fake_run)
    result = ci_node.dispatch({
        "operation": "health", "managed_root": str(managed),
        "python_interpreter": sys.executable, "p_cores": 4,
        "token_pool_size": 1, "node_path": str(configured_path),
    })

    assert result["load"]["tools"] == {"python": True, "node": True, "git": True}
    assert result["peer_address"] == "2001:db8::10"
    assert seen["env"]["PATH"] == str(configured_path)
    assert os.environ["PATH"] == str(tmp_path / "empty-path")

def test_node_does_not_use_relaunch_status_for_first_workload(tmp_path, monkeypatch):
    label = "com.goalflight.remote-ci.recorded"
    output = f"PID\tStatus\tLabel\n222\t1792\t{label}\n"
    run = tmp_path / "run"
    run.mkdir()
    (run / "lease.json").write_text("{}", encoding="utf-8")
    state = {"launch_label": label, "workload_pid": "111"}
    monkeypatch.setattr(ci_node, "_run_command", lambda argv: subprocess.CompletedProcess(
        argv, 0, output, "",
    ))

    code = ci_node._capture_exit_status(run, state)

    assert code is None
    assert state.get("exit_known") is not True

def test_node_ignores_completed_relaunch_noop_status(tmp_path, monkeypatch):
    label = "com.goalflight.remote-ci.completed-relaunch"
    run = tmp_path / "run"
    run.mkdir()
    (run / "lease.json").write_text("{}", encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 111, "start": [1, 2], "coalition_id": 3,
        "boot_session": "boot",
    }), encoding="utf-8")
    (run / "relaunch-noop").touch()
    monkeypatch.setattr(ci_node, "_run_command", lambda argv: subprocess.CompletedProcess(
        argv, 0, f"PID\tStatus\tLabel\n-\t0\t{label}\n", "",
    ))

    state = {"launch_label": label, "workload_pid": "111"}
    assert ci_node._capture_exit_status(run, state) is None
    assert state.get("exit_known") is not True

def test_node_uses_exit_record_before_launchctl_status(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "lease.json").write_text("{}", encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 111, "start": [1, 2], "coalition_id": 3,
        "boot_session": "boot",
    }), encoding="utf-8")
    (run / "exit.json").write_text(json.dumps({
        "pid": 111, "returncode": 7, "signal": None,
    }), encoding="utf-8")
    monkeypatch.setattr(ci_node, "_run_command", lambda argv: subprocess.CompletedProcess(
        argv, 0, "PID\tStatus\tLabel\n222\t0\tcom.goalflight.remote-ci.recorded\n", "",
    ))

    state = {"launch_label": "com.goalflight.remote-ci.recorded", "workload_pid": "111"}
    assert ci_node._capture_exit_status(run, state) == 7
    assert state["exit_known"] is True

def test_node_decodes_first_incarnation_wait_status(tmp_path, monkeypatch):
    label = "com.goalflight.remote-ci.first"
    run = tmp_path / "run"
    run.mkdir()
    (run / "lease.json").write_text("{}", encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 111, "start": [1, 2], "coalition_id": 3,
        "boot_session": "boot",
    }), encoding="utf-8")
    monkeypatch.setattr(ci_node, "_run_command", lambda argv: subprocess.CompletedProcess(
        argv, 0, f"PID\tStatus\tLabel\n-\t1792\t{label}\n", "",
    ))

    state = {"launch_label": label, "workload_pid": "111"}
    assert ci_node._capture_exit_status(run, state) == 7
    assert state["exit_known"] is True

def test_workload_wrapper_does_not_rerun_after_launchd_restarts_it(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    _write_workload_deadline(run)
    spec_path = run / "spec.json"
    gate_path = run / "workload-go"
    gate_path.touch()
    count_path = tmp_path / "runs.txt"
    spec_path.write_text(json.dumps({
        "cwd": str(tmp_path),
        "argv": [sys.executable, "-c", "from pathlib import Path; Path(" +
        repr(str(count_path)) + ").open('a').write('run\\n'); raise SystemExit(7)"],
        "env": os.environ.copy(), "emit_receipt": False,
    }), encoding="utf-8")
    wrapper = tmp_path / "workload-launch.py"
    wrapper.write_text(ci_node._WORKLOAD_WRAPPER, encoding="utf-8")
    for attempt in range(2):
        result = subprocess.run(
            [sys.executable, str(wrapper), str(spec_path), str(gate_path)],
            capture_output=True, text=True, check=False, timeout=15,
        )
        assert result.returncode == (7 if attempt == 0 else 0), result.stderr

    assert count_path.read_text(encoding="utf-8").splitlines() == ["run"]
    exit_record = json.loads((run / "exit.json").read_text(encoding="utf-8"))
    assert exit_record["returncode"] == 7
    assert isinstance(exit_record["pid"], int)


def test_workload_wrapper_exits_when_start_gate_never_opens(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    spec_path = run / "spec.json"
    gate_path = run / "workload-go"
    spec_path.write_text(json.dumps({"env": {}}), encoding="utf-8")
    gate_timeout = f"time.monotonic() + {ci_node.WORKLOAD_START_GATE_TIMEOUT_SECONDS!r}"
    wrapper_source = ci_node._WORKLOAD_WRAPPER.replace(
        gate_timeout, "time.monotonic() + 0.05", 1,
    )
    wrapper = run / "workload-launch.py"
    wrapper.write_text(wrapper_source, encoding="utf-8")

    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(wrapper), str(spec_path), str(gate_path)],
        capture_output=True, text=True, check=False, timeout=3,
    )

    assert time.monotonic() - started < 1
    assert result.returncode == ci_node.EXIT_DEADLINE
    assert not (run / "command-started").exists()


def test_workload_wrapper_records_signal_exit(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    _write_workload_deadline(run)
    spec_path = run / "spec.json"
    gate_path = run / "workload-go"
    gate_path.touch()
    spec_path.write_text(json.dumps({
        "argv": [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
        "env": os.environ.copy(), "emit_receipt": False,
    }), encoding="utf-8")

    subprocess.run(
        [sys.executable, "-c", ci_node._WORKLOAD_WRAPPER, str(spec_path), str(gate_path)],
        capture_output=True, text=True, check=False, timeout=15,
    )

    exit_record = json.loads((run / "exit.json").read_text(encoding="utf-8"))
    assert exit_record["returncode"] == -signal.SIGTERM
    assert exit_record["signal"] == signal.SIGTERM


def test_workload_wrapper_bounds_child_wait_by_lease_deadline(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    _write_workload_deadline(run, timeout=0.05)
    spec_path = run / "spec.json"
    gate_path = run / "workload-go"
    gate_path.touch()
    spec_path.write_text(json.dumps({
        "argv": [sys.executable, "-c", "import time; time.sleep(10)"],
        "env": os.environ.copy(), "emit_receipt": False,
    }), encoding="utf-8")

    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", ci_node._WORKLOAD_WRAPPER, str(spec_path), str(gate_path)],
        capture_output=True, text=True, check=False, timeout=3,
    )

    assert time.monotonic() - started < 1
    assert result.returncode == ci_node.EXIT_DEADLINE
    assert json.loads((run / "exit.json").read_text())["returncode"] == ci_node.EXIT_DEADLINE


def test_remote_tmpdir_is_per_lease_and_outside_repository(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    admission = managed / "admission"
    admission.mkdir(parents=True)

    scratch = ci_node._run_scratch_directory(admission, "lease-one")
    result = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.mkdtemp())"],
        env={**os.environ, "TMPDIR": str(scratch)}, capture_output=True,
        text=True, check=True,
    )
    temporary = Path(result.stdout.strip()).resolve()

    assert scratch == managed / "tmp" / "lease-one"
    assert temporary.is_relative_to(managed)
    assert repo not in temporary.parents

def test_remote_tmpdir_refuses_managed_root_inside_git_worktree(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    admission = repo / "managed" / "admission"
    admission.mkdir(parents=True)

    with pytest.raises(ValueError, match="inside a Git worktree"):
        ci_node._run_scratch_directory(admission, "lease-one")

def _tail(text: str) -> dict:
    return {"text": text, "truncated": False, "omitted_bytes": 0}

def test_died_command_returns_stdout_and_stderr(tmp_path, capsys):
    config = _config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_: None)
    receipt = json.dumps({
        "schema": ci.RECEIPT_SCHEMA, "hostname": "MEASURED-NODE.local",
        "status": "fail", "returncode": 1, "failed": 1,
    }) + "\n"
    record = {
        "state": "released", "holder_alive": False,
        "sample": {"hostname": "measured-node"},
        "result": {
            "status": "died", "returncode": 1, "exit_known": True,
            "command_started": True, "timed_out": False,
            "streams": {
                "stdout": {"tail": _tail(receipt)},
                "stderr": {"tail": _tail("remote stderr\n")},
            },
        },
    }

    class Node:
        box = config.boxes["box-a"]

        def call(self, operation, **kwargs):
            assert operation == "status"
            return record

    outcome = runner._watch(spec(), Node(), {"run_dir": "/runs/died", "lease_token": "t"}, {})
    assert outcome.status == "red"
    ci._print_remote_result({"candidate": outcome.to_dict()})
    captured = capsys.readouterr()
    assert receipt.strip() in captured.out
    assert "remote stderr" in captured.err

def test_unknown_died_exit_ignores_stdout_receipt_and_requires_local_fallback(tmp_path, monkeypatch):
    config = _config(tmp_path)
    runner = RemoteRunner(config, executor=lambda *_: None)
    receipt = json.dumps({"hostname": "measured-node"}) + "\n"
    record = {
        "state": "released", "holder_alive": False,
        "sample": {"hostname": "measured-node"},
        "result": {
            "status": "died", "returncode": 2, "exit_known": False,
            "command_started": True, "timed_out": False,
            "streams": {
                "stdout": {"tail": _tail(receipt)},
                "stderr": {"tail": _tail("node exited before recording status\n")},
            },
        },
    }

    class Node:
        box = config.boxes["box-a"]

        def call(self, operation, **kwargs):
            return record

    outcome = runner._watch(spec(), Node(), {"run_dir": "/runs/unknown", "lease_token": "t"}, {})
    assert outcome.status == "setup-failed"
    assert outcome.receipt is None
    assert "node exited before recording status" in outcome.error
    monkeypatch.setattr(ci, "_pin_request_box", lambda request, *_args, **_kwargs:
                        {**request, "box": "box-a"})
    monkeypatch.setattr(ci, "_run_arm_with_placement_retries", lambda *_args, **_kwargs: outcome)
    remote = ci.run_remote_command({
        "request_id": "unknown-exit", "candidate_sha": "a" * 40,
        "command": ["pytest"], "command_id": "heavy-gate",
    }, runner)
    assert remote["status"] == "LOCAL_FALLBACK_REQUIRED"

def test_truncated_success_receipt_requires_local_fallback(tmp_path):
    config = _config(tmp_path)
    remote = RemoteRunner(config, executor=lambda *_: None)
    record = {
        "state": "released", "holder_alive": False,
        "sample": {"hostname": "measured-node"},
        "result": {
            "status": "completed", "returncode": 0, "exit_known": True,
            "command_started": True, "timed_out": False,
            "streams": {
                "stdout": {"tail": {
                    "text": '{"schema":"goalflight.remote-ci.receipt.v1",',
                    "truncated": True, "omitted_bytes": 50,
                }},
                "stderr": {"tail": _tail("")},
            },
        },
    }

    class Node:
        box = config.boxes["box-a"]

        def call(self, operation, **kwargs):
            assert operation == "status"
            return record

    outcome = remote._watch(
        spec("truncated-success"), Node(),
        {"run_dir": "/runs/truncated", "lease_token": "t"}, {},
    )

    class OutcomeRunner(SuccessfulCommandRunner):
        def run_arm(self, arm_spec, *, admission_deadline=None):
            return outcome

    result = ci.run_remote_command(
        _command_request("truncated-success"), OutcomeRunner(config),
    )

    assert outcome.status == "setup-failed"
    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"

def test_run_daemon_processes_independent_requests_concurrently(tmp_path, monkeypatch):
    config = replace(_config(tmp_path), poll_seconds=0.01)
    daemon = ci.GateDaemon(config)
    requests = [config.queue_dir / f"request-{n}.json" for n in range(2)]
    for path in requests:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_command_request(path.stem)), encoding="utf-8")
    lock = threading.Lock()
    active = 0
    max_active = 0
    completed = set()

    class StopLoop(Exception):
        pass

    def pending_paths():
        if len(completed) == 2:
            raise StopLoop
        return [path for path in requests if path.exists()]

    def process(request, runner):
        del runner
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.1)
        with lock:
            active -= 1
            completed.add(request["request_id"])
        return {
            "schema": ci.RESULT_SCHEMA, "request_id": request["request_id"],
            "status": "GREEN", "candidate": {"returncode": 0},
        }

    daemon.pending_paths = pending_paths
    daemon._snapshot_command_request = lambda request: (request, None)
    monkeypatch.setattr(ci, "run_remote_command", process)
    with _independent_deadline(), pytest.raises(StopLoop):
        daemon.run()

    assert completed == {"request-0", "request-1"}
    assert max_active == 2


class _StopDaemonForTest(Exception):
    pass


def _spool_test_request(config, request_id):
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    path = config.queue_dir / f"{request_id}.json"
    path.write_text("{}", encoding="utf-8")
    return path


def _request_daemon_drain(config, *, pid=None):
    path = config.state_dir / "drain.request"
    path.parent.mkdir(parents=True, exist_ok=True)
    if pid is None:
        path.touch()
    else:
        path.write_text(f"{pid}\n", encoding="utf-8")
    return path


def _start_test_daemon(daemon):
    stop = threading.Event()
    errors = []
    pending_paths = daemon.pending_paths

    def guarded_pending_paths():
        if stop.is_set():
            raise _StopDaemonForTest
        return pending_paths()

    def run():
        try:
            daemon.run()
        except _StopDaemonForTest:
            pass
        except BaseException as exc:
            errors.append(exc)

    daemon.pending_paths = guarded_pending_paths
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, stop, errors


def _wait_for_daemon_status(config, status, *, timeout=2, **fields):
    status_path = config.state_dir / "daemon.status.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        if value.get("status") == status and all(
            value.get(key) == expected for key, expected in fields.items()
        ):
            return value
        time.sleep(0.005)
    return None


def _test_result(request_id):
    return {
        "schema": ci.RESULT_SCHEMA,
        "request_id": request_id,
        "status": "GREEN",
    }


def test_daemon_drain_leaves_queued_request_for_next_daemon(tmp_path):
    config = replace(_config(tmp_path), poll_seconds=0.005)
    running_id = "request-running"
    queued_id = "request-queued"
    running_path = _spool_test_request(config, running_id)
    daemon = ci.GateDaemon(config)
    started = threading.Event()
    release = threading.Event()
    claimed = []

    def process(path):
        claimed.append(path.stem)
        if path.stem == running_id:
            started.set()
            if not release.wait(5):
                raise RuntimeError("test did not release the in-flight request")
        return _test_result(path.stem)

    daemon._process_request = process
    thread, stop, errors = _start_test_daemon(daemon)
    try:
        assert started.wait(2)
        _request_daemon_drain(config)
        queued_path = _spool_test_request(config, queued_id)
        assert _wait_for_daemon_status(config, "draining") is not None
        time.sleep(0.05)
        assert queued_id not in claimed
        assert queued_path.exists()
        assert not (config.result_dir / f"{queued_id}.json").exists()
    finally:
        release.set()
        if thread.is_alive():
            stop.set()
        thread.join(5)

    assert not thread.is_alive()
    assert errors == []
    assert json.loads((config.result_dir / f"{running_id}.json").read_text())[
        "status"
    ] == "GREEN"
    assert not running_path.exists()
    assert queued_path.exists()
    assert not (config.result_dir / f"{queued_id}.json").exists()

    # Both daemon instances share this test process; model the old daemon's
    # former PID as distinct from the successor so startup clears the flag.
    stale_pid = os.getpid() + 1
    drain_path = config.state_dir / "drain.request"
    drain_path.write_text(f"{stale_pid}\n", encoding="utf-8")
    next_daemon = ci.GateDaemon(config)
    next_claimed = []
    next_daemon._process_request = lambda path: (
        next_claimed.append(path.stem) or _test_result(path.stem)
    )
    next_daemon.run(once=True)

    assert next_claimed == [queued_id]
    assert not queued_path.exists()
    assert json.loads((config.result_dir / f"{queued_id}.json").read_text())[
        "status"
    ] == "GREEN"
    assert not (config.state_dir / "drain.request").exists()


def test_daemon_drain_finishes_and_publishes_in_flight_request(tmp_path):
    config = replace(_config(tmp_path), poll_seconds=0.005)
    request_id = "request-in-flight"
    request_path = _spool_test_request(config, request_id)
    daemon = ci.GateDaemon(config)
    started = threading.Event()
    release = threading.Event()

    def process(path):
        started.set()
        if not release.wait(5):
            raise RuntimeError("test did not release the in-flight request")
        return _test_result(path.stem)

    daemon._process_request = process
    thread, stop, errors = _start_test_daemon(daemon)
    try:
        assert started.wait(2)
        _request_daemon_drain(config)
        assert _wait_for_daemon_status(config, "draining") is not None
    finally:
        release.set()
        if thread.is_alive():
            stop.set()
        thread.join(5)

    assert not thread.is_alive()
    assert errors == []
    assert not request_path.exists()
    assert json.loads((config.result_dir / f"{request_id}.json").read_text()) == (
        _test_result(request_id)
    )


def test_daemon_exits_after_in_flight_request_finishes(tmp_path):
    config = replace(_config(tmp_path), poll_seconds=0.005)
    request_id = "request-held"
    _spool_test_request(config, request_id)
    daemon = ci.GateDaemon(config)
    started = threading.Event()
    release = threading.Event()

    def process(path):
        started.set()
        if not release.wait(5):
            raise RuntimeError("test did not release the in-flight request")
        return _test_result(path.stem)

    daemon._process_request = process
    thread, stop, errors = _start_test_daemon(daemon)
    try:
        assert started.wait(2)
        _request_daemon_drain(config)
        assert _wait_for_daemon_status(config, "draining") is not None
        assert thread.is_alive()
        assert not (config.result_dir / f"{request_id}.json").exists()
        release.set()
        thread.join(2)
    finally:
        release.set()
        if thread.is_alive():
            stop.set()
        thread.join(5)

    assert not thread.is_alive()
    assert errors == []
    assert json.loads((config.result_dir / f"{request_id}.json").read_text())[
        "status"
    ] == "GREEN"


def test_fresh_daemon_clears_live_unrelated_pid_before_running_and_claims_work(
    tmp_path, monkeypatch,
):
    config = _config(tmp_path)
    request_id = "request-after-restart"
    request_path = _spool_test_request(config, request_id)
    drain_path = config.state_dir / "drain.request"
    drain_path.parent.mkdir(parents=True, exist_ok=True)
    unrelated_pid = os.getppid()
    drain_path.write_text(f"{unrelated_pid}\n", encoding="utf-8")
    status_path = config.state_dir / "daemon.status.json"
    original_atomic_write_json = ci._atomic_write_json

    def assert_cleanup_before_running(path, value):
        if path == status_path and value.get("status") == "running":
            assert not drain_path.exists()
        original_atomic_write_json(path, value)

    monkeypatch.setattr(ci, "_atomic_write_json", assert_cleanup_before_running)
    daemon = ci.GateDaemon(config)
    claimed = []

    def process(path):
        claimed.append(path.stem)
        return _test_result(path.stem)

    daemon._process_request = process
    daemon.run(once=True)

    assert claimed == [request_id]
    assert not request_path.exists()
    assert json.loads((config.result_dir / f"{request_id}.json").read_text())[
        "status"
    ] == "GREEN"
    status = json.loads(
        (config.state_dir / "daemon.status.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "stopped"


@pytest.mark.parametrize("flag_kind", ["pid", "malformed"])
def test_successor_honors_drain_flag_written_after_running(
    tmp_path, monkeypatch, flag_kind,
):
    config = replace(_config(tmp_path), poll_seconds=0.005)
    request_id = "request-successor-queued"
    request_path = _spool_test_request(config, request_id)
    status_path = config.state_dir / "daemon.status.json"
    daemon = ci.GateDaemon(config)
    status_written = threading.Event()
    allow_startup = threading.Event()
    original_atomic_write_json = ci._atomic_write_json
    claimed = []

    def hold_after_running_status(path, value):
        original_atomic_write_json(path, value)
        if path == status_path and value.get("status") == "running":
            status_written.set()
            if not allow_startup.wait(5):
                raise RuntimeError("test did not release daemon startup")

    def process(path):
        claimed.append(path.stem)
        return _test_result(path.stem)

    monkeypatch.setattr(ci, "_atomic_write_json", hold_after_running_status)
    daemon._process_request = process
    thread, stop, errors = _start_test_daemon(daemon)
    try:
        assert status_written.wait(2)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if flag_kind == "pid":
            _request_daemon_drain(config, pid=status["pid"])
        else:
            drain_path = config.state_dir / "drain.request"
            drain_path.write_text("not a pid\n", encoding="utf-8")
        allow_startup.set()
        assert _wait_for_daemon_status(config, "stopped") is not None
    finally:
        allow_startup.set()
        if thread.is_alive():
            stop.set()
        thread.join(5)

    assert not thread.is_alive()
    assert errors == []
    assert claimed == []
    assert request_path.exists()
    assert not (config.result_dir / f"{request_id}.json").exists()


def test_fresh_daemon_clears_malformed_drain_flag_at_startup(tmp_path):
    config = _config(tmp_path)
    request_id = "request-garbage-flag"
    request_path = _spool_test_request(config, request_id)
    drain_path = config.state_dir / "drain.request"
    drain_path.parent.mkdir(parents=True, exist_ok=True)
    drain_path.write_text("not a pid\n", encoding="utf-8")
    claimed = []

    daemon = ci.GateDaemon(config)
    daemon._process_request = lambda path: (
        claimed.append(path.stem) or _test_result(path.stem)
    )
    daemon.run(once=True)

    assert claimed == [request_id]
    assert not request_path.exists()
    assert not drain_path.exists()
    assert json.loads((config.result_dir / f"{request_id}.json").read_text())[
        "status"
    ] == "GREEN"


def test_health_reports_daemon_drain_state(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "daemon.status.json").write_text(json.dumps({
        "pid": 123,
        "status": "draining",
        "drain_timeout_reported": True,
    }), encoding="utf-8")
    monkeypatch.setattr(ci, "RemoteRunner", lambda *_args, **_kwargs: SimpleNamespace(nodes={}))

    health = ci.health_census(config)

    assert health["daemon"] == {
        "status": "draining",
        "drain_timeout_reported": True,
    }


def test_daemon_reports_a_drain_timeout_without_abandoning_work(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(ci, "DAEMON_DRAIN_TIMEOUT_SECONDS", 0.02)
    config = replace(_config(tmp_path), poll_seconds=0.005)
    request_id = "request-slow"
    _spool_test_request(config, request_id)
    daemon = ci.GateDaemon(config)
    started = threading.Event()
    release = threading.Event()

    def process(path):
        started.set()
        if not release.wait(5):
            raise RuntimeError("test did not release the in-flight request")
        return _test_result(path.stem)

    daemon._process_request = process
    thread, stop, errors = _start_test_daemon(daemon)
    try:
        assert started.wait(2)
        _request_daemon_drain(config)
        assert _wait_for_daemon_status(config, "draining") is not None
        assert _wait_for_daemon_status(
            config, "draining", drain_timeout_reported=True,
        ) is not None
        assert thread.is_alive()
        assert not (config.result_dir / f"{request_id}.json").exists()
    finally:
        release.set()
        if thread.is_alive():
            stop.set()
        thread.join(5)

    assert not thread.is_alive()
    assert errors == []
    assert json.loads((config.result_dir / f"{request_id}.json").read_text())[
        "status"
    ] == "GREEN"
    assert "drain timed out" in capsys.readouterr().err.lower()

def test_daemon_executor_queue_obeys_request_admission_deadline(tmp_path, monkeypatch):
    config = _config(tmp_path, token_pool_size=1)
    config = replace(
        config,
        admission=replace(config.admission, max_wait_seconds=0.05),
        poll_seconds=0.005,
    )
    daemon = ci.GateDaemon(config)
    requests = [config.queue_dir / "request-running.json", config.queue_dir / "request-queued.json"]
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    for path in requests:
        path.write_text(json.dumps(_command_request(path.stem)), encoding="utf-8")
    original_process = daemon._process_request

    class StopLoop(Exception):
        pass

    def pending_paths():
        if all((config.result_dir / f"{path.stem}.json").exists() for path in requests):
            raise StopLoop
        return [path for path in requests if path.exists()]

    def process(path):
        if path.stem == "request-running":
            time.sleep(0.2)
            return {
                "schema": ci.RESULT_SCHEMA,
                "request_id": path.stem,
                "project_id": config.repo,
                "status": "GREEN",
            }
        return original_process(path)

    daemon._snapshot_command_request = lambda request: (request, None)
    daemon.pending_paths = pending_paths
    daemon._process_request = process
    monkeypatch.setattr(ci, "run_remote_command", lambda request, _runner: {
        "schema": ci.RESULT_SCHEMA,
        "request_id": request["request_id"],
        "status": "GREEN",
        "candidate": {"returncode": 0},
    })
    with _independent_deadline(), pytest.raises(StopLoop):
        daemon.run()

    queued = json.loads((config.result_dir / "request-queued.json").read_text())
    assert queued["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert queued["fallback_reason"] == "queue-deadline-exceeded"

@pytest.mark.parametrize("route", ["command", "targeted", "matched"])
def test_route_entrypoints_keep_the_absolute_admission_deadline(tmp_path, route):
    config = _config(tmp_path)
    runner = SimpleNamespace(
        config=config,
        choose_box=lambda *_args, **_kwargs: pytest.fail("expired deadline reached placement"),
    )
    request = {
        "request_id": "expired-admission",
        "project_id": config.repo,
        "kind": "gate" if route != "targeted" else "targeted",
        "_admission_deadline": time.monotonic() - 1,
    }
    if route == "command":
        request["command"] = [sys.executable, "-m", "pytest", "tests/test_one.py"]
    else:
        request.update(
            tip_sha="a" * 40,
            candidate_sha="b" * 40,
            test_files=["tests/test_one.py"],
            selection=["tests/test_one.py"],
        )

    result = {
        "command": ci.run_remote_command,
        "targeted": ci.run_targeted,
        "matched": ci.run_matched_pair,
    }[route](request, runner)

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert result["fallback_reason"] == "queue-deadline-exceeded"

def test_daemon_admission_deadline_starts_after_snapshot_preparation(tmp_path, monkeypatch):
    config = _config(tmp_path, token_pool_size=1)
    config = replace(
        config,
        admission=replace(config.admission, max_wait_seconds=0.2),
        poll_seconds=0.005,
    )
    daemon = ci.GateDaemon(config)
    request_path = config.queue_dir / "snapshot-deadline.json"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "snapshot-source"
    _init_repo(source)
    (source / "tracked.txt").write_text("snapshot source\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "snapshot source")
    request = _command_request(request_path.stem, project_id=config.repo)
    request["repo_root"] = str(source)
    request_path.write_text(json.dumps(request), encoding="utf-8")
    submitted_at = 1000.0
    os.utime(request_path, (submitted_at, submitted_at))
    clock = {"now": 10.0}
    monkeypatch.setattr(ci, "time", SimpleNamespace(
        monotonic=lambda: clock["now"], time=lambda: submitted_at + 0.15,
    ))
    released = []
    snapshots = []
    release_snapshot = ci._release_snapshot

    snapshot_worktree = ci._snapshot_worktree

    def slow_snapshot(*args, **kwargs):
        clock["now"] += 0.1
        snapshot = snapshot_worktree(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot

    def track_release(*args):
        released.append(args)
        release_snapshot(*args)

    monkeypatch.setattr(ci, "_snapshot_worktree", slow_snapshot)
    monkeypatch.setattr(ci, "_release_snapshot", track_release)
    admitted = []

    def run_remote_command(request, _runner):
        admitted.append(request["_admission_deadline"])
        return {"schema": ci.RESULT_SCHEMA, "status": "GREEN"}

    monkeypatch.setattr(ci, "run_remote_command", run_remote_command)

    result = daemon._process_request(request_path)

    assert result["status"] == "GREEN"
    assert admitted == [pytest.approx(10.3)]
    snapshot = snapshots[0]
    assert released == [(
        Path(snapshot["root"]), snapshot["ref"], snapshot["snapshot_sha"],
        Path(snapshot["bundle_path"]),
    )]
    assert not Path(snapshot["bundle_path"]).exists()
    ref_status = subprocess.run(
        ["git", "-C", str(source), "show-ref", "--verify", "--quiet", snapshot["ref"]],
        capture_output=True, text=True,
    )
    assert ref_status.returncode != 0


def test_daemon_does_not_admit_request_cancelled_during_snapshot(tmp_path, monkeypatch):
    config = _config(tmp_path, token_pool_size=1)
    daemon = ci.GateDaemon(config)
    request_path = config.queue_dir / "cancelled-snapshot.json"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps(_command_request(request_path.stem, project_id=config.repo)),
        encoding="utf-8",
    )
    source = tmp_path / "cancelled-snapshot-source"
    _init_repo(source)
    (source / "tracked.txt").write_text("snapshot source\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "snapshot source")
    snapshot = ci._snapshot_worktree(
        source, request_path.stem, config.queue_dir,
        cwd_relative="", caller_env={},
    )

    def cancelled_snapshot(request):
        ci._mark_request_cancelled(config, request["request_id"], "wrapper-deadline")
        return request, snapshot

    monkeypatch.setattr(daemon, "_snapshot_command_request", cancelled_snapshot)
    monkeypatch.setattr(
        ci, "run_remote_command",
        lambda *_args: pytest.fail("cancelled request reached remote admission"),
    )

    result = daemon._process_request(request_path)

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert result["fallback_reason"] == "wrapper-cancelled"
    assert not Path(snapshot["bundle_path"]).exists()


def test_daemon_does_not_admit_request_when_caller_dies_during_snapshot(
    tmp_path, monkeypatch,
):
    config = _config(tmp_path, token_pool_size=1)
    daemon = ci.GateDaemon(config)
    request_path = config.queue_dir / "dead-caller-snapshot.json"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "dead-caller-snapshot-source"
    _init_repo(source)
    (source / "tracked.txt").write_text("snapshot source\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "snapshot source")
    request = _command_request(request_path.stem, project_id=config.repo)
    request["repo_root"] = str(source)
    request["caller_identity"] = {"pid": 123, "start_token": "caller-token"}
    request_path.write_text(json.dumps(request), encoding="utf-8")
    alive = {"value": True}
    monkeypatch.setattr(
        ci.goalflight_compat, "process_identity_matches",
        lambda _pid, _token: alive["value"],
    )
    snapshot_request = daemon._snapshot_command_request

    def caller_dies_during_snapshot(request):
        result = snapshot_request(request)
        alive["value"] = False
        return result

    monkeypatch.setattr(daemon, "_snapshot_command_request", caller_dies_during_snapshot)
    monkeypatch.setattr(
        ci, "run_remote_command",
        lambda *_args: pytest.fail("abandoned request reached remote admission"),
    )

    result = daemon._process_request(request_path)

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert result["fallback_reason"] == "wrapper-cancelled"


def _daemon_project_repo(root):
    _init_repo(root)
    tests_dir = root / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_one.py"
    test_file.write_text("def test_one(): pass\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    test_file.write_text("def test_one(): assert True\n", encoding="utf-8")
    _git(root, "commit", "--quiet", "-am", "candidate")
    return base_sha, _git(root, "rev-parse", "HEAD").stdout.strip()


def _box_command_daemon(tmp_path, repo, *, cancelled=False):
    config = replace(_config(tmp_path), admission_mode="box")
    request_id = "resident-daemon-cancel" if cancelled else "resident-daemon-collect"
    request_path = config.queue_dir / f"{request_id}.json"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request = _command_request(request_id, repo_root=repo, project_id=config.repo)
    request["box"] = "box-a"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    node = _DaemonResidentNode(config, cancelled=cancelled)
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: pytest.fail("unexpected transport"))
    runner.nodes["box-a"] = node
    runner.choose_box = lambda **_kwargs: None
    runner.reap = lambda: []
    return config, request_path, node, runner


def test_blocking_rollback_cancels_wrapper_give_up_before_checkpoint_cleanup(
    tmp_path,
):
    repo = tmp_path / "project"
    _daemon_project_repo(repo)
    config, request_path, node, runner = _box_command_daemon(tmp_path, repo)
    assert ci.GateDaemon(config, runner=runner).poll_once()["status"] == "SUBMITTED"
    ci._mark_request_cancelled(config, request_path.stem, "wrapper-gave-up")
    config.result_dir.mkdir(parents=True, exist_ok=True)
    ci._atomic_write_json(config.result_dir / f"{request_path.stem}.json", {
        "schema": ci.RESULT_SCHEMA, "request_id": request_path.stem,
        "status": "REMOTE_UNAVAILABLE",
    })

    blocking_config = replace(config, admission_mode="blocking")
    blocking_runner = RemoteRunner(
        blocking_config,
        executor=lambda *_args, **_kwargs: pytest.fail("accepted ticket ran locally"),
    )
    blocking_runner.nodes["box-a"] = node
    blocking_runner.choose_box = lambda **_kwargs: pytest.fail(
        "accepted ticket entered blocking placement",
    )
    blocking_runner.reap = lambda: []

    result = ci.GateDaemon(blocking_config, runner=blocking_runner).poll_once()

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert node.calls.count("cancel") == 1
    assert node.calls.count("enqueue") == 0
    assert node.calls.count("status") == 1
    assert not request_path.exists()
    assert not ci._request_cancel_path(blocking_config, request_path.stem).exists()
    published = json.loads(
        (blocking_config.result_dir / f"{request_path.stem}.json").read_text(),
    )
    assert published["status"] == "LOCAL_FALLBACK_REQUIRED"


def test_blocking_queue_expiry_keeps_resident_collector_checkpoint(
    tmp_path,
):
    repo = tmp_path / "project"
    _daemon_project_repo(repo)
    config, resident_path, node, runner = _box_command_daemon(tmp_path, repo)
    assert ci.GateDaemon(config, runner=runner).poll_once()["status"] == "SUBMITTED"
    node.running_status_polls = 1

    one_seat = replace(config.boxes["box-a"], max_concurrent_seats=1)
    blocking_config = replace(
        config, admission_mode="blocking", boxes={"box-a": one_seat},
        admission=replace(config.admission, max_wait_seconds=0.04),
        poll_seconds=0.005,
    )
    blocking_runner = RemoteRunner(
        blocking_config,
        executor=lambda *_args, **_kwargs: pytest.fail("accepted ticket ran locally"),
    )
    blocking_runner.nodes["box-a"] = node
    blocking_runner.choose_box = lambda **_kwargs: pytest.fail(
        "accepted ticket entered blocking placement",
    )
    blocking_runner.reap = lambda: []
    daemon = ci.GateDaemon(blocking_config, runner=blocking_runner)
    running_path = blocking_config.queue_dir / "01-blocking-request.json"
    running_path.write_text(
        json.dumps(_command_request(running_path.stem, project_id=blocking_config.repo)),
        encoding="utf-8",
    )
    original_process = daemon._process_request

    class StopLoop(Exception):
        pass

    def pending_paths():
        running_result = blocking_config.result_dir / f"{running_path.stem}.json"
        resident_result = blocking_config.result_dir / f"{resident_path.stem}.json"
        if (running_result.exists()
                and (node.calls.count("status") >= 1 or resident_result.exists()
                     or not resident_path.exists())):
            raise StopLoop
        return [path for path in (running_path, resident_path) if path.exists()]

    def process(path, *, immediate_failure_reason=None):
        if path == running_path and immediate_failure_reason is None:
            time.sleep(0.15)
            return {
                "schema": ci.RESULT_SCHEMA,
                "request_id": path.stem,
                "status": "GREEN",
            }
        return original_process(path, immediate_failure_reason=immediate_failure_reason)

    daemon.pending_paths = pending_paths
    daemon._process_request = process
    with _independent_deadline(), pytest.raises(StopLoop):
        daemon.run()

    assert json.loads(
        (blocking_config.result_dir / f"{running_path.stem}.json").read_text(),
    )["status"] == "GREEN"
    assert resident_path.is_file()
    assert not (blocking_config.result_dir / f"{resident_path.stem}.json").exists()
    assert node.calls.count("submit") == 1
    assert node.calls.count("status") == 1
    assert node.calls.count("enqueue") == 0


def test_box_daemon_restarts_and_collects_accepted_command_without_local_worktree(
    tmp_path,
):
    repo = tmp_path / "project"
    _daemon_project_repo(repo)
    config, request_path, node, runner = _box_command_daemon(tmp_path, repo)
    first = ci.GateDaemon(config, runner=runner).poll_once()
    assert first["status"] == "SUBMITTED"
    assert node.calls.count("submit") == 1
    saved = json.loads(request_path.read_text(encoding="utf-8"))
    candidate_sha = saved["candidate_sha"]
    assert saved["_resident"]["arms"]["candidate"]["phase"] == "accepted"
    assert not (config.result_dir / f"{request_path.stem}.json").exists()

    # Simulate the old cleanup ordering: accepted ownership must still collect
    # when the local ref and bundle are gone.
    ci._release_snapshot(
        Path(saved["snapshot_root"]), saved["snapshot_ref"], saved["snapshot_sha"],
        Path(saved["bundle_path"]),
    )
    shutil.rmtree(repo)
    second = ci.GateDaemon(config, runner=runner).poll_once()

    result_path = config.result_dir / f"{request_path.stem}.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert second["status"] == result["status"] == "GREEN"
    assert result["candidate"]["receipt"]["hostname"] == "measured-node"
    assert node.calls.count("submit") == 1
    assert node.calls.count("status") == 1
    assert node.calls.count("fetch") == 2
    assert not request_path.exists()


def test_box_daemon_collects_checkpoint_after_restart_in_blocking_mode(tmp_path):
    repo = tmp_path / "project"
    _daemon_project_repo(repo)
    config, request_path, node, _runner = _box_command_daemon(tmp_path, repo)
    submitted = ci.GateDaemon(config, runner=_runner).poll_once()
    assert submitted["status"] == "SUBMITTED"
    assert request_path.is_file()
    assert node.calls.count("submit") == 1

    blocking_config = replace(config, admission_mode="blocking")
    blocking_runner = RemoteRunner(
        blocking_config,
        executor=lambda *_args, **_kwargs: pytest.fail("accepted ticket ran locally"),
    )
    blocking_runner.nodes["box-a"] = node
    blocking_runner.choose_box = lambda **_kwargs: pytest.fail(
        "accepted ticket entered blocking placement",
    )
    blocking_runner.reap = lambda: []

    collected = ci.GateDaemon(blocking_config, runner=blocking_runner).poll_once()

    result_path = blocking_config.result_dir / f"{request_path.stem}.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert collected["status"] == result["status"] == "GREEN"
    assert node.calls.count("submit") == 1
    assert node.calls.count("status") == 1
    assert node.calls.count("fetch") == 2
    assert not request_path.exists()


def test_box_daemon_recovers_matched_pair_checkpoint_in_blocking_mode(tmp_path):
    repo = tmp_path / "project"
    base_sha, candidate_sha = _daemon_project_repo(repo)
    config = replace(_config(tmp_path), admission_mode="box")
    request_id = "resident-pair-mode-rollback"
    request_path = config.queue_dir / f"{request_id}.json"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps({
        "schema": ci.REQUEST_SCHEMA, "request_id": request_id,
        "project_id": config.repo, "kind": "gate", "repo_root": str(repo),
        "tip_sha": base_sha, "candidate_sha": candidate_sha,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py"], "box": "box-a",
    }), encoding="utf-8")
    node = _DaemonResidentNode(config)
    runner = RemoteRunner(
        config, executor=lambda *_args, **_kwargs: pytest.fail("unexpected transport"),
    )
    runner.nodes["box-a"] = node
    runner.choose_box = lambda **_kwargs: None
    runner.reap = lambda: []

    submitted = ci.GateDaemon(config, runner=runner).poll_once()
    saved = json.loads(request_path.read_text(encoding="utf-8"))
    assert submitted["status"] == "SUBMITTED"
    assert saved["_resident"]["arms"]["base"]["phase"] == "accepted"
    assert "candidate" not in saved["_resident"]["arms"]
    assert node.calls.count("submit") == 1

    blocking_config = replace(config, admission_mode="blocking")
    blocking_runner = RemoteRunner(
        blocking_config,
        executor=lambda *_args, **_kwargs: pytest.fail("accepted ticket ran locally"),
    )
    blocking_runner.nodes["box-a"] = node
    blocking_runner.choose_box = lambda **_kwargs: None
    blocking_runner.reap = lambda: []
    blocking_daemon = ci.GateDaemon(blocking_config, runner=blocking_runner)

    resumed = blocking_daemon.poll_once()
    assert resumed["status"] == "SUBMITTED"
    assert request_path.is_file()
    assert node.calls.count("submit") == 2
    assert node.calls.count("status") == 1
    assert node.calls.count("fetch") == 2
    assert "enqueue" not in node.calls

    collected = blocking_daemon.poll_once()
    result_path = blocking_config.result_dir / f"{request_id}.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert collected["status"] == result["status"] == "GREEN"
    assert node.calls.count("submit") == 2
    assert node.calls.count("status") == 3
    assert node.calls.count("fetch") == 6
    assert "enqueue" not in node.calls
    assert not request_path.exists()


def test_box_daemon_retains_accepted_ticket_while_its_box_is_unmapped(tmp_path):
    repo = tmp_path / "project"
    _daemon_project_repo(repo)
    config, request_path, node, runner = _box_command_daemon(tmp_path, repo)
    daemon = ci.GateDaemon(config, runner=runner)

    submitted = daemon.poll_once()
    assert submitted["status"] == "SUBMITTED"
    assert node.calls.count("submit") == 1
    del runner.nodes["box-a"]

    unavailable = daemon.poll_once()

    assert unavailable["status"] == "SUBMITTED"
    assert "box-a" in unavailable["candidate"]["error"]
    assert request_path.is_file()
    assert not (config.result_dir / f"{request_path.stem}.json").exists()
    assert node.calls.count("submit") == 1
    assert node.calls.count("status") == 0
    assert node.calls.count("fetch") == 0

    runner.nodes["box-a"] = node
    collected = daemon.poll_once()

    result = json.loads((config.result_dir / f"{request_path.stem}.json").read_text())
    assert collected["status"] == result["status"] == "GREEN"
    assert node.calls.count("submit") == 1
    assert node.calls.count("status") == 1
    assert node.calls.count("fetch") == 2
    assert not request_path.exists()


@pytest.mark.parametrize("remove_worktree", [False, True], ids=["selector", "worktree"])
def test_box_daemon_collects_accepted_profiled_ticket_after_local_source_is_removed(
    tmp_path, remove_worktree,
):
    repo = tmp_path / "project"
    base_sha, candidate_sha = _daemon_project_repo(repo)
    config_dir = repo / ".goal-flight"
    config_dir.mkdir()
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {
        "interpreter": sys.executable,
        "version_probe": [sys.executable, "--version"],
    }
    config_path = config_dir / "remote-ci.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = replace(_load_fake_node_config(config_path), admission_mode="box")
    request_id = "profile-selector-removed-after-acceptance"
    request_path = config.queue_dir / f"{request_id}.json"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps({
        "schema": ci.REQUEST_SCHEMA, "request_id": request_id,
        "project_id": config.repo, "kind": "targeted",
        "tip_sha": base_sha, "candidate_sha": candidate_sha,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py"], "box": "box-a",
    }), encoding="utf-8")
    node = _DaemonResidentNode(config)
    runner = RemoteRunner(config, executor=lambda *_args: pytest.fail("unexpected transport"))
    runner.nodes["box-a"] = node
    runner.choose_box = lambda **_kwargs: "box-a"
    runner.reap = lambda: []

    first = ci.GateDaemon(config, runner=runner).poll_once()
    assert first["status"] == "SUBMITTED"
    assert node.calls.count("submit") == 1
    if remove_worktree:
        shutil.rmtree(repo)
    else:
        (repo / "tests" / "test_one.py").unlink()

    second = ci.GateDaemon(config, runner=runner).poll_once()

    result = json.loads((config.result_dir / f"{request_id}.json").read_text())
    assert second["status"] == result["status"] == "GREEN"
    assert node.calls.count("submit") == 1
    assert node.calls.count("status") == 1
    assert node.calls.count("fetch") == 2
    assert not request_path.exists()


def test_box_daemon_reconciles_lost_submit_reply_after_selector_removal(tmp_path):
    repo = tmp_path / "project"
    base_sha, candidate_sha = _daemon_project_repo(repo)
    config_dir = repo / ".goal-flight"
    config_dir.mkdir()
    raw = _profile_raw_config(tmp_path)
    raw["environment"] = {
        "interpreter": sys.executable,
        "version_probe": [sys.executable, "--version"],
    }
    config_path = config_dir / "remote-ci.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = replace(_load_fake_node_config(config_path), admission_mode="box")
    request_id = "lost-submit-reply-selector-removed"
    request_path = config.queue_dir / f"{request_id}.json"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps({
        "schema": ci.REQUEST_SCHEMA, "request_id": request_id,
        "project_id": config.repo, "kind": "targeted",
        "tip_sha": base_sha, "candidate_sha": candidate_sha,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py"], "box": "box-a",
    }), encoding="utf-8")
    node = _DaemonResidentNode(
        config, lose_submit_reply=True, lookup_unreachable=True,
        running_status_polls=1,
    )
    runner = RemoteRunner(config, executor=lambda *_args: pytest.fail("unexpected transport"))
    runner.nodes["box-a"] = node
    runner.choose_box = lambda **_kwargs: "box-a"
    runner.reap = lambda: []

    first = ci.GateDaemon(config, runner=runner).poll_once()
    assert first["status"] == "SUBMITTED"
    saved = json.loads(request_path.read_text())
    assert saved["_resident"]["arms"]["candidate"]["phase"] == "submitting"
    assert node.calls.count("submit") == 1

    (repo / "tests" / "test_one.py").unlink()
    second = ci.GateDaemon(config, runner=runner).poll_once()
    assert second["status"] == "SUBMITTED"
    assert request_path.is_file()
    assert not (config.result_dir / f"{request_id}.json").exists()
    assert node.calls.count("list") == 1
    assert node.calls.count("status") == 0

    node.lookup_unreachable = False
    third = ci.GateDaemon(config, runner=runner).poll_once()
    assert third["status"] == "SUBMITTED"
    saved = json.loads(request_path.read_text())
    assert saved["_resident"]["arms"]["candidate"]["phase"] == "collecting"

    fourth = ci.GateDaemon(config, runner=runner).poll_once()
    result = json.loads((config.result_dir / f"{request_id}.json").read_text())
    assert fourth["status"] == result["status"] == "GREEN"
    assert node.calls.count("submit") == 1
    assert node.calls.count("list") == 2
    assert node.calls.count("status") == 2
    assert node.calls.count("fetch") == 2
    assert not request_path.exists()


def test_box_daemon_collects_after_private_upload_is_published_late(tmp_path):
    repo = tmp_path / "project"
    _daemon_project_repo(repo)
    config, request_path, node, runner = _box_command_daemon(tmp_path, repo)
    node.lose_submit_reply = True
    node.publish_ticket = False

    first = ci.GateDaemon(config, runner=runner).poll_once()
    assert first["status"] == "SUBMITTED"
    shutil.rmtree(repo)
    second = ci.GateDaemon(config, runner=runner).poll_once()

    assert second["status"] == "SUBMITTED"
    assert request_path.is_file()
    assert json.loads(request_path.read_text())["_resident"]["arms"]["candidate"]["phase"] == "submitting"
    assert not (config.result_dir / f"{request_path.stem}.json").exists()
    assert node.calls.count("submit") == 1
    assert node.calls.count("list") == 1
    assert node.calls.count("status") == 0

    node.ticket_record = node.pending_ticket_record
    third = ci.GateDaemon(config, runner=runner).poll_once()
    result = json.loads((config.result_dir / f"{request_path.stem}.json").read_text())
    assert third["status"] == result["status"] == "GREEN"
    assert node.calls.count("submit") == 1
    assert node.calls.count("list") == 2
    assert node.calls.count("status") == 1
    assert node.calls.count("fetch") == 2
    assert not request_path.exists()


def test_box_daemon_keeps_unreadable_ticket_checkpoint_until_readable(tmp_path):
    repo = tmp_path / "project"
    _daemon_project_repo(repo)
    config, request_path, node, runner = _box_command_daemon(tmp_path, repo)
    node.lose_submit_reply = True
    node.unreadable = True

    first = ci.GateDaemon(config, runner=runner).poll_once()
    assert first["status"] == "SUBMITTED"
    shutil.rmtree(repo)
    second = ci.GateDaemon(config, runner=runner).poll_once()

    assert second["status"] == "SUBMITTED"
    assert request_path.is_file()
    assert json.loads(request_path.read_text())["_resident"]["arms"]["candidate"]["phase"] == "submitting"
    assert not (config.result_dir / f"{request_path.stem}.json").exists()
    assert node.calls.count("submit") == 1
    assert node.calls.count("list") == 1
    assert node.calls.count("status") == 0

    node.unreadable = False
    third = ci.GateDaemon(config, runner=runner).poll_once()
    result = json.loads((config.result_dir / f"{request_path.stem}.json").read_text())
    assert third["status"] == result["status"] == "GREEN"
    assert node.calls.count("submit") == 1
    assert node.calls.count("list") == 2
    assert node.calls.count("status") == 1
    assert node.calls.count("fetch") == 2
    assert not request_path.exists()


def test_box_daemon_restart_cancels_an_accepted_command_ticket(tmp_path):
    repo = tmp_path / "project"
    _daemon_project_repo(repo)
    config, request_path, node, runner = _box_command_daemon(
        tmp_path, repo, cancelled=True,
    )
    assert ci.GateDaemon(config, runner=runner).poll_once()["status"] == "SUBMITTED"
    saved = json.loads(request_path.read_text(encoding="utf-8"))
    candidate_sha = saved["candidate_sha"]
    shutil.rmtree(repo)
    ci._mark_request_cancelled(config, request_path.stem, "wrapper-deadline")

    collected = []
    original_run_arm = runner.run_arm

    def record_spec(spec, **kwargs):
        collected.append(spec.candidate_sha)
        return original_run_arm(spec, **kwargs)

    runner.run_arm = record_spec
    result = ci.GateDaemon(config, runner=runner).poll_once()

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert result["remote_ticket_accepted"] is True
    assert collected == [candidate_sha]
    assert node.calls.count("submit") == 1
    assert node.calls.count("cancel") == 1
    assert not request_path.exists()


@pytest.mark.parametrize("kind", ["targeted", "gate"])
def test_box_daemon_snapshots_targeted_and_matched_pair_requests(
    tmp_path, monkeypatch, kind,
):
    repo = tmp_path / "project"
    base_sha, candidate_sha = _daemon_project_repo(repo)
    config = replace(_config(tmp_path), admission_mode="box")
    request_id = f"ordinary-{kind}"
    request_path = config.queue_dir / f"{request_id}.json"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request = {
        "schema": ci.REQUEST_SCHEMA, "request_id": request_id,
        "project_id": config.repo, "kind": kind, "repo_root": str(repo),
        "tip_sha": base_sha, "candidate_sha": candidate_sha,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py"], "box": "box-a",
    }
    request_path.write_text(json.dumps(request), encoding="utf-8")
    releases = []
    release_snapshot = ci._release_snapshot

    def verify_release_after_publish(root, ref, sha, bundle):
        assert (config.result_dir / f"{request_id}.json").is_file()
        releases.append((ref, sha, bundle))
        release_snapshot(root, ref, sha, bundle)

    monkeypatch.setattr(ci, "_release_snapshot", verify_release_after_publish)

    def run_request(queued, _runner):
        persisted = json.loads(request_path.read_text(encoding="utf-8"))
        assert persisted["candidate_sha"] == candidate_sha
        assert persisted["snapshot_root"] == str(repo)
        if kind == "gate":
            base, candidate = ci.build_pair_specs(queued, config)
            specs = (base, candidate)
            assert base.snapshot_sha == base_sha
            assert candidate.snapshot_sha == candidate_sha
            assert base.bundle_path != candidate.bundle_path
        else:
            specs = (ci.ArmSpec(
                arm="candidate", sha=candidate_sha, tip_sha=base_sha,
                candidate_sha=candidate_sha, test_files=tuple(queued["test_files"]),
                selection=tuple(queued["selection"]), request_id=request_id,
                box="box-a", overlay=False,
                snapshot_sha=queued["snapshot_sha"],
                bundle_path=Path(queued["bundle_path"]),
                snapshot_root=Path(queued["snapshot_root"]),
                snapshot_ref=queued["snapshot_ref"],
            ),)
        for spec in specs:
            bundle = ci._create_snapshot_bundle(
                spec.snapshot_root, spec.snapshot_ref, spec.bundle_path,
                spec.snapshot_sha, None,
            )
            assert bundle is not None and bundle.is_file()
            verified = subprocess.run(
                ["git", "-C", str(repo), "bundle", "verify", str(bundle)],
                capture_output=True, text=True, timeout=15,
            )
            assert verified.returncode == 0, verified.stderr
        return {"schema": ci.RESULT_SCHEMA, "request_id": request_id, "status": "GREEN"}

    monkeypatch.setattr(ci, "run_targeted" if kind == "targeted" else "run_matched_pair", run_request)
    daemon = ci.GateDaemon(config, runner=SimpleNamespace(reap=lambda: None))
    result = daemon.poll_once()

    assert result["status"] == "GREEN"
    assert len(releases) == (2 if kind == "gate" else 1)
    assert not request_path.exists()
    assert not list(config.queue_dir.glob("*.bundle"))


def test_blocking_matched_pair_keeps_shared_candidate_snapshot(tmp_path):
    config = _config(tmp_path)
    candidate_bundle = config.queue_dir / "blocking-pair.bundle"
    request = {
        "request_id": "blocking-pair", "box": "box-a",
        "tip_sha": "a" * 40, "candidate_sha": "b" * 40,
        "snapshot_sha": "b" * 40, "bundle_path": str(candidate_bundle),
        "snapshot_root": str(tmp_path), "snapshot_ref": "refs/candidate",
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py"],
    }

    base, candidate = ci.build_pair_specs(request, config)

    assert base.snapshot_sha == candidate.snapshot_sha == request["snapshot_sha"]
    assert base.bundle_path == candidate.bundle_path == candidate_bundle
    assert base.snapshot_root == candidate.snapshot_root == Path(tmp_path)
    assert base.snapshot_ref == candidate.snapshot_ref == request["snapshot_ref"]


def test_box_matched_pair_executes_on_a_cold_fake_box(node_env, tmp_path):
    reason = launchctl_unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    _default_config, executor, _default_runner, _node = node_env
    measured_hostname = ci.socket.gethostname()
    executor.hostname = measured_hostname
    repo = tmp_path / "project"
    base_sha, candidate_sha = _daemon_project_repo(repo)
    config_dir = repo / ".goal-flight"
    config_dir.mkdir()
    raw = _profile_raw_config(tmp_path)
    raw["boxes"]["box-a"]["expected_hostname"] = measured_hostname
    raw["environment"] = {
        "interpreter": sys.executable,
        "version_probe": [sys.executable, "--version"],
    }
    config_path = config_dir / "remote-ci.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = replace(_load_fake_node_config(config_path), admission_mode="box")
    assert not (executor.root / "repos" / config.repo / "mirror.git").exists()
    request_id = "cold-box-matched-pair"
    request_path = config.queue_dir / f"{request_id}.json"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps({
        "schema": ci.REQUEST_SCHEMA, "request_id": request_id,
        "project_id": config.repo, "kind": "gate", "repo_root": str(repo),
        "tip_sha": base_sha, "candidate_sha": candidate_sha,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py"], "box": "box-a",
    }), encoding="utf-8")
    runner = RemoteRunner(config, executor=executor)
    daemon = ci.GateDaemon(config, runner=runner)
    deadline = time.monotonic() + 90
    result = None
    while time.monotonic() < deadline:
        result = daemon.poll_once()
        if result is not None and result.get("status") != "SUBMITTED":
            break
        time.sleep(0.05)

    assert result is not None and result["status"] == "GREEN", result
    assert result["base"]["returncode"] == result["candidate"]["returncode"] == 0
    assert result["base"]["receipt"]["hostname"] == measured_hostname
    assert result["candidate"]["receipt"]["hostname"] == measured_hostname
    assert not request_path.exists()


def test_blocking_command_snapshot_is_not_persisted(tmp_path, monkeypatch):
    repo = tmp_path / "project"
    _daemon_project_repo(repo)
    config = _config(tmp_path)
    request = _command_request("blocking-snapshot", repo_root=repo, project_id=config.repo)
    request_path = config.queue_dir / "blocking-snapshot.json"
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(ci, "run_remote_command", lambda queued, _runner: {
        "schema": ci.RESULT_SCHEMA, "request_id": queued["request_id"], "status": "GREEN",
    })

    result = ci.GateDaemon(config)._process_request(request_path)

    assert result["status"] == "GREEN"
    assert json.loads(request_path.read_text(encoding="utf-8")) == request
    assert not (config.queue_dir / f"{request_path.stem}.bundle").exists()


def test_unknown_caller_identity_does_not_cancel_request(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(
        ci.goalflight_compat, "process_identity_matches", lambda _pid, _token: None,
    )

    assert not ci._request_cancelled(
        config, "unknown-caller", {"pid": 123, "start_token": "caller-token"},
    )


def test_fail_policy_refuses_saturated_daemon_executor_without_later_run(tmp_path):
    config = _config(tmp_path, token_pool_size=1)
    config = replace(
        config,
        fallback="fail",
        admission=replace(config.admission, max_wait_seconds=2),
        poll_seconds=0.005,
    )
    daemon = ci.GateDaemon(config)
    requests = [config.queue_dir / "01-running.json", config.queue_dir / "02-fail.json"]
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    for path in requests:
        path.write_text(
            json.dumps(_command_request(path.stem, project_id=config.repo)),
            encoding="utf-8",
        )
    running_started = threading.Event()
    running_finished = threading.Event()
    refused_while_busy = []
    processed = []
    original_process = daemon._process_request

    class StopLoop(Exception):
        pass

    def pending_paths():
        if all((config.result_dir / f"{path.stem}.json").exists() for path in requests):
            raise StopLoop
        if not running_started.is_set():
            return [requests[0]] if requests[0].exists() else []
        if not running_finished.is_set():
            return [requests[1]] if requests[1].exists() else []
        return [path for path in requests if path.exists()]

    def process(path, *, immediate_failure_reason=None):
        if immediate_failure_reason is not None:
            refused_while_busy.append(not running_finished.is_set())
            return original_process(
                path, immediate_failure_reason=immediate_failure_reason,
            )
        processed.append(path.stem)
        if path == requests[0]:
            running_started.set()
            time.sleep(0.15)
            running_finished.set()
        return {
            "schema": ci.RESULT_SCHEMA,
            "request_id": path.stem,
            "project_id": config.repo,
            "status": "GREEN",
        }

    daemon.pending_paths = pending_paths
    daemon._process_request = process
    with _independent_deadline(), pytest.raises(StopLoop):
        daemon.run()

    refused = json.loads((config.result_dir / "02-fail.json").read_text())
    assert refused["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert refused["fallback_reason"] == "daemon-capacity-refused"
    assert refused["project_id"] == config.repo
    assert refused["policy_outcome"] == "failed"
    assert refused_while_busy == [True]
    assert processed == ["01-running"]


def test_command_request_rejects_base_sha(tmp_path):
    config = _config(tmp_path)
    request = _command_request()
    request["kind"] = "targeted"
    request["base_sha"] = "a" * 40

    with pytest.raises(ci.RemoteCIError, match="base_sha command requests must use kind gate"):
        ci.validate_request(request, config)


def test_wrapper_rejects_base_without_pair_using_v6_contract(monkeypatch, capfd):
    local_calls = []
    monkeypatch.setattr(ci, "_run_local_command", lambda *args, **kwargs: local_calls.append(args))

    with pytest.raises(SystemExit) as caught:
        ci.wrapper_main([
            "--kind", "gate", "--cmd-id", "heavy-gate", "--base", "HEAD", "--",
            sys.executable, "-c", "pass",
        ])

    assert caught.value.code == 2
    assert "--base, --recheck, and --json require --pair" in capfd.readouterr().err
    assert not local_calls


def test_unwritable_timing_dir_does_not_fail_the_request(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_TIMING_DIR", str(blocker / "timing"))
    node._begin_timing_record({"operation": "status"})
    assert node._TIMING_RECORD is None
    node._write_timing_record()
