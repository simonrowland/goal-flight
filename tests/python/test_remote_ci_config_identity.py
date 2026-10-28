"""Regression tests for remote-CI daemon config identity and admission."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import goalflight_remote_ci as ci
from goalflight_remote_ci import GateDaemon, load_config, parse_receipt


def _raw_config(tmp_path: Path) -> dict:
    return {
        "schema": "goalflight.remote-ci.config.v2",
        "paths": {
            "queue_dir": str(tmp_path / "queue"),
            "state_dir": str(tmp_path / "state"),
            "result_dir": str(tmp_path / "results"),
        },
        "daemon": {
            "lock_file": str(tmp_path / "state" / "daemon.lock"),
            "pid_file": str(tmp_path / "state" / "daemon.pid"),
            "poll_seconds": 0.01,
        },
        "boxes": {
            "box-a": {
                "host": "ci-worker.example.invalid",
                "expected_hostname": "measured-node",
                "python_interpreter": sys.executable,
                "p_cores": 20,
                "token_pool_size": 2,
                "remote_exec": ["scripted-remote", "{host}", "{script}"],
                "env": {},
                "managed_run_directory": str(tmp_path / "fake-node"),
            }
        },
        "admission": {"queue_wait_seconds": 0.01},
        "runner": {
            "command": ["run", "{arm}", "{sha}", "{selection}"],
            "test_command": ["python3", "-m", "pytest"],
            "env": {},
            "timeout_seconds": 30,
            "self_cap": 4,
            "self_cap_env": "TEST_WORKERS",
            "base_collection_option": "--continue-on-collection-errors",
        },
        "selection": {
            "path_prefixes": ["tests/"],
            "allowed_options": ["-q", "-v", "--continue-on-collection-errors"],
            "value_options": ["-k", "-m"],
            "allowed_option_prefixes": [],
            "require_selector": True,
            "max_targeted_files": 20,
        },
    }


def _config(tmp_path: Path) -> ci.DaemonConfig:
    path = tmp_path / "remote-ci.json"
    path.write_text(json.dumps(_raw_config(tmp_path)), encoding="utf-8")
    managed_path = ci._managed_path
    try:
        ci._managed_path = lambda value, label: (
            Path(value) if label.endswith("managed_run_directory")
            and Path(value).is_absolute() else managed_path(value, label)
        )
        return load_config(path)
    finally:
        ci._managed_path = managed_path


def _queue_request(config: ci.DaemonConfig, request_id: str) -> Path:
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request_path = config.queue_dir / f"{request_id}.json"
    request_path.write_text("{}", encoding="utf-8")
    return request_path


def _assert_identity_was_loaded(config: ci.DaemonConfig) -> ci.ConfigIdentity:
    identity = getattr(config, "config_identity", None)
    assert identity is not None
    return identity


def test_receipt_carries_loaded_config_identity_and_exposes_divergence(tmp_path: Path):
    config = _config(tmp_path)
    identity = _assert_identity_was_loaded(config)
    loaded_bytes = config.path.read_bytes()

    assert identity.sha256 == hashlib.sha256(loaded_bytes).hexdigest()
    assert identity.mtime_ns == config.path.stat().st_mtime_ns
    assert identity.size_bytes == len(loaded_bytes)
    receipt = parse_receipt({
        "hostname": "measured-node",
        **ci._daemon_config_receipt_metadata(config),
    })
    assert receipt.metadata["daemon_config_sha256"] == identity.sha256
    assert receipt.metadata["daemon_config_mtime_ns"] == identity.mtime_ns
    assert receipt.metadata["daemon_config_size_bytes"] == identity.size_bytes

    changed_config = _raw_config(tmp_path)
    changed_config["daemon"]["poll_seconds"] = 0.02
    changed_bytes = json.dumps(changed_config).encode("utf-8")
    original_stat = config.path.stat()
    assert len(changed_bytes) == identity.size_bytes
    config.path.write_bytes(changed_bytes)
    os.utime(config.path, ns=(original_stat.st_atime_ns, identity.mtime_ns))
    current_identity = ci._read_config_identity(config.path)
    assert receipt.metadata["daemon_config_sha256"] != current_identity.sha256
    assert receipt.metadata["daemon_config_mtime_ns"] == current_identity.mtime_ns
    assert receipt.metadata["daemon_config_size_bytes"] == current_identity.size_bytes


def _assert_receipt_identity(receipt: ci.RemoteReceipt, identity: ci.ConfigIdentity) -> None:
    assert receipt.metadata["daemon_config_sha256"] == identity.sha256
    assert receipt.metadata["daemon_config_mtime_ns"] == identity.mtime_ns
    assert receipt.metadata["daemon_config_size_bytes"] == identity.size_bytes


def _runner_spec(request_id: str) -> ci.ArmSpec:
    return ci.ArmSpec(
        arm="candidate",
        sha="b" * 40,
        tip_sha="a" * 40,
        candidate_sha="b" * 40,
        test_files=("tests/test_one.py",),
        selection=("tests/test_one.py", "-q"),
        request_id=request_id,
        box="box-a",
        overlay=False,
    )


def test_runner_watch_result_carries_config_identity(tmp_path: Path):
    config = _config(tmp_path)
    identity = _assert_identity_was_loaded(config)
    runner = ci.RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    receipt_text = json.dumps({
        "hostname": "measured-node",
        "status": "pass",
        "returncode": 0,
        "passed": 1,
    })
    stream = {"tail": {"text": receipt_text, "truncated": False, "omitted_bytes": 0}}
    empty_stream = {"tail": {"text": "", "truncated": False, "omitted_bytes": 0}}
    record = {
        "sample": {"hostname": "measured-node"},
        "result": {
            "status": "finished",
            "returncode": 0,
            "exit_known": True,
            "command_started": True,
            "streams": {"stdout": stream, "stderr": empty_stream},
        },
    }

    class Node:
        box = config.boxes["box-a"]
        pinned_host = None

        def call(self, operation, **_kwargs):
            assert operation == "status"
            return record

    outcome = runner._watch(
        _runner_spec("runner-watch-receipt"),
        Node(),
        {"run_dir": "/runs/watch", "lease_token": "watch-token"},
        {},
    )

    assert outcome.receipt is not None
    assert outcome.status == "green"
    _assert_receipt_identity(outcome.receipt, identity)


def test_resident_runner_result_carries_config_identity(tmp_path: Path):
    config = _config(tmp_path)
    identity = _assert_identity_was_loaded(config)
    runner = ci.RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    receipt_body = json.dumps({
        "hostname": "measured-node",
        "status": "pass",
        "returncode": 0,
        "passed": 1,
    }).encode("utf-8")
    compressed = gzip.compress(receipt_body)

    class Node:
        box = config.boxes["box-a"]

        def call(self, operation, **kwargs):
            assert operation == "fetch"
            body = compressed if kwargs["stream"] == "stdout" else gzip.compress(b"")
            return {
                "status": "ok",
                "compression": "gzip",
                "body_gzip_base64": base64.b64encode(body).decode("ascii"),
                "size_bytes": len(receipt_body) if kwargs["stream"] == "stdout" else 0,
                "sha256": hashlib.sha256(
                    receipt_body if kwargs["stream"] == "stdout" else b"",
                ).hexdigest(),
                "truncated": False,
            }

    outcome = runner._resident_terminal_outcome(
        _runner_spec("resident-runner-receipt"),
        Node(),
        {"run_dir": "/runs/resident", "lease_token": "resident-token"},
        {
            "sample": {"hostname": "measured-node"},
            "result": {
                "status": "finished",
                "returncode": 0,
                "exit_known": True,
                "command_started": True,
            },
        },
    )

    assert outcome is not None
    assert outcome.receipt is not None
    assert outcome.status == "green"
    _assert_receipt_identity(outcome.receipt, identity)


def test_config_snapshot_rejects_path_replaced_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    config_path = tmp_path / "remote-ci.json"
    replacement_path = tmp_path / "replacement.json"
    config_path.write_bytes(b"original config")
    replacement_path.write_bytes(b"replacement config")
    real_fstat = os.fstat
    calls = 0

    def replace_path_after_read(file_descriptor: int):
        nonlocal calls
        result = real_fstat(file_descriptor)
        calls += 1
        if calls == 2:
            os.replace(replacement_path, config_path)
        return result

    monkeypatch.setattr(ci.os, "fstat", replace_path_after_read)

    with pytest.raises(ci.ConfigError, match="changed while it was being read"):
        ci._read_config_snapshot(config_path)


def test_changed_config_refuses_new_admission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _config(tmp_path)
    _assert_identity_was_loaded(config)
    request_path = _queue_request(config, "changed-config")
    daemon = GateDaemon(config, runner=SimpleNamespace(reap=lambda: None))
    monkeypatch.setattr(
        daemon, "_process_request",
        lambda _path: pytest.fail("stale configuration admitted new work"),
    )
    changed_config = _raw_config(tmp_path)
    changed_config["daemon"]["poll_seconds"] = 0.02
    config.path.write_text(json.dumps(changed_config), encoding="utf-8")

    result = daemon.poll_once()

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert result["fallback_reason"] == "daemon-config-changed-restart-required"
    assert "restart" in result["error"].casefold()
    assert not request_path.exists()


def test_unchanged_config_does_not_refuse_new_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    config = _config(tmp_path)
    _assert_identity_was_loaded(config)
    _queue_request(config, "unchanged-config")
    daemon = GateDaemon(config, runner=SimpleNamespace(reap=lambda: None))
    processed = {
        "schema": ci.RESULT_SCHEMA,
        "request_id": "unchanged-config",
        "status": "GREEN",
    }
    monkeypatch.setattr(daemon, "_process_request", lambda _path: processed)

    assert daemon.poll_once() is processed


def test_already_admitted_request_finishes_under_its_loaded_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    config = _config(tmp_path)
    original_identity = _assert_identity_was_loaded(config)
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request_id = "already-admitted"
    request_path = config.queue_dir / f"{request_id}.json"
    request_path.write_text(json.dumps({
        "schema": ci.REQUEST_SCHEMA,
        "request_id": request_id,
        "project_id": config.repo,
        "kind": "gate",
        "command_id": "full-gate",
        "command": ["./tests/run.sh"],
        "repo_root": str(tmp_path),
        "caller_env": {},
        "_resident": {"arms": {"candidate": {"phase": "accepted"}}},
    }), encoding="utf-8")
    runner = SimpleNamespace(config=config, reap=lambda: None)
    daemon = GateDaemon(config, runner=runner)

    def complete_under_loaded_config(_request, active_runner):
        assert active_runner.config.config_identity == original_identity
        return {"schema": ci.RESULT_SCHEMA, "request_id": request_id, "status": "GREEN"}

    monkeypatch.setattr(ci, "run_remote_command", complete_under_loaded_config)
    changed_config = _raw_config(tmp_path)
    changed_config["daemon"]["poll_seconds"] = 0.02
    config.path.write_text(json.dumps(changed_config), encoding="utf-8")

    result = daemon.poll_once()

    assert result["status"] == "GREEN"
    assert request_path.exists()


def test_unreadable_config_refuses_new_admission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _config(tmp_path)
    _assert_identity_was_loaded(config)
    _queue_request(config, "unreadable-config")
    daemon = GateDaemon(config, runner=SimpleNamespace(reap=lambda: None))
    monkeypatch.setattr(
        daemon, "_process_request",
        lambda _path: pytest.fail("unreadable configuration admitted new work"),
    )
    config.path.unlink()
    config.path.mkdir()

    result = daemon.poll_once()

    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert result["fallback_reason"] == "daemon-config-unreadable-restart-required"
    assert "restart" in result["error"].casefold()
    assert "cannot read config" in result["error"].casefold()
