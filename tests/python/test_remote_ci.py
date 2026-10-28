"""Hermetic contracts for the project-neutral remote CI runner."""

from __future__ import annotations

import base64
from dataclasses import replace
import errno
import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import warnings

import pytest
import goalflight_remote_ci as ci
from goalflight_remote_ci import (
    ArmOutcome, ArmSpec, BoxConfig, CommandResult, DaemonConfig, GateDaemon,
    RemoteRunner, ReceiptError, build_pair_specs, health_census,
    load_config, matched_pair_verdict, parse_receipt, receipt_from_output,
    run_command, list_remote_leases, submit_request, validate_request,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "remote_ci"


@pytest.fixture(autouse=True)
def fake_ssh_connection(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.2 51234 192.0.2.10 22")


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
                "expected_hostname": "measured-node",
                "python_interpreter": sys.executable,
                "p_cores": 20,
                "token_pool_size": token_pool_size,
                "remote_exec": ["scripted-remote", "{host}", "{script}"],
                "env": {},
                # Config-only tests must remain valid when pytest's temp base
                # is under /tmp, which production correctly rejects.
                "managed_run_directory": str(ROOT / "tests" / "fixtures" / "remote_ci" / "managed"),
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
    raw = _raw_config(tmp_path, token_pool_size=token_pool_size)
    raw["boxes"]["box-a"]["managed_run_directory"] = str(tmp_path / "fake-node")
    path.write_text(json.dumps(raw), encoding="utf-8")
    return _load_fake_node_config(path)


def _queue_daemon_request(config: DaemonConfig, request_id: str) -> Path:
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    request_path = config.queue_dir / f"{request_id}.json"
    request_path.write_text("{}", encoding="utf-8")
    return request_path


def _git(root: Path, *args: str) -> str:
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_NAMESPACE"):
        env.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        check=False, env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Remote CI test")
    _git(root, "config", "user.email", "remote-ci-test@example.invalid")


_TEST_SOURCE_TEMPORARY = None
_TEST_SOURCE = None


def _test_source():
    """A tiny real Git history for fake-node pool setup."""
    global _TEST_SOURCE_TEMPORARY, _TEST_SOURCE
    if _TEST_SOURCE is None:
        _TEST_SOURCE_TEMPORARY = tempfile.TemporaryDirectory(
            prefix="remote-ci-source-", dir=os.environ.get("TMPDIR"),
        )
        root = Path(_TEST_SOURCE_TEMPORARY.name) / "repo"
        _init_repo(root)
        (root / "README").write_text("source snapshot\n", encoding="utf-8")
        nested = root / "nested"
        nested.mkdir()
        (nested / "included.txt").write_text("bundled nested directory\n", encoding="utf-8")
        _git(root, "add", "README", "nested/included.txt")
        _git(root, "commit", "-m", "source snapshot")
        sha = _git(root, "rev-parse", "HEAD")
        _TEST_SOURCE = (root, sha, Path(_TEST_SOURCE_TEMPORARY.name))
    return _TEST_SOURCE


def _source_bundle_env(record):
    root, sha, _temporary_root = _test_source()
    run_dir = Path(record["run_directory"])
    bundle = run_dir / "source.bundle"
    _git(root, "bundle", "create", str(bundle), "HEAD")
    return {
        "GOALFLIGHT_REMOTE_CI_SHA": sha,
        "GOALFLIGHT_REMOTE_CI_SOURCE_BUNDLE": str(bundle),
    }


def _load_fake_node_config(path: Path) -> DaemonConfig:
    """Load an isolated test config whose fake managed root is under tmp_path."""
    managed_path = ci._managed_path
    try:
        ci._managed_path = lambda value, label: (
            Path(value) if label.endswith("managed_run_directory")
            and Path(value).is_absolute() else managed_path(value, label)
        )
        return load_config(path)
    finally:
        ci._managed_path = managed_path


def _real_user_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _safe_managed_temp_base(path: Path) -> Path:
    """Keep fake managed roots out of /tmp and the real user's Goal Flight state."""
    try:
        base = Path(path).resolve()
        forbidden = (
            Path("/tmp").resolve(),
            (_real_user_home() / ".goal-flight").resolve(),
        )
    except (KeyError, OSError):
        return Path("/var/tmp")
    if any(base.is_relative_to(root) for root in forbidden):
        return Path("/var/tmp")
    return base


@pytest.fixture
def non_tmp_managed_base(tmp_path: Path):
    """Give tests managed roots outside /tmp and the real user's Goal Flight state."""
    temp_base = _safe_managed_temp_base(tmp_path)
    with tempfile.TemporaryDirectory(
        prefix=".remote-ci-managed-", dir=temp_base,
    ) as root:
        yield Path(root)


def test_managed_base_rejects_tmpdir_inside_real_home_goalflight(tmp_path, monkeypatch):
    isolated_home = tmp_path / "isolated-home"
    fake_real_home = tmp_path / "fake-real-home"
    managed_tmp = fake_real_home / ".goal-flight" / "remote-ci-managed" / "tmp" / "lease"
    managed_tmp.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("TMPDIR", str(managed_tmp))
    monkeypatch.setattr(sys.modules[__name__], "_real_user_home", lambda: fake_real_home)

    assert Path.home() == isolated_home
    assert (
        _safe_managed_temp_base(Path(os.environ["TMPDIR"]) / "pytest")
        == Path("/var/tmp")
    )


def _box(config: DaemonConfig) -> BoxConfig:
    return config.boxes["box-a"]



_REMOTE_CI_LABEL = "com.goalflight.remote-ci."
# Coalition ids a test's teardown swept. The opt-in fixture checks these after
# launch-capable integration cases finish their normal managed-job cleanup.
_swept_coalitions: list[int] = []
_sweep_unknown: list[int] = []


# Bound before tests replace subprocess.run. Teardown must still see launchctl.
_launchctl_run = subprocess.run
_launchd_observation_status = None
_launchd_unavailable_warning_emitted = False


def _remote_ci_launchd_jobs():
    """label -> pid or None when this session cannot observe launchd."""
    global _launchd_observation_status
    if _launchd_observation_status is True:
        return None

    deadline = time.monotonic() + 60
    listed = None
    empty_denials = 0
    while True:
        try:
            listed = _launchctl_run(["launchctl", "list"], capture_output=True, text=True)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM, errno.ENOENT):
                if _launchd_observation_status is False:
                    raise AssertionError(
                        "launchctl list failed after being observed; cannot verify leak state") from exc
                _launchd_observation_status = True
                return None
            listed = None
            empty_denials = 0
        if listed is None:
            empty_denials = 0
        if listed is not None and listed.returncode == 0:
            _launchd_observation_status = False
            break
        if listed is not None:
            output = f"{listed.stdout or ''}\n{listed.stderr or ''}".lower()
            denied = any(phrase in output for phrase in (
                "operation not permitted", "permission denied", "not authorized",
                "not privileged", "not allowed", "sandbox",
            ))
            empty_sandbox_denial = (
                listed.returncode == 1
                and not (listed.stdout or "").strip()
                and not (listed.stderr or "").strip()
            )
            # This sandbox emits rc=1 with empty streams, but one identical
            # failure can be transient. Require two adjacent probes before
            # caching it; after any successful listing, never disable the guard.
            if empty_sandbox_denial and _launchd_observation_status is None:
                empty_denials += 1
                if empty_denials >= 2:
                    _launchd_observation_status = True
                    return None
            else:
                empty_denials = 0
            if denied:
                if _launchd_observation_status is False:
                    raise AssertionError(
                        "launchctl list failed after being observed; cannot verify leak state")
                _launchd_observation_status = True
                return None
        if time.monotonic() >= deadline:
            if _launchd_observation_status is False:
                raise AssertionError(
                    "launchctl list failed after being observed; cannot verify leak state")
            _launchd_observation_status = True
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
    """Use production cleanup to remove jobs only after their trees are proven empty."""
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
        cid = lease.get("coalition_id")
        holder = lease.get("holder_coalition_id")
        # The login-session coalition is never a kill target. A lease that
        # failed to record a private id must not take the test process with it.
        if (isinstance(cid, int) and not isinstance(cid, bool) and cid > 0
                and cid != holder):
            _swept_coalitions.append(cid)
        result = node.finish_dead_workload(
            managed / "admission", run, lease, "test-teardown")
        if not isinstance(result, dict) or result.get("status") == "unknown":
            if isinstance(cid, int) and not isinstance(cid, bool) and cid > 0:
                _sweep_unknown.append(cid)
        label = lease.get("launch_label")
        if (lease.get("state") == "released" and isinstance(label, str)
                and label.startswith(_REMOTE_CI_LABEL)):
            if not node._remove_job(label):
                raise AssertionError(f"test teardown could not remove launchd job {label}")


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


def _warn_launchd_guard_unavailable():
    global _launchd_unavailable_warning_emitted
    if _launchd_unavailable_warning_emitted:
        return
    _launchd_unavailable_warning_emitted = True
    warnings.warn(
        "launchd leak assertion skipped: launchctl list is not observable in this session",
        pytest.PytestWarning,
        stacklevel=2,
    )


def _launchd_guard_fixture(nodeid):
    request = type("Request", (), {
        "node": type("Node", (), {"nodeid": nodeid})(),
    })()
    return no_leftover_remote_ci_job.__wrapped__(request)


def test_launchd_guard_fixture_fails_when_a_job_is_left(monkeypatch):
    _swept_coalitions.clear()
    _sweep_unknown.clear()
    results = iter((
        subprocess.CompletedProcess(["launchctl", "list"], 0, "PID\tStatus\tLabel\n", ""),
        subprocess.CompletedProcess(
            ["launchctl", "list"], 0,
            "PID\tStatus\tLabel\n4242\t0\tcom.goalflight.remote-ci.leaked\n", ""),
    ))
    monkeypatch.setattr(sys.modules[__name__], "_launchctl_run", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(sys.modules[__name__], "_launchd_observation_status", None)
    guard = _launchd_guard_fixture("designed-red")
    next(guard)
    with pytest.raises(AssertionError, match="jobs=.*com.goalflight.remote-ci.leaked"):
        next(guard)


def test_launchd_guard_fixture_passes_when_no_job_is_left(monkeypatch):
    _swept_coalitions.clear()
    _sweep_unknown.clear()
    result = subprocess.CompletedProcess(["launchctl", "list"], 0, "PID\tStatus\tLabel\n", "")
    monkeypatch.setattr(sys.modules[__name__], "_launchctl_run", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(sys.modules[__name__], "_launchd_observation_status", None)
    guard = _launchd_guard_fixture("positive-control")
    next(guard)
    with pytest.raises(StopIteration):
        next(guard)


def test_launchctl_sandbox_denial_is_cached_for_the_session(monkeypatch):
    calls = []

    def denied(argv, **kwargs):
        del kwargs
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(sys.modules[__name__], "_launchctl_run", denied)
    monkeypatch.setattr(sys.modules[__name__], "_launchd_observation_status", None)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    assert _remote_ci_launchd_jobs() is None
    assert _remote_ci_launchd_jobs() is None
    assert len(calls) == 2


def test_launchctl_transient_failure_keeps_retry_window(monkeypatch):
    calls = []

    def transient_then_success(argv, **kwargs):
        del kwargs
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 1, "", "temporarily unavailable")
        return subprocess.CompletedProcess(argv, 0, "PID\tStatus\tLabel\n", "")

    monkeypatch.setattr(sys.modules[__name__], "_launchctl_run", transient_then_success)
    monkeypatch.setattr(sys.modules[__name__], "_launchd_observation_status", None)
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    assert _remote_ci_launchd_jobs() == {}
    assert len(calls) == 2


def test_observed_launchd_does_not_become_unobservable_after_empty_failure(monkeypatch):
    calls = []

    def success_empty_success(argv, **kwargs):
        del kwargs
        calls.append(argv)
        if len(calls) == 2:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "PID\tStatus\tLabel\n", "")

    monkeypatch.setattr(sys.modules[__name__], "_launchctl_run", success_empty_success)
    monkeypatch.setattr(sys.modules[__name__], "_launchd_observation_status", None)
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    assert _remote_ci_launchd_jobs() == {}
    assert _remote_ci_launchd_jobs() == {}
    assert _launchd_observation_status is False
    assert len(calls) == 3


def test_launchd_unavailable_warning_is_emitted_once(monkeypatch):
    monkeypatch.setattr(
        sys.modules[__name__], "_launchd_unavailable_warning_emitted", False)
    with pytest.warns(pytest.PytestWarning, match="launchd leak assertion skipped") as caught:
        _warn_launchd_guard_unavailable()
        _warn_launchd_guard_unavailable()
    assert len(caught) == 1


def _assert_no_leftover_remote_ci_state(nodeid: str, before, after) -> None:
    leaked = {label: pid for label, pid in after.items() if label not in before}
    members, unknown = _live_coalition_members(list(_swept_coalitions))
    unknown = list(dict.fromkeys([*unknown, *_sweep_unknown]))
    if leaked or members or unknown:
        raise AssertionError(
            f"{nodeid} left remote CI state behind: "
            f"jobs={leaked} members={members} unknown={unknown}")


@pytest.fixture
def no_leftover_remote_ci_job(request):
    """Guard only tests that can start a launchd job or live coalition member.

    Jobs are per-user, not per tmp_path. A later test sees whatever the
    previous test did not remove. Tests opt in when they use an unmocked start
    path; tests that mock _submit_workload do not need a host-global guard.
    """
    _swept_coalitions.clear()
    _sweep_unknown.clear()
    before = _remote_ci_launchd_jobs()
    if before is None:
        _warn_launchd_guard_unavailable()
        yield
        return
    yield
    after = _remote_ci_launchd_jobs()
    if after is None:
        _warn_launchd_guard_unavailable()
        return
    _assert_no_leftover_remote_ci_state(request.node.nodeid, before, after)


@pytest.fixture
def requires_live_launchd(no_leftover_remote_ci_job):
    if _launchd_observation_status is True:
        pytest.skip("requires live launchd; launchctl list is not observable in this session")


class ScriptedExecutor:
    """Run the shipped node program in an isolated fake box; never SSH or ps."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.remote_scripts = []
        self.load1 = 0
        self.cpu_busy_fraction = 0.0
        self.hostname = "measured-node"
        self.before = None
        self._short_command_window = None

    def use_short_command_window(self, seconds=0.3):
        """Install a test copy with a short command window for expiry cases."""
        # The executor launches another interpreter and forks its holder, so seed a hash-matched test copy.
        source = (ROOT / "scripts" / "goalflight_remote_ci_node.py").read_bytes()
        window = (
            b"    return (\n"
            b"        BUNDLE_BASIS_TIMEOUT_SECONDS\n"
            b"        + BUNDLE_PREPARATION_TIMEOUT_SECONDS\n"
            b"        + BUNDLE_TRANSFER_TIMEOUT_SECONDS\n"
            b"        + max(30.0, 2.0 * controller_poll_seconds + 10.0)\n"
            b"    )\n"
        )
        assert source.count(window) == 1
        source = source.replace(window, f"    return {seconds!r}\n".encode())
        digest = hashlib.sha256(source).hexdigest()
        helper_dir = self.root / "helpers" / "remote-ci"
        helper_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        helper = helper_dir / f"goalflight_remote_ci_node-{digest}.py"
        helper.write_bytes(source)
        helper.chmod(0o600)
        self._short_command_window = (digest, len(source))

    def __call__(self, argv, env, timeout, input_data=None, input_stream=None):
        assert argv[0] == "scripted-remote"
        assert isinstance(argv[1], str) and argv[1]
        shell = shlex.split(argv[2])
        if input_data is not None and shell[:2] == ["cat", ">"]:
            transfer = shlex.split(argv[2])
            assert transfer[:2] == ["cat", ">"]
            Path(transfer[2]).write_bytes(input_data)
            return CommandResult(0)
        if input_stream is not None:
            assert shell[3] == "--stream-submit"
            self.remote_scripts.append(shlex.join(shell))
            self.calls.append("submit")
            shell[0] = sys.executable
            authority = str(self.root / "authority.json")
            idle = max(0.0, min(100.0, 100.0 * (1.0 - self.cpu_busy_fraction)))
            top_output = f"CPU usage: 0.0% user, 0.0% sys, {idle:.1f}% idle"
            shell[2] = (
                "import builtins,os,socket,subprocess,shutil;"
                "builtins._GOALFLIGHT_REMOTE_CI_AUTHORITY = " + repr(authority) + ";"
                "builtins._goalflight_test_subprocess_run=subprocess.run\n"
                "def _goalflight_test_run(argv,*args,**kwargs):\n"
                " if argv and argv[0]=='/usr/bin/top': return subprocess.CompletedProcess(argv,0," + repr(top_output) + ", '')\n"
                " return builtins._goalflight_test_subprocess_run(argv,*args,**kwargs)\n"
                "subprocess.run=_goalflight_test_run;"
                "shutil.which=lambda name:'/fake/'+name;"
                "os.getloadavg=lambda:(" + repr(self.load1) + ",0,0);"
                "socket.gethostname=lambda:" + repr(self.hostname) + ";"
                "exec(compile(" + repr(shell[2]) + ",' <remote-ci-launcher>','exec'))"
            )
            return run_command(shell, timeout=timeout, input_stream=input_stream)
        payload = json.loads(base64.b64decode(shell[3]))
        if self._short_command_window is not None:
            payload["helper_sha256"], payload["helper_size_bytes"] = self._short_command_window
            shell[3] = base64.b64encode(json.dumps(
                payload, separators=(",", ":"), sort_keys=True).encode()).decode()
        self.remote_scripts.append(shlex.join(shell))
        expected = payload["helper_sha256"]
        helper = (Path(payload["managed_root"]) / "helpers" / "remote-ci" /
                  f"goalflight_remote_ci_node-{expected}.py")
        try:
            installed = hashlib.sha256(helper.read_bytes()).hexdigest() == expected
        except OSError:
            installed = False
        uploading = input_data is not None or len(shell) > 4
        runs_helper = installed or uploading
        if runs_helper:
            self.calls.append(payload["operation"])
        if self.before and runs_helper:
            self.before(payload)
        shell[3] = base64.b64encode(json.dumps(payload, separators=(",", ":"),
                                               sort_keys=True).encode()).decode()
        shell[0] = sys.executable
        authority = str(self.root / "authority.json")
        idle = max(0.0, min(100.0, 100.0 * (1.0 - self.cpu_busy_fraction)))
        top_output = f"CPU usage: 0.0% user, 0.0% sys, {idle:.1f}% idle"
        shell[2] = (
            "import builtins,os,socket,subprocess,shutil;"
            "builtins._GOALFLIGHT_REMOTE_CI_AUTHORITY = " + repr(authority) + ";"
            "builtins._goalflight_test_subprocess_run=subprocess.run\n"
            "def _goalflight_test_run(argv,*args,**kwargs):\n"
            " if argv and argv[0]=='/usr/bin/top': return subprocess.CompletedProcess(argv,0," + repr(top_output) + ", '')\n"
            " return builtins._goalflight_test_subprocess_run(argv,*args,**kwargs)\n"
            "subprocess.run=_goalflight_test_run;"
            "shutil.which=lambda name:'/fake/'+name;"
            "os.getloadavg=lambda:(" + repr(self.load1) + ",0,0);"
            "socket.gethostname=lambda:" + repr(self.hostname) + ";"
            "exec(compile(" + repr(shell[2]) + ",' <remote-ci-launcher>','exec'))"
        )
        return run_command(
            shell, timeout=timeout, input_data=input_data,
            env={**env, "SSH_CONNECTION": "192.0.2.2 51234 192.0.2.10 22"},
        )

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
            owner = {name: record.get(name) for name in
                     ("owner_host", "owner_pid", "owner_start_token", "owner_identity")}
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


def pool_spec(request_id="request-1"):
    root, sha, temporary_root = _test_source()
    bundle_path = temporary_root / f"bundle-{time.time_ns()}.bundle"
    return ArmSpec("candidate", sha, sha, sha,
                   ("tests/test_one.py",), ("tests/test_one.py", "-v"),
                   request_id, "box-a", False, bundle_path=bundle_path,
                   snapshot_root=root, snapshot_ref="HEAD")


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
                     command={"argv": [sys.executable, "-c", code],
                              "env": _source_bundle_env(record), "timeout": 10})


def cancel_admitted(node, record):
    owner = {name: record.get(name) for name in
             ("owner_host", "owner_pid", "owner_start_token", "owner_identity")}
    identity = record.get("remote_run")
    if identity is None:
        return node.call("release", **key(record), expected_owner=owner)
    return node.call("cancel", **key(record), identity=identity, expected_owner=owner)


def test_config_validation_rejects_ad_hoc_managed_run_directory(tmp_path: Path) -> None:
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["managed_run_directory"] = "/tmp/remote-ci-runs"
    path = tmp_path / "bad-managed-root.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Exception, match="managed_run_directory must not be under /tmp"):
        load_config(path)


@pytest.mark.parametrize(("length", "valid"), [(64, True), (65, False)])
def test_config_repo_id_fits_node_namespace(tmp_path: Path, length: int, valid: bool) -> None:
    raw = _raw_config(tmp_path)
    raw["repo"] = "p" * length
    raw["boxes"]["box-a"]["managed_run_directory"] = str(
        ROOT / "tests" / "fixtures" / "remote_ci" / "managed"
    )
    path = tmp_path / "repo-id-length.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    if valid:
        assert load_config(path).repo == "p" * length
    else:
        with pytest.raises(ci.ConfigError, match="repo.*64"):
            load_config(path)
        import goalflight_remote_ci_node as node
        with pytest.raises(ValueError, match="repo.*64"):
            node.safe_repo("p" * length)


def test_wrapper_does_not_stamp_skill_project_for_another_git_repo(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    caller = tmp_path / "project-a"
    _init_repo(caller)
    (caller / ".goal-flight").mkdir()
    (caller / ".goal-flight" / "remote-ci.json").write_text(
        '{"repo": "project-a"}\n', encoding="utf-8",
    )
    (caller / "README").write_text("fixture\n", encoding="utf-8")
    _git(caller, "add", ".goal-flight/remote-ci.json", "README")
    _git(caller, "commit", "-m", "fixture")
    monkeypatch.chdir(caller)
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", raising=False)
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CPU_ROUTE", raising=False)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))

    raw = _raw_config(tmp_path / "skill-config")
    raw["repo"] = "goal-flight"
    raw["fallback"] = "fail"
    config_path = tmp_path / "skill-config" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    loaded_paths = []

    def load_config(path):
        loaded_paths.append(path)
        return config

    monkeypatch.setattr(ci, "_load_local_config", load_config)
    monkeypatch.setattr(ci, "daemon_is_running", lambda _config: False)

    result = ci.wrapper_main([
        "--cmd-id", "foreign-project-check", "--", sys.executable, "-c", "pass",
    ])

    output = capsys.readouterr()
    assert result == 2
    assert output.out == ""
    assert "--config <path>" in output.err
    assert "GOALFLIGHT_REMOTE_CI_CONFIG" in output.err
    assert not loaded_paths


def test_wrapper_config_defaults_to_skill_repository_only(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    skill_root = tmp_path / "skill-repository"
    _init_repo(skill_root)
    skill_scripts = skill_root / "scripts"
    skill_scripts.mkdir()
    skill_config = skill_root / ".goal-flight" / "remote-ci.json"
    skill_config.parent.mkdir()
    skill_config.write_text('{"repo": "goal-flight"}\n', encoding="utf-8")
    monkeypatch.setattr(ci, "__file__", str(skill_scripts / "goalflight_remote_ci.py"))

    default_path, caller_repo = ci._resolve_wrapper_config(None, skill_root, skill_root)

    assert default_path == skill_config
    assert caller_repo == "goal-flight"
    with pytest.raises(ci.ConfigError, match="--config.*GOALFLIGHT_REMOTE_CI_CONFIG"):
        ci._resolve_wrapper_config(None, tmp_path, None)

    unrelated = tmp_path / "goal-flight"
    _init_repo(unrelated)
    (unrelated / "README").write_text("fixture\n", encoding="utf-8")
    _git(unrelated, "add", "README")
    _git(unrelated, "commit", "-m", "fixture")
    with pytest.raises(ci.ConfigError, match="--config.*GOALFLIGHT_REMOTE_CI_CONFIG"):
        ci._resolve_wrapper_config(None, unrelated, unrelated)


@pytest.mark.parametrize("via_environment", [False, True])
def test_wrapper_explicit_config_sources_are_preserved(
    tmp_path: Path, monkeypatch, via_environment: bool,
) -> None:
    caller = tmp_path / "project-a"
    _init_repo(caller)
    (caller / "README").write_text("fixture\n", encoding="utf-8")
    _git(caller, "add", "README")
    _git(caller, "commit", "-m", "fixture")
    monkeypatch.chdir(caller)
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", raising=False)
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CPU_ROUTE", "local")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))

    raw = _raw_config(tmp_path / "explicit-config")
    raw["repo"] = "project-a"
    config_path = tmp_path / "explicit-config" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    loaded_paths = []

    def load_config(path):
        loaded_paths.append(path)
        return config

    monkeypatch.setattr(ci, "_load_local_config", load_config)
    monkeypatch.setattr(
        ci, "_run_local_command",
        lambda *_args, **_kwargs: (0, {}),
    )
    args = ["--cmd-id", "explicit-config-control", "--", sys.executable, "-c", "pass"]
    if via_environment:
        monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_CONFIG", str(config_path))
    else:
        args = ["--config", str(config_path), *args]

    assert ci.wrapper_main(args) == 0
    assert loaded_paths == [config_path]


def test_wrapper_refuses_config_whose_repo_differs_from_caller(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    caller = tmp_path / "project-a"
    _init_repo(caller)
    (caller / ".goal-flight").mkdir()
    (caller / ".goal-flight" / "remote-ci.json").write_text(
        '{"repo": "project-a"}\n', encoding="utf-8",
    )
    (caller / "README").write_text("fixture\n", encoding="utf-8")
    _git(caller, "add", ".goal-flight/remote-ci.json", "README")
    _git(caller, "commit", "-m", "fixture")
    monkeypatch.chdir(caller)
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", raising=False)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))

    raw = _raw_config(tmp_path / "mismatched-config")
    raw["repo"] = "goal-flight"
    config_path = tmp_path / "mismatched-config" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(config_path)
    monkeypatch.setattr(ci, "_load_local_config", lambda _path: config)

    result = ci.wrapper_main([
        "--config", str(config_path), "--cmd-id", "mismatch-check", "--",
        sys.executable, "-c", "pass",
    ])

    output = capsys.readouterr()
    assert result == 2
    assert output.out == ""
    assert "config repo 'goal-flight' does not match caller repository 'project-a'" in output.err
    assert "--config <path>" in output.err
    assert "GOALFLIGHT_REMOTE_CI_CONFIG" in output.err


def test_config_validation_rejects_unknown_selection_option(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request = {
        "schema": "goalflight.remote-ci.request.v1",
        "request_id": "request-1",
        "project_id": config.repo,
        "kind": "targeted",
        "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py", "--not-allowed"],
    }
    with pytest.raises(Exception, match="option is not allowed"):
        validate_request(request, config)


def test_command_request_defers_tree_metadata_to_daemon(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request = validate_request({
        "schema": ci.REQUEST_SCHEMA,
        "request_id": "command-1",
        "project_id": config.repo,
        "kind": "gate",
        "command_id": "full-gate",
        "command": ["./tests/run.sh"],
        "repo_root": str(tmp_path / "repo"),
        "cwd_relative": "tests",
        "caller_env": {"CI": "1"},
        "receipt_metadata": {
            "dispatch_id": "dispatch-ci-2", "controller_label": "beta",
        },
    }, config)
    assert request["repo_root"] == str((tmp_path / "repo").resolve())
    assert request["cwd_relative"] == "tests"
    assert request["receipt_metadata"] == {
        "dispatch_id": "dispatch-ci-2", "controller_label": "beta",
    }
    assert not {"head_sha", "snapshot_sha", "candidate_sha", "tip_sha", "bundle_path"} & request.keys()


def test_git_snapshot_timeout_fails_as_a_remote_ci_error(tmp_path: Path, monkeypatch) -> None:
    def expire(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(ci.subprocess, "run", expire)
    with pytest.raises(ci.RemoteCIError, match="git status exceeded 0.01s"):
        ci._git_output(tmp_path, ["status"], timeout=0.01)


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


def test_informational_failure_fields_do_not_change_gate_verdict() -> None:
    baseline = {
        "schema": ci.RECEIPT_SCHEMA, "hostname": "studio.example.invalid",
        "status": "fail", "returncode": 1, "passed": 2, "failed": 1,
        "errors": 0, "skipped": 0, "failing_node_ids": ["tests/test.py::test_x"],
        "collection_import_errors": [], "first_error_lines": [],
    }
    detailed = {
        **baseline,
        "failure_summary": {
            "failing_node_ids": ["tests/test.py::test_x"],
            "collection_import_errors": [],
        },
        "failure_excerpt": ["E   assertion failed"],
        "failure_summary_complete": True,
        "stdout_path": "/remote/run/stdout",
    }
    base = parse_receipt(FIXTURES / "receipt-pass.json")
    plain_verdict = matched_pair_verdict(
        ArmOutcome("base", "green", 0, base),
        ArmOutcome("candidate", "red", 1, parse_receipt(baseline)),
    )
    detailed_verdict = matched_pair_verdict(
        ArmOutcome("base", "green", 0, base),
        ArmOutcome("candidate", "red", 1, parse_receipt(detailed)),
    )
    assert parse_receipt(detailed).to_dict()["failure_summary"] == detailed["failure_summary"]
    decision_fields = (
        "status", "base_red", "candidate_red",
        "new_failure_node_ids", "fixed_failure_node_ids",
    )
    assert tuple(detailed_verdict[field] for field in decision_fields) == tuple(
        plain_verdict[field] for field in decision_fields
    )
    assert ci._remote_returncode(detailed_verdict) == ci._remote_returncode(plain_verdict)


def test_red_pair_exit_uses_nonzero_base_status_or_one_for_receipt_red() -> None:
    assert ci._remote_returncode({
        "status": "RED", "base": {"returncode": 9},
        "candidate": {"returncode": 0},
    }) == 9
    assert ci._remote_returncode({
        "status": "RED", "base": {"returncode": 0},
        "candidate": {"returncode": 0},
    }) == 1


def test_remote_returncode_reconciles_receipts_with_green_status() -> None:
    receipt = {
        "schema": ci.RECEIPT_SCHEMA,
        "hostname": "studio.example.invalid",
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
    green = {
        "status": "GREEN",
        "candidate": {"status": "green", "returncode": 0, "receipt": receipt},
    }
    red_receipt = {
        **receipt,
        "passed": 0,
        "failed": 1,
        "failing_node_ids": ["tests/test_one.py::test_red"],
    }
    false_green = {
        "status": "GREEN",
        "candidate": {"status": "green", "returncode": 0, "receipt": red_receipt},
    }
    no_nodes = {
        "status": "GREEN",
        "candidate": {
            "status": "green", "returncode": 0,
            "receipt": {**receipt, "passed": 0},
        },
    }

    assert ci._remote_returncode(green) == 0
    assert ci._remote_returncode(false_green) == 1
    assert ci._remote_returncode(no_nodes) == 1
    assert ci._zero_test_node_arms(no_nodes) == ["candidate"]


def test_receipt_parser_uses_measured_hostname_and_rejects_alias_only() -> None:
    receipt = parse_receipt(FIXTURES / "receipt-pass.json")
    assert receipt.hostname == "ci-worker.example.invalid"
    assert receipt.duration_seconds == 18.4
    assert "dispatch_id" not in receipt.to_dict()
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
                "project_id": config.repo,
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


def test_daemon_snapshots_complete_worktree_without_touching_worker_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".gitignore").write_text("tests/ignored.py\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/test_changed.py").write_text("BASE = 1\n", encoding="utf-8")
    (repo / "tests/test_removed.py").write_text("REMOVE = 1\n", encoding="utf-8")
    (repo / "src").mkdir()
    executable = repo / "src/run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repo / "src/old.txt").write_text("rename me\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "config", "core.filemode", "false")
    index_before = (repo / ".git/index").read_bytes()

    (repo / "tests/test_changed.py").write_text("CANDIDATE = 2\n", encoding="utf-8")
    (repo / "tests/test_removed.py").unlink()
    (repo / "tests/test_added.py").write_text("ADDED = 3\n", encoding="utf-8")
    (repo / "tests/ignored.py").write_text("IGNORED = 4\n", encoding="utf-8")
    executable.chmod(0o755)
    (repo / "src/old.txt").rename(repo / "src/new.txt")
    (repo / "src/link").symlink_to("new.txt")
    whitespace_name = repo / "src/trailing "
    whitespace_name.write_text("preserve filename\n", encoding="utf-8")

    daemon_root = tmp_path / "daemon"
    daemon_root.mkdir()
    config = _config(daemon_root)
    config.queue_dir.mkdir(parents=True)
    daemon = GateDaemon(config)
    request = {
        "request_id": "snapshot-1", "command_id": "full-gate",
        "command": ["./tests/run.sh"], "kind": "gate",
        "repo_root": str(repo), "cwd_relative": "",
        "caller_env": {"CI": "1"},
    }
    request, snapshot = daemon._snapshot_command_request(request)
    try:
        assert request["head_sha"] == base_sha
        assert request["snapshot_sha"] != base_sha
        assert request["dirty"] is True
        assert (repo / ".git/index").read_bytes() == index_before
        assert _git(repo, "rev-parse", snapshot["ref"]) == snapshot["snapshot_sha"]
        bundle = ci._create_snapshot_bundle(
            snapshot["root"], snapshot["ref"], snapshot["bundle_path"],
            snapshot["snapshot_sha"], None,
        )
        assert subprocess.run(
            ["git", "-C", str(repo), "bundle", "verify", str(bundle)],
            capture_output=True, text=True, check=False,
        ).returncode == 0
        entries = _git(repo, "ls-tree", "-r", "-z", snapshot["snapshot_sha"]).split("\0")
        modes = {entry.partition("\t")[2]: entry.split(" ", 1)[0]
                 for entry in entries if entry}
        assert "tests/ignored.py" not in modes
        assert "src/trailing " in modes
        assert modes["src/run.sh"] == "100755"
        assert modes["src/link"] == "120000"
        assert "src/old.txt" not in modes and "src/new.txt" in modes
        assert "tests/test_removed.py" not in modes
        assert _git(repo, "show", f"{snapshot['snapshot_sha']}:tests/test_changed.py") == "CANDIDATE = 2"
    finally:
        ci._release_snapshot(Path(snapshot["root"]), snapshot["ref"],
                             snapshot["snapshot_sha"], snapshot["bundle_path"])


def test_daemon_snapshot_includes_staged_only_new_file(tmp_path: Path) -> None:
    repo = tmp_path / "staged-only-repo"
    _init_repo(repo)
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "base")
    new_file = repo / "tests" / "test_staged.py"
    new_file.parent.mkdir()
    new_file.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "tests/test_staged.py")
    index_before = (repo / ".git" / "index").read_bytes()

    daemon_root = tmp_path / "daemon"
    daemon_root.mkdir()
    config = _config(daemon_root)
    config.queue_dir.mkdir(parents=True)
    snapshot = ci._snapshot_worktree(
        repo, "staged-only", config.queue_dir, cwd_relative="", caller_env={},
    )
    try:
        paths = _git(
            repo, "ls-tree", "-r", "--name-only", snapshot["snapshot_sha"],
        ).splitlines()
        assert "tests/test_staged.py" in paths
        assert snapshot["dirty"] is True
        assert (repo / ".git" / "index").read_bytes() == index_before
    finally:
        ci._release_snapshot(
            Path(snapshot["root"]), snapshot["ref"],
            snapshot["snapshot_sha"], snapshot["bundle_path"],
        )


def test_daemon_snapshot_excludes_untracked_nested_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README").write_text("repo\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "base")

    nested = repo / "deps" / "nested"
    _init_repo(nested)
    (nested / "source.txt").write_text("nested\n", encoding="utf-8")
    _git(nested, "add", "-A")
    _git(nested, "commit", "-m", "nested")
    assert ci._unsupported_snapshot_reason(repo) is None

    config_root = tmp_path / "config"
    config_root.mkdir()
    config = _config(config_root)
    config.queue_dir.mkdir(parents=True)
    snapshot = ci._snapshot_worktree(
        repo, "nested-checkout", config.queue_dir,
        cwd_relative="", caller_env={},
    )
    try:
        entries = _git(repo, "ls-tree", "-r", "-z", snapshot["snapshot_sha"]).split("\0")
        paths = {entry.partition("\t")[2] for entry in entries if entry}
        assert "README" in paths
        assert not any(path == "deps/nested" or path.startswith("deps/nested/")
                       for path in paths)
        assert not any(entry.startswith("160000 ") for entry in entries if entry)
    finally:
        ci._release_snapshot(Path(snapshot["root"]), snapshot["ref"],
                             snapshot["snapshot_sha"], snapshot["bundle_path"])


@pytest.mark.parametrize(("kind", "expected"), [
    ("lfs", "Git LFS"),
    ("submodule", "submodules"),
    ("absolute-symlink", "absolute symlink"),
])
def test_daemon_falls_back_for_unsupported_snapshot_content(tmp_path: Path, kind, expected) -> None:
    repo = tmp_path / kind
    _init_repo(repo)
    (repo / "README").write_text("repo\n", encoding="utf-8")
    if kind == "lfs":
        (repo / ".gitattributes").write_text(
            "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
        (repo / "large.bin").write_text(
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:" + "a" * 64 + "\nsize 5\n", encoding="utf-8")
    elif kind == "absolute-symlink":
        (repo / "absolute-link").symlink_to(str(repo / "target"))
        _git(repo, "add", "-A")
    else:
        _git(repo, "add", "README")
        _git(repo, "update-index", "--add", "--cacheinfo",
             "160000," + "a" * 40 + ",deps/submodule")
    if kind == "lfs":
        _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")

    config_root = tmp_path / "config"
    config_root.mkdir()
    config = _config(config_root)
    config.queue_dir.mkdir(parents=True)
    request_path = config.queue_dir / f"{kind}.json"
    request_path.write_text(json.dumps({
        "schema": ci.REQUEST_SCHEMA,
        "request_id": kind,
        "project_id": config.repo,
        "kind": "gate",
        "command_id": "full-gate",
        "command": ["./tests/run.sh"],
        "repo_root": str(repo),
        "cwd_relative": "",
        "caller_env": {},
    }), encoding="utf-8")
    result = GateDaemon(config)._process_request(request_path)
    assert result["status"] == "LOCAL_FALLBACK_REQUIRED"
    assert expected in result["error"]


def test_candidate_test_overlay_applies_to_a_base_checkout(tmp_path: Path) -> None:
    import goalflight_remote_ci_node as node

    source = tmp_path / "source"
    _init_repo(source)
    tests_dir = source / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_changed.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    (tests_dir / "test_removed.py").write_text("OLD = True\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "base")
    base_sha = _git(source, "rev-parse", "HEAD")

    (tests_dir / "test_changed.py").write_text("VALUE = 'candidate'\n", encoding="utf-8")
    (tests_dir / "test_removed.py").unlink()
    (tests_dir / "test_added.py").write_text("NEW = True\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "candidate")
    candidate_sha = _git(source, "rev-parse", "HEAD")

    slot = tmp_path / "slot"
    subprocess.run(["git", "clone", "--no-hardlinks", str(source), str(slot)],
                   capture_output=True, text=True, check=True)
    _git(slot, "checkout", "--detach", base_sha)
    assert (slot / "tests/test_changed.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"
    assert (slot / "tests/test_removed.py").is_file()

    assert node.overlay_candidate_tests(
        slot, candidate_sha,
        ["tests/test_added.py", "tests/test_changed.py", "tests/test_removed.py"],
    )
    assert (slot / "tests/test_changed.py").read_text(encoding="utf-8") == "VALUE = 'candidate'\n"
    assert (slot / "tests/test_added.py").read_text(encoding="utf-8") == "NEW = True\n"
    assert not (slot / "tests/test_removed.py").exists()


def test_run_command_starts_driver_in_its_own_session() -> None:
    result = run_command(
        [sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"],
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "True"


@pytest.mark.parametrize(("leader_status", "identity_match"), [(None, False), (0, True)])
def test_run_command_reports_unknown_when_group_leader_is_not_signalable(
    monkeypatch, leader_status, identity_match
):
    class Stream:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class Process:
        pid = 4242

        def __init__(self):
            self.stdout = Stream()
            self.stderr = Stream()

        def poll(self):
            return leader_status

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(["fake"], timeout)

    process = Process()
    with monkeypatch.context() as patch:
        patch.setattr(ci.subprocess, "Popen", lambda *args, **kwargs: process)
        patch.setattr(ci.goalflight_compat, "process_start_identity",
                      lambda pid: {"pid": pid, "start_token": "old"})
        patch.setattr(ci.goalflight_compat, "process_identity_matches",
                      lambda *_: identity_match)
        patch.setattr(ci.os, "getpgid", lambda pid: 4242)
        patch.setattr(ci.os, "killpg",
                      lambda *args: pytest.fail("reused group was signalled"))
        result = run_command(["fake"], timeout=0.1)
    assert result.unknown is True
    assert result.timed_out is True
    assert process.stdout.closed and process.stderr.closed


def test_controller_owner_requires_its_process_start_token(tmp_path, monkeypatch):
    owner = RemoteRunner._owner()
    assert owner["owner_pid"] == os.getpid()
    assert owner["owner_start_token"]
    runner = RemoteRunner(_config(tmp_path), executor=lambda argv, env, timeout: None)
    monkeypatch.setattr(ci.goalflight_compat, "process_identity_matches", lambda *_: None)
    assert runner._owner_alive(owner) is None
    assert runner._owns(owner) is False
    monkeypatch.setattr(ci.goalflight_compat, "process_identity_matches", lambda *_: False)
    assert runner._owner_alive(owner) is False


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


def test_command_override_config_is_validated_without_echoing_values(tmp_path, capsys):
    raw = _raw_config(tmp_path)
    raw["runner"]["command_overrides"] = {
        "example-command-isolated": {
            "env_set": {"PYTHONPATH": "src", "TOKEN": "secret-value"},
            "env_unset": ["EXAMPLE_DEP_ROOT", "EXAMPLE_DEP_EXTRACT_ROOT"],
        }
    }
    config = DaemonConfig.from_mapping(raw, path=tmp_path / "config.json")
    override = config.runner.command_overrides["example-command-isolated"]
    assert override.env_set == {"PYTHONPATH": "src", "TOKEN": "secret-value"}
    assert override.env_unset == ("EXAMPLE_DEP_ROOT", "EXAMPLE_DEP_EXTRACT_ROOT")
    raw["runner"]["command_overrides"]["example-command-isolated"]["profile"] = False
    with pytest.raises(ci.ConfigError, match="unknown keys") as error:
        DaemonConfig.from_mapping(raw, path=tmp_path / "config.json")
    assert "secret-value" not in str(error.value)
    assert capsys.readouterr().out == ""


def test_command_override_rejects_invalid_environment_names(tmp_path):
    raw = _raw_config(tmp_path)
    raw["runner"]["command_overrides"] = {
        "gate": {"env_set": {"BAD-NAME": "do-not-print"}}
    }
    with pytest.raises(ci.ConfigError, match="environment names") as error:
        DaemonConfig.from_mapping(raw, path=tmp_path / "config.json")
    assert "do-not-print" not in str(error.value)


def test_local_env_must_cover_box_environment_keys_across_boxes(tmp_path):
    raw = _raw_config(tmp_path)
    raw["boxes"]["box-a"]["env"] = {"PATH": "/remote/bin", "PYTHONPATH": "/remote/python"}
    raw["boxes"]["box-b"] = {
        **raw["boxes"]["box-a"],
        "host": "second-ci-worker.example.invalid",
        "expected_hostname": "second-measured-node",
        "env": {"EXAMPLE_DEP_ROOT": "/remote/pm2"},
    }
    raw["local_env"] = {"PATH": None, "PYTHONPATH": "local-python"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ci.ConfigError, match=r"profile-env-unavailable-locally.*EXAMPLE_DEP_ROOT"):
        _load_fake_node_config(path)

    raw["local_env"]["EXAMPLE_DEP_ROOT"] = "/local/pm2"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    assert config.local_env["PATH"] is None


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
    cancel_admitted(node, first)
    wait_state(node, older, {"admitted"})
    assert other.call("status", **key(younger))["state"] == "queued"
    cancel_admitted(node, older)
    younger_admitted = wait_state(other, younger, {"admitted"})
    cancel_admitted(other, younger_admitted)
    wait_state(other, younger_admitted, {"released"})


def test_unsafe_load_and_live_caps_do_not_admit(node_env):
    config, executor, runner, node = node_env
    executor.load1 = 21
    executor.cpu_busy_fraction = 0.0
    load_only = enqueue(node, "load-only-high")
    assert wait_state(node, load_only, {"admitted"})["sample"]["cpu_busy_fraction"] == 0.0
    cancel_admitted(node, load_only)
    wait_state(node, load_only, {"released"})

    executor.cpu_busy_fraction = 0.99
    executor.load1 = 3
    cpu_only = enqueue(node, "cpu-only-high")
    assert wait_state(node, cpu_only, {"admitted"})["sample"]["cpu_busy_fraction"] == 0.99
    cancel_admitted(node, cpu_only)
    wait_state(node, cpu_only, {"released"})

    executor.load1 = 41
    saturated = enqueue(node, "both-signals-high")
    time.sleep(0.05)
    assert node.call("status", **key(saturated))["state"] == "queued"
    node.call("release", **key(saturated))
    wait_state(node, saturated, {"released"})

    (executor.root / "admission" / "caps.json").write_text(json.dumps({"p_cores": 2}))
    limited = enqueue(node, "limited")
    time.sleep(0.05)
    assert node.call("status", **key(limited))["state"] == "queued"
    node.call("release", **key(limited))
    wait_state(node, limited, {"released"})
    (executor.root / "admission" / "caps.json").write_text(json.dumps({"p_cores": 4}))
    executor.load1 = 3
    executor.cpu_busy_fraction = 0.99
    admitted = enqueue(node, "admitted-with-headroom")
    assert wait_state(node, admitted, {"admitted"})["sample"]["load1"] == 3
    cancel_admitted(node, admitted)
    wait_state(node, admitted, {"released"})


@pytest.mark.parametrize(
    ("load1", "busy", "should_admit"),
    [
        (55.999, 0.95, True),  # Just below 2x 28 physical cores.
        (56, 0.949, True),     # Busy just below the 95% threshold.
        (56, 0.95, False),     # Both thresholds are inclusive.
        (40, 0.93, True),      # High load still has CPU headroom.
    ],
)
def test_node_saturation_threshold_boundaries(node_env, load1, busy, should_admit):
    config, executor, _, _ = node_env
    box = replace(config.boxes["box-a"], p_cores=28)
    config = replace(config, boxes={**config.boxes, "box-a": box})
    node = RemoteRunner(config, executor=executor).nodes["box-a"]
    executor.load1 = load1
    executor.cpu_busy_fraction = busy
    queued = enqueue(node, f"saturation-{load1}-{busy}")
    policy = json.loads((executor.root / "admission" / "policy.json").read_text())
    assert set(policy) == {"p_cores", "token_pool_size"}

    if should_admit:
        admitted = wait_state(node, queued, {"admitted"})
        cancel_admitted(node, admitted)
        wait_state(node, admitted, {"released"})
    else:
        time.sleep(0.05)
        assert node.call("status", **key(queued))["state"] == "queued"
        node.call("release", **key(queued))
        wait_state(node, queued, {"released"})


def test_client_and_node_saturation_constants_match():
    import goalflight_remote_ci_node as node

    assert ci.SATURATION_LOAD_FACTOR == node.SATURATION_LOAD_FACTOR == 2
    assert ci.SATURATION_BUSY_FRACTION == node.SATURATION_BUSY_FRACTION == 0.95


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_run_persists_identity_before_start_and_receipt_is_measured(node_env):
    config, executor, runner, node = node_env
    caller_env = {"TEST_WORKERS": "4"}
    receipt_metadata = {
        "dispatch_id": "dispatch-ci-1",
        "controller_label": "alpha-main",
    }
    receipt = (FIXTURES / "receipt-pass.json").read_text().replace("ci-worker.example.invalid", "measured-node")
    config = replace(config, runner=replace(config.runner, command=(
        sys.executable, "-c", "import os,json; r=json.loads(os.environ['GOALFLIGHT_REMOTE_CI_LEASE_RECORD_JSON']);"
        "assert r['remote_run']['pid']; assert os.environ['TEST_WORKERS']=='4'; "
        "assert 'GOALFLIGHT_DISPATCH_ID' not in os.environ; "
        "assert 'GOALFLIGHT_CONTROLLER_LABEL' not in os.environ; "
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
    outcome = runner.run_arm(replace(
        pool_spec(), caller_env=caller_env, receipt_metadata=receipt_metadata,
    ))
    assert outcome.status == "green"
    assert outcome.receipt.hostname == "measured-node"
    metadata = outcome.receipt.to_dict()
    assert metadata["dispatch_id"] == "dispatch-ci-1"
    assert metadata["controller_label"] == "alpha-main"
    assert len(metadata["command_sha"]) == 64
    parsed_receipt = parse_receipt(metadata)
    assert parsed_receipt.to_dict()["dispatch_id"] == "dispatch-ci-1"
    assert executor.calls.index("enqueue") < executor.calls.index("start")
    assert outcome.to_dict()["lease"]["lease_token"]


def test_post_enqueue_and_verification_status_use_full_transport_budget(
    tmp_path, monkeypatch,
):
    config = _green_config(_config(tmp_path, token_pool_size=1))
    config = replace(
        config, admission=replace(config.admission, max_wait_seconds=0.1),
    )
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    node = runner.nodes["box-a"]
    clock = [100.0]
    admission_deadline = 100.1
    monkeypatch.setattr(ci.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        runner, "choose_box", lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )

    receipt = (FIXTURES / "receipt-pass.json").read_text(encoding="utf-8").replace(
        "ci-worker.example.invalid", "measured-node",
    )
    run_dir = str(tmp_path / "remote-run")
    admitted_record = {
        "run_directory": run_dir,
        "lease_token": "lease-token",
        "lease_id": "lease-id",
        "measured_hostname": "measured-node",
        "destination_address": "192.0.2.10",
        "owner_identity": "owner-identity",
        "state": "admitted",
        "sample": {"hostname": "measured-node", "self_cap": 4},
        "slot": str(tmp_path / "slot"),
        "checkout_dir": str(tmp_path / "slot"),
        "token_index": "0",
    }
    queued_record = {
        "run_directory": run_dir,
        "lease_token": "lease-token",
        "lease_id": "lease-id",
        "measured_hostname": "measured-node",
        "destination_address": "192.0.2.10",
        "owner_identity": "owner-identity",
        "state": "queued",
    }
    started = False
    pre_start_statuses = 0
    operations = []
    enqueue_waits = []
    enqueue_transport_timeouts = []
    status_transport_timeouts = []

    def call(operation, **kwargs):
        nonlocal pre_start_statuses, started
        operations.append(operation)
        if operation == "enqueue":
            enqueue_waits.append(kwargs.get("queue_wait_seconds"))
            enqueue_transport_timeouts.append(kwargs.get("_transport_timeout"))
            clock[0] = admission_deadline + 1
            return dict(queued_record)
        if operation == "start":
            started = True
            clock[0] = admission_deadline + 1
            return {"status": "started"}
        if operation == "status":
            status_transport_timeouts.append(kwargs.get("_transport_timeout"))
            if not started:
                pre_start_statuses += 1
                return {**admitted_record, "result": None}
            return {
                "state": "released",
                "holder_alive": True,
                "measured_hostname": "measured-node",
                "destination_address": "192.0.2.10",
                "remote_run": {
                    "host": "measured-node",
                    "address": "192.0.2.10",
                    "pid": "123", "start_token": "token", "run_dir": run_dir,
                    "lease_id": "lease-id", "lease_token": "lease-token",
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
        if operation == "release":
            return {"status": "releasing"}
        raise AssertionError(f"unexpected remote operation: {operation}")

    monkeypatch.setattr(node, "call", call)

    result = ci.run_remote_command({
        "request_id": "long-status-verification",
        "box": "box-a",
        "candidate_sha": "b" * 40,
        "head_sha": "a" * 40,
        "snapshot_sha": "b" * 40,
        "dirty": False,
        "command_id": "heavy-gate",
        "command": [sys.executable, "-c", "pass"],
        "route_reason": "heavy-command-allowlist",
        "caller_env": {},
        "_admission_deadline": admission_deadline,
    }, runner)

    assert clock[0] > admission_deadline
    assert started
    assert result["status"] == "GREEN"
    assert result["candidate"]["status"] == "green"
    assert enqueue_waits == [pytest.approx(config.admission.max_wait_seconds)]
    assert enqueue_transport_timeouts == [pytest.approx(
        enqueue_waits[0] + ci.REMOTE_TRANSPORT_ALLOWANCE_SECONDS,
    )]
    assert enqueue_transport_timeouts[0] > enqueue_waits[0]
    assert status_transport_timeouts == [
        ci.REMOTE_STATUS_TIMEOUT_SECONDS,
        ci.REMOTE_STATUS_TIMEOUT_SECONDS,
    ]
    assert operations[:3] == ["enqueue", "status", "start"]
    assert "release" not in operations


def test_late_enqueue_withdraws_when_deadline_status_is_still_queued(tmp_path, monkeypatch):
    config = _green_config(_config(tmp_path, token_pool_size=1))
    config = replace(
        config, admission=replace(config.admission, max_wait_seconds=0.1),
    )
    runner = RemoteRunner(config, executor=lambda *_args, **_kwargs: None)
    node = runner.nodes["box-a"]
    clock = [100.0]
    admission_deadline = 100.1
    monkeypatch.setattr(ci.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        runner, "choose_box", lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )
    run_dir = str(tmp_path / "remote-run")
    queued_record = {
        "run_directory": run_dir,
        "lease_token": "lease-token",
        "lease_id": "lease-id",
        "measured_hostname": "measured-node",
        "destination_address": "192.0.2.10",
        "owner_identity": "owner-identity",
        "state": "queued",
    }
    operations = []

    def call(operation, **_kwargs):
        operations.append(operation)
        if operation == "enqueue":
            clock[0] = admission_deadline + 1
            return dict(queued_record)
        if operation == "status":
            return {**queued_record, "result": None}
        if operation == "release":
            return {"status": "releasing"}
        raise AssertionError(f"unexpected remote operation: {operation}")

    monkeypatch.setattr(node, "call", call)
    outcome = runner.run_arm(spec("late-queued"), admission_deadline=admission_deadline)

    assert outcome.status == "capacity"
    assert operations[:3] == ["enqueue", "status", "release"]
    assert "start" not in operations


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_crash_reaper_reads_node_identity_without_local_running_record(node_env, monkeypatch):
    config, executor, runner, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    start(node, held)
    running = wait_state(node, held, {"running"})
    assert not (config.state_dir / "runs").exists()
    monkeypatch.setattr(ci.goalflight_compat, "process_identity_matches", lambda *_: False)
    results = runner.reap()
    assert results[0]["status"] == "cancelled"
    final = node.call("status", **key(held))
    assert not final["holder_alive"]
    assert final["remote_run"] == running["remote_run"]
    assert node.call("health")["tokens"]["in_use"] == 0


@pytest.mark.parametrize("unknown", ["start_token", "pid", "run_dir"])
def test_cancel_refuses_unproven_identity(node_env, unknown):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    identity = dict(held["remote_run"], **{unknown: "wrong"})
    with pytest.raises(ci.RemoteCIError, match="identity"):
        node.call("cancel", **key(held), identity=identity,
                  expected_owner=RemoteRunner._owner())
    assert node.call("health")["tokens"]["in_use"] == 1


# Round 3 removed reattach and request dedupe: an ambiguous outcome is rerun
# locally, so reattach-only ownership-transfer cases no longer apply. Cancel
# still requires the current owner, covered by test_cancel_requires_expected_owner.
def test_reaper_retains_unknown_owner(node_env, monkeypatch):
    _, executor, runner, node = node_env
    wait_state(node, enqueue(node), {"admitted"})
    monkeypatch.setattr(ci.goalflight_compat, "process_identity_matches", lambda *_: None)
    assert runner.reap()[0]["status"] == "unknown"
    assert "cancel" not in executor.calls


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_receipt_with_alias_instead_of_measured_hostname_is_red(node_env):
    config, executor, _, _ = node_env
    encoded = base64.b64encode((FIXTURES / "receipt-pass.json").read_bytes()).decode()
    config = replace(config, runner=replace(config.runner, command=(
        sys.executable, "-c", "import base64;print(base64.b64decode(" + repr(encoded) + ").decode())")))
    outcome = RemoteRunner(config, executor=executor).run_arm(pool_spec())
    # A hostname mismatch is an unverified remote run, so the daemon falls
    # back locally and never treats the receipt as a remote test verdict.
    assert outcome.status == "setup-failed"
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


def test_box_choice_uses_headroom_then_spreads_ties(tmp_path, monkeypatch):
    raw = _raw_config(tmp_path)
    second = dict(raw["boxes"]["box-a"])
    second.update(host="worker-b.example.invalid", expected_hostname="measured-b",
                  managed_run_directory=str(tmp_path / "node-b"))
    raw["boxes"]["box-b"] = second
    path = tmp_path / "two-boxes.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    samples = {
        "box-a": {"hostname": "measured-node", "load1": 15, "cpu_busy_fraction": 0.8},
        "box-b": {"hostname": "measured-b", "load1": 2, "cpu_busy_fraction": 0.2},
    }

    def health(node, operation, **kwargs):
        assert operation == "health"
        return {"load": samples[node.box.name], "tokens": {"free": 28}}

    monkeypatch.setattr(ci.RemoteNode, "call", health)
    runner = RemoteRunner(config, executor=lambda *args, **kwargs: None)
    assert runner.choose_box() == "box-b"
    samples["box-a"].update(load1=2, cpu_busy_fraction=0.2)
    assert runner.choose_box() == "box-a"
    assert runner.choose_box() == "box-b"


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_lost_launch_response_requires_local_fallback_and_cancels_best_effort(node_env):
    """An uncertain start has no remote verdict; do not reattach and reuse it."""
    config, executor, _, node = node_env
    config = replace(config, runner=replace(config.runner,
        command=(sys.executable, "-c", "import time;time.sleep(.3)")))
    def losing_executor(argv, env, timeout, input_data=None):
        result = executor(argv, env, timeout, input_data=input_data)
        if input_data is None and executor.calls and executor.calls[-1] == "start":
            return CommandResult(2, stderr="lost response")
        return result
    runner = RemoteRunner(config, executor=losing_executor)
    try:
        outcome = runner.run_arm(pool_spec())
    except ci.AmbiguousRemoteError as exc:
        assert "no clean verified outcome" in str(exc)
    else:
        pytest.fail(
            "expected an ambiguous start; "
            f"got {outcome.status}: {outcome.error}; calls={executor.calls}"
        )
    assert executor.calls.count("start") == 1
    assert "cancel" in executor.calls
    deadline = time.monotonic() + 5
    while node.call("health")["tokens"]["in_use"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert node.call("health")["tokens"]["in_use"] == 0


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
def test_duplicate_start_cannot_execute_twice(node_env):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    start(node, held)
    with pytest.raises(ci.RemoteCIError, match="already started|does not hold admission"):
        start(node, held)


def test_release_on_admitted_lease_withdraws_before_start(node_env):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    released = node.call("release", **key(held))
    assert released == {"status": "releasing"}
    assert (Path(held["run_directory"]) / "release.json").exists()
    assert not (Path(held["run_directory"]) / "command.json").exists()
    assert wait_state(node, held, {"released"})["state"] == "released"


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_node_runs_command_in_forwarded_working_directory(node_env):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    cwd = Path(held["slot"]) / "nested"
    node.call("start", **key(held), command={
        "argv": [sys.executable, "-c",
                 "import os;assert os.path.isfile('included.txt');print(os.getcwd())"],
        "env": _source_bundle_env(held), "timeout": 10, "cwd": str(cwd),
    })
    deadline = time.monotonic() + 10
    current = None
    while time.monotonic() < deadline:
        current = node.call("status", **key(held))
        if current.get("result") is not None:
            break
        time.sleep(0.02)
    assert current is not None and current.get("result") is not None
    assert current["result"]["status"] == "completed"
    assert current["result"]["streams"]["stdout"]["tail"]["text"].strip().startswith(str(cwd))


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_command_override_env_is_applied_on_node(node_env):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    command = {
        "argv": [sys.executable, "-c",
                 "import os;print(os.getenv('PYTHONPATH'));"
                 "print(os.getenv('EXAMPLE_DEP_ROOT', 'absent'));"
                 "print(os.getenv('EXAMPLE_DEP_EXTRACT_ROOT', 'absent'))"],
        "env": {
            **_source_bundle_env(held),
            "PYTHONPATH": "box-path",
            "EXAMPLE_DEP_ROOT": "/box/pm2",
            "EXAMPLE_DEP_EXTRACT_ROOT": "/box/extract",
            "GOALFLIGHT_REMOTE_CI_PROFILE_ENABLED": "0",
            "GOALFLIGHT_REMOTE_CI_COMMAND_OVERRIDE_ID": "example-command-isolated",
        },
        "env_set": {"PYTHONPATH": "src"},
        "env_unset": ["EXAMPLE_DEP_ROOT", "EXAMPLE_DEP_EXTRACT_ROOT"],
        "timeout": 10,
    }
    node.call("start", **key(held), command=command)
    deadline = time.monotonic() + 10
    current = None
    while time.monotonic() < deadline:
        current = node.call("status", **key(held))
        if current.get("result") is not None:
            break
        time.sleep(0.02)
    assert current is not None and current.get("result") is not None
    assert current["result"]["status"] == "completed"
    stdout = current["result"]["streams"]["stdout"]["tail"]["text"]
    assert stdout.splitlines()[:3] == ["src", "absent", "absent"]


def test_runner_routes_command_override_by_id_and_receipt(node_env, monkeypatch):
    config, executor, _, _ = node_env
    override_id = "example-command-isolated"
    command_override = ci.CommandOverride(
        {"PYTHONPATH": "src"},
        ("EXAMPLE_DEP_ROOT", "EXAMPLE_DEP_EXTRACT_ROOT"),
    )
    runner_config = replace(
        config.runner,
        command_overrides={override_id: command_override},
    )
    config = replace(config, runner=runner_config, profile_enabled=True)
    monkeypatch.setattr(ci, "_profile_command_files", lambda *_args, **_kwargs: ())
    seen = {}

    def capture_start(payload):
        if payload["operation"] == "start":
            seen.update(payload["command"])

    executor.before = capture_start
    runner = RemoteRunner(config, executor=executor)
    monkeypatch.setattr(
        runner, "choose_box", lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )
    node = runner.nodes["box-a"]
    original_call = node.call
    monkeypatch.setattr(
        node, "call",
        lambda operation, *args, **kwargs: (
            seen.update(kwargs["command"]) or {}
            if operation == "start" else original_call(operation, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(
        runner, "_watch",
        lambda spec, *_args: ci.ArmOutcome(
            spec.arm, "completed", 0,
            ci.parse_receipt({
                "hostname": "measured-node", "status": "pass", "returncode": 0,
                "command_override_id": override_id,
            }),
        ),
    )
    run_spec = replace(
        pool_spec("override-run"),
        command=("python3", "-m", "pytest", "tests/test_one.py"),
        command_id=override_id,
    )
    outcome = runner.run_arm(run_spec)
    assert outcome.status == "completed", outcome.error
    assert seen["env"]["GOALFLIGHT_REMOTE_CI_PROFILE_ENABLED"] == "1"
    assert seen["env"]["GOALFLIGHT_REMOTE_CI_COMMAND_OVERRIDE_ID"] == override_id
    assert seen["env_set"] == {"PYTHONPATH": "src"}
    assert seen["env_unset"] == ["EXAMPLE_DEP_ROOT", "EXAMPLE_DEP_EXTRACT_ROOT"]
    assert "profile_isolated" not in seen
    assert seen["emit_receipt"] is True
    assert outcome.receipt is not None
    assert outcome.receipt.metadata["command_override_id"] == override_id

    ordinary = replace(pool_spec("ordinary-run"), command_id="ordinary-command")
    ordinary_config = replace(config, profile_enabled=False)
    ordinary_seen = {}
    executor.before = lambda payload: ordinary_seen.update(payload["command"]) \
        if payload["operation"] == "start" else None
    ordinary_runner = RemoteRunner(ordinary_config, executor=executor)
    monkeypatch.setattr(
        ordinary_runner, "choose_box",
        lambda requested=None, **_kwargs:
        ci.BoxPlacement(requested or "box-a", "measured-node", "192.0.2.10"),
    )
    ordinary_node = ordinary_runner.nodes["box-a"]
    ordinary_call = ordinary_node.call
    monkeypatch.setattr(
        ordinary_node, "call",
        lambda operation, *args, **kwargs: (
            ordinary_seen.update(kwargs["command"]) or {}
            if operation == "start" else ordinary_call(operation, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(
        ordinary_runner, "_watch",
        lambda spec, *_args: ci.ArmOutcome(spec.arm, "completed", 0, None),
    )
    ordinary_outcome = ordinary_runner.run_arm(ordinary)
    assert ordinary_outcome.status == "completed"
    assert "env_set" not in ordinary_seen
    assert "env_unset" not in ordinary_seen
    assert "GOALFLIGHT_REMOTE_CI_COMMAND_OVERRIDE_ID" not in ordinary_seen["env"]


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_timeout_cancels_node_group_and_frees_token(node_env):
    config, executor, runner, node = node_env
    config = replace(config, runner=replace(config.runner, timeout_seconds=.2,
        command=(sys.executable, "-c", "import time;time.sleep(20)")))
    result = RemoteRunner(config, executor=executor).run_arm(pool_spec())
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
    monkeypatch.setattr(ci.goalflight_compat, "process_identity_matches", lambda *_: False)
    assert corrected.reap()[0]["status"] == "cancelled"
    deadline = time.monotonic() + 5
    while node.call("health")["tokens"]["in_use"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert node.call("health")["tokens"]["in_use"] == 0


def test_base_overlay_selection_contract(node_env):
    config, _, _, _ = node_env
    # Pair specs require the daemon's box pin before either arm is constructed.
    request = {"request_id": "r", "box": "box-a",
               "tip_sha": "a"*40, "candidate_sha": "b"*40,
               "test_files": ["tests/test_one.py"], "selection": ["tests/test_one.py", "-q"],
               "receipt_metadata": {
                   "dispatch_id": "dispatch-ci-3", "controller_label": "beta",
               }}
    base, candidate = build_pair_specs(request, config)
    assert base.overlay and not candidate.overlay
    assert base.sha == request["tip_sha"]
    assert base.box == candidate.box == "box-a"
    assert base.test_files == candidate.test_files == ("tests/test_one.py",)
    assert "--continue-on-collection-errors" in base.selection
    assert base.receipt_metadata == candidate.receipt_metadata == request["receipt_metadata"]


def test_matched_pair_pins_both_arms_to_one_box(tmp_path):
    config = _config(tmp_path)
    config.queue_dir.mkdir(parents=True)
    queued = config.queue_dir / "pair-pin.json"
    queued.write_text("{}", encoding="utf-8")
    receipt = parse_receipt(FIXTURES / "receipt-pass.json")

    class Runner:
        def __init__(self):
            self.config = config
            self.boxes = []

        def choose_box(self, requested=None, *, excluding=(), deadline=None):
            assert not excluding
            assert deadline is not None
            return "box-a"

        def run_arm(self, arm, *, admission_deadline=None):
            assert admission_deadline is not None
            self.boxes.append(arm.box)
            return ArmOutcome(arm.arm, "green", 0, receipt)

    runner = Runner()
    result = ci.run_matched_pair({
        "request_id": "pair-pin", "project_id": config.repo, "tip_sha": "a" * 40,
        "candidate_sha": "b" * 40, "test_files": ["tests/test_one.py"],
        "selection": ["tests/test_one.py", "-q"],
    }, runner)
    assert result["status"] == "GREEN"
    assert runner.boxes == ["box-a", "box-a"]
    assert json.loads(queued.read_text())["box"] == "box-a"


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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_dropped_enqueue_response_does_not_stick_the_token(node_env):
    config, executor, _, node = node_env
    config = _green_config(config)

    def transport(argv, env, timeout, input_data=None):
        result = executor(argv, env, timeout, input_data=input_data)
        if input_data is None and executor.calls and executor.calls[-1] == "enqueue":
            return CommandResult(255, stdout=result.stdout, stderr="connection reset by peer")
        return result

    outcome = RemoteRunner(config, executor=transport).run_arm(pool_spec())
    assert outcome.status == "green", outcome.error
    _free_tokens(node)


def test_live_owner_reaps_admission_whose_release_never_landed(node_env):
    config, executor, _, node = node_env
    config = _green_config(config)
    node.call("health")  # Exercise lost calls after the first install handshake.
    drop = {"on": True}

    def transport(argv, env, timeout):
        payload = json.loads(base64.b64decode(shlex.split(argv[2])[3]))
        if drop["on"] and payload["operation"] in {"status", "release", "cancel"}:
            return CommandResult(255, stderr="no route to host")
        result = executor(argv, env, timeout)
        if drop["on"] and payload["operation"] == "enqueue":
            wait_state(node, json.loads(result.stdout)["result"], {"admitted"})
        return result

    runner = RemoteRunner(config, executor=transport)
    with pytest.raises(ci.RemoteCIError, match="no route to host"):
        runner.run_arm(pool_spec())
    assert node.call("health")["tokens"]["in_use"] == 1
    drop["on"] = False
    assert runner.reap()[0]["status"] == "releasing"
    _free_tokens(node)


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_next_arm_recovers_a_forgotten_token(node_env):
    config, executor, _, node = node_env
    config = _green_config(config)
    node.call("health")  # Exercise lost calls after the first install handshake.
    drop = {"on": True}

    def transport(argv, env, timeout, input_data=None):
        if input_data is not None:
            return executor(argv, env, timeout, input_data=input_data)
        payload = json.loads(base64.b64decode(shlex.split(argv[2])[3]))
        if drop["on"] and payload["operation"] in {"status", "release", "cancel"}:
            return CommandResult(255, stderr="no route to host")
        result = executor(argv, env, timeout, input_data=input_data)
        if drop["on"] and payload["operation"] == "enqueue":
            wait_state(node, json.loads(result.stdout)["result"], {"admitted"})
        return result

    sleeps = []

    def sleeper(seconds):
        sleeps.append(seconds)
        if seconds > 1:
            raise AssertionError(f"admission slept {seconds}s behind a forgotten token")

    runner = RemoteRunner(config, executor=transport, sleeper=sleeper)
    with pytest.raises(ci.RemoteCIError, match="no route to host"):
        runner.run_arm(pool_spec())
    assert node.call("health")["tokens"]["in_use"] == 1
    drop["on"] = False
    outcome = runner.run_arm(pool_spec("request-2"))
    assert outcome.status == "green", outcome.error
    assert sleeps and max(sleeps) <= 1
    _free_tokens(node)


def test_admitted_holder_releases_when_the_command_never_arrives(node_env):
    _, executor, _, node = node_env
    executor.use_short_command_window()
    forgotten = node.call(
        "enqueue", request_id="forgotten", arm="candidate",
        owner=RemoteRunner._owner(),
    )
    other = enqueue(node, "other")
    admitted = wait_state(node, other, {"admitted"})
    assert admitted["lease_id"] != forgotten["lease_id"]
    released = wait_state(node, forgotten, {"released"})
    assert released.get("release_reason") == "command-wait"
    assert released.get("result", {}).get("status") == "setup-failed"
    assert released["result"].get("command_started") is False


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_real_admitted_holder_survives_bundle_preparation_delay(node_env):
    _, executor, _, node = node_env
    executor.use_short_command_window(1.5)
    held = wait_state(node, enqueue(node, "bundle-delay"), {"admitted"})

    # Model local bundle creation and upload while the real node holder waits.
    time.sleep(1.0)
    start(node, held, "pass")
    released = wait_state(node, held, {"released"})
    assert released["result"]["status"] == "completed"
    assert released["result"]["command_started"] is True


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
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
                                           ("owner_host", "owner_pid", "owner_start_token",
                                            "owner_identity")})
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_admission_poll_does_not_park_for_the_queue_wait(tmp_path, non_tmp_managed_base):
    raw = _raw_config(tmp_path, token_pool_size=1)
    raw["boxes"]["box-a"]["managed_run_directory"] = str(non_tmp_managed_base / "fake-node")
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
            cancel_admitted(node, blocker)

    runner = RemoteRunner(config, executor=executor, sleeper=sleeper)
    node = runner.nodes["box-a"]
    try:
        blocker = wait_state(
            node,
            enqueue(node, "blocker", owner=dict(RemoteRunner._owner(), owner_pid=os.getpid() + 1)),
            {"admitted"},
        )
        outcome = runner.run_arm(pool_spec())
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
    config_a = _load_fake_node_config(path_a)
    raw["boxes"]["box-a"]["managed_run_directory"] = str(root_b)
    path_b = tmp_path / "b.json"
    path_b.write_text(json.dumps(raw), encoding="utf-8")
    config_b = _load_fake_node_config(path_b)
    executor = ScriptedExecutor(tmp_path / "box-authority")
    node_a = RemoteRunner(config_a, executor=executor).nodes["box-a"]
    node_b = RemoteRunner(config_b, executor=executor).nodes["box-a"]
    try:
        held = wait_state(node_a, enqueue(node_a), {"admitted"})
        with pytest.raises(ci.RemoteCIError, match="admission authority"):
            enqueue(node_b)
        assert node_a.call("health")["tokens"]["in_use"] == 1
        assert not (root_b / "admission").exists()
        assert not (root_b / "helpers").exists()
        assert str(root_a) in held["run_directory"]
    finally:
        executor.close(config_a)


def test_cancel_requires_expected_owner(node_env):
    _, _, _, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    with pytest.raises(ci.RemoteCIError, match="expected_owner"):
        node.call("cancel", **key(held), identity=held["remote_run"])
    assert node.call("health")["tokens"]["in_use"] == 1


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_reap_kills_orphaned_workload_after_its_deadline(node_env, tmp_path):
    _, _, runner, node = node_env
    held = wait_state(node, enqueue(node), {"admitted"})
    pidfile = tmp_path / "orphan.pid"
    node.call("start", **key(held), command={
        "argv": [sys.executable, "-c",
                 "import os,time\n"
                 f"open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
                 "time.sleep(30)\n"],
        "env": _source_bundle_env(held),
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
        lease["phase_deadline_epoch"] = time.time() - 1
        lease_path.write_text(json.dumps(lease), encoding="utf-8")
        assert runner.reap()[0]["status"] == "cancelled"
        _wait_dead(child, node, held)
        _free_tokens(node)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_released_run_bodies_are_pruned(node_env):
    _, executor, _, node = node_env
    finished = wait_state(node, enqueue(node, "done"), {"admitted"})
    start(node, finished, "import sys; sys.exit(0)")
    wait_state(node, finished, {"released"})
    dry = node.call("gc", apply=False, retain_max_count=0, retain_max_age_seconds=0,
                    retain_failure_count=0, retain_failure_age_seconds=0)
    assert not any(row.get("class") == "ELIGIBLE" and row["path"] == finished["run_directory"]
                   for row in dry)
    assert Path(finished["run_directory"]).exists()
    # The retention floor is measured from terminal completion (59ae8ea9),
    # so age the result row only after verifying a fresh completion is kept.
    result_index = executor.root / "results" / "index.jsonl"
    aged_rows = []
    for raw in result_index.read_text(encoding="utf-8").splitlines():
        row = json.loads(raw)
        if row.get("run_id") == finished["lease_id"]:
            row["finish"] = 0
        aged_rows.append(json.dumps(row))
    result_index.write_text("\n".join(aged_rows) + "\n", encoding="utf-8")
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
def test_checkout_directory_stays_inside_the_managed_root(node_env):
    config, executor, _, _ = node_env
    seen = {}

    def before(payload):
        if payload["operation"] == "start":
            seen["checkout"] = payload["command"]["env"]["GOALFLIGHT_REMOTE_CI_CHECKOUT_DIR"]
            seen["run_dir"] = payload["run_dir"]

    executor.before = before
    RemoteRunner(_green_config(config), executor=executor).run_arm(pool_spec())
    slot = seen["checkout"]
    assert Path(slot).is_dir()
    assert slot == str(Path(executor.root) / "repos" / "default" / "seats" / "s-01")
    assert str(executor.root) in slot


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
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
                                     ("owner_host", "owner_pid", "owner_start_token",
                                      "owner_identity")})


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
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


def _two_token(tmp_path, non_tmp_managed_base):
    raw = _raw_config(tmp_path, token_pool_size=2)
    root = non_tmp_managed_base / "two-token-node"
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
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
        "env": _source_bundle_env(held),
        "timeout": 30,
    })
    child = _child_pid(pidfile, node, held)
    try:
        os.kill(int(held["remote_run"]["pid"]), signal.SIGKILL)
        lease_path = Path(held["run_directory"]) / "lease.json"
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        lease["deadline_epoch"] = time.time() - 1
        lease["phase_deadline_epoch"] = time.time() - 1
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_unresolved_slot_stays_reserved_for_the_successor(tmp_path):
    raw = _raw_config(tmp_path, token_pool_size=2)
    root = tmp_path / "two-token-node"
    raw["boxes"]["box-a"]["managed_run_directory"] = str(root)
    path = tmp_path / "two-token.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _load_fake_node_config(path)
    executor = ScriptedExecutor(root)
    runner = RemoteRunner(config, executor=executor)
    node = runner.nodes["box-a"]
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
        lease["phase_deadline_epoch"] = time.time() - 1
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_stale_clear_does_not_signal_the_slots_current_occupant(tmp_path, non_tmp_managed_base):
    config, executor, _, node = _two_token(tmp_path, non_tmp_managed_base)
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_seat_is_recloned_from_base_between_runs(node_env):
    _, executor, _, node = node_env
    held = wait_state(node, enqueue(node, "init"), {"admitted"})
    start(node, held,
          "import pathlib, subprocess\n"
          "subprocess.check_call(['git', 'init'])\n"
          "pathlib.Path('README').write_text('x')\n"
          "pathlib.Path('run-residue').write_text('dirty')\n"
          "subprocess.check_call(['git', 'add', 'README'])\n"
          "subprocess.check_call(['git', '-c', 'user.email=t@example.com',"
          " '-c', 'user.name=t', 'commit', '-m', 'init'])\n")
    released = wait_state(node, held, {"released"})
    slot = Path(released["slot"])
    assert (slot / ".git").is_dir()
    assert not (slot / "SLOT.json").exists()
    assert not (slot / "slot.lock").exists()
    again = wait_state(node, enqueue(node, "reuse"), {"admitted"})
    start(node, again,
          "import pathlib, sys\n"
          "sys.exit(0 if (pathlib.Path('.git').is_dir() and "
          "not pathlib.Path('run-residue').exists() and "
          "pathlib.Path('README').read_text() == 'source snapshot\\n') else 3)\n")
    done = wait_state(node, again, {"released"})
    assert done["result"]["returncode"] == 0
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
def test_controller_repo_reaches_the_slot_and_the_result(tmp_path, non_tmp_managed_base):
    raw = _raw_config(tmp_path, token_pool_size=1)
    raw["repo"] = "goal-flight"
    root = non_tmp_managed_base / "repo-node"
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
        outcome = runner.run_arm(pool_spec())
        assert seen["repo"] == "goal-flight"
        assert outcome.lease["slot"].endswith("/repos/goal-flight/seats/s-01")
        # The watch returns when result.json appears, before the holder fsyncs
        # the index and marks the lease released.
        wait_state(runner.nodes["box-a"], outcome.lease, {"released"})
        line = json.loads((root / "results" / "index.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert line["repo"] == "goal-flight"
        kiln = replace(config, repo="kiln")
        executor.before = None
        kiln_runner = RemoteRunner(kiln, executor=executor)
        second = kiln_runner.run_arm(pool_spec("request-2"))
        assert second.lease["slot"].endswith("/repos/kiln/seats/s-01")
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
    outcome = RemoteRunner(config, executor=executor).run_arm(pool_spec())
    assert outcome.status == "capacity"
    assert outcome.returncode == 75
    assert outcome.status != "died"
    assert "REMOTE-DIED" not in (outcome.error or "")
    final = json.loads((Path(outcome.lease["run_directory"]) / "result.json").read_text(encoding="utf-8"))
    assert final["status"] == "capacity-refused"
    assert final["returncode"] != 77
    status = RemoteRunner(config, executor=executor).nodes["box-a"].call(
        "status", **key(outcome.lease)
    )
    for stream in status["result"]["streams"].values():
        assert stream["size_bytes"] == 0
        assert stream["sha256"] == hashlib.sha256(b"").hexdigest()
    _free_tokens(RemoteRunner(config, executor=executor).nodes["box-a"])


def test_unreachable_keep_machinery_is_gone():
    source = (ROOT / "scripts" / "goalflight_remote_ci_node.py").read_text(encoding="utf-8")
    assert "unknown-descendants" not in source
    assert "def token_held" not in source
    assert "RUN_MARKER" not in source
    assert "allowed_pgid" not in source


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
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
        "env": _source_bundle_env(held),
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
        lease["phase_deadline_epoch"] = time.time() - 1
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


@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
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
    monkeypatch.setattr(node, "_private_tree_proof",
                        lambda state, cid, members=None, run=None: True)
    monkeypatch.setattr(node, "TREE_GRACE_SECONDS", 0)
    monkeypatch.setattr(node.os, "kill", fake_kill)
    assert node._signal_incarnation({}, 4321, (10, 20), 100) is True
    assert signals == [signal.SIGTERM]
    assert signal.SIGKILL not in signals


def test_same_boot_coalition_id_uses_xnu_monotonicity_without_parentage(tmp_path, monkeypatch):
    """XNU's boot-local monotonic ID makes the recorded coalition stable."""
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 4242, "start": [1, 2], "coalition_id": 100,
        "boot_session": "boot-a",
    }), encoding="utf-8")
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 7)
    monkeypatch.setattr(node, "_boot_session_id", lambda: "boot-a")
    state = {
        "coalition_id": 100, "coalition_boot_session": "boot-a",
        "holder_coalition_id": 5, "holder_coalition_boot_session": "boot-a",
        "workload_pid": 4242, "workload_start": [1, 2],
    }
    assert node._private_tree_proof(state, 100, [(99991, (1, 2))], run=run) is True


