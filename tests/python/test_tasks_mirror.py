#!/usr/bin/env python3
"""Hermetic test for the tasks.jsonl <-> tasks-data.js mirror checker.

Drives scripts/check_tasks_mirror.js (node-only; no network, no localhost):
  - PASS on the tracked known-good fixture templates/state-skeleton/.
  - FAIL on planted-drift temp copies (changed field / added id / stray status).

If node is unavailable, the whole file SKIPs (runner-recognized "SKIP:" prefix).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_tasks_mirror.js"
FIXTURE = ROOT / "templates" / "state-skeleton"
NODE = shutil.which("node")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def run_checker(target_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, str(CHECKER), str(target_dir)],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _copy_fixture(dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE / "tasks.jsonl", dst / "tasks.jsonl")
    shutil.copy(FIXTURE / "tasks-data.js", dst / "tasks-data.js")


def test_default_fixture_passes() -> None:
    # No dir arg -> checker defaults to templates/state-skeleton/.
    proc = subprocess.run(
        [NODE, str(CHECKER)],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert_true("default fixture exit 0", proc.returncode == 0)
    assert_true("default fixture OK line", "OK: tasks mirror in sync" in proc.stdout)


def test_explicit_fixture_passes() -> None:
    proc = run_checker(FIXTURE)
    assert_true("explicit fixture exit 0", proc.returncode == 0)
    assert_true("explicit fixture OK line", "OK: tasks mirror in sync" in proc.stdout)


def test_drift_changed_field_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _copy_fixture(d)
        # Mutate a title in tasks.jsonl only -> data.js no longer mirrors it.
        jsonl = d / "tasks.jsonl"
        lines = jsonl.read_text().splitlines()
        first = json.loads(lines[0])
        first["title"] = "DRIFTED title that data.js does not carry"
        lines[0] = json.dumps(first)
        jsonl.write_text("\n".join(lines) + "\n")

        proc = run_checker(d)
    assert_true("changed-field exits non-zero", proc.returncode != 0)
    assert_true("changed-field reports field diff", "differs between the two files" in proc.stderr)
    assert_true("changed-field names the field", 'field "title"' in proc.stderr)


def test_drift_added_id_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _copy_fixture(d)
        # Add a brand-new id to tasks.jsonl that data.js lacks.
        jsonl = d / "tasks.jsonl"
        extra = {
            "id": "t-999",
            "kind": "task",
            "title": "Orphan id present only in tasks.jsonl.",
            "blocked_by": [],
            "links": [],
            "done": False,
        }
        jsonl.write_text(jsonl.read_text() + json.dumps(extra) + "\n")

        proc = run_checker(d)
    assert_true("added-id exits non-zero", proc.returncode != 0)
    assert_true("added-id reports id-set diff", "id-sets differ" in proc.stderr)
    assert_true("added-id names the id", "t-999" in proc.stderr)


def test_drift_injected_status_key_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _copy_fixture(d)
        # Inject a stray status key into BOTH files (so they still mirror each
        # other) -> the no-status invariant must still fail it.
        jsonl = d / "tasks.jsonl"
        lines = jsonl.read_text().splitlines()
        first = json.loads(lines[0])
        first["status"] = "in_progress"
        lines[0] = json.dumps(first)
        jsonl.write_text("\n".join(lines) + "\n")

        proc = run_checker(d)
    assert_true("status-key exits non-zero", proc.returncode != 0)
    assert_true("status-key reports stray status", "stray `status` key" in proc.stderr)


def main() -> None:
    if not NODE:
        print("SKIP: test_tasks_mirror.py: node not found on PATH")
        return
    if not CHECKER.is_file():
        raise AssertionError(f"checker missing: {CHECKER}")

    test_default_fixture_passes()
    test_explicit_fixture_passes()
    test_drift_changed_field_fails()
    test_drift_added_id_fails()
    test_drift_injected_status_key_fails()
    print("OK: 5 tasks mirror tests pass")


if __name__ == "__main__":
    main()
