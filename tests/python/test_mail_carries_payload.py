#!/usr/bin/env python3
"""t-289: drain yields the worker's work, not a pointer to it."""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_watch as watch  # noqa: E402


POLL_SECS = 0.05
IDLE_DISPATCH = "t288-resume-all-engines"
HEADLINE = (
    "t288-resume-all-engines — resume wired for grok/claude/cursor/kimi/codex"
)


def _set_state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    values = {
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
        "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journal-state"),
        "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
        "GOALFLIGHT_STATE_DIR": str(tmp_path / "dispatch-state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp_path / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
        "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
        "GOALFLIGHT_TEST_MODE": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
        if value != "/dev/null":
            Path(value).mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
    env = os.environ.copy()
    env.update(values)
    return env


def _git_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    return project


def _claim(authority: journal.Journal, label: str = "ctl"):
    claimed = authority.claim_or_renew_lease(
        label, principal={"principal_id": f"{label}-principal"}
    )
    assert claimed.committed and claimed.value is not None
    return claimed.value


def test_outbox_headline_prefers_complete_marker_over_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.Journal.create(project)
    prepared = authority.prepare_attempt("b169-drain-may-lie-2")
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={
            "state": "complete",
            "terminal_state": "complete",
            "last_marker": {
                "kind": "COMPLETE",
                "line": 40,
                "text": HEADLINE.replace("t288-resume-all-engines", "b169-drain-may-lie-2"),
            },
        },
    )
    assert committed.committed
    row = authority.read_all(
        "SELECT payload_json FROM terminal_outbox WHERE attempt_id = ?",
        (prepared.value.attempt_id,),
    )[0]
    payload = json.loads(str(row["payload_json"]))
    assert payload["terminal_state"] == "complete"
    assert payload["complete"] is True
    assert payload["text"] == (
        "b169-drain-may-lie-2 — resume wired for grok/claude/cursor/kimi/codex"
    )
    assert not payload["text"].startswith("dispatch terminal:")


def test_outbox_headline_falls_back_when_no_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.Journal.create(project)
    prepared = authority.prepare_attempt("no-marker-worker")
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "outcome": {}},
    )
    assert committed.committed
    payload = json.loads(
        str(
            authority.read_all(
                "SELECT payload_json FROM terminal_outbox WHERE attempt_id = ?",
                (prepared.value.attempt_id,),
            )[0]["payload_json"]
        )
    )
    assert payload["terminal_state"] == "complete"
    assert payload["text"] == "dispatch terminal: complete"


def test_outbox_headline_helper_is_the_load_bearing_projection() -> None:
    observation = {
        "headline": HEADLINE,
        "outcome": {"error": {"text": "must-not-win"}},
    }
    assert journal.outbox_headline_text("idle_timeout", observation) == HEADLINE
    assert journal.outbox_headline_text(
        "complete",
        {"last_marker": {"kind": "COMPLETE", "text": HEADLINE}, "outcome": {}},
    ) == HEADLINE
    assert journal.outbox_headline_text(
        "complete",
        {"outcome": {"error": {"text": "legacy-error-channel"}}},
    ) == "legacy-error-channel"
    assert journal.outbox_headline_text("complete", {"outcome": {}}) == (
        "dispatch terminal: complete"
    )
    src = inspect.getsource(journal.outbox_headline_text)
    assert "headline" in src
    assert "dispatch terminal:" in src


def test_outbox_does_not_headline_scrape_attention_last_marker() -> None:
    """last_marker BLOCKED without harvest headline is scrape, not an escalation."""

    excerpt = "sandbox denied the write"
    scrape = {"kind": "BLOCKED", "line": 2, "text": excerpt}
    assert journal.outbox_headline_text("worker_dead", {"last_marker": scrape}) == (
        "dispatch terminal: worker_dead"
    )
    assert journal.outbox_headline_text(
        "blocked", {"headline": excerpt, "last_marker": scrape}
    ) == excerpt


def test_harvest_headline_when_status_last_marker_is_none(tmp_path: Path) -> None:
    tail = tmp_path / "worker.tail"
    tail.write_text(
        "working\n"
        f"!COMPLETE: {HEADLINE}\n"
        "still summarizing the fold\n",
        encoding="utf-8",
    )
    harvested = watch.harvest_headline_marker(
        {"last_marker": None, "terminal_marker": None, "markers": []},
        tail,
        expected_dispatch_id=IDLE_DISPATCH,
    )
    assert harvested is not None
    assert harvested["kind"] == "COMPLETE"
    assert harvested["text"] == HEADLINE


