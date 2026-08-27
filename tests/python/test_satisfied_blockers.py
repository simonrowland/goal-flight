#!/usr/bin/env python3
"""Satisfied blockers are a historical record, not a live gate."""

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


def test_unsatisfied_blockers_predicate_classes() -> None:
    by_id = {
        "t-open": {"id": "t-open", "kind": "task", "done": False},
        "t-done": {"id": "t-done", "kind": "task", "schema_version": 1, "done": True, "done_reviewed": False},
        "t-accepted": {"id": "t-accepted", "kind": "task", "done": True, "done_reviewed": True},
        "q-closed": {"id": "q-closed", "kind": "decision", "done": True},
        "legacy": {"id": "legacy", "kind": "task", "done": True},
    }
    assert_eq("open gates", T.unsatisfied_blockers({"blocked_by": ["t-open"]}, by_id), ["t-open"])
    assert_eq("closed done is satisfied", T.unsatisfied_blockers({"blocked_by": ["t-done"]}, by_id), [])
    assert_eq("done-reviewed is satisfied", T.unsatisfied_blockers({"blocked_by": ["t-accepted"]}, by_id), [])
    assert_eq("closed decision is satisfied", T.unsatisfied_blockers({"blocked_by": ["q-closed"]}, by_id), [])
    assert_eq("tombstoned-as-done is satisfied", T.unsatisfied_blockers({"blocked_by": ["legacy"]}, by_id), [])
    assert_eq(
        "missing id gates and is named",
        T.unsatisfied_blockers({"blocked_by": ["t-missing"]}, by_id),
        ["t-missing"],
    )
    assert_eq(
        "mixed list keeps only live/unresolvable, in stored order",
        T.unsatisfied_blockers({"blocked_by": ["t-done", "t-open", "t-ghost", "t-accepted"]}, by_id),
        ["t-open", "t-ghost"],
    )
    assert_eq("empty blocked_by", T.unsatisfied_blockers({"blocked_by": []}, by_id), [])


def test_done_succeeds_when_blockers_are_all_done() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        blocker = _new(project, env, "Closed blocker")
        item_id = _new(project, env, "Dependent on closed blocker", "--blocked-by", blocker)
        closed = _run_task(project, env, "done", blocker)
        assert_true(f"close blocker exits 0: {closed.stderr}", closed.returncode == 0)
        proc = _run_task(project, env, "done", item_id)
        assert_true(f"done without --force exits 0: {proc.stderr}", proc.returncode == 0)
        assert_eq("done prints the id", proc.stdout.strip(), item_id)
        item = _show(project, env, item_id)
        assert_true("dependent is done", item.get("done") is True)
        assert_eq("blocked_by is not pruned", item.get("blocked_by"), [blocker])
        actions = [entry.get("action") for entry in item.get("audit", [])]
        assert_true("normal done audit recorded", "done" in actions)


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
        assert_true("refusal keeps --force hint", "--force" in proc.stderr)
        item = _show(project, env, item_id)
        assert_true("unresolvable refusal does not close", item.get("done") is not True)


def test_next_includes_satisfied_blockers_and_excludes_live() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        closed = _new(project, env, "Closed gate")
        open_gate = _new(project, env, "Open gate")
        done_closed = _run_task(project, env, "done", closed)
        assert_true(f"close gate exits 0: {done_closed.stderr}", done_closed.returncode == 0)
        satisfied = _new(project, env, "Satisfied blocked row", "--blocked-by", closed)
        live = _new(project, env, "Live blocked row", "--blocked-by", open_gate)
        proc = _run_task(project, env, "next", "--json")
        assert_true(f"next exits 0: {proc.stderr}", proc.returncode == 0)
        ids = [row["id"] for row in json.loads(proc.stdout)]
        assert_true("next includes satisfied-blocked row", satisfied in ids)
        assert_true("next excludes live-blocked row", live not in ids)
        assert_true("next excludes the closed blocker itself", closed not in ids)
        assert_true("next includes the still-open gate as dispatchable", open_gate in ids)


def test_list_outstanding_satisfied_blocked_is_dispatchable() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        closed = _new(project, env, "Closed gate")
        done_closed = _run_task(project, env, "done", closed)
        assert_true(f"close gate exits 0: {done_closed.stderr}", done_closed.returncode == 0)
        item_id = _new(project, env, "Satisfied blocked outstanding", "--blocked-by", closed)
        proc = _run_task(project, env, "list", "outstanding", "--json")
        assert_true(f"list outstanding exits 0: {proc.stderr}", proc.returncode == 0)
        by_id = {row["id"]: row for row in json.loads(proc.stdout)}
        assert_true("satisfied-blocked row is outstanding", item_id in by_id)
        status = by_id[item_id].get("derived_status")
        assert_eq("satisfied-blocked derived status is pending", status, "pending")
        assert_true("satisfied-blocked is not waiting/blocked", status not in {"waiting", "blocked"})


def main() -> None:
    test_unsatisfied_blockers_predicate_classes()
    test_done_succeeds_when_blockers_are_all_done()
    test_done_refuses_open_blocker_and_names_it()
    test_done_refuses_unresolvable_blocker_and_names_it()
    test_next_includes_satisfied_blockers_and_excludes_live()
    test_list_outstanding_satisfied_blocked_is_dispatchable()
    print("OK: satisfied-blocker tests pass")


if __name__ == "__main__":
    main()
