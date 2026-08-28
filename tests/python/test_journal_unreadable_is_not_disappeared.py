#!/usr/bin/env python3
"""An unreadable present journal is UNREADABLE, never Disappeared/"dead".

``os.path.lexists`` is ``lstat`` with ``except (OSError, ValueError): return
False``, so a permission failure on a present journal used to raise
``JournalDisappeared`` — the documented *verifiably absent* verdict — and
every consumer then rendered a live controller as gone: probe "dead", an
honest-empty roster, and ``controller_beacon_absent`` for the abandoned
gate. Only a genuine FileNotFoundError may become JournalDisappeared; any
other OSError is JournalIOError (unknown), and unknown keeps the gate shut.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _project(tmp_path: Path, name: str = "project") -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def _hold_live_controller(root: Path, label: str = "engine"):
    registered = sessions.register_controller(root, label, session_id=f"{label}-nonce")
    assert registered["registered"] is True, registered
    holder = wake.register_lease_holder(
        root,
        controller_label=label,
        lease_nonce=registered["session"]["lease_nonce"],
    )
    return registered, holder


def test_unreadable_journal_probes_unreadable_not_dead(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _registered, holder = _hold_live_controller(root)
    journal_dir = journal.resolve_journal_path(root).parent
    try:
        state, session = sessions.probe_live_session(root, label="engine")
        assert state == "live", (state, session)

        os.chmod(journal_dir, 0o000)
        try:
            state, session = sessions.probe_live_session(root, label="engine")
            assert state == "unreadable", (state, session)
            assert session is None

            inactive, evidence = dispatch._abandoned_controller_evidence(
                {"controller_label": "engine", "project_root": str(root)}
            )
            assert (inactive, evidence) == (False, "controller_indeterminate")

            records, error = sessions._probe_registered_controller_records(root)
            assert records is None
            assert error == "JournalIOError"

            roster = sessions.controller_roster(root, ledger_records=[])
            assert roster["measurements"]["controller_registry"]["measured"] is False
            lines = sessions.controller_roster_lines(roster)
            assert len(lines) == 1
            assert lines[0].startswith("controllers unreadable"), lines
        finally:
            os.chmod(journal_dir, 0o700)

        state, session = sessions.probe_live_session(root, label="engine")
        assert state == "live", (state, session)
    finally:
        holder.close()


def test_absent_journal_still_probes_dead(tmp_path: Path) -> None:
    root = _project(tmp_path)
    state, session = sessions.probe_live_session(root, label="engine")
    assert (state, session) == ("dead", None)

    records, error = sessions._probe_registered_controller_records(root)
    assert records == [] and error is None

    roster = sessions.controller_roster(root, ledger_records=[])
    assert roster["measurements"]["controller_registry"]["measured"] is True
    assert roster["controllers"] == []
    assert sessions.controller_roster_lines(roster) == []

    inactive, evidence = dispatch._abandoned_controller_evidence(
        {"controller_label": "engine", "project_root": str(root)}
    )
    assert (inactive, evidence) == (True, "controller_beacon_absent")


def test_open_reader_splits_absent_from_unreadable(tmp_path: Path) -> None:
    root = _project(tmp_path)
    with pytest.raises(journal.JournalDisappeared):
        journal.Journal.open_reader(root)

    authority = journal.Journal.create(root)
    journal_dir = authority.path.parent
    del authority
    reader = journal.Journal.open_reader(root)
    assert reader is not None

    os.chmod(journal_dir, 0o000)
    try:
        with pytest.raises(journal.JournalIOError, match="unverified"):
            journal.Journal.open_reader(root)
        with pytest.raises(journal.JournalIOError, match="unverified"):
            journal.Journal(root)
    finally:
        os.chmod(journal_dir, 0o700)

    reader = journal.Journal.open_reader(root)
    assert reader is not None


def test_open_or_create_never_bootstraps_over_unreadable(tmp_path: Path) -> None:
    root = _project(tmp_path)
    authority = journal.Journal.create(root)
    result = authority.claim_or_renew_lease(
        "engine",
        principal={"pid": os.getpid(), "start_token": "presence-test", "hostname": "test"},
    )
    assert result.committed, result.reason
    journal_dir = authority.path.parent
    before = set(journal_dir.iterdir())

    os.chmod(journal_dir, 0o000)
    try:
        with pytest.raises(journal.JournalIOError, match="unverified"):
            journal.open_or_create_journal(root)
    finally:
        os.chmod(journal_dir, 0o700)
    assert set(journal_dir.iterdir()) == before, "no bootstrap was attempted"
    reopened = journal.open_or_create_journal(root)
    assert reopened.active_lease("engine") is not None

    fresh = _project(tmp_path, "fresh")
    created = journal.open_or_create_journal(fresh)
    assert created.path.exists()
