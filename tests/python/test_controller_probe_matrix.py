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
from common import continue_prescribed_step_two_checks  # noqa: E402


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
