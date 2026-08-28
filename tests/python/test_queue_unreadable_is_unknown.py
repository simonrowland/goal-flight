#!/usr/bin/env python3
"""An unreadable queue or journals index is UNKNOWN, never an empty one.

``Path.glob`` swallows ``PermissionError`` and yields nothing, which used to
render a present queue envelope as "no carrier" (licensing
``worker_provably_gone`` for a live dispatch) and an unreadable journals
index as an empty fleet. Listing must raise, and the carrier/adjudication
layer maps that to UNKNOWN: a keep for the abandoned gate, a refuse for
blind claim recovery and drain, and a visible listing error in reports.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_controllers as controllers  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_journal as journal  # noqa: E402


def _queue(tmp_path: Path) -> Path:
    queue = tmp_path / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    return queue


def _envelope(queue: Path, dispatch_id: str) -> Path:
    path = queue / f"{dispatch_id}.json"
    path.write_text(
        json.dumps({"dispatch_id": dispatch_id, "state": "queued"}) + "\n",
        encoding="utf-8",
    )
    return path


def _claim_marker(queue: Path, dispatch_id: str, pid: int) -> Path:
    path = queue / f"{dispatch_id}.json.claimed-{pid}"
    path.write_text(
        json.dumps({"dispatch_id": dispatch_id, "state": "claimed"}) + "\n",
        encoding="utf-8",
    )
    return path


def _running_record(dispatch_id: str) -> dict:
    return {
        "dispatch_id": dispatch_id,
        "state": "running",
        "transport": "dispatch",
        "hostname": socket.gethostname(),
    }


def test_unreadable_queue_carrier_is_unknown_and_kept(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    envelope = _envelope(queue, "disp-1")

    carrier = D._claim_has_active_carrier(queue, "disp-1")
    assert carrier.kind is D.ClaimCarrierKind.QUEUED

    os.chmod(queue, 0o000)
    try:
        carrier = D._claim_has_active_carrier(queue, "disp-1")
        assert carrier.kind is D.ClaimCarrierKind.UNKNOWN
        assert carrier.reason.startswith("queue_dir_unreadable"), carrier
        assert bool(carrier), "UNKNOWN must stay truthy so presence gates keep refusing"

        evaluation = D._evaluate_abandoned_dispatch(
            _running_record("disp-1"),
            queue_dir=queue,
            capacity_state={},
            now_s=0.0,
            stale_s=0.0,
        )
        assert evaluation["eligible"] is False, evaluation
        assert evaluation["reason"] == "unknown_claimer", evaluation
        assert "worker_provably_gone" != evaluation["reason"]
    finally:
        os.chmod(queue, 0o700)
    assert envelope.is_file(), "the envelope was never touched"

    carrier = D._claim_has_active_carrier(queue, "disp-1")
    assert carrier.kind is D.ClaimCarrierKind.QUEUED


def test_absent_and_empty_queue_carrier_stays_none(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-queue"
    assert D._claim_has_active_carrier(missing, "disp-1").kind is D.ClaimCarrierKind.NONE

    queue = _queue(tmp_path)
    assert D._claim_has_active_carrier(queue, "disp-1").kind is D.ClaimCarrierKind.NONE
    assert D._abandoned_carrier_keep_reason(D.ClaimCarrierStatus()) is None


def test_summarize_claim_markers_unreadable_reports_listing_error(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _claim_marker(queue, "disp-2", 424242)

    summary = D._summarize_claim_markers(queue)
    assert summary == {"dead": 0, "unknown": 1, "live": 0}

    os.chmod(queue, 0o000)
    try:
        summary = D._summarize_claim_markers(queue)
        assert summary.get("listing_error", "").startswith("queue_dir_unreadable"), summary
        fields = D._claimer_report_fields(queue)
        assert fields["queue_listing_error"].startswith("queue_dir_unreadable"), fields
    finally:
        os.chmod(queue, 0o700)

    summary = D._summarize_claim_markers(queue)
    assert summary == {"dead": 0, "unknown": 1, "live": 0}

    missing = tmp_path / "no-such-queue"
    assert D._summarize_claim_markers(missing) == {"dead": 0, "unknown": 0, "live": 0}


def test_recovery_unreadable_queue_refuses_blind_reconcile(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    claim = _claim_marker(queue, "disp-3", 424242)
    original_bytes = claim.read_bytes()

    empty = D._recover_claimed_queue_entries(_queue(tmp_path / "other"), stale_s=0.0)
    assert "listing_error" not in empty
    assert empty["restored"] == empty["cleared"] == empty["pending_launch"] == 0

    os.chmod(queue, 0o000)
    try:
        recovery = D._recover_claimed_queue_entries(queue, stale_s=0.0)
        assert recovery["listing_error"].startswith("queue_dir_unreadable"), recovery
        assert recovery["restored"] == 0
        assert recovery["cleared"] == 0
        assert recovery["quarantined"] == 0
        assert recovery["ledger_terminalized"] == 0
        assert any(
            "queue_dir_unreadable" in str(pending.get("reason"))
            for pending in recovery["pending_reasons"]
        ), recovery
    finally:
        os.chmod(queue, 0o700)
    assert claim.read_bytes() == original_bytes, (
        "blind recovery must not rename, terminalize, or rewrite the claim"
    )


def test_drain_unreadable_queue_fails_instead_of_reporting_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = _queue(tmp_path)
    envelope = _envelope(queue, "disp-4")

    os.chmod(queue, 0o000)
    try:
        rc = D._cmd_drain(["--queue-dir", str(queue), "--json"])
    finally:
        os.chmod(queue, 0o700)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 1
    assert "queue_dir_unreadable" in payload["error"], payload
    assert envelope.is_file(), "the envelope was never touched"

    envelope.unlink()
    rc = D._cmd_drain(["--queue-dir", str(queue), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert "queue_listing_error" not in payload
    assert payload["remaining"] == 0


def test_iter_journal_files_unreadable_index_is_unknown_not_empty(tmp_path: Path) -> None:
    first = tmp_path / "project-a"
    second = tmp_path / "project-b"
    first.mkdir()
    second.mkdir()
    journal.Journal.create(first)
    journal.Journal.create(second)
    index = journal.journals_index_dir()

    files = journal.iter_journal_files()
    assert len(files) == 2
    assert controllers.collect_controller_rows(idle_hours=24, ledger_records=[]) == []

    os.chmod(index, 0o000)
    try:
        with pytest.raises(journal.JournalIOError):
            journal.iter_journal_files()
        rows = controllers.collect_controller_rows(idle_hours=24, ledger_records=[])
        assert len(rows) == 1, rows
        assert rows[0]["bucket"] == "unknown"
        assert "unreadable" in rows[0]["unknown_reason"], rows[0]
    finally:
        os.chmod(index, 0o755)

    assert len(journal.iter_journal_files()) == 2


def test_iter_journal_files_absent_index_is_empty(tmp_path: Path) -> None:
    # The isolated env points GOALFLIGHT_JOURNAL_DIR at a never-created tree.
    assert not journal.journals_index_dir().exists()
    assert journal.iter_journal_files() == []
    assert controllers.collect_controller_rows(idle_hours=24, ledger_records=[]) == []


def test_iter_journal_files_unreadable_project_dir_yields_unknown_row(tmp_path: Path) -> None:
    project = tmp_path / "project-c"
    project.mkdir()
    authority = journal.Journal.create(project)
    journal_dir = authority.path.parent

    assert len(journal.iter_journal_files()) == 1

    os.chmod(journal_dir, 0o000)
    try:
        files = journal.iter_journal_files()
        assert files == [journal_dir / journal.JOURNAL_FILE_NAME], files
        rows = controllers.collect_controller_rows(idle_hours=24, ledger_records=[])
        assert len(rows) == 1, rows
        assert rows[0]["bucket"] == "unknown"
    finally:
        os.chmod(journal_dir, 0o700)

    assert journal.iter_journal_files() == [authority.path]
