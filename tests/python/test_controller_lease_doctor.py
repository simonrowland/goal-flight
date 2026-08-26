#!/usr/bin/env python3
"""Doctor reports controller lease liveness from real kernel lock witnesses."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_compat as compat  # noqa: E402
import goalflight_doctor as doctor  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    values = {
        "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
        "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journals"),
        "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
        "GOALFLIGHT_STATE_DIR": str(tmp_path / "state"),
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp_path / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    project = tmp_path / "project"
    project.mkdir()
    return project


def _claim(authority: journal.Journal, label: str) -> journal.LeaseIdentity:
    identity = compat.process_start_identity(os.getpid())
    assert identity is not None
    claimed = authority.claim_or_renew_lease(
        label,
        principal={
            "pid": os.getpid(),
            "start_token": str(identity["start_token"]),
            "hostname": socket.gethostname(),
        },
    )
    assert claimed.committed and claimed.value is not None
    return claimed.value


def test_doctor_reports_kernel_live_and_active_but_dead_controller_leases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _isolate(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(project)
    live = _claim(authority, "live-controller")
    dead = _claim(authority, "dead-controller")
    live_holder = wake.register_lease_holder(
        project,
        controller_label=live.label,
        lease_nonce=live.nonce,
    )
    dead_holder = wake.register_lease_holder(
        project,
        controller_label=dead.label,
        lease_nonce=dead.nonce,
    )
    dead_holder.close()
    try:
        result = doctor.check_controller_lease_liveness(project)
        assert result["active_controller_leases_in_project"] == 2
        assert result["active_but_dead_controller_leases_in_project"] == 1
        assert result["ok"] is False
        assert {
            str(row["label"]): row["holder_live"] for row in result["leases"]
        } == {
            "dead-controller": False,
            "live-controller": True,
        }

        session = doctor.check_session_status(ROOT, project)
        assert "active_leases_in_project" not in session
        assert session["active_capacity_leases_in_project"] == 0
    finally:
        live_holder.close()


def _touch_journal_path(project: Path) -> Path:
    path = journal.resolve_journal_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_doctor_absent_journal_is_healthy_missing_lease_surface(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _isolate(monkeypatch, tmp_path)
    result = doctor.check_controller_lease_liveness(project)
    assert result["ok"] is True
    assert result["present"] is False
    assert result["leases"] == []
    assert result["active_controller_leases_in_project"] == 0
    assert "error" not in result


@pytest.mark.parametrize(
    "error",
    (
        journal.JournalIOError("present journal cannot be opened"),
        journal.JournalDisappeared("journal vanished after path check"),
        journal.JournalBusy("journal connection remained busy"),
    ),
)
def test_doctor_unreadable_journal_is_not_false_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    project = _isolate(monkeypatch, tmp_path)
    _touch_journal_path(project)

    def refuse_open(_project_root, **_kwargs):
        raise error

    monkeypatch.setattr(doctor.goalflight_journal.Journal, "open_reader", refuse_open)
    result = doctor.check_controller_lease_liveness(project)
    assert result["ok"] is False
    assert result["present"] is True
    assert type(error).__name__ in str(result.get("error") or "")
    assert result["leases"] == []
    assert result["active_controller_leases_in_project"] == 0
    line = doctor.status_line(
        result["ok"],
        "controller lease holder liveness",
        result.get("error"),
    )
    assert line.startswith("[WARN]")
