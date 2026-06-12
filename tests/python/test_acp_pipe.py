#!/usr/bin/env python3
"""Hermetic SDK transport tests."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("exercises POSIX ACP worker process lifecycle")

import asyncio
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from acp_pool import compute_pool_ceiling, managed_pool  # noqa: E402
from acp_runner import (  # noqa: E402
    TERMINAL_MARKERS,
    early_actionable_marker,
    extract_markers,
    has_actionable_marker_values,
    is_sentinel_marker_payload,
    run_prompt,
)
import goalflight_acp_permits as permits  # noqa: E402
import goalflight_acp_run  # noqa: E402
import goalflight_adapter_readiness  # noqa: E402
from goalflight_acp_client import spawn_acp_connection  # noqa: E402

FAKE = ROOT / "tests/fixtures/acp_fake_agent.py"


def _write_supported_adapter_manifest(directory: Path, name: str) -> None:
    (directory / f"{name}.json").write_text(json.dumps({
        "support": {
            "controller": {"capability": "supported", "fallback": "worker_only"},
            "worker": {"capability": "supported", "transport": ["acp"], "fallback": "tail_file"},
        },
        "local_readiness_state": {
            "controller": "probe_required",
            "worker": "probe_required",
            "last_probe_ids": ["python-version"],
        },
        "live_gate": {"function": "validate_adapter_gate", "default": "deny"},
        "status_contract": {"terminal_states": ["complete"], "stale_after_s": 60},
        "permission_surface": {
            "plugin_sandbox": {},
            "auto_approve_detection": {"strict_fail": True},
        },
        "discovery": {
            "probes": [{
                "id": "python-version",
                "argv": [sys.executable, "--version"],
                "safe_for_setup": True,
                "network": False,
                "model_consuming": False,
            }],
        },
        "invocation": {"exec": {"arg_policy": {"forbidden_args": []}}},
    }))


async def _connect(scenario: str, *, limit: str | None = None, drain_cap: str | None = None):
    old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
    old_limit = os.environ.get("GOALFLIGHT_ACP_LIMIT")
    old_drain_cap = os.environ.get("GOALFLIGHT_ACP_OVERSIZED_DRAIN_CAP")
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = scenario
    if limit is not None:
        os.environ["GOALFLIGHT_ACP_LIMIT"] = limit
    else:
        os.environ.pop("GOALFLIGHT_ACP_LIMIT", None)
    if drain_cap is not None:
        os.environ["GOALFLIGHT_ACP_OVERSIZED_DRAIN_CAP"] = drain_cap
    else:
        os.environ.pop("GOALFLIGHT_ACP_OVERSIZED_DRAIN_CAP", None)
    try:
        conn = await spawn_acp_connection(
            sys.executable,
            [str(FAKE)],
            agent="fake",
            session_id=f"test-{scenario}",
            cwd=str(ROOT),
        )
        await conn.initialize()
        await conn.new_session(str(ROOT))
        return conn
    finally:
        if old_scenario is None:
            os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
        else:
            os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old_scenario
        if old_limit is None:
            os.environ.pop("GOALFLIGHT_ACP_LIMIT", None)
        else:
            os.environ["GOALFLIGHT_ACP_LIMIT"] = old_limit
        if old_drain_cap is None:
            os.environ.pop("GOALFLIGHT_ACP_OVERSIZED_DRAIN_CAP", None)
        else:
            os.environ["GOALFLIGHT_ACP_OVERSIZED_DRAIN_CAP"] = old_drain_cap


async def _connect_inline(
    scenario: str,
    *,
    permission_dir: str,
    inline_timeout: float = 180.0,
    user_timeout: float = 36000.0,
):
    """Spawn a fake-agent connection with permission_mode='inline' and an explicit
    (test-isolated) permission_dir so the inline file-IPC round-trip is hermetic."""
    old = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = scenario
    try:
        conn = await spawn_acp_connection(
            sys.executable,
            [str(FAKE)],
            agent="fake",
            session_id=f"test-{scenario}",
            cwd=str(ROOT),
            auto_allow_tools=True,
            permission_mode="inline",
            permission_dir=permission_dir,
            permission_inline_timeout_s=inline_timeout,
            permission_user_timeout_s=user_timeout,
        )
        await conn.initialize()
        await conn.new_session(str(ROOT))
        return conn
    finally:
        if old is None:
            os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
        else:
            os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old


async def _relay_decision(
    directory: str,
    decision: str,
    *,
    option_id: str | None = None,
    hold_extra: float = 0.0,
    timeout: float = 20.0,
):
    """Simulate the orchestrator relay: wait for a pending inline request to appear,
    optionally HOLD `hold_extra` seconds (to exercise run_prompt's idle tolerance),
    then write the decision. Returns the request record, or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reqs = permits.list_requests(directory)
        if reqs:
            if hold_extra:
                await asyncio.sleep(hold_extra)
            permits.write_decision(directory, reqs[0]["key"], decision, option_id)
            return reqs[0]
        await asyncio.sleep(0.05)
    return None


async def case_echo_roundtrip() -> None:
    conn = await _connect("echo")
    try:
        result = await run_prompt(conn, "hello", idle_timeout=5)
        assert result.ok, result
        assert result.text == "echo"
        assert conn.verified_pgid == conn.proc.pid
        assert conn.client.activity.raw_events_seen >= 3
        assert conn.client.activity.wedge_progress_seen >= 1
    finally:
        await conn.kill()


async def case_overlimit_frame_drops_and_continues() -> None:
    conn = await _connect("overlimit", limit="4k")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert result.text == "after-limit", result.text
        assert conn.guarded_reader.dropped_frames == 1
        assert conn.client.activity.dropped_frames == 1
        snapshot = conn.client.activity.snapshot()
        assert snapshot["dropped_frames"] == 1
        record = snapshot["dropped_frame_records"][0]
        assert record["byte_count"] > 4096, record
        assert record["kind"] == "notification", record
        assert len(record["head"].encode("utf-8")) <= 1024, record
        assert "x" * 2048 not in record["head"], record
    finally:
        await conn.kill()


async def case_overlimit_request_gets_safe_error_reply() -> None:
    conn = await _connect("overlimit_request", limit="4k")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert result.text == "request-error:oversized frame dropped", result.text
        record = conn.client.activity.snapshot()["dropped_frame_records"][0]
        assert record["kind"] == "request", record
        assert record["id"] == 4242, record
        assert record["safe_reply_sent"] is True, record
        assert record["byte_count"] > 4096, record
        assert len(record["head"].encode("utf-8")) <= 1024, record
    finally:
        await conn.kill()


async def case_overlimit_request_late_id_gets_safe_error_reply() -> None:
    conn = await _connect("overlimit_request_late_id", limit="4k")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert result.text == "request-error:oversized frame dropped", result.text
        record = conn.client.activity.snapshot()["dropped_frame_records"][0]
        assert record["kind"] == "request", record
        assert record["id"] == 5151, record
        assert record["safe_reply_sent"] is True, record
        assert record.get("id_unrecoverable") is not True, record
        assert record["byte_count"] > 4096, record
        assert len(record["head"].encode("utf-8")) <= 1024, record
        assert "x" * 2048 not in record["head"], record
    finally:
        await conn.kill()


async def case_overlimit_no_newline_hits_drain_cap() -> None:
    conn = await _connect("overlimit_no_newline", limit="4k", drain_cap="8k")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert not result.ok, result
        assert result.error, result
        record = conn.client.activity.snapshot()["dropped_frame_records"][0]
        assert record["kind"] == "notification", record
        assert record["drain_cap_exceeded"] is True, record
        assert record["drain_cap"] == 8192, record
        assert record["byte_count"] > 8192, record
        assert len(record["head"].encode("utf-8")) <= 1024, record
        assert conn.alive is False, record
    finally:
        await conn.kill()


async def case_stderr_burst_without_newline_drains() -> None:
    old_burst = os.environ.get("GOALFLIGHT_FAKE_ACP_STDERR_BURST_BYTES")
    os.environ["GOALFLIGHT_FAKE_ACP_STDERR_BURST_BYTES"] = str(2 * 1024 * 1024)
    try:
        conn = await _connect("stderr_burst", limit="4k")
    finally:
        if old_burst is None:
            os.environ.pop("GOALFLIGHT_FAKE_ACP_STDERR_BURST_BYTES", None)
        else:
            os.environ["GOALFLIGHT_FAKE_ACP_STDERR_BURST_BYTES"] = old_burst
    try:
        result = await asyncio.wait_for(run_prompt(conn, "go", idle_timeout=3), timeout=8)
        assert result.ok, result
        assert result.text == "stderr-burst-done", result.text
    finally:
        await conn.kill()


async def case_overlimit_response_fails_cleanly() -> None:
    conn = await _connect("overlimit_response", limit="4k")
    start = time.monotonic()
    try:
        result = await run_prompt(conn, "go", idle_timeout=0.2)
        elapsed = time.monotonic() - start
        assert not result.ok, result
        assert result.error and result.error.get("message") == "agent_timeout (idle)", result
        assert elapsed < 4.0, elapsed
        assert conn.guarded_reader.dropped_frames == 1
        assert conn.client.activity.dropped_frames == 1
    finally:
        await conn.kill()


async def case_runner_overlimit_response_status_counts_drop() -> None:
    old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
    old_limit = os.environ.get("GOALFLIGHT_ACP_LIMIT")
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_agent_command = goalflight_acp_run.agent_command
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "overlimit_response"
    os.environ["GOALFLIGHT_ACP_LIMIT"] = "4k"
    goalflight_acp_run.agent_command = lambda agent, model=None: (sys.executable, [str(FAKE)])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
            _write_supported_adapter_manifest(tmp_path, "fake-runner")
            status_path = tmp_path / "status.json"
            dispatch_id = f"test-runner-overlimit-{os.getpid()}"
            payload = await goalflight_acp_run.run(
                argparse.Namespace(
                    agent="fake-runner",
                    cwd=str(ROOT),
                    session_id=f"{dispatch_id}-session",
                    dispatch_id=dispatch_id,
                    prompt_id=None,
                    prompt=None,
                    prompt_text="go",
                    mode="one-shot",
                    status_json=str(status_path),
                    idle_timeout=0.2,
                    heartbeat_interval=5.0,
                    wedge_samples=100,
                    max_tool_s=60.0,
                    max_quiet_s=60.0,
                    cpu_epsilon=0.1,
                )
            )
            status = json.loads(status_path.read_text())
            assert payload["state"] == "failed", payload
            assert status["state"] == "failed", status
            assert payload.get("error", {}).get("message") == "agent_timeout (idle)", payload
            assert status.get("acp_dropped_frames") == 1, status
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        if old_scenario is None:
            os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
        else:
            os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old_scenario
        if old_limit is None:
            os.environ.pop("GOALFLIGHT_ACP_LIMIT", None)
        else:
            os.environ["GOALFLIGHT_ACP_LIMIT"] = old_limit
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir


async def case_runner_blocks_probe_required_adapter() -> None:
    old_agent_command = goalflight_acp_run.agent_command
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    goalflight_acp_run.agent_command = lambda agent, model=None: (sys.executable, [str(FAKE)])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            (tmp_path / "blocked-runner.json").write_text(json.dumps({
                "support": {
                    "controller": {"capability": "supported", "fallback": "worker_only"},
                    "worker": {"capability": "supported", "transport": ["acp"], "fallback": "tail_file"},
                },
                "local_readiness_state": {
                    "controller": "probe_required",
                    "worker": "probe_required",
                    "last_probe_ids": ["missing-probe"],
                },
                "live_gate": {"function": "validate_adapter_gate", "default": "deny"},
                "status_contract": {"terminal_states": ["complete"], "stale_after_s": 60},
                "permission_surface": {
                    "plugin_sandbox": {},
                    "auto_approve_detection": {"strict_fail": True},
                },
                "discovery": {"probes": []},
                "invocation": {"exec": {"arg_policy": {"forbidden_args": []}}},
            }))
            status_path = tmp_path / "status.json"
            payload = await goalflight_acp_run.run(argparse.Namespace(
                agent="blocked-runner",
                cwd=str(ROOT),
                session_id=f"blocked-{os.getpid()}",
                dispatch_id=f"blocked-{os.getpid()}",
                prompt_id=None,
                prompt=None,
                prompt_text="should not spawn",
                mode="one-shot",
                status_json=str(status_path),
                idle_timeout=0.2,
                heartbeat_interval=5.0,
                wedge_samples=100,
                max_tool_s=60.0,
                max_quiet_s=60.0,
                progress_stall_s=60.0,
                liveness_profile=None,
                remote_turn_silence_s=None,
                remote_turn_cancel_grace_s=0.0,
                cpu_epsilon=0.1,
            ))
            assert payload["state"] == "blocked_adapter_gate", payload
            assert payload["worker_pid"] is None, payload
            assert payload["error"]["reason"] == "probe_required", payload
            assert status_path.exists(), "blocked gate should write status"
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir


async def case_runner_blocks_invalid_adapter_manifest() -> None:
    old_agent_command = goalflight_acp_run.agent_command
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    goalflight_acp_run.agent_command = lambda agent, model=None: (sys.executable, [str(FAKE)])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            (tmp_path / "invalid-runner.json").write_text("{not-json")
            status_path = tmp_path / "status.json"
            payload = await goalflight_acp_run.run(argparse.Namespace(
                agent="invalid-runner",
                cwd=str(ROOT),
                session_id=f"invalid-{os.getpid()}",
                dispatch_id=f"invalid-{os.getpid()}",
                prompt_id=None,
                prompt=None,
                prompt_text="should not spawn",
                mode="one-shot",
                status_json=str(status_path),
                idle_timeout=0.2,
                heartbeat_interval=5.0,
                wedge_samples=100,
                max_tool_s=60.0,
                max_quiet_s=60.0,
                progress_stall_s=60.0,
                liveness_profile=None,
                remote_turn_silence_s=None,
                remote_turn_cancel_grace_s=0.0,
                cpu_epsilon=0.1,
            ))
            assert payload["state"] == "blocked_adapter_gate", payload
            assert payload["worker_pid"] is None, payload
            assert payload["error"]["reason"] == "adapter_manifest_invalid", payload
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir


async def case_runner_blocks_missing_adapter_manifest() -> None:
    old_agent_command = goalflight_acp_run.agent_command
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    goalflight_acp_run.agent_command = lambda agent, model=None: (sys.executable, [str(FAKE)])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            status_path = tmp_path / "status.json"
            payload = await goalflight_acp_run.run(argparse.Namespace(
                agent="missing-runner",
                cwd=str(ROOT),
                session_id=f"missing-{os.getpid()}",
                dispatch_id=f"missing-{os.getpid()}",
                prompt_id=None,
                prompt=None,
                prompt_text="should not spawn",
                mode="one-shot",
                status_json=str(status_path),
                idle_timeout=0.2,
                heartbeat_interval=5.0,
                wedge_samples=100,
                max_tool_s=60.0,
                max_quiet_s=60.0,
                progress_stall_s=60.0,
                liveness_profile=None,
                remote_turn_silence_s=None,
                remote_turn_cancel_grace_s=0.0,
                cpu_epsilon=0.1,
            ))
            assert payload["state"] == "blocked_adapter_gate", payload
            assert payload["worker_pid"] is None, payload
            assert payload["error"]["reason"] == "adapter_manifest_missing", payload
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir


async def case_permission_auto_allow() -> None:
    conn = await _connect("permission")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert "permission:opt_once" in result.text
        assert conn.client.activity.outstanding_count() == 0
    finally:
        await conn.kill()


async def case_permission_codex_shape_unblocks() -> None:
    # Real codex-acp built-in-tool gate shape: allow_once + reject_once, no
    # allow_always (captured 2026-05-21). Auto-allow must pick the allow_once
    # ('approved'), the worker must unblock, and the gate must NOT be answered
    # with the reject ('abort'). This is the hermetic permission-round-trip
    # regression for the shape codex-acp actually sends.
    conn = await _connect("permission_codex")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert "permission:approved" in result.text, result.text
        assert "permission:abort" not in result.text, result.text
        assert conn.client.activity.outstanding_count() == 0
    finally:
        await conn.kill()


async def case_permission_reject_first_picks_allow() -> None:
    # Reject option offered FIRST. The old options[0] fallback would have
    # answered with 'abort' (auto-allow turned into auto-DENY); the selector
    # must still pick the allow_once 'approved'.
    conn = await _connect("permission_reject_first")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert "permission:approved" in result.text, result.text
        assert "permission:abort" not in result.text, result.text
    finally:
        await conn.kill()


async def case_permission_reject_only_cancels_and_unblocks() -> None:
    # Only a reject option exists: auto-allow cannot grant, so it cancels
    # cleanly. The point is liveness — the worker still gets a definitive
    # answer and completes its turn (never wedges), and we never wrap the
    # reject id in an AllowedOutcome.
    conn = await _connect("permission_reject_only")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert "permission:abort" not in result.text, result.text
        assert conn.client.activity.outstanding_count() == 0
    finally:
        await conn.kill()


async def case_permission_auto_allow_false_denies_and_unblocks() -> None:
    # auto_allow_tools=False must DENY cleanly through the full subprocess path,
    # NOT hang. The worker sends request_permission; the handler returns
    # DeniedOutcome(cancelled); the worker still reaches end_turn with
    # outstanding_count()==0. Guards the 0.3.0 "every worker hangs on
    # method_not_found" regression on the real liveness path (the unit test only
    # proves no-raise).
    old = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "permission_codex"
    try:
        conn = await spawn_acp_connection(
            sys.executable, [str(FAKE)], agent="fake",
            session_id="test-noallow", cwd=str(ROOT), auto_allow_tools=False,
        )
        await conn.initialize()
        await conn.new_session(str(ROOT))
        try:
            result = await run_prompt(conn, "go", idle_timeout=5)
            assert result.ok, result
            assert "permission:approved" not in result.text, result.text
            assert conn.client.activity.outstanding_count() == 0
        finally:
            await conn.kill()
    finally:
        if old is None:
            os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
        else:
            os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old


async def case_permission_elicitation_unblocks() -> None:
    # The codex-acp wedge FIX path: an MCP tool that elicits (request_user_input)
    # surfaces as a session/request_permission once codex-acp runs with
    # features.tool_call_mcp_elicitation=true. The options carry multiple
    # allow_always entries + a reject (the real shape captured from
    # context-mode ctx_index). Auto-allow must pick allow_once and the
    # worker must unblock -- never 'cancel'.
    conn = await _connect("permission_elicitation")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert "permission:approved" in result.text, result.text
        assert "permission:cancel" not in result.text, result.text
        assert conn.client.activity.outstanding_count() == 0
    finally:
        await conn.kill()

async def case_permission_escalate_out_of_worktree() -> None:
    # A worker permission request targeting a path OUTSIDE its cwd is ESCALATED:
    # the ACP request is answered with a cancel (worker never held open), the turn
    # is cancelled with USER-CONFIRM, and the escalation is surfaced for the
    # orchestrator to relay to the user + re-dispatch. The 'previously it hung' case,
    # now liveness-safe.
    conn = await _connect("permission_escalate")  # cwd == ROOT
    try:
        result = await run_prompt(conn, "go", idle_timeout=10)
        assert result.cancelled_for_marker, result
        assert result.early_marker == "USER-CONFIRM", result.early_marker
        assert result.permission_escalations, result
        esc = result.permission_escalations[0]
        assert esc.get("title") == "Edit /etc/hosts", esc
        assert "/etc/hosts" in (esc.get("targets_outside_cwd") or []), esc
        assert conn.client.activity.outstanding_count() == 0
    finally:
        await conn.kill()


async def case_permission_fetch_escalates() -> None:
    # A network/fetch tool (ToolKind 'fetch') is escalated even with no file
    # locations -- internet access is a boundary the orchestrator routes to the user.
    conn = await _connect("permission_fetch")
    try:
        result = await run_prompt(conn, "go", idle_timeout=10)
        assert result.cancelled_for_marker, result
        assert result.permission_escalations, result
        assert result.permission_escalations[0].get("kind") == "fetch", result.permission_escalations
    finally:
        await conn.kill()


async def case_permission_inline_allow_authorizes_in_place() -> None:
    # permission_mode="inline": a boundary-crossing request is HELD open while the
    # orchestrator authorizes it in place (file IPC), then the worker proceeds with
    # the real allow option -- NO re-dispatch. Also proves run_prompt tolerates the
    # hold: idle_timeout (1s) is shorter than the orchestrator's hold (2s), so the
    # worker would be cancelled if the inline hold weren't treated as healthy.
    with tempfile.TemporaryDirectory() as d:
        conn = await _connect_inline("permission_inline", permission_dir=d, inline_timeout=30.0)
        try:
            run_task = asyncio.create_task(run_prompt(conn, "go", idle_timeout=1))
            relayed = await _relay_decision(d, "allow", option_id="approved", hold_extra=2.0)
            assert relayed is not None, "controller never saw the inline request"
            assert relayed.get("title") == "Edit /etc/hosts", relayed
            assert "/etc/hosts" in (relayed.get("targets_outside_cwd") or []), relayed
            result = await run_task
            assert result.ok, result
            assert "permission:approved" in result.text, result.text
            assert not result.permission_escalations, result.permission_escalations
            assert conn.client.activity.outstanding_count() == 0
            assert permits.list_requests(d) == [], "worker did not clean up after resolving"
        finally:
            await conn.kill()


async def case_permission_inline_deny_cancels_in_place() -> None:
    # An inline DENY is a definitive in-place answer: the worker's gate is
    # cancelled and it proceeds (no re-dispatch, no escalation surfaced).
    with tempfile.TemporaryDirectory() as d:
        conn = await _connect_inline("permission_inline", permission_dir=d, inline_timeout=30.0)
        try:
            run_task = asyncio.create_task(run_prompt(conn, "go", idle_timeout=10))
            relayed = await _relay_decision(d, "deny")
            assert relayed is not None, "controller never saw the inline request"
            result = await run_task
            assert result.ok, result
            assert "permission:cancelled" in result.text, result.text
            assert not result.permission_escalations, result.permission_escalations
            assert conn.client.activity.outstanding_count() == 0
        finally:
            await conn.kill()


async def case_permission_inline_timeout_auto_declines() -> None:
    # No orchestrator answers within the (awake-time) inline timeout: the worker is
    # given a definitive DENY and CONTINUES its turn (NO re-dispatch). The
    # auto-decline is surfaced informationally.
    with tempfile.TemporaryDirectory() as d:
        conn = await _connect_inline("permission_inline", permission_dir=d, inline_timeout=0.5)
        try:
            result = await run_prompt(conn, "go", idle_timeout=10)
            assert result.ok, result
            assert "permission:cancelled" in result.text, result.text
            assert not result.permission_escalations, result
            assert result.permission_auto_declined, result
            assert result.permission_auto_declined[0].get("reason") == "controller_timeout"
            assert conn.client.activity.outstanding_count() == 0
            assert permits.list_requests(d) == []
        finally:
            await conn.kill()


async def case_permission_inline_ack_extends_to_user_window() -> None:
    with tempfile.TemporaryDirectory() as d:
        conn = await _connect_inline("permission_inline", permission_dir=d,
                                     inline_timeout=0.5, user_timeout=10.0)
        try:
            run_task = asyncio.create_task(run_prompt(conn, "go", idle_timeout=10))
            # ack immediately, decide only AFTER the 0.5s orchestrator window
            for _ in range(200):
                reqs = permits.list_requests(d)
                if reqs:
                    permits.write_ack(d, reqs[0]["key"])
                    await asyncio.sleep(1.0)            # past the orchestrator window
                    permits.write_decision(d, reqs[0]["key"], "allow", "approved")
                    break
                await asyncio.sleep(0.02)
            result = await run_task
            assert result.ok, result
            assert "permission:approved" in result.text, result.text
            assert not result.permission_auto_declined, result  # ack extended, not declined
        finally:
            await conn.kill()


async def case_permission_inline_ack_then_user_timeout() -> None:
    with tempfile.TemporaryDirectory() as d:
        conn = await _connect_inline("permission_inline", permission_dir=d,
                                     inline_timeout=0.5, user_timeout=0.6)
        try:
            run_task = asyncio.create_task(run_prompt(conn, "go", idle_timeout=10))
            for _ in range(200):
                reqs = permits.list_requests(d)
                if reqs:
                    permits.write_ack(d, reqs[0]["key"])   # ack, never decide
                    break
                await asyncio.sleep(0.02)
            result = await run_task
            assert result.ok, result
            assert result.permission_auto_declined, result
            assert result.permission_auto_declined[0]["reason"] == "user_timeout", result.permission_auto_declined
        finally:
            await conn.kill()


async def case_pool_inline_threading() -> None:
    # AcpProcessPool(permission_mode="inline", permission_dir=...) threads the
    # inline posture to the spawned connection's client and authorizes through the
    # pool path end-to-end.
    config = {"fake": {"command": sys.executable, "acp_args": [str(FAKE)], "working_dir": str(ROOT)}}
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "permission_inline"
    with tempfile.TemporaryDirectory() as d:
        async with managed_pool(
            config,
            install_signal_handlers=False,
            auto_allow_tools=True,
            permission_mode="inline",
            permission_dir=d,
            permission_inline_timeout_s=30.0,
        ) as pool:
            conn = await pool.get_or_create("fake", "inline", cwd=str(ROOT))
            assert conn.client.permission_mode == "inline"
            assert Path(conn.client.permission_dir) == Path(d), conn.client.permission_dir
            run_task = asyncio.create_task(run_prompt(conn, "go", idle_timeout=10))
            relayed = await _relay_decision(d, "allow", option_id="approved")
            assert relayed is not None, "controller never saw the inline request"
            result = await run_task
            assert result.ok, result
            assert "permission:approved" in result.text, result.text


async def case_tool_tracking_closes() -> None:
    conn = await _connect("tool_tracking")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert result.tool_calls
        assert conn.client.activity.outstanding_count() == 0
    finally:
        await conn.kill()


async def case_fine_chunks_vendor_no_dup_marker() -> None:
    conn = await _connect("fine_chunks_vendor")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert result.text == "COMPLETE: grok smoke\n", repr(result.text)
        markers = extract_markers(result.text)
        assert markers["COMPLETE"] == ["grok smoke"], markers
    finally:
        await conn.kill()


async def case_blocked_none_signoff_completes() -> None:
    conn = await _connect("blocked_none")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert not result.cancelled_for_marker, result
        assert result.early_marker is None, result
        markers = extract_markers(result.text)
        assert markers["BLOCKED"] == ["none"], markers
        assert markers["COMPLETE"] == ["goal done"], markers
        assert early_actionable_marker(markers) is None
    finally:
        await conn.kill()


async def case_user_need_none_signoff_completes() -> None:
    conn = await _connect("user_need_none")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert not result.cancelled_for_marker, result
        markers = extract_markers(result.text)
        assert markers["USER-NEED"] == ["none"], markers
        assert early_actionable_marker(markers) is None
    finally:
        await conn.kill()


async def case_realtime_blocked_cancels_before_prompt_resolves() -> None:
    conn = await _connect("blocked")
    start = time.monotonic()
    try:
        result = await run_prompt(conn, "go", idle_timeout=30)
        elapsed = time.monotonic() - start
        assert result.cancelled_for_marker, result
        assert result.early_marker == "BLOCKED"
        assert elapsed < 5.0, elapsed
        assert extract_markers(result.text)["BLOCKED"] == ["need maintainer"]
    finally:
        await conn.kill()


async def case_realtime_blocked_cancel_rebuilds_pool_connection() -> None:
    config = {
        "fake": {
            "command": sys.executable,
            "acp_args": [str(FAKE)],
            "working_dir": str(ROOT),
        }
    }
    async with managed_pool(config, install_signal_handlers=False, auto_allow_tools=True) as pool:
        old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
        try:
            os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "blocked"
            conn1 = await pool.get_or_create("fake", "reuse", cwd=str(ROOT))
            pid1 = conn1.proc.pid
            result1 = await run_prompt(conn1, "go", idle_timeout=30)
            assert result1.cancelled_for_marker, result1
            assert not conn1.reusable

            os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "echo"
            conn2 = await pool.get_or_create("fake", "reuse", cwd=str(ROOT))
            assert conn2 is not conn1
            assert conn2.proc.pid != pid1
            result2 = await run_prompt(conn2, "again", idle_timeout=5)
            assert result2.ok, result2
            assert result2.text == "echo"
        finally:
            if old_scenario is None:
                os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
            else:
                os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old_scenario


async def case_run_prompt_cancel_marks_unreusable() -> None:
    conn = await _connect("blocked")
    try:
        task = asyncio.create_task(run_prompt(conn, "go", idle_timeout=30))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not getattr(conn.client, "_prompt_in_use", False):
            await asyncio.sleep(0)
        assert conn.client._prompt_in_use is True
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError), results
        assert conn.reusable is False
        assert conn.client._prompt_in_use is False
    finally:
        await conn.kill()


