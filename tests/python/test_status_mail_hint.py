"""Regression: goalflight_status surfaces a read-side "you have mail" hint.

Controllers run goalflight_status.py constantly; the controller-mail design wants
the "you have mail" signal piggybacked onto that call (computed FRESH, fail-open,
never stored in any status JSON) so a controller learns it has open user-needs
without having to remember to poll `goalflight_messages relay`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_status as S  # noqa: E402
import goalflight_messages as M  # noqa: E402
import goalflight_task as T  # noqa: E402


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


def assert_eq(name: str, got: object, exp: object) -> None:
    if got != exp:
        raise AssertionError(f"{name}: got {got!r}, expected {exp!r}")


def _with_mail_dirs(fn):
    with tempfile.TemporaryDirectory() as d:
        msgs = Path(d) / "messages"
        fleet = Path(d) / "fleet"
        msgs.mkdir()
        fleet.mkdir()
        saved = {k: os.environ.get(k) for k in ("GOALFLIGHT_MESSAGES_DIR", "GOALFLIGHT_FLEET_DIR")}
        os.environ["GOALFLIGHT_MESSAGES_DIR"] = str(msgs)
        os.environ["GOALFLIGHT_FLEET_DIR"] = str(fleet)
        try:
            return fn(msgs, fleet)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _bare_payload(mail: dict) -> dict:
    return {
        "scope": {"project_root": "/tmp/x"},
        "capacity": {"operating_cap": 8},
        "dispatch": {"records": []},
        "capacity_state": {"leases": {}, "cooldowns": {}},
        "rate_pressure": {},
        "warnings": [],
        "mail": mail,
    }


def _task_item(item_id: str, title: str) -> dict:
    return {
        "schema_version": 1,
        "id": item_id,
        "kind": "task",
        "title": title,
        "blocked_by": [],
        "links": [],
        "done": False,
        "created_at": "2026-07-01T00:00:00+00:00",
        "created_by": "test",
    }


def test_no_mail_empty_summary() -> None:
    out = _with_mail_dirs(lambda m, f: S._mail_summary())
    assert_eq("empty inbox -> {}", out, {})


def test_open_user_need_surfaces_hint_with_detail() -> None:
    def body(msgs, fleet):
        M.post_message(
            dispatch_id="worker-7",
            msg_type="user_need",
            payload={"text": "need a decision on the schema change"},
            messages_dir=msgs,
        )
        return S._mail_summary({"worker-7"})

    out = _with_mail_dirs(body)
    assert_eq("count", out.get("count"), 1)
    need = (out.get("needs") or [{}])[0]
    assert_eq("dispatch id", need.get("dispatch_id"), "worker-7")
    assert_eq("type", need.get("type"), "user_need")
    hint = out.get("hint") or ""
    assert_true("hint names the relay command", "goalflight_messages.py relay" in hint)
    assert_true("hint carries the mail glyph", "\U0001f4ec" in hint)
    # Enough DETAIL to follow up from a status check: the need text + id appear.
    assert_true("hint shows the need text", "decision on the schema change" in hint)
    assert_true("hint shows the dispatch id", "worker-7" in hint)


def test_mail_hint_strips_control_chars() -> None:
    def body(msgs, fleet):
        M.post_message(
            dispatch_id="worker-esc",
            msg_type="user_need",
            payload={"text": "needs\x1b[31m review\rnow\nplease"},
            messages_dir=msgs,
        )
        return S._mail_summary({"worker-esc"})

    out = _with_mail_dirs(body)
    text = out["needs"][0]["text"]
    hint = out["hint"]
    for bad in ("\x1b", "\r"):
        assert_true("need text strips C0 controls", bad not in text)
        assert_true("hint strips C0 controls", bad not in hint)
    assert_true("escaped bracket text remains inert", "[31m review now please" in hint)


def test_ownership_filter_excludes_other_controllers_workers() -> None:
    # The mailbox is machine-global; a controller must only see needs from ITS own
    # dispatches. A need from a dispatch this controller doesn't own is filtered out.
    def body(msgs, fleet):
        M.post_message(dispatch_id="mine-1", msg_type="user_need", payload={"text": " mine"}, messages_dir=msgs)
        M.post_message(dispatch_id="theirs-9", msg_type="blocked", payload={"text": "not mine"}, messages_dir=msgs)
        return S._mail_summary({"mine-1"})  # only own dispatch

    out = _with_mail_dirs(body)
    assert_eq("only owned need surfaces", out.get("count"), 1)
    assert_eq("and it is the owned one", out["needs"][0]["dispatch_id"], "mine-1")


def test_task_store_inbox_id_agrees_between_worktree_and_main_root() -> None:
    # Live regression (2026-07-02): the nudge WRITER resolves the canonical
    # git common-dir parent, but the reader hashed the raw worktree path —
    # reader and writer watched DIFFERENT inboxes in any linked worktree.
    import subprocess

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        main = base / "mainrepo"
        main.mkdir()
        env = os.environ.copy()
        env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})
        for cmd in (
            ["git", "init", "-q"],
            ["git", "commit", "-q", "--allow-empty", "-m", "root"],
            ["git", "worktree", "add", "-q", str(base / "wt"), "-b", "wt-branch"],
        ):
            subprocess.run(cmd, cwd=str(main), env=env, check=True, capture_output=True)
        from_main = M._task_store_dispatch_id(main)
        from_wt = M._task_store_dispatch_id(base / "wt")
        assert_true("main root derives an id", bool(from_main))
        assert_true(
            f"worktree and main root agree on the inbox id ({from_wt} vs {from_main})",
            from_wt == from_main,
        )


def test_scoped_status_mail_includes_project_task_store_nudge() -> None:
    def body(msgs, fleet):
        project = msgs.parent / "project"
        store = T.TaskStore(project)
        store.save_items_atomic([_task_item("t-001", "A"), _task_item("t-002", "B")])
        T._post_next_nudge(store.next_frontier(), project)
        return S._mail_summary(set(), project_root=project)

    out = _with_mail_dirs(body)
    assert_eq("task-store nudge count", out.get("count"), 1)
    need = out["needs"][0]
    assert_true("task-store dispatch surfaced", need["dispatch_id"].startswith("task-store:"))
    assert_eq("nudge kind surfaced", need["type"], T.NEXT_NUDGE_KIND)
    assert_true("hint shows nudge text", "2 parallel-ready (t-001, t-002) -> fan out?" in out["hint"])


def test_stale_task_store_parallel_nudge_stays_silent() -> None:
    def body(msgs, fleet):
        project = msgs.parent / "project"
        store = T.TaskStore(project)
        store.save_items_atomic([_task_item("t-001", "A"), _task_item("t-002", "B")])
        T._post_next_nudge(store.next_frontier(), project)
        store.save_items_atomic([_task_item("t-001", "A")])
        return S._mail_summary(set(), project_root=project)

    out = _with_mail_dirs(body)
    assert_eq("stale task-store nudge suppressed", out, {})


def test_reviewed_done_suggest_nudge_stays_silent() -> None:
    def body(msgs, fleet):
        project = msgs.parent / "project"
        store = T.TaskStore(project)
        store.save_items_atomic([{**_task_item("t-001", "Done work"), "done": True}])
        T.post_done_suggest_nudge(["t-001"], project, "worker-1")
        current = S._mail_summary(set(), project_root=project)
        store.save_items_atomic([{**_task_item("t-001", "Done work"), "done": True, "done_reviewed": True}])
        stale = S._mail_summary(set(), project_root=project)
        return current, stale

    current, stale = _with_mail_dirs(body)
    assert_eq("current done-suggest surfaced", current.get("needs", [{}])[0].get("type"), T.DONE_SUGGEST_NUDGE_KIND)
    assert_eq("reviewed done-suggest suppressed", stale, {})


def test_mail_check_is_fail_open() -> None:
    # If the mail layer raises, a status call must NOT break -> summary is {}.
    saved = M.controller_mail_summary
    M.controller_mail_summary = lambda **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
    try:
        assert_eq("raise -> {}", S._mail_summary({"x"}), {})
    finally:
        M.controller_mail_summary = saved  # type: ignore[assignment]


def test_render_text_includes_hint_when_present() -> None:
    lines = S.render_text(_bare_payload({"hint": "\U0001f4ec mail: 2 open user-need(s) from [a, b] - run: goalflight_messages.py relay"}), 20)
    assert_true("hint line present", any("mail:" in ln for ln in lines))


def test_render_text_silent_when_no_mail() -> None:
    lines = S.render_text(_bare_payload({}), 20)
    assert_true("no mail line when inbox empty", not any("mail:" in ln for ln in lines))


def test_non_regular_inbox_file_does_not_hang() -> None:
    # A FIFO/device named *.jsonl in the inbox dir must be SKIPPED, not opened:
    # read_text()'s open() would block forever on a FIFO and hang status before
    # the fail-open guard could fire (independent-review P2). Bound with SIGALRM so
    # a regression FAILS loudly instead of hanging the whole suite.
    import signal

    if not hasattr(signal, "SIGALRM"):
        return  # platform without alarm (e.g. Windows) — skip

    def body(msgs, fleet):
        M.post_message(dispatch_id="real", msg_type="user_need", payload={"text": "q"}, messages_dir=msgs)
        os.mkfifo(msgs / "trap.jsonl")  # would block open() if not skipped

        def _boom(signum, frame):
            raise TimeoutError("mail check hung on a non-regular inbox file")

        old = signal.signal(signal.SIGALRM, _boom)
        signal.alarm(5)
        try:
            return S._mail_summary()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    out = _with_mail_dirs(body)
    assert_eq("real need still surfaced, FIFO skipped without hang", out.get("count"), 1)


def test_corrupt_unrelated_inbox_does_not_suppress_owned_need() -> None:
    # Convergence P2: an UNRELATED controller's malformed inbox must NOT drop this
    # controller's own need. Scoped reads (own inboxes only) + per-inbox tolerance
    # in build_aggregate both guard this; previously the global read raised and the
    # fail-open wrapper returned {}.
    def body(msgs, fleet):
        M.post_message(dispatch_id="mine-1", msg_type="user_need", payload={"text": "approve?"}, messages_dir=msgs)
        (msgs / "theirs-9.jsonl").write_text("{ not valid json\n", encoding="utf-8")  # corrupt, unrelated
        return S._mail_summary({"mine-1"})

    out = _with_mail_dirs(body)
    assert_eq("owned need survives a corrupt unrelated inbox", out.get("count"), 1)
    assert_eq("and it is the owned one", out["needs"][0]["dispatch_id"], "mine-1")


def test_post_message_fails_closed_on_non_regular_inbox() -> None:
    # Convergence P2: the shared writer must fail CLOSED on a FIFO/device inbox
    # (raise, not block) so CLI/MCP/direct callers cannot hang on open(). Alarm-bounded.
    import signal

    if not hasattr(signal, "SIGALRM"):
        return

    def body(msgs, fleet):
        os.mkfifo(msgs / "w.jsonl")  # FIFO inbox: next_seq read / open("a") would block

        def _boom(signum, frame):
            raise TimeoutError("post_message hung on a non-regular inbox")

        old = signal.signal(signal.SIGALRM, _boom)
        signal.alarm(5)
        raised = None
        try:
            M.post_message(dispatch_id="w", msg_type="user_need", payload={"text": "x"}, messages_dir=msgs)
        except M.MessageError:
            raised = "MessageError"
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        return raised

    assert_eq("writer fails closed (MessageError), no hang", _with_mail_dirs(body), "MessageError")


def main() -> None:
    tests = [
        test_no_mail_empty_summary,
        test_open_user_need_surfaces_hint_with_detail,
        test_mail_hint_strips_control_chars,
        test_ownership_filter_excludes_other_controllers_workers,
        test_task_store_inbox_id_agrees_between_worktree_and_main_root,
        test_scoped_status_mail_includes_project_task_store_nudge,
        test_stale_task_store_parallel_nudge_stays_silent,
        test_reviewed_done_suggest_nudge_stays_silent,
        test_mail_check_is_fail_open,
        test_render_text_includes_hint_when_present,
        test_render_text_silent_when_no_mail,
        test_non_regular_inbox_file_does_not_hang,
        test_corrupt_unrelated_inbox_does_not_suppress_owned_need,
        test_post_message_fails_closed_on_non_regular_inbox,
    ]
    for t in tests:
        t()
    print(f"PASS tests/python/test_status_mail_hint.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