def test_mismatched_recorded_coalition_ids_are_not_private(tmp_path, monkeypatch):
    """Reject a wrapper record whose coalition ID differs from durable state."""
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 4242, "start": [1, 2], "coalition_id": 101,
        "boot_session": "boot-a",
    }), encoding="utf-8")
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 7)
    monkeypatch.setattr(node, "_boot_session_id", lambda: "boot-a")
    state = {
        "coalition_id": 100, "coalition_boot_session": "boot-a",
        "holder_coalition_id": 5, "holder_coalition_boot_session": "boot-a",
        "workload_pid": 4242, "workload_start": [1, 2],
    }
    assert node._private_tree_proof(state, 100, run=run) is False


def test_stale_coalition_boot_session_is_not_proven_private(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 4242, "start": [1, 2], "coalition_id": 100,
        "boot_session": "boot-old",
    }), encoding="utf-8")
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 7)
    monkeypatch.setattr(node, "_boot_session_id", lambda: "boot-new")
    state = {
        "coalition_id": 100, "coalition_boot_session": "boot-old",
        "holder_coalition_id": 5, "holder_coalition_boot_session": "boot-old",
        "workload_pid": 4242, "workload_start": [1, 2],
    }
    assert node._private_tree_proof(state, 100, run=run) is False


