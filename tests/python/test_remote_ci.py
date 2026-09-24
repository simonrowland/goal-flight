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
import stat
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
                "managed_run_directory": str(tmp_path / "fake-node"),
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



_REMOTE_CI_LABEL = "com.goalflight.remote-ci."
# Coalition ids a test's teardown swept. The autouse fixture checks they
# have no live member after the launchd job is gone.
_swept_coalitions: list[int] = []
_sweep_unknown: list[int] = []
_sweep_unsignalled: list[tuple] = []


# Bound before tests replace subprocess.run. Teardown must still see launchctl.
_launchctl_run = subprocess.run


def _remote_ci_launchd_jobs():
    """label -> pid or None. None if launchctl cannot be listed before the deadline.

    A single failed listing under load is not proof, and it is not a pass.
    Wait until a listing arrives. Giving up still fails the test.
    """
    deadline = time.monotonic() + 60
    listed = None
    while True:
        try:
            listed = _launchctl_run(["launchctl", "list"], capture_output=True, text=True)
        except OSError:
            listed = None
        if listed is not None and listed.returncode == 0:
            break
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)
    jobs = {}
    for line in listed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            parts = line.split()
        if len(parts) < 3:
            continue
        label = parts[-1]
        if not label.startswith(_REMOTE_CI_LABEL):
            continue
        jobs[label] = int(parts[0]) if parts[0].isdigit() else None
    return jobs


def _sweep_managed_jobs(managed: Path) -> None:
    """Remove this root's launchd jobs and kill coalition members still alive."""
    import goalflight_remote_ci_node as node

    runs = []
    for root in (managed / "runs", managed / "admission" / "runs"):
        if root.is_dir():
            runs.extend(path for path in root.iterdir() if path.is_dir() and not path.is_symlink())
    for run in runs:
        try:
            lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        label = lease.get("launch_label") or ""
        cid = lease.get("coalition_id")
        holder = lease.get("holder_coalition_id")
        if label:
            _launchctl_run(["launchctl", "remove", label], capture_output=True, text=True)
        # The login-session coalition is never a kill target. A lease that
        # failed to record a private id must not take the test process with it.
        if (isinstance(cid, int) and not isinstance(cid, bool) and cid > 0
                and cid != holder):
            _swept_coalitions.append(cid)
            members = node._coalition_members(cid)
            if members is None:
                # Cannot prove the tree is empty. Do not treat that as gone.
                _sweep_unknown.append(cid)
                continue
            for pid, start in members:
                if pid <= 1 or pid == os.getpid():
                    continue
                if not node._signal_incarnation(pid, start, cid):
                    _sweep_unsignalled.append((cid, pid, start))


def _live_coalition_members(coalitions: list[int]):
    """(live members, coalition ids that could not be enumerated)."""
    import goalflight_remote_ci_node as node

    found = []
    unknown = []
    for cid in coalitions:
        members = node._coalition_members(cid)
        if members is None:
            unknown.append(cid)
            continue
        for pid, start in members:
            if pid > 1 and pid != os.getpid():
                found.append((pid, start))
    return found, unknown


@pytest.fixture(autouse=True)
def no_leftover_remote_ci_job(request):
    """Fail if a test leaves a launchd job or a live coalition member.

    Jobs are per-user, not per tmp_path. A later test sees whatever the
    previous test did not remove.
    """
    before = _remote_ci_launchd_jobs()
    _swept_coalitions.clear()
    _sweep_unknown.clear()
    _sweep_unsignalled.clear()
    yield
    after = _remote_ci_launchd_jobs()
    if before is None or after is None:
        raise AssertionError(
            f"{request.node.nodeid}: launchctl list failed; cannot prove jobs were removed")
    leaked = {label: pid for label, pid in after.items() if label not in before}
    members, unknown = _live_coalition_members(list(_swept_coalitions))
    unknown = list(dict.fromkeys([*unknown, *_sweep_unknown]))
    if leaked or members or unknown or _sweep_unsignalled:
        raise AssertionError(
            f"{request.node.nodeid} left remote CI state behind: "
            f"jobs={leaked} members={members} unknown={unknown} "
            f"unsignalled={_sweep_unsignalled}")


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
        # Honour the managed root in the payload. Rewriting it hid two projects
        # on one box creating two token pools.
        shell[-1] = base64.b64encode(json.dumps(payload).encode()).decode()
        shell[0] = sys.executable
        authority = str(self.root / "authority.json")
        shell[2] = (
            "import builtins,os,socket;"
            "builtins._GOALFLIGHT_REMOTE_CI_AUTHORITY = " + repr(authority) + ";"
            "os.getloadavg=lambda:(" + repr(self.load1) + ",0,0);"
            "socket.gethostname=lambda:'measured-node';" + shell[2]
        )
        return run_command(shell, timeout=timeout)

    def close(self, config):
        node = RemoteRunner(config, executor=self).nodes["box-a"]
        try:
            records = node.call("list")
        except ci.RemoteCIError:
            records = []
        for record in records:
            if record.get("state") == "UNREADABLE":
                continue
            key = {"run_dir": record["run_directory"], "lease_token": record["lease_token"]}
            owner = {name: record.get(name) for name in ("owner_host", "owner_pid", "owner_identity")}
            try:
                if record.get("remote_run"):
                    node.call("cancel", **key, identity=record["remote_run"], expected_owner=owner)
                elif record.get("state") != "released":
                    node.call("release", **key, expected_owner=owner)
            except ci.RemoteCIError:
                pass
        # Cancel removes the job. This sweep is the backstop, and it fails
        # the test if a member cannot be proved dead.
        _sweep_managed_jobs(self.root)


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
    assert Path(held["run_directory"]).parent == executor.root / "runs"
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
    other_root = executor.root.parent / "two-token-node"
    other_executor = ScriptedExecutor(other_root)
    box = replace(config.boxes["box-a"], token_pool_size=2, managed_run_directory=other_root)
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

    sleeps = []

    def sleeper(seconds):
        sleeps.append(seconds)
        if seconds > 1:
            raise AssertionError(f"admission slept {seconds}s behind a forgotten token")

    runner = RemoteRunner(config, executor=transport, sleeper=sleeper)
    with pytest.raises(ci.RemoteCIError, match="no route to host"):
        runner.run_arm(spec())
    assert node.call("health")["tokens"]["in_use"] == 1
    drop["on"] = False
    outcome = runner.run_arm(spec("request-2"))
    assert outcome.status == "green"
    assert sleeps and max(sleeps) <= 1
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
    child = _child_pid(pidfile)
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
        result = node.call("cancel", **key(held), identity=held["remote_run"],
                           expected_owner={name: held[name] for name in
                                           ("owner_host", "owner_pid", "owner_identity")})
        # The dead holder used to leave the launchd job until the deadline.
        assert result["status"] in {"cleared", "cancelled"}
        _wait_dead(child, node, held)
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

    def sleeper(seconds):
        sleeps.append(seconds)
        if seconds > 1:
            raise AssertionError(f"admission slept {seconds}s, parking a free token")
        if not released["ok"]:
            released["ok"] = True
            node.call("release", **key(blocker))

    runner = RemoteRunner(config, executor=executor, sleeper=sleeper)
    node = runner.nodes["box-a"]
    try:
        blocker = wait_state(
            node,
            enqueue(node, "blocker", owner=dict(RemoteRunner._owner(), owner_pid=os.getpid() + 1)),
            {"admitted"},
        )
        outcome = runner.run_arm(spec())
        assert outcome.status == "green"
        assert sleeps and max(sleeps) <= 1
        assert released["ok"]
    finally:
        executor.close(config)


def _diagnose_launch(node, record):
    """launchctl print, coalition members, and the run directory."""
    chunks = []
    status = {}
    if node is not None and record is not None:
        try:
            status = node.call("status", **key(record))
        except Exception as exc:
            chunks.append(f"status error: {exc}")
        else:
            chunks.append("status=" + json.dumps({
                key_name: status.get(key_name) for key_name in (
                    "state", "launch_label", "coalition_id", "workload_pid",
                    "holder_alive", "run_directory")
            }, default=str))
    label = status.get("launch_label") if isinstance(status, dict) else None
    if label:
        for domain in (f"gui/{os.getuid()}", f"user/{os.getuid()}"):
            printed = _launchctl_run(
                ["launchctl", "print", f"{domain}/{label}"],
                capture_output=True, text=True)
            chunks.append(
                f"launchctl print {domain}/{label} rc={printed.returncode}\n"
                f"{printed.stdout[-1500:]}\n{printed.stderr[-400:]}")
            if printed.returncode == 0:
                break
    run = None
    if isinstance(record, dict):
        run = record.get("run_directory")
    if run is None and isinstance(status, dict):
        run = status.get("run_directory")
    if run and Path(run).is_dir():
        chunks.append("run dir: " + ", ".join(sorted(path.name for path in Path(run).iterdir())))
    cid = status.get("coalition_id") if isinstance(status, dict) else None
    if isinstance(cid, int) and not isinstance(cid, bool):
        import goalflight_remote_ci_node as node_mod
        chunks.append("members=" + repr(node_mod._coalition_members(cid)))
    return "\n".join(chunks) if chunks else "no launch diagnostic context"


