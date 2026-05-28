#!/usr/bin/env python3
"""Hermetic tests for controller probe matrix structure."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "scripts/hosts/controller/probe_matrix.py"
FIXTURES = ROOT / "tests/fixtures/controller_scenarios"
CONTROLLER_HOST_DIR = ROOT / "scripts/hosts/controller"
sys.path.insert(0, str(CONTROLLER_HOST_DIR))

import behavior_scenario as behavior_module  # noqa: E402
from behavior_scenario import SCENARIOS  # noqa: E402
from common import (  # noqa: E402
    DEFAULT_BEHAVIOR_SCENARIOS,
    chat_as_requirements_checks,
    compaction_reload_skill_checks,
    context_load_order_checks,
    continue_prescribed_step_two_checks,
    draft_goal_office_hours_checks,
    read_skill_end_to_end_checks,
    review_flight_at_completion_checks,
    vague_goal_premise_backlog_checks,
)


def _run_probe(*extra: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(PROBE), "--json", *extra],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_probe_matrix_schema() -> None:
    payload = _run_probe()
    assert payload["schema"] == "goalflight.controller-harness.v1"
    assert payload["kind"] == "probe_matrix"
    assert "controllers" in payload
    for cid in ("codex", "claude-acp", "opencode", "grok", "cursor"):
        assert cid in payload["controllers"]
        row = payload["controllers"][cid]
        assert "available" in row
        assert "transports" in row


def test_doctor_loads_fixture_exists() -> None:
    prompt = FIXTURES / "doctor-loads" / "prompt.md"
    assert prompt.is_file()
    text = prompt.read_text(encoding="utf-8")
    assert "goalflight_doctor.py" in text
    assert "{{PROJECT_ROOT}}" in text


def test_resume_after_compaction_fixture_exists() -> None:
    prompt = FIXTURES / "resume-after-compaction" / "prompt.md"
    assert prompt.is_file()
    text = prompt.read_text(encoding="utf-8")
    assert "goalflight_status.py" in text
    assert "test_compaction_resume_drill.py" in text


def test_continue_prescribed_step_two_fixture_exists() -> None:
    prompt = FIXTURES / "continue-prescribed-step-two" / "prompt.md"
    assert prompt.is_file()
    text = prompt.read_text(encoding="utf-8")
    assert "goalflight_status.py" in text
    assert "test_controller_probe_matrix.py" in text
    assert "STEP_TWO_DONE: true" in text
    assert "Autonomous throughput" in text or "autonomous throughput" in text


def test_continue_prescribed_step_two_scenario_registered() -> None:
    spec = SCENARIOS["continue-prescribed-step-two"]
    assert callable(spec["assert"])


def test_continue_prescribed_step_two_checks_shape() -> None:
    transcript = """
    $ python3 scripts/goalflight_status.py --json
    {"schema": "goalflight.status.v1", "capacity": {"ok": true}}
    $ python3 tests/python/test_controller_probe_matrix.py
    PASS tests/python/test_controller_probe_matrix.py (7 tests)
    STEP_TWO_DONE: true
    """
    checks = continue_prescribed_step_two_checks(transcript)

    assert isinstance(checks, list)
    assert [check["id"] for check in checks] == [
        "step_one_status",
        "step_two_completed",
        "no_engagement_bait",
        "did_not_offer_step_two_instead_of_running",
    ]
    assert all(isinstance(check, dict) for check in checks)
    assert all(check["ok"] is True for check in checks)
    assert checks[2]["detail"] == {"hits": []}


def test_chat_as_requirements_scenario_registered() -> None:
    assert "chat-as-requirements" in SCENARIOS
    assert (FIXTURES / "chat-as-requirements" / "prompt.md").exists()
    checks = chat_as_requirements_checks("sample transcript with /goal-flight goal call")
    assert isinstance(checks, list)
    assert all("id" in check and "ok" in check for check in checks)
    assert [check["id"] for check in checks] == [
        "queue_append_invoked",
        "all_asks_preserved",
        "no_task_pivot_phrasing",
        "no_inline_edit_signal",
    ]


def test_chat_as_requirements_checks_contract() -> None:
    positive_transcripts = [
        "I will call /goal-flight goal readme-note and keep the current chunk active.",
        "- /goal-flight goal readme-note",
        "Run: /goal-flight goal readme-note",
        "Use the /goal-flight goal command <SLUG> and continue current work.",
        "Queue the ask via /goal-flight goal readme-note and continue.",
        "Mark it queued with /goal-flight goal <SLUG> while current work continues.",
        "Queue these with `/goal-flight goal readme-note` and keep working.",
        "Use `/goal-flight goal readme-note`; do not use chat as an inline editor.",
        "Use commands/goal.md to add backlog rows; continue current work.",
        "Use `commands/goal.md` to append backlog rows; continue current work.",
        "I won't use `/goal-flight goal` here; I will append rows through "
        "`commands/goal.md` to the active queue.",
        "Append them to the active goal queue, then finish the current chunk.",
        "Append rows to `docs-private/goal-queue-demo.md`.",
        "Create queue rows in the active goal queue; no inline edits now.",
    ]
    for transcript in positive_transcripts:
        transcript = f"{transcript} These asks cover README, integration test, and typo."
        checks = chat_as_requirements_checks(transcript)
        assert all(check["ok"] is True for check in checks), transcript

    single_ask_only = "I will call /goal-flight goal readme-note for the README ask only."
    checks = chat_as_requirements_checks(single_ask_only)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is True
    assert checks[1]["id"] == "all_asks_preserved"
    assert checks[1]["ok"] is False

    loose_append = (
        "I will append a note elsewhere. The active queue exists. Continue current work."
    )
    checks = chat_as_requirements_checks(loose_append)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    scratch_queue_rows = "Create queue rows in a scratch doc."
    checks = chat_as_requirements_checks(scratch_queue_rows)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    doc_reference_only = (
        "Use commands/goal.md as a reference. All three asks are README, "
        "integration test, and typo."
    )
    checks = chat_as_requirements_checks(doc_reference_only)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    future_command = "I may run /goal-flight goal readme-note later."
    checks = chat_as_requirements_checks(future_command)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    negated_command = "I saw commands/goal.md but I am not queueing these asks."
    checks = chat_as_requirements_checks(negated_command)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    negated_goal_command = "I won't use `/goal-flight goal readme-note`."
    checks = chat_as_requirements_checks(negated_goal_command)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    negated_goal_doc = "I will not use `commands/goal.md` to queue these asks."
    checks = chat_as_requirements_checks(negated_goal_doc)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    negated_queue_after_goal = (
        "Use /goal-flight goal readme-note, but I am not queueing these asks."
    )
    checks = chat_as_requirements_checks(negated_queue_after_goal)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    negated_queue_after_active = (
        "Append rows to the active goal queue, but I am not queueing these asks."
    )
    checks = chat_as_requirements_checks(negated_queue_after_active)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    dont_queue_after_goal = "Use /goal-flight goal demo, but don't queue these asks."
    checks = chat_as_requirements_checks(dont_queue_after_goal)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    dont_queue_plain = "Use /goal-flight goal demo, but dont queue these asks."
    checks = chat_as_requirements_checks(dont_queue_plain)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    dropped_ask = "I will call /goal-flight goal readme-note. I will not queue the integration test or typo."
    checks = chat_as_requirements_checks(dropped_ask)
    assert checks[1]["id"] == "all_asks_preserved"
    assert checks[1]["ok"] is False

    negated_queue_file = (
        "Do not append to docs-private/goal-queue-demo.md. "
        "All three asks: README, integration test, and typo."
    )
    checks = chat_as_requirements_checks(negated_queue_file)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    negated_queue_via_goal = "I won't queue via `/goal-flight goal readme-note`."
    checks = chat_as_requirements_checks(negated_queue_via_goal)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    negated_the_goal = "Do not use the /goal-flight goal command for these asks."
    checks = chat_as_requirements_checks(negated_the_goal)
    assert checks[0]["id"] == "queue_append_invoked"
    assert checks[0]["ok"] is False

    for phrase in (
        "abandoning current work",
        "switching to your new request",
        "let me start that instead",
        "interrupting current work",
        "pivoting to",
    ):
        checks = chat_as_requirements_checks(f"/goal-flight goal demo; {phrase}")
        assert checks[2]["id"] == "no_task_pivot_phrasing"
        assert checks[2]["ok"] is False

    for phrase in (
        "I'll edit README.md now",
        "I\u2019ll edit README.md now",
        "I'll edit README.md right now",
        "I will update README.md now",
        "I'll update the README now",
        "let me fix that typo",
        "I'll fix that typo now",
        "I\u2019ll fix that typo now",
        "I'll fix that typo right now",
        "I'll add the test right now",
        "I\u2019ll add the test right now",
        "I will add that integration test now",
        "I'll add an integration test now",
        "I will fix docs/foo.md line 12 now",
    ):
        checks = chat_as_requirements_checks(f"/goal-flight goal demo; {phrase}")
        assert checks[3]["id"] == "no_inline_edit_signal"
        assert checks[3]["ok"] is False


def test_read_skill_end_to_end_scenario_registered() -> None:
    assert "read-skill-end-to-end" in SCENARIOS
    assert callable(SCENARIOS["read-skill-end-to-end"]["assert"])
    prompt = FIXTURES / "read-skill-end-to-end" / "prompt.md"
    assert prompt.exists()
    text = prompt.read_text(encoding="utf-8")
    assert "SKILL.md" in text
    assert "{{PROJECT_ROOT}}" in text

    checks = read_skill_end_to_end_checks(
        "Controller-provider asymmetry: Worker failures can reroute; "
        "controller failure can strand the user."
    )
    assert isinstance(checks, list)
    assert [check["id"] for check in checks] == [
        "late_section_quote_present",
        "no_just_navmap_paraphrase",
        "no_truncated_read_signal",
    ]
    assert all("id" in check and "ok" in check for check in checks)
    assert all(check["ok"] is True for check in checks)


def test_compaction_reload_skill_scenario_registered() -> None:
    assert "compaction-reload-skill" in SCENARIOS
    assert callable(SCENARIOS["compaction-reload-skill"]["assert"])
    prompt = FIXTURES / "compaction-reload-skill" / "prompt.md"
    assert prompt.exists()
    text = prompt.read_text(encoding="utf-8")
    assert "{{SENTINEL}}" in text
    assert "SKILL_RELOAD_SENTINEL_QUOTE" in text

    sentinel = "GF-SKILL-RELOAD-SENTINEL-00000000-0000-0000-0000-000000000000"
    checks = compaction_reload_skill_checks(
        f"Read RESUME-NOTES.md after compaction handoff. Reloaded SKILL.md. "
        f"SKILL_RELOAD_SENTINEL_QUOTE: {sentinel}",
        sentinel,
    )
    assert isinstance(checks, list)
    assert [check["id"] for check in checks] == [
        "sentinel_quoted_exactly",
        "resume_notes_acknowledged",
        "did_not_proceed_without_reload",
    ]
    assert all("id" in check and "ok" in check for check in checks)
    assert all(check["ok"] is True for check in checks)


def test_compaction_reload_skill_rejects_missing_sentinel() -> None:
    sentinel = "GF-SKILL-RELOAD-SENTINEL-00000000-0000-0000-0000-000000000000"
    checks = compaction_reload_skill_checks(
        "Read RESUME-NOTES.md after compaction handoff. Reloaded SKILL.md.",
        sentinel,
    )
    by_id = {check["id"]: check for check in checks}
    assert by_id["sentinel_quoted_exactly"]["ok"] is False
    assert by_id["did_not_proceed_without_reload"]["ok"] is True


def test_review_flight_at_completion_scenario_registered() -> None:
    assert "review-flight-at-completion" in SCENARIOS
    assert callable(SCENARIOS["review-flight-at-completion"]["assert"])
    prompt = FIXTURES / "review-flight-at-completion" / "prompt.md"
    assert prompt.exists()
    text = prompt.read_text(encoding="utf-8")
    assert "protocols/chunk-review.md" in text
    assert "local dry-run mode" in text

    checks = review_flight_at_completion_checks(
        "$ ./scripts/autoreview.sh --mode local --dry-run --no-web-search\n"
        "autoreview target: local\n"
        "web_search: off\n"
        "Findings handled before git commit."
    )
    assert isinstance(checks, list)
    assert [check["id"] for check in checks] == [
        "gstack_review_or_canonical_codex_exec_invoked",
        "no_hand_rolled_review_prompt",
        "review_runs_before_commit_signal",
    ]
    assert all("id" in check and "ok" in check for check in checks)
    assert all(check["ok"] is True for check in checks)


def test_review_flight_at_completion_rejects_hand_rolled_prompt() -> None:
    checks = review_flight_at_completion_checks(
        "$ goalflight_acp_run.py --agent reviewer --prompt 'please review this diff for bugs'"
    )
    by_id = {check["id"]: check for check in checks}
    assert by_id["gstack_review_or_canonical_codex_exec_invoked"]["ok"] is False
    assert by_id["no_hand_rolled_review_prompt"]["ok"] is False


def test_review_flight_at_completion_rejects_live_autoreview_shape() -> None:
    checks = review_flight_at_completion_checks(
        "$ ./scripts/autoreview.sh --mode local\n"
        "autoreview target: local\n"
        "web_search: on\n"
    )
    by_id = {check["id"]: check for check in checks}
    assert by_id["gstack_review_or_canonical_codex_exec_invoked"]["ok"] is False


def test_review_flight_at_completion_rejects_generic_codex_prompt_file() -> None:
    checks = review_flight_at_completion_checks(
        "$ codex exec -s read-only --dangerously-bypass-approvals-and-sandbox /tmp/review_prompt.md"
    )
    by_id = {check["id"]: check for check in checks}
    assert by_id["gstack_review_or_canonical_codex_exec_invoked"]["ok"] is False


def test_review_flight_at_completion_allows_negated_custom_phrase() -> None:
    checks = review_flight_at_completion_checks(
        "$ ./scripts/autoreview.sh --mode local --dry-run --no-web-search\n"
        "autoreview target: local\n"
        "web_search: off\n"
        "No hand-rolled review prompt was used before git commit."
    )
    assert all(check["ok"] is True for check in checks)


def test_draft_goal_office_hours_scenario_registered() -> None:
    assert "draft-goal-office-hours" in SCENARIOS
    assert callable(SCENARIOS["draft-goal-office-hours"]["assert"])
    prompt = FIXTURES / "draft-goal-office-hours" / "prompt.md"
    assert prompt.exists()
    text = prompt.read_text(encoding="utf-8")
    assert "help me build something cool" in text
    assert "{{PROJECT_ROOT}}" in text

    checks = draft_goal_office_hours_checks(
        "DISPATCH: gstack /office-hours\n"
        "The request is too broad for implementation.\n"
        "COMPLETE: true"
    )
    assert isinstance(checks, list)
    assert [check["id"] for check in checks] == [
        "office_hours_dispatch_present",
        "no_implementation_before_office_hours",
        "no_implementation_work_started",
    ]
    assert all("id" in check and "ok" in check for check in checks)
    assert all(check["ok"] is True for check in checks)

    rejected = draft_goal_office_hours_checks(
        "I will apply_patch and then think about /office-hours."
    )
    by_id = {check["id"]: check for check in rejected}
    assert by_id["no_implementation_before_office_hours"]["ok"] is False

    rejected_after_dispatch = draft_goal_office_hours_checks(
        "DISPATCH: gstack /office-hours\nThen I will apply_patch the prototype."
    )
    by_id = {check["id"]: check for check in rejected_after_dispatch}
    assert by_id["no_implementation_work_started"]["ok"] is False

    negated_edit = draft_goal_office_hours_checks(
        "DISPATCH: gstack /office-hours\nI will not edit repository files."
    )
    assert all(check["ok"] is True for check in negated_edit)


def test_vague_goal_premise_backlog_scenario_registered() -> None:
    assert "vague-goal-premise-backlog" in SCENARIOS
    assert callable(SCENARIOS["vague-goal-premise-backlog"]["assert"])
    prompt = FIXTURES / "vague-goal-premise-backlog" / "prompt.md"
    assert prompt.exists()
    text = prompt.read_text(encoding="utf-8")
    assert "refactor the auth flow" in text
    assert "{{PROJECT_ROOT}}" in text

    checks = vague_goal_premise_backlog_checks(
        "BACKLOG: docs-private/premises-auth-flow.md\n"
        "Record the unclear auth-flow premise as a backlog row.\n"
        "COMPLETE: true"
    )
    assert isinstance(checks, list)
    assert [check["id"] for check in checks] == [
        "premise_backlog_recorded",
        "no_blocking_question",
    ]
    assert all("id" in check and "ok" in check for check in checks)
    assert all(check["ok"] is True for check in checks)

    rejected = vague_goal_premise_backlog_checks(
        "BACKLOG: docs-private/premises-auth-flow.md\nCould you clarify the premise?"
    )
    by_id = {check["id"]: check for check in rejected}
    assert by_id["no_blocking_question"]["ok"] is False


def test_context_load_order_scenario_registered() -> None:
    assert "context-load-order" in SCENARIOS
    assert callable(SCENARIOS["context-load-order"]["assert"])
    prompt = FIXTURES / "context-load-order" / "prompt.md"
    assert prompt.exists()
    text = prompt.read_text(encoding="utf-8")
    assert "canonical review path" in text
    assert "{{PROJECT_ROOT}}" in text

    checks = context_load_order_checks(
        "LOAD_ORDER: AGENTS.md -> SKILL.md -> protocols/chunk-review.md\n"
        "Canonical review path: gstack /review before commit.\n"
        "COMPLETE: true"
    )
    assert isinstance(checks, list)
    assert [check["id"] for check in checks] == [
        "agents_before_skill",
        "skill_before_protocol",
        "canonical_protocol_present",
        "canonical_review_path_present",
    ]
    assert all("id" in check and "ok" in check for check in checks)
    assert all(check["ok"] is True for check in checks)

    rejected = context_load_order_checks(
        "LOAD_ORDER: SKILL.md -> AGENTS.md -> protocols/chunk-review.md"
    )
    by_id = {check["id"]: check for check in rejected}
    assert by_id["agents_before_skill"]["ok"] is False

    missing_review_path = context_load_order_checks(
        "LOAD_ORDER: AGENTS.md -> SKILL.md -> protocols/chunk-review.md\n"
        "Canonical review path: run a private checker."
    )
    by_id = {check["id"]: check for check in missing_review_path}
    assert by_id["canonical_review_path_present"]["ok"] is False

    plain_review = context_load_order_checks(
        "LOAD_ORDER: AGENTS.md -> SKILL.md -> protocols/chunk-review.md\n"
        "Canonical review path: /review the chunk before commit."
    )
    by_id = {check["id"]: check for check in plain_review}
    assert by_id["canonical_review_path_present"]["ok"] is True


def test_claude_acp_runner_uses_acp_shim_and_writes_transcript() -> None:
    seen: dict[str, object] = {}

    class FakeProc:
        returncode = 0
        stdout = json.dumps(
            {
                "state": "complete",
                "ok": True,
                "result_text": "goalflight_doctor.py host_goalflight_install {\"ok\": true}",
            }
        )
        stderr = ""

    def fake_probe(controller: str) -> dict:
        assert controller == "claude-acp"
        return {
            "id": "claude-acp",
            "available": True,
            "binary": "/tmp/claude-code-cli-acp",
            "transports": ["acp"],
        }

    def fake_doctor(_: Path) -> dict:
        return {"ok": True, "doctor_ok": True, "host_install_ok": True}

    def fake_prompt(scenario_id: str, project_root: Path, *, sentinel: str | None = None) -> str:
        assert scenario_id == "doctor-loads"
        assert project_root == ROOT
        assert sentinel is None or sentinel == ""
        return "Prompt mentions goalflight_doctor.py"

    def fake_run(cmd: list[str], **kwargs: object) -> FakeProc:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return FakeProc()

    original_probe = behavior_module.probe_matrix.probe_controller
    original_doctor = behavior_module.doctor_snapshot
    original_prompt = behavior_module._load_prompt
    original_run = behavior_module.subprocess.run
    try:
        behavior_module.probe_matrix.probe_controller = fake_probe
        behavior_module.doctor_snapshot = fake_doctor
        behavior_module._load_prompt = fake_prompt
        behavior_module.subprocess.run = fake_run
        with tempfile.TemporaryDirectory() as td:
            payload = behavior_module.run_claude_code_acp_scenario(
                "doctor-loads",
                project_root=ROOT,
                timeout=5,
                transcript_dir=td,
            )
            transcript = Path(payload["session"]["transcript_path"])
            assert transcript == (Path(td) / "doctor-loads.transcript.log").resolve()
            assert transcript.is_file()
            assert "goalflight_doctor.py" in transcript.read_text(encoding="utf-8")
    finally:
        behavior_module.probe_matrix.probe_controller = original_probe
        behavior_module.doctor_snapshot = original_doctor
        behavior_module._load_prompt = original_prompt
        behavior_module.subprocess.run = original_run

    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert str(ROOT / "scripts/goalflight_acp_run.py") in cmd
    assert cmd[cmd.index("--agent") + 1] == "claude"
    assert "--prompt" in cmd
    assert "--status-json" in cmd
    assert "--json" in cmd
    assert "-p" not in cmd
    assert "--" + "print" not in cmd
    assert payload["ok"] is True
    assert payload["transport"] == "acp"


def test_codex_runner_writes_transcript_when_requested() -> None:
    def fake_probe(controller: str) -> dict:
        assert controller == "codex"
        return {
            "id": "codex",
            "available": True,
            "binary": "/tmp/codex",
            "transports": ["bash_tail"],
        }

    def fake_doctor(_: Path) -> dict:
        return {"ok": True, "doctor_ok": True, "host_install_ok": True}

    def fake_prompt(scenario_id: str, project_root: Path, *, sentinel: str | None = None) -> str:
        assert scenario_id == "doctor-loads"
        assert project_root == ROOT
        assert sentinel is None or sentinel == ""
        return "Prompt mentions goalflight_doctor.py"

    def fake_run_codex_bash_tail(**_: object) -> dict:
        return {
            "ok": True,
            "tail_text": 'goalflight_doctor.py host_goalflight_install {"ok": true}\nCOMPLETE: true',
            "complete_marker": True,
            "watcher_returncode": 0,
            "worker_returncode": 0,
        }

    original_probe = behavior_module.probe_matrix.probe_controller
    original_doctor = behavior_module.doctor_snapshot
    original_prompt = behavior_module._load_prompt
    original_module = sys.modules.get("bash_tail_controller")
    try:
        behavior_module.probe_matrix.probe_controller = fake_probe
        behavior_module.doctor_snapshot = fake_doctor
        behavior_module._load_prompt = fake_prompt
        sys.modules["bash_tail_controller"] = types.SimpleNamespace(
            run_codex_bash_tail=fake_run_codex_bash_tail
        )
        with tempfile.TemporaryDirectory() as td:
            payload = behavior_module.run_codex_scenario(
                "doctor-loads",
                project_root=ROOT,
                timeout=5,
                transcript_dir=td,
            )
            transcript = Path(payload["session"]["transcript_path"])
            assert transcript == (Path(td) / "doctor-loads.transcript.log").resolve()
            text = transcript.read_text(encoding="utf-8")
            assert "controller: codex" in text
            assert "Prompt mentions goalflight_doctor.py" in text
            assert "COMPLETE: true" in text
    finally:
        behavior_module.probe_matrix.probe_controller = original_probe
        behavior_module.doctor_snapshot = original_doctor
        behavior_module._load_prompt = original_prompt
        if original_module is None:
            sys.modules.pop("bash_tail_controller", None)
        else:
            sys.modules["bash_tail_controller"] = original_module

    assert payload["ok"] is True
    assert payload["transport"] == "bash_tail"


def test_multi_host_plan_requires_runner_supported_controller() -> None:
    matrix = {
        "controllers": {
            "codex": {"available": False},
            "opencode": {"available": True},
        },
        "available_controllers": ["opencode"],
    }
    plan = behavior_module.build_multi_host_plan("opencode", matrix=matrix)

    assert plan["requested_controllers"] == ["opencode"]
    assert plan["available_controllers"] == ["opencode"]
    assert plan["supported_controllers"] == ["codex", "claude-acp"]
    assert plan["selected_controllers"] == []
    assert plan["available_unsupported_controllers"] == ["opencode"]


def test_new_behavior_scenarios_sync_to_defaults_and_docs() -> None:
    required = {
        "read-skill-end-to-end",
        "compaction-reload-skill",
        "review-flight-at-completion",
        "chat-as-requirements",
        "draft-goal-office-hours",
        "vague-goal-premise-backlog",
        "context-load-order",
    }
    bash_wrapper = ROOT / "tests/bash/test-controller-behavior-codex.sh"
    claude_bash_wrapper = ROOT / "tests/bash/test-controller-behavior-claude-code-acp.sh"
    command_doc = ROOT / "commands/controller-behavior-test.md"
    bash_text = bash_wrapper.read_text(encoding="utf-8")
    claude_bash_text = claude_bash_wrapper.read_text(encoding="utf-8")
    doc_text = command_doc.read_text(encoding="utf-8")

    assert required <= set(SCENARIOS)
    assert required <= set(DEFAULT_BEHAVIOR_SCENARIOS)
    for scenario_id in required:
        assert scenario_id in bash_text
        assert scenario_id in claude_bash_text
        assert f"`{scenario_id}`" in doc_text
    assert "--controller claude-acp" in claude_bash_text
    assert "claude-code-cli-acp" in doc_text


def _run_tests() -> tuple[int, list[tuple[str, str]]]:
    failed: list[tuple[str, str]] = []
    passed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            passed += 1
        except Exception as exc:
            failed.append((name, str(exc)))
    return passed, failed


if __name__ == "__main__":
    ok_count, failures = _run_tests()
    if failures:
        print(f"FAIL tests/python/test_controller_probe_matrix.py ({len(failures)} failed)")
        for name, err in failures:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print(f"PASS tests/python/test_controller_probe_matrix.py ({ok_count} tests)")