def test_kill_escalation_rechecks_coalition_before_sigkill(monkeypatch):
    import goalflight_remote_ci_node as node

    signals = []

    def coalition(pid):
        del pid
        return 100 if not signals else 300

    monkeypatch.setattr(node, "_same_process", lambda pid, started: True)
    monkeypatch.setattr(node, "_coalition_id", coalition)
    monkeypatch.setattr(node, "_private_tree_proof",
                        lambda state, cid, members=None, run=None: True)
    monkeypatch.setattr(node, "TREE_GRACE_SECONDS", 0)
    monkeypatch.setattr(node.os, "kill", lambda pid, sig: signals.append(sig))
    assert node._signal_incarnation({}, 20, (99, 99), 100) is False
    assert signals == [signal.SIGTERM]
    assert signal.SIGKILL not in signals


def test_new_seat_pool_does_not_reuse_legacy_full_copy_slots(tmp_path, non_tmp_managed_base):
    import fcntl

    config, executor, _, node = _two_token(tmp_path, non_tmp_managed_base)
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
        assert str(fresh["slot"]).endswith("/seats/s-01")
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
        if argv[:2] == ["launchctl", "list"]:
            class Result:
                returncode = 0
                stderr = ""
                stdout = ""

            return Result()
        if argv[:2] == ["sysctl", "-n"]:
            class Result:
                returncode = 0
                stderr = ""
                stdout = "boot-a\n"

            return Result()
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

    (run / "wrapper-identity.json").write_text(json.dumps(
        {"pid": 42, "start": [10, 20], "coalition_id": 100,
         "boot_session": "boot-a"}), encoding="utf-8")
    monkeypatch.setattr(node.subprocess, "run", fake_run)
    monkeypatch.setattr(node, "_job_view", lambda label: ("running", 42, None))
    monkeypatch.setattr(node, "_stable_identity", lambda pid: ((10, 20), 100, "boot-a"))
    monkeypatch.setattr(node, "_same_process", lambda pid, started: True)
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 5)
    monkeypatch.setattr(node, "_boot_session_id", lambda: "boot-a")
    monkeypatch.setattr(node, "_remember_identity", remember)
    launched = node._submit_workload(run, state, ["/bin/sleep", "1"], {}, "")
    assert launched[0] == 42
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["coalition_id"] == 100
    assert lease["coalition_boot_session"] == "boot-a"
    assert lease["holder_coalition_boot_session"] == "boot-a"
    assert lease["launch_label"] == "com.goalflight.remote-ci.abc"
    assert (run / "workload-go").is_file()


