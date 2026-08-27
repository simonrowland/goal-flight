"""Persistent backup doorbells are a pool of 2, total coverage 4."""

from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_wake as wake  # noqa: E402


@pytest.fixture()
def isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, journal.LeaseIdentity]:
    label = "backup-depth"
    for key in (
        "GOALFLIGHT_LISTENER_SLOTS",
        "GOALFLIGHT_LISTENER_LOW_WATER",
        "GOALFLIGHT_PERSISTENT_BACKUP_SLOTS",
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_PROMPT_FILE",
        "GOALFLIGHT_STEER_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journals"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER_DIR", str(tmp_path / "wake-ledger"))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("GOALFLIGHT_TEST_MODE", "1")
    monkeypatch.setattr(wake, "_process_listing", lambda: [])
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label,
        principal={"principal_id": "backup-depth-principal"},
    )
    assert claimed.committed and claimed.value is not None
    return project, claimed.value


def _arm_stream(project: Path, lease: journal.LeaseIdentity) -> object:
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=360,
    )
    return wake.register_waiter(
        project,
        controller_label=lease.label,
        kind=wake.MONITOR_KIND,
        generation_key=lease.nonce,
    )


def _backups(project: Path, lease: journal.LeaseIdentity, count: int) -> ExitStack:
    stack = ExitStack()
    slots = max(count, wake.persistent_backup_slot_count())
    for _ in range(count):
        stack.enter_context(
            wake.register_listener_waiter(
                project,
                controller_label=lease.label,
                generation_key=lease.nonce,
                slots=slots,
            )
        )
    return stack


def test_persistent_wake_target_defaults_to_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", raising=False)
    monkeypatch.delenv("GOALFLIGHT_LISTENER_SLOTS", raising=False)
    assert wake.DEFAULT_LISTENER_SLOTS == 4
    assert wake.DEFAULT_PERSISTENT_BACKUP_SLOTS == 2
    assert not hasattr(wake, "MAX_LISTENER_SLOTS")
    assert wake.PERSISTENT_WAKE_TARGET == 4
    assert wake.persistent_backup_slot_count() == 2
    assert wake.persistent_wake_target() == 4
    assert wake.PERSISTENT_WAKE_TARGET == (
        1 + wake.DEFAULT_PERSISTENT_BACKUP_SLOTS + 1
    )


def test_persistent_backup_slots_env_override_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", raising=False)
    assert wake.persistent_backup_slot_count() == 2
    monkeypatch.setenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", "8")
    assert wake.persistent_backup_slot_count() == 8
    assert wake.persistent_wake_target() == 10
    monkeypatch.setenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", "1")
    assert wake.persistent_backup_slot_count() == 1
    assert wake.persistent_wake_target() == 3
    monkeypatch.setenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", "32")
    assert wake.persistent_backup_slot_count() == 32
    monkeypatch.setenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", "40")
    assert wake.persistent_backup_slot_count() == 40
    assert wake.persistent_wake_target() == 42
    monkeypatch.setenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", "0")
    with pytest.raises(ValueError, match="at least 1"):
        wake.persistent_backup_slot_count()
    monkeypatch.setenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", "nope")
    with pytest.raises(ValueError, match="integer"):
        wake.persistent_backup_slot_count()
    with pytest.raises(ValueError, match="at least 1"):
        wake.persistent_backup_slot_count(0)
    assert wake.persistent_backup_slot_count(33) == 33
    assert wake.persistent_backup_slot_count(40) == 40


def test_portable_listener_slots_remain_a_different_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOALFLIGHT_LISTENER_SLOTS", raising=False)
    monkeypatch.delenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", raising=False)
    monkeypatch.setenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", "8")
    assert wake.listener_slot_count() == wake.DEFAULT_LISTENER_SLOTS == 4
    assert wake.persistent_backup_slot_count() == 8
    monkeypatch.delenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", raising=False)
    monkeypatch.setenv("GOALFLIGHT_LISTENER_SLOTS", "2")
    assert wake.listener_slot_count() == 2
    assert wake.persistent_backup_slot_count() == 2
    assert wake.persistent_wake_target() == 4


def test_decayed_backup_pool_is_degraded_not_live(
    isolated: tuple[Path, journal.LeaseIdentity],
) -> None:
    project, lease = isolated
    with _arm_stream(project, lease):
        with _backups(project, lease, 1):
            with wake.register_watchdog_waiter(
                project,
                controller_label=lease.label,
                generation_key=lease.nonce,
            ):
                status = wake.coverage_status(
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                )
    assert status["wake_mode"] == "persistent"
    assert status["covered"] is True
    assert status["backup"]["state"] == "degraded"
    assert status["backup"]["observed"] == 1
    assert status["backup"]["target"] == 2
    assert status["portable_live_waiters"] == 1
    assert status["portable_target_waiters"] == 2
    assert status["live_waiters"] == 3
    assert status["target_waiters"] == 4
    assert "backup" in status["missing_components"]
    assert status["reason"] == "persistent-backup-degraded"


