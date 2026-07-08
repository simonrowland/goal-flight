#!/usr/bin/env python3
"""Hermetic test for the tasks.jsonl <-> tasks-data.js mirror checker.

Drives scripts/check_tasks_mirror.js (node-only; no network, no localhost):
  - PASS on the tracked empty scaffold templates/state-skeleton/.
  - PASS on the non-empty fixture tests/fixtures/tasks-mirror/.
  - FAIL on planted-drift temp copies (changed field / added id / stray status).

If node is unavailable, the whole file SKIPs (runner-recognized "SKIP:" prefix).
"""

from __future__ import annotations

import datetime as dt
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_tasks_mirror.js"
SKELETON = ROOT / "templates" / "state-skeleton"
FIXTURE = ROOT / "tests" / "fixtures" / "tasks-mirror"
TASK = ROOT / "goalflight_task.py"
NODE = shutil.which("node")

os.environ.setdefault(
    "GOALFLIGHT_TASK_STORE_DIR",
    tempfile.mkdtemp(prefix="goalflight-task-store-test-"),
)

# The canonical task store now lives OUT of the project tree (durable base,
# per-repo namespaced). The in-tree docs-private/dashboard are a one-way EXPORT
# plus the home for human/external inputs. White-box pokes at store-internal
# machine state (tasks.jsonl read as canonical, the publish marker, seq files,
# tasks.lock, generated markdown/mirror views, live-checker pair) must target
# the CANONICAL location; human/external inputs (reviews, RESUME-NOTES,
# history.md, harvest source markdown) stay in-tree.
sys.path.insert(0, str(ROOT))
import goalflight_task as _gft  # noqa: E402


def _canonical_docs(project_root: Path) -> Path:
    return _gft.resolve_task_store_dir(project_root) / "docs-private"


def _canonical_dashboard(project_root: Path) -> Path:
    return _gft.resolve_task_store_dir(project_root) / "dashboard"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def run_checker(target_dir: Path, *, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, str(CHECKER), str(target_dir)],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def run_live_checker(project_root: Path, *, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, str(CHECKER), str(_canonical_docs(project_root)), str(_canonical_dashboard(project_root))],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def run_task(
    project_root: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess:
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
        timeout=timeout,
        check=False,
    )


def run_task_no_hang(project_root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return run_task(project_root, *args, timeout=2)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"task command hung on non-regular input: {' '.join(args)}") from exc


def _copy_fixture(dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE / "tasks.jsonl", dst / "tasks.jsonl")
    shutil.copy(FIXTURE / "tasks-data.js", dst / "tasks-data.js")


def _write_tasks(project_root: Path, items: list[dict]) -> None:
    # Seed the CANONICAL store the tool reads (out of the project tree), plus
    # its dashboard mirror, so a subsequent read/mutation sees this state as
    # canonical rather than migrating an in-tree legacy file.
    docs = _canonical_docs(project_root)
    dashboard = _canonical_dashboard(project_root)
    docs.mkdir(parents=True, exist_ok=True)
    dashboard.mkdir(parents=True, exist_ok=True)
    (docs / "tasks.jsonl").write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in items),
        encoding="utf-8",
    )
    module = _load_goalflight_task_module()
    (dashboard / "tasks-data.js").write_text(
        module._items_data_js(items),
        encoding="utf-8",
    )


def _read_items(project_root: Path) -> list[dict]:
    path = _canonical_docs(project_root) / "tasks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_goalflight_task_module():
    spec = importlib.util.spec_from_file_location("goalflight_task", TASK)
    assert_true("goalflight_task.py import spec", spec is not None and spec.loader is not None)
    module = importlib.util.module_from_spec(spec)
    sys.modules["goalflight_task"] = module
    spec.loader.exec_module(module)
    return module


def test_default_fixture_passes() -> None:
    # No dir arg -> checker defaults to the empty templates/state-skeleton/.
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
    assert_true("default scaffold store is empty", (SKELETON / "tasks.jsonl").read_text(encoding="utf-8") == "")
    assert_true("default scaffold mirror is empty", "window.GF_ITEMS = [];" in (SKELETON / "tasks-data.js").read_text(encoding="utf-8"))


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


