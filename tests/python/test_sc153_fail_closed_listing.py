#!/usr/bin/env python3
"""SC-153: unreadable / empty / missing is not permission to act.

Every case constructs the actually-unreadable condition (chmod 000 directory
or child of an unsearchable parent, empty controller label, token-unbound
terminal row). Mocks that raise where glob returns [] are forbidden here.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import socket
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_adapter_readiness as readiness  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_fs as fs  # noqa: E402
import goalflight_ledger as L  # noqa: E402
import goalflight_watch as W  # noqa: E402
import goalflight_worktree_gc as gc  # noqa: E402


pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 is a POSIX probe"),
    pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses mode 000",
    ),
]


@contextlib.contextmanager
def _mode(path: Path, mode: int, restore: int = 0o700):
    os.chmod(path, mode)
    try:
        yield
    finally:
        os.chmod(path, restore)


def test_primitive_glob_empty_is_unreadable_not_absent(tmp_path: Path) -> None:
    directory = tmp_path / "runs.d"
    directory.mkdir()
    (directory / "live.json").write_text("{}", encoding="utf-8")
    with _mode(directory, 0o000):
        assert list(directory.glob("*.json")) == []
        state, entries = fs.list_dir(directory)
        assert state == "unreadable"
        assert entries == []
        suffix_state, suffix_entries = fs.list_dir_suffix(directory, ".json")
        assert suffix_state == "unreadable"
        assert suffix_entries == []
        assert fs.path_presence(directory) == "present"


def test_primitive_child_of_unsearchable_parent_is_unknown(tmp_path: Path) -> None:
    parent = tmp_path / "hidden"
    parent.mkdir()
    child = parent / "row.json"
    child.write_text("{}", encoding="utf-8")
    with _mode(parent, 0o000):
        assert child.exists() is False
        assert fs.path_presence(child) == "unknown"
        state, entries = fs.list_dir(parent)
        assert state == "unreadable"
        assert entries == []


def test_primitive_missing_dir_is_absent_not_unreadable(tmp_path: Path) -> None:
    missing = tmp_path / "no-such"
    assert fs.path_presence(missing) == "absent"
    state, entries = fs.list_dir(missing)
    assert state == "absent"
    assert entries == []


def test_dangling_symlink_runs_d_is_unreadable_not_unowned(tmp_path: Path) -> None:
    """P1: present dangling symlink is unreadable, not absent.

    lstat of the symlink inode succeeds; iterdir follows and raises
    FileNotFoundError. Mapping that to absent licenses check_unowned yes
    and would delete the live worker's tree.
    """
    target = tmp_path / "real-ledger"
    target.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    (target / "live.json").write_text(
        json.dumps(
            {
                "dispatch_id": "live-owner",
                "state": "running",
                "worker_cwd": str(wt),
            }
        ),
        encoding="utf-8",
    )
    runs = tmp_path / "runs.d"
    runs.symlink_to(target)
    assert fs.path_presence(runs) == "present"
    live_state, live_entries = fs.list_dir(runs)
    assert live_state == "ok"
    assert any(entry.name == "live.json" for entry in live_entries)
    assert gc.check_unowned(str(wt), runs)["verdict"] == "no"
    target.rename(tmp_path / "real-ledger-gone")
    revert_failure = ""
    try:
        assert fs.path_presence(runs) == "present"
        state, entries = fs.list_dir(runs)
        assert state == "unreadable"
        assert entries == []
        suffix_state, suffix_entries = fs.list_dir_suffix(runs, ".json")
        assert suffix_state == "unreadable"
        assert suffix_entries == []
        records, unreadable = gc.read_ledger_records(runs)
        assert records == []
        assert unreadable == [str(runs)]
        verdict = gc.check_unowned(str(wt), runs)
        assert verdict["verdict"] != "yes"
        assert verdict["verdict"] == "unknown"
        assert "unreadable" in verdict["reason"]
        missing = tmp_path / "no-such-runs.d"
        assert fs.list_dir(missing)[0] == "absent"
    finally:
        try:
            runs.unlink()
        except OSError as exc:
            revert_failure = f"unlink {runs}: {exc}"
    assert revert_failure == "", revert_failure


def test_primitive_symlink_loop_is_unreadable(tmp_path: Path) -> None:
    loop = tmp_path / "runs.d"
    revert_failure = ""
    try:
        loop.symlink_to(loop)
        assert fs.path_presence(loop) == "present"
        state, entries = fs.list_dir(loop)
        assert state == "unreadable"
        assert entries == []
    finally:
        try:
            loop.unlink()
        except OSError as exc:
            revert_failure = f"unlink {loop}: {exc}"
    assert revert_failure == "", revert_failure


def test_primitive_file_where_dir_expected_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "runs.d"
    path.write_text("not a directory\n", encoding="utf-8")
    assert fs.path_presence(path) == "present"
    state, entries = fs.list_dir(path)
    assert state == "unreadable"
    assert entries == []


def test_primitive_removed_between_lstat_and_iterdir_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: the directory inode itself vanished after a successful lstat."""
    directory = tmp_path / "runs.d"
    directory.mkdir()
    original_lstat = os.lstat
    first = {"pending": True}

    def lstat_then_remove(path: object, *args: object, **kwargs: object):
        result = original_lstat(path, *args, **kwargs)
        if first["pending"] and os.fspath(path) == os.fspath(directory):
            first["pending"] = False
            directory.rmdir()
        return result

    monkeypatch.setattr(os, "lstat", lstat_then_remove)
    state, entries = fs.list_dir(directory)
    assert state == "absent"
    assert entries == []
    assert fs.path_presence(directory) == "absent"


