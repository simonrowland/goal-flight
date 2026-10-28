#!/usr/bin/env python3
"""Focused dispatcher tests for first-class ACP agents."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_run  # noqa: E402
import goalflight_dispatch as dispatch_mod  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_liveness  # noqa: E402
import goalflight_os_sandbox  # noqa: E402
import goalflight_reap_dispatch_homes as retention  # noqa: E402
import goalflight_watch  # noqa: E402
import goalflight_worktree_pool  # noqa: E402


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _normalize(agent: str) -> str:
    args = SimpleNamespace(agent=agent)
    dispatch_mod._normalize_acp_agent(args)
    return args.agent


def test_normalize_acp_agents() -> None:
    assert _normalize("worker") == "codex-acp"
    assert _normalize("codex") == "codex-acp"
    assert _normalize("codex-acp") == "codex-acp"
    assert _normalize("cursor") == "cursor"
    assert _normalize("cursor-agent") == "cursor"
    assert _normalize("claude") == "claude"
    assert _normalize("claude-acp") == "claude"
    assert _normalize("claude-code-cli-acp") == "claude"
    assert _normalize("grok-acp") == "grok-acp"

    try:
        _normalize("not-real")
    except dispatch_mod.DispatchUsageError as exc:
        assert "codex-acp, grok-acp, cursor, or claude-acp" in str(exc)
    else:
        raise AssertionError("bogus ACP agent did not raise")


def _base_acp_args(tmp: Path, *, agent: str, dispatch_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        agent=agent,
        model=None,
        prompt_file=None,
        cwd=str(tmp),
        read_only=False,
        prompt="COMPLETE: no-op",
        max_idle_secs="300",
        poll_secs="0.1",
        dispatch_id=dispatch_id,
        status_json=None,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        billing="sub",
        tail=None,
        priority="normal",
        capacity_wait_s=None,
    )


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "goalflight-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Goal Flight Test"], cwd=repo, check=True
    )
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def test_build_acp_cfg_agent_liveness_defaults() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for agent in ("cursor", "claude"):
            args = _base_acp_args(tmp, agent=agent, dispatch_id=f"{agent}-cfg")
            cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / f"{agent}.json")
            assert cfg.agent == agent
            assert dispatch_mod.SEARCH_SCOPE_PREAMBLE in cfg.prompt_text
            assert cfg.liveness_profile == "remote_api"

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="codex-cfg")
        cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / "codex.json")
        assert cfg.agent == "codex-acp"
        assert dispatch_mod.SEARCH_SCOPE_PREAMBLE in cfg.prompt_text
        assert dispatch_mod.WORKER_ONLY_PREAMBLE in cfg.prompt_text
        assert dispatch_mod.SCOPE_GUARD_PREAMBLE in cfg.prompt_text
        assert dispatch_mod.INSTRUCTION_REFUSAL_PREAMBLE in cfg.prompt_text
        assert "Verification named in the brief" in cfg.prompt_text
        # ACP's applied sandbox is decided after this config is built.
        assert "Sandbox limits for this dispatch" not in cfg.prompt_text
        assert cfg.liveness_profile is None

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="priority-cfg")
        args.priority = "bulk"
        args.capacity_wait_s = 12.5
        args.account = "explicit-seat"
        cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / "priority.json")
        assert cfg.priority == "bulk"
        assert cfg.capacity_wait_s == 12.5
        assert cfg.account == "explicit-seat"

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="resume-cfg")
        args.parent_dispatch_id = "parent-dispatch"
        resumed = dispatch_mod._build_acp_cfg(
            args, status_json=tmp / "resume.json",
        )
        assert resumed.prompt_text.startswith(
            dispatch_mod.WORKER_ONLY_PREAMBLE
            + "\n\n"
            + dispatch_mod.RESUME_CONTRACT_SUPERSESSION_PREAMBLE
        )


def test_macos_codex_acp_read_only_uses_shared_checkout() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="codex-ro")
        args.read_only = True
        args.worker = []
        args.shape = "acp"
        with patch.object(goalflight_acp_run.goalflight_compat, "is_macos", return_value=True):
            assert goalflight_acp_run.acp_permission_read_only_supported("codex-acp")
            cfg = dispatch_mod._build_acp_cfg(
                args, status_json=tmp / "codex-ro.json"
            )
        assert cfg.worktree == "shared-read-only"


def test_macos_codex_acp_read_only_in_place_uses_project_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="codex-ro-in-place")
        args.read_only = True
        args.in_place = True
        args.worker = []
        args.shape = "acp"
        with patch.object(goalflight_acp_run.goalflight_compat, "is_macos", return_value=True):
            assert goalflight_acp_run.acp_permission_read_only_supported("codex-acp")
            cfg = dispatch_mod._build_acp_cfg(
                args, status_json=tmp / "codex-ro-in-place.json"
            )
        assert cfg.worktree == "off"
        assert Path(cfg.cwd).resolve() == tmp.resolve()


def test_read_only_acp_resume_rejects_rebound_checkout_before_spawn() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = _make_repo(tmp)

        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", *args],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()

        recorded_head = git("rev-parse", "HEAD")
        parent_id = "acp-rebound-parent"
        git("branch", f"worktree/{parent_id}", recorded_head)
        recorded_checkout = tmp / "recorded-checkout"
        git("worktree", "add", str(recorded_checkout), f"worktree/{parent_id}")
        (repo / "tracked.txt").write_text("advanced\n", encoding="utf-8")
        git("add", "tracked.txt")
        git("commit", "-m", "advance main")

        state_dir = tmp / "state"
        record = {
            "schema": dispatch_mod.goalflight_ledger.SCHEMA,
            "dispatch_id": parent_id,
            "agent": "claude",
            "engine": "claude",
            "shape": "acp",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(recorded_checkout),
            "worktree_id": recorded_checkout.name,
            "worktree_path": str(recorded_checkout),
            "worktree_branch": f"worktree/{parent_id}",
            "worktree_head": recorded_head,
            "engine_session_id": "00000000-0000-4000-8000-000000000033",
        }
        args = _base_acp_args(tmp, agent="claude", dispatch_id="acp-rebound-child")
        args.cwd = str(repo)
        args.parent_dispatch_id = parent_id
        args.read_only = True
        args.worker = []
        args.shape = "acp"

        with (
            patch.dict(os.environ, _capacity_env(state_dir), clear=True),
            patch.object(
                goalflight_acp_run,
                "acp_permission_read_only_supported",
                return_value=True,
            ),
        ):
            dispatch_mod.goalflight_ledger.write_record(record)
            cfg = dispatch_mod._build_acp_cfg(
                args, status_json=tmp / "acp-rebound.status.json"
            )
            assert cfg.worktree == "shared-read-only"
            try:
                dispatch_mod._revalidate_read_only_resume_worktree(cfg)
            except dispatch_mod.DispatchUsageError as exc:
                assert "actual branch main" in str(exc)
                assert f"expected branch worktree/{parent_id}" in str(exc)
            else:
                raise AssertionError("rebound read-only ACP checkout was accepted")


def test_acp_help_describes_repository_worktree_pool() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "goalflight_acp_run.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "repository-scoped" in proc.stdout, proc.stdout
    assert "worktrees/s-N" in proc.stdout, proc.stdout
    assert "lazy seat" not in proc.stdout, proc.stdout
    assert "wt-1..wt-N" not in proc.stdout, proc.stdout


def test_direct_acp_entry_checks_floor_before_runner(
    tmp_path: Path, monkeypatch
) -> None:
    called = []
    monkeypatch.setattr(dispatch_mod, "_admit_dispatch_disk_floor", lambda: False)
    monkeypatch.setattr(
        goalflight_acp_run,
        "run_acp_dispatch",
        lambda *_args, **_kwargs: called.append(True),
    )

    result = goalflight_acp_run.main(
        ["--agent", "fake-acp", "--cwd", str(tmp_path), "--dispatch-id", "low-space"]
    )

    assert result == 75
    assert called == []


def test_dispatcher_bound_acp_uses_one_seat_and_records_actual_cwd() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = _make_repo(tmp)
        state_dir = tmp / "state"
        status_json = tmp / "acp-single-bind.status.json"
        env = _capacity_env(
            state_dir,
            GOALFLIGHT_WORKTREE_SEATS="2",
            GOALFLIGHT_CAPACITY_WAIT_S="0",
        )
        env.pop(goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, None)
        env.pop(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, None)
        args = _base_acp_args(
            repo,
            agent="fake-acp",
            dispatch_id="acp-single-bind",
        )
        args.worktree = "HEAD"
        args.cwd = None
        args.in_place = False
        args.controller_label = None
        args.unregistered_forced = True
        args.launch_detached = False
        args.acp_detached_child = False
        args._worktree_seat = None
        args.status_json = str(status_json)
        args.tail = str(tmp / "acp-single-bind.tail")

        saved = _install_fake_acp_after_capacity()
        fake_spawn = goalflight_acp_run.spawn_and_handshake_with_retry
        runner_spawn: dict[str, object] = {}

        async def capture_spawn(*spawn_args, **spawn_kwargs):
            runner_spawn["cwd"] = spawn_kwargs["cwd"]
            runner_spawn["pass_fds"] = spawn_kwargs["pass_fds"]
            runner_spawn["env"] = spawn_kwargs["env"]
            assert dispatch_mod.goalflight_ledger.record_path(
                args.dispatch_id
            ).is_file()
            return await fake_spawn(*spawn_args, **spawn_kwargs)

        goalflight_acp_run.spawn_and_handshake_with_retry = capture_spawn
        try:
            with contextlib.chdir(repo), patch.dict(os.environ, env, clear=True):
                outer = dispatch_mod._bind_dispatch_worktree(args)
                assert outer is not None
                seat = outer.path
                seat_fd = outer.fileno()
                rc = dispatch_mod._run_acp_shape(
                    args,
                    base=tmp / "dispatch",
                    account_env={},
                )
                ledger_path = dispatch_mod.goalflight_ledger.record_path(
                    args.dispatch_id
                )
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        finally:
            _restore_fake_acp(saved)
            if args._worktree_seat is not None:
                args._worktree_seat.release()

        assert rc == 0
        assert ledger["project_root"] == str(repo.resolve())
        assert ledger["worker_cwd"] == str(seat.resolve())
        assert runner_spawn["cwd"] == str(seat.resolve())
        assert seat_fd in runner_spawn["pass_fds"]
        assert len(runner_spawn["pass_fds"]) == 2
        assert not (seat.parent / "s-2").exists()
        worker_env = runner_spawn["env"]
        assert isinstance(worker_env, dict)
        scratch = Path(worker_env["GOALFLIGHT_SCRATCH_DIR"])
        assert scratch.is_dir()
        assert worker_env["TMPDIR"] == str(scratch)


def _assert_acp_runner_hands_off_admitted_seat(worktree_mode: str) -> None:
    class FakeSeat:
        def __init__(self, root: Path) -> None:
            self.path = root / "standalone-seat"
            self.path.mkdir()
            self.seat_name = "s-1"
            self.branch = "goalflight/dispatch/standalone-acp-create"
            self.quarantine_branch = None
            self._lock = (root / "standalone-seat.lock").open("w+")

        def fileno(self) -> int:
            return self._lock.fileno()

        def release(self) -> None:
            self._lock.close()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / f"acp-admitted-seat-{worktree_mode}.status.json"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=f"acp-admitted-seat-{worktree_mode}",
            status_json=status_json,
            capacity_wait_s=0.0,
        )
        cfg.worktree = worktree_mode
        cfg.worktree_base = "HEAD" if worktree_mode == "create" else None
        cfg.in_place = False
        fake_seat = FakeSeat(tmp)
        seat_fd = fake_seat.fileno()
        spawn_details: dict[str, object] = {}
        env = _capacity_env(state_dir)
        env.pop(goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, None)
        env.pop(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, None)
        saved = _install_fake_acp_after_capacity()
        fake_spawn = goalflight_acp_run.spawn_and_handshake_with_retry

        async def capture_spawn(*spawn_args, **spawn_kwargs):
            spawn_details["cwd"] = spawn_kwargs["cwd"]
            spawn_details["pass_fds"] = spawn_kwargs["pass_fds"]
            spawn_details["env"] = spawn_kwargs["env"]
            return await fake_spawn(*spawn_args, **spawn_kwargs)

        goalflight_acp_run.spawn_and_handshake_with_retry = capture_spawn
        try:
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(dispatch_mod, "_admit_dispatch_worktree", return_value=fake_seat) as central_admit,
            ):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
                ledger = json.loads(
                    dispatch_mod.goalflight_ledger.record_path(
                        cfg.dispatch_id
                    ).read_text(encoding="utf-8")
                )
        finally:
            _restore_fake_acp(saved)
            if not fake_seat._lock.closed:
                fake_seat.release()

        assert payload["state"] == "complete"
        assert central_admit.call_count == 1
        assert payload["worker_cwd"] == str(fake_seat.path)
        assert spawn_details["cwd"] == str(fake_seat.path)
        assert seat_fd in spawn_details["pass_fds"]
        assert spawn_details["env"][goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV] == str(
            seat_fd
        )
        assert ledger["worker_cwd"] == str(fake_seat.path)


def test_acp_runner_hands_off_any_admitted_seat() -> None:
    for worktree_mode in ("create", "off"):
        _assert_acp_runner_hands_off_admitted_seat(worktree_mode)


def test_acp_runner_uses_checkout_selected_during_read_only_admission() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        old_checkout = tmp / "old-checkout"
        admitted_checkout = tmp / "admitted-shared-checkout"
        old_checkout.mkdir()
        admitted_checkout.mkdir()
        cfg = _acp_cfg(
            tmp,
            dispatch_id="acp-read-only-admitted-cwd",
            status_json=tmp / "acp-read-only-admitted-cwd.status.json",
            capacity_wait_s=0.0,
        )
        cfg.cwd = str(old_checkout)
        cfg.worktree = "off"
        cfg.in_place = True
        cfg.read_only = True
        spawn_details: dict[str, object] = {}

        def admit_read_only_checkout(current) -> None:
            current.cwd = str(admitted_checkout)
            current._worktree_id = admitted_checkout.name
            current._worktree_path = str(admitted_checkout)
            current._worktree_read_only = True
            current._worktree_read_only_hold = None
            return None

        saved = _install_fake_acp_after_capacity()
        fake_spawn = goalflight_acp_run.spawn_and_handshake_with_retry

        async def capture_spawn(*spawn_args, **spawn_kwargs):
            spawn_details["cwd"] = spawn_kwargs["cwd"]
            return await fake_spawn(*spawn_args, **spawn_kwargs)

        goalflight_acp_run.spawn_and_handshake_with_retry = capture_spawn
        env = _capacity_env(state_dir)
        env.pop(goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, None)
        env.pop(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, None)
        try:
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    dispatch_mod,
                    "_admit_dispatch_worktree",
                    admit_read_only_checkout,
                ),
            ):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
                ledger = json.loads(
                    dispatch_mod.goalflight_ledger.record_path(
                        cfg.dispatch_id
                    ).read_text(encoding="utf-8")
                )
        finally:
            _restore_fake_acp(saved)

        assert payload["state"] == "complete"
        assert payload["worker_cwd"] == str(admitted_checkout)
        assert spawn_details["cwd"] == str(admitted_checkout)
        assert ledger["worker_cwd"] == str(admitted_checkout)


def test_detached_acp_child_inherits_outer_seat_fd_and_cwd() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = _make_repo(tmp)
        parent_lease = goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "detached-acp-seat",
            controller_label="controller",
        )
        seat = parent_lease.path
        lock_fd = parent_lease.fileno()
        released = False

        def release() -> None:
            nonlocal released
            parent_lease.release()
            released = True

        lease = SimpleNamespace(path=seat, fileno=lambda: lock_fd, release=release)
        args = SimpleNamespace(
            _original_argv=[
                "--agent",
                "fake-acp",
                "--shape",
                "acp",
                "--worktree",
                "HEAD",
                "--prompt",
                "test",
            ],
            _worktree_seat=lease,
            agent="fake-acp",
            background_default_notice=False,
            cwd=str(seat),
            project_root=str(repo),
            dispatch_id="detached-acp-seat",
            queue_launch_token=None,
            unregistered_forced=True,
        )
        captured: dict[str, object] = {}

        def spawn(argv, **kwargs):
            captured["argv"] = argv
            captured["fd_env"] = kwargs["env"].get(
                goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV
            )
            captured["pass_fds"] = goalflight_worktree_pool.pass_worktree_lock_fds(
                kwargs["env"]
            )
            child_args = _base_acp_args(
                repo,
                agent="fake-acp",
                dispatch_id="detached-acp-seat-child",
            )
            child_args.cwd = str(seat)
            child_args.project_root = str(repo)
            child_args.worktree = "HEAD"
            child_args._worktree_seat = None
            child_args.unregistered_forced = True
            with patch.dict(os.environ, kwargs["env"], clear=True):
                child_cfg = dispatch_mod._build_acp_cfg(
                    child_args,
                    status_json=tmp / "child.status.json",
                    base=tmp / "dispatch",
                )
                captured["child_cwd"] = child_cfg.cwd
                captured["child_worktree"] = child_cfg.worktree
                captured["child_payload"] = asyncio.run(
                    goalflight_acp_run.run_acp_dispatch(child_cfg)
                )
            return 424242

        record = {"worker_pid": 424243, "queue_launch_token": None}
        clean_env = _capacity_env(tmp / "state")
        clean_env.pop(goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, None)
        clean_env.pop(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, None)
        saved = _install_fake_acp_after_capacity()
        try:
            with (
                patch.dict(os.environ, clean_env, clear=True),
                patch.object(dispatch_mod, "_spawn_daemonized_process", side_effect=spawn),
                patch.object(dispatch_mod, "_apply_web_qa_env"),
                patch.object(dispatch_mod, "_find_dispatch_record", return_value=record),
                patch.object(dispatch_mod, "_mark_queue_claim_worker_spawn_intent"),
                patch.object(dispatch_mod, "_mark_queue_claim_worker_spawned"),
                patch.object(dispatch_mod, "_release_worktree_occupancy_lock"),
            ):
                rc = dispatch_mod._run_acp_detached_launcher(
                    args,
                    status_json=tmp / "status.json",
                    tail_path=tmp / "tail.log",
                    account_env={},
                    env_remove=[],
                    capacity_wait_s=0,
                )
        finally:
            _restore_fake_acp(saved)
            if not released:
                lease.release()

        child_argv = captured["argv"]
        assert rc == 0
        assert Path(child_argv[child_argv.index("--cwd") + 1]) == seat.resolve()
        assert captured["fd_env"] == str(lock_fd)
        assert captured["pass_fds"] == (lock_fd,)
        assert captured["child_cwd"] == str(seat.resolve())
        assert captured["child_worktree"] == "off"
        assert captured["child_payload"]["state"] == "complete"
        assert released
        assert args._worktree_seat is None


def test_detached_acp_launcher_propagates_child_dispatch_refusal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    status_json = tmp_path / "resume.status.json"
    reason = "resume source no longer exists"
    args = SimpleNamespace(
        _original_argv=["--agent", "claude", "--dispatch-id", "resume-child"],
        _worktree_seat=None,
        agent="claude",
        background_default_notice=False,
        dispatch_id="resume-child",
        queue_launch_token="queue-token",
    )

    def spawn(_argv, **_kwargs):
        status_json.write_text(
            json.dumps(
                {
                    "state": "failed_worktree",
                    "dispatch_refused": True,
                    "error": reason,
                }
            ),
            encoding="utf-8",
        )
        return 424242

    monkeypatch.setattr(dispatch_mod, "_spawn_daemonized_process", spawn)
    monkeypatch.setattr(dispatch_mod, "_apply_web_qa_env", lambda *_args: None)
    monkeypatch.setattr(dispatch_mod, "_find_dispatch_record", lambda *_args: None)
    monkeypatch.setattr(
        dispatch_mod.goalflight_compat, "pid_alive", lambda _pid: False
    )
    monkeypatch.setattr(
        dispatch_mod, "_release_worktree_occupancy_lock", lambda *_args: None
    )
    monkeypatch.setattr(
        dispatch_mod, "_release_redispatch_store_lock", lambda *_args: None
    )
    monkeypatch.setattr(
        dispatch_mod,
        "_release_read_only_resume_reservation",
        lambda *_args: None,
    )

    rc = dispatch_mod._run_acp_detached_launcher(
        args,
        status_json=status_json,
        tail_path=tmp_path / "resume.tail",
        account_env={},
        env_remove=[],
        capacity_wait_s=0,
    )

    assert rc == 64
    assert reason in capsys.readouterr().err


def test_build_acp_cfg_injects_orientation_prompt_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prompt = tmp / "prompt.md"
        prompt.write_text("Do ACP work.\n", encoding="utf-8")
        orientation = tmp / "docs-private" / "rag" / "ORIENTATION.md"
        orientation.parent.mkdir(parents=True)
        orientation.write_text("project orientation\n", encoding="utf-8")

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="orientation-acp")
        args.prompt = None
        args.prompt_file = str(prompt)
        cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / "orientation.json")

        assert cfg.prompt is None
        assert cfg.original_prompt_file == str(prompt.resolve())
        assert "PROJECT ORIENTATION\n" in cfg.prompt_text
        assert f"Path: {orientation.resolve()}" in cfg.prompt_text
        assert dispatch_mod.PROJECT_ORIENTATION_SCOPE_RULE in cfg.prompt_text
        assert "Do ACP work." in cfg.prompt_text
        assert dispatch_mod.SEARCH_SCOPE_PREAMBLE in cfg.prompt_text

        args.no_orientation = True
        suppressed = dispatch_mod._build_acp_cfg(args, status_json=tmp / "suppressed.json")
        assert suppressed.prompt is None
        assert suppressed.prompt_text == (
            f"{dispatch_mod.WORKER_ONLY_PREAMBLE}\n\n"
            f"{dispatch_mod.SEARCH_SCOPE_PREAMBLE}\n\n"
            f"{dispatch_mod.SCOPE_GUARD_PREAMBLE}\n\n"
            f"{dispatch_mod.INSTRUCTION_REFUSAL_PREAMBLE}\n\nDo ACP work.\n"
        )
        assert suppressed.original_prompt_file == str(prompt.resolve())


def test_build_acp_cfg_includes_remote_ci_instruction_from_project_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path
    config = project_root / ".goal-flight" / "remote-ci.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"runner": {
        "heavy_command_ids": ["acp-project-gate"]
    }}), encoding="utf-8")
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do remote ACP work.\n", encoding="utf-8")
    args = _base_acp_args(tmp_path, agent="codex-acp", dispatch_id="remote-acp")
    args.prompt = None
    args.prompt_file = str(prompt)

    cfg = dispatch_mod._build_acp_cfg(
        args, status_json=tmp_path / "dispatch" / "remote-acp.status.json"
    )

    assert cfg.prompt_text.count("Remote CI: run test suites and other heavy CPU") == 1
    assert '"$GOALFLIGHT_REMOTE_RUN" --kind gate --cmd-id acp-project-gate' in cfg.prompt_text
    assert "Do remote ACP work." in cfg.prompt_text


def test_acp_production_path_persists_and_reminds_with_assembled_prompt() -> None:
    captured: dict[str, object] = {}
    old_run = goalflight_acp_run.run_acp_dispatch

    async def fake_run(cfg):
        captured["cfg"] = cfg
        return {
            "state": "complete",
            "dispatch_id": cfg.dispatch_id,
            "agent": cfg.agent,
            "worker_pid": 4242,
            "worker_alive": False,
        }

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dispatch_dir = tmp / "dispatch"
        prompt = tmp / "brief.md"
        prompt.write_text("Do ACP production work.\n", encoding="utf-8")
        orientation = tmp / "docs-private" / "rag" / "ORIENTATION.md"
        orientation.parent.mkdir(parents=True)
        orientation.write_text("project orientation\n", encoding="utf-8")
        status_json = dispatch_dir / "acp-production.status.json"

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="acp-production")
        args.prompt = None
        args.prompt_file = str(prompt)
        args.status_json = str(status_json)
        args.tail = str(dispatch_dir / "acp-production.tail")
        old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
        old_dispatch_dir = os.environ.get("GOALFLIGHT_DISPATCH_DIR")
        stderr = io.StringIO()
        try:
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
            os.environ["GOALFLIGHT_DISPATCH_DIR"] = str(dispatch_dir)
            goalflight_acp_run.run_acp_dispatch = fake_run
            with contextlib.redirect_stderr(stderr):
                rc = dispatch_mod._run_acp_shape(
                    args,
                    base=dispatch_dir,
                    account_env={},
                )
        finally:
            goalflight_acp_run.run_acp_dispatch = old_run
            if old_state is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state
            if old_dispatch_dir is None:
                os.environ.pop("GOALFLIGHT_DISPATCH_DIR", None)
            else:
                os.environ["GOALFLIGHT_DISPATCH_DIR"] = old_dispatch_dir

        assert rc == 0
        cfg = captured["cfg"]
        watcher_prompt = Path(cfg.watcher_prompt_file)
        assert watcher_prompt.parent == status_json.parent.resolve()
        assert watcher_prompt.is_file()
        assert _mode(watcher_prompt) == 0o600
        assert _mode(watcher_prompt.parent) == 0o700
        persisted = watcher_prompt.read_text(encoding="utf-8")
        assert dispatch_mod.PROMPT_FILE_PREAMBLE in persisted
        assert "PROJECT ORIENTATION\n" in persisted
        assert f"Path: {orientation.resolve()}" in persisted
        assert "Do ACP production work." in persisted
        # The post-dispatch reminder is one line now: dispatch id + status path.
        # The watcher invocation that names --ignore-prompt-file moved behind
        # --hints, because repeating it on every launch turned a real guard rail
        # into wallpaper. Assert BOTH halves so neither can silently regress:
        # the terse default, and the hinted block still naming the prompt file.
        assert "[goal-flight] dispatched" in stderr.getvalue()
        assert f"--ignore-prompt-file {watcher_prompt}" not in stderr.getvalue()
        hinted = "\n".join(
            dispatch_mod._status_reminder_lines(
                "acp-agents",
                status_json=status_json,
                tail_path=status_json.with_suffix(".tail"),
                worker_pid=4321,
                shape="acp",
                prompt_path=watcher_prompt,
                hints=True,
            )
        )
        assert f"--ignore-prompt-file {watcher_prompt}" in hinted


def test_acp_inline_prompt_uses_same_assembled_prompt_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        status_json = tmp / "dispatch" / "inline-acp.status.json"
        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="inline-acp")
        args.prompt = "inline ACP task with !COMPLETE: inline-acp — example"

        cfg = dispatch_mod._build_acp_cfg(
            args,
            status_json=status_json,
            base=tmp / "dispatch",
        )

        watcher_prompt = Path(cfg.watcher_prompt_file)
        assert watcher_prompt == status_json.resolve().with_name(
            "inline-acp.assembled.prompt"
        )
        assert watcher_prompt.read_text(encoding="utf-8") == (
            f"{dispatch_mod.PROMPT_FILE_PREAMBLE}\n\n"
            f"{dispatch_mod.WORKER_ONLY_PREAMBLE}\n\n"
            f"{dispatch_mod.SEARCH_SCOPE_PREAMBLE}\n\n"
            f"{dispatch_mod.SCOPE_GUARD_PREAMBLE}\n\n"
            f"{dispatch_mod.INSTRUCTION_REFUSAL_PREAMBLE}\n\n"
            f"{args.prompt}"
        )


def test_acp_prompt_history_rewrite_remains_private() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "dispatch" / "private.assembled.prompt"
        cfg = SimpleNamespace(watcher_prompt_file=str(path))

        assert (
            goalflight_acp_run._persist_watcher_prompt_turn(cfg, "first")
            == path.resolve()
        )
        assert path.read_text(encoding="utf-8") == "first"
        assert _mode(path) == 0o600
        assert _mode(path.parent) == 0o700

        path.chmod(0o644)
        goalflight_acp_run._persist_watcher_prompt_turn(cfg, "replacement")
        assert path.read_text(encoding="utf-8") == "replacement"
        assert _mode(path) == 0o600


def test_dispatch_help_skips_legacy_prompt_sweep_mutation_pair() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatch"
        dispatch_dir.mkdir(parents=True, mode=0o755)
        dispatch_dir.chmod(0o755)
        legacy_prompt = dispatch_dir / "legacy.assembled.prompt"
        legacy_prompt.write_text("legacy private prompt", encoding="utf-8")
        legacy_prompt.chmod(0o644)

        with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    dispatch_mod.main(["--help"])
                except SystemExit as exc:
                    assert exc.code == 0
                else:  # pragma: no cover - argparse --help always exits
                    raise AssertionError("dispatcher --help did not exit")

        assert _mode(dispatch_dir) == 0o755
        assert _mode(legacy_prompt) == 0o644
        assert not (dispatch_dir / dispatch_mod.LEGACY_PROMPT_SWEEP_MARKER).exists()


def test_dispatch_startup_sweep_is_once_and_best_effort_mutation_pair() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatch"
        dispatch_dir.mkdir(parents=True, mode=0o755)
        dispatch_dir.chmod(0o755)
        repaired_prompt = dispatch_dir / "repaired.assembled.prompt"
        repaired_prompt.write_text("repair me", encoding="utf-8")
        repaired_prompt.chmod(0o644)
        error_prompt = dispatch_dir / "error.assembled.prompt"
        error_prompt.write_text("constructed chmod failure", encoding="utf-8")
        error_prompt.chmod(0o644)
        original_chmod = Path.chmod
        chmod_errors = 0

        def selective_chmod(path: Path, mode: int, *args, **kwargs):
            nonlocal chmod_errors
            if path == error_prompt:
                chmod_errors += 1
                raise PermissionError("constructed sidecar chmod failure")
            return original_chmod(path, mode, *args, **kwargs)

        stderr = io.StringIO()
        with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
            with patch.object(Path, "chmod", selective_chmod):
                with contextlib.redirect_stderr(stderr):
                    assert dispatch_mod._prepare_private_dispatch_dir() == dispatch_dir

            assert _mode(dispatch_dir) == 0o700
            assert _mode(repaired_prompt) == 0o600
            assert _mode(error_prompt) == 0o644
            marker = dispatch_dir / dispatch_mod.LEGACY_PROMPT_SWEEP_MARKER
            assert marker.is_file()
            assert "constructed sidecar chmod failure" in stderr.getvalue()

            # Mutation control: without the marker guard, the second call would
            # repair this deliberately re-loosened file and retry the error.
            repaired_prompt.chmod(0o644)
            with patch.object(Path, "chmod", selective_chmod):
                assert dispatch_mod._prepare_private_dispatch_dir() == dispatch_dir

        assert _mode(repaired_prompt) == 0o644
        assert chmod_errors == 1


def test_dispatch_startup_sweep_marker_serializes_concurrent_first_run() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatch"
        dispatch_dir.mkdir(parents=True)
        legacy_prompt = dispatch_dir / "concurrent.assembled.prompt"
        legacy_prompt.write_text("repair once", encoding="utf-8")
        legacy_prompt.chmod(0o644)
        original_chmod = Path.chmod
        prompt_chmods = 0
        count_lock = threading.Lock()
        start = threading.Barrier(3)
        outcomes: list[Path] = []

        def counted_chmod(path: Path, mode: int, *args, **kwargs):
            nonlocal prompt_chmods
            if path == legacy_prompt:
                with count_lock:
                    prompt_chmods += 1
                time.sleep(0.05)
            return original_chmod(path, mode, *args, **kwargs)

        def prepare() -> None:
            start.wait()
            outcomes.append(dispatch_mod._prepare_private_dispatch_dir())

        with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
            with patch.object(Path, "chmod", counted_chmod):
                workers = [threading.Thread(target=prepare) for _ in range(2)]
                for worker in workers:
                    worker.start()
                start.wait()
                for worker in workers:
                    worker.join(timeout=5)
                    assert not worker.is_alive()

        assert outcomes == [dispatch_dir, dispatch_dir]
        assert prompt_chmods == 1
        assert _mode(legacy_prompt) == 0o600


def test_watcher_with_retired_prompt_and_status_exits_without_status_write() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "retired.tail"
        status_json = tmp / "retired.status.json"
        prompt = dispatch_mod._acp_watcher_prompt_path(status_json)
        tail.write_text("stale worker output\n", encoding="utf-8")
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
        env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
        env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
        env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
        env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")

        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_watch.py"),
                "--pid",
                "99999999",
                "--tail",
                str(tail),
                "--status-json",
                str(status_json),
                "--dispatch-id",
                "retired-startup",
                "--ignore-prompt-file",
                str(prompt),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert proc.stdout.count("dispatch retired") == 1, proc.stdout
        assert not status_json.exists()


def test_watcher_both_absent_returns_before_scanner_initialization() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "retired.tail"
        status_json = tmp / "retired.status.json"
        prompt = dispatch_mod._acp_watcher_prompt_path(status_json)
        tail.write_text("stale worker output\n", encoding="utf-8")
        argv = [
            str(ROOT / "scripts" / "goalflight_watch.py"),
            "--pid",
            "4242",
            "--tail",
            str(tail),
            "--status-json",
            str(status_json),
            "--dispatch-id",
            "retired-before-scan",
            "--ignore-prompt-file",
            str(prompt),
        ]
        output = io.StringIO()

        with patch.object(sys, "argv", argv), \
                patch.object(
                    goalflight_watch,
                    "IncrementalTailScanner",
                    side_effect=AssertionError("retired watcher initialized scanner"),
                ), \
                contextlib.redirect_stdout(output):
            rc = goalflight_watch.main()

        assert rc == 0
        assert output.getvalue().count("dispatch retired") == 1
        assert not status_json.exists()


def test_nonterminal_dispatch_record_authorizes_missing_status_creation() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "recoverable.tail"
        status_json = tmp / "recoverable.status.json"
        prompt = dispatch_mod._acp_watcher_prompt_path(status_json)
        tail.write_text("worker stopped without a marker\n", encoding="utf-8")
        prompt.write_text("live dispatch prompt\n", encoding="utf-8")

        class FinishedScanner:
            def __init__(self, *_args, **_kwargs):
                pass

            def scan(self, **_kwargs):
                return goalflight_watch.TailScanResult(
                    markers=[],
                    mail_markers=[],
                    terminal=None,
                    size=tail.stat().st_size,
                    content_bytes=0,
                    validation_bytes=0,
                    lines_materialized=0,
                    resynced=False,
                    resync_reason=None,
                    fence_unbalanced=False,
                )

        class NoTrace:
            def __init__(self, **_kwargs):
                pass

            def sample(self, **_kwargs):
                return {"trace_active": False}

        argv = [
            str(ROOT / "scripts" / "goalflight_watch.py"),
            "--pid",
            "4242",
            "--tail",
            str(tail),
            "--status-json",
            str(status_json),
            "--dispatch-id",
            "recoverable-status",
            "--ignore-prompt-file",
            str(prompt),
        ]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(goalflight_watch, "IncrementalTailScanner", FinishedScanner), \
                patch.object(goalflight_watch, "TraceLiveness", NoTrace), \
                patch.object(goalflight_watch, "worker_alive", return_value=(False, "dead", None)), \
                patch.object(goalflight_watch, "_dispatch_record_is_nonterminal", return_value=True), \
                patch.object(goalflight_watch, "_finish_existing_ledger", return_value=None), \
                patch.object(goalflight_watch.signal, "signal", return_value=None), \
                patch.object(goalflight_watch.atexit, "register", return_value=None), \
                contextlib.redirect_stdout(output):
            rc = goalflight_watch.main()

        payload = json.loads(status_json.read_text(encoding="utf-8"))
        assert rc == 1, (rc, output.getvalue(), payload)
        assert payload.get("state") == "worker_dead", payload
        assert "dispatch retired" not in output.getvalue()


def test_missing_status_without_nonterminal_record_returns_before_scan() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "orphan.tail"
        status_json = tmp / "orphan.status.json"
        prompt = dispatch_mod._acp_watcher_prompt_path(status_json)
        tail.write_text("stale worker output\n", encoding="utf-8")
        prompt.write_text("orphaned prompt\n", encoding="utf-8")
        argv = [
            str(ROOT / "scripts" / "goalflight_watch.py"),
            "--pid",
            "4242",
            "--tail",
            str(tail),
            "--status-json",
            str(status_json),
            "--dispatch-id",
            "orphan-without-record",
            "--ignore-prompt-file",
            str(prompt),
        ]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(
                    goalflight_watch,
                    "IncrementalTailScanner",
                    side_effect=AssertionError("retired orphan initialized scanner"),
                ), \
                patch.object(
                    goalflight_watch,
                    "_dispatch_record_is_nonterminal",
                    return_value=False,
                ), \
                contextlib.redirect_stdout(output):
            rc = goalflight_watch.main()

        assert rc == 0
        assert output.getvalue().count("dispatch retired") == 1
        assert not status_json.exists()


def test_watcher_prompt_history_retains_original_and_bounded_recent_turns() -> None:
    history = goalflight_acp_run._WatcherPromptHistory()
    total_recent = goalflight_acp_run.WATCHER_PROMPT_HISTORY_RECENT_TURNS + 3
    history.append("original prompt")
    for turn in range(1, total_recent + 1):
        history.append(f"steer turn {turn}")

    persisted = history.render()
    assert history.original == "original prompt"
    assert len(history.recent) == goalflight_acp_run.WATCHER_PROMPT_HISTORY_RECENT_TURNS
    assert history.recent[0] == "steer turn 4"
    assert history.recent[-1] == f"steer turn {total_recent}"
    assert "steer turn 1\n\n" not in persisted
    assert "omitted 3 earlier turn(s)" in persisted
    assert "original prompt" in persisted
    assert persisted.count("watcher prompt history truncated") == 1


def _capacity_env(state_dir: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    isolation_root = state_dir.parent
    env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
    env["GOALFLIGHT_DISPATCH_DIR"] = str(state_dir / "dispatch")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(isolation_root / "journal")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(isolation_root / "task-store")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(isolation_root / "messages")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(isolation_root / "wake-ledger")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(isolation_root / "pids")
    env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "1"
    env.update(extra)
    return env


def test_capacity_env_ignores_constructed_live_journal() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        live_journal_dir = tmp / "live-journal"
        with patch.dict(
            os.environ,
            {"GOALFLIGHT_JOURNAL_DIR": str(live_journal_dir)},
        ):
            live_authority = goalflight_journal.open_or_create_journal(ROOT)
            prepared = live_authority.prepare_attempt("queued-acp")
            assert prepared.committed and prepared.value is not None
            terminal = live_authority.commit_terminal(
                prepared.value.attempt_id,
                terminal_state="complete",
                observation={"source": "constructed live epoch-5 journal"},
            )
            assert terminal.committed
            env = _capacity_env(tmp / "isolated" / "state")

        expected = {
            "GOALFLIGHT_JOURNAL_DIR",
            "GOALFLIGHT_TASK_STORE_DIR",
            "GOALFLIGHT_MESSAGES_DIR",
            "GOALFLIGHT_WAKE_LEDGER_DIR",
            "GOAL_FLIGHT_PIDFILE_DIR",
        }
        assert expected <= env.keys()
        assert env["GOALFLIGHT_JOURNAL_DIR"] != str(live_journal_dir)
        with patch.dict(os.environ, env, clear=True):
            isolated_authority = goalflight_journal.open_or_create_journal(ROOT)
            assert isolated_authority.attempt_for_dispatch("queued-acp") is None

        live_attempt = live_authority.attempt_for_dispatch("queued-acp")
        assert live_attempt is not None
        assert live_attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL


def _capacity_cmd(state_dir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [sys.executable, "scripts/goalflight_capacity.py", *args],
        cwd=ROOT,
        env=_capacity_env(state_dir),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise AssertionError(f"capacity command failed: {proc.args}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


_HELD_CAPACITY_WORKERS: dict[tuple[str, str], subprocess.Popen] = {}


def _hold_capacity(state_dir: Path, *, agent: str = "fake-acp", dispatch_id: str = "held-acp-capacity") -> str:
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        proc = _capacity_cmd(
            state_dir,
            [
                "acquire",
                "--agent",
                agent,
                "--dispatch-id",
                dispatch_id,
                "--project-root",
                str(ROOT),
                "--ttl-s",
                "60",
                "--worker-pid",
                str(worker.pid),
            ],
        )
    except BaseException:
        worker.terminate()
        worker.wait(timeout=5)
        raise
    lease_id = json.loads(proc.stdout)["lease"]["lease_id"]
    _HELD_CAPACITY_WORKERS[(str(state_dir), lease_id)] = worker
    return lease_id


def _release_capacity(state_dir: Path, lease_id: str) -> None:
    worker = _HELD_CAPACITY_WORKERS.pop((str(state_dir), lease_id), None)
    if worker is not None:
        worker.terminate()
        worker.wait(timeout=5)
    _capacity_cmd(state_dir, ["release", "--lease-id", lease_id])


def _wait_for_status(path: Path, state: str, *, timeout_s: float = 5.0) -> dict:
    deadline = time.time() + timeout_s
    last: dict | None = None
    while time.time() < deadline:
        if path.exists():
            try:
                last = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
            else:
                if last.get("state") == state:
                    return last
        time.sleep(0.05)
    raise AssertionError(f"status {path} did not reach {state}; last={last}")


def _acp_cfg(tmp: Path, *, dispatch_id: str, status_json: Path, capacity_wait_s: float | None) -> SimpleNamespace:
    project_root = str(_make_repo(tmp))
    return goalflight_acp_run.normalized_acp_dispatch_cfg(
        SimpleNamespace(
            agent="fake-acp",
            model=None,
            install_slot=None,
            project_root=project_root,
            cwd=project_root,
            worktree="off",
            in_place=True,
            session_id=None,
            dispatch_id=dispatch_id,
            priority="normal",
            capacity_wait_s=capacity_wait_s,
            prompt_id=None,
            prompt=None,
            prompt_text="COMPLETE: fake ACP done",
            prompt_b64=None,
            mode="one-shot",
            idle_timeout=5.0,
            status_json=str(status_json),
            steer_file=str(tmp / f"{dispatch_id}.steer.jsonl"),
            context_mode="disabled",
            os_sandbox=goalflight_acp_run.OS_SANDBOX_OFF,
            permission_mode="auto",
            permission_dir=None,
            permission_inline_timeout_s=None,
            permission_user_timeout_s=None,
            permission_allow_tool_title_pattern=[],
            read_only=False,
            interactive=False,
            heartbeat_interval=0.05,
            wedge_samples=1,
            max_tool_s=5.0,
            max_quiet_s=2.0,
            progress_stall_s=2.0,
            liveness_profile="local_compute",
            remote_turn_silence_s=None,
            remote_turn_cancel_grace_s=1.0,
            cpu_epsilon=0.1,
            unregistered_forced=True,
            json=False,
        )
    )


@contextlib.contextmanager
def _registered_acp_test_controller():
    def stamp_controller(args, _project_root):
        args.controller_label = "acp-test"
        args.controller_session_id = "acp-test-session"
        args._controller_beacon_pid = 41001
        return {"claimed": True}

    with (
        patch.object(dispatch_mod, "_stamp_controller_session", stamp_controller),
        patch.object(
            dispatch_mod,
            "_prepare_attempt_controller_registration",
            return_value=None,
        ),
    ):
        yield


def test_acp_prelaunch_refusals_are_delivered_to_launching_controller() -> None:
    refusals = (
        "windows",
        "prompt_read",
        "adapter_gate",
        "sandbox_profile",
        "sandbox_request",
        "sandbox_preflight",
        "sandbox_write_roots",
        "waiting_capacity_record",
    )
    expected_states = {
        "windows": "blocked_windows_dispatch",
        "prompt_read": "failed",
        "adapter_gate": "blocked_adapter_gate",
        "sandbox_profile": "blocked_os_sandbox",
        "sandbox_request": "blocked_os_sandbox",
        "sandbox_preflight": "blocked_os_sandbox",
        "sandbox_write_roots": "blocked_os_sandbox",
        "waiting_capacity_record": "failed_worktree",
    }
    for refusal in refusals:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "state"
            dispatch_id = f"acp-{refusal}"
            cfg = _acp_cfg(
                tmp,
                dispatch_id=dispatch_id,
                status_json=tmp / f"{dispatch_id}.status.json",
                capacity_wait_s=0.0,
            )
            env = _capacity_env(
                state_dir,
                GOALFLIGHT_CAPACITY_CONF=os.devnull,
            )
            owner_label = "launcher"
            owner_nonce = "launcher-session"
            owner_pid = 41001
            if refusal == "windows":
                cfg.controller_label = owner_label
                cfg.controller_session_id = owner_nonce
                cfg.controller_pid = owner_pid
                Path(cfg.status_json).write_text(
                    json.dumps(
                        {
                            "schema": "goalflight.acp-run.v1",
                            "dispatch_id": dispatch_id,
                            "project_root": cfg.project_root,
                            "controller_label": owner_label,
                            "controller_session_id": owner_nonce,
                            "controller_pid": owner_pid,
                        }
                    ),
                    encoding="utf-8",
                )

            with patch.dict(os.environ, env, clear=True):
                authority = goalflight_journal.open_or_create_journal(
                    Path(cfg.project_root)
                )
                for label, nonce, pid in (
                    (owner_label, owner_nonce, owner_pid),
                    ("observer", "observer-session", 41002),
                ):
                    claimed = authority.claim_or_renew_lease(
                        label,
                        principal={
                            "pid": pid,
                            "start_token": f"{label}-start",
                        },
                        nonce=nonce,
                    )
                    assert claimed.committed and claimed.value is not None
                active_labels = {
                    str(row["label"])
                    for row in authority.read_all(
                        """SELECT label FROM controller_leases
                           WHERE project_root = ? AND state = 'ACTIVE'""",
                        (str(authority.project_root),),
                    )
                }
                assert active_labels == {owner_label, "observer"}, active_labels

                def stamp_controller(args, _project_root):
                    args.controller_label = owner_label
                    args.controller_session_id = owner_nonce
                    args._controller_beacon_pid = owner_pid
                    return {"claimed": True}

                cfg.prompt = (
                    str(tmp / "missing-prompt.md")
                    if refusal == "prompt_read"
                    else None
                )
                if refusal == "sandbox_profile":
                    cfg.os_sandbox = "invalid-profile"
                elif refusal in {
                    "sandbox_request",
                    "sandbox_preflight",
                    "sandbox_write_roots",
                }:
                    cfg.os_sandbox = "read-only"
                sandbox_validation = (
                    [None, "sandbox request refusal"]
                    if refusal == "sandbox_request"
                    else None
                )
                sandbox_preflight = (
                    goalflight_acp_run.OsSandboxError("sandbox preflight refusal")
                    if refusal == "sandbox_preflight"
                    else None
                )
                sandbox_roots = (
                    goalflight_acp_run.OsSandboxError("sandbox roots refusal")
                    if refusal == "sandbox_write_roots"
                    else None
                )
                with (
                    patch.object(
                        dispatch_mod,
                        "_stamp_controller_session",
                        stamp_controller,
                    ),
                    patch.object(
                        dispatch_mod,
                        "_prepare_attempt_controller_registration",
                        return_value=None,
                    ),
                    patch.object(
                        goalflight_acp_run.goalflight_compat,
                        "is_windows",
                        return_value=refusal == "windows",
                    ),
                    patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
                    patch.object(
                        goalflight_acp_run,
                        "validate_acp_dispatch_readiness",
                        return_value=(
                            "adapter gate refusal"
                            if refusal == "adapter_gate"
                            else None
                        ),
                    ),
                    patch.object(
                        goalflight_acp_run,
                        "validate_os_sandbox_request",
                        side_effect=sandbox_validation,
                        return_value=None,
                    ),
                    patch.object(
                        goalflight_acp_run,
                        "preflight_os_sandbox",
                        side_effect=sandbox_preflight,
                        return_value=None,
                    ),
                    patch(
                        "goalflight_os_sandbox.macos_write_roots",
                        side_effect=sandbox_roots,
                        return_value=[],
                    ),
                    patch.object(
                        goalflight_acp_run,
                        "_record_acp_ledger_state",
                        side_effect=(
                            dispatch_mod.DispatchUsageError("waiting record refused")
                            if refusal == "waiting_capacity_record"
                            else None
                        ),
                    ),
                ):
                    payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))

                assert payload["dispatch_id"] == dispatch_id, payload
                assert payload["state"] == expected_states[refusal], payload
                attempt = authority.attempt_for_dispatch(dispatch_id)
                assert attempt is not None
                assert attempt.lifecycle_state in goalflight_journal.ATTEMPT_FINAL_STATES
                projected = authority.project_terminal_outbox(
                    messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"])
                )
                assert len(projected) == 1, projected
                deliveries = authority.read_all(
                    """SELECT event_type, recipient_label FROM delivery_events
                       WHERE stream_id = ? AND withdrawn_at IS NULL
                       ORDER BY recipient_label""",
                    (dispatch_id,),
                )
                assert [
                    (str(row["event_type"]), str(row["recipient_label"]))
                    for row in deliveries
                ] == [("blocked", owner_label)], deliveries
                assert payload["controller_label"] == owner_label, payload
                assert payload["controller_session_id"] == owner_nonce, payload
                assert payload["controller_pid"] == owner_pid, payload


def test_windows_refusal_keeps_ownerless_queue_entry_from_ambient_controller() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "acp-windows-ownerless"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=tmp / f"{dispatch_id}.status.json",
            capacity_wait_s=0.0,
        )
        cfg.controller_label = "launcher"
        cfg.request_envelope = {
            "dispatch_id": dispatch_id,
            "project_root": cfg.project_root,
        }
        env = _capacity_env(
            state_dir,
            GOALFLIGHT_CAPACITY_CONF=os.devnull,
        )
        env["GOALFLIGHT_CONTROLLER_LABEL"] = "launcher"
        env["GOALFLIGHT_CONTROLLER_LEASE_NONCE"] = "launcher-session"
        with patch.dict(os.environ, env, clear=True):
            authority = goalflight_journal.open_or_create_journal(
                Path(cfg.project_root)
            )
            for label, nonce, pid in (
                ("launcher", "launcher-session", 41001),
                ("observer", "observer-session", 41002),
            ):
                claimed = authority.claim_or_renew_lease(
                    label,
                    principal={"pid": pid, "start_token": f"{label}-start"},
                    nonce=nonce,
                )
                assert claimed.committed and claimed.value is not None

            payload, _status_path = goalflight_acp_run.write_windows_refusal_status(
                cfg
            )
            assert payload["state"] == "blocked_windows_dispatch"
            assert "controller_label" not in payload
            assert "controller_session_id" not in payload
            authority.project_terminal_outbox(
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"])
            )
            deliveries = authority.read_all(
                """SELECT recipient_label FROM delivery_events
                   WHERE stream_id = ? AND withdrawn_at IS NULL
                   ORDER BY recipient_label""",
                (dispatch_id,),
            )
            assert [str(row["recipient_label"]) for row in deliveries] == [
                "launcher",
                "observer",
            ], deliveries


def test_windows_refusal_uses_parent_queue_owner_over_ambient_controller() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "acp-windows-parent-queue-owner"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=tmp / f"{dispatch_id}.status.json",
            capacity_wait_s=0.0,
        )
        cfg.request_envelope = {
            "dispatch_id": dispatch_id,
            "project_root": cfg.project_root,
            "controller_label": "controller-b",
            "controller_session_id": "controller-b-session",
            "controller_pid": 41002,
        }
        env = _capacity_env(
            state_dir,
            GOALFLIGHT_CAPACITY_CONF=os.devnull,
        )
        env["GOALFLIGHT_CONTROLLER_LABEL"] = "controller-a"
        env["GOALFLIGHT_CONTROLLER_LEASE_NONCE"] = "controller-a-session"
        with patch.dict(os.environ, env, clear=True):
            authority = goalflight_journal.open_or_create_journal(
                Path(cfg.project_root)
            )
            for label, nonce, pid in (
                ("controller-a", "controller-a-session", 41001),
                ("controller-b", "controller-b-session", 41002),
            ):
                claimed = authority.claim_or_renew_lease(
                    label,
                    principal={"pid": pid, "start_token": f"{label}-start"},
                    nonce=nonce,
                )
                assert claimed.committed and claimed.value is not None

            payload, _status_path = goalflight_acp_run.write_windows_refusal_status(
                cfg
            )
            assert payload["controller_label"] == "controller-b", payload
            assert payload["controller_session_id"] == "controller-b-session", payload
            authority.project_terminal_outbox(
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"])
            )
            deliveries = authority.read_all(
                """SELECT recipient_label FROM delivery_events
                   WHERE stream_id = ? AND withdrawn_at IS NULL""",
                (dispatch_id,),
            )
            assert [str(row["recipient_label"]) for row in deliveries] == [
                "controller-b"
            ], deliveries


class _FakeAcpConn:
    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.client = SimpleNamespace(_prompt_in_use=False)
        self.acp_session_id = None
        self.os_sandbox_metadata = None

    async def close_gracefully(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)

    async def kill(self) -> None:
        await self.close_gracefully()

    async def cancel(self) -> None:
        return None


def _install_fake_acp_after_capacity():
    old_spawn = goalflight_acp_run.spawn_and_handshake_with_retry
    old_prompt = goalflight_acp_run.run_prompt
    old_validate = goalflight_acp_run.validate_acp_dispatch_readiness

    async def fake_spawn(command, command_args, **kwargs):
        admission = kwargs.get("launch_admission")
        scope = admission() if callable(admission) else contextlib.nullcontext(object())
        with scope as status_lock:
            if status_lock is None:
                raise RuntimeError("fake ACP worker refused at launch admission")
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=kwargs.get("cwd"),
                env=kwargs.get("env"),
            )
        return proc, _FakeAcpConn(proc)

    async def fake_prompt(_conn, _text, **_kwargs):
        return goalflight_acp_run.PromptResult(
            text="COMPLETE: fake ACP done\n",
            stop_reason="end_turn",
        )

    goalflight_acp_run.spawn_and_handshake_with_retry = fake_spawn
    goalflight_acp_run.run_prompt = fake_prompt
    goalflight_acp_run.validate_acp_dispatch_readiness = lambda *_args, **_kwargs: None
    return old_spawn, old_prompt, old_validate


def _restore_fake_acp(saved) -> None:
    old_spawn, old_prompt, old_validate = saved
    goalflight_acp_run.spawn_and_handshake_with_retry = old_spawn
    goalflight_acp_run.run_prompt = old_prompt
    goalflight_acp_run.validate_acp_dispatch_readiness = old_validate


def _write_grok_resume_parent(
    repo: Path,
    *,
    dispatch_id: str,
    session_id: str,
    ended_at: str | None = None,
) -> Path:
    session = dispatch_mod._seat_session_dir("seat-g", "grok", str(repo), session_id)
    assert session is not None
    session.mkdir(parents=True)
    (session / "summary.json").write_text("{}", encoding="utf-8")
    (session / "chat_history.jsonl").write_text("{}\n", encoding="utf-8")
    record = {
        "schema": dispatch_mod.goalflight_ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "grok-acp",
        "engine": "grok",
        "shape": "acp",
        "state": "complete",
        "terminal_state": "complete",
        "project_root": str(repo),
        "worker_cwd": str(repo),
        "worker_pid": None,
        "account": "seat-g",
        "effective_account": "seat-g",
        "engine_session_id": session_id,
        "dispatch_argv": ["--agent", "grok-acp", "--shape", "acp", "--cwd", str(repo)],
    }
    if ended_at is not None:
        record["ended_at"] = ended_at
    dispatch_mod.goalflight_ledger.write_record(record)
    return session


def test_acp_resume_completes_above_floor_through_dispatch_main(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _make_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    grok_home = home / ".goal-flight" / "accounts" / "seat-g" / "grok" / ".grok"
    grok_home.mkdir(parents=True)
    (grok_home / "config.toml").write_text(
        '[cli]\n\n[ui]\npermission_mode = "always-approve"\n',
        encoding="utf-8",
    )
    (grok_home / "auth.json").write_text("{}\n", encoding="utf-8")
    state = tmp_path / "state"
    env = _capacity_env(state)
    env.update(
        HOME=str(home),
        GOALFLIGHT_CAPACITY_CONF=os.devnull,
        GOAL_FLIGHT_PIDFILE_DIR=str(state / "p"),
        GOALFLIGHT_CODEX_STATE_DIR=str(state),
    )
    parent_id = "acp-resume-parent"
    child_id = "acp-resume-child"
    session_id = "00000000-0000-4000-8000-000000000025"
    prompt = tmp_path / "acp-resume.md"
    prompt.write_text("Continue the recorded session.", encoding="utf-8")
    saved = _install_fake_acp_after_capacity()
    result: list[object] = []
    build_parser = dispatch_mod._build_launch_parser

    def foreground_launch_parser():
        parser = build_parser()
        parser.set_defaults(foreground=True)
        return parser

    monkeypatch.setattr(dispatch_mod, "_build_launch_parser", foreground_launch_parser)

    monkeypatch.setattr(dispatch_mod, "_configured_account_names", lambda *_args: {"seat-g"})
    monkeypatch.setattr(dispatch_mod, "_seat_probe_says_usable", lambda *_args, **_kw: True)
    monkeypatch.setattr(dispatch_mod, "_account_quota_blocked", lambda *_args, **_kw: False)
    monkeypatch.setattr(dispatch_mod, "_grok_account_admission_reason", lambda *_args, **_kw: None)
    monkeypatch.setattr(dispatch_mod, "_resolve_launch_account_env", lambda *_args: {})
    monkeypatch.setattr(dispatch_mod, "_default_dispatch_id", lambda *_args: child_id)
    monkeypatch.setattr(
        dispatch_mod.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            free=dispatch_mod.DISK_FREE_SPACE_FLOOR_BYTES + 1
        ),
    )
    monkeypatch.setattr(goalflight_acp_run, "cleanup_ghosts", lambda: 0)
    monkeypatch.setattr(goalflight_acp_run, "agent_command", lambda *_args, **_kw: (sys.executable, []))

    def resume() -> None:
        try:
            result.append(
                dispatch_mod._cmd_resume(
                    [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
                )
            )
        except BaseException as exc:  # re-raised in the test thread
            result.append(exc)

    try:
        with patch.dict(os.environ, env, clear=True):
            session = dispatch_mod._seat_session_dir(
                "seat-g", "grok", str(repo), session_id
            )
            assert session is not None
            session.mkdir(parents=True)
            (session / "summary.json").write_text("{}", encoding="utf-8")
            (session / "chat_history.jsonl").write_text("{}\n", encoding="utf-8")
            dispatch_mod.goalflight_ledger.write_record(
                {
                    "schema": dispatch_mod.goalflight_ledger.SCHEMA,
                    "dispatch_id": parent_id,
                    "agent": "grok-acp",
                    "engine": "grok",
                    "shape": "acp",
                    "state": "complete",
                    "terminal_state": "complete",
                    "project_root": str(repo),
                    "worker_cwd": str(repo),
                    "worker_pid": None,
                    "account": "seat-g",
                    "effective_account": "seat-g",
                    "engine_session_id": session_id,
                    "dispatch_argv": [
                        "--agent", "grok-acp", "--shape", "acp",
                        "--cwd", str(repo), "--in-place", "--foreground",
                    ],
                }
            )
            thread = threading.Thread(target=resume, daemon=True)
            thread.start()
            thread.join(timeout=15)
            assert not thread.is_alive(), "ACP resume blocked while publishing its ledger row"
            assert result == [0]
            child = json.loads(
                dispatch_mod.goalflight_ledger.record_path(child_id).read_text(
                    encoding="utf-8"
                )
            )
            assert child["parent_dispatch_id"] == parent_id
            assert child["shape"] == "acp"
            assert child["state"] == "complete"
            assert session.is_dir()
    finally:
        _restore_fake_acp(saved)


def test_acp_capacity_handoff_precedes_spawn_and_attaches_immediately() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "handoff-acp.status.json"
        cfg = _acp_cfg(
            tmp,
            dispatch_id="handoff-acp",
            status_json=status_json,
            capacity_wait_s=0.0,
        )
        events: list[str] = []
        real_mark = goalflight_acp_run.goalflight_capacity.mark_lease_spawning
        real_attach = goalflight_acp_run.attach_worker_to_capacity_lease

        def mark(lease_id: str | None) -> bool:
            events.append("spawning")
            return real_mark(lease_id)

        def attach(lease_id: str | None, worker_pid: int, worker_pgid: int | None = None) -> None:
            events.append("attach")
            real_attach(lease_id, worker_pid, worker_pgid)

        async def fake_spawn(_command, _args, **kwargs):
            with kwargs["launch_admission"]() as status_lock:
                assert status_lock is not None
                events.append("spawn")
                proc = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    cwd=kwargs["cwd"],
                    env=kwargs["env"],
                )
            await kwargs["on_attempt"](0, proc)
            return proc, _FakeAcpConn(proc)

        async def fake_prompt(_conn, _text, **_kwargs):
            return goalflight_acp_run.PromptResult(
                text="COMPLETE: handoff tested\n",
                stop_reason="end_turn",
            )

        with (
            patch.dict(os.environ, _capacity_env(state_dir), clear=True),
            patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
            patch.object(
                goalflight_acp_run.goalflight_capacity,
                "mark_lease_spawning",
                mark,
            ),
            patch.object(goalflight_acp_run, "attach_worker_to_capacity_lease", attach),
            patch.object(goalflight_acp_run, "spawn_and_handshake_with_retry", fake_spawn),
            patch.object(goalflight_acp_run, "run_prompt", fake_prompt),
            patch.object(
                goalflight_acp_run,
                "validate_acp_dispatch_readiness",
                lambda *_args, **_kwargs: None,
            ),
        ):
            payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))

        assert payload["state"] == "complete", payload
        assert events[:3] == ["spawning", "spawn", "attach"], events


def test_acp_spawn_refuses_completion_published_before_admission() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "completed-before-acp-spawn.status.json"
        dispatch_id = "completed-before-acp-spawn"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=status_json,
            capacity_wait_s=0.0,
        )
        dispatch_mod.write_status(
            status_json,
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": dispatch_id,
                "state": "complete",
                "terminal_state": "complete",
                "worker_alive": False,
            },
        )
        spawns: list[object] = []

        class FakeConnection(_FakeAcpConn):
            async def initialize(self, timeout: float = 60.0) -> None:
                return None

            async def new_session(self, cwd: str, timeout: float = 60.0) -> str:
                return "fake-acp-session"

        async def fake_spawn(*_args, **kwargs):
            spawns.append(kwargs)
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=kwargs.get("cwd"),
                env=kwargs.get("env"),
                stderr=subprocess.PIPE,
            )
            return FakeConnection(proc)

        async def fake_prompt(_conn, _text, **_kwargs):
            return goalflight_acp_run.PromptResult(
                text="COMPLETE: fake ACP done\n",
                stop_reason="end_turn",
            )

        with (
            _registered_acp_test_controller(),
            patch.dict(os.environ, _capacity_env(state_dir), clear=True),
            patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
            patch.object(
                goalflight_acp_run,
                "validate_acp_dispatch_readiness",
                return_value=None,
            ),
            patch.object(goalflight_acp_run, "spawn_acp_connection", fake_spawn),
            patch.object(goalflight_acp_run, "run_prompt", fake_prompt),
        ):
            asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
            authority = goalflight_journal.open_or_create_journal(
                Path(cfg.project_root)
            )
            attempt = authority.attempt_for_dispatch(dispatch_id)
            assert attempt is not None, "ACP launch refusal removed its journal attempt"
            assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL, attempt
            outbox_count = authority.read_all(
                "SELECT COUNT(*) AS count FROM terminal_outbox WHERE attempt_id = ?",
                (attempt.attempt_id,),
            )[0]["count"]
            assert outbox_count == 1, outbox_count
            completion = authority.commit_terminal(
                attempt.attempt_id,
                terminal_state="complete",
                observation={
                    "state": "complete",
                    "terminal_state": "complete",
                    "worker_still_alive": False,
                },
                event_type="result",
            )
            assert completion.committed and completion.value is not None, completion
            assert completion.value.terminal_state == "complete", completion.value
            assert completion.value.idempotent, completion.value
            assert authority.read_all(
                "SELECT COUNT(*) AS count FROM terminal_outbox WHERE attempt_id = ?",
                (attempt.attempt_id,),
            )[0]["count"] == outbox_count

        assert spawns == [], spawns
        status = json.loads(status_json.read_text(encoding="utf-8"))
        assert status["state"] == "complete", status


def test_acp_occupancy_override_reaches_admission_and_warns() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _capacity_env(tmp / "state")
        env.pop(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, None)
        env.pop(goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, None)
        saved = _install_fake_acp_after_capacity()
        try:
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
            ):
                lock = goalflight_worktree_pool.try_acquire_worktree_path_lock(tmp, "incumbent")
                try:
                    for forced in (False, True):
                        dispatch_id = f"occupancy-{forced}"
                        args = _base_acp_args(tmp, agent="fake-acp", dispatch_id=dispatch_id)
                        args.unregistered_forced = True
                        args.occupied_worktree_forced = forced
                        args.in_place = True
                        args.tail = str(tmp / f"{dispatch_id}.tail")
                        cfg = dispatch_mod._build_acp_cfg(
                            args, status_json=tmp / f"{dispatch_id}.json", base=tmp,
                        )
                        stderr = io.StringIO()
                        with contextlib.redirect_stderr(stderr):
                            payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
                        assert goalflight_acp_run.acp_dispatch_exit_code(payload) == (0 if forced else 64), payload
                        if forced:
                            assert payload["state"] == "complete", payload
                            for output in (stderr.getvalue(), Path(args.tail).read_text()):
                                assert "--occupied-worktree-forced accepted" in output, output
                                assert "incumbent" in output, output
                        else:
                            assert "incumbent" in payload["error"], payload
                            assert "incumbent" in stderr.getvalue(), stderr.getvalue()
                finally:
                    lock.release()
        finally:
            _restore_fake_acp(saved)
        assert goalflight_acp_run.acp_dispatch_exit_code({"state": "failed_worktree"}) == 1


def _run_acp_thread(cfg: SimpleNamespace):
    result: dict[str, object] = {}

    def target() -> None:
        try:
            result["payload"] = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        except BaseException as exc:  # pragma: no cover - re-raised below
            result["exc"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def test_task_linked_acp_status_admission_defers_without_holding_ledger() -> None:
    with tempfile.TemporaryDirectory() as td:
        _assert_task_linked_acp_status_admission_defers_without_holding_ledger(
            Path(td)
        )


def _assert_task_linked_acp_status_admission_defers_without_holding_ledger(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    status_path = tmp_path / "status.json"
    cfg = _acp_cfg(
        tmp_path,
        dispatch_id="task-linked-status-admission",
        status_json=status_path,
        capacity_wait_s=0.0,
    )
    cfg.queue_launch_token = "queue-token"
    ledger_lock_requested = threading.Event()

    def task_ledger_lock():
        ledger_lock_requested.set()
        return dispatch_mod.goalflight_ledger.StateLock()

    cfg.redispatch_spawn_lock = task_ledger_lock
    cfg.redispatch_spawn_guard = lambda: None
    deadline_args: list[float | None] = []
    spawned: list[bool] = []
    admission_requested = threading.Event()
    admission_finished = threading.Event()
    continue_admission = threading.Event()
    original_admission = dispatch_mod._status_launch_admission

    def observe_admission(path, token, **kwargs):
        deadline_args.append(kwargs.get("deadline_s"))
        admission_requested.set()
        continue_admission.wait(timeout=5.0)

        @contextlib.contextmanager
        def observed_admission():
            try:
                with original_admission(path, token, **kwargs) as status_lock:
                    yield status_lock
            finally:
                admission_finished.set()

        return observed_admission()

    async def unexpected_spawn(*_args, **_kwargs):
        spawned.append(True)
        raise RuntimeError("ACP spawned despite status-lock contention")

    class NoStartupGate:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    saved = _install_fake_acp_after_capacity()
    goalflight_acp_run.spawn_and_handshake_with_retry = saved[0]
    lock = None
    thread = None
    result: dict[str, object] = {}
    ledger_lock = None
    try:
        with (
            patch.dict(
                os.environ,
                _capacity_env(state_dir, GOALFLIGHT_CAPACITY_CONF=os.devnull),
                clear=True,
            ),
            patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
            patch.object(goalflight_acp_run, "spawn_acp_connection", unexpected_spawn),
            patch.object(goalflight_acp_run, "StartupGate", NoStartupGate),
            patch.object(dispatch_mod, "_status_launch_admission", observe_admission),
        ):
            thread, result = _run_acp_thread(cfg)
            reached_admission = admission_requested.wait(timeout=10.0)
            if reached_admission:
                lock = goalflight_liveness.status_write_lock(status_path)
                lock.__enter__()
                continue_admission.set()
                ledger_lock = dispatch_mod.goalflight_ledger.StateLock.try_acquire(
                    time.monotonic() + 0.5
                )
                if ledger_lock is not None:
                    ledger_lock.release()
                assert admission_finished.wait(timeout=2.0), (
                    "ACP did not finish the bounded admission while status was held"
                )
    finally:
        continue_admission.set()
        if lock is not None:
            lock.__exit__(None, None, None)
        if thread is not None:
            thread.join(timeout=3.0)
        _restore_fake_acp(saved)

    assert thread is not None and not thread.is_alive(), "ACP admission did not finish"
    assert deadline_args and admission_requested.is_set(), (
        "ACP did not reach launch admission; "
        f"ledger_lock_requested={ledger_lock_requested.is_set()} result={result!r}"
    )
    assert ledger_lock is not None, "ACP kept the task ledger while waiting for status"
    assert deadline_args[0] is not None, "ACP admission used an unbounded status-lock wait"
    assert spawned == [], spawned
    payload = result.get("payload")
    assert isinstance(payload, dict), result
    assert payload.get("status_launch_admission_deferred") is True, {
        "state": payload.get("state"),
        "dispatch_refused": payload.get("dispatch_refused"),
        "error": payload.get("error"),
    }
    assert payload.get("state") == "queued", payload
    old_env = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(_capacity_env(state_dir, GOALFLIGHT_CAPACITY_CONF=os.devnull))
        attempt = goalflight_journal.open_or_create_journal(
            Path(cfg.project_root)
        ).attempt_for_dispatch(cfg.dispatch_id)
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    assert attempt is not None
    assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_PREPARED, attempt
    assert attempt.launch_token == cfg.queue_launch_token, attempt
    queue_args = SimpleNamespace(
        from_queue=True,
        queue_launch_token="queue-token",
        queue_claim_path="claim.json",
    )
    assert dispatch_mod._is_retryable_queued_refusal(queue_args, payload.get("error"))


def test_task_linked_acp_releases_ledger_before_handshake_status_write() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        cfg = _acp_cfg(
            tmp,
            dispatch_id="task-linked-handshake-status",
            status_json=tmp / "status.json",
            capacity_wait_s=0.0,
        )
        cfg.task_ids = ["task-linked"]
        cfg.redispatch_spawn_lock = lambda: dispatch_mod.goalflight_ledger.StateLock()
        cfg.redispatch_spawn_guard = lambda: None
        ledger_available_during_spawn: list[bool] = []
        saved = _install_fake_acp_after_capacity()

        async def checking_spawn(_command, _args, **kwargs):
            with kwargs["launch_admission"]() as status_lock:
                assert status_lock is not None
                proc = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    cwd=kwargs.get("cwd"),
                    env=kwargs.get("env"),
                )
            lock = dispatch_mod.goalflight_ledger.StateLock.try_acquire(
                time.monotonic() + 0.25
            )
            ledger_available_during_spawn.append(lock is not None)
            if lock is not None:
                lock.release()
            await kwargs["on_attempt"](0, proc)
            return proc, _FakeAcpConn(proc)

        goalflight_acp_run.spawn_and_handshake_with_retry = checking_spawn
        try:
            with (
                patch.dict(os.environ, _capacity_env(state_dir), clear=True),
                patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
            ):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)

        assert payload.get("state") == "complete", payload
        assert ledger_available_during_spawn == [True], ledger_available_during_spawn


def test_task_linked_acp_rechecks_supersession_at_spawn() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "task-linked-superseded-at-spawn"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=tmp / "status.json",
            capacity_wait_s=0.0,
        )
        cfg.task_ids = ["task-linked"]
        cfg.queue_launch_token = "superseded-spawn-token"
        cfg.redispatch_spawn_lock = lambda: dispatch_mod.goalflight_ledger.StateLock()
        cfg.redispatch_spawn_guard = lambda: dispatch_mod._redispatch_spawn_guard(cfg)
        run_env = _capacity_env(state_dir)
        with dispatch_mod.goalflight_ledger.StateLock():
            dispatch_mod.goalflight_ledger.write_record(
                {
                    "schema": dispatch_mod.goalflight_ledger.SCHEMA,
                    "dispatch_id": dispatch_id,
                    "project_root": cfg.project_root,
                    "task_ids": cfg.task_ids,
                    "state": "queued",
                    "terminal_state": "unknown",
                    "queue_launch_token": cfg.queue_launch_token,
                }
            )

        original_admission = dispatch_mod._status_launch_admission
        superseded = False
        spawned: list[bool] = []
        saved = _install_fake_acp_after_capacity()

        @contextlib.contextmanager
        def supersede_after_initial_guard(path, token, **kwargs):
            nonlocal superseded
            with original_admission(path, token, **kwargs) as status_lock:
                if status_lock is not None and not superseded:
                    with dispatch_mod.goalflight_ledger.StateLock():
                        record = dispatch_mod.goalflight_ledger.read_record(dispatch_id)
                        assert record is not None
                        record["superseded_by"] = "replacement-dispatch"
                        dispatch_mod.goalflight_ledger.write_record(record)
                    superseded = True
                yield status_lock

        async def spawn_at_fenced_boundary(_command, _args, **kwargs):
            with kwargs["launch_admission"]() as status_lock:
                assert status_lock is not None
                spawned.append(True)
                proc = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    cwd=kwargs.get("cwd"),
                    env=kwargs.get("env"),
                )
            await kwargs["on_attempt"](0, proc)
            return proc, _FakeAcpConn(proc)

        goalflight_acp_run.spawn_and_handshake_with_retry = spawn_at_fenced_boundary
        try:
            with (
                patch.dict(os.environ, run_env, clear=True),
                patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
                patch.object(
                    dispatch_mod,
                    "_status_launch_admission",
                    supersede_after_initial_guard,
                ),
            ):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)

        assert superseded, "the replacement did not land between initial guard and spawn"
        assert spawned == [], spawned
        assert payload.get("state") == "failed", payload
        assert "superseded by replacement-dispatch" in str(payload.get("error")), payload
        with patch.dict(os.environ, run_env, clear=True):
            attempt = goalflight_journal.open_or_create_journal(
                Path(cfg.project_root)
            ).attempt_for_dispatch(dispatch_id)
        assert attempt is not None
        assert attempt.lifecycle_state in goalflight_journal.ATTEMPT_FINAL_STATES, attempt


def test_task_linked_acp_early_supersession_terminalizes_prepared_attempt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "task-linked-superseded-before-spawn-guard"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=tmp / "status.json",
            capacity_wait_s=0.0,
        )
        cfg.task_ids = ["task-linked"]
        cfg.queue_launch_token = "early-superseded-token"
        cfg.redispatch_spawn_lock = lambda: dispatch_mod.goalflight_ledger.StateLock()
        cfg.redispatch_spawn_guard = lambda: dispatch_mod._redispatch_spawn_guard(cfg)
        run_env = _capacity_env(state_dir)
        with patch.dict(os.environ, run_env, clear=True):
            with dispatch_mod.goalflight_ledger.StateLock():
                dispatch_mod.goalflight_ledger.write_record(
                    {
                        "schema": dispatch_mod.goalflight_ledger.SCHEMA,
                        "dispatch_id": dispatch_id,
                        "project_root": cfg.project_root,
                        "task_ids": cfg.task_ids,
                        "state": "queued",
                        "terminal_state": "unknown",
                        "queue_launch_token": cfg.queue_launch_token,
                        "superseded_by": "replacement-dispatch",
                    }
                )

            spawns: list[bool] = []

            async def unexpected_spawn(*_args, **_kwargs):
                spawns.append(True)
                raise AssertionError("superseded ACP attempt spawned")

            with (
                patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
                patch.object(
                    goalflight_acp_run,
                    "validate_acp_dispatch_readiness",
                    return_value=None,
                ),
                patch.object(
                    goalflight_acp_run,
                    "spawn_and_handshake_with_retry",
                    unexpected_spawn,
                ),
            ):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))

            assert spawns == [], spawns
            assert payload.get("state") == "failed", payload
            attempt = goalflight_journal.open_or_create_journal(
                Path(cfg.project_root)
            ).attempt_for_dispatch(dispatch_id)
            assert attempt is not None
            assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL, attempt
            record = dispatch_mod.goalflight_ledger.read_record(dispatch_id)
            assert record is not None
            assert record.get("prelaunch_failure") is True, record


def test_acp_lost_capacity_lease_terminalizes_prepared_attempt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "acp-capacity-lease-lost-before-spawn"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=tmp / "status.json",
            capacity_wait_s=0.0,
        )
        run_env = _capacity_env(state_dir)
        with patch.dict(os.environ, run_env, clear=True):
            spawns: list[bool] = []
            lost_leases: list[str | None] = []

            def lose_capacity_lease(lease_id: str | None) -> bool:
                lost_leases.append(lease_id)
                return False

            async def unexpected_spawn(*_args, **_kwargs):
                spawns.append(True)
                raise AssertionError("ACP spawned after losing its capacity lease")

            with (
                patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
                patch.object(
                    goalflight_acp_run,
                    "validate_acp_dispatch_readiness",
                    return_value=None,
                ),
                patch.object(
                    goalflight_acp_run.goalflight_capacity,
                    "mark_lease_spawning",
                    lose_capacity_lease,
                ),
                patch.object(
                    goalflight_acp_run,
                    "spawn_and_handshake_with_retry",
                    unexpected_spawn,
                ),
            ):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))

            assert lost_leases and lost_leases[0], lost_leases
            assert spawns == [], spawns
            assert payload.get("state") == "failed", payload
            attempt = goalflight_journal.open_or_create_journal(
                Path(cfg.project_root)
            ).attempt_for_dispatch(dispatch_id)
            assert attempt is not None
            assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL, attempt
            record = dispatch_mod.goalflight_ledger.read_record(dispatch_id)
            assert record is not None
            assert record.get("prelaunch_failure") is True, record


def test_acp_scratch_setup_failure_terminalizes_first_prepared_attempt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "acp-scratch-setup-failed"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=tmp / "status.json",
            capacity_wait_s=0.0,
        )
        scratch_path = tmp / "scratch-is-a-file"
        scratch_path.write_text("obstruction", encoding="utf-8")
        cfg._worker_scratch_dir = str(scratch_path)
        with (
            _registered_acp_test_controller(),
            patch.dict(os.environ, _capacity_env(state_dir), clear=True),
            patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
            patch.object(
                goalflight_acp_run,
                "validate_acp_dispatch_readiness",
                return_value=None,
            ),
        ):
            payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))

        assert payload.get("state") == "failed_worktree", payload
        with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
            authority = goalflight_journal.open_or_create_journal(
                Path(cfg.project_root)
            )
            attempt = authority.attempt_for_dispatch(dispatch_id)
            record = dispatch_mod.goalflight_ledger.read_record(dispatch_id)
        assert attempt is not None
        assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL, attempt
        assert record is not None
        assert record.get("terminal_state") == "error", record
        assert record.get("prelaunch_failure") is True, record


def test_acp_sandbox_prep_return_terminalizes_first_prepared_attempt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "acp-sandbox-prep-failed"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=tmp / "status.json",
            capacity_wait_s=0.0,
        )
        cfg.os_sandbox = goalflight_os_sandbox.OS_SANDBOX_WORKSPACE_WRITE
        with (
            _registered_acp_test_controller(),
            patch.dict(os.environ, _capacity_env(state_dir), clear=True),
            patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
            patch.object(
                goalflight_acp_run,
                "validate_acp_dispatch_readiness",
                return_value=None,
            ),
            patch.object(goalflight_acp_run, "preflight_os_sandbox", return_value=None),
            patch.object(
                goalflight_acp_run, "validate_os_sandbox_request", return_value=None
            ),
            patch.object(
                goalflight_os_sandbox, "macos_write_roots", return_value=[]
            ),
            patch.object(
                goalflight_acp_run,
                "prepare_os_sandbox_command",
                side_effect=goalflight_acp_run.OsSandboxError("sandbox prep failed"),
            ) as prepare_sandbox,
        ):
            payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))

        assert prepare_sandbox.call_count == 1, prepare_sandbox.call_count
        assert payload.get("state") == "blocked_os_sandbox", payload
        with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
            authority = goalflight_journal.open_or_create_journal(
                Path(cfg.project_root)
            )
            attempt = authority.attempt_for_dispatch(dispatch_id)
            record = dispatch_mod.goalflight_ledger.read_record(dispatch_id)
        assert attempt is not None
        assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL, attempt
        assert record is not None
        assert record.get("terminal_state") == "blocked_os_sandbox", record
        assert record.get("prelaunch_failure") is True, record


def test_acp_interrupts_terminalize_first_prepared_attempt() -> None:
    for interruption in (asyncio.CancelledError, KeyboardInterrupt):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "state"
            dispatch_id = f"acp-interrupted-{interruption.__name__.lower()}"
            cfg = _acp_cfg(
                tmp,
                dispatch_id=dispatch_id,
                status_json=tmp / "status.json",
                capacity_wait_s=0.0,
            )

            def interrupt_setup(*_args, **_kwargs):
                raise interruption()

            with (
                _registered_acp_test_controller(),
                patch.dict(os.environ, _capacity_env(state_dir), clear=True),
                patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
                patch.object(
                    goalflight_acp_run,
                    "validate_acp_dispatch_readiness",
                    return_value=None,
                ),
                patch.object(
                    goalflight_acp_run.goalflight_cursor,
                    "isolate_context_mode",
                    side_effect=interrupt_setup,
                ),
            ):
                try:
                    asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
                except BaseException as exc:
                    assert isinstance(exc, interruption), exc
                else:
                    raise AssertionError(f"{interruption.__name__} was swallowed")

            with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
                authority = goalflight_journal.open_or_create_journal(
                    Path(cfg.project_root)
                )
                attempt = authority.attempt_for_dispatch(dispatch_id)
                record = dispatch_mod.goalflight_ledger.read_record(dispatch_id)
            assert attempt is not None
            assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL, attempt
            assert record is not None
            assert record.get("state") == "failed", record
            assert record.get("terminal_state") == "error", record
            assert record.get("prelaunch_failure") is True, record


def test_concurrent_acp_resumes_publish_under_lock_and_both_launch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    grok_home = home / ".goal-flight" / "accounts" / "seat-g" / "grok" / ".grok"
    grok_home.mkdir(parents=True)
    (grok_home / "config.toml").write_text(
        '[cli]\n\n[ui]\npermission_mode = "always-approve"\n', encoding="utf-8"
    )
    (grok_home / "auth.json").write_text("{}\n", encoding="utf-8")
    env = _capacity_env(
        state_dir,
        HOME=str(home),
        GOALFLIGHT_CAPACITY_CONF=os.devnull,
        GO_FLIGHT_PIDFILE_DIR=str(state_dir / "p"),
        GOALFLIGHT_CODEX_STATE_DIR=str(state_dir),
    )
    saved = _install_fake_acp_after_capacity()
    monkeypatch.setattr(goalflight_acp_run, "cleanup_ghosts", lambda: 0)
    monkeypatch.setattr(
        goalflight_acp_run,
        "agent_command",
        lambda *_args, **_kwargs: (sys.executable, []),
    )

    async def allow_capacity(*_args, **_kwargs):
        return {"decision": "allow", "lease": {"lease_id": None}}

    monkeypatch.setattr(
        goalflight_acp_run.goalflight_capacity,
        "acquire_with_wait_async",
        allow_capacity,
    )
    original_lock = dispatch_mod._resume_publication_lock
    lock_observations: dict[str, bool] = {}
    both_at_lock = threading.Barrier(2)

    @contextlib.contextmanager
    def observe_first_publication(args):
        child_id = str(args.dispatch_id)
        lock_observations[child_id] = (
            dispatch_mod.goalflight_ledger.read_record(child_id) is not None
        )
        both_at_lock.wait(timeout=10)
        with original_lock(args):
            yield

    monkeypatch.setattr(dispatch_mod, "_resume_publication_lock", observe_first_publication)
    try:
        with patch.dict(os.environ, env, clear=True):
            base = _acp_cfg(
                tmp_path,
                dispatch_id="acp-resume-child-one",
                status_json=tmp_path / "child-one.status.json",
                capacity_wait_s=0.0,
            )
            second = SimpleNamespace(**vars(base))
            second.dispatch_id = "acp-resume-child-two"
            second.status_json = str(tmp_path / "child-two.status.json")
            first_session = "00000000-0000-4000-8000-000000000035"
            second_session = "00000000-0000-4000-8000-000000000024"
            _write_grok_resume_parent(
                Path(base.project_root),
                dispatch_id="acp-resume-parent-one",
                session_id=first_session,
            )
            _write_grok_resume_parent(
                Path(base.project_root),
                dispatch_id="acp-resume-parent-two",
                session_id=second_session,
            )
            for cfg, parent_id, session_id in (
                (base, "acp-resume-parent-one", first_session),
                (second, "acp-resume-parent-two", second_session),
            ):
                cfg.agent = "grok-acp"
                cfg.account = "seat-g"
                cfg.parent_dispatch_id = parent_id
                cfg.engine_session_id = session_id

            threads = [
                _run_acp_thread(base),
                _run_acp_thread(second),
            ]
            for thread, _result in threads:
                thread.join(timeout=20)
            assert all(not thread.is_alive() for thread, _result in threads)
            assert lock_observations == {
                "acp-resume-child-one": False,
                "acp-resume-child-two": False,
            }
            for (_thread, result), child_id, parent_id in zip(
                threads,
                ("acp-resume-child-one", "acp-resume-child-two"),
                ("acp-resume-parent-one", "acp-resume-parent-two"),
            ):
                assert "exc" not in result, result.get("exc")
                payload = result["payload"]
                assert payload["state"] == "complete", payload
                child = dispatch_mod.goalflight_ledger.read_record(child_id)
                assert child is not None and child["parent_dispatch_id"] == parent_id
                assert child["state"] == "complete", child
    finally:
        _restore_fake_acp(saved)


def test_acp_resume_losing_sweep_race_refuses_and_terminalizes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    grok_home = home / ".goal-flight" / "accounts" / "seat-g" / "grok" / ".grok"
    grok_home.mkdir(parents=True)
    (grok_home / "config.toml").write_text(
        '[cli]\n\n[ui]\npermission_mode = "always-approve"\n', encoding="utf-8"
    )
    (grok_home / "auth.json").write_text("{}\n", encoding="utf-8")
    env = _capacity_env(
        state_dir,
        HOME=str(home),
        GOALFLIGHT_CAPACITY_CONF=os.devnull,
        GO_FLIGHT_PIDFILE_DIR=str(state_dir / "p"),
        GOALFLIGHT_CODEX_STATE_DIR=str(state_dir),
    )
    old_end = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
    saved = _install_fake_acp_after_capacity()
    monkeypatch.setattr(goalflight_acp_run, "cleanup_ghosts", lambda: 0)
    monkeypatch.setattr(
        goalflight_acp_run,
        "agent_command",
        lambda *_args, **_kwargs: (sys.executable, []),
    )
    monkeypatch.setattr(retention, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    legacy_scratch = tmp_path / "legacy-claude-scratch"
    legacy_scratch.mkdir()
    original_lock = dispatch_mod._resume_publication_lock
    reports: list[dict] = []

    @contextlib.contextmanager
    def sweep_before_first_publication(args):
        report = retention._run_dispatch_retention(
            homes_dir=dispatch_mod._codex_dispatch_homes_dir(),
            ledger_dir=dispatch_mod.goalflight_ledger.runs_dir(create=True),
            dispatch_dir=dispatch_mod._dispatch_base_dir(),
            pidfile_dir=dispatch_mod.goalflight_dispatch_paths.pidfile_dir(),
            legacy_scratch_dir=legacy_scratch,
            now=dt.datetime.now(dt.timezone.utc),
            identity_probe=lambda _record: (False, "dead"),
        )
        reports.append(report)
        with original_lock(args):
            yield

    monkeypatch.setattr(dispatch_mod, "_resume_publication_lock", sweep_before_first_publication)
    monkeypatch.setattr(
        goalflight_acp_run,
        "spawn_and_handshake_with_retry",
        lambda *_args, **_kwargs: pytest.fail("refused resume must not spawn"),
    )
    try:
        with patch.dict(os.environ, env, clear=True):
            cfg = _acp_cfg(
                tmp_path,
                dispatch_id="acp-sweep-race-child",
                status_json=tmp_path / "acp-sweep-race.status.json",
                capacity_wait_s=0.0,
            )
            session_id = "00000000-0000-4000-8000-000000000025"
            session = _write_grok_resume_parent(
                Path(cfg.project_root),
                dispatch_id="acp-sweep-race-parent",
                session_id=session_id,
                ended_at=old_end,
            )
            cfg.agent = "grok-acp"
            cfg.account = "seat-g"
            cfg.parent_dispatch_id = "acp-sweep-race-parent"
            cfg.engine_session_id = session_id
            payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
            assert goalflight_acp_run.acp_dispatch_exit_code(payload) == 64, payload
            assert payload["error"] == "resume source no longer exists", payload
            assert reports[0]["deleted_count"] >= 1, reports
            assert not session.exists()
            assert dispatch_mod.goalflight_ledger.read_record(cfg.dispatch_id) is None
            attempt = goalflight_journal.open_or_create_journal(
                Path(cfg.project_root)
            ).attempt_for_dispatch(cfg.dispatch_id)
            assert attempt is not None
            assert attempt.lifecycle_state in goalflight_journal.ATTEMPT_FINAL_STATES
    finally:
        _restore_fake_acp(saved)


def test_acp_capacity_wait_queues_until_slot_frees() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "queued-acp.status.json"
        lease_id = _hold_capacity(state_dir)
        cfg = _acp_cfg(tmp, dispatch_id="queued-acp", status_json=status_json, capacity_wait_s=6.0)
        saved = _install_fake_acp_after_capacity()
        try:
            # ACP startup's unrelated ghost sweep uses a macOS ``ps`` probe.
            # This capacity test already controls process identity through the
            # fake ACP seam; keep the sandboxed test hermetic and out of ps.
            with (
                patch.dict(os.environ, _capacity_env(state_dir), clear=True),
                patch.object(goalflight_acp_run, "cleanup_ghosts", return_value=0),
            ):
                thread, result = _run_acp_thread(cfg)
                waiting = _wait_for_status(status_json, "waiting_capacity", timeout_s=5.0)
                assert waiting["reason"]["decision"] == "wait", waiting
                ledger = json.loads(
                    dispatch_mod.goalflight_ledger.record_path("queued-acp").read_text(
                        encoding="utf-8"
                    )
                )
                assert ledger["state"] == "waiting_capacity", ledger
                assert "worker_cwd" not in ledger
                assert "engine_session_id" not in ledger
                assert ledger.get("acp_session_id") in (None, "")
                attempt = goalflight_journal.open_or_create_journal(
                    Path(cfg.project_root)
                ).attempt_for_dispatch("queued-acp")
                assert attempt is not None
                assert attempt.lifecycle_state not in goalflight_journal.ATTEMPT_FINAL_STATES
                _release_capacity(state_dir, lease_id)
                thread.join(timeout=20)
                if thread.is_alive():
                    raise AssertionError("ACP queued run did not finish after capacity release")
                if "exc" in result:
                    raise result["exc"]  # type: ignore[misc]
                payload = result["payload"]
        finally:
            _restore_fake_acp(saved)
        final = json.loads(status_json.read_text())
        assert payload["state"] == "complete", payload
        assert final["state"] == "complete", final


def test_acp_capacity_acquire_error_terminalizes_direct_attempt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "acp-capacity-acquire-error"
        status_json = tmp / f"{dispatch_id}.status.json"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=status_json,
            capacity_wait_s=0.0,
        )
        saved = _install_fake_acp_after_capacity()
        try:
            with (
                patch.dict(os.environ, _capacity_env(state_dir), clear=True),
                patch.object(
                    goalflight_acp_run.goalflight_capacity,
                    "acquire_with_wait_async",
                    side_effect=RuntimeError("capacity backend failed"),
                ),
            ):
                try:
                    asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
                except RuntimeError as exc:
                    assert str(exc) == "capacity backend failed"
                else:
                    raise AssertionError("capacity acquisition error did not propagate")
                ledger_path = dispatch_mod.goalflight_ledger.record_path(dispatch_id)
        finally:
            _restore_fake_acp(saved)

        status = json.loads(status_json.read_text(encoding="utf-8"))
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        assert status["state"] == "failed", status
        assert ledger["state"] == "failed", ledger
        assert ledger["terminal_state"] == "error", ledger


def test_acp_capacity_acquire_error_keeps_queue_attempt_prepared() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "queued-acp-capacity-acquire-error"
        status_json = tmp / f"{dispatch_id}.status.json"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=status_json,
            capacity_wait_s=0.0,
        )
        cfg.preserve_capacity_refusal_attempt = True
        saved = _install_fake_acp_after_capacity()
        try:
            with (
                patch.dict(os.environ, _capacity_env(state_dir), clear=True),
                patch.object(
                    goalflight_acp_run.goalflight_capacity,
                    "acquire_with_wait_async",
                    side_effect=RuntimeError("capacity backend failed"),
                ),
            ):
                try:
                    asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
                except RuntimeError as exc:
                    assert str(exc) == "capacity backend failed"
                else:
                    raise AssertionError("capacity acquisition error did not propagate")

                status = json.loads(status_json.read_text(encoding="utf-8"))
                ledger = json.loads(
                    dispatch_mod.goalflight_ledger.record_path(dispatch_id).read_text(
                        encoding="utf-8"
                    )
                )
                attempt = goalflight_journal.open_or_create_journal(
                    Path(cfg.project_root)
                ).attempt_for_dispatch(dispatch_id)
        finally:
            _restore_fake_acp(saved)

        assert attempt is not None
        assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_PREPARED
        assert status["state"] == "starting", status
        assert "terminal_state" not in status, status
        assert ledger["state"] == "waiting_capacity", ledger
        assert ledger["terminal_state"] == "unknown", ledger


def test_acp_task_cancel_during_capacity_wait_terminalizes_prepared_attempt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "acp-task-cancel-during-capacity-wait"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=tmp / f"{dispatch_id}.status.json",
            capacity_wait_s=30.0,
        )
        cfg.dispatch_id = None
        entered_wait = asyncio.Event()

        async def wait_for_capacity(*_args, **_kwargs):
            entered_wait.set()
            await asyncio.Event().wait()

        saved = _install_fake_acp_after_capacity()
        try:
            with (
                _registered_acp_test_controller(),
                patch.dict(os.environ, _capacity_env(state_dir), clear=True),
                patch.object(
                    goalflight_acp_run.goalflight_capacity,
                    "acquire_with_wait_async",
                    side_effect=wait_for_capacity,
                ),
                patch.object(
                    goalflight_acp_run,
                    "_default_dispatch_id",
                    side_effect=[dispatch_id, f"{dispatch_id}-wrong-finalizer"],
                ) as generated_dispatch_id,
            ):
                async def cancel_waiting_task() -> None:
                    task = asyncio.create_task(
                        goalflight_acp_run.run_acp_dispatch(cfg)
                    )
                    await asyncio.wait_for(entered_wait.wait(), timeout=5.0)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        return
                    raise AssertionError("Task.cancel() was swallowed")

                asyncio.run(cancel_waiting_task())
                attempt = goalflight_journal.open_or_create_journal(
                    Path(cfg.project_root)
                ).attempt_for_dispatch(dispatch_id)
                record = dispatch_mod.goalflight_ledger.read_record(dispatch_id)
                assert cfg.dispatch_id == dispatch_id
                assert generated_dispatch_id.call_count == 1
        finally:
            _restore_fake_acp(saved)

        assert attempt is not None
        assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL, attempt
        assert record is not None
        assert record.get("state") == "failed", record
        assert record.get("terminal_state") == "error", record


def test_acp_keyboard_interrupt_during_capacity_wait_terminalizes_prepared_attempt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "acp-keyboard-interrupt-during-capacity-wait"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=tmp / f"{dispatch_id}.status.json",
            capacity_wait_s=30.0,
        )

        async def interrupt_wait(*_args, **_kwargs):
            raise KeyboardInterrupt("capacity wait interrupted")

        saved = _install_fake_acp_after_capacity()
        try:
            with (
                _registered_acp_test_controller(),
                patch.dict(os.environ, _capacity_env(state_dir), clear=True),
                patch.object(
                    goalflight_acp_run.goalflight_capacity,
                    "acquire_with_wait_async",
                    side_effect=interrupt_wait,
                ),
            ):
                try:
                    asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
                except KeyboardInterrupt as exc:
                    assert str(exc) == "capacity wait interrupted"
                else:
                    raise AssertionError("KeyboardInterrupt was swallowed")
                attempt = goalflight_journal.open_or_create_journal(
                    Path(cfg.project_root)
                ).attempt_for_dispatch(dispatch_id)
                record = dispatch_mod.goalflight_ledger.read_record(dispatch_id)
        finally:
            _restore_fake_acp(saved)

        assert attempt is not None
        assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL, attempt
        assert record is not None
        assert record.get("state") == "failed", record
        assert record.get("terminal_state") == "error", record


def test_acp_sigterm_during_initial_lease_status_update_releases_capacity() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "acp-sigterm-initial-lease-status"
        status_json = tmp / f"{dispatch_id}.status.json"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=status_json,
            capacity_wait_s=0.0,
        )
        real_write_status = goalflight_acp_run.write_status
        cancelled_initial_update = False

        class ReceivedSigterm:
            def __init__(self, _loop, _task):
                self.received = True
                self.signum = signal.SIGTERM

            def install(self) -> None:
                pass

            def restore(self) -> None:
                pass

        def interrupt_initial_lease_update(path: Path, payload: dict) -> None:
            nonlocal cancelled_initial_update
            if payload.get("lease_id") and not cancelled_initial_update:
                cancelled_initial_update = True
                raise asyncio.CancelledError()
            real_write_status(path, payload)

        saved = _install_fake_acp_after_capacity()
        try:
            with (
                _registered_acp_test_controller(),
                patch.dict(os.environ, _capacity_env(state_dir), clear=True),
                patch.object(
                    goalflight_acp_run, "_SigtermCancelBridge", ReceivedSigterm
                ),
                patch.object(
                    goalflight_acp_run,
                    "write_status",
                    side_effect=interrupt_initial_lease_update,
                ),
            ):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
                attempt = goalflight_journal.open_or_create_journal(
                    Path(cfg.project_root)
                ).attempt_for_dispatch(dispatch_id)
                record = dispatch_mod.goalflight_ledger.read_record(dispatch_id)
                leases = dispatch_mod.goalflight_capacity.load_state()["leases"]
                dispatch_leases = [
                    lease
                    for lease in leases.values()
                    if lease.get("dispatch_id") == dispatch_id
                ]
        finally:
            _restore_fake_acp(saved)

        assert payload.get("terminated_by_signal") == "SIGTERM", payload
        assert attempt is not None
        assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL, attempt
        assert record is not None
        assert record.get("terminal_state") == "error", record
        assert len(dispatch_leases) == 1, dispatch_leases
        assert (
            dispatch_leases[0].get("state")
            in dispatch_mod.goalflight_capacity.TERMINAL_LEASE_STATES
        ), dispatch_leases


def test_acp_capacity_wait_deadline_blocks() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "deadline-acp.status.json"
        lease_id = _hold_capacity(state_dir)
        cfg = _acp_cfg(tmp, dispatch_id="deadline-acp", status_json=status_json, capacity_wait_s=0.2)
        saved = _install_fake_acp_after_capacity()
        try:
            with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)
            _release_capacity(state_dir, lease_id)
        status = json.loads(status_json.read_text())
        assert payload["state"] == "blocked_capacity", payload
        assert status["reason"]["decision"] == "wait", status
        assert status["reason"]["attempts"] >= 2, status
        assert status["reason"]["waited_s"] >= 0.0, status


def test_acp_capacity_wait_zero_single_shot() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "zero-acp.status.json"
        lease_id = _hold_capacity(state_dir)
        cfg = _acp_cfg(tmp, dispatch_id="zero-acp", status_json=status_json, capacity_wait_s=0.0)
        saved = _install_fake_acp_after_capacity()
        try:
            with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)
            _release_capacity(state_dir, lease_id)
        status = json.loads(status_json.read_text())
        assert payload["state"] == "blocked_capacity", payload
        assert "attempts" not in status["reason"] and "waited_s" not in status["reason"], status


def test_acp_capacity_wait_sigterm_terminalizes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "sigterm-acp.status.json"
        lease_id = _hold_capacity(state_dir)
        cfg = _acp_cfg(tmp, dispatch_id="sigterm-acp", status_json=status_json, capacity_wait_s=6.0)
        saved = _install_fake_acp_after_capacity()
        signal_thread_errors: list[BaseException] = []

        def send_sigterm_after_waiting() -> None:
            try:
                _wait_for_status(status_json, "waiting_capacity", timeout_s=5.0)
                os.kill(os.getpid(), signal.SIGTERM)
            except BaseException as exc:  # pragma: no cover - surfaced below
                signal_thread_errors.append(exc)

        signal_thread = threading.Thread(target=send_sigterm_after_waiting, daemon=True)
        try:
            with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
                signal_thread.start()
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)
            _release_capacity(state_dir, lease_id)
        signal_thread.join(timeout=1)
        if signal_thread_errors:
            raise signal_thread_errors[0]
        status = json.loads(status_json.read_text())
        assert payload["state"] == "blocked_capacity", payload
        assert status["state"] == "blocked_capacity", status
        assert status["reason"]["reason"] == "wait_interrupted", status
        assert status["reason"]["attempts"] == 1, status
        assert status["reason"]["waited_s"] < 6.0, status


def _main_capture_for(agent: str) -> tuple[int, dict[str, object]]:
    captured: dict[str, object] = {}
    old_argv = sys.argv[:]
    old_run = dispatch_mod._run_acp_shape
    old_assign_session = dispatch_mod._ensure_assigned_engine_session
    old_auth_preflight = dispatch_mod._validate_claude_auth_before_attempt
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")

    class ShapeResolved(Exception):
        pass

    def fake_run(args, *, base: Path, account_env: dict[str, str]) -> int:
        captured["agent"] = args.agent
        captured["shape"] = args.shape
        captured["base"] = str(base)
        captured["account_env"] = dict(account_env)
        return 0

    def stop_before_bash_launch(args) -> None:
        if args.shape == "bash":
            captured["agent"] = args.agent
            captured["shape"] = args.shape
            raise ShapeResolved
        old_assign_session(args)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
            dispatch_mod._run_acp_shape = fake_run
            dispatch_mod._ensure_assigned_engine_session = stop_before_bash_launch
            dispatch_mod._validate_claude_auth_before_attempt = lambda *_args, **_kwargs: None
            sys.argv = [
                "goalflight_dispatch.py",
                "--agent",
                agent,
                "--unregistered-forced",
                "--prompt",
                "COMPLETE: no-op",
                "--cwd",
                str(tmp),
            ]
            try:
                rc = dispatch_mod.main()
            except ShapeResolved:
                rc = 0
        finally:
            dispatch_mod._run_acp_shape = old_run
            dispatch_mod._ensure_assigned_engine_session = old_assign_session
            dispatch_mod._validate_claude_auth_before_attempt = old_auth_preflight
            sys.argv = old_argv
            if old_state_dir is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
    return rc, captured


def test_auto_shape_routes_claude_to_acp_and_cursor_to_bash() -> None:
    """Cursor moved to bash-tail; claude keeps acp.

    This asserted that cursor auto-routed to acp. It no longer does: bash-tail is
    measured good for cursor (five of five trials correct, exit 0, ~50s each)
    while acp is the path with no evidence behind it, so cursor now resolves to
    bash and acp is refused outright rather than merely deprioritised.

    The claude half is unchanged and still checked here — the point of the
    original test was that auto-resolution routes deliberately per engine, and
    that is still the property worth pinning.
    """
    # Stop the bash route after shape resolution: this test must not launch a
    # real cursor worker merely to observe which transport main() selected.
    rc, captured = _main_capture_for("cursor")
    assert rc == 0
    assert captured["shape"] == "bash"
    assert captured["agent"] == "cursor"

    rc, captured = _main_capture_for("claude-acp")
    assert rc == 0
    assert captured["shape"] == "acp"
    assert captured["agent"] == "claude"


def test_claude_auth_preflight_refuses_before_attempt() -> None:
    completed = subprocess.CompletedProcess(
        ["claude", "auth", "status", "--json"],
        0,
        stdout='{"loggedIn": false, "authMethod": "none"}\n',
        stderr="",
    )
    auth_probe_calls = 0
    validate_auth = dispatch_mod._validate_claude_auth_before_attempt

    def fake_auth_preflight(args, account_env) -> None:
        nonlocal auth_probe_calls

        def fake_runner(argv, **kwargs):
            nonlocal auth_probe_calls
            auth_probe_calls += 1
            assert argv == ["claude", "auth", "status", "--json"]
            assert "ANTHROPIC_API_KEY" not in kwargs["env"]
            return completed

        validate_auth(args, account_env, runner=fake_runner)

    for shape in ("acp", "bash"):
        dispatch_id = f"claude-auth-preflight-refusal-{shape}"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = {
                "GOALFLIGHT_STATE_DIR": str(tmp / "state"),
                "GOALFLIGHT_JOURNAL_DIR": str(tmp / "journal"),
                "GOALFLIGHT_MESSAGES_DIR": str(tmp / "messages"),
                "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp / "wake-ledger"),
                "GOALFLIGHT_TASK_STORE_DIR": str(tmp / "task-store"),
                "GOAL_FLIGHT_PIDFILE_DIR": str(tmp / "pidfiles"),
                "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
            }
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    dispatch_mod,
                    "_validate_claude_auth_before_attempt",
                    side_effect=fake_auth_preflight,
                ),
                patch.object(
                    dispatch_mod,
                    "_run_acp_shape",
                    side_effect=AssertionError("unauthenticated Claude reached ACP launch"),
                ),
                patch.object(
                    dispatch_mod,
                    "_spawn_daemonized_process",
                    side_effect=AssertionError("unauthenticated Claude reached bash launch"),
                ),
                contextlib.redirect_stderr(stderr),
            ):
                rc = dispatch_mod.main(
                    [
                        "--agent",
                        "claude",
                        "--shape",
                        shape,
                        "--foreground",
                        "--unregistered-forced",
                        "--dispatch-id",
                        dispatch_id,
                        "--prompt",
                        "write a trivial probe file",
                        "--cwd",
                        str(tmp),
                    ]
                )
                assert not dispatch_mod.goalflight_ledger.record_path(dispatch_id).exists()
                assert not list((tmp / "journal").rglob("state-journal.sqlite3"))

        assert rc == 64, stderr.getvalue()
        assert "not authenticated" in stderr.getvalue(), stderr.getvalue()
        assert "claude auth login" in stderr.getvalue(), stderr.getvalue()
    assert auth_probe_calls == 2


def test_claude_auth_preflight_accepts_logged_in_session() -> None:
    calls = 0

    def fake_runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        assert argv == ["claude", "auth", "status", "--json"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"loggedIn": true, "authMethod": "oauth"}\n',
            stderr="",
        )

    args = SimpleNamespace(agent="claude", billing="sub")
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "must-be-stripped"}):
        dispatch_mod._validate_claude_auth_before_attempt(
            args,
            {},
            runner=fake_runner,
        )
    assert calls == 1


def _run_acp_shape_env_capture(agent: str, env_key: str) -> dict[str, str | None]:
    captured: dict[str, str | None] = {}
    old_run = goalflight_acp_run.run_acp_dispatch
    old_value = os.environ.get(env_key)
    old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_dispatch_dir = os.environ.get("GOALFLIGHT_DISPATCH_DIR")

    async def fake_run(cfg):
        captured[env_key] = os.environ.get(env_key)
        return {
            "state": "complete",
            "dispatch_id": cfg.dispatch_id,
            "agent": cfg.agent,
            "worker_pid": None,
            "worker_alive": False,
        }

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        args = _base_acp_args(tmp, agent=agent, dispatch_id=f"{agent}-env")
        try:
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
            os.environ["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "dispatch")
            os.environ[env_key] = "must-not-leak"
            goalflight_acp_run.run_acp_dispatch = fake_run
            rc = dispatch_mod._run_acp_shape(args, base=tmp / "dispatch", account_env={})
        finally:
            goalflight_acp_run.run_acp_dispatch = old_run
            if old_value is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = old_value
            if old_state is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state
            if old_dispatch_dir is None:
                os.environ.pop("GOALFLIGHT_DISPATCH_DIR", None)
            else:
                os.environ["GOALFLIGHT_DISPATCH_DIR"] = old_dispatch_dir
    assert rc == 0
    return captured


def test_subscription_env_scrub_for_cursor_and_claude_acp() -> None:
    assert _run_acp_shape_env_capture("cursor", "CURSOR_API_KEY")["CURSOR_API_KEY"] is None
    assert _run_acp_shape_env_capture("claude", "ANTHROPIC_API_KEY")["ANTHROPIC_API_KEY"] is None


def test_acp_spawn_intent_for_foreground_and_detached_child(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    seen: list[str] = []

    async def fake_run(cfg):
        assert dispatch_mod._SPAWN_INTENT is True
        assert dispatch_mod._SPAWN_INTENT_DISPATCH_ID == cfg.dispatch_id
        seen.append(cfg.dispatch_id)
        return {
            "state": "complete",
            "dispatch_id": cfg.dispatch_id,
            "agent": cfg.agent,
            "worker_pid": None,
            "worker_alive": False,
        }

    monkeypatch.setattr(goalflight_acp_run, "run_acp_dispatch", fake_run)
    for dispatch_id, detached_child in (
        ("acp-spawn-foreground", False),
        ("acp-spawn-detached-child", True),
    ):
        args = _base_acp_args(
            tmp_path, agent="claude", dispatch_id=dispatch_id,
        )
        args.launch_detached = detached_child
        args.acp_detached_child = detached_child
        args.dispatch_warnings = []
        with patch.object(dispatch_mod, "_SPAWN_INTENT", False), patch.object(
            dispatch_mod, "_SPAWN_INTENT_DISPATCH_ID", None
        ), contextlib.redirect_stdout(io.StringIO()):
            assert dispatch_mod._run_acp_shape(
                args, base=tmp_path / "dispatch", account_env={},
            ) == 0

    assert seen == ["acp-spawn-foreground", "acp-spawn-detached-child"]


def test_acp_worker_preserves_exported_remote_ci_config(tmp_path, monkeypatch) -> None:
    configured = str(tmp_path / "operator-config.json")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_CONFIG", configured)

    spawn_env = goalflight_acp_run._worker_spawn_env(
        SimpleNamespace(agent="claude"), None,
    )

    assert spawn_env["GOALFLIGHT_REMOTE_CI_CONFIG"] == configured
    remote_run = Path(spawn_env["GOALFLIGHT_REMOTE_RUN"])
    assert remote_run == (
        Path(goalflight_acp_run.goalflight_dispatch_paths.__file__).resolve().parent.parent
        / "bin" / "goalflight-remote-run"
    ).resolve()
    assert remote_run.is_file() and os.access(remote_run, os.X_OK)


def test_acp_worker_omits_remote_runner_without_project_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    monkeypatch.setenv("GOALFLIGHT_REMOTE_RUN", "/stale/runner")

    spawn_env = goalflight_acp_run._worker_spawn_env(
        SimpleNamespace(agent="claude", project_root=str(tmp_path)), None,
    )

    assert "GOALFLIGHT_REMOTE_RUN" not in spawn_env


def test_acp_launch_uses_project_remote_ci_config_and_grants_its_spool(
    tmp_path, monkeypatch,
) -> None:
    project_root = tmp_path / "pm2"
    worker_cwd = project_root / "worktrees" / "s-1"
    config_path = project_root / ".goal-flight" / "remote-ci.json"
    spool = tmp_path / "state" / "remote-ci" / "projects" / "pm2" / "spool"
    worker_cwd.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"paths": {
        "queue_dir": str(spool / "requests"),
        "result_dir": str(spool / "results"),
    }}), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    captured: dict[str, str] = {}

    async def fake_run(cfg):
        captured.update(goalflight_acp_run._worker_spawn_env(cfg, None))
        return {
            "state": "complete",
            "dispatch_id": cfg.dispatch_id,
            "agent": cfg.agent,
            "worker_pid": None,
            "worker_alive": False,
        }

    args = _base_acp_args(
        worker_cwd, agent="claude", dispatch_id="acp-project-remote-ci",
    )
    args.project_root = str(project_root)
    monkeypatch.setattr(goalflight_acp_run, "run_acp_dispatch", fake_run)

    assert dispatch_mod._run_acp_shape(
        args, base=tmp_path / "dispatch", account_env={},
    ) == 0

    assert captured["GOALFLIGHT_REMOTE_CI_CONFIG"] == str(config_path)
    assert Path(captured["GOALFLIGHT_REMOTE_CI_SPOOL_DIR"]) == spool
    _profile, roots = goalflight_os_sandbox.macos_sandbox_profile(
        str(Path(sys.executable).resolve().parent),
        goalflight_os_sandbox.OS_SANDBOX_WORKSPACE_WRITE,
        agent="claude", command="claude", environment={**os.environ, **captured},
    )
    assert (spool / "requests").resolve() in {
        Path(root).resolve() for root in roots
    }


def test_acp_worker_scratch_is_created_and_exported_for_resume_child() -> None:
    captured: dict[str, tuple[str, str]] = {}
    controller_tmpdir: Path | None = None

    async def fake_run(cfg):
        nonlocal controller_tmpdir
        spawn_env = goalflight_acp_run._worker_spawn_env(cfg, None)
        scratch = spawn_env["GOALFLIGHT_SCRATCH_DIR"]
        Path(scratch).mkdir(parents=True, exist_ok=True, mode=0o700)
        assert Path(scratch).is_dir()
        tmpdir = spawn_env["TMPDIR"]
        assert tmpdir == scratch
        assert os.environ.get("TMPDIR") == str(controller_tmpdir)
        tempfile.tempdir = None
        assert Path(tempfile.gettempdir()) == controller_tmpdir
        captured[cfg.dispatch_id] = (scratch, tmpdir)
        return {
            "state": "complete",
            "dispatch_id": cfg.dispatch_id,
            "agent": cfg.agent,
            "worker_pid": None,
            "worker_alive": False,
        }

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        controller_tmpdir = tmp / "controller-temp"
        controller_tmpdir.mkdir()
        dispatch = tmp / "dispatch"
        with patch.dict(
            os.environ,
            {
                "GOALFLIGHT_STATE_DIR": str(tmp / "state"),
                "GOALFLIGHT_DISPATCH_DIR": str(dispatch),
                "GOALFLIGHT_JOURNAL_DIR": str(tmp / "journals"),
                "GOALFLIGHT_TASK_STORE_DIR": str(tmp / "task-store"),
                "GOALFLIGHT_MESSAGES_DIR": str(tmp / "messages"),
                "GOAL_FLIGHT_PIDFILE_DIR": str(tmp / "pidfiles"),
                "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
                "TMPDIR": str(controller_tmpdir),
            },
        ):
            old_tempdir = tempfile.tempdir
            try:
                with patch.object(goalflight_acp_run, "run_acp_dispatch", fake_run):
                    for dispatch_id, parent_id in (
                        ("claude-scratch-fresh", None),
                        ("claude-scratch-resume-child", "claude-scratch-parent"),
                    ):
                        args = _base_acp_args(tmp, agent="claude", dispatch_id=dispatch_id)
                        args.parent_dispatch_id = parent_id
                        parent_env = os.environ.copy()
                        tempfile.tempdir = None
                        assert dispatch_mod._run_acp_shape(
                            args, base=dispatch, account_env={}
                        ) == 0
                        assert os.environ == parent_env
                        assert tempfile.tempdir == str(controller_tmpdir)
            finally:
                tempfile.tempdir = old_tempdir

    assert captured == {
        "claude-scratch-fresh": (
            str(dispatch / "claude-scratch-fresh.scratch"),
            str(dispatch / "claude-scratch-fresh.scratch"),
        ),
        "claude-scratch-resume-child": (
            str(dispatch / "claude-scratch-resume-child.scratch"),
            str(dispatch / "claude-scratch-resume-child.scratch"),
        ),
    }
    assert captured["claude-scratch-fresh"] != captured["claude-scratch-resume-child"]


def test_detached_acp_env_uses_created_worker_scratch() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        dispatch = tmp_path / "state" / "dispatch"
        args = _base_acp_args(
            tmp_path, agent="claude", dispatch_id="claude-detached-scratch"
        )
        args.launch_detached = True
        captured: dict[str, object] = {}
        cfg = SimpleNamespace(
            agent="claude",
            capacity_wait_s=0,
            _worker_scratch_dir=str(
                dispatch / "claude-detached-scratch.scratch"
            ),
        )

        def capture_detached(_args, **kwargs):
            captured["account_env"] = kwargs["account_env"]
            Path(cfg._worker_scratch_dir).mkdir(
                parents=True, exist_ok=True, mode=0o700
            )
            captured["worker_env"] = goalflight_acp_run._worker_spawn_env(cfg, None)
            return 0

        with (
            patch.dict(
                os.environ,
                {
                    "GOALFLIGHT_STATE_DIR": str(tmp_path / "state"),
                    "GOALFLIGHT_DISPATCH_DIR": str(dispatch),
                },
            ),
            patch.object(
                dispatch_mod,
                "_refuse_reused_dispatch_id_for_launch",
                lambda *_args, **_kwargs: None,
            ),
            patch.object(
                dispatch_mod,
                "_build_acp_cfg",
                lambda *_args, **_kwargs: cfg,
            ),
            patch.object(dispatch_mod, "_emit_dispatch_warnings", lambda *_args, **_kwargs: None),
            patch.object(dispatch_mod, "_capacity_wait_seconds", lambda _args: 0),
            patch.object(dispatch_mod, "_run_acp_detached_launcher", capture_detached),
        ):
            assert dispatch_mod._run_acp_shape(args, base=dispatch, account_env={}) == 0

        account_env = captured["account_env"]
        worker_env = captured["worker_env"]
        assert isinstance(account_env, dict)
        assert isinstance(worker_env, dict)
        assert "TMPDIR" not in account_env
        scratch = Path(worker_env["GOALFLIGHT_SCRATCH_DIR"])
        assert scratch.is_dir()
        assert worker_env["TMPDIR"] == str(scratch)


def main() -> None:
    test_normalize_acp_agents()
    test_build_acp_cfg_agent_liveness_defaults()
    test_build_acp_cfg_injects_orientation_prompt_text()
    test_dispatcher_bound_acp_uses_one_seat_and_records_actual_cwd()
    test_acp_production_path_persists_and_reminds_with_assembled_prompt()
    test_acp_inline_prompt_uses_same_assembled_prompt_path()
    test_acp_prompt_history_rewrite_remains_private()
    test_dispatch_help_skips_legacy_prompt_sweep_mutation_pair()
    test_dispatch_startup_sweep_is_once_and_best_effort_mutation_pair()
    test_dispatch_startup_sweep_marker_serializes_concurrent_first_run()
    test_watcher_with_retired_prompt_and_status_exits_without_status_write()
    test_watcher_both_absent_returns_before_scanner_initialization()
    test_nonterminal_dispatch_record_authorizes_missing_status_creation()
    test_missing_status_without_nonterminal_record_returns_before_scan()
    test_watcher_prompt_history_retains_original_and_bounded_recent_turns()
    test_capacity_env_ignores_constructed_live_journal()
    test_acp_prelaunch_refusals_are_delivered_to_launching_controller()
    test_task_linked_acp_status_admission_defers_without_holding_ledger()
    test_task_linked_acp_releases_ledger_before_handshake_status_write()
    test_task_linked_acp_rechecks_supersession_at_spawn()
    test_task_linked_acp_early_supersession_terminalizes_prepared_attempt()
    test_acp_lost_capacity_lease_terminalizes_prepared_attempt()
    test_acp_scratch_setup_failure_terminalizes_first_prepared_attempt()
    test_acp_sandbox_prep_return_terminalizes_first_prepared_attempt()
    test_acp_interrupts_terminalize_first_prepared_attempt()
    test_acp_capacity_wait_queues_until_slot_frees()
    test_acp_capacity_acquire_error_terminalizes_direct_attempt()
    test_acp_capacity_acquire_error_keeps_queue_attempt_prepared()
    test_acp_task_cancel_during_capacity_wait_terminalizes_prepared_attempt()
    test_acp_keyboard_interrupt_during_capacity_wait_terminalizes_prepared_attempt()
    test_acp_sigterm_during_initial_lease_status_update_releases_capacity()
    test_acp_capacity_wait_deadline_blocks()
    test_acp_capacity_wait_zero_single_shot()
    test_acp_capacity_wait_sigterm_terminalizes()
    test_acp_spawn_refuses_completion_published_before_admission()
    test_auto_shape_routes_claude_to_acp_and_cursor_to_bash()
    test_claude_auth_preflight_refuses_before_attempt()
    test_claude_auth_preflight_accepts_logged_in_session()
    test_subscription_env_scrub_for_cursor_and_claude_acp()
    test_acp_worker_scratch_is_created_and_exported_for_resume_child()
    test_detached_acp_env_uses_created_worker_scratch()
    print("OK: goalflight_dispatch ACP agent tests pass")


if __name__ == "__main__":
    main()
