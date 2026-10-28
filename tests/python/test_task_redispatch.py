#!/usr/bin/env python3
"""Hermetic contract tests for redispatching an existing task item."""

from __future__ import annotations

import json
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
    ref_name = dead["wip_ref"].rsplit("/", 1)[-1]
    assert ref_name.rsplit("-", 2)[-2].isdecimal()
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", dead["wip_ref"]],
        cwd=wip,
    ).returncode == 0
    assert [item["id"] for item in store.load_items()] == ["t-001"]


def test_never_launched_attempt_does_not_block_parent_wip_pin(authority, tmp_path, monkeypatch):
    root, _store, records, make_args = authority
    wip = tmp_path / "parent-wip"
    wip.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wip, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=wip, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=wip, check=True)
    (wip / "work.txt").write_text("parent work\n", encoding="utf-8")
    subprocess.run(["git", "add", "work.txt"], cwd=wip, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "parent work"], cwd=wip, check=True)
    parent = {
        "dispatch_id": "launched-parent", "project_root": str(root),
        "task_ids": ["t-001"], "state": "worker_dead", "terminal_state": "worker_dead",
        "reason": "worker_dead_no_terminal_marker:death_cause=unharvested_work_committed",
        "worker_cwd": str(wip),
    }
    never = {
        "dispatch_id": "never-launched-retry", "project_root": str(root),
        "task_ids": ["t-001"], "state": "worker_dead", "terminal_state": "worker_dead",
        "reason": "partial_task_supersession",
    }
    records.append(parent)
    records.append(never)

    class Reader:
        def read_all(self, *_args):
            return [{"lifecycle_state": D.goalflight_journal.ATTEMPT_TERMINAL, "launch_epoch": 0,
                     "worker_instance_json": None,
                     "terminal_outcome_json": None}]

    monkeypatch.setattr(D.goalflight_journal.Journal, "open_reader", lambda _root: Reader())
    launch_args = make_args(force=True)
    D._refuse_launch_blocked_by_completion_authority(launch_args)
    D._commit_task_redispatch(launch_args)

    assert parent["wip_ref"].startswith("refs/goalflight/keep/")
    assert "wip_ref" not in never
    assert never["superseded_by"] == parent["superseded_by"] == "fresh"


@pytest.mark.parametrize(
    ("evidence", "status_evidence", "tail_output", "expected_never_launched"),
    [
        (None, None, "", True),
        ({"worker_pid": 4242}, None, "", False),
        ({"worker_identity": {"pid": 4242, "start_token": "worker"}}, None, "", False),
        ({"worker_cwd": "recorded-worker"}, None, "", False),
        ({"worktree_seat": "s-1"}, None, "", False),
        (None, {"worker_pid": 4242}, "", False),
        (None, None, "worker output\n", False),
    ],
)
def test_ownerless_drain_retry_record_redispatch_requires_no_worker_evidence(
    authority, tmp_path, monkeypatch, evidence, status_evidence, tail_output,
    expected_never_launched
):
    root, _store, records, make_args = authority
    record = json.loads(
        """{
          "agent": "codex",
          "dispatch_id": "example-dispatch-1",
          "epoch": "status-00000000000000000000000000000004",
          "liveness_state": "worker_dead",
          "project_root": "/tmp/example-project",
          "reason": "partial_task_supersession",
          "reconciliation": {
            "checked_output": true,
            "checked_task_ids": ["b-0001"],
            "claim_recovery_count": 0,
            "launch_timeout_s": 300.0,
            "queue_launch_token": "00000000000000000000000000000003",
            "salvage_required": true,
            "source": "drain_claim_recovery"
          },
          "schema": "goalflight.status.v1",
          "shape": "bash",
          "state": "worker_dead",
          "terminal_marker": null,
          "terminal_state": "worker_dead",
          "task_ids": ["b-0001"],
          "updated_at": 1767225600,
          "worker_alive": false,
          "worker_pid": null
        }"""
    )
    record["project_root"] = str(root)
    if evidence:
        record.update(evidence)
    if evidence and evidence.get("worker_cwd"):
        record["worker_cwd"] = str(tmp_path / evidence["worker_cwd"])

    dispatch_dir = D._dispatch_base_dir()
    status_path = dispatch_dir / f"{record['dispatch_id']}.status.json"
    tail_path = dispatch_dir / f"{record['dispatch_id']}.tail"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    status_record = json.loads(json.dumps(record))
    if status_evidence:
        status_record.update(status_evidence)
    status_path.write_text(json.dumps(status_record), encoding="utf-8")
    tail_path.write_text(tail_output, encoding="utf-8")
    if evidence and evidence.get("worker_cwd"):
        (tmp_path / evidence["worker_cwd"]).mkdir()

    class Reader:
        def read_all(self, *_args):
            return [{
                "lifecycle_state": D.goalflight_journal.ATTEMPT_TERMINAL,
                "launch_epoch": 0,
                "worker_instance_json": None,
                "terminal_outcome_json": None,
            }]

    monkeypatch.setattr(D.goalflight_journal.Journal, "open_reader", lambda _root: Reader())
    records.append(record)
    assert D._record_proves_never_launched(record, []) is expected_never_launched

    args = make_args(force=True)
    args._redispatch_pending = {
        "records": [record],
        "wip_refs": {},
        "completed": [],
        "replacement_id": "fresh",
        "actor": "controller",
        "reason": "forced redispatch",
        "committed": False,
    }
    if expected_never_launched:
        D._commit_task_redispatch(args)
        assert record["superseded_by"] == "fresh"
        assert "wip_ref" not in record
    else:
        with pytest.raises(D.DispatchUsageError):
            D._commit_task_redispatch(args)


