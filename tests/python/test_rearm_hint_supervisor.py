"""b-244: re-arm hints must not tell operators to fight a live supervisor."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_doctor as doctor  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_wake as wake  # noqa: E402


@pytest.fixture()
def isolated(
    tmp_path: Path,
) -> tuple[Path, journal.LeaseIdentity]:
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        "hint-ctl",
        principal={"principal_id": "rearm-hint-supervisor"},
    )
    assert claimed.committed and claimed.value is not None
    return project, claimed.value


def _component_commands(
    project: Path, lease: journal.LeaseIdentity
) -> tuple[str, str, str]:
    return (
        wake.follow_start_command(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        ),
        wake.persistent_backup_start_command(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        ),
        wake.follow_watchdog_start_command(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        ),
    )


def _persistent_shortfall_plan(
    project: Path,
    lease: journal.LeaseIdentity,
    monkeypatch: pytest.MonkeyPatch,
    listing: list[tuple[int | None, str]] | None,
) -> dict[str, object]:
    monkeypatch.setattr(wake, "_process_listing", lambda: listing)
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=360,
    )
    status = wake.coverage_status(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    return wake.coverage_rearm_plan(
        status,
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        work_in_flight=True,
    )


def test_rearm_hint_supervised_omits_component_commands(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    supervise_cmd = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    plan = _persistent_shortfall_plan(
        project, lease, monkeypatch, [(4242, supervise_cmd)]
    )
    hint = wake.coverage_rearm_hint(plan)
    stream_cmd, backup_cmd, watchdog_cmd = _component_commands(project, lease)
    assert plan["supervisor"] == wake.SUPERVISOR_RUNNING
    assert plan["missing"] > 0
    assert stream_cmd not in hint
    assert backup_cmd not in hint
    assert watchdog_cmd not in hint
    assert supervise_cmd in hint
    assert "Restart the supervisor" in hint
    assert "stopped slot is not recovered" in hint
    activity = wake.listener_activity_hint(
        int(plan["live"]),
        int(plan["target"]),
        str(plan["command"]),
        work_in_flight=True,
        supervisor=str(plan["supervisor"]),
        supervise_command=str(plan["supervise_command"]),
    )
    assert stream_cmd not in activity
    assert backup_cmd not in activity
    assert watchdog_cmd not in activity
    assert supervise_cmd in activity


def test_rearm_hint_unsupervised_keeps_three_command_form(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    plan = _persistent_shortfall_plan(project, lease, monkeypatch, [])
    hint = wake.coverage_rearm_hint(plan)
    stream_cmd, backup_cmd, watchdog_cmd = _component_commands(project, lease)
    assert plan["supervisor"] == wake.SUPERVISOR_ABSENT
    assert "Restart the supervisor" not in hint
    assert "If you are running `supervise`" not in hint
    assert "could not tell whether `supervise`" not in hint
    assert stream_cmd in hint
    assert backup_cmd in hint
    assert watchdog_cmd in hint
    assert "host persistent stdout monitor" in hint
    assert "own tracked background task" in hint
    activity = wake.listener_activity_hint(
        int(plan["live"]),
        int(plan["target"]),
        str(plan["command"]),
        work_in_flight=True,
        supervisor=str(plan["supervisor"]),
        supervise_command=str(plan["supervise_command"]),
    )
    assert activity == (
        f"listener depth {plan['live']}/{plan['target']} — "
        f"{plan['missing']} missing; {plan['command']}"
    )


def test_rearm_hint_undetermined_is_true_either_way(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    plan = _persistent_shortfall_plan(project, lease, monkeypatch, None)
    hint = wake.coverage_rearm_hint(plan)
    stream_cmd, backup_cmd, watchdog_cmd = _component_commands(project, lease)
    assert plan["supervisor"] == wake.SUPERVISOR_UNKNOWN
    assert "could not tell whether `supervise`" in hint
    assert "If you are running `supervise`, restart it" in hint
    assert "Otherwise arm these:" in hint
    assert stream_cmd in hint
    assert backup_cmd in hint
    assert watchdog_cmd in hint
    activity = wake.listener_activity_hint(
        int(plan["live"]),
        int(plan["target"]),
        str(plan["command"]),
        work_in_flight=True,
        supervisor=str(plan["supervisor"]),
        supervise_command=str(plan["supervise_command"]),
    )
    assert "if you are running `supervise`, restart it" in activity
    assert str(plan["command"]) in activity


def test_unbindable_supervise_argv_is_unknown_not_absent(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    listing = [
        (
            99,
            "python3 scripts/goalflight_messages.py supervise "
            f"--project-root {project}",
        )
    ]
    plan = _persistent_shortfall_plan(project, lease, monkeypatch, listing)
    hint = wake.coverage_rearm_hint(plan)
    assert plan["supervisor"] == wake.SUPERVISOR_UNKNOWN
    assert "could not tell whether `supervise`" in hint
    assert wake.follow_start_command(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ) in hint


def test_rearm_hint_supervised_portable_shortfall_omits_listen(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """b-242 shortfall: supervise is live, children never armed, mode is portable."""
    project, lease = isolated
    supervise_cmd = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    monkeypatch.setattr(wake, "_process_listing", lambda: [(4242, supervise_cmd)])
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
    hint = wake.coverage_rearm_hint(plan)
    listen_cmd = wake.listener_start_command(
        project, controller_label=lease.label
    )
    assert plan.get("wake_mode") != "persistent"
    assert plan["supervisor"] == wake.SUPERVISOR_RUNNING
    assert supervise_cmd in hint
    assert listen_cmd not in hint
    assert "Restart the supervisor" in hint
    activity = wake.listener_activity_hint(
        int(plan["live"]),
        int(plan["target"]),
        str(plan["command"]),
        work_in_flight=True,
        supervisor=str(plan["supervisor"]),
        supervise_command=str(plan["supervise_command"]),
    )
    assert supervise_cmd in activity
    assert listen_cmd not in activity


def test_truncated_nonce_is_unknown_not_absent(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    truncated = (
        "python3 scripts/goalflight_messages.py supervise "
        f"--project-root {project} --controller-label {lease.label} "
        f"--lease-nonce {lease.nonce[:12]}"
    )
    plan = _persistent_shortfall_plan(
        project, lease, monkeypatch, [(8, truncated)]
    )
    hint = wake.coverage_rearm_hint(plan)
    assert plan["supervisor"] == wake.SUPERVISOR_UNKNOWN
    assert "could not tell whether `supervise`" in hint


def test_other_generation_supervise_is_absent(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    other = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce="not-this-generation",
    )
    plan = _persistent_shortfall_plan(
        project, lease, monkeypatch, [(7, other)]
    )
    hint = wake.coverage_rearm_hint(plan)
    assert plan["supervisor"] == wake.SUPERVISOR_ABSENT
    assert "Restart the supervisor" not in hint
    assert wake.follow_start_command(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ) in hint


def test_reminder_and_activity_surfaces_follow_the_plan(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    supervise_cmd = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    plan = _persistent_shortfall_plan(
        project, lease, monkeypatch, [(4242, supervise_cmd)]
    )
    stream = __import__("io").StringIO()
    line = messages.emit_listener_reminder(
        project_root=project,
        controller_label=lease.label,
        exposure=1,
        stream=stream,
    )
    assert line is not None
    assert supervise_cmd in line
    assert wake.follow_start_command(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ) not in line
    once = wake.consume_listener_activity_signal(project, lease.label, plan)
    assert supervise_cmd in once
    assert wake.follow_start_command(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ) not in once
    assert wake.consume_listener_activity_signal(project, lease.label, plan) == ""


def test_doctor_wake_coverage_reports_supervisor_state(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    supervise_cmd = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    _persistent_shortfall_plan(
        project, lease, monkeypatch, [(4242, supervise_cmd)]
    )
    result = doctor.check_wake_coverage(project)
    assert result["present"] is True
    assert result["pools"]
    pool = result["pools"][0]
    assert pool["label"] == lease.label
    assert pool["supervisor"] == wake.SUPERVISOR_RUNNING
    assert supervise_cmd in str(pool["hint"])
    assert wake.follow_start_command(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ) not in str(pool["hint"])
    line = doctor.status_line(
        pool.get("ok"),
        f"wake coverage {pool.get('label')}",
        f"{pool.get('live_waiters')}/{pool.get('target_waiters')} "
        f"supervisor={pool.get('supervisor')} missing="
        f"{','.join(str(name) for name in (pool.get('missing_components') or [])) or 'none'}",
    )
    assert "wake coverage hint-ctl" in line
    assert "supervisor=running" in line
