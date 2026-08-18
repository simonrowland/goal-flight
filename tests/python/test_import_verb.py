#!/usr/bin/env python3
"""Focused tests for importing agent-authored JSONL draft sidecars."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import goalflight_task as task_module

TASK = ROOT / "goalflight_task.py"
CHECKER = ROOT / "scripts" / "check_tasks_mirror.js"
NODE = shutil.which("node")


def run_task(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(project / ".goal-flight-state")
    return subprocess.run(
        [sys.executable, str(TASK), "--project-root", str(project), *args],
        cwd=str(project), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
    )


def write_sidecar(project: Path, records: list[dict]) -> Path:
    path = project / "drafts.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    return path


def read_items(project: Path) -> list[dict]:
    path = project / "docs-private" / "tasks.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def draft(draft_id: str, title: str, **extra) -> dict:
    return {"schema_version": 1, "id": draft_id, "kind": "task", "title": title, **extra}


def test_happy_path_forward_blocker_remap_and_preservation() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [
            draft("draft-first", "First", blocked_by=["draft-later"], links=["doc.md", "doc.md"], tags=["a", "a"]),
            {**draft("draft-later", "Later", lane="active"), "kind": "bug"},
            {**draft("draft-choice", "Choose"), "kind": "decision"},
        ])
        proc = run_task(project, "import", source.name, "--json")
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["imported"] == 3 and payload["errors"] == 0
        assert payload["id_mapping"] == {"draft-first": "t-001", "draft-later": "b-001", "draft-choice": "q-001"}
        by_id = {item["id"]: item for item in read_items(project)}
        assert by_id["t-001"]["blocked_by"] == ["b-001"]
        assert by_id["t-001"]["links"] == ["doc.md", "doc.md"]
        assert by_id["t-001"]["tags"] == ["a", "a"]
        assert by_id["t-001"]["lane"] == "deferred" and by_id["b-001"]["lane"] == "active"
        assert by_id["t-001"]["import_source"] == str(source.resolve())
        assert by_id["t-001"]["import_draft_id"] == "draft-first"
        assert by_id["t-001"]["audit"][0]["action"] == "import"


def test_unknown_ref_error_is_atomic_and_lists_offenders() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [draft("draft-one", "One", blocked_by=["draft-missing", "t-999"])])
        proc = run_task(project, "import", source.name, "--json")
        assert proc.returncode == 1
        payload = json.loads(proc.stdout)
        assert payload["imported"] == 0 and payload["skipped_duplicates"] == 0
        assert payload["errors"] == 1
        assert "draft-missing" in payload["error_messages"][0] and "t-999" in payload["error_messages"][0]
        assert read_items(project) == []


def test_existing_real_id_passthrough() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        existing = run_task(project, "new", "Existing").stdout.strip()
        source = write_sidecar(project, [draft("draft-child", "Child", blocked_by=[existing])])
        proc = run_task(project, "import", source.name, "--json")
        assert proc.returncode == 0, proc.stderr
        child = next(item for item in read_items(project) if item["title"] == "Child")
        assert child["blocked_by"] == [existing]


def test_idempotent_rerun_and_same_content_with_new_draft_id() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [draft("draft-one", " Stable   Title ")])
        first = run_task(project, "import", source.name, "--json")
        assert first.returncode == 0, first.stderr
        source = write_sidecar(project, [draft("draft-renamed", "stable title")])
        second = run_task(project, "import", source.name, "--json")
        payload = json.loads(second.stdout)
        assert second.returncode == 0 and payload["imported"] == 0 and payload["skipped_duplicates"] == 1
        assert payload["id_mapping"]["draft-renamed"] == "t-001"
        assert len(read_items(project)) == 1


def test_same_set_values_in_different_order_dedup_silently() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [
            draft("draft-one", "Same", blocked_by=["draft-b", "draft-c"], links=["a", "b"], tags=["x", "y"]),
            draft("draft-two", "same", blocked_by=["draft-c", "draft-b"], links=["b", "a"], tags=["y", "x"]),
            draft("draft-b", "Blocker B"),
            draft("draft-c", "Blocker C"),
        ])
        proc = run_task(project, "import", source.name, "--json")
        payload = json.loads(proc.stdout)
        assert proc.returncode == 0, proc.stderr
        assert payload["imported"] == 3 and payload["skipped_duplicates"] == 1 and payload["errors"] == 0
        assert payload["id_mapping"]["draft-one"] == payload["id_mapping"]["draft-two"]


def test_same_key_different_lane_blockers_or_links_is_a_hard_error() -> None:
    variants = [
        (draft("draft-one", "Same", lane="active"), draft("draft-two", "same", lane="deferred")),
        (draft("draft-one", "Same", blocked_by=["draft-b"]), draft("draft-two", "same", blocked_by=["draft-c"])),
        (draft("draft-one", "Same", links=["a"]), draft("draft-two", "same", links=["b"])),
    ]
    for first, second in variants:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            source = write_sidecar(project, [first, second, draft("draft-b", "B"), draft("draft-c", "C")])
            proc = run_task(project, "import", source.name, "--json")
            payload = json.loads(proc.stdout)
            assert proc.returncode == 1
            assert payload["imported"] == 0 and payload["errors"] == 1
            assert "content collision" in payload["error_messages"][0]
            assert read_items(project) == []


def test_collision_with_existing_import_item_dedups_instead_of_duplicating() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [draft("draft-original", "Existing import", links=["doc"], tags=["tag"])])
        first = run_task(project, "import", source.name, "--json")
        assert first.returncode == 0, first.stderr
        source = write_sidecar(project, [draft("draft-new", "existing   import", links=["doc"], tags=["tag"])])
        second = run_task(project, "import", source.name, "--json")
        payload = json.loads(second.stdout)
        assert second.returncode == 0, second.stderr
        assert payload["imported"] == 0 and payload["skipped_duplicates"] == 1 and payload["errors"] == 0
        assert len(read_items(project)) == 1


def test_same_title_with_different_tags_is_a_hard_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [
            draft("draft-one", "Same title", tags=["alpha"]),
            draft("draft-two", "same   title", tags=["beta"]),
        ])
        proc = run_task(project, "import", source.name, "--json")
        assert proc.returncode == 1
        payload = json.loads(proc.stdout)
        assert payload["imported"] == 0 and payload["errors"] == 1
        assert "content collision" in payload["error_messages"][0]
        assert "draft-one" in payload["error_messages"][0] and "draft-two" in payload["error_messages"][0]
        assert read_items(project) == []


def test_validation_failure_does_not_consume_ids() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        store_base = project / ".goal-flight-state"
        with mock.patch.dict(os.environ, {"GOALFLIGHT_TASK_STORE_DIR": str(store_base)}):
            store = task_module.TaskStore(project)
            assert store.reserve_id("t") == "t-001"
            before = store.seq_path.read_bytes()
            source = write_sidecar(project, [draft("draft-bad", "Bad")])
            args = SimpleNamespace(sidecar=str(source), json=True, dry_run=False, actor="test")
            with mock.patch.object(store, "save_items_atomic", side_effect=task_module.TaskError("validation failed")):
                try:
                    task_module._cmd_import(store, args)
                except task_module.TaskError as exc:
                    assert "validation failed" in str(exc)
                else:
                    raise AssertionError("expected forced validation failure")
            assert store.seq_path.read_bytes() == before
            assert store.reserve_id("t") == "t-002"


def test_self_blocked_draft_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [draft("draft-self", "Self", blocked_by=["draft-self"])])
        proc = run_task(project, "import", source.name, "--json")
        assert proc.returncode == 1
        payload = json.loads(proc.stdout)
        assert payload["imported"] == 0 and payload["errors"] == 1
        assert "self blocked_by reference" in payload["error_messages"][0]
        assert "draft-self" in payload["error_messages"][0]
        assert read_items(project) == []


def test_two_record_dependency_cycle_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [
            draft("draft-a", "Alpha", blocked_by=["draft-b"]),
            draft("draft-b", "Beta", blocked_by=["draft-a"]),
        ])
        proc = run_task(project, "import", source.name, "--json")
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1
        assert payload["imported"] == 0
        cycle_error = next(message for message in payload["error_messages"] if "dependency cycle" in message)
        assert "draft-a" in cycle_error and "draft-b" in cycle_error
        assert read_items(project) == []


def test_acyclic_dependency_chain_imports() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [
            draft("draft-a", "Alpha", blocked_by=["draft-b"]),
            draft("draft-b", "Beta", blocked_by=["draft-c"]),
            draft("draft-c", "Gamma"),
        ])
        proc = run_task(project, "import", source.name, "--json")
        payload = json.loads(proc.stdout)
        assert proc.returncode == 0, proc.stderr
        assert payload["imported"] == 3 and payload["errors"] == 0


def test_same_title_cross_reference_cannot_collapse_to_self_blocking() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [
            draft("draft-one", "Same", blocked_by=["draft-two"]),
            draft("draft-two", "same", blocked_by=["draft-one"]),
        ])
        proc = run_task(project, "import", source.name, "--json")
        assert proc.returncode == 1
        payload = json.loads(proc.stdout)
        assert "self blocked_by reference" in payload["error_messages"][0]
        assert "draft-one" in payload["error_messages"][0] and "draft-two" in payload["error_messages"][0]


def test_malformed_arrays_are_reported_without_traceback() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [draft("draft-bad", "Bad", blocked_by=42, tags=["ok", 7])])
        proc = run_task(project, "import", source.name, "--json")
        assert proc.returncode == 1
        payload = json.loads(proc.stdout)
        assert payload["errors"] == 2
        assert any("blocked_by must be an array of strings" in message for message in payload["error_messages"])
        assert any("tags must be an array of strings" in message for message in payload["error_messages"])
        assert "Traceback" not in proc.stderr


def test_dry_run_does_not_write_or_reserve_ids() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [draft("draft-one", "One")])
        proc = run_task(project, "import", source.name, "--dry-run", "--json")
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["id_mapping"] == {"draft-one": "t-001"}
        assert not (project / "docs-private").exists()
        applied = run_task(project, "import", source.name, "--json")
        assert json.loads(applied.stdout)["id_mapping"] == {"draft-one": "t-001"}


def test_refuses_real_id_fields_and_reports_json_errors() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [{"schema_version": 1, "id": "t-123", "kind": "task", "title": "No"}])
        proc = run_task(project, "import", source.name, "--json")
        assert proc.returncode == 1
        payload = json.loads(proc.stdout)
        assert payload["errors"] == 1 and "looks like a real store id" in payload["error_messages"][0]
        assert read_items(project) == []


def test_mirror_pair_consistent_after_import() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        source = write_sidecar(project, [draft("draft-one", "One")])
        proc = run_task(project, "import", source.name)
        assert proc.returncode == 0, proc.stderr
        checked = subprocess.run(
            [NODE, str(CHECKER), str(project / "docs-private"), str(project / "dashboard")],
            cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        )
        assert checked.returncode == 0, checked.stderr


if __name__ == "__main__":
    test_happy_path_forward_blocker_remap_and_preservation()
    test_unknown_ref_error_is_atomic_and_lists_offenders()
    test_existing_real_id_passthrough()
    test_idempotent_rerun_and_same_content_with_new_draft_id()
    test_same_set_values_in_different_order_dedup_silently()
    test_same_key_different_lane_blockers_or_links_is_a_hard_error()
    test_collision_with_existing_import_item_dedups_instead_of_duplicating()
    test_same_title_with_different_tags_is_a_hard_error()
    test_validation_failure_does_not_consume_ids()
    test_self_blocked_draft_is_rejected()
    test_two_record_dependency_cycle_is_rejected()
    test_acyclic_dependency_chain_imports()
    test_same_title_cross_reference_cannot_collapse_to_self_blocking()
    test_malformed_arrays_are_reported_without_traceback()
    test_dry_run_does_not_write_or_reserve_ids()
    test_refuses_real_id_fields_and_reports_json_errors()
    test_mirror_pair_consistent_after_import()
    print("PASS")