async def case_goal_marker_extraction() -> None:
    conn = await _connect("goal")
    try:
        result = await run_prompt(conn, "/goal", idle_timeout=5)
        assert result.ok, result
        markers = extract_markers(result.text)
        assert markers["STATUS"] == ["working"]
        assert markers["COMPLETE"] == ["goal done"]
    finally:
        await conn.kill()


async def case_managed_pool_sdk_connection() -> None:
    config = {
        "fake": {
            "command": sys.executable,
            "acp_args": [str(FAKE)],
            "working_dir": str(ROOT),
        }
    }
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "echo"
    async with managed_pool(config, install_signal_handlers=False, auto_allow_tools=True) as pool:
        conn = await pool.get_or_create("fake", "pool", cwd=str(ROOT))
        result = await run_prompt(conn, "pool", idle_timeout=5)
        assert result.ok, result
        assert pool.stats["total"] == 1


async def case_pool_context_mode_rebuild() -> None:
    # P2d: a pooled connection carries the context_mode it was launched with.
    # Reusing the same session with the SAME mode returns the same worker;
    # requesting a DIFFERENT mode rebuilds (never serve a wrong-posture worker).
    config = {"fake": {"command": sys.executable, "acp_args": [str(FAKE)], "working_dir": str(ROOT)}}
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "echo"
    async with managed_pool(config, install_signal_handlers=False, auto_allow_tools=True, context_mode=True) as pool:
        c1 = await pool.get_or_create("fake", "s", cwd=str(ROOT))
        assert c1.context_mode is True
        pid1 = c1.proc.pid
        # same session + same (default) mode -> reuse
        assert (await pool.get_or_create("fake", "s", cwd=str(ROOT))) is c1
        # same session + DIFFERENT mode -> rebuild
        c2 = await pool.get_or_create("fake", "s", cwd=str(ROOT), context_mode=False)
        assert c2 is not c1
        assert c2.context_mode is False
        assert c2.proc.pid != pid1


