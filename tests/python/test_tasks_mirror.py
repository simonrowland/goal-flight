#!/usr/bin/env python3
"""Hermetic test for the tasks.jsonl <-> tasks-data.js mirror checker.

Drives scripts/check_tasks_mirror.js (node-only; no network, no localhost):
  - PASS on the tracked known-good fixture templates/state-skeleton/.
  - FAIL on planted-drift temp copies (changed field / added id / stray status).

If node is unavailable, the whole file SKIPs (runner-recognized "SKIP:" prefix).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_tasks_mirror.js"
FIXTURE = ROOT / "templates" / "state-skeleton"
TASK = ROOT / "goalflight_task.py"
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


def run_task(project_root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, str(TASK), "--project-root", str(project_root), *args],
        cwd=str(ROOT),
        env=merged_env,
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


def _write_tasks(project_root: Path, items: list[dict]) -> None:
    docs = project_root / "docs-private"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "tasks.jsonl").write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in items),
        encoding="utf-8",
    )
    (docs / "tasks-data.js").write_text(
        "window.GF_ITEMS = " + json.dumps(items, indent=2) + ";\n",
        encoding="utf-8",
    )


def _load_goalflight_task_module():
    spec = importlib.util.spec_from_file_location("goalflight_task", TASK)
    assert_true("goalflight_task.py import spec", spec is not None and spec.loader is not None)
    module = importlib.util.module_from_spec(spec)
    sys.modules["goalflight_task"] = module
    spec.loader.exec_module(module)
    return module


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


def test_goalflight_task_new_allocator_concurrency() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        procs = [
            subprocess.Popen(
                [sys.executable, str(TASK), "--project-root", str(project), "new", f"Concurrent task {i}"],
                cwd=str(ROOT),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for i in range(2)
        ]
        finished = [proc.communicate(timeout=30) + (proc.returncode,) for proc in procs]

        ids = []
        for stdout, stderr, returncode in finished:
            assert_true(f"new exits 0: {stderr}", returncode == 0)
            ids.append(stdout.strip())
        assert_true("two ids allocated", len(ids) == 2)
        assert_true("ids do not collide", len(set(ids)) == 2)
        assert_true("ids use task family", all(item.startswith("t-") for item in ids))

        proc = run_checker(project / "docs-private")
        assert_true("concurrent allocator mirror valid", proc.returncode == 0)


def test_goalflight_task_status_uses_breadcrumb_when_ledger_missing() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project-a"
        state_dir = Path(td) / "state"
        item = {
            "id": "t-001",
            "kind": "task",
            "title": "Finished worker with reaped ledger",
            "blocked_by": [],
            "links": [],
            "done": False,
            "dispatches": [
                {
                    "dispatch_id": "codex-old",
                    "state": "complete",
                    "terminal_state": "complete",
                    "ended_at": "2026-06-01T00:00:00+00:00",
                }
            ],
        }
        _write_tasks(project, [item])

        proc = run_task(project, "status", "--json", env={"GOALFLIGHT_STATE_DIR": str(state_dir)})
        assert_true(f"status exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("breadcrumb fallback worker-finished", payload["items"][0]["derived_status"] == "worker-finished")


def test_goalflight_task_status_filters_ledger_by_project_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project_a = base / "project-a"
        project_b = base / "project-b"
        state_dir = base / "state"
        _write_tasks(
            project_a,
            [
                {
                    "id": "t-001",
                    "kind": "task",
                    "title": "Project A task",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                }
            ],
        )
        runs = state_dir / "runs.d"
        runs.mkdir(parents=True)
        (runs / "foreign.json").write_text(
            json.dumps(
                {
                    "schema": "goalflight.dispatch.v1",
                    "dispatch_id": "foreign",
                    "task_id": "t-001",
                    "project_root": str(project_b),
                    "state": "complete",
                    "terminal_state": "complete",
                    "started_at": "2026-06-01T00:00:00+00:00",
                    "ended_at": "2026-06-01T00:01:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        proc = run_task(project_a, "status", "--json", env={"GOALFLIGHT_STATE_DIR": str(state_dir)})
        assert_true(f"status exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("foreign ledger row ignored", payload["items"][0]["derived_status"] == "pending")


def test_goalflight_task_atomic_write_rejects_bad_content() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        proc = run_task(project, "new", "Good task")
        assert_true(f"seed new exits 0: {proc.stderr}", proc.returncode == 0)
        docs = project / "docs-private"
        before_jsonl = (docs / "tasks.jsonl").read_text(encoding="utf-8")
        before_data = (docs / "tasks-data.js").read_text(encoding="utf-8")

        module = _load_goalflight_task_module()
        store = module.TaskStore(project)

        def inject_bad(items: list[dict]) -> None:
            items.append(
                {
                    "id": "t-999",
                    "kind": "task",
                    "title": "Bad stored status",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                    "status": "pending",
                }
            )

        try:
            store.mutate_items(inject_bad)
        except module.TaskError as exc:
            assert_true("bad write reports file line", "tasks.jsonl: line" in str(exc))
        else:
            raise AssertionError("bad write unexpectedly succeeded")

        assert_true("tasks.jsonl unchanged after rejected write", (docs / "tasks.jsonl").read_text(encoding="utf-8") == before_jsonl)
        assert_true("tasks-data.js unchanged after rejected write", (docs / "tasks-data.js").read_text(encoding="utf-8") == before_data)
        proc = run_checker(docs)
        assert_true("live pair still valid", proc.returncode == 0)


def test_goalflight_task_sync_repairs_stale_mirror() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        item = {
            "id": "t-001",
            "kind": "task",
            "title": "Canonical task title",
            "blocked_by": [],
            "links": [],
            "done": False,
        }
        _write_tasks(project, [item])
        docs = project / "docs-private"
        stale = dict(item)
        stale["title"] = "Stale mirror title"
        (docs / "tasks-data.js").write_text("window.GF_ITEMS = " + json.dumps([stale], indent=2) + ";\n", encoding="utf-8")

        proc = run_task(project, "sync", "--by", "watcher")
        assert_true(f"sync exits 0: {proc.stderr}", proc.returncode == 0)
        proc = run_checker(docs)
        assert_true("sync repaired mirror", proc.returncode == 0)


def main() -> None:
    if not NODE:
        print("SKIP: test_tasks_mirror.py: node not found on PATH")
        return
    if not CHECKER.is_file():
        raise AssertionError(f"checker missing: {CHECKER}")
    if not TASK.is_file():
        raise AssertionError(f"task helper missing: {TASK}")

    test_default_fixture_passes()
    test_explicit_fixture_passes()
    test_drift_changed_field_fails()
    test_drift_added_id_fails()
    test_drift_injected_status_key_fails()
    test_goalflight_task_new_allocator_concurrency()
    test_goalflight_task_status_uses_breadcrumb_when_ledger_missing()
    test_goalflight_task_status_filters_ledger_by_project_root()
    test_goalflight_task_atomic_write_rejects_bad_content()
    test_goalflight_task_sync_repairs_stale_mirror()
    print("OK: 10 tasks mirror/task-store tests pass")


if __name__ == "__main__":
    main()