def test_harvest_does_not_promote_extract_markers_attention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Relay/mid-tail BLOCKED in extract_markers must not become the outbox line."""

    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    monkeypatch.chdir(project)
    excerpt = "sandbox denied the write"
    cases = (
        ("quoted", f"worker copied prior output\n> BLOCKED: {excerpt}\n", "worker_dead"),
        ("list-item", f"checklist\n- BLOCKED: {excerpt}\n", "worker_dead"),
        ("mid-tail", f"work stalled\nBLOCKED: {excerpt}\nkept going\n", "worker_dead"),
        ("genuine", f"work stalled\nBLOCKED: {excerpt}\n", "blocked"),
    )
    for label, tail_text, expected_state in cases:
        tail = tmp_path / f"{label}.tail"
        tail.write_text(tail_text, encoding="utf-8")
        markers, _size = watch.extract_markers(tail)
        harvested = watch.harvest_headline_marker(
            {
                "last_marker": markers[-1] if markers else None,
                "terminal_marker": None,
                "markers": markers,
            },
            tail,
            expected_dispatch_id=f"harvest-{label}",
        )
        if expected_state == "blocked":
            assert harvested is not None and harvested["kind"] == "BLOCKED", (label, harvested)
            assert harvested["text"] == excerpt, (label, harvested)
            dispatch_id = f"harvest-{label}"
            error = watch._finish_existing_ledger(
                dispatch_id,
                "blocked",
                "marker:BLOCKED",
                worker_still_alive=False,
                terminal_marker=harvested,
                headline=str(harvested.get("text") or ""),
            )
            assert error is None, (label, error)
            payload = json.loads(
                str(
                    journal.Journal(project).read_all(
                        "SELECT payload_json FROM terminal_outbox WHERE recipient = ?",
                        (dispatch_id,),
                    )[0]["payload_json"]
                )
            )
            assert payload["text"] == excerpt, (label, payload)
            continue
        assert harvested is None or harvested.get("kind") != "BLOCKED", (label, harvested)
        dispatch_id = f"harvest-{label}"
        error = watch._finish_existing_ledger(
            dispatch_id,
            "worker_dead",
            "worker_dead_no_terminal_marker:death_cause=no_evidence",
            worker_still_alive=False,
            headline=None,
        )
        assert error is None, (label, error)
        payload = json.loads(
            str(
                journal.Journal(project).read_all(
                    "SELECT payload_json FROM terminal_outbox WHERE recipient = ?",
                    (dispatch_id,),
                )[0]["payload_json"]
            )
        )
        assert payload["text"] == "dispatch terminal: worker_dead", (label, payload)
        assert excerpt not in payload["text"], (label, payload)


def test_finish_ledger_complete_marker_becomes_mail_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    monkeypatch.chdir(project)
    dispatch_id = "b169-drain-may-lie-2"
    marker = {
        "kind": "COMPLETE",
        "line": 12,
        "text": f"{dispatch_id} — drain no longer prints 'no mail' without a unique mailbox",
    }
    error = watch._finish_existing_ledger(
        dispatch_id,
        "complete",
        "marker:COMPLETE",
        worker_still_alive=False,
        headline=marker["text"],
    )
    assert error is None
    authority = journal.Journal(project)
    outbox = authority.read_all(
        "SELECT event_type, payload_json FROM terminal_outbox WHERE recipient = ?",
        (dispatch_id,),
    )
    assert len(outbox) == 1
    payload = json.loads(str(outbox[0]["payload_json"]))
    assert outbox[0]["event_type"] == "result"
    assert payload["terminal_state"] == "complete"
    assert payload["text"] == marker["text"]
    observation = payload.get("observation") or {}
    assert observation.get("headline") == marker["text"]
    assert "error" not in (observation.get("outcome") or {})
    attempt = authority.attempt_for_dispatch(dispatch_id)
    assert attempt is not None
    stored = json.loads(
        str(
            authority.read_all(
                "SELECT terminal_outcome_json FROM dispatch_attempts WHERE attempt_id = ?",
                (attempt.attempt_id,),
            )[0]["terminal_outcome_json"]
        )
    )
    assert stored.get("headline") == marker["text"]
    assert "error" not in (stored.get("outcome") or {})


def test_blocked_marker_still_populates_error_and_mails_headline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    monkeypatch.chdir(project)
    dispatch_id = "blocked-worker"
    marker = {
        "kind": "BLOCKED",
        "line": 8,
        "text": f"{dispatch_id} — sandbox refused the write",
    }
    error = watch._finish_existing_ledger(
        dispatch_id,
        "blocked",
        "marker:BLOCKED",
        worker_still_alive=False,
        terminal_marker=marker,
    )
    assert error is None
    authority = journal.Journal(project)
    outbox = authority.read_all(
        "SELECT event_type, payload_json FROM terminal_outbox WHERE recipient = ?",
        (dispatch_id,),
    )
    assert len(outbox) == 1
    payload = json.loads(str(outbox[0]["payload_json"]))
    assert outbox[0]["event_type"] == "blocked"
    assert payload["text"] == marker["text"]
    outcome = (payload.get("observation") or {}).get("outcome") or {}
    assert isinstance(outcome.get("error"), dict)
    assert outcome["error"]["marker_kind"] == "BLOCKED"
    assert outcome["error"]["text"] == marker["text"]
    assert (payload.get("observation") or {}).get("headline") == marker["text"]


def test_complete_terminal_marker_does_not_enter_the_error_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Passing COMPLETE as terminal_marker must not wrap it into outcome.error."""
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    monkeypatch.chdir(project)
    marker = {
        "kind": "COMPLETE",
        "line": 3,
        "text": "clean-complete — clean",
    }
    assert (
        watch._finish_existing_ledger(
            "clean-complete",
            "complete",
            "marker:COMPLETE",
            worker_still_alive=False,
            terminal_marker=marker,
        )
        is None
    )
    payload = json.loads(
        str(
            journal.Journal(project).read_all(
                "SELECT payload_json FROM terminal_outbox WHERE recipient = ?",
                ("clean-complete",),
            )[0]["payload_json"]
        )
    )
    assert payload["text"] == "clean-complete — clean"
    outcome = (payload.get("observation") or {}).get("outcome") or {}
    assert "error" not in outcome
    assert outcome.get("reason") == "marker:COMPLETE"


