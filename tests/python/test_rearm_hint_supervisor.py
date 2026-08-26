"""b-244: re-arm hints must not tell operators to fight a live supervisor."""

from __future__ import annotations

from collections.abc import Iterator
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import sys

import pytest

from machine_isolation import AMBIENT_IDENTITY_ENV, isolated_machine_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_doctor as doctor  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_wake as wake  # noqa: E402
import goalflight_wake_supervise as supervise  # noqa: E402


@pytest.fixture()
def isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, journal.LeaseIdentity]]:
    label = "hint-ctl"
    env = dict(os.environ)
    for key in AMBIENT_IDENTITY_ENV:
        env.pop(key, None)
        monkeypatch.delenv(key, raising=False)
    env.pop("GOALFLIGHT_WAKE_LEDGER", None)
    env.update(isolated_machine_env(tmp_path))
    env.update(
        {
            "GOALFLIGHT_ROOT": str(ROOT),
            "GOALFLIGHT_CONTROLLER_LABEL": label,
            "GOALFLIGHT_PROCESS_ROLE": "controller",
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_WAKE_ENTRY_POLL_S": "0",
        }
    )
    for key, value in env.items():
        if key.startswith("GOAL") or key == "PYTHONUNBUFFERED":
            monkeypatch.setenv(key, value)
    # Component commands must execute the tree under test, not a separately
    # installed skill copy that may legitimately lag this branch.
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label,
        principal={"principal_id": "rearm-hint-supervisor"},
    )
    assert claimed.committed and claimed.value is not None
    with wake.register_lease_holder(
        project,
        controller_label=claimed.value.label,
        lease_nonce=claimed.value.nonce,
    ):
        yield project, claimed.value


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


def _run_supervised_child(
    project: Path,
    lease: journal.LeaseIdentity,
    *,
    kind: str,
    command: str,
) -> tuple[list[dict[str, object]], supervise.ChildExit]:
    env = dict(os.environ)
    env["GOALFLIGHT_TEST_MODE"] = "1"
    host = supervise.RealHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
    )
    child = host.spawn(kind, command)
    lines: list[str] = []
    child_exit: supervise.ChildExit | None = None
    deadline = time.monotonic() + 8.0
    try:
        while child_exit is None and time.monotonic() < deadline:
            result = host.wait([child], min(0.1, deadline - time.monotonic()))
            for _child, line in result.lines:
                lines.append(line)
                assert host.write_stdout(line)
            if result.exits:
                child_exit = result.exits[0]
    finally:
        host.kill_all()
    assert child_exit is not None, f"supervised {kind} child did not exit"
    records = [json.loads(line) for line in lines if line.startswith("{")]
    return records, child_exit


def _minimal_doctor_payload(wake_coverage: dict[str, object]) -> dict[str, object]:
    """Smallest payload that still drives the production human renderer."""
    return {
        "plugin": {},
        "claude": {},
        "codex": {"cli": {}},
        "context_mode": {},
        "cursor_context_mode": {},
        "opencode_context_mode": {},
        "gstack": {},
        "gstack_browser": {},
        "autoreview": {},
        "cursor": {"agent": {}, "models": {}},
        "opencode": {},
        "grok": {},
        "acp": {},
        "project": {},
        "wake_coverage": wake_coverage,
    }


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


def test_supervisor_forwarded_listener_exit_keeps_reason_not_action(
    isolated: tuple[Path, journal.LeaseIdentity],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, lease = isolated
    authority = journal.Journal(project)
    assert authority.prepare_attempt("supervised-listener-exit").committed
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=360,
    )
    command = (
        f"{wake.persistent_backup_start_command(project, controller_label=lease.label, lease_nonce=lease.nonce)} "
        "--timeout-s 0.15 --poll-secs 0.02"
    )
    with wake.register_watchdog_waiter(
        project,
        controller_label=lease.label,
        generation_key=lease.nonce,
    ):
        _records, child_exit = _run_supervised_child(
            project,
            lease,
            kind="backup",
            command=command,
        )
    forwarded = capsys.readouterr().err
    component_command = wake.persistent_backup_start_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    assert child_exit.returncode == 1
    assert "listen: timeout: no waking event before timeout" in forwarded
    assert component_command not in forwarded
    assert "re-arm" not in forwarded


def test_supervisor_forwarded_json_exit_keeps_reason_not_rearm_plan(
    isolated: tuple[Path, journal.LeaseIdentity],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, lease = isolated
    authority = journal.Journal(project)
    assert authority.prepare_attempt("supervised-json-listener-exit").committed
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=360,
    )
    command_parts = shlex.split(
        wake.persistent_backup_start_command(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
    )
    command_parts[0] = sys.executable
    command_parts.extend(["--timeout-s", "0.15", "--poll-secs", "0.02", "--json"])
    command = shlex.join(command_parts)
    with wake.register_watchdog_waiter(
        project,
        controller_label=lease.label,
        generation_key=lease.nonce,
    ):
        records, child_exit = _run_supervised_child(
            project,
            lease,
            kind="backup",
            command=command,
        )
    forwarded = capsys.readouterr()
    record = next(row for row in records if row.get("kind") == "exit")
    assert child_exit.returncode == 1
    assert record["reason"] == "timeout"
    assert record["detail"] == "no waking event before timeout"
    assert "rearm" not in record
    assert "rearm_error" not in record
    assert wake.persistent_backup_start_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    ) not in forwarded.out