def test_never_launched_proof_rejects_nonzero_worker_output(authority, tmp_path, monkeypatch):
    root, _store, _records, _make_args = authority

    class Reader:
        def read_all(self, *_args):
            return [{"lifecycle_state": D.goalflight_journal.ATTEMPT_TERMINAL, "launch_epoch": 0,
                     "worker_instance_json": None,
                     "terminal_outcome_json": None}]

    monkeypatch.setattr(D.goalflight_journal.Journal, "open_reader", lambda _root: Reader())
    record = {"dispatch_id": "never-output", "project_root": str(root)}
    tail = tmp_path / "worker.tail"
    record["stdout_path"] = str(tail)
    tail.touch()
    assert D._record_proves_never_launched(record, [])
    tail.write_text("worker ran\n", encoding="utf-8")
    assert not D._record_proves_never_launched(record, [])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_root", 7),
        ("stdout_path", "/bad\0tail"),
        ("stdout_path", "~unknown_user/tail"),
    ],
)
def test_never_launched_proof_fails_closed_on_malformed_paths(
    authority, monkeypatch, field, value
):
    root, _store, _records, _make_args = authority

    class Reader:
        def read_all(self, *_args):
            return [{
                "lifecycle_state": D.goalflight_journal.ATTEMPT_TERMINAL,
                "launch_epoch": 0,
                "worker_instance_json": None,
                "terminal_outcome_json": None,
            }]

    monkeypatch.setattr(D.goalflight_journal.Journal, "open_reader", lambda _root: Reader())
    record = {"dispatch_id": "malformed-never-launched", "project_root": str(root)}
    record[field] = value
    assert D._record_proves_never_launched(record, []) is False


@pytest.mark.parametrize(
    "evidence",
    [
        {"remote_launch_receipt": {"remote_pid": 4242, "worktree_path": "/remote/wt"}},
        {"launch_unconfirmed": True},
        {"launch_issued_at": "2026-10-17T00:00:00Z"},
        {"expected_worker_identity": {"pid": 4242}},
    ],
)
def test_remote_launch_evidence_is_not_never_launched(authority, monkeypatch, evidence):
    root, _store, _records, _make_args = authority

    class Reader:
        def read_all(self, *_args):
            return [{"lifecycle_state": D.goalflight_journal.ATTEMPT_TERMINAL, "launch_epoch": 0,
                     "worker_instance_json": None,
                     "terminal_outcome_json": None}]

    monkeypatch.setattr(D.goalflight_journal.Journal, "open_reader", lambda _root: Reader())
    record = {"dispatch_id": "remote-launch", "project_root": str(root), **evidence}
    assert not D._record_proves_never_launched(record, [])


