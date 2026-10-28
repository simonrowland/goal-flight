#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

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


def _write_active_notes(project: Path) -> None:
    docs = project / "docs-private"
    docs.mkdir(parents=True, exist_ok=True)
    docs.joinpath("RESUME-NOTES-2026-07-02.md").write_text("---\nstate: active\n---\n", encoding="utf-8")


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_FLEET_DIR"] = str(tmp / "fleet")
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "state" / "dispatch")
    env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = ""
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
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


_ISOLATED_ENV_KEYS = (
    "GOALFLIGHT_MESSAGES_DIR",
    "GOALFLIGHT_FLEET_DIR",
    "GOALFLIGHT_STATE_DIR",
    "GOALFLIGHT_DISPATCH_DIR",
    "GOALFLIGHT_QUESTION_OUTBOX_DIR",
    "GOALFLIGHT_TASK_STORE_DIR",
    "GOALFLIGHT_JOURNAL_DIR",
    "GOALFLIGHT_WAKE_LEDGER_DIR",
)


@contextlib.contextmanager
def _applied_env(env: dict[str, str]):
    old = {key: os.environ.get(key) for key in _ISOLATED_ENV_KEYS}
    try:
        for key in _ISOLATED_ENV_KEYS:
            os.environ[key] = env[key]
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _user_need_deliveries(env: dict[str, str], project: Path) -> list[dict]:
    """Every journaled user_need row, including later-replaced ones.

    Inbox coalesce is the wrong signal for this bug: replace_if already leaves
    one carrier envelope while each new event_uuid still lands in delivery_events.
    """
    import goalflight_journal as J

    with _applied_env(env):
        path = J.resolve_journal_path(project)
        if not path.exists():
            return []
        authority = J.Journal(project)
        rows = authority.read_all(
            "SELECT event_uuid, stream_seq, event_type, wake_class, withdrawn_at "
            "FROM delivery_events WHERE event_type = 'user_need' "
            "ORDER BY stream_seq, event_uuid"
        )
        return [dict(row) for row in rows]


def _ready_items(count: int, *, top_id: str = "t-022") -> list[dict]:
    items = [_item(top_id, "Frontier work")]
    for index in range(1, count):
        items.append(_item(f"t-x{index:03d}", f"Ready {index}"))
    return items


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
        # Reap concurrently. If the child exits while the parent blocks in the
        # watcher subprocess, it otherwise remains a zombie whose PID still
        # looks live and the correctly rejected foreign prompt marker masks
        # that harness deadlock until the subprocess timeout.
        reaper = threading.Thread(target=worker.wait)
        reaper.start()
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
        try:
            return subprocess.run(
                args,
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
        finally:
            reaper.join(timeout=5)
            if reaper.is_alive():
                worker.kill()
                reaper.join(timeout=5)


def test_resume_nudge_posts_on_text_only_and_coalesces() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [_item("t-001", "Ready A"), _item("t-002", "Ready B")])
        _write_active_notes(project)

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
        assert_true("resume text names top task", "2 tasks ready (top: t-001)" in payload["text"])
        assert_true("resume text has no interrogative", "?" not in payload["text"])

        _write_items(project, [_item("t-001", "Ready A"), _item("t-002", "Ready B"), _item("t-003", "Ready C")])
        proc = _run_session_status(project, env, "--text")
        assert_true(f"changed session-status --text exits 0: {proc.stderr}", proc.returncode == 0)
        nudges = _task_store_nudges(env, project, T.RESUME_NUDGE_KIND)
        assert_eq("added ready item keeps one standing resume nudge", len(nudges), 1)
        assert_eq("same top does not rewrite frontier ids", nudges[0]["payload"]["frontier_ids"], ["t-001", "t-002"])
        assert_eq("same top does not journal a second event", len(_user_need_deliveries(env, project)), 1)