def test_idle_timeout_still_mails_the_complete_headline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """t-288: watcher called idle_timeout first; the COMPLETE is still the mail."""
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    monkeypatch.chdir(project)
    tail = tmp_path / "worker.tail"
    status = tmp_path / "watcher.status.json"
    tail.write_text(
        "working on resume wiring\n"
        f"!COMPLETE: {HEADLINE}\n"
        "still summarizing the fold\n",
        encoding="utf-8",
    )
    payloads: list[dict] = []
    clock = [0.0]
    real_write_status = watch.write_status
    real_scan = watch.IncrementalTailScanner.scan

    def capture_status(path: Path, payload: dict) -> None:
        payloads.append(json.loads(json.dumps(payload)))
        real_write_status(path, payload)

    def hidden_scan(self, *, kimi_output: bool = False):
        result = real_scan(self, kimi_output=kimi_output)
        # Live incremental harvest missed the sign-off; idle_timeout must not.
        result.markers = []
        result.mail_markers = []
        result.terminal = None
        return result

    def fake_monotonic() -> float:
        return clock[0]

    def controlled_sleep(seconds: float) -> None:
        clock[0] += float(seconds)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "goalflight_watch.py",
            "--pid",
            "424242",
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--dispatch-id",
            IDLE_DISPATCH,
            "--poll-secs",
            str(POLL_SECS),
            "--max-idle-secs",
            "0.04",
            "--agent",
            "test",
        ],
    )
    monkeypatch.setattr(watch, "write_status", capture_status)
    monkeypatch.setattr(watch.IncrementalTailScanner, "scan", hidden_scan)
    monkeypatch.setattr(watch, "active_monotonic", fake_monotonic)
    monkeypatch.setattr(watch.time, "sleep", controlled_sleep)
    monkeypatch.setattr(watch.atexit, "register", lambda _callback: None)
    monkeypatch.setattr(watch.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        watch,
        "worker_alive",
        lambda pid, _identity: (True, "live", {"pid": pid}),
    )
    monkeypatch.setattr(watch, "process_group_id", lambda pid: pid)
    monkeypatch.setattr(watch, "pgroup_cpu_pct", lambda _pgid: 0.0)
    monkeypatch.setattr(watch, "system_starved", lambda: False)
    monkeypatch.setattr(watch.TraceLiveness, "sample", lambda self, **_kwargs: {})

    rc = watch.main()
    assert rc == 2
    final = payloads[-1]
    assert final["state"] == "idle_timeout"
    assert final["last_marker"]["kind"] == "COMPLETE"
    assert final["last_marker"]["text"] == HEADLINE
    assert final.get("terminal_marker") in (None, {})
    outbox = journal.Journal(project).read_all(
        "SELECT event_type, payload_json FROM terminal_outbox WHERE recipient = ?",
        (IDLE_DISPATCH,),
    )
    assert len(outbox) == 1
    payload = json.loads(str(outbox[0]["payload_json"]))
    assert payload["terminal_state"] == "idle_timeout"
    assert payload["text"] == HEADLINE
    assert "dispatch terminal:" not in payload["text"]
    observation = payload.get("observation") or {}
    assert observation.get("headline") == HEADLINE
    assert "error" not in (observation.get("outcome") or {})


