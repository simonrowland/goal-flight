#!/usr/bin/env python3
"""Satisfied blockers are done-reviewed; awaiting-review still gates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "goalflight_task.py"
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import goalflight_task as T  # noqa: E402


def assert_eq(name: str, got: object, exp: object) -> None:
    if got != exp:
        raise AssertionError(f"{name}: got {got!r}, expected {exp!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "state" / "dispatch")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
    env["GOALFLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = os.devnull
    env["GOALFLIGHT_DISABLE_NUDGES"] = "1"
    for key in (
        "GOALFLIGHT_MESSAGES_DIR",
        "GOALFLIGHT_STATE_DIR",
        "GOALFLIGHT_DISPATCH_DIR",
        "GOALFLIGHT_TASK_STORE_DIR",
        "GOALFLIGHT_JOURNAL_DIR",
        "GOALFLIGHT_WAKE_LEDGER_DIR",
        "GOALFLIGHT_PIDFILE_DIR",
        "GOAL_FLIGHT_PIDFILE_DIR",
    ):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


def _run_task(project: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TASK), "--project-root", str(project), "--by", "tester", *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _new(project: Path, env: dict[str, str], title: str, *args: str) -> str:
    proc = _run_task(project, env, "new", title, *args)
    assert_true(f"new {title!r} exits 0: {proc.stderr}", proc.returncode == 0)
    item_id = proc.stdout.strip()
    assert_true(f"new {title!r} printed an id", bool(item_id))
    return item_id


def _show(project: Path, env: dict[str, str], item_id: str) -> dict:
    proc = _run_task(project, env, "show", item_id, "--json")
    assert_true(f"show {item_id} exits 0: {proc.stderr}", proc.returncode == 0)
    return json.loads(proc.stdout)


def _accept(project: Path, env: dict[str, str], item_id: str) -> None:
    reviewed = _run_task(project, env, "review", item_id, "--verdict", "clean", "--dispatch", f"review-{item_id}")
    assert_true(f"review {item_id} exits 0: {reviewed.stderr}", reviewed.returncode == 0)
    accepted = _run_task(project, env, "accept", item_id)
    assert_true(f"accept {item_id} exits 0: {accepted.stderr}", accepted.returncode == 0)
    item = _show(project, env, item_id)
    assert_true(f"{item_id} is done-reviewed after accept", item.get("done_reviewed") is True)


def _write_jsonl(project: Path, items: list[dict]) -> None:
    docs = project / "docs-private"
    docs.mkdir(parents=True, exist_ok=True)
    docs.joinpath("tasks.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
        encoding="utf-8",
    )


def _next_ids(project: Path, env: dict[str, str]) -> list[str]:
    proc = _run_task(project, env, "next", "--json")
    assert_true(f"next exits 0: {proc.stderr}", proc.returncode == 0)
    return [row["id"] for row in json.loads(proc.stdout)]


def test_unsatisfied_blockers_predicate_classes() -> None:
    by_id = {
        "t-open": {"id": "t-open", "kind": "task", "done": False},
        "t-done": {"id": "t-done", "kind": "task", "schema_version": 1, "done": True, "done_reviewed": False},
        "t-accepted": {"id": "t-accepted", "kind": "task", "done": True, "done_reviewed": True},
        "q-closed": {"id": "q-closed", "kind": "decision", "done": True},
    }
    assert_eq("open gates", T.unsatisfied_blockers({"blocked_by": ["t-open"]}, by_id), ["t-open"])
    assert_eq(
        "awaiting-review (bare done) gates",
        T.unsatisfied_blockers({"blocked_by": ["t-done"]}, by_id),
        ["t-done"],
    )
    assert_eq("done-reviewed is satisfied", T.unsatisfied_blockers({"blocked_by": ["t-accepted"]}, by_id), [])
    assert_eq("closed decision is satisfied", T.unsatisfied_blockers({"blocked_by": ["q-closed"]}, by_id), [])
    assert_eq(
        "raw tombstone dict without migrate-on-read still gates",
        T.unsatisfied_blockers({"blocked_by": ["legacy"]}, {"legacy": {"id": "legacy", "kind": "task", "done": True}}),
        ["legacy"],
    )
    assert_eq(
        "missing id gates and is named",
        T.unsatisfied_blockers({"blocked_by": ["t-missing"]}, by_id),
        ["t-missing"],
    )
    assert_eq(
        "empty-string blocker gates",
        T.unsatisfied_blockers({"blocked_by": [""]}, by_id),
        [""],
    )
    assert_eq(
        "mixed list keeps only live/unresolvable, in stored order",
        T.unsatisfied_blockers({"blocked_by": ["t-done", "t-open", "t-ghost", "t-accepted", ""]}, by_id),
        ["t-done", "t-open", "t-ghost", ""],
    )
    assert_eq("empty blocked_by", T.unsatisfied_blockers({"blocked_by": []}, by_id), [])


def test_done_succeeds_when_blockers_are_done_reviewed() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        blocker = _new(project, env, "Reviewed blocker")
        closed = _run_task(project, env, "done", blocker)
        assert_true(f"close blocker exits 0: {closed.stderr}", closed.returncode == 0)
        _accept(project, env, blocker)
        item_id = _new(project, env, "Dependent on reviewed blocker", "--blocked-by", blocker)
        proc = _run_task(project, env, "done", item_id)
        assert_true(f"done without --force exits 0: {proc.stderr}", proc.returncode == 0)
        assert_eq("done prints the id", proc.stdout.strip(), item_id)
        item = _show(project, env, item_id)
        assert_true("dependent is done", item.get("done") is True)
        assert_eq("blocked_by is not pruned", item.get("blocked_by"), [blocker])
        actions = [entry.get("action") for entry in item.get("audit", [])]
        assert_true("normal done audit recorded", "done" in actions)


def test_done_refuses_awaiting_review_blocker_and_names_state() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        blocker = _new(project, env, "Unreviewed blocker")
        closed = _run_task(project, env, "done", blocker)
        assert_true(f"close blocker exits 0: {closed.stderr}", closed.returncode == 0)
        item_id = _new(project, env, "Dependent on unreviewed blocker", "--blocked-by", blocker)
        proc = _run_task(project, env, "done", item_id)
        assert_true("awaiting-review blocker refuses done", proc.returncode != 0)
        assert_true("refusal names the blocker id", blocker in proc.stderr)
        assert_true("refusal names awaiting-review state", "done but awaiting review" in proc.stderr)
        assert_true("refusal tells operator to accept/review", "accept/review it" in proc.stderr)
        assert_true("refusal keeps --force hint", "--force" in proc.stderr)
        item = _show(project, env, item_id)
        assert_true("awaiting-review refusal does not close", item.get("done") is not True)
        forced = _run_task(project, env, "done", item_id, "--force")
        assert_true(f"--force still closes: {forced.stderr}", forced.returncode == 0)
        forced_item = _show(project, env, item_id)
        assert_true("--force marks done", forced_item.get("done") is True)
        assert_eq("--force leaves blocked_by in place", forced_item.get("blocked_by"), [blocker])


def test_done_refuses_open_blocker_and_names_it() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        blocker = _new(project, env, "Open blocker")
        item_id = _new(project, env, "Dependent on open blocker", "--blocked-by", blocker)
        proc = _run_task(project, env, "done", item_id)
        assert_true("open blocker refuses done", proc.returncode != 0)
        assert_true("refusal names the open id", blocker in proc.stderr)
        assert_true("open refusal is not the awaiting-review phrasing", "awaiting review" not in proc.stderr)
        assert_true("refusal keeps --force hint", "--force" in proc.stderr)
        item = _show(project, env, item_id)
        assert_true("open-blocker refusal does not close", item.get("done") is not True)
        forced = _run_task(project, env, "done", item_id, "--force")
        assert_true(f"--force still closes: {forced.stderr}", forced.returncode == 0)
        forced_item = _show(project, env, item_id)
        assert_true("--force marks done", forced_item.get("done") is True)
        assert_eq("--force leaves blocked_by in place", forced_item.get("blocked_by"), [blocker])


def test_done_refuses_unresolvable_blocker_and_names_it() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        missing = "t-999"
        item_id = _new(project, env, "Dependent on missing blocker", "--blocked-by", missing)
        proc = _run_task(project, env, "done", item_id)
        assert_true("unresolvable blocker refuses done", proc.returncode != 0)
        assert_true("refusal names the unresolvable id", missing in proc.stderr)
        assert_true("unresolvable refusal is not the awaiting-review phrasing", "awaiting review" not in proc.stderr)
        assert_true("refusal keeps --force hint", "--force" in proc.stderr)
        item = _show(project, env, item_id)
        assert_true("unresolvable refusal does not close", item.get("done") is not True)


def test_next_and_list_honor_done_reviewed_not_bare_done() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        reviewed = _new(project, env, "Reviewed gate")
        unreviewed = _new(project, env, "Unreviewed gate")
        open_gate = _new(project, env, "Open gate")
        done_reviewed = _run_task(project, env, "done", reviewed)
        assert_true(f"close reviewed gate exits 0: {done_reviewed.stderr}", done_reviewed.returncode == 0)
        _accept(project, env, reviewed)
        done_unreviewed = _run_task(project, env, "done", unreviewed)
        assert_true(f"close unreviewed gate exits 0: {done_unreviewed.stderr}", done_unreviewed.returncode == 0)
        satisfied = _new(project, env, "Dependent on reviewed gate", "--blocked-by", reviewed)
        waiting = _new(project, env, "Dependent on unreviewed gate", "--blocked-by", unreviewed)
        live = _new(project, env, "Dependent on open gate", "--blocked-by", open_gate)
        ids = _next_ids(project, env)
        assert_true("next includes done-reviewed-blocked row", satisfied in ids)
        assert_true("next excludes awaiting-review-blocked row", waiting not in ids)
        assert_true("next excludes live-blocked row", live not in ids)
        assert_true("next excludes the reviewed blocker itself", reviewed not in ids)
        assert_true("next excludes the unreviewed blocker itself", unreviewed not in ids)
        assert_true("next includes the still-open gate as dispatchable", open_gate in ids)
        proc = _run_task(project, env, "list", "outstanding", "--json")
        assert_true(f"list outstanding exits 0: {proc.stderr}", proc.returncode == 0)
        by_id = {row["id"]: row for row in json.loads(proc.stdout)}
        assert_true("done-reviewed-blocked row is outstanding", satisfied in by_id)
        assert_eq("done-reviewed-blocked derived status is pending", by_id[satisfied].get("derived_status"), "pending")
        assert_true("awaiting-review-blocked row is outstanding", waiting in by_id)
        assert_eq("awaiting-review-blocked derived status is waiting", by_id[waiting].get("derived_status"), "waiting")


def test_tombstone_blocker_via_store_read() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_jsonl(
            project,
            [
                {
                    "id": "t-001",
                    "kind": "task",
                    "title": "Legacy tombstone",
                    "done": True,
                    "blocked_by": [],
                    "links": [],
                },
                {
                    "id": "t-002",
                    "kind": "task",
                    "title": "Dependent on tombstone",
                    "done": False,
                    "blocked_by": ["t-001"],
                    "links": [],
                },
            ],
        )
        raw = (project / "docs-private" / "tasks.jsonl").read_text(encoding="utf-8")
        assert_true("seed is legacy JSONL without schema_version", "schema_version" not in raw)
        previous_store = os.environ.get("GOALFLIGHT_TASK_STORE_DIR")
        os.environ["GOALFLIGHT_TASK_STORE_DIR"] = env["GOALFLIGHT_TASK_STORE_DIR"]
        try:
            loaded = {item["id"]: item for item in T.TaskStore(project).load_items()}
        finally:
            if previous_store is None:
                os.environ.pop("GOALFLIGHT_TASK_STORE_DIR", None)
            else:
                os.environ["GOALFLIGHT_TASK_STORE_DIR"] = previous_store
        assert_true("migrate-on-read stamps tombstone done_reviewed", loaded["t-001"].get("done_reviewed") is True)
        assert_true("raw file still has no schema_version after read", "schema_version" not in raw)
        shown = _show(project, env, "t-001")
        assert_true("show surfaces migrated done_reviewed", shown.get("done_reviewed") is True)
        ids = _next_ids(project, env)
        assert_true("next includes tombstone-blocked dependent", "t-002" in ids)
        dependent = _show(project, env, "t-002")
        assert_eq("tombstone-blocked derived status is pending", dependent.get("derived_status"), "pending")
        proc = _run_task(project, env, "done", "t-002")
        assert_true(f"done on tombstone-blocked dependent exits 0: {proc.stderr}", proc.returncode == 0)


def test_empty_string_blocker_gates_guard_and_frontier() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_jsonl(
            project,
            [
                {
                    "schema_version": 1,
                    "id": "t-001",
                    "kind": "task",
                    "title": "Empty blocker token",
                    "done": False,
                    "blocked_by": [""],
                    "links": [],
                },
            ],
        )
        item = _show(project, env, "t-001")
        assert_eq("empty-string blocker derived status is waiting", item.get("derived_status"), "waiting")
        ids = _next_ids(project, env)
        assert_true("next excludes empty-string-blocked row", "t-001" not in ids)
        proc = _run_task(project, env, "done", "t-001")
        assert_true("empty-string blocker refuses done", proc.returncode != 0)
        assert_true("refusal names the empty token", "''" in proc.stderr)
        assert_true("empty-token refusal is not the awaiting-review phrasing", "awaiting review" not in proc.stderr)
        assert_true("refusal keeps --force hint", "--force" in proc.stderr)
        still = _show(project, env, "t-001")
        assert_true("empty-string refusal does not close", still.get("done") is not True)


def main() -> None:
    test_unsatisfied_blockers_predicate_classes()
    test_done_succeeds_when_blockers_are_done_reviewed()
    test_done_refuses_awaiting_review_blocker_and_names_state()
    test_done_refuses_open_blocker_and_names_it()
    test_done_refuses_unresolvable_blocker_and_names_it()
    test_next_and_list_honor_done_reviewed_not_bare_done()
    test_tombstone_blocker_via_store_read()
    test_empty_string_blocker_gates_guard_and_frontier()
    print("OK: satisfied-blocker tests pass")


if __name__ == "__main__":
    main()