def _child_pid(pidfile, node=None, record=None):
    """Wait until the workload writes its pid. Slow launchd is not a 5s failure."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        text = pidfile.read_text() if pidfile.exists() else ""
        if text.strip():
            return int(text.strip())
        time.sleep(0.05)
    raise AssertionError(
        "workload pid was not written\n" + _diagnose_launch(node, record))


def test_two_managed_roots_do_not_create_two_pools(tmp_path):
    root_a = tmp_path / "node-a"
    root_b = tmp_path / "node-b"
    raw = _raw_config(tmp_path, token_pool_size=1)
    raw["boxes"]["box-a"]["managed_run_directory"] = str(root_a)
    path_a = tmp_path / "a.json"
    path_a.write_text(json.dumps(raw), encoding="utf-8")
    config_a = load_config(path_a)
    raw["boxes"]["box-a"]["managed_run_directory"] = str(root_b)
    path_b = tmp_path / "b.json"
    path_b.write_text(json.dumps(raw), encoding="utf-8")
    config_b = load_config(path_b)
    executor = ScriptedExecutor(tmp_path / "box-authority")
    node_a = RemoteRunner(config_a, executor=executor).nodes["box-a"]
    node_b = RemoteRunner(config_b, executor=executor).nodes["box-a"]
    try:
        held = wait_state(node_a, enqueue(node_a), {"admitted"})
        with pytest.raises(ci.RemoteCIError, match="admission authority"):
            enqueue(node_b)
        assert node_a.call("health")["tokens"]["in_use"] == 1
        assert not (root_b / "admission").exists()
        assert str(root_a) in held["run_directory"]
    finally:
        executor.close(config_a)


def test_cancel_requires_expected_owner(node_env):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    with pytest.raises(ci.RemoteCIError, match="expected_owner"):
        node.call("cancel", **key(held), identity=held["remote_run"])
    assert node.call("health")["tokens"]["in_use"] == 1


def test_stale_controller_cannot_cancel_after_reattach(node_env, tmp_path):
    config, executor, _, node = node_env
    pidfile = tmp_path / "stale.pid"
    config = replace(config, runner=replace(config.runner, command=(
        sys.executable, "-c",
        "import os,time\n"
        f"open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n",
    )))
    owner_b = dict(RemoteRunner._owner(), owner_pid=os.getpid() + 1, owner_identity="other-controller")
    seen = {}
    phase = {"watch": False}

    def transport(argv, env, timeout):
        payload = json.loads(base64.b64decode(shlex.split(argv[2])[-1]))
        operation = payload["operation"]
        if operation == "start":
            result = executor(argv, env, timeout)
            phase["watch"] = True
            return result
        if operation == "status" and phase["watch"]:
            result = executor(argv, env, timeout)
            if result.returncode == 0 and result.stdout.strip():
                record = json.loads(result.stdout)
                if record.get("state") == "running":
                    phase["watch"] = False
                    node.call("attach", **key(record), identity=record["remote_run"], owner=owner_b)
                    seen["run_dir"] = record["run_directory"]
                    return CommandResult(255, stderr="lost status")
            return result
        result = executor(argv, env, timeout)
        if operation == "cancel" and result.returncode == 0 and result.stdout.strip():
            seen["cancel_owner"] = payload.get("expected_owner")
            seen["cancel_status"] = json.loads(result.stdout).get("status")
        return result

    runner = RemoteRunner(config, executor=transport)
    with pytest.raises(ci.RemoteCIError, match="lost status"):
        runner.run_arm(spec())
    assert seen["cancel_status"] == "owned"
    assert seen["cancel_owner"]["owner_pid"] == os.getpid()
    child = _child_pid(pidfile)
    os.kill(child, 0)
    assert not (Path(seen["run_dir"]) / "cancel.json").exists()
    assert node.call("health")["tokens"]["in_use"] == 1
    os.kill(child, signal.SIGKILL)


def test_reap_kills_orphaned_workload_after_its_deadline(node_env, tmp_path):
    _, _, runner, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    pidfile = tmp_path / "orphan.pid"
    node.call("start", **key(held), command={
        "argv": [sys.executable, "-c",
                 "import os,time\n"
                 f"open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
                 "time.sleep(30)\n"],
        "env": {},
        "timeout": 30,
    })
    child = _child_pid(pidfile, node, held)
    try:
        os.kill(int(held["remote_run"]["pid"]), signal.SIGKILL)
        # The deadline is what reap checks. A 0.4s command timeout is already
        # past by the time a slow launchd writes the pid, so the job is killed
        # before this file exists. Put the deadline in the past after the pid
        # is there.
        lease_path = Path(held["run_directory"]) / "lease.json"
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        lease["deadline_epoch"] = time.time() - 1
        lease_path.write_text(json.dumps(lease), encoding="utf-8")
        assert runner.reap()[0]["status"] == "cancelled"
        _wait_dead(child, node, held)
        _free_tokens(node)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_operator_clear_audits_a_dead_holder(node_env, tmp_path):
    _, executor, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    with pytest.raises(ci.RemoteCIError, match="holder is alive"):
        node.call("clear", **key(held), identity=held["remote_run"], operator=RemoteRunner._owner())
    pidfile = tmp_path / "clear.pid"
    start(node, held,
          "import os,time\n"
          f"open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
          "time.sleep(30)\n")
    child = _child_pid(pidfile)
    try:
        os.kill(int(held["remote_run"]["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 5
        while node.call("status", **key(held))["holder_alive"] and time.monotonic() < deadline:
            time.sleep(0.01)
        result = node.call("clear", **key(held), identity=held["remote_run"],
                           operator=RemoteRunner._owner())
        assert result["status"] == "cleared"
        audit = (executor.root / "admission" / "audit.log").read_text(encoding="utf-8")
        assert held["lease_id"] in audit and "operator-clear" in audit
        _wait_dead(child, node, held)
        _free_tokens(node)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_released_run_bodies_are_pruned(node_env):
    _, executor, _, node = node_env
    finished = wait_state(node, enqueue(node, "done"), {"admitted"})
    start(node, finished, "import sys; sys.exit(0)")
    wait_state(node, finished, {"released"})
    dry = node.call("gc", apply=False, retain_max_count=0, retain_max_age_seconds=0,
                    retain_failure_count=0, retain_failure_age_seconds=0)
    assert any(row["class"] == "ELIGIBLE" and row["path"] == finished["run_directory"] for row in dry)
    assert Path(finished["run_directory"]).exists()
    tokens = executor.root / "admission" / "tokens"
    (tokens / "sentinel").write_text("keep", encoding="utf-8")
    unknown = wait_state(node, enqueue(node, "unknown"), {"admitted"})
    pidfile_holder = int(unknown["remote_run"]["pid"])
    start(node, unknown, "import time; time.sleep(30)")
    wait_state(node, unknown, {"running"})
    os.kill(pidfile_holder, signal.SIGKILL)
    try:
        node.call("enqueue", request_id="prune", arm="candidate", owner=RemoteRunner._owner(),
                  retain_max_count=0, retain_max_age_seconds=0)
        assert not Path(finished["run_directory"]).exists()
        assert Path(unknown["run_directory"]).exists()
        assert (tokens / "sentinel").read_text(encoding="utf-8") == "keep"
        assert finished["lease_id"] in (executor.root / "results" / "index.jsonl").read_text(encoding="utf-8")
    finally:
        # The orphan still holds the token; clearing it is the supported path.
        try:
            node.call("clear", **key(unknown), identity=unknown["remote_run"],
                      operator=RemoteRunner._owner())
        except ci.RemoteCIError:
            pass


def test_checkout_directory_stays_inside_the_managed_root(node_env):
    config, executor, _, _ = node_env
    seen = {}

    def before(payload):
        if payload["operation"] == "start":
            seen["checkout"] = payload["command"]["env"]["GOALFLIGHT_REMOTE_CI_CHECKOUT_DIR"]
            seen["run_dir"] = payload["run_dir"]

    executor.before = before
    RemoteRunner(_green_config(config), executor=executor).run_arm(spec())
    slot = seen["checkout"]
    assert Path(slot).is_dir()
    assert slot == str(Path(executor.root) / "repos" / "default" / "slots" / "s-01")
    assert str(executor.root) in slot
    assert not slot.startswith(str(Path.home()))


def test_poll_once_does_not_write_a_request_mirror(tmp_path):
    config = _config(tmp_path, token_pool_size=1)
    executor = ScriptedExecutor(tmp_path / "fake-node")
    request = {
        "schema": "goalflight.remote-ci.request.v1",
        "request_id": "request-1",
        "kind": "targeted",
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py", "-q"],
    }
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    (config.queue_dir / "request-1.json").write_text(json.dumps(request), encoding="utf-8")
    try:
        result = ci.GateDaemon(config, runner=RemoteRunner(config, executor=executor)).poll_once()
        assert result is not None
        assert (config.result_dir / "request-1.json").exists()
        assert not (config.state_dir / "runs" / "request-1.json").exists()
    finally:
        executor.close(config)


def _cancel(node, record):
    return node.call("cancel", **key(record), identity=record["remote_run"],
                     expected_owner={name: record[name] for name in
                                     ("owner_host", "owner_pid", "owner_identity")})


def test_cancel_kills_background_grandchildren_before_releasing_the_token(node_env, tmp_path):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    pidfile = tmp_path / "bg.pids"
    start(node, held,
          "import pathlib, subprocess, time\n"
          f"out = pathlib.Path({str(pidfile)!r})\n"
          "procs = [subprocess.Popen(['sleep', '30']) for _ in range(2)]\n"
          "out.write_text('\\n'.join(str(p.pid) for p in procs))\n"
          "time.sleep(30)\n")
    deadline = time.monotonic() + 60
    pids = []
    while time.monotonic() < deadline:
        text = pidfile.read_text() if pidfile.exists() else ""
        pids = [int(line) for line in text.split() if line.strip()]
        if len(pids) == 2:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(
            "workload pids were not written\n" + _diagnose_launch(node, held))
    for pid in pids:
        os.kill(pid, 0)
    assert _cancel(node, held)["status"] == "cancelled"
    for pid in pids:
        _wait_dead(pid, node, held)
    _free_tokens(node)


def test_cancel_kills_a_setsid_descendant_before_releasing_the_token(node_env, tmp_path):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    pidfile = tmp_path / "escaped.pid"
    start(node, held,
          "import os, time\n"
          "pid = os.fork()\n"
          "if pid == 0:\n"
          "    os.setsid()\n"
          f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
          "    time.sleep(60)\n"
          "    os._exit(0)\n"
          "time.sleep(60)\n")
    child = _child_pid(pidfile)
    os.kill(child, 0)
    assert _cancel(node, held)["status"] == "cancelled"
    _wait_dead(child, node, held)
    _free_tokens(node)


def _two_token(tmp_path):
    raw = _raw_config(tmp_path, token_pool_size=2)
    root = tmp_path / "two-token-node"
    raw["boxes"]["box-a"]["managed_run_directory"] = str(root)
    path = tmp_path / "two-token.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(path)
    executor = ScriptedExecutor(root)
    runner = RemoteRunner(config, executor=executor)
    return config, executor, runner, runner.nodes["box-a"]


def _wait_dead(pid, node=None, record=None):
    """Wait until ``pid`` is gone, including a zombie.

    ``kill(pid, 0)`` still succeeds for a zombie. ``getpgid`` does not.
    A slow reap is not a 2s failure. The error includes the launch state.
    """
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            os.getpgid(pid)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"pid {pid} still alive\n" + _diagnose_launch(node, record))


def test_cancel_kills_a_child_that_setsid_chdirs_and_closes_fds(node_env, tmp_path):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    pidfile = tmp_path / "detached.pid"
    start(node, held,
          "import subprocess, time\n"
          "child = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True,"
          " cwd='/', close_fds=True)\n"
          f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
          "time.sleep(60)\n")
    child = _child_pid(pidfile)
    assert os.getpgid(child) == child
    assert _cancel(node, held)["status"] == "cancelled"
    _wait_dead(child, node, held)
    _free_tokens(node)


def test_reap_holds_capacity_until_the_escaped_child_is_dead(node_env, tmp_path):
    _, _, runner, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    pidfile = tmp_path / "reap-detached.pid"
    node.call("start", **key(held), command={
        "argv": [sys.executable, "-c",
                 "import subprocess, time\n"
                 "child = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True,"
                 " cwd='/', close_fds=True)\n"
                 f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
                 "time.sleep(60)\n"],
        "env": {},
        "timeout": 30,
    })
    child = _child_pid(pidfile, node, held)
    try:
        os.kill(int(held["remote_run"]["pid"]), signal.SIGKILL)
        lease_path = Path(held["run_directory"]) / "lease.json"
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        lease["deadline_epoch"] = time.time() - 1
        lease_path.write_text(json.dumps(lease), encoding="utf-8")
        assert node.call("health")["tokens"]["in_use"] == 1
        assert runner.reap()[0]["status"] == "cancelled"
        _wait_dead(child, node, held)
        _free_tokens(node)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_draining_lease_reserves_a_token_with_no_flock(node_env):
    _, executor, _, node = node_env
    managed = executor.root
    run = managed / "runs" / "drainlease"
    run.mkdir(parents=True)
    remote = {
        "host": "measured-node", "pid": "999999", "start_token": "s",
        "run_dir": str(run), "lease_id": "drainlease", "lease_token": "tok",
    }
    (run / "lease.json").write_text(json.dumps({
        "schema": "goalflight.remote-ci.lease.v1",
        "lease_id": "drainlease", "lease_token": "tok",
        "state": "draining", "token_index": 0,
        "run_directory": str(run), "remote_run": remote,
    }), encoding="utf-8")
    (run / "owner.json").write_text(json.dumps(RemoteRunner._owner()), encoding="utf-8")
    admission = managed / "admission"
    admission.mkdir(parents=True, exist_ok=True)
    (admission / "policy.json").write_text(
        json.dumps({"p_cores": 20, "token_pool_size": 1}), encoding="utf-8")
    assert node.call("health")["tokens"]["in_use"] == 1
    waiting = enqueue(node, "blocked-by-drain")
    time.sleep(0.25)
    assert node.call("status", **key(waiting))["state"] == "queued"
    assert node.call("health")["tokens"]["in_use"] == 1


def test_unresolved_slot_stays_reserved_for_the_successor(tmp_path):
    config, executor, runner, node = _two_token(tmp_path)
    child_b = None
    try:
        first = wait_state(node, enqueue(node, "run-a"), {"admitted"})
        pidfile = tmp_path / "a.pid"
        start(node, first,
              "import os, time\n"
              f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
              "time.sleep(30)\n")
        workload = _child_pid(pidfile)
        running = wait_state(node, first, {"running"})
        os.kill(int(running["remote_run"]["pid"]), signal.SIGKILL)
        os.kill(workload, signal.SIGKILL)
        _wait_dead(workload, node, running)
        lease_path = Path(running["run_directory"]) / "lease.json"
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        lease["deadline_epoch"] = time.time() - 1
        lease_path.write_text(json.dumps(lease), encoding="utf-8")
        # A different controller host keeps reap from treating B as forgotten.
        other = dict(RemoteRunner._owner(), owner_host="not-this-host")
        second = wait_state(node, enqueue(node, "run-b", owner=other), {"admitted"})
        assert second["slot"] != running["slot"]
        bpid = tmp_path / "b.pid"
        start(node, second,
              "import os, time\n"
              f"open({str(bpid)!r}, 'w').write(str(os.getpid()))\n"
              "time.sleep(30)\n")
        child_b = _child_pid(bpid)
        results = {row["lease_id"]: row["status"] for row in runner.reap()}
        assert results[running["lease_id"]] == "cancelled"
        os.kill(child_b, 0)
    finally:
        if child_b is not None:
            try:
                os.kill(child_b, signal.SIGKILL)
            except ProcessLookupError:
                pass
        executor.close(config)


def test_stale_clear_does_not_signal_the_slots_current_occupant(tmp_path):
    config, executor, _, node = _two_token(tmp_path)
    sleeper = None
    try:
        first = wait_state(node, enqueue(node, "run-a"), {"admitted"})
        pidfile = tmp_path / "fence.pid"
        start(node, first,
              "import os, time\n"
              f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
              "time.sleep(30)\n")
        workload = _child_pid(pidfile)
        running = wait_state(node, first, {"running"})
        os.kill(int(running["remote_run"]["pid"]), signal.SIGKILL)
        os.kill(workload, signal.SIGKILL)
        _wait_dead(workload, node, running)
        slot = Path(running["slot"])
        marker = slot.parent.parent / "slot-meta" / (slot.name + ".json")
        marker.parent.mkdir(parents=True, exist_ok=True)
        current = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
        current.update(state="leased", lease_id="successor-not-a")
        marker.write_text(json.dumps(current), encoding="utf-8")
        # Our child, so a killed process stays a zombie until we wait on it.
        # os.kill(pid, 0) still succeeds for that zombie and would hide the kill.
        sleeper = subprocess.Popen(["/bin/sleep", "60"], cwd=slot, start_new_session=True)
        time.sleep(0.2)
        node.call("clear", **key(running), identity=running["remote_run"])
        waited, _status = os.waitpid(sleeper.pid, os.WNOHANG)
        assert waited == 0
        os.kill(sleeper.pid, 0)
    finally:
        if sleeper is not None:
            try:
                os.kill(sleeper.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        executor.close(config)


def test_clean_git_slot_is_reused_instead_of_quarantined(node_env):
    _, executor, _, node = node_env
    held = wait_state(node, enqueue(node, "init"), {"admitted"})
    start(node, held,
          "import pathlib, subprocess\n"
          "subprocess.check_call(['git', 'init'])\n"
          "pathlib.Path('README').write_text('x')\n"
          "subprocess.check_call(['git', 'add', 'README'])\n"
          "subprocess.check_call(['git', '-c', 'user.email=t@example.com',"
          " '-c', 'user.name=t', 'commit', '-m', 'init'])\n")
    released = wait_state(node, held, {"released"})
    slot = Path(released["slot"])
    assert (slot / ".git").is_dir()
    assert not (slot / "SLOT.json").exists()
    assert not (slot / "slot.lock").exists()
    again = wait_state(node, enqueue(node, "reuse"), {"admitted"})
    start(node, again, "import os, sys\nsys.exit(0 if os.path.isdir('.git') else 3)\n")
    done = wait_state(node, again, {"released"})
    assert done["result"]["returncode"] == 0
    quarantine = executor.root / "quarantine"
    assert not quarantine.exists() or not any(quarantine.iterdir())
    assert (Path(again["slot"]) / ".git").is_dir()


def test_result_index_is_fsynced_before_a_body_can_be_collected(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    kinds = []

    def spy(fd):
        kinds.append(stat.S_ISDIR(os.fstat(fd).st_mode))

    monkeypatch.setattr(node.os, "fsync", spy)
    managed = tmp_path / "managed"
    run = managed / "runs" / "abc"
    run.mkdir(parents=True)
    (run / "result.json").write_text(json.dumps({"returncode": 0}), encoding="utf-8")
    node.append_result(managed, {
        "lease_id": "abc", "repo": "goal-flight", "release_reason": "completed",
    }, run)
    assert False in kinds and True in kinds
    line = json.loads((managed / "results" / "index.jsonl").read_text(encoding="utf-8"))
    assert line["run_id"] == "abc"
    assert line["repo"] == "goal-flight"


def test_legacy_admission_runs_remain_recoverable(node_env):
    _, executor, _, node = node_env
    managed = executor.root
    run = managed / "admission" / "runs" / "legacylease"
    run.mkdir(parents=True)
    remote = {
        "host": "h", "pid": "4242", "start_token": "st", "run_dir": str(run),
        "lease_id": "legacylease", "lease_token": "tok",
    }
    owner = RemoteRunner._owner()
    (run / "lease.json").write_text(json.dumps({
        "schema": "goalflight.remote-ci.lease.v1",
        "lease_id": "legacylease", "lease_token": "tok",
        "state": "running", "token_index": 0,
        "run_directory": str(run), "remote_run": remote,
        "managed_run_directory": str(managed),
    }), encoding="utf-8")
    (run / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    admission = managed / "admission"
    admission.mkdir(parents=True, exist_ok=True)
    (admission / "policy.json").write_text(
        json.dumps({"p_cores": 20, "token_pool_size": 1}), encoding="utf-8")
    assert any(row["lease_id"] == "legacylease" for row in node.call("list"))
    assert node.call("status", run_dir=str(run), lease_token="tok")["state"] == "running"
    assert node.call("health")["tokens"]["in_use"] == 1
    waiting = enqueue(node, "needs-a-token")
    time.sleep(0.25)
    assert node.call("status", **key(waiting))["state"] == "queued"
    unknown = node.call("cancel", run_dir=str(run), lease_token="tok", identity=remote,
                        expected_owner=owner)
    assert unknown["status"] == "unknown"
    assert node.call("health")["tokens"]["in_use"] == 1
    cleared = node.call("clear", run_dir=str(run), lease_token="tok", identity=remote)
    assert cleared["status"] == "cleared"
    wait_state(node, waiting, {"admitted"})


def test_controller_repo_reaches_the_slot_and_the_result(tmp_path):
    raw = _raw_config(tmp_path, token_pool_size=1)
    raw["repo"] = "goal-flight"
    root = tmp_path / "repo-node"
    raw["boxes"]["box-a"]["managed_run_directory"] = str(root)
    path = tmp_path / "repo.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _green_config(load_config(path))
    executor = ScriptedExecutor(root)
    seen = {}

    def before(payload):
        if payload["operation"] == "enqueue":
            seen["repo"] = payload.get("repo")

    executor.before = before
    try:
        runner = RemoteRunner(config, executor=executor)
        outcome = runner.run_arm(spec())
        assert seen["repo"] == "goal-flight"
        assert outcome.lease["slot"].endswith("/repos/goal-flight/slots/s-01")
        # The watch returns when result.json appears, before the holder fsyncs
        # the index and marks the lease released.
        wait_state(runner.nodes["box-a"], outcome.lease, {"released"})
        line = json.loads((root / "results" / "index.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert line["repo"] == "goal-flight"
        kiln = replace(config, repo="kiln")
        executor.before = None
        kiln_runner = RemoteRunner(kiln, executor=executor)
        second = kiln_runner.run_arm(spec("request-2"))
        assert second.lease["slot"].endswith("/repos/kiln/slots/s-01")
        wait_state(kiln_runner.nodes["box-a"], second.lease, {"released"})
        assert '"repo": "kiln"' in (root / "results" / "index.jsonl").read_text(encoding="utf-8")
    finally:
        executor.close(config)


def test_capacity_refusal_is_not_reported_as_death(node_env):
    config, executor, _, _ = node_env
    config = _green_config(config)
    floor = executor.root / "admission" / "resource-floor.json"

    def before(payload):
        if payload["operation"] == "start":
            floor.parent.mkdir(parents=True, exist_ok=True)
            floor.write_text(json.dumps({"refuse": "capacity"}), encoding="utf-8")

    executor.before = before
    outcome = RemoteRunner(config, executor=executor).run_arm(spec())
    assert outcome.status == "capacity"
    assert outcome.returncode == 75
    assert outcome.status != "died"
    assert "REMOTE-DIED" not in (outcome.error or "")
    final = json.loads((Path(outcome.lease["run_directory"]) / "result.json").read_text(encoding="utf-8"))
    assert final["status"] == "capacity-refused"
    assert final["returncode"] != 77
    _free_tokens(RemoteRunner(config, executor=executor).nodes["box-a"])


def test_unreachable_keep_machinery_is_gone():
    source = (ROOT / "scripts" / "goalflight_remote_ci_node.py").read_text(encoding="utf-8")
    assert "unknown-descendants" not in source
    assert "def token_held" not in source
    assert "RUN_MARKER" not in source
    assert "allowed_pgid" not in source


def test_reap_kills_a_setsid_orphan_after_its_parent_exits(node_env, tmp_path):
    """Parent exit reparents the child to launchd. The coalition still names it."""
    _, _, runner, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    pidfile = tmp_path / "orphan-sleep.pid"
    node.call("start", **key(held), command={
        "argv": [sys.executable, "-c",
                 "import subprocess, time\n"
                 "child = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True,"
                 " cwd='/', close_fds=True)\n"
                 f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
                 "time.sleep(60)\n"],
        "env": {},
        "timeout": 30,
    })
    child = _child_pid(pidfile)
    try:
        running = wait_state(node, held, {"running"})
        deadline = time.monotonic() + 60
        while not running.get("coalition_id") and time.monotonic() < deadline:
            time.sleep(0.05)
            running = node.call("status", **key(held))
        if not running.get("coalition_id"):
            raise AssertionError(
                "coalition was not recorded\n" + _diagnose_launch(node, held))
        assert running.get("workload_start")
        os.kill(int(running["remote_run"]["pid"]), signal.SIGKILL)
        os.kill(int(running["workload_pid"]), signal.SIGKILL)
        _wait_dead(int(running["workload_pid"]), node, held)
        os.kill(child, 0)
        lease_path = Path(running["run_directory"]) / "lease.json"
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        lease["deadline_epoch"] = time.time() - 1
        lease_path.write_text(json.dumps(lease), encoding="utf-8")
        assert node.call("health")["tokens"]["in_use"] == 1
        assert runner.reap()[0]["status"] == "cancelled"
        _wait_dead(child, node, held)
        _free_tokens(node)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_sigterm_grandchild_is_killed_with_the_coalition(node_env, tmp_path):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    pidfile = tmp_path / "grand.pid"
    ready = tmp_path / "handler-ready"
    start(node, held,
          "import os, signal, time\n"
          "def onterm(signum, frame):\n"
          "    signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
          "    pid = os.fork()\n"
          "    if pid == 0:\n"
          "        signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
          "        os.setsid()\n"
          "        time.sleep(60)\n"
          "        os._exit(0)\n"
          f"    open({str(pidfile)!r}, 'w').write(str(pid))\n"
          "    time.sleep(30)\n"
          "signal.signal(signal.SIGTERM, onterm)\n"
          f"open({str(ready)!r}, 'w').write('1')\n"
          "time.sleep(60)\n")
    running = wait_state(node, held, {"running"})
    deadline = time.monotonic() + 60
    while not running.get("coalition_id") and time.monotonic() < deadline:
        time.sleep(0.05)
        running = node.call("status", **key(held))
    if not running.get("coalition_id"):
        raise AssertionError("coalition was not recorded\n" + _diagnose_launch(node, held))
    # Do not cancel on a command-line check. That races the handler install
    # when launchd is slow, and the grandchild pid is never written.
    ready_deadline = time.monotonic() + 60
    while not ready.is_file() and time.monotonic() < ready_deadline:
        time.sleep(0.05)
    if not ready.is_file():
        raise AssertionError(
            "handler was not installed\n" + _diagnose_launch(node, held))
    assert _cancel(node, held)["status"] == "cancelled"
    child = _child_pid(pidfile, node, held)
    try:
        # getpgid, not kill(pid, 0): a zombie is not a live grandchild, and
        # launchd's reap timing is global state left by earlier jobs.
        _wait_dead(child, node, held)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _free_tokens(node)


def test_kill_escalation_stops_when_the_pid_is_reused(monkeypatch):
    import goalflight_remote_ci_node as node

    signals = []

    def same(pid, started):
        del pid, started
        return not signals

    def fake_kill(pid, sig):
        del pid
        signals.append(sig)

    monkeypatch.setattr(node, "_same_process", same)
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 100)
    monkeypatch.setattr(node, "TREE_GRACE_SECONDS", 0)
    monkeypatch.setattr(node.os, "kill", fake_kill)
    assert node._signal_incarnation(4321, (10, 20), 100) is True
    assert signals == [signal.SIGTERM]
    assert signal.SIGKILL not in signals


def test_kill_escalation_rechecks_coalition_before_sigkill(monkeypatch):
    import goalflight_remote_ci_node as node

    signals = []

    def coalition(pid):
        del pid
        return 100 if not signals else 300

    monkeypatch.setattr(node, "_same_process", lambda pid, started: True)
    monkeypatch.setattr(node, "_coalition_id", coalition)
    monkeypatch.setattr(node, "TREE_GRACE_SECONDS", 0)
    monkeypatch.setattr(node.os, "kill", lambda pid, sig: signals.append(sig))
    assert node._signal_incarnation(20, (99, 99), 100) is False
    assert signals == [signal.SIGTERM]
    assert signal.SIGKILL not in signals


def test_legacy_checkout_slot_is_not_granted_to_the_next_run(tmp_path):
    import fcntl

    config, executor, _, node = _two_token(tmp_path)
    managed = executor.root
    slot = managed / "repos" / "default" / "slots" / "s-01"
    slot.mkdir(parents=True)
    run = managed / "runs" / "legacyrun"
    run.mkdir(parents=True)
    (slot / "SLOT.json").write_text(json.dumps({
        "state": "leased", "lease_id": "legacyrun", "run_dir": str(run),
    }), encoding="utf-8")
    lock = (slot / "slot.lock").open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    remote = {
        "host": "h", "pid": "9", "start_token": "st", "run_dir": str(run),
        "lease_id": "legacyrun", "lease_token": "tok",
    }
    (run / "lease.json").write_text(json.dumps({
        "schema": "goalflight.remote-ci.lease.v1",
        "lease_id": "legacyrun", "lease_token": "tok",
        "state": "running", "token_index": 0, "slot": str(slot),
        "run_directory": str(run), "remote_run": remote,
    }), encoding="utf-8")
    (run / "owner.json").write_text(json.dumps(RemoteRunner._owner()), encoding="utf-8")
    admission = managed / "admission"
    admission.mkdir(parents=True, exist_ok=True)
    (admission / "policy.json").write_text(
        json.dumps({"p_cores": 20, "token_pool_size": 2}), encoding="utf-8")
    try:
        fresh = wait_state(node, enqueue(node, "after-upgrade"), {"admitted"})
        assert not str(fresh["slot"]).endswith("/slots/s-01")
        assert str(fresh["slot"]).endswith("/slots/s-02")
        assert fresh["token_index"] == 1
    finally:
        lock.close()
        executor.close(config)


def test_directory_fsync_is_repeated_after_an_interrupted_append(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    directory_fsyncs = {"n": 0, "fail_once": True}

    def spy(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            return
        if directory_fsyncs["fail_once"]:
            directory_fsyncs["fail_once"] = False
            raise OSError("directory fsync interrupted")
        directory_fsyncs["n"] += 1

    monkeypatch.setattr(node.os, "fsync", spy)
    managed = tmp_path / "managed"
    run = managed / "runs" / "abc"
    run.mkdir(parents=True)
    state = {"lease_id": "abc", "repo": "goal-flight", "release_reason": "completed"}
    with pytest.raises(OSError, match="directory fsync"):
        node.append_result(managed, state, run)
    node.append_result(managed, state, run)
    assert directory_fsyncs["n"] >= 1
    assert (managed / "results" / "index.jsonl").exists()


def _lease_run(tmp_path, **fields):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {"lease_id": "abc", "state": "running"}
    state.update(fields)
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    return node, run, state


def test_job_label_is_durable_before_launchctl_submit(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {"lease_id": "abc", "state": "running"}
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    seen = {}

    def fake_run(argv, **kwargs):
        del kwargs
        if argv[:2] == ["launchctl", "submit"]:
            lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
            seen["label"] = lease.get("launch_label")
            seen["submitted"] = lease.get("launch_submitted")
            seen["gate"] = (run / "workload-go").exists()

            class Result:
                returncode = 1
                stderr = "submit refused"
                stdout = ""

            return Result()
        raise AssertionError(argv)

    monkeypatch.setattr(node.subprocess, "run", fake_run)
    assert node._submit_workload(run, state, ["/bin/true"], {}, "") == "submit refused"
    assert seen["label"] == "com.goalflight.remote-ci.abc"
    assert seen["submitted"] is True
    assert seen["gate"] is False


def test_gate_opens_only_after_the_coalition_is_recorded(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {"lease_id": "abc", "state": "running"}
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")

    def fake_run(argv, **kwargs):
        del argv, kwargs

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        return Result()

    original = node._remember_identity

    def remember(target, current):
        if current.get("coalition_id") and (target / "workload-go").exists():
            raise AssertionError("gate opened before the coalition was durable")
        original(target, current)

    monkeypatch.setattr(node.subprocess, "run", fake_run)
    monkeypatch.setattr(node, "_job_view", lambda label: ("running", 42, None))
    monkeypatch.setattr(node, "_stable_identity", lambda pid: ((10, 20), 100))
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 5)
    monkeypatch.setattr(node, "_remember_identity", remember)
    launched = node._submit_workload(run, state, ["/bin/sleep", "1"], {}, "")
    assert launched[0] == 42
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["coalition_id"] == 100
    assert lease["launch_label"] == "com.goalflight.remote-ci.abc"
    assert (run / "workload-go").is_file()


def test_gate_stays_closed_without_a_durable_coalition_id(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {"lease_id": "abc", "state": "running"}
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    original = node._remember_identity

    def remember(target, current):
        if current.get("coalition_id"):
            return
        original(target, current)

    monkeypatch.setattr(node.subprocess, "run", lambda argv, **kwargs: type("R", (), {
        "returncode": 0, "stderr": "", "stdout": ""})())
    monkeypatch.setattr(node, "_job_view", lambda label: ("running", 42, None))
    monkeypatch.setattr(node, "_stable_identity", lambda pid: ((10, 20), 100))
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 5)
    monkeypatch.setattr(node, "_remember_identity", remember)
    launched = node._submit_workload(run, state, ["/bin/sleep", "1"], {}, "")
    assert isinstance(launched, str)
    assert not (run / "workload-go").exists()
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert "coalition_id" not in lease
    assert state.get("coalition_id") == 100


def test_unrecorded_coalition_does_not_count_as_an_empty_tree(tmp_path, monkeypatch):
    node, run, state = _lease_run(
        tmp_path, launch_label="com.goalflight.remote-ci.abc")
    monkeypatch.setattr(node, "_job_view", lambda label: ("running", 40, None))
    monkeypatch.setattr(node, "_stable_identity", lambda pid: None)
    assert node.clear_tree(tmp_path, state, run) is False
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["launch_label"] == "com.goalflight.remote-ci.abc"
    assert lease["job_remove_pending"] is True


def test_failed_coalition_query_of_a_live_pid_is_unknown(monkeypatch):
    import goalflight_remote_ci_node as node

    monkeypatch.setattr(node, "_all_pids", lambda: [10, 11])
    monkeypatch.setattr(node, "_pid_exists", lambda pid: True)

    def coalition(pid):
        if pid == 10:
            return None
        return 7

    monkeypatch.setattr(node, "_coalition_id", coalition)
    assert node._coalition_members(100) is None


def test_member_dying_during_the_final_check_is_not_an_empty_tree(monkeypatch):
    """A death during the confirming check is not an empty coalition.

    The child born as that member exits is invisible to the raced pass.
    The next complete pass must see it. Returning [] here is the bug.
    """
    import goalflight_remote_ci_node as node

    snapshots = iter([[10], [10], [11], [11]])

    def all_pids():
        return list(next(snapshots))

    monkeypatch.setattr(node, "_all_pids", all_pids)
    monkeypatch.setattr(node, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 100)
    monkeypatch.setattr(node, "_start_time", lambda pid: (5, pid))
    monkeypatch.setattr(node, "_same_process", lambda pid, started: pid != 10)
    assert node._coalition_members(100) == [(11, (5, 11))]


def test_complete_rescan_after_a_mid_check_death_proves_empty(monkeypatch):
    import goalflight_remote_ci_node as node

    snapshots = iter([[10], [10], [99], [99]])

    def all_pids():
        return list(next(snapshots))

    def coalition(pid):
        return 100 if pid == 10 else 7

    monkeypatch.setattr(node, "_all_pids", all_pids)
    monkeypatch.setattr(node, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(node, "_coalition_id", coalition)
    monkeypatch.setattr(node, "_start_time", lambda pid: (5, pid))
    monkeypatch.setattr(node, "_same_process", lambda pid, started: False)
    assert node._coalition_members(100) == []


def test_pid_that_vanishes_during_classify_does_not_end_the_pass(monkeypatch):
    """A member that disappears while it is being classified is not an empty tree.

    The pid list can omit a child that already exists. A process that was
    read in this coalition and then vanished must start another full pass.
    A process that was already gone before its coalition was read is not
    that member; otherwise a busy machine could never prove a tree empty.
    """
    import goalflight_remote_ci_node as node

    snapshots = iter([[10], [10], [11], [11]])
    reads = {"n": 0}

    def all_pids():
        return list(next(snapshots))

    def coalition(pid):
        if pid != 10:
            return 100
        reads["n"] += 1
        return 100 if reads["n"] == 1 else None

    monkeypatch.setattr(node, "_all_pids", all_pids)
    monkeypatch.setattr(node, "_pid_exists", lambda pid: pid != 10)
    monkeypatch.setattr(node, "_coalition_id", coalition)
    monkeypatch.setattr(node, "_start_time", lambda pid: None if pid == 10 else (5, pid))
    monkeypatch.setattr(node, "_same_process", lambda pid, started: pid != 10)
    assert node._coalition_members(100) == [(11, (5, 11))]


def test_child_born_during_the_scan_is_not_an_empty_tree(monkeypatch):
    import goalflight_remote_ci_node as node

    calls = {"n": 0}

    def all_pids():
        calls["n"] += 1
        if calls["n"] == 1:
            return [10]
        return [11]

    monkeypatch.setattr(node, "_all_pids", all_pids)
    monkeypatch.setattr(node, "_pid_exists", lambda pid: pid != 10)
    monkeypatch.setattr(node, "_coalition_id", lambda pid: None if pid == 10 else 100)
    monkeypatch.setattr(node, "_start_time", lambda pid: (5, 5))
    assert node._coalition_members(100) == [(11, (5, 5))]


def test_complete_scan_with_no_members_is_empty(monkeypatch):
    import goalflight_remote_ci_node as node

    monkeypatch.setattr(node, "_all_pids", lambda: [10])
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 7)
    assert node._coalition_members(100) == []


def test_membership_and_start_must_name_one_incarnation(monkeypatch):
    import goalflight_remote_ci_node as node

    calls = {"n": 0}

    def coalition(pid):
        del pid
        calls["n"] += 1
        return 100 if calls["n"] % 2 == 1 else 300

    monkeypatch.setattr(node, "_all_pids", lambda: [20])
    monkeypatch.setattr(node, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(node, "_coalition_id", coalition)
    monkeypatch.setattr(node, "_start_time", lambda pid: (99, 99))
    assert node._coalition_members(100) is None


def test_exit_status_is_stored_before_the_job_is_removed(tmp_path, monkeypatch):
    node, run, state = _lease_run(
        tmp_path,
        launch_label="com.goalflight.remote-ci.abc",
        coalition_id=100,
        holder_coalition_id=5,
        workload_pid="40",
        workload_start=[1, 2],
    )
    order = []

    def view(label):
        del label
        order.append("view")
        return ("exited", None, 19)

    def remove(label):
        del label
        order.append("remove")
        return True

    monkeypatch.setattr(node, "_job_view", view)
    monkeypatch.setattr(node, "_remove_job", remove)
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [])
    assert node.clear_tree(tmp_path, state, run) is True
    assert state["exit_known"] is True
    assert state["exit_code"] == 19
    assert order[0] == "view"
    assert "remove" in order
    assert order.index("view") < order.index("remove")
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["exit_code"] == 19
    assert node._terminal_result({"exit_known": False})["status"] == "died"
    assert node._terminal_result({"exit_known": False})["returncode"] == 2
    assert node._terminal_result({"exit_known": True, "exit_code": 19})["status"] == "died"


def test_failed_job_removal_stays_tracked_for_reap(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    managed = tmp_path / "managed"
    root = managed / "admission"
    run = managed / "runs" / "abc"
    root.mkdir(parents=True)
    run.mkdir(parents=True)
    (root / "queue.lock").write_text("", encoding="utf-8")
    state = {
        "lease_id": "abc", "lease_token": "tok", "state": "running",
        "launch_label": "com.goalflight.remote-ci.abc",
        "coalition_id": 100, "holder_coalition_id": 5,
        "workload_pid": "40", "workload_start": [1, 2],
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [])
    monkeypatch.setattr(node, "_job_view", lambda label: ("exited", None, 3))
    monkeypatch.setattr(node, "_remove_job", lambda label: False)
    first = node.finish_dead_workload(root, run, dict(state), "deadline")
    assert first["status"] == "unknown"
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["state"] == "draining"
    assert lease["launch_label"] == "com.goalflight.remote-ci.abc"
    assert lease["job_remove_pending"] is True
    monkeypatch.setattr(node, "_remove_job", lambda label: True)
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    second = node.finish_dead_workload(root, run, lease, "deadline")
    assert second["status"] == "cancelled"
    assert json.loads((run / "lease.json").read_text(encoding="utf-8"))["state"] == "released"


def test_capacity_is_not_released_before_the_job_is_gone(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    managed = tmp_path / "managed"
    root = managed / "admission"
    run = managed / "runs" / "abc"
    root.mkdir(parents=True)
    run.mkdir(parents=True)
    (root / "queue.lock").write_text("", encoding="utf-8")
    state = {
        "lease_id": "abc", "lease_token": "tok", "state": "running",
        "token_index": 0, "launch_label": "com.goalflight.remote-ci.abc",
        "coalition_id": 100, "holder_coalition_id": 5,
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(node, "clear_tree", lambda *args, **kwargs: True)
    monkeypatch.setattr(node, "_remove_job", lambda label: False)
    token = (run / "token-standin").open("a+")
    try:
        node._release_holder(root, run, dict(state), token)
    finally:
        token.close()
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["state"] == "draining"
    assert lease["job_remove_pending"] is True
    assert not (managed / "results" / "index.jsonl").exists()
    monkeypatch.setattr(node, "_remove_job", lambda label: True)
    token = (run / "token-standin").open("a+")
    try:
        node._release_holder(root, run, lease, token)
    finally:
        token.close()
    released = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert released["state"] == "released"
    assert released["job_remove_pending"] is False


def _dead_holder_request(tmp_path, monkeypatch, *, state_name, deadline_offset):
    import builtins
    import goalflight_remote_ci_node as node

    monkeypatch.setattr(
        builtins, "_GOALFLIGHT_REMOTE_CI_AUTHORITY",
        str(tmp_path / "auth.json"), raising=False)
    managed = tmp_path / "managed"
    run = managed / "runs" / "abc"
    run.mkdir(parents=True)
    remote = {
        "host": "h", "pid": "44", "start_token": "st",
        "run_dir": str(run), "lease_id": "abc", "lease_token": "tok",
    }
    owner = {"owner_host": "h", "owner_pid": 1, "owner_identity": "t"}
    state = {
        "schema": "goalflight.remote-ci.lease.v1",
        "lease_id": "abc", "lease_token": "tok", "state": state_name,
        "token_index": 0, "launch_label": "com.goalflight.remote-ci.abc",
        "coalition_id": 100, "holder_coalition_id": 5,
        "deadline_epoch": time.time() + deadline_offset,
        "remote_run": remote, "job_remove_pending": True,
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    return node, managed, run, remote, owner


def test_draining_lease_retries_removal_before_the_deadline(tmp_path, monkeypatch):
    node, managed, run, remote, owner = _dead_holder_request(
        tmp_path, monkeypatch, state_name="draining", deadline_offset=3600)
    removes = {"n": 0}

    def remove(label):
        del label
        removes["n"] += 1
        return False

    monkeypatch.setattr(node, "_remove_job", remove)
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [])
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    result = node.dispatch({
        "operation": "cancel", "managed_root": str(managed), "box": "b",
        "p_cores": 4, "token_pool_size": 1, "run_dir": str(run),
        "lease_token": "tok", "identity": remote, "expected_owner": owner,
    })
    assert removes["n"] >= 1
    assert result["status"] == "unknown"
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["state"] == "draining"


def test_draining_lease_without_launch_identity_keeps_capacity(tmp_path, monkeypatch):
    """Pre-deadline reap must not treat 'no label' as an empty tree."""
    import builtins
    import goalflight_remote_ci_node as node

    monkeypatch.setattr(
        builtins, "_GOALFLIGHT_REMOTE_CI_AUTHORITY",
        str(tmp_path / "auth.json"), raising=False)
    managed = tmp_path / "managed"
    run = managed / "runs" / "abc"
    run.mkdir(parents=True)
    slot = tmp_path / "slot"
    slot.mkdir()
    remote = {
        "host": "h", "pid": "44", "start_token": "st",
        "run_dir": str(run), "lease_id": "abc", "lease_token": "tok",
    }
    owner = {"owner_host": "h", "owner_pid": 1, "owner_identity": "t"}
    state = {
        "schema": "goalflight.remote-ci.lease.v1",
        "lease_id": "abc", "lease_token": "tok", "state": "draining",
        "token_index": 0, "slot": str(slot),
        "deadline_epoch": time.time() + 3600,
        "remote_run": remote,
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    monkeypatch.setattr(node, "cwd_intruders", lambda paths: [os.getpid()])
    result = node.dispatch({
        "operation": "cancel", "managed_root": str(managed), "box": "b",
        "p_cores": 4, "token_pool_size": 1, "run_dir": str(run),
        "lease_token": "tok", "identity": remote, "expected_owner": owner,
    })
    assert result["status"] == "unknown"
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["state"] == "draining"
    monkeypatch.setattr(node, "cwd_intruders", lambda paths: None)
    again = node.dispatch({
        "operation": "cancel", "managed_root": str(managed), "box": "b",
        "p_cores": 4, "token_pool_size": 1, "run_dir": str(run),
        "lease_token": "tok", "identity": remote, "expected_owner": owner,
    })
    assert again["status"] == "unknown"
    assert json.loads((run / "lease.json").read_text(encoding="utf-8"))["state"] == "draining"


def test_cwd_intruders_sees_through_a_symlink(tmp_path):
    """lsof reports the resolved cwd. The stored slot path may be the link."""
    import goalflight_remote_ci_node as node

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=real)
    try:
        found = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            found = node.cwd_intruders([link])
            if found and proc.pid in found:
                break
            time.sleep(0.05)
        assert found is not None and proc.pid in found
        state = {"lease_id": "abc", "state": "draining", "slot": str(link)}
        run = tmp_path / "run"
        run.mkdir()
        (run / "lease.json").write_text("{}", encoding="utf-8")
        assert node.clear_tree(tmp_path, state, run) is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_cwd_intruders_matches_a_case_variant(tmp_path):
    """Same directory, different spelling. realpath keeps the caller's case."""
    import goalflight_remote_ci_node as node

    real = tmp_path / "CaseSlot"
    real.mkdir()
    variant = tmp_path / "caseslot"
    assert variant.exists()
    assert os.stat(real).st_ino == os.stat(variant).st_ino
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=real)
    try:
        found = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            found = node.cwd_intruders([variant])
            if found and proc.pid in found:
                break
            time.sleep(0.05)
        assert found is not None and proc.pid in found
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_unreadable_cwd_liveness_is_not_an_empty_slot(tmp_path, monkeypatch):
    """EPERM is not ESRCH. An unverifiable occupant keeps the slot."""
    import goalflight_remote_ci_node as node

    real = tmp_path / "real"
    real.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=real)
    real_getpgid = os.getpgid
    try:
        seen = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            seen = node.cwd_intruders([real])
            if seen and proc.pid in seen:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("cwd occupant was not visible")

        def getpgid(pid):
            if int(pid) == proc.pid:
                raise PermissionError(1, "Operation not permitted")
            return real_getpgid(pid)

        monkeypatch.setattr(node.os, "getpgid", getpgid)
        assert node.cwd_intruders([real]) is None
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_absent_launch_with_a_live_cwd_is_not_empty(tmp_path, monkeypatch):
    """Intent without launch_submitted, job absent, someone still in the dir."""
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "slot": str(run),
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=run)
    try:
        seen = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            seen = node.cwd_intruders([run])
            if seen and proc.pid in seen:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("cwd occupant was not visible")
        assert node.clear_tree(tmp_path, state, run) is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_absent_launch_releases_when_cwd_is_empty(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "slot": str(run),
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    monkeypatch.setattr(node, "cwd_intruders", lambda paths: [])
    assert node.clear_tree(tmp_path, state, run) is True


def test_reused_job_pid_is_not_adopted(tmp_path, monkeypatch):
    """A pid that changes between the listing and the identity read is not killed."""
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    calls = {"n": 0}

    def job_view(label):
        del label
        calls["n"] += 1
        if calls["n"] == 1:
            return ("running", 50, None)
        return ("running", 99, None)

    killed = []
    monkeypatch.setattr(node, "_job_view", job_view)
    monkeypatch.setattr(node, "_stable_identity", lambda pid: ((1, 2), 777))
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 5)
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [])
    monkeypatch.setattr(node, "_remove_job", lambda label: False)
    monkeypatch.setattr(node.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert node.clear_tree(tmp_path, state, run) is False
    assert state.get("coalition_id") != 777
    assert killed == []


def test_command_timeout_does_not_prove_an_empty_tree(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 10)

    monkeypatch.setattr(node.subprocess, "run", boom)
    assert node._launchctl_jobs() is None
    assert node._job_view("com.goalflight.remote-ci.abc") == "unknown"
    assert node.cwd_snapshot() is None
    run = tmp_path / "run"
    run.mkdir()
    (run / "lease.json").write_text("{}", encoding="utf-8")
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "slot": str(run),
    }
    assert node.clear_tree(tmp_path, state, run) is False
    assert node._remove_job("com.goalflight.remote-ci.abc") is False


