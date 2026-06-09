#!/usr/bin/env python3
"""--model passthrough: a selected model flows into grok/codex commands on both
the ACP path (agent_command) and the bash path (build_worker); default unchanged;
agents without a known --model flag are not touched.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("ACP runner import is POSIX-only in this suite")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_run as acp  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402

MODEL = "grok-composer-2.5-fast"
RESEARCH_MODEL = "grok-composer-2.5-fast"


class _FakeProc:
    pass


class _FakeConn:
    def __init__(self, *, fail_model: bool = False) -> None:
        self.proc = _FakeProc()
        self.fail_model = fail_model
        self.calls: list[tuple] = []

    async def initialize(self, *, timeout: float) -> None:
        self.calls.append(("initialize", timeout))

    async def new_session(self, cwd: str, *, timeout: float) -> str:
        self.calls.append(("new_session", cwd, timeout))
        return "session-1"

    async def set_session_model(self, model: str, *, timeout: float) -> None:
        self.calls.append(("set_session_model", model, timeout))
        if self.fail_model:
            raise RuntimeError("model rejected")


async def _handshake_calls(agent: str, model: str | None, *, fail_model: bool = False) -> list[tuple]:
    original_spawn = acp.spawn_acp_connection
    fake_conn = _FakeConn(fail_model=fail_model)

    async def fake_spawn(*_args, **_kwargs):
        return fake_conn

    acp.spawn_acp_connection = fake_spawn
    try:
        await acp.spawn_and_handshake_with_retry(
            "agent-bin",
            [],
            agent=agent,
            session_id="session-test",
            cwd="/tmp/x",
            attempts=1,
            handshake_timeout=1.0,
            session_model=model,
        )
    finally:
        acp.spawn_acp_connection = original_spawn
    return fake_conn.calls


def case_agent_command_per_agent_placement() -> None:
    # grok ACP: --model must sit BEFORE the `stdio` terminal (verified form).
    for agent in ("grok", "grok-acp"):
        _, args = acp.agent_command(agent, model=MODEL)
        assert args[-3:] == ["--model", MODEL, "stdio"], (agent, args)
    # codex: global `-c model=<id>` override (NOT --model).
    _, args = acp.agent_command("codex", model=MODEL)
    assert args[:2] == ["-c", f"model={MODEL}"] and "--model" not in args, args
    # cursor/opencode: --model ahead of the (sub)command.
    for agent in ("cursor", "opencode"):
        _, args = acp.agent_command(agent, model=MODEL)
        assert args[:2] == ["--model", MODEL], (agent, args)
    # claude-code-cli-acp enters ACP server mode only with no argv flags.
    for agent in ("claude", "claude-acp"):
        _, args = acp.agent_command(agent, model=MODEL)
        assert args == [], (agent, args)


def case_grok_acp_default_model() -> None:
    _, args = acp.agent_command("grok-acp")
    assert args[-3:] == ["--model", MODEL, "stdio"], args
    _, args_explicit = acp.agent_command("grok-acp", model=None)
    assert args_explicit == ["agent", "--model", MODEL, "stdio"], args_explicit


def case_agent_command_defaults() -> None:
    # No model -> agents keep their own default (no selector injected).
    for agent in ("grok", "codex", "cursor", "opencode", "claude", "claude-acp"):
        base = acp.agent_command(agent)
        assert acp.agent_command(agent, model=None) == base, agent
        flat = " ".join(base[1])
        assert "--model" not in flat and "model=" not in flat, (agent, base)
    # explicit --model is not argv-passed for claude ACP; the adapter would stop
    # speaking ACP on stdio.
    _, args = acp.agent_command("claude", model="haiku")
    assert args == [], args


def case_claude_model_applies_after_session_new() -> None:
    for agent in ("claude", "claude-acp"):
        calls = asyncio.run(_handshake_calls(agent, "haiku"))
        assert calls[:2] == [("initialize", 1.0), ("new_session", "/tmp/x", 1.0)], calls
        assert ("set_session_model", "haiku", 1.0) in calls, (agent, calls)

    for agent in ("grok", "codex", "cursor", "opencode"):
        calls = asyncio.run(_handshake_calls(agent, "haiku"))
        assert all(call[0] != "set_session_model" for call in calls), (agent, calls)

    calls = asyncio.run(_handshake_calls("claude", "haiku", fail_model=True))
    assert ("set_session_model", "haiku", 1.0) in calls, calls


def _build(agent, model, *, raw=None):
    ns = argparse.Namespace(agent=agent, cwd="/tmp/x", read_only=False, model=model)
    argv, _ = dispatch.build_worker(ns, "/tmp/p.md", raw)
    return argv


def case_build_worker_injects_model() -> None:
    for agent in ("codex", "grok-code", "grok-research"):
        argv = _build(agent, MODEL)
        assert "--model" in argv and argv[argv.index("--model") + 1] == MODEL, (agent, argv)
    # codex has no default model: none passed -> no --model.
    argv_codex = _build("codex", None)
    assert "--model" not in argv_codex, argv_codex
    # grok-code defaults to the fast coding model (Composer 2.5) when none is passed.
    argv_code = _build("grok-code", None)
    assert (
        "--model" in argv_code
        and argv_code[argv_code.index("--model") + 1] == "grok-composer-2.5-fast"
    ), argv_code
    # grok-research defaults to grok-composer-2.5-fast (grok-build broken on this box).
    argv_research = _build("grok-research", None)
    assert (
        "--model" in argv_research
        and argv_research[argv_research.index("--model") + 1] == RESEARCH_MODEL
    ), argv_research
    assert "--disable-web-search" not in argv_research, argv_research
    # raw `-- <cmd>` passthrough ignores model (the orchestrator supplies the cmd).
    assert _build("x", MODEL, raw=["echo", "hi"]) == ["echo", "hi"]


def case_retired_bare_grok_agent_label() -> None:
    ns = argparse.Namespace(agent="grok", cwd="/tmp/x", read_only=False, model=None)
    try:
        dispatch._validate_before_side_effects(ns, [])
    except dispatch.DispatchUsageError as exc:
        msg = str(exc)
        assert "retired" in msg.lower(), msg
        assert "grok-code" in msg and "grok-research" in msg, msg
    else:
        raise AssertionError("expected DispatchUsageError for --agent grok")


def main() -> None:
    case_agent_command_per_agent_placement()
    case_grok_acp_default_model()
    case_agent_command_defaults()
    case_claude_model_applies_after_session_new()
    case_build_worker_injects_model()
    case_retired_bare_grok_agent_label()
    print("OK: model passthrough tests pass")


if __name__ == "__main__":
    main()
