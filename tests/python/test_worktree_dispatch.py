#!/usr/bin/env python3
"""Tests for local worktree seat routing."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("worktree dispatch tests use POSIX symlink semantics")

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

TEST_RUNTIME = tempfile.TemporaryDirectory(prefix="goal-flight-worktree-tests-")
TEST_RUNTIME_ROOT = Path(TEST_RUNTIME.name)
os.environ["GOALFLIGHT_CAPACITY_CONF"] = os.devnull
os.environ["GOALFLIGHT_TASK_STORE_DIR"] = str(TEST_RUNTIME_ROOT / "task-store")
os.environ["GOALFLIGHT_JOURNAL_DIR"] = str(TEST_RUNTIME_ROOT / "journal")
os.environ["GOALFLIGHT_MESSAGES_DIR"] = str(TEST_RUNTIME_ROOT / "messages")
os.environ["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(TEST_RUNTIME_ROOT / "wake-ledger")
os.environ["GOAL_FLIGHT_PIDFILE_DIR"] = str(TEST_RUNTIME_ROOT / "pids")
os.environ.pop("GOALFLIGHT_STEER_FILE", None)
os.environ.pop("GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE", None)

import goalflight_acp_run
import goalflight_capacity
import goalflight_doctor
import goalflight_worktree_pool

OS_SANDBOX_OFF = goalflight_acp_run.OS_SANDBOX_OFF


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")
    return result.stdout


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "goalflight-test@example.invalid")
    git(repo, "config", "user.name", "Goal Flight Test")
    (repo / "tracked.txt").write_text("base\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "base")
    return repo


@contextmanager
def seat_limit(limit: int):
    name = goalflight_worktree_pool.WORKTREE_SEATS_ENV
    prior = os.environ.get(name)
    os.environ[name] = str(limit)
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prior


def test_worktree_create_routes_distinct_cwds_and_stale_probe() -> None:
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_seats = os.environ.get(goalflight_worktree_pool.WORKTREE_SEATS_ENV)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            os.environ["GOALFLIGHT_STATE_DIR"] = str(root / "state")
            os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
            os.environ[goalflight_worktree_pool.WORKTREE_SEATS_ENV] = "2"
            repo = make_repo(root)

            args_one = argparse.Namespace(cwd=str(repo))
            args_two = argparse.Namespace(cwd=str(repo))
            lease_one = goalflight_acp_run.create_and_route_dispatch_worktree(
                args_one,
                repo,
                "acp-test-one",
            )
            lease_two = goalflight_acp_run.create_and_route_dispatch_worktree(
                args_two,
                repo,
                "acp-test-two",
            )
            wt_one = lease_one.path
            wt_two = lease_two.path

            # macOS resolves /var/folders → /private/var/folders; goalflight_acp_run
            # calls project_root.resolve() before building the worktree path. Resolve
            # `repo` for comparison so the assertion isn't fooled by the /private prefix.
            resolved_repo = repo.resolve()
            assert_true("distinct worktrees", wt_one != wt_two)
            assert_true("first under managed root", wt_one.parent == resolved_repo / "worktrees")
            assert_true("second under managed root", wt_two.parent == resolved_repo / "worktrees")
            assert_true("first cfg cwd unchanged", args_one.cwd == str(repo))
            assert_true("second cfg cwd unchanged", args_two.cwd == str(repo))

            (wt_one / "tracked.txt").write_text("worker one\n")
            (wt_two / "tracked.txt").write_text("worker two\n")
            assert_true("first edit isolated", (wt_one / "tracked.txt").read_text() == "worker one\n")
            assert_true("second edit isolated", (wt_two / "tracked.txt").read_text() == "worker two\n")
            assert_true("main worktree unchanged", (repo / "tracked.txt").read_text() == "base\n")
            assert_true("first status modified", " M tracked.txt" in git(wt_one, "status", "--short"))
            assert_true("second status modified", " M tracked.txt" in git(wt_two, "status", "--short"))

            probe = goalflight_doctor.check_worktrees(repo)
            assert_true("doctor count", probe["count"] == 2)
            assert_true("doctor paths", str(wt_one) in probe["paths"] and str(wt_two) in probe["paths"])
            by_path = {item["path"]: item for item in probe["details"]}
            assert_true("doctor detail head", by_path[str(wt_one)]["head"] is not None)
            assert_true("doctor detail dirty", by_path[str(wt_two)]["dirty"] is True)
            assert_true("first seat held", by_path[str(wt_one)]["seat_state"] == "held")
            assert_true("second occupant named", by_path[str(wt_two)]["occupant_dispatch_id"] == "acp-test-two")
            assert_true("pooled seats are never stale", probe["stale"] == [])

            git(wt_one, "reset", "--hard")
            git(wt_two, "reset", "--hard")
            lease_one.release()
            lease_two.release()
            git(repo, "worktree", "remove", "--force", str(wt_one))
            git(repo, "worktree", "remove", "--force", str(wt_two))
            git(repo, "worktree", "prune")
            assert_true("main clean after teardown", git(repo, "status", "--short") == "")
    finally:
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
        if old_seats is None:
            os.environ.pop(goalflight_worktree_pool.WORKTREE_SEATS_ENV, None)
        else:
            os.environ[goalflight_worktree_pool.WORKTREE_SEATS_ENV] = old_seats


def test_worktree_root_symlink_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        root = Path(td)
        repo = make_repo(root)
        outside = root / "outside"
        outside.mkdir()
        (repo / "worktrees").symlink_to(outside, target_is_directory=True)

        try:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "acp-test-symlink")
        except goalflight_worktree_pool.WorktreeSeatError as exc:
            assert_true("symlink error", "symlink" in str(exc))
        else:
            raise AssertionError("symlinked worktrees root was accepted")

        probe = goalflight_doctor.check_worktrees(repo)
        assert_true("doctor rejects symlink", probe["ok"] is False)
        assert_true("doctor symlink error", "symlink" in probe.get("error", ""))

        (repo / "worktrees").unlink()
        (repo / "worktrees").symlink_to(root / "missing", target_is_directory=True)
        broken_probe = goalflight_doctor.check_worktrees(repo)
        assert_true("doctor rejects broken symlink", broken_probe["ok"] is False)
        assert_true("doctor broken symlink error", "symlink" in broken_probe.get("error", ""))


def test_worktree_leaf_symlink_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        root = Path(td)
        repo = make_repo(root)
        outside = root / "outside"
        outside.mkdir()
        label = goalflight_worktree_pool.default_controller_ring_label(
            None, project_root=repo
        )
        managed = repo / "worktrees" / label
        managed.mkdir(parents=True)
        (managed / "s-1").symlink_to(outside, target_is_directory=True)

        try:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "acp-test-leaf")
        except goalflight_worktree_pool.WorktreeSeatError as exc:
            assert_true("leaf symlink error", "symlink" in str(exc))
        else:
            raise AssertionError("symlinked worktree leaf was accepted")


def test_doctor_capacity_unknown_does_not_mark_stale() -> None:
    old_capacity = goalflight_doctor.goalflight_capacity
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            wt = repo.resolve() / "worktrees" / "acp-test-unknown"
            wt.parent.mkdir()
            git(repo, "worktree", "add", "--detach", str(wt), "HEAD")

            goalflight_doctor.goalflight_capacity = None
            probe = goalflight_doctor.check_worktrees(repo)
            assert_true("capacity unknown warns", probe["ok"] is False)
            assert_true("capacity unknown does not mark stale", probe["stale"] == [])
            assert_true("path still listed", str(wt) in probe["paths"])
    finally:
        goalflight_doctor.goalflight_capacity = old_capacity


def test_doctor_flags_registered_leaf_escape() -> None:
    old_run = goalflight_doctor.run
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            outside = root / "outside"
            outside.mkdir()
            managed = repo / "worktrees"
            managed.mkdir()
            leaf = managed / "acp-test-escape"
            leaf.symlink_to(outside, target_is_directory=True)

            def fake_run(_cmd, cwd=None, timeout=8.0):
                return {
                    "ok": True,
                    "stdout": f"worktree {leaf}\nHEAD 0000000",
                    "stderr": "",
                    "returncode": 0,
                }

            goalflight_doctor.run = fake_run
            probe = goalflight_doctor.check_worktrees(repo)
            assert_true("doctor rejects leaf escape", probe["ok"] is False)
            assert_true("leaf escape listed", str(leaf.absolute()) in probe["escaped"])
    finally:
        goalflight_doctor.run = old_run


def test_doctor_flags_orphaned_blocking_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = make_repo(root)
        orphan = repo / "worktrees" / "acp-orphan"
        orphan.mkdir(parents=True)
        probe = goalflight_doctor.check_worktrees(repo)
        assert_true("orphan blocks", probe["ok"] is False)
        assert_true("orphan listed", str(orphan.absolute()) in probe["blocking_paths"])


def test_execute_parallel_docs_require_worktree_create() -> None:
    text = (ROOT / "commands" / "execute.md").read_text()
    protocol = (ROOT / "protocols" / "worktrees-parallel.md").read_text()
    assert_true("isolation is not a mode", "Isolation is not a mode" in text)
    assert_true(
        "canonical snippet has no --cwd",
        "--agent <ready-agent> --prompt-file p.md" in text
        and "--cwd ." not in text,
    )
    assert_true("HEAD-only base documented", "committed `HEAD`" in text)
    assert_true("preserve unrelated WIP", "Preserve unrelated WIP." in text)
    assert_true(
        "never move another owner's WIP",
        "Never stash, move, or discard another owner's WIP" in text,
    )
    assert_true("captive slot range documented", "worktrees/<controller-label>/s-1" in text)
    assert_true("hard ceiling documented", "hard ceiling" in text)
    assert_true("fuse default documented", "default 24" in text)
    assert_true("index the captive ring", "Index the captive ring" in protocol)
    assert_true(
        "do not exclude captive seats",
        "Do **not** exclude `worktrees/s-*`" in protocol,
    )
    assert_true("codedb ignore location documented", "`.codedbignore`" in protocol)
    assert_true(
        "protocol forbids sequential-root split",
        "sequential dispatch" not in protocol.lower()
        or "stays in the project root" not in protocol,
    )


class FakeProc:
    pid = os.getpid()
    returncode = 0


class FakeConn:
    os_sandbox_metadata = None

    async def close_gracefully(self) -> None:
        return None


class FakePromptResult:
    ok = True
    error = None
    cancelled_for_marker = False
    early_marker = None
    permission_auto_declined: list[dict] = []
    permission_escalations: list[dict] = []
    text = "COMPLETE: acp-run-contract — true\n"
    stop_reason = "end_turn"
    out_of_scope_writes: list[str] = []


async def fake_run_prompt(*_args, **_kwargs):
    return FakePromptResult()


def runner_args(repo: Path, dispatch_id: str, status_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        agent="codex",
        unregistered_forced=True,
        install_slot=None,
        cwd=str(repo),
        worktree="create",
        session_id="test-session",
        dispatch_id=dispatch_id,
        prompt_id="prompt-1",
        prompt=None,
        prompt_text="test prompt",
        prompt_b64=None,
        mode="one-shot",
        idle_timeout=300.0,
        status_json=str(status_path),
        context_mode="disabled",
        os_sandbox=OS_SANDBOX_OFF,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        permission_allow_tool_title_pattern=[],
        heartbeat_interval=15.0,
        wedge_samples=4,
        max_tool_s=goalflight_acp_run.DEFAULT_MAX_TOOL_S,
        max_quiet_s=3600.0,
        progress_stall_s=300.0,
        liveness_profile=None,
        remote_turn_silence_s=None,
        remote_turn_cancel_grace_s=goalflight_acp_run.DEFAULT_REMOTE_TURN_CANCEL_GRACE_S,
        cpu_epsilon=0.1,
        json=True,
    )


def test_runner_worktree_status_and_capacity_contract() -> None:
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_seats = os.environ.get(goalflight_worktree_pool.WORKTREE_SEATS_ENV)
    patches = {
        "agent_command": goalflight_acp_run.agent_command,
        "validate_acp_dispatch_readiness": goalflight_acp_run.validate_acp_dispatch_readiness,
        "validate_os_sandbox_request": goalflight_acp_run.validate_os_sandbox_request,
        "preflight_os_sandbox": goalflight_acp_run.preflight_os_sandbox,
        "cleanup_ghosts": goalflight_acp_run.cleanup_ghosts,
        "spawn_and_handshake_with_retry": goalflight_acp_run.spawn_and_handshake_with_retry,
        "run_prompt": goalflight_acp_run.run_prompt,
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            os.environ["GOALFLIGHT_STATE_DIR"] = str(root / "state")
            os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
            os.environ[goalflight_worktree_pool.WORKTREE_SEATS_ENV] = "1"
            repo = make_repo(root)
            status_path = repo / "status.json"
            args = runner_args(repo, "acp-run-contract", status_path)
            spawn_calls: list[dict] = []

            async def capture_spawn(*_args, **kwargs):
                spawn_calls.append(kwargs)
                return FakeProc(), FakeConn()

            goalflight_acp_run.agent_command = lambda _agent, model=None, fast=False: ("fake-agent", [])
            goalflight_acp_run.validate_acp_dispatch_readiness = lambda _agent, _cmd: None
            goalflight_acp_run.validate_os_sandbox_request = lambda _agent, _profile: None
            goalflight_acp_run.preflight_os_sandbox = lambda _profile: None
            goalflight_acp_run.cleanup_ghosts = lambda: None
            goalflight_acp_run.spawn_and_handshake_with_retry = capture_spawn
            goalflight_acp_run.run_prompt = fake_run_prompt

            payload = asyncio.run(goalflight_acp_run.run(args))
            status = json.loads(status_path.read_text())
            # Worktree path is built from repo.resolve() in the runner (macOS
            # /var/folders → /private/var/folders resolution); compare against
            # the same.
            worktree_path = repo.resolve() / "worktrees" / "wt-1"
            assert_true("runner complete", payload["state"] == "complete")
            assert_true("status worktree path", status["worktree_path"] == str(worktree_path))
            assert_true("status worker cwd", status["worker_cwd"] == str(worktree_path))
            assert_true("status project root", status["project_root"] == str(repo.resolve()))
            assert_true("worktree left on disk", worktree_path.is_dir())
            assert_true("worker spawned in worktree", spawn_calls[0]["cwd"] == str(worktree_path))
            inherited_fds = spawn_calls[0]["pass_fds"]
            assert_true("worker inherits seat lock", len(inherited_fds) == 1)

            state = goalflight_capacity.load_state()
            leases = list(state.get("leases", {}).values())
            lease = next(lease for lease in leases if lease.get("dispatch_id") == "acp-run-contract")
            assert_true("capacity preserves project root", lease["project_root"] == str(repo.resolve()))
            assert_true("capacity records worker cwd", lease["worker_cwd"] == str(worktree_path))
            assert_true("capacity records worktree path", lease["worktree_path"] == str(worktree_path))
    finally:
        for name, value in patches.items():
            setattr(goalflight_acp_run, name, value)
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
        if old_seats is None:
            os.environ.pop(goalflight_worktree_pool.WORKTREE_SEATS_ENV, None)
        else:
            os.environ[goalflight_worktree_pool.WORKTREE_SEATS_ENV] = old_seats


def test_capacity_denied_does_not_create_worktree() -> None:
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_agent_command = goalflight_acp_run.agent_command
    old_readiness = goalflight_acp_run.validate_acp_dispatch_readiness
    old_sandbox = goalflight_acp_run.validate_os_sandbox_request
    old_acquire = goalflight_capacity.cmd_acquire

    def deny_capacity(_args: argparse.Namespace) -> int:
        print(json.dumps({"decision": "wait", "reason": "test_capacity"}))
        return 2

    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            os.environ["GOALFLIGHT_STATE_DIR"] = str(root / "state")
            os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
            repo = make_repo(root)
            status_path = repo / "capacity-denied-status.json"
            args = runner_args(repo, "acp-capacity-denied", status_path)

            goalflight_acp_run.agent_command = lambda _agent, model=None, fast=False: ("fake-agent", [])
            goalflight_acp_run.validate_acp_dispatch_readiness = lambda _agent, _cmd: None
            goalflight_acp_run.validate_os_sandbox_request = lambda _agent, _profile: None
            goalflight_capacity.cmd_acquire = deny_capacity

            payload = asyncio.run(goalflight_acp_run.run(args))
            assert_true("capacity blocked", payload["state"] == "blocked_capacity")
            assert_true(
                "worktree not created on capacity block",
                not (repo / "worktrees").exists(),
            )
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_acp_run.validate_acp_dispatch_readiness = old_readiness
        goalflight_acp_run.validate_os_sandbox_request = old_sandbox
        goalflight_capacity.cmd_acquire = old_acquire
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir


def test_runner_worktree_create_failure_writes_failed_status() -> None:
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_seats = os.environ.get(goalflight_worktree_pool.WORKTREE_SEATS_ENV)
    old_agent_command = goalflight_acp_run.agent_command
    old_readiness = goalflight_acp_run.validate_acp_dispatch_readiness
    old_sandbox = goalflight_acp_run.validate_os_sandbox_request
    old_preflight = goalflight_acp_run.preflight_os_sandbox
    old_spawn = goalflight_acp_run.spawn_and_handshake_with_retry
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            os.environ["GOALFLIGHT_STATE_DIR"] = str(root / "state")
            os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
            os.environ[goalflight_worktree_pool.WORKTREE_SEATS_ENV] = "1"
            repo = make_repo(root)
            existing = repo / "worktrees" / "wt-1"
            existing.mkdir(parents=True)
            status_path = repo / "existing-status.json"
            args = runner_args(repo, "acp-existing", status_path)
            spawn_calls: list[dict] = []

            async def capture_spawn(*_args, **kwargs):
                spawn_calls.append(kwargs)
                return FakeProc(), FakeConn()

            goalflight_acp_run.agent_command = lambda _agent, model=None, fast=False: ("fake-agent", [])
            goalflight_acp_run.validate_acp_dispatch_readiness = lambda _agent, _cmd: None
            goalflight_acp_run.validate_os_sandbox_request = lambda _agent, _profile: None
            goalflight_acp_run.preflight_os_sandbox = lambda _profile: None
            goalflight_acp_run.spawn_and_handshake_with_retry = capture_spawn

            payload = asyncio.run(goalflight_acp_run.run(args))
            status = json.loads(status_path.read_text())
            assert_true("worktree create failure state", payload["state"] == "failed_worktree")
            assert_true("status failed worktree", status["state"] == "failed_worktree")
            assert_true("worker not spawned", spawn_calls == [])
            state = goalflight_capacity.load_state()
            lease = next(lease for lease in state.get("leases", {}).values() if lease.get("dispatch_id") == "acp-existing")
            assert_true("lease released as failed_worktree", lease["state"] == "failed_worktree")
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_acp_run.validate_acp_dispatch_readiness = old_readiness
        goalflight_acp_run.validate_os_sandbox_request = old_sandbox
        goalflight_acp_run.preflight_os_sandbox = old_preflight
        goalflight_acp_run.spawn_and_handshake_with_retry = old_spawn
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
        if old_seats is None:
            os.environ.pop(goalflight_worktree_pool.WORKTREE_SEATS_ENV, None)
        else:
            os.environ[goalflight_worktree_pool.WORKTREE_SEATS_ENV] = old_seats


def main() -> None:
    tests = [
        test_worktree_create_routes_distinct_cwds_and_stale_probe,
        test_worktree_root_symlink_is_rejected,
        test_worktree_leaf_symlink_is_rejected,
        test_doctor_capacity_unknown_does_not_mark_stale,
        test_doctor_flags_registered_leaf_escape,
        test_doctor_flags_orphaned_blocking_path,
        test_execute_parallel_docs_require_worktree_create,
        test_runner_worktree_status_and_capacity_contract,
        test_capacity_denied_does_not_create_worktree,
        test_runner_worktree_create_failure_writes_failed_status,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