def test_unlisted_submit_is_not_an_empty_tree(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {"lease_id": "abc", "state": "running"}
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 30.0
        return clock["t"]

    monkeypatch.setattr(node.time, "monotonic", monotonic)
    monkeypatch.setattr(node.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(node.subprocess, "run", lambda argv, **kwargs: type("R", (), {
        "returncode": 0, "stderr": "", "stdout": ""})())
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    assert node._submit_workload(run, state, ["/bin/sleep", "1"], {}, "") is None
    assert state.get("launch_submitted") is True
    assert not state.get("launch_seen")
    # The job was never listed. A live cwd is not an empty tree.
    monkeypatch.setattr(node, "cwd_intruders", lambda paths: [os.getpid()])
    assert node.clear_tree(tmp_path, state, run) is False


def test_unreadable_coalition_is_not_an_empty_sweep(tmp_path, monkeypatch):
    """A scan that cannot prove membership neither kills nor counts as empty."""
    import goalflight_remote_ci_node as node

    managed = tmp_path / "managed"
    run = managed / "runs" / "abc"
    run.mkdir(parents=True)
    (run / "lease.json").write_text(json.dumps({
        "launch_label": "com.goalflight.remote-ci.abc",
        "coalition_id": 4242,
        "holder_coalition_id": 7,
    }), encoding="utf-8")
    monkeypatch.setattr(node, "_coalition_members", lambda cid: None)
    monkeypatch.setattr(
        sys.modules[__name__], "_launchctl_run",
        lambda *args, **kwargs: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    _sweep_unknown.clear()
    try:
        _sweep_managed_jobs(managed)
        assert _sweep_unknown == [4242]
        assert _sweep_unsignalled == []
    finally:
        _sweep_unknown.clear()
        _swept_coalitions.clear()
        _sweep_unsignalled.clear()


def test_sweep_rechecks_incarnation_before_kill(tmp_path, monkeypatch):
    """The teardown sweep must not signal a pid whose incarnation was not re-read."""
    import goalflight_remote_ci_node as node

    managed = tmp_path / "managed"
    run = managed / "runs" / "abc"
    run.mkdir(parents=True)
    (run / "lease.json").write_text(json.dumps({
        "launch_label": "com.goalflight.remote-ci.abc",
        "coalition_id": 4242,
        "holder_coalition_id": 7,
    }), encoding="utf-8")
    started = (11, 22)
    calls = []
    killed = []

    def signal_incarnation(pid, started_at, cid):
        calls.append((pid, started_at, cid))
        return False

    monkeypatch.setattr(node, "_coalition_members", lambda cid: [(4321, started)])
    monkeypatch.setattr(node, "_signal_incarnation", signal_incarnation)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(
        sys.modules[__name__], "_launchctl_run",
        lambda *args, **kwargs: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    try:
        _sweep_managed_jobs(managed)
        assert calls == [(4321, started, 4242)]
        assert killed == []
        assert _sweep_unsignalled == [(4242, 4321, started)]
    finally:
        _sweep_unknown.clear()
        _swept_coalitions.clear()
        _sweep_unsignalled.clear()


def test_dead_running_holder_removes_the_launchd_job(tmp_path, monkeypatch):
    """A dead holder must not leave its launchd job until the deadline."""
    node, managed, run, remote, owner = _dead_holder_request(
        tmp_path, monkeypatch, state_name="running", deadline_offset=3600)
    removes = {"n": 0}
    monkeypatch.setattr(node, "_remove_job", lambda label: removes.__setitem__("n", removes["n"] + 1) or False)
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [])
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    result = node.dispatch({
        "operation": "cancel", "managed_root": str(managed), "box": "b",
        "p_cores": 4, "token_pool_size": 1, "run_dir": str(run),
        "lease_token": "tok", "identity": remote, "expected_owner": owner,
    })
    assert removes["n"] >= 1
    assert result["status"] == "unknown"
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["state"] == "draining"
    assert lease["job_remove_pending"] is True


def test_deadline_kill_of_a_dead_holder_records_deadline(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    managed = tmp_path / "managed"
    root = managed / "admission"
    root.mkdir(parents=True)
    (root / "queue.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(node, "clear_tree", lambda *args, **kwargs: True)

    def make(name, **extra):
        run = managed / "runs" / name
        run.mkdir(parents=True)
        state = {
            "lease_id": name, "lease_token": "tok", "state": "draining",
            "token_index": 0, "launch_label": "com.goalflight.remote-ci." + name,
        }
        state.update(extra)
        (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
        return run, state

    run, state = make("noexit")
    assert node.finish_dead_workload(root, run, state, "deadline")["status"] == "cancelled"
    body = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert body == {"returncode": 124, "timed_out": True, "status": "deadline"}

    run, state = make("zero", exit_known=True, exit_code=0)
    node.finish_dead_workload(root, run, state, "deadline")
    body = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert body["status"] == "deadline"
    assert body["returncode"] == 124
    assert body["timed_out"] is True

    run, state = make("kept")
    (run / "result.json").write_text(
        json.dumps({"status": "died", "returncode": 9, "timed_out": False}),
        encoding="utf-8")
    node.finish_dead_workload(root, run, state, "deadline")
    body = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert body["status"] == "died"
    assert body["returncode"] == 9


def test_unreadable_lease_holds_capacity(tmp_path, monkeypatch):
    import builtins
    import goalflight_remote_ci_node as node

    monkeypatch.setattr(
        builtins, "_GOALFLIGHT_REMOTE_CI_AUTHORITY",
        str(tmp_path / "auth.json"), raising=False)
    managed = tmp_path / "managed"
    run = managed / "runs" / "bad"
    run.mkdir(parents=True)
    (run / "lease.json").write_text("{", encoding="utf-8")
    assert node.reserved_token_indexes(managed) is None
    report = node.dispatch({
        "operation": "health", "managed_root": str(managed), "box": "b",
        "p_cores": 8, "token_pool_size": 2,
    })
    assert report["tokens"]["in_use"] == 2
    assert report["tokens"]["free"] == 0
    listed = node.dispatch({
        "operation": "list", "managed_root": str(managed), "box": "b",
        "p_cores": 8, "token_pool_size": 2,
    })
    unread = [row for row in listed if row.get("state") == "UNREADABLE"]
    assert len(unread) == 1
    assert unread[0]["path"] == str(run / "lease.json")
    assert unread[0]["run_directory"] == str(run)


def test_wait_dead_treats_a_zombie_as_gone(monkeypatch):
    clock = {"n": 0}

    def monotonic():
        clock["n"] += 1
        return 0.0 if clock["n"] < 6 else 10.0

    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(os, "getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError(pid)))
    _wait_dead(123)


def test_wait_dead_dumps_state_when_the_pid_survives(monkeypatch):
    clock = {"n": 0}

    def monotonic():
        clock["n"] += 1
        return 0.0 if clock["n"] < 3 else 100.0

    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monkeypatch.setattr(os, "getpgid", lambda pid: 0)
    with pytest.raises(AssertionError, match="no launch diagnostic context"):
        _wait_dead(123)


@pytest.mark.parametrize("kind,status,code,extra", [
    ("capacity-refused", "capacity", 75, {}),
    ("cancelled", "cancelled", 130, {}),
    ("deadline", "timeout", 124, {"timed_out": True}),
    ("died", "died", 9, {}),
])
def test_structured_outcome_ignores_stdout(tmp_path, kind, status, code, extra):
    config = _config(tmp_path)
    runner = RemoteRunner(config, executor=lambda argv, env, timeout: None)
    identity = {
        "host": "measured-node", "pid": "4", "start_token": "tok",
        "run_dir": "/runs/abc", "lease_id": "abc", "lease_token": "lease",
    }
    record = {
        "remote_run": identity,
        "holder_alive": True,
        "sample": {"hostname": "measured-node"},
        "result": {
            "status": kind,
            "returncode": code,
            "stdout": "initializing tests\n",
            "stderr": "",
            **extra,
        },
    }

    class Node:
        def call(self, operation, **kwargs):
            assert operation == "status"
            return record

    outcome = runner._watch(
        spec(), Node(), {"run_dir": "/runs/abc", "lease_token": "lease"}, {})
    assert outcome.status == status
    assert outcome.returncode == code


# HOST-ONLY TESTS
# These start real launchd jobs. The regressions above do not.

def test_fast_exit_keeps_its_status_and_kills_the_detached_child(node_env, tmp_path):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    pidfile = tmp_path / "fast-child.pid"
    node.call("start", **key(held), command={
        "argv": [sys.executable, "-c",
                 "import os, time\n"
                 f"path = {str(pidfile)!r}\n"
                 "pid = os.fork()\n"
                 "if pid == 0:\n"
                 "    os.setsid()\n"
                 "    os.chdir('/')\n"
                 "    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)\n"
                 "    os.write(fd, str(os.getpid()).encode())\n"
                 "    os.close(fd)\n"
                 "    time.sleep(60)\n"
                 "    os._exit(0)\n"
                 "while True:\n"
                 "    try:\n"
                 "        if os.path.getsize(path) > 0:\n"
                 "            break\n"
                 "    except OSError:\n"
                 "        pass\n"
                 "    time.sleep(0.01)\n"
                 "os._exit(19)\n"],
        "env": {},
        "timeout": 30,
    })
    child = _child_pid(pidfile)
    try:
        deadline = time.monotonic() + 8
        result = None
        while time.monotonic() < deadline:
            current = node.call("status", **key(held))
            result = current.get("result")
            if result:
                break
            time.sleep(0.05)
        assert result is not None
        assert result["status"] == "died"
        assert result["returncode"] == 19
        # A just-killed pid can still be a zombie. kill(pid, 0) succeeds
        # until it is reaped, so a single check flakes. Wait until it is gone.
        _wait_dead(child, node, held)
        _free_tokens(node)
        listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        assert held["lease_id"] not in listed.stdout
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_unreadable_leader_liveness_does_not_start_cleanup(tmp_path, monkeypatch):
    """None from the leader check is not an exit. Cleanup must wait."""
    import builtins
    import goalflight_remote_ci_node as node

    monkeypatch.setattr(
        builtins, "_GOALFLIGHT_REMOTE_CI_AUTHORITY",
        str(tmp_path / "auth.json"), raising=False)
    phase = {"n": 0}

    def same(pid, started):
        del pid, started
        phase["n"] += 1
        if phase["n"] < 5:
            return None
        return False

    def clear(*args, **kwargs):
        del args, kwargs
        if phase["n"] < 5:
            raise AssertionError("cleanup started while leader liveness was unknown")
        return True

    monkeypatch.setattr(node, "_same_process", same)
    monkeypatch.setattr(node, "clear_tree", clear)
    monkeypatch.setattr(node, "_submit_workload", lambda *args, **kwargs: (42, (1, 2), 100))
    monkeypatch.setattr(node, "_await_workload_exec", lambda *args, **kwargs: True)
    monkeypatch.setattr(node, "_remove_job", lambda label: True)
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [])
    monkeypatch.setattr(node, "cwd_intruders", lambda paths: [])
    managed = tmp_path / "managed"
    owner = {"owner_host": "h", "owner_pid": 1, "owner_identity": "t"}
    record = node.dispatch({
        "operation": "enqueue", "managed_root": str(managed), "box": "b",
        "p_cores": 100000, "token_pool_size": 1, "poll_seconds": 0.01,
        "request_id": "leader-unknown", "arm": "candidate", "owner": owner,
        "command_wait_seconds": 5,
    })
    holder = None
    key = {
        "operation": "status", "managed_root": str(managed), "box": "b",
        "p_cores": 100000, "token_pool_size": 1,
        "run_dir": record["run_directory"], "lease_token": record["lease_token"],
    }
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = node.dispatch(key)
            if current.get("state") == "admitted":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("holder was not admitted")
        node.dispatch({
            **key,
            "operation": "start",
            "command": {"argv": ["/bin/sleep", "30"], "env": {}, "timeout": 30},
        })
        deadline = time.monotonic() + 5
        body = None
        while time.monotonic() < deadline:
            lease_path = Path(record["run_directory"]) / "lease.json"
            try:
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                lease = {}
            remote = lease.get("remote_run") or {}
            if remote.get("pid"):
                holder = int(remote["pid"])
            result_path = Path(record["run_directory"]) / "result.json"
            if result_path.exists():
                body = json.loads(result_path.read_text(encoding="utf-8"))
                break
            time.sleep(0.02)
        assert body is not None
        assert "leader liveness was unknown" not in str(body.get("error"))
    finally:
        if holder:
            try:
                os.kill(holder, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(holder, 0)
            except ChildProcessError:
                pass
