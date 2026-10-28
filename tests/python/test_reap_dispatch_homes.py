"""Fixture-only tests for Goal Flight's unified retention sweep."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import fcntl
import gzip
import json
import os
from pathlib import Path
import signal
import shutil
import shlex
import stat
import subprocess
import sys
import threading
import textwrap
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch  # noqa: E402
import goalflight_dispatch_paths  # noqa: E402
import goalflight_reap_dispatch_homes as reaper  # noqa: E402
import goalflight_seat_dedupe as seat_dedupe  # noqa: E402
import goalflight_status  # noqa: E402


NOW = dt.datetime(2026, 10, 12, 20, 0, tzinfo=dt.timezone.utc)
SESSION_A = "00000000-0000-4000-8000-000000000034"
SESSION_B = "00000000-0000-4000-8000-000000000025"


def _fixture_tree(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "machine-state"
    codex_state = tmp_path / "codex-state"
    homes = codex_state / "dispatch-homes"
    dispatch = state / "dispatch"
    pidfiles = tmp_path / "pidfiles"
    ledger = state / "runs.d"
    legacy_scratch = tmp_path / "claude-temp-root"
    for path in (homes, dispatch, pidfiles, ledger, legacy_scratch):
        path.mkdir(parents=True)
    test_project = state / "test-project"
    test_project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(test_project)], check=True)
    monkeypatch.setattr(reaper, "_legacy_claude_scratch_dir", lambda: legacy_scratch)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(ledger))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(codex_state))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(dispatch))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(pidfiles))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    import goalflight_task

    monkeypatch.setattr(goalflight_task, "read_project_registry", lambda: [])

    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {
            os.path.abspath(path): (True, "") for path in paths
        },
    )
    monkeypatch.setattr(
        reaper.shutil, "disk_usage", lambda _path: SimpleNamespace(free=100_000_000_000),
    )
    return homes, ledger, dispatch, pidfiles


def _home(homes: Path, dispatch_id: str) -> Path:
    path = homes / dispatch_id
    path.mkdir()
    (path / "state.sqlite").write_bytes(b"home state" * 128)
    old = (NOW - dt.timedelta(days=2)).timestamp()
    os.utime(path / "state.sqlite", (old, old))
    os.utime(path, (old, old))
    return path


def _scratch_dir(
    dispatch: Path, dispatch_id: str, *, idle: dt.timedelta = dt.timedelta(hours=7),
    files: dict[str, bytes] | None = None,
) -> Path:
    path = dispatch / f"{reaper.goalflight_compat.safe_dispatch_filename(dispatch_id)}.scratch"
    path.mkdir()
    for name, contents in (files or {}).items():
        child = path / name
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_bytes(contents)
    timestamp = (NOW - idle).timestamp()
    for directory, directories, filenames in os.walk(path, topdown=False, followlinks=False):
        for name in [*directories, *filenames]:
            os.utime(Path(directory) / name, (timestamp, timestamp), follow_symlinks=False)
        os.utime(directory, (timestamp, timestamp))
    return path


def _record(
    ledger: Path,
    dispatch_id: str,
    *,
    state: str = "complete",
    ended_at: dt.datetime | None = NOW - dt.timedelta(days=2),
    **fields: object,
) -> dict:
    record = {
        "schema": "goalflight.dispatch.v1",
        "dispatch_id": dispatch_id,
        "state": state,
        "worker_pid": 999_999_991,
        "project_root": str(ledger.parent / "test-project"),
        **fields,
    }
    if ended_at is not None:
        record["ended_at"] = ended_at.isoformat().replace("+00:00", "Z")
    filename = reaper.goalflight_compat.safe_dispatch_filename(dispatch_id)
    (ledger / f"{filename}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def _mark_managed_seat_reclaimable(
    ledger: Path, project_root: Path, seat: Path, dispatch_id: str,
) -> None:
    import goalflight_worktree_pool

    dead_pid = 2**30
    worker_identity = {"pid": dead_pid, "start_token": "exited-test-worker"}
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(
        project_root, seat.name,
    )
    with lock_path.open("r+", encoding="utf-8") as lock_file:
        metadata = goalflight_worktree_pool._lock_metadata(lock_file)
        metadata.update(pid=dead_pid, start_token=worker_identity["start_token"])
        lock_file.seek(0)
        lock_file.truncate()
        json.dump(metadata, lock_file, sort_keys=True)
        lock_file.write("\n")
        lock_file.flush()
    _record(
        ledger, dispatch_id, state="complete", worker_pid=dead_pid,
        worker_identity=worker_identity, project_root=str(project_root),
    )


def _dead(_record: dict, **_kwargs) -> tuple[bool, str]:
    return False, "dead"


def _run(
    homes, ledger, dispatch, pidfiles, *, probe=_dead, delete=True,
    dispatch_gc_apply=None, scratch_days=3, tail_days=7,
    scratch_expire_resumable_days=None,
):
    return reaper._run_dispatch_retention(
        homes_dir=homes,
        ledger_dir=ledger,
        dispatch_dir=dispatch,
        pidfile_dir=pidfiles,
        now=NOW,
        identity_probe=probe,
        delete=delete,
        dispatch_gc_apply=dispatch_gc_apply,
        scratch_days=scratch_days,
        tail_days=tail_days,
        scratch_expire_resumable_days=scratch_expire_resumable_days,
    )


def test_dispatch_artifact_retention_dry_run_reports_age_based_classes(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    _record(ledger, "old-dispatch", ended_at=NOW - dt.timedelta(days=10))
    scratch = dispatch / "old-dispatch.scratch"
    scratch.mkdir()
    (scratch / "marker").write_bytes(b"scratch")
    tail = dispatch / "old-dispatch.tail"
    tail.write_bytes(b"tail output")

    report = _run(homes, ledger, dispatch, pidfiles, delete=False)
    gc = report["dispatch_gc"]

    assert gc["mode"] == "dry-run"
    assert gc["classes"]["would_remove"] == {
        "count": 1, "bytes": reaper.allocated_tree_bytes(scratch),
    }
    assert gc["classes"]["would_compress"] == {
        "count": 1, "bytes": reaper.allocated_tree_bytes(tail),
    }
    assert gc["pin_strategy"].startswith("explicit dispatch id list")
    assert scratch.is_dir() and tail.is_file()


def test_dispatch_artifact_retention_apply_removes_scratch_and_compresses_tail(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    _record(ledger, "old-dispatch", ended_at=NOW - dt.timedelta(days=10))
    scratch = dispatch / "old-dispatch.scratch"
    scratch.mkdir()
    (scratch / "marker").write_bytes(b"scratch")
    tail = dispatch / "old-dispatch.tail"
    tail.write_bytes(b"tail output")

    report = _run(
        homes, ledger, dispatch, pidfiles,
        delete=True, dispatch_gc_apply=True,
    )
    compressed = Path(str(tail) + ".gz")

    assert not scratch.exists() and not tail.exists()
    assert gzip.decompress(compressed.read_bytes()) == b"tail output"
    assert report["dispatch_gc"]["removed_count"] == 1
    assert report["dispatch_gc"]["compressed_count"] == 1


def test_dispatch_artifact_retention_keeps_live_unknown_recent_pinned_and_resumed(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    _record(ledger, "live-worker", ended_at=NOW - dt.timedelta(days=10))
    _record(ledger, "unknown-state", state="unknown-state", ended_at=NOW - dt.timedelta(days=10))
    _record(ledger, "recent-worker", ended_at=NOW - dt.timedelta(days=1))
    _record(ledger, "pinned-worker", ended_at=NOW - dt.timedelta(days=10))
    _record(ledger, "parent-worker", ended_at=NOW - dt.timedelta(days=10))
    _record(
        ledger, "resume-child", state="running", ended_at=None,
        parent_dispatch_id="parent-worker",
    )
    (ledger / "broken-worker.json").write_text("{", encoding="utf-8")
    (dispatch / "live-worker.scratch").mkdir()
    (dispatch / "missing-worker.scratch").mkdir()
    (dispatch / "unknown-state.scratch").mkdir()
    (dispatch / "broken-worker.scratch").mkdir()
    (dispatch / "recent-worker.scratch").mkdir()
    (dispatch / "pinned-worker.scratch").mkdir()
    (dispatch / "pinned-worker.tail").write_bytes(b"pinned")
    (dispatch / "parent-worker.scratch").mkdir()
    (dispatch / reaper.DISPATCH_GC_PIN_FILE).write_text("pinned-worker\n", encoding="utf-8")

    def probe(record):
        return (True, "live") if record["dispatch_id"] == "live-worker" else _dead(record)

    report = _run(homes, ledger, dispatch, pidfiles, probe=probe, delete=False)
    classes = report["dispatch_gc"]["classes"]
    assert classes["skipped_live"]["count"] == 1
    assert classes["skipped_unknown"]["count"] == 4
    assert classes["skipped_recent"]["count"] == 1
    assert classes["skipped_pinned"]["count"] == 2
    live_reason = report["dispatch_gc"]["skipped_live_by_reason"]["live_worker"]
    assert live_reason["count"] == 1
    assert live_reason["example_dispatch_ids"] == ["live-worker"]


def test_resumable_scratch_default_keeps_live_unknown_and_pinned(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    for dispatch_id in ("resumable-live", "resumable-unknown", "resumable-pinned"):
        _record(
            ledger, dispatch_id, state="blocked",
            ended_at=NOW - dt.timedelta(days=10),
        )
        (dispatch / f"{dispatch_id}.scratch").mkdir()
    (dispatch / reaper.DISPATCH_GC_PIN_FILE).write_text(
        "resumable-pinned\n", encoding="utf-8",
    )

    def probe(record):
        dispatch_id = record["dispatch_id"]
        if dispatch_id == "resumable-live":
            return True, "live"
        if dispatch_id == "resumable-unknown":
            return False, "identity_indeterminate"
        return _dead(record)

    report = _run(homes, ledger, dispatch, pidfiles, probe=probe, delete=False)
    classes = report["dispatch_gc"]["classes"]

    assert classes["skipped_live"]["count"] == 1
    assert classes["skipped_unknown"]["count"] == 1
    assert classes["skipped_pinned"]["count"] == 1
    assert all(
        (dispatch / f"{dispatch_id}.scratch").is_dir()
        for dispatch_id in ("resumable-live", "resumable-unknown", "resumable-pinned")
    )


def test_nonterminal_artifacts_use_worker_identity_before_live_classification(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    for dispatch_id in ("orphaned-worker", "unverified-worker", "live-running-worker"):
        _record(
            ledger, dispatch_id, state="running",
            ended_at=NOW - dt.timedelta(days=10),
        )
        (dispatch / f"{dispatch_id}.scratch").mkdir()

    def probe(record):
        dispatch_id = record["dispatch_id"]
        if dispatch_id == "orphaned-worker":
            return False, "pid_reused_start_token"
        if dispatch_id == "live-running-worker":
            return True, "live"
        return False, "identity_indeterminate"

    report = _run(homes, ledger, dispatch, pidfiles, probe=probe, delete=False)
    classes = report["dispatch_gc"]["classes"]

    assert classes["skipped_live"]["count"] == 1
    assert classes["skipped_unknown"]["count"] == 2


def test_identity_inactive_uses_worker_identity_not_controller_fallback() -> None:
    terminal = {
        "dispatch_id": "terminal-without-worker",
        "state": "failed_worktree",
        "worker_pid": None,
        "controller_pid": 123,
        "controller_identity": {"pid": 123, "start_token": "controller-generation"},
        "claimant_pid": 456,
        "claimant_identity": {"pid": 456, "start_token": "claimant-generation"},
    }
    queued = {**terminal, "dispatch_id": "queued-without-worker", "state": "queued"}

    assert reaper._identity_inactive(terminal, reaper.goalflight_ledger.identity_matches) == (
        False, "liveness_indeterminate",
    )
    assert reaper._identity_inactive(queued, reaper.goalflight_ledger.identity_matches) == (
        False, "liveness_indeterminate",
    )


def test_dispatcher_spawn_proof_controls_terminal_no_pid_age(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    rows = (
        ("failed-no-worker", "failed_worktree", None),
        ("queued-no-worker", "queued", None),
        ("throttled-dead-worker", "transient_throttle", 123),
        ("breadcrumb-dead-worker", "blocked_task_breadcrumb", 456),
    )
    for dispatch_id, state, worker_pid in rows:
        _record(
            ledger, dispatch_id, state=state,
            ended_at=NOW - dt.timedelta(days=10), worker_pid=worker_pid,
            controller_pid=789,
            controller_identity={"pid": 789, "start_token": "controller-generation"},
            **(
                {"prelaunch_failure": True, "spawn_state": "none"}
                if dispatch_id == "failed-no-worker" else {}
            ),
        )
        (dispatch / f"{dispatch_id}.scratch").mkdir()
    _record(
        ledger, "spawn-attempted-no-worker", state="complete",
        ended_at=NOW - dt.timedelta(days=10), worker_pid=None,
        prelaunch_failure=True, worker_spawn_attempted=True,
        spawn_state="attempted",
    )
    (dispatch / "spawn-attempted-no-worker.scratch").mkdir()
    _record(
        ledger, "lineage-parent", state="complete",
        ended_at=NOW - dt.timedelta(days=10), worker_pid=789,
    )
    _record(
        ledger, "lineage-child", state="failed_worktree",
        ended_at=NOW - dt.timedelta(days=10), worker_pid=None,
        parent_dispatch_id="lineage-parent",
        prelaunch_failure=True, spawn_state="none",
    )
    (dispatch / "lineage-parent.scratch").mkdir()

    def probe(record):
        return (False, "no_pid") if not record.get("worker_pid") else (False, "dead")

    original_group_check = reaper._scratch_process_group_alive

    def group_check(record):
        if record["dispatch_id"] == "queued-no-worker":
            pytest.fail("a queued record without a worker PID is orphaned, not live by PGID")
        return original_group_check(record)

    monkeypatch.setattr(reaper, "_scratch_process_group_alive", group_check)
    report = _run(homes, ledger, dispatch, pidfiles, probe=probe, delete=False)
    classes = report["dispatch_gc"]["classes"]

    assert classes["would_remove"]["count"] == 4
    assert classes["skipped_unknown"]["count"] == 2
    assert classes["skipped_live"]["count"] == 0


def test_skipped_live_reason_examples_are_capped_at_twenty(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    for index in range(25):
        dispatch_id = f"live-{index:02d}"
        _record(ledger, dispatch_id, ended_at=NOW - dt.timedelta(days=10))
        (dispatch / f"{dispatch_id}.scratch").mkdir()

    report = _run(
        homes, ledger, dispatch, pidfiles,
        probe=lambda _record: (True, "live"), delete=False,
    )
    reason = report["dispatch_gc"]["skipped_live_by_reason"]["live_worker"]

    assert reason["count"] == 25
    assert len(reason["example_dispatch_ids"]) == 20
    assert reason["example_dispatch_ids"] == [f"live-{index:02d}" for index in range(20)]


def test_process_group_check_ignores_reused_pgid_without_dispatcher_contract(
    monkeypatch,
) -> None:
    record = {
        "dispatch_id": "reused-group",
        "worker_pid": 456,
        "worker_pgid": 456,
        "worker_identity": {"pid": 456, "start_token": "old-generation"},
    }
    monkeypatch.setattr(
        goalflight_dispatch, "_queue_claim_identity_status",
        lambda _pid, _identity: ("dead", "pid_reused_start_token"),
    )
    monkeypatch.setattr(
        goalflight_dispatch, "_producer_set_contract_enumerable",
        lambda _record: False,
    )
    monkeypatch.setattr(
        os, "killpg",
        lambda *_args: pytest.fail("bare reused PGID must not be probed as ownership"),
    )

    assert reaper._scratch_process_group_alive(record) is False


def test_process_group_check_keeps_an_identity_matched_worker_live(monkeypatch) -> None:
    record = {
        "dispatch_id": "live-group",
        "worker_pid": 456,
        "worker_pgid": 456,
        "worker_identity": {"pid": 456, "start_token": "old-generation"},
        "worker_group_leader_identity": {"pid": 456, "start_token": "old-generation"},
        "queue_launch_token": "launch-token",
        "producer_group_contract": True,
        "producer_group_contract_enforced": True,
    }
    monkeypatch.setattr(
        goalflight_dispatch, "_queue_claim_identity_status",
        lambda _pid, _identity: ("live", "live"),
    )
    monkeypatch.setattr(
        goalflight_dispatch, "_producer_set_contract_enumerable",
        lambda _record: True,
    )
    assert reaper._scratch_process_group_alive(record) is True


def test_reused_group_contract_is_unknown_not_live(monkeypatch) -> None:
    record = {
        "dispatch_id": "reused-contracted-group",
        "worker_pid": 456,
        "worker_pgid": 456,
        "worker_identity": {"pid": 456, "start_token": "old-generation"},
        "worker_group_leader_identity": {"pid": 456, "start_token": "old-generation"},
        "queue_launch_token": "launch-token",
        "producer_group_contract": True,
        "producer_group_contract_enforced": True,
    }
    monkeypatch.setattr(
        goalflight_dispatch, "_queue_claim_identity_status",
        lambda _pid, _identity: ("dead", "pid_reused_start_token"),
    )
    monkeypatch.setattr(
        goalflight_dispatch, "_producer_set_contract_enumerable",
        lambda _record: True,
    )
    monkeypatch.setattr(
        goalflight_dispatch, "enumerate_token_producers",
        lambda _entry: goalflight_dispatch.ProducerSetResult(
            goalflight_dispatch.ProducerSetState.LIVE,
            reason="group_contains_processes",
        ),
    )

    assert reaper._scratch_process_group_alive(record) is None


def test_dispatch_artifact_retention_expires_old_resumable_and_salvage_scratch(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    for dispatch_id, state in (
        ("quota-walled", "quota_exhausted"),
        ("blocked-worker", "blocked"),
        ("salvage-worker", "salvage_needed"),
    ):
        _record(ledger, dispatch_id, state=state, ended_at=NOW - dt.timedelta(days=10))
        (dispatch / f"{dispatch_id}.scratch").mkdir()

    resume_checks = []

    def resumable(dispatch_id, **_kwargs):
        resume_checks.append(dispatch_id)
        return {"dispatch_id": dispatch_id}

    monkeypatch.setattr(goalflight_dispatch, "_validate_resume_source", resumable)
    report = _run(homes, ledger, dispatch, pidfiles, delete=False)

    assert report["dispatch_gc"]["classes"]["would_remove_resumable"]["count"] == 3
    assert set(resume_checks) == {"quota-walled", "blocked-worker"}
    assert all((dispatch / f"{dispatch_id}.scratch").is_dir() for dispatch_id in (
        "quota-walled", "blocked-worker", "salvage-worker",
    ))


def test_resumable_scratch_expiration_defaults_to_three_days(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    for dispatch_id, age_days in (("resumable-four-days", 4), ("resumable-two-days", 2)):
        _record(ledger, dispatch_id, ended_at=NOW - dt.timedelta(days=age_days))
        (dispatch / f"{dispatch_id}.scratch").mkdir()
    (dispatch / "resumable-four-days.tail").write_bytes(b"must retain tail")
    monkeypatch.setattr(
        goalflight_dispatch, "_validate_resume_source",
        lambda dispatch_id, **_kwargs: {"dispatch_id": dispatch_id},
    )

    default_report = _run(homes, ledger, dispatch, pidfiles, delete=False)
    three_day_report = _run(
        homes, ledger, dispatch, pidfiles, delete=False,
        scratch_expire_resumable_days=3,
    )
    one_day_report = _run(
        homes, ledger, dispatch, pidfiles, delete=False,
        scratch_expire_resumable_days=1,
    )

    assert (
        reaper.build_parser().parse_args(["--dry-run"]).scratch_expire_resumable_days
        == 3
    )
    assert default_report["dispatch_gc"]["scratch_expire_resumable_days"] == 3
    assert default_report["dispatch_gc"]["classes"]["would_remove_resumable"]["count"] == 1
    assert default_report["dispatch_gc"]["classes"]["skipped_recent"]["count"] == 1
    assert default_report["dispatch_gc"]["classes"]["skipped_resumable"]["count"] == 1
    assert three_day_report["dispatch_gc"]["classes"]["would_remove_resumable"]["count"] == 1
    assert three_day_report["dispatch_gc"]["classes"]["skipped_recent"]["count"] == 1
    assert one_day_report["dispatch_gc"]["classes"]["would_remove_resumable"]["count"] == 2
    assert one_day_report["dispatch_gc"]["classes"]["skipped_resumable"]["count"] == 1
    summary = reaper._format_dispatch_gc_report(default_report["dispatch_gc"])
    assert "scratch_expire_resumable_days=3" in summary
    assert "skipped-recent: count=1 bytes=" in summary
    assert all((dispatch / f"{dispatch_id}.scratch").is_dir() for dispatch_id in (
        "resumable-four-days", "resumable-two-days",
    ))
    assert (dispatch / "resumable-four-days.tail").is_file()


def test_resumable_scratch_expiration_apply_removes_only_scratch(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    _record(ledger, "resumable-old", ended_at=NOW - dt.timedelta(days=5))
    scratch = dispatch / "resumable-old.scratch"
    scratch.mkdir()
    (scratch / "marker").write_bytes(b"scratch")
    tail = dispatch / "resumable-old.tail"
    tail.write_bytes(b"must retain tail")
    monkeypatch.setattr(
        goalflight_dispatch, "_validate_resume_source",
        lambda dispatch_id, **_kwargs: {"dispatch_id": dispatch_id},
    )

    report = _run(
        homes, ledger, dispatch, pidfiles, delete=True, dispatch_gc_apply=True,
    )

    assert not scratch.exists()
    assert tail.read_bytes() == b"must retain tail"
    assert report["dispatch_gc"]["removed_count"] == 1
    assert report["dispatch_gc"]["compressed_count"] == 0


def test_resumability_uses_sweep_snapshot_without_reading_ledger_again(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    record = _record(
        ledger,
        "snapshot-resume-check",
        ended_at=NOW - dt.timedelta(days=5),
        engine="grok",
        engine_session_id="00000000-0000-4000-8000-000000000033",
    )
    (dispatch / "snapshot-resume-check.scratch").mkdir()

    reads = {"find": 0, "scan": 0}
    original_find = goalflight_dispatch._find_dispatch_record
    original_read_records = reaper.goalflight_ledger.read_records

    def count_find(dispatch_id: str):
        reads["find"] += 1
        return original_find(dispatch_id)

    def count_read_records():
        reads["scan"] += 1
        return original_read_records()

    monkeypatch.setattr(goalflight_dispatch, "_find_dispatch_record", count_find)
    monkeypatch.setattr(reaper.goalflight_ledger, "read_records", count_read_records)

    report = _run(homes, ledger, dispatch, pidfiles, delete=False)

    assert report["dispatch_gc"]["classes"]["would_remove_resumable"]["count"] == 1
    assert reads == {"find": 0, "scan": 0}
    assert record["dispatch_id"] == "snapshot-resume-check"


def test_identity_snapshot_timeout_does_not_fall_back_to_unbounded_probe(
    monkeypatch,
) -> None:
    record = {"dispatch_id": "timed-out-identity", "worker_pid": 12345}
    monkeypatch.setattr(
        reaper.goalflight_ledger, "_process_identity_snapshot", lambda *_args, **_kwargs: {},
    )

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("timed snapshot used the unbounded identity probe")

    monkeypatch.setattr(reaper.goalflight_ledger, "identity_matches", unexpected_probe)
    probe = reaper._identity_snapshot_probe(
        [record], unexpected_probe, deadline=time.monotonic() + 1,
    )

    assert probe(record) == (False, "identity_indeterminate")


def test_dispatch_artifact_retention_uses_registered_git_keep_refs_and_lineage(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "test@example.com"], check=True)
    (project / "tracked.txt").write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "first"], check=True)
    pinned_head = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (project / "tracked.txt").write_text("second\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "second"], check=True)
    field_head = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(project), "update-ref", "refs/keep/recorded-field", field_head],
        check=True,
    )

    registered = tmp_path / "registered-project"
    subprocess.run(["git", "clone", "-q", str(project), str(registered)], check=True)
    subprocess.run(
        ["git", "-C", str(registered), "update-ref", "refs/keep/registered-head", pinned_head],
        check=True,
    )
    import goalflight_task

    monkeypatch.setattr(
        goalflight_task, "read_project_registry",
        lambda: [{"project_root": str(registered)}],
    )

    _record(
        ledger, "lineage-parent", ended_at=NOW - dt.timedelta(days=10),
        project_root=str(project),
    )
    _record(
        ledger, "head-pinned-child", ended_at=NOW - dt.timedelta(days=10),
        project_root=str(registered), worktree_head=pinned_head,
        parent_dispatch_id="lineage-parent",
    )
    _record(
        ledger, "recorded-ref-pinned", ended_at=NOW - dt.timedelta(days=10),
        project_root=str(project), worktree_keep_ref="refs/keep/recorded-field",
    )
    (dispatch / "lineage-parent.scratch").mkdir()
    (dispatch / "head-pinned-child.tail").write_bytes(b"child tail")
    (dispatch / "recorded-ref-pinned.scratch").mkdir()

    report = _run(homes, ledger, dispatch, pidfiles, delete=False)

    assert report["dispatch_gc"]["classes"]["skipped_pinned"]["count"] == 3


def test_dispatch_artifact_retention_treats_unreadable_git_as_unknown(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    _record(
        ledger, "git-unknown", ended_at=NOW - dt.timedelta(days=10),
        project_root=str(not_a_repo), worktree_head="a" * 40,
    )
    (dispatch / "git-unknown.scratch").mkdir()

    report = _run(homes, ledger, dispatch, pidfiles, delete=False)

    assert report["dispatch_gc"]["classes"]["skipped_unknown"]["count"] == 1
    assert report["dispatch_gc"]["errors"]


def test_canonical_repo_stat_failure_only_marks_its_records_unknown(
    tmp_path: Path, monkeypatch,
) -> None:
    dispatch = tmp_path / "dispatch"
    dispatch.mkdir()
    unreadable = tmp_path / "unreadable-repo"
    readable = tmp_path / "readable-repo"
    subprocess.run(["git", "init", "--quiet", str(unreadable)], check=True)
    subprocess.run(["git", "init", "--quiet", str(readable)], check=True)
    import goalflight_task

    monkeypatch.setattr(
        goalflight_task,
        "read_project_registry",
        lambda: [{"project_root": str(unreadable)}, {"project_root": str(readable)}],
    )
    real_stat = Path.stat

    def unreadable_stat(path: Path, *args, **kwargs):
        if path.name == unreadable.name:
            raise PermissionError("repo stat denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", unreadable_stat)
    record = {
        "dispatch_id": "keep-ref-in-unreadable-repo",
        "project_root": str(unreadable),
        "worktree_keep_ref": "refs/keep/protected",
    }

    pins, errors, unknown_dispatch_ids = reaper._dispatch_gc_pins(dispatch, [record])

    assert pins == set()
    assert unknown_dispatch_ids == {"keep-ref-in-unreadable-repo"}
    assert any(str(unreadable) in error and "stat failed" in error for error in errors)


def test_dead_project_root_does_not_block_healthy_root_retention(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    healthy_root = tmp_path / "healthy-project"
    subprocess.run(["git", "init", "--quiet", str(healthy_root)], check=True)
    dead_root = tmp_path / "removed-project"
    for dispatch_id, fields in (
        ("dead-root-record", {"project_root": str(dead_root)}),
        ("healthy-root-record", {"project_root": str(healthy_root)}),
        ("missing-root-record", {"project_root": None}),
    ):
        _record(ledger, dispatch_id, ended_at=NOW - dt.timedelta(days=10), **fields)
        (dispatch / f"{dispatch_id}.scratch").mkdir()

    report = _run(homes, ledger, dispatch, pidfiles, delete=False)
    classes = report["dispatch_gc"]["classes"]

    assert classes["would_remove"]["count"] == 1
    assert classes["skipped_unknown"]["count"] == 2
    assert (dispatch / "dead-root-record.scratch").is_dir()
    assert (dispatch / "missing-root-record.scratch").is_dir()
    assert (dispatch / "healthy-root-record.scratch").is_dir()


def test_incomplete_pinned_lineage_does_not_block_unrelated_records(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    _record(
        ledger, "incomplete-pinned-record",
        ended_at=NOW - dt.timedelta(days=10), parent_dispatch_id="missing-parent",
    )
    _record(ledger, "independent-record", ended_at=NOW - dt.timedelta(days=10))
    (dispatch / ".dispatch-gc-pins").write_text(
        "incomplete-pinned-record\n", encoding="utf-8",
    )
    (dispatch / "incomplete-pinned-record.scratch").mkdir()
    (dispatch / "independent-record.scratch").mkdir()

    report = _run(homes, ledger, dispatch, pidfiles, delete=False)
    classes = report["dispatch_gc"]["classes"]

    assert classes["would_remove"]["count"] == 1
    assert classes["skipped_unknown"]["count"] == 1
    assert (dispatch / "incomplete-pinned-record.scratch").is_dir()
    assert (dispatch / "independent-record.scratch").is_dir()
    assert any("pinned dispatch lineage is incomplete" in error
               for error in report["dispatch_gc"]["errors"])


def test_dispatch_artifact_retention_checks_process_groups_for_tail_scratch_and_home(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    for dispatch_id in ("group-tail", "group-scratch", "group-home"):
        _record(ledger, dispatch_id, ended_at=NOW - dt.timedelta(days=10))
    (dispatch / "group-tail.tail").write_bytes(b"tail")
    (dispatch / "group-scratch.scratch").mkdir()
    (homes / "group-home").mkdir()
    monkeypatch.setattr(
        reaper, "_scratch_process_group_alive",
        lambda record: record["dispatch_id"].startswith("group-"),
    )

    report = _run(homes, ledger, dispatch, pidfiles, delete=False)

    assert report["dispatch_gc"]["classes"]["skipped_live"]["count"] == 2
    live_reason = report["dispatch_gc"]["skipped_live_by_reason"]["worker_process_group_alive"]
    assert live_reason["count"] == 2
    assert live_reason["example_dispatch_ids"] == ["group-scratch", "group-tail"]
    assert report["kept_reasons"]["worker_process_group_alive"] == 1
    assert report["dispatch_gc"]["classes"]["would_remove"]["count"] == 0
    assert report["dispatch_gc"]["classes"]["would_compress"]["count"] == 0


def test_tail_append_after_archive_publish_keeps_original_and_removes_archive(
    tmp_path: Path, monkeypatch,
) -> None:
    dispatch = tmp_path / "dispatch"
    dispatch.mkdir()
    tail = dispatch / "race.tail"
    tail.write_bytes(b"initial")
    original_replace = os.replace

    def append_after_publish(*args, **kwargs):
        result = original_replace(*args, **kwargs)
        with tail.open("ab") as source:
            source.write(b"late append")
        return result

    monkeypatch.setattr(os, "replace", append_after_publish)
    with pytest.raises(OSError, match="tail changed before source unlink"):
        reaper._compress_dispatch_tail(tail, dispatch)

    assert tail.read_bytes() == b"initiallate append"
    assert not Path(str(tail) + ".gz").exists()
    assert not Path(str(tail) + ".gz.tmp").exists()


def test_tail_fsync_failure_after_source_unlink_keeps_published_archive(
    tmp_path: Path, monkeypatch,
) -> None:
    dispatch = tmp_path / "dispatch"
    dispatch.mkdir()
    tail = dispatch / "fsync-failure.tail"
    tail.write_bytes(b"retained output")
    real_fsync = os.fsync
    calls = 0

    def fail_final_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("final directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(reaper.os, "fsync", fail_final_directory_fsync)
    with pytest.raises(OSError, match="final directory fsync failed"):
        reaper._compress_dispatch_tail(tail, dispatch)

    archive = Path(str(tail) + ".gz")
    assert not tail.exists()
    assert gzip.decompress(archive.read_bytes()) == b"retained output"
    assert not Path(str(tail) + ".gz.tmp").exists()


def test_tail_compression_skips_when_producer_flock_is_held(tmp_path: Path) -> None:
    dispatch = tmp_path / "dispatch"
    dispatch.mkdir()
    tail = dispatch / "locked.tail"
    tail.write_bytes(b"producer output")
    with tail.open("ab") as producer:
        fcntl.flock(producer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(OSError, match="tail_locked"):
                reaper._compress_dispatch_tail(tail, dispatch)
        finally:
            fcntl.flock(producer.fileno(), fcntl.LOCK_UN)

    assert tail.read_bytes() == b"producer output"
    assert not Path(str(tail) + ".gz").exists()


def test_tail_flock_busy_is_reported_as_live_and_kept(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(
        reaper, "_dispatch_gc_is_resumable",
        lambda _record, _cache, **_kwargs: False,
    )
    _record(ledger, "locked-tail", ended_at=NOW - dt.timedelta(days=10))
    tail = dispatch / "locked-tail.tail"
    tail.write_bytes(b"still being produced")

    with tail.open("ab") as producer:
        fcntl.flock(producer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            report = _run(
                homes, ledger, dispatch, pidfiles,
                delete=True, dispatch_gc_apply=True,
            )
        finally:
            fcntl.flock(producer.fileno(), fcntl.LOCK_UN)

    assert tail.read_bytes() == b"still being produced"
    assert report["dispatch_gc"]["classes"]["skipped_live"]["count"] == 1
    assert report["kept_reasons"]["tail_locked"] == 1


def test_dispatch_artifact_retention_reports_mtime_fallback_only_without_ended_at(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    _record(ledger, "legacy-worker", ended_at=None)
    scratch = dispatch / "legacy-worker.scratch"
    scratch.mkdir()
    old = (NOW - dt.timedelta(days=5)).timestamp()
    os.utime(scratch, (old, old))

    report = _run(homes, ledger, dispatch, pidfiles, delete=False)

    assert report["dispatch_gc"]["classes"]["would_remove"]["count"] == 1
    assert report["dispatch_gc"]["mtime_fallback_count"] == 1


def test_resumable_scratch_with_missing_ended_at_is_kept_as_unknown_age(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    _record(ledger, "legacy-resumable", state="salvage_needed", ended_at=None)
    scratch = dispatch / "legacy-resumable.scratch"
    scratch.mkdir()
    nested_file = scratch / "output.log"
    nested_file.write_text("initial", encoding="utf-8")
    old = (NOW - dt.timedelta(days=10)).timestamp()
    os.utime(scratch, (old, old))
    nested_file.write_text("recent update", encoding="utf-8")
    assert scratch.stat().st_mtime < (NOW - dt.timedelta(days=3)).timestamp()

    report = _run(homes, ledger, dispatch, pidfiles, delete=False)

    assert scratch.is_dir()
    assert report["dispatch_gc"]["classes"]["skipped_unknown"]["count"] == 1
    assert report["dispatch_gc"]["mtime_fallback_count"] == 0


def test_dispatch_artifact_retention_refuses_root_and_nested_symlink_escapes(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    _record(ledger, "root-link", ended_at=NOW - dt.timedelta(days=10))
    _record(ledger, "nested-link", ended_at=NOW - dt.timedelta(days=10))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep", encoding="utf-8")
    (dispatch / "root-link.scratch").symlink_to(outside, target_is_directory=True)
    nested = dispatch / "nested-link.scratch"
    nested.mkdir()
    (nested / "outside-link").symlink_to(outside, target_is_directory=True)

    report = _run(homes, ledger, dispatch, pidfiles, delete=True, dispatch_gc_apply=True)
    assert report["dispatch_gc"]["classes"]["skipped_unsafe"]["count"] == 2
    assert (outside / "keep").read_text(encoding="utf-8") == "keep"
    assert nested.is_dir()


def test_scratch_removal_rejects_dispatch_root_swap_after_safety_check(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    _record(ledger, "ancestor-race", ended_at=NOW - dt.timedelta(days=10))
    scratch = dispatch / "ancestor-race.scratch"
    scratch.mkdir()
    (scratch / "marker").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = outside / scratch.name
    escaped.mkdir()
    (escaped / "marker").write_text("outside", encoding="utf-8")
    original_safe = reaper._dispatch_gc_path_safe
    checks = 0

    def swap_after_final_check(path, root):
        nonlocal checks
        result = original_safe(path, root)
        if path == scratch and result[0]:
            checks += 1
            if checks == 3:
                saved = root.with_name(root.name + ".saved")
                root.rename(saved)
                root.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(reaper, "_dispatch_gc_path_safe", swap_after_final_check)
    report = _run(
        homes, ledger, dispatch, pidfiles,
        delete=True, dispatch_gc_apply=True,
    )

    assert checks >= 3
    assert report["dispatch_gc"]["classes"]["skipped_unsafe"]["count"] == 1
    assert (escaped / "marker").read_text(encoding="utf-8") == "outside"


def test_compressed_dispatch_tail_readers_degrade_explicitly(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    tail = dispatch / "compressed-worker.tail"
    compressed = Path(str(tail) + ".gz")
    with gzip.open(compressed, "wb") as stream:
        stream.write(b"final response\n!COMPLETE: compressed-worker - done\n")
    _record(ledger, "compressed-worker", stdout_path=str(tail), worker_still_alive=False)

    assert goalflight_dispatch.main(["final", "compressed-worker"]) != 0
    assert "gzip-compressed" in capsys.readouterr().err
    assert goalflight_status._tail_path_from_record(
        {"stdout_path": str(tail)},
    ) == compressed
    assert goalflight_status._wait_tail_activity_counts(str(compressed))["compressed"] is True
    watch = subprocess.run(
        [
            "bash", str(ROOT / "scripts/watch-dispatch-tail.sh"),
            "--pid", "1", "--tail", str(compressed),
            "--controller-pid", "1", "--agent", "codex", "--session-id", "test",
        ],
        cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, check=False,
    )
    assert watch.returncode == 0
    assert "gzip-compressed" in watch.stderr


def test_periodic_dispatch_cleanup_requires_explicit_local_flag(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    _homes, _ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        reaper,
        "run_retention_sweep",
        lambda **kwargs: calls.append(kwargs) or {"ran": False, "skipped": "recent_sweep"},
    )

    assert reaper.main(["--delete"]) == 0
    assert calls[-1]["dispatch_gc_apply"] is False
    assert calls[-1]["force"] is False
    config = Path.home() / ".goal-flight" / "capacity.local.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"dispatch_gc_enabled": True}), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", str(config))

    assert reaper.main(["--delete"]) == 0
    assert calls[-1]["dispatch_gc_apply"] is True
    assert calls[-1]["force"] is False
    assert "dispatch artifact retention" not in capsys.readouterr().err

    assert reaper.main(["--apply"]) == 0
    assert calls[-1]["dispatch_gc_apply"] is True
    assert calls[-1]["force"] is True


def test_reaper_cli_defaults_to_dispatch_gc_dry_run(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    _record(ledger, "cli-worker", ended_at=NOW - dt.timedelta(days=5))
    scratch = dispatch / "cli-worker.scratch"
    scratch.mkdir()
    monkeypatch.setattr(reaper.goalflight_ledger, "identity_matches", _dead)
    monkeypatch.setattr(
        reaper, "_dispatch_gc_is_resumable",
        lambda _record, _cache, **_kwargs: False,
    )

    assert reaper.main(["--dry-run"]) == 0
    output = capsys.readouterr()
    assert "dispatch artifact retention (dry-run)" in output.err
    assert "would-remove: count=1" in output.err, output.err
    assert scratch.is_dir()


def test_sweep_deletion_matches_deployed_base_outside_dispatch_artifacts(
    tmp_path: Path, monkeypatch,
) -> None:
    source = subprocess.check_output(
        ["git", "show", "aa130f1b:scripts/goalflight_reap_dispatch_homes.py"],
        cwd=ROOT, text=True,
    )
    baseline: dict[str, object] = {"__name__": "goalflight_reaper_deployed_base"}
    exec(compile(source, "goalflight_reaper_deployed_base.py", "exec"), baseline)
    revised: dict[str, object] = {"__name__": "goalflight_reaper_revised"}
    revised_source = Path(reaper.__file__).read_text(encoding="utf-8")
    exec(compile(revised_source, "goalflight_reaper_revised.py", "exec"), revised)

    dead_worker = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    dead_pid = dead_worker.pid
    dead_worker.wait(timeout=10)
    live_worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.05)
        live_identity = reaper.goalflight_ledger.process_identity(live_worker.pid)
        assert live_identity is not None
    except BaseException:
        live_worker.terminate()
        live_worker.wait(timeout=10)
        raise

    def stable_identity(record: dict) -> tuple[bool, str]:
        if record.get("worker_pid") == live_worker.pid:
            return True, "live"
        return False, "dead"

    def fixture(root: Path):
        homes = root / "codex-state" / "dispatch-homes"
        ledger = root / "state" / "runs.d"
        dispatch = root / "state" / "dispatch"
        pidfiles = root / "pidfiles"
        legacy = root / "legacy"
        task_store = root / "task-store"
        project_roots = (root / "project-a", root / "project-b")
        for path in (homes, ledger, dispatch, pidfiles, legacy, task_store):
            path.mkdir(parents=True)
        for project_root in project_roots:
            project_root.mkdir()
            _cow_git("init", "--quiet", "-b", "main", cwd=project_root)
            _cow_git("config", "user.name", "Goal Flight Test", cwd=project_root)
            _cow_git("config", "user.email", "test@example.invalid", cwd=project_root)
            _cow_git("commit", "--quiet", "--allow-empty", "-m", "base", cwd=project_root)
        import goalflight_worktree_pool

        def add_managed_worktree(project_root: Path, dispatch_id: str) -> Path:
            lease = goalflight_worktree_pool.acquire_worktree_seat(
                project_root, dispatch_id,
            )
            path = lease.path
            lease.release()
            return path

        removable = add_managed_worktree(project_roots[0], "gc-removable")
        late_owned = add_managed_worktree(project_roots[0], "gc-late-owned")
        _mark_managed_seat_reclaimable(
            ledger, project_roots[0], removable, "gc-removable",
        )
        _mark_managed_seat_reclaimable(
            ledger, project_roots[0], late_owned, "gc-late-owned",
        )
        live_owned = add_managed_worktree(project_roots[1], "gc-live-owner")
        fixtures = (
            ("old", dt.timedelta(days=5), dead_pid, None, project_roots[0]),
            ("recent", dt.timedelta(hours=1), dead_pid, None, project_roots[1]),
            ("live", dt.timedelta(days=5), live_worker.pid, live_identity, project_roots[1]),
        )
        # Codex-home cleanup has its own tests; this comparison isolates other artifacts.
        for dispatch_id, age, worker_pid, worker_identity, project_root in fixtures:
            record = {
                "schema": "goalflight.dispatch.v1", "dispatch_id": dispatch_id,
                "state": "complete", "ended_at": (NOW - age).isoformat().replace("+00:00", "Z"),
                "worker_pid": worker_pid,
                "project_root": str(project_root),
            }
            if worker_identity is not None:
                record["worker_identity"] = worker_identity
            filename = reaper.goalflight_compat.safe_dispatch_filename(dispatch_id)
            (ledger / f"{filename}.json").write_text(json.dumps(record), encoding="utf-8")
            (dispatch / f"{dispatch_id}.tail").write_text("tail", encoding="utf-8")
            scratch = dispatch / f"{dispatch_id}.scratch"
            scratch.mkdir()
            scratch_time = (NOW - age).timestamp()
            os.utime(scratch, (scratch_time, scratch_time))
        owner = {
            "schema": "goalflight.dispatch.v1",
            "dispatch_id": "gc-live-owner",
            "state": "running",
            "worker_pid": live_worker.pid,
            "worker_identity": live_identity,
            "worker_cwd": str(live_owned),
            "project_root": str(project_roots[1]),
        }
        owner_name = reaper.goalflight_compat.safe_dispatch_filename("gc-live-owner")
        (ledger / f"{owner_name}.json").write_text(json.dumps(owner), encoding="utf-8")
        (legacy / "old").mkdir()
        (legacy / "old" / "scratch.dat").write_bytes(b"legacy scratch")
        return (
            homes, ledger, dispatch, pidfiles, legacy, root,
            removable, live_owned, late_owned, project_roots,
        )

    def run(module_globals: dict[str, object], root: Path):
        calls: list[str] = []
        gc_ledger_reads: list[Path] = []
        gc_reports: list[dict[str, object]] = []
        module_globals["DISPATCH_ARTIFACT_BUDGET_BYTES"] = 0
        delete = module_globals["_delete_artifact"]

        def recording_delete(path, kind):
            calls.append(Path(path).relative_to(relative_root).as_posix())
            return delete(path, kind)

        module_globals["_delete_artifact"] = recording_delete
        import goalflight_worktree_gc
        import goalflight_worktree_pool
        import goalflight_seat_dedupe
        import goalflight_task

        with monkeypatch.context() as isolated:
            isolated.setenv("GOALFLIGHT_STATE_DIR", str(root / "state"))
            isolated.setenv("GOALFLIGHT_DISPATCH_DIR", str(root / "state" / "dispatch"))
            isolated.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(root / "codex-state"))
            isolated.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(root / "pidfiles"))
            isolated.setenv("GOALFLIGHT_PIDFILE_DIR", str(root / "pidfiles"))
            isolated.setenv("GOALFLIGHT_TASK_STORE_DIR", str(root / "task-store"))
            homes, ledger, dispatch, pidfiles, legacy, relative_root, removable, live_owned, late_owned, project_roots = fixture(root)
            late_owner_added = False
            original_index = goalflight_worktree_gc.ledger_index_for_dir

            def count_ledger_read(path, *args, **kwargs):
                nonlocal late_owner_added
                index = original_index(path, *args, **kwargs)
                gc_ledger_reads.append(Path(path))
                if not late_owner_added:
                    owner = {
                        "schema": "goalflight.dispatch.v1",
                        "dispatch_id": "gc-late-owner",
                        "state": "running",
                        "worker_pid": live_worker.pid,
                        "worker_identity": live_identity,
                        "worker_cwd": str(late_owned),
                        "project_root": str(project_roots[0]),
                    }
                    owner_name = reaper.goalflight_compat.safe_dispatch_filename(
                        "gc-late-owner"
                    )
                    (ledger / f"{owner_name}.json").write_text(
                        json.dumps(owner), encoding="utf-8",
                    )
                    late_owner_added = True
                return index

            def capture_gc_report(*args, **kwargs):
                result = original_gc_main(*args, **kwargs)
                try:
                    gc_reports.append(json.loads(sys.stdout.getvalue()))
                except (AttributeError, json.JSONDecodeError):
                    pass
                return result

            module_globals["_legacy_claude_scratch_dir"] = lambda: legacy
            module_globals["_legacy_scratch_process_gate"] = lambda _path: (True, "")
            module_globals["_legacy_scratch_process_snapshot"] = lambda paths, *, deadline=None: {
                os.path.abspath(path): (True, "") for path in paths
            }
            original_gc_main = goalflight_worktree_gc.main
            isolated.setattr(
                goalflight_worktree_pool, "_process_cwd_snapshot",
                lambda *, timeout_s, deadline=None: [],
            )
            isolated.setattr(goalflight_worktree_gc, "ledger_index_for_dir", count_ledger_read)
            isolated.setattr(goalflight_worktree_gc, "main", capture_gc_report)
            isolated.setattr(
                goalflight_worktree_pool, "reap_read_only_worktrees",
                lambda _root, *, deadline=None: None,
            )
            isolated.setattr(goalflight_task, "read_project_registry", lambda: [])
            isolated.setattr(
                goalflight_task, "resolve_state_base_dir", lambda: root / "task-store"
            )
            isolated.setattr(
                goalflight_seat_dedupe, "_dedupe_pools",
                lambda roots, **_kwargs: {
                    "seats_scanned": 0, "files_cloned": 0,
                    "bytes_shared": 0, "errors": [],
                },
            )
            sweep_body = module_globals["_run_sweep_body"]
            sweep_kwargs = {
                "homes_dir": homes,
                "ledger_dir": ledger,
                "now": NOW,
                "identity_probe": stable_identity,
            }
            if "DISPATCH_GC_SCRATCH_DAYS" in module_globals:
                sweep_kwargs["dispatch_gc_apply"] = True
            report = sweep_body(**sweep_kwargs)
        artifacts = report["artifacts"]
        # Dispatch GC changes are in scope; compare the shared sweep and the
        # unaffected legacy and budget paths against the deployed base.
        accounting = {
            key: artifacts[key] for key in (
                "budget_measurable", "legacy_eligible_count",
                "legacy_eligible_bytes", "legacy_deleted_count",
                "legacy_deleted_bytes", "legacy_would_delete_count",
                "legacy_would_delete_bytes", "legacy_kept_reasons",
            )
        }
        gc_entries = []
        for gc_report in gc_reports:
            for entry in gc_report.get("entries", []):
                path = Path(entry["path"])
                conditions = entry.get("conditions", {})
                unowned = conditions.get("unowned", {})
                gc_entries.append({
                    "path": path.relative_to(relative_root).as_posix(),
                    "decision": entry.get("decision"),
                    "reason": str(entry.get("reason", "")).replace(
                        str(relative_root), "<root>"
                    ),
                    "outcome": entry.get("outcome"),
                    "unowned_verdict": unowned.get("verdict"),
                    "unowned_reason": str(unowned.get("reason", "")).replace(
                        str(relative_root), "<root>"
                    ),
                })
        sweep = {
            "worktree_gc": report["worktree_gc"],
            "worktree_gc_removals": sorted(
                Path(path).relative_to(relative_root).as_posix()
                for path in report["worktree_gc_removals"]
            ),
            "worktree_gc_entries": sorted(gc_entries, key=lambda entry: entry["path"]),
            "cleanup_errors": report["cleanup_errors"],
        }
        expected_worktrees = {
            name: path.relative_to(relative_root).as_posix()
            for name, path in (
                ("removable", removable),
                ("live_owned", live_owned),
                ("late_owned", late_owned),
            )
        }
        outside_dispatch_gc = [path for path in calls if not path.startswith("state/dispatch/")]
        return sorted(outside_dispatch_gc), accounting, sweep, gc_ledger_reads, expected_worktrees

    try:
        deployed = run(baseline, tmp_path / "deployed")
        actual = run(revised, tmp_path / "revised")
    finally:
        live_worker.terminate()
        live_worker.wait(timeout=10)
    assert actual[:3] == deployed[:3]
    assert len(deployed[3]) == 4
    assert len(actual[3]) == 3
    assert all(path == tmp_path / "deployed" / "state" / "runs.d" for path in deployed[3])
    assert all(path == tmp_path / "revised" / "state" / "runs.d" for path in actual[3])
    assert deployed[4] == actual[4]
    for result in (deployed, actual):
        removals = result[2]["worktree_gc_removals"]
        assert removals == [result[4]["removable"]]
        entries = {entry["path"]: entry for entry in result[2]["worktree_gc_entries"]}
        assert entries[result[4]["removable"]]["outcome"] == "removed"
        live_entry = entries[result[4]["live_owned"]]
        assert live_entry["decision"] == "retain"
        assert live_entry["unowned_verdict"] == "no"
        late_entry = entries[result[4]["late_owned"]]
        assert late_entry["decision"] == "remove"
        assert late_entry["outcome"] == "retained"
        assert "changed_before_remove" in late_entry["reason"]
        assert "gc-late-owner" in late_entry["reason"]


def test_identity_snapshot_uses_one_ps_and_preserves_pid_start_token_checks(
    monkeypatch,
) -> None:
    ledger = reaper.goalflight_ledger
    calls = []
    events = []
    monkeypatch.setattr(ledger.goalflight_compat, "pid_liveness", lambda _pid: True)
    monkeypatch.setattr(
        ledger.goalflight_compat,
        "process_start_identity",
        lambda pid: events.append(("token", pid))
        or {"pid": pid, "start_token": f"generation:{pid}"},
    )

    def ps_output(args, **kwargs):
        calls.append((args, kwargs))
        events.append(("ps", tuple(args[-1:])))
        return subprocess.CompletedProcess(
            args, 0,
            "123 1 1 Mon Oct 12 20:00:00 2026 python python worker-123\n"
            "456 1 1 Mon Oct 12 20:00:01 2026 python python worker-456\n",
            "",
        )

    monkeypatch.setattr(ledger.subprocess, "run", ps_output)
    identities = ledger._process_identity_snapshot([456, 123])

    assert len(calls) == 1
    assert calls[0][0][-2:] == ["-p", "123,456"]
    assert identities[123]["start_token"] == "generation:123"
    assert identities[456]["start_token"] == "generation:456"
    assert events == [("ps", ("123,456",)), ("token", 123), ("token", 456)]
    assert ledger.identity_matches(
        {"worker_pid": 123, "worker_identity": {"start_token": "generation:123"}},
        current_identity=identities[123],
    ) == (True, "live")
    assert ledger.identity_matches(
        {"worker_pid": 456, "worker_identity": {"start_token": "old-generation"}},
        current_identity=identities[456],
    ) == (False, "pid_reused_start_token")


def test_deletion_uses_a_fresh_lineage_identity_snapshot_immediately_before_remove(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "fresh-check")
    _record(
        ledger, "fresh-check", worker_pid=999_999_991,
        worker_identity={"pid": 999_999_991, "start_token": "generation:123"},
    )
    snapshots = []

    def process_snapshot(pids, *, deadline=None):
        snapshots.append(tuple(pids))
        if len(snapshots) < 2:
            return {999_999_991: None}
        return {999_999_991: {
            "pid": 999_999_991,
            "identity_probe_error": True,
            "identity_available": False,
        }}

    monkeypatch.setattr(
        reaper.goalflight_ledger, "_process_identity_snapshot", process_snapshot,
    )

    report = reaper._run_dispatch_retention(
        homes_dir=homes,
        ledger_dir=ledger,
        dispatch_dir=dispatch,
        pidfile_dir=pidfiles,
        now=NOW,
        identity_probe=reaper.goalflight_ledger.identity_matches,
    )

    assert snapshots == [(999_999_991,), (999_999_991,)]
    assert home.is_dir()
    assert report["deleted_count"] == 0
    assert report["kept_reasons"]["liveness_indeterminate"] == 1


def test_deletion_sizes_artifact_before_acquiring_resume_locks(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "size-before-lock")
    _record(ledger, "size-before-lock")
    events = []
    real_size = reaper.allocated_tree_bytes
    real_lock_users = reaper._lock_users

    def record_size(path, **kwargs):
        if Path(path) == home:
            events.append("size")
        return real_size(path, **kwargs)

    def record_lock_users(ids, *args, **kwargs):
        if ids == {"size-before-lock"}:
            events.append("lock")
        return real_lock_users(ids, *args, **kwargs)

    monkeypatch.setattr(reaper, "allocated_tree_bytes", record_size)
    monkeypatch.setattr(reaper, "_lock_users", record_lock_users)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert not home.exists()
    assert report["deleted_count"] == 1
    assert events == ["size", "size", "lock"]


def test_seat_liveness_takes_one_snapshot_for_only_relevant_records(
    tmp_path: Path,
) -> None:
    seat = tmp_path / "seat"
    seat.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    records = [
        {"dispatch_id": "inside", "worker_pid": 123, "worker_cwd": str(seat)},
        {"dispatch_id": "outside", "worker_pid": 456, "worker_cwd": str(other)},
        {"dispatch_id": "pathless", "worker_pid": 789},
    ]
    snapshot_calls = []

    def snapshot(rows, deadline):
        snapshot_calls.append(([row["dispatch_id"] for row in rows], deadline))
        return lambda _record: (False, "dead")

    def inactive(record, probe):
        return reaper._identity_inactive(record, probe)

    live, reason = seat_dedupe._seat_has_live_worker(
        seat,
        records,
        lambda _record, _deadline: (False, "dead"),
        inactive,
        time.monotonic() + 5,
        snapshot,
    )

    assert (live, reason) == (False, None)
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0][0] == ["inside", "pathless"]


def test_sweep_reports_wall_and_process_cpu_for_each_phase(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "_project_roots", lambda _records, _errors, **_kwargs: set())

    result = reaper._run_sweep_body(
        homes_dir=homes, ledger_dir=ledger, now=NOW, identity_probe=_dead,
    )

    phases = {
        "dispatch_artifacts_scan", "legacy_scratch", "deletion", "gc",
        "read_only", "dedupe", "traces", "snapshots",
    }
    assert phases.issubset(result["timings"])
    assert phases.issubset(result["timings"]["cpu"])
    assert all(result["timings"][name] >= 0 for name in phases)
    assert all(result["timings"]["cpu"][name] >= 0 for name in phases)
    measurements = result["_measurements"]
    assert measurements["second_ledger_read"]["calls"] == 1
    assert measurements["second_ledger_read"]["wall_seconds"] >= 0
    assert measurements["project_root_discovery"]["calls"] == 1
    assert measurements["project_root_discovery"]["roots"] == 0
    assert set(measurements["external_commands"]) == {"ps", "lsof", "git"}
    assert measurements["children_cpu_seconds"] is None or measurements[
        "children_cpu_seconds"
    ] >= 0


def test_external_command_measurement_counts_ps_lsof_and_git(monkeypatch) -> None:
    calls = []
    ledger = reaper.goalflight_ledger

    def fake_run(args, **_kwargs):
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(ledger.goalflight_compat, "pid_liveness", lambda _pid: True)
    monkeypatch.setattr(
        ledger.goalflight_compat, "process_start_identity",
        lambda pid: {"pid": pid, "start_token": f"generation:{pid}"},
    )
    with reaper._measure_external_commands() as commands:
        for name in ("lsof", "git"):
            subprocess.run([name, "--version"], check=False)
        identities = ledger._process_identity_snapshot([123])

    assert identities[123]["start_token"] == "generation:123"
    assert calls == ["lsof", "git", "ps"]
    assert {name: stats["calls"] for name, stats in commands.items()} == {
        "ps": 1, "lsof": 1, "git": 1,
    }
    assert all(stats["wall_seconds"] >= 0 for stats in commands.values())


def test_production_retention_constants_are_pinned() -> None:
    assert reaper.DISPATCH_ARTIFACT_BUDGET_BYTES == 40_000_000_000
    assert reaper.DISPATCH_ARTIFACT_MINIMUM_AGE == dt.timedelta(hours=24)
    assert reaper.DISPATCH_SCRATCH_MINIMUM_AGE == dt.timedelta(hours=6)


def test_sweep_canonicalizes_root_aliases_before_reaping(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "BatteryTool"
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    git_env = os.environ.copy()
    git_env["GIT_OPTIONAL_LOCKS"] = "0"
    canonical_proc = subprocess.run(
        [
            "git", "-C", str(project), "-c", "core.fsmonitor=false",
            "rev-parse", "--path-format=absolute", "--show-toplevel",
        ],
        capture_output=True,
        text=True,
        env=git_env,
        check=True,
    )
    canonical = Path(canonical_proc.stdout.strip())
    lowercase = canonical.with_name(canonical.name.lower())
    data_alias = Path("/System/Volumes/Data") / canonical.relative_to("/")
    if not all(
        alias.is_dir() and os.path.samefile(canonical, alias)
        for alias in (lowercase, data_alias)
    ):
        pytest.skip("fixture filesystem does not expose both root aliases")

    import goalflight_task
    import goalflight_worktree_gc
    import goalflight_worktree_pool

    monkeypatch.setattr(
        goalflight_task,
        "read_project_registry",
        lambda: [
            {"project_root": str(data_alias)},
            {"project_root": str(canonical)},
        ],
    )
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(
        goalflight_task,
        "read_project_registry",
        lambda: [
            {"project_root": str(data_alias)},
            {"project_root": str(canonical)},
        ],
    )
    monkeypatch.setattr(reaper, "_run_dispatch_retention", lambda **_kwargs: {})
    calls = []
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "reap_read_only_worktrees",
        lambda root, *, deadline=None: calls.append(("read-only", root)),
    )
    monkeypatch.setattr(
        goalflight_worktree_gc,
        "main",
        lambda argv, **_kwargs: calls.append(("gc", argv)) or 0,
    )

    reaper._run_sweep_body(
        homes_dir=homes,
        ledger_dir=ledger,
        now=NOW,
        identity_probe=_dead,
    )

    assert calls == [
        ("read-only", canonical),
        (
            "gc",
            [
                str(canonical), "--ledger-dir", str(ledger), "--apply", "--json",
            ],
        ),
    ]


def test_project_root_git_failure_is_skipped_and_logged(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "not-a-repo"
    project.mkdir()
    import goalflight_task

    monkeypatch.setattr(goalflight_task, "read_project_registry", lambda: [])
    monkeypatch.setattr(
        reaper.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 128, "", "not a git repository"),
    )
    errors = []

    roots = reaper._project_roots([{"project_root": str(project)}], errors)

    assert roots == set()
    assert errors == [
        f"{project}: project root canonicalization failed: not a git repository"
    ]


def test_project_root_git_timeout_is_skipped_and_logged(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "slow-repo"
    project.mkdir()
    import goalflight_task

    monkeypatch.setattr(goalflight_task, "read_project_registry", lambda: [])
    calls = []

    def timeout_git(args, **kwargs):
        calls.append((args, kwargs.get("timeout")))
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(reaper.subprocess, "run", timeout_git)
    errors = []

    roots = reaper._project_roots([{"project_root": str(project)}], errors)

    assert roots == set()
    assert calls == [(
        [
            "git", "-C", str(project), "-c", "core.fsmonitor=false",
            "rev-parse", "--path-format=absolute", "--show-toplevel",
        ],
        reaper._PROJECT_ROOT_GIT_TIMEOUT_SECONDS,
    )]
    assert len(errors) == 1
    assert str(project) in errors[0]
    assert "TimeoutExpired" in errors[0]


def test_missing_project_root_is_skipped_without_git_call(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "removed-project"
    import goalflight_task

    monkeypatch.setattr(goalflight_task, "read_project_registry", lambda: [])
    calls = []

    def unexpected_git(args, **_kwargs):
        calls.append(args)
        raise AssertionError("missing project roots must not invoke Git")

    monkeypatch.setattr(reaper.subprocess, "run", unexpected_git)
    errors = []

    roots = reaper._project_roots([{"project_root": str(project)}], errors)

    assert roots == set()
    assert calls == []
    assert errors == [f"{project}: project root is missing"]


def test_unreadable_parent_project_root_is_passed_to_git_and_logged(
    tmp_path: Path, monkeypatch
) -> None:
    parent = tmp_path / "unreadable-parent"
    parent.mkdir()
    project = parent / "project"
    import goalflight_fs
    import goalflight_task

    monkeypatch.setattr(goalflight_task, "read_project_registry", lambda: [])
    real_lstat = os.lstat

    def unreadable_lstat(path, *args, **kwargs):
        if Path(path) == project:
            raise PermissionError("parent is not searchable")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(goalflight_fs.os, "lstat", unreadable_lstat)
    assert goalflight_fs.path_presence(project) == "unknown"
    calls = []

    def failed_git(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 128, "", "permission denied")

    monkeypatch.setattr(reaper.subprocess, "run", failed_git)
    errors = []

    roots = reaper._project_roots([{"project_root": str(project)}], errors)

    assert roots == set()
    assert calls and calls[0][2] == str(project)
    assert errors == [
        f"{project}: project root canonicalization failed: permission denied"
    ]


def test_regular_file_project_root_is_passed_to_git_and_logged(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project-file"
    project.write_text("not a directory", encoding="utf-8")
    import goalflight_task

    monkeypatch.setattr(goalflight_task, "read_project_registry", lambda: [])
    errors = []

    roots = reaper._project_roots([{"project_root": str(project)}], errors)

    assert roots == set()
    assert len(errors) == 1
    assert errors[0].startswith(
        f"{project}: project root canonicalization failed:"
    )


def test_old_dispatch_reaps_home_tail_pid_rollout_and_grok_session_but_keeps_status(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)

    codex_id = "old-codex"
    codex_home = _home(homes, codex_id)
    status = dispatch / f"{codex_id}.status.json"
    _record(
        ledger,
        codex_id,
        agent="codex",
        engine="codex",
        account="seat-a",
        effective_account="seat-a",
        codex_session_id=SESSION_A,
        codex_home=str(codex_home),
        codex_home_owner_dispatch_id=codex_id,
        status_path=str(status),
    )
    codex_account = Path.home() / ".goal-flight" / "accounts" / "seat-a" / "codex"
    rollout = codex_account / "sessions" / f"rollout-2026-10-01T00-00-00-{SESSION_A}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_bytes(b"rollout" * 128)
    tail = dispatch / f"{codex_id}.tail"
    tail.write_bytes(b"tail" * 128)
    status.write_text("{}", encoding="utf-8")
    pidfile = pidfiles / "123.bashtail.456.jsonl"
    pidfile.write_text(json.dumps({"session_id": codex_id}), encoding="utf-8")

    grok_id = "old-grok"
    _home(homes, grok_id)
    _record(
        ledger,
        grok_id,
        agent="grok",
        engine="grok",
        account="seat-g",
        effective_account="seat-g",
        engine_session_id=SESSION_B,
        worker_cwd="/project/grok",
    )
    grok_session = goalflight_dispatch._seat_session_dir(
        "seat-g", "grok", "/project/grok", SESSION_B
    )
    assert grok_session is not None
    grok_session.mkdir(parents=True)
    (grok_session / "summary.json").write_text("{}", encoding="utf-8")
    (grok_session / "chat_history.jsonl").write_text("{}\n", encoding="utf-8")

    report = _run(homes, ledger, dispatch, pidfiles)

    assert report["deleted_count"] >= 5
    assert not (homes / codex_id).exists()
    assert tail.is_file() and status.exists() and not pidfile.exists()
    assert not rollout.exists()
    assert not (homes / grok_id).exists()
    assert not grok_session.exists()
    assert report["total_allocated_bytes"] == reaper.allocated_tree_bytes(tail)


@pytest.mark.parametrize("worker_pgid", [None, 4242])
def test_dispatch_scratch_is_left_to_age_based_dispatch_gc(
    tmp_path: Path, monkeypatch, worker_pgid: int | None
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    dispatch_id = "old:worker"
    scratch = _scratch_dir(
        dispatch, dispatch_id, files={"checkout-marker": b"scratch" * 256},
    )
    scratch_bytes = reaper.allocated_tree_bytes(scratch)
    fields = {"worker_pgid": worker_pgid} if worker_pgid is not None else {}
    _record(ledger, dispatch_id, **fields)
    killpg_calls = []

    def group_gone(pgid, sig):
        killpg_calls.append((pgid, sig))
        raise ProcessLookupError()

    monkeypatch.setattr(reaper.os, "killpg", group_gone)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_deleted_count"] == 0
    assert report["total_allocated_bytes"] == scratch_bytes
    assert killpg_calls == []


def test_scratch_uses_short_minimum_age_while_home_and_tail_are_kept(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    dispatch_id = "seven-hour-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id)
    tail = dispatch / f"{dispatch_id}.tail"
    tail.write_bytes(b"tail")
    home = _home(homes, dispatch_id)
    _record(ledger, dispatch_id, ended_at=NOW - dt.timedelta(hours=7))

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert home.is_dir() and tail.is_file()
    assert report["scratch_deleted_count"] == 0


def test_scratch_inside_six_hour_minimum_age_is_kept(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    dispatch_id = "five-hour-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id, idle=dt.timedelta(hours=5))
    _record(ledger, dispatch_id, ended_at=NOW - dt.timedelta(days=2))

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_deleted_count"] == 0


def test_dispatch_scratch_is_not_deleted_by_the_bounded_artifact_budget(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    dispatch_id = "locked-seven-hour-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id)
    tail = dispatch / f"{dispatch_id}.tail"
    tail.write_bytes(b"tail")
    home = _home(homes, dispatch_id)
    record_path = ledger / f"{dispatch_id}.json"
    _record(ledger, dispatch_id, ended_at=NOW - dt.timedelta(hours=12))
    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert home.is_dir() and tail.is_file()
    assert report["scratch_deleted_count"] == 0


def test_terminal_scratch_uses_tree_mtime_without_budget_pressure(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "tree-mtime-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id)
    _record(ledger, dispatch_id, ended_at=NOW - dt.timedelta(minutes=5))

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_minimum_idle_seconds"] == 6 * 3600
    assert report["scratch_would_delete_count"] == 1
    assert report["scratch_deleted_count"] == 0


def test_terminal_scratch_keeps_nonterminal_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "running-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id)
    _record(ledger, dispatch_id, state="running")

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_kept_reasons"]["non_terminal"] == 1


def test_terminal_scratch_keeps_live_worker_pid(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "live-pid-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id)
    _record(ledger, dispatch_id)

    report = _run(homes, ledger, dispatch, pidfiles, probe=lambda _row: (True, "live"))

    assert scratch.is_dir()
    assert report["scratch_kept_reasons"]["live_worker"] == 1


@pytest.mark.parametrize("reference_field", ["parent_dispatch_id", "codex_home_owner_dispatch_id"])
def test_terminal_scratch_keeps_nonterminal_references(
    tmp_path: Path, monkeypatch, reference_field: str
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "referenced-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id)
    _record(ledger, dispatch_id)
    _record(
        ledger, "live-child", state="running", **{reference_field: dispatch_id},
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_kept_reasons"]["used_by_non_terminal_record"] == 1


def test_terminal_scratch_keeps_open_file(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "open-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id)
    _record(ledger, dispatch_id)
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {os.path.abspath(path): (False, "lsof_hit") for path in paths},
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_kept_reasons"]["open_file"] == 1


def test_terminal_scratch_lsof_timeout_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    process_snapshot = reaper._legacy_scratch_process_snapshot
    real_run = reaper.subprocess.run
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "timeout-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id)
    _record(ledger, dispatch_id)
    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", process_snapshot)

    def timeout_lsof(args, **kwargs):
        if args[0] != "lsof":
            return real_run(args, **kwargs)
        assert args[0] == "lsof"
        assert kwargs["timeout"] == reaper._LEGACY_SCRATCH_LSOF_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(reaper.subprocess, "run", timeout_lsof)
    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_kept_reasons"]["lsof_timeout"] == 1


def test_terminal_scratch_never_follows_a_symlinked_root(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "linked-scratch"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    scratch = dispatch / f"{dispatch_id}.scratch"
    scratch.symlink_to(outside, target_is_directory=True)
    _record(ledger, dispatch_id)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert report["scratch_kept_reasons"]["not_a_real_directory"] == 1


@pytest.mark.parametrize(
    ("free_bytes", "eligible", "idle_seconds"),
    [
        (reaper.DISPATCH_SCRATCH_FREE_SPACE_FLOOR_BYTES - 1, True, 3600),
        (reaper.DISPATCH_SCRATCH_FREE_SPACE_FLOOR_BYTES + 1, False, 6 * 3600),
    ],
)
def test_terminal_scratch_floor_selects_idle_threshold(
    tmp_path: Path, monkeypatch, free_bytes: int, eligible: bool, idle_seconds: int
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "floor-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id, idle=dt.timedelta(hours=2))
    _record(ledger, dispatch_id)
    monkeypatch.setattr(
        reaper.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free_bytes),
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_low_space"] is eligible
    assert report["scratch_minimum_idle_seconds"] == idle_seconds
    assert report["scratch_would_delete_count"] == int(eligible)
    assert report["scratch_deleted_count"] == 0


def test_terminal_scratch_size_warning_does_not_block_deletion(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "large-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id, files={"marker": b"larger than warning"})
    _record(ledger, dispatch_id, ended_at=NOW - dt.timedelta(minutes=5))
    monkeypatch.setattr(reaper, "DISPATCH_SCRATCH_SIZE_WARNING_BYTES", 1)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_size_warnings"][dispatch_id] > 1
    assert report["scratch_would_delete_count"] == 1
    assert report["scratch_deleted_count"] == 0


def test_terminal_scratch_rechecks_references_after_lsof(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "reference-added-during-lsof"
    scratch = _scratch_dir(dispatch, dispatch_id)
    _record(ledger, dispatch_id)

    def lsof(paths, *, deadline=None):
        _record(ledger, "new-running-child", state="running", parent_dispatch_id=dispatch_id)
        return {os.path.abspath(path): (True, "") for path in paths}

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", lsof)
    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_kept_reasons"]["used_by_non_terminal_record"] == 1


def test_terminal_scratch_rechecks_worker_liveness_after_lsof(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "worker-live-during-lsof"
    scratch = _scratch_dir(dispatch, dispatch_id)
    _record(ledger, dispatch_id)
    probe_calls = 0

    def becomes_live(_record):
        nonlocal probe_calls
        probe_calls += 1
        return (probe_calls >= 3, "live" if probe_calls >= 3 else "dead")

    report = _run(homes, ledger, dispatch, pidfiles, probe=becomes_live)

    assert scratch.is_dir()
    assert report["scratch_kept_reasons"]["live_worker"] == 1


def test_terminal_scratch_rechecks_references_after_identity_probe(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "reference-added-during-identity-probe"
    scratch = _scratch_dir(dispatch, dispatch_id)
    parent = _record(ledger, dispatch_id)
    assert "engine" not in parent
    lsof_ran = False
    original_probe = reaper._identity_snapshot_probe

    def lsof(paths, *, deadline=None):
        nonlocal lsof_ran
        lsof_ran = True
        return {os.path.abspath(path): (True, "") for path in paths}

    def publish_during_probe(records, probe, *, deadline=None):
        result = original_probe(records, probe, deadline=deadline)
        if deadline is not None and lsof_ran:
            _record(
                ledger, "probe-running-child", state="running",
                parent_dispatch_id=dispatch_id,
            )
        return result

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", lsof)
    monkeypatch.setattr(reaper, "_identity_snapshot_probe", publish_during_probe)
    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_kept_reasons"]["used_by_non_terminal_record"] == 1


def test_terminal_scratch_identity_probe_timeout_keeps_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "identity-probe-timeout"
    scratch = _scratch_dir(dispatch, dispatch_id)
    _record(ledger, dispatch_id)
    original_probe = reaper._identity_snapshot_probe
    probe_calls = 0

    def time_out_probe(records, probe, *, deadline=None):
        nonlocal probe_calls
        if deadline is not None and probe_calls:
            raise subprocess.TimeoutExpired(["ps"], 30)
        if deadline is not None:
            probe_calls += 1
        return original_probe(records, probe, deadline=deadline)

    monkeypatch.setattr(reaper, "_identity_snapshot_probe", time_out_probe)
    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_kept_reasons"]["identity_probe_timeout"] == 1


def test_unreadable_post_lsof_ledger_keeps_candidates_and_continues_sweep(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    scratch_dirs = []
    for dispatch_id in ("partial-ledger-one", "partial-ledger-two"):
        scratch_dirs.append(_scratch_dir(dispatch, dispatch_id))
        _record(ledger, dispatch_id)
    tail = dispatch / "tail-owner.tail"
    tail.write_text("tail", encoding="utf-8")
    _record(ledger, "tail-owner", ended_at=NOW - dt.timedelta(days=2))

    def lsof(paths, *, deadline=None):
        (ledger / "partial.json").write_text("{", encoding="utf-8")
        return {os.path.abspath(path): (True, "") for path in paths}

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", lsof)
    report = _run(homes, ledger, dispatch, pidfiles)

    assert all(path.is_dir() for path in scratch_dirs)
    assert report["scratch_kept_reasons"]["ledger_unreadable"] == 2
    assert report["mode"] == "delete"
    assert tail.is_file()
    assert report["dispatch_gc"]["classes"]["skipped_recent"]["count"] == 3


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "reason"),
    [
        ("DISPATCH_SCRATCH_WALK_MAX_ENTRIES", 1, "walk_entry_limit"),
        ("DISPATCH_SCRATCH_WALK_TIMEOUT_SECONDS", 0, "walk_timeout"),
    ],
)
def test_terminal_scratch_keeps_directory_when_scan_limit_is_reached(
    tmp_path: Path, monkeypatch, limit_name: str, limit_value: int | float,
    reason: str,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "bounded-walk-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id, files={"marker": b"file"})
    _record(ledger, dispatch_id)
    monkeypatch.setattr(reaper, limit_name, limit_value)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_kept_reasons"][reason] == 1


def test_terminal_scratch_tree_is_walked_once_per_sweep(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    dispatch_id = "single-walk-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id)
    _record(ledger, dispatch_id)
    original_walk = reaper.allocated_tree_bytes
    scratch_walks = []

    def observe_walk(path, **kwargs):
        if Path(path) == scratch:
            scratch_walks.append(path)
        return original_walk(path, **kwargs)

    monkeypatch.setattr(reaper, "allocated_tree_bytes", observe_walk)
    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_would_delete_count"] == 1
    assert report["scratch_deleted_count"] == 0
    assert scratch_walks == [scratch]


@pytest.mark.parametrize("probe_error", [None, PermissionError("probe denied")])
def test_dispatch_scratch_is_kept_while_worker_group_may_be_alive(
    tmp_path: Path, monkeypatch, probe_error: Exception | None
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    dispatch_id = "live-scratch-worker"
    scratch = _scratch_dir(
        dispatch, dispatch_id, files={"browser-profile": b"still in use"},
    )
    _record(ledger, dispatch_id, worker_pgid=4343)
    killpg_calls = []

    def group_alive(pgid, sig):
        killpg_calls.append((pgid, sig))
        if probe_error is not None:
            raise probe_error

    monkeypatch.setattr(reaper.os, "killpg", group_alive)

    _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert killpg_calls == []


def test_legacy_claude_scratch_requires_one_old_owner_and_clear_lsof(
    tmp_path: Path, monkeypatch
) -> None:
    process_snapshot = reaper._legacy_scratch_process_snapshot
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", process_snapshot)
    legacy_root = reaper._legacy_claude_scratch_dir()
    old = legacy_root / "task-old-owner"
    old.mkdir()
    (old / "checkout-marker").write_bytes(b"old" * 256)
    old_bytes = reaper.allocated_tree_bytes(old)
    _record(ledger, "old-owner")

    young = legacy_root / "task-young-owner"
    young.mkdir()
    _record(ledger, "young-owner", ended_at=NOW - dt.timedelta(hours=23))
    live = legacy_root / "task-live-owner"
    live.mkdir()
    _record(ledger, "live-owner")

    ambiguous = legacy_root / "prefix-double-owner"
    ambiguous.mkdir()
    _record(ledger, "double-owner")
    _record(ledger, "prefix-double-owner")

    unmapped = legacy_root / "unmapped-scratch"
    unmapped.mkdir()
    project_bucket = legacy_root / "-Users-project-path"
    project_bucket.mkdir()
    file_path = legacy_root / "file-owner"
    file_path.write_text("not a directory", encoding="utf-8")
    _record(ledger, "file-owner")
    lsof_hit = legacy_root / "task-lsof-owner"
    lsof_hit.mkdir()
    _record(ledger, "lsof-owner")

    real_run = reaper.subprocess.run
    lsof_calls = []
    lsof_timeouts = []

    def fixture_lsof(argv, **kwargs):
        if argv[0] == "lsof":
            lsof_calls.append(argv)
            lsof_timeouts.append(kwargs.get("timeout"))
            output = f"p123\0cPython\0\nfcwd\0tDIR\0n{lsof_hit}\0\n"
            return subprocess.CompletedProcess(argv, 0, output, "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(reaper.subprocess, "run", fixture_lsof)

    def probe(record):
        return (True, "live") if record["dispatch_id"] == "live-owner" else _dead(record)

    report = _run(homes, ledger, dispatch, pidfiles, probe=probe)

    assert not old.exists()
    assert report["legacy_deleted_count"] == 1
    assert report["legacy_deleted_bytes"] == old_bytes
    assert young.is_dir() and live.is_dir() and ambiguous.is_dir()
    assert unmapped.is_dir() and project_bucket.is_dir() and file_path.is_file()
    assert lsof_hit.is_dir()
    for reason in (
        "inside_minimum_age", "live_worker", "multiple_owners", "unmapped",
        "project_path_bucket", "not_directory", "lsof_hit",
    ):
        assert report["legacy_kept_reasons"][reason] == 1
    assert lsof_calls == [
        ["lsof", "-a", "-u", str(os.getuid()), "-Fpcfnt0"],
        ["lsof", "-a", "-u", str(os.getuid()), "-Fpcfnt0"],
    ]
    assert set(lsof_timeouts) == {reaper._LEGACY_SCRATCH_LSOF_TIMEOUT_SECONDS}


def test_sweep_keeps_live_recent_and_unknown_and_deletes_terminal_old(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    live = _home(homes, "live-owner")
    recent = _home(homes, "recent-owner")
    old = _home(homes, "old-owner")
    unknown = _home(homes, "unknown-owner")
    _record(ledger, "live-owner")
    _record(ledger, "recent-owner", ended_at=NOW - dt.timedelta(hours=1))
    _record(ledger, "old-owner")
    old_scratch = reaper._legacy_claude_scratch_dir() / "old-owner"
    old_scratch.mkdir()
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {
            os.path.abspath(path): (True, "") for path in paths
        },
    )

    def probe(record):
        return (True, "live") if record["dispatch_id"] == "live-owner" else _dead(record)

    report = _run(homes, ledger, dispatch, pidfiles, probe=probe)

    assert live.is_dir() and recent.is_dir() and unknown.is_dir()
    assert not old.exists() and not old_scratch.exists()
    assert report["eligible_count"] == 1
    assert report["deleted_count"] == 1
    assert report["legacy_eligible_count"] == 1
    assert report["legacy_deleted_count"] == 1
    assert report["kept_reasons"]["live_worker"] == 1
    assert report["kept_reasons"]["inside_minimum_age"] == 1


@pytest.mark.parametrize(
    "output",
    [
        pytest.param("p123\0cPython\0\nf0\0tREG\0n/tmp/file\0", id="truncated"),
        pytest.param(
            "p123\0cPython\0\nf0\0n/tmp/file\0tREG\0\n",
            id="malformed-field-order",
        ),
        pytest.param(
            "p123\0cPython\0\nfNOFD\0tNOFD\0n/proc/123/fd: Permission denied\0\n",
            id="nofd-permission-denied",
        ),
        pytest.param(
            "p123\0cPython\0\nf0\0nPermission denied\0\n",
            id="unresolved-permission-denied",
        ),
        pytest.param(
            "p123\0cPython\0\nf0\0tVNODE\0nno more information\0\n",
            id="vnode-no-more-information",
        ),
        pytest.param(
            "p123\0cPython\0\nf0\0tVNODE\0nvnode: Input/output error\0\n",
            id="vnode-io-error",
        ),
        pytest.param(
            "p123\0cPython\0\nf0\0tVNODE\0nunknown diagnostic\0\n",
            id="unknown-diagnostic",
        ),
        pytest.param(
            "p123\0cPython\0\nf0\0tUNKNOWN\0nopaque object\0\n",
            id="unknown-type-nonpath",
        ),
        pytest.param("garbage\0\n", id="unparsable"),
        pytest.param("", id="empty-while-processes-exist"),
        pytest.param(None, id="timeout"),
    ],
)
def test_legacy_scratch_is_kept_when_batched_lsof_snapshot_is_invalid(
    tmp_path: Path, monkeypatch,
    output: str | None,
) -> None:
    process_snapshot = reaper._legacy_scratch_process_snapshot
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", process_snapshot)
    legacy_root = reaper._legacy_claude_scratch_dir()
    paths = [legacy_root / f"old-owner-{index}" for index in range(2)]
    for index, path in enumerate(paths):
        path.mkdir()
        _record(ledger, f"old-owner-{index}")

    real_run = reaper.subprocess.run
    lsof_calls = []

    def malformed_lsof(argv, **kwargs):
        if argv[0] == "lsof":
            lsof_calls.append((argv, kwargs.get("timeout")))
            if output is None:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return subprocess.CompletedProcess(argv, 0, output, "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(reaper.subprocess, "run", malformed_lsof)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert all(path.is_dir() for path in paths)
    reason = "lsof_timeout" if output is None else "lsof_error"
    assert report["legacy_kept_reasons"][reason] == len(paths)
    assert lsof_calls == [(
        ["lsof", "-a", "-u", str(os.getuid()), "-Fpcfnt0"],
        reaper._LEGACY_SCRATCH_LSOF_TIMEOUT_SECONDS,
    )] * 2


@pytest.mark.parametrize(
    ("filename", "fields"),
    [
        ("open-file", "f0\0n{path}\0"),
        ("Permission denied", "f0\0tREG\0n{path}\0"),
    ],
)
def test_legacy_lsof_matches_absolute_path_without_requiring_type(
    tmp_path: Path, monkeypatch, filename: str, fields: str,
) -> None:
    candidate = tmp_path / filename
    candidate.mkdir()
    output = f"p123\0cPython\0\n{fields.format(path=candidate)}\n"
    monkeypatch.setattr(
        reaper.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output, ""),
    )

    assert reaper._legacy_scratch_process_snapshot([candidate]) == {
        os.path.abspath(candidate): (False, "lsof_hit")
    }


@pytest.mark.parametrize(
    "diagnostic",
    ["vnode: FD unavailable", "vnode: process unavailable"],
)
def test_legacy_lsof_ignores_framed_gone_diagnostics(
    tmp_path: Path, monkeypatch, diagnostic: str,
) -> None:
    candidate = tmp_path / "old-candidate"
    candidate.mkdir()
    output = f"p123\0cPython\0\nf0\0n{diagnostic}\0\n"
    monkeypatch.setattr(
        reaper.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output, ""),
    )

    assert reaper._legacy_scratch_process_snapshot([candidate]) == {
        os.path.abspath(candidate): (True, "")
    }


@pytest.mark.parametrize(
    ("file_type", "name"),
    [
        ("IPv4", "*:443"),
        ("PIPE", "->0x1234"),
        ("CHAN", "00000000-0000-4000-8000-000000000032[1]"),
        ("NEXUS", ""),
    ],
)
def test_legacy_lsof_ignores_recognized_non_file_records(
    tmp_path: Path, monkeypatch, file_type: str, name: str,
) -> None:
    candidate = tmp_path / "old-candidate"
    candidate.mkdir()
    output = f"p123\0cPython\0\nf0\0t{file_type}\0n{name}\0\n"
    monkeypatch.setattr(
        reaper.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output, ""),
    )

    assert reaper._legacy_scratch_process_snapshot([candidate]) == {
        os.path.abspath(candidate): (True, "")
    }


def test_legacy_scratch_is_kept_while_background_child_holds_file(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    legacy = reaper._legacy_claude_scratch_dir() / "task-owner"
    legacy.mkdir()
    held_file = legacy / "held-open"
    child = subprocess.Popen(
        [
            sys.executable, "-c",
            "import sys,time; f=open(sys.argv[1], 'w'); f.write('ready'); f.flush(); time.sleep(60)",
            str(held_file),
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if held_file.exists() and held_file.read_text(encoding="utf-8") == "ready":
                break
            time.sleep(0.01)
        assert held_file.read_text(encoding="utf-8") == "ready"
        _record(ledger, "owner", worker_pgid=os.getpgid(child.pid))
        snapshot_calls = []
        monkeypatch.setattr(
            reaper, "_legacy_scratch_process_snapshot",
            lambda paths, *, deadline=None: snapshot_calls.extend(paths) or {
                os.path.abspath(path): (True, "") for path in paths
            },
        )

        report = _run(homes, ledger, dispatch, pidfiles)

        assert child.poll() is None
        assert legacy.is_dir()
        assert report["legacy_deleted_count"] == 0
        assert snapshot_calls == [legacy, legacy]
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def test_legacy_scratch_requires_both_lsof_snapshots_to_be_clear(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    legacy = reaper._legacy_claude_scratch_dir() / "task-owner"
    legacy.mkdir()
    _record(ledger, "owner")
    snapshots = []

    def snapshot(paths, *, deadline=None):
        snapshots.append(list(paths))
        clear = len(snapshots) == 1
        return {
            os.path.abspath(path): (clear, "" if clear else "lsof_hit")
            for path in paths
        }

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", snapshot)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert legacy.is_dir()
    assert report["legacy_deleted_count"] == 0
    assert report["legacy_kept_reasons"]["lsof_hit"] == 1
    assert snapshots == [[legacy], [legacy]]


def test_legacy_owner_is_rechecked_against_locked_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    legacy = reaper._legacy_claude_scratch_dir() / "prefix-owner"
    legacy.mkdir()
    _record(ledger, "owner")
    lock_users = reaper._lock_users
    published = False

    def publish_ambiguous_owner(ids, lock_index, held=None):
        nonlocal published
        if not published:
            _record(ledger, "prefix-owner")
            published = True
        return lock_users(ids, lock_index, held)

    monkeypatch.setattr(reaper, "_lock_users", publish_ambiguous_owner)
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {
            os.path.abspath(path): (True, "") for path in paths
        },
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert legacy.is_dir()
    assert report["legacy_kept_reasons"]["multiple_owners"] == 1


@pytest.mark.parametrize("change", ["child_unreadable", "owner_running", "owner_young"])
def test_legacy_scratch_rechecks_owner_and_lineage_in_final_locked_snapshot(
    tmp_path: Path, monkeypatch, change: str,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    legacy = reaper._legacy_claude_scratch_dir() / "task-owner"
    legacy.mkdir()
    owner = _record(ledger, "owner")
    if change == "child_unreadable":
        _record(ledger, "child", parent_dispatch_id="owner")
    owner_path = ledger / "owner.json"
    child_path = ledger / "child.json"
    lock_users = reaper._lock_users
    records_from_directory = reaper._records_from_directory
    locked = False
    changed = False
    lsof_calls = []

    def mark_locks_acquired(ids, lock_index, held=None):
        nonlocal locked
        acquired = lock_users(ids, lock_index, held)
        locked = acquired is not None
        return acquired

    def change_final_snapshot(ledger_dir, *, dispatch_ids=None, record_names=None):
        nonlocal changed
        if locked and dispatch_ids is None and not changed:
            changed = True
            if change == "child_unreadable":
                child_path.write_text("{broken", encoding="utf-8")
            else:
                current_owner = json.loads(owner_path.read_text(encoding="utf-8"))
                if change == "owner_running":
                    current_owner["state"] = "running"
                else:
                    current_owner["ended_at"] = (NOW - dt.timedelta(hours=1)).isoformat()
                owner_path.write_text(json.dumps(current_owner), encoding="utf-8")
        return records_from_directory(
            ledger_dir, dispatch_ids=dispatch_ids, record_names=record_names,
        )

    monkeypatch.setattr(reaper, "_lock_users", mark_locks_acquired)
    monkeypatch.setattr(reaper, "_records_from_directory", change_final_snapshot)
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: lsof_calls.extend(paths) or {
            os.path.abspath(path): (True, "") for path in paths
        },
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert owner["dispatch_id"] == "owner"
    assert legacy.is_dir()
    assert lsof_calls == [legacy, legacy]
    if change == "child_unreadable":
        assert report["legacy_kept_reasons"]["eligibility_changed"] == 1
    else:
        assert report["legacy_kept_reasons"][
            "non_terminal" if change == "owner_running" else "inside_minimum_age"
        ] == 1


def test_legacy_scratch_is_kept_for_matching_unreadable_ledger_record(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    legacy = reaper._legacy_claude_scratch_dir() / "task-owner"
    legacy.mkdir()
    _record(ledger, "owner")
    (ledger / "task-owner.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda _paths, *, deadline=None: pytest.fail("unknown legacy owner must not reach lsof"),
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert legacy.is_dir()
    assert report["legacy_kept_reasons"]["unknown_owner"] == 1


def test_legacy_scratch_matches_unreadable_transformed_dispatch_filename(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    legacy = reaper._legacy_claude_scratch_dir() / "prefix:part-owner"
    legacy.mkdir()
    _record(ledger, "owner")
    filename = reaper.goalflight_compat.safe_dispatch_filename("prefix:part-owner")
    (ledger / f"{filename}.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda _paths, *, deadline=None: pytest.fail("possible unknown owner must not reach lsof"),
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert legacy.is_dir()
    assert report["legacy_kept_reasons"]["unknown_owner"] == 1


def test_legacy_scratch_kept_when_locked_lineage_record_becomes_unreadable(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    legacy = reaper._legacy_claude_scratch_dir() / "task-owner"
    legacy.mkdir()
    _record(ledger, "owner")
    child = _record(ledger, "child", parent_dispatch_id="owner")
    child_path = ledger / f"{reaper.goalflight_compat.safe_dispatch_filename('child')}.json"
    lock_users = reaper._lock_users
    corrupted = False

    def corrupt_child_before_lock(ids, lock_index, held=None):
        nonlocal corrupted
        if not corrupted:
            child_path.write_text("{broken", encoding="utf-8")
            corrupted = True
        return lock_users(ids, lock_index, held)

    monkeypatch.setattr(reaper, "_lock_users", corrupt_child_before_lock)
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {
            os.path.abspath(path): (True, "") for path in paths
        },
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert child["dispatch_id"] == "child"
    assert legacy.is_dir()
    assert report["legacy_kept_reasons"]["eligibility_changed"] == 1


def test_unparseable_terminal_timestamp_does_not_abort_sweep(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    malformed = reaper._legacy_claude_scratch_dir() / "task-malformed-time"
    malformed.mkdir()
    malformed_record = _record(ledger, "malformed-time")
    malformed_path = ledger / "malformed-time.json"
    malformed_record["ended_at"] = "0001-01-01T00:00:00+01:00"
    malformed_path.write_text(json.dumps(malformed_record), encoding="utf-8")
    eligible = reaper._legacy_claude_scratch_dir() / "task-eligible-time"
    eligible.mkdir()
    _record(ledger, "eligible-time")
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {
            os.path.abspath(path): (True, "") for path in paths
        },
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert malformed.is_dir()
    assert not eligible.exists()
    assert report["legacy_kept_reasons"]["missing_or_invalid_ended_at"] == 1
    assert report["legacy_deleted_count"] == 1


def test_legacy_scratch_keeps_owner_with_missing_lineage(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    legacy = reaper._legacy_claude_scratch_dir() / "task-orphan-child"
    legacy.mkdir()
    _record(ledger, "orphan-child", parent_dispatch_id="missing-parent")

    report = _run(homes, ledger, dispatch, pidfiles)

    assert legacy.is_dir()
    assert report["legacy_kept_reasons"]["missing_lineage"] == 1


def test_retention_report_keeps_v1_json_shape_and_logs_new_categories(
    monkeypatch, capsys
) -> None:
    artifacts = {
        "schema": reaper.SCHEMA,
        "mode": "delete",
        "budget_bytes": reaper.DISPATCH_ARTIFACT_BUDGET_BYTES,
        "minimum_age_seconds": reaper.DISPATCH_ARTIFACT_MINIMUM_AGE.total_seconds(),
        "total_allocated_bytes": 128,
        "budget_measurable": True,
        "eligible_count": 1,
        "eligible_allocated_bytes": 128,
        "deleted_count": 1,
        "deleted_allocated_bytes": 128,
        "kept_reasons": {},
        "scratch_deleted_count": 1,
        "scratch_deleted_bytes": 64,
        "scratch_free_bytes": 10_000_000_000,
        "scratch_free_floor_bytes": reaper.DISPATCH_SCRATCH_FREE_SPACE_FLOOR_BYTES,
        "scratch_low_space": True,
        "scratch_minimum_idle_seconds": 3600,
        "scratch_size_warnings": {"large-id": 6_000_000_000},
        "scratch_kept_reasons": {"lsof_timeout": 1},
        "legacy_deleted_count": 1,
        "legacy_deleted_bytes": 32,
        "legacy_kept_reasons": {"lsof_hit": 1},
    }
    monkeypatch.setattr(
        reaper,
        "run_retention_sweep",
        lambda **_kwargs: {
            "ran": True,
            "artifacts": artifacts,
            "trace_buckets": 0,
            "snapshots": 0,
            "worktree_gc": 0,
            "worktree_gc_removals": [],
            "read_only_pools": 0,
            "copy_on_write": {
                "seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0,
            },
            "copy_on_write_reasons": {
                "seats": {"live": 2, "lock_busy": 1},
                "files": {"recent": 3, "xattr_differ": 1},
            },
            "cleanup_errors": [],
        },
    )

    assert reaper.main(["--delete", "--json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema"] == reaper.SCHEMA
    assert "scratch_deleted_count" not in payload
    assert "legacy_deleted_count" not in payload
    assert "legacy_kept_reasons" not in payload
    assert "scratch_deleted=1:" in captured.err
    assert "floor=below" in captured.err
    assert "large=large-id:5.6 GiB" in captured.err
    assert 'kept={"lsof_timeout": 1}' in captured.err
    assert "legacy_deleted=1:" in captured.err
    assert 'legacy_kept={"lsof_hit": 1}' in captured.err
    assert (
        "seat dedupe reasons: seats{live=2 unknown_liveness=0 lock_busy=1"
        in captured.err
    )
    assert "files{no_source=0 recent=3" in captured.err
    assert "xattr_differ=1" in captured.err


def test_budget_deletes_oldest_terminal_bundle_first(tmp_path: Path, monkeypatch) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    oldest = _home(homes, "oldest")
    newer = _home(homes, "newer")
    _record(ledger, "oldest", ended_at=NOW - dt.timedelta(days=5))
    _record(ledger, "newer", ended_at=NOW - dt.timedelta(days=2))
    keep_bytes = reaper.allocated_tree_bytes(newer)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", keep_bytes)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert not oldest.exists()
    assert newer.is_dir()
    assert report["total_allocated_bytes"] == keep_bytes
    assert report["deleted_count"] == 1


def test_shared_home_waits_for_latest_user_and_orders_by_its_end_time(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    shared = _home(homes, "parent")
    unrelated = _home(homes, "five-day")
    parent = _record(ledger, "parent", ended_at=NOW - dt.timedelta(days=8))
    _record(
        ledger,
        "child",
        ended_at=NOW - dt.timedelta(hours=23),
        parent_dispatch_id="parent",
        codex_home_owner_dispatch_id="parent",
    )
    unrelated_record = _record(ledger, unrelated.name, ended_at=NOW - dt.timedelta(days=5))
    keep_bytes = reaper.allocated_tree_bytes(shared)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", keep_bytes)

    first = _run(homes, ledger, dispatch, pidfiles)

    assert shared.is_dir()
    assert not unrelated.exists()
    assert first["total_allocated_bytes"] == keep_bytes

    unrelated = _home(homes, unrelated.name)
    _record(ledger, unrelated.name, ended_at=NOW - dt.timedelta(days=5))
    _record(
        ledger,
        "child",
        ended_at=NOW - dt.timedelta(hours=25),
        parent_dispatch_id="parent",
        codex_home_owner_dispatch_id="parent",
    )
    second = _run(homes, ledger, dispatch, pidfiles)

    assert parent["dispatch_id"] == "parent"
    assert unrelated_record["dispatch_id"] == "five-day"
    assert not unrelated.exists()
    assert shared.is_dir()
    assert second["deleted_count"] == 1
    assert second["total_allocated_bytes"] == keep_bytes


@pytest.mark.parametrize(
    ("state", "age", "probe", "reason"),
    [
        ("withdrawn", dt.timedelta(days=2), lambda _r: (True, "live"), "live_worker"),
        ("complete", dt.timedelta(days=2), lambda _r: (False, "no_pid"), "liveness_indeterminate"),
        ("running", dt.timedelta(days=2), _dead, "non_terminal"),
        ("complete", dt.timedelta(hours=23), _dead, "inside_minimum_age"),
    ],
)
def test_ineligible_dispatches_are_kept(
    tmp_path: Path, monkeypatch, state, age, probe, reason
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "keep-me")
    _record(ledger, home.name, state=state, ended_at=NOW - age)

    report = _run(homes, ledger, dispatch, pidfiles, probe=probe)

    assert home.is_dir()
    assert report["kept_reasons"][reason] == 1


def test_published_resume_claim_keeps_parent_home(tmp_path: Path, monkeypatch) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    parent = _home(homes, "parent")
    _record(ledger, "parent", ended_at=NOW - dt.timedelta(days=3))
    _record(
        ledger,
        "child",
        state="waiting_capacity",
        ended_at=NOW,
        worker_pid=None,
        parent_dispatch_id="parent",
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert parent.is_dir()
    assert report["kept_reasons"]["non_terminal"] == 1


def test_child_written_during_scan_is_seen_under_resume_lock(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    parent_home = _home(homes, "grok-parent")
    _record(
        ledger,
        "grok-parent",
        ended_at=NOW - dt.timedelta(days=3),
        agent="grok",
        engine="grok",
        account="seat-g",
        effective_account="seat-g",
        engine_session_id=SESSION_B,
        worker_cwd="/project/grok",
    )
    session = goalflight_dispatch._seat_session_dir("seat-g", "grok", "/project/grok", SESSION_B)
    assert session is not None
    session.mkdir(parents=True)
    (session / "summary.json").write_text("{}", encoding="utf-8")
    (session / "chat_history.jsonl").write_text("{}\n", encoding="utf-8")
    scan = reaper._scan_dispatch_artifacts
    real_read = reaper._records_from_directory
    lock_path = goalflight_dispatch_paths.engine_resume_lock_path("grok", SESSION_B)
    read_count = 0
    listdir_count = 0
    real_listdir = os.listdir

    def write_child_after_scan(*args, **kwargs):
        artifacts = scan(*args, **kwargs)
        _record(
            ledger,
            "grok-child",
            state="running",
            ended_at=NOW - dt.timedelta(hours=1),
            parent_dispatch_id="grok-parent",
            agent="grok",
            engine="grok",
            account="seat-g",
            effective_account="seat-g",
            engine_session_id=SESSION_B,
            worker_cwd="/project/grok",
        )
        return artifacts

    def assert_locked_read(path, *args, **kwargs):
        nonlocal read_count
        read_count += 1
        if read_count > 1:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        return real_read(path, *args, **kwargs)

    def assert_locked_listdir(path):
        nonlocal listdir_count
        if Path(path) == ledger:
            listdir_count += 1
            if listdir_count > 1:
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
        return real_listdir(path)

    monkeypatch.setattr(reaper, "_scan_dispatch_artifacts", write_child_after_scan)
    monkeypatch.setattr(reaper, "_records_from_directory", assert_locked_read)
    monkeypatch.setattr(os, "listdir", assert_locked_listdir)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert parent_home.is_dir()
    assert session.is_dir()
    assert report["deleted_count"] == 0


def test_retention_rechecks_candidate_records_with_bounded_ledger_reads(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    candidate_ids = {f"candidate-{index:04d}" for index in range(1000)}
    for dispatch_id in candidate_ids:
        _home(homes, dispatch_id)
        _record(ledger, dispatch_id)
    for index in range(1000):
        _record(ledger, f"inactive-{index:04d}", state="running")

    ledger_reads = 0
    ledger_listdirs = 0
    read_text = Path.read_text
    listdir = os.listdir

    def count_ledger_reads(path, *args, **kwargs):
        nonlocal ledger_reads
        if path.parent == ledger:
            ledger_reads += 1
        return read_text(path, *args, **kwargs)

    def count_ledger_listdirs(path):
        nonlocal ledger_listdirs
        if Path(path) == ledger:
            ledger_listdirs += 1
        return listdir(path)

    monkeypatch.setattr(Path, "read_text", count_ledger_reads)
    monkeypatch.setattr(os, "listdir", count_ledger_listdirs)
    probed_ids = []

    def probe(record):
        probed_ids.append(record["dispatch_id"])
        return _dead(record)

    report = _run(homes, ledger, dispatch, pidfiles, probe=probe)

    assert report["eligible_count"] == 1000
    assert report["deleted_count"] == 1000
    assert ledger_reads <= 4000
    assert ledger_listdirs == 2
    assert len(probed_ids) == 2000
    assert set(probed_ids) == candidate_ids


def test_retention_lists_ledger_after_candidate_lock_and_sees_new_child(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    for index in range(4):
        dispatch_id = f"candidate-{index:04d}"
        _home(homes, dispatch_id)
        _record(ledger, dispatch_id)

    ledger_listdirs = 0
    listdir = os.listdir

    def count_ledger_listdirs(path):
        nonlocal ledger_listdirs
        if Path(path) == ledger:
            ledger_listdirs += 1
        return listdir(path)

    monkeypatch.setattr(os, "listdir", count_ledger_listdirs)
    lock_users = reaper._lock_users
    child_written = False
    parent_id = None

    def publish_child_before_lock(ids, lock_index, held=None):
        nonlocal child_written, parent_id
        if not child_written:
            parent_id = next(iter(ids))
            _record(
                ledger,
                "late-child",
                state="running",
                parent_dispatch_id=parent_id,
            )
            child_written = True
        return lock_users(ids, lock_index, held)

    monkeypatch.setattr(reaper, "_lock_users", publish_child_before_lock)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert parent_id is not None
    assert (homes / parent_id).is_dir()
    assert report["deleted_count"] == 3
    assert ledger_listdirs >= 2  # initial scan plus a fresh scan after locking


def test_new_terminal_child_lock_is_added_before_sweep_delete(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    parent_home = _home(homes, "grok-parent")
    _record(
        ledger,
        "grok-parent",
        ended_at=NOW - dt.timedelta(days=3),
        agent="grok",
        engine="grok",
        account="seat-g",
        effective_account="seat-g",
        engine_session_id=SESSION_B,
        worker_cwd="/project/grok",
    )
    session = goalflight_dispatch._seat_session_dir(
        "seat-g", "grok", "/project/grok", SESSION_B
    )
    assert session is not None
    session.mkdir(parents=True)
    (session / "summary.json").write_text("{}", encoding="utf-8")
    (session / "chat_history.jsonl").write_text("{}\n", encoding="utf-8")
    scan = reaper._scan_dispatch_artifacts
    child_written = False

    def write_child_after_scan(*args, **kwargs):
        nonlocal child_written
        artifacts = scan(*args, **kwargs)
        if not child_written:
            child_written = True
            _record(
                ledger,
                "grok-child",
                state="complete",
                ended_at=NOW - dt.timedelta(days=2),
                parent_dispatch_id="grok-parent",
                agent="grok",
                engine="grok",
                account="seat-g",
                effective_account="seat-g",
                engine_session_id=SESSION_B,
                worker_cwd="/project/grok",
            )
        return artifacts

    locked_users = []
    resolve_lock = goalflight_dispatch._resume_lock_path_info

    def record_locked_user(record, dispatch_id):
        locked_users.append(dispatch_id)
        return resolve_lock(record, dispatch_id)

    monkeypatch.setattr(reaper, "_scan_dispatch_artifacts", write_child_after_scan)
    monkeypatch.setattr(
        goalflight_dispatch, "_resume_lock_path_info", record_locked_user
    )

    report = _run(homes, ledger, dispatch, pidfiles)

    assert "grok-child" in locked_users
    assert report["deleted_count"] > 0
    assert not parent_home.exists()


def test_sweep_indexes_codex_rollouts_once_per_account(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    account_home = Path.home() / ".goal-flight" / "accounts" / "seat-a" / "codex"
    rollout_paths = []
    for index, session_id in enumerate((SESSION_A, SESSION_B)):
        dispatch_id = f"codex-index-{index}"
        home = _home(homes, dispatch_id)
        _record(
            ledger,
            dispatch_id,
            agent="codex",
            engine="codex",
            account="seat-a",
            effective_account="seat-a",
            codex_session_id=session_id,
            codex_home=str(home),
            codex_home_owner_dispatch_id=dispatch_id,
        )
        rollout = account_home / "sessions" / f"rollout-2026-10-01T00-00-00-{session_id}.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_bytes(f"rollout {index}".encode())
        rollout_paths.append(rollout)
    import goalflight_codex_sessions

    real_index = goalflight_codex_sessions.rollout_index
    calls = []

    def count_index(home):
        calls.append(home)
        return real_index(home)

    monkeypatch.setattr(goalflight_codex_sessions, "rollout_index", count_index)

    _run(homes, ledger, dispatch, pidfiles)

    assert calls == [account_home]
    assert all(not path.exists() for path in rollout_paths)


def test_recorded_output_paths_are_deleted_only_inside_dispatch_dir(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "paths")
    outside = tmp_path / "shared.log"
    inside = dispatch / "nested" / "prompt.out"
    inside.parent.mkdir()
    outside.write_text("shared", encoding="utf-8")
    inside.write_text("private", encoding="utf-8")
    _record(
        ledger, "paths", ended_at=NOW - dt.timedelta(days=10),
        stdout_path=str(inside), tail_path=str(outside),
    )

    _run(homes, ledger, dispatch, pidfiles)

    assert not home.exists()
    assert not inside.exists()
    assert gzip.decompress(Path(str(inside) + ".gz").read_bytes()) == b"private"
    assert outside.is_file()


def test_unmapped_account_rollout_is_never_touched(tmp_path: Path, monkeypatch) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "mapped-dispatch")
    _record(
        ledger,
        home.name,
        agent="codex",
        engine="codex",
        account="seat-a",
        effective_account="seat-a",
        codex_session_id=SESSION_A,
        codex_home=str(home),
        codex_home_owner_dispatch_id=home.name,
    )
    account_home = Path.home() / ".goal-flight" / "accounts" / "seat-a" / "codex"
    unmapped = account_home / "sessions" / f"rollout-2026-10-01T00-00-00-{SESSION_B}.jsonl"
    unmapped.parent.mkdir(parents=True)
    unmapped.write_text("unmapped", encoding="utf-8")

    _run(homes, ledger, dispatch, pidfiles)

    assert not home.exists()
    assert unmapped.is_file()


def test_missing_ledger_and_unmapped_home_count_but_are_not_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    orphan = _home(homes, "orphan-home")

    report = _run(homes, ledger, dispatch, pidfiles)

    assert orphan.is_dir()
    assert report["total_allocated_bytes"] == reaper.allocated_tree_bytes(orphan)
    assert report["deleted_count"] == 0


def test_trace_excerpt_expiry_is_thirty_days(tmp_path: Path) -> None:
    traces = tmp_path / "docs-private" / "traces"
    old = traces / "2026-09-11"
    recent = traces / "2026-09-13"
    old.mkdir(parents=True)
    recent.mkdir()
    (old / "excerpt.md").write_text("old", encoding="utf-8")
    (recent / "excerpt.md").write_text("recent", encoding="utf-8")

    assert reaper._expire_trace_archives(tmp_path, now=NOW) == 1
    assert not old.exists()
    assert recent.is_dir()


def test_sweep_runs_worktree_and_read_only_reapers_without_dispatches(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    calls = []
    cwd_snapshots = []
    import goalflight_worktree_gc
    import goalflight_worktree_pool

    monkeypatch.setattr(reaper, "_project_roots", lambda _records, _errors, **_kwargs: {project})

    def process_cwd_snapshot(*, timeout_s, deadline=None):
        cwd_snapshots.append(timeout_s)
        return None

    def reap_read_only(root, **kwargs):
        census = kwargs["process_cwd_census"]
        assert census() is None
        assert census() is None
        calls.append(("read-only", root))

    def run_gc(argv, **kwargs):
        census = kwargs["_process_cwd_census"]
        assert census() is None
        calls.append(("gc", argv))
        return 0

    monkeypatch.setattr(
        goalflight_worktree_pool, "_process_cwd_snapshot", process_cwd_snapshot
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "reap_read_only_worktrees",
        reap_read_only,
    )
    monkeypatch.setattr(
        goalflight_worktree_gc,
        "main",
        run_gc,
    )

    result = reaper._run_sweep_body(
        homes_dir=homes,
        ledger_dir=ledger,
        now=NOW,
        identity_probe=_dead,
        deadline=time.monotonic() + 60,
    )

    assert calls == [
        ("read-only", project),
        (
            "gc",
            [
                str(project), "--ledger-dir", str(ledger), "--apply", "--json",
            ],
        ),
    ]
    assert result["artifacts"]["total_allocated_bytes"] == 0
    assert len(cwd_snapshots) == 1
    assert 0 < cwd_snapshots[0] <= 30
    assert result["_measurements"]["cwd_snapshot_count"] == 1
    assert result["_measurements"]["fresh_cwd_snapshot_count"] == 0


def _cow_git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _cow_empty_base(project: Path) -> tuple[str, str]:
    project.mkdir()
    _cow_git("init", "--quiet", "-b", "main", cwd=project)
    _cow_git("config", "user.name", "Goal Flight Test", cwd=project)
    _cow_git("config", "user.email", "test@example.invalid", cwd=project)
    _cow_git("commit", "--quiet", "--allow-empty", "-m", "base", cwd=project)
    base = _cow_git("rev-parse", "HEAD", cwd=project)
    _cow_git("checkout", "--quiet", "-b", "seat-source", cwd=project)
    (project / "source.txt").write_bytes(b"shared across seats\n")
    _cow_git("add", "source.txt", cwd=project)
    _cow_git("commit", "--quiet", "-m", "source", cwd=project)
    source_commit = _cow_git("rev-parse", "HEAD", cwd=project)
    _cow_git("checkout", "--quiet", "-b", "seat-target", base, cwd=project)
    (project / "target.txt").write_bytes(b"shared across seats\n")
    os.chmod(project / "target.txt", 0o755)
    _cow_git("add", "target.txt", cwd=project)
    _cow_git("commit", "--quiet", "-m", "target", cwd=project)
    target_commit = _cow_git("rev-parse", "HEAD", cwd=project)
    _cow_git("checkout", "--quiet", "main", cwd=project)
    (project / "main.txt").write_bytes(b"main only\n")
    _cow_git("add", "main.txt", cwd=project)
    _cow_git("commit", "--quiet", "-m", "main", cwd=project)
    return source_commit, target_commit


def _cow_pool_seats(project: Path, commits: list[str]) -> list[Path]:
    import goalflight_worktree_pool

    leases = [
        goalflight_worktree_pool.acquire_worktree_seat(project, f"cow-fixture-{i}")
        for i in range(len(commits))
    ]
    paths = [lease.path for lease in leases]
    try:
        for lease, commit in zip(leases, commits):
            _cow_git("checkout", "--quiet", "--detach", commit, cwd=lease.path)
    finally:
        for lease in leases:
            lease.release()
    return paths


def _age_cow_checkout(checkout: Path) -> None:
    tracked = subprocess.run(
        ["git", "-C", str(checkout), "ls-files", "-z"],
        check=True, capture_output=True,
    ).stdout
    old_ns = time.time_ns() - 3600 * 1_000_000_000
    for raw_path in tracked.split(b"\0"):
        if not raw_path:
            continue
        path = checkout / os.fsdecode(raw_path)
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                os.utime(path, ns=(info.st_atime_ns, old_ns), follow_symlinks=False)
        except OSError:
            pass


def _cow_dedupe(
    project: Path, *, records=None, identity_probe=None, age_files: bool = True,
    seat_busy=None,
) -> dict:
    if age_files:
        import goalflight_worktree_pool

        for checkout in [
            project, *goalflight_worktree_pool._registered_pool_worktrees(project)
        ]:
            _age_cow_checkout(checkout)
    previous_seat_busy = getattr(seat_dedupe, "_seat_has_open_handles", None)
    if seat_busy is None:
        seat_dedupe._seat_has_open_handles = lambda _seat, _timeout, *, deadline=None: False
    else:
        seat_dedupe._seat_has_open_handles = seat_busy
    try:
        probe = identity_probe or reaper.goalflight_ledger.identity_matches

        def timed_probe(record, deadline):
            if probe is reaper.goalflight_ledger.identity_matches:
                return reaper.goalflight_ledger._identity_matches_before_deadline(
                    record, deadline
                )
            return probe(record)

        return seat_dedupe.dedupe_pool(
            project,
            records=records or [],
            identity_probe=timed_probe,
            identity_inactive=reaper._identity_inactive,
        )
    finally:
        if previous_seat_busy is None:
            if hasattr(seat_dedupe, "_seat_has_open_handles"):
                del seat_dedupe._seat_has_open_handles
        else:
            seat_dedupe._seat_has_open_handles = previous_seat_busy


def _simple_cow_pool(tmp_path: Path, monkeypatch, content: bytes = b"shared\n"):
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    _cow_git("init", "--quiet", "-b", "main", cwd=project)
    _cow_git("config", "user.name", "Goal Flight Test", cwd=project)
    _cow_git("config", "user.email", "test@example.invalid", cwd=project)
    (project / "shared.txt").write_bytes(content)
    _cow_git("add", "shared.txt", cwd=project)
    _cow_git("commit", "--quiet", "-m", "shared", cwd=project)
    commit = _cow_git("rev-parse", "HEAD", cwd=project)
    seats = _cow_pool_seats(project, [commit, commit])
    return project, seats


def test_seat_dedupe_does_not_follow_symlinked_index_parents(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    _cow_git("init", "--quiet", "-b", "main", cwd=project)
    _cow_git("config", "user.name", "Goal Flight Test", cwd=project)
    _cow_git("config", "user.email", "test@example.invalid", cwd=project)
    nested = project / "nested"
    nested.mkdir()
    (nested / "shared.txt").write_bytes(b"shared outside the seat\n")
    _cow_git("add", "nested/shared.txt", cwd=project)
    _cow_git("commit", "--quiet", "-m", "nested file", cwd=project)
    commit = _cow_git("rev-parse", "HEAD", cwd=project)
    seats = _cow_pool_seats(project, [commit, commit])
    for checkout in [project, *seats]:
        _age_cow_checkout(checkout)

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "shared.txt"
    outside_file.write_bytes(b"shared outside the seat\n")
    old_ns = time.time_ns() - 3600 * 1_000_000_000
    os.utime(outside_file, ns=(old_ns, old_ns))
    outside_before = (
        outside_file.stat().st_ino,
        outside_file.stat().st_mtime_ns,
        outside_file.read_bytes(),
    )
    for seat in seats:
        shutil.rmtree(seat / "nested")
        (seat / "nested").symlink_to(outside, target_is_directory=True)

    report = _cow_dedupe(project, age_files=False)

    assert report["files_cloned"] == 0
    assert all((seat / "nested").is_symlink() for seat in seats)
    assert (
        outside_file.stat().st_ino,
        outside_file.stat().st_mtime_ns,
        outside_file.read_bytes(),
    ) == outside_before


def test_seat_dedupe_uses_cross_seat_blob_and_skips_already_shared_file(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    source_commit, target_commit = _cow_empty_base(project)
    source, target = _cow_pool_seats(project, [source_commit, target_commit])
    import goalflight_worktree_pool

    first_seat = goalflight_worktree_pool._registered_pool_worktrees(project)[0].resolve()
    designated_source = source if first_seat == source.resolve() else target
    designated_path = designated_source / ("source.txt" if designated_source == source else "target.txt")
    source_before = designated_path.stat().st_ino
    target_file = target / "target.txt"
    target_before_mode = target_file.stat().st_mode & 0o777
    index_path = Path(_cow_git("rev-parse", "--git-path", "index", cwd=target))
    if not index_path.is_absolute():
        index_path = target / index_path
    index_mtime = index_path.stat().st_mtime_ns

    report = _cow_dedupe(project)

    assert {key: report[key] for key in (
        "seats_scanned", "files_cloned", "bytes_shared", "errors",
    )} == {
        "seats_scanned": 2,
        "files_cloned": 1,
        "bytes_shared": len(b"shared across seats\n"),
        "errors": [],
    }
    assert set(report["skip_counts"]) == {"seats", "files"}
    assert target_file.read_bytes() == (source / "source.txt").read_bytes()
    assert target_file.stat().st_mode & 0o777 == target_before_mode == 0o755
    assert index_path.stat().st_mtime_ns == index_mtime
    assert designated_path.stat().st_ino == source_before
    assert seat_dedupe._private_bytes(target_file) == 0
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    first_status = subprocess.run(
        ["git", "-C", str(target), "status", "--porcelain"],
        check=True, capture_output=True, env=env,
    ).stdout
    assert first_status == b""
    second = _cow_dedupe(project)
    assert second["files_cloned"] == 0
    assert second["bytes_shared"] == 0
    assert designated_path.stat().st_ino == source_before
    _cow_git("checkout", "--quiet", "--detach", source_commit, cwd=target)
    assert _cow_git("status", "--porcelain", cwd=target) == ""


def test_seat_dedupe_keeps_a_destination_write_during_comparison(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    destination = seats[-1] / "shared.txt"
    before = destination.stat().st_ino
    same_bytes = getattr(
        seat_dedupe,
        "_same_bytes",
        lambda first, second, _deadline: first.read_bytes() == second.read_bytes(),
    )

    def race_after_compare(first: Path, second: Path, deadline: float) -> bool:
        equal = same_bytes(first, second, deadline)
        if equal and second == destination:
            second.write_bytes(b"editor write during clone\n")
        return equal

    monkeypatch.setattr(seat_dedupe, "_same_bytes", race_after_compare, raising=False)
    report = _cow_dedupe(project)
    assert destination.read_bytes() == b"editor write during clone\n"
    assert destination.stat().st_ino == before
    assert _cow_git("status", "--porcelain", cwd=seats[-1])


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_seat_dedupe_keeps_hidden_worktree_edits(
    tmp_path: Path, monkeypatch, flag: str
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    destination = seats[-1] / "shared.txt"
    local = b"hidden local edit\n"
    _cow_git("update-index", flag, "--", "shared.txt", cwd=seats[-1])
    destination.write_bytes(local)
    before_inode = destination.stat().st_ino
    report = _cow_dedupe(project)
    assert destination.read_bytes() == local
    assert destination.stat().st_ino == before_inode
    assert report["files_cloned"] <= 1


def test_seat_dedupe_keeps_head_bytes_when_index_has_another_staged_blob(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    _cow_git("init", "--quiet", "-b", "main", cwd=project)
    _cow_git("config", "user.name", "Goal Flight Test", cwd=project)
    _cow_git("config", "user.email", "test@example.invalid", cwd=project)
    (project / "donor.txt").write_bytes(b"staged bytes\n")
    (project / "target.txt").write_bytes(b"HEAD bytes\n")
    _cow_git("add", "donor.txt", "target.txt", cwd=project)
    _cow_git("commit", "--quiet", "-m", "base", cwd=project)
    commit = _cow_git("rev-parse", "HEAD", cwd=project)
    seats = _cow_pool_seats(project, [commit, commit])
    target = seats[-1] / "target.txt"
    (seats[-1] / "target.txt").write_bytes(b"staged bytes\n")
    _cow_git("add", "target.txt", cwd=seats[-1])
    target.write_bytes(b"HEAD bytes\n")
    before_status = _cow_git("status", "--porcelain", cwd=seats[-1])
    before_inode = target.stat().st_ino

    _cow_dedupe(project)

    assert target.read_bytes() == b"HEAD bytes\n"
    assert target.stat().st_ino == before_inode
    assert _cow_git("status", "--porcelain", cwd=seats[-1]) == before_status
    assert before_status.startswith("MM target.txt")


def test_seat_dedupe_keeps_filtered_crlf_and_ident_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    _cow_git("init", "--quiet", "-b", "main", cwd=project)
    _cow_git("config", "user.name", "Goal Flight Test", cwd=project)
    _cow_git("config", "user.email", "test@example.invalid", cwd=project)
    (project / ".gitattributes").write_text(
        "donor.txt text eol=lf ident\ntarget.txt text eol=crlf ident\n",
        encoding="utf-8",
    )
    _cow_git("add", ".gitattributes", cwd=project)
    _cow_git("commit", "--quiet", "-m", "attributes", cwd=project)
    base = _cow_git("rev-parse", "HEAD", cwd=project)
    _cow_git("checkout", "--quiet", "-b", "seat-donor", cwd=project)
    (project / "donor.txt").write_bytes(b"line\n$Id$\n")
    _cow_git("add", "donor.txt", cwd=project)
    _cow_git("commit", "--quiet", "-m", "donor", cwd=project)
    donor = _cow_git("rev-parse", "HEAD", cwd=project)
    _cow_git("checkout", "--quiet", "-b", "seat-destination", base, cwd=project)
    (project / "target.txt").write_bytes(b"line\n$Id$\n")
    _cow_git("add", "target.txt", cwd=project)
    _cow_git("commit", "--quiet", "-m", "destination", cwd=project)
    destination_commit = _cow_git("rev-parse", "HEAD", cwd=project)
    seats = _cow_pool_seats(project, [donor, destination_commit])
    destination = seats[-1] / "target.txt"
    before = destination.read_bytes()
    assert b"\r\n" in before and b"$Id:" in before

    _cow_dedupe(project)

    assert destination.read_bytes() == before
    assert _cow_git("status", "--porcelain", cwd=seats[-1]) == ""


def test_seat_dedupe_skips_the_whole_seat_when_lsof_reports_a_process(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    (project / "second.txt").write_bytes(b"second shared file\n")
    _cow_git("add", "second.txt", cwd=project)
    _cow_git("commit", "--quiet", "-m", "second", cwd=project)
    commit = _cow_git("rev-parse", "HEAD", cwd=project)
    for seat in seats:
        _cow_git("checkout", "--quiet", "--detach", commit, cwd=seat)
    files = [seats[0] / name for name in ("shared.txt", "second.txt")]
    before = {path: (path.stat().st_ino, path.read_bytes()) for path in files}
    busy_seats = []

    def report_busy(
        seat: Path, _timeout: float, *, deadline: float | None = None,
    ) -> bool:
        busy_seats.append(seat)
        return seat == seats[0]

    report = _cow_dedupe(project, seat_busy=report_busy)
    assert busy_seats.count(seats[0]) == 1
    assert busy_seats.count(seats[1]) == 1
    assert len(busy_seats) == len(seats)
    assert {path: (path.stat().st_ino, path.read_bytes()) for path in files} == before


def test_seat_dedupe_skips_a_seat_when_lsof_fails(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    before = (seats[0] / "shared.txt").stat().st_ino
    def fail_lsof(_seat, _timeout, *, deadline=None):
        raise OSError("lsof unavailable")
    _cow_dedupe(project, seat_busy=fail_lsof)
    assert (seats[0] / "shared.txt").stat().st_ino == before


def test_seat_dedupe_processes_seats_when_lsof_reports_no_own_uid_process(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    import goalflight_subprocess

    original_run = goalflight_subprocess.run_with_deadline
    lsof_calls = []

    def no_process(args, **kwargs):
        if args[0] == "lsof":
            lsof_calls.append(args)
            return subprocess.CompletedProcess(args, 1, b"", b"")
        return original_run(args, **kwargs)

    monkeypatch.setattr(goalflight_subprocess, "run_with_deadline", no_process)
    original_gate = seat_dedupe._seat_has_open_handles
    report = _cow_dedupe(
        project,
        seat_busy=lambda seat, timeout, *, deadline=None: original_gate(
            seat, timeout, deadline=deadline,
        ),
    )

    assert report["files_cloned"] > 0
    assert len(lsof_calls) == len(seats)
    assert all(
        args[1:4] == ["-a", "-u", str(os.getuid())]
        and args[-1] == "-Fp0"
        for args in lsof_calls
    )


def test_seat_dedupe_rechecks_idle_gate_under_replacement_locks(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    target = seats[-1]
    destination = target / "shared.txt"
    before = (destination.stat().st_ino, destination.read_bytes())
    import goalflight_worktree_pool

    original_liveness = seat_dedupe._seat_has_live_worker
    liveness_checks = []
    target_idle_checks = 0

    def record_liveness(*args, **kwargs):
        liveness_checks.append(args[0])
        return original_liveness(*args, **kwargs)

    def process_appeared(seat, _timeout, *, deadline=None):
        nonlocal target_idle_checks
        if seat != target:
            return False
        target_idle_checks += 1
        locks, error = seat_dedupe._try_hold_seat_locks(
            project,
            target,
            goalflight_worktree_pool,
            deadline=time.monotonic() + 5,
        )
        if locks is not None:
            for descriptor in reversed(locks):
                os.close(descriptor)
        assert locks is None, error
        return True

    monkeypatch.setattr(seat_dedupe, "_seat_has_live_worker", record_liveness)
    report = _cow_dedupe(project, seat_busy=process_appeared)

    assert target_idle_checks == 1
    assert all(liveness_checks.count(seat) == 2 for seat in seats)
    assert (destination.stat().st_ino, destination.read_bytes()) == before
    assert report["files_cloned"] > 0


def test_seat_dedupe_precomputes_cwd_matches_and_scans_each_seat_once(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    _cow_git("init", "--quiet", "-b", "main", cwd=project)
    _cow_git("config", "user.name", "Goal Flight Test", cwd=project)
    _cow_git("config", "user.email", "test@example.invalid", cwd=project)
    for index in range(24):
        (project / f"shared-{index:02}.txt").write_bytes(f"shared-{index}\n".encode())
    _cow_git("add", "-A", cwd=project)
    _cow_git("commit", "--quiet", "-m", "shared files", cwd=project)
    commit = _cow_git("rev-parse", "HEAD", cwd=project)
    seats = _cow_pool_seats(project, [commit, commit])

    class CountingRecords(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    records = CountingRecords(
        {
            "dispatch_id": f"outside-{index}",
            "worker_cwd": str(tmp_path / "outside" / str(index)),
        }
        for index in range(96)
    )
    original_liveness = seat_dedupe._seat_has_live_worker
    checked_records = {}
    liveness_calls = {seat: 0 for seat in seats}

    def count_liveness(seat, seat_records, *args, **kwargs):
        if seat in liveness_calls:
            liveness_calls[seat] += 1
            previous = checked_records.setdefault(seat, seat_records)
            assert previous is seat_records
        return original_liveness(seat, seat_records, *args, **kwargs)

    monkeypatch.setattr(seat_dedupe, "_seat_has_live_worker", count_liveness)
    recursive_scans = []
    report = _cow_dedupe(
        project,
        records=records,
        seat_busy=lambda seat, _timeout, *, deadline=None: recursive_scans.append(seat) or False,
    )

    assert report["files_cloned"] == 24 * len(seats)
    assert sorted(recursive_scans, key=os.fspath) == sorted(seats, key=os.fspath)
    assert liveness_calls == {seat: 2 for seat in seats}
    assert all(not checked_records[seat] for seat in seats)
    assert records.iterations == 1


def test_seat_dedupe_skips_missing_occupancy_lock_without_git_writes(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    import goalflight_worktree_pool

    seat = seats[-1]
    occupancy_lock = goalflight_worktree_pool.worktree_path_lock_path(seat)
    occupancy_lock.unlink()
    git_dir = project / ".git"

    def git_dir_state():
        state = {}
        for directory, dirs, files in os.walk(git_dir):
            for name in [*dirs, *files]:
                path = Path(directory) / name
                info = path.lstat()
                content = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
                state[str(path.relative_to(git_dir))] = (
                    info.st_mode, info.st_size, info.st_mtime_ns, content,
                )
        return state

    before_git_dir = git_dir_state()
    destination = seat / "shared.txt"
    before_destination = (destination.stat().st_ino, destination.read_bytes())

    _cow_dedupe(project)

    assert not occupancy_lock.exists()
    assert git_dir_state() == before_git_dir
    assert (destination.stat().st_ino, destination.read_bytes()) == before_destination


def test_seat_idle_gate_parses_only_lsof_process_ids(tmp_path: Path, monkeypatch) -> None:
    seat = tmp_path / "fixture-seat"
    seat.mkdir()
    calls = []

    def listed(args, **kwargs):
        calls.append((args, kwargs["timeout"]))
        return subprocess.CompletedProcess(args, 0, b"p123\0", b"")

    monkeypatch.setattr(seat_dedupe.subprocess, "run", listed)
    assert seat_dedupe._seat_has_open_handles(seat, 2.5)
    assert calls == [(
        ["lsof", "-a", "-u", str(os.getuid()), "+D", str(seat), "-Fp0"], 2.5,
    )]


@pytest.mark.parametrize(
    "result",
    [
        # Real `lsof -Fp0` output for a seat that is some process's cwd.
        subprocess.CompletedProcess(["lsof"], 0, b"p34936\0\nfcwd\0\np54210\0\nfcwd\0\n", b""),
        subprocess.CompletedProcess(["lsof"], 0, b"filename-without-pid\0", b""),
        subprocess.CompletedProcess(["lsof"], 2, b"p123\0", b"permission denied"),
        subprocess.CompletedProcess(["lsof"], 1, b"p123\0", b""),
        subprocess.CompletedProcess(["lsof"], 0, b"p123\0", b"warning"),
        subprocess.CompletedProcess(["lsof"], 0, b"", b""),
    ],
)
def test_seat_idle_gate_treats_any_reported_handle_as_busy(
    tmp_path: Path, monkeypatch, result
) -> None:
    seat = tmp_path / "fixture-seat"
    seat.mkdir()
    monkeypatch.setattr(seat_dedupe.subprocess, "run", lambda *args, **kwargs: result)
    assert seat_dedupe._seat_has_open_handles(seat, 2) is True


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess(["lsof"], 3, b"", b""),
        subprocess.CompletedProcess(["lsof"], 1, b"", b"lsof: bad option"),
    ],
)
def test_seat_idle_gate_raises_on_failed_lsof_without_output(
    tmp_path: Path, monkeypatch, result
) -> None:
    seat = tmp_path / "fixture-seat"
    seat.mkdir()
    monkeypatch.setattr(seat_dedupe.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(OSError):
        seat_dedupe._seat_has_open_handles(seat, 2)


def test_seat_idle_gate_accepts_empty_lsof_exit_one(tmp_path: Path, monkeypatch) -> None:
    seat = tmp_path / "fixture-seat"
    seat.mkdir()
    result = subprocess.CompletedProcess(["lsof"], 1, b"", b"")
    monkeypatch.setattr(seat_dedupe.subprocess, "run", lambda *args, **kwargs: result)
    assert not seat_dedupe._seat_has_open_handles(seat, 2)


def test_seat_idle_gate_rejects_lsof_timeout(tmp_path: Path, monkeypatch) -> None:
    seat = tmp_path / "fixture-seat"
    seat.mkdir()

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("lsof", 2)

    monkeypatch.setattr(seat_dedupe.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        seat_dedupe._seat_has_open_handles(seat, 2)


def _cow_xattr(action: str, path: Path, value: str = "keep") -> bytes:
    args = ["xattr", "-w", "user.probe", value, str(path)] if action == "set" else [
        "xattr", "-p", "user.probe", str(path)
    ] if action == "get" else ["xattr", "-d", "user.probe", str(path)]
    result = subprocess.run(args, check=True, capture_output=True)
    return result.stdout.strip()


def _cow_acl(add: bool, path: Path) -> None:
    args = ["chmod", "+a" if add else "-a", "everyone deny write", str(path)]
    subprocess.run(args, check=True, capture_output=True)


def _cow_forbid_replace(monkeypatch, path: Path) -> None:
    local_os = SimpleNamespace(**vars(os))
    replace = local_os.replace

    def guarded(source, destination):
        assert destination != path
        return replace(source, destination)

    local_os.replace = guarded
    monkeypatch.setattr(seat_dedupe, "os", local_os)


@pytest.mark.parametrize(
    ("destination_value", "expected_clones"), [("same", 1), ("different", 0)]
)
def test_seat_dedupe_compares_extended_attributes(
    tmp_path: Path, monkeypatch, destination_value: str, expected_clones: int,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    source_commit, target_commit = _cow_empty_base(project)
    source, target = _cow_pool_seats(project, [source_commit, target_commit])
    source_file = source / "source.txt"
    destination = target / "target.txt"
    _cow_xattr("set", source_file, "same")
    _cow_xattr("set", destination, destination_value)
    source_xattrs = seat_dedupe._extended_attributes(source_file)
    destination_xattrs = seat_dedupe._extended_attributes(destination)
    assert source_xattrs is not None and destination_xattrs is not None
    before_inode = destination.stat().st_ino

    report = _cow_dedupe(project)

    assert report["files_cloned"] == expected_clones
    after_xattrs = seat_dedupe._extended_attributes(destination)
    assert after_xattrs is not None
    if expected_clones:
        assert source_xattrs == destination_xattrs
        assert after_xattrs == destination_xattrs
        assert _cow_xattr("get", destination) == b"same"
        assert seat_dedupe._private_bytes(destination) == 0
    else:
        assert destination.stat().st_ino == before_inode
        assert after_xattrs == destination_xattrs
        assert _cow_xattr("get", destination) == destination_value.encode()


def test_seat_dedupe_ignores_only_provenance_xattr(
    tmp_path: Path, monkeypatch,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    source_commit, target_commit = _cow_empty_base(project)
    source, target = _cow_pool_seats(project, [source_commit, target_commit])
    source_file = source / "source.txt"
    destination = target / "target.txt"
    before_inode = destination.stat().st_ino

    def xattrs(path: Path) -> dict[bytes, bytes]:
        if path == source_file:
            provenance = b"source-process"
        elif path == destination:
            provenance = b"destination-process"
        else:
            assert path.suffix == ".clone"
            provenance = b"sweep-process"
        return {
            b"com.apple.provenance": provenance,
            b"user.fixture": b"same-content-metadata",
        }

    monkeypatch.setattr(seat_dedupe, "_extended_attributes", xattrs)

    report = _cow_dedupe(project)

    assert report["files_cloned"] == 1
    assert report["skip_counts"]["files"]["xattr_differ"] == 0
    assert destination.stat().st_ino != before_inode
    assert seat_dedupe._private_bytes(destination) == 0


def test_seat_dedupe_still_rejects_other_xattr_differences(
    tmp_path: Path, monkeypatch,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    source_commit, target_commit = _cow_empty_base(project)
    source, target = _cow_pool_seats(project, [source_commit, target_commit])
    source_file = source / "source.txt"
    destination = target / "target.txt"
    before_inode = destination.stat().st_ino
    source_xattrs = {
        b"com.apple.provenance": b"source-process",
        b"com.apple.lastuseddate#PS": b"source-value",
    }
    destination_xattrs = {
        b"com.apple.provenance": b"destination-process",
        b"com.apple.lastuseddate#PS": b"destination-value",
    }

    def xattrs(path: Path) -> dict[bytes, bytes]:
        return source_xattrs if path == source_file else destination_xattrs

    monkeypatch.setattr(seat_dedupe, "_extended_attributes", xattrs)

    report = _cow_dedupe(project)

    assert not seat_dedupe._xattrs_match(source_xattrs, destination_xattrs)
    assert report["files_cloned"] == 0
    assert report["skip_counts"]["files"]["xattr_differ"] == 1
    assert destination.stat().st_ino == before_inode


def test_seat_dedupe_keeps_destination_when_donor_xattrs_change_after_precheck(
    tmp_path: Path, monkeypatch,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    source_commit, target_commit = _cow_empty_base(project)
    source, target = _cow_pool_seats(project, [source_commit, target_commit])
    source_file = source / "source.txt"
    destination = target / "target.txt"
    _cow_xattr("set", source_file, "same")
    _cow_xattr("set", destination, "same")
    destination_xattrs = seat_dedupe._extended_attributes(destination)
    assert destination_xattrs is not None
    before_inode = destination.stat().st_ino
    clone_apfs_file = seat_dedupe._clone_apfs_file

    def change_donor_xattrs_then_clone(source_path: Path, staged_path: Path) -> None:
        assert source_path == source_file
        _cow_xattr("set", source_path, "changed")
        clone_apfs_file(source_path, staged_path)

    monkeypatch.setattr(seat_dedupe, "_clone_apfs_file", change_donor_xattrs_then_clone)

    report = _cow_dedupe(project)

    assert report["files_cloned"] == 0
    assert destination.stat().st_ino == before_inode
    assert seat_dedupe._extended_attributes(destination) == destination_xattrs
    assert _cow_xattr("get", destination) == b"same"


def test_seat_dedupe_skips_hardlinked_files(tmp_path: Path, monkeypatch) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    hardlink_file = seats[1] / "shared.txt"
    alias = tmp_path / "hardlink-alias"
    os.link(hardlink_file, alias)
    hardlink_inode = hardlink_file.stat().st_ino

    _cow_dedupe(project)

    assert hardlink_file.stat().st_ino == hardlink_inode
    assert os.stat(alias).st_ino == hardlink_inode
    alias.unlink()


def test_seat_dedupe_skips_files_with_an_acl(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    for checkout in [project, *seats]:
        _age_cow_checkout(checkout)
    target = seats[0] / "shared.txt"
    inode = target.stat().st_ino
    try:
        _cow_acl(True, target)
    except subprocess.CalledProcessError:
        pytest.skip("setting a fixture ACL is unavailable")
    try:
        if hasattr(seat_dedupe, "_has_acl"):
            assert seat_dedupe._has_acl(target)
        _cow_forbid_replace(monkeypatch, target)
        _cow_dedupe(project, age_files=False)
        assert target.stat().st_ino == inode
        assert _cow_git("status", "--porcelain", cwd=seats[0]) == ""
    finally:
        _cow_acl(False, target)


def test_seat_dedupe_skips_immutable_files(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    immutable_file = seats[0] / "shared.txt"
    immutable_flag = getattr(stat, "UF_IMMUTABLE", 0x2)
    try:
        os.chflags(immutable_file, immutable_flag)
    except OSError:
        pytest.skip("setting BSD immutable flags is unavailable")
    immutable_inode = immutable_file.stat().st_ino
    try:
        _cow_forbid_replace(monkeypatch, immutable_file)
        _cow_dedupe(project)
        assert immutable_file.stat().st_ino == immutable_inode
        assert all(_cow_git("status", "--porcelain", cwd=seat) == "" for seat in seats)
    finally:
        os.chflags(immutable_file, 0)


@pytest.mark.parametrize("killpoint", ["before", "during", "after"])
def test_seat_dedupe_sigkill_keeps_status_and_next_pass_cleans_scratch(
    tmp_path: Path, monkeypatch, killpoint: str
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    for checkout in [project, *seats]:
        _age_cow_checkout(checkout)
    before = {
        seat: _cow_git("status", "--porcelain", cwd=seat) for seat in seats
    }
    code = r'''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import goalflight_seat_dedupe as d
point = sys.argv[3]
clone = d._clone_apfs_file
replace = os.replace
if point == "before":
    def kill_after_clone(source, temporary):
        clone(source, temporary)
        os.kill(os.getpid(), signal.SIGKILL)
    d._clone_apfs_file = kill_after_clone
elif point == "during":
    def kill_at_replace(source, destination):
        os.kill(os.getpid(), signal.SIGKILL)
    os.replace = kill_at_replace
else:
    def kill_after_replace(source, destination):
        replace(source, destination)
        os.kill(os.getpid(), signal.SIGKILL)
    os.replace = kill_after_replace
def inactive(record, probe):
    return True, "fixture"
d.dedupe_pool(Path(sys.argv[1]), records=[], identity_probe=lambda r, _timeout: (True, "fixture"), identity_inactive=inactive)
'''
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(project), str(ROOT / "scripts"), killpoint],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert process.wait(timeout=30) == -signal.SIGKILL
    assert {
        seat: _cow_git("status", "--porcelain", cwd=seat) for seat in seats
    } == before
    scratch = project / "worktrees" / ".goalflight-dedupe-tmp"
    assert scratch.is_dir()
    stale_passes = list(scratch.iterdir())
    assert len(stale_passes) == 1
    assert stale_passes[0].name.startswith(f"{process.pid}-")
    _cow_dedupe(project)
    assert list(scratch.iterdir()) == []
    assert {
        seat: _cow_git("status", "--porcelain", cwd=seat) for seat in seats
    } == before


def test_seat_dedupe_checks_lsof_once_under_each_processing_lock(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    import goalflight_worktree_pool

    assert len(seats) >= 2
    checked = []

    def inspect_under_lock(seat, _timeout, *, deadline=None):
        checked.append(seat)
        locks, error = seat_dedupe._try_hold_seat_locks(
            project, seat, goalflight_worktree_pool,
            deadline=time.monotonic() + 5,
        )
        assert locks is None, error
        return False

    monkeypatch.setattr(
        seat_dedupe, "_seat_has_open_handles", inspect_under_lock, raising=False,
    )
    _cow_dedupe(project, seat_busy=inspect_under_lock)
    assert set(checked) == set(seats)
    assert len(checked) == len(seats)
    for seat in seats:
        locks, error = seat_dedupe._try_hold_seat_locks(
            project, seat, goalflight_worktree_pool,
            deadline=time.monotonic() + 30,
        )
        assert locks is not None, error
        for descriptor in reversed(locks):
            os.close(descriptor)


def test_seat_dedupe_processes_a_finished_inspection_while_another_is_slow(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    slow_seat, fast_seat = seats[0], seats[-1]
    slow_inspection_started = threading.Event()
    release_slow_inspection = threading.Event()
    fast_seat_replacement_started = threading.Event()
    clone = seat_dedupe._clone_if_unchanged
    original_liveness = seat_dedupe._seat_has_live_worker
    liveness_calls = {seat: 0 for seat in seats}

    def inspect_liveness(seat: Path, *args, **kwargs):
        first_call = liveness_calls[seat] == 0
        liveness_calls[seat] += 1
        if seat == slow_seat and first_call:
            slow_inspection_started.set()
            if not release_slow_inspection.wait(timeout=10):
                raise TimeoutError("slow seat inspection was not released")
        elif seat == fast_seat and first_call:
            assert slow_inspection_started.wait(timeout=5)
        return original_liveness(seat, *args, **kwargs)

    def record_fast_replacement(*args, **kwargs):
        if kwargs["destination_root"] == fast_seat:
            fast_seat_replacement_started.set()
        return clone(*args, **kwargs)

    monkeypatch.setattr(seat_dedupe, "_clone_if_unchanged", record_fast_replacement)
    monkeypatch.setattr(seat_dedupe, "_seat_has_live_worker", inspect_liveness)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as caller:
        run = caller.submit(_cow_dedupe, project)
        try:
            assert fast_seat_replacement_started.wait(timeout=5)
            assert not release_slow_inspection.is_set()
        finally:
            release_slow_inspection.set()
        report = run.result(timeout=30)

    assert report["files_cloned"] >= 1


def test_seat_dedupe_releases_queued_inspection_locks(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    import goalflight_worktree_pool

    indexed: set[Path] = set()
    index_guard = threading.Lock()
    all_indexed = threading.Event()
    index_files = seat_dedupe._git_index_files
    clone = seat_dedupe._clone_if_unchanged
    queued_lock_was_free = []

    def record_index(checkout: Path, **kwargs):
        files = index_files(checkout, **kwargs)
        if checkout in seats:
            with index_guard:
                indexed.add(checkout)
                if len(indexed) == len(seats):
                    all_indexed.set()
        return files

    def check_queued_lock(*args, **kwargs):
        current = kwargs["destination_root"]
        queued = next(seat for seat in seats if seat != current)
        if all_indexed.wait(timeout=5):
            end = time.monotonic() + 2
            while time.monotonic() < end:
                locks, _error = seat_dedupe._try_hold_seat_locks(
                    project, queued, goalflight_worktree_pool, deadline=end,
                )
                if locks is not None:
                    for descriptor in reversed(locks):
                        os.close(descriptor)
                    queued_lock_was_free.append(True)
                    break
                time.sleep(0.01)
        return clone(*args, **kwargs)

    monkeypatch.setattr(seat_dedupe, "_git_index_files", record_index)
    monkeypatch.setattr(seat_dedupe, "_clone_if_unchanged", check_queued_lock)
    report = _cow_dedupe(project)

    assert report["files_cloned"] >= 1
    assert queued_lock_was_free


def test_seat_dedupe_obeys_its_per_pass_time_budget(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    monkeypatch.setattr(
        seat_dedupe, "_PASS_BUDGET_SECONDS", 0.000001, raising=False
    )
    open_calls = []
    monkeypatch.setattr(
        seat_dedupe, "_seat_has_open_handles", lambda seat, _timeout, *, deadline=None: open_calls.append(seat) or False,
        raising=False,
    )
    report = _cow_dedupe(project)
    assert report["files_cloned"] == 0
    assert open_calls == []
    assert all((seat / "shared.txt").stat().st_nlink == 1 for seat in seats)


def test_seat_dedupe_shares_before_budget_expires_after_two_seats(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    source_commit, target_commit = _cow_empty_base(project)
    source, target, later = _cow_pool_seats(
        project, [source_commit, target_commit, target_commit]
    )
    clock = [100.0]

    with monkeypatch.context() as budget_patch:
        budget_patch.setattr(seat_dedupe.time, "monotonic", lambda: clock[0])
        budget_patch.setattr(seat_dedupe, "_PASS_BUDGET_SECONDS", 10)
        clone = seat_dedupe._clone_if_unchanged

        def clone_then_expire(*args, **kwargs):
            shared = clone(*args, **kwargs)
            if shared:
                clock[0] = kwargs["deadline"] + 1
            return shared

        budget_patch.setattr(seat_dedupe, "_clone_if_unchanged", clone_then_expire)
        first = _cow_dedupe(project)

    assert first["seats_scanned"] == 2
    assert first["files_cloned"] == 1
    target_private = seat_dedupe._private_bytes(target / "target.txt")
    later_private = seat_dedupe._private_bytes(later / "target.txt")
    assert (target_private == 0) != (later_private == 0)

    second = _cow_dedupe(project)
    assert second["files_cloned"] == 1
    assert seat_dedupe._private_bytes(target / "target.txt") == 0
    assert seat_dedupe._private_bytes(later / "target.txt") == 0


def test_seat_dedupe_caps_each_seat_and_releases_locks(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    source_commit, target_commit = _cow_empty_base(project)
    source, capped, later = _cow_pool_seats(
        project, [source_commit, target_commit, target_commit]
    )
    for seat in (capped, later):
        (seat / "target2.txt").write_bytes(b"shared across seats\n")
        _cow_git("add", "target2.txt", cwd=seat)

    clock = threading.local()
    clock.now = 100.0
    budget = 5
    original_clone = seat_dedupe._clone_if_unchanged
    expired = False
    checked = []

    with monkeypatch.context() as budget_patch:
        budget_patch.setattr(
            seat_dedupe.time, "monotonic", lambda: getattr(clock, "now", 100.0)
        )
        budget_patch.setattr(seat_dedupe, "_PASS_BUDGET_SECONDS", 1000)
        budget_patch.setattr(seat_dedupe, "_SEAT_BUDGET_SECONDS", budget)

        def clone_then_expire(*args, **kwargs):
            nonlocal expired
            shared = original_clone(*args, **kwargs)
            if kwargs["destination_root"] == capped and shared and not expired:
                expired = True
                clock.now = kwargs["deadline"] + 0.1
            return shared

        def check_previous_lock(
            seat: Path, _timeout: float, *, deadline: float | None = None,
        ) -> bool:
            checked.append(seat)
            return False

        budget_patch.setattr(
            seat_dedupe, "_clone_if_unchanged", clone_then_expire
        )
        budget_patch.setattr(
            seat_dedupe, "_seat_has_open_handles", check_previous_lock
        )
        report = _cow_dedupe(project, seat_busy=check_previous_lock)

    assert all(seat in checked for seat in (source, capped, later))
    assert report["skip_counts"]["seats"]["seat_budget"] == 1
    assert report["files_cloned"] == 3
    assert seat_dedupe._private_bytes(capped / "target.txt") == 0
    assert seat_dedupe._private_bytes(capped / "target2.txt") > 0
    assert seat_dedupe._private_bytes(later / "target.txt") == 0
    assert seat_dedupe._private_bytes(later / "target2.txt") == 0


def test_seat_dedupe_uses_one_fresh_seat_lsof_check_before_processing(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    target = seats[0]
    target_checks = 0

    def become_busy(
        seat: Path, _timeout: float, *, deadline: float | None = None,
    ) -> bool:
        nonlocal target_checks
        if seat == target:
            target_checks += 1
            return True
        return False

    report = _cow_dedupe(project, seat_busy=become_busy)

    assert target_checks == 1
    assert report["skip_counts"]["seats"]["lsof_busy"] >= 1
    assert seat_dedupe._private_bytes(target / "shared.txt") > 0


def test_seat_dedupe_releases_unprocessed_locks_at_pass_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    clock = [100.0]
    simultaneous_inspections = threading.Barrier(len(seats))
    inspected = set()
    inspection_guard = threading.Lock()
    index_files = seat_dedupe._git_index_files
    clone = seat_dedupe._clone_if_unchanged
    expired = False

    def simultaneous_index(checkout: Path, **kwargs):
        if checkout not in seats:
            return index_files(checkout, **kwargs)
        with inspection_guard:
            first_check = checkout not in inspected
            inspected.add(checkout)
        if first_check:
            simultaneous_inspections.wait(timeout=5)
        return index_files(checkout, **kwargs)

    def clone_then_expire(*args, **kwargs):
        nonlocal expired
        shared = clone(*args, **kwargs)
        if shared and not expired:
            expired = True
            clock[0] = 111.0
        return shared

    with monkeypatch.context() as budget_patch:
        budget_patch.setattr(seat_dedupe.time, "monotonic", lambda: clock[0])
        budget_patch.setattr(seat_dedupe, "_PASS_BUDGET_SECONDS", 10)
        budget_patch.setattr(seat_dedupe, "_SEAT_BUDGET_SECONDS", 5)
        budget_patch.setattr(seat_dedupe, "_git_index_files", simultaneous_index)
        budget_patch.setattr(seat_dedupe, "_clone_if_unchanged", clone_then_expire)
        report = _cow_dedupe(project)

    assert report["files_cloned"] == 1
    import goalflight_worktree_pool

    for seat in seats:
        locks, error = seat_dedupe._try_hold_seat_locks(
            project, seat, goalflight_worktree_pool,
            deadline=time.monotonic() + 30,
        )
        assert locks is not None, error
        for descriptor in reversed(locks):
            os.close(descriptor)


def test_seat_dedupe_does_not_count_budget_after_finishing_files(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)

    report = _cow_dedupe(project)

    assert report["files_cloned"] == len(seats)
    assert report["skip_counts"]["seats"]["seat_budget"] == 0


def test_stalled_ps_identity_probe_respects_the_remaining_pass_budget(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    import goalflight_ledger

    budget = 5.0
    monkeypatch.setattr(seat_dedupe, "_PASS_BUDGET_SECONDS", budget)
    monkeypatch.setattr(goalflight_ledger.goalflight_compat, "pid_liveness", lambda _pid: True)
    monkeypatch.setattr(
        goalflight_ledger.goalflight_compat,
        "process_start_identity",
        lambda pid: {"pid": pid, "start_token": "fixture"},
    )
    timeouts = []

    def stalled_ps(args, **kwargs):
        timeout = kwargs["timeout"]
        timeouts.append(timeout)
        time.sleep(timeout)
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(
        goalflight_ledger.goalflight_subprocess, "run_with_deadline", stalled_ps,
    )
    record = {
        "worker_pid": 424242,
        "worker_identity": {"pid": 424242, "start_token": "fixture"},
        "worker_cwd": str(seats[0]),
    }
    destination = seats[0] / "shared.txt"
    before_destination = (destination.stat().st_ino, destination.read_bytes())

    started = time.monotonic()
    deadline = started + budget
    seat_dedupe._dedupe_pool(
        project,
        records=[record],
        identity_probe=goalflight_ledger.identity_matches,
        identity_inactive=reaper._identity_inactive,
        deadline=deadline,
        identity_snapshot_probe=lambda rows, remaining: reaper._identity_snapshot_probe(
            rows, goalflight_ledger.identity_matches, deadline=remaining,
        ),
    )
    elapsed = time.monotonic() - started

    assert timeouts and all(0 < timeout <= budget for timeout in timeouts)
    assert elapsed <= budget + 0.25
    assert (destination.stat().st_ino, destination.read_bytes()) == before_destination


def test_slow_content_comparison_aborts_before_replacing_destination(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source.write_bytes(b"same bytes\n")
    destination.write_bytes(b"same bytes\n")
    before = (destination.stat().st_ino, destination.read_bytes())

    monkeypatch.setattr(
        seat_dedupe,
        "_file_info",
        lambda path, **kwargs: (
            path.lstat(),
            1 if kwargs["needs_private_bytes"] and path == destination else 0,
        ),
    )
    monkeypatch.setattr(seat_dedupe, "_extended_attributes", lambda _path: {})
    monkeypatch.setattr(
        seat_dedupe,
        "_clone_apfs_file",
        lambda first, second: shutil.copyfile(first, second),
    )
    original_open = Path.open
    read_calls = []
    elapsed_clock = [0.0]
    monkeypatch.setattr(seat_dedupe.time, "monotonic", lambda: elapsed_clock[0])

    class SlowReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size):
            read_calls.append(size)
            elapsed_clock[0] += 0.08
            return self.stream.read(size)

    def slow_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return SlowReader(stream) if path.parent == scratch else stream

    monkeypatch.setattr(Path, "open", slow_open)
    shared = seat_dedupe._clone_if_unchanged(
        source,
        destination,
        scratch,
        source_root=tmp_path,
        source_index_path=b"source",
        destination_root=tmp_path,
        destination_index_path=b"destination",
        device=destination.stat().st_dev,
        now_ns=time.time_ns(),
        deadline=0.04,
    )

    assert shared == 0
    assert read_calls == [1024 * 1024]
    assert elapsed_clock[0] <= 0.04 + 0.08
    assert (destination.stat().st_ino, destination.read_bytes()) == before


def test_slow_final_xattr_reads_abort_before_replacing_destination(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source.write_bytes(b"same bytes\n")
    destination.write_bytes(b"same bytes\n")
    before = (destination.stat().st_ino, destination.read_bytes())
    monkeypatch.setattr(
        seat_dedupe,
        "_file_info",
        lambda path, **kwargs: (
            path.lstat(),
            1 if kwargs["needs_private_bytes"] and path == destination else 0,
        ),
    )
    xattr_reads = []
    elapsed_clock = [0.0]

    def slow_xattr_read(_path: Path) -> dict[bytes, bytes]:
        xattr_reads.append(_path)
        if len(xattr_reads) > 2:
            elapsed_clock[0] += 0.03
        return {}

    monkeypatch.setattr(seat_dedupe, "_extended_attributes", slow_xattr_read)
    monkeypatch.setattr(
        seat_dedupe,
        "_clone_apfs_file",
        lambda first, second: shutil.copyfile(first, second),
    )
    monkeypatch.setattr(seat_dedupe.time, "monotonic", lambda: elapsed_clock[0])

    shared = seat_dedupe._clone_if_unchanged(
        source,
        destination,
        scratch,
        source_root=tmp_path,
        source_index_path=b"source",
        destination_root=tmp_path,
        destination_index_path=b"destination",
        device=destination.stat().st_dev,
        now_ns=time.time_ns(),
        deadline=0.04,
    )

    assert shared == 0
    assert len(xattr_reads) == 4
    assert elapsed_clock[0] >= 0.04
    assert (destination.stat().st_ino, destination.read_bytes()) == before


def test_seat_dedupe_skips_recently_modified_files(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    destination = seats[-1] / "shared.txt"
    inode = destination.stat().st_ino
    report = _cow_dedupe(project, age_files=False)
    assert report["files_cloned"] == 0
    assert destination.stat().st_ino == inode


def test_seat_dedupe_uses_only_read_only_git_with_optional_locks_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    project, seats = _simple_cow_pool(tmp_path, monkeypatch)
    git_dir = project / ".git"
    filter_marker = git_dir / "lfs-clean-ran"
    clean_filter = shlex.join([
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran'); sys.stdout.buffer.write(sys.stdin.buffer.read())",
        str(filter_marker),
    ])
    _cow_git("config", "filter.lfs.clean", clean_filter, cwd=project)
    _cow_git("config", "filter.lfs.smudge", "cat", cwd=project)
    _cow_git("config", "filter.lfs.required", "true", cwd=project)
    (project / ".gitattributes").write_text("payload.lfs filter=lfs\n", encoding="utf-8")
    _cow_git("add", ".gitattributes", cwd=project)
    _cow_git("commit", "--quiet", "-m", "filter attributes", cwd=project)
    (project / "payload.lfs").write_bytes(b"large object payload\n")
    _cow_git("add", "payload.lfs", cwd=project)
    assert filter_marker.exists()
    filter_marker.unlink()
    _cow_git("commit", "--quiet", "-m", "filtered payload", cwd=project)
    commit = _cow_git("rev-parse", "HEAD", cwd=project)
    for seat in seats:
        _cow_git("checkout", "--quiet", "--detach", commit, cwd=seat)
    for checkout in [project, *seats]:
        _age_cow_checkout(checkout)
    _cow_git("-c", "core.fsmonitor=false", "status", "--porcelain", cwd=seats[0])
    assert filter_marker.exists()
    filter_marker.unlink()
    _cow_git("config", "core.fsmonitor", "true", cwd=project)

    def git_dir_state() -> dict[str, tuple[int, int, int, bytes | None]]:
        state = {}
        for directory, dirs, files in os.walk(git_dir):
            for name in [*dirs, *files]:
                path = Path(directory) / name
                info = path.lstat()
                content = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
                state[str(path.relative_to(git_dir))] = (
                    info.st_mode, info.st_size, info.st_mtime_ns, content,
                )
        return state

    before_git_dir = git_dir_state()
    import goalflight_subprocess

    run = goalflight_subprocess.run_with_deadline
    git_commands = []

    def read_only_git(args, **kwargs):
        if args[0] == "git":
            env = kwargs.get("env") or os.environ
            git_commands.append((args, env.get("GIT_OPTIONAL_LOCKS")))
        return run(args, **kwargs)

    monkeypatch.setattr(goalflight_subprocess, "run_with_deadline", read_only_git)
    _cow_dedupe(project, age_files=False)
    assert git_commands
    assert all(optional_locks == "0" for _args, optional_locks in git_commands)
    assert all(
        any(args[index:index + 2] == ["-c", "core.fsmonitor=false"] for index in range(len(args) - 1))
        for args, _locks in git_commands
    )
    forbidden = {
        "add", "checkout", "reset", "update-index", "write-tree", "commit",
        "hash-object", "read-tree", "apply", "merge", "rebase", "clean", "status",
    }
    assert all(not forbidden.intersection(args) for args, _locks in git_commands)
    assert git_dir_state() == before_git_dir
    assert not filter_marker.exists()


def test_seat_dedupe_uses_one_deadline_across_pools(monkeypatch) -> None:
    roots = {Path("fixture-a"), Path("fixture-b")}
    deadlines = []
    monkeypatch.setattr(seat_dedupe, "_PASS_BUDGET_SECONDS", 900)
    monkeypatch.setattr(
        seat_dedupe,
        "_dedupe_pool",
        lambda root, **kwargs: deadlines.append(kwargs["deadline"]) or {
            "seats_scanned": 0,
            "files_cloned": 0,
            "bytes_shared": 0,
            "errors": [],
        },
    )

    report = seat_dedupe._dedupe_pools(
        roots,
        records=[],
        identity_probe=lambda _record: (True, "fixture"),
        identity_inactive=lambda _record, _probe: (True, "fixture"),
    )

    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert report == {
        "seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0, "errors": [],
        "skip_counts": seat_dedupe._new_skip_counts(),
    }


def test_terminal_no_cwd_record_without_pid_does_not_block_seat() -> None:
    result = seat_dedupe._seat_has_live_worker(
        Path("/seat"),
        [{"state": "complete"}],
        lambda _record, _deadline: (False, "no_pid"),
        reaper._identity_inactive,
        time.monotonic() + 5,
    )

    assert result == (False, None)


def test_nonterminal_no_cwd_record_without_pid_still_blocks_seat() -> None:
    result = seat_dedupe._seat_has_live_worker(
        Path("/seat"),
        [{"state": "running"}],
        lambda _record, _deadline: (False, "no_pid"),
        reaper._identity_inactive,
        time.monotonic() + 5,
    )

    assert result == (True, "unknown_liveness")


def test_terminal_no_cwd_record_with_indeterminate_pid_still_blocks_seat() -> None:
    result = seat_dedupe._seat_has_live_worker(
        Path("/seat"),
        [{"state": "complete", "worker_pid": 123}],
        lambda _record, _deadline: (False, "identity_indeterminate"),
        reaper._identity_inactive,
        time.monotonic() + 5,
    )

    assert result == (True, "unknown_liveness")


def test_scratch_cleanup_removes_only_dead_passes(tmp_path: Path, monkeypatch) -> None:
    scratch = tmp_path / "scratch"
    live_pid = os.getpid()
    dead_pass = scratch / "99999999-111"
    live_pass = scratch / f"{live_pid}-222"
    dead_pass.mkdir(parents=True)
    live_pass.mkdir()
    (dead_pass / "leftover.clone").write_bytes(b"stale")
    (live_pass / "active.clone").write_bytes(b"active")
    monkeypatch.setattr(seat_dedupe, "_pid_is_alive", lambda pid: pid == live_pid)

    assert seat_dedupe._clear_stale_scratch(scratch, time.monotonic() + 5)
    assert not dead_pass.exists()
    assert (live_pass / "active.clone").read_bytes() == b"active"


def test_seat_dedupe_skips_held_live_and_nonregular_or_dirty_files(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    import goalflight_worktree_pool

    project = tmp_path / "project"
    nested = tmp_path / "nested"
    nested.mkdir()
    _cow_git("init", "--quiet", "-b", "main", cwd=nested)
    _cow_git("config", "user.name", "Goal Flight Test", cwd=nested)
    _cow_git("config", "user.email", "test@example.invalid", cwd=nested)
    (nested / "nested.txt").write_bytes(b"submodule file\n")
    _cow_git("add", "nested.txt", cwd=nested)
    _cow_git("commit", "--quiet", "-m", "nested", cwd=nested)

    project.mkdir()
    _cow_git("init", "--quiet", "-b", "main", cwd=project)
    _cow_git("config", "user.name", "Goal Flight Test", cwd=project)
    _cow_git("config", "user.email", "test@example.invalid", cwd=project)
    (project / "clean.txt").write_bytes(b"clean copy\n")
    (project / "modified.txt").write_bytes(b"committed\n")
    (project / "link").symlink_to("clean.txt")
    _cow_git("add", "clean.txt", "modified.txt", "link", cwd=project)
    _cow_git(
        "-c", "protocol.file.allow=always", "submodule", "add", "--quiet",
        str(nested), "module", cwd=project,
    )
    _cow_git("commit", "--quiet", "-m", "fixture", cwd=project)
    commit = _cow_git("rev-parse", "HEAD", cwd=project)
    leases = [
        goalflight_worktree_pool.acquire_worktree_seat(project, f"cow-skips-{i}")
        for i in range(3)
    ]
    idle, live, held = [lease.path for lease in leases]
    try:
        for lease in leases:
            _cow_git("checkout", "--quiet", "--detach", commit, cwd=lease.path)
        for lease in leases[:2]:
            lease.release()
        _cow_git(
            "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--quiet",
            "module", cwd=idle,
        )
        modified = idle / "modified.txt"
        modified.write_bytes(b"local edit\n")
        untracked = idle / "untracked.txt"
        untracked.write_bytes(b"leave alone\n")
        nested_file = idle / "module" / "nested.txt"
        nested_before = (nested_file.stat().st_ino, nested_file.read_bytes())
        dirty_before = (modified.stat().st_ino, modified.read_bytes())
        untracked_before = (untracked.stat().st_ino, untracked.read_bytes())
        link_before = (os.readlink(idle / "link"), (idle / "link").lstat().st_ino)
        live_before = ((live / "clean.txt").stat().st_ino, (live / "clean.txt").read_bytes())
        held_before = ((held / "clean.txt").stat().st_ino, (held / "clean.txt").read_bytes())
        reference_before = ((project / "clean.txt").stat().st_ino, (project / "clean.txt").read_bytes())
        record = {"state": "running", "worker_cwd": str(live)}

        report = _cow_dedupe(
            project,
            records=[record],
            identity_probe=lambda _record: (True, "live"),
        )

        assert report["seats_scanned"] == 3
        assert report["files_cloned"] == 2
        assert report["bytes_shared"] == len(b"clean copy\n") + (project / ".gitmodules").stat().st_size
        assert (idle / "clean.txt").read_bytes() == (project / "clean.txt").read_bytes()
        assert (modified.stat().st_ino, modified.read_bytes()) == dirty_before
        assert (untracked.stat().st_ino, untracked.read_bytes()) == untracked_before
        assert (os.readlink(idle / "link"), (idle / "link").lstat().st_ino) == link_before
        assert (nested_file.stat().st_ino, nested_file.read_bytes()) == nested_before
        assert ((live / "clean.txt").stat().st_ino, (live / "clean.txt").read_bytes()) == live_before
        assert ((held / "clean.txt").stat().st_ino, (held / "clean.txt").read_bytes()) == held_before
        assert ((project / "clean.txt").stat().st_ino, (project / "clean.txt").read_bytes()) == reference_before
    finally:
        leases[2].release()


def test_seat_dedupe_skips_when_the_pool_allocation_lock_is_held(
    tmp_path: Path, monkeypatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("APFS clonefile is available on macOS")
    _fixture_tree(tmp_path, monkeypatch)
    import goalflight_worktree_pool

    project = tmp_path / "project"
    project.mkdir()
    _cow_git("init", "--quiet", "-b", "main", cwd=project)
    _cow_git("config", "user.name", "Goal Flight Test", cwd=project)
    _cow_git("config", "user.email", "test@example.invalid", cwd=project)
    (project / "shared.txt").write_bytes(b"shared\n")
    _cow_git("add", "shared.txt", cwd=project)
    _cow_git("commit", "--quiet", "-m", "base", cwd=project)
    lease = goalflight_worktree_pool.acquire_worktree_seat(project, "cow-held-seat")
    try:
        seat = lease.path
        before = ((seat / "shared.txt").stat().st_ino, (seat / "shared.txt").read_bytes())
        report = _cow_dedupe(project)
        assert report["seats_scanned"] == 1
        assert report["files_cloned"] == 0
        assert ((seat / "shared.txt").stat().st_ino, (seat / "shared.txt").read_bytes()) == before
    finally:
        lease.release()


def test_sweep_reports_read_only_and_gc_exceptions(tmp_path: Path, monkeypatch) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setattr(reaper, "_project_roots", lambda _records, _errors, **_kwargs: {project})

    import goalflight_worktree_gc
    import goalflight_worktree_pool

    def fail_pool(_root, *, deadline=None):
        raise OSError("pool unavailable")

    def fail_gc(_argv, **_kwargs):
        raise RuntimeError("gc unavailable")

    monkeypatch.setattr(goalflight_worktree_pool, "reap_read_only_worktrees", fail_pool)
    monkeypatch.setattr(goalflight_worktree_gc, "main", fail_gc)

    result = reaper._run_sweep_body(homes_dir=homes, ledger_dir=ledger, now=NOW, identity_probe=_dead)

    assert any("read-only pool: OSError: pool unavailable" in error for error in result["cleanup_errors"])
    assert any("worktree GC: RuntimeError: gc unavailable" in error for error in result["cleanup_errors"])

def test_sweep_reports_worktree_gc_failure_details(tmp_path: Path, monkeypatch) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setattr(reaper, "_project_roots", lambda _records, _errors, **_kwargs: {project})

    import goalflight_worktree_gc
    import goalflight_worktree_pool

    monkeypatch.setattr(
        goalflight_worktree_pool, "reap_read_only_worktrees",
        lambda _root, *, deadline=None: None,
    )
    monkeypatch.setattr(
        seat_dedupe,
        "_dedupe_pools",
        lambda *_args, **_kwargs: {
            "seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0, "errors": [],
        },
    )

    def fail_gc(_argv, **_kwargs):
        print(json.dumps({"entries": [{
            "path": str(project / "worktrees" / "seat"),
            "outcome": "failed",
            "error": "worktree is locked",
        }]}))
        return 1

    monkeypatch.setattr(goalflight_worktree_gc, "main", fail_gc)

    result = reaper._run_sweep_body(
        homes_dir=homes,
        ledger_dir=ledger,
        now=NOW,
        identity_probe=_dead,
    )

    assert result["cleanup_errors"] == [
        f"{project}: worktree GC: {project / 'worktrees' / 'seat'}: worktree is locked"
    ]


def test_sweep_gc_limits_removals_to_managed_worktrees(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()

    def git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)
    git(project, "config", "user.email", "test@example.invalid")
    git(project, "config", "user.name", "Test")
    (project / "base.txt").write_text("base\n", encoding="utf-8")
    git(project, "add", "base.txt")
    git(project, "commit", "-qm", "init")

    external = tmp_path / "operator-worktree"
    git(project, "worktree", "add", "-q", "-b", "operator-owned", str(external))
    (external / "merged.txt").write_text("merged\n", encoding="utf-8")
    git(external, "add", "merged.txt")
    git(external, "commit", "-qm", "merged work")
    git(project, "merge", "-q", "--ff-only", "operator-owned")

    import goalflight_worktree_gc
    import goalflight_worktree_pool

    monkeypatch.setenv(goalflight_worktree_pool.WORKTREE_SEATS_ENV, "1")
    lease = goalflight_worktree_pool.acquire_worktree_seat(project, "retention-gc-test")
    seat = lease.path
    lease.release()
    _mark_managed_seat_reclaimable(ledger, project, seat, "retention-gc-test")
    base = git(project, "rev-parse", "HEAD")
    readonly, _created = goalflight_worktree_pool.shared_read_only_worktree(
        project, base=base
    )

    monkeypatch.setattr(reaper, "_project_roots", lambda _records, _errors, **_kwargs: {project})
    monkeypatch.setattr(reaper, "_expire_trace_archives", lambda _root, **_kwargs: 0)
    monkeypatch.setattr(
        goalflight_worktree_pool, "reap_read_only_worktrees",
        lambda _root, *, deadline=None: None,
    )
    monkeypatch.setattr(
        seat_dedupe,
        "_dedupe_pools",
        lambda *_args, **_kwargs: {
            "seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0, "errors": [],
        },
    )

    assert goalflight_worktree_gc.main(
        [str(project), "--json"], goalflight_managed_only=True
    ) == 0
    report = json.loads(capsys.readouterr().out)
    external_entry = next(e for e in report["entries"] if Path(e["path"]) == external)
    seat_entry = next(e for e in report["entries"] if Path(e["path"]) == seat)
    readonly_entry = next(e for e in report["entries"] if Path(e["path"]) == readonly)
    assert external_entry["decision"] == "retain"
    assert external_entry["reason"] == "not goal-flight-managed"
    assert seat_entry["decision"] == "remove"
    assert readonly_entry["decision"] == "retain"
    assert readonly_entry["conditions"]["grace"]["verdict"] == "no"

    monkeypatch.setenv(goalflight_worktree_pool.WORKTREE_SEATS_ENV, "bogus")
    assert goalflight_worktree_gc.main(
        [str(project), "--json"], goalflight_managed_only=True
    ) == 0
    unknown_report = json.loads(capsys.readouterr().out)
    unknown_seat = next(e for e in unknown_report["entries"] if Path(e["path"]) == seat)
    assert unknown_seat["pool_seat"]["verdict"] == "unknown"
    assert unknown_seat["decision"] == "retain"
    assert unknown_seat["reason"] == "not goal-flight-managed"
    monkeypatch.setenv(goalflight_worktree_pool.WORKTREE_SEATS_ENV, "1")

    result = reaper._run_sweep_body(
        homes_dir=homes,
        ledger_dir=ledger,
        now=NOW,
        identity_probe=_dead,
    )

    assert external.is_dir()
    assert not seat.exists()
    assert readonly.is_dir()
    assert result["worktree_gc_removals"] == [str(seat)]


def test_sweep_logs_stage_timings(tmp_path: Path, monkeypatch, capsys) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    stages = (
        "dispatch_artifacts_scan", "legacy_scratch", "deletion", "traces",
        "read_only", "gc", "snapshots", "dedupe",
    )
    timings = {name: 1.25 for name in stages}
    timings.update(
        total=8.75,
        cpu={**{name: 0.25 for name in stages}, "total": 2.0},
        projects={"traces": "/project", "dedupe": "/dedupe-project"},
    )
    monkeypatch.setattr(
        reaper, "run_retention_sweep",
        lambda **_kwargs: {
            "ran": True,
            "artifacts": {"deleted_count": 0, "deleted_allocated_bytes": 0,
                          "total_allocated_bytes": 0, "scratch_deleted_count": 0,
                          "scratch_deleted_bytes": 0, "legacy_deleted_count": 0,
                          "legacy_deleted_bytes": 0, "legacy_kept_reasons": {},
                          "budget_met": False, "kept_reasons": {"open_file": 2},
                          "codex_home_maintenance": {
                              "kept_reasons": {"resume_lock_busy": 3},
                          },
                          "files_walked": 42},
            "trace_buckets": 0, "snapshots": 0, "worktree_gc": 0,
            "worktree_gc_removals": [], "read_only_pools": 0,
            "copy_on_write": {"seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0},
            "cleanup_errors": [], "timings": timings,
            "_measurements": {
                "second_ledger_read": {"calls": 1, "wall_seconds": 0.2},
                "project_root_discovery": {
                    "calls": 1, "wall_seconds": 0.3, "roots": 2,
                },
                "external_commands": {
                    name: {"calls": count, "wall_seconds": count / 10}
                    for name, count in (("ps", 3), ("lsof", 4), ("git", 5))
                },
                "children_cpu_seconds": 0.6,
            },
        },
    )

    assert reaper.main(["--delete", "--json"]) == 0

    captured = capsys.readouterr()
    logged = captured.err
    summary = next(line for line in logged.splitlines()
                   if line.startswith("retention sweep:"))
    assert "duration=8.8s" in summary
    assert "files_walked=42" in summary
    assert "budget_met=False" in summary
    assert 'budget_keep_reasons={"open_file": 2}' in summary
    assert 'codex_keep_reasons={"resume_lock_busy": 3}' in summary
    assert "phase_wall=" in summary and "phase_cpu=" in summary
    measurement_summary = next(
        line for line in logged.splitlines()
        if line.startswith("retention sweep measurements:")
    )
    assert "second_ledger_read=1/0.2s" in measurement_summary
    assert "project_root_discovery=1/0.3s roots=2" in measurement_summary
    assert "ps:3/0.3s,lsof:4/0.4s,git:5/0.5s" in measurement_summary
    assert "children_cpu=0.6s" in measurement_summary
    payload = json.loads(captured.out)
    assert "_measurements" not in payload
    assert payload["timings"]["dispatch_artifacts_scan"] == 1.25
    assert payload["timings"]["cpu"]["deletion"] == 0.25
    lines = [line for line in logged.splitlines()
             if line.startswith("retention sweep timing:")]
    assert len(lines) == 1
    assert all(f"{name}=1.2s/0.2s" in lines[0] for name in stages)
    assert "traces=1.2s/0.2s@/project" in lines[0]
    assert "dedupe=1.2s/0.2s@/dedupe-project" in lines[0]
    assert "total=8.8s" in lines[0]


def test_darwin_background_role_is_cleared_before_delete_walk(monkeypatch) -> None:
    events = []

    class FakeSetPriority:
        def __call__(self, which, who, priority):
            events.append(("setpriority", which, who, priority))
            return 0

    class FakeLib:
        setpriority = FakeSetPriority()

    monkeypatch.setattr(reaper.sys, "platform", "darwin")
    monkeypatch.setattr(
        reaper.ctypes, "CDLL", lambda *args, **kwargs: events.append(("load", args[0])) or FakeLib()
    )
    monkeypatch.setattr(
        reaper, "run_retention_sweep",
        lambda **_kwargs: events.append(("walk",)) or {"ran": False, "skipped": "already_running"},
    )

    assert reaper.main(["--delete"]) == 0
    assert [event[0] for event in events] == ["load", "setpriority", "walk"]
    assert events[1][1:] == (4, 0, 0)


def test_darwin_priority_failure_is_nonfatal_and_logged_once(monkeypatch, capsys) -> None:
    class FakeSetPriority:
        def __call__(self, *_args):
            return -1

    class FakeLib:
        setpriority = FakeSetPriority()

    monkeypatch.setattr(reaper, "_darwin_priority_warning_logged", False)
    monkeypatch.setattr(reaper.sys, "platform", "darwin")
    monkeypatch.setattr(reaper.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLib())
    monkeypatch.setattr(reaper.ctypes, "get_errno", lambda: 5)
    monkeypatch.setattr(
        reaper, "run_retention_sweep",
        lambda **_kwargs: {"ran": False, "skipped": "already_running"},
    )

    assert reaper.main(["--delete"]) == 0
    assert reaper.main(["--delete"]) == 0
    assert capsys.readouterr().err.count("could not clear darwin background role") == 1


def test_sweep_logs_each_gc_removal(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "removed-seat"
    sweep = {
        "ran": True,
        "artifacts": {
            "deleted_count": 0,
            "deleted_allocated_bytes": 0,
            "total_allocated_bytes": 0,
            "scratch_deleted_count": 0,
            "scratch_deleted_bytes": 0,
            "legacy_deleted_count": 0,
            "legacy_deleted_bytes": 0,
            "legacy_kept_reasons": {},
        },
        "copy_on_write": {"seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0},
        "trace_buckets": 0,
        "snapshots": 0,
        "worktree_gc": 1,
        "read_only_pools": 1,
        "worktree_gc_removals": [str(path)],
        "cleanup_errors": [],
    }
    monkeypatch.setattr(reaper, "run_retention_sweep", lambda **_kwargs: sweep)

    assert reaper.main(["--delete", "--json"]) == 0

    assert f"retention sweep worktree GC removed: {path}" in capsys.readouterr().err


def test_sweep_logs_and_counts_pool_and_seat_dedupe_errors(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setattr(reaper, "_project_roots", lambda _records, _errors, **_kwargs: {project})
    monkeypatch.setattr(reaper, "_expire_trace_archives", lambda _root, **_kwargs: 0)
    monkeypatch.setattr(reaper, "_prune_task_store_snapshots", lambda: 0)

    import goalflight_worktree_gc
    import goalflight_worktree_pool

    monkeypatch.setattr(
        goalflight_worktree_pool, "reap_read_only_worktrees",
        lambda _root, *, deadline=None: None,
    )
    monkeypatch.setattr(goalflight_worktree_gc, "main", lambda _argv, **_kwargs: 0)
    errors = [
        f"{project}: pool setup failed",
        f"{project / 'worktrees' / 's-1'}: lsof failed",
    ]
    monkeypatch.setattr(
        seat_dedupe,
        "_dedupe_pools",
        lambda *_args, **_kwargs: {
            "seats_scanned": 1,
            "files_cloned": 0,
            "bytes_shared": 0,
            "errors": errors,
        },
    )
    body = reaper._run_sweep_body(
        homes_dir=homes, ledger_dir=ledger, now=NOW, identity_probe=_dead
    )
    assert body["cleanup_errors"] == errors
    monkeypatch.setattr(
        reaper, "run_retention_sweep", lambda **_kwargs: {"ran": True, **body}
    )

    assert reaper.main(["--delete", "--json"]) == 0

    logged = capsys.readouterr().err
    assert "errors=2" in logged
    assert all(f"retention sweep cleanup error: {error}" in logged for error in errors)


def test_sweep_runs_dedupe_once_after_all_retention(tmp_path: Path, monkeypatch) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    roots = {tmp_path / "project-a", tmp_path / "project-b"}
    for root in roots:
        (root / ".git").mkdir(parents=True)
    ordered_roots = sorted(roots, key=os.fspath)
    events = []

    monkeypatch.setattr(
        reaper, "_run_dispatch_retention",
        lambda **_kwargs: events.append("artifacts") or {},
    )
    monkeypatch.setattr(reaper, "_project_roots", lambda _records, _errors, **_kwargs: roots)
    monkeypatch.setattr(
        reaper, "_expire_trace_archives",
        lambda root, **_kwargs: events.append(("traces", root)) or 0,
    )
    monkeypatch.setattr(reaper, "_prune_task_store_snapshots", lambda: events.append("snapshots") or 0)
    import goalflight_worktree_gc
    import goalflight_worktree_pool

    gc_ledger_index = goalflight_worktree_gc.LedgerIndex([], [])
    ledger_reads = []
    gc_snapshots = []
    monkeypatch.setattr(
        goalflight_worktree_gc,
        "ledger_index_for_dir",
        lambda path, *, deadline=None: ledger_reads.append(path) or gc_ledger_index,
    )

    monkeypatch.setattr(
        goalflight_worktree_pool,
        "reap_read_only_worktrees",
        lambda root, *, deadline=None: events.append(("read-only", root)),
    )
    def fake_gc(argv, **kwargs):
        gc_snapshots.append(kwargs.get("_initial_ledger_index"))
        events.append(("gc", Path(argv[0])))
        return 0

    monkeypatch.setattr(goalflight_worktree_gc, "main", fake_gc)
    def fake_dedupe_pools(roots, **kwargs):
        kwargs["project_timings"].update({ordered_roots[0]: 0.5, ordered_roots[1]: 1.0})
        events.append(("dedupe", roots))
        return {
            "seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0, "errors": [],
        }
    monkeypatch.setattr(seat_dedupe, "_dedupe_pools", fake_dedupe_pools)

    result = reaper._run_sweep_body(
        homes_dir=homes,
        ledger_dir=ledger,
        now=NOW,
        identity_probe=_dead,
        on_retention_finished=lambda: events.append("cooldown-stamp"),
    )

    assert result["cleanup_errors"] == []
    assert ledger_reads == [ledger]
    assert gc_snapshots == [gc_ledger_index, gc_ledger_index]
    assert result["timings"]["dedupe"] == 1.0
    assert result["timings"]["projects"]["dedupe"] == str(ordered_roots[1])

    assert events == [
        "artifacts",
        ("traces", ordered_roots[0]),
        ("read-only", ordered_roots[0]),
        ("gc", ordered_roots[0]),
        ("traces", ordered_roots[1]),
        ("read-only", ordered_roots[1]),
        ("gc", ordered_roots[1]),
        "snapshots",
        "cooldown-stamp",
        ("dedupe", ordered_roots),
    ]
    assert result["copy_on_write"] == {
        "seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0,
    }


def test_retention_cooldown_uses_retention_finish_time(tmp_path: Path, monkeypatch) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    import goalflight_ledger

    lock = goalflight_ledger.state_dir() / "retention-sweep.lock"
    monkeypatch.setattr(reaper, "SWEEP_INTERVAL_S", 60 * 60)
    clock = [100_000.0]
    real_utime = os.utime
    monkeypatch.setattr(reaper.time, "time", lambda: clock[0])

    def controlled_utime(path, times=None, **kwargs):
        if Path(path) == lock:
            return real_utime(path, (clock[0], clock[0]), **kwargs)
        return real_utime(path, times, **kwargs)

    monkeypatch.setattr(reaper.os, "utime", controlled_utime)
    calls = []

    def body(**kwargs):
        calls.append("retention")
        kwargs["on_retention_finished"]()
        calls.append("dedupe")
        clock[0] += 15 * 60
        return {}

    monkeypatch.setattr(reaper, "_run_sweep_body", body)

    first = reaper.run_retention_sweep(homes_dir=homes, ledger_dir=ledger, now=NOW)
    clock[0] += 60 * 60 + 1
    second = reaper.run_retention_sweep(homes_dir=homes, ledger_dir=ledger, now=NOW)

    assert first["ran"] and second["ran"]
    assert calls == ["retention", "dedupe", "retention", "dedupe"]


def test_sweep_leaves_seat_archive_directory_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    archive_dir = homes.parent / "seat-archive"
    archive_dir.mkdir()
    archive = archive_dir / "preserved.tar.gz"
    archive.write_bytes(b"durable seat archive")
    monkeypatch.setenv("GOALFLIGHT_SEAT_ARCHIVE_DIR", str(archive_dir))

    result = reaper.run_retention_sweep(
        homes_dir=homes,
        ledger_dir=ledger,
        now=NOW,
        identity_probe=_dead,
    )

    assert result["ran"]
    assert archive.read_bytes() == b"durable seat archive"


def test_sweep_leaves_seat_archive_directory_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    archive_dir = homes.parent / "seat-archive"
    archive_dir.mkdir()
    archive = archive_dir / "preserved.tar.gz"
    archive.write_bytes(b"durable seat archive")
    monkeypatch.setenv("GOALFLIGHT_SEAT_ARCHIVE_DIR", str(archive_dir))

    result = reaper.run_retention_sweep(
        homes_dir=homes,
        ledger_dir=ledger,
        now=NOW,
        identity_probe=_dead,
    )

    assert result["ran"]
    assert archive.read_bytes() == b"durable seat archive"


def test_sweep_dedupes_aliases_once_with_git_root_spelling(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    nested = project / "nested"
    nested.mkdir()
    alias = Path(f"{nested}/..")

    git_env = os.environ.copy()
    git_env["GIT_OPTIONAL_LOCKS"] = "0"
    canonical_result = subprocess.run(
        [
            "git", "-C", str(project), "-c", "core.fsmonitor=false",
            "rev-parse", "--path-format=absolute", "--show-toplevel",
        ],
        capture_output=True,
        text=True,
        env=git_env,
        check=True,
    )
    canonical = Path(canonical_result.stdout.strip())
    records = [
        {"project_root": str(project)},
        {"project_root": str(alias)},
    ]
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)

    import goalflight_task
    import goalflight_worktree_gc
    import goalflight_worktree_pool

    monkeypatch.setattr(goalflight_task, "read_project_registry", lambda: [])
    monkeypatch.setattr(reaper, "_run_dispatch_retention", lambda **_kwargs: {})
    gc_ledger_index = goalflight_worktree_gc.LedgerIndex(records, [])
    monkeypatch.setattr(
        goalflight_worktree_gc,
        "ledger_index_for_dir",
        lambda _ledger, *, deadline=None: gc_ledger_index,
    )
    monkeypatch.setattr(reaper, "_prune_task_store_snapshots", lambda: 0)
    monkeypatch.setattr(reaper, "_expire_trace_archives", lambda _root, **_kwargs: 0)
    monkeypatch.setattr(
        goalflight_worktree_pool, "reap_read_only_worktrees",
        lambda _root, *, deadline=None: None,
    )
    monkeypatch.setattr(goalflight_worktree_gc, "main", lambda _argv, **_kwargs: 0)
    dedupe_calls = []
    monkeypatch.setattr(
        seat_dedupe,
        "_dedupe_pools",
        lambda roots, **_kwargs: dedupe_calls.append(roots) or {
            "seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0, "errors": [],
        },
    )

    reaper._run_sweep_body(
        homes_dir=homes,
        ledger_dir=ledger,
        now=NOW,
        identity_probe=_dead,
    )

    assert dedupe_calls == [[canonical]]


def test_dry_run_reports_the_same_candidates_without_deleting(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "dry-run")
    _record(ledger, "dry-run")

    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead, delete=False,
    )

    assert home.is_dir()
    assert report["mode"] == "dry-run"
    assert report["eligible_count"] == 1
    assert report["eligible_allocated_bytes"] == reaper.allocated_tree_bytes(home)


def test_sweep_keeps_two_dated_task_and_dashboard_snapshots(
    tmp_path: Path, monkeypatch
) -> None:
    _fixture_tree(tmp_path, monkeypatch)
    log = tmp_path / "task-store" / "task-stores" / "repo" / "docs-private" / "log"
    log.mkdir(parents=True)
    for stream in ("tasks", "tasks-data"):
        suffix = ".jsonl" if stream == "tasks" else ".js"
        for index in range(4):
            path = log / f"{stream}-20261012T00000{index}Z{suffix}"
            path.write_text("snapshot", encoding="utf-8")
            os.utime(path, (1000 + index, 1000 + index))

    assert reaper._prune_task_store_snapshots() == 4
    assert len(list(log.glob("tasks-*.jsonl"))) == 2
    assert len(list(log.glob("tasks-data-*.js"))) == 2


def test_sweep_uses_one_flock_and_skips_overlap(tmp_path: Path, monkeypatch) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "SWEEP_INTERVAL_S", 0)
    entered = threading.Event()
    release = threading.Event()

    def blocked_body(**_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return {"test": True}

    monkeypatch.setattr(reaper, "_run_sweep_body", blocked_body)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reaper.run_retention_sweep, homes_dir=homes, ledger_dir=ledger, now=NOW)
        assert entered.wait(timeout=5)
        second = reaper.run_retention_sweep(homes_dir=homes, ledger_dir=ledger, now=NOW)
        release.set()
        assert first.result(timeout=5) == {"ran": True, "test": True}

    assert second == {"ran": False, "skipped": "already_running"}


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup requires POSIX")
def test_sweep_deadline_kills_git_descendant_and_releases_flock(tmp_path: Path) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    script = textwrap.dedent(
        r"""
        import json
        import os
        from pathlib import Path
        import signal
        import subprocess
        import sys
        import time

        root_dir = Path(sys.argv[1])
        scripts = Path(sys.argv[2])
        sys.path.insert(0, str(scripts))
        state = root_dir / "state"
        homes = root_dir / "homes"
        ledger = state / "runs.d"
        dispatch = state / "dispatch"
        pidfiles = root_dir / "pidfiles"
        legacy = root_dir / "legacy"
        project = root_dir / "project"
        fake_bin = root_dir / "bin"
        home = root_dir / "home"
        for path in (state, homes, ledger, dispatch, pidfiles, legacy, fake_bin, home):
            path.mkdir(parents=True, exist_ok=True)
        project.mkdir()
        subprocess.run([sys.argv[3], "init", "--quiet", str(project)], check=True)

        shim = fake_bin / "git"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import subprocess, sys\n"
            "if 'worktree' in sys.argv and 'list' in sys.argv:\n"
            "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "    open(__import__('os').environ['GRANDCHILD_PID_FILE'], 'w').write(str(child.pid))\n"
            "    raise SystemExit(0)\n"
            "import os\n"
            "real_git = os.environ['REAL_GIT']\n"
            "os.execv(real_git, [real_git, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        os.environ.update({
            "HOME": str(home),
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "GOALFLIGHT_STATE_DIR": str(state),
            "GRANDCHILD_PID_FILE": str(root_dir / "grandchild.pid"),
            "REAL_GIT": sys.argv[3],
            "GOALFLIGHT_JOURNAL_DIR": str(ledger),
            "GOALFLIGHT_DISPATCH_DIR": str(dispatch),
            "GOALFLIGHT_PIDFILE_DIR": str(pidfiles),
            "GOAL_FLIGHT_PIDFILE_DIR": str(pidfiles),
            "GOALFLIGHT_CODEX_STATE_DIR": str(root_dir / "codex-state"),
            "GOALFLIGHT_TASK_STORE_DIR": str(root_dir / "task-store"),
            "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
        })

        import goalflight_reap_dispatch_homes as reaper

        reaper._SWEEP_TIMEOUT_SECONDS = 10.0
        reaper._legacy_claude_scratch_dir = lambda: legacy
        reaper._project_roots = lambda _records, _errors, **_kwargs: {project}
        reaper._run_dispatch_retention = lambda **_kwargs: {
            "deadline_skipped": [],
            "mode": "delete",
            "deleted_count": 0,
            "deleted_allocated_bytes": 0,
            "total_allocated_bytes": 0,
            "scratch_deleted_count": 0,
            "scratch_deleted_bytes": 0,
            "legacy_deleted_count": 0,
            "legacy_deleted_bytes": 0,
            "legacy_kept_reasons": {},
            "budget_bytes": 0,
            "eligible_count": 0,
            "eligible_allocated_bytes": 0,
            "legacy_would_delete_count": 0,
            "legacy_would_delete_bytes": 0,
        }
        reaper._legacy_scratch_process_snapshot = lambda paths, *, deadline=None: {
            os.path.abspath(path): (True, "") for path in paths
        }
        original_run = reaper.run_retention_sweep
        first = {}
        def capture_run(**kwargs):
            result = original_run(**kwargs)
            first.update(result)
            return result
        reaper.run_retention_sweep = capture_run
        started = time.monotonic()
        status = reaper.main([
            "--delete", "--homes-dir", str(homes), "--ledger-dir", str(ledger),
        ])
        elapsed = time.monotonic() - started
        assert status == 0
        assert first.get("ran") is True
        assert first.get("deadline_reached") is True, first
        assert first.get("deadline_skipped")
        assert elapsed < 11.0, elapsed
        child_pid = int((root_dir / "grandchild.pid").read_text())
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            os.kill(child_pid, signal.SIGKILL)
            raise AssertionError(f"git grandchild {child_pid} survived timeout cleanup")

        reaper.run_retention_sweep = original_run
        reaper._run_sweep_body = lambda **_kwargs: {"deadline_reached": False}
        second = original_run(
            homes_dir=homes, ledger_dir=ledger, force=True,
        )
        assert second.get("ran") is True, second
        print(json.dumps({"elapsed": elapsed, "second_sweep_ran": second["ran"]}))
        """
    )
    stdout_path = tmp_path / "sweep.stdout"
    stderr_path = tmp_path / "sweep.stderr"
    with stdout_path.open("w") as stdout_file, stderr_path.open("w") as stderr_file:
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), str(ROOT / "scripts"), real_git],
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            grandchild_pid_file = tmp_path / "grandchild.pid"
            if grandchild_pid_file.exists():
                try:
                    os.kill(int(grandchild_pid_file.read_text()), signal.SIGKILL)
                except (OSError, ValueError):
                    pass
            pytest.fail("the bounded sweep scenario exceeded its outer 20 second watchdog")
        stdout_file.flush()
        stderr_file.flush()
    stdout = stdout_path.read_text()
    stderr = stderr_path.read_text()

    assert child.returncode == 0, f"stdout={stdout}\nstderr={stderr}"
    assert "retention sweep deadline reached; skipped=" in stderr
    assert '"second_sweep_ran": true' in stdout


def test_allocated_tree_bytes_skips_entries_that_vanish_during_the_walk(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "scratch"
    (root / "sub").mkdir(parents=True)
    keep = root / "keep.bin"
    keep.write_bytes(b"x" * 8192)
    gone_file = root / "gone.tmp"
    gone_file.write_bytes(b"y" * 8192)
    gone_dir = root / "sub"
    real_lstat, real_scandir = Path.lstat, os.scandir

    def racing_lstat(self, *args, **kwargs):
        if self == gone_file:
            raise FileNotFoundError(2, "vanished", str(self))
        return real_lstat(self, *args, **kwargs)

    def racing_scandir(path):
        if Path(path) == gone_dir:
            raise FileNotFoundError(2, "vanished", str(path))
        return real_scandir(path)

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    monkeypatch.setattr(reaper.os, "scandir", racing_scandir)
    size = reaper.allocated_tree_bytes(root)
    assert size >= real_lstat(keep).st_blocks * 512 > 0


def test_allocated_tree_bytes_still_raises_for_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        reaper.allocated_tree_bytes(tmp_path / "absent")


def test_allocated_tree_bytes_counts_walked_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    (root / "one").write_text("1", encoding="utf-8")
    (child / "two").write_text("2", encoding="utf-8")
    walked = [0]

    reaper.allocated_tree_bytes(root, walked_files=walked)

    assert walked == [2]


def test_allocated_tree_bytes_skips_a_child_whose_parent_became_a_file(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "scratch"
    (root / "build").mkdir(parents=True)
    child = root / "build" / "output.o"
    child.write_bytes(b"z" * 4096)
    real_lstat = Path.lstat

    def racing_lstat(self, *args, **kwargs):
        if self == child:
            raise NotADirectoryError(20, "parent replaced by a file", str(self))
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    assert reaper.allocated_tree_bytes(root) >= 0


def test_one_unmeasurable_artifact_does_not_stop_other_deletions(tmp_path: Path, monkeypatch) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    oldest = _home(homes, "oldest")
    unreadable = _home(homes, "unreadable")
    _record(ledger, "oldest", ended_at=NOW - dt.timedelta(days=5))
    _record(ledger, "unreadable", ended_at=NOW - dt.timedelta(days=6))
    real_measure = reaper.allocated_tree_bytes

    def measure(root, *, walked_files=None):
        if Path(root) == unreadable:
            raise PermissionError(13, "unreadable", str(root))
        return real_measure(root, walked_files=walked_files)

    monkeypatch.setattr(reaper, "allocated_tree_bytes", measure)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert not oldest.exists()
    assert unreadable.is_dir()
    assert report["budget_measurable"] is False
    assert report["deleted_count"] == 1


def _set_tree_mtime(root: Path, timestamp: float) -> None:
    paths = [root]
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        paths.extend(parent / name for name in [*dirs, *files])
    for path in reversed(paths):
        os.utime(path, (timestamp, timestamp), follow_symlinks=False)


def _codex_maintenance_fixture(
    tmp_path: Path, monkeypatch, *, state: str = "complete",
    include_codex_home: bool = True,
) -> tuple[Path, Path, Path, dict]:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    home = homes / "codex-maintenance"
    home.mkdir()
    (home / "state.sqlite").write_bytes(b"database")
    fields = {"engine": "codex", "codex_session_id": SESSION_A}
    if include_codex_home:
        fields["codex_home"] = str(home)
    record = _record(ledger, home.name, state=state, **fields)
    old = (NOW - dt.timedelta(hours=3)).timestamp()
    _set_tree_mtime(home, old)
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {os.path.abspath(path): (True, "") for path in paths},
    )
    return homes, ledger, home, record


def _run_codex_maintenance(
    homes: Path, ledger: Path, *, probe=_dead, delete: bool = True,
) -> dict:
    inventory = reaper._complete_record_inventory(ledger)
    assert inventory is not None
    records, by_id, _children = inventory
    return reaper._run_codex_home_maintenance(
        homes_dir=homes, ledger_dir=ledger, records=records, by_id=by_id,
        records_complete=True,
        now=NOW, identity_probe=probe, probe=probe, delete=delete,
        protected_home_ids=set(),
    )


def test_codex_home_maintenance_keeps_nonterminal_record(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, home, _record = _codex_maintenance_fixture(
        tmp_path, monkeypatch, state="running", include_codex_home=False,
    )

    _run_codex_maintenance(homes, ledger)

    assert home.is_dir()


def test_codex_home_maintenance_keeps_live_worker(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, home, _record = _codex_maintenance_fixture(tmp_path, monkeypatch)

    _run_codex_maintenance(homes, ledger, probe=lambda _row: (True, "live"))

    assert home.is_dir()


@pytest.mark.parametrize(
    "reference", ["codex_home", "codex_home_owner_dispatch_id"],
)
def test_codex_home_maintenance_keeps_home_used_by_each_live_reference(
    tmp_path: Path, monkeypatch, reference: str,
) -> None:
    homes, ledger, home, _owner_record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    fields = {reference: str(home) if reference == "codex_home" else home.name}
    _record(ledger, f"live-child-{reference}", state="running", engine="codex", **fields)

    _run_codex_maintenance(homes, ledger)

    assert home.is_dir()


def test_codex_home_maintenance_fails_closed_when_open_files_are_unknown(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, home, _record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    calls = []

    def unknown_open_state(paths, *, deadline=None):
        calls.append(list(paths))
        return {os.path.abspath(path): (False, "lsof_error") for path in paths}

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", unknown_open_state)
    _run_codex_maintenance(homes, ledger)

    assert calls == [[home]]
    assert home.is_dir()


def test_codex_home_maintenance_keeps_home_with_open_file(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, home, _record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {os.path.abspath(path): (False, "lsof_hit") for path in paths},
    )

    _run_codex_maintenance(homes, ledger)

    assert home.is_dir()


def test_codex_home_maintenance_keeps_recent_home(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, home, _record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    recent = (NOW - dt.timedelta(hours=1)).timestamp()
    _set_tree_mtime(home, recent)

    _run_codex_maintenance(homes, ledger)

    assert home.is_dir()


def test_codex_home_maintenance_skips_busy_resume_lock(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, home, _record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "_lock_users", lambda *_args, **_kwargs: None)

    _run_codex_maintenance(homes, ledger)

    assert home.is_dir()


def test_codex_home_maintenance_skips_unresolvable_required_lock(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, home, _record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        goalflight_dispatch, "_resume_lock_path_info", lambda *_args: (None, True),
    )
    monkeypatch.setattr(reaper, "_lock_users", lambda *_args, **_kwargs: ([], set()))

    _run_codex_maintenance(homes, ledger)

    assert home.is_dir()


def test_codex_home_maintenance_never_follows_a_symlinked_home(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, _home, _owner_record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    target = tmp_path / "outside-home"
    target.mkdir()
    protected = target / "state.sqlite"
    protected.write_bytes(b"outside")
    link = homes / "symlink-home"
    link.symlink_to(target, target_is_directory=True)
    _record(ledger, link.name, engine="codex", codex_session_id=SESSION_B)
    monkeypatch.setattr(
        reaper, "_tree_snapshot",
        lambda _path: {
            "entries": set(), "fingerprints": {}, "newest_mtime": (NOW - dt.timedelta(hours=3)).timestamp(),
            "allocated_bytes": 1, "has_symlink": False,
        },
    )
    monkeypatch.setattr(reaper.shutil, "rmtree", lambda path: Path(path).unlink())
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {os.path.abspath(path): (True, "") for path in paths},
    )

    _run_codex_maintenance(homes, ledger)

    assert link.is_symlink()
    assert protected.read_bytes() == b"outside"


@pytest.mark.parametrize("change", ["live_reference", "live_worker", "recent_mtime", "open_file"])
def test_codex_home_maintenance_revalidates_eligibility_after_lock(
    tmp_path: Path, monkeypatch, change: str,
) -> None:
    homes, ledger, home, _owner_record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    real_lock_users = reaper._lock_users
    changed = False
    probe_calls = 0

    def probe(record):
        nonlocal probe_calls
        probe_calls += 1
        if change == "live_worker" and probe_calls > 1:
            return True, "live"
        return _dead(record)

    def lock_then_change(ids, *args, **kwargs):
        nonlocal changed
        if not changed and ids == {home.name}:
            changed = True
            if change == "live_reference":
                _record(
                    ledger, "published-resume", state="running", engine="codex",
                    codex_home=str(home), codex_home_owner_dispatch_id=home.name,
                )
            elif change == "recent_mtime":
                recent = (NOW - dt.timedelta(minutes=30)).timestamp()
                os.utime(home / "state.sqlite", (recent, recent))
            elif change == "open_file":
                monkeypatch.setattr(
                    reaper, "_legacy_scratch_process_snapshot",
                    lambda paths, *, deadline=None: {
                        os.path.abspath(path): (False, "lsof_hit") for path in paths
                    },
                )
        return real_lock_users(ids, *args, **kwargs)

    monkeypatch.setattr(reaper, "_lock_users", lock_then_change)
    _run_codex_maintenance(homes, ledger, probe=probe)

    assert changed
    assert home.is_dir()
    assert (home / "state.sqlite").is_file()


def test_codex_home_maintenance_reloads_ledger_after_lsof(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, home, _owner_record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    sibling = _record(ledger, "lsof-published-resume", engine="codex")
    sibling_path = ledger / f"{reaper.goalflight_compat.safe_dispatch_filename(sibling['dispatch_id'])}.json"
    calls = []

    def rewrite_during_lsof(paths, *, deadline=None):
        calls.append(list(paths))
        rewritten = json.loads(sibling_path.read_text(encoding="utf-8"))
        rewritten.update(state="running", codex_home=str(home))
        sibling_path.write_text(json.dumps(rewritten), encoding="utf-8")
        return {os.path.abspath(path): (True, "") for path in paths}

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", rewrite_during_lsof)

    report = _run_codex_maintenance(homes, ledger)

    assert calls == [[home]]
    assert home.is_dir()
    assert (home / "state.sqlite").is_file()
    assert report["kept_reasons"]["used_by_non_terminal_record"] == 1


def test_budget_home_deletion_rechecks_codex_safety_without_housekeeping(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "budget-recent-home")
    _record(
        ledger, home.name, engine="codex", codex_session_id=SESSION_A,
        codex_home=str(home),
    )
    recent = (NOW - dt.timedelta(minutes=30)).timestamp()
    os.utime(home / "state.sqlite", (recent, recent))
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {os.path.abspath(path): (True, "") for path in paths},
    )

    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead,
    )

    assert home.is_dir()
    assert report["deleted_count"] == 0
    assert report["kept_reasons"]["recently_modified"] == 1


@pytest.mark.parametrize(
    ("open_reason", "expected_reason"),
    [("lsof_hit", "open_file"), ("lsof_error", "lsof_error")],
)
def test_budget_home_deletion_fails_closed_on_open_state(
    tmp_path: Path, monkeypatch, open_reason: str, expected_reason: str,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, f"budget-{expected_reason}")
    _record(
        ledger, home.name, engine="codex", codex_session_id=SESSION_A,
        codex_home=str(home),
    )
    calls = []

    def open_state(paths, *, deadline=None):
        calls.append(list(paths))
        return {
            os.path.abspath(path): (False, open_reason)
            for path in paths
        }

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", open_state)
    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead,
    )

    assert calls == [[home]]
    assert home.is_dir()
    assert report["deleted_count"] == 0
    assert report["kept_reasons"][expected_reason] == 1


def test_budget_home_deletion_keeps_home_and_reports_lsof_timeout(
    tmp_path: Path, monkeypatch,
) -> None:
    process_snapshot = reaper._legacy_scratch_process_snapshot
    real_run = reaper.subprocess.run
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", process_snapshot)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "budget-lsof-timeout")
    _record(
        ledger, home.name, engine="codex", codex_session_id=SESSION_A,
        codex_home=str(home),
    )
    calls = []

    def timeout_lsof(args, **kwargs):
        if args[0] != "lsof":
            return real_run(args, **kwargs)
        assert args[0] == "lsof"
        calls.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(reaper.subprocess, "run", timeout_lsof)
    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead,
    )

    assert calls == [reaper._LEGACY_SCRATCH_LSOF_TIMEOUT_SECONDS]
    assert home.is_dir()
    assert report["deleted_count"] == 0
    assert report["kept_reasons"]["lsof_timeout"] == 1


def test_budget_home_deletion_rechecks_existing_row_rewritten_under_lock(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "budget-row-rewrite")
    _record(
        ledger, home.name, engine="codex", codex_session_id=SESSION_A,
        codex_home=str(home),
    )
    sibling = _record(ledger, "existing-sibling", engine="codex")
    sibling_path = ledger / f"{reaper.goalflight_compat.safe_dispatch_filename(sibling['dispatch_id'])}.json"
    real_lock_users = reaper._lock_users
    rewrote = False

    def lock_then_rewrite(ids, *args, **kwargs):
        nonlocal rewrote
        if not rewrote and ids == {home.name}:
            rewritten = json.loads(sibling_path.read_text(encoding="utf-8"))
            rewritten.update(state="running", codex_home=str(home))
            sibling_path.write_text(json.dumps(rewritten), encoding="utf-8")
            rewrote = True
        return real_lock_users(ids, *args, **kwargs)

    monkeypatch.setattr(reaper, "_lock_users", lock_then_rewrite)
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {os.path.abspath(path): (True, "") for path in paths},
    )
    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead,
    )

    assert rewrote
    assert home.is_dir()
    assert (home / "state.sqlite").is_file()
    assert report["deleted_count"] == 0
    assert report["kept_reasons"]["used_by_non_terminal_record"] == 1


def test_budget_home_deletion_reloads_ledger_after_lsof(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "budget-lsof-row-rewrite")
    _record(
        ledger, home.name, engine="codex", codex_session_id=SESSION_A,
        codex_home=str(home),
    )
    sibling = _record(ledger, "budget-existing-sibling", engine="codex")
    sibling_path = ledger / f"{reaper.goalflight_compat.safe_dispatch_filename(sibling['dispatch_id'])}.json"
    calls = []

    def rewrite_during_lsof(paths, *, deadline=None):
        calls.append(list(paths))
        rewritten = json.loads(sibling_path.read_text(encoding="utf-8"))
        rewritten.update(state="running", codex_home=str(home))
        sibling_path.write_text(json.dumps(rewritten), encoding="utf-8")
        return {os.path.abspath(path): (True, "") for path in paths}

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", rewrite_during_lsof)
    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead,
    )

    assert calls == [[home]]
    assert home.is_dir()
    assert (home / "state.sqlite").is_file()
    assert report["deleted_count"] == 0
    assert report["kept_reasons"]["used_by_non_terminal_record"] == 1


def test_budget_does_not_delete_a_home_kept_by_maintenance_in_same_sweep(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "same-sweep-protection")
    _record(
        ledger, home.name, engine="codex", codex_session_id=SESSION_A,
        codex_home=str(home),
    )
    calls = []

    def open_state(paths, *, deadline=None):
        calls.append(tuple(paths))
        result = len(calls) > 1
        return {
            os.path.abspath(path): (result, "" if result else "lsof_error")
            for path in paths
        }

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", open_state)

    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead,
        include_housekeeping=True,
    )

    assert home.is_dir()
    assert calls == [(home,)]
    assert report["codex_home_maintenance"]["kept_reasons"]["lsof_error"] == 1
    assert report["deleted_count"] == 0


def test_budget_deletes_rollout_home_after_the_codex_trim(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "budget-rollout-home")
    sessions = home / "sessions"
    sessions.mkdir()
    rollout = sessions / "rollout-kept.jsonl"
    rollout.write_text("resume", encoding="utf-8")
    _record(
        ledger, home.name, engine="codex", codex_session_id=SESSION_A,
        codex_home=str(home),
    )
    _set_tree_mtime(home, (NOW - dt.timedelta(hours=3)).timestamp())
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {os.path.abspath(path): (True, "") for path in paths},
    )

    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead,
        include_housekeeping=True,
    )

    assert not home.exists()
    assert report["codex_home_maintenance"]["trimmed_count"] == 1
    assert report["codex_home_maintenance"]["kept_reasons"]["rollout_present"] == 1
    assert report["deleted_count"] == 1
    assert report["budget_met"] is True


@pytest.mark.parametrize("include_junk", [False, True])
def test_trim_unlinks_symlinks_inside_cache_and_budget_can_delete_home(
    tmp_path: Path, monkeypatch, include_junk: bool,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, f"trim-cache-symlink-{include_junk}")
    sessions = home / "sessions"
    sessions.mkdir()
    (sessions / "rollout-kept.jsonl").write_text("resume", encoding="utf-8")
    cache = home / "cache"
    cache.mkdir()
    outside = tmp_path / f"outside-cache-{include_junk}"
    outside.write_text("outside", encoding="utf-8")
    (cache / "link").symlink_to(outside)
    if include_junk:
        (cache / "junk").write_text("discard", encoding="utf-8")
    _record(
        ledger, home.name, engine="codex", codex_session_id=SESSION_A,
        codex_home=str(home),
    )
    _set_tree_mtime(home, (NOW - dt.timedelta(hours=3)).timestamp())
    _clear_session_open_state(monkeypatch)

    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead,
        include_housekeeping=True,
    )

    assert not home.exists()
    assert outside.read_text(encoding="utf-8") == "outside"
    assert report["codex_home_maintenance"]["trimmed_count"] == 1
    assert "trim_error" not in report["codex_home_maintenance"]["kept_reasons"]
    assert report["deleted_count"] == 1
    assert report["budget_met"] is True


def test_budget_deletes_symlinked_home_entry_without_following_target(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    home = _home(homes, "budget-symlink-home")
    (home / "auth.json").write_bytes(b"auth" * 128)
    outside = tmp_path / "outside-session-data"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("outside", encoding="utf-8")
    (home / "sessions").symlink_to(outside, target_is_directory=True)
    _record(
        ledger, home.name, engine="codex", codex_session_id=SESSION_A,
        codex_home=str(home),
    )
    _set_tree_mtime(home, (NOW - dt.timedelta(hours=3)).timestamp())
    _clear_session_open_state(monkeypatch)

    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead,
        include_housekeeping=True,
    )

    assert not home.exists()
    assert marker.read_text(encoding="utf-8") == "outside"
    assert report["codex_home_maintenance"]["kept_reasons"]["symlink_present"] == 1
    assert report["deleted_count"] == 1


def test_scheduled_sweep_deletes_rolloutless_home_without_budget_or_24h_gate(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, _dispatch, _pidfiles = _fixture_tree(tmp_path, monkeypatch)
    home = homes / "rolloutless-terminal"
    home.mkdir()
    (home / "state.sqlite").write_bytes(b"database")
    _record(
        ledger, home.name, ended_at=NOW - dt.timedelta(hours=1),
        engine="codex", codex_session_id=SESSION_A, codex_home=str(home),
    )
    _set_tree_mtime(home, (NOW - dt.timedelta(hours=3)).timestamp())
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {os.path.abspath(path): (True, "") for path in paths},
    )

    result = reaper._run_sweep_body(
        homes_dir=homes, ledger_dir=ledger, now=NOW, identity_probe=_dead,
    )

    assert not home.exists()
    maintenance = result["artifacts"]["codex_home_maintenance"]
    assert maintenance["deleted_count"] == 1
    assert maintenance["deleted_bytes"] > 0
    assert result["artifacts"]["deleted_count"] == 0


def test_codex_home_maintenance_keeps_resume_state_and_orphan_wals(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, home, _record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    sessions = home / "sessions"
    sessions.mkdir()
    rollout = sessions / "rollout-kept.jsonl"
    rollout.write_text("resume", encoding="utf-8")
    kept = {
        "auth.json": b"auth", "config.toml": b"config", "hooks.json": b"hooks",
        "installation_id": b"installation",
    }
    for name, content in kept.items():
        (home / name).write_bytes(content)
    targets = ("cache", "plugins", "shell_snapshots", "tmp", "thread-writer-locks")
    for name in targets:
        directory = home / name
        directory.mkdir()
        (directory / "junk").write_bytes(b"junk")
    outside = tmp_path / "outside-target"
    outside.mkdir()
    outside_file = outside / "must-survive"
    outside_file.write_bytes(b"outside")
    (home / "cache" / "link").symlink_to(outside, target_is_directory=True)
    database = home / "thread.sqlite"
    database.write_bytes(b"db")
    Path(f"{database}-wal").write_bytes(b"wal")
    Path(f"{database}-shm").write_bytes(b"shm")
    orphan_wal = home / "orphan.sqlite-wal"
    orphan_wal.write_bytes(b"orphan wal")
    orphan_shm = home / "orphan.sqlite-shm"
    orphan_shm.write_bytes(b"orphan shm")
    (home / "models_cache.json").write_text("{}", encoding="utf-8")
    _set_tree_mtime(home, (NOW - dt.timedelta(hours=3)).timestamp())

    result = _run_codex_maintenance(homes, ledger)

    assert result["trimmed_count"] == 1
    assert result["trimmed_bytes"] > 0
    assert sessions.is_dir() and rollout.read_text(encoding="utf-8") == "resume"
    assert all((home / name).read_bytes() == content for name, content in kept.items())
    assert database.exists() is False
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert orphan_wal.read_bytes() == b"orphan wal"
    assert orphan_shm.read_bytes() == b"orphan shm"
    assert not (home / "models_cache.json").exists()
    assert all(not (home / name / "junk").exists() for name in targets)
    assert not (home / "cache").exists()
    assert not (home / "cache" / "link").is_symlink()
    assert outside_file.read_bytes() == b"outside"


def test_codex_home_maintenance_keeps_wal_when_database_unlink_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, home, _record = _codex_maintenance_fixture(tmp_path, monkeypatch)
    sessions = home / "sessions"
    sessions.mkdir()
    (sessions / "rollout-kept.jsonl").write_text("resume", encoding="utf-8")
    database = home / "state.sqlite"
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    wal.write_bytes(b"wal")
    shm.write_bytes(b"shm")
    _set_tree_mtime(home, (NOW - dt.timedelta(hours=3)).timestamp())
    real_unlink = Path.unlink

    def fail_database_unlink(path, *args, **kwargs):
        if path == database:
            raise PermissionError("database is protected")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_database_unlink)

    result = _run_codex_maintenance(homes, ledger)

    assert database.is_file()
    assert wal.is_file()
    assert shm.is_file()
    assert result["trimmed_count"] == 0
    assert result["kept_reasons"]["trim_error"] == 1


def _grok_archive_fixture(
    tmp_path: Path, monkeypatch, *, session_id: str = SESSION_B,
) -> tuple[Path, Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    accounts = home / ".goal-flight" / "accounts"
    session = accounts / "acct" / "grok" / ".grok" / "sessions" / "cwd" / session_id
    session.mkdir(parents=True)
    (session / "chat.json").write_text("chat", encoding="utf-8")
    (session / "nested").mkdir()
    (session / "nested" / "result.json").write_text("result", encoding="utf-8")
    _set_tree_mtime(session, (NOW - dt.timedelta(days=8)).timestamp())
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    _record(
        ledger, "tracked-grok", engine="grok", engine_session_id=SESSION_A,
    )
    return accounts, home / ".goal-flight" / "archive" / "grok-sessions", session, ledger


def _clear_session_open_state(monkeypatch) -> None:
    monkeypatch.setattr(
        reaper, "_legacy_scratch_process_snapshot",
        lambda paths, *, deadline=None: {os.path.abspath(path): (True, "") for path in paths},
    )


def _fake_tar_zstd_run(
    *, fail_zstd: bool = False, change: bool = False,
    member_mismatch: bool = False,
):
    archive_entries = {}

    def run(args, **kwargs):
        if args[0] == "tar" and "-cf" in args:
            archive = Path(args[args.index("-cf") + 1])
            cwd = Path(args[args.index("-C") + 1])
            session = cwd / args[-1]
            archive_entries[archive] = sorted(reaper._tree_snapshot(session)["entries"])
            archive.write_bytes(b"archive contents")
            if change:
                (session / "chat.json").write_text("changed", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[0] == "zstd":
            if fail_zstd:
                raise subprocess.CalledProcessError(1, args, b"", b"corrupt archive")
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[0] == "tar" and "-tf" in args:
            members = archive_entries[Path(args[-1])]
            if member_mismatch:
                members = members[:-1]
            return subprocess.CompletedProcess(
                args, 0, "\n".join(members) + "\n", "",
            )
        raise AssertionError(f"unexpected archive command: {args}")

    return run


def test_grok_orphan_archive_verifies_and_removes_only_untracked_idle_session(
    tmp_path: Path, monkeypatch,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    tracked = session.parent / SESSION_A
    tracked.mkdir()
    (tracked / "chat.json").write_text("tracked", encoding="utf-8")
    _set_tree_mtime(tracked, (NOW - dt.timedelta(days=8)).timestamp())
    recent = session.parent / "recent-session"
    recent.mkdir()
    (recent / "chat.json").write_text("recent", encoding="utf-8")
    _set_tree_mtime(recent, (NOW - dt.timedelta(days=1)).timestamp())
    _clear_session_open_state(monkeypatch)
    monkeypatch.setattr(reaper.subprocess, "run", _fake_tar_zstd_run())

    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    archive = archive_root / "acct" / "cwd" / f"{SESSION_B}.tar.zst"
    assert result["archived_count"] == 1
    assert result["archive_file_bytes"] == len(b"archive contents")
    assert result["reclaimed_bytes"] > 0
    assert archive.read_bytes() == b"archive contents"
    assert not session.exists()
    assert tracked.is_dir()
    assert recent.is_dir()


@pytest.mark.parametrize("timeout_stage", ["create", "verify", "list"])
def test_grok_archive_subprocess_timeouts_keep_source_and_continue(
    tmp_path: Path, monkeypatch, timeout_stage: str,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    next_session = session.parent / "z-next-session"
    next_session.mkdir()
    (next_session / "chat.json").write_text("next", encoding="utf-8")
    _set_tree_mtime(next_session, (NOW - dt.timedelta(days=8)).timestamp())
    _clear_session_open_state(monkeypatch)
    archive_entries = {}
    calls = []

    def timeout_then_continue(args, **kwargs):
        timeout = kwargs.get("timeout")
        calls.append((args, timeout))
        assert (
            reaper._GROK_ARCHIVE_TIMEOUT_FLOOR_SECONDS
            <= timeout <= reaper._GROK_ARCHIVE_TIMEOUT_CAP_SECONDS
        )
        is_timed_session = SESSION_B in str(args)
        if args[0] == "tar" and "-cf" in args:
            archive = Path(args[args.index("-cf") + 1])
            cwd = Path(args[args.index("-C") + 1])
            source = cwd / args[-1]
            archive.write_bytes(b"partial archive" if is_timed_session else b"verified archive")
            if is_timed_session and timeout_stage == "create":
                raise subprocess.TimeoutExpired(args, timeout)
            archive_entries[archive] = sorted(reaper._tree_snapshot(source)["entries"])
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[0] == "zstd":
            if is_timed_session and timeout_stage == "verify":
                raise subprocess.TimeoutExpired(args, timeout)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[0] == "tar" and "-tf" in args:
            if is_timed_session and timeout_stage == "list":
                raise subprocess.TimeoutExpired(args, timeout)
            members = archive_entries[Path(args[-1])]
            return subprocess.CompletedProcess(args, 0, "\n".join(members) + "\n", "")
        raise AssertionError(f"unexpected archive command: {args}")

    monkeypatch.setattr(reaper.subprocess, "run", timeout_then_continue)

    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    timed_archive = archive_root / "acct" / "cwd" / f"{SESSION_B}.tar.zst"
    next_archive = archive_root / "acct" / "cwd" / "z-next-session.tar.zst"
    assert session.is_dir()
    assert not timed_archive.exists()
    assert not Path(f"{timed_archive}.partial").exists()
    assert not next_session.exists()
    assert next_archive.is_file()
    assert result["kept_reasons"]["archive_timeout"] == 1
    assert result["archived_count"] == 1
    assert any(
        (
            (timeout_stage == "create" and args[0] == "tar" and "-cf" in args)
            or (timeout_stage == "verify" and args[0] == "zstd")
            or (timeout_stage == "list" and args[0] == "tar" and "-tf" in args)
        )
        and SESSION_B in str(args)
        for args, _timeout in calls
    )


def test_grok_orphan_archive_keeps_session_when_zstd_verification_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    _clear_session_open_state(monkeypatch)
    monkeypatch.setattr(
        reaper.subprocess, "run", _fake_tar_zstd_run(fail_zstd=True),
    )

    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    assert session.is_dir()
    assert result["archived_count"] == 0
    assert result["kept_reasons"]["archive_verification_failed"] == 1
    assert not (archive_root / "acct" / "cwd" / f"{SESSION_B}.tar.zst").exists()
    assert not (archive_root / "acct" / "cwd" / f"{SESSION_B}.tar.zst.partial").exists()


def test_grok_orphan_archive_keeps_session_when_member_list_mismatches(
    tmp_path: Path, monkeypatch,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    _clear_session_open_state(monkeypatch)
    monkeypatch.setattr(
        reaper.subprocess, "run", _fake_tar_zstd_run(member_mismatch=True),
    )

    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    assert session.is_dir()
    assert result["archived_count"] == 0
    assert result["kept_reasons"]["archive_verification_failed"] == 1
    assert not (archive_root / "acct" / "cwd" / f"{SESSION_B}.tar.zst.partial").exists()


@pytest.mark.parametrize("reason", ["lsof_hit", "lsof_error"])
def test_grok_orphan_archive_fails_closed_on_initial_open_state(
    tmp_path: Path, monkeypatch, reason: str,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    calls = []

    def open_state(paths, *, deadline=None):
        calls.append(list(paths))
        return {os.path.abspath(path): (False, reason) for path in paths}

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", open_state)
    monkeypatch.setattr(reaper.subprocess, "run", _fake_tar_zstd_run())

    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    assert calls == [[session]]
    assert session.is_dir()
    assert result["archived_count"] == 0
    expected = "session_open" if reason == "lsof_hit" else reason
    assert result["kept_reasons"][expected] == 1


def test_grok_orphan_archive_never_follows_session_symlinks(
    tmp_path: Path, monkeypatch,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside-session-target"
    outside.mkdir()
    outside_file = outside / "keep.txt"
    outside_file.write_text("outside", encoding="utf-8")
    (session / "link-to-outside").symlink_to(outside, target_is_directory=True)
    root_link = session.parent / "session-link"
    root_link.symlink_to(outside, target_is_directory=True)
    _set_tree_mtime(session, (NOW - dt.timedelta(days=8)).timestamp())
    _clear_session_open_state(monkeypatch)
    monkeypatch.setattr(reaper.subprocess, "run", _fake_tar_zstd_run())

    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    assert result["archived_count"] == 1
    assert not session.exists()
    assert root_link.is_symlink()
    assert outside_file.read_text(encoding="utf-8") == "outside"


def test_grok_orphan_archive_skips_symlinked_session_root_before_snapshot(
    tmp_path: Path, monkeypatch,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside-session-root"
    outside.mkdir()
    root_link = session.parent / "symlink-session"
    root_link.symlink_to(outside, target_is_directory=True)
    _set_tree_mtime(session, (NOW - dt.timedelta(days=1)).timestamp())
    original_snapshot = reaper._tree_snapshot
    snapshot_calls = []

    def accept_symlink_root(path):
        path = Path(path)
        if path == root_link:
            snapshot_calls.append(path)
            return {
                "entries": {root_link.name}, "fingerprints": {root_link.name: ("same",)},
                "newest_mtime": (NOW - dt.timedelta(days=8)).timestamp(),
                "allocated_bytes": 1, "has_symlink": False,
            }
        return original_snapshot(path)

    monkeypatch.setattr(reaper, "_tree_snapshot", accept_symlink_root)
    _clear_session_open_state(monkeypatch)
    tar_run = _fake_tar_zstd_run()
    archive_commands = []

    def record_archive_command(args, **kwargs):
        archive_commands.append(args)
        return tar_run(args, **kwargs)

    monkeypatch.setattr(reaper.subprocess, "run", record_archive_command)
    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    assert snapshot_calls == []
    assert archive_commands == []
    assert root_link.is_symlink()
    assert session.is_dir()
    assert result["archived_count"] == 0
    assert result["kept_reasons"]["symlink_session"] == 1


def test_grok_archive_real_tar_stores_symlink_without_target_contents(
    tmp_path: Path, monkeypatch,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside-tar-target"
    outside.mkdir()
    outside_file = outside / "private-content.txt"
    outside_file.write_text("outside-only", encoding="utf-8")
    link = session / "link-to-outside"
    link.symlink_to(outside, target_is_directory=True)
    _set_tree_mtime(session, (NOW - dt.timedelta(days=8)).timestamp())
    _clear_session_open_state(monkeypatch)
    tar_path = shutil.which("tar")
    if tar_path is None:
        pytest.skip("tar is not installed on this host")
    real_run = subprocess.run

    probe_dir = tmp_path / "tar-capability-probe"
    probe_dir.mkdir()
    probe_tree = probe_dir / "session-tree"
    (probe_tree / "nested").mkdir(parents=True)
    (probe_tree / "nested" / "fixture.txt").write_text(
        "controlled tar input\n", encoding="utf-8",
    )
    probe_outside = tmp_path / "tar-capability-target"
    probe_outside.mkdir()
    (probe_outside / "outside.txt").write_text("outside-only", encoding="utf-8")
    (probe_tree / "link-to-outside").symlink_to(
        probe_outside, target_is_directory=True,
    )
    probe_archive = tmp_path / "tar-capability-probe.tar.partial"
    probe_archive.write_text("preexisting partial\n", encoding="utf-8")
    try:
        probe = real_run(
            [
                tar_path, "-cf", str(probe_archive), "-C", str(probe_dir),
                probe_tree.name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except PermissionError:
        pytest.skip("sandbox denies exec of tar")
    except FileNotFoundError as exc:
        raise AssertionError("tar disappeared after PATH resolution") from exc
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("tar capability probe timed out") from exc
    if probe.returncode != 0:
        output = f"{probe.stdout or ''}\n{probe.stderr or ''}".lower()
        if "operation not permitted" in output or "permission denied" in output:
            pytest.skip("sandbox denies tar archive access")
        raise AssertionError(
            "tar capability probe failed without a sandbox refusal: "
            f"exit={probe.returncode}, stdout={probe.stdout!r}, stderr={probe.stderr!r}"
        )
    assert probe_archive.is_file() and probe_archive.stat().st_size > len(
        "preexisting partial\n"
    ), "tar capability probe did not overwrite its controlled partial archive"

    try:
        listing = real_run(
            [tar_path, "-tf", str(probe_archive)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except PermissionError:
        pytest.skip("sandbox denies exec of tar")
    except FileNotFoundError as exc:
        raise AssertionError("tar disappeared after PATH resolution") from exc
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("tar member-list capability probe timed out") from exc
    if listing.returncode != 0:
        output = f"{listing.stdout or ''}\n{listing.stderr or ''}".lower()
        if "operation not permitted" in output or "permission denied" in output:
            pytest.skip("sandbox denies tar archive access")
        raise AssertionError(
            "tar member-list capability probe failed without a sandbox refusal: "
            f"exit={listing.returncode}, stdout={listing.stdout!r}, "
            f"stderr={listing.stderr!r}"
        )
    probe_members = {member.rstrip("/") for member in listing.stdout.splitlines()}
    expected_probe_members = {
        probe_tree.name,
        f"{probe_tree.name}/nested",
        f"{probe_tree.name}/nested/fixture.txt",
        f"{probe_tree.name}/link-to-outside",
    }
    assert probe_members == expected_probe_members, (
        "tar capability probe did not list the controlled tree and symlink: "
        f"{probe_members!r}"
    )
    assert all("outside.txt" not in member for member in probe_members)

    def real_tar_without_zstd(args, **kwargs):
        if args[0] == "zstd":
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[0] == "tar":
            # Use the platform's real tar for traversal and member verification;
            # compression is stubbed so this checks only symlink handling.
            tar_args = [arg for arg in args[1:] if arg != "--zstd"]
            return real_run([tar_path, *tar_args], **kwargs)
        raise AssertionError(f"unexpected archive command: {args}")

    monkeypatch.setattr(reaper.subprocess, "run", real_tar_without_zstd)
    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    archive = archive_root / "acct" / "cwd" / f"{SESSION_B}.tar.zst"
    listing = real_run(
        [tar_path, "-tf", str(archive)], check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    members = {member.rstrip("/") for member in listing}
    assert result["archived_count"] == 1
    assert f"{SESSION_B}/link-to-outside" in members
    assert all("private-content.txt" not in member for member in members)
    verbose = real_run(
        [tar_path, "-tvf", str(archive)], check=True, capture_output=True, text=True,
    ).stdout
    assert "link-to-outside ->" in verbose
    assert all("private-content.txt" not in line for line in verbose.splitlines())
    assert outside_file.read_text(encoding="utf-8") == "outside-only"


def test_grok_orphan_archive_refuses_unreadable_ledger_row(
    tmp_path: Path, monkeypatch,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    (ledger / "unreadable.json").write_text("{", encoding="utf-8")
    _clear_session_open_state(monkeypatch)

    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    assert result["refused"] is True
    assert result["refusal_reason"] == "ledger unreadable or incomplete"
    assert session.is_dir()


def test_grok_orphan_archive_refuses_conflicting_session_identity(
    tmp_path: Path, monkeypatch,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    _record(
        ledger, "conflicting-grok", engine="grok",
        engine_session_id=SESSION_A, codex_session_id=SESSION_B,
    )
    _clear_session_open_state(monkeypatch)

    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    assert result["refused"] is True
    assert result["refusal_reason"] == "session identity indeterminate"
    assert session.is_dir()


def test_grok_orphan_archive_keeps_session_when_zstd_is_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    _clear_session_open_state(monkeypatch)

    def missing_zstd(args, **_kwargs):
        if args[0] == "tar" and "-cf" in args:
            archive = Path(args[args.index("-cf") + 1])
            cwd = Path(args[args.index("-C") + 1])
            session_dir = cwd / args[-1]
            archive.write_bytes(b"archive contents")
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[0] == "zstd":
            raise FileNotFoundError("zstd not found")
        raise AssertionError(f"unexpected archive command: {args}")

    monkeypatch.setattr(reaper.subprocess, "run", missing_zstd)
    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    assert session.is_dir()
    assert result["archived_count"] == 0
    assert result["kept_reasons"]["archive_verification_failed"] == 1


def test_grok_orphan_archive_refuses_empty_session_id_ledger(
    tmp_path: Path, monkeypatch,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    for record_path in ledger.glob("*.json"):
        record_path.unlink()

    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    assert result["refused"] is True
    assert result["refusal_reason"] == "ledger returned no session ids"
    assert session.is_dir()


@pytest.mark.parametrize("change,open_after", [(True, False), (False, True)])
def test_grok_orphan_archive_rechecks_changes_and_open_handles(
    tmp_path: Path, monkeypatch, change: bool, open_after: bool,
) -> None:
    accounts, archive_root, session, ledger = _grok_archive_fixture(tmp_path, monkeypatch)
    open_calls = []

    def open_state(paths, *, deadline=None):
        open_calls.append(list(paths))
        opened = open_after and len(open_calls) == 2
        return {
            os.path.abspath(path): (not opened, "lsof_hit" if opened else "")
            for path in paths
        }

    monkeypatch.setattr(reaper, "_legacy_scratch_process_snapshot", open_state)
    monkeypatch.setattr(
        reaper.subprocess, "run", _fake_tar_zstd_run(change=change),
    )

    result = reaper._run_grok_orphan_archive(
        ledger_dir=ledger, now=NOW, delete=True,
        accounts_dir=accounts, archive_root=archive_root,
    )

    assert len(open_calls) == 2
    assert session.is_dir()
    assert result["archived_count"] == 0
    expected = "session_open_after_archive" if open_after else "session_changed_during_archive"
    assert result["kept_reasons"][expected] == 1
    assert not (archive_root / "acct" / "cwd" / f"{SESSION_B}.tar.zst").exists()


def test_dispatch_retention_reports_housekeeping_counts(
    tmp_path: Path, monkeypatch,
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    codex = {"trimmed_count": 2, "trimmed_bytes": 30, "deleted_count": 1, "deleted_bytes": 40}
    grok = {"archived_count": 3, "archived_bytes": 50, "archive_file_bytes": 20}
    monkeypatch.setattr(reaper, "_run_codex_home_maintenance", lambda **_kwargs: codex)
    monkeypatch.setattr(reaper, "_run_grok_orphan_archive", lambda **_kwargs: grok)

    report = reaper._run_dispatch_retention(
        homes_dir=homes, ledger_dir=ledger, dispatch_dir=dispatch,
        pidfile_dir=pidfiles, now=NOW, identity_probe=_dead,
        delete=False, include_housekeeping=True,
    )

    assert report["codex_home_maintenance"] == codex
    assert report["grok_orphan_archive"] == grok


def test_sweep_deletion_matches_deployed_base(tmp_path: Path, monkeypatch) -> None:
    source = subprocess.check_output(
        ["git", "show", "aa130f1b:scripts/goalflight_reap_dispatch_homes.py"],
        cwd=ROOT, text=True,
    )
    baseline: dict[str, object] = {"__name__": "goalflight_reaper_deployed_base"}
    exec(compile(source, "goalflight_reaper_deployed_base.py", "exec"), baseline)
    revised: dict[str, object] = {"__name__": "goalflight_reaper_revised"}
    revised_source = Path(reaper.__file__).read_text(encoding="utf-8")
    exec(compile(revised_source, "goalflight_reaper_revised.py", "exec"), revised)

    dead_worker = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    dead_pid = dead_worker.pid
    dead_worker.wait(timeout=10)
    live_worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.05)
        live_identity = reaper.goalflight_ledger.process_identity(live_worker.pid)
        assert live_identity is not None
    except BaseException:
        live_worker.terminate()
        live_worker.wait(timeout=10)
        raise

    def stable_identity(record: dict) -> tuple[bool, str]:
        if record.get("worker_pid") == live_worker.pid:
            return True, "live"
        return False, "dead"

    def fixture(root: Path):
        homes = root / "codex-state" / "dispatch-homes"
        ledger = root / "state" / "runs.d"
        dispatch = root / "state" / "dispatch"
        pidfiles = root / "pidfiles"
        legacy = root / "legacy"
        task_store = root / "task-store"
        project_roots = (root / "project-a", root / "project-b")
        for path in (homes, ledger, dispatch, pidfiles, legacy, task_store):
            path.mkdir(parents=True)
        for project_root in project_roots:
            project_root.mkdir()
            _cow_git("init", "--quiet", "-b", "main", cwd=project_root)
            _cow_git("config", "user.name", "Goal Flight Test", cwd=project_root)
            _cow_git("config", "user.email", "test@example.invalid", cwd=project_root)
            _cow_git("commit", "--quiet", "--allow-empty", "-m", "base", cwd=project_root)
        import goalflight_worktree_pool

        def add_managed_worktree(project_root: Path, dispatch_id: str) -> Path:
            lease = goalflight_worktree_pool.acquire_worktree_seat(
                project_root, dispatch_id,
            )
            path = lease.path
            lease.release()
            return path

        removable = add_managed_worktree(project_roots[0], "gc-removable")
        late_owned = add_managed_worktree(project_roots[0], "gc-late-owned")
        _mark_managed_seat_reclaimable(
            ledger, project_roots[0], removable, "gc-removable",
        )
        _mark_managed_seat_reclaimable(
            ledger, project_roots[0], late_owned, "gc-late-owned",
        )
        live_owned = add_managed_worktree(project_roots[1], "gc-live-owner")
        fixtures = (
            ("old", dt.timedelta(days=5), dead_pid, None, project_roots[0]),
            ("recent", dt.timedelta(hours=12), dead_pid, None, project_roots[1]),
            ("live", dt.timedelta(days=5), live_worker.pid, live_identity, project_roots[1]),
        )
        # Codex-home cleanup has its own tests; this comparison isolates other artifacts.
        for dispatch_id, age, worker_pid, worker_identity, project_root in fixtures:
            record = {
                "schema": "goalflight.dispatch.v1", "dispatch_id": dispatch_id,
                "state": "complete", "ended_at": (NOW - age).isoformat().replace("+00:00", "Z"),
                "worker_pid": worker_pid,
                "project_root": str(project_root),
            }
            if worker_identity is not None:
                record["worker_identity"] = worker_identity
            filename = reaper.goalflight_compat.safe_dispatch_filename(dispatch_id)
            (ledger / f"{filename}.json").write_text(json.dumps(record), encoding="utf-8")
            (dispatch / f"{dispatch_id}.tail").write_text("tail", encoding="utf-8")
            scratch = dispatch / f"{dispatch_id}.scratch"
            scratch.mkdir()
            scratch_time = (NOW - age).timestamp()
            os.utime(scratch, (scratch_time, scratch_time))
        owner = {
            "schema": "goalflight.dispatch.v1",
            "dispatch_id": "gc-live-owner",
            "state": "running",
            "worker_pid": live_worker.pid,
            "worker_identity": live_identity,
            "worker_cwd": str(live_owned),
            "project_root": str(project_roots[1]),
        }
        owner_name = reaper.goalflight_compat.safe_dispatch_filename("gc-live-owner")
        (ledger / f"{owner_name}.json").write_text(json.dumps(owner), encoding="utf-8")
        (legacy / "old").mkdir()
        (legacy / "old" / "scratch.dat").write_bytes(b"legacy scratch")
        return (
            homes, ledger, dispatch, pidfiles, legacy, root,
            removable, live_owned, late_owned, project_roots,
        )

    def run(module_globals: dict[str, object], root: Path):
        calls: list[str] = []
        gc_ledger_reads: list[Path] = []
        gc_reports: list[dict[str, object]] = []
        module_globals["DISPATCH_ARTIFACT_BUDGET_BYTES"] = 0
        delete = module_globals["_delete_artifact"]

        def recording_delete(path, kind):
            calls.append(Path(path).relative_to(relative_root).as_posix())
            return delete(path, kind)

        module_globals["_delete_artifact"] = recording_delete
        import goalflight_worktree_gc
        import goalflight_worktree_pool
        import goalflight_seat_dedupe
        import goalflight_task

        with monkeypatch.context() as isolated:
            isolated.setenv("GOALFLIGHT_STATE_DIR", str(root / "state"))
            isolated.setenv("GOALFLIGHT_DISPATCH_DIR", str(root / "state" / "dispatch"))
            isolated.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(root / "codex-state"))
            isolated.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(root / "pidfiles"))
            isolated.setenv("GOALFLIGHT_PIDFILE_DIR", str(root / "pidfiles"))
            isolated.setenv("GOALFLIGHT_TASK_STORE_DIR", str(root / "task-store"))
            homes, ledger, dispatch, pidfiles, legacy, relative_root, removable, live_owned, late_owned, project_roots = fixture(root)
            late_owner_added = False
            original_index = goalflight_worktree_gc.ledger_index_for_dir

            def count_ledger_read(path, *args, **kwargs):
                nonlocal late_owner_added
                index = original_index(path, *args, **kwargs)
                gc_ledger_reads.append(Path(path))
                if not late_owner_added:
                    owner = {
                        "schema": "goalflight.dispatch.v1",
                        "dispatch_id": "gc-late-owner",
                        "state": "running",
                        "worker_pid": live_worker.pid,
                        "worker_identity": live_identity,
                        "worker_cwd": str(late_owned),
                        "project_root": str(project_roots[0]),
                    }
                    owner_name = reaper.goalflight_compat.safe_dispatch_filename(
                        "gc-late-owner"
                    )
                    (ledger / f"{owner_name}.json").write_text(
                        json.dumps(owner), encoding="utf-8",
                    )
                    late_owner_added = True
                return index

            def capture_gc_report(*args, **kwargs):
                result = original_gc_main(*args, **kwargs)
                try:
                    gc_reports.append(json.loads(sys.stdout.getvalue()))
                except (AttributeError, json.JSONDecodeError):
                    pass
                return result

            module_globals["_legacy_claude_scratch_dir"] = lambda: legacy
            module_globals["_legacy_scratch_process_gate"] = lambda _path: (True, "")
            module_globals["_legacy_scratch_process_snapshot"] = lambda paths, *, deadline=None: {
                os.path.abspath(path): (True, "") for path in paths
            }
            original_gc_main = goalflight_worktree_gc.main
            isolated.setattr(
                goalflight_worktree_pool, "_process_cwd_snapshot",
                lambda *, timeout_s, deadline=None: [],
            )
            isolated.setattr(goalflight_worktree_gc, "ledger_index_for_dir", count_ledger_read)
            isolated.setattr(goalflight_worktree_gc, "main", capture_gc_report)
            isolated.setattr(
                goalflight_worktree_pool, "reap_read_only_worktrees",
                lambda _root, *, deadline=None: None,
            )
            isolated.setattr(goalflight_task, "read_project_registry", lambda: [])
            isolated.setattr(
                goalflight_task, "resolve_state_base_dir", lambda: root / "task-store"
            )
            isolated.setattr(
                goalflight_seat_dedupe, "_dedupe_pools",
                lambda roots, **_kwargs: {
                    "seats_scanned": 0, "files_cloned": 0,
                    "bytes_shared": 0, "errors": [],
                },
            )
            report = module_globals["_run_sweep_body"](
                homes_dir=homes, ledger_dir=ledger, now=NOW,
                identity_probe=stable_identity,
            )
        artifacts = report["artifacts"]
        accounting = {
            key: artifacts[key] for key in (
                "budget_measurable", "legacy_eligible_count",
                "legacy_eligible_bytes", "legacy_deleted_count",
                "legacy_deleted_bytes", "legacy_would_delete_count",
                "legacy_would_delete_bytes", "legacy_kept_reasons",
            )
        }
        gc_entries = []
        for gc_report in gc_reports:
            for entry in gc_report.get("entries", []):
                path = Path(entry["path"])
                conditions = entry.get("conditions", {})
                unowned = conditions.get("unowned", {})
                gc_entries.append({
                    "path": path.relative_to(relative_root).as_posix(),
                    "decision": entry.get("decision"),
                    "reason": str(entry.get("reason", "")).replace(
                        str(relative_root), "<root>"
                    ),
                    "outcome": entry.get("outcome"),
                    "unowned_verdict": unowned.get("verdict"),
                    "unowned_reason": str(unowned.get("reason", "")).replace(
                        str(relative_root), "<root>"
                    ),
                })
        sweep = {
            "worktree_gc": report["worktree_gc"],
            "worktree_gc_removals": sorted(
                Path(path).relative_to(relative_root).as_posix()
                for path in report["worktree_gc_removals"]
            ),
            "worktree_gc_entries": sorted(gc_entries, key=lambda entry: entry["path"]),
            "cleanup_errors": report["cleanup_errors"],
        }
        expected_worktrees = {
            name: path.relative_to(relative_root).as_posix()
            for name, path in (
                ("removable", removable),
                ("live_owned", live_owned),
                ("late_owned", late_owned),
            )
        }
        outside_dispatch_gc = [
            path for path in calls if not path.startswith("state/dispatch/")
        ]
        dispatch_gc_deletions = [
            path for path in calls if path.startswith("state/dispatch/")
        ]
        return (
            sorted(outside_dispatch_gc), accounting, sweep, gc_ledger_reads,
            expected_worktrees, sorted(dispatch_gc_deletions), artifacts.get("dispatch_gc"),
        )

    try:
        deployed = run(baseline, tmp_path / "deployed")
        actual = run(revised, tmp_path / "revised")
    finally:
        live_worker.terminate()
        live_worker.wait(timeout=10)
    assert actual[:3] == deployed[:3]
    assert len(deployed[3]) == 4
    assert len(actual[3]) == 3
    assert all(path == tmp_path / "deployed" / "state" / "runs.d" for path in deployed[3])
    assert all(path == tmp_path / "revised" / "state" / "runs.d" for path in actual[3])
    assert deployed[4] == actual[4]
    assert actual[5] == []
    assert actual[6]["mode"] == "dry-run"
    assert actual[6]["removed_count"] == 0
    assert actual[6]["compressed_count"] == 0
    for result in (deployed, actual):
        removals = result[2]["worktree_gc_removals"]
        assert removals == [result[4]["removable"]]
        entries = {entry["path"]: entry for entry in result[2]["worktree_gc_entries"]}
        assert entries[result[4]["removable"]]["outcome"] == "removed"
        live_entry = entries[result[4]["live_owned"]]
        assert live_entry["decision"] == "retain"
        assert live_entry["unowned_verdict"] == "no"
        late_entry = entries[result[4]["late_owned"]]
        assert late_entry["decision"] == "remove"
        assert late_entry["outcome"] == "retained"
        assert "changed_before_remove" in late_entry["reason"]
        assert "gc-late-owner" in late_entry["reason"]


@pytest.mark.parametrize("worker_pgid", [None, 4242])
def test_dispatch_scratch_counts_toward_budget_and_is_deleted_with_dispatch(
    tmp_path: Path, monkeypatch, worker_pgid: int | None
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    dispatch_id = "old:worker"
    scratch = _scratch_dir(
        dispatch, dispatch_id, files={"checkout-marker": b"scratch" * 256},
    )
    scratch_bytes = reaper.allocated_tree_bytes(scratch)
    fields = {"worker_pgid": worker_pgid} if worker_pgid is not None else {}
    _record(ledger, dispatch_id, **fields)
    killpg_calls = []

    def group_gone(pgid, sig):
        killpg_calls.append((pgid, sig))
        raise ProcessLookupError()

    monkeypatch.setattr(reaper.os, "killpg", group_gone)

    report = _run(homes, ledger, dispatch, pidfiles)

    assert scratch.is_dir()
    assert report["scratch_deleted_count"] == 0
    assert report["scratch_eligible_bytes"] == scratch_bytes
    assert report["total_allocated_bytes"] == scratch_bytes
    if worker_pgid is None:
        assert report["scratch_would_delete_count"] == 1
        assert report["scratch_would_delete_bytes"] == scratch_bytes
    else:
        assert report["scratch_would_delete_count"] == 0
        assert report["scratch_would_delete_bytes"] == 0
        assert (
            report["scratch_kept_reasons"]["process_group_liveness_indeterminate"] == 1
        )
    assert killpg_calls == []


def test_scratch_short_minimum_age_is_rechecked_under_lock(
    tmp_path: Path, monkeypatch
) -> None:
    homes, ledger, dispatch, pidfiles = _fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(reaper, "DISPATCH_ARTIFACT_BUDGET_BYTES", 0)
    dispatch_id = "locked-seven-hour-scratch"
    scratch = _scratch_dir(dispatch, dispatch_id)
    tail = dispatch / f"{dispatch_id}.tail"
    tail.write_bytes(b"tail")
    home = _home(homes, dispatch_id)
    record_path = ledger / f"{dispatch_id}.json"
    _record(ledger, dispatch_id, ended_at=NOW - dt.timedelta(hours=30))
    updated = False

    def age_moves_during_scan(_record: dict) -> tuple[bool, str]:
        nonlocal updated
        if not updated:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["ended_at"] = (NOW - dt.timedelta(hours=7)).isoformat().replace("+00:00", "Z")
            record_path.write_text(json.dumps(record), encoding="utf-8")
            updated = True
        return _dead(_record)

    report = _run(homes, ledger, dispatch, pidfiles, probe=age_moves_during_scan)

    assert updated
    assert scratch.is_dir()
    assert home.is_dir() and tail.is_file()
    assert report["scratch_would_delete_count"] == 1
    assert report["scratch_deleted_count"] == 0
