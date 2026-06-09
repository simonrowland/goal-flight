#!/usr/bin/env python3
"""Focused dispatcher tests for first-class ACP agents."""

from __future__ import annotations

import os
import sys
import tempfile
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


def case_normalize_acp_agents() -> None:
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
    )


def case_build_acp_cfg_agent_liveness_defaults() -> None:
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


def case_auto_shape_routes_cursor_and_claude_to_acp() -> None:
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


def case_subscription_env_scrub_for_cursor_and_claude_acp() -> None:
    assert _run_acp_shape_env_capture("cursor", "CURSOR_API_KEY")["CURSOR_API_KEY"] is None
    assert _run_acp_shape_env_capture("claude", "ANTHROPIC_API_KEY")["ANTHROPIC_API_KEY"] is None


def main() -> None:
    case_normalize_acp_agents()
    case_build_acp_cfg_agent_liveness_defaults()
    case_auto_shape_routes_cursor_and_claude_to_acp()
    case_subscription_env_scrub_for_cursor_and_claude_acp()
    print("OK: goalflight_dispatch ACP agent tests pass")


if __name__ == "__main__":
    main()
