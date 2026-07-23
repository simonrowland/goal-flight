#!/usr/bin/env python3
"""Failure-mode tests for SDK liveness and guard behavior."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("uses POSIX process groups, start_new_session, and signals")

import asyncio
import argparse
import contextlib
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
FAKE_AGENT = ROOT / "tests/fixtures/acp_fake_agent.py"

from goalflight_acp_client import (  # noqa: E402
    AcpError,
    AcpLivenessActivity,
    AcpProcessPool,
    GoalflightClient,
    MAX_DROPPED_FRAME_RECORDS,
    MAX_PERMISSION_ROUTER_DECISIONS,
    JsonRpcLineFilterReader,
    PoolExhaustedError,
    _classify_oversized_json_rpc_head,
)
from goalflight_acp_run import (  # noqa: E402
    _resolve_steer_file,
    adapter_liveness_config,
    agent_command,
    decide_terminal_state,
    spawn_and_handshake_with_retry,
)
import goalflight_acp_permits  # noqa: E402
import goalflight_compat  # noqa: E402
from acp_runner import has_actionable_marker_values  # noqa: E402
from goalflight_liveness import heartbeat_wedge_decision, pgroup_cpu_pct, progress_stall_decision  # noqa: E402


def env_override_fields(text: str, env_name: str) -> dict[str, str]:
    for line in text.splitlines():
        if "GOALFLIGHT_ENV_OVERRIDE" not in line:
            continue
        tokens = shlex.split(line)
        fields = dict(token.split("=", 1) for token in tokens[1:] if "=" in token)
        if fields.get("env") == env_name:
            return fields
    raise AssertionError(f"missing GOALFLIGHT_ENV_OVERRIDE for {env_name}: {text!r}")


def skipif(condition: bool, reason: str):
    def _decorator(func):
        def _wrapped(*args, **kwargs):
            if condition:
                print(f"SKIP: {func.__name__}: {reason}")
                return None
            return func(*args, **kwargs)
        return _wrapped
    return _decorator


def _vendor_event() -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "_x.ai/vendor_progress",
                "content": {"type": "text", "text": "noise"},
            },
        },
    }


def case_vendor_flood_idle_waits_for_quiet_backstop() -> None:
    activity = AcpLivenessActivity()
    for i in range(10):
        activity.note_message(_vendor_event(), 100.0 + i)
    assert activity.raw_events_seen == 10
    assert activity.wedge_progress_seen == 0

    dead = 0
    previous_progress = 0
    for _ in range(3):
        decision = heartbeat_wedge_decision(
            pid_alive=True,
            pgroup_cpu=0.0,
            wedge_progress_seen=activity.wedge_progress_seen,
            previous_wedge_progress_seen=previous_progress,
            outstanding_count=activity.outstanding_count(),
            cpu_epsilon_pct=0.1,
            previous_dead_samples=dead,
            wedge_samples=3,
        )
        dead = decision.dead_samples
        previous_progress = activity.wedge_progress_seen
    assert decision.dead_samples == 3
    assert decision.wedged is False


def case_dropped_frame_records_are_bounded() -> None:
    activity = AcpLivenessActivity()
    for i in range(MAX_DROPPED_FRAME_RECORDS + 5):
        activity.note_dropped_frame({"seq": i, "head": "x" * 1024})
    snapshot = activity.snapshot()
    records = snapshot["dropped_frame_records"]
    assert snapshot["dropped_frames"] == MAX_DROPPED_FRAME_RECORDS + 5
    assert len(records) == MAX_DROPPED_FRAME_RECORDS, records
    assert records[0]["seq"] == 5, records
    assert records[-1]["seq"] == MAX_DROPPED_FRAME_RECORDS + 4, records
    assert all(len(record["head"].encode("utf-8")) <= 1024 for record in records)


def case_empty_oversized_head_assumes_request_for_reply() -> None:
    kind, has_request_id, request_id, id_unrecoverable = _classify_oversized_json_rpc_head(
        b"",
        scan_complete=False,
        scan_truncated=False,
    )
    assert kind == "request"
    assert has_request_id is False
    assert request_id is None
    assert id_unrecoverable is True


def case_vendor_flood_cpu_busy_is_alive() -> None:
    activity = AcpLivenessActivity()
    activity.note_message(_vendor_event(), 100.0)
    decision = heartbeat_wedge_decision(
        pid_alive=True,
        pgroup_cpu=4.0,
        wedge_progress_seen=activity.wedge_progress_seen,
        previous_wedge_progress_seen=activity.wedge_progress_seen,
        outstanding_count=0,
        cpu_epsilon_pct=0.1,
        previous_dead_samples=2,
        wedge_samples=3,
    )
    assert decision.dead_sample is False
    assert decision.wedged is False


def case_standard_progress_resets_wedge_streak() -> None:
    activity = AcpLivenessActivity()
    activity.note_message(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hi"},
                },
            },
        },
        100.0,
    )
    decision = heartbeat_wedge_decision(
        pid_alive=True,
        pgroup_cpu=0.0,
        wedge_progress_seen=activity.wedge_progress_seen,
        previous_wedge_progress_seen=0,
        outstanding_count=0,
        cpu_epsilon_pct=0.1,
        previous_dead_samples=2,
        wedge_samples=3,
    )
    assert decision.dead_sample is False
    assert decision.dead_samples == 0


def case_permission_timeout_unblocks_wedge() -> None:
    activity = AcpLivenessActivity(permission_timeout_s=2.0)
    activity.note_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/request_permission",
            "params": {"toolCall": {"toolCallId": "p1"}},
        },
        10.0,
    )
    snapshot = activity.snapshot(13.0)
    assert snapshot["outstanding_count"] == 1
    assert "p1" in activity.pending_permissions
    timed_out_tool = activity.timed_out(13.0, max_tool_s=60.0)
    assert timed_out_tool is not None
    tool_id, age_s = timed_out_tool
    terminal_state = "tool_timeout"
    terminal_error = {
        "code": -1,
        "message": "tool_timeout",
        "toolCallId": tool_id,
        "age_s": round(age_s, 3),
    }
    assert terminal_state == "tool_timeout"
    assert terminal_error["toolCallId"] == "p1"
    assert terminal_error["age_s"] == 3.0
    assert activity.outstanding_count(13.0) == 0


def case_progress_stall_wall_ignores_raw_vendor_noise() -> None:
    activity = AcpLivenessActivity()
    activity.reset_progress_clock(0.0)
    activity.note_message(_vendor_event(), 299.0)

    snapshot = activity.snapshot(301.0)

    assert snapshot["quiet_for_s"] == 2.0
    assert progress_stall_decision(
        pid_alive=True,
        progress_quiet_s=snapshot["progress_quiet_for_s"],
        progress_stall_s=300.0,
        outstanding_count=snapshot["outstanding_count"],
    ) is True


def case_adapter_manifest_liveness_defaults() -> None:
    profile, remote_turn_silence_s = adapter_liveness_config("codex")
    assert profile == "remote_api"
    assert remote_turn_silence_s == 1200.0

    profile, remote_turn_silence_s = adapter_liveness_config("codex-acp")
    assert profile == "remote_api"
    assert remote_turn_silence_s == 1200.0

    profile, remote_turn_silence_s = adapter_liveness_config("cursor-agent")
    assert profile == "remote_api"
    assert remote_turn_silence_s == 1200.0

    profile, remote_turn_silence_s = adapter_liveness_config("claude")
    assert profile == "remote_api"
    assert remote_turn_silence_s == 1200.0

    profile, remote_turn_silence_s = adapter_liveness_config("claude-acp")
    assert profile == "remote_api"
    assert remote_turn_silence_s == 1200.0

    profile, remote_turn_silence_s = adapter_liveness_config("missing-agent")
    assert profile == "local_compute"
    assert remote_turn_silence_s == 1200.0


def case_manifest_acp_command_defaults() -> None:
    binary, args = agent_command("grok")
    assert Path(binary).name == "grok"
    assert args == ["agent", "stdio"]

    binary, args = agent_command("grok-acp")
    assert Path(binary).name == "grok"
    # grok-acp now omits --model too (grok's CLI default grok-4.5 applies and
    # writes reliably through ACP); an explicit model still passes through.
    assert args == ["agent", "stdio"]

    binary, args = agent_command("cursor")
    assert Path(binary).name == "cursor-agent"
    assert args == ["acp"]

    binary, args = agent_command("cursor-agent")
    assert Path(binary).name == "cursor-agent"
    assert args == ["acp"]

    binary, args = agent_command("codex-acp")
    assert binary == "codex-acp"
    assert args == []

    binary, args = agent_command("claude-acp")
    assert Path(binary).name == "claude-code-cli-acp"
    assert args == []  # claude ACP server mode requires no argv flags

    binary, args = agent_command("opencode")
    assert Path(binary).name == "opencode"
    assert args == ["acp"]


def case_json_rpc_stdout_filter() -> None:
    async def _run() -> None:
        inner = asyncio.StreamReader()
        inner.feed_data(b"[opencode-litellm] discovered models\n")
        inner.feed_data(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
        inner.feed_eof()
        filtered = JsonRpcLineFilterReader(inner)
        line = await filtered.readuntil(b"\n")
        assert line.startswith(b"{"), line
        assert filtered.skipped_lines == 1

    asyncio.run(_run())


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _make_fake_agent_wrapper(tmp: Path, scenario: str | None = None) -> Path:
    wrapper = tmp / "fake-acp-agent"
    lines = ["#!/usr/bin/env bash"]
    if scenario is not None:
        # Bake the scenario into the wrapper so direct-spawn cases (which inherit
        # the test process env rather than passing an explicit subprocess env)
        # select it without mutating os.environ.
        lines.append(f"export GOALFLIGHT_FAKE_ACP_SCENARIO={shlex.quote(scenario)}")
    lines.append(f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_AGENT))}")
    wrapper.write_text("\n".join(lines) + "\n")
    wrapper.chmod(0o755)
    return wrapper


def _write_supported_adapter_manifest(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
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


def _force_kill(pid: object) -> None:
    """Best-effort reap of a possibly-orphaned worker pid (test cleanup only)."""
    if not isinstance(pid, int):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGKILL)


def _kill_from_status(status: Path) -> None:
    try:
        payload = json.loads(status.read_text())
    except Exception:
        payload = {}
    pgid = payload.get("pgid")
    if isinstance(pgid, int):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    pid = payload.get("worker_pid")
    if isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _run_fake_runner(
    scenario: str,
    *,
    progress_stall_s: float,
    heartbeat_interval: float = 0.1,
    wedge_samples: int = 2,
    idle_timeout: float = 10.0,
    max_quiet_s: float = 10.0,
    max_tool_s: float = 10.0,
    liveness_profile: str | None = None,
    remote_turn_silence_s: float | None = None,
    remote_turn_cancel_grace_s: float = 0.1,
    stall_kill: bool = False,
    extra_env: dict[str, str] | None = None,
    state_snapshot: dict | None = None,
    timeout_s: float = 30.0,
) -> tuple[int, dict, str, str]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status = tmp / f"{scenario}.status.json"
        wrapper = _make_fake_agent_wrapper(tmp)
        adapters_dir = tmp / "adapters"
        _write_supported_adapter_manifest(adapters_dir, wrapper.name)
        env = os.environ.copy()
        env.pop("GOALFLIGHT_STEER_FILE", None)
        env.pop("GOAL_FLIGHT_PERMISSION_DIR", None)
        env.update(
            {
                "GOALFLIGHT_STATE_DIR": str(state_dir),
                "GOALFLIGHT_FAKE_ACP_SCENARIO": scenario,
                "GOALFLIGHT_FAKE_ACP_INTERVAL": "0.05",
                "GOALFLIGHT_ACP_PYTHON": sys.executable,
                "GOALFLIGHT_ADAPTERS_DIR": str(adapters_dir),
                "GOALFLIGHT_ALLOW_ADAPTERS_DIR_OVERRIDE": "1",
            }
        )
        if extra_env:
            env.update(extra_env)
        args = [
            sys.executable,
            "scripts/goalflight_acp_run.py",
            "--agent",
            str(wrapper),
            "--cwd",
            str(ROOT),
            "--prompt-text",
            "hello",
            "--status-json",
            str(status),
            "--heartbeat-interval",
            str(heartbeat_interval),
            "--wedge-samples",
            str(wedge_samples),
            "--progress-stall-s",
            str(progress_stall_s),
            "--idle-timeout",
            str(idle_timeout),
            "--max-quiet-s",
            str(max_quiet_s),
            "--max-tool-s",
            str(max_tool_s),
            "--json",
        ]
        if liveness_profile is not None:
            args.extend(["--liveness-profile", liveness_profile])
        if remote_turn_silence_s is not None:
            args.extend(["--remote-turn-silence-s", str(remote_turn_silence_s)])
            args.extend(["--remote-turn-cancel-grace-s", str(remote_turn_cancel_grace_s)])
        if stall_kill:
            args.append("--stall-kill")
        proc = subprocess.Popen(
            args,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_from_status(status)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            stdout, stderr = proc.communicate()
            raise AssertionError(f"{scenario} runner timed out\nstdout={stdout}\nstderr={stderr}")
        if not status.exists():
            raise AssertionError(f"{scenario} wrote no status\nstdout={stdout}\nstderr={stderr}")
        status_payload = json.loads(status.read_text())
        if state_snapshot is not None:
            capacity_path = state_dir / "capacity.json"
            state_snapshot["capacity"] = (
                json.loads(capacity_path.read_text()) if capacity_path.exists() else {}
            )
            runs_dir = state_dir / "runs.d"
            state_snapshot["records"] = [
                json.loads(path.read_text())
                for path in sorted(runs_dir.glob("*.json"))
            ] if runs_dir.exists() else []
        return proc.returncode, status_payload, stdout, stderr


@skipif(os.name == "nt", reason="matrix timeout reap is POSIX process-group behavior")
def case_matrix_timeout_reaps_runner_process_group() -> None:
    import goalflight_acp_push_gate_matrix as matrix

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        child_pid_file = tmp / "child.pid"
        fake_runner = tmp / "hanging_matrix_runner.py"
        fake_runner.write_text(
            "\n".join(
                [
                    "import json, os, subprocess, sys, time",
                    "from pathlib import Path",
                    "status_path = Path(sys.argv[sys.argv.index('--status-json') + 1])",
                    "child_file = Path(os.environ['GF_MATRIX_CHILD_PID_FILE'])",
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                    "child_file.write_text(str(child.pid), encoding='utf-8')",
                    "status_path.write_text(json.dumps({'state': 'running', 'worker_pid': child.pid, 'pgid': os.getpgid(0)}) + '\\n', encoding='utf-8')",
                    "time.sleep(60)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        old_runner = matrix.RUNNER
        old_child_file = os.environ.get("GF_MATRIX_CHILD_PID_FILE")
        os.environ["GF_MATRIX_CHILD_PID_FILE"] = str(child_pid_file)
        try:
            matrix.RUNNER = fake_runner
            run = matrix._run_acp_case(
                matrix.AgentSpec("fake", "fake", timeout_s=0.5),
                state_root=tmp / "matrix-state",
                cwd=tmp,
                dispatch_id="matrix-timeout-reap",
                prompt="hang",
                timeout_s=0.5,
                idle_timeout_s=10.0,
            )
            assert run.timed_out, run
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            deadline = time.time() + 5.0
            while time.time() < deadline and _pid_alive(child_pid):
                time.sleep(0.05)
            assert not _pid_alive(child_pid), f"matrix timeout left child alive: {child_pid}"
        finally:
            matrix.RUNNER = old_runner
            if old_child_file is None:
                os.environ.pop("GF_MATRIX_CHILD_PID_FILE", None)
            else:
                os.environ["GF_MATRIX_CHILD_PID_FILE"] = old_child_file
            if child_pid_file.exists():
                with contextlib.suppress(Exception):
                    _force_kill(int(child_pid_file.read_text(encoding="utf-8")))


def case_permission_router_audit_bounded_and_truncated() -> None:
    async def _run() -> None:
        client = GoalflightClient(cwd=str(ROOT))
        options = [{"kind": "allow_once", "optionId": "ok"}]
        for i in range(MAX_PERMISSION_ROUTER_DECISIONS + 5):
            await client.request_permission(
                options,
                "session-audit",
                {
                    "toolCallId": f"perm-{i}",
                    "title": "T" * 400,
                    "kind": "read",
                },
            )
        rows = client.permission_router_decisions
        assert len(rows) == MAX_PERMISSION_ROUTER_DECISIONS, rows
        assert rows[0]["tool_call_id"] == "perm-5", rows[0]
        assert len(rows[-1]["title"]) <= 160, rows[-1]

    asyncio.run(_run())


def case_normal_dispatch_hides_matrix_audit_surface() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "permission_codex",
        progress_stall_s=5.0,
        timeout_s=30.0,
    )
    assert returncode == 0, (returncode, stdout, stderr, status)
    assert status["state"] == "complete", status
    assert "permission_router_decisions" not in status, status
    assert "tool_calls" not in status, status


def case_matrix_env_surfaces_bounded_audit() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "permission_codex",
        progress_stall_s=5.0,
        extra_env={"GOALFLIGHT_ACP_LIVE_MATRIX": "1"},
        timeout_s=30.0,
    )
    assert returncode == 0, (returncode, stdout, stderr, status)
    assert status["state"] == "complete", status
    rows = status.get("permission_router_decisions") or []
    assert rows and len(rows) <= MAX_PERMISSION_ROUTER_DECISIONS, status
    assert status.get("tool_calls") == [], status


def case_matrix_claude_defer_skips_remaining_cases() -> None:
    import goalflight_acp_push_gate_matrix as matrix

    spec = matrix.AgentSpec("claude-acp", "claude-acp", defer_headless_failures=True)
    old_agents = matrix.AGENTS
    old_preflight = matrix._preflight
    old_round_trip = matrix._round_trip
    old_auto_and_locations = matrix._auto_and_locations
    old_ledger_stats = matrix._ledger_stats
    old_held_permission = matrix._held_permission
    old_silent_turn = matrix._silent_turn
    try:
        matrix.AGENTS = (spec,)
        matrix._preflight = lambda _spec: (True, "ready")
        matrix._round_trip = lambda _spec, _state_root, _work_root: matrix.MatrixCell(
            "claude-acp", "round_trip", "PASS", "complete", "matrix-claude-roundtrip"
        )
        matrix._auto_and_locations = lambda _spec, _state_root, _work_root: (
            matrix.MatrixCell(
                "claude-acp",
                "auto_permission",
                "SKIP",
                "claude-acp deferred (headless auth/PTY): failed",
                "matrix-claude-boundary",
            ),
            matrix.MatrixCell("claude-acp", "locations", "FAIL", "should not be emitted"),
        )

        def _should_not_run(*_args, **_kwargs):
            raise AssertionError("deferred claude-acp row should not run later matrix cases")

        matrix._ledger_stats = _should_not_run
        matrix._held_permission = _should_not_run
        matrix._silent_turn = _should_not_run
        with tempfile.TemporaryDirectory() as td:
            rc, report = matrix.run_matrix(
                argparse.Namespace(agents=["claude-acp"], state_dir=td, hold_seconds=65.0)
            )
        assert rc == 0, report
        by_prop = {row["property"]: row for row in report["results"]}
        assert by_prop["round_trip"]["status"] == "PASS", by_prop
        for prop in ("auto_permission", "ledger_stats", "held_permission", "locations", "silent_turn"):
            assert by_prop[prop]["status"] == "SKIP", by_prop
            assert by_prop[prop]["detail"].startswith("claude-acp deferred"), by_prop[prop]
    finally:
        matrix.AGENTS = old_agents
        matrix._preflight = old_preflight
        matrix._round_trip = old_round_trip
        matrix._auto_and_locations = old_auto_and_locations
        matrix._ledger_stats = old_ledger_stats
        matrix._held_permission = old_held_permission
        matrix._silent_turn = old_silent_turn


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_raw_vendor_flood_hits_progress_stall_and_reaps() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "raw_vendor_flood",
        progress_stall_s=0.5,
        stall_kill=True,
    )

    assert returncode != 0, stdout
    assert status["state"] == "wedged", status
    assert status["error"]["message"] == "progress_stall", status
    assert status["wedge_progress_seen"] == 0, status
    assert status["events_seen"] > 0, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_progress_stall_detaches_by_default() -> None:
    state_snapshot: dict = {}
    returncode, status, stdout, stderr = _run_fake_runner(
        "progress_then_silent",
        progress_stall_s=0.5,
        heartbeat_interval=0.1,
        wedge_samples=999,
        idle_timeout=0.0,
        max_quiet_s=30.0,
        max_tool_s=30.0,
        state_snapshot=state_snapshot,
        timeout_s=30.0,
    )
    worker_pid = status.get("worker_pid")
    try:
        assert returncode != 0, stdout
        assert status["state"] == "stalled", status
        assert status["error"]["message"] == "progress_stall", status
        assert status["worker_alive"] is True, status
        assert status["worker_still_alive"] is True, status
        assert status["killed_by_heartbeat"] is False, status
        assert status["wedged_by_heartbeat"] is False, status
        assert status["markers"]["STALLED"], status
        assert _pid_alive(worker_pid), (status, stderr)

        dispatch_id = status["dispatch_id"]
        records = [r for r in state_snapshot.get("records", []) if r.get("dispatch_id") == dispatch_id]
        assert records and records[-1].get("state") == "stalled", records
        assert records[-1].get("terminal_state") == "stalled", records[-1]
        assert records[-1].get("worker_still_alive") is True, records[-1]
        leases = [
            lease
            for lease in (state_snapshot.get("capacity", {}).get("leases") or {}).values()
            if lease.get("dispatch_id") == dispatch_id
        ]
        assert leases and leases[-1].get("state") == "active", leases
        assert not leases[-1].get("released_at"), leases[-1]
        assert leases[-1].get("worker_pid") == worker_pid, leases[-1]
        assert leases[-1].get("controller_pid") == worker_pid, leases[-1]
    finally:
        _force_kill(worker_pid)


def case_detached_pidfile_entry_survives_ghost_cleanup() -> None:
    # P0 (the orphan the adversarial review caught): D2 leaves the worker running
    # on a non-destructive stall, so its pidfile entry must be marked detached and
    # a LATER cleanup_ghosts (this orchestrator's next dispatch OR a sibling project
    # sharing /tmp/goal-flight-*/goal-flight-acp-pids.d) MUST SKIP it, not SIGKILL
    # the live, intentional worker. Proven against a real live process + isolated
    # pidfile dir; the inverse (detached=false) must still be reaped.
    import goalflight_acp_client as ac
    # Monkeypatch the module's _PIDFILE_DIR directly (cleanup_ghosts reads it at
    # call time). Do NOT importlib.reload(ac): reload creates a SECOND module copy
    # that diverges from the spawn_and_handshake_with_retry imported at top of this
    # file, corrupting shared connection/event-loop state for later cases in the
    # same process (it false-failed case_handshake_wedge_kills_before_respawn).
    old_pidfile_dir = ac._PIDFILE_DIR
    old_ps_meta = ac._ps_meta
    tmp = Path(tempfile.mkdtemp(prefix="gf-detach-ghost-"))
    ac._PIDFILE_DIR = tmp
    worker = subprocess.Popen(["sleep", "30"])
    try:
        dead_controller_pid = 999999  # not a live pid
        lstart, comm = "Mon Jan  1 00:00:00 2026", "sleep"

        def fake_ps_meta(pid: int):
            if pid == worker.pid:
                return lstart, comm
            if pid == dead_controller_pid:
                return None
            return old_ps_meta(pid)

        ac._ps_meta = fake_ps_meta
        pidfile = tmp / f"{dead_controller_pid}.jsonl"
        base = {
            "pid": worker.pid, "pgid": worker.pid, "started_at": lstart,
            "cmd": comm, "agent": "codex-acp", "session_id": "s",
        }
        # detached worker -> NOT killed, survives.
        pidfile.write_text(json.dumps({**base, "detached": True}) + "\n")
        assert ac.cleanup_ghosts() == 0, "detached worker must not be reaped"
        assert worker.poll() is None, "detached worker must survive cleanup_ghosts"
        # control: identical entry without detached -> reaped (skip stays specific).
        pidfile.write_text(json.dumps({**base, "detached": False}) + "\n")
        assert ac.cleanup_ghosts() == 1, "non-detached ghost must still be reaped"
    finally:
        with contextlib.suppress(Exception):
            worker.kill()
        ac._PIDFILE_DIR = old_pidfile_dir
        ac._ps_meta = old_ps_meta


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_progress_then_silent_wedges_and_reaps() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "progress_then_silent",
        progress_stall_s=30.0,
        liveness_profile="local_compute",
        extra_env={"GOALFLIGHT_TEST_PGROUP_CPU_PCT": "0.0", "GOALFLIGHT_TEST_MODE": "1"},
    )

    assert returncode != 0, stdout
    assert status["state"] == "wedged", status
    assert status["error"]["message"] == "wedged_by_heartbeat", status
    assert status["wedge_progress_seen"] >= 1, status
    assert status["heartbeat_dead_samples"] >= 2, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_remote_long_reasoning_pause_survives_old_walls() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "long_reasoning_pause",
        progress_stall_s=0.3,
        idle_timeout=0.0,
        max_quiet_s=0.3,
        max_tool_s=30.0,
        liveness_profile="remote_api",
        remote_turn_silence_s=3.0,
        extra_env={"GOALFLIGHT_FAKE_ACP_LONG_PAUSE_S": "1.0"},
        timeout_s=30.0,
    )

    assert returncode == 0, (stdout, stderr, status)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status
    assert status["liveness_profile"] == "remote_api", status
    assert "finished" in (status.get("text_excerpt") or ""), status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_remote_dead_silent_turn_hits_remote_wall() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "dead_silent_turn",
        progress_stall_s=30.0,
        heartbeat_interval=0.1,
        idle_timeout=0.0,
        max_quiet_s=30.0,
        max_tool_s=30.0,
        liveness_profile="remote_api",
        remote_turn_silence_s=0.4,
        remote_turn_cancel_grace_s=0.1,
        timeout_s=30.0,
    )

    assert returncode != 0, stdout
    assert status["state"] == "remote_turn_silence", status
    assert status["error"]["message"] == "remote_turn_silence", status
    assert status["liveness_profile"] == "remote_api", status
    assert status["turn_silent_for_s"] >= 0.4, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_max_quiet_requires_confirmed_idle_cpu() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "long_reasoning_pause",
        progress_stall_s=30.0,
        heartbeat_interval=0.05,
        wedge_samples=99,
        idle_timeout=5.0,
        max_quiet_s=0.05,
        max_tool_s=30.0,
        extra_env={
            "GOALFLIGHT_FAKE_ACP_LONG_PAUSE_S": "0.6",
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_TEST_PGROUP_CPU_PCT": "unavailable",
        },
        timeout_s=30.0,
    )

    assert returncode == 0, (stdout, stderr, status)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status
    assert status.get("error") is None, status


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_max_quiet_kills_confirmed_idle_cpu() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "long_reasoning_pause",
        progress_stall_s=30.0,
        heartbeat_interval=0.05,
        wedge_samples=99,
        idle_timeout=5.0,
        max_quiet_s=0.05,
        max_tool_s=30.0,
        extra_env={
            "GOALFLIGHT_FAKE_ACP_LONG_PAUSE_S": "0.6",
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_TEST_PGROUP_CPU_PCT": "0.0",
        },
        timeout_s=30.0,
    )

    assert returncode != 0, (stdout, stderr, status)
    assert status["state"] == "wedged", status
    assert status["error"]["message"] == "max_quiet_s", status
    assert status["error"]["cpu_pct"] == 0.0, status
    assert status["worker_alive"] is False, status


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_max_quiet_ignores_busy_cpu() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "long_reasoning_pause",
        progress_stall_s=30.0,
        heartbeat_interval=0.05,
        wedge_samples=99,
        idle_timeout=5.0,
        max_quiet_s=0.05,
        max_tool_s=30.0,
        extra_env={
            "GOALFLIGHT_FAKE_ACP_LONG_PAUSE_S": "0.6",
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_TEST_PGROUP_CPU_PCT": "5.0",
        },
        timeout_s=30.0,
    )

    assert returncode == 0, (stdout, stderr, status)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_thought_stream_survives_progress_stall_wall() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "thought_stream_pause",
        progress_stall_s=0.5,
        heartbeat_interval=0.05,
        idle_timeout=0.0,
        max_quiet_s=30.0,
        extra_env={
            "GOALFLIGHT_FAKE_ACP_INTERVAL": "0.1",
            "GOALFLIGHT_FAKE_ACP_THOUGHT_CHUNKS": "10",
        },
        timeout_s=30.0,
    )

    # Thought/progress events prove the worker was live, but they are not final
    # assistant output. The transport completed without a required terminal
    # marker, so the result must not be reported as complete.
    assert returncode != 0, (stdout, stderr, status)
    assert status["state"] == "failed", status
    assert status["ok"] is False, status
    assert status["reason"] == "empty_session", status
    assert status["error"]["reason"] == "empty_session", status
    assert status["wedge_progress_seen"] >= 1, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


def case_terminal_state_endturn_beats_tail_race_wedge() -> None:
    # Classification precedence (pure): a genuine end_turn must beat a heartbeat
    # wedge that tripped in the completion tail, but a real wedge (no end_turn)
    # must still win. Deterministic guard for the runner's decide_terminal_state.

    # end_turn received + heartbeat wedged in the tail -> complete, not wedged.
    state, error = decide_terminal_state(
        result_ok=True,
        result_error=None,
        result_text="Completed work.",
        heartbeat_outcome="wedged",
        killed_by_heartbeat=True,
        cancelled_for_marker=False,
        early_marker=None,
        heartbeat_error={"code": -1, "message": "wedged_by_heartbeat"},
    )
    assert state == "complete", state
    assert error is None, error

    # Real wedge (no end_turn) -> the heartbeat verdict wins.
    state, error = decide_terminal_state(
        result_ok=False,
        result_error={"code": -1, "message": "agent_timeout (idle)"},
        heartbeat_outcome="wedged",
        killed_by_heartbeat=True,
        cancelled_for_marker=False,
        early_marker=None,
        heartbeat_error={"code": -1, "message": "wedged_by_heartbeat"},
    )
    assert state == "wedged", state
    assert error == {"code": -1, "message": "wedged_by_heartbeat"}, error

    # tool_timeout is an outstanding-tool anomaly, NOT a silence wedge -> end_turn
    # does NOT refute it; it wins even when result_ok is True.
    state, error = decide_terminal_state(
        result_ok=True,
        result_error=None,
        heartbeat_outcome="tool_timeout",
        killed_by_heartbeat=True,
        cancelled_for_marker=False,
        early_marker=None,
        heartbeat_error={"code": -1, "message": "tool_timeout", "toolCallId": "t1"},
    )
    assert state == "tool_timeout", state
    assert error["message"] == "tool_timeout", error

    # killed_by_heartbeat with no recorded outcome defaults to the silence wedge.
    state, error = decide_terminal_state(
        result_ok=False,
        result_error=None,
        heartbeat_outcome=None,
        killed_by_heartbeat=True,
        cancelled_for_marker=False,
        early_marker=None,
        heartbeat_error=None,
    )
    assert state == "wedged", state
    assert error == {"code": -1, "message": "wedged"}, error

    # Early marker cancel, no heartbeat -> blocked.
    state, error = decide_terminal_state(
        result_ok=False,
        result_error=None,
        heartbeat_outcome=None,
        killed_by_heartbeat=False,
        cancelled_for_marker=True,
        early_marker="BLOCKED",
        heartbeat_error=None,
    )
    assert state == "blocked", state
    assert error["marker"] == "BLOCKED", error

    # Plain failure (no end_turn, no heartbeat, no marker) -> failed.
    state, error = decide_terminal_state(
        result_ok=False,
        result_error={"code": -1, "message": "agent_timeout (idle)"},
        heartbeat_outcome=None,
        killed_by_heartbeat=False,
        cancelled_for_marker=False,
        early_marker=None,
        heartbeat_error=None,
    )
    assert state == "failed", state


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_blocked_none_completes() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "blocked_none",
        progress_stall_s=30.0,
        idle_timeout=5.0,
        timeout_s=20.0,
    )

    assert returncode == 0, (stdout, stderr, status)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status
    assert status["error"] is None, status
    assert status["markers"]["BLOCKED"] == ["none"], status
    assert status["markers"]["COMPLETE"] == ["goal done"], status
    assert not has_actionable_marker_values(status["markers"], "BLOCKED")


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_blocked_substantive_cancels() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "blocked",
        progress_stall_s=30.0,
        heartbeat_interval=1.0,
        wedge_samples=999,
        idle_timeout=5.0,
        timeout_s=20.0,
    )

    assert returncode != 0, (stdout, stderr, status)
    assert status["state"] == "blocked", status
    assert status["ok"] is False, status
    assert status["error"]["marker"] == "BLOCKED", status
    assert status["error"]["message"] == "early_marker_cancelled", status
    assert status["markers"]["BLOCKED"] == ["need maintainer"], status


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_user_need_none_completes() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "user_need_none",
        progress_stall_s=30.0,
        idle_timeout=5.0,
        timeout_s=20.0,
    )

    assert returncode == 0, (stdout, stderr, status)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status
    assert status["error"] is None, status
    assert status["markers"]["USER-NEED"] == ["none"], status
    assert not has_actionable_marker_values(status["markers"], "USER-NEED")


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_idle_silent_idle_timeout_reaps() -> None:
    # IdleLivenessGate / on_idle path: a worker that emits nothing and never
    # responds is reaped by the run_prompt idle timeout (not the heartbeat —
    # the wedge backstop can't fire without a prior progress event). Keep the
    # heartbeat backstops well out of the way (30s) so only the idle path can
    # trip; the idle classifier writes state="failed" + agent_timeout(idle),
    # a signature distinct from the heartbeat "wedged" terminals.
    returncode, status, stdout, stderr = _run_fake_runner(
        "idle_silent",
        progress_stall_s=30.0,
        idle_timeout=0.5,
        max_quiet_s=30.0,
        max_tool_s=30.0,
        timeout_s=20.0,
    )

    assert returncode != 0, stdout
    assert status["state"] == "failed", status
    assert status["error"]["message"] == "agent_timeout (idle)", status
    assert status["wedge_progress_seen"] == 0, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_oversized_frame_dropped_then_completes() -> None:
    # GuardedStreamReader drop-and-continue at the runner level: with a small
    # ACP frame limit the agent's over-limit frame is dropped (and counted in
    # acp_dropped_frames via the shared liveness activity) while the runner
    # reads past it to the following normal frame + end_turn and finishes clean.
    # (Malformed-but-in-limit JSON tolerance is the SDK's job, not the runner's,
    # so it isn't re-tested here — acp_dropped_frames is the runner-owned signal.)
    #
    # heartbeat_interval is kept far above the (sub-second) turn duration so this
    # wiring test stays deterministic and focused on the drop-and-continue path,
    # independent of heartbeat timing. (The classification race a fast heartbeat
    # used to expose — a tail-race wedge mislabeling a completed turn — is fixed
    # in decide_terminal_state and guarded deterministically by
    # case_terminal_state_endturn_beats_tail_race_wedge.)
    returncode, status, stdout, stderr = _run_fake_runner(
        "overlimit",
        progress_stall_s=30.0,
        heartbeat_interval=30.0,
        extra_env={"GOALFLIGHT_ACP_LIMIT": "4096"},
    )

    assert returncode == 0, (stdout, stderr)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status
    assert status["acp_dropped_frames"] >= 1, status
    record = status["acp_dropped_frame_records"][0]
    assert record["byte_count"] > 4096, status
    assert record["kind"] == "notification", status
    assert len(record["head"].encode("utf-8")) <= 1024, status
    assert "after-limit" in (status.get("text_excerpt") or ""), status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_oversized_request_gets_safe_reply() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "overlimit_request",
        progress_stall_s=30.0,
        heartbeat_interval=30.0,
        extra_env={"GOALFLIGHT_ACP_LIMIT": "4096"},
    )

    assert returncode == 0, (stdout, stderr)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status
    assert status["acp_dropped_frames"] >= 1, status
    record = status["acp_dropped_frame_records"][0]
    assert record["kind"] == "request", status
    assert record["id"] == 4242, status
    assert record["safe_reply_sent"] is True, status
    assert record["byte_count"] > 4096, status
    assert len(record["head"].encode("utf-8")) <= 1024, status
    assert "request-error:oversized frame dropped" in (status.get("text_excerpt") or ""), status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_oversized_request_late_id_gets_safe_reply() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "overlimit_request_late_id",
        progress_stall_s=30.0,
        heartbeat_interval=30.0,
        extra_env={"GOALFLIGHT_ACP_LIMIT": "4096"},
    )

    assert returncode == 0, (stdout, stderr)
    assert status["state"] == "complete", status
    assert status["ok"] is True, status
    assert status["acp_dropped_frames"] >= 1, status
    record = status["acp_dropped_frame_records"][0]
    assert record["kind"] == "request", status
    assert record["id"] == 5151, status
    assert record["safe_reply_sent"] is True, status
    assert record.get("id_unrecoverable") is not True, status
    assert record["byte_count"] > 4096, status
    assert len(record["head"].encode("utf-8")) <= 1024, status
    assert "request-error:oversized frame dropped" in (status.get("text_excerpt") or ""), status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_oversized_no_newline_kills_worker() -> None:
    returncode, status, stdout, stderr = _run_fake_runner(
        "overlimit_no_newline",
        progress_stall_s=30.0,
        heartbeat_interval=30.0,
        idle_timeout=5.0,
        timeout_s=20.0,
        extra_env={
            "GOALFLIGHT_ACP_LIMIT": "4096",
            "GOALFLIGHT_ACP_OVERSIZED_DRAIN_CAP": "8192",
        },
    )

    assert returncode != 0, stdout
    assert status["state"] == "failed", status
    assert status["error"], status
    assert status["acp_dropped_frames"] >= 1, status
    record = status["acp_dropped_frame_records"][0]
    assert record["kind"] == "notification", status
    assert record["drain_cap_exceeded"] is True, status
    assert record["drain_cap"] == 8192, status
    assert record["byte_count"] > 8192, status
    assert len(record["head"].encode("utf-8")) <= 1024, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_goal_mode_progress_stall_backstop() -> None:
    # idle-timeout=0 (goal mode: rely on PID liveness + terminal markers). The
    # run_prompt idle path is fully disabled, so the heartbeat progress-stall
    # wall is the only backstop — it must still reap a raw-vendor-noise flood
    # (vendor events never reset the standard-progress clock).
    returncode, status, stdout, stderr = _run_fake_runner(
        "raw_vendor_flood",
        progress_stall_s=0.5,
        idle_timeout=0.0,
        stall_kill=True,
    )

    assert returncode != 0, stdout
    assert status["state"] == "wedged", status
    assert status["error"]["message"] == "progress_stall", status
    assert status["wedge_progress_seen"] == 0, status
    assert status["events_seen"] > 0, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_goal_mode_heartbeat_backstop() -> None:
    # idle-timeout=0 (goal mode), progress-stall wall held off (30s): the
    # heartbeat dead-sample wedge detector is the only backstop and must still
    # reap a worker that made one bit of progress then went silent.
    returncode, status, stdout, stderr = _run_fake_runner(
        "progress_then_silent",
        progress_stall_s=30.0,
        idle_timeout=0.0,
        extra_env={"GOALFLIGHT_TEST_PGROUP_CPU_PCT": "0.0", "GOALFLIGHT_TEST_MODE": "1"},
    )

    assert returncode != 0, stdout
    assert status["state"] == "wedged", status
    assert status["error"]["message"] == "wedged_by_heartbeat", status
    assert status["wedge_progress_seen"] >= 1, status
    assert status["heartbeat_dead_samples"] >= 2, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_runner_tool_timeout_reaps() -> None:
    # Per-tool absolute wall: a worker that opens a tool call and never resolves
    # it (no completed update, no end_turn) is reaped by tool_timeout. The
    # outstanding tool gates OFF the silence-class backstops, so tool_timeout is
    # the deterministic terminal — and decide_terminal_state surfaces it as a
    # real anomaly rather than masking it (end_turn never arrives here anyway,
    # but tool_timeout is exempt from result_ok masking by design).
    returncode, status, stdout, stderr = _run_fake_runner(
        "tool_stuck",
        progress_stall_s=30.0,
        max_tool_s=0.5,
    )

    assert returncode != 0, stdout
    assert status["state"] == "tool_timeout", status
    assert status["error"]["message"] == "tool_timeout", status
    assert status["killed_by_heartbeat"] is True, status
    assert status["worker_alive"] is False, status
    assert not _pid_alive(status.get("worker_pid")), (status, stderr)


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_handshake_wedge_kills_before_respawn() -> None:
    # spawn_and_handshake_with_retry against a worker that spawns but never
    # answers initialize. Each attempt must hit the handshake_timeout, kill the
    # wedged worker BEFORE respawning (never leave an identity-matched PID
    # alive), and after exhausting attempts raise AcpError with no orphan left.
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            wrapper = _make_fake_agent_wrapper(Path(td), scenario="handshake_wedge")
            spawned: list[int] = []
            pid0_alive_at_respawn: dict[str, object] = {"value": None}

            def record(attempt: int, proc: object) -> None:
                spawned.append(proc.pid)  # type: ignore[attr-defined]
                if attempt == 1 and spawned:
                    # Captured at the moment the respawn spawns: attempt 0's
                    # worker must already be dead (kill-before-respawn).
                    pid0_alive_at_respawn["value"] = _pid_alive(spawned[0])

            try:
                raised: AcpError | None = None
                try:
                    await spawn_and_handshake_with_retry(
                        str(wrapper),
                        [],
                        agent="fake",
                        session_id="s",
                        cwd=str(ROOT),
                        attempts=2,
                        handshake_timeout=0.5,
                        on_attempt=record,
                    )
                except AcpError as e:
                    raised = e
                assert raised is not None, "expected AcpError after exhausting handshake attempts"
                assert "handshake failed after 2 attempt" in str(raised), str(raised)
                assert len(spawned) == 2, spawned
                assert spawned[0] != spawned[1], spawned
                assert pid0_alive_at_respawn["value"] is False, "attempt-0 worker not killed before respawn"
                for pid in spawned:
                    assert not _pid_alive(pid), f"handshake-wedged worker {pid} left alive"
            finally:
                for pid in spawned:
                    _force_kill(pid)

    asyncio.run(_run())


@skipif(os.name == "nt", reason="native Windows ACP dispatch is refused in Phase 1")
def case_pool_exhaustion_then_drain() -> None:
    # AcpProcessPool: get_or_create up to the ceiling, the next raises
    # PoolExhaustedError without spawning, and shutdown() drains every worker
    # (no surviving PIDs).
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            wrapper = _make_fake_agent_wrapper(Path(td))  # default handshake-only agent
            config = {"fake": {"command": str(wrapper), "acp_args": []}}
            pool = AcpProcessPool(config, max_processes=2, max_per_agent=10, auto_allow_tools=True)
            spawned: list[int] = []
            try:
                c1 = await pool.get_or_create("fake", "s1", cwd=str(ROOT))
                c2 = await pool.get_or_create("fake", "s2", cwd=str(ROOT))
                spawned += [c1.proc.pid, c2.proc.pid]
                assert pool.stats["total"] == 2, pool.stats
                assert _pid_alive(c1.proc.pid) and _pid_alive(c2.proc.pid), spawned

                raised = False
                try:
                    await pool.get_or_create("fake", "s3", cwd=str(ROOT))
                except PoolExhaustedError:
                    raised = True
                assert raised, "expected PoolExhaustedError at the pool ceiling"
                assert pool.stats["total"] == 2, pool.stats

                await pool.shutdown()
                assert pool.stats["total"] == 0, pool.stats
                for pid in spawned:
                    assert not _pid_alive(pid), f"worker {pid} survived pool drain"
            finally:
                with contextlib.suppress(Exception):
                    await pool.shutdown()
                for pid in spawned:
                    _force_kill(pid)

    asyncio.run(_run())


def case_env_ipc_paths_are_constrained() -> None:
    keys = [
        "GOALFLIGHT_STATE_DIR",
        "GOALFLIGHT_STEER_FILE",
        "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE",
        goalflight_acp_permits.ENV_PERMISSION_DIR,
        goalflight_acp_permits.ENV_PERMISSION_DIR_ALLOW,
    ]
    old_env = {key: os.environ.get(key) for key in keys}
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state = tmp / "state"
            dispatch = state / "dispatch"
            dispatch.mkdir(parents=True)

            os.environ["GOALFLIGHT_STATE_DIR"] = str(state)
            os.environ["GOALFLIGHT_STEER_FILE"] = str(dispatch / "worker.steer.jsonl")
            os.environ.pop("GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE", None)
            steer, steer_source = _resolve_steer_file(argparse.Namespace(steer_file=None), "worker")
            assert steer == dispatch / "worker.steer.jsonl"
            assert steer_source == "env:state_root"

            os.environ["GOALFLIGHT_STEER_FILE"] = str(tmp / "outside.steer.jsonl")
            ignored = False
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    _resolve_steer_file(argparse.Namespace(steer_file=None), "worker")
            except ValueError as exc:
                ignored = "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE" in str(exc)
            assert ignored, "external steer env path should require allow gate"
            warning = env_override_fields(err.getvalue(), "GOALFLIGHT_STEER_FILE")
            assert warning["action"] == "ignored"
            assert warning["reason"] == "outside_state_root"
            assert warning["source"] == str(tmp / "outside.steer.jsonl")

            os.environ["GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE"] = "1"
            steer, steer_source = _resolve_steer_file(argparse.Namespace(steer_file=None), "worker")
            assert steer == tmp / "outside.steer.jsonl"
            assert steer_source == "env:allow"

            os.environ[goalflight_acp_permits.ENV_PERMISSION_DIR] = str(dispatch / "perms")
            os.environ.pop(goalflight_acp_permits.ENV_PERMISSION_DIR_ALLOW, None)
            permission = goalflight_acp_permits.permission_dir(
                None,
                allowed_roots=[state, dispatch],
            )
            assert permission == dispatch / "perms"

            os.environ[goalflight_acp_permits.ENV_PERMISSION_DIR] = str(tmp / "outside-perms")
            ignored = False
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    goalflight_acp_permits.permission_dir(None, allowed_roots=[state, dispatch])
            except ValueError as exc:
                ignored = goalflight_acp_permits.ENV_PERMISSION_DIR_ALLOW in str(exc)
            assert ignored, "external permission env path should require allow gate"
            warning = env_override_fields(err.getvalue(), goalflight_acp_permits.ENV_PERMISSION_DIR)
            assert warning["action"] == "ignored"
            assert warning["reason"] == "outside_state_root"
            assert warning["source"] == str(tmp / "outside-perms")

            os.environ[goalflight_acp_permits.ENV_PERMISSION_DIR_ALLOW] = "1"
            permission = goalflight_acp_permits.permission_dir(
                None,
                allowed_roots=[state, dispatch],
            )
            assert permission == tmp / "outside-perms"
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def case_env_override_warning_shell_tokens_round_trip() -> None:
    source = "/tmp/goal flight/source with spaces"
    err = io.StringIO()
    goalflight_compat.env_override_warning(
        "GOALFLIGHT_TEST_SPACE_SOURCE",
        "ignored",
        "space_source_regression",
        source=source,
        extra={"pattern_count": 2},
        stream=err,
    )
    fields = env_override_fields(err.getvalue(), "GOALFLIGHT_TEST_SPACE_SOURCE")
    assert fields == {
        "env": "GOALFLIGHT_TEST_SPACE_SOURCE",
        "action": "ignored",
        "reason": "space_source_regression",
        "source": source,
        "pattern_count": "2",
    }


def case_test_mode_hooks_require_gate() -> None:
    keys = [
        "GOALFLIGHT_TEST_MODE",
        "GOALFLIGHT_TEST_PGROUP_CPU_PCT",
        "GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_FILE",
    ]
    old_env = {key: os.environ.get(key) for key in keys}
    try:
        os.environ["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "12.5"
        os.environ["GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_FILE"] = "/tmp/marker"
        os.environ.pop("GOALFLIGHT_TEST_MODE", None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert pgroup_cpu_pct(None) is None
            marker = goalflight_compat.allowed_env_override(
                "GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_FILE",
                "",
                test_mode=True,
            )
        assert marker is None
        warning = err.getvalue()
        assert "env=GOALFLIGHT_TEST_PGROUP_CPU_PCT action=ignored" in warning
        assert "env=GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_FILE action=ignored" in warning

        os.environ["GOALFLIGHT_TEST_MODE"] = "1"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert pgroup_cpu_pct(None) == 12.5
            marker = goalflight_compat.allowed_env_override(
                "GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_FILE",
                "",
                test_mode=True,
            )
        assert marker == "/tmp/marker"
        warning = err.getvalue()
        assert "env=GOALFLIGHT_TEST_PGROUP_CPU_PCT action=active" in warning
        assert "env=GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_FILE action=active" in warning
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    case_vendor_flood_idle_waits_for_quiet_backstop()
    case_dropped_frame_records_are_bounded()
    case_empty_oversized_head_assumes_request_for_reply()
    case_vendor_flood_cpu_busy_is_alive()
    case_standard_progress_resets_wedge_streak()
    case_permission_timeout_unblocks_wedge()
    case_progress_stall_wall_ignores_raw_vendor_noise()
    case_adapter_manifest_liveness_defaults()
    case_manifest_acp_command_defaults()
    case_json_rpc_stdout_filter()
    case_matrix_timeout_reaps_runner_process_group()
    case_permission_router_audit_bounded_and_truncated()
    case_normal_dispatch_hides_matrix_audit_surface()
    case_matrix_env_surfaces_bounded_audit()
    case_matrix_claude_defer_skips_remaining_cases()
    case_runner_raw_vendor_flood_hits_progress_stall_and_reaps()
    case_runner_progress_stall_detaches_by_default()
    case_detached_pidfile_entry_survives_ghost_cleanup()
    case_runner_progress_then_silent_wedges_and_reaps()
    case_runner_remote_long_reasoning_pause_survives_old_walls()
    case_runner_remote_dead_silent_turn_hits_remote_wall()
    case_runner_max_quiet_requires_confirmed_idle_cpu()
    case_runner_max_quiet_kills_confirmed_idle_cpu()
    case_runner_max_quiet_ignores_busy_cpu()
    case_runner_thought_stream_survives_progress_stall_wall()
    case_terminal_state_endturn_beats_tail_race_wedge()
    case_runner_blocked_none_completes()
    case_runner_blocked_substantive_cancels()
    case_runner_user_need_none_completes()
    case_runner_idle_silent_idle_timeout_reaps()
    case_runner_oversized_frame_dropped_then_completes()
    case_runner_oversized_request_gets_safe_reply()
    case_runner_oversized_request_late_id_gets_safe_reply()
    case_runner_oversized_no_newline_kills_worker()
    case_runner_goal_mode_progress_stall_backstop()
    case_runner_goal_mode_heartbeat_backstop()
    case_runner_tool_timeout_reaps()
    case_handshake_wedge_kills_before_respawn()
    case_pool_exhaustion_then_drain()
    case_env_ipc_paths_are_constrained()
    case_env_override_warning_shell_tokens_round_trip()
    case_test_mode_hooks_require_gate()
    print("OK: ACP SDK failure-mode tests pass")


if __name__ == "__main__":
    main()
