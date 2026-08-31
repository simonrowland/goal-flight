#!/usr/bin/env python3
"""Leftover glob/is_dir/restore sites keep unreadable distinct from empty.

Sibling listings of the same queue and journals index still collapsed a failed
read into "nothing found". Decision paths must use iterdir / _lstat_presence;
absent stays empty, unreadable is unknown.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
import goalflight_fleet_console as fleet  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_status as status  # noqa: E402


def _queue(tmp_path: Path) -> Path:
    queue = tmp_path / "parent" / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    return queue


def _envelope(queue: Path, dispatch_id: str) -> Path:
    path = queue / f"{dispatch_id}.json"
    path.write_text(
        json.dumps({"dispatch_id": dispatch_id, "state": "queued"}) + "\n",
        encoding="utf-8",
    )
    return path


def test_parent_unreadable_queue_carrier_is_unknown(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    envelope = _envelope(queue, "disp-parent")
    parent = queue.parent
    assert D._claim_has_active_carrier(queue, "disp-parent").kind is D.ClaimCarrierKind.QUEUED

    os.chmod(parent, 0o000)
    try:
        carrier = D._claim_has_active_carrier(queue, "disp-parent")
        assert carrier.kind is D.ClaimCarrierKind.UNKNOWN, carrier
        assert carrier.reason.startswith("queue_dir_unreadable"), carrier
        summary = D._summarize_claim_markers(queue)
        assert str(summary.get("listing_error") or "").startswith(
            "queue_dir_unreadable"
        ), summary
    finally:
        os.chmod(parent, 0o700)
    assert envelope.is_file()
    assert D._claim_has_active_carrier(queue, "disp-parent").kind is D.ClaimCarrierKind.QUEUED


def test_existing_queue_paths_split_absent_present_unreadable(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    envelope = _envelope(queue, "disp-exist")
    missing = queue / "no-such.json"
    assert D._existing_queue_entry_paths(missing) == []
    assert D._existing_queue_entry_paths(envelope) == [envelope]

    os.chmod(queue, 0o000)
    try:
        with pytest.raises(OSError, match="queue dir unreadable"):
            D._existing_queue_entry_paths(envelope)
        assert D._requeue_child_exists(queue, "disp-exist") is True
    finally:
        os.chmod(queue, 0o700)

    empty = tmp_path / "empty-queue"
    empty.mkdir()
    assert D._requeue_child_exists(empty, "disp-exist") is False


def test_status_queue_depth_unreadable_is_unknown_not_zero(tmp_path: Path) -> None:
    queue = Path(os.environ["GOALFLIGHT_STATE_DIR"]) / "dispatch-queue"
    queue.mkdir(parents=True, exist_ok=True)
    (queue / "pending.json").write_text("{}", encoding="utf-8")
    assert status._dispatch_queue_depth() == 1

    os.chmod(queue, 0o000)
    try:
        assert status._dispatch_queue_depth() is None
        warnings = status._queue_drainer_warnings()
        assert warnings and warnings[0]["code"] == "queue_unreadable"
        assert warnings[0]["queue_depth"] is None
    finally:
        os.chmod(queue, 0o700)

    missing = tmp_path / "no-state"
    os.environ["GOALFLIGHT_STATE_DIR"] = str(missing)
    try:
        assert status._dispatch_queue_depth() == 0
    finally:
        os.environ["GOALFLIGHT_STATE_DIR"] = str(queue.parent)


def test_fleet_console_unreadable_index_is_not_empty_roots(tmp_path: Path) -> None:
    index = Path(os.environ["GOALFLIGHT_JOURNAL_DIR"]) / "journals"
    index.mkdir(parents=True, exist_ok=True)
    project_dir = index / "project-aaaaaaaaaa"
    project_dir.mkdir()
    (project_dir / journal.JOURNAL_FILE_NAME).write_bytes(b"")
    # Readable listing finds the conventional journal path.
    errors: list[str] = []
    roots = fleet._active_controller_roots_from_journals(errors)
    assert isinstance(roots, set)
    # The zero-byte journal used to be silently omitted, so this test passed
    # while encoding the bug. Its unreadability must mark the fleet sample.
    assert errors and errors[0].startswith("controller_journal:")
    metadata = fleet._metadata(
        "fleet",
        generation_id="journal-error",
        started_at="2026-08-31T00:00:00+00:00",
        finished_at="2026-08-31T00:00:01+00:00",
        errors=errors,
    )
    assert metadata["incomplete"] is True

    os.chmod(index, 0o000)
    try:
        with pytest.raises((journal.JournalIOError, OSError)):
            fleet._active_controller_roots_from_journals()
    finally:
        os.chmod(index, 0o700)


def test_fleet_plane_local_status_failure_still_measures_queue(tmp_path: Path) -> None:
    state_dir = Path(os.environ["GOALFLIGHT_STATE_DIR"])
    queue = state_dir / "dispatch-queue"
    queue.mkdir(parents=True, exist_ok=True)
    (queue / "pending.json").write_text(
        json.dumps(
            {
                "dispatch_id": "queued-1",
                "project_root": str(tmp_path / "proj"),
            }
        ),
        encoding="utf-8",
    )
    with (
        mock.patch.object(
            fleet.goalflight_status,
            "status_payload",
            side_effect=RuntimeError("local status unreadable"),
        ),
        mock.patch.object(
            fleet.goalflight_fleet_status_cli,
            "build_fleet_status",
            return_value={},
        ),
        mock.patch.object(fleet.goalflight_usage, "collect_usage", return_value=[]),
        mock.patch.object(fleet.goalflight_task, "read_project_registry", return_value=[]),
        mock.patch.object(fleet, "_active_controller_roots_from_journals", return_value=[]),
    ):
        payload = fleet.build_fleet_plane(generation_id="local-status-failure")
    assert payload["machine"]["queue_depth"] == 1
    assert payload["incomplete"] is True
    assert payload["last_error"]
    assert "local_status" in str(payload["last_error"])


def test_fleet_console_queue_unreadable_or_malformed_is_unknown(tmp_path: Path) -> None:
    upstream_errors: list[str] = []
    by_root, depth = fleet._queue_summary(
        {"dispatch": {"records": []}},
        upstream_errors,
    )
    assert by_root == {}
    assert depth is None
    assert upstream_errors == []

    measured_empty = tmp_path / "measured-empty"
    measured_empty.mkdir()
    empty_errors: list[str] = []
    by_root, depth = fleet._queue_summary(
        {"dispatch": {"state_dir": str(measured_empty)}},
        empty_errors,
    )
    assert by_root == {}
    assert depth == 0
    assert empty_errors == []

    state_dir = tmp_path / "state"
    queue = state_dir / "dispatch-queue"
    queue.mkdir(parents=True)
    entry = queue / "pending.json"
    entry.write_text(
        json.dumps({"dispatch_id": "pending", "project_root": str(tmp_path / "project")}),
        encoding="utf-8",
    )
    machine = {"dispatch": {"state_dir": str(state_dir)}}

    errors: list[str] = []
    os.chmod(queue, 0o000)
    try:
        by_root, depth = fleet._queue_summary(machine, errors)
    finally:
        os.chmod(queue, 0o700)
    assert by_root == {}
    assert depth is None
    assert errors and errors[0].startswith("dispatch_queue:")
    assert fleet._metadata(
        "fleet",
        generation_id="queue-error",
        started_at="2026-08-31T00:00:00+00:00",
        finished_at="2026-08-31T00:00:01+00:00",
        errors=errors,
    )["incomplete"] is True

    entry.write_text("{", encoding="utf-8")
    errors = []
    by_root, depth = fleet._queue_summary(machine, errors)
    assert by_root == {}
    assert depth is None
    assert errors and errors[0].startswith("dispatch_queue_entry:")

    projects = [{"project_id": fleet._project_id(str(tmp_path / "project"))}]
    fleet._attach_queue_rows(projects, by_root, queue_known=depth is not None)
    assert projects[0]["queue"]["depth"] is None


def test_restore_and_create_split_absent_from_unreadable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    authority = journal.Journal.create(root)
    snapshot = tmp_path / "snap.sqlite"
    snapshot.write_bytes(authority.path.read_bytes())
    journal_dir = authority.path.parent
    missing = tmp_path / "missing-project"
    missing.mkdir()
    with pytest.raises(journal.JournalDisappeared, match="absent"):
        journal.restore_snapshot(missing, snapshot, i_understand=True)

    os.chmod(journal_dir, 0o000)
    try:
        with pytest.raises(journal.JournalIOError, match="unverified"):
            journal.restore_snapshot(root, snapshot, i_understand=True)
        with pytest.raises(journal.JournalIOError, match="unverified"):
            journal.Journal.create(root)
    finally:
        os.chmod(journal_dir, 0o700)
