#!/usr/bin/env python3
"""Hermetic tests for ACP between-turn steer delivery."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("uses POSIX subprocess liveness for ACP fake worker")

import contextlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
FAKE = ROOT / "tests" / "fixtures" / "acp_fake_agent.py"
sys.path.insert(0, str(ROOT / "scripts"))
import goalflight_steer_mailbox  # noqa: E402
from goalflight_liveness import active_monotonic  # noqa: E402


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
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_ADAPTERS_DIR"] = str(tmp / "adapters")
    env["GOALFLIGHT_ALLOW_ADAPTERS_DIR_OVERRIDE"] = "1"
    env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "steer_multiturn"
    env["GOALFLIGHT_FAKE_ACP_TURN1_FILE"] = str(tmp / "turn1")
    env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "1.0"
    return env


def _project(tmp: Path) -> Path:
    project = tmp / "project"
    project.mkdir(exist_ok=True)
    return project


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


def _wait_for_active_seconds(duration_s: float) -> None:
    deadline = active_monotonic() + duration_s
    while active_monotonic() < deadline:
        time.sleep(min(0.01, max(0.0, deadline - active_monotonic())))


def _wait_for_worker_questions(
    path: Path,
    *,
    count: int = 1,
    exclude_question_ids: set[str] | None = None,
    timeout_s: float = 10.0,
) -> list[dict]:
    excluded = exclude_question_ids or set()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if path.exists():
            questions = [
                entry
                for entry in (
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                )
                if entry.get("direction") == "worker_to_controller"
                and entry.get("question_id") not in excluded
            ]
            if len(questions) >= count:
                return questions[:count]
        time.sleep(0.05)
    raise AssertionError(f"worker USER-CONFIRM not routed before timeout: {path}")


def _wait_for_worker_question(
    path: Path,
    timeout_s: float = 10.0,
    *,
    exclude_question_ids: set[str] | None = None,
) -> dict:
    return _wait_for_worker_questions(
        path,
        count=1,
        exclude_question_ids=exclude_question_ids,
        timeout_s=timeout_s,
    )[0]


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
        _project(tmp) / ".goalflight-fake-guard-target"
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
            str(_project(tmp)),
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
    exclude_question_ids: set[str] | None = None,
    extra_env: dict[str, str] | None = None,
    steer_messages: list[str] | None = None,
    pre_question_messages: list[str] | None = None,
    delay_before_messages_s: float = 0.0,
    poll_s: float = 0.05,
) -> tuple[subprocess.Popen[str], str, str, dict, Path, dict]:
    _write_fake_codex_acp_manifest(tmp / "adapters")
    env = _env(tmp)
    env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = scenario
    env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "0.5"
    env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = "5"
    env.update(extra_env or {})
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
            str(_project(tmp)),
            "--prompt",
            "initial task",
            "--status-json",
            str(status_path),
            "--poll-secs",
            str(poll_s),
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
        for message in pre_question_messages or []:
            ledger_path = (
                Path(env["GOALFLIGHT_STATE_DIR"])
                / "runs.d"
                / f"{dispatch_id}.json"
            )
            deadline = time.monotonic() + 10.0
            while True:
                ledger_record = None
                if ledger_path.exists():
                    with contextlib.suppress(OSError, json.JSONDecodeError):
                        ledger_record = json.loads(ledger_path.read_text())
                if (
                    isinstance(ledger_record, dict)
                    and ledger_record.get("state") == "running"
                    and ledger_record.get("worker_pid")
                ):
                    break
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=1)
                    raise AssertionError(
                        f"runner exited before ledger registration: {stdout}\n{stderr}"
                    )
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        f"runner did not publish its worker before timeout: {dispatch_id}"
                    )
                time.sleep(0.01)
            reply = subprocess.run(
                [sys.executable, str(DISPATCH), "steer", dispatch_id, message],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            assert reply.returncode == 0, reply.stdout + reply.stderr
        question = _wait_for_worker_question(
            mailbox,
            exclude_question_ids=exclude_question_ids,
        )
        messages = (
            [
                message.format(question_id=question["question_id"])
                for message in steer_messages
            ]
            if steer_messages is not None
            else [
                f"USER-CONFIRM-ANSWER: {question['question_id']} {decision}"
                for decision in decisions
            ]
        )
        if delay_before_messages_s > 0:
            _wait_for_active_seconds(delay_before_messages_s)
        for index, message in enumerate(messages):
            reply = subprocess.run(
                [
                    sys.executable,
                    str(DISPATCH),
                    "steer",
                    dispatch_id,
                    message,
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            assert reply.returncode == 0, reply.stdout + reply.stderr
            if index + 1 < len(messages) and delay_between_decisions_s > 0:
                _wait_for_active_seconds(delay_between_decisions_s)
        stdout, stderr = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=10)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    return proc, stdout, stderr, status, guarded, question


def _answer_confirmation(
    *,
    dispatch_id: str,
    question_id: str,
    decision: str,
    cwd: Path,
    env: dict[str, str],
) -> None:
    reply = subprocess.run(
        [
            sys.executable,
            str(DISPATCH),
            "steer",
            dispatch_id,
            f"USER-CONFIRM-ANSWER: {question_id} {decision}",
        ],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert reply.returncode == 0, reply.stdout + reply.stderr


def _start_confirmation_runner(
    tmp: Path,
    *,
    scenario: str,
    dispatch_id: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[str], dict[str, str], Path, Path, Path]:
    _write_fake_codex_acp_manifest(tmp / "adapters")
    env = _env(tmp)
    env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = scenario
    env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = "5"
    env.update(extra_env or {})
    guarded = tmp / f"{dispatch_id}-guarded"
    env["GOALFLIGHT_FAKE_ACP_GUARDED_FILE"] = str(guarded)
    status_path = tmp / f"{dispatch_id}.status.json"
    mailbox = (
        Path(env["GOALFLIGHT_STATE_DIR"])
        / "dispatch"
        / f"{dispatch_id}.steer.jsonl"
    )
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
            str(_project(tmp)),
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
    return proc, env, guarded, status_path, mailbox


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
                str(_project(tmp)),
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
                str(_project(tmp)),
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
                str(_project(tmp)),
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


def case_user_confirm_midrun_yes_records_consent_without_authorizing_action() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "user_confirm_continue"
        env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "0.5"
        env["GOALFLIGHT_HEARTBEAT_INTERVAL"] = "0.05"
        env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = "5"
        guarded = tmp / "guarded-action"
        env["GOALFLIGHT_FAKE_ACP_GUARDED_FILE"] = str(guarded)
        env["GOALFLIGHT_FAKE_ACP_REQUEST_GUARDED_PERMISSION"] = "1"
        env["GOALFLIGHT_ACP_LIVE_MATRIX"] = "1"
        env["GOALFLIGHT_FAKE_ACP_PERMISSION_LOCATION"] = str(
            _project(tmp) / ".goalflight-fake-guard-target"
        )
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
                str(_project(tmp)),
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
            "marker_redirect_seen=true",
            "guarded_action_taken=false",
        ], status
        assert "RESULT: draft=preserved-before-question" in status["result_text"], status
        assert not guarded.exists(), status
        resolved = status.get("user_confirm_resolved") or []
        assert resolved and resolved[0]["controller_decision"] == "yes", status
        assert resolved[0]["guarded_action_authorized"] is False, status
        assert any(
            decision.get("reason") == "user_confirm_denied"
            and decision.get("tool_call_id") == "guarded-after-confirm"
            for decision in (status.get("permission_router_decisions") or [])
        ), status

        controller_inbox = Path(env["GOALFLIGHT_MESSAGES_DIR"]) / f"{dispatch_id}.jsonl"
        envelopes = [
            json.loads(line)
            for line in controller_inbox.read_text(encoding="utf-8").splitlines()
        ]
        assert [envelope["type"] for envelope in envelopes] == [
            "user_confirm",
            "controller-notice",
            "result",
        ], envelopes
        assert envelopes[0]["dispatch_id"] == dispatch_id, envelopes
        assert envelopes[0]["payload"]["question_id"] == question["question_id"], envelopes
        result_envelope = envelopes[2]
        assert result_envelope["source"]["transport"] == "journal", envelopes
        journals = list((tmp / "journal").rglob("state-journal.sqlite3"))
        assert len(journals) == 1, journals
        with sqlite3.connect(journals[0]) as connection:
            outbox = connection.execute(
                """SELECT event_uuid, event_type, projected_at
                   FROM terminal_outbox WHERE recipient = ?""",
                (dispatch_id,),
            ).fetchall()
        assert len(outbox) == 1, outbox
        assert outbox[0][0] == result_envelope["id"], (outbox, envelopes)
        assert outbox[0][1] == "result" and outbox[0][2] is not None, outbox


def case_midturn_mailbox_yes_is_reconciled_before_timeout_denial() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(
            tmp / "adapters",
            remote_turn_silence_s=0.1,
        )
        env = _env(tmp)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "user_confirm_hang_after_marker"
        env["GOALFLIGHT_FAKE_ACP_HANG_S"] = "30"
        env["GOALFLIGHT_HEARTBEAT_INTERVAL"] = "0.05"
        env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = "2"
        dispatch_id = "acp-user-confirm-midturn-yes"
        status_path = tmp / "status.json"
        mailbox = (
            Path(env["GOALFLIGHT_STATE_DIR"])
            / "dispatch"
            / f"{dispatch_id}.steer.jsonl"
        )
        env["GOALFLIGHT_STEER_FILE"] = str(mailbox)
        proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_acp_run.py"),
                "--agent",
                "codex-acp",
                "--dispatch-id",
                dispatch_id,
                "--cwd",
                str(_project(tmp)),
                "--prompt-text",
                "initial task",
                "--status-json",
                str(status_path),
                "--heartbeat-interval",
                "0.05",
                "--wedge-samples",
                "2",
                "--progress-stall-s",
                "0.1",
                "--idle-timeout",
                "0.1",
                "--max-quiet-s",
                "10",
                "--max-tool-s",
                "10",
                "--liveness-profile",
                "remote_api",
                "--remote-turn-silence-s",
                "0.1",
                "--remote-turn-cancel-grace-s",
                "0.1",
                "--user-confirm-timeout-s",
                "2",
                "--json",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            question = _wait_for_worker_question(mailbox, timeout_s=10)
            reply = goalflight_steer_mailbox.append_steer_entry(
                mailbox,
                f"USER-CONFIRM-ANSWER: {question['question_id']} yes",
                dispatch_id=dispatch_id,
                kind="user_confirm_reply",
                reply_to=question["question_id"],
                decision="yes",
            )
            stdout, stderr = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.communicate(timeout=10)

        status = json.loads(status_path.read_text(encoding="utf-8"))
        rows = [
            json.loads(line)
            for line in mailbox.read_text(encoding="utf-8").splitlines()
        ]
        resolved = status.get("user_confirm_resolved") or []
        assert (
            reply["reply_to"] == question["question_id"]
            and status["ok"] is False
            and status["state"] == "remote_turn_silence"
            and status.get("user_confirm_pending") == []
            and status.get("user_confirm_timeout_count") is None
            and status.get("user_confirm_overdue") is False
            and len(resolved) == 1
            and resolved[0]["controller_decision"] == "yes"
            and resolved[0]["guarded_action_authorized"] is False
            and bool(resolved[0].get("arbitration_closed_at"))
            and not any(
                row.get("reply_to") == question["question_id"]
                and row.get("decision") == "no"
                for row in rows
            )
        ), (proc.returncode, stdout, stderr, status, rows)


def case_post_deadline_yes_before_delayed_read_is_denied() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc, stdout, stderr, status, guarded, question = _run_answered_confirmation(
            tmp,
            scenario="user_confirm_continue",
            dispatch_id="acp-user-confirm-late-before-read",
            decisions=["yes"],
            delay_before_messages_s=0.4,
            poll_s=1.0,
            extra_env={
                "GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP": "1.5",
                "GOALFLIGHT_USER_CONFIRM_TIMEOUT_S": "0.2",
            },
        )
        resolved = status.get("user_confirm_resolved") or []
        rejected = status.get("user_confirm_rejected_reply_seqs") or []
        mailbox = (
            Path(_env(tmp)["GOALFLIGHT_STATE_DIR"])
            / "dispatch"
            / "acp-user-confirm-late-before-read.steer.jsonl"
        )
        rows = goalflight_steer_mailbox.read_steer_entries(mailbox)
        late_reply = [
            row
            for row in rows
            if row.get("text")
            == f"USER-CONFIRM-ANSWER: {question['question_id']} yes"
        ]
        assert (
            status["state"] == "blocked_user_confirm_denied"
            and not guarded.exists()
            and len(resolved) == 1
            and resolved[0]["question_id"] == question["question_id"]
            and resolved[0]["controller_decision"] == "no"
            and resolved[0]["guarded_action_authorized"] is False
            and len(late_reply) == 1
            and set(rejected) == {late_reply[0]["seq"]}
        ), (stdout, stderr, status, rows)


def case_uncorrelated_user_confirm_yes_is_not_authorization() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "user_confirm_continue"
        env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "0.5"
        # The unrelated/unknown reply must remain non-authorizing whether it
        # lands just before or just after the absolute fail-closed deadline.
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
                str(_project(tmp)),
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
        wrong_reply_seq = next(
            int(entry["seq"])
            for entry in (
                json.loads(line)
                for line in mailbox.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if entry.get("text") == "USER-CONFIRM-ANSWER: wrong-question-id yes"
        )
        assert status["state"] == "blocked_user_confirm_denied", status
        assert status["ok"] is False, status
        assert status.get("user_confirm_rejected_reply_seqs") == [wrong_reply_seq], status
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
            _project(tmp) / ".goalflight-fake-guard-target"
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
                str(_project(tmp)),
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


def case_prefixed_same_turn_guarded_action_is_denied_without_answer() -> None:
    """The sigiled marker must engage the guard before the next tool request."""
    with tempfile.TemporaryDirectory() as td:
        run, status, guarded, _mailbox = _run_confirmation_scenario(
            Path(td),
            scenario="user_confirm_prefixed_same_turn_guard",
            dispatch_id="acp-user-confirm-prefixed-same-turn-guard",
            live_matrix=True,
        )
        decisions = [
            decision
            for decision in (status.get("permission_router_decisions") or [])
            if decision.get("tool_call_id") == "marker-guard-continue"
        ]
        guarded_denial = any(
            decision.get("decision") == "deny"
            and decision.get("reason") in {"user_confirm_pending", "user_confirm_denied"}
            for decision in decisions
        )
        failures = []
        if guarded.exists():
            failures.append("guarded action reached auto-allow and wrote its sentinel")
        if not guarded_denial or any(
            decision.get("decision") == "allow" for decision in decisions
        ):
            failures.append(f"permission router did not fail closed: {decisions!r}")
        if run.returncode == 0 or status.get("state") != "blocked_user_confirm_denied":
            failures.append(
                f"confirmation was not routed as a blocker: rc={run.returncode} "
                f"state={status.get('state')!r}"
            )
        if "same_turn_guarded_action_taken=false" not in (status.get("result_text") or ""):
            failures.append("worker did not observe a denied same-turn action")
        assert not failures, (failures, run.stdout, run.stderr, status)


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
                str(_project(tmp)),
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
            and decision.get("reason")
            in {"user_confirm_pending", "user_confirm_denied"}
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
        rows = [
            json.loads(line)
            for line in mailbox.read_text(encoding="utf-8").splitlines()
        ]
        questions = [
            row for row in rows if row.get("direction") == "worker_to_controller"
        ]
        pending = status.get("user_confirm_pending") or []
        resolved = status.get("user_confirm_resolved") or []
        assert (
            run.returncode != 0
            and status["state"] == "blocked_user_confirm"
            and status["error"]["message"] == "user_confirm_unresolved_after_denial"
            and "independent_work=preserved_after_denial" in status["result_text"]
            and not guarded.exists()
            and len(questions) == 2
            and pending == []
            and len(resolved) == 2
            and resolved[0]["decision_scope"] == resolved[1]["decision_scope"]
            and resolved[1]["controller_decision"] == "no"
            and resolved[1]["guarded_action_authorized"] is False
            and resolved[1]["resolution_reason"] == "run_terminal:blocked_user_confirm"
        ), (run.stdout, run.stderr, status, rows)


def case_hard_blocker_preserves_same_chunk_confirm_evidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, status, _guarded, mailbox = _run_confirmation_scenario(
            Path(td),
            scenario="user_confirm_then_blocked",
            dispatch_id="acp-user-confirm-hard-block",
            timeout_s=5.0,
        )
        assert run.returncode != 0, (run.stdout, run.stderr, status)
        assert status["state"] == "blocked", status
        assert "RESULT: partial=survives" in status["result_text"], status
        assert status.get("user_confirm_pending") == [], status
        resolved = status.get("user_confirm_resolved") or []
        assert len(resolved) == 1, status
        assert resolved[0]["controller_decision"] == "no", status
        assert resolved[0]["guarded_action_authorized"] is False, status
        assert resolved[0]["resolution_reason"] == "run_terminal:blocked", status
        rows = [
            json.loads(line)
            for line in mailbox.read_text(encoding="utf-8").splitlines()
        ]
        questions = [
            row for row in rows if row.get("direction") == "worker_to_controller"
        ]
        assert len(questions) == 1, rows
        assert questions[0]["question_id"] == resolved[0]["question_id"], (rows, status)


def case_restart_does_not_reuse_question_id_or_accept_stale_yes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dispatch_id = "acp-user-confirm-restart"
        successor_dispatch_id = "acp-user-confirm-restart-successor"
        first_run, first_status, _first_guarded, _mailbox = _run_confirmation_scenario(
            tmp,
            scenario="user_confirm_then_blocked",
            dispatch_id=dispatch_id,
            timeout_s=5,
        )
        assert first_run.returncode != 0, (first_run.stdout, first_run.stderr, first_status)
        first_questions = (
            first_status.get("user_confirm_pending")
            or first_status.get("user_confirm_resolved")
            or []
        )
        assert len(first_questions) == 1, first_status
        pending_snapshot = dict(first_questions[0])
        old_question_id = pending_snapshot["question_id"]
        for field in (
            "controller_decision",
            "reply_steer_seq",
            "resolved_at",
            "resolution_reason",
        ):
            pending_snapshot.pop(field, None)
        pending_snapshot["guarded_action_authorized"] = False
        restart_status = dict(first_status)
        restart_status["dispatch_id"] = successor_dispatch_id
        restart_status["user_confirm_pending"] = [pending_snapshot]
        restart_status["user_confirm_resolved"] = []
        (tmp / f"{successor_dispatch_id}.status.json").write_text(
            json.dumps(restart_status),
            encoding="utf-8",
        )

        second_proc, second_stdout, second_stderr, second_status, guarded, new_question = (
            _run_answered_confirmation(
                tmp,
                scenario="user_confirm_continue",
                dispatch_id=successor_dispatch_id,
                decisions=["yes"],
                pre_question_messages=[
                    f"USER-CONFIRM-ANSWER: {old_question_id} yes"
                ],
                exclude_question_ids={old_question_id},
                extra_env={
                    "GOALFLIGHT_FAKE_ACP_REQUEST_GUARDED_PERMISSION": "1",
                    "GOALFLIGHT_ACP_LIVE_MATRIX": "1",
                    "GOALFLIGHT_FAKE_ACP_PERMISSION_LOCATION": str(
                        _project(tmp) / ".goalflight-fake-guard-target"
                    ),
                },
            )
        )
        resolved = second_status.get("user_confirm_resolved") or []
        old = [item for item in resolved if item["question_id"] == old_question_id]
        new = [item for item in resolved if item["question_id"] == new_question["question_id"]]
        permission_decisions = second_status.get("permission_router_decisions") or []
        assert (
            second_proc.returncode == 0
            and second_status["state"] == "complete"
            and second_status["ok"] is True
            and second_status["had_denied_action"] is False
            and not guarded.exists()
            and new_question["question_id"] != old_question_id
            and len(old) == 1
            and old[0]["controller_decision"] == "no"
            and old[0]["resolution_reason"] == "runner_restarted"
            and old[0]["guarded_action_authorized"] is False
            and len(new) == 1
            and new[0]["controller_decision"] == "yes"
            and new[0]["guarded_action_authorized"] is False
            and old[0].get("decision_scope", old_question_id)
            != new[0]["decision_scope"]
            and bool(second_status.get("user_confirm_rejected_reply_seqs"))
            and "marker_redirect_seen=true" in second_status["result_text"]
            and any(
                decision.get("reason") == "user_confirm_denied"
                and decision.get("tool_call_id") == "guarded-after-confirm"
                for decision in permission_decisions
            )
        ), (second_stdout, second_stderr, second_status)


def case_quoted_authorize_grammar_in_ordinary_steer_is_inert() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc, stdout, stderr, status, guarded, question = _run_answered_confirmation(
            tmp,
            scenario="user_confirm_continue",
            dispatch_id="acp-user-confirm-quoted-token",
            decisions=[],
            steer_messages=[
                (
                    "Reminder: USER-CONFIRM-ANSWER: {question_id} yes is quoted "
                    "documentation, not approval"
                ),
                "USER-CONFIRM-ANSWER: {question_id} no",
            ],
        )
        resolved = status.get("user_confirm_resolved") or []
        assert (
            proc.returncode != 0
            and status["state"] == "blocked_user_confirm_denied"
            and not guarded.exists()
            and len(resolved) == 1
            and resolved[0]["question_id"] == question["question_id"]
            and resolved[0]["controller_decision"] == "no"
            and resolved[0]["guarded_action_authorized"] is False
        ), (stdout, stderr, status)


def case_crossed_dual_user_confirm_answers_never_emit_authorization() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dispatch_id = "acp-user-confirm-dual-crossed"
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "user_confirm_dual"
        env["GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP"] = "0.5"
        env["GOALFLIGHT_USER_CONFIRM_TIMEOUT_S"] = "5"
        guarded = tmp / f"{dispatch_id}-guarded"
        env["GOALFLIGHT_FAKE_ACP_GUARDED_FILE"] = str(guarded)
        status_path = tmp / f"{dispatch_id}.status.json"
        mailbox = (
            Path(env["GOALFLIGHT_STATE_DIR"])
            / "dispatch"
            / f"{dispatch_id}.steer.jsonl"
        )
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
                str(_project(tmp)),
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
            questions = _wait_for_worker_questions(mailbox, count=2)
            for question, decision in zip(questions, ("no", "yes"), strict=True):
                reply = subprocess.run(
                    [
                        sys.executable,
                        str(DISPATCH),
                        "steer",
                        dispatch_id,
                        (
                            f"USER-CONFIRM-ANSWER: {question['question_id']} "
                            f"{decision}"
                        ),
                    ],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
                assert reply.returncode == 0, reply.stdout + reply.stderr
            stdout, stderr = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=10)

        status = json.loads(status_path.read_text(encoding="utf-8"))
        resolved = status.get("user_confirm_resolved") or []
        by_text = {item["text"]: item for item in resolved}
        denied = by_text["authorize guarded sentinel A write? [Y/N]"]
        recorded_yes = by_text["authorize guarded sentinel B write? [Y/N]"]
        assert (
            proc.returncode != 0
            and status["state"] == "blocked_user_confirm_denied"
            and len(resolved) == 2
            and denied["controller_decision"] == "no"
            and recorded_yes["controller_decision"] == "yes"
            and denied["decision_scope"] == recorded_yes["decision_scope"]
            and denied["guarded_action_authorized"] is False
            and recorded_yes["guarded_action_authorized"] is False
            and not guarded.exists()
            and "guarded_action_taken=false" in status["result_text"]
        ), (stdout, stderr, status)


def case_prior_denial_blocks_later_scope_in_same_generation() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dispatch_id = "acp-user-confirm-prior-denial"
        proc, env, guarded, status_path, mailbox = _start_confirmation_runner(
            tmp,
            scenario="user_confirm_later_after_no",
            dispatch_id=dispatch_id,
            extra_env={
                "GOALFLIGHT_FAKE_ACP_FIRST_TURN_SLEEP": "0.5",
                "GOALFLIGHT_ACP_LIVE_MATRIX": "1",
                "GOALFLIGHT_FAKE_ACP_PERMISSION_LOCATION": str(
                    _project(tmp) / ".goalflight-fake-guard-target"
                ),
            },
        )
        try:
            first = _wait_for_worker_question(mailbox)
            _answer_confirmation(
                dispatch_id=dispatch_id,
                question_id=first["question_id"],
                decision="no",
                cwd=ROOT,
                env=env,
            )
            second = _wait_for_worker_question(
                mailbox,
                exclude_question_ids={first["question_id"]},
            )
            _answer_confirmation(
                dispatch_id=dispatch_id,
                question_id=second["question_id"],
                decision="yes",
                cwd=ROOT,
                env=env,
            )
            stdout, stderr = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=10)

        status = json.loads(status_path.read_text(encoding="utf-8"))
        resolved = status.get("user_confirm_resolved") or []
        by_text = {item["text"]: item for item in resolved}
        denied = by_text["authorize guarded sentinel A write? [Y/N]"]
        recorded_yes = by_text["authorize guarded sentinel B write? [Y/N]"]
        permission_decisions = status.get("permission_router_decisions") or []
        assert (
            proc.returncode != 0
            and status["state"] == "blocked_user_confirm_denied"
            and len(resolved) == 2
            and denied["controller_decision"] == "no"
            and recorded_yes["controller_decision"] == "yes"
            and denied["generation"] == recorded_yes["generation"]
            and denied["decision_scope"] != recorded_yes["decision_scope"]
            and denied["guarded_action_authorized"] is False
            and recorded_yes["guarded_action_authorized"] is False
            and not guarded.exists()
            and "authorization_token_seen=false" in status["result_text"]
            and "read_only_reason_seen=true" in status["result_text"]
            and "permission_selected=false" in status["result_text"]
            and any(
                decision.get("reason") == "user_confirm_denied"
                for decision in permission_decisions
            )
        ), (stdout, stderr, status)


def case_same_turn_permission_escalation_and_marker_yes_both_stay_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dispatch_id = "acp-permission-and-marker"
        escalation_target = tmp / f"{dispatch_id}-escalation"
        proc, env, guarded, status_path, mailbox = _start_confirmation_runner(
            tmp,
            scenario="permission_escalation_and_marker",
            dispatch_id=dispatch_id,
            extra_env={
                "GOALFLIGHT_ACP_LIVE_MATRIX": "1",
                "GOALFLIGHT_FAKE_ACP_ESCALATION_FILE": str(escalation_target),
                "GOALFLIGHT_FAKE_ACP_PERMISSION_LOCATION": str(
                    _project(tmp) / ".goalflight-fake-guard-target"
                ),
            },
        )
        try:
            questions = _wait_for_worker_questions(mailbox, count=2)
            for question in questions:
                _answer_confirmation(
                    dispatch_id=dispatch_id,
                    question_id=question["question_id"],
                    decision="yes",
                    cwd=ROOT,
                    env=env,
                )
            stdout, stderr = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=10)

        status = json.loads(status_path.read_text(encoding="utf-8"))
        resolved = status.get("user_confirm_resolved") or []
        by_origin = {item["origin"]: item for item in resolved}
        escalation = by_origin["permission_escalation"]
        marker = by_origin["worker_marker"]
        permission_decisions = status.get("permission_router_decisions") or []
        assert (
            proc.returncode != 0
            and status["state"] == "blocked_permission_denied"
            and status["had_denied_action"] is True
            and len(resolved) == 2
            and escalation["controller_decision"] == "yes"
            and marker["controller_decision"] == "yes"
            and escalation["generation"] == marker["generation"]
            and escalation["turn_index"] == marker["turn_index"]
            and escalation["decision_scope"] != marker["decision_scope"]
            and escalation["guarded_action_authorized"] is False
            and marker["guarded_action_authorized"] is False
            and not escalation_target.exists()
            and "marker_permission_selected=false" in status["result_text"]
            and "marker_redirect_seen=true" in status["result_text"]
            and any(
                decision.get("reason") == "user_confirm_denied"
                and decision.get("tool_call_id")
                == "marker-after-permission-escalation"
                for decision in permission_decisions
            )
            and not guarded.exists()
        ), (stdout, stderr, status)


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


def case_correlated_worker_marker_yes_never_opens_non_read_permissions() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc, stdout, stderr, status, guarded, _question = _run_answered_confirmation(
            tmp,
            scenario="user_confirm_continue",
            dispatch_id="acp-user-confirm-yes",
            decisions=["yes"],
            extra_env={
                "GOALFLIGHT_FAKE_ACP_REQUEST_GUARDED_PERMISSION": "1",
                "GOALFLIGHT_ACP_LIVE_MATRIX": "1",
                "GOALFLIGHT_FAKE_ACP_PERMISSION_LOCATION": str(
                    _project(tmp) / ".goalflight-fake-guard-target"
                ),
            },
        )
        assert proc.returncode == 0, (stdout, stderr, status)
        assert status["state"] == "complete", status
        assert status["ok"] is True, status
        assert not guarded.exists(), status
        resolved = status.get("user_confirm_resolved") or []
        assert len(resolved) == 1, status
        assert resolved[0]["controller_decision"] == "yes", status
        assert resolved[0]["guarded_action_authorized"] is False, status
        assert status["had_denied_action"] is False, status
        assert "marker_redirect_seen=true" in status["result_text"], status
        assert "guarded_action_taken=false" in status["result_text"], status
        assert any(
            decision.get("reason") == "user_confirm_denied"
            and decision.get("tool_call_id") == "guarded-after-confirm"
            for decision in (status.get("permission_router_decisions") or [])
        ), status


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
    case_user_confirm_midrun_yes_records_consent_without_authorizing_action()
    case_midturn_mailbox_yes_is_reconciled_before_timeout_denial()
    case_post_deadline_yes_before_delayed_read_is_denied()
    case_uncorrelated_user_confirm_yes_is_not_authorization()
    case_same_turn_guarded_action_is_denied_without_answer()
    case_prefixed_same_turn_guarded_action_is_denied_without_answer()
    case_user_confirm_timeout_is_fail_closed_then_continues()
    case_split_user_confirm_routes_once()
    case_fenced_user_confirm_example_does_not_activate_guard()
    case_kindless_permission_is_denied_while_confirm_pending()
    case_repeated_question_after_timeout_blocks_with_partial_work()
    case_hard_blocker_preserves_same_chunk_confirm_evidence()
    case_restart_does_not_reuse_question_id_or_accept_stale_yes()
    case_quoted_authorize_grammar_in_ordinary_steer_is_inert()
    case_crossed_dual_user_confirm_answers_never_emit_authorization()
    case_prior_denial_blocks_later_scope_in_same_generation()
    case_same_turn_permission_escalation_and_marker_yes_both_stay_closed()
    case_conflicting_user_confirm_answers_are_deny_biased()
    case_correlated_worker_marker_yes_never_opens_non_read_permissions()
    case_permission_escalation_yes_is_acknowledgment_not_authorization()
    case_permission_escalation_records_correlated_no_and_nonclean_terminal()
    print("OK: ACP steer tests pass")


if __name__ == "__main__":
    main()