def test_primitive_empty_readable_dir_still_unowned(tmp_path: Path) -> None:
    directory = tmp_path / "runs.d"
    directory.mkdir()
    state, entries = fs.list_dir(directory)
    assert state == "ok"
    assert entries == []
    wt = tmp_path / "wt"
    wt.mkdir()
    verdict = gc.check_unowned(str(wt), directory)
    assert verdict["verdict"] == "yes"
    assert "no non-terminal dispatch records this path" in verdict["reason"]


def test_c1_unlistable_ledger_dir_is_not_unowned(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "runs.d"
    ledger_dir.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    (ledger_dir / "live.json").write_text(
        json.dumps(
            {
                "dispatch_id": "live-owner",
                "state": "running",
                "worker_cwd": str(wt),
            }
        ),
        encoding="utf-8",
    )
    with _mode(ledger_dir, 0o000):
        records, unreadable = gc.read_ledger_records(ledger_dir)
        assert records == []
        assert unreadable == [str(ledger_dir)]
        verdict = gc.check_unowned(str(wt), ledger_dir)
    assert verdict["verdict"] == "unknown"
    assert "unreadable" in verdict["reason"]


def test_c1_healthy_readable_ledger_still_reports_owner(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "runs.d"
    ledger_dir.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    (ledger_dir / "live.json").write_text(
        json.dumps(
            {
                "dispatch_id": "live-owner",
                "state": "running",
                "worker_cwd": str(wt),
            }
        ),
        encoding="utf-8",
    )
    records, unreadable = gc.read_ledger_records(ledger_dir)
    assert len(records) == 1
    assert unreadable == []
    verdict = gc.check_unowned(str(wt), ledger_dir)
    assert verdict["verdict"] == "no"


def test_c2_c7_unreadable_ledger_is_not_retired() -> None:
    dispatch_id = "c7-unreadable"
    path = L.record_path(dispatch_id, create=True)
    path.write_text(
        json.dumps({"dispatch_id": dispatch_id, "state": "running"}),
        encoding="utf-8",
    )
    runs = path.parent
    with _mode(runs, 0o000):
        assert W._dispatch_record_is_nonterminal(dispatch_id) is None


def test_c2_c7_absent_record_is_not_nonterminal() -> None:
    assert W._dispatch_record_is_nonterminal("no-such-dispatch") is False


def test_c2_live_ledger_is_nonterminal() -> None:
    dispatch_id = "c2-live"
    L.write_record(
        {
            "dispatch_id": dispatch_id,
            "state": "running",
            "terminal_state": "unknown",
        }
    )
    assert W._dispatch_record_is_nonterminal(dispatch_id) is True


def test_c3_status_file_absent_is_indeterminate(tmp_path: Path) -> None:
    status = tmp_path / "gone.status.json"
    record = {"dispatch_id": "c3", "status_path": str(status)}
    payload, evidence = D._abandoned_status_payload(record)
    assert payload is None
    assert evidence == "status_file_absent"


def test_c3_status_pointer_absent_still_indeterminate() -> None:
    payload, evidence = D._abandoned_status_payload({"dispatch_id": "c3"})
    assert payload is None
    assert evidence == "status_path_absent"


def test_c4_output_child_of_unsearchable_parent_is_not_absent(tmp_path: Path) -> None:
    parent = tmp_path / "hidden"
    parent.mkdir()
    out = parent / "worker.tail"
    out.write_text("output\n", encoding="utf-8")
    record = {"stdout_path": str(out)}
    with _mode(parent, 0o000):
        readable, evidence = D._abandoned_output_evidence(record)
    assert readable is False
    assert "unsearchable" in evidence


def test_c4_output_genuinely_absent_is_still_absent(tmp_path: Path) -> None:
    record = {"stdout_path": str(tmp_path / "no-such.tail")}
    readable, evidence = D._abandoned_output_evidence(record)
    assert readable is True
    assert evidence == "output_file_absent"


def test_c5_empty_controller_label_is_not_unowned() -> None:
    inactive, evidence = D._abandoned_controller_evidence({})
    assert inactive is False
    assert evidence == "controller_identity_absent"


def test_c6_token_mismatch_is_not_existing_terminal_record() -> None:
    record = {
        "dispatch_id": "reused-id",
        "state": "complete",
        "terminal_state": "complete",
        "queue_launch_token": "attempt-old",
    }
    entry = {
        "dispatch_id": "reused-id",
        "queue_launch_token": "attempt-new",
        "created_at": "2026-08-28T00:00:00+00:00",
    }
    decision = D._entry_completion_authority(entry, record)
    assert decision is None or decision.get("reason") != "existing_terminal_record"


def test_c6_missing_token_defers_instead_of_unlinking() -> None:
    record = {
        "dispatch_id": "reused-id",
        "state": "complete",
        "terminal_state": "complete",
        "queue_launch_token": "attempt-old",
    }
    entry = {"dispatch_id": "reused-id", "created_at": "2026-08-28T00:00:00+00:00"}
    decision = D._entry_completion_authority(entry, record)
    assert decision is not None
    assert decision["state"] == "deferred"
    assert decision["reason"] == "completion_authority_token_indeterminate"


def test_c6_matching_token_still_authorizes_completion() -> None:
    record = {
        "dispatch_id": "same-attempt",
        "state": "complete",
        "terminal_state": "complete",
        "queue_launch_token": "attempt-1",
    }
    entry = {
        "dispatch_id": "same-attempt",
        "queue_launch_token": "attempt-1",
        "created_at": "2026-08-28T00:00:00+00:00",
    }
    decision = D._entry_completion_authority(entry, record)
    assert decision is not None
    assert decision["reason"] == "existing_terminal_record"
    assert decision["state"] == "complete"


def test_brief_c3_live_nameless_missing_project_root_is_unknown() -> None:
    identity = L.process_identity(os.getpid())
    assert identity is not None
    L.write_record(
        {
            "dispatch_id": "rootless-live",
            "state": "running",
            "terminal_state": "unknown",
            "transport": "dispatch",
            "hostname": socket.gethostname(),
            "worker_pid": os.getpid(),
            "worker_identity": identity,
        }
    )
    tree = Path.cwd()
    args = SimpleNamespace(cwd=str(tree), dispatch_id="second-writer")
    occupied, unknown, _state = D._worktree_incumbent_reason(args)
    assert occupied is None
    assert unknown
    assert "no project_root" in unknown


def test_write_record_refuses_unreadable_existing_row(tmp_path: Path) -> None:
    dispatch_id = "overwrite-unreadable"
    path = L.record_path(dispatch_id, create=True)
    path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "state": "complete",
                "ended_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    with _mode(path.parent, 0o000):
        with pytest.raises(OSError, match="unreadable"):
            L.write_record(
                {
                    "dispatch_id": dispatch_id,
                    "state": "running",
                    "ended_at": "2099-01-01T00:00:00+00:00",
                }
            )


def test_write_record_healthy_absent_row_still_creates() -> None:
    dispatch_id = "fresh-row"
    path = L.write_record({"dispatch_id": dispatch_id, "state": "running"})
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["dispatch_id"] == dispatch_id


def test_adapter_manifest_unsearchable_parent_is_unreadable_not_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    (adapters / "grok.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(readiness, "ADAPTERS_DIR", adapters)
    with _mode(adapters, 0o000):
        manifest, reason = readiness.load_manifest_with_reason("grok")
    assert manifest is None
    assert reason == "adapter_manifest_unreadable"
    gate = {
        "allowed": False,
        "reason": reason,
    }
    assert readiness.os_sandbox_refusal_is_retryable(gate) is True


def test_validate_acp_readiness_marks_unreadable_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    (adapters / "grok.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(readiness, "ADAPTERS_DIR", adapters)
    with _mode(adapters, 0o000):
        gate = readiness.validate_acp_dispatch_readiness("grok", ["grok"])
    assert gate is not None
    assert gate["reason"] == "adapter_manifest_unreadable"
    assert gate["retryable"] is True
    assert readiness.os_sandbox_refusal_is_retryable(gate) is True


def test_requeue_unreadable_ledger_preserves_claim(tmp_path: Path) -> None:
    dispatch_id = "requeue-unread"
    path = L.record_path(dispatch_id, create=True)
    path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "state": "blocked_auth",
                "effective_account": "seat",
            }
        ),
        encoding="utf-8",
    )
    txn = SimpleNamespace(queue_locked=True, ledger_locked=True)
    with _mode(path.parent, 0o000):
        keep = D._maybe_requeue_terminal_claim(
            txn,
            {"dispatch_id": dispatch_id, "request": {}},
            queue_dir=tmp_path,
            tail=tmp_path / "tail",
        )
    assert keep is False


def test_unscoped_drain_retains_unreadable_queue_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = Path(os.environ["GOALFLIGHT_STATE_DIR"]) / "dispatch-queue"
    queue.mkdir(parents=True, exist_ok=True)
    broken = queue / "battery.json"
    broken.write_text("{truncated", encoding="utf-8")
    monkeypatch.setattr(D, "_run_drain_prelaunch_hook", lambda _agents: None)
    monkeypatch.setattr(D, "_release_stale_capacity_for_drain", lambda: None)
    payload = D._drain_queue_once(
        argparse.Namespace(
            queue_dir=None,
            remote_node=None,
            capacity_wait_s=0.0,
            claim_stale_s=D.QUEUE_CLAIM_STALE_S,
            limit=0,
        )
    )
    assert broken.exists()
    assert broken.read_text(encoding="utf-8") == "{truncated"
    assert not list(queue.glob("*.failed"))
    assert not list(queue.glob("*.claimed-*"))
    assert payload["failed"] == 0
    assert any(
        item.get("dispatch_id") == "battery"
        and item.get("reason") == "unreadable_queue_entry"
        for item in payload.get("details") or []
    ), payload.get("details")