def test_exited_job_without_holder_proof_can_release_an_empty_tree(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 42, "start": [10, 20],
    }), encoding="utf-8")
    monkeypatch.setattr(node, "_job_view", lambda label: ("exited", None, 19))
    removed = []
    monkeypatch.setattr(node, "_remove_job", lambda label: removed.append(label) or True)
    # The launcher exited before command-started, so there is no workload tree.
    assert not node._workload_released(run, state)
    assert node.clear_tree(tmp_path, state, run) is True
    assert state["exit_code"] == 19
    assert state["exit_known"] is True
    assert removed == [state["launch_label"]]

    started = tmp_path / "started"
    started.mkdir()
    started_state = {
        "lease_id": "started", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.started",
    }
    (started / "lease.json").write_text(json.dumps(started_state), encoding="utf-8")
    (started / "wrapper-identity.json").write_text(json.dumps({
        "pid": 42, "start": [10, 20],
    }), encoding="utf-8")
    (started / "workload-go").touch()
    assert node._workload_released(started, started_state)
    assert node.clear_tree(tmp_path, started_state, started) is False
    assert removed == [state["launch_label"]]


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

    (run / "wrapper-identity.json").write_text(json.dumps(
        {"pid": 42, "start": [10, 20], "coalition_id": 100,
         "boot_session": "boot-a"}), encoding="utf-8")
    monkeypatch.setattr(node.subprocess, "run", lambda argv, **kwargs: type("R", (), {
        "returncode": 0, "stderr": "", "stdout": ""})())
    monkeypatch.setattr(node, "_job_view", lambda label: ("running", 42, None))
    monkeypatch.setattr(node, "_stable_identity", lambda pid: ((10, 20), 100, "boot-a"))
    monkeypatch.setattr(node, "_same_process", lambda pid, started: True)
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 5)
    monkeypatch.setattr(node, "_boot_session_id", lambda: "boot-a")
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
        coalition_boot_session="boot-a",
        holder_coalition_id=5,
        holder_coalition_boot_session="boot-a",
        workload_pid="40",
        workload_start=[1, 2],
    )
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 40, "start": [1, 2], "coalition_id": 100,
        "boot_session": "boot-a",
    }), encoding="utf-8")
    (run / "command-started").touch()
    (run / "exit.json").write_text(json.dumps({
        "pid": 40, "returncode": 19, "signal": None,
    }), encoding="utf-8")
    monkeypatch.setattr(node, "_boot_session_id", lambda: "boot-a")
    monkeypatch.setattr(node, "_coalition_id", lambda _pid: 5)
    monkeypatch.setattr(node, "_verify_incarnation", lambda *_args: False)
    monkeypatch.setattr(node, "_coalition_members", lambda _cid: [])
    removed = []

    def remove(label):
        lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
        assert (run / "exit.json").is_file()
        assert lease["exit_known"] is True
        assert lease["exit_code"] == 19
        removed.append(label)
        return True

    monkeypatch.setattr(node, "_remove_job", remove)
    assert node.clear_tree(tmp_path, state, run) is True
    assert removed == [state["launch_label"]]
    assert state["exit_known"] is True
    assert state["exit_code"] == 19
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
        "coalition_id": 100, "coalition_boot_session": "boot-a",
        "holder_coalition_id": 5, "holder_coalition_boot_session": "boot-a",
        "workload_pid": "40", "workload_start": [1, 2],
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 40, "start": [1, 2], "coalition_id": 100,
        "boot_session": "boot-a",
    }), encoding="utf-8")
    monkeypatch.setattr(node, "_boot_session_id", lambda: "boot-a")
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 5)
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
    owner = {"owner_host": "h", "owner_pid": 1, "owner_start_token": "st",
             "owner_identity": "t"}
    state = {
        "schema": "goalflight.remote-ci.lease.v1",
        "lease_id": "abc", "lease_token": "tok", "state": state_name,
        "token_index": 0, "launch_label": "com.goalflight.remote-ci.abc",
        "coalition_id": 100, "coalition_boot_session": "boot-a",
        "holder_coalition_id": 5, "holder_coalition_boot_session": "boot-a",
        "deadline_epoch": time.time() + deadline_offset,
        "remote_run": remote, "job_remove_pending": True,
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 40, "start": [1, 2], "coalition_id": 100,
        "boot_session": "boot-a",
    }), encoding="utf-8")
    monkeypatch.setattr(node, "_boot_session_id", lambda: "boot-a")
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 5)
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
    owner = {"owner_host": "h", "owner_pid": 1, "owner_start_token": "st",
             "owner_identity": "t"}
    state = {
        "schema": "goalflight.remote-ci.lease.v1",
        "lease_id": "abc", "lease_token": "tok", "state": "draining",
        "token_index": 0, "slot": str(slot),
        "deadline_epoch": time.time() + 3600,
        "remote_run": remote,
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    monkeypatch.setattr(node, "cwd_intruders", lambda paths, **_kwargs: [os.getpid()])
    monkeypatch.setattr(node, "cwd_snapshot", lambda: [])
    result = node.dispatch({
        "operation": "cancel", "managed_root": str(managed), "box": "b",
        "p_cores": 4, "token_pool_size": 1, "run_dir": str(run),
        "lease_token": "tok", "identity": remote, "expected_owner": owner,
    })
    assert result["status"] == "unknown"
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["state"] == "draining"
    monkeypatch.setattr(node, "cwd_intruders", lambda paths, **_kwargs: None)
    monkeypatch.setattr(node, "cwd_snapshot", lambda: [])
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


def test_live_wrapper_outside_an_empty_cwd_keeps_the_slot(tmp_path, monkeypatch):
    """launch_submitted, absent listing, empty cwd, wrapper alive elsewhere."""
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "launch_submitted": True,
        "launch_intent_at": time.time(),
        "slot": str(run),
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps(
        {"pid": 4242, "start": [1, 2], "coalition_id": 100,
         "boot_session": "boot-a"}), encoding="utf-8")
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    monkeypatch.setattr(node, "cwd_intruders", lambda paths, **_kwargs: [])
    monkeypatch.setattr(node, "cwd_snapshot", lambda: [])
    monkeypatch.setattr(node, "_same_process", lambda pid, started: True)
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [])
    monkeypatch.setattr(node, "_remove_job", lambda label: True)
    assert node.clear_tree(tmp_path, state, run) is False