def test_drain_rank_and_chatter_predicates() -> None:
    signal = {"type": "merge-request", "payload": {"text": "land this"}}
    need = {"type": "user_need", "payload": {"text": "choose a target"}}
    blocked = {"type": "blocked", "payload": {"text": "sandbox denied"}}
    chatter = {
        "type": "result",
        "source": {"adapter": "journal-outbox", "transport": "journal"},
        "payload": {"text": "dispatch terminal: complete", "terminal_state": "complete"},
    }
    headline = {
        "type": "result",
        "source": {"adapter": "journal-outbox", "transport": "journal"},
        "payload": {"text": HEADLINE, "terminal_state": "complete"},
    }
    notice = {"type": "controller-notice", "payload": {"text": "hello"}}
    assert messages.drain_rank(signal) == 0
    assert messages.drain_rank(need) == 0
    assert messages.drain_rank(blocked) == 0
    assert messages.drain_rank(chatter) == 2
    assert messages.is_drain_chatter(chatter) is True
    assert messages.is_drain_chatter(headline) is False
    assert messages.drain_rank(headline) == 1
    assert messages.drain_rank(notice) == 1
    ordered = messages.order_drain_items(
        [
            ({"stream_seq": 1}, chatter),
            ({"stream_seq": 2}, signal),
            ({"stream_seq": 3}, notice),
            ({"stream_seq": 4}, need),
        ]
    )
    assert [item[1]["type"] for item in ordered] == [
        "merge-request",
        "user_need",
        "controller-notice",
        "result",
    ]


def _write_dispatch(project: Path, dispatch_id: str, label: str) -> None:
    runs = Path(os.environ["GOALFLIGHT_STATE_DIR"]) / "runs.d"
    runs.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "goalflight.dispatch.v1",
        "dispatch_id": dispatch_id,
        "project_root": str(project.resolve()),
        "state": "running",
        "controller_label": label,
        "started_at": "2026-08-19T00:00:00+00:00",
    }
    (runs / f"{dispatch_id}.json").write_text(json.dumps(record) + "\n", encoding="utf-8")


def _post(
    *,
    project: Path,
    messages_dir: Path,
    label: str,
    dispatch_id: str,
    msg_type: str,
    payload: dict,
    adapter: str = "pytest",
    addressed: bool = False,
) -> dict:
    addressee = (
        messages.controller_addressee(label, project_root=project) if addressed else None
    )
    if not addressed:
        _write_dispatch(project, dispatch_id, label)
    return messages.post_message(
        dispatch_id=dispatch_id,
        msg_type=msg_type,
        payload=payload,
        messages_dir=messages_dir,
        source={"node": "test", "adapter": adapter, "transport": "controller"},
        addressee=addressee,
    )["envelope"]


