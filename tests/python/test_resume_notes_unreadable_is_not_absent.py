#!/usr/bin/env python3
"""A present resume note whose read cannot complete is UNKNOWN, not absent.

``_resume_notes_active`` used to return ``(False, "resume notes unreadable")``
on ``OSError``, so ``aggregate_status()["active"]`` collapsed to False when
that was the only signal. The --text verdict then said there was no active
session, and orchestrators that branch on that label skipped the resume.
A LABEL may render unknown; it must not certify inactivity from a failed
read. An absent note stays a measured negative.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_session_status as sessions  # noqa: E402


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "docs-private").mkdir(parents=True)
    return root


def _write_notes(project: Path, body: str) -> Path:
    path = project / "docs-private" / "RESUME-NOTES-2026-09-05.md"
    path.write_text(body, encoding="utf-8")
    return path


def _claims_no_active_session(status: dict) -> bool:
    if status["active"] is False:
        return True
    text = sessions.to_text(status)
    lowered = text.lower()
    return "no active goal-flight session" in lowered or "not an active session" in lowered


def test_unreadable_resume_note_does_not_claim_no_active_session(tmp_path: Path) -> None:
    project = _project(tmp_path)
    notes = _write_notes(
        project,
        "---\nstate: active\n---\n\n# Resume\n\n**Status:** active\n",
    )
    readable = sessions.aggregate_status(project)
    assert readable["active"] is True
    assert readable["resume_notes_active"] is True

    os.chmod(notes, 0o000)
    try:
        with pytest.raises(OSError):
            notes.read_text(encoding="utf-8")
        notes_active, notes_reason = sessions._resume_notes_active(notes)
        status = sessions.aggregate_status(project)
        text = sessions.to_text(status)
        assert notes_active is not False, (notes_active, notes_reason)
        assert "unreadable" in notes_reason
        assert status["resume_notes_active"] is not False
        assert status["resume_notes_reason"] == notes_reason
        assert status["newest_resume_notes"] is not None
        assert not _claims_no_active_session(status), text
        assert "unknown" in text.lower()
    finally:
        os.chmod(notes, 0o644)

    restored = sessions.aggregate_status(project)
    assert restored["active"] is True
    assert restored["resume_notes_active"] is True


def test_absent_resume_note_stays_inactive(tmp_path: Path) -> None:
    project = _project(tmp_path)
    status = sessions.aggregate_status(project)
    assert status["active"] is False
    assert status["resume_notes_active"] is False
    assert status["newest_resume_notes"] is None
    assert _claims_no_active_session(status)


def test_readable_complete_resume_note_stays_inactive(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_notes(project, "---\nstate: complete\n---\n\n# Done\n")
    status = sessions.aggregate_status(project)
    assert status["active"] is False
    assert status["resume_notes_active"] is False
    assert "unreadable" not in status["resume_notes_reason"]
    assert _claims_no_active_session(status)


def test_unreadable_notes_do_not_hide_a_measured_active_queue(tmp_path: Path) -> None:
    project = _project(tmp_path)
    queue = project / "docs-private" / "goal-queue-demo.md"
    queue.write_text(
        "---\n"
        "slug: demo\n"
        "state: active\n"
        f"last-touched: {sessions._now_iso()}\n"
        "---\n\n# Demo\n",
        encoding="utf-8",
    )
    notes = _write_notes(project, "---\nstate: active\n---\n")
    os.chmod(notes, 0o000)
    try:
        with pytest.raises(OSError):
            notes.read_text(encoding="utf-8")
        status = sessions.aggregate_status(project)
        assert status["active"] is True
        assert status["resume_notes_active"] is not False
        assert "unreadable" in status["resume_notes_reason"]
        assert "active goal-flight session" in sessions.to_text(status)
    finally:
        os.chmod(notes, 0o644)
