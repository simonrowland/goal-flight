#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SESSION_STATUS = ROOT / "scripts" / "goalflight_session_status.py"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def write_tasks(project: Path, items: list[dict]) -> None:
    docs = project / "docs-private"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "tasks.jsonl").write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in items),
        encoding="utf-8",
    )


def run_session_status(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GOALFLIGHT_MESSAGES_DIR"] = str(project / ".messages")
    return subprocess.run(
        [sys.executable, str(SESSION_STATUS), "--project-root", str(project), *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def message_files(project: Path) -> list[Path]:
    return sorted(project.joinpath(".messages").glob("*.jsonl"))


def write_active_notes(project: Path) -> None:
    docs = project / "docs-private"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "RESUME-NOTES-2026-07-02.md").write_text("---\nstate: active\n---\n", encoding="utf-8")


def test_session_status_prints_nonzero_backlog_counts() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Deferred work",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                    "lane": "deferred",
                },
                {
                    "schema_version": 1,
                    "id": "t-002",
                    "kind": "task",
                    "title": "Held work",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                    "lane": "held",
                },
                {
                    "schema_version": 1,
                    "id": "t-003",
                    "kind": "task",
                    "title": "Blocked work",
                    "blocked_by": ["q-001"],
                    "links": [],
                    "done": False,
                },
                {
                    "schema_version": 1,
                    "id": "q-001",
                    "kind": "decision",
                    "title": "Open decision",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                },
            ],
        )

        proc = run_session_status(project, "--text")
        assert_true(f"session-status text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("nonzero counts printed", "1 deferred · 1 held · 1 blocked" in proc.stdout)

        proc = run_session_status(project, "--json")
        assert_true(f"session-status json exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("json carries structured counts", payload["backlog_counts"] == {"deferred": 1, "held": 1, "blocked": 1})


def test_session_status_silent_when_backlog_counts_zero() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Accepted deferred work",
                    "blocked_by": [],
                    "links": [],
                    "done": True,
                    "done_reviewed": True,
                    "lane": "deferred",
                }
            ],
        )

        proc = run_session_status(project, "--text")
        assert_true(f"session-status text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("zero counts stay silent", "deferred" not in proc.stdout and "held" not in proc.stdout and "blocked" not in proc.stdout)


def test_session_status_omits_zero_backlog_buckets() -> None:
    # P2 lock: when only one bucket is non-zero, do NOT print the zero buckets
    # (e.g. avoid "1 deferred · 0 held · 0 blocked").
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Only deferred work",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                    "lane": "deferred",
                }
            ],
        )
        proc = run_session_status(project, "--text")
        assert_true(f"session-status text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("singleton prints the non-zero bucket", "1 deferred" in proc.stdout)
        assert_true("singleton omits the zero held bucket", "0 held" not in proc.stdout)
        assert_true("singleton omits the zero blocked bucket", "0 blocked" not in proc.stdout)


def test_session_status_does_not_double_count_reserved_blocked_items() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Deferred but blocked",
                    "blocked_by": ["q-001"],
                    "links": [],
                    "done": False,
                    "lane": "deferred",
                },
                {
                    "schema_version": 1,
                    "id": "q-001",
                    "kind": "decision",
                    "title": "Open decision",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                },
            ],
        )

        proc = run_session_status(project, "--json")
        assert_true(f"session-status json exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("reserved blocked item counted only as deferred", payload["backlog_counts"] == {"deferred": 1, "held": 0, "blocked": 0})

        proc = run_session_status(project, "--text")
        assert_true(f"session-status text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("text shows deferred", "1 deferred" in proc.stdout)
        assert_true("text does not double-count blocked", "blocked" not in proc.stdout)


def test_session_status_degrades_present_unreadable_store() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        docs = project / "docs-private"
        docs.mkdir(parents=True)
        (docs / "tasks.jsonl").write_text("{not valid json\n", encoding="utf-8")

        proc = run_session_status(project, "--json")
        assert_true(f"session-status json exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("corrupt store has null backlog counts", payload["backlog_counts"] is None)
        assert_true("corrupt store carries backlog error", "tasks.jsonl" in payload.get("backlog_error", ""))

        proc = run_session_status(project, "--text")
        assert_true(f"session-status text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("text reports degraded backlog", "backlog: store read degraded" in proc.stdout)
        assert_true("text does not report silent zero buckets", "0 deferred" not in proc.stdout and "0 held" not in proc.stdout and "0 blocked" not in proc.stdout)


def test_session_status_resume_directive_active_ready_only() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        write_active_notes(project)
        write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Ready work",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                }
            ],
        )

        proc = run_session_status(project, "--text")
        assert_true(f"session-status text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true(
            "active ready text carries resume directive",
            "resume: run python3 goalflight_task.py next -> continue the top task (t-001 Ready work)" in proc.stdout,
        )
        assert_true("active ready posts resume nudge", len(message_files(project)) == 1)

        proc = run_session_status(project, "--json")
        assert_true(f"session-status json exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("json carries ready frontier count", payload["ready_frontier"]["count"] == 1)
        assert_true("json carries ready top id", payload["ready_frontier"]["top_id"] == "t-001")


def test_session_status_resume_directive_prints_when_inactive_ready() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Ready but inactive",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                }
            ],
        )
        proc = run_session_status(project, "--text")
        assert_true(f"inactive text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true(
            "inactive ready carries resume directive",
            "resume: run python3 goalflight_task.py next -> continue the top task (t-001 Ready but inactive)" in proc.stdout,
        )
        assert_true("inactive ready does not post resume nudge", message_files(project) == [])

    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        write_active_notes(project)
        write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Done work",
                    "blocked_by": [],
                    "links": [],
                    "done": True,
                    "done_reviewed": True,
                }
            ],
        )
        proc = run_session_status(project, "--text")
        assert_true(f"active empty text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("active empty frontier has no directive", "resume:" not in proc.stdout)


def main() -> None:
    test_session_status_prints_nonzero_backlog_counts()
    test_session_status_silent_when_backlog_counts_zero()
    test_session_status_omits_zero_backlog_buckets()
    test_session_status_does_not_double_count_reserved_blocked_items()
    test_session_status_degrades_present_unreadable_store()
    test_session_status_resume_directive_active_ready_only()
    test_session_status_resume_directive_prints_when_inactive_ready()
    print("OK: 7 session-status backlog count tests pass")


if __name__ == "__main__":
    main()