def test_absent_submit_without_a_wrapper_identity_is_not_released(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "launch_submitted": True,
        "launch_intent_at": time.time() - 120,
        "slot": str(run),
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    monkeypatch.setattr(node, "cwd_intruders", lambda paths, **_kwargs: [])
    monkeypatch.setattr(node, "cwd_snapshot", lambda: [])
    assert node.clear_tree(tmp_path, state, run) is False
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease["state"] != "released"
    assert lease.get("identity_status") == "unproven"


def test_ambiguous_launch_failure_is_not_an_empty_tree(tmp_path, monkeypatch):
    """launch-failed plus an absent listing and no identity is not empty."""
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "launch_submitted": True,
        "launch_intent_at": time.time() - 120,
        "release_reason": "launch-failed",
        "slot": str(run),
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    monkeypatch.setattr(node, "cwd_intruders", lambda paths, **_kwargs: [])
    monkeypatch.setattr(node, "cwd_snapshot", lambda: [])
    assert node.clear_tree(tmp_path, state, run) is False
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease.get("identity_status") == "unproven"
    assert lease.get("state") != "released"


def test_not_loaded_submit_can_release_when_the_label_was_never_present(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {"lease_id": "abc", "state": "running"}
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")

    def submit(argv, **kwargs):
        del kwargs
        if argv[:2] == ["launchctl", "list"]:
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        return type("R", (), {
            "returncode": 1, "stderr": "service is not loaded\n", "stdout": ""})()

    monkeypatch.setattr(node.subprocess, "run", submit)
    assert "not loaded" in node._submit_workload(run, state, ["/bin/true"], {}, "")
    assert state.get("launch_rejected") is True
    assert node.clear_tree(tmp_path, state, run) is True


def test_missing_holder_coalition_does_not_signal_outside_the_wrapper(tmp_path, monkeypatch):
    """A live wrapper coalition with no holder proof is never signalled."""
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "launch_submitted": True,
        "launch_intent_at": time.time(),
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps(
        {"pid": 4242, "start": [1, 2], "coalition_id": 100}), encoding="utf-8")
    killed = []
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    monkeypatch.setattr(node, "_same_process", lambda pid, started: True)
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 100)
    monkeypatch.setattr(node, "_coalition_members",
                        lambda cid: [(4242, (1, 2)), (99991, (3, 4))])
    monkeypatch.setattr(node, "_remove_job", lambda label: True)
    monkeypatch.setattr(node.os, "kill", lambda pid, sig: killed.append(pid))
    assert node.clear_tree(tmp_path, state, run) is False
    assert killed == []
    lease = json.loads((run / "lease.json").read_text(encoding="utf-8"))
    assert lease.get("identity_status") == "holder-coalition-unknown"