async def case_pool_cwd_rebuild() -> None:
    config = {"fake": {"command": sys.executable, "acp_args": [str(FAKE)], "working_dir": str(ROOT)}}
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "echo"
    async with managed_pool(config, install_signal_handlers=False, auto_allow_tools=True) as pool:
        c1 = await pool.get_or_create("fake", "s", cwd=str(ROOT))
        pid1 = c1.proc.pid
        with tempfile.TemporaryDirectory() as tmpdir:
            c2 = await pool.get_or_create("fake", "s", cwd=tmpdir)
            assert c2 is not c1
            assert c2.proc.pid != pid1
            assert Path(c2.cwd).resolve() == Path(tmpdir).resolve()


def case_codex_acp_args_injection_unit() -> None:
    # Single-boundary codex-acp arg injection + the context-mode toggle
    # (ensure_codex_acp_args, called by spawn_acp_connection), covering the
    # runner, AcpProcessPool config, and any custom launcher.
    from goalflight_acp_client import (
        CODEX_ACP_ELICITATION_ARGS,
        ensure_codex_acp_args as ensure,
        ensure_codex_acp_elicitation,
    )
    ELICIT = "features.tool_call_mcp_elicitation=true"
    DISABLE = "mcp_servers.context-mode.enabled=false"
    # context_mode=True (default): elicitation routed through the permission channel
    assert ensure("codex-acp", []) == ["-c", ELICIT]
    assert ensure("/opt/homebrew/bin/codex-acp", []) == ["-c", ELICIT]  # abs path basename
    # appended LAST, caller args preserved
    assert ensure("codex-acp", ["-c", 'model="x"']) == ["-c", 'model="x"', "-c", ELICIT]
    # idempotent, and a conflicting prior value (true OR false) is stripped -> ours wins, once
    assert ensure("codex-acp", ["-c", ELICIT]) == ["-c", ELICIT]
    assert ensure("codex-acp", ["-c", "features.tool_call_mcp_elicitation=false"]) == ["-c", ELICIT]
    # cross-key: enabled mode strips a caller's OPPOSITE context-mode-disable too
    assert ensure("codex-acp", ["-c", "mcp_servers.context-mode.enabled=false"]) == ["-c", ELICIT]
    # context_mode=False: disable context-mode for this worker (no MCP elicitation surface)
    assert ensure("codex-acp", [], context_mode=False) == ["-c", DISABLE]
    assert ensure("codex-acp", ["-c", "mcp_servers.context-mode.enabled=true"], context_mode=False) == ["-c", DISABLE]
    # cross-key: disabled mode strips a caller's OPPOSITE elicitation flag too
    assert ensure("codex-acp", ["-c", "features.tool_call_mcp_elicitation=true"], context_mode=False) == ["-c", DISABLE]
    # back-compat alias preserves the original elicitation-only API.
    assert ensure_codex_acp_elicitation("codex-acp", []) == list(CODEX_ACP_ELICITATION_ARGS)
    # no-op for every other adapter / command
    for other in ("grok", "cursor", "claude-code-cli-acp", "/usr/bin/python3"):
        assert ensure(other, []) == [], other
        assert ELICIT not in ensure(other, ["-x"]) and DISABLE not in ensure(other, ["-x"]), other