def test_zero_backup_is_missing_not_live(
    isolated: tuple[Path, journal.LeaseIdentity],
) -> None:
    project, lease = isolated
    with _arm_stream(project, lease):
        with wake.register_watchdog_waiter(
            project,
            controller_label=lease.label,
            generation_key=lease.nonce,
        ):
            status = wake.coverage_status(
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
            )
    assert status["covered"] is False
    assert status["backup"]["state"] == "missing"
    assert status["backup"]["observed"] == 0
    assert status["backup"]["target"] == 2
    assert status["live_waiters"] == 2
    assert status["target_waiters"] == 4
    assert status["missing_components"] == ["backup"]
    assert status["reason"] == "persistent-backup-missing"


def test_full_backup_pool_is_live(
    isolated: tuple[Path, journal.LeaseIdentity],
) -> None:
    project, lease = isolated
    with _arm_stream(project, lease):
        with _backups(project, lease, 2):
            with wake.register_watchdog_waiter(
                project,
                controller_label=lease.label,
                generation_key=lease.nonce,
            ):
                status = wake.coverage_status(
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                )
    assert status["covered"] is True
    assert status["backup"]["state"] == "live"
    assert status["backup"]["observed"] == 2
    assert status["backup"]["target"] == 2
    assert status["live_waiters"] == status["target_waiters"] == 4
    assert status["missing_components"] == []
    assert status["reason"] == "persistent-covered"


def test_rearm_commands_restore_backup_shortfall(
    isolated: tuple[Path, journal.LeaseIdentity],
) -> None:
    project, lease = isolated
    with _arm_stream(project, lease):
        with _backups(project, lease, 1):
            with wake.register_watchdog_waiter(
                project,
                controller_label=lease.label,
                generation_key=lease.nonce,
            ):
                status = wake.coverage_status(
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                )
                commands = wake.coverage_rearm_commands(
                    status,
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                )
                plan = wake.coverage_rearm_plan(
                    status,
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                    work_in_flight=True,
                )
    backup_cmd = wake.persistent_backup_start_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    assert "--listener-slots" in backup_cmd
    assert backup_cmd.split()[backup_cmd.split().index("--listener-slots") + 1] == "2"
    assert commands == [backup_cmd]
    assert plan["missing"] == 1
    assert plan["commands"] == commands
    hint = wake.coverage_rearm_hint(plan)
    assert "persistent wake coverage 3/4" in hint
    assert hint.count(backup_cmd) == 1


def test_healthy_full_pool_emits_no_nag(
    isolated: tuple[Path, journal.LeaseIdentity],
) -> None:
    project, lease = isolated
    with _arm_stream(project, lease):
        with _backups(project, lease, 2):
            with wake.register_watchdog_waiter(
                project,
                controller_label=lease.label,
                generation_key=lease.nonce,
            ):
                status = wake.coverage_status(
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                )
                plan = wake.coverage_rearm_plan(
                    status,
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                    work_in_flight=True,
                )
                silent = wake.coverage_rearm_plan(
                    status,
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                    work_in_flight=False,
                )
    assert plan["missing"] == 0
    assert plan.get("commands") == []
    assert wake.coverage_rearm_hint(plan) == ""
    assert wake.coverage_rearm_hint(silent) == ""
    assert wake.listener_floor_hint(
        int(status["portable_live_waiters"]),
        int(status["portable_target_waiters"]),
        "CMD",
        work_in_flight=True,
    ) == ""
    assert wake.listener_depth_plan(
        int(status["portable_live_waiters"]),
        int(status["portable_target_waiters"]),
        "CMD",
        work_in_flight=True,
    )["missing"] == 0


def test_shortfall_is_silent_without_work_in_flight(
    isolated: tuple[Path, journal.LeaseIdentity],
) -> None:
    project, lease = isolated
    with _arm_stream(project, lease):
        with _backups(project, lease, 1):
            status = wake.coverage_status(
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
            )
            plan = wake.coverage_rearm_plan(
                status,
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                work_in_flight=False,
            )
    assert plan["live"] == 2
    assert plan["missing"] == 2
    assert wake.coverage_rearm_hint(plan) == ""
    assert wake.listener_floor_hint(
        int(status["portable_live_waiters"]),
        int(status["portable_target_waiters"]),
        "CMD",
        work_in_flight=False,
    ) == ""


def test_override_to_eight_backups_makes_target_ten(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consumers of target/live/missing must survive 3 → 10, not only 3 → 8."""
    project, lease = isolated
    monkeypatch.setenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", "8")
    assert wake.persistent_wake_target() == 10
    with _arm_stream(project, lease):
        with _backups(project, lease, 1):
            status = wake.coverage_status(
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
            )
            commands = wake.coverage_rearm_commands(
                status,
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
            )
            plan = wake.coverage_rearm_plan(
                status,
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                work_in_flight=True,
            )
    assert status["target_waiters"] == 10
    assert status["live_waiters"] == 2
    assert status["backup"]["target"] == 8
    assert status["backup"]["observed"] == 1
    assert status["backup"]["state"] == "degraded"
    backup_cmd = wake.persistent_backup_start_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    assert backup_cmd.split()[backup_cmd.split().index("--listener-slots") + 1] == "8"
    assert commands.count(backup_cmd) == 7
    assert any("--watch-follow" in row for row in commands)
    assert plan["target"] == 10
    assert plan["live"] == 2
    assert plan["missing"] == 8
    hint = wake.coverage_rearm_hint(plan)
    assert "2/10" in hint
    assert hint.count(backup_cmd) == 7
