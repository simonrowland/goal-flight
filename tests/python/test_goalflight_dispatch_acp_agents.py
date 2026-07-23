#!/usr/bin/env python3
"""Focused dispatcher tests for first-class ACP agents."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_run  # noqa: E402
import goalflight_dispatch as dispatch_mod  # noqa: E402


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


def test_build_acp_cfg_agent_liveness_defaults() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for agent in ("cursor", "claude"):
            args = _base_acp_args(tmp, agent=agent, dispatch_id=f"{agent}-cfg")
            cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / f"{agent}.json")
            assert cfg.agent == agent
            assert cfg.liveness_profile == "remote_api"

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="codex-cfg")
        cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / "codex.json")
        assert cfg.agent == "codex-acp"
        assert cfg.liveness_profile is None

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="priority-cfg")
        args.priority = "bulk"
        args.capacity_wait_s = 12.5
        args.account = "explicit-seat"
        cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / "priority.json")
        assert cfg.priority == "bulk"
        assert cfg.capacity_wait_s == 12.5
        assert cfg.account == "explicit-seat"


def test_build_acp_cfg_injects_orientation_prompt_text() -> None:
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

        args.no_orientation = True
        suppressed = dispatch_mod._build_acp_cfg(args, status_json=tmp / "suppressed.json")
        assert suppressed.prompt == str(prompt.resolve())
        assert suppressed.prompt_text is None
        assert suppressed.original_prompt_file == str(prompt.resolve())


def _capacity_env(state_dir: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
    env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "1"
    env.update(extra)
    return env


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


def _hold_capacity(state_dir: Path, *, agent: str = "fake-acp", dispatch_id: str = "held-acp-capacity") -> str:
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
        ],
    )
    return json.loads(proc.stdout)["lease"]["lease_id"]


def _release_capacity(state_dir: Path, lease_id: str) -> None:
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
    return goalflight_acp_run.normalized_acp_dispatch_cfg(
        SimpleNamespace(
            agent="fake-acp",
            model=None,
            install_slot=None,
            cwd=str(ROOT),
            worktree="off",
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
            json=False,
        )
    )


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

    async def fake_spawn(*_args, **_kwargs):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
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


def test_acp_capacity_wait_queues_until_slot_frees() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "queued-acp.status.json"
        lease_id = _hold_capacity(state_dir)
        cfg = _acp_cfg(tmp, dispatch_id="queued-acp", status_json=status_json, capacity_wait_s=6.0)
        saved = _install_fake_acp_after_capacity()
        old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
        old_max = os.environ.get("GOALFLIGHT_CAPACITY_MAX_TOTAL")
        os.environ.update(_capacity_env(state_dir))
        try:
            thread, result = _run_acp_thread(cfg)
            waiting = _wait_for_status(status_json, "waiting_capacity", timeout_s=5.0)
            assert waiting["reason"]["decision"] == "wait", waiting
            _release_capacity(state_dir, lease_id)
            thread.join(timeout=20)
            if thread.is_alive():
                raise AssertionError("ACP queued run did not finish after capacity release")
            if "exc" in result:
                raise result["exc"]  # type: ignore[misc]
            payload = result["payload"]
        finally:
            _restore_fake_acp(saved)
            if old_state is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state
            if old_max is None:
                os.environ.pop("GOALFLIGHT_CAPACITY_MAX_TOTAL", None)
            else:
                os.environ["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = old_max
        final = json.loads(status_json.read_text())
        assert payload["state"] == "complete", payload
        assert final["state"] == "complete", final


def test_acp_capacity_wait_deadline_blocks() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "deadline-acp.status.json"
        lease_id = _hold_capacity(state_dir)
        cfg = _acp_cfg(tmp, dispatch_id="deadline-acp", status_json=status_json, capacity_wait_s=0.2)
        saved = _install_fake_acp_after_capacity()
        old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
        old_max = os.environ.get("GOALFLIGHT_CAPACITY_MAX_TOTAL")
        os.environ.update(_capacity_env(state_dir))
        try:
            payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)
            _release_capacity(state_dir, lease_id)
            if old_state is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state
            if old_max is None:
                os.environ.pop("GOALFLIGHT_CAPACITY_MAX_TOTAL", None)
            else:
                os.environ["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = old_max
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
        old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
        old_max = os.environ.get("GOALFLIGHT_CAPACITY_MAX_TOTAL")
        os.environ.update(_capacity_env(state_dir))
        try:
            payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)
            _release_capacity(state_dir, lease_id)
            if old_state is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state
            if old_max is None:
                os.environ.pop("GOALFLIGHT_CAPACITY_MAX_TOTAL", None)
            else:
                os.environ["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = old_max
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
        old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
        old_max = os.environ.get("GOALFLIGHT_CAPACITY_MAX_TOTAL")
        signal_thread_errors: list[BaseException] = []

        def send_sigterm_after_waiting() -> None:
            try:
                _wait_for_status(status_json, "waiting_capacity", timeout_s=5.0)
                os.kill(os.getpid(), signal.SIGTERM)
            except BaseException as exc:  # pragma: no cover - surfaced below
                signal_thread_errors.append(exc)

        os.environ.update(_capacity_env(state_dir))
        signal_thread = threading.Thread(target=send_sigterm_after_waiting, daemon=True)
        try:
            signal_thread.start()
            payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)
            _release_capacity(state_dir, lease_id)
            if old_state is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state
            if old_max is None:
                os.environ.pop("GOALFLIGHT_CAPACITY_MAX_TOTAL", None)
            else:
                os.environ["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = old_max
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
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")

    def fake_run(args, *, base: Path, account_env: dict[str, str]) -> int:
        captured["agent"] = args.agent
        captured["shape"] = args.shape
        captured["base"] = str(base)
        captured["account_env"] = dict(account_env)
        return 0

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
            dispatch_mod._run_acp_shape = fake_run
            sys.argv = [
                "goalflight_dispatch.py",
                "--agent",
                agent,
                "--prompt",
                "COMPLETE: no-op",
                "--cwd",
                str(tmp),
            ]
            rc = dispatch_mod.main()
        finally:
            dispatch_mod._run_acp_shape = old_run
            sys.argv = old_argv
            if old_state_dir is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
    return rc, captured


def test_auto_shape_routes_cursor_and_claude_to_acp() -> None:
    rc, captured = _main_capture_for("cursor")
    assert rc == 0
    assert captured["shape"] == "acp"
    assert captured["agent"] == "cursor"

    rc, captured = _main_capture_for("claude-acp")
    assert rc == 0
    assert captured["shape"] == "acp"
    assert captured["agent"] == "claude"


def _run_acp_shape_env_capture(agent: str, env_key: str) -> dict[str, str | None]:
    captured: dict[str, str | None] = {}
    old_run = goalflight_acp_run.run_acp_dispatch
    old_value = os.environ.get(env_key)

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
            os.environ[env_key] = "must-not-leak"
            goalflight_acp_run.run_acp_dispatch = fake_run
            rc = dispatch_mod._run_acp_shape(args, base=tmp / "dispatch", account_env={})
        finally:
            goalflight_acp_run.run_acp_dispatch = old_run
            if old_value is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = old_value
    assert rc == 0
    return captured


def test_subscription_env_scrub_for_cursor_and_claude_acp() -> None:
    assert _run_acp_shape_env_capture("cursor", "CURSOR_API_KEY")["CURSOR_API_KEY"] is None
    assert _run_acp_shape_env_capture("claude", "ANTHROPIC_API_KEY")["ANTHROPIC_API_KEY"] is None


def main() -> None:
    test_normalize_acp_agents()
    test_build_acp_cfg_agent_liveness_defaults()
    test_build_acp_cfg_injects_orientation_prompt_text()
    test_acp_capacity_wait_queues_until_slot_frees()
    test_acp_capacity_wait_deadline_blocks()
    test_acp_capacity_wait_zero_single_shot()
    test_acp_capacity_wait_sigterm_terminalizes()
    test_auto_shape_routes_cursor_and_claude_to_acp()
    test_subscription_env_scrub_for_cursor_and_claude_acp()
    print("OK: goalflight_dispatch ACP agent tests pass")


if __name__ == "__main__":
    main()
