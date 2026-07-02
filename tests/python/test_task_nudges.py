#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WATCH = SCRIPTS / "goalflight_watch.py"
SESSION_STATUS = SCRIPTS / "goalflight_session_status.py"
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


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    return env


def _task_store_nudges(env: dict[str, str], project: Path, kind: str) -> list[dict]:
    inbox = M.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), T._next_nudge_dispatch_id(project))
    if not inbox.exists():
        return []
    return [
        envelope
        for envelope in M.read_envelopes(inbox)
        if envelope.get("type") == "user_need" and (envelope.get("payload") or {}).get("nudge_kind") == kind
    ]


def _run_session_status(project: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
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


def _run_watcher_once(
    project: Path,
    env: dict[str, str],
    tmp: Path,
    *,
    dispatch_id: str,
    task_ids: str,
    worker_code: str,
    ignore_prompt: Path | None = None,
    initial_tail: str = "",
) -> subprocess.CompletedProcess[str]:
    tail = tmp / f"{dispatch_id}.log"
    status = tmp / f"{dispatch_id}.status.json"
    tail.write_text(initial_tail, encoding="utf-8")
    with tail.open("a", encoding="utf-8") as log:
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        args = [
            sys.executable,
            str(WATCH),
            "--pid",
            str(worker.pid),
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--dispatch-id",
            dispatch_id,
            "--project-root",
            str(project),
            "--task-ids",
            task_ids,
            "--poll-secs",
            "0.05",
            "--max-idle-secs",
            "2",
        ]
        if ignore_prompt is not None:
            args.extend(["--ignore-prompt-file", str(ignore_prompt)])
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        worker.wait(timeout=5)
        return proc


def test_resume_nudge_posts_on_text_only_and_coalesces() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [_item("t-001", "Ready A"), _item("t-002", "Ready B")])

        proc = _run_session_status(project, env, "--json")
        assert_true(f"session-status --json exits 0: {proc.stderr}", proc.returncode == 0)
        assert_eq("json path posts no resume nudge", _task_store_nudges(env, project, T.RESUME_NUDGE_KIND), [])

        for _ in range(2):
            proc = _run_session_status(project, env, "--text")
            assert_true(f"session-status --text exits 0: {proc.stderr}", proc.returncode == 0)
        nudges = _task_store_nudges(env, project, T.RESUME_NUDGE_KIND)
        assert_eq("resume nudge deduped", len(nudges), 1)
        payload = nudges[0]["payload"]
        assert_eq("resume frontier ids", payload["frontier_ids"], ["t-001", "t-002"])
        assert_true("resume text names top task", "2 tasks ready (top: t-001) -> continue?" in payload["text"])

        _write_items(project, [_item("t-001", "Ready A"), _item("t-002", "Ready B"), _item("t-003", "Ready C")])
        proc = _run_session_status(project, env, "--text")
        assert_true(f"changed session-status --text exits 0: {proc.stderr}", proc.returncode == 0)
        nudges = _task_store_nudges(env, project, T.RESUME_NUDGE_KIND)
        assert_eq("changed resume frontier coalesces to one current nudge", len(nudges), 1)
        assert_eq("changed resume frontier ids", nudges[0]["payload"]["frontier_ids"], ["t-001", "t-002", "t-003"])


def test_resume_nudge_silent_on_empty_frontier() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [])
        proc = _run_session_status(project, env, "--text")
        assert_true(f"empty session-status --text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_eq("empty frontier posts no resume nudge", _task_store_nudges(env, project, T.RESUME_NUDGE_KIND), [])


def test_watcher_done_suggest_posts_once_per_completion_and_dedups() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [_item("t-001", "Ready A"), _item("t-002", "Ready B")])
        worker_code = "import time; time.sleep(0.1); print('COMPLETE: done', flush=True)"
        for _ in range(2):
            proc = _run_watcher_once(
                project,
                env,
                tmp,
                dispatch_id="dispatch-1",
                task_ids="t-002,t-001",
                worker_code=worker_code,
            )
            assert_true(f"watcher completion exits complete or already reaped: {proc.stderr}", proc.returncode in (0, 1))
        nudges = _task_store_nudges(env, project, T.DONE_SUGGEST_NUDGE_KIND)
        assert_eq("done-suggest deduped", len(nudges), 1)
        payload = nudges[0]["payload"]
        assert_eq("done-suggest task ids sorted", payload["task_ids"], ["t-001", "t-002"])
        assert_eq("done-suggest dispatch", payload["worker_dispatch_id"], "dispatch-1")
        assert_true("done-suggest text asks review accept", "worker says done: t-001, t-002 -> review + accept?" in payload["text"])


def test_watcher_prompt_echo_does_not_post_done_suggest() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [_item("t-001", "Ready A")])
        prompt = tmp / "prompt.md"
        prompt.write_text("COMPLETE: done\n", encoding="utf-8")
        proc = _run_watcher_once(
            project,
            env,
            tmp,
            dispatch_id="dispatch-echo",
            task_ids="t-001",
            worker_code="import time; time.sleep(0.1)",
            ignore_prompt=prompt,
            initial_tail="COMPLETE: done\n",
        )
        assert_true(f"watcher prompt echo exits without crashing: {proc.stderr}", proc.returncode in (0, 1, 3))
        assert_eq("prompt echo posts no done-suggest", _task_store_nudges(env, project, T.DONE_SUGGEST_NUDGE_KIND), [])


def main() -> None:
    test_resume_nudge_posts_on_text_only_and_coalesces()
    test_resume_nudge_silent_on_empty_frontier()
    test_watcher_done_suggest_posts_once_per_completion_and_dedups()
    test_watcher_prompt_echo_does_not_post_done_suggest()
    print("OK: task nudge tests pass")


if __name__ == "__main__":
    main()
