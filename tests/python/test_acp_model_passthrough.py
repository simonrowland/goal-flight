#!/usr/bin/env python3
"""--model passthrough: a selected model flows into grok/codex commands on both
the ACP path (agent_command) and the bash path (build_worker); default unchanged;
agents without a known --model flag are not touched.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("ACP runner import is POSIX-only in this suite")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("GOALFLIGHT_ACP_PYTHON", sys.executable)

import goalflight_acp_run as acp  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402

MODEL = "grok-composer-2.5-fast"  # a valid explicit --model pin (still selectable)
FAST_TIER = "service_tier=priority"
FAST_NOTE = "FAST: codex-acp service_tier=priority — premium processing (~1.5x token spend)"
# Both grok presets inject NO --model by default — grok's own CLI default
# (grok-4.5 as of 2026-07-08) applies, auto-tracking forward. An explicit
# --model still passes through (tested with MODEL above).


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


def case_agent_command_fast_tier() -> None:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        _, fast_args = acp.agent_command("codex-acp", fast=True)
    assert fast_args[:2] == ["-c", FAST_TIER], fast_args
    assert stderr.getvalue().strip() == FAST_NOTE, stderr.getvalue()

    _, default_args = acp.agent_command("codex-acp", fast=False)
    assert FAST_TIER not in default_args, default_args

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        _, grok_args = acp.agent_command("grok-acp", fast=True)
    assert FAST_TIER not in grok_args and grok_args[-1] == "stdio", grok_args
    assert stderr.getvalue() == "", stderr.getvalue()

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        _, combined_args = acp.agent_command("codex-acp", model="X", fast=True)
    assert combined_args[:4] == ["-c", FAST_TIER, "-c", "model=X"], combined_args
    assert stderr.getvalue().strip() == FAST_NOTE, stderr.getvalue()


def case_grok_acp_default_model() -> None:
    # grok-acp now omits --model by default — grok's CLI default (grok-4.5)
    # applies and writes reliably through ACP (validated live 2026-07-08); the
    # old composer ACP pin is retired.
    _, args = acp.agent_command("grok-acp")
    assert args == ["agent", "stdio"], args
    _, args_none = acp.agent_command("grok-acp", model=None)
    assert args_none == ["agent", "stdio"], args_none
    # an explicit model still passes through (before the stdio terminal).
    _, args_explicit = acp.agent_command("grok-acp", model=MODEL)
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
    # grok-code / grok-research inject NO --model by default — grok's own CLI
    # default (grok-4.5) applies, auto-tracking forward. Explicit --model above
    # still passes through.
    argv_code = _build("grok-code", None)
    assert "--model" not in argv_code, argv_code
    argv_research = _build("grok-research", None)
    assert "--model" not in argv_research, argv_research
    # grok-research keeps web tools ON (grok-4.5 supports web_search/web_fetch).
    assert "--disable-web-search" not in argv_research, argv_research
    # grok 0.2.39 regression: in single-turn `--prompt-file` mode EVERY
    # `--permission-mode` value stops the file-write tool from writing (none produce
    # the file); the empty no-ops surface as worker_dead_no_terminal_marker. Omitting
    # the flag is the only invocation that writes in-cwd non-interactively. Lock the
    # omit for both presets so a future "re-add acceptEdits" cannot regress
    # edit-heavy chunks.
    for agent in ("grok-code", "grok-research"):
        argv = _build(agent, None)
        assert "--permission-mode" not in argv, (agent, argv)
        assert "acceptEdits" not in argv, (agent, argv)
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


def _acp_dispatch_args(
    *,
    cwd: Path,
    prompt_file: str | None = None,
    prompt: str | None = None,
    fast: bool = False,
):
    return argparse.Namespace(
        agent="codex-acp",
        model=None,
        fast=fast,
        cwd=str(cwd),
        read_only=False,
        dispatch_id="acp-prompt-env",
        task_ids=[],
        priority="normal",
        capacity_wait_s=None,
        prompt_file=prompt_file,
        prompt=prompt,
        max_idle_secs=300.0,
        poll_secs=0.1,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        interactive=False,
        queue_launch_token=None,
    )


def case_dispatch_acp_cfg_passes_resolved_prompt_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prompt = tmp / "brief.md"
        prompt.write_text("do work\n", encoding="utf-8")
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            args = _acp_dispatch_args(cwd=tmp, prompt_file="brief.md")
            cfg = dispatch._build_acp_cfg(args, status_json=tmp / "status.json", base=tmp / "dispatch")
        finally:
            os.chdir(old_cwd)

        assert cfg.prompt == str(prompt.resolve()), cfg.prompt
        assert cfg.original_prompt_file == str(prompt.resolve()), cfg.original_prompt_file
        env = acp._worker_spawn_env(cfg, acp._resolve_original_prompt_file(cfg))
        assert env["GOALFLIGHT_PROMPT_FILE"] == str(prompt.resolve()), env.get("GOALFLIGHT_PROMPT_FILE")


def case_dispatch_acp_cfg_passes_fast() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        args = _acp_dispatch_args(cwd=tmp, prompt="do work", fast=True)
        cfg = dispatch._build_acp_cfg(args, status_json=tmp / "status.json")
    assert cfg.fast is True, cfg.fast


def case_direct_acp_prompt_exports_resolved_prompt_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prompt = tmp / "direct.md"
        prompt.write_text("direct work\n", encoding="utf-8")
        old_cwd = Path.cwd()
        old_env = os.environ.get("GOALFLIGHT_PROMPT_FILE")
        os.environ["GOALFLIGHT_PROMPT_FILE"] = "stale"
        os.chdir(tmp)
        try:
            cfg = argparse.Namespace(agent="codex-acp", install_slot=None, prompt="direct.md", original_prompt_file=None)
            resolved = acp._resolve_original_prompt_file(cfg)
            env = acp._worker_spawn_env(cfg, resolved)
        finally:
            os.chdir(old_cwd)
            if old_env is None:
                os.environ.pop("GOALFLIGHT_PROMPT_FILE", None)
            else:
                os.environ["GOALFLIGHT_PROMPT_FILE"] = old_env

        assert resolved == str(prompt.resolve()), resolved
        assert env["GOALFLIGHT_PROMPT_FILE"] == str(prompt.resolve()), env.get("GOALFLIGHT_PROMPT_FILE")


def case_promptless_acp_spawn_env_clears_prompt_file() -> None:
    old_env = os.environ.get("GOALFLIGHT_PROMPT_FILE")
    os.environ["GOALFLIGHT_PROMPT_FILE"] = "stale"
    try:
        cfg = argparse.Namespace(agent="codex-acp", install_slot=None, prompt=None, original_prompt_file=None)
        env = acp._worker_spawn_env(cfg, acp._resolve_original_prompt_file(cfg))
    finally:
        if old_env is None:
            os.environ.pop("GOALFLIGHT_PROMPT_FILE", None)
        else:
            os.environ["GOALFLIGHT_PROMPT_FILE"] = old_env

    assert "GOALFLIGHT_PROMPT_FILE" not in env, env.get("GOALFLIGHT_PROMPT_FILE")


def case_acp_prompt_file_preamble_is_shared() -> None:
    assembled = acp._prompt_with_original_prompt_file_preamble("body\n", "/tmp/brief.md")
    assert dispatch.PROMPT_FILE_PREAMBLE in assembled, assembled
    assert assembled.endswith("body\n"), assembled
    assert acp._prompt_with_original_prompt_file_preamble("body", None) == "body"


def test_acp_prompt_file_env_and_preamble() -> None:
    case_dispatch_acp_cfg_passes_resolved_prompt_file()
    case_direct_acp_prompt_exports_resolved_prompt_file()
    case_promptless_acp_spawn_env_clears_prompt_file()
    case_acp_prompt_file_preamble_is_shared()


def main() -> None:
    case_agent_command_per_agent_placement()
    case_agent_command_fast_tier()
    case_grok_acp_default_model()
    case_agent_command_defaults()
    case_claude_model_applies_after_session_new()
    case_build_worker_injects_model()
    case_retired_bare_grok_agent_label()
    case_dispatch_acp_cfg_passes_resolved_prompt_file()
    case_dispatch_acp_cfg_passes_fast()
    case_direct_acp_prompt_exports_resolved_prompt_file()
    case_promptless_acp_spawn_env_clears_prompt_file()
    case_acp_prompt_file_preamble_is_shared()
    print("OK: model passthrough tests pass")


if __name__ == "__main__":
    main()
