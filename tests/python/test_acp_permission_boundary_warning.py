#!/usr/bin/env python3
"""Hermetic startup warning tests for ACP permission boundary posture."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("ACP dispatch runner is POSIX-only in this suite")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_run  # noqa: E402
from goalflight_acp_boundaries import (  # noqa: E402
    PERMISSION_BOUNDARY_WARNING,
    UNRELIABLE_ESCALATION_AGENTS,
    permission_boundary_warning,
)


def case_warning_predicate_matrix() -> None:
    assert UNRELIABLE_ESCALATION_AGENTS == frozenset(
        {"cursor", "cursor-agent", "grok", "grok-acp"}
    )
    assert "codex" not in UNRELIABLE_ESCALATION_AGENTS
    assert "codex-acp" not in UNRELIABLE_ESCALATION_AGENTS

    for agent in UNRELIABLE_ESCALATION_AGENTS:
        assert permission_boundary_warning(
            agent=agent,
            permission_mode="auto",
            os_sandbox_profile="off",
        ) == PERMISSION_BOUNDARY_WARNING
        assert permission_boundary_warning(
            agent=agent,
            permission_mode="auto",
            os_sandbox_profile="workspace-write",
        ) is None
        assert permission_boundary_warning(
            agent=agent,
            permission_mode="auto",
            os_sandbox_profile="off",
            read_only=True,
        ) is None

    assert permission_boundary_warning(
        agent="codex-acp",
        permission_mode="auto",
        os_sandbox_profile="off",
    ) is None
    assert permission_boundary_warning(
        agent="codex",
        permission_mode="auto",
        os_sandbox_profile="off",
    ) is None
    # Case/whitespace variants must STILL warn (grok F2 P1: agent names are
    # normalized .strip().lower(), so "Cursor"/" GROK " can't slip the set and
    # fail-open on the warning).
    for variant in ("Cursor", "GROK", " grok-acp ", "Cursor-Agent"):
        assert permission_boundary_warning(
            agent=variant,
            permission_mode="auto",
            os_sandbox_profile="off",
        ) == PERMISSION_BOUNDARY_WARNING, variant
    assert permission_boundary_warning(
        agent="cursor",
        permission_mode="inline",
        os_sandbox_profile="off",
    ) is None
    assert permission_boundary_warning(
        agent="cursor",
        permission_mode="inline",
        os_sandbox_profile="off",
        interactive=True,
    ) == PERMISSION_BOUNDARY_WARNING


async def _run_blocked_startup(
    *,
    agent: str,
    permission_mode: str = "auto",
    os_sandbox: str = "off",
    read_only: bool = False,
    interactive: bool = False,
) -> tuple[dict, str]:
    old_agent_command = goalflight_acp_run.agent_command
    old_validate = goalflight_acp_run.validate_acp_dispatch_readiness
    old_acquire = goalflight_acp_run.goalflight_capacity.cmd_acquire
    old_record = goalflight_acp_run.goalflight_ledger.cmd_record
    goalflight_acp_run.agent_command = lambda _agent, model=None, fast=False: (
        sys.executable,
        ["-c", "print('never')"],
    )
    goalflight_acp_run.validate_acp_dispatch_readiness = (
        lambda _agent, _command: {"reason": "probe_required"}
    )
    goalflight_acp_run.goalflight_capacity.cmd_acquire = (
        lambda _args: (_ for _ in ()).throw(AssertionError("capacity acquired"))
    )
    goalflight_acp_run.goalflight_ledger.cmd_record = (
        lambda _args: (_ for _ in ()).throw(AssertionError("ledger mutated"))
    )
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            status_path = tmp / "status.json"
            old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    payload = await goalflight_acp_run.run(
                        argparse.Namespace(
                            agent=agent,
                            install_slot=None,
                            cwd=str(ROOT),
                            worktree="off",
                            session_id=f"{agent}-warning-test",
                            dispatch_id=f"{agent}-warning-test",
                            prompt_id=None,
                            prompt=None,
                            prompt_text="should block before spawn",
                            prompt_b64=None,
                            mode="one-shot",
                            status_json=str(status_path),
                            context_mode="disabled",
                            os_sandbox=os_sandbox,
                            permission_mode=permission_mode,
                            permission_dir=None,
                            permission_inline_timeout_s=None,
                            permission_user_timeout_s=None,
                            read_only=read_only,
                            interactive=interactive,
                            permission_allow_tool_title_pattern=[],
                            heartbeat_interval=5.0,
                            wedge_samples=100,
                            max_tool_s=60.0,
                            max_quiet_s=60.0,
                            progress_stall_s=60.0,
                            liveness_profile=None,
                            remote_turn_silence_s=None,
                            remote_turn_cancel_grace_s=0.0,
                            steer_file=None,
                            cpu_epsilon=0.1,
                            json=False,
                        )
                    )
            finally:
                if old_state_dir is None:
                    os.environ.pop("GOALFLIGHT_STATE_DIR", None)
                else:
                    os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
            status = json.loads(status_path.read_text(encoding="utf-8"))
            assert payload["state"] == "blocked_adapter_gate", payload
            return status, stderr.getvalue()
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_acp_run.validate_acp_dispatch_readiness = old_validate
        goalflight_acp_run.goalflight_capacity.cmd_acquire = old_acquire
        goalflight_acp_run.goalflight_ledger.cmd_record = old_record


def case_runner_status_and_stderr_warning() -> None:
    async def _run() -> None:
        for agent in ("cursor", "grok"):
            status, stderr = await _run_blocked_startup(agent=agent)
            assert status["permission_boundary_warning"] == PERMISSION_BOUNDARY_WARNING, status
            assert PERMISSION_BOUNDARY_WARNING in stderr, stderr
            assert "goalflight_acp_run: WARNING:" in stderr, stderr

        no_warning_cases = [
            {"agent": "codex-acp"},
            {"agent": "cursor", "os_sandbox": "workspace-write"},
            {"agent": "grok", "read_only": True},
        ]
        for kwargs in no_warning_cases:
            status, stderr = await _run_blocked_startup(**kwargs)
            assert "permission_boundary_warning" not in status, (kwargs, status)
            assert PERMISSION_BOUNDARY_WARNING not in stderr, (kwargs, stderr)

    asyncio.run(_run())


def main() -> None:
    case_warning_predicate_matrix()
    case_runner_status_and_stderr_warning()
    print("OK: ACP permission boundary warning tests pass")


if __name__ == "__main__":
    main()
