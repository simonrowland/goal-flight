#!/usr/bin/env python3
"""Hermetic SDK transport tests."""

from __future__ import annotations

import asyncio
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from acp_pool import compute_pool_ceiling, managed_pool  # noqa: E402
from acp_runner import extract_markers, run_prompt  # noqa: E402
import goalflight_acp_run  # noqa: E402
from goalflight_acp_client import spawn_acp_connection  # noqa: E402

FAKE = ROOT / "test/fixtures/acp_fake_agent.py"


async def _connect(scenario: str, *, limit: str | None = None):
    old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
    old_limit = os.environ.get("GOALFLIGHT_ACP_LIMIT")
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = scenario
    if limit is not None:
        os.environ["GOALFLIGHT_ACP_LIMIT"] = limit
    else:
        os.environ.pop("GOALFLIGHT_ACP_LIMIT", None)
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
        assert conn.client.activity.snapshot()["dropped_frames"] == 1
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
    old_agent_command = goalflight_acp_run.agent_command
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "overlimit_response"
    os.environ["GOALFLIGHT_ACP_LIMIT"] = "4k"
    goalflight_acp_run.agent_command = lambda agent: (sys.executable, [str(FAKE)])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
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


async def case_permission_auto_allow() -> None:
    conn = await _connect("permission")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert "permission:opt_always" in result.text
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
    # context-mode ctx_index). Auto-allow must pick an allow_always and the
    # worker must unblock -- never 'cancel'.
    conn = await _connect("permission_elicitation")
    try:
        result = await run_prompt(conn, "go", idle_timeout=5)
        assert result.ok, result
        assert "permission:approved-for-session" in result.text, result.text
        assert "permission:cancel" not in result.text, result.text
        assert conn.client.activity.outstanding_count() == 0
    finally:
        await conn.kill()


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


def case_codex_acp_elicitation_injection_unit() -> None:
    # The codex-acp wedge fix is injected at the SINGLE spawn boundary
    # (ensure_codex_acp_elicitation, called by spawn_acp_connection), so it
    # covers the runner, AcpProcessPool config, and any custom launcher. Without
    # the flag, an MCP tool that elicits (e.g. context-mode ctx_index) wedges
    # codex-acp at its first tool call (~0% CPU forever).
    from goalflight_acp_client import (
        ensure_codex_acp_elicitation as ensure,
        CODEX_ACP_ELICITATION_ARGS,
    )
    FLAG = "features.tool_call_mcp_elicitation=true"
    # bare codex-acp gets the exact flag (order matters: -c then value), prepended
    assert ensure("codex-acp", []) == ["-c", FLAG]
    assert ensure("codex-acp", []) == list(CODEX_ACP_ELICITATION_ARGS)
    # absolute-path basename still matches
    assert ensure("/opt/homebrew/bin/codex-acp", []) == ["-c", FLAG]
    # idempotent: never doubles the flag
    assert ensure("codex-acp", ["-c", FLAG]) == ["-c", FLAG]
    # preserves caller args (pool config), flag prepended once
    assert ensure("codex-acp", ["-c", 'model="x"']) == ["-c", FLAG, "-c", 'model="x"']
    # no-op for every other adapter / command
    for other in ("grok", "cursor", "claude-code-cli-acp", "/usr/bin/python3"):
        assert ensure(other, []) == [], other
        assert FLAG not in ensure(other, ["-x"]), other


def case_permission_handler_selection_unit() -> None:
    # Unit-test GoalflightClient.request_permission selection + deny semantics
    # directly (no subprocess), against codex-acp's REAL option shape.
    import types
    from goalflight_acp_client import GoalflightClient

    def opt(kind: str, oid: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(kind=kind, option_id=oid, name=kind)

    tc = types.SimpleNamespace(tool_call_id="t1", id="t1", title="Edit foo")
    codex = [opt("allow_once", "approved"), opt("reject_once", "abort")]

    async def _run() -> None:
        client = GoalflightClient(auto_allow_tools=True)
        # codex shape -> picks the allow_once, returns an AllowedOutcome
        r = await client.request_permission(codex, "s", tc)
        assert r.outcome.outcome == "selected", r.outcome
        assert getattr(r.outcome, "option_id", None) == "approved", r.outcome
        # reject offered FIRST -> still the allow, never 'abort'
        r2 = await client.request_permission(list(reversed(codex)), "s", tc)
        assert getattr(r2.outcome, "option_id", None) == "approved", r2.outcome
        # allow_always beats allow_once even when offered last
        three = [opt("reject_once", "no"), opt("allow_once", "once"), opt("allow_always", "always")]
        r3 = await client.request_permission(three, "s", tc)
        assert getattr(r3.outcome, "option_id", None) == "always", r3.outcome
        # only reject options -> cancel, never wrap a reject id in AllowedOutcome
        r4 = await client.request_permission([opt("reject_once", "abort")], "s", tc)
        assert r4.outcome.outcome == "cancelled", r4.outcome
        # malformed "allow"-prefix kinds (allowed/allowance/allowfoo) are NOT
        # allow_* options -> must fail closed (cancel), never auto-grant.
        r4b = await client.request_permission([opt("allowed", "sneaky"), opt("allowance", "x")], "s", tc)
        assert r4b.outcome.outcome == "cancelled", r4b.outcome
        # auto_allow_tools=False -> deny cleanly; must NOT raise method_not_found
        # (the 0.3.0 "every worker hangs on first tool call" regression).
        denier = GoalflightClient(auto_allow_tools=False)
        r5 = await denier.request_permission(codex, "s", tc)
        assert r5.outcome.outcome == "cancelled", r5.outcome

    asyncio.run(_run())


def case_marker_parser() -> None:
    sample = "**STATUS:** working\nUSER-CONFIRM: approve\nCOMPLETE: done\n"
    markers = extract_markers(sample)
    assert markers["STATUS"] == ["working"]
    assert markers["USER-CONFIRM"] == ["approve"]
    assert markers["COMPLETE"] == ["done"]


def case_pool_ceiling_fallback(tmp: Path | None = None) -> None:
    missing = ROOT / "test/.missing-env-caveats.md"
    assert compute_pool_ceiling(missing) >= 1


async def amain() -> None:
    await case_echo_roundtrip()
    await case_overlimit_frame_drops_and_continues()
    await case_overlimit_response_fails_cleanly()
    await case_runner_overlimit_response_status_counts_drop()
    await case_permission_auto_allow()
    await case_permission_codex_shape_unblocks()
    await case_permission_reject_first_picks_allow()
    await case_permission_reject_only_cancels_and_unblocks()
    await case_permission_auto_allow_false_denies_and_unblocks()
    await case_permission_elicitation_unblocks()
    await case_tool_tracking_closes()
    await case_fine_chunks_vendor_no_dup_marker()
    await case_realtime_blocked_cancels_before_prompt_resolves()
    await case_realtime_blocked_cancel_rebuilds_pool_connection()
    await case_goal_marker_extraction()
    await case_managed_pool_sdk_connection()


def main() -> None:
    case_marker_parser()
    case_codex_acp_elicitation_injection_unit()
    case_permission_handler_selection_unit()
    case_pool_ceiling_fallback()
    asyncio.run(amain())
    print("OK: ACP SDK pipe tests pass")


if __name__ == "__main__":
    main()
