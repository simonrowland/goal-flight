from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import goalflight_acp_run as acp_run
import goalflight_dispatch as dispatch
import goalflight_worktree_pool


@pytest.mark.parametrize("state", ["queued", "waiting_capacity", "submitted", "claimed"])
def test_prelaunch_state_is_not_a_worktree_incumbent(
    state: str,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seat = tmp_path / "s-1"
    seat.mkdir()
    monkeypatch.setattr(
        dispatch.goalflight_ledger,
        "read_records",
        lambda: [
            {
                "dispatch_id": "waiting-owner",
                "state": state,
                "worker_cwd": str(seat),
                "project_root": str(tmp_path),
            }
        ],
    )
    args = argparse.Namespace(dispatch_id="new-dispatch", parent_dispatch_id=None, cwd=str(seat))
    assert dispatch._worktree_incumbent_reason(args) == (None, None, None)


def test_acp_waiting_capacity_record_has_no_worker_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[argparse.Namespace] = []
    monkeypatch.setattr(acp_run.goalflight_ledger, "worker_spawn_state", lambda _pid: "none")
    monkeypatch.setattr(acp_run.goalflight_ledger, "cmd_record", lambda ns: captured.append(ns) or 0)
    cfg = argparse.Namespace(
        prompt_id=None,
        prompt=None,
        task_ids=[],
        agent="test-dispatch",
        account=None,
        request_envelope=None,
        session_id="session",
        queue_launch_token=None,
        cwd=str(tmp_path / "seat"),
    )
    acp_run._record_acp_ledger_state(
        cfg,
        dispatch_id="waiting-acp",
        project_root=tmp_path,
        controller_pid=None,
        controller_session_id=None,
        controller_label=None,
        status_path=tmp_path / "status.json",
        payload={},
        effective_account=None,
        lease_id=None,
        worker_pid=None,
        state="waiting_capacity",
        worker_cwd=tmp_path / "seat",
    )
    assert captured and captured[0].worker_cwd is None


def test_starting_record_remains_a_worktree_incumbent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seat = tmp_path / "s-1"
    seat.mkdir()
    monkeypatch.setattr(
        dispatch.goalflight_ledger,
        "read_records",
        lambda: [
            {
                "dispatch_id": "bound-owner",
                "state": "starting",
                "worker_cwd": str(seat),
                "project_root": str(tmp_path),
            }
        ],
    )
    args = argparse.Namespace(dispatch_id="new-dispatch", parent_dispatch_id=None, cwd=str(seat))
    occupied, unknown, state = dispatch._worktree_incumbent_reason(args)
    assert occupied and "bound-owner" in occupied
    assert unknown is None
    assert state == "starting"


def test_requeue_child_drops_parent_worktree_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch, "_dispatch_base_dir", lambda: tmp_path)
    entry = {
        "schema": dispatch.DISPATCH_QUEUE_PINNED_SCHEMA,
        "dispatch_id": "parent-retry",
        "worktree_seat": "s-1",
        "worktree_path": str(tmp_path / "s-1"),
        "worktree_base_sha": "a" * 40,
        "worktree_pin_holder": "parent-retry",
        "dispatch_argv": [
            "--dispatch-id",
            "parent-retry",
            "--cwd",
            str(tmp_path / "s-1"),
            "--worktree-pin-holder",
            "parent-retry",
            "--skip-seat-reset",
        ],
        "request": {},
    }
    child = dispatch._requeue_child_entry(
        entry,
        child_id="child-retry",
        requeued_from="parent-retry",
        queue_dir=tmp_path / "queue",
        not_before=None,
    )
    assert child is not None
    assert child["schema"] == dispatch.DISPATCH_QUEUE_SCHEMA
    assert not any(
        key in child for key in ("worktree_seat", "worktree_path", "worktree_base_sha", "worktree_pin_holder")
    )
    assert "--cwd" not in child["dispatch_argv"]
    assert "--worktree-pin-holder" not in child["dispatch_argv"]
    assert "--skip-seat-reset" not in child["dispatch_argv"]


def test_pinned_carrier_uses_v2_schema_and_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim = tmp_path / "claim.json"
    lease = argparse.Namespace(path=tmp_path / "s-1", seat_name="s-1")
    lease.path.mkdir()
    claim.write_text(
        json.dumps(
            {
                "schema": dispatch.DISPATCH_QUEUE_SCHEMA,
                "queue_launch_token": "token",
                "dispatch_argv": ["--dispatch-id", "pinned"],
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        dispatch_id="pinned",
        from_queue=True,
        queue_claim_path=str(claim),
        queue_launch_token="token",
    )
    monkeypatch.setattr(dispatch, "_reconstruct_launch_argv", lambda recorded, **kwargs: recorded + ["--worktree-pin-holder", "pinned"])
    dispatch._persist_queue_worktree_pin(args, lease, base_commit="b" * 40)
    carrier = json.loads(claim.read_text(encoding="utf-8"))
    assert carrier["schema"] == dispatch.DISPATCH_QUEUE_PINNED_SCHEMA
    assert carrier["worktree_pin_holder"] == "pinned"
    assert "--worktree-pin-holder" in carrier["dispatch_argv"]


def test_standalone_acp_has_no_worktree_fallback() -> None:
    assert not hasattr(acp_run, "create_and_route_dispatch_worktree")


def test_seat_affinity_reads_git_metadata_without_git_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seat = tmp_path / "s-1"
    git_dir = tmp_path / "gitdir"
    seat.mkdir()
    git_dir.mkdir()
    (seat / ".git").write_text("gitdir: ../gitdir\n", encoding="utf-8")
    base = "c" * 40
    (git_dir / "HEAD").write_text(base + "\n", encoding="utf-8")
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_git_proc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("git subprocess")),
    )
    assert goalflight_worktree_pool._seat_base_distance(seat, base) == 0