def test_dead_wrapper_with_an_empty_coalition_can_release(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "launch_submitted": True,
        "launch_intent_at": time.time() - 120,
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps(
        {"pid": 4242, "start": [1, 2], "coalition_id": 100,
         "boot_session": "boot-a"}), encoding="utf-8")
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    monkeypatch.setattr(node, "cwd_intruders", lambda paths, **_kwargs: [])
    monkeypatch.setattr(node, "cwd_snapshot", lambda: [])
    monkeypatch.setattr(node, "_same_process", lambda pid, started: False)
    monkeypatch.setattr(node, "_boot_session_id", lambda: "boot-a")
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [])
    assert node.clear_tree(tmp_path, state, run) is True


def test_dead_wrapper_with_an_unreadable_coalition_is_not_empty(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "launch_submitted": True,
        "launch_intent_at": time.time(),
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps(
        {"pid": 4242, "start": [1, 2], "coalition_id": 100}), encoding="utf-8")
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    monkeypatch.setattr(node, "cwd_intruders", lambda paths, **_kwargs: [])
    monkeypatch.setattr(node, "cwd_snapshot", lambda: [])
    monkeypatch.setattr(node, "_same_process", lambda pid, started: False)
    monkeypatch.setattr(node, "_coalition_members", lambda cid: None)
    assert node.clear_tree(tmp_path, state, run) is False


