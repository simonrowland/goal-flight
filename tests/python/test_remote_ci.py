"""Hermetic contracts for the project-neutral remote CI runner."""

from __future__ import annotations

import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time

import pytest
import goalflight_remote_ci as ci
from goalflight_remote_ci import (
    ArmOutcome, ArmSpec, BoxConfig, CommandResult, DaemonConfig, GateDaemon,
    RemoteRunIdentity, RemoteRunner, ReceiptError, build_pair_specs, health_census,
    load_config, matched_pair_verdict, parse_receipt, receipt_from_output,
    run_command, list_remote_leases, submit_request, validate_request,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "remote_ci"


def _raw_config(tmp_path: Path, *, token_pool_size: int = 2) -> dict:
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
                "p_cores": 20,
                "token_pool_size": token_pool_size,
                "remote_exec": ["scripted-remote", "{host}", "{script}"],
                "env": {},
                "managed_run_directory": "/var/lib/example-ci",
            }
        },
        "admission": {
            "queue_wait_seconds": 0.01,
        },
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


def _config(tmp_path: Path, *, token_pool_size: int = 2) -> DaemonConfig:
    path = tmp_path / "remote-ci.json"
    path.write_text(json.dumps(_raw_config(tmp_path, token_pool_size=token_pool_size)), encoding="utf-8")
    return load_config(path)


def _box(config: DaemonConfig) -> BoxConfig:
    return config.boxes["box-a"]