def case_permission_handler_selection_unit() -> None:
    # Unit-test GoalflightClient.request_permission selection + deny semantics
    # directly (no subprocess), against codex-acp's REAL option shape.
    import types
    from goalflight_acp_client import GoalflightClient

    def opt(kind: str, oid: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(kind=kind, option_id=oid, name=kind)

    tc = types.SimpleNamespace(tool_call_id="t1", id="t1", title="Edit foo")
    codex = [opt("allow_once", "approved"), opt("reject_once", "abort")]
    assert (
        GoalflightClient._select_allow_option(
            [{"kind": "allow_once", "optionId": "approved"}, {"kind": "reject_once", "optionId": "abort"}]
        )
        == "approved"
    )

    async def _run() -> None:
        client = GoalflightClient(auto_allow_tools=True)
        # codex shape -> picks the allow_once, returns an AllowedOutcome
        r = await client.request_permission(codex, "s", tc)
        assert r.outcome.outcome == "selected", r.outcome
        assert getattr(r.outcome, "option_id", None) == "approved", r.outcome
        # reject offered FIRST -> still the allow, never 'abort'
        r2 = await client.request_permission(list(reversed(codex)), "s", tc)
        assert getattr(r2.outcome, "option_id", None) == "approved", r2.outcome
        # allow_once is least-privilege and beats allow_always
        three = [opt("reject_once", "no"), opt("allow_once", "once"), opt("allow_always", "always")]
        r3 = await client.request_permission(three, "s", tc)
        assert getattr(r3.outcome, "option_id", None) == "once", r3.outcome
        # only reject options -> cancel, never wrap a reject id in AllowedOutcome
        r4 = await client.request_permission([opt("reject_once", "abort")], "s", tc)
        assert r4.outcome.outcome == "cancelled", r4.outcome
        # malformed "allow"-prefix kinds (allowed/allowance/allowfoo) are NOT
        # allow_* options -> must fail closed (cancel), never auto-grant.
        r4b = await client.request_permission([opt("allowed", "sneaky"), opt("allowance", "x"), opt("allowfoo", "y")], "s", tc)
        assert r4b.outcome.outcome == "cancelled", r4b.outcome
        # auto_allow_tools=False -> deny cleanly; must NOT raise method_not_found
        # (the 0.3.0 "every worker hangs on first tool call" regression).
        denier = GoalflightClient(auto_allow_tools=False)
        r5 = await denier.request_permission(codex, "s", tc)
        assert r5.outcome.outcome == "cancelled", r5.outcome

    asyncio.run(_run())

def case_permission_inline_rejects_nonallow_option_unit() -> None:
    import types
    from goalflight_acp_client import GoalflightClient

    def opt(kind: str, oid: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(kind=kind, option_id=oid, name=kind)

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as d:
            client = GoalflightClient(permission_mode="inline", permission_dir=d)
            options = [opt("allow_once", "approved"), opt("reject_once", "abort")]
            forged = client._outcome_from_decision(
                {"decision": "allow", "option_id": "abort"}, options
            )
            assert forged.outcome.outcome == "cancelled", forged.outcome
            allowed = client._outcome_from_decision(
                {"decision": "allow", "option_id": "approved"}, options
            )
            assert allowed.outcome.outcome == "selected", allowed.outcome
            assert getattr(allowed.outcome, "option_id", None) == "approved", allowed.outcome
            defaulted = client._outcome_from_decision({"decision": "allow"}, options)
            assert defaulted.outcome.outcome == "selected", defaulted.outcome
            assert getattr(defaulted.outcome, "option_id", None) == "approved", defaulted.outcome

    asyncio.run(_run())


def case_permission_select_prefers_allow_once_unit() -> None:
    import types
    from goalflight_acp_client import GoalflightClient

    def opt(kind: str, oid: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(kind=kind, option_id=oid, name=kind)

    assert (
        GoalflightClient._select_allow_option(
            [opt("allow_once", "once"), opt("allow_always", "always")]
        )
        == "once"
    )


def case_permission_policy_unit() -> None:
    # Controller-as-auto-mode router default policy: auto-allow in-worktree,
    # escalate boundary crossings (out-of-worktree targets, network/fetch).
    import types
    from goalflight_acp_client import (
        default_permission_policy as policy,
        PERMISSION_ALLOW,
        PERMISSION_ESCALATE,
    )
    cwd = str(ROOT)

    def tc(**kw) -> types.SimpleNamespace:
        return types.SimpleNamespace(**kw)

    # in-worktree edit -> allow
    assert policy(tc(locations=[{"path": str(ROOT / "scripts/x.py")}], kind="edit"), [], cwd) == PERMISSION_ALLOW
    # out-of-worktree edit -> escalate
    assert policy(tc(locations=[{"path": "/etc/hosts"}], kind="edit"), [], cwd) == PERMISSION_ESCALATE
    # relative path resolving outside cwd -> escalate
    assert policy(tc(locations=[{"path": "../../../etc/hosts"}], kind="edit"), [], cwd) == PERMISSION_ESCALATE
    # network/fetch -> escalate even with no file locations
    assert policy(tc(locations=[], kind="fetch"), [], cwd) == PERMISSION_ESCALATE
    # shell/unknown/future side effects -> escalate
    assert policy(tc(locations=[], kind="execute"), [], cwd) == PERMISSION_ESCALATE
    assert policy(tc(locations=[], kind="other"), [], cwd) == PERMISSION_ESCALATE
    assert policy(tc(locations=[], kind="switch_mode"), [], cwd) == PERMISSION_ESCALATE
    # benign in-workspace MCP/elicitation (no kind, no locations) -> allow
    assert policy(tc(title="Approve Index Content"), [], cwd) == PERMISSION_ALLOW
    # write-like (edit/delete/move) with NO verifiable locations -> escalate (fail closed)
    assert policy(tc(kind="edit"), [], cwd) == PERMISSION_ESCALATE
    assert policy(tc(kind="delete"), [], cwd) == PERMISSION_ESCALATE
    assert policy(tc(kind="move"), [], cwd) == PERMISSION_ESCALATE
    # read-like with no locations -> allow (no state change to scope-check)
    assert policy(tc(kind="read"), [], cwd) == PERMISSION_ALLOW
    assert policy(tc(kind="search"), [], cwd) == PERMISSION_ALLOW
    assert policy(tc(kind="think"), [], cwd) == PERMISSION_ALLOW
    # unknown cwd + a located target -> cannot prove in-scope -> escalate (fail closed)
    assert policy(tc(locations=[{"path": "/etc/hosts"}], kind="edit"), [], None) == PERMISSION_ESCALATE
    # unknown cwd + NO locations (benign MCP elicitation) -> allow (nothing to prove)
    assert policy(tc(title="Approve Index Content"), [], None) == PERMISSION_ALLOW
    # dict-shaped tool_call routes identically to an SDK object (no getattr-only bypass)
    assert policy({"locations": [{"path": "/etc/hosts"}], "kind": "edit"}, [], cwd) == PERMISSION_ESCALATE
    assert policy({"kind": "fetch"}, [], cwd) == PERMISSION_ESCALATE
    assert policy({"locations": [{"path": str(ROOT / "scripts/x.py")}], "kind": "edit"}, [], cwd) == PERMISSION_ALLOW
    assert policy({"title": "Approve Index Content"}, [], cwd) == PERMISSION_ALLOW


def case_permission_policy_os_sandbox_unit() -> None:
    import types
    from goalflight_acp_client import (
        default_permission_policy as base_policy,
        permission_policy_for_dispatch,
        PERMISSION_ALLOW,
        PERMISSION_ESCALATE,
    )
    from goalflight_os_sandbox import OS_SANDBOX_OFF, OS_SANDBOX_READ_ONLY
    cwd = str(ROOT)

    def tc(**kw) -> types.SimpleNamespace:
        return types.SimpleNamespace(**kw)

    off = permission_policy_for_dispatch(OS_SANDBOX_OFF)
    ro = permission_policy_for_dispatch(OS_SANDBOX_READ_ONLY)

    assert off(tc(locations=[], kind="execute"), [], cwd) == PERMISSION_ESCALATE
    assert ro(tc(locations=[], kind="execute"), [], cwd) == PERMISSION_ALLOW
    assert ro(tc(locations=[], kind="fetch"), [], cwd) == PERMISSION_ALLOW
    assert ro(
        tc(locations=[{"path": "/etc/hosts"}], kind="execute"), [], cwd
    ) == PERMISSION_ESCALATE
    # custom base policy still applies for non-side-effect kinds
    assert ro(tc(locations=[{"path": str(ROOT / "scripts/x.py")}], kind="edit"), [], cwd) == PERMISSION_ALLOW
    wrapped = permission_policy_for_dispatch(OS_SANDBOX_READ_ONLY, base=base_policy)
    assert wrapped(tc(locations=[], kind="execute"), [], cwd) == PERMISSION_ALLOW


def case_permits_ipc_roundtrip_unit() -> None:
    # The inline file-IPC contract: write a request, the orchestrator lists it and
    # writes a decision, the worker reads it, then clears. Here we assert the
    # round-trip + that an answered request drops out of list_requests and that a
    # bad decision value is rejected.
    with tempfile.TemporaryDirectory() as d:
        assert permits.list_requests(d) == []
        key = permits.make_key("sess/with:unsafe", "tool 1")
        assert "/" not in key and ":" not in key and " " not in key, key
        permits.write_request(d, {"key": key, "title": "Edit /etc/hosts", "kind": "edit"})
        reqs = permits.list_requests(d)
        assert len(reqs) == 1 and reqs[0]["key"] == key, reqs
        assert reqs[0]["schema"] == permits.REQUEST_SCHEMA and reqs[0].get("created_at"), reqs[0]
        # not yet decided
        assert permits.read_decision(d, key) is None
        permits.write_decision(d, key, "allow", option_id="approved")
        got = permits.read_decision(d, key)
        assert got and got["decision"] == "allow" and got["option_id"] == "approved", got
        # an answered request is hidden from the orchestrator's pending list
        assert permits.list_requests(d) == [], "answered request still listed as pending"
        permits.clear(d, key)
        assert not permits.request_path(d, key).exists()
        assert not permits.decision_path(d, key).exists()
        # sweep reaps aged cruft but never a fresh file
        permits.write_request(d, {"key": "fresh.tc.aaaaaaaa", "title": "t"})
        old = permits.request_path(d, "stale.tc.bbbbbbbb")
        old.write_text("{}")
        os.utime(old, (time.time() - 7200, time.time() - 7200))  # 2h old
        assert permits.sweep(d, max_age_s=3600) == 1, "sweep should drop exactly the aged file"
        assert not old.exists() and permits.request_path(d, "fresh.tc.aaaaaaaa").exists()
        # missing record key and bad decision value are rejected
        try:
            permits.write_request(d, {"title": "no key"})
            raise AssertionError("write_request must require a key")
        except ValueError:
            pass
        try:
            permits.write_decision(d, key, "maybe")
            raise AssertionError("write_decision must reject non-allow/deny")
        except ValueError:
            pass
    # PID-scoped default dir (no explicit / no env) isolates concurrent orchestrators
    old_env = os.environ.pop(permits.ENV_PERMISSION_DIR, None)
    try:
        assert str(os.getpid()) in str(permits.permission_dir())
        os.environ[permits.ENV_PERMISSION_DIR] = "/tmp/explicit-perms"
        assert permits.permission_dir() == Path("/tmp/explicit-perms")
        assert permits.permission_dir("/override") == Path("/override")  # explicit wins
    finally:
        if old_env is None:
            os.environ.pop(permits.ENV_PERMISSION_DIR, None)
        else:
            os.environ[permits.ENV_PERMISSION_DIR] = old_env


def case_permits_first_writer_wins_unit() -> None:
    with tempfile.TemporaryDirectory() as d:
        key = "first.tc.aaaaaaaa"
        permits.write_decision(d, key, "deny")
        permits.write_decision(d, key, "allow", "approved")
        got = permits.read_decision(d, key)
        assert got and got["decision"] == "deny", got


def case_permits_read_decision_validates_unit() -> None:
    with tempfile.TemporaryDirectory() as d:
        key = "validate.tc.aaaaaaaa"
        p = permits.decision_path(d, key)
        p.write_text(json.dumps({"decision": "allow"}), encoding="utf-8")
        assert permits.read_decision(d, key) is None
        permits.write_decision(d, key, "allow", "approved")
        got = permits.read_decision(d, key)
        assert got and got["decision"] == "allow", got
        p.write_text(
            json.dumps(
                {
                    "schema": permits.DECISION_SCHEMA,
                    "key": "different.tc.aaaaaaaa",
                    "decision": "allow",
                }
            ),
            encoding="utf-8",
        )
        assert permits.read_decision(d, key) is None


def case_permits_write_decision_replaces_malformed_unit() -> None:
    with tempfile.TemporaryDirectory() as d:
        key = "malformed.tc.aaaaaaaa"
        permits.decision_path(d, key).write_text("{bad json", encoding="utf-8")
        permits.write_decision(d, key, "deny")
        got = permits.read_decision(d, key)
        assert got and got["decision"] == "deny", got


def case_permits_ack_roundtrip_unit() -> None:
    with tempfile.TemporaryDirectory() as d:
        key = "ack.tc.aaaaaaaa"
        permits.write_request(d, {"key": key, "title": "Edit /etc/hosts", "kind": "edit"})
        assert not permits.read_ack(d, key)
        permits.write_ack(d, key)
        assert permits.read_ack(d, key)
        reqs = permits.list_requests(d)
        assert len(reqs) == 1 and reqs[0]["key"] == key and reqs[0]["acked"] is True, reqs
        permits.clear(d, key)
        assert not permits.ack_path(d, key).exists()
        assert not permits.read_ack(d, key)


def case_permits_fifo_decision_does_not_hide_unit() -> None:
    if not hasattr(os, "mkfifo"):
        return
    with tempfile.TemporaryDirectory() as d:
        key = "fifo-visible.tc.aaaaaaaa"
        permits.write_request(d, {"key": key, "title": "Edit /etc/hosts", "kind": "edit"})
        os.mkfifo(permits.decision_path(d, key))
        reqs = permits.list_requests(d)
        assert len(reqs) == 1 and reqs[0]["key"] == key, reqs
        permits.clear(d, key)


def case_permits_read_ack_key_match_unit() -> None:
    with tempfile.TemporaryDirectory() as d:
        permits.write_ack(d, "k1")
        assert permits.read_ack(d, "k1") is True
        assert permits.read_ack(d, "k2") is False
        permits.ack_path(d, "k2").write_text(json.dumps({"schema": permits.ACK_SCHEMA, "key": "k1"}))
        assert permits.read_ack(d, "k2") is False
        permits.write_request(d, {"key": "k1", "title": "Edit /etc/hosts", "kind": "edit"})
        reqs = permits.list_requests(d)
        assert len(reqs) == 1 and reqs[0]["key"] == "k1" and reqs[0]["acked"] is True, reqs


def case_permits_write_decision_replaces_fifo_unit() -> None:
    if not hasattr(os, "mkfifo"):
        return
    with tempfile.TemporaryDirectory() as d:
        key = "fifo-replace.tc.aaaaaaaa"
        p = permits.decision_path(d, key)
        os.mkfifo(p)
        permits.write_decision(d, key, "allow", "approved")
        got = permits.read_decision(d, key)
        assert got and got["decision"] == "allow", got
        assert stat.S_ISREG(os.lstat(p).st_mode)


def case_permits_rejects_fifo_unit() -> None:
    if not hasattr(os, "mkfifo"):
        return
    with tempfile.TemporaryDirectory() as d:
        key = "fifo.tc.aaaaaaaa"
        os.mkfifo(permits.decision_path(d, key))
        assert permits.read_decision(d, key) is None


def case_permission_inline_cancel_answers_unit() -> None:
    # P0 (grok review): if the inline hold is cancelled mid-poll (event-loop /
    # connection teardown), the handler must STILL answer the worker's synchronous
    # gate (definitive deny) instead of propagating CancelledError with no reply --
    # else a still-alive worker wedges. Also: hold released + IPC files cleaned up.
    import types
    from goalflight_acp_client import GoalflightClient, PERMISSION_ESCALATE

    def opt(kind: str, oid: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(kind=kind, option_id=oid, name=kind)

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as d:
            client = GoalflightClient(
                auto_allow_tools=True,
                permission_mode="inline",
                permission_dir=d,
                permission_inline_timeout_s=30.0,
                permission_policy=lambda tc, o, cwd: PERMISSION_ESCALATE,
            )
            tc = types.SimpleNamespace(tool_call_id="t-cancel", id="t-cancel", title="Edit /etc/hosts")
            opts = [opt("allow_once", "approved"), opt("reject_once", "abort")]
            task = asyncio.create_task(client.request_permission(opts, "s", tc))
            for _ in range(100):  # let it enter the hold (write request + poll)
                await asyncio.sleep(0.02)
                if client.activity.has_inline_holds():
                    break
            assert client.activity.has_inline_holds(), "handler never entered the inline hold"
            task.cancel()
            try:
                resp = await task
            except asyncio.CancelledError:
                raise AssertionError("handler propagated CancelledError without answering the worker")
            assert resp.outcome.outcome == "cancelled", resp.outcome
            assert not client.activity.has_inline_holds(), "hold not released on cancel"
            assert permits.list_requests(d) == [], "IPC files not cleaned up on cancel"

    asyncio.run(_run())


def case_activity_inline_hold_unit() -> None:
    # AcpLivenessActivity inline-hold accounting: a held permission counts toward
    # outstanding_count (so the heartbeat grants silence grace) but is EXEMPT from
    # the short permission_timeout_s expiry and unrelated max_tool_s; its own
    # deadline + grace reaps it. Releasing clears it.
    from goalflight_acp_client import AcpLivenessActivity, INLINE_HOLD_GRACE_S

    act = AcpLivenessActivity(permission_timeout_s=30.0)
    assert not act.has_inline_holds()
    assert act.outstanding_count() == 0
    deadline = 100.0
    act.hold_inline_permission("perm-1", deadline=deadline)
    assert act.has_inline_holds()
    assert act.outstanding_count() == 1
    assert act.snapshot(now=0.0)["inline_held"] == 1
    # At its own deadline, the inline hold must NOT be reaped by permission
    # timeout or an unrelated max_tool_s -- it is still healthy.
    assert act.timed_out(now=deadline, max_tool_s=1.0) is None
    assert act.has_inline_holds(), "inline hold wrongly expired by permission_timeout_s"
    # ...but the hold's own deadline + grace IS a backstop for a stuck hold.
    reaped = act.timed_out(now=deadline + INLINE_HOLD_GRACE_S, max_tool_s=1800.0)
    assert reaped is not None and reaped[0] == "perm-1", reaped
    assert reaped[1] == INLINE_HOLD_GRACE_S, reaped
    assert not act.has_inline_holds(), "deadline grace backstop should drop the hold"
    # release is idempotent + clears
    act.hold_inline_permission("perm-2", deadline=deadline)
    act.release_inline_permission("perm-2")
    assert not act.has_inline_holds()
    act.release_inline_permission("missing")  # no raise

def case_marker_parser() -> None:
    sample = "**STATUS:** working\nUSER-CONFIRM: approve\nCOMPLETE: done\n"
    markers = extract_markers(sample)
    assert markers["STATUS"] == ["working"]
    assert markers["USER-CONFIRM"] == ["approve"]
    assert markers["COMPLETE"] == ["done"]


def case_ready_marker_parser() -> None:
    """READY: is in the marker vocabulary (Investigator file-backed findings)."""
    sample = (
        "TL;DR: audit done\n"
        "READY: docs-private/research/2026-06-03-audit/findings.md\n"
    )
    markers = extract_markers(sample)
    assert markers["READY"] == ["docs-private/research/2026-06-03-audit/findings.md"]
    assert "READY" in TERMINAL_MARKERS


def case_sentinel_marker_payloads_unit() -> None:
    for payload in ("none", "NONE", "  none  ", "N/A", "(none)", "-", "", "   "):
        assert is_sentinel_marker_payload(payload), payload
    assert not is_sentinel_marker_payload("missing API key, cannot proceed")
    blocked_none = extract_markers(
        "RESULT:\n- work done\n\nBLOCKED: none\nCOMPLETE: goal done\n"
    )
    assert blocked_none["BLOCKED"] == ["none"]
    assert not has_actionable_marker_values(blocked_none, "BLOCKED")
    assert early_actionable_marker(blocked_none) is None
    substantive = extract_markers("BLOCKED: missing API key, cannot proceed\n")
    assert has_actionable_marker_values(substantive, "BLOCKED")
    assert early_actionable_marker(substantive) == "BLOCKED"
    mixed = extract_markers("BLOCKED: none\nBLOCKED: missing API key\n")
    assert has_actionable_marker_values(mixed, "BLOCKED")
    assert early_actionable_marker(mixed) == "BLOCKED"
    user_need_none = extract_markers("USER-NEED: none\n")
    assert user_need_none["USER-NEED"] == ["none"]
    assert not has_actionable_marker_values(user_need_none, "USER-NEED")
    user_need_real = extract_markers("USER-NEED: pick deployment target\n")
    assert has_actionable_marker_values(user_need_real, "USER-NEED")
    assert early_actionable_marker(user_need_real) == "USER-NEED"


def case_rate_pressure_terminal_state_unit() -> None:
    base = {
        "result_ok": True,
        "result_error": None,
        "heartbeat_outcome": None,
        "killed_by_heartbeat": False,
        "cancelled_for_marker": False,
        "early_marker": None,
        "heartbeat_error": None,
        "stop_reason": "end_turn",
    }
    plan_block = "You've hit the plan limit. Check your settings to continue."
    state, error = goalflight_acp_run.decide_terminal_state(
        **base,
        result_text=plan_block,
        successful_terminal_marker=False,
    )
    assert state == "blocked", (state, error)
    assert error and error["signature"] == "check your settings to continue", error
    assert "Check your settings to continue" in error["excerpt"], error

    marker_text = f"Work output: {plan_block}\nCOMPLETE: done\n"
    markers = extract_markers(marker_text)
    state, error = goalflight_acp_run.decide_terminal_state(
        **base,
        result_text=marker_text,
        successful_terminal_marker=goalflight_acp_run._successful_terminal_marker(markers),
    )
    assert state == "complete", (state, error)

    blocked_marker_text = "BLOCKED: real reason\n"
    blocked_marker_markers = extract_markers(blocked_marker_text)
    state, error = goalflight_acp_run.decide_terminal_state(
        **base,
        result_text=blocked_marker_text,
        successful_terminal_marker=False,
    )
    assert state == "complete", (state, error)
    assert error is None, error
    state = goalflight_acp_run._state_after_actionable_terminal_markers(state, blocked_marker_markers)
    assert state == "blocked", (state, error)
    assert error is None, error

    blocked_signature_text = "BLOCKED: provider said rate limit exceeded\n"
    blocked_signature_markers = extract_markers(blocked_signature_text)
    state, error = goalflight_acp_run.decide_terminal_state(
        **base,
        result_text=blocked_signature_text,
        successful_terminal_marker=False,
    )
    assert state == "blocked", (state, error)
    assert error and error["message"] == "provider_limit_signature_without_terminal_marker", error
    assert error["signature"] == "rate limit", error
    state = goalflight_acp_run._state_after_actionable_terminal_markers(
        state,
        blocked_signature_markers,
    )
    assert state == "blocked", (state, error)

    turn_one = goalflight_acp_run.PromptResult(
        text='Docs example quotes "Check your settings to continue" as UI copy.\n',
        stop_reason="end_turn",
    )
    turn_two = goalflight_acp_run.PromptResult(
        text="Final innocent output with no terminal marker.\n",
        stop_reason="end_turn",
    )
    merged = goalflight_acp_run._merge_prompt_results([turn_one, turn_two])
    final_turn = goalflight_acp_run._last_prompt_result([turn_one, turn_two], merged)
    assert final_turn.text == turn_two.text
    state, error = goalflight_acp_run.decide_terminal_state(
        **base,
        result_text=final_turn.text,
        successful_terminal_marker=False,
    )
    assert state == "complete", (state, error)
    assert error is None, error
    legacy_state, legacy_error = goalflight_acp_run.decide_terminal_state(
        **base,
        result_text=merged.text,
        successful_terminal_marker=False,
    )
    assert legacy_state == "blocked", (legacy_state, legacy_error)
    assert legacy_error and legacy_error["signature"] == "check your settings to continue", legacy_error

    state, error = goalflight_acp_run.decide_terminal_state(
        **base,
        result_text="Completed ordinary analysis with no provider block.",
        successful_terminal_marker=False,
    )
    assert state == "complete", (state, error)


def case_pool_ceiling_fallback(tmp: Path | None = None) -> None:
    missing = ROOT / "tests/python/.missing-env-caveats.md"
    assert compute_pool_ceiling(missing) >= 1


async def amain() -> None:
    await case_echo_roundtrip()
    await case_overlimit_frame_drops_and_continues()
    await case_overlimit_request_gets_safe_error_reply()
    await case_overlimit_request_late_id_gets_safe_error_reply()
    await case_overlimit_no_newline_hits_drain_cap()
    await case_stderr_burst_without_newline_drains()
    await case_overlimit_response_fails_cleanly()
    await case_runner_overlimit_response_status_counts_drop()
    await case_runner_blocks_probe_required_adapter()
    await case_runner_blocks_invalid_adapter_manifest()
    await case_runner_blocks_missing_adapter_manifest()
    await case_permission_auto_allow()
    await case_permission_codex_shape_unblocks()
    await case_permission_reject_first_picks_allow()
    await case_permission_reject_only_cancels_and_unblocks()
    await case_permission_auto_allow_false_denies_and_unblocks()
    await case_permission_elicitation_unblocks()
    await case_permission_escalate_out_of_worktree()
    await case_permission_fetch_escalates()
    await case_permission_inline_allow_authorizes_in_place()
    await case_permission_inline_deny_cancels_in_place()
    await case_permission_inline_timeout_auto_declines()
    await case_permission_inline_ack_extends_to_user_window()
    await case_permission_inline_ack_then_user_timeout()
    await case_pool_inline_threading()
    await case_tool_tracking_closes()
    await case_fine_chunks_vendor_no_dup_marker()
    await case_blocked_none_signoff_completes()
    await case_user_need_none_signoff_completes()
    await case_realtime_blocked_cancels_before_prompt_resolves()
    await case_realtime_blocked_cancel_rebuilds_pool_connection()
    await case_run_prompt_cancel_marks_unreusable()
    await case_goal_marker_extraction()
    await case_managed_pool_sdk_connection()
    await case_pool_context_mode_rebuild()
    await case_pool_cwd_rebuild()


def main() -> None:
    case_marker_parser()
    case_ready_marker_parser()
    case_sentinel_marker_payloads_unit()
    case_rate_pressure_terminal_state_unit()
    case_codex_acp_args_injection_unit()
    case_permission_handler_selection_unit()
    case_permission_inline_rejects_nonallow_option_unit()
    case_permission_select_prefers_allow_once_unit()
    case_permission_policy_unit()
    case_permission_policy_os_sandbox_unit()
    case_permits_ipc_roundtrip_unit()
    case_permits_ack_roundtrip_unit()
    case_permits_fifo_decision_does_not_hide_unit()
    case_permits_read_ack_key_match_unit()
    case_permits_first_writer_wins_unit()
    case_permits_read_decision_validates_unit()
    case_permits_write_decision_replaces_malformed_unit()
    case_permits_write_decision_replaces_fifo_unit()
    case_permits_rejects_fifo_unit()
    case_permission_inline_cancel_answers_unit()
    case_activity_inline_hold_unit()
    case_pool_ceiling_fallback()
    asyncio.run(amain())
    print("OK: ACP SDK pipe tests pass")


if __name__ == "__main__":
    main()
