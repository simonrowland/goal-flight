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
