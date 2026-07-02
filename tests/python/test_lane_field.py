#!/usr/bin/env python3
"""Focused tests for task-store lanes and reserved-lane backlog rendering."""

from __future__ import annotations

import importlib.util
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


def run_checker(project_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, str(CHECKER), str(project_root / "docs-private"), str(project_root / "dashboard")],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def load_goalflight_task_module():
    spec = importlib.util.spec_from_file_location("goalflight_task", TASK)
    assert_true("goalflight_task.py import spec", spec is not None and spec.loader is not None)
    module = importlib.util.module_from_spec(spec)
    sys.modules["goalflight_task"] = module
    spec.loader.exec_module(module)
    return module


def read_items(project_root: Path) -> list[dict]:
    path = project_root / "docs-private" / "tasks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_data_js_items(project_root: Path) -> list[dict]:
    text = (project_root / "dashboard" / "tasks-data.js").read_text(encoding="utf-8")
    prefix = "window.GF_ITEMS = "
    start = text.index(prefix) + len(prefix)
    end = text.index(";\nif (typeof module", start)
    return json.loads(text[start:end])


def test_lane_validates_as_string() -> None:
    module = load_goalflight_task_module()
    bad = [{"id": "t-001", "kind": "task", "title": "bad lane", "lane": 7}]
    try:
        module._validate_items_for_write(bad, source="unit")
    except module.TaskError as exc:
        assert_true("write validator names lane string contract", "lane must be a string" in str(exc))
    else:
        raise AssertionError("write validator accepted non-string lane")

    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        docs = project / "docs-private"
        docs.mkdir(parents=True)
        (docs / "tasks.jsonl").write_text(
            json.dumps({"id": "t-001", "kind": "task", "title": "bad lane", "lane": 7}) + "\n",
            encoding="utf-8",
        )
        proc = run_task(project, "status")
        assert_true("read validator rejects non-string lane", proc.returncode == 1)
        assert_true("read validator error mentions lane", "lane must be a string" in proc.stderr)


def test_lane_cli_and_reserved_backlog_view() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)

        proc = run_task(project, "new", "Deferred task", "--lane", "deferred", "--by", "tester")
        assert_true(f"new --lane deferred exits 0: {proc.stderr}", proc.returncode == 0)
        deferred_id = proc.stdout.strip()

        proc = run_task(project, "new", "Held task", "--by", "tester")
        assert_true(f"new held candidate exits 0: {proc.stderr}", proc.returncode == 0)
        held_id = proc.stdout.strip()

        proc = run_task(project, "lane", held_id, "held", "--by", "tester")
        assert_true(f"lane <id> held exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("lane success echoed to stderr", f"{held_id} -> lane held" in proc.stderr)

        proc = run_task(project, "new", "Feature lane task", "--lane", "release", "--by", "tester")
        assert_true(f"new --lane release exits 0: {proc.stderr}", proc.returncode == 0)
        release_id = proc.stdout.strip()

        docs = project / "docs-private"
        items = read_items(project)
        by_id = {item["id"]: item for item in items}
        assert_true("new --lane deferred stores lane", by_id[deferred_id]["lane"] == "deferred")
        assert_true("lane verb stores held lane", by_id[held_id]["lane"] == "held")
        assert_true("free-text lane stored", by_id[release_id]["lane"] == "release")
        assert_true(
            "lane mutation audit stamped",
            any(
                entry.get("action") == "lane"
                and entry.get("actor") == "tester"
                and entry.get("lane") == "held"
                for entry in by_id[held_id].get("audit", [])
            ),
        )

        task_md = (docs / "task-decomposition.md").read_text(encoding="utf-8")
        assert_true("backlog section rendered", "## Backlog" in task_md)
        active, backlog = task_md.split("## Backlog", 1)
        assert_true("deferred renders in backlog", f"### {deferred_id}" in backlog)
        assert_true("held renders in backlog", f"### {held_id}" in backlog)
        assert_true("free-text lane excluded from reserved backlog", f"### {release_id}" not in backlog)
        assert_true("free-text lane remains active", f"### {release_id}" in active)
        assert_true("deferred excluded from active sections", f"### {deferred_id}" not in active)
        assert_true("held excluded from active sections", f"### {held_id}" not in active)

        proc = run_checker(project)
        assert_true(f"mirror checker accepts lane: {proc.stderr}", proc.returncode == 0)

        data_items = read_data_js_items(project)
        assert_true("tasks.jsonl has no status key", all("status" not in item for item in items))
        assert_true("tasks-data.js has no status key", all("status" not in item for item in data_items))


def test_lane_rejects_reserved_near_miss_but_allows_distinct_free_text() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        proc = run_task(project, "new", "Park me", "--lane", "deferred", "--by", "tester")
        assert_true(f"new deferred exits 0: {proc.stderr}", proc.returncode == 0)
        item_id = proc.stdout.strip()

        proc = run_task(project, "lane", item_id, "hield", "--by", "tester")
        assert_true("near-miss lane exits nonzero", proc.returncode != 0)
        assert_true("near-miss hint names held", "did you mean 'held'?" in proc.stderr)
        item = read_items(project)[0]
        assert_true("near-miss did not unpark item", item.get("lane") == "deferred")

        proc = run_task(project, "lane", item_id, "Held", "--by", "tester")
        assert_true("case-variant reserved lane exits nonzero", proc.returncode != 0)
        assert_true("case-variant hint names held", "did you mean 'held'?" in proc.stderr)
        item = read_items(project)[0]
        assert_true("case-variant did not unpark item", item.get("lane") == "deferred")

        for lane in ("help", "hold", "ui"):
            proc = run_task(project, "lane", item_id, lane, "--by", "tester")
            assert_true(f"distinct free-text lane {lane} exits 0: {proc.stderr}", proc.returncode == 0)
            assert_true(f"distinct free-text lane {lane} echoed", f"{item_id} -> lane {lane}" in proc.stderr)
            item = read_items(project)[0]
            assert_true(f"distinct free-text lane {lane} stored", item.get("lane") == lane)

        proc = run_task(project, "new", "Bad create lane", "--lane", "hield", "--by", "tester")
        assert_true("create near-miss lane exits nonzero", proc.returncode != 0)
        assert_true("create near-miss hint names held", "did you mean 'held'?" in proc.stderr)

        proc = run_task(project, "capture", "Bad capture lane", "--lane", "hield", "--by", "tester")
        assert_true("capture near-miss lane exits nonzero", proc.returncode != 0)
        assert_true("capture near-miss hint names held", "did you mean 'held'?" in proc.stderr)


def main() -> None:
    if not NODE:
        print("SKIP: test_lane_field.py: node not found on PATH")
        return
    test_lane_validates_as_string()
    test_lane_cli_and_reserved_backlog_view()
    test_lane_rejects_reserved_near_miss_but_allows_distinct_free_text()
    print("OK: lane field tests pass")


if __name__ == "__main__":
    main()
