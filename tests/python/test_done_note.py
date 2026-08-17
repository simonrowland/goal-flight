#!/usr/bin/env python3
"""Focused tests for goalflight_task.py done --note and note-less done."""

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


def read_items(project_root: Path) -> list[dict]:
    path = project_root / "docs-private" / "tasks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_done_without_note_still_closes() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        item_id = run_task(project, "new", "Close me", "--by", "tester").stdout.strip()
        proc = run_task(project, "done", item_id, "--by", "tester")
        assert_true(f"note-less done exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("note-less done prints the id", proc.stdout.strip() == item_id)
        item = read_items(project)[0]
        assert_true("note-less done marks done", item.get("done") is True)
        assert_true("note-less done does not invent notes", "notes" not in item)
        assert_true(
            "note-less done audit is only done",
            [entry.get("action") for entry in item.get("audit", []) if entry.get("action") in {"append", "done"}]
            == ["done"],
        )


def test_done_note_appends_then_closes() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        item_id = run_task(project, "new", "Close with note", "--by", "tester").stdout.strip()
        proc = run_task(project, "done", item_id, "--note", "shipped in 588711a", "--by", "tester")
        assert_true(f"done --note exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("done --note prints the id", proc.stdout.strip() == item_id)
        item = read_items(project)[0]
        assert_true("done --note marks done", item.get("done") is True)
        notes = item.get("notes")
        assert_true("done --note stores a notes list", isinstance(notes, list) and len(notes) == 1)
        assert_true("done --note text matches", notes[0].get("text") == "shipped in 588711a")
        assert_true("done --note stamps actor", notes[0].get("actor") == "tester")
        assert_true("done --note stamps at", bool(notes[0].get("at")))
        actions = [entry.get("action") for entry in item.get("audit", []) if entry.get("action") in {"append", "done"}]
        assert_true("done --note appends before done in audit", actions == ["append", "done"])


def test_done_note_keeps_resolution_and_rejects_already_done() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        item_id = run_task(project, "new", "Already closing", "--by", "tester").stdout.strip()
        first = run_task(
            project,
            "done",
            item_id,
            "--resolution",
            "worker-complete",
            "--note",
            "first close",
            "--by",
            "tester",
        )
        assert_true(f"first done --note ok: {first.stderr}", first.returncode == 0)
        item = read_items(project)[0]
        assert_true("resolution still stored", item.get("resolution") == "worker-complete")
        second = run_task(project, "done", item_id, "--note", "second close", "--by", "tester")
        assert_true("already-done still rejected", second.returncode != 0)
        assert_true("already-done names the id", f"{item_id}: already done" in second.stderr)
        item = read_items(project)[0]
        notes = item.get("notes") or []
        assert_true("rejected second note does not append", [note.get("text") for note in notes] == ["first close"])


def test_done_note_matches_append_shape() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        item_id = run_task(project, "new", "Shape check", "--by", "tester").stdout.strip()
        append = run_task(project, "append", item_id, "via append", "--by", "tester")
        assert_true(f"append setup ok: {append.stderr}", append.returncode == 0)
        done = run_task(project, "done", item_id, "--note", "via done", "--by", "tester")
        assert_true(f"done --note after append ok: {done.stderr}", done.returncode == 0)
        notes = read_items(project)[0].get("notes") or []
        assert_true("both notes present", [note.get("text") for note in notes] == ["via append", "via done"])
        for note in notes:
            assert_true("note has at/actor/text", set(note) >= {"at", "actor", "text"})
        if NODE:
            proc = subprocess.run(
                [NODE, str(CHECKER), str(project / "docs-private"), str(project / "dashboard")],
                cwd=str(ROOT),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            assert_true(f"mirror checker accepts done notes: {proc.stderr}", proc.returncode == 0)


def main() -> None:
    test_done_without_note_still_closes()
    test_done_note_appends_then_closes()
    test_done_note_keeps_resolution_and_rejects_already_done()
    test_done_note_matches_append_shape()
    print("OK: done --note tests pass")


if __name__ == "__main__":
    main()