def test_drift_undefined_value_in_data_js_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        item = {
            "id": "t-001",
            "kind": "task",
            "title": "JSON-only item",
            "blocked_by": [],
            "links": [],
            "done": False,
        }
        (d / "tasks.jsonl").write_text(json.dumps(item, separators=(",", ":")) + "\n", encoding="utf-8")
        (d / "tasks-data.js").write_text(
            "\n".join(
                [
                    "window.GF_ITEMS = [{",
                    '  "id": "t-001",',
                    '  "kind": "task",',
                    '  "title": "JSON-only item",',
                    '  "blocked_by": [],',
                    '  "links": [],',
                    '  "done": false,',
                    "  \"extra\": undefined",
                    "}];",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        proc = run_checker(d)
    assert_true("undefined drift exits non-zero", proc.returncode != 0)
    assert_true("undefined drift reports undefined", "undefined" in proc.stderr)


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

        proc = run_live_checker(project)
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


def test_goalflight_task_sync_writes_mirror_only_derived_status() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project-a"
        items = [
            {
                "schema_version": 1,
                "id": "t-001",
                "kind": "task",
                "title": "Working task",
                "blocked_by": [],
                "links": [],
                "done": False,
                "dispatches": [{"dispatch_id": "dispatch-1", "state": "working", "ts": "2026-06-01T00:00:00+00:00"}],
            },
            {
                "schema_version": 1,
                "id": "t-002",
                "kind": "task",
                "title": "Finished worker task",
                "blocked_by": [],
                "links": [],
                "done": False,
                "dispatches": [{"dispatch_id": "dispatch-2", "state": "worker-finished", "ts": "2026-06-01T00:00:00+00:00"}],
            },
            {
                "schema_version": 1,
                "id": "t-003",
                "kind": "task",
                "title": "Done awaiting review",
                "blocked_by": [],
                "links": [],
                "done": True,
                "done_reviewed": False,
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
        ]
        _write_tasks(project, items)

        proc = run_task(project, "sync", "--by", "watcher")
        assert_true(f"sync exits 0: {proc.stderr}", proc.returncode == 0)
        data_js = (_canonical_dashboard(project) / "tasks-data.js").read_text(encoding="utf-8")
        payload = data_js.split("window.GF_ITEMS = ", 1)[1].split(";\nif", 1)[0]
        data_items = {item["id"]: item for item in json.loads(payload)}
        meta_payload = data_js.split("window.GF_META = ", 1)[1].rsplit(";\n", 1)[0]
        meta = json.loads(meta_payload)
        assert_true("working derived status in mirror", data_items["t-001"]["derived_status"] == "working")
        assert_true("finished worker becomes awaiting review in mirror", data_items["t-002"]["derived_status"] == "awaiting-review")
        assert_true("done unresolved remains awaiting review in mirror", data_items["t-003"]["derived_status"] == "awaiting-review")
        assert_true("decision derived status in mirror", data_items["q-001"]["derived_status"] == "decision")
        assert_true("GF_META schema", meta["schema"] == 1)
        assert_true("GF_META generated_at string", isinstance(meta["generated_at"], str) and meta["generated_at"])
        assert_true("GF_META counts working", meta["counts"]["working"] == 1)
        assert_true("GF_META counts awaiting-review", meta["counts"]["awaiting-review"] == 2)
        assert_true("GF_META counts decision", meta["counts"]["decision"] == 1)
        assert_true("derived status not persisted", all("derived_status" not in item for item in _read_items(project)))
        proc = run_live_checker(project)
        assert_true("mirror with derived_status passes checker", proc.returncode == 0)


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
            for line in (_canonical_docs(project) / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
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


def test_goalflight_task_list_lane_facet_and_status_collision() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project-a"
        items = [
            {
                "schema_version": 1,
                "id": "t-001",
                "kind": "task",
                "title": "Deferred work",
                "blocked_by": [],
                "links": [],
                "done": False,
                "lane": "deferred",
                "created_at": "2020-01-01T00:00:00+00:00",
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
                "created_at": "2020-01-01T00:00:00+00:00",
            },
            {
                "schema_version": 1,
                "id": "t-003",
                "kind": "task",
                "title": "UI work",
                "blocked_by": [],
                "links": [],
                "done": False,
                "lane": "ui",
                "created_at": "2020-01-01T00:00:00+00:00",
            },
            {
                "schema_version": 1,
                "id": "t-004",
                "kind": "task",
                "title": "Status-word lane but done reviewed",
                "blocked_by": [],
                "links": [],
                "done": True,
                "done_reviewed": True,
                "lane": "outstanding",
                "created_at": "2020-01-01T00:00:00+00:00",
            },
            {
                "schema_version": 1,
                "id": "t-005",
                "kind": "task",
                "title": "Status-word lane and outstanding",
                "blocked_by": [],
                "links": [],
                "done": False,
                "lane": "outstanding",
                "created_at": "2020-01-01T00:00:00+00:00",
            },
            {
                "schema_version": 1,
                "id": "t-006",
                "kind": "task",
                "title": "Deferred but done reviewed",
                "blocked_by": [],
                "links": [],
                "done": True,
                "done_reviewed": True,
                "lane": "deferred",
                "created_at": "2020-01-01T00:00:00+00:00",
            },
        ]
        _write_tasks(project, items)

        proc = run_task(project, "list", "deferred", "--json")
        assert_true(f"list deferred exits 0: {proc.stderr}", proc.returncode == 0)
        deferred = json.loads(proc.stdout)
        assert_true("list deferred filters outstanding reserved lane", [item["id"] for item in deferred] == ["t-001"])

        proc = run_task(project, "list", "done-reviewed", "--lane", "deferred", "--json")
        assert_true(f"list done-reviewed --lane deferred exits 0: {proc.stderr}", proc.returncode == 0)
        done_deferred = json.loads(proc.stdout)
        assert_true("raw lane filter still composes with done-reviewed", [item["id"] for item in done_deferred] == ["t-006"])

        proc = run_task(project, "list", "held", "--json")
        assert_true(f"list held exits 0: {proc.stderr}", proc.returncode == 0)
        held = json.loads(proc.stdout)
        assert_true("list held filters reserved lane", [item["id"] for item in held] == ["t-002"])

        proc = run_task(project, "list", "--lane", "ui", "--json")
        assert_true(f"list --lane ui exits 0: {proc.stderr}", proc.returncode == 0)
        ui = json.loads(proc.stdout)
        assert_true("list --lane filters free-text lane", [item["id"] for item in ui] == ["t-003"])

        proc = run_task(project, "list", "ui", "--json")
        assert_true("bare free-text lane is rejected", proc.returncode != 0)

        proc = run_task(project, "list", "outstanding", "--json")
        assert_true(f"list outstanding exits 0: {proc.stderr}", proc.returncode == 0)
        outstanding = {item["id"] for item in json.loads(proc.stdout)}
        assert_true("positional outstanding remains status", "t-004" not in outstanding and "t-005" in outstanding)

        proc = run_task(project, "list", "--lane", "outstanding", "--json")
        assert_true(f"list --lane outstanding exits 0: {proc.stderr}", proc.returncode == 0)
        status_lane = {item["id"] for item in json.loads(proc.stdout)}
        assert_true("--lane status word filters lane", status_lane == {"t-004", "t-005"})

        proc = run_task(project, "list", "deferred", "--lane", "ui", "--json")
        assert_true("reserved positional + --lane is rejected", proc.returncode != 0)
        assert_true(
            "rejection names the reserved-positional/--lane conflict",
            "reserved-lane positional cannot be combined with --lane" in proc.stderr,
        )


def test_goalflight_task_edit_existing_item_fields_and_audit() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project-a"
        prompts = project / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "initial.md").write_text("initial prompt\n", encoding="utf-8")
        (prompts / "rendered.md").write_text("rendered prompt\n", encoding="utf-8")
        items = [
            {
                "schema_version": 1,
                "id": "t-010",
                "kind": "task",
                "title": "Rendered prompt task",
                "blocked_by": ["q-001"],
                "links": [],
                "done": False,
                "lane": "ui",
                "created_at": "2020-01-01T00:00:00+00:00",
                "audit": [{"at": "2020-01-01T00:00:00+00:00", "actor": "test", "action": "new"}],
            },
        ]
        items.extend(
            {
                "schema_version": 1,
                "id": blocker_id,
                "kind": "task",
                "title": f"Blocker {blocker_id}",
                "blocked_by": [],
                "links": [],
                "done": False,
                "created_at": "2020-01-01T00:00:00+00:00",
                "audit": [{"at": "2020-01-01T00:00:00+00:00", "actor": "test", "action": "new"}],
            }
            for blocker_id in ["q-001", "q-002", "q-003"]
        )
        _write_tasks(project, items)
        proc = run_task(project, "sync")
        assert_true(f"seed sync exits 0: {proc.stderr}", proc.returncode == 0)

        proc = run_task(project, "set-prompt-path", "t-010", str(prompts / "initial.md"))
        assert_true(f"set-prompt-path exits 0: {proc.stderr}", proc.returncode == 0)
        item = _read_items(project)[0]
        assert_true("set-prompt-path stores absolute inside-root path as relative", item["prompt_path"] == "prompts/initial.md")
        prompt_audit = item["audit"][-1]
        assert_true("set-prompt-path audit action", prompt_audit["action"] == "set-prompt-path")
        assert_true("set-prompt-path audit new path is relative", prompt_audit["prompt_path"] == "prompts/initial.md")
        proc = run_task(project, "show", "t-010", "--json")
        assert_true(f"show --json exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("show serves relative prompt_path", json.loads(proc.stdout)["prompt_path"] == "prompts/initial.md")
        proc = run_task(project, "show", "t-010", "--prompt")
        assert_true(f"show --prompt exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("relative-stored prompt_path still serves prompt content", proc.stdout == "initial prompt\n")

        proc = run_task(
            project,
            "set",
            "t-010",
            "--prompt-path",
            "prompts/rendered.md",
            "--lane",
            "held",
            "--blocked-by",
            "q-002,q-003",
        )
        assert_true(f"set exits 0: {proc.stderr}", proc.returncode == 0)
        item = _read_items(project)[0]
        assert_true("set updates prompt_path", item["prompt_path"] == "prompts/rendered.md")
        assert_true("set updates reserved lane", item["lane"] == "held")
        assert_true("set replaces blocked_by", item["blocked_by"] == ["q-002", "q-003"])
        audit_tail = item["audit"][-3:]
        assert_true("set writes per-field audit actions", [entry["action"] for entry in audit_tail] == ["set-prompt-path", "lane", "set-blocked-by"])
        assert_true("set prompt audit old path", audit_tail[0]["old_prompt_path"] == "prompts/initial.md")
        assert_true("set prompt audit new path", audit_tail[0]["prompt_path"] == "prompts/rendered.md")
        assert_true("set lane audit old lane", audit_tail[1]["old_lane"] == "ui")
        assert_true("set lane audit new lane", audit_tail[1]["lane"] == "held")
        assert_true("set blocked_by audit old list", audit_tail[2]["old_blocked_by"] == ["q-001"])
        assert_true("set blocked_by audit new list", audit_tail[2]["blocked_by"] == ["q-002", "q-003"])

        proc = run_task(project, "set", "t-010")
        assert_true("set rejects missing field flags", proc.returncode != 0)
        assert_true("set missing flags error is clear", "expected at least one" in proc.stderr)

        proc = run_task(project, "set", "missing", "--lane", "held")
        assert_true("set rejects unknown item", proc.returncode != 0)
        assert_true("set unknown item error is clear", "item not found: missing" in proc.stderr)

        before = _read_items(project)
        proc = run_task(project, "set-prompt-path", "missing", "prompts/initial.md")
        assert_true("set-prompt-path rejects unknown item", proc.returncode != 0)
        assert_true("set-prompt-path unknown item error is clear", "item not found: missing" in proc.stderr)
        assert_true("set-prompt-path unknown item leaves store unchanged", _read_items(project) == before)


def test_goalflight_task_set_prompt_path_rejects_unsafe_paths_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project-a"
        prompts = project / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "ok.md").write_text("ok prompt\n", encoding="utf-8")
        (prompts / "target.md").write_text("target prompt\n", encoding="utf-8")
        (prompts / "link.md").symlink_to(prompts / "target.md")
        outside = root / "outside.md"
        outside.write_text("outside prompt\n", encoding="utf-8")
        items = [
            {
                "schema_version": 1,
                "id": "t-020",
                "kind": "task",
                "title": "Unsafe prompt path task",
                "blocked_by": [],
                "links": [],
                "done": False,
                "prompt_path": "prompts/ok.md",
                "created_at": "2020-01-01T00:00:00+00:00",
                "audit": [{"at": "2020-01-01T00:00:00+00:00", "actor": "test", "action": "new"}],
            },
        ]
        _write_tasks(project, items)
        proc = run_task(project, "sync")
        assert_true(f"seed sync exits 0: {proc.stderr}", proc.returncode == 0)

        unsafe_cases = [
            (str(outside), "resolves outside project root"),
            ("../outside.md", "contains '..' component"),
            ("prompts/link.md", "refusing symlink path component"),
        ]
        for prompt_path, expected_error in unsafe_cases:
            proc = run_task(project, "set-prompt-path", "t-020", prompt_path)
            assert_true(f"set-prompt-path rejects {prompt_path!r}", proc.returncode != 0)
            assert_true(f"set-prompt-path reports {expected_error}", expected_error in proc.stderr)
            item = _read_items(project)[0]
            assert_true("unsafe set-prompt-path leaves prompt_path unchanged", item["prompt_path"] == "prompts/ok.md")
            assert_true("unsafe set-prompt-path leaves audit unchanged", [entry["action"] for entry in item["audit"]] == ["new"])

            before = _read_items(project)
            proc = run_task(project, "set", "t-020", "--prompt-path", prompt_path, "--lane", "held")
            assert_true(f"set --prompt-path rejects {prompt_path!r}", proc.returncode != 0)
            assert_true(f"set --prompt-path reports {expected_error}", expected_error in proc.stderr)
            assert_true("unsafe set --prompt-path leaves item unchanged", _read_items(project) == before)


def test_goalflight_task_set_blocked_by_rejects_invalid_item_ids_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project-a"
        items = [
            {
                "schema_version": 1,
                "id": "t-030",
                "kind": "task",
                "title": "Blocked replacement task",
                "blocked_by": ["t-031"],
                "links": [],
                "done": False,
                "lane": "ui",
                "created_at": "2020-01-01T00:00:00+00:00",
                "audit": [{"at": "2020-01-01T00:00:00+00:00", "actor": "test", "action": "new"}],
            },
            {
                "schema_version": 1,
                "id": "t-031",
                "kind": "task",
                "title": "Existing blocker",
                "blocked_by": [],
                "links": [],
                "done": False,
                "created_at": "2020-01-01T00:00:00+00:00",
                "audit": [{"at": "2020-01-01T00:00:00+00:00", "actor": "test", "action": "new"}],
            },
        ]
        _write_tasks(project, items)
        proc = run_task(project, "sync")
        assert_true(f"seed sync exits 0: {proc.stderr}", proc.returncode == 0)

        cases = [
            ("t-030", "blocked_by cannot include itself"),
            ("t-999", "blocked_by item not found: t-999"),
        ]
        for blocker, expected_error in cases:
            before = _read_items(project)
            proc = run_task(project, "set", "t-030", "--lane", "held", "--blocked-by", blocker)
            assert_true(f"set rejects invalid blocked_by {blocker}", proc.returncode != 0)
            assert_true(f"set reports invalid blocked_by {blocker}", expected_error in proc.stderr)
            assert_true("invalid blocked_by replacement leaves item unchanged", _read_items(project) == before)


def test_goalflight_task_set_prompt_path_dispatch_frontier_dry_run_uses_prompt_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project-a"
        prompts = project / "prompts"
        prompts.mkdir(parents=True)
        prompt_file = prompts / "frontier.md"
        prompt_file.write_text("frontier prompt\n", encoding="utf-8")
        items = [
            {
                "schema_version": 1,
                "id": "t-040",
                "kind": "task",
                "title": "Frontier prompt task",
                "blocked_by": [],
                "links": [],
                "done": False,
                "created_at": "2020-01-01T00:00:00+00:00",
                "audit": [{"at": "2020-01-01T00:00:00+00:00", "actor": "test", "action": "new"}],
            },
        ]
        _write_tasks(project, items)
        proc = run_task(project, "sync")
        assert_true(f"seed sync exits 0: {proc.stderr}", proc.returncode == 0)

        proc = run_task(project, "set-prompt-path", "t-040", "prompts/frontier.md")
        assert_true(f"set-prompt-path exits 0: {proc.stderr}", proc.returncode == 0)
        item = _read_items(project)[0]
        assert_true("set-prompt-path retrofits prompt_path", item["prompt_path"] == "prompts/frontier.md")

        proc = run_task(project, "dispatch-frontier", "--dry-run")
        assert_true(f"dispatch-frontier --dry-run exits 0: {proc.stderr}", proc.returncode == 0)
        resolved = prompt_file.resolve(strict=False)
        assert_true("dispatch-frontier dry-run emits prompt file flag", f"--prompt-file {resolved}" in proc.stdout)
        assert_true("dispatch-frontier dry-run keeps item and agent", "t-040 ->" in proc.stdout and "-> codex" in proc.stdout)


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
                },
                {
                    "schema_version": 1,
                    "id": "t-002",
                    "kind": "task",
                    "title": "Not done",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                    "done_reviewed": False,
                }
            ],
        )

        proc = run_task(project, "accept", "t-001")
        assert_true("accept without review fails", proc.returncode != 0 and "no logged review" in proc.stderr)
        assert_true("accept review hint uses real command", "python3 goalflight_task.py review t-001 --verdict clean --dispatch <id>" in proc.stderr)

        proc = run_task(project, "accept", "t-002")
        assert_true("accept before done fails", proc.returncode != 0 and "not DONE/awaiting-review" in proc.stderr)
        assert_true("accept done hint uses real command", "python3 goalflight_task.py done t-002" in proc.stderr)

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


def test_goalflight_task_review_captures_confirmed_bug_item() -> None:
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

        proc = run_task(
            project,
            "review",
            "t-001",
            "--verdict",
            "findings",
            "--dispatch",
            "review-1",
            "--findings",
            "docs-private/reviews/t-001.md",
            "--bug",
            "Confirmed review finding remains unfixed",
            "--bug-pattern",
            "bp-007",
            "--bug-severity",
            "high",
            "--json",
        )
        assert_true(f"review bug capture exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("review returns one captured bug", len(payload["bugs"]) == 1)

        bug = json.loads(run_task(project, "show", payload["bugs"][0], "--json").stdout)
        assert_true("review bug kind/source", bug["kind"] == "bug" and bug["source"] == "review")
        assert_true("review bug pattern tag", bug["pattern"] == "bp-007" and "bp-007" in bug["tags"])
        assert_true("review bug linked to reviewed item", bug["review_item_id"] == "t-001" and "t-001" in bug["links"])
        assert_true("review bug linked to review breadcrumb", bug["review_dispatch_id"] == "review-1" and "review-1" in bug["review_breadcrumb_key"])
        assert_true("review bug links findings file", "docs-private/reviews/t-001.md" in bug["links"])

        proc = run_task(
            project,
            "review",
            "t-001",
            "--verdict",
            "findings",
            "--dispatch",
            "review-1",
            "--findings",
            "docs-private/reviews/t-001.md",
            "--bug",
            "Confirmed review finding remains unfixed",
            "--bug-pattern",
            "bp-007",
            "--json",
        )
        assert_true(f"review bug recapture exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("review bug capture idempotent by review key", payload["bugs"] == [] and payload["skipped_bugs"] == 1)


def test_goalflight_task_harvest_idempotent_with_source_links_and_history() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        # Harvest sources + history.md are human/external inputs the tool reads
        # in-tree; the canonical store lives elsewhere (seeded by _write_tasks).
        docs = project / "docs-private"
        docs.mkdir(parents=True, exist_ok=True)
        _write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "b-001",
                    "kind": "bug",
                    "title": "Known backlog bug already tracked",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                    "source": "sweep",
                },
                {
                    "schema_version": 1,
                    "id": "b-002",
                    "kind": "bug",
                    "title": "Compare A < B & C already tracked",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                    "source": "sweep",
                }
            ],
        )
        goal_queue = docs / "goal-queue-demo.md"
        goal_queue.write_text(
            "\n".join(
                [
                    "# Goal Queue",
                    "",
                    "### t-099",
                    "",
                    "**Queue task needing task-store seed**",
                    "",
                    "- Status: to do",
                    "",
                    "### t-100",
                    "",
                    "**Already done queue task**",
                    "",
                    "- Status: done",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        queue_before = goal_queue.read_text(encoding="utf-8")
        (docs / "bug-backlog.md").write_text(
            "\n".join(
                [
                    "# Bug Backlog",
                    "",
                    "### bp-001",
                    "",
                    "**Known backlog bug already tracked**",
                    "",
                    "- Status: to do",
                    "",
                    "### bp-002",
                    "",
                    "**Harvested backlog bug**",
                    "",
                    "- Status: to do",
                    "",
                    "### bp-003",
                    "",
                    "**Compare A &lt; B &amp; C already tracked**",
                    "",
                    "- Status: to do",
                    "",
                    "### bp-004",
                    "",
                    "**Harvested special A < B & C**",
                    "",
                    "- Status: to do",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        review_dir = docs / "reviews"
        review_dir.mkdir()
        (review_dir / "backlog-review.md").write_text(
            "1. **P1 - Review backlog finding not yet tracked.**\n",
            encoding="utf-8",
        )
        (review_dir / "historical-cleanup-review.md").write_text(
            "1. **P0 - Historical review finding should not be harvested from a non-backlog file.**\n",
            encoding="utf-8",
        )
        (docs / "RESUME-NOTES-2026-06-01.md").write_text(
            "\n".join(
                [
                    "# RESUME-NOTES 2026-06-01",
                    "",
                    "## TL;DR",
                    "",
                    "Earlier state to backfill.",
                    "",
                    "## NEXT ACTIONS",
                    "",
                    "1. Older action should only enter history, not draft items.",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (docs / "RESUME-NOTES-2026-06-02.md").write_text(
            "\n".join(
                [
                    "# RESUME-NOTES 2026-06-02",
                    "",
                    "## TL;DR",
                    "",
                    "Current state for harvest.",
                    "",
                    "## NEXT ACTIONS",
                    "",
                    "1. Resume-only action to seed.",
                    "",
                    "## Decisions - DO NOT re-litigate",
                    "",
                    "- ADR-099: Keep this settled decision.",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        proc = run_task(project, "harvest", "--json")
        assert_true(f"harvest exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("harvest created draft items", len(payload["created"]) == 5)
        assert_true("harvest backfilled both resume notes", payload["history_added"] == ["docs-private/RESUME-NOTES-2026-06-01.md", "docs-private/RESUME-NOTES-2026-06-02.md"])
        assert_true("goal queue left as-is", goal_queue.read_text(encoding="utf-8") == queue_before)

        items = _read_items(project)
        harvested = [item for item in items if "harvest" in item.get("tags", [])]
        titles = [item["title"] for item in harvested]
        assert_true("goal queue items not harvested", all(item.get("harvest_source") != "goal-queue" for item in harvested))
        assert_true("goal queue title ignored", "Queue task needing task-store seed" not in titles)
        assert_true("known backlog bug deduped by title", titles.count("Known backlog bug already tracked") == 0)
        assert_true("html entity backlog title deduped by normalized title", all("Compare A" not in item["title"] for item in harvested))
        assert_true("html-special backlog title harvested", "Harvested special A < B & C" in titles)
        assert_true("source link present on every harvested item", all(item.get("links") and item.get("source_ref") in item.get("links") for item in harvested))
        assert_true("harvested items are draft tagged", all("draft" in item.get("tags", []) for item in harvested))
        assert_true("review finding harvested as bug", any(item["kind"] == "bug" and item.get("severity") == "high" for item in harvested))
        assert_true("ordinary historical review file ignored", "Historical review finding should not be harvested from a non-backlog file." not in titles)
        assert_true("resume next-action harvested", "Resume-only action to seed." in titles)
        assert_true("resume decision harvested as decision", any(item["kind"] == "decision" and item["title"].startswith("ADR-099") for item in harvested))

        history_before = (docs / "history.md").read_text(encoding="utf-8")
        proc = run_task(project, "harvest", "--json")
        assert_true(f"second harvest exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("harvest idempotent", payload["created"] == [] and payload["history_added"] == [])
        assert_true("history write-once", (docs / "history.md").read_text(encoding="utf-8") == history_before)


def test_goalflight_task_harvest_ignores_skeleton_placeholders() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        docs = project / "docs-private"
        docs.mkdir(parents=True, exist_ok=True)
        _write_tasks(project, [])
        shutil.copy(SKELETON / "bug-patterns.md", docs / "bug-patterns.md")
        (docs / "bug-backlog.md").write_text(
            "\n".join(
                [
                    "# Bug Backlog",
                    "",
                    "### bp-010",
                    "",
                    "**<one-line bug placeholder>**",
                    "",
                    "- Status: to do",
                    "",
                    "- Corpus: <SC-xx, or local-only>",
                    "- Sweep status: <swept @ `<SHA>` | pending>",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        proc = run_task(project, "harvest", "--json")
        assert_true(f"skeleton harvest exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("skeleton placeholders create no items", payload["created"] == [])
        assert_true("skeleton placeholders leave task store empty", _read_items(project) == [])


def test_goalflight_task_harvest_allows_real_angle_bracket_titles() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        docs = project / "docs-private"
        docs.mkdir(parents=True, exist_ok=True)
        _write_tasks(project, [])
        (docs / "bug-backlog.md").write_text(
            "\n".join(
                [
                    "# Bug Backlog",
                    "",
                    "### bp-020",
                    "",
                    "**<one-line bug placeholder>**",
                    "",
                    "- Status: to do",
                    "",
                    "### bp-021",
                    "",
                    "**Fix <Foo> parser regression**",
                    "",
                    "- Status: to do",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        review_dir = docs / "reviews"
        review_dir.mkdir()
        (review_dir / "angle-backlog.md").write_text(
            "1. **P1 - XSS via <script> in title**\n",
            encoding="utf-8",
        )

        proc = run_task(project, "harvest", "--json")
        assert_true(f"angle-bracket harvest exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("only real angle-bracket items harvested", len(payload["created"]) == 2)
        titles = [item["title"] for item in _read_items(project)]
        assert_true("real generic title harvested", "Fix <Foo> parser regression" in titles)
        assert_true("real review tag title harvested", "XSS via <script> in title" in titles)
        assert_true("skeleton placeholder still skipped", "<one-line bug placeholder>" not in titles)


def test_goalflight_task_harvest_keeps_literal_punctuation_distinct() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        docs = project / "docs-private"
        docs.mkdir(parents=True, exist_ok=True)
        _write_tasks(project, [])
        (docs / "bug-backlog.md").write_text(
            "\n".join(
                [
                    "# Bug Backlog",
                    "",
                    "### bp-030",
                    "",
                    "**Fix parser*edge case**",
                    "",
                    "- Status: to do",
                    "",
                    "### bp-031",
                    "",
                    "**Fix parser_edge case**",
                    "",
                    "- Status: to do",
                    "",
                    "### bp-032",
                    "",
                    "**Fix parser~edge case**",
                    "",
                    "- Status: to do",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        proc = run_task(project, "harvest", "--json")
        assert_true(f"punctuation harvest exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("literal punctuation titles do not dedupe together", len(payload["created"]) == 3)
        titles = [item["title"] for item in _read_items(project)]
        assert_true("literal star title harvested", "Fix parser*edge case" in titles)
        assert_true("literal underscore title harvested", "Fix parser_edge case" in titles)
        assert_true("literal tilde title harvested", "Fix parser~edge case" in titles)


def test_goalflight_task_harvest_allows_nested_generated_basename_sources() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        docs = project / "docs-private"
        _write_tasks(project, [])
        nested = docs / "reviews" / "findings"
        nested.mkdir(parents=True)
        (nested / "task-decomposition.md").write_text(
            "1. **P2 - Nested generated basename still harvested**\n",
            encoding="utf-8",
        )

        proc = run_task(project, "harvest", "--json")
        assert_true(f"nested basename harvest exits 0: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("nested generated basename source harvested", len(payload["created"]) == 1)
        titles = [item["title"] for item in _read_items(project)]
        assert_true("nested generated basename title present", "Nested generated basename still harvested" in titles)


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
            for line in (_canonical_docs(project) / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
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
        docs = _canonical_docs(project)
        dashboard = _canonical_dashboard(project)
        before_jsonl = (docs / "tasks.jsonl").read_text(encoding="utf-8")
        before_data = (dashboard / "tasks-data.js").read_text(encoding="utf-8")

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
        assert_true("tasks-data.js unchanged after rejected write", (dashboard / "tasks-data.js").read_text(encoding="utf-8") == before_data)
        proc = run_live_checker(project)
        assert_true("live pair still valid", proc.returncode == 0)


def test_goalflight_task_refuses_symlinked_state_write_dirs() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        escaped_docs = root / "escaped-docs"
        escaped_docs.mkdir()
        (project / "docs-private").symlink_to(escaped_docs)

        proc = run_task(project, "new", "Should not write outside")
        assert_true("symlinked docs-private write fails", proc.returncode == 1)
        assert_true("docs-private failure names state dir", "docs-private" in proc.stderr)
        assert_true("docs-private failure is containment/symlink", "escapes project root" in proc.stderr or "symlink" in proc.stderr)
        assert_true("no tasks written outside docs-private", not (escaped_docs / "tasks.jsonl").exists())
        assert_true("no seq written outside docs-private", not (escaped_docs / ".task-seq").exists())

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        docs = project / "docs-private"
        docs.mkdir(parents=True)
        (docs / "tasks.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Seed",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        escaped_dashboard = root / "escaped-dashboard"
        escaped_dashboard.mkdir()
        (project / "dashboard").symlink_to(escaped_dashboard)

        proc = run_task(project, "sync")
        assert_true("symlinked dashboard write fails", proc.returncode == 1)
        assert_true("dashboard failure names state dir", "dashboard" in proc.stderr)
        assert_true("dashboard failure is containment/symlink", "escapes project root" in proc.stderr or "symlink" in proc.stderr)
        assert_true("no tasks-data written outside dashboard", not (escaped_dashboard / "tasks-data.js").exists())


def test_goalflight_task_interrupted_publish_marker_repairs_mirror() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        old_item = {
            "schema_version": 1,
            "id": "t-001",
            "kind": "task",
            "title": "Old mirror title",
            "blocked_by": [],
            "links": [],
            "done": False,
        }
        _write_tasks(project, [old_item])
        docs = _canonical_docs(project)
        new_item = dict(old_item)
        new_item["title"] = "New canonical title"
        (docs / "tasks.jsonl").write_text(json.dumps(new_item, separators=(",", ":")) + "\n", encoding="utf-8")
        (docs / ".tasks-publish-incomplete.json").write_text(
            json.dumps({"schema": "goalflight.tasks.publish.v1", "canonical": "tasks.jsonl"}) + "\n",
            encoding="utf-8",
        )

        proc = run_task(project, "status", "--json")
        assert_true(f"status repairs interrupted publish: {proc.stderr}", proc.returncode == 0)
        payload = json.loads(proc.stdout)
        assert_true("status read canonical item", payload["items"][0]["title"] == "New canonical title")
        assert_true("publish marker cleared", not (docs / ".tasks-publish-incomplete.json").exists())
        assert_true("mirror repaired to canonical title", "New canonical title" in (_canonical_dashboard(project) / "tasks-data.js").read_text(encoding="utf-8"))
        assert_true("markdown repaired to canonical title", "New canonical title" in (docs / "task-decomposition.md").read_text(encoding="utf-8"))
        proc = run_live_checker(project)
        assert_true("repaired pair passes checker", proc.returncode == 0)


def test_goalflight_task_resume_history_uses_atomic_writer() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        docs = project / "docs-private"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "RESUME-NOTES-2026-06-01.md").write_text(
            "\n".join(
                [
                    "# RESUME-NOTES 2026-06-01",
                    "",
                    "## TL;DR",
                    "",
                    "Atomic history write candidate.",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        history = docs / "history.md"
        history.write_text("existing history\n", encoding="utf-8")
        module = _load_goalflight_task_module()
        store = module.TaskStore(project)
        before = history.read_text(encoding="utf-8")
        original = module._atomic_write_text

        def fail_atomic_write(path: Path, text: str, *, prefix: str = ".tmp-") -> None:
            (path.parent / ".history-forced-partial").write_text("partial write should not replace history\n", encoding="utf-8")
            raise OSError("forced atomic write failure")

        module._atomic_write_text = fail_atomic_write
        try:
            try:
                module._append_resume_history(store, "tester")
            except OSError:
                pass
            else:
                raise AssertionError("history write did not use atomic writer")
        finally:
            module._atomic_write_text = original

        assert_true("history target unchanged after atomic failure", history.read_text(encoding="utf-8") == before)


def test_goalflight_task_resume_history_filters_subset_race_under_lock() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        docs = project / "docs-private"
        docs.mkdir(parents=True, exist_ok=True)
        for day in ("01", "02"):
            (docs / f"RESUME-NOTES-2026-06-{day}.md").write_text(
                "\n".join(
                    [
                        f"# RESUME-NOTES 2026-06-{day}",
                        "",
                        "## TL;DR",
                        "",
                        f"History race candidate {day}.",
                        "",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        history = docs / "history.md"
        history.write_text("existing history\n", encoding="utf-8")
        module = _load_goalflight_task_module()
        store = module.TaskStore(project)
        original_lock = store.store_lock
        raced_source = "docs-private/RESUME-NOTES-2026-06-01.md"

        @contextlib.contextmanager
        def racing_lock():
            with original_lock():
                history.write_text(
                    history.read_text(encoding="utf-8")
                    + f"\n## 2026-06-01 - RESUME-NOTES-2026-06-01.md\n\n- Source: {raced_source}\n- Harvested by: other\n",
                    encoding="utf-8",
                )
                yield

        store.store_lock = racing_lock
        appended = module._append_resume_history(store, "tester")
        text = history.read_text(encoding="utf-8")
        assert_true("history subset race reports only still-missing source", appended == ["docs-private/RESUME-NOTES-2026-06-02.md"])
        assert_true("raced source not duplicated", text.count(f"- Source: {raced_source}") == 1)
        assert_true("missing source appended", text.count("- Source: docs-private/RESUME-NOTES-2026-06-02.md") == 1)


def _assert_non_regular_task_read_fails(project: Path, *args: str) -> None:
    proc = run_task_no_hang(project, *args)
    assert_true(f"{args} rejects non-regular file: stdout={proc.stdout} stderr={proc.stderr}", proc.returncode != 0)
    assert_true(f"{args} reports non-regular file", "refusing to open non-regular file" in proc.stderr)


def test_goalflight_task_rejects_non_regular_store_files_without_hanging() -> None:
    item = {
        "schema_version": 1,
        "id": "t-001",
        "kind": "task",
        "title": "Non-regular guard seed",
        "blocked_by": [],
        "links": [],
        "done": False,
    }
    commands = [
        ("status", "--json"),
        ("show", "t-001", "--json"),
        ("list", "outstanding", "--json"),
        ("new", "Should fail before writing"),
        ("sync",),
    ]

    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_tasks(project, [item])
        docs = _canonical_docs(project)
        target = docs / "tasks-target.jsonl"
        target.write_text((docs / "tasks.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        (docs / "tasks.jsonl").unlink()
        (docs / "tasks.jsonl").symlink_to(target)

        for command in commands:
            _assert_non_regular_task_read_fails(project, *command)

        proc = run_checker(docs, timeout=2)
        assert_true("checker rejects symlinked tasks.jsonl", proc.returncode != 0)
        assert_true("checker reports non-regular tasks.jsonl", "refusing to read non-regular file" in proc.stderr)

    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_tasks(project, [item])
        docs = _canonical_docs(project)
        seq_target = docs / "seq-target.json"
        seq_target.write_text('{"t": 1}\n', encoding="utf-8")
        (docs / ".task-seq").symlink_to(seq_target)
        _assert_non_regular_task_read_fails(project, "new", "Should fail on seq")

    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_tasks(project, [item])
        docs = _canonical_docs(project)
        seq_lock_target = docs / "seq-lock-target"
        seq_lock_target.write_text("", encoding="utf-8")
        (docs / ".task-seq.lock").symlink_to(seq_lock_target)
        _assert_non_regular_task_read_fails(project, "new", "Should fail on seq lock")

    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_tasks(project, [item])
        docs = _canonical_docs(project)
        lock_target = docs / "tasks-lock-target"
        lock_target.write_text("", encoding="utf-8")
        (docs / "tasks.lock").symlink_to(lock_target)
        _assert_non_regular_task_read_fails(project, "sync")

    if hasattr(os, "mkfifo"):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            _write_tasks(project, [item])
            docs = _canonical_docs(project)
            (docs / "tasks.jsonl").unlink()
            os.mkfifo(docs / "tasks.jsonl")
            for command in commands:
                _assert_non_regular_task_read_fails(project, *command)


def test_goalflight_task_sequence_write_uses_atomic_fsync_writer() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_tasks(project, [])
        module = _load_goalflight_task_module()
        store = module.TaskStore(project)
        calls: list[tuple[Path, str]] = []
        original = module._atomic_write_text

        def tracking_atomic_write(path: Path, text: str, *, prefix: str = ".tmp-") -> None:
            calls.append((path, prefix))
            original(path, text, prefix=prefix)

        module._atomic_write_text = tracking_atomic_write
        try:
            item_id = store.reserve_id("t")
        finally:
            module._atomic_write_text = original
        assert_true("reserved first task id", item_id == "t-001")
        assert_true("sequence used atomic writer", len(calls) == 1)
        assert_true("sequence atomic path", calls[0][0].resolve() == (_canonical_docs(project) / ".task-seq").resolve())
        assert_true("sequence atomic prefix", calls[0][1] == ".task-seq-")
        assert_true("sequence file written", json.loads((_canonical_docs(project) / ".task-seq").read_text())["t"] == 1)


def test_goalflight_task_max_sequence_avoids_publish_recovery_under_store_lock() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_tasks(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-005",
                    "kind": "task",
                    "title": "Existing task",
                    "blocked_by": [],
                    "links": [],
                    "done": False,
                }
            ],
        )
        module = _load_goalflight_task_module()
        store = module.TaskStore(project)
        store.publish_marker_path.write_text('{"generation":"survived-crash"}\n', encoding="utf-8")

        def fail_recovery() -> None:
            raise AssertionError("publish recovery re-entered while store lock was held")

        store._recover_interrupted_publish = fail_recovery
        with store.store_lock():
            item_id = store.reserve_id("t")
        assert_true("sequence advanced without publish recovery", item_id == "t-006")


def test_goalflight_task_next_nudge_logs_failure_once_per_key() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        module = _load_goalflight_task_module()
        module._NEXT_NUDGE_LOGGED_FAILURES.clear()
        rows = [{"id": "t-001"}, {"id": "t-002"}]
        fake_messages = types.SimpleNamespace(
            default_messages_dir=lambda: (_ for _ in ()).throw(RuntimeError("mail unavailable"))
        )
        original_messages = sys.modules.get("goalflight_messages")
        sys.modules["goalflight_messages"] = fake_messages
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                module._post_next_nudge(rows, project)
                module._post_next_nudge(rows, project)
        finally:
            if original_messages is None:
                sys.modules.pop("goalflight_messages", None)
            else:
                sys.modules["goalflight_messages"] = original_messages
        text = stderr.getvalue()
        assert_true("nudge failure logged once", text.count("next nudge failed") == 1)
        assert_true("nudge failure includes exception", "mail unavailable" in text)


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
        stale = dict(item)
        stale["title"] = "Stale mirror title"
        (_canonical_dashboard(project) / "tasks-data.js").write_text("window.GF_ITEMS = " + json.dumps([stale], indent=2) + ";\n", encoding="utf-8")

        proc = run_task(project, "sync", "--by", "watcher")
        assert_true(f"sync exits 0: {proc.stderr}", proc.returncode == 0)
        proc = run_live_checker(project)
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
        data_js = (_canonical_dashboard(project) / "tasks-data.js").read_text(encoding="utf-8")
        assert_true("script end escaped", "</script" not in data_js.lower())
        assert_true("raw img tag escaped", "<img" not in data_js.lower())
        assert_true("json payload carries escaped script start", "\\u003c/script" in data_js.lower())
        proc = run_live_checker(project)
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
                "id": "t-004",
                "kind": "task",
                "title": "Deferred task.",
                "blocked_by": [],
                "links": [],
                "done": False,
                "lane": "deferred",
            },
            {
                "id": "t-005",
                "kind": "task",
                "title": "Held task.",
                "blocked_by": [],
                "links": [],
                "done": False,
                "lane": "held",
            },
            {
                "id": "t-006",
                "kind": "task",
                "title": "Free-text lane task.",
                "blocked_by": [],
                "links": [],
                "done": False,
                "lane": "release",
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

        docs = _canonical_docs(project)
        task_md = (docs / "task-decomposition.md").read_text(encoding="utf-8")
        done_md = (docs / "tasks-done.md").read_text(encoding="utf-8")
        bug_md = (docs / "bug-backlog.md").read_text(encoding="utf-8")
        bugs_done_md = (docs / "bugs-done.md").read_text(encoding="utf-8")

        assert_true("waiting section present", "## Waiting" in task_md)
        assert_true("unresolved bug blocker linked", "[b-001](ticket.html?id=b-001)" in task_md)
        assert_true("cross-kind decision link rendered", "[q-001](ticket.html?id=q-001)" in task_md)
        assert_true("resolved blocker stays to-do", "### t-003" in task_md.split("## In progress", 1)[0])
        assert_true("reserved-lane backlog section present", "## Backlog" in task_md)
        active_task_md, task_backlog_md = task_md.split("## Backlog", 1)
        assert_true("deferred task rendered in backlog section", "### t-004" in task_backlog_md)
        assert_true("held task rendered in backlog section", "### t-005" in task_backlog_md)
        assert_true("free-text lane excluded from reserved backlog", "### t-006" not in task_backlog_md)
        assert_true("free-text lane remains in active sections", "### t-006" in active_task_md)
        assert_true("deferred task excluded from active sections", "### t-004" not in active_task_md)
        assert_true("held task excluded from active sections", "### t-005" not in active_task_md)
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
    test_drift_undefined_value_in_data_js_fails()
    test_goalflight_task_new_allocator_concurrency()
    test_goalflight_task_status_uses_breadcrumb_when_ledger_missing()
    test_goalflight_task_status_filters_ledger_by_project_root()
    test_goalflight_task_status_uses_latest_dispatch_breadcrumb()
    test_goalflight_task_sync_writes_mirror_only_derived_status()
    test_goalflight_task_sync_appends_plural_task_ids()
    test_goalflight_task_list_filters_outstanding_awaiting_review_since()
    test_goalflight_task_list_lane_facet_and_status_collision()
    test_goalflight_task_edit_existing_item_fields_and_audit()
    test_goalflight_task_set_prompt_path_rejects_unsafe_paths_without_mutation()
    test_goalflight_task_set_blocked_by_rejects_invalid_item_ids_without_mutation()
    test_goalflight_task_set_prompt_path_dispatch_frontier_dry_run_uses_prompt_file()
    test_goalflight_task_two_state_accept_and_review_breadcrumb()
    test_goalflight_task_review_captures_confirmed_bug_item()
    test_goalflight_task_harvest_idempotent_with_source_links_and_history()
    test_goalflight_task_harvest_ignores_skeleton_placeholders()
    test_goalflight_task_harvest_allows_real_angle_bracket_titles()
    test_goalflight_task_harvest_keeps_literal_punctuation_distinct()
    test_goalflight_task_harvest_allows_nested_generated_basename_sources()
    test_goalflight_task_schema_version_tolerance_and_read_api()
    test_goalflight_task_append_dispatch_breadcrumbs_preserves_history()
    test_goalflight_task_atomic_write_rejects_bad_content()
    test_goalflight_task_refuses_symlinked_state_write_dirs()
    test_goalflight_task_interrupted_publish_marker_repairs_mirror()
    test_goalflight_task_resume_history_uses_atomic_writer()
    test_goalflight_task_resume_history_filters_subset_race_under_lock()
    test_goalflight_task_rejects_non_regular_store_files_without_hanging()
    test_goalflight_task_sequence_write_uses_atomic_fsync_writer()
    test_goalflight_task_max_sequence_avoids_publish_recovery_under_store_lock()
    test_goalflight_task_next_nudge_logs_failure_once_per_key()
    test_goalflight_task_sync_repairs_stale_mirror()
    test_goalflight_task_data_js_escapes_script_end_and_html()
    test_goalflight_task_sync_generates_markdown_views()
    print("OK: 39 tasks mirror/task-store tests pass")


if __name__ == "__main__":
    main()