def test_stale_listing_does_not_adopt_a_reused_pid(tmp_path, monkeypatch):
    """Same pid, new start token, launchd still listing the old pid."""
    import goalflight_remote_ci_node as node

    run = tmp_path / "run"
    run.mkdir()
    state = {
        "lease_id": "abc", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "launch_submitted": True,
        "launch_intent_at": time.time(),
    }
    (run / "lease.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps(
        {"pid": 50, "start": [1, 2], "coalition_id": 100,
         "boot_session": "boot-a"}), encoding="utf-8")
    killed = []
    monkeypatch.setattr(node, "_job_view", lambda label: ("running", 50, None))
    monkeypatch.setattr(node, "_stable_identity", lambda pid: ((9, 9), 777, "boot-a"))
    monkeypatch.setattr(node, "_same_process", lambda pid, started: tuple(started) == (9, 9))
    monkeypatch.setattr(node, "_coalition_id", lambda pid: 5)
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [(50, (9, 9))])
    monkeypatch.setattr(node, "_remove_job", lambda label: False)
    monkeypatch.setattr(node.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert node.clear_tree(tmp_path, state, run) is False
    assert state.get("coalition_id") != 777
    assert killed == []


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
    monkeypatch.setattr(node, "_stable_identity", lambda pid: ((1, 2), 777, "boot-a"))
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
    monkeypatch.setattr(node, "cwd_intruders", lambda paths, **_kwargs: [os.getpid()])
    monkeypatch.setattr(node, "cwd_snapshot", lambda: [])
    assert node.clear_tree(tmp_path, state, run) is False


def test_unreadable_coalition_is_not_an_empty_sweep(tmp_path, monkeypatch):
    """A scan that cannot prove membership neither removes nor counts as empty."""
    import goalflight_remote_ci_node as node

    managed = tmp_path / "managed"
    run = managed / "runs" / "abc"
    run.mkdir(parents=True)
    (managed / "admission").mkdir()
    (managed / "admission" / "queue.lock").write_text("", encoding="utf-8")
    (run / "lease.json").write_text(json.dumps({
        "lease_id": "abc", "lease_token": "tok", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "coalition_id": 4242,
        "holder_coalition_id": 7,
    }), encoding="utf-8")
    monkeypatch.setattr(node, "_job_view", lambda label: "unknown")
    monkeypatch.setattr(node, "_remove_job", lambda label: (_ for _ in ()).throw(
        AssertionError("unknown tree must not remove the job")))
    _sweep_unknown.clear()
    try:
        _sweep_managed_jobs(managed)
        assert _sweep_unknown == [4242]
    finally:
        _sweep_unknown.clear()
        _swept_coalitions.clear()


def test_sweep_rechecks_incarnation_before_kill(tmp_path, monkeypatch):
    """The teardown path uses production incarnation checks and keeps UNKNOWN held."""
    import goalflight_remote_ci_node as node

    managed = tmp_path / "managed"
    run = managed / "runs" / "abc"
    run.mkdir(parents=True)
    (managed / "admission").mkdir()
    (managed / "admission" / "queue.lock").write_text("", encoding="utf-8")
    (run / "lease.json").write_text(json.dumps({
        "lease_id": "abc", "lease_token": "tok", "state": "draining",
        "launch_label": "com.goalflight.remote-ci.abc",
        "coalition_id": 4242,
        "coalition_boot_session": "boot-a",
        "holder_coalition_id": 7,
        "holder_coalition_boot_session": "boot-a",
        "workload_pid": 4321,
        "workload_start": [11, 22],
    }), encoding="utf-8")
    (run / "wrapper-identity.json").write_text(json.dumps({
        "pid": 4321, "start": [11, 22], "coalition_id": 4242,
        "boot_session": "boot-a",
    }), encoding="utf-8")
    started = (11, 22)
    calls = []

    def signal_incarnation(state, pid, started_at, cid, run=None):
        del state
        calls.append((pid, started_at, cid, run))
        return False

    def coalition(pid):
        return 99 if pid == os.getpid() else 4242

    monkeypatch.setattr(node, "_boot_session_id", lambda: "boot-a")
    monkeypatch.setattr(node, "_coalition_id", coalition)
    monkeypatch.setattr(node, "_job_view", lambda label: "absent")
    monkeypatch.setattr(node, "_verify_incarnation", lambda pid, start: True)
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [(4321, started)])
    monkeypatch.setattr(node, "_signal_incarnation", signal_incarnation)
    monkeypatch.setattr(node, "_remove_job", lambda label: (_ for _ in ()).throw(
        AssertionError("live members must prevent job removal")))
    try:
        _sweep_managed_jobs(managed)
        assert calls and all(call[:3] == (4321, started, 4242) for call in calls)
        assert _sweep_unknown == [4242]
    finally:
        _sweep_unknown.clear()
        _swept_coalitions.clear()


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


