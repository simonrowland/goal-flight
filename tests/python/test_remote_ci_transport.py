"""Transport and bounded-result contracts for the remote CI runner."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
from threading import Barrier
from types import SimpleNamespace

import pytest
import goalflight_remote_ci as ci
from goalflight_remote_ci import CommandResult, RemoteRunner
from test_remote_ci import ScriptedExecutor, _config, _raw_config, spec


@pytest.fixture
def node_env(tmp_path):
    config = _config(tmp_path)
    executor = ScriptedExecutor(tmp_path / "fake-node")
    runner = RemoteRunner(config, executor=executor)
    yield config, executor, runner, runner.nodes["box-a"]
    executor.close(config)


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


def test_remote_node_installs_helper_once_and_then_sends_only_operation_payload(node_env):
    _, executor, _, node = node_env
    helper = Path(ci.__file__).with_name("goalflight_remote_ci_node.py").read_bytes()
    encoded_program = base64.b64encode(helper).decode("ascii")

    node.call("health")
    node.call("health")

    assert encoded_program not in executor.remote_scripts[-1], (
        "second call still uploads the complete node helper")


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
    config_a = ci.load_config(path_a)
    raw_b = _raw_config(tmp_path / "config-b")
    raw_b["boxes"]["box-a"]["managed_run_directory"] = str(root_b)
    path_b = tmp_path / "config-b.json"
    path_b.write_text(json.dumps(raw_b), encoding="utf-8")
    config_b = ci.load_config(path_b)
    executor = ScriptedExecutor(tmp_path / "fake-node")
    probe_barrier = Barrier(2)

    def simultaneous_probes(argv, env, timeout):
        result = executor(argv, env, timeout)
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


def test_queued_arm_status_poll_uses_configured_interval(tmp_path):
    raw = _raw_config(tmp_path)
    raw["daemon"]["poll_seconds"] = 5.0
    raw["admission"]["queue_wait_seconds"] = 30.0
    path = tmp_path / "queued-poll.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = ci.load_config(path)
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
        "owner_identity": "owner",
        "token_index": 0,
        "sample": {"self_cap": 4},
        "slot": "/managed/slots/s-01",
        "holder_alive": True,
    }

    class QueuedNode:
        box = remote_node.box

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
    expected = ci.ArmOutcome(spec().arm, "green", 0, None)
    runner._watch = lambda arm_spec, node, key, owner: expected

    assert runner.run_arm(spec()) == expected
    assert sleeps == [5.0], "queued status poll was capped below daemon.poll_seconds"


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

    with pytest.raises(ci.RemoteCIError, match="protocol mismatch.*expected 2.*received 1"):
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

    with pytest.raises(ci.RemoteCIError, match="protocol mismatch: expected 2, received 1"):
        node.call("health")


def test_direct_ssh_uses_private_persistent_control_socket(tmp_path):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-ssh.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = ci.load_config(path)
    commands = []

    def executor(argv, env, timeout):
        commands.append(tuple(argv))
        return CommandResult(0, json.dumps({"protocol_version": 2, "result": []}), "", False)

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


def test_long_state_directory_still_uses_short_ssh_socket_path(tmp_path):
    raw = _raw_config(tmp_path)
    raw["paths"]["state_dir"] = str(tmp_path / ("state" * 24))
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-long-state.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    runner = RemoteRunner(ci.load_config(path), executor=lambda argv, env, timeout: None)

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
    result = CommandResult(0, json.dumps({"protocol_version": 2, "result": []}), "", False)
    commands = []

    def executor(argv, env, timeout):
        commands.append(tuple(argv))
        return result

    runner = RemoteRunner(ci.load_config(path), executor=executor)
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
    result = CommandResult(0, json.dumps({"protocol_version": 2, "result": []}), "", False)
    commands = []

    def executor(argv, env, timeout):
        commands.append(tuple(argv))
        return result

    runner = RemoteRunner(ci.load_config(path), executor=executor)
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
    runner = RemoteRunner(ci.load_config(path), executor=lambda argv, env, timeout: None)
    node = runner.nodes["box-a"]
    node.control_dir = Path("/tmp") / ("x" * 120)

    command = node._command_template()

    assert command[1:4] == (
        "-oControlMaster=no", "-oControlPersist=no", "-oControlPath=none",
    )
    assert "ControlPath" in caplog.text and "disabling SSH multiplexing" in caplog.text


def test_symlinked_ssh_socket_directory_disables_multiplexing(tmp_path, monkeypatch, caplog):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["ssh", "{host}", "{script}"]
    path = tmp_path / "remote-ci-symlink-socket.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    runner = RemoteRunner(ci.load_config(path), executor=lambda argv, env, timeout: None)
    node = runner.nodes["box-a"]
    _inject_control_directory_metadata(
        node, monkeypatch, mode=stat.S_IFLNK | 0o777, owner_uid=os.geteuid()
    )
    command = node._command_template()

    assert command[1:4] == (
        "-oControlMaster=no", "-oControlPersist=no", "-oControlPath=none",
    )
    assert "directory is a symlink" in caplog.text
