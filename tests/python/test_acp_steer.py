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


def _write_fake_codex_acp_manifest(
    directory: Path,
    *,
    remote_turn_silence_s: float | None = None,
) -> None:
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
                "status_contract": {
                    "terminal_states": ["complete", "failed"],
                    "stale_after_s": 60,
                    **(
                        {
                            "liveness_profile": "remote_api",
                            "remote_turn_silence_s": remote_turn_silence_s,
                        }
                        if remote_turn_silence_s is not None
                        else {}
                    ),
                },
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
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_ADAPTERS_DIR"] = str(tmp / "adapters")
    env["GOALFLIGHT_ALLOW_ADAPTERS_DIR_OVERRIDE"] = "1"
    env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "steer_multiturn"
    env["GOALFLIGHT_FAKE_ACP_TURN1_FILE"] = str(tmp / "turn1")
    env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "1.0"
    return env


def _wait_for(
    path: Path,
    timeout_s: float = 10.0,
    proc: subprocess.Popen[str] | None = None,
) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if path.exists():
            return
        if proc is not None and proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"worker exited before condition: {path}\n"
                f"rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.05)
    raise AssertionError(f"condition not met before timeout: {path}")


def _wait_for_worker_question(path: Path, timeout_s: float = 10.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                if entry.get("direction") == "worker_to_controller":
                    return entry
        time.sleep(0.05)
    raise AssertionError(f"worker USER-CONFIRM not routed before timeout: {path}")


def _run_confirmation_scenario(
    tmp: Path,
    *,
    scenario: str,
    dispatch_id: str,
    timeout_s: float = 0.2,
    live_matrix: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict, Path, Path]:
    _write_fake_codex_acp_manifest(tmp / "adapters")
    env = _env(tmp)
    env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = scenario
    env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "0.05"
    env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = str(timeout_s)
    if live_matrix:
        env["GOALFLIGHT_ACP_LIVE_MATRIX"] = "1"
    guarded = tmp / f"{dispatch_id}-guarded"
    env["GOALFLIGHT_FAKE_ACP_GUARDED_FILE"] = str(guarded)
    env["GOALFLIGHT_FAKE_ACP_PERMISSION_LOCATION"] = str(
        ROOT / ".goalflight-fake-guard-target"
    )
    status_path = tmp / f"{dispatch_id}.status.json"
    run = subprocess.run(
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
            "--foreground",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    status = json.loads(status_path.read_text(encoding="utf-8"))
    mailbox = Path(env["GOALFLIGHT_STATE_DIR"]) / "dispatch" / f"{dispatch_id}.steer.jsonl"
    return run, status, guarded, mailbox


def _run_answered_confirmation(
    tmp: Path,
    *,
    scenario: str,
    dispatch_id: str,
    decisions: list[str],
    delay_between_decisions_s: float = 0.0,
) -> tuple[subprocess.Popen[str], str, str, dict, Path, dict]:
    _write_fake_codex_acp_manifest(tmp / "adapters")
    env = _env(tmp)
    env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = scenario
    env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "0.5"
    env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = "5"
    guarded = tmp / f"{dispatch_id}-guarded"
    env["GOALFLIGHT_FAKE_ACP_GUARDED_FILE"] = str(guarded)
    status_path = tmp / f"{dispatch_id}.status.json"
    mailbox = Path(env["GOALFLIGHT_STATE_DIR"]) / "dispatch" / f"{dispatch_id}.steer.jsonl"
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
            "--foreground",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        question = _wait_for_worker_question(mailbox)
        for index, decision in enumerate(decisions):
            reply = subprocess.run(
                [
                    sys.executable,
                    str(DISPATCH),
                    "steer",
                    dispatch_id,
                    f"USER-CONFIRM-ANSWER: {question['question_id']} {decision}",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            assert reply.returncode == 0, reply.stdout + reply.stderr
            if index + 1 < len(decisions) and delay_between_decisions_s > 0:
                time.sleep(delay_between_decisions_s)
        stdout, stderr = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=10)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    return proc, stdout, stderr, status, guarded, question


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
                "--foreground",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for(Path(env["GOALFLIGHT_FAKE_ACP_TURN1_FILE"]), proc=proc)
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
        env["GOALFLIGHT_TEST_MODE"] = "1"
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
                "--foreground",
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
                "--foreground",
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


def case_user_confirm_midrun_routes_without_cancelling() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(
            tmp / "adapters",
            remote_turn_silence_s=0.01,
        )
        env = _env(tmp)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "user_confirm_continue"
        env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "0.5"
        env["GOALFLIGHT_HEARTBEAT_INTERVAL"] = "0.05"
        env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = "5"
        guarded = tmp / "guarded-action"
        env["GOALFLIGHT_FAKE_ACP_GUARDED_FILE"] = str(guarded)
        dispatch_id = "acp-user-confirm-route"
        status_path = tmp / "status.json"
        mailbox = Path(env["GOALFLIGHT_STATE_DIR"]) / "dispatch" / f"{dispatch_id}.steer.jsonl"
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
                "--foreground",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            question = _wait_for_worker_question(mailbox)
            assert proc.poll() is None, "USER-CONFIRM cancelled the live dispatch"
            assert question["dispatch_id"] == dispatch_id, question
            assert question["kind"] == "user_confirm", question
            assert "guarded sentinel" in question["text"], question
            assert question["context"]["guarded_action_authorized"] is False, question
            reply = subprocess.run(
                [
                    sys.executable,
                    str(DISPATCH),
                    "steer",
                    dispatch_id,
                    (
                        f"USER-CONFIRM-ANSWER: {question['question_id']} yes "
                        "authorize this guarded action"
                    ),
                ],
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

        assert reply.returncode == 0, reply.stdout + reply.stderr
        assert proc.returncode == 0, f"rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        markers = status.get("markers") or {}
        assert status["state"] == "complete", status
        assert status["ok"] is True, status
        assert status.get("steer_delivered_seqs") == [2], status
        assert 1 not in (status.get("steer_delivered_seqs") or []), status
        assert markers.get("USER-CONFIRM") == [
            "authorize guarded sentinel write? [Y/N]"
        ], status
        assert markers.get("RESULT") == [
            "draft=preserved-before-question",
            "guarded_action_taken=true",
        ], status
        assert "RESULT: draft=preserved-before-question" in status["result_text"], status
        assert guarded.read_text(encoding="utf-8") == "authorized\n"
        resolved = status.get("user_confirm_resolved") or []
        assert resolved and resolved[0]["controller_decision"] == "yes", status
        assert resolved[0]["guarded_action_authorized"] is True, status

        controller_inbox = Path(env["GOALFLIGHT_MESSAGES_DIR"]) / f"{dispatch_id}.jsonl"
        envelopes = [
            json.loads(line)
            for line in controller_inbox.read_text(encoding="utf-8").splitlines()
        ]
        assert envelopes[0]["dispatch_id"] == dispatch_id, envelopes
        assert envelopes[0]["type"] == "user_confirm", envelopes
        assert envelopes[0]["payload"]["question_id"] == question["question_id"], envelopes


def case_uncorrelated_user_confirm_yes_is_not_authorization() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "user_confirm_continue"
        env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "0.5"
        # The unrelated/unknown reply is already pending when the absolute
        # question deadline expires. It must not postpone fail-closed denial.
        env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = "0.2"
        guarded = tmp / "guarded-uncorrelated-action"
        env["GOALFLIGHT_FAKE_ACP_GUARDED_FILE"] = str(guarded)
        dispatch_id = "acp-user-confirm-wrong-id"
        status_path = tmp / "status.json"
        mailbox = Path(env["GOALFLIGHT_STATE_DIR"]) / "dispatch" / f"{dispatch_id}.steer.jsonl"
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
                "--foreground",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            question = _wait_for_worker_question(mailbox)
            reply = subprocess.run(
                [
                    sys.executable,
                    str(DISPATCH),
                    "steer",
                    dispatch_id,
                    "USER-CONFIRM-ANSWER: wrong-question-id yes",
                ],
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

        assert reply.returncode == 0, reply.stdout + reply.stderr
        assert proc.returncode != 0, f"rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert status["state"] == "blocked_user_confirm_denied", status
        assert status["ok"] is False, status
        assert status.get("user_confirm_rejected_reply_seqs") == [2], status
        assert status.get("user_confirm_timeout_count") == 1, status
        resolved = status.get("user_confirm_resolved") or []
        assert resolved and resolved[0]["question_id"] == question["question_id"], status
        assert resolved[0]["controller_decision"] == "no", status
        assert "guarded_action_taken=false" in status["result_text"], status
        assert not guarded.exists(), "uncorrelated yes authorized the guarded action"


def case_same_turn_guarded_action_is_denied_without_answer() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "user_confirm_same_turn_guard"
        env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = "0.2"
        guarded = tmp / "same-turn-guarded-action"
        env["GOALFLIGHT_FAKE_ACP_GUARDED_FILE"] = str(guarded)
        env["GOALFLIGHT_FAKE_ACP_PERMISSION_LOCATION"] = str(
            ROOT / ".goalflight-fake-guard-target"
        )
        status_path = tmp / "status.json"
        run = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--shape",
                "acp",
                "--agent",
                "codex-acp",
                "--dispatch-id",
                "acp-user-confirm-same-turn-guard",
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
                "--foreground",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert run.returncode != 0, (run.stdout, run.stderr, status)
        assert status["state"] == "blocked_user_confirm_denied", status
        assert status["ok"] is False, status
        assert "same_turn_guarded_action_taken=false" in status["result_text"], status
        assert not guarded.exists(), "same-turn action ran before confirmation"


def case_user_confirm_timeout_is_fail_closed_then_continues() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "user_confirm_continue"
        env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "0.05"
        env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = "0.2"
        guarded = tmp / "guarded-timeout-action"
        env["GOALFLIGHT_FAKE_ACP_GUARDED_FILE"] = str(guarded)
        dispatch_id = "acp-user-confirm-timeout"
        status_path = tmp / "status.json"
        run = subprocess.run(
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
                "--foreground",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert run.returncode != 0, (run.stdout, run.stderr, status)
        assert status["state"] == "blocked_user_confirm_denied", status
        assert status["ok"] is False, status
        assert status.get("user_confirm_timeout_count") == 1, status
        assert not guarded.exists(), "timeout became implicit approval"
        resolved = status.get("user_confirm_resolved") or []
        assert resolved and resolved[0]["controller_decision"] == "no", status
        assert "guarded_action_taken=false" in status["result_text"], status


def case_split_user_confirm_routes_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, status, guarded, mailbox = _run_confirmation_scenario(
            Path(td),
            scenario="user_confirm_split_marker",
            dispatch_id="acp-user-confirm-split",
        )
        assert run.returncode != 0, (run.stdout, run.stderr, status)
        assert status["state"] == "blocked_user_confirm_denied", status
        assert status["ok"] is False, status
        rows = [
            json.loads(line)
            for line in mailbox.read_text(encoding="utf-8").splitlines()
        ]
        questions = [
            row for row in rows if row.get("direction") == "worker_to_controller"
        ]
        assert len(questions) == 1, rows
        assert not guarded.exists(), "split marker timeout authorized guarded action"


def case_fenced_user_confirm_example_does_not_activate_guard() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, status, guarded, mailbox = _run_confirmation_scenario(
            Path(td),
            scenario="user_confirm_fenced_example",
            dispatch_id="acp-user-confirm-fenced",
        )
        assert run.returncode == 0, (run.stdout, run.stderr, status)
        assert status["state"] == "complete", status
        assert guarded.read_text(encoding="utf-8") == "authorized\n"
        assert not (status.get("user_confirm_pending") or []), status
        if mailbox.exists():
            rows = [
                json.loads(line)
                for line in mailbox.read_text(encoding="utf-8").splitlines()
            ]
            assert not [
                row for row in rows if row.get("direction") == "worker_to_controller"
            ], rows


def case_kindless_permission_is_denied_while_confirm_pending() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, status, guarded, _mailbox = _run_confirmation_scenario(
            Path(td),
            scenario="user_confirm_kindless_guard",
            dispatch_id="acp-user-confirm-kindless",
            live_matrix=True,
        )
        assert run.returncode != 0, (run.stdout, run.stderr, status)
        assert status["state"] == "blocked_user_confirm_denied", status
        assert status["ok"] is False, status
        assert not guarded.exists(), "kindless request bypassed pending confirmation"
        decisions = status.get("permission_router_decisions") or []
        assert any(
            decision.get("decision") == "deny"
            and decision.get("reason") == "user_confirm_pending"
            and not decision.get("kind")
            for decision in decisions
        ), decisions


def case_repeated_question_after_timeout_blocks_with_partial_work() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, status, guarded, mailbox = _run_confirmation_scenario(
            Path(td),
            scenario="user_confirm_repeat_after_no",
            dispatch_id="acp-user-confirm-repeat",
        )
        assert run.returncode != 0, (run.stdout, run.stderr, status)
        assert status["state"] == "blocked_user_confirm", status
        assert status["error"]["message"] == "user_confirm_unresolved_after_denial", status
        assert "independent_work=preserved_after_denial" in status["result_text"], status
        assert not guarded.exists(), "repeated question became implicit approval"
        rows = [
            json.loads(line)
            for line in mailbox.read_text(encoding="utf-8").splitlines()
        ]
        questions = [
            row for row in rows if row.get("direction") == "worker_to_controller"
        ]
        assert len(questions) == 2, rows


def case_hard_blocker_preserves_same_chunk_confirm_evidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, status, _guarded, mailbox = _run_confirmation_scenario(
            Path(td),
            scenario="user_confirm_then_blocked",
            dispatch_id="acp-user-confirm-hard-block",
        )
        assert run.returncode != 0, (run.stdout, run.stderr, status)
        assert status["state"] == "blocked", status
        assert "RESULT: partial=survives" in status["result_text"], status
        pending = status.get("user_confirm_pending") or []
        assert len(pending) == 1, status
        rows = [
            json.loads(line)
            for line in mailbox.read_text(encoding="utf-8").splitlines()
        ]
        questions = [
            row for row in rows if row.get("direction") == "worker_to_controller"
        ]
        assert len(questions) == 1, rows
        assert questions[0]["question_id"] == pending[0]["question_id"], (rows, status)


def case_restart_does_not_reuse_question_id_or_accept_stale_yes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dispatch_id = "acp-user-confirm-restart"
        first_run, first_status, _first_guarded, _mailbox = _run_confirmation_scenario(
            tmp,
            scenario="user_confirm_then_blocked",
            dispatch_id=dispatch_id,
        )
        assert first_run.returncode != 0, (first_run.stdout, first_run.stderr, first_status)
        first_pending = first_status.get("user_confirm_pending") or []
        assert len(first_pending) == 1, first_status
        old_question_id = first_pending[0]["question_id"]

        stale_reply = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "steer",
                dispatch_id,
                f"USER-CONFIRM-ANSWER: {old_question_id} yes",
            ],
            cwd=ROOT,
            env=_env(tmp),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        assert stale_reply.returncode == 0, stale_reply.stdout + stale_reply.stderr

        second_run, second_status, guarded, _mailbox = _run_confirmation_scenario(
            tmp,
            scenario="user_confirm_continue",
            dispatch_id=dispatch_id,
        )
        assert second_run.returncode != 0, (
            second_run.stdout,
            second_run.stderr,
            second_status,
        )
        assert not guarded.exists(), "a stale yes authorized the restarted run"
        resolved = second_status.get("user_confirm_resolved") or []
        old = [item for item in resolved if item["question_id"] == old_question_id]
        new = [item for item in resolved if item["question_id"] != old_question_id]
        assert len(old) == 1 and old[0]["controller_decision"] == "no", second_status
        assert old[0]["resolution_reason"] == "runner_restarted", second_status
        assert len(new) == 1 and new[0]["controller_decision"] == "no", second_status


def case_conflicting_user_confirm_answers_are_deny_biased() -> None:
    with tempfile.TemporaryDirectory() as td:
        proc, stdout, stderr, status, guarded, _question = _run_answered_confirmation(
            Path(td),
            scenario="user_confirm_continue",
            dispatch_id="acp-user-confirm-conflict",
            decisions=["yes", "no"],
            delay_between_decisions_s=2.0,
        )
        assert proc.returncode != 0, (stdout, stderr, status)
        assert status["state"] == "blocked_user_confirm_denied", status
        assert status["ok"] is False, status
        assert not guarded.exists(), "yes won over a conflicting no"
        resolved = status.get("user_confirm_resolved") or []
        assert len(resolved) == 1, status
        assert resolved[0]["controller_decision"] == "no", status
        assert resolved[0]["guarded_action_authorized"] is False, status
        assert resolved[0]["decision_conflict"] is True, status
        assert "guarded_action_taken=false" in status["result_text"], status


def case_correlated_worker_marker_yes_authorizes_only_its_action() -> None:
    with tempfile.TemporaryDirectory() as td:
        proc, stdout, stderr, status, guarded, _question = _run_answered_confirmation(
            Path(td),
            scenario="user_confirm_continue",
            dispatch_id="acp-user-confirm-yes",
            decisions=["yes"],
        )
        assert proc.returncode == 0, (stdout, stderr, status)
        assert status["state"] == "complete", status
        assert status["ok"] is True, status
        assert guarded.read_text(encoding="utf-8") == "authorized\n"
        resolved = status.get("user_confirm_resolved") or []
        assert len(resolved) == 1, status
        assert resolved[0]["controller_decision"] == "yes", status
        assert resolved[0]["guarded_action_authorized"] is True, status
        assert status["had_denied_action"] is False, status


def case_permission_escalation_yes_is_acknowledgment_not_authorization() -> None:
    with tempfile.TemporaryDirectory() as td:
        proc, stdout, stderr, status, guarded, _question = _run_answered_confirmation(
            Path(td),
            scenario="permission_escalate_continue",
            dispatch_id="acp-permission-confirm-yes",
            decisions=["yes"],
        )
        assert proc.returncode != 0, (stdout, stderr, status)
        assert status["state"] == "blocked_permission_denied", status
        assert status["ok"] is False, status
        assert not guarded.exists(), "permission-escalation yes authorized an alternate route"
        resolved = status.get("user_confirm_resolved") or []
        assert len(resolved) == 1, status
        assert resolved[0]["origin"] == "permission_escalation", status
        assert resolved[0]["controller_decision"] == "yes", status
        assert resolved[0]["guarded_action_authorized"] is False, status
        assert "guarded_action_taken=false" in status["result_text"], status


def case_permission_escalation_records_correlated_no_and_nonclean_terminal() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, status, guarded, _mailbox = _run_confirmation_scenario(
            Path(td),
            scenario="permission_escalate_continue",
            dispatch_id="acp-permission-confirm-timeout",
        )
        assert run.returncode != 0, (run.stdout, run.stderr, status)
        assert status["state"] == "blocked_permission_denied", status
        assert status["ok"] is False, status
        escalations = status.get("permission_pending") or []
        assert len(escalations) == 1, status
        assert escalations[0]["decision"] == "escalate", status
        assert escalations[0]["tool_call_id"] == "perm-continue", status
        assert escalations[0]["title"] == "Write guarded sentinel", status
        resolved = status.get("user_confirm_resolved") or []
        assert len(resolved) == 1, status
        assert resolved[0]["origin"] == "permission_escalation", status
        assert resolved[0]["controller_decision"] == "no", status
        assert resolved[0]["guarded_action_authorized"] is False, status
        assert resolved[0]["permission"]["tool_call_id"] == "perm-continue", status
        assert status["had_denied_action"] is True, status
        assert status["had_user_confirm_denial"] is True, status
        assert not guarded.exists(), "permission router authorized an outside-cwd write"
        assert "RESULT: permission=denied safe_work=preserved" in status["result_text"], status


def main() -> None:
    case_acp_mailbox_steer_delivered_at_next_turn_and_acked()
    case_mid_turn_steer_does_not_extend_wedge_deadline()
    case_nonterminal_steer_turn_continues_to_real_terminal()
    case_user_confirm_midrun_routes_without_cancelling()
    case_uncorrelated_user_confirm_yes_is_not_authorization()
    case_same_turn_guarded_action_is_denied_without_answer()
    case_user_confirm_timeout_is_fail_closed_then_continues()
    case_split_user_confirm_routes_once()
    case_fenced_user_confirm_example_does_not_activate_guard()
    case_kindless_permission_is_denied_while_confirm_pending()
    case_repeated_question_after_timeout_blocks_with_partial_work()
    case_hard_blocker_preserves_same_chunk_confirm_evidence()
    case_restart_does_not_reuse_question_id_or_accept_stale_yes()
    case_conflicting_user_confirm_answers_are_deny_biased()
    case_correlated_worker_marker_yes_authorizes_only_its_action()
    case_permission_escalation_yes_is_acknowledgment_not_authorization()
    case_permission_escalation_records_correlated_no_and_nonclean_terminal()
    print("OK: ACP steer tests pass")


if __name__ == "__main__":
    main()