@pytest.mark.parametrize(
    ("marker_state", "expected_indexes", "expected_reason"),
    [
        ("indexed", {0}, None),
        ("legacy", None, "unknown-token-ownership"),
        ("unreadable", None, "unknown-token-ownership"),
        ("missing", None, "unknown-token-ownership"),
    ],
)
def test_unreadable_admitted_lease_reserves_token_ownership(
        tmp_path, monkeypatch, marker_state, expected_indexes, expected_reason):
    import builtins
    import goalflight_remote_ci_node as node

    monkeypatch.setattr(
        builtins, "_GOALFLIGHT_REMOTE_CI_AUTHORITY",
        str(tmp_path / "auth.json"), raising=False)
    monkeypatch.setattr(node, "sample", lambda *_args: {
        "hostname": "test-node", "load1": 0.0, "load_average": [0.0, 0.0, 0.0],
        "cpu_busy_fraction": 0.0, "p_cores": 8, "token_pool_size": 2,
        "self_cap": 4, "tools": {"python": True, "node": True, "git": True},
    })
    managed = tmp_path / "managed"
    run = managed / "runs" / "bad"
    run.mkdir(parents=True)
    (run / "lease.json").write_text(json.dumps({
        "lease_id": "bad", "state": "admitted", "token_index": 0,
    }), encoding="utf-8")
    marker = managed / "repos" / "project-a" / "slot-meta" / "s-01.json"
    marker.parent.mkdir(parents=True)
    if marker_state == "indexed":
        marker.write_text(json.dumps({
            "state": "leased", "lease_id": "bad", "token_index": 0,
            "run_dir": str(run),
        }), encoding="utf-8")
    elif marker_state == "legacy":
        marker.write_text(json.dumps({
            "state": "leased", "lease_id": "bad", "run_dir": str(run),
        }), encoding="utf-8")
    elif marker_state == "unreadable":
        marker.write_text(json.dumps({
            "state": "leased", "lease_id": "bad", "token_index": 0,
            "run_dir": str(run),
        }), encoding="utf-8")
    token_path = managed / "admission" / "tokens" / "0"
    token_path.parent.mkdir(parents=True)
    token = node.try_lock(token_path)
    assert token is not None
    token.close()  # the holder exited while its launchd workload remains
    (run / "lease.json").write_text("{", encoding="utf-8")
    if marker_state == "unreadable":
        read_json = node.read_json

        def unreadable_marker(path):
            if Path(path) == marker:
                raise PermissionError("slot marker is unreadable")
            return read_json(path)

        monkeypatch.setattr(node, "read_json", unreadable_marker)
    reserved, reason = node.reserved_token_indexes(managed, token_pool_size=2)
    assert reserved == expected_indexes
    assert reason == expected_reason
    assert node.admission_queue_state(managed) == ({}, {})
    report = node.dispatch({
        "operation": "health", "managed_root": str(managed), "box": "b",
        "p_cores": 8, "token_pool_size": 2, "python_interpreter": sys.executable,
    })
    assert report["tokens"].get("reservation_reason") == expected_reason
    assert report["tokens"]["in_use"] == (1 if expected_reason is None else 2)
    assert report["tokens"]["free"] == (1 if expected_reason is None else 0)
    listed = node.dispatch({
        "operation": "list", "managed_root": str(managed), "box": "b",
        "p_cores": 8, "token_pool_size": 2, "python_interpreter": sys.executable,
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
    ("died", "setup-failed", 2, {"returncode": 9}),
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
        box = config.boxes["box-a"]

        def call(self, operation, **kwargs):
            assert operation == "status"
            return record

    outcome = runner._watch(
        spec(), Node(), {"run_dir": "/runs/abc", "lease_token": "lease"}, {})
    # A `died` status without exit_known is not a command verdict, so it falls back.
    assert outcome.status == status
    assert outcome.returncode == code


def test_requirement_hash_inputs_follow_attached_and_spaced_includes(tmp_path):
    import goalflight_remote_ci_node as node

    slot = tmp_path / "slot"
    slot.mkdir()
    (slot / "requirements-test.txt").write_text(
        "-rshared.txt\n-c constraints.txt\n-r=attached.txt\n-cconstraints-extra.txt\n",
        encoding="utf-8")
    for name in ("shared.txt", "constraints.txt", "attached.txt", "constraints-extra.txt"):
        (slot / name).write_text(name + "==1\n", encoding="utf-8")
    inputs = node._requirement_inputs(slot.resolve(), Path("requirements-test.txt"))
    assert {path.name for path, _ in inputs} == {
        "requirements-test.txt", "shared.txt", "constraints.txt",
        "attached.txt", "constraints-extra.txt",
    }


def test_incomplete_cached_venv_is_rebuilt_without_completion_marker(tmp_path, monkeypatch):
    import goalflight_remote_ci_node as node

    slot = tmp_path / "slot"
    slot.mkdir()
    (slot / "requirements-test.txt").write_text("pytest==9.0.3\n", encoding="utf-8")
    calls = []

    def setup(argv, **kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["-m", "venv"]:
            target = Path(argv[3])
            (target / "bin").mkdir(parents=True)
            python = target / "bin/python"
            python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            python.chmod(0o755)
        stdout = "Python 3.14.5\n" if argv[-1] == "--version" else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(node, "_run_setup", setup)
    managed = tmp_path / "managed"
    venv, digest = node.prepare_venv(
        managed, "repo", slot, "requirements-test.txt", sys.executable)
    marker = venv / ".goalflight-complete"
    assert marker.read_text().strip() == digest
    marker.unlink()
    calls.clear()
    rebuilt, rebuilt_digest = node.prepare_venv(
        managed, "repo", slot, "requirements-test.txt", sys.executable)
    assert rebuilt == venv and rebuilt_digest == digest
    assert sum(call[1:3] == ["-m", "venv"] for call in calls) == 1
    assert marker.read_text().strip() == digest


# This detached-fork case exercises the host's real launchd behavior; earlier
# unmocked start-path regressions opt into the same leak guard individually.
@pytest.mark.usefixtures("no_leftover_remote_ci_job")
@pytest.mark.usefixtures("requires_live_launchd")
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
        "env": _source_bundle_env(held),
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
    monkeypatch.setattr(node, "sample", lambda *_args: {
        "hostname": "test-node", "load1": 0.0, "load_average": [0.0, 0.0, 0.0],
        "cpu_busy_fraction": 0.0, "p_cores": 100000, "token_pool_size": 1,
        "self_cap": 4, "tools": {"python": True, "node": True, "git": True},
    })
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
    def submit(run, *args, **kwargs):
        del args, kwargs
        (run / "command-started").touch()
        return 42, (1, 2), 100

    monkeypatch.setattr(node, "_submit_workload", submit)
    monkeypatch.setattr(node, "_remove_job", lambda label: True)
    monkeypatch.setattr(node, "_coalition_members", lambda cid: [])
    monkeypatch.setattr(node, "cwd_intruders", lambda paths, **_kwargs: [])
    monkeypatch.setattr(node, "cwd_snapshot", lambda: [])
    # dispatch() forks this holder from pytest, so it inherits this test seam.
    monkeypatch.setattr(node, "admission_command_window_seconds", lambda _: 0.3)
    managed = tmp_path / "managed"
    owner = {"owner_host": "h", "owner_pid": 1, "owner_start_token": "st",
             "owner_identity": "t"}
    record = node.dispatch({
        "operation": "enqueue", "managed_root": str(managed), "box": "b",
        "p_cores": 100000, "token_pool_size": 1, "poll_seconds": 0.01,
        "controller_poll_seconds": 0.01,
        "python_interpreter": sys.executable,
        "request_id": "leader-unknown", "arm": "candidate", "owner": owner,
    })
    holder = None
    key = {
        "operation": "status", "managed_root": str(managed), "box": "b",
        "p_cores": 100000, "token_pool_size": 1,
        "controller_poll_seconds": 0.01,
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


def test_enqueue_without_controller_poll_seconds_is_rejected(tmp_path, monkeypatch):
    import builtins
    import goalflight_remote_ci_node as node

    monkeypatch.setattr(
        builtins, "_GOALFLIGHT_REMOTE_CI_AUTHORITY",
        str(tmp_path / "auth.json"), raising=False)
    with pytest.raises(ValueError, match="controller_poll_seconds must be a positive finite number"):
        node.dispatch({
            "operation": "enqueue", "managed_root": str(tmp_path / "managed"), "box": "b",
            "p_cores": 1, "token_pool_size": 1, "poll_seconds": 0.01,
            "request_id": "missing-poll", "arm": "candidate",
            "owner": {"owner_host": "h", "owner_pid": 1,
                      "owner_start_token": "st", "owner_identity": "t"},
        })