@pytest.mark.parametrize("field", ["remote_pid_lstart", "remote_pid_identity"])
def test_remote_pid_identity_fields_are_not_never_launched(authority, monkeypatch, field):
    root, _store, _records, _make_args = authority

    class Reader:
        def read_all(self, *_args):
            return [{"lifecycle_state": D.goalflight_journal.ATTEMPT_TERMINAL, "launch_epoch": 0,
                     "worker_instance_json": None,
                     "terminal_outcome_json": None}]

    monkeypatch.setattr(D.goalflight_journal.Journal, "open_reader", lambda _root: Reader())
    record = {"dispatch_id": "remote-pid-evidence", "project_root": str(root), field: "present"}
    assert not D._record_proves_never_launched(record, [])


def test_unknown_record_field_is_not_never_launched(authority, monkeypatch):
    root, _store, _records, _make_args = authority

    class Reader:
        def read_all(self, *_args):
            return [{"lifecycle_state": D.goalflight_journal.ATTEMPT_TERMINAL,
                     "launch_epoch": 0, "worker_instance_json": None,
                     "terminal_outcome_json": None}]

    monkeypatch.setattr(D.goalflight_journal.Journal, "open_reader", lambda _root: Reader())
    record = {
        "dispatch_id": "future-evidence",
        "project_root": str(root),
        "some_future_field": "x",
    }
    assert not D._record_proves_never_launched(record, [])


@pytest.mark.parametrize(
    "outcome",
    [
        {"terminal_state": "worker_dead", "future_outcome": "x"},
        {"reconciliation": {"source": "drain_claim_recovery", "future_detail": "x"}},
    ],
)
def test_unknown_nested_outcome_field_is_not_never_launched(authority, outcome):
    root, _store, _records, _make_args = authority
    record = {
        "dispatch_id": "future-outcome-evidence",
        "project_root": str(root),
        "outcome": outcome,
    }
    assert not D._record_proves_never_launched(record, [])


@pytest.mark.parametrize(
    "reconciliation",
    [
        {"checked_output": {"worker_pid": 4242}},
        {"checked_task_ids": "b-1"},
        {"claim_recovery_count": False},
        {"launch_timeout_s": float("inf")},
        {"queue_launch_token": 42},
        {"salvage_required": 1},
        {"source": []},
    ],
)
@pytest.mark.parametrize("source", ["ledger", "sidecar"])
def test_top_level_reconciliation_values_must_match_their_shapes(
    authority, tmp_path, monkeypatch, reconciliation, source
):
    root, _store, _records, _make_args = authority

    class Reader:
        def read_all(self, *_args):
            return [{
                "lifecycle_state": D.goalflight_journal.ATTEMPT_TERMINAL,
                "launch_epoch": 0,
                "worker_instance_json": None,
                "terminal_outcome_json": None,
            }]

    monkeypatch.setattr(D.goalflight_journal.Journal, "open_reader", lambda _root: Reader())
    record = {"dispatch_id": "malformed-reconciliation", "project_root": str(root)}
    if source == "ledger":
        record["reconciliation"] = reconciliation
    else:
        status_path = tmp_path / "malformed-reconciliation.status.json"
        record["status_path"] = str(status_path)
        status_path.write_text(
            json.dumps({
                "dispatch_id": record["dispatch_id"],
                "project_root": record["project_root"],
                "state": "worker_dead",
                "reconciliation": reconciliation,
            }),
            encoding="utf-8",
        )
    assert not D._record_proves_never_launched(record, [])


@pytest.mark.parametrize(
    "terminal_outcome",
    [
        {"outcome": {"worker_pid": 4242}},
        {
            "metadata": [
                {"attempt": {"identity": {"worker_pid": 4242}}}
            ]
        },
        {"future": {"details": "unrecognized terminal structure"}},
    ],
)
def test_terminal_outcome_rejects_nested_worker_evidence_and_unknown_shapes(
    authority, monkeypatch, terminal_outcome
):
    root, _store, _records, _make_args = authority

    class Reader:
        def read_all(self, *_args):
            return [{
                "lifecycle_state": D.goalflight_journal.ATTEMPT_TERMINAL,
                "launch_epoch": 0,
                "worker_instance_json": None,
                "terminal_outcome_json": json.dumps(terminal_outcome),
            }]

    monkeypatch.setattr(D.goalflight_journal.Journal, "open_reader", lambda _root: Reader())
    record = {
        "dispatch_id": "nested-terminal-worker-evidence",
        "project_root": str(root),
    }
    assert not D._record_proves_never_launched(record, [])