def test_drain_leads_with_signal_and_keeps_chatter_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    label = "drain-rank-ctl"
    messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", label)
    monkeypatch.setattr(messages, "_current_project_root", lambda: project)
    authority = journal.open_or_create_journal(project)
    _claim(authority, label)
    _post(
        project=project,
        messages_dir=messages_dir,
        label=label,
        dispatch_id="chatter-worker",
        msg_type="result",
        payload={
            "text": "dispatch terminal: complete",
            "terminal_state": "complete",
            "complete": True,
        },
        adapter="journal-outbox",
    )
    _post(
        project=project,
        messages_dir=messages_dir,
        label=label,
        dispatch_id="patch-topic",
        msg_type="merge-request",
        payload={
            "subject": "wire resume engines",
            "text": "From 1111111111111111111111111111111111111111 Mon Sep 17 00:00:00 2001\n",
        },
        adapter="grok",
        addressed=True,
    )
    _post(
        project=project,
        messages_dir=messages_dir,
        label=label,
        dispatch_id="need-topic",
        msg_type="user_need",
        payload={"text": "choose the release target"},
    )
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        peek_rc = messages.main(["relay", "--new"])
    assert peek_rc == 0
    peek_text = stdout.getvalue()
    assert "chatter-worker" in peek_text
    assert "patch-topic" in peek_text
    assert "need-topic" in peek_text

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        drain_rc = messages.main(["relay", "--drain"])
    assert drain_rc == 0
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 4
    kinds = [line.split()[0] for line in lines[:3]]
    assert set(kinds[:2]) == {"[merge-request]", "[user_need]"}
    assert kinds[2] == "[result]"
    patch_line = next(line for line in lines if line.startswith("[merge-request]"))
    assert "wire resume engines from grok" in patch_line
    assert "next: git am" in patch_line
    assert "dispatch terminal: complete" in lines[2]
    assert lines[3].startswith("drained 3 · cursor ")
    assert all("\n" not in line for line in lines)
    lease = authority.active_lease(label)
    assert lease is not None
    assert list(authority.cursor_peek(label, nonce=lease.nonce).items) == []


def test_drain_json_order_matches_ranked_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    label = "drain-json-ctl"
    messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", label)
    monkeypatch.setattr(messages, "_current_project_root", lambda: project)
    authority = journal.open_or_create_journal(project)
    _claim(authority, label)
    _post(
        project=project,
        messages_dir=messages_dir,
        label=label,
        dispatch_id="chatter-worker",
        msg_type="result",
        payload={"text": "dispatch terminal: idle_timeout", "terminal_state": "idle_timeout"},
        adapter="journal-outbox",
    )
    _post(
        project=project,
        messages_dir=messages_dir,
        label=label,
        dispatch_id="finding-topic",
        msg_type="finding",
        payload={"text": "listener re-arm swallowed a pending event"},
        addressed=True,
    )
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = messages.main(["relay", "--drain", "--json"])
    assert rc == 0
    body = json.loads(stdout.getvalue())
    assert body["drained"] == 2
    assert body["status"] == "drained"
    assert [item["dispatch_id"] for item in body["items"]] == [
        "finding-topic",
        "chatter-worker",
    ]


def test_merge_request_can_address_a_controller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    label = "main-ctl"
    messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", label)
    monkeypatch.setattr(messages, "_current_project_root", lambda: project)
    authority = journal.open_or_create_journal(project)
    _claim(authority, label)
    envelope = _post(
        project=project,
        messages_dir=messages_dir,
        label=label,
        dispatch_id="side-branch",
        msg_type="merge-request",
        payload={
            "subject": "land the idle harvest",
            "text": "diff --git a/watch.py b/watch.py\n",
        },
        adapter="grok",
        addressed=True,
    )
    assert envelope["type"] == "merge-request"
    assert envelope["addressee"]["label"] == label
    with pytest.raises(messages.MessageError, match="controller addressing"):
        messages.post_message(
            dispatch_id="nope",
            msg_type="advisory",
            payload={"text": "generic"},
            messages_dir=messages_dir,
            addressee=messages.controller_addressee(label, project_root=project),
        )
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert messages.main(["relay", "--drain"]) == 0
    line = stdout.getvalue().splitlines()[0]
    assert line.startswith("[merge-request] side-branch seq=1 — ")
    assert "land the idle harvest from grok" in line
    assert "next: git apply" in line


def test_patch_drain_line_is_one_truncated_line() -> None:
    envelope = {
        "type": "patch",
        "dispatch_id": "long-topic",
        "seq": 4,
        "source": {"adapter": "codex", "node": "box"},
        "payload": {
            "subject": "x" * 400,
            "text": "diff --git a/foo b/foo\n",
        },
    }
    row = {"stream_id": "long-topic", "stream_seq": 4, "event_type": "patch"}
    line = messages.format_receipt_headline(row, envelope)
    assert line.startswith("[patch] long-topic seq=4 — ")
    assert "next: git apply" in line
    assert "from codex" in line
    assert "\n" not in line
    assert "..." in line
    assert len(line) < 250
