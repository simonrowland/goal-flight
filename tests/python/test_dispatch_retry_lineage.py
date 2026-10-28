"""Quota retry children keep their parent's dispatch attribution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402


def write_legacy_malformed_record(record: dict) -> None:
    path = dispatch.goalflight_ledger.record_path(record["dispatch_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def test_quota_retry_child_inherits_parent_identity(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    queue = tmp_path / "queue"
    parent_id = "retry-parent"
    parent = {
        "dispatch_id": parent_id,
        "controller_label": "controller-owner",
        "project_root": str(project),
    }
    dispatch.goalflight_ledger.write_record(parent)
    entry = {
        "schema": dispatch.DISPATCH_QUEUE_SCHEMA,
        "dispatch_id": parent_id,
        "project_root": str(project),
        "dispatch_argv": [
            "--agent",
            "codex",
            "--controller-session-id",
            "controller-session",
        ],
        "request": {"project_root": str(project)},
    }

    child = dispatch._requeue_child_entry(
        entry,
        child_id="retry-child",
        requeued_from=parent_id,
        parent_record=parent,
        queue_dir=queue,
        not_before=None,
    )

    assert child is not None
    assert child["controller_label"] == "controller-owner"
    assert child["project_root"] == str(project)
    assert child["requeued_from"] == parent_id
    assert child["request"]["controller_label"] == "controller-owner"
    assert child["request"]["project_root"] == str(project)
    assert child["request"]["requeued_from"] == parent_id
    assert dispatch._option_value_before_worker_remainder(
        child["dispatch_argv"], "--controller-label"
    ) == "controller-owner"

    recorded = {}
    write_record = dispatch.goalflight_ledger.cmd_record

    def capture_record(args):
        recorded.update(vars(args))
        return write_record(args)

    monkeypatch.setattr(dispatch.goalflight_ledger, "cmd_record", capture_record)
    monkeypatch.setattr(
        dispatch,
        "_canonical_replay_argv",
        lambda *_args, **_kwargs: child["dispatch_argv"],
    )
    monkeypatch.setattr(dispatch, "_os_sandbox_posture", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        dispatch, "_export_dashboard_status_for_project", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        dispatch, "_upsert_project_registry_for_dispatch", lambda *_args, **_kwargs: None
    )
    dispatch._record_ledger(
        argparse.Namespace(
            dispatch_id="retry-child",
            task_ids=[],
            agent="codex",
            shape="bash",
            account=None,
            parent_dispatch_id=None,
            controller_label=None,
            controller_session_id=None,
            _prelaunch_controller_label=None,
            cwd=str(project),
            _original_argv=child["dispatch_argv"],
            launch_detached=False,
        ),
        project_root=project,
        prompt_path=None,
        status_json=tmp_path / "retry-child.status.json",
        tail=tmp_path / "retry-child.tail",
        lease_id=None,
        worker_pid=None,
        state="starting",
        request_envelope=child,
    )

    assert recorded["parent_dispatch_id"] is None
    assert recorded["project_root"] == str(project)
    ledger_record = dispatch.goalflight_ledger.read_record("retry-child")
    assert ledger_record["controller_label"] == "controller-owner"
    assert ledger_record["project_root"] == str(project)
    authority = dispatch.goalflight_journal.Journal.open_reader(project)
    attempt = authority.read_all(
        "SELECT lifecycle_state FROM dispatch_attempts WHERE dispatch_id = ?",
        ("retry-child",),
    )[0]
    assert attempt["lifecycle_state"] == dispatch.goalflight_journal.ATTEMPT_STARTING
    envelope = json.loads(recorded["request_envelope_json"])
    assert envelope["controller_label"] == "controller-owner"
    assert envelope["requeued_from"] == parent_id

    existing_parent_id = "existing-parent"
    dispatch.goalflight_ledger.write_record(
        {
            "dispatch_id": existing_parent_id,
            "controller_label": "existing-owner",
            "project_root": str(project),
        }
    )
    preserved_envelope = dict(
        child,
        dispatch_id="preserved-child",
        requeued_from=existing_parent_id,
    )
    initial_args = argparse.Namespace(**recorded)
    initial_args.dispatch_id = "preserved-child"
    initial_args.state = "queued"
    initial_args.controller_label = None
    initial_args.request_envelope_json = json.dumps(preserved_envelope, sort_keys=True)
    assert write_record(initial_args) == 0

    inherited_args = argparse.Namespace(**vars(initial_args))
    inherited_args.controller_label = None
    inherited_args.request_envelope_json = json.dumps(
        dict(child, dispatch_id="preserved-child"), sort_keys=True
    )
    assert write_record(inherited_args) == 0
    preserved_record = dispatch.goalflight_ledger.read_record("preserved-child")
    assert preserved_record["controller_label"] == "existing-owner"


def test_requeue_refuses_to_launch_with_malformed_inherited_owner(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    parent_id = "malformed-retry-parent"
    raw_owner = {"secret": "requeue-owner-sentinel"}
    raw_label = str(raw_owner)
    parent = {
        "dispatch_id": parent_id,
        "controller_label": raw_owner,
        "project_root": str(project),
    }
    write_legacy_malformed_record(parent)
    entry = {
        "dispatch_id": parent_id,
        "project_root": str(project),
        "dispatch_argv": ["--agent", "codex", "--controller-label", raw_label],
        "request": {"controller_label": raw_label, "project_root": str(project)},
    }

    child = dispatch._requeue_child_entry(
        entry,
        child_id="malformed-retry-child",
        requeued_from=parent_id,
        parent_record=parent,
        queue_dir=tmp_path / "queue",
        not_before=None,
    )

    assert child is None
    assert not dispatch.goalflight_ledger.record_path(
        "malformed-retry-child", create=False
    ).exists()


def test_recording_child_does_not_materialize_malformed_parent_owner(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    parent_id = "malformed-record-parent"
    raw_owner = {"secret": "record-owner-sentinel"}
    raw_label = str(raw_owner)
    write_legacy_malformed_record({
        "dispatch_id": parent_id,
        "controller_label": raw_owner,
        "project_root": str(project),
    })
    child_id = "malformed-record-child"
    envelope = {
        "dispatch_id": child_id,
        "project_root": str(project),
        "requeued_from": parent_id,
    }
    args = argparse.Namespace(
        dispatch_id=child_id,
        prompt_id=None,
        prompt_path=None,
        task_ids=[],
        agent="codex",
        engine="codex",
        shape="bash",
        account="default",
        effective_account=None,
        request_envelope_json=json.dumps(envelope),
        transport="dispatch",
        project_root=str(project),
        controller_pid=None,
        controller_session_id=None,
        controller_label=raw_label,
        claimant_pid=None,
        worker_pid=None,
        acp_session_id=None,
        logical_session_id=child_id,
        engine_session_id=None,
        codex_session_id=None,
        codex_home=None,
        codex_home_owner_dispatch_id=None,
        parent_dispatch_id=None,
        resume_mode=None,
        model=None,
        reasoning_effort=None,
        worker_cwd=None,
        worktree_id=None,
        worktree_seat=None,
        worktree_path=None,
        worktree_base=None,
        worktree_head=None,
        worktree_branch=None,
        worktree_keep_ref=None,
        worktree_quarantine_ref=None,
        worktree_reclaimed_dispatch_id=None,
        worktree_ignored_archive=None,
        lock_holder=None,
        dispatch_argv=["--agent", "codex", "--controller-label", raw_label],
        lease_id=None,
        stdout_path=None,
        stderr_path=None,
        status_path=None,
        os_sandbox_json=None,
        queue_launch_token=None,
        detached=False,
        state="starting",
        json=True,
    )

    assert dispatch.goalflight_ledger.cmd_record(args) == 0
    child = dispatch.goalflight_ledger.read_record(child_id)
    assert child["controller_label"] is None
    assert raw_label not in json.dumps(child)
    attempt = dispatch.goalflight_journal.Journal.open_reader(project).read_all(
        "SELECT owner_controller_label FROM dispatch_attempts WHERE dispatch_id = ?",
        (child_id,),
    )[0]
    assert attempt["owner_controller_label"] is None


def test_resume_fallback_refuses_malformed_inherited_owner(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    parent_id = "malformed-resume-parent"
    raw_owner = {"secret": "resume-owner-sentinel"}
    raw_label = str(raw_owner)
    write_legacy_malformed_record({
        "dispatch_id": parent_id,
        "controller_label": raw_owner,
        "project_root": str(project),
    })
    record = {
        "dispatch_id": "materialized-resume-child",
        "controller_label": raw_label,
        "parent_dispatch_id": parent_id,
        "project_root": str(project),
    }

    with pytest.raises(dispatch.DispatchUsageError) as exc_info:
        dispatch._resume_recorded_controller_label(record)

    assert "malformed controller owner metadata" in str(exc_info.value)
    assert "ancestor depth 1" in str(exc_info.value)
    assert "dict" in str(exc_info.value)
    assert raw_label not in str(exc_info.value)
