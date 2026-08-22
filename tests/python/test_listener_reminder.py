"""Journal coverage reminder contracts; no host process inspection."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as msgs  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _authority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, journal.Journal, journal.LeaseIdentity]:
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pids"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER_DIR", str(tmp_path / "wake-ledger"))
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "bugs")
    root = tmp_path / "project"
    root.mkdir()
    authority = journal.open_or_create_journal(root)
    result = authority.claim_or_renew_lease(
        "bugs",
        principal={"pid": 51001, "start_token": "controller-token"},
    )
    assert result.committed and result.value is not None
    return root, authority, result.value


def test_reminder_uses_held_waiter_lock_not_journal_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority, lease = _authority(monkeypatch, tmp_path)
    assert msgs.controller_mail_summary(task_store_project_root=root)["controller_label"] == "bugs"
    stream = io.StringIO()
    line = msgs.emit_listener_reminder(
        project_root=root, controller_label="bugs", exposure=1, stream=stream
    )
    assert line is not None
    assert line.startswith("listener pool n=0; start: ")
    assert "--controller-label bugs" in line
    assert "--report-pending" in line

    armed = authority.arm_listener(
        "bugs",
        nonce=lease.nonce,
        pid=52001,
        start_token="listener-token",
        parent_pid=51001,
    )
    assert armed.committed
    with wake.register_listener_waiter(
        root,
        controller_label="bugs",
        generation_key=lease.nonce,
        slots=wake.DEFAULT_LISTENER_SLOTS,
    ):
        stream = io.StringIO()
        reserve_line = msgs.emit_listener_reminder(
            project_root=root,
            controller_label="bugs",
            exposure=1,
            stream=stream,
        )
        assert reserve_line is not None
        assert reserve_line.startswith(f"listener pool n=1/{wake.DEFAULT_LISTENER_SLOTS} — reserve down; re-arm: ")
        assert reserve_line.count("--report-pending") == wake.DEFAULT_LISTENER_SLOTS - 1
        # Silence requires a FULL pool: fill the remaining slots (one is
        # already held above) and only then does the reminder go quiet.
        with contextlib.ExitStack() as rest:
            for _slot in range(wake.DEFAULT_LISTENER_SLOTS - 1):
                rest.enter_context(
                    wake.register_listener_waiter(
                        root,
                        controller_label="bugs",
                        generation_key=lease.nonce,
                        slots=wake.DEFAULT_LISTENER_SLOTS,
                    )
                )
            stream = io.StringIO()
            assert msgs.emit_listener_reminder(
                project_root=root,
                controller_label="bugs",
                exposure=1,
                stream=stream,
            ) is None
            assert stream.getvalue() == ""


def test_armed_journal_row_without_held_lock_is_not_authoritative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority, lease = _authority(monkeypatch, tmp_path)
    assert authority.arm_listener(
        "bugs", nonce=lease.nonce, pid=52001, start_token="stored", parent_pid=51001
    ).committed
    # The ledger directory must exist so the waiter probe can verifiably
    # report the LOCK absent; an absent directory is probe-unavailable, not
    # a statement about locks.
    wake.ledger_dir(root).mkdir(parents=True, exist_ok=True)
    status = msgs.listener_coverage_status(
        root,
        "bugs",
        identity_probe=lambda pid: {"pid": pid, "start_token": "reused-pid"},
    )
    assert status["covered"] is False
    assert status["reason"] == "no-live-waiter-lock"


def test_reminder_gates_on_exposure_and_reports_missing_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, _authority_value, _lease = _authority(monkeypatch, tmp_path)
    stream = io.StringIO()
    assert msgs.emit_listener_reminder(
        project_root=root, controller_label="bugs", exposure=0, stream=stream
    ) is None
    assert stream.getvalue() == ""

    line = msgs.emit_listener_reminder(
        project_root=root, controller_label=None, exposure=1, stream=stream
    )
    assert line is not None
    assert "not registered" in line
    assert "--controller-startup" in line


def test_controller_mail_exit_code_actions_and_skill_pool_instruction() -> None:
    doctrine = (ROOT / "protocols" / "controller-mail.md").read_text(encoding="utf-8")
    expected_rows = (
        ("| 0 | Ring:", "remaining-depth"),
        ("| 1 | Timeout:", "clean timer expiry"),
        ("| 2 | Infrastructure or corruption", "avoid a restart loop"),
        ("| 3 | Contention, supersession", "Reconcile the active lease"),
        ("| 4 | Detached-listener refusal", "tracked background listener"),
    )
    for meaning, action in expected_rows:
        row = next((line for line in doctrine.splitlines() if meaning in line), "")
        assert row, f"missing listener exit-code row: {meaning}"
        assert action in row, f"listener exit-code row lacks supervisor action: {meaning}"

    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    controller_entry = next(
        line for line in skill.splitlines() if line.startswith("Controller entry auto-claims")
    )
    assert "goalflight_messages.py follow" in controller_entry
    assert "three missed heartbeat intervals" in controller_entry
    assert "listen --listener-slots 1 --report-pending" in controller_entry
    assert "listen --watch-follow" in controller_entry
    assert "never consumes a delivery slot" in controller_entry
    assert "live/3" in controller_entry
    assert "portable pool of four" in controller_entry
    assert "restore depth" in controller_entry
