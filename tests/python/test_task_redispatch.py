#!/usr/bin/env python3
"""Hermetic contract tests for redispatching an existing task item."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


@pytest.fixture
def authority(tmp_path, monkeypatch):
    from test_dispatch_queue import _isolated_completion_authority

    with _isolated_completion_authority(tmp_path):
        store = D.goalflight_task.TaskStore(tmp_path)
        item = D.goalflight_task._make_item(
            "t-001",
            kind="task",
            title="redispatch fixture",
            actor="test",
        )
        store.mutate_items(lambda items: items.append(item))
        class LedgerRecords(list[dict]):
            def append(self, value: dict) -> None:
                super().append(value)
                D.goalflight_ledger.write_record(dict(value))

        records: list[dict] = LedgerRecords()
        monkeypatch.setattr(D, "_pass_ledger_records", lambda: records)
        monkeypatch.setattr(D, "_find_dispatch_record", lambda _id: None)
        monkeypatch.setattr(store.__class__, "_records_by_task", lambda _self: {})
        monkeypatch.setattr(
            D.goalflight_ledger,
            "utc_now",
            lambda: "2026-01-02T00:00:00+00:00",
        )

        def args(*, force: bool = False):
            return SimpleNamespace(
                dispatch_id="fresh",
                task_ids=["t-001"],
                cwd=str(tmp_path),
                project_root=str(tmp_path),
                force=force,
                parent_dispatch_id=None,
                _original_argv=["--task", "t-001"],
            )

        yield tmp_path, store, records, args


@pytest.mark.parametrize(
    ("label", "state", "terminal_state", "reason"),
    [
        ("launch-failure", "failed", "failed", "launch failed before spawn"),
        (
            "worker-dead-no-marker",
            "worker_dead",
            "worker_dead",
            "worker_dead_no_terminal_marker:death_cause=no_evidence",
        ),
        ("blocked", "blocked", "blocked", "attention marker"),
        ("quota", "quota_exhausted", "quota_exhausted", "insufficient quota"),
        ("withdrawn", "withdrawn", "withdrawn", "operator withdrawal"),
    ],
)
def test_terminal_failure_redispatch_keeps_one_task_item(
    authority, label, state, terminal_state, reason
):
    _tmp, store, records, make_args = authority
    records.append(
        {
            "dispatch_id": f"old-{label}",
            "project_root": str(store.project_root),
            "task_ids": ["t-001"],
            "state": state,
            "terminal_state": terminal_state,
            "reason": reason,
            "withdrawn_by": "operator" if state == "withdrawn" else None,
        }
    )

    D._refuse_launch_blocked_by_completion_authority(make_args())
    store.append_dispatch_breadcrumbs(
        ["t-001"],
        {"dispatch_id": "fresh", "state": "worker-failed", "terminal_state": state},
        "tester",
    )

    items = store.load_items()
    assert [item["id"] for item in items] == ["t-001"], label
    assert [crumb["dispatch_id"] for crumb in items[0]["dispatches"]] == ["fresh"]
    assert [row["id"] for row in D.goalflight_task.list("outstanding", project_root=store.project_root)] == ["t-001"]
    assert store.next_frontier() == []
    assert not store.seq_path.exists()


def test_live_dispatch_refuses_and_names_force(authority, capsys):
    _tmp, _store, records, make_args = authority
    records.append(
        {
            "dispatch_id": "live-001",
            "project_root": str(_tmp),
            "task_ids": ["t-001"],
            "state": "running",
            "terminal_state": "unknown",
        }
    )

    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(make_args())
    output = capsys.readouterr()
    assert "live-001" in output.err
    assert "--force" in output.err


def test_multi_task_dispatch_cannot_be_partially_superseded(authority):
    _tmp, _store, records, make_args = authority
    old = {
        "dispatch_id": "multi-001",
        "project_root": str(_tmp),
        "task_ids": ["t-001", "t-002"],
        "state": "running",
        "terminal_state": "unknown",
    }
    records.append(old)

    with pytest.raises(D.DispatchUsageError, match="t-002"):
        D._refuse_launch_blocked_by_completion_authority(make_args())

    assert "superseded_by" not in old


@pytest.mark.parametrize("state", ["waiting_capacity", "claimed"])
def test_prelaunch_dispatch_is_live_and_refuses_by_default(authority, capsys, state):
    _tmp, _store, records, make_args = authority
    records.append(
        {
            "dispatch_id": f"{state}-001",
            "project_root": str(_tmp),
            "task_ids": ["t-001"],
            "state": state,
            "terminal_state": "unknown",
        }
    )

    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(make_args())
    assert f"{state}-001" in capsys.readouterr().err


def test_force_live_dispatch_records_supersession_without_new_item(authority):
    _tmp, store, records, make_args = authority
    live = {
        "dispatch_id": "live-001",
        "project_root": str(_tmp),
        "task_ids": ["t-001"],
        "state": "running",
        "terminal_state": "unknown",
    }
    records.append(live)

    launch_args = make_args(force=True)
    D._refuse_launch_blocked_by_completion_authority(launch_args)
    D._commit_task_redispatch(launch_args)

    assert live["superseded_by"] == "fresh"
    assert live["supersession_actor"]
    assert "force" in live["supersession_reason"]
    assert [item["id"] for item in store.load_items()] == ["t-001"]


def test_done_task_refuses_without_force(authority):
    _tmp, store, _records, make_args = authority

    def mark_done(items):
        items[0].update(
            done=True,
            done_reviewed=True,
            done_at="2026-01-03T00:00:00+00:00",
            done_reviewed_at="2026-01-03T00:00:01+00:00",
            done_reviewed_by="old-reviewer",
            closed_at="2026-01-03T00:00:00+00:00",
            accepted_review_dispatch_id="old-review",
            accepted_review_findings_ref="old-findings.md",
        )

    store.mutate_items(mark_done)
    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(make_args())


def test_done_task_refuses_even_when_done_before_new_dispatch(authority):
    _tmp, store, _records, make_args = authority

    def mark_done(items):
        items[0].update(
            done=True,
            done_at="2025-01-01T00:00:00+00:00",
            closed_at="2025-01-01T00:00:00+00:00",
        )

    store.mutate_items(mark_done)
    with pytest.raises(D.DispatchUsageError):
        D._refuse_launch_blocked_by_completion_authority(make_args())


def test_force_done_task_reopens_same_item(authority):
    _tmp, store, _records, make_args = authority

    def mark_done(items):
        items[0].update(
            done=True,
            done_reviewed=True,
            done_at="2026-01-03T00:00:00+00:00",
            closed_at="2026-01-03T00:00:00+00:00",
            done_reviewed_at="2026-01-03T00:00:01+00:00",
            done_reviewed_by="old-reviewer",
            accepted_review_dispatch_id="old-review",
            accepted_review_findings_ref="old-findings.md",
        )

    store.mutate_items(mark_done)
    launch_args = make_args(force=True)
    D._refuse_launch_blocked_by_completion_authority(launch_args)
    D._commit_task_redispatch(launch_args)

    items = store.load_items()
    assert [item["id"] for item in items] == ["t-001"]
    assert items[0]["done"] is False
    assert "done_reviewed_by" not in items[0]
    assert "accepted_review_dispatch_id" not in items[0]
    assert "accepted_review_findings_ref" not in items[0]
    assert any(entry.get("action") == "redispatch-reopen" for entry in items[0]["audit"])


def test_force_advanced_dead_dispatch_pins_wip_and_releases_task(authority, tmp_path):
    _tmp, store, records, make_args = authority
    wip = tmp_path / "wip"
    wip.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wip, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=wip, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=wip, check=True)
    (wip / "work.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "work.txt"], cwd=wip, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=wip, check=True)
    (wip / "work.txt").write_text("advanced\n", encoding="utf-8")
    subprocess.run(["git", "add", "work.txt"], cwd=wip, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "wip"], cwd=wip, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=wip, text=True).strip()
    dead = {
        "dispatch_id": "dead-wip",
        "project_root": str(_tmp),
        "task_ids": ["t-001"],
        "state": "worker_dead",
        "terminal_state": "worker_dead",
        "reason": "worker_dead_no_terminal_marker:death_cause=unharvested_work_committed",
        "worker_cwd": str(wip),
        "head_commit": head,
    }
    records.append(dead)

    launch_args = make_args(force=True)
    D._refuse_launch_blocked_by_completion_authority(launch_args)
    D._commit_task_redispatch(launch_args)

    assert dead["superseded_by"] == "fresh"
    assert dead["wip_ref"].startswith("refs/goalflight/keep/")
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", dead["wip_ref"]],
        cwd=wip,
    ).returncode == 0
    assert [item["id"] for item in store.load_items()] == ["t-001"]


def test_force_advanced_pin_and_supersession_share_ledger_lock(authority, monkeypatch):
    """A resume cannot pass the ledger fence while WIP pinning is in flight."""
    _tmp, _store, records, make_args = authority
    dead = {
        "dispatch_id": "dead-wip",
        "project_root": str(_tmp),
        "task_ids": ["t-001"],
        "state": "worker_dead",
        "terminal_state": "worker_dead",
        "reason": "worker_dead_no_terminal_marker:unharvested_work",
        "worker_cwd": str(_tmp / "wip"),
    }
    records.append(dead)

    pin_entered = threading.Event()
    contender_started = threading.Event()
    contender_acquired = threading.Event()
    contender_thread = []
    pin_calls = []
    resume_errors = []

    D.goalflight_ledger.write_record(
        {
            "dispatch_id": "resume-child",
            "project_root": str(_tmp),
            "task_ids": ["t-001"],
            "state": "starting",
            "terminal_state": "unknown",
            "parent_dispatch_id": "dead-wip",
        }
    )
    resume_args = SimpleNamespace(
        dispatch_id="resume-child",
        parent_dispatch_id="dead-wip",
        task_ids=["t-001"],
    )

    def pin(record):
        pin_calls.append(record["dispatch_id"])
        pin_entered.set()

        def contender():
            contender_started.set()
            try:
                with D.goalflight_ledger.StateLock():
                    contender_acquired.set()
                    D._redispatch_spawn_guard(resume_args)
            except Exception as exc:  # noqa: BLE001 - assert the production fence below
                resume_errors.append(exc)

        thread = threading.Thread(target=contender)
        contender_thread.append(thread)
        thread.start()
        assert contender_started.wait(timeout=1)
        assert not contender_acquired.wait(timeout=0.05)
        return "refs/goalflight/keep/redispatch-dead-wip"

    monkeypatch.setattr(D, "_pin_redispatch_wip", pin)
    launch_args = make_args(force=True)
    D._refuse_launch_blocked_by_completion_authority(launch_args)
    assert pin_calls == []
    D._commit_task_redispatch(launch_args)

    assert pin_entered.is_set()
    contender_thread[0].join(timeout=1)
    assert not contender_thread[0].is_alive()
    assert contender_acquired.is_set()
    assert len(resume_errors) == 1
    assert isinstance(resume_errors[0], D.DispatchUsageError)
    assert "resume source was superseded" in str(resume_errors[0])
    assert dead["superseded_by"] == "fresh"
    assert dead["wip_ref"].startswith("refs/goalflight/keep/")


@pytest.mark.parametrize("completion_time", [None, "not-a-time"])
def test_force_done_task_reopens_when_completion_time_is_unreadable(authority, completion_time):
    _tmp, store, _records, make_args = authority

    def mark_done(items):
        items[0].update(
            done=True,
            done_reviewed=True,
            done_at=completion_time,
            done_reviewed_at=completion_time,
            closed_at=completion_time,
        )

    store.mutate_items(mark_done)
    launch_args = make_args(force=True)
    D._refuse_launch_blocked_by_completion_authority(launch_args)
    D._commit_task_redispatch(launch_args)

    items = store.load_items()
    assert [item["id"] for item in items] == ["t-001"]
    assert items[0]["done"] is False
    assert any(entry.get("action") == "redispatch-reopen" for entry in items[0]["audit"])


def test_force_advanced_dirty_dispatch_pins_without_mutating_worktree(authority, tmp_path):
    _tmp, store, records, make_args = authority
    wip = tmp_path / "dirty-wip"
    wip.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wip, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=wip, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=wip, check=True)
    (wip / "work.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "work.txt"], cwd=wip, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=wip, check=True)
    (wip / "work.txt").write_text("dirty\n", encoding="utf-8")
    (wip / "untracked.txt").write_text("new\n", encoding="utf-8")
    dead = {
        "dispatch_id": "dead-dirty-wip",
        "project_root": str(_tmp),
        "task_ids": ["t-001"],
        "state": "worker_dead",
        "terminal_state": "worker_dead",
        "reason": "worker_dead_no_terminal_marker:death_cause=unharvested_work_dirty",
        "worker_cwd": str(wip),
    }
    records.append(dead)

    launch_args = make_args(force=True)
    D._refuse_launch_blocked_by_completion_authority(launch_args)
    D._commit_task_redispatch(launch_args)

    assert dead["wip_ref"].startswith("refs/goalflight/keep/")
    assert subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=wip, text=True
    ).splitlines() == [" M work.txt", "?? untracked.txt"]
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", dead["wip_ref"]], cwd=wip
    ).returncode == 0


def test_superseded_old_carrier_is_not_launchable(authority):
    _tmp, _store, _records, make_args = authority
    old = {
        "dispatch_id": "old-carrier",
        "project_root": str(_tmp),
        "task_ids": ["t-001"],
        "state": "running",
        "terminal_state": "unknown",
        "superseded_by": "fresh",
    }
    entry = {
        "dispatch_id": "old-carrier",
        "project_root": str(_tmp),
        "task_ids": ["t-001"],
        "created_at": "2026-01-02T00:00:00+00:00",
    }
    decision = D._entry_completion_authority(entry, old)
    assert decision["state"] == "superseded"
    assert decision["reason"] == "redispatch_superseded"


def test_launch_parser_accepts_task_force():
    args = D._build_launch_parser().parse_args(["--task", "t-001", "--force"])
    assert args.tasks == ["t-001"]
    assert args.force is True