@pytest.mark.parametrize(
    ("launch_epoch", "include_epoch", "expected"),
    [
        (0, True, True),
        (1, True, False),
        (None, True, False),
        ("0", True, False),
        (-1, True, False),
        (False, True, False),
        (None, False, False),
    ],
)
def test_production_record_requires_zero_journal_launch_epoch(
    authority, tmp_path, monkeypatch, launch_epoch, include_epoch, expected
):
    root, _store, _records, _make_args = authority
    tail = tmp_path / "production-retry.tail"
    status = tmp_path / "production-retry.status.json"
    tail.touch()
    status.touch()
    record = {
        "agent": "codex",
        "attempt_id": "00000000-0000-4000-8000-000000000034",
        "dispatch_id": "example-dispatch-2",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "liveness_state": "worker_dead",
        "outcome": {
            "reason": "partial_task_supersession",
            "reconciliation": {
                "checked_output": True,
                "checked_task_ids": ["b-0002"],
                "claim_recovery_count": 0,
                "launch_timeout_s": 300.0,
                "queue_launch_token": "00000000000000000000000000000005",
                "salvage_required": True,
                "source": "drain_claim_recovery",
            },
            "terminal_state": "worker_dead",
        },
        "producer_descendants": [],
        "producer_group_contract": False,
        "producer_group_contract_enforced": False,
        "project_root": str(root),
        "queue_launch_token": "00000000000000000000000000000005",
        "reason": "partial_task_supersession",
        "salvage_required": True,
        "schema": "goalflight.dispatch.v1",
        "shape": "bash",
        "started_at": "2026-01-01T00:00:00+00:00",
        "state": "worker_dead",
        "status_path": str(status),
        "stdout_path": str(tail),
        "task_ids": ["b-0002"],
        "terminal_event_uuid": "00000000-0000-4000-8000-000000000029",
        "terminal_state": "worker_dead",
        "transition_id": "00000000-0000-4000-8000-000000000025",
        "transport": "dispatch",
        "updated_at": "2026-01-01T00:00:02+00:00",
        "worker_group_leader_identity": None,
        "worker_identity": None,
        "worker_pgid": None,
        "worker_pid": None,
        "worker_still_alive": False,
    }
    expected_keys = {
        "agent", "attempt_id", "dispatch_id", "ended_at", "liveness_state",
        "outcome", "producer_descendants", "producer_group_contract",
        "producer_group_contract_enforced", "project_root", "queue_launch_token",
        "reason", "salvage_required", "schema", "shape", "started_at", "state",
        "status_path", "stdout_path", "task_ids", "terminal_event_uuid",
        "terminal_state", "transition_id", "transport", "updated_at",
        "worker_group_leader_identity", "worker_identity", "worker_pgid",
        "worker_pid", "worker_still_alive",
    }
    assert set(record) == expected_keys

    class Reader:
        def read_all(self, *_args):
            attempt = {
                "lifecycle_state": D.goalflight_journal.ATTEMPT_TERMINAL,
                "worker_instance_json": None,
                "terminal_outcome_json": json.dumps({"terminal_state": "worker_dead"}),
            }
            if include_epoch:
                attempt["launch_epoch"] = launch_epoch
            return [attempt]

    monkeypatch.setattr(D.goalflight_journal.Journal, "open_reader", lambda _root: Reader())
    assert D._record_proves_never_launched(record, []) is expected


@pytest.mark.parametrize(("started", "expected"), [(False, True), (True, False)])
def test_never_launched_proof_uses_real_journal_rows(authority, started, expected):
    root, _store, _records, _make_args = authority
    dispatch_id = f"real-journal-{int(started)}"
    journal = D.goalflight_journal.Journal.create(root)
    prepared = journal.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None, prepared.reason

    if started:
        started_result = journal.start_attempt(
            prepared.value.attempt_id, prepared.value.launch_token
        )
        assert started_result.committed, started_result.reason

    terminal = journal.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="worker_dead",
        observation={"terminal_state": "worker_dead"},
    )
    assert terminal.committed, terminal.reason

    assert D._record_proves_never_launched(
        {"dispatch_id": dispatch_id, "project_root": str(root)}, []
    ) is expected


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

    def pin(record, _carrier_entries):
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
