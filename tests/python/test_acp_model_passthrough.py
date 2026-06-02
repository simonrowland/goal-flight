#!/usr/bin/env python3
"""--model passthrough: a selected model flows into grok/codex commands on both
the ACP path (agent_command) and the bash path (build_worker); default unchanged;
agents without a known --model flag are not touched.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("ACP runner import is POSIX-only in this suite")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_run as acp  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402

MODEL = "grok-composer-2.5-fast"


def case_agent_command_per_agent_placement() -> None:
    # grok ACP: --model must sit BEFORE the `stdio` terminal (verified form).
    _, args = acp.agent_command("grok", model=MODEL)
    assert args[-3:] == ["--model", MODEL, "stdio"], args
    # codex: global `-c model=<id>` override (NOT --model).
    _, args = acp.agent_command("codex", model=MODEL)
    assert args[:2] == ["-c", f"model={MODEL}"] and "--model" not in args, args
    # claude/cursor/opencode: --model ahead of the (sub)command.
    for agent in ("claude", "cursor", "opencode"):
        _, args = acp.agent_command(agent, model=MODEL)
        assert args[:2] == ["--model", MODEL], (agent, args)


def case_agent_command_defaults() -> None:
    # No model -> most agents keep their own default (no selector injected),
    # EXCEPT claude which defaults to its strongest (opus) for worker quality.
    for agent in ("grok", "codex", "cursor", "opencode"):
        base = acp.agent_command(agent)
        assert acp.agent_command(agent, model=None) == base, agent
        flat = " ".join(base[1])
        assert "--model" not in flat and "model=" not in flat, (agent, base)
    for agent in ("claude", "claude-acp"):
        _, args = acp.agent_command(agent)
        assert args[:2] == ["--model", "opus"], (agent, args)
    # explicit --model overrides the opus default (e.g. a fast model for speed).
    _, args = acp.agent_command("claude", model="haiku")
    assert args[:2] == ["--model", "haiku"], args


def _build(agent, model, *, raw=None):
    ns = argparse.Namespace(agent=agent, cwd="/tmp/x", read_only=False, model=model)
    argv, _ = dispatch.build_worker(ns, "/tmp/p.md", raw)
    return argv


def case_build_worker_injects_model() -> None:
    for agent in ("codex", "grok"):
        argv = _build(agent, MODEL)
        assert "--model" in argv and argv[argv.index("--model") + 1] == MODEL, (agent, argv)
        argv0 = _build(agent, None)  # default: no model -> no --model
        assert "--model" not in argv0, (agent, argv0)
    # raw `-- <cmd>` passthrough ignores model (the orchestrator supplies the cmd).
    assert _build("x", MODEL, raw=["echo", "hi"]) == ["echo", "hi"]


def main() -> None:
    case_agent_command_per_agent_placement()
    case_agent_command_defaults()
    case_build_worker_injects_model()
    print("OK: model passthrough tests pass")


if __name__ == "__main__":
    main()
