#!/usr/bin/env python3
"""Focused tests for the `capture` verb (reflex-create with deferred defaults + stderr hint)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "goalflight_task.py"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def run_task(project_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TASK), "--project-root", str(project_root), *args],
        cwd=str(ROOT),
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _new_project(td: str) -> Path:
    project = Path(td)
    (project / "docs-private").mkdir(parents=True)
    return project


def _show(project: Path, item_id: str) -> dict:
    proc = run_task(project, "show", item_id, "--json")
    assert_true(f"show {item_id} ok: {proc.stderr}", proc.returncode == 0)
    return json.loads(proc.stdout)


def test_capture_reflex_defaults() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        proc = run_task(project, "capture", "Investigate flaky test")
        assert_true(f"capture ok: {proc.stderr}", proc.returncode == 0)
        item_id = proc.stdout.strip()
        assert_true("capture prints a bare id on stdout", item_id.startswith("t-"))
        item = _show(project, item_id)
        assert_true("capture defaults kind=task", item.get("kind") == "task")
        assert_true("capture defaults lane=deferred", item.get("lane") == "deferred")
        assert_true("capture sets source=cwd", item.get("source") == str(ROOT))


def test_capture_severity_implies_bug() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        proc = run_task(project, "capture", "Race in retry path", "--severity", "P2")
        assert_true(f"capture --severity ok: {proc.stderr}", proc.returncode == 0)
        item = _show(project, proc.stdout.strip())
        assert_true("--severity implies kind=bug", item.get("kind") == "bug")
        assert_true("severity stored", item.get("severity") == "P2")
        assert_true("bug id family", item["id"].startswith("b-"))


def test_capture_lane_override() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        proc = run_task(project, "capture", "UI polish", "--lane", "ui")
        assert_true(f"capture --lane ok: {proc.stderr}", proc.returncode == 0)
        item = _show(project, proc.stdout.strip())
        assert_true("explicit --lane overrides deferred default", item.get("lane") == "ui")


def test_capture_hint_on_stderr_stdout_clean() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        # plain form: stdout is the bare id, hint goes to stderr
        proc = run_task(project, "capture", "Something")
        assert_true("next-step hint on stderr", "captured" in proc.stderr and "promote:" in proc.stderr)
        assert_true("stdout carries no hint", "captured" not in proc.stdout)
        assert_true("stdout is a single bare id", proc.stdout.strip().startswith("t-") and "\n" not in proc.stdout.strip())
        # --json form: stdout is clean parseable JSON, hint still on stderr
        proc = run_task(project, "capture", "Another", "--json")
        payload = json.loads(proc.stdout)
        assert_true("--json stdout is clean {id: ...}", isinstance(payload, dict) and "id" in payload)
        assert_true("--json still emits stderr hint", "captured" in proc.stderr)


def test_capture_no_status_key() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        run_task(project, "capture", "one")
        run_task(project, "capture", "two", "--severity", "P1")
        raw = (project / "docs-private" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
        items = [json.loads(line) for line in raw if line.strip()]
        assert_true("captured items exist", len(items) == 2)
        assert_true("no forbidden status key", all("status" not in item for item in items))


def test_capture_explicit_kind_task_beats_severity() -> None:
    # Adversarial: an EXPLICIT --kind must win over the --severity->bug reflex.
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        proc = run_task(project, "capture", "Explicit task", "--kind", "task", "--severity", "P2")
        assert_true(f"capture --kind task --severity ok: {proc.stderr}", proc.returncode == 0)
        item_id = proc.stdout.strip()
        item = _show(project, item_id)
        assert_true("explicit --kind task wins over severity reflex", item.get("kind") == "task")
        assert_true("explicit task keeps a t- id", item_id.startswith("t-"))
        assert_true("severity still stored on the task", item.get("severity") == "P2")


def main() -> None:
    test_capture_reflex_defaults()
    test_capture_severity_implies_bug()
    test_capture_lane_override()
    test_capture_hint_on_stderr_stdout_clean()
    test_capture_no_status_key()
    test_capture_explicit_kind_task_beats_severity()
    print("OK: capture verb tests pass")


if __name__ == "__main__":
    main()