class ScriptedExecutor:
    """Run the shipped node program in an isolated fake box; never SSH or ps."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.load1 = 0
        self.before = None

    def __call__(self, argv, env, timeout):
        assert argv[:2] == ["scripted-remote", "ci-worker.example.invalid"]
        shell = shlex.split(argv[2])
        payload = json.loads(base64.b64decode(shell[-1]))
        self.calls.append(payload["operation"])
        if self.before:
            self.before(payload)
        payload["managed_root"] = str(self.root)
        shell[-1] = base64.b64encode(json.dumps(payload).encode()).decode()
        shell[0] = sys.executable
        shell[2] = (
            "import os,socket;os.getloadavg=lambda:(" + repr(self.load1) + ",0,0);"
            "socket.gethostname=lambda:'measured-node';" + shell[2]
        )
        return run_command(shell, timeout=timeout)

    def close(self, config):
        node = RemoteRunner(config, executor=self).nodes["box-a"]
        for record in node.call("list"):
            key = {"run_dir": record["run_directory"], "lease_token": record["lease_token"]}
            if record.get("remote_run"):
                node.call("cancel", **key, identity=record["remote_run"])
            elif record["state"] != "released":
                node.call("release", **key)


@pytest.fixture
def node_env(tmp_path):
    config = _config(tmp_path, token_pool_size=1)
    executor = ScriptedExecutor(tmp_path / "fake-node")
    runner = RemoteRunner(config, executor=executor)
    yield config, executor, runner, runner.nodes["box-a"]
    executor.close(config)


def spec(request_id="request-1"):
    return ArmSpec("candidate", "b"*40, "a"*40, "b"*40,
                   ("tests/test_one.py",), ("tests/test_one.py", "-v"),
                   request_id, "box-a", False)


def enqueue(node, request_id="request-1", owner=None):
    return node.call("enqueue", request_id=request_id, arm="candidate",
                     owner=owner or RemoteRunner._owner())


def key(record):
    return {"run_dir": record["run_directory"], "lease_token": record["lease_token"]}


def wait_state(node, record, states):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = node.call("status", **key(record))
        if current["state"] in states:
            return current
        time.sleep(0.01)
    raise AssertionError(f"never reached {states}: {current}")


def start(node, record, code="import time; time.sleep(3)"):
    return node.call("start", **key(record),
                     command={"argv": [sys.executable, "-c", code], "env": {}, "timeout": 10})


def test_config_validation_rejects_ad_hoc_managed_run_directory(tmp_path: Path) -> None:
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["managed_run_directory"] = "/tmp/remote-ci-runs"
    path = tmp_path / "bad-managed-root.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Exception, match="managed_run_directory must not be under /tmp"):
        load_config(path)


def test_config_validation_rejects_unknown_selection_option(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request = {
        "schema": "goalflight.remote-ci.request.v1",
        "request_id": "request-1",
        "kind": "targeted",
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py", "--not-allowed"],
    }
    with pytest.raises(Exception, match="option is not allowed"):
        validate_request(request, config)


def test_base_import_error_is_red_even_when_exit_and_summary_are_green(tmp_path: Path) -> None:
    del tmp_path
    base = parse_receipt(FIXTURES / "receipt-base-import-error.json")
    candidate = parse_receipt(FIXTURES / "receipt-pass.json")
    verdict = matched_pair_verdict(
        ArmOutcome("base", "green", 0, base),
        ArmOutcome("candidate", "green", 0, candidate),
    )
    assert verdict["status"] == "RED"
    assert verdict["base_red"] is True
    assert verdict["candidate_red"] is False
    assert verdict["measured_hostnames"]["base"] == "ci-worker.example.invalid"


def test_receipt_parser_uses_measured_hostname_and_rejects_alias_only() -> None:
    receipt = parse_receipt(FIXTURES / "receipt-pass.json")
    assert receipt.hostname == "ci-worker.example.invalid"
    assert receipt.duration_seconds == 18.4
    with pytest.raises(ReceiptError, match="answering hostname"):
        parse_receipt({"status": "pass", "passed": 1})
    assert receipt_from_output("noise\n" + json.dumps(receipt.to_dict())).hostname == receipt.hostname


def test_submit_request_is_validated_and_queue_file_is_atomic(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "request.json"
    source.write_text(
        json.dumps(
            {
                "schema": "goalflight.remote-ci.request.v1",
                "request_id": "request-1",
                "kind": "targeted",
                "tip_sha": "a" * 40,
                "candidate_sha": "b" * 40,
                "test_files": ["tests/test_one.py"],
                "selection": ["tests/test_one.py", "-q"],
            }
        ),
        encoding="utf-8",
    )
    queued = submit_request(source, config)
    assert queued == config.queue_dir / "request-1.json"
    assert json.loads(queued.read_text())["request_id"] == "request-1"


def test_run_command_starts_driver_in_its_own_session() -> None:
    result = run_command(
        [sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"],
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "True"


def test_config_requires_v2_remote_exec_and_preserves_node_paths(tmp_path):
    raw = _raw_config(tmp_path)
    del raw["boxes"]["box-a"]["remote_exec"]
    with pytest.raises(ci.ConfigError, match="remote_exec"):
        DaemonConfig.from_mapping(raw, path=tmp_path / "config.json")
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["remote_exec"] = ["executor"]
    with pytest.raises(ci.ConfigError, match="script"):
        DaemonConfig.from_mapping(raw, path=tmp_path / "config.json")
    raw["schema"] = "goalflight.remote-ci.config.v1"
    with pytest.raises(ci.ConfigError, match="schema"):
        DaemonConfig.from_mapping(raw, path=tmp_path / "config.json")


@pytest.mark.parametrize("field", ["chunk_size", "verbose_option", "cancel_command",
                                   "watch_command", "collect_command"])
def test_obsolete_runner_settings_are_not_silently_ignored(tmp_path, field):
    raw = _raw_config(tmp_path)
    raw["runner"][field] = 20
    with pytest.raises(ci.ConfigError, match="obsolete"):
        DaemonConfig.from_mapping(raw, path=tmp_path / "config.json")


@pytest.mark.parametrize("field,value", [
    ("token_key", "old-token"),
    ("load_command", ["custom-load-probe"]),
])
def test_removed_box_admission_probes_are_not_silently_ignored(tmp_path, field, value):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"][field] = value
    with pytest.raises(ci.ConfigError, match="obsolete"):
        DaemonConfig.from_mapping(raw, path=tmp_path / "config.json")


def test_node_admission_never_creates_controller_paths(node_env, monkeypatch):
    config, executor, runner, node = node_env
    original = Path.mkdir
    def guarded(path, *args, **kwargs):
        assert "/var/lib/example-ci" not in str(path)
        assert "fake-node" not in str(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", guarded)
    held = wait_state(node, enqueue(node), {"admitted"})
    assert held["sample"]["hostname"] == "measured-node"
    assert Path(held["run_directory"]).parent == executor.root / "admission" / "runs"
    assert health_census(config, executor=executor)["boxes"][0]["tokens"]["in_use"] == 1
    assert list_remote_leases(config, executor=executor)[0]["lease_id"] == held["lease_id"]


def test_flock_token_released_by_sigkill_of_node_holder(node_env):
    config, executor, runner, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    waiting = enqueue(node, "request-2")
    assert node.call("status", **key(waiting))["state"] == "queued"
    os.kill(int(held["remote_run"]["pid"]), signal.SIGKILL)
    admitted = wait_state(node, waiting, {"admitted"})
    assert admitted["token_index"] == held["token_index"]


def test_shared_fifo_between_project_daemons_and_stale_ticket_cleanup(node_env):
    config, executor, runner, node = node_env
    first = wait_state(node, enqueue(node, "holder"), {"admitted"})
    # Distinct controller configs/project queues share the same node authority.
    other = RemoteRunner(replace(config, queue_dir=config.queue_dir / "project-b"), executor=executor).nodes["box-a"]
    stale = enqueue(node, "stale")
    # queued identity is written asynchronously, so wait for its incarnation.
    deadline = time.monotonic() + 5
    while not stale.get("remote_run") and time.monotonic() < deadline:
        stale = node.call("status", **key(stale))
    os.kill(int(stale["remote_run"]["pid"]), signal.SIGKILL)
    older = enqueue(node, "zzz-older")
    younger = enqueue(other, "aaa-younger")
    assert older["ticket"] < younger["ticket"]
    node.call("release", **key(first))
    wait_state(node, older, {"admitted"})
    assert other.call("status", **key(younger))["state"] == "queued"
    node.call("release", **key(older))
    wait_state(other, younger, {"admitted"})


def test_unsafe_load_and_live_caps_do_not_admit(node_env):
    config, executor, runner, node = node_env
    executor.load1 = 21
    queued = enqueue(node)
    time.sleep(0.05)
    assert node.call("status", **key(queued))["state"] == "queued"
    assert node.call("health")["tokens"]["in_use"] == 0
    node.call("release", **key(queued))
    wait_state(node, queued, {"released"})
    executor.load1 = 3
    (executor.root / "admission" / "caps.json").write_text(json.dumps({"p_cores": 2}))
    limited = enqueue(node, "limited")
    time.sleep(0.05)
    assert node.call("status", **key(limited))["state"] == "queued"
    (executor.root / "admission" / "caps.json").write_text(json.dumps({"p_cores": 20, "self_cap": 2}))
    admitted = wait_state(node, limited, {"admitted"})
    assert admitted["sample"]["load1"] == 3


def test_run_persists_identity_before_start_and_receipt_is_measured(node_env):
    config, executor, runner, node = node_env
    receipt = (FIXTURES / "receipt-pass.json").read_text().replace("ci-worker.example.invalid", "measured-node")
    config = replace(config, runner=replace(config.runner, command=(
        sys.executable, "-c", "import os,json; r=json.loads(os.environ['GOALFLIGHT_REMOTE_CI_LEASE_RECORD_JSON']);"
        "assert r['remote_run']['pid']; assert os.environ['TEST_WORKERS']=='4'; "
        "import base64;print(base64.b64decode(" + repr(base64.b64encode(receipt.encode()).decode()) + ").decode())")))
    runner = RemoteRunner(config, executor=executor)
    def before(payload):
        if payload["operation"] == "start":
            mirror = config.state_dir / "runs" / "request-1-candidate.json"
            assert not mirror.exists()
            durable = json.loads((Path(payload["run_dir"]) / "lease.json").read_text())
            assert durable["remote_run"]["pid"]
            assert durable["remote_run"]["start_token"]
            assert durable["remote_run"]["run_dir"] == payload["run_dir"]
    executor.before = before
    outcome = runner.run_arm(spec())
    assert outcome.status == "green"
    assert outcome.receipt.hostname == "measured-node"
    assert executor.calls.index("enqueue") < executor.calls.index("start")
    assert outcome.to_dict()["lease"]["lease_token"]


def test_crash_reaper_reads_node_identity_without_local_running_record(node_env, monkeypatch):
    config, executor, runner, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    start(node, held)
    running = wait_state(node, held, {"running"})
    assert not (config.state_dir / "runs").exists()
    monkeypatch.setattr(ci, "_pid_alive", lambda _: False)
    results = runner.reap()
    assert results[0]["status"] == "cancelled"
    final = node.call("status", **key(held))
    assert not final["holder_alive"]
    assert final["remote_run"] == running["remote_run"]
    assert node.call("health")["tokens"]["in_use"] == 0


@pytest.mark.parametrize("unknown", ["start_token", "pid", "run_dir"])
def test_cancel_and_attach_refuse_unproven_identity(node_env, unknown):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    identity = dict(held["remote_run"], **{unknown: "wrong"})
    for operation in ("cancel", "attach"):
        with pytest.raises(ci.RemoteCIError, match="identity"):
            node.call(operation, **key(held), identity=identity, owner=RemoteRunner._owner())
    assert node.call("health")["tokens"]["in_use"] == 1


def test_reaper_retains_unknown_owner(node_env, monkeypatch):
    _, executor, runner, node = node_env
    wait_state(node, enqueue(node), {"admitted"})
    monkeypatch.setattr(ci, "_pid_alive", lambda _: None)
    assert runner.reap()[0]["status"] == "unknown"
    assert "cancel" not in executor.calls


def test_stale_reaper_cannot_cancel_after_reattach_transfers_owner(node_env):
    _, _, _, node = node_env
    old_owner = dict(RemoteRunner._owner(), owner_pid=99999999)
    held = wait_state(node, enqueue(node, owner=old_owner), {"admitted"})
    node.call("attach", **key(held), identity=held["remote_run"], owner=RemoteRunner._owner())
    result = node.call("cancel", **key(held), identity=held["remote_run"], expected_owner=old_owner)
    assert result["status"] == "owned"
    assert node.call("health")["tokens"]["in_use"] == 1
    assert not (Path(held["run_directory"]) / "cancel.json").exists()


def test_receipt_with_alias_instead_of_measured_hostname_is_red(node_env):
    config, executor, _, _ = node_env
    encoded = base64.b64encode((FIXTURES / "receipt-pass.json").read_bytes()).decode()
    config = replace(config, runner=replace(config.runner, command=(
        sys.executable, "-c", "import base64;print(base64.b64decode(" + repr(encoded) + ").decode())")))
    outcome = RemoteRunner(config, executor=executor).run_arm(spec())
    assert outcome.status == "red"
    assert "measured node" in outcome.error


def test_health_counts_distinct_token_locks(node_env):
    config, executor, _, _ = node_env
    other_executor = ScriptedExecutor(executor.root.parent / "two-token-node")
    box = replace(config.boxes["box-a"], token_pool_size=2)
    config = replace(config, boxes={"box-a": box})
    node = RemoteRunner(config, executor=other_executor).nodes["box-a"]
    try:
        wait_state(node, enqueue(node), {"admitted"})
        assert node.call("health")["tokens"] == {"total": 2, "free": 1, "in_use": 1}
    finally:
        other_executor.close(config)


def test_reattach_inherits_original_token_and_never_starts_twice(node_env):
    config, executor, runner, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    receipt = (FIXTURES / "receipt-pass.json").read_text().replace("ci-worker.example.invalid", "measured-node")
    start(node, held, "import time;time.sleep(.5);print(" + repr(receipt) + ")")
    identity = RemoteRunIdentity.from_mapping(held["remote_run"])
    waiting = enqueue(node, "request-2")
    def before(payload):
        if payload["operation"] == "attach":
            assert json.loads((Path(waiting["run_directory"]) / "lease.json").read_text())["state"] == "queued"
    executor.before = before
    log = Path(held["run_directory"]) / "launch.log"
    assert ci.parse_launch_identity(log.read_text()) == identity
    result = runner.reattach_launch_log(log, spec())
    assert result.status == "green"
    assert executor.calls.count("start") == 1
    assert executor.calls.count("enqueue") == 2
    assert "attach" in executor.calls


def test_reattach_rejects_queued_holder_without_token(node_env):
    _, _, runner, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    queued = enqueue(node, "queued")
    deadline = time.monotonic() + 5
    while not queued.get("remote_run") and time.monotonic() < deadline:
        queued = node.call("status", **key(queued))
    with pytest.raises(ci.RemoteCIError, match="no admission"):
        runner.reattach(spec(), RemoteRunIdentity.from_mapping(queued["remote_run"]))
    assert node.call("status", **key(held))["state"] == "admitted"


def test_lost_launch_response_cancels_proven_run_before_freeing_token(node_env):
    config, executor, _, node = node_env
    config = replace(config, runner=replace(config.runner,
        command=(sys.executable, "-c", "import time;time.sleep(20)")))
    def losing_executor(argv, env, timeout):
        result = executor(argv, env, timeout)
        if executor.calls[-1] == "start":
            return CommandResult(2, stderr="lost response")
        return result
    runner = RemoteRunner(config, executor=losing_executor)
    with pytest.raises(ci.RemoteCIError, match="lost response"):
        runner.run_arm(spec())
    assert node.call("health")["tokens"]["in_use"] == 0
    assert "release" not in executor.calls
    assert "cancel" in executor.calls


def test_duplicate_start_cannot_execute_twice(node_env):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    start(node, held)
    with pytest.raises(ci.RemoteCIError, match="already started|does not hold admission"):
        start(node, held)


def test_released_admission_rejects_a_delayed_launch(node_env):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    node.call("release", **key(held))
    with pytest.raises(ci.RemoteCIError, match="release is pending|does not hold admission"):
        start(node, held)
    assert not (Path(held["run_directory"]) / "command.json").exists()


def test_reattach_cannot_inherit_released_by_dead_holder(node_env):
    config, executor, runner, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    os.kill(int(held["remote_run"]["pid"]), signal.SIGKILL)
    deadline = time.monotonic() + 5
    while node.call("status", **key(held))["holder_alive"] and time.monotonic() < deadline:
        time.sleep(.01)
    with pytest.raises(ci.RemoteCIError, match="holder is gone"):
        runner.reattach(spec(), RemoteRunIdentity.from_mapping(held["remote_run"]))


def test_timeout_cancels_node_group_and_frees_token(node_env):
    config, executor, runner, node = node_env
    config = replace(config, runner=replace(config.runner, timeout_seconds=.2,
        command=(sys.executable, "-c", "import time;time.sleep(20)")))
    result = RemoteRunner(config, executor=executor).run_arm(spec())
    assert result.timed_out and result.cancelled
    deadline = time.monotonic() + 5
    while node.call("health")["tokens"]["in_use"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert node.call("health")["tokens"]["in_use"] == 0


def test_cap_policy_mismatch_fails_closed(node_env):
    config, executor, _, node = node_env
    node.call("health")
    box = replace(config.boxes["box-a"], token_pool_size=2)
    other = RemoteRunner(replace(config, boxes={"box-a": box}), executor=executor).nodes["box-a"]
    with pytest.raises(ci.RemoteCIError, match="policy differs"):
        enqueue(other)


def test_corrected_cap_can_list_and_reap(node_env, monkeypatch):
    config, executor, _, node = node_env
    node.call("health")
    held = wait_state(node, enqueue(node), {"admitted"})
    box = replace(config.boxes["box-a"], token_pool_size=2)
    corrected = RemoteRunner(replace(config, boxes={"box-a": box}), executor=executor)
    found = corrected.nodes["box-a"].call("list")
    assert found[0]["lease_id"] == held["lease_id"]
    with pytest.raises(ci.RemoteCIError, match="policy differs"):
        enqueue(corrected.nodes["box-a"])
    monkeypatch.setattr(ci, "_pid_alive", lambda _: False)
    assert corrected.reap()[0]["status"] == "cancelled"
    deadline = time.monotonic() + 5
    while node.call("health")["tokens"]["in_use"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert node.call("health")["tokens"]["in_use"] == 0


def test_base_overlay_selection_contract(node_env):
    config, _, _, _ = node_env
    request = {"request_id": "r", "tip_sha": "a"*40, "candidate_sha": "b"*40,
               "test_files": ["tests/test_one.py"], "selection": ["tests/test_one.py", "-q"]}
    base, candidate = build_pair_specs(request, config)
    assert base.overlay and not candidate.overlay
    assert base.sha == request["tip_sha"]
    assert "--continue-on-collection-errors" in base.selection


def test_queue_order_is_stable(tmp_path):
    config = _config(tmp_path)
    config.queue_dir.mkdir(parents=True)
    for name in ("request-002", "request-001"):
        (config.queue_dir / f"{name}.json").write_text("{}")
    assert [p.stem for p in GateDaemon(config).pending_paths()] == ["request-001", "request-002"]


def _green_config(config):
    receipt = (FIXTURES / "receipt-pass.json").read_text().replace(
        "ci-worker.example.invalid", "measured-node")
    command = (
        sys.executable, "-c",
        "import base64;print(base64.b64decode("
        + repr(base64.b64encode(receipt.encode()).decode())
        + ").decode())",
    )
    return replace(config, runner=replace(config.runner, command=command))


def _free_tokens(node):
    deadline = time.monotonic() + 5
    while node.call("health")["tokens"]["in_use"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert node.call("health")["tokens"]["in_use"] == 0


def test_dropped_enqueue_response_does_not_stick_the_token(node_env):
    config, executor, _, node = node_env
    config = _green_config(config)

    def transport(argv, env, timeout):
        result = executor(argv, env, timeout)
        if executor.calls[-1] == "enqueue":
            return CommandResult(255, stdout=result.stdout, stderr="connection reset by peer")
        return result

    outcome = RemoteRunner(config, executor=transport).run_arm(spec())
    assert outcome.status == "green"
    _free_tokens(node)


def test_live_owner_reaps_admission_whose_release_never_landed(node_env):
    config, executor, _, node = node_env
    config = _green_config(config)
    drop = {"on": True}

    def transport(argv, env, timeout):
        payload = json.loads(base64.b64decode(shlex.split(argv[2])[-1]))
        if drop["on"] and payload["operation"] in {"status", "release", "cancel"}:
            return CommandResult(255, stderr="no route to host")
        result = executor(argv, env, timeout)
        if drop["on"] and payload["operation"] == "enqueue":
            wait_state(node, json.loads(result.stdout), {"admitted"})
        return result

    runner = RemoteRunner(config, executor=transport)
    with pytest.raises(ci.RemoteCIError, match="no route to host"):
        runner.run_arm(spec())
    assert node.call("health")["tokens"]["in_use"] == 1
    drop["on"] = False
    assert runner.reap()[0]["status"] == "releasing"
    _free_tokens(node)


def test_next_arm_recovers_a_forgotten_token(node_env):
    config, executor, _, node = node_env
    config = _green_config(config)
    drop = {"on": True}

    def transport(argv, env, timeout):
        payload = json.loads(base64.b64decode(shlex.split(argv[2])[-1]))
        if drop["on"] and payload["operation"] in {"status", "release", "cancel"}:
            return CommandResult(255, stderr="no route to host")
        result = executor(argv, env, timeout)
        if drop["on"] and payload["operation"] == "enqueue":
            wait_state(node, json.loads(result.stdout), {"admitted"})
        return result

    clock = {"t0": time.monotonic()}

    def sleeper(seconds):
        if time.monotonic() - clock["t0"] > 3:
            raise AssertionError("next admission stayed parked behind a forgotten token")
        time.sleep(seconds)

    runner = RemoteRunner(config, executor=transport, sleeper=sleeper)
    with pytest.raises(ci.RemoteCIError, match="no route to host"):
        runner.run_arm(spec())
    assert node.call("health")["tokens"]["in_use"] == 1
    drop["on"] = False
    clock["t0"] = time.monotonic()
    outcome = runner.run_arm(spec("request-2"))
    assert outcome.status == "green"
    _free_tokens(node)


def test_admitted_holder_releases_when_the_command_never_arrives(node_env):
    _, _, _, node = node_env
    forgotten = node.call(
        "enqueue", request_id="forgotten", arm="candidate",
        owner=RemoteRunner._owner(), command_wait_seconds=0.3,
    )
    other = enqueue(node, "other")
    admitted = wait_state(node, other, {"admitted"})
    assert admitted["lease_id"] != forgotten["lease_id"]
    released = wait_state(node, forgotten, {"released"})
    assert released.get("release_reason") == "command-wait"


def test_sigkill_of_holder_keeps_the_token_while_the_workload_runs(node_env, tmp_path):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    pidfile = tmp_path / "workload.pid"
    start(node, held,
          "import os,time\n"
          f"open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
          "time.sleep(30)\n")
    deadline = time.monotonic() + 5
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pidfile.exists()
    child = int(pidfile.read_text())
    waiting = None
    try:
        os.kill(int(held["remote_run"]["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 5
        while node.call("status", **key(held))["holder_alive"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not node.call("status", **key(held))["holder_alive"]
        os.kill(child, 0)
        assert node.call("health")["tokens"]["in_use"] == 1
        waiting = enqueue(node, "request-2")
        time.sleep(0.25)
        assert node.call("status", **key(waiting))["state"] == "queued"
        assert node.call("health")["tokens"]["in_use"] == 1
        result = node.call("cancel", **key(held), identity=held["remote_run"])
        assert result["status"] == "unknown"
        os.kill(child, 0)
        assert node.call("health")["tokens"]["in_use"] == 1
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if waiting is not None:
            try:
                node.call("release", **key(waiting))
            except ci.RemoteCIError:
                pass
        deadline = time.monotonic() + 5
        while node.call("health")["tokens"]["in_use"] and time.monotonic() < deadline:
            time.sleep(0.01)


def test_admission_poll_does_not_park_for_the_queue_wait(tmp_path):
    raw = _raw_config(tmp_path, token_pool_size=1)
    raw["admission"]["queue_wait_seconds"] = 30
    raw["daemon"]["poll_seconds"] = 0.05
    path = tmp_path / "remote-ci.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _green_config(load_config(path))
    executor = ScriptedExecutor(tmp_path / "fake-node")
    sleeps = []
    released = {"ok": False}
    clock = {"t0": time.monotonic()}

    def sleeper(seconds):
        sleeps.append(seconds)
        if seconds > 1:
            raise AssertionError(f"admission slept {seconds}s, parking a free token")
        if time.monotonic() - clock["t0"] > 3:
            raise AssertionError("admission poll parked past 3s")
        if not released["ok"]:
            released["ok"] = True
            node.call("release", **key(blocker))
        time.sleep(seconds)

    runner = RemoteRunner(config, executor=executor, sleeper=sleeper)
    node = runner.nodes["box-a"]
    try:
        blocker = wait_state(
            node,
            enqueue(node, "blocker", owner=dict(RemoteRunner._owner(), owner_pid=os.getpid() + 1)),
            {"admitted"},
        )
        clock["t0"] = time.monotonic()
        outcome = runner.run_arm(spec())
        assert outcome.status == "green"
        assert sleeps and max(sleeps) <= 1
        assert time.monotonic() - clock["t0"] < 3
    finally:
        executor.close(config)
