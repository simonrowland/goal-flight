#!/usr/bin/env python3
"""Focused tests for task-store `next` frontier and parallel nudge."""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "goalflight_task.py"
CHECKER = ROOT / "scripts" / "check_tasks_mirror.js"
NODE = shutil.which("node")
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import goalflight_messages as M  # noqa: E402
import goalflight_task as T  # noqa: E402


def assert_eq(name: str, got: object, exp: object) -> None:
    if got != exp:
        raise AssertionError(f"{name}: got {got!r}, expected {exp!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _item(item_id: str, title: str, *, kind: str = "task", **extra) -> dict:
    item = {
        "schema_version": 1,
        "id": item_id,
        "kind": kind,
        "title": title,
        "blocked_by": [],
        "links": [],
        "done": False,
        "created_at": "2026-07-01T00:00:00+00:00",
        "created_by": "test",
    }
    item.update(extra)
    return item


def _write_items(project: Path, items: list[dict]) -> None:
    docs = project / "docs-private"
    docs.mkdir(parents=True, exist_ok=True)
    docs.joinpath("tasks.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in items),
        encoding="utf-8",
    )


def _run_task(project: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TASK), "--project-root", str(project), *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    return env


def _frontier_fixture() -> list[dict]:
    return [
        _item("q-001", "Reviewed gate", kind="decision", done=True, done_reviewed=True),
        _item("q-002", "Open gate", kind="decision"),
        _item("t-001", "Ready task"),
        _item("b-001", "Ready bug", kind="bug"),
        _item("t-002", "Blocked by reviewed gate", blocked_by=["q-001"]),
        _item("t-003", "Blocked by open gate", blocked_by=["q-002"]),
        _item("t-004", "Awaiting review", done=True),
        _item("t-005", "Done reviewed", done=True, done_reviewed=True),
        _item("t-006", "Delegated", dispatches=[{"dispatch_id": "d-1", "state": "working", "ts": "2026-07-01T00:01:00+00:00"}]),
        _item("q-003", "Decision is not dispatchable", kind="decision"),
    ]


def test_next_frontier_filters_to_dispatchable_level_zero() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        _write_items(project, _frontier_fixture())
        proc = _run_task(project, _env(tmp), "next")
        assert_true(f"next exits 0: {proc.stderr}", proc.returncode == 0)
        assert_eq(
            "plain frontier id title rows",
            proc.stdout.splitlines(),
            [
                "t-001 Ready task",
                "b-001 Ready bug",
                "t-002 Blocked by reviewed gate",
            ],
        )


def test_next_json_shape_and_no_status_key() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        _write_items(project, _frontier_fixture())
        proc = _run_task(project, _env(tmp), "next", "--json")
        assert_true(f"next --json exits 0: {proc.stderr}", proc.returncode == 0)
        rows = json.loads(proc.stdout)
        assert_eq("json frontier ids", [row["id"] for row in rows], ["t-001", "b-001", "t-002"])
        assert_true("derived_status exposed", all(row.get("derived_status") == "pending" for row in rows))
        assert_true("no durable status key", all("status" not in row for row in rows))


def test_parallel_nudge_posts_once_for_same_frontier() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [_item("t-001", "A"), _item("t-002", "B")])
        for _ in range(2):
            proc = _run_task(project, env, "next")
            assert_true(f"next exits 0: {proc.stderr}", proc.returncode == 0)
        inbox = M.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), T.NEXT_NUDGE_DISPATCH_ID)
        envelopes = M.read_envelopes(inbox)
        assert_eq("deduped nudge count", len(envelopes), 1)
        payload = envelopes[0]["payload"]
        assert_eq("nudge type", envelopes[0]["type"], "user_need")
        assert_eq("frontier ids sorted", payload["frontier_ids"], ["t-001", "t-002"])
        assert_true("nudge text asks fan out", "2 parallel-ready (t-001, t-002) -> fan out?" in payload["text"])


def test_parallel_nudge_silent_for_single_frontier() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [_item("t-001", "Solo")])
        proc = _run_task(project, env, "next")
        assert_true(f"next exits 0: {proc.stderr}", proc.returncode == 0)
        inbox = M.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), T.NEXT_NUDGE_DISPATCH_ID)
        assert_true("single frontier creates no inbox", not inbox.exists())


def test_next_still_prints_when_mail_import_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        _write_items(project, [_item("t-001", "A"), _item("t-002", "B")])
        store = T.TaskStore(project)
        args = SimpleNamespace(json=False)
        real_import = builtins.__import__

        def fail_mail(name, *args, **kwargs):
            if name == "goalflight_messages":
                raise ImportError("mail unavailable")
            return real_import(name, *args, **kwargs)

        out = io.StringIO()
        builtins.__import__ = fail_mail
        try:
            with contextlib.redirect_stdout(out):
                rc = T._cmd_next(store, args)
        finally:
            builtins.__import__ = real_import
        assert_eq("next return code", rc, 0)
        assert_eq("frontier still printed", out.getvalue().splitlines(), ["t-001 A", "t-002 B"])


def test_check_tasks_mirror_accepts_next_fixture_without_status() -> None:
    if not NODE:
        print("SKIP: test_next_frontier.py mirror checker: node not found on PATH")
        return
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        store = T.TaskStore(project)
        store.save_items_atomic(_frontier_fixture())
        proc = subprocess.run(
            [NODE, str(CHECKER), str(project / "docs-private")],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        assert_true(f"mirror checker exits 0: {proc.stderr}", proc.returncode == 0)
        raw_items = [
            json.loads(line)
            for line in (project / "docs-private" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        data_js = (project / "docs-private" / "tasks-data.js").read_text(encoding="utf-8")
        payload = data_js.split("window.GF_ITEMS = ", 1)[1].split(";\nif (typeof module", 1)[0]
        mirrored = json.loads(payload)
        assert_true("tasks.jsonl has no status key", all("status" not in item for item in raw_items))
        assert_true("tasks-data.js has no status key", all("status" not in item for item in mirrored))


def main() -> None:
    test_next_frontier_filters_to_dispatchable_level_zero()
    test_next_json_shape_and_no_status_key()
    test_parallel_nudge_posts_once_for_same_frontier()
    test_parallel_nudge_silent_for_single_frontier()
    test_next_still_prints_when_mail_import_fails()
    test_check_tasks_mirror_accepts_next_fixture_without_status()
    print("OK: next frontier tests pass")


if __name__ == "__main__":
    main()
