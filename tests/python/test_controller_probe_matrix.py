#!/usr/bin/env python3
"""Hermetic tests for controller probe matrix structure."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "scripts/hosts/controller/probe_matrix.py"
FIXTURES = ROOT / "tests/fixtures/controller_scenarios"
CONTROLLER_HOST_DIR = ROOT / "scripts/hosts/controller"
sys.path.insert(0, str(CONTROLLER_HOST_DIR))

from behavior_scenario import SCENARIOS  # noqa: E402
from common import (  # noqa: E402
    DEFAULT_BEHAVIOR_SCENARIOS,
    compaction_reload_skill_checks,
    continue_prescribed_step_two_checks,
    read_skill_end_to_end_checks,
    review_flight_at_completion_checks,
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


def test_new_behavior_scenarios_sync_to_defaults_and_docs() -> None:
    required = {
        "read-skill-end-to-end",
        "compaction-reload-skill",
        "review-flight-at-completion",
    }
    bash_wrapper = ROOT / "tests/bash/test-controller-behavior-codex.sh"
    command_doc = ROOT / "commands/controller-behavior-test.md"
    bash_text = bash_wrapper.read_text(encoding="utf-8")
    doc_text = command_doc.read_text(encoding="utf-8")

    assert required <= set(SCENARIOS)
    assert required <= set(DEFAULT_BEHAVIOR_SCENARIOS)
    for scenario_id in required:
        assert scenario_id in bash_text
        assert f"`{scenario_id}`" in doc_text


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
