#!/usr/bin/env python3
"""Hermetic tests for ACP between-turn steer delivery."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("uses POSIX subprocess liveness for ACP fake worker")

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
FAKE = ROOT / "tests" / "fixtures" / "acp_fake_agent.py"


def _write_fake_codex_acp_manifest(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "codex-acp.json").write_text(
        json.dumps(
            {
                "schema": "goalflight.agent-adapter.v1",
                "support": {
                    "controller": {"capability": "supported", "fallback": "worker_only"},
                    "worker": {
                        "capability": "supported",
                        "transport": ["acp"],
                        "fallback": "tail_file",
                    },
                },
                "local_readiness_state": {
                    "controller": "probe_required",
                    "worker": "probe_required",
                    "last_probe_ids": ["python-version"],
                },
                "live_gate": {"function": "validate_adapter_gate", "default": "deny"},
                "status_contract": {"terminal_states": ["complete", "failed"], "stale_after_s": 60},
                "permission_surface": {
                    "plugin_sandbox": {},
                    "auto_approve_detection": {"strict_fail": True},
                },
                "discovery": {
                    "probes": [
                        {
                            "id": "python-version",
                            "argv": [sys.executable, "--version"],
                            "safe_for_setup": True,
                            "network": False,
                            "model_consuming": False,
                        }
                    ]
                },
                "invocation": {
                    "exec": {
                        "kind": "acp",
                        "binary": sys.executable,
                        "args": [str(FAKE)],
                        "arg_policy": {"forbidden_args": []},
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_ADAPTERS_DIR"] = str(tmp / "adapters")
    env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "steer_multiturn"
    env["GOALFLIGHT_FAKE_ACP_TURN1_FILE"] = str(tmp / "turn1")
    env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "1.0"
    return env


def _wait_for(path: Path, timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"condition not met before timeout: {path}")


def case_acp_mailbox_steer_delivered_at_next_turn_and_acked() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp)
        dispatch_id = "acp-between-turn-steer"
        status_path = tmp / "status.json"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(DISPATCH),
                "--shape",
                "acp",
                "--agent",
                "codex-acp",
                "--dispatch-id",
                dispatch_id,
                "--cwd",
                str(ROOT),
                "--prompt",
                "initial task",
                "--status-json",
                str(status_path),
                "--poll-secs",
                "0.05",
                "--max-idle-secs",
                "10",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for(Path(env["GOALFLIGHT_FAKE_ACP_TURN1_FILE"]))
            steer = subprocess.run(
                [sys.executable, str(DISPATCH), "steer", dispatch_id, "redirect now"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            stdout, stderr = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=10)

        assert steer.returncode == 0, steer.stdout + steer.stderr
        assert "steer appended:" in steer.stdout, steer.stdout
        assert proc.returncode == 0, f"rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
        assert "connection already running a prompt" not in stderr, stderr

        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert status["state"] == "complete", status
        assert status.get("steer_delivered_seqs") == [1], status
        assert status.get("steer_acked_seqs") == [1], status
        assert "STEER-ACK" in (status.get("markers") or {}), status
        assert (status.get("markers") or {}).get("STEER-ACK") == ["1"], status

        listed = subprocess.run(
            [sys.executable, str(DISPATCH), "steer", dispatch_id, "--list"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        assert listed.returncode == 0, listed.stdout + listed.stderr
        assert "\ttrue\tredirect now" in listed.stdout, listed.stdout


def case_mid_turn_steer_does_not_extend_wedge_deadline() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "progress_then_silent"
        env["GOALFLIGHT_FAKE_ACP_PROGRESS_FILE"] = str(tmp / "progress")
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        dispatch_id = "acp-midturn-steer-wedges"
        status_path = tmp / "status.json"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(DISPATCH),
                "--shape",
                "acp",
                "--agent",
                "codex-acp",
                "--dispatch-id",
                dispatch_id,
                "--cwd",
                str(ROOT),
                "--prompt",
                "initial task",
                "--status-json",
                str(status_path),
                "--poll-secs",
                "0.05",
                "--max-idle-secs",
                "0",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for(Path(env["GOALFLIGHT_FAKE_ACP_PROGRESS_FILE"]))
            steer = subprocess.run(
                [sys.executable, str(DISPATCH), "steer", dispatch_id, "redirect now"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            stdout, stderr = proc.communicate(timeout=6)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=10)

        assert steer.returncode == 0, steer.stdout + steer.stderr
        assert proc.returncode != 0, f"rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert status["state"] == "wedged", status
        assert status["error"]["message"] == "wedged_by_heartbeat", status
        assert status.get("steer_acked_seqs") == [], status
        assert status.get("wedge_progress_seen", 0) >= 1, status


def case_nonterminal_steer_turn_continues_to_real_terminal() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "steer_nonterminal_then_complete"
        dispatch_id = "acp-nonterminal-steer-continues"
        status_path = tmp / "status.json"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(DISPATCH),
                "--shape",
                "acp",
                "--agent",
                "codex-acp",
                "--dispatch-id",
                dispatch_id,
                "--cwd",
                str(ROOT),
                "--prompt",
                "initial task",
                "--status-json",
                str(status_path),
                "--poll-secs",
                "0.05",
                "--max-idle-secs",
                "10",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for(Path(env["GOALFLIGHT_FAKE_ACP_TURN1_FILE"]))
            steer = subprocess.run(
                [sys.executable, str(DISPATCH), "steer", dispatch_id, "redirect now"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            stdout, stderr = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=10)

        assert steer.returncode == 0, steer.stdout + steer.stderr
        assert proc.returncode == 0, f"rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        markers = status.get("markers") or {}
        assert status["state"] == "complete", status
        assert status.get("steer_delivered_seqs") == [1], status
        assert status.get("steer_acked_seqs") == [1], status
        assert markers.get("STEER-ACK") == ["1"], status
        assert markers.get("COMPLETE") == ["continued after steer"], status
        assert "STATUS: steer accepted; continuing" in (status.get("text_excerpt") or ""), status


def main() -> None:
    case_acp_mailbox_steer_delivered_at_next_turn_and_acked()
    case_mid_turn_steer_does_not_extend_wedge_deadline()
    case_nonterminal_steer_turn_continues_to_real_terminal()
    print("OK: ACP steer tests pass")


if __name__ == "__main__":
    main()
