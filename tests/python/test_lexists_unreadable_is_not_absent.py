#!/usr/bin/env python3
"""Doctor and dispatch launch treat unreadable journals as unknown, not absent.

``os.path.lexists`` is ``lstat`` with ``except OSError: return False``, so a
chmod-000 journal dir used to take the no-journal arm: doctor reported a
healthy missing lease surface, launch skipped attempt fencing, and drain
minted a fresh queue_launch_token. Only FileNotFoundError is absent.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_doctor as doctor  # noqa: E402
import goalflight_journal as journal  # noqa: E402


def _project(tmp_path: Path, name: str = "project") -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def test_doctor_unreadable_journal_is_not_healthy_absent(tmp_path: Path) -> None:
    root = _project(tmp_path)
    authority = journal.open_or_create_journal(root)
    journal_dir = authority.path.parent
    live = doctor.check_controller_lease_liveness(root)
    assert live["present"] is True
    wake = doctor.check_wake_coverage(root)
    assert wake["present"] is True

    os.chmod(journal_dir, 0o000)
    try:
        assert journal._lstat_presence(authority.path) == "unknown"
        assert os.path.lexists(authority.path) is False

        leases = doctor.check_controller_lease_liveness(root)
        assert leases["ok"] is False, leases
        assert leases["present"] is True, leases
        assert "error" in leases
        assert leases["leases"] is None
        assert leases["active_controller_leases_in_project"] is None
        assert leases["active_but_dead_controller_leases_in_project"] is None
        assert leases["unknown_controller_lease_holders_in_project"] is None

        coverage = doctor.check_wake_coverage(root)
        assert coverage["ok"] is False, coverage
        assert coverage["present"] is True, coverage
        assert "error" in coverage
    finally:
        os.chmod(journal_dir, 0o700)

    restored = doctor.check_controller_lease_liveness(root)
    assert restored["present"] is True
    assert "error" not in restored


def test_doctor_absent_journal_stays_healthy_missing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    leases = doctor.check_controller_lease_liveness(root)
    assert leases["ok"] is True
    assert leases["present"] is False
    assert "error" not in leases
    coverage = doctor.check_wake_coverage(root)
    assert coverage["ok"] is True
    assert coverage["present"] is False
    assert "error" not in coverage


def test_attempt_claiming_unreadable_journal_does_not_skip_fencing(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    authority = journal.open_or_create_journal(root)
    journal_dir = authority.path.parent
    argv = [sys.executable, "-c", "pass"]

    missing = _project(tmp_path, "missing")
    absent_argv, claimed = dispatch._attempt_claiming_worker_argv(
        missing, "disp-absent", argv
    )
    assert claimed is False
    assert absent_argv == argv

    os.chmod(journal_dir, 0o000)
    try:
        with pytest.raises(journal.JournalIOError):
            dispatch._attempt_claiming_worker_argv(root, "disp-unreadable", argv)
    finally:
        os.chmod(journal_dir, 0o700)


def test_queue_launch_token_unreadable_does_not_mint_fresh_uuid(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    authority = journal.open_or_create_journal(root)
    prepared = authority.prepare_attempt("disp-prepared")
    assert prepared.committed and prepared.value is not None
    expected = prepared.value.launch_token
    journal_dir = authority.path.parent
    entry = {"dispatch_id": "disp-prepared", "project_root": str(root)}

    assert dispatch._queue_launch_token(entry) == expected

    missing_entry = {
        "dispatch_id": "disp-absent",
        "project_root": str(_project(tmp_path, "missing")),
    }
    minted = dispatch._queue_launch_token(missing_entry)
    uuid.UUID(minted)

    os.chmod(journal_dir, 0o000)
    try:
        with pytest.raises(journal.JournalIOError):
            dispatch._queue_launch_token(entry)
    finally:
        os.chmod(journal_dir, 0o700)

    assert dispatch._queue_launch_token(entry) == expected