def test_worker_task_nudges_use_the_dispatch_journal_outbox() -> None:
    import goalflight_journal as J
    import goalflight_watch as W

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [_item("t-001", "Ready A"), _item("t-002", "Ready B")])
        dispatch_id = T._next_nudge_dispatch_id(project)
        worker_dispatch_id = "worker-task-nudge"
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{worker_dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        label = "task-nudge-projection-controller"
        with _applied_env(env), mock.patch.dict(
            os.environ, {"GOALFLIGHT_DISPATCH_ID": ""}, clear=False
        ):
            authority = J.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "task-nudge-projection-test"},
            )
            assert_true("task nudge controller lease claimed", claimed.committed)
            previous = M.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_need",
                payload={
                    "text": "old next frontier",
                    "nudge_kind": T.NEXT_NUDGE_KIND,
                    "project_root": str(project.resolve()),
                },
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                source={"node": "local", "adapter": "task-store", "transport": "next-frontier"},
                replace_if=lambda item: (item.get("payload") or {}).get("nudge_kind") == T.NEXT_NUDGE_KIND,
            )
            old_event_uuid = previous["envelope"]["id"]

        with _applied_env(env), mock.patch.dict(
            os.environ, {"GOALFLIGHT_DISPATCH_ID": worker_dispatch_id}, clear=False
        ), mock.patch.object(
            J,
            "open_or_create_journal",
            side_effect=J.JournalIOError("journal access denied"),
        ), contextlib.redirect_stderr(io.StringIO()):
            T._post_next_nudge(T.TaskStore(project).next_frontier(), project)
            T.post_resume_nudge(project)
            T.post_done_suggest_nudge(["t-001"], project, worker_dispatch_id)

        queued = sorted((outbox / "journal").glob("journal-*.delivery"))
        assert_eq("each denied task nudge is in this worker's outbox", len(queued), 3)
        assert_eq(
            "outbox sequence is monotonic",
            [json.loads(path.read_text())["sequence"] for path in queued],
            [1, 2, 3],
        )

        with _applied_env(env):
            projected = W.consume_worker_journal_outbox(
                outbox,
                worker_dispatch_id,
                budget_s=5.0,
            )
            assert_eq("watcher projects each queued task nudge", projected, 3)
            authority = J.Journal(project)
            rows = authority.read_all(
                "SELECT event_uuid, projected_at, withdrawn_at FROM delivery_events "
                "WHERE project_root = ? ORDER BY stream_seq, event_uuid",
                (str(project.resolve()),),
            )
            by_event = {str(row["event_uuid"]): row for row in rows}
            assert_true("replacement withdraws prior task nudge", by_event[old_event_uuid]["withdrawn_at"] is not None)
            assert_eq("the three task-store nudges are projected", len(rows), 4)
            assert_true(
                "new task nudge projections are complete",
                all(
                    row["projected_at"] is not None
                    for event_id, row in by_event.items()
                    if event_id != old_event_uuid
                ),
            )


def test_replaced_worker_task_nudge_is_projected_exactly_once() -> None:
    import goalflight_journal as J
    import goalflight_watch as W

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        project.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        with _applied_env(env):
            J.open_or_create_journal(project)
        _write_items(project, [_item("t-001", "Ready A"), _item("t-002", "Ready B")])
        old_worker = "old-task-nudge-worker"
        new_worker = "new-task-nudge-worker"
        old_outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{old_worker}.questions"
        new_outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{new_worker}.questions"
        old_outbox.mkdir(parents=True)
        new_outbox.mkdir(parents=True)

        with _applied_env(env), mock.patch.dict(
            os.environ,
            {
                "GOALFLIGHT_DISPATCH_ID": old_worker,
                "GOALFLIGHT_QUESTION_OUTBOX_DIR": str(old_outbox),
            },
            clear=False,
        ), mock.patch.object(
            J,
            "open_or_create_journal",
            side_effect=J.JournalIOError("journal access denied"),
        ), contextlib.redirect_stderr(io.StringIO()):
            T._post_next_nudge(T.TaskStore(project).next_frontier(), project)

        old_nudge = _task_store_nudges(env, project, T.NEXT_NUDGE_KIND)[0]
        _write_items(
            project,
            [
                _item("t-001", "Ready A"),
                _item("t-002", "Ready B"),
                _item("t-003", "Ready C"),
            ],
        )
        with _applied_env(env), mock.patch.dict(
            os.environ,
            {
                "GOALFLIGHT_DISPATCH_ID": new_worker,
                "GOALFLIGHT_QUESTION_OUTBOX_DIR": str(new_outbox),
            },
            clear=False,
        ), mock.patch.object(
            J,
            "open_or_create_journal",
            side_effect=J.JournalIOError("journal access denied"),
        ), contextlib.redirect_stderr(io.StringIO()):
            T._post_next_nudge(T.TaskStore(project).next_frontier(), project)

        live_nudges = _task_store_nudges(env, project, T.NEXT_NUDGE_KIND)
        assert_eq("task store retains only the replacement nudge", len(live_nudges), 1)
        replacement = live_nudges[0]
        assert_true("replacement has a new event identity", replacement["id"] != old_nudge["id"])
        with _applied_env(env):
            assert_eq(
                "replacement outbox projects once",
                W.consume_worker_journal_outbox(new_outbox, new_worker, budget_s=2.0),
                1,
            )
        for index in range(20):
            M.post_message(
                dispatch_id=str(replacement["dispatch_id"]),
                msg_type="status",
                payload={"text": f"carrier filler {index}"},
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                source={"node": "peer", "adapter": "pytest", "transport": "worker"},
                deliver_to_worker=True,
                project_journal_delivery=False,
            )

        with _applied_env(env):
            old_projection = sum(
                W.consume_worker_journal_outbox(old_outbox, old_worker, budget_s=2.0)
                for _ in range(4)
            )
            assert_eq("superseded outbox event is skipped", old_projection, 0)
            authority = J.Journal(project)
            rows = authority.read_all(
                "SELECT event_uuid, projected_at FROM delivery_events "
                "WHERE project_root = ? ORDER BY stream_seq",
                (str(project.resolve()),),
            )
            assert_eq("only one nudge delivery row exists", len(rows), 1)
            assert_eq("the replacement is the projected event", rows[0]["event_uuid"], replacement["id"])
            assert_true("replacement delivery is projected", rows[0]["projected_at"] is not None)


