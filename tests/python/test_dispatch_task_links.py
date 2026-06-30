#!/usr/bin/env python3
"""Regression tests for dispatch --task task/bug linkage."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("dispatch task-link tests launch POSIX workers")

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
TASK = ROOT / "goalflight_task.py"
NODE = shutil.which("node")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    return env


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


def _load_items(project: Path) -> list[dict]:
    path = project / "docs-private" / "tasks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def case_dispatch_task_ids_update_ledger_and_breadcrumbs() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        project = tmp / "project"
        project.mkdir()
        task = _run_task(project, env, "new", "Linked task").stdout.strip()
        bug = _run_task(project, env, "new", "--kind", "bug", "Linked bug").stdout.strip()
        assert_true("task id allocated", task == "t-001")
        assert_true("bug id allocated", bug == "b-001")

        worker_code = (
            "import time; "
            "print('STATUS: linked worker running', flush=True); "
            "time.sleep(0.2); "
            "print('COMPLETE: linked worker done', flush=True)"
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                "task-link",
                "--cwd",
                str(project),
                "--task",
                f"{task},{bug}",
                "--tail",
                str(tmp / "task-link.tail"),
                "--status-json",
                str(tmp / "task-link.status.json"),
                "--poll-secs",
                "0.2",
                "--max-idle-secs",
                "10",
                "--foreground",
                "--",
                sys.executable,
                "-c",
                worker_code,
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        assert_true(f"dispatch exits 0: {proc.stderr}\n{proc.stdout}", proc.returncode == 0)

        record = json.loads((tmp / "state" / "runs.d" / "task-link.json").read_text(encoding="utf-8"))
        assert_true("ledger records plural task_ids", record.get("task_ids") == [task, bug])
        assert_true("ledger omits singular task_id", "task_id" not in record)

        by_id = {item["id"]: item for item in _load_items(project)}
        for item_id in (task, bug):
            dispatches = by_id[item_id].get("dispatches")
            assert_true(f"{item_id} dispatches is list", isinstance(dispatches, list))
            states = [entry.get("state") for entry in dispatches]
            assert_true(f"{item_id} got working breadcrumb", "working" in states)
            assert_true(f"{item_id} got finished breadcrumb", states[-1] == "worker-finished")
            final = dispatches[-1]
            assert_true(f"{item_id} final dispatch id", final.get("dispatch_id") == "task-link")
            assert_true(f"{item_id} final marker", final.get("marker", {}).get("kind") == "COMPLETE")
            assert_true(f"{item_id} final snapshot", isinstance(final.get("last_worker_state"), dict))

        status = _run_task(project, env, "status", "--json")
        assert_true(f"status exits 0: {status.stderr}", status.returncode == 0)
        payload = json.loads(status.stdout)
        statuses = {item["id"]: item["derived_status"] for item in payload["items"]}
        assert_true("task derived awaiting-review", statuses[task] == "awaiting-review")
        assert_true("bug derived awaiting-review", statuses[bug] == "awaiting-review")


def main() -> None:
    if not NODE:
        print("SKIP: test_dispatch_task_links.py: node not found on PATH")
        return
    case_dispatch_task_ids_update_ledger_and_breadcrumbs()
    print("OK: dispatch task-link tests pass")


if __name__ == "__main__":
    main()
