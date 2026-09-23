"""Hermetic contracts for the project-neutral remote CI runner."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from goalflight_remote_ci import (
    AdmissionConfig,
    ArmOutcome,
    ArmSpec,
    BoxConfig,
    CommandResult,
    DaemonConfig,
    GateDaemon,
    LoadSample,
    RemoteRunIdentity,
    RemoteLeaseRegistry,
    RemoteRunner,
    ReceiptError,
    TokenPool,
    build_pair_specs,
    chunk_test_files,
    health_census,
    load_config,
    cleanup_dead_leases,
    matched_pair_verdict,
    parse_receipt,
    reap_orphans,
    receipt_from_output,
    run_command,
    list_remote_leases,
    submit_request,
    validate_request,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "remote_ci"


def _raw_config(tmp_path: Path, *, token_pool_size: int = 2) -> dict:
    return {
        "schema": "goalflight.remote-ci.config.v1",
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
                "token_key": "ci-worker.example.invalid",
                "p_cores": 20,
                "token_pool_size": token_pool_size,
                "load_command": [sys.executable, "-c", "print('{}')"],
                "env": {},
                "managed_run_directory": str(tmp_path / "managed-runs"),
            }
        },
        "admission": {
            "token_directory": str(tmp_path / "shared-tokens"),
            "queue_wait_seconds": 0.01,
        },
        "runner": {
            "command": ["run", "{arm}", "{sha}", "{selection}"],
            "watch_command": ["watch", "{run_dir}", "{pid}", "{start_token}"],
            "collect_command": ["collect", "{run_dir}", "{pid}", "{start_token}"],
            "cancel_command": ["cancel", "{run_dir}", "{pid}", "{start_token}", "{reason}"],
            "test_command": ["python3", "-m", "pytest"],
            "env": {},
            "timeout_seconds": 30,
            "self_cap": 4,
            "self_cap_env": "TEST_WORKERS",
            "chunk_size": 20,
            "verbose_option": "-v",
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


def test_config_validation_rejects_relative_shared_token_directory(tmp_path: Path) -> None:
    raw = _raw_config(tmp_path)
    raw["admission"]["token_directory"] = "relative/tokens"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Exception, match="token_directory must be absolute"):
        load_config(path)


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


def test_flock_token_is_released_by_sigkill(tmp_path: Path) -> None:
    config = _config(tmp_path, token_pool_size=1)
    box = _box(config)
    child_code = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from goalflight_remote_ci import AdmissionConfig, BoxConfig, TokenPool, LoadSample
box = BoxConfig('box-a', 'ci-worker.example.invalid', 'ci-worker.example.invalid', 20, 1, ('true',), {})
pool = TokenPool(box, AdmissionConfig(Path(sys.argv[2]), 1, None), load_probe=lambda _: LoadSample('ci-worker.example.invalid', 0, 20))
lease = pool.try_acquire()
print('READY', flush=True)
time.sleep(60)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            str(ROOT / "scripts"),
            str(config.admission.token_directory),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        parent_pool = TokenPool(
            box,
            config.admission,
            load_probe=lambda _: LoadSample("ci-worker.example.invalid", 0, 20),
        )
        assert parent_pool.try_acquire() is None
        process.kill()
        process.wait(timeout=5)
        lease = parent_pool.try_acquire()
        assert lease is not None
        lease.release()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_admission_queues_until_token_and_load_are_safe(tmp_path: Path) -> None:
    config = _config(tmp_path, token_pool_size=1)
    box = _box(config)
    holder = TokenPool(box, config.admission, load_probe=lambda _: LoadSample("ci-worker.example.invalid", 0, 20))
    held = holder.try_acquire()
    assert held is not None
    sleeps: list[float] = []
    probes = iter([LoadSample("ci-worker.example.invalid", 21, 20), LoadSample("ci-worker.example.invalid", 3, 20)])
    contender = TokenPool(
        box,
        config.admission,
        load_probe=lambda _: next(probes),
        sleeper=lambda seconds: sleeps.append(seconds) or held.release(),
    )
    lease = contender.acquire()
    assert lease.sample is not None and lease.sample.load1 == 3
    assert len(sleeps) == 2
    lease.release()


def test_queue_order_is_stable_and_single_runner_picks_oldest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.queue_dir.mkdir(parents=True)
    for name in ("request-002", "request-001"):
        (config.queue_dir / f"{name}.json").write_text("{}", encoding="utf-8")
    daemon = GateDaemon(config, runner=RemoteRunner(config, load_probe=lambda _: LoadSample("ci-worker.example.invalid", 0, 20)))
    assert [path.stem for path in daemon.pending_paths()] == ["request-001", "request-002"]


def test_local_timeout_propagates_remote_cancel_and_releases_token(tmp_path: Path) -> None:
    config = _config(tmp_path, token_pool_size=1)
    calls: list[tuple[list[str], dict[str, str], float | None]] = []

    def executor(argv, env, timeout):
        calls.append((list(argv), dict(env), timeout))
        if argv[0] == "run":
            return CommandResult(
                124,
                f"REMOTE_RUN_LAUNCHED pid=42 start_token=abc run_dir={_box(config).managed_run_directory / 'remote-run'} lock_dir=/remote/lock\n",
                "",
                True,
            )
        return CommandResult(0)

    runner = RemoteRunner(
        config,
        executor=executor,
        load_probe=lambda _: LoadSample("ci-worker.example.invalid", 0, 20),
    )
    outcome = runner.run_arm(
        ArmSpec(
            "candidate",
            "b" * 40,
            "a" * 40,
            "b" * 40,
            ("tests/test_one.py",),
            ("tests/test_one.py", "-v"),
            "request-1",
            "box-a",
            False,
        )
    )
    assert outcome.status == "timeout"
    assert outcome.cancelled is True
    assert calls[1][0] == [
        "cancel",
        str(_box(config).managed_run_directory / "remote-run"),
        "42",
        "abc",
        "local-timeout",
    ]
    lease = TokenPool(_box(config), config.admission).try_acquire()
    assert lease is not None
    lease.release()


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


def test_chunking_adds_verbose_to_each_twenty_file_chunk() -> None:
    files = [f"tests/test_{index}.py" for index in range(41)]
    chunks = chunk_test_files(files, size=20)
    assert [len(chunk) for chunk in chunks] == [21, 21, 2]
    assert all(chunk[-1] == "-v" for chunk in chunks)


def test_live_caps_health_census_and_base_overlay_contract(tmp_path: Path) -> None:
    raw = _raw_config(tmp_path, token_pool_size=2)
    cap_file = tmp_path / "caps.json"
    cap_file.write_text(
        json.dumps({"self_cap": 3, "boxes": {"box-a": {"p_cores": 20, "token_pool_size": 1}}}),
        encoding="utf-8",
    )
    raw["admission"]["live_cap_file"] = str(cap_file)
    path = tmp_path / "live-cap-config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(path)
    pool = TokenPool(_box(config), config.admission, load_probe=lambda _: LoadSample("ci-worker.example.invalid", 0, 20))
    assert pool.census() == {"total": 1, "free": 1, "in_use": 0}
    request = {
        "request_id": "request-1",
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py", "-q"],
    }
    base, candidate = build_pair_specs(request, config)
    assert base.overlay is True
    assert base.sha == request["tip_sha"]
    assert base.test_files == ("tests/test_one.py",)
    assert candidate.overlay is False
    env_seen: dict[str, str] = {}

    def executor(_argv, env, _timeout):
        env_seen.update(env)
        return CommandResult(0, (FIXTURES / "receipt-pass.json").read_text())

    runner = RemoteRunner(
        config,
        executor=executor,
        load_probe=lambda _: LoadSample("ci-worker.example.invalid", 0, 20),
    )
    outcome = runner.run_arm(candidate)
    assert env_seen["TEST_WORKERS"] == "3"
    assert outcome.lease is not None
    assert outcome.lease.state == "released"
    assert outcome.lease.lease_token == env_seen["GOALFLIGHT_REMOTE_CI_LEASE_TOKEN"]
    assert Path(outcome.lease.run_directory).parent == _box(config).managed_run_directory
    assert any(
        lease["lease_id"] == outcome.lease.lease_id and lease["state"] == "released"
        for lease in list_remote_leases(config)
    )
    census = health_census(
        config,
        load_probe=lambda _: LoadSample("ci-worker.example.invalid", 4, 20),
    )
    assert census["boxes"][0]["load"]["hostname"] == "ci-worker.example.invalid"
    assert census["boxes"][0]["tokens"]["total"] == 1


def test_orphan_reaper_carries_exact_remote_identity() -> None:
    cancelled: list[tuple[RemoteRunIdentity, str]] = []
    records = [
        {
            "request_id": "request-1",
            "state": "running",
            "owner_pid": 1,
            "remote_run": {
                "host": "ci-worker.example.invalid",
                "pid": "77",
                "start_token": "start-9",
                "run_dir": "/remote/run-9",
            },
        }
    ]
    result = reap_orphans(
        records,
        owner_alive=lambda _record: False,
        cancel=lambda identity, reason: cancelled.append((identity, reason)),
    )
    assert result[0]["status"] == "cancelled"
    assert cancelled[0][0].pid == "77"
    assert cancelled[0][0].start_token == "start-9"
    assert cancelled[0][1] == "orphaned-remote-run"


def test_orphan_reaper_does_not_release_or_cancel_when_owner_liveness_is_unknown() -> None:
    cancelled: list[RemoteRunIdentity] = []
    result = reap_orphans(
        [{"request_id": "request-unknown", "state": "running", "owner_pid": 77}],
        owner_alive=lambda _record: None,
        cancel=lambda identity, _reason: cancelled.append(identity),
    )
    assert result == [{"request_id": "request-unknown", "status": "unknown"}]
    assert cancelled == []


def test_remote_lease_list_and_owner_death_cleanup_require_proof(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = RemoteLeaseRegistry(config)
    lease = registry.acquire(_box(config), request_id="request-lease", arm="candidate")
    record = lease.record
    assert Path(record.run_directory).parent == _box(config).managed_run_directory
    assert record.owner_identity
    assert record.lease_token
    assert any(
        item["lease_id"] == record.lease_id and item["state"] == "active"
        for item in list_remote_leases(config)
    )

    assert cleanup_dead_leases(config, owner_alive=lambda _record: None) == [
        {"lease_id": record.lease_id, "status": "unknown"}
    ]
    assert any(
        item["lease_id"] == record.lease_id and item["state"] == "active"
        for item in list_remote_leases(config)
    )

    # Simulate the kernel releasing a killed owner; the record is still active
    # until cleanup observes a proven-dead owner.
    lease.released = True
    lease.handle.close()
    assert cleanup_dead_leases(config, owner_alive=lambda _record: False) == [
        {"lease_id": record.lease_id, "status": "released"}
    ]
    released = next(item for item in list_remote_leases(config) if item["lease_id"] == record.lease_id)
    assert released["state"] == "released"
    assert released["release_reason"] == "owner-death"


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


def test_reattach_watches_and_collects_launch_log_without_rerunning(tmp_path: Path) -> None:
    config = _config(tmp_path)
    launch_log = tmp_path / "launch.log"
    launch_log.write_text(
        f"REMOTE_RUN_LAUNCHED pid=42 start_token=abc run_dir={_box(config).managed_run_directory / 'request-1-candidate'} lock_dir=/remote/lock\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def executor(argv, _env, _timeout):
        calls.append(list(argv))
        if argv[0] == "watch":
            return CommandResult(0)
        if argv[0] == "collect":
            return CommandResult(0, (FIXTURES / "receipt-pass.json").read_text())
        raise AssertionError(f"unexpected command: {argv}")

    runner = RemoteRunner(
        config,
        executor=executor,
        load_probe=lambda _: LoadSample("ci-worker.example.invalid", 0, 20),
    )
    outcome = runner.reattach_launch_log(
        launch_log,
        ArmSpec(
            "candidate",
            "b" * 40,
            "a" * 40,
            "b" * 40,
            (),
            (),
            "request-1",
            "box-a",
            False,
        ),
    )
    assert outcome.status == "green"
    assert outcome.identity == RemoteRunIdentity(
        "ci-worker.example.invalid",
        "42",
        "abc",
        str(_box(config).managed_run_directory / "request-1-candidate"),
        "/remote/lock",
    )
    assert calls == [
        ["watch", str(_box(config).managed_run_directory / "request-1-candidate"), "42", "abc"],
        ["collect", str(_box(config).managed_run_directory / "request-1-candidate"), "42", "abc"],
    ]


def test_reattach_timeout_cancels_recorded_identity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[list[str]] = []

    def executor(argv, _env, _timeout):
        calls.append(list(argv))
        if argv[0] == "watch":
            return CommandResult(124, timed_out=True)
        assert argv[0] == "cancel"
        return CommandResult(0)

    runner = RemoteRunner(config, executor=executor)
    outcome = runner.reattach(
        ArmSpec("candidate", "b" * 40, "a" * 40, "b" * 40, (), (), "request-1", "box-a", False),
        RemoteRunIdentity(
            "ci-worker.example.invalid",
            "42",
            "abc",
            str(_box(config).managed_run_directory / "request-1-candidate"),
        ),
    )
    assert outcome.status == "timeout"
    assert outcome.cancelled is True
    assert calls == [
        ["watch", str(_box(config).managed_run_directory / "request-1-candidate"), "42", "abc"],
        [
            "cancel",
            str(_box(config).managed_run_directory / "request-1-candidate"),
            "42",
            "abc",
            "reattach-timeout",
        ],
    ]