def test_resume_nudge_count_drift_same_top_is_one_journal_event() -> None:
    """Reproduce relay seq 50/53/58/62/67: count drift is not five events.

    The live session re-evaluated a standing ready frontier (top stayed t-022)
    as 76→77→76→78→76 and journaled a new drain line each time. Inbox
    coalesce hid the repetition on the carrier; the journal did not.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_active_notes(project)
        counts = (76, 77, 76, 78, 76)
        for count in counts:
            _write_items(project, _ready_items(count, top_id="t-022"))
            proc = _run_session_status(project, env, "--text")
            assert_true(f"session-status --text exits 0 at count={count}: {proc.stderr}", proc.returncode == 0)
        deliveries = _user_need_deliveries(env, project)
        assert_eq("count-drift standing state is one journal event, not five", len(deliveries), 1)
        nudges = _task_store_nudges(env, project, T.RESUME_NUDGE_KIND)
        assert_eq("inbox still has the one standing resume nudge", len(nudges), 1)
        text = str(nudges[0]["payload"]["text"])
        assert_true("standing text names the unchanged top", "top: t-022" in text)
        assert_true("standing text is not an engagement prompt", "?" not in text)
        assert_eq("wake_class stays quiet", deliveries[0]["wake_class"], "quiet")


def test_resume_nudge_silent_on_empty_frontier() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [])
        _write_active_notes(project)
        proc = _run_session_status(project, env, "--text")
        assert_true(f"empty session-status --text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_eq("empty frontier posts no resume nudge", _task_store_nudges(env, project, T.RESUME_NUDGE_KIND), [])
        assert_eq("empty frontier journals no user_need", len(_user_need_deliveries(env, project)), 0)


def test_watcher_done_suggest_posts_once_per_completion_and_dedups() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [_item("t-001", "Ready A"), _item("t-002", "Ready B")])
        worker_code = (
            "import time; time.sleep(0.1); "
            "print('COMPLETE: dispatch-1 — done', flush=True)"
        )
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
        assert_true("done-suggest text names the finished tasks", "worker says done: t-001, t-002" in payload["text"])
        assert_true("done-suggest text has no interrogative", "?" not in payload["text"])


def test_watcher_prompt_echo_does_not_post_done_suggest() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_items(project, [_item("t-001", "Ready A")])
        prompt = tmp / "prompt.md"
        prompt.write_text("COMPLETE: dispatch-echo — done\n", encoding="utf-8")
        proc = _run_watcher_once(
            project,
            env,
            tmp,
            dispatch_id="dispatch-echo",
            task_ids="t-001",
            worker_code="import time; time.sleep(0.1)",
            ignore_prompt=prompt,
            initial_tail="COMPLETE: dispatch-echo — done\n",
        )
        assert_true(f"watcher prompt echo exits without crashing: {proc.stderr}", proc.returncode in (0, 1, 3))
        assert_eq("prompt echo posts no done-suggest", _task_store_nudges(env, project, T.DONE_SUGGEST_NUDGE_KIND), [])


def test_resume_nudge_new_top_still_posts() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        env = _env(tmp)
        _write_active_notes(project)
        _write_items(project, [_item("t-022", "Old top"), _item("t-040", "Still ready")])
        proc = _run_session_status(project, env, "--text")
        assert_true(f"first session-status --text exits 0: {proc.stderr}", proc.returncode == 0)
        assert_eq("first top is one event", len(_user_need_deliveries(env, project)), 1)

        _write_items(project, [_item("t-023", "New top"), _item("t-040", "Still ready")])
        proc = _run_session_status(project, env, "--text")
        assert_true(f"new-top session-status --text exits 0: {proc.stderr}", proc.returncode == 0)
        deliveries = _user_need_deliveries(env, project)
        assert_eq("new frontier item is a second event", len(deliveries), 2)
        assert_eq("new top stays quiet", {row["wake_class"] for row in deliveries}, {"quiet"})
        nudges = _task_store_nudges(env, project, T.RESUME_NUDGE_KIND)
        assert_eq("inbox coalesces to the current top", len(nudges), 1)
        text = str(nudges[0]["payload"]["text"])
        assert_true("current text names the new top", "top: t-023" in text)
        assert_true("new-top text has no interrogative", "?" not in text)


def test_done_suggest_same_tasks_new_dispatch_is_one_event() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        project.mkdir()
        env = _env(tmp)
        with _applied_env(env):
            T.post_done_suggest_nudge(["t-257"], project, "dispatch-a")
            T.post_done_suggest_nudge(["t-257"], project, "dispatch-b")
        deliveries = _user_need_deliveries(env, project)
        assert_eq("same finished task is one event across dispatches", len(deliveries), 1)
        assert_eq("done-suggest wake_class stays quiet", deliveries[0]["wake_class"], "quiet")
        nudges = _task_store_nudges(env, project, T.DONE_SUGGEST_NUDGE_KIND)
        assert_eq("one standing done-suggest", len(nudges), 1)
        text = str(nudges[0]["payload"]["text"])
        assert_true("done-suggest names the task", "worker says done: t-257" in text)
        assert_true("done-suggest has no interrogative", "?" not in text)


def test_resolve_journal_path_unresolvable_is_unreadable_not_a_write_refuse() -> None:
    """Naming a journal is a read. A missing checkout must not raise TaskError.

    Reverting ``resolve_journal_path`` to ``resolve_project_root`` raises
    TaskError here and kills a caller that was only asking where the journal
    is. Capture of the same path must still refuse.
    """
    import goalflight_journal as J

    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "no-such-project"
        assert_true("fixture root does not exist", not missing.exists())
        try:
            J.resolve_journal_path(missing)
        except J.JournalIOError as exc:
            text = str(exc)
            assert_true("read names the unresolvable root", "unresolvable project root" in text)
            assert_true(
                "read does not refuse as a write",
                "Refusing to write to another store" not in text,
            )
        except T.TaskError as exc:
            raise AssertionError(
                f"resolve_journal_path used the write-refuse helper: {exc}"
            ) from exc
        else:
            raise AssertionError("expected JournalIOError for an unresolvable root")
        try:
            T.resolve_project_root(str(missing))
        except T.TaskError as exc:
            assert_true(
                "write of the same path still refuses",
                "Refusing to write to another store" in str(exc),
            )
        else:
            raise AssertionError("write path must still refuse an unresolvable root")


def test_done_suggest_new_task_still_posts() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        project.mkdir()
        env = _env(tmp)
        with _applied_env(env):
            T.post_done_suggest_nudge(["t-257"], project, "dispatch-a")
            T.post_done_suggest_nudge(["t-258"], project, "dispatch-b")
        deliveries = _user_need_deliveries(env, project)
        assert_eq("a newly finished task is a second event", len(deliveries), 2)
        assert_eq("new done-suggest stays quiet", {row["wake_class"] for row in deliveries}, {"quiet"})
        nudges = _task_store_nudges(env, project, T.DONE_SUGGEST_NUDGE_KIND)
        assert_eq("inbox coalesces to the latest done-suggest", len(nudges), 1)
        text = str(nudges[0]["payload"]["text"])
        assert_true("latest text names the new task", "worker says done: t-258" in text)
        assert_true("new-task done-suggest has no interrogative", "?" not in text)


def main() -> None:
    test_resume_nudge_posts_on_text_only_and_coalesces()
    test_worker_task_nudges_use_the_dispatch_journal_outbox()
    test_resume_nudge_count_drift_same_top_is_one_journal_event()
    test_resume_nudge_new_top_still_posts()
    test_resume_nudge_silent_on_empty_frontier()
    test_watcher_done_suggest_posts_once_per_completion_and_dedups()
    test_watcher_prompt_echo_does_not_post_done_suggest()
    test_done_suggest_same_tasks_new_dispatch_is_one_event()
    test_done_suggest_new_task_still_posts()
    test_resolve_journal_path_unresolvable_is_unreadable_not_a_write_refuse()
    print("OK: task nudge tests pass")


if __name__ == "__main__":
    main()
