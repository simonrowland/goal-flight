#!/usr/bin/env python3
"""Focused tests for goalflight_task.py append."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "goalflight_task.py"
CHECKER = ROOT / "scripts" / "check_tasks_mirror.js"
NODE = shutil.which("node")


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


def read_items(project_root: Path) -> list[dict]:
    path = project_root / "docs-private" / "tasks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_data_js_items(project_root: Path) -> list[dict]:
    text = (project_root / "docs-private" / "tasks-data.js").read_text(encoding="utf-8")
    prefix = "window.GF_ITEMS = "
    start = text.index(prefix) + len(prefix)
    end = text.index(";\nif (typeof module", start)
    return json.loads(text[start:end])


def test_append_single_and_batch() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        first = run_task(project, "new", "First task", "--by", "tester").stdout.strip()
        second = run_task(project, "new", "Second task", "--by", "tester").stdout.strip()

        proc = run_task(project, "append", first, "single note", "--by", "tester")
        assert_true(f"single append exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("single append stdout names id", proc.stdout.strip() == first)
        assert_true("single append stderr hint", "appended note to 1 item(s)" in proc.stderr)

        proc = run_task(project, "append", f"{first},{second}", "batch note", "--by", "tester", "--json")
        assert_true(f"batch append exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("json append keeps stderr clean", proc.stderr == "")
        payload = json.loads(proc.stdout)
        assert_true("batch append json ids", payload["items"] == [first, second])

        by_id = {item["id"]: item for item in read_items(project)}
        first_notes = by_id[first].get("notes")
        second_notes = by_id[second].get("notes")
        assert_true("first notes is list", isinstance(first_notes, list))
        assert_true("second notes is list", isinstance(second_notes, list))
        assert_true("first got both notes", [note.get("text") for note in first_notes] == ["single note", "batch note"])
        assert_true("second got batch note", [note.get("text") for note in second_notes] == ["batch note"])
        for item_id in (first, second):
            item = by_id[item_id]
            assert_true(
                f"{item_id} append audit stamped",
                any(entry.get("action") == "append" and entry.get("actor") == "tester" for entry in item.get("audit", [])),
            )

        docs = project / "docs-private"
        proc = run_checker(docs)
        assert_true(f"mirror checker accepts notes: {proc.stderr}", proc.returncode == 0)
        data_items = read_data_js_items(project)
        assert_true("tasks.jsonl has no status key", all("status" not in item for item in by_id.values()))
        assert_true("tasks-data.js has no status key", all("status" not in item for item in data_items))


def test_append_unknown_id_is_atomic() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        item_id = run_task(project, "new", "Only task", "--by", "tester").stdout.strip()
        proc = run_task(project, "append", f"{item_id},t-999", "should not land", "--by", "tester")
        assert_true("unknown append exits nonzero", proc.returncode != 0)
        assert_true("unknown append names missing id", "item not found: t-999" in proc.stderr)
        item = read_items(project)[0]
        assert_true("unknown append did not mutate existing item", "notes" not in item)


def main() -> None:
    if not NODE:
        print("SKIP: test_append_verb.py: node not found on PATH")
        return
    test_append_single_and_batch()
    test_append_unknown_id_is_atomic()
    print("OK: append verb tests pass")


if __name__ == "__main__":
    main()
