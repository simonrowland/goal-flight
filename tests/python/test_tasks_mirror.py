#!/usr/bin/env python3
"""Hermetic test for the tasks.jsonl <-> tasks-data.js mirror checker.

Drives scripts/check_tasks_mirror.js (node-only; no network, no localhost):
  - PASS on the tracked known-good fixture templates/state-skeleton/.
  - FAIL on planted-drift temp copies (changed field / added id / stray status).

If node is unavailable, the whole file SKIPs (runner-recognized "SKIP:" prefix).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
    module = _load_goalflight_task_module()
    (docs / "tasks-data.js").write_text(
        module._items_data_js(items),
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
        assert_true("breadcrumb fallback awaiting-review", payload["items"][0]["derived_status"] == "awaiting-review")


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


def test_goalflight_task_status_uses_latest_dispatch_breadcrumb() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project-a"
        state_dir = Path(td) / "state"
        item = {
            "id": "t-001",
            "kind": "task",
            "title": "Retried worker",
            "blocked_by": [],
            "links": [],
            "done": False,
            "dispatches": [
                {
                    "dispatch_id": "codex-old",
                    "state": "worker-finished",
                    "ts": "2026-06-01T00:00:00+00:00",
                },
                {
                    "dispatch_id": "codex-retry",
                    "state": "working",
                    "ts": "2026-06-01T00:10:00+00:00",
                },
            ],
        }
        _write_tasks(project, [item])

        proc = run_task(project, "status", "--json", env={"GOALFLIGHT_STATE_DIR": str(state_dir)})
        assert_true(f"status exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("newest breadcrumb wins", payload["items"][0]["derived_status"] == "working")


def test_goalflight_task_sync_appends_plural_task_ids() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project-a"
        state_dir = base / "state"
        _write_tasks(
            project,
            [
                {
                    "id": "t-001",
                    "kind": "task",
                    "title": "Task dispatch link",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                },
                {
                    "id": "b-001",
                    "kind": "bug",
                    "title": "Bug dispatch link",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                },
            ],
        )
        runs = state_dir / "runs.d"
        runs.mkdir(parents=True)
        (runs / "dispatch-a.json").write_text(
            json.dumps(
                {
                    "schema": "goalflight.dispatch.v1",
                    "dispatch_id": "dispatch-a",
                    "task_ids": ["t-001", "b-001"],
                    "project_root": str(project),
                    "state": "complete",
                    "terminal_state": "complete",
                    "started_at": "2026-06-01T00:00:00+00:00",
                    "ended_at": "2026-06-01T00:01:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        proc = run_task(project, "sync", "--by", "watcher", env={"GOALFLIGHT_STATE_DIR": str(state_dir)})
        assert_true(f"sync exits 0: {proc.stderr}", proc.returncode == 0)
        proc = run_task(project, "sync", "--by", "watcher", env={"GOALFLIGHT_STATE_DIR": str(state_dir)})
        assert_true(f"second sync exits 0: {proc.stderr}", proc.returncode == 0)

        items = [
            json.loads(line)
            for line in (project / "docs-private" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for item in items:
            dispatches = item.get("dispatches")
            assert_true(f"{item['id']} has one idempotent breadcrumb", isinstance(dispatches, list) and len(dispatches) == 1)
            crumb = dispatches[0]
            assert_true(f"{item['id']} breadcrumb state", crumb.get("state") == "worker-finished")
            assert_true(f"{item['id']} breadcrumb snapshot", isinstance(crumb.get("last_worker_state"), dict))


def test_goalflight_task_list_filters_outstanding_awaiting_review_since() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project-a"
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        items = [
            {
                "schema_version": 1,
                "id": "t-001",
                "kind": "task",
                "title": "Open task",
                "blocked_by": [],
                "links": [],
                "done": False,
                "created_at": "2020-01-01T00:00:00+00:00",
            },
            {
                "schema_version": 1,
                "id": "t-002",
                "kind": "task",
                "title": "Worker done",
                "blocked_by": [],
                "links": [],
                "done": True,
                "done_reviewed": False,
                "created_at": "2020-01-01T00:00:00+00:00",
            },
            {
                "schema_version": 1,
                "id": "t-003",
                "kind": "task",
                "title": "Accepted task",
                "blocked_by": [],
                "links": [],
                "done": True,
                "done_reviewed": True,
                "created_at": "2020-01-01T00:00:00+00:00",
            },
            {
                "schema_version": 1,
                "id": "t-004",
                "kind": "task",
                "title": "Recent delegation",
                "blocked_by": [],
                "links": [],
                "done": False,
                "created_at": "2020-01-01T00:00:00+00:00",
                "dispatches": [{"dispatch_id": "recent", "state": "working", "ts": now}],
            },
            {
                "schema_version": 1,
                "id": "b-001",
                "kind": "bug",
                "title": "Blocked bug",
                "blocked_by": ["q-001"],
                "links": [],
                "done": False,
                "created_at": "2020-01-01T00:00:00+00:00",
            },
            {
                "schema_version": 1,
                "id": "t-005",
                "kind": "task",
                "title": "Old delegation with recent review",
                "blocked_by": [],
                "links": [],
                "done": False,
                "created_at": "2020-01-01T00:00:00+00:00",
                "dispatches": [
                    {"dispatch_id": "old", "state": "worker-finished", "ts": "2020-01-01T00:10:00+00:00"},
                    {"dispatch_id": "review-recent", "role": "review", "verdict": "clean", "ts": now},
                ],
            },
            {
                "schema_version": 1,
                "id": "q-001",
                "kind": "decision",
                "title": "Open decision",
                "blocked_by": [],
                "links": [],
                "done": False,
                "created_at": "2020-01-01T00:00:00+00:00",
            },
        ]
        _write_tasks(project, items)

        proc = run_task(project, "list", "outstanding", "--json")
        assert_true(f"list outstanding exits 0: {proc.stderr}", proc.returncode == 0)
        outstanding = {item["id"]: item for item in json.loads(proc.stdout)}
        assert_true("done-reviewed excluded from outstanding", "t-003" not in outstanding)
        assert_true("awaiting review included in outstanding", outstanding["t-002"]["derived_status"] == "awaiting-review")
        assert_true("waiting included in outstanding", outstanding["b-001"]["derived_status"] == "waiting")

        proc = run_task(project, "list", "awaiting-review", "--json")
        assert_true(f"list awaiting-review exits 0: {proc.stderr}", proc.returncode == 0)
        awaiting = json.loads(proc.stdout)
        assert_true("awaiting-review filter", [item["id"] for item in awaiting] == ["t-002", "t-005"])

        proc = run_task(project, "list", "delegated", "--since", "now-3600", "--json")
        assert_true(f"list delegated --since exits 0: {proc.stderr}", proc.returncode == 0)
        delegated = json.loads(proc.stdout)
        assert_true("recent delegated filter", [item["id"] for item in delegated] == ["t-004"])
        assert_true("query_epoch is UTC int seconds", isinstance(delegated[0].get("query_epoch"), int) and delegated[0]["query_epoch"] > 0)

        proc = run_task(project, "list", "--kind", "bug", "--blocked-by", "q-001", "--json")
        assert_true(f"list kind+blocked exits 0: {proc.stderr}", proc.returncode == 0)
        blocked = json.loads(proc.stdout)
        assert_true("kind and blocker filters AND", [item["id"] for item in blocked] == ["b-001"])


def test_goalflight_task_two_state_accept_and_review_breadcrumb() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Awaiting review",
                    "blocked_by": [],
                    "links": [],
                    "done": True,
                    "done_reviewed": False,
                }
            ],
        )

        proc = run_task(project, "accept", "t-001")
        assert_true("accept without review fails", proc.returncode != 0 and "no logged review" in proc.stderr)

        proc = run_task(project, "review", "t-001", "--verdict", "findings", "--dispatch", "review-1", "--findings", "docs-private/reviews/t-001.md")
        assert_true(f"findings review exits 0: {proc.stderr}", proc.returncode == 0)
        proc = run_task(project, "accept", "t-001")
        assert_true("accept with findings review fails", proc.returncode != 0 and "not clean" in proc.stderr)

        proc = run_task(project, "review", "t-001", "--verdict", "clean", "--dispatch", "review-2")
        assert_true(f"clean review exits 0: {proc.stderr}", proc.returncode == 0)
        proc = run_task(project, "accept", "t-001", "--by", "controller")
        assert_true(f"accept exits 0: {proc.stderr}", proc.returncode == 0)

        item = json.loads(run_task(project, "show", "t-001", "--json").stdout)
        assert_true("accept flips done-reviewed", item["done_reviewed"] is True and item["derived_status"] == "done-reviewed")
        reviews = [crumb for crumb in item.get("dispatches", []) if crumb.get("role") == "review"]
        assert_true("review breadcrumbs append", [crumb["dispatch_id"] for crumb in reviews] == ["review-1", "review-2"])
        assert_true("accepted review recorded", item.get("accepted_review_dispatch_id") == "review-2")


def test_goalflight_task_schema_version_tolerance_and_read_api() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_tasks(
            project,
            [
                {
                    "id": "t-001",
                    "kind": "task",
                    "title": "Legacy open task",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                    "extra_future_field": {"kept": True},
                },
                {
                    "schema_version": 99,
                    "id": "t-002",
                    "kind": "task",
                    "title": "Future schema task",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                },
            ],
        )
        module = _load_goalflight_task_module()

        one = module.get("t-001", project_root=project)
        assert_true("missing schema version tolerated", one["schema_version"] == 1)
        assert_true("unknown optional fields preserved", one["extra_future_field"] == {"kept": True})
        assert_true("future schema version tolerated", module.get("t-002", project_root=project)["schema_version"] == 99)
        assert_true("api outstanding same row shape", [item["id"] for item in module.outstanding(project_root=project)] == ["t-001", "t-002"])

        proc = run_task(project, "sync", "--by", "watcher")
        assert_true(f"sync exits 0: {proc.stderr}", proc.returncode == 0)
        rows = [
            json.loads(line)
            for line in (project / "docs-private" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert_true("sync writes schema_version on every item", all(isinstance(item.get("schema_version"), int) for item in rows))


def test_goalflight_task_append_dispatch_breadcrumbs_preserves_history() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Retry task",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                }
            ],
        )
        module = _load_goalflight_task_module()
        store = module.TaskStore(project)
        store.append_dispatch_breadcrumbs(["t-001"], {"dispatch_id": "dispatch-1", "state": "working", "ts": "2026-06-01T00:00:00+00:00"}, "watcher")
        store.append_dispatch_breadcrumbs(["t-001"], {"dispatch_id": "dispatch-2", "state": "worker-finished", "ts": "2026-06-01T00:10:00+00:00"}, "watcher")

        item = module.get("t-001", project_root=project)
        dispatches = item.get("dispatches")
        assert_true("dispatch history is append list", isinstance(dispatches, list) and len(dispatches) == 2)
        assert_true("subsequent dispatch did not overwrite", [entry["dispatch_id"] for entry in dispatches] == ["dispatch-1", "dispatch-2"])
        assert_true("latest appended breadcrumb drives status", item["derived_status"] == "awaiting-review")


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


def test_goalflight_task_data_js_escapes_script_end_and_html() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        bad_title = "Bad </script><img src=x onerror=alert(1)> t-001"
        proc = run_task(
            project,
            "new",
            bad_title,
            "--acceptance",
            "accept </script><img src=x onerror=alert(1)>",
            "--prompt",
            "prompt </script><img src=x onerror=alert(1)>",
        )
        assert_true(f"new with script-like title exits 0: {proc.stderr}", proc.returncode == 0)
        docs = project / "docs-private"
        data_js = (docs / "tasks-data.js").read_text(encoding="utf-8")
        assert_true("script end escaped", "</script" not in data_js.lower())
        assert_true("raw img tag escaped", "<img" not in data_js.lower())
        assert_true("json payload carries escaped script start", "\\u003c/script" in data_js.lower())
        proc = run_checker(docs)
        assert_true("escaped data mirror remains valid", proc.returncode == 0)


def test_goalflight_task_sync_generates_markdown_views() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        items = [
            {
                "id": "t-001",
                "kind": "task",
                "title": "Task blocked by bug b-001 and decision q-001.",
                "blocked_by": ["b-001"],
                "links": ["q-001"],
                "done": False,
                "acceptance": "shows in Waiting linked to b-001.",
            },
            {
                "id": "t-002",
                "kind": "task",
                "title": "Done task.",
                "blocked_by": [],
                "links": [],
                "done": True,
            },
            {
                "id": "t-003",
                "kind": "task",
                "title": "Task whose blocker is already fixed.",
                "blocked_by": ["b-002"],
                "links": [],
                "done": False,
            },
            {
                "id": "b-001",
                "kind": "bug",
                "title": "Open bug.",
                "blocked_by": [],
                "links": ["t-001"],
                "done": False,
                "severity": "high",
                "source": "test",
            },
            {
                "id": "b-002",
                "kind": "bug",
                "title": "Fixed bug.",
                "blocked_by": [],
                "links": ["t-003"],
                "done": True,
                "severity": "low",
                "source": "test",
            },
            {
                "id": "q-001",
                "kind": "decision",
                "title": "Open decision.",
                "blocked_by": [],
                "links": ["t-001"],
                "done": False,
            },
        ]
        _write_tasks(project, items)
        proc = run_task(project, "sync", "--by", "watcher")
        assert_true(f"sync exits 0: {proc.stderr}", proc.returncode == 0)

        docs = project / "docs-private"
        task_md = (docs / "task-decomposition.md").read_text(encoding="utf-8")
        done_md = (docs / "tasks-done.md").read_text(encoding="utf-8")
        bug_md = (docs / "bug-backlog.md").read_text(encoding="utf-8")
        bugs_done_md = (docs / "bugs-done.md").read_text(encoding="utf-8")

        assert_true("waiting section present", "## Waiting" in task_md)
        assert_true("unresolved bug blocker linked", "[b-001](ticket.html?id=b-001)" in task_md)
        assert_true("cross-kind decision link rendered", "[q-001](ticket.html?id=q-001)" in task_md)
        assert_true("resolved blocker stays to-do", "### t-003" in task_md.split("## In progress", 1)[0])
        assert_true("done task rendered in done view", "### t-002" in done_md)
        assert_true("open bug rendered in backlog", "### b-001" in bug_md)
        assert_true("fixed bug excluded from backlog", "### b-002" not in bug_md)
        assert_true("fixed bug rendered in done view", "### b-002" in bugs_done_md)

        before = {path.name: path.read_text(encoding="utf-8") for path in docs.glob("*.md")}
        proc = run_task(project, "sync", "--by", "watcher")
        assert_true(f"second sync exits 0: {proc.stderr}", proc.returncode == 0)
        after = {path.name: path.read_text(encoding="utf-8") for path in docs.glob("*.md")}
        assert_true("generated markdown idempotent", before == after)


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
    test_goalflight_task_status_uses_latest_dispatch_breadcrumb()
    test_goalflight_task_sync_appends_plural_task_ids()
    test_goalflight_task_list_filters_outstanding_awaiting_review_since()
    test_goalflight_task_two_state_accept_and_review_breadcrumb()
    test_goalflight_task_schema_version_tolerance_and_read_api()
    test_goalflight_task_append_dispatch_breadcrumbs_preserves_history()
    test_goalflight_task_atomic_write_rejects_bad_content()
    test_goalflight_task_sync_repairs_stale_mirror()
    test_goalflight_task_data_js_escapes_script_end_and_html()
    test_goalflight_task_sync_generates_markdown_views()
    print("OK: 18 tasks mirror/task-store tests pass")


if __name__ == "__main__":
    main()