def test_unsupervised_json_exit_keeps_direct_rearm_plan(
    isolated: tuple[Path, journal.LeaseIdentity],
) -> None:
    project, lease = isolated
    authority = journal.Journal(project)
    assert authority.prepare_attempt("unsupervised-json-listener-exit").committed
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=360,
    )
    command_parts = shlex.split(
        wake.persistent_backup_start_command(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
    )
    command_parts[0] = sys.executable
    command_parts.extend(["--timeout-s", "0.15", "--poll-secs", "0.02", "--json"])
    command = shlex.join(command_parts)
    env = dict(os.environ)
    env.pop("GOALFLIGHT_SUPERVISED", None)
    with wake.register_watchdog_waiter(
        project,
        controller_label=lease.label,
        generation_key=lease.nonce,
    ):
        completed = subprocess.run(
            command_parts,
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
        )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert payload["reason"] == "timeout"
    assert payload["rearm"]["command"] == command
    assert wake.SEPARATE_TRACKED_ARM_RULE in payload["rearm"]["hint"]


@pytest.mark.parametrize("event_type", ["listener-dead", "watchdog-dead"])
def test_supervisor_forwarded_dead_event_keeps_reason_not_rearm_command(
    isolated: tuple[Path, journal.LeaseIdentity],
    capsys: pytest.CaptureFixture[str],
    event_type: str,
) -> None:
    project, lease = isolated
    if event_type == "listener-dead":
        wake.activate_monitor_state(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            heartbeat_s=0.05,
            dead_after_s=0.15,
            now_epoch=time.time() - 1,
        )
        wake.record_monitor_fault(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            reason="test-follow-fault",
        )
        command = (
            f"{wake.follow_watchdog_start_command(project, controller_label=lease.label, lease_nonce=lease.nonce)} "
            "--timeout-s 2 --poll-secs 0.02 --listener-slots 2 --report-pending"
        )
        forbidden = wake.follow_start_command(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
        forbidden_stderr = (
            forbidden,
            wake.persistent_backup_start_command(
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
            ),
        )
        kind = "watchdog"
    else:
        wake.activate_monitor_state(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            heartbeat_s=120,
            dead_after_s=360,
        )
        command = (
            f"{wake.persistent_backup_start_command(project, controller_label=lease.label, lease_nonce=lease.nonce)} "
            "--timeout-s 2 --poll-secs 0.02"
        )
        forbidden = wake.follow_watchdog_start_command(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
        forbidden_stderr = (forbidden,)
        kind = "backup"
    monitor_waiter = None
    if event_type == "watchdog-dead":
        monitor_waiter = wake.register_waiter(
            project,
            controller_label=lease.label,
            kind=wake.MONITOR_KIND,
            generation_key=lease.nonce,
        )
    try:
        records, child_exit = _run_supervised_child(
            project,
            lease,
            kind=kind,
            command=command,
        )
    finally:
        if monitor_waiter is not None:
            monitor_waiter.close()
    forwarded_stderr = capsys.readouterr().err
    matches = [
        row
        for row in records
        if isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == event_type
    ]
    assert matches, {
        "records": records,
        "returncode": child_exit.returncode,
        "output": child_exit.output,
        "stderr": forwarded_stderr,
    }
    record = matches[0]
    payload = record["payload"]
    assert isinstance(payload, dict)
    assert child_exit.returncode == 0
    assert payload["reason"]
    assert "rearm_command" not in payload
    assert forbidden not in json.dumps(record)
    for action_command in forbidden_stderr:
        assert action_command not in forwarded_stderr
    if event_type == "listener-dead":
        assert "supervisor owns backup replacement" in forwarded_stderr


@pytest.mark.parametrize(
    ("builder", "status"),
    [
        (
            messages._follow_dead_record,
            {"state": "stale", "age_s": 2.0, "dead_after_s": 1.0},
        ),
        (
            messages._watchdog_dead_record,
            {
                "live_waiters": 1,
                "target_waiters": 8,
                "missing_components": ["watchdog"],
            },
        ),
    ],
)
def test_unsupervised_dead_events_keep_direct_rearm_command(
    monkeypatch: pytest.MonkeyPatch,
    builder,
    status: dict[str, object],
) -> None:
    monkeypatch.delenv("GOALFLIGHT_SUPERVISED", raising=False)
    record = builder(status, rearm_command="EXACT UNSUPERVISED COMMAND")
    assert record["payload"]["rearm_command"] == "EXACT UNSUPERVISED COMMAND"


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
    lines = doctor.collect_human_lines(
        _minimal_doctor_payload(result)  # type: ignore[arg-type]
    )
    line = next(line for line in lines if "wake coverage hint-ctl" in line)
    assert "wake coverage hint-ctl" in line
    assert "supervisor=running" in line
    assert "Restart the supervisor" in line
    assert supervise_cmd in line
    parsed = doctor.parse_status_line(line)
    assert "Restart the supervisor" in parsed["detail"]
