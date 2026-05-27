#!/usr/bin/env python3
"""Hermetic tests for post-compaction resume drill (structure + fast subset)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRILL = ROOT / "scripts/hosts/controller/compaction_resume_drill.py"
FIXTURE = ROOT / "tests/fixtures/compaction_handoff/RESUME-NOTES.md"


def _run_drill(*extra: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(DRILL), "--directory", str(ROOT), *extra],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_fixture_resume_notes_exists() -> None:
    assert FIXTURE.is_file()
    text = FIXTURE.read_text(encoding="utf-8")
    assert "goalflight_status" in text
    assert "First 5 minutes" in text


def test_compaction_drill_status_only() -> None:
    payload = _run_drill("--resume-notes", str(FIXTURE), "--json")
    assert payload["schema"] == "goalflight.controller-harness.v1"
    assert payload.get("scenario") == "compaction-resume-drill"
    assert not payload.get("skipped")
    ids = {c["id"] for c in payload.get("checks") or []}
    assert "resume_notes_readable" in ids
    assert "goalflight_status" in ids
    assert "git_snapshot" in ids
    assert payload.get("ok")


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
        print(f"FAIL tests/python/test_compaction_resume_drill.py ({len(failures)} failed)")
        for name, err in failures:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print(f"PASS tests/python/test_compaction_resume_drill.py ({ok_count} tests)")
